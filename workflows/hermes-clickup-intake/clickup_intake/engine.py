"""S2 workflow engine (plan r5 §4.4-§8.7, §10).

``evaluate(store, now, obs, config)`` recomputes every decision from ``state.sqlite3`` plus
this scan's observations. It commits glue transitions (phase, relays, classifications,
approval lifecycle, ticket issue compare-and-set) and enqueues each external effect as an
outbox row in the SAME transaction, so a crash never loses a consumed input. It returns
the newly queued intents; the S4 executor sends pending rows in order after rechecking
``rules.preconditions_ok`` (a failed recheck is ``cancelled_stale``, dependents too).

Effect kinds: cu_comment, cu_reply, cu_status, cu_attach (ClickUp, target = task id);
kb_create, kb_comment, kb_attach, kb_subscribe, kb_unblock, kb_complete, kb_archive
(Kanban, payload ``card``); alert (stdout, deduped by idem_key).
"""

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field

from . import kanban_port as kp
from . import render, rules
from .config import Config
from .contracts import BlockerPreconditions, EffectIntent, Preconditions
from .store import revoke_live_tickets

ACTIVE = ("held", "recorded", "staged", "released", "started", "implemented", "ticketed")
REISSUE_REASONS = ("new_input", "hold", "operator")
EARLY_PHASES = ("planning", "awaiting_business", "awaiting_technical")
APPROVAL_PHASES = ("awaiting_approval", "approval_queued", "development", "implemented", "promotion_window",
                   "promotion_held", "ready_for_deployment")
AMENDMENT_PHASES = ("development", "implemented", "promotion_window", "promotion_held", "ready_for_deployment")
REQUEST_SKILLS = ("plan", "pragmatic-code")
FAILED = ("cancelled_stale", "failed", "held_ambiguous")

NOTICE_UNKNOWN = "Not accepted: post the command as a new plain-text comment without quotes or formatting"
NOTICE_EDITED = "Not accepted: this comment was edited; post the command as a NEW plain comment"
NOTICE_STALE = "Not accepted: r%d is not the current published plan; the current plan is r%d"
NOTICE_VAGUE = "Not accepted: to approve, post a NEW plain comment with exactly: Approve plan r%d for development"
NOTICE_REVISING = "Approval of r%d is no longer valid; plan being revised"
NOTICE_HELD = "Promotion of r%d to dev is held: new input is being reviewed"
NOTICE_SCOPE = "Promotion of r%d held: scope change; revised plan required"
REVIEW_NOTE = "r%d passed review but is unpublished; classify"


@dataclass(frozen=True)
class Observation:
    """This scan's observations, supplied by S4 (none of it is persisted content).

    comments: (task, kind, id) -> (comment parts, comment_text) for comments/replies fetched this scan;
    cards: card id -> ``show --json`` dict for glue-owned cards and ``clickup_gates`` ids (§4.1 step 5);
    attachments: card id -> [{"name", "sha256"}] from ``attachments --json`` + local SHA-256;
    confirmed: task ids whose GET Task this scan confirmed the home list (write scope, §9).
    """
    scan_started_ms: int = 0
    comments: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)
    attachments: dict = field(default_factory=dict)
    confirmed: frozenset = frozenset()


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _in(names):
    return ", ".join("'%s'" % n for n in names)


def _or(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


class _Ctx:
    def __init__(self, store, now, obs, cfg):
        self.store, self.c, self.now, self.obs, self.cfg = store, store.conn, now, obs, cfg
        self.new = []

    def task(self, tid):
        r = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def approval(self, aid):
        return dict(self.c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone())

    def state(self, key):
        r = self.c.execute("SELECT state FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return r[0] if r else None

    def ref(self, key):
        r = self.c.execute("SELECT state, result_ref FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return r[1] if r and r[0] == "done" and r[1] else None

    def show(self, card):
        return self.obs.cards.get(card) if card else None

    def barrier(self, t, anchor_ms=0):
        return rules.barrier_ok(t, anchor_ms, self.now, self.obs.scan_started_ms)

    def item(self, tid, kind, iid):
        r = self.c.execute("SELECT * FROM items WHERE task=? AND kind=? AND id=?", (tid, kind, iid)).fetchone()
        return dict(r) if r else None

    def put(self, key, kind, target, payload, pre, depends=(), rearm=False):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        vals = (_sha(body), body, json.dumps(dataclasses.asdict(pre)), json.dumps(list(depends)))
        cur = self.c.execute(
            "INSERT OR IGNORE INTO outbox(idem_key, task, kind, target, payload_sha256, body, preconditions,"
            " depends_on) VALUES (?,?,?,?,?,?,?,?)", (key, pre.task_id, kind, target) + vals)
        if cur.rowcount == 0 and rearm:  # a wake cancelled at send time was never sent: plan it again
            cur = self.c.execute(  # ... unless the operator abandoned it (``outbox abandon``; resend is explicit)
                "UPDATE outbox SET payload_sha256=?, body=?, preconditions=?, depends_on=?, state='pending',"
                " outcome=NULL, attempts=0, next_attempt_ms=NULL WHERE idem_key=? AND state='cancelled_stale'"
                " AND outcome IS NOT 'ABANDONED'", vals + (key,))
        if cur.rowcount == 1:
            self.new.append(EffectIntent(key, kind, target, payload, pre, tuple(depends)))

    def cu(self, tid, key, kind, payload, depends=(), anchor=None, **pre):
        payload = dict(payload, task=tid, ref="cu-" + _sha(key)[:12])
        if anchor is not None:  # quiet-expired row: rules.send_barrier_ok at every send/retry (§4.4)
            payload["barrier_anchor_ms"] = anchor
        cls = BlockerPreconditions if "blocker_id" in pre else Preconditions  # legacy rows keep 9-key JSON
        self.put(key, kind, tid, payload, cls(task_id=tid, list_id=self.cfg.list_id, **pre), depends)

    def comment(self, tid, key, text, depends=(), **pre):
        self.cu(tid, key, "cu_comment", {"text": text}, depends, **pre)

    def reply(self, tid, key, root, text, **pre):
        self.cu(tid, key, "cu_reply", {"root": root, "text": text}, (), **pre)

    def set_status(self, tid, key, status, depends=(), anchor=None, **pre):
        if status == "deployed" or status not in {n for n, _ in self.cfg.expected_statuses}:
            raise ValueError("status %r is never written" % status)
        self.cu(tid, key, "cu_status", {"status": status}, depends, anchor, **pre)

    def kb(self, tid, key, kind, card, payload=None, depends=(), rearm=False, anchor=None, **pre):
        payload = dict(payload or {}, card=card)
        if anchor is not None:
            payload["barrier_anchor_ms"] = anchor
        cls = BlockerPreconditions if "blocker_id" in pre else Preconditions  # legacy rows keep 9-key JSON
        self.put(key, kind, card, payload, cls(task_id=tid, **pre), depends, rearm)

    def wake(self, tid, key, kind, card, payload=None, depends=(), **pre):
        """Request create / relay rows: no-ops once the task is terminal or not_admitted (§4.2-§4.4, §10.1);
        quiet-expired pickup/relay, so each send and retry also waits for the current barrier (§4.4)."""
        self.kb(tid, key, kind, card, payload, depends, True, 0, phases=rules.LIVE_PHASES, **pre)

    def alert(self, key, text, tid):
        self.put("clickup-dp:alert:" + key, "alert", "operator", {"text": text}, Preconditions(task_id=tid))

    def set_phase(self, tid, phase):
        self.c.execute("UPDATE tasks SET phase=? WHERE clickup_task_id=?", (phase, tid))


def evaluate(store, now, obs=None, config=None):
    """Engine evaluation (§4.1 step 6): returns the EffectIntents newly queued in the outbox."""
    ctx = _Ctx(store, now, obs or Observation(), config or Config())
    tids = [r[0] for r in store.conn.execute("SELECT clickup_task_id FROM tasks ORDER BY clickup_task_id")]
    for tid in tids:
        if ctx.task(tid)["phase"] in (None, "baseline", "not_admitted"):
            continue  # never read, never acted on (§4.3, §6)
        _candidacy(ctx, tid)
        _revocations(ctx, tid)
        t = ctx.task(tid)
        if not t["hold_reason"]:
            _disabled_blockers(ctx, tid)
        if t["terminal"]:
            _terminal(ctx, tid)
            _closed_task_blockers(ctx, tid)
            if not t["hold_reason"]:
                _fences(ctx, tid)  # r12 §3.3 parks and fence/park readback need no live task (r11 §9, r12 §9)
            continue
        if t["hold_reason"] or tid not in ctx.obs.confirmed:
            continue  # owner V1 (the approved temporary-hold rule): a temporary hold pauses its blockers and fences, never final
        _outputs(ctx, tid)
        _deliver(ctx, tid)
        _fences(ctx, tid)
        _commands(ctx, tid)
        _requests(ctx, tid)
        _approvals(ctx, tid)
        _detect(ctx, tid)
    return ctx.new


# ---- question relay detection (blocker plan r9 §4.2, r11 §3.2, r12 §3.2) ----------------

BLOCKER_FINAL = ("internal", "resumed", "resumed_unconfirmed", "released_by_owner", "scope_change", "superseded",
                 "terminal")
FENCE_FINAL = ("released", "refused", "owner_released", "abandoned")
WATCHED_STATES = ("released", "started", "implemented", "ticketed", "promoted")
CUTOVER_KEY = "blocker_cutover_ms"


def _cutover_ms(ctx):
    r = ctx.c.execute("SELECT value FROM meta WHERE key=?", (CUTOVER_KEY,)).fetchone()
    return json.loads(r[0]) if r else None


def _watched(ctx, tid):
    """{card: approval} for the task's watched approvals (blocker plan r6 §4.2): the orchestrator, final_e2e,
    promotion, cleanup and lineage cards; never the gate card."""
    out = {}
    for a in map(dict, ctx.c.execute("SELECT * FROM approvals WHERE task=? ORDER BY id", (tid,))):
        if a["state"] not in WATCHED_STATES and (a["state"] != "revoked" or a["revoked_reason"] == "superseded"):
            continue
        g = _gates(a) or {}
        for card in [a["orchestrator_card"], g.get("final_e2e"), g.get("promotion"), g.get("cleanup")] \
                + list(g.get("lineage") or []):
            if isinstance(card, str) and card and card != a["gate_card"]:
                out.setdefault(card, a)
    return out


def _detect(ctx, tid):
    """Record protocol question, fence and park parents of watched cards (r9 §4.2, r11 §3.2, r12 §3.2)."""
    cut = _cutover_ms(ctx)
    if cut is None:
        return  # feature off: no detection
    for w, a in _watched(ctx, tid).items():
        ws = ctx.show(w)
        for p in (ws or {}).get("parents") or []:
            if not isinstance(p, str) or ctx.c.execute(
                    "SELECT 1 FROM blockers WHERE question_card=? UNION SELECT 1 FROM fences WHERE fence_card=?",
                    (p, p)).fetchone():
                continue
            ps = ctx.show(p)
            kind, verdict = kp.protocol_parent(w, ws, ps, cut) if ps else (None, None)
            if kind is None or verdict == "wait" or verdict.startswith("ignored:"):
                continue
            with ctx.store.transaction() as c:
                if kind == "question" and verdict == "record":
                    t = ctx.task(tid)
                    runs = [r.get("id") for r in ws.get("runs") or [] if isinstance(r, dict) and isinstance(r.get("id"), int)]
                    run = max(runs) if runs else None
                    lane = next(("review" if (e.get("payload") or {}).get("source_status") == "review" else "work"
                                 for e in reversed(ws.get("events") or []) if isinstance(e, dict)
                                 and e.get("kind") == "dependency_wait" and e.get("run_id") == run), "work")
                    c.execute("INSERT INTO blockers(task, question_card, card, run_id, created_ms, body_sha256,"
                              " approval_id, phase_at_detect, state, origin_lane, updated_ms)"
                              " VALUES (?,?,?,?,?,?,?,?,'detected',?,?)",
                              (tid, p, w, run, ctx.now, _sha(ps["task"].get("body") or ""), a["id"], t["phase"], lane, ctx.now))
                elif kind == "question":
                    ctx.alert("blocker:%s:%s" % (p, verdict), "CLICKUP-QUESTION card %s of %s was not recorded (%s)" % (p, w, verdict[6:]), tid)
                else:
                    created = ps["task"].get("created_at")
                    ok = verdict == "record"
                    c.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, anchor_ms, state, updated_ms)"
                              " VALUES (?,?,?,?,?,?,?,?)", (tid, w, p, kind, created,
                              (created + 1) * 1000 if ok and kind == "fence" else None, "open" if ok else "refused", ctx.now))
                    if not ok:
                        ctx.alert("%s:%s:refused" % (kind, p), "CLICKUP-%s card %s of %s refused (%s); its children: %s" % (kind.upper(), p, w, verdict[8:], ", ".join(map(str, ps.get("children") or [])) or "none"), tid)


# ---- commands (§7) -------------------------------------------------------------------------

def _candidacy(ctx, tid):
    """Record each fetched comment's command candidacy once; content itself is never stored."""
    for (task, kind, iid), (parts, text) in sorted(ctx.obs.comments.items()):
        if task != tid or kind not in ("comment", "reply"):
            continue
        it = ctx.item(tid, kind, iid)
        if it is None or it["command_result"] is not None:
            continue
        if it["author_id"] == ctx.cfg.bot_user_id:
            res = "bot"
        else:
            k, n, p = rules.command_of(parts, text, ctx.cfg.plain_attr_allowlist)
            res = "none" if k is None else "cand:%s%s" % (k, "" if n is None else ":%d" % n)
            if k == "unknown":
                ctx.alert("shape:" + p.shape_sig, "ClickUp schema drift: new UNKNOWN comment shape %s (task %s)"
                          % (p.shape_sig[:12], tid), tid)
        ctx.c.execute("UPDATE items SET command_result=? WHERE task=? AND kind=? AND id=?", (res, tid, kind, iid))


def _commands(ctx, tid):
    if not ctx.barrier(ctx.task(tid)):
        return
    for it in ctx.c.execute("SELECT * FROM items WHERE task=? AND command_result LIKE 'cand:%'"
                            " ORDER BY date_ms, first_seen_ms, id", (tid,)).fetchall():
        _command(ctx, tid, dict(it))


def _root(ctx, tid, cid):
    """Thread root of an observed comment/reply of THIS task, else None: unknown or foreign ids are never a
    reply destination (§7.1, §8.3, §9)."""
    r = ctx.c.execute("SELECT kind, parent_id FROM items WHERE task=? AND id=? AND kind IN ('comment', 'reply')",
                      (tid, cid)).fetchone()
    root = (r[1] if r[0] == "reply" else cid) if r else None
    return root if root and ctx.item(tid, "comment", root) else None


def _posted(ctx, tid, pattern):
    """The bot's own observed comment for the latest done cu_comment whose key matches ``pattern``."""
    r = ctx.c.execute("SELECT result_ref FROM outbox WHERE task=? AND kind='cu_comment' AND state='done'"
                      " AND idem_key LIKE ? ORDER BY rowid DESC LIMIT 1", (tid, pattern)).fetchone()
    return ctx.item(tid, "comment", r[0]) if r and r[0] else None


def _command(ctx, tid, it):
    _, kind, *rest = it["command_result"].split(":")
    rev = int(rest[0]) if rest else None
    t, cfg = ctx.task(tid), ctx.cfg
    root = it["parent_id"] if it["kind"] == "reply" and it["parent_id"] else it["id"]

    def finish(result, notice=None):
        with ctx.store.transaction() as c:
            c.execute("UPDATE items SET command_result=? WHERE task=? AND kind=? AND id=?",
                      (result, tid, it["kind"], it["id"]))
            if notice:
                ctx.reply(tid, "clickup-dp:%s:notice:%s:%s" % (tid, it["kind"], it["id"]), root, notice)

    if kind == "quoted":
        return finish("context")
    if kind == "unknown":
        return finish("rejected:unknown", NOTICE_UNKNOWN)
    edited = it["cur_hash"] != it["first_hash"]
    names = list(cfg.approvers.values())

    if kind in ("close_business", "close_technical"):
        stage = kind[len("close_"):]
        if t["phase"] != "awaiting_" + stage:
            return _close_blockers(ctx, tid, it, kind, stage, edited, finish)
        base = _posted(ctx, tid, "clickup-dp:%s:q:%s:%%" % (tid, stage))
        if base is None:
            return  # question post not observed yet
        if not rules.is_new(it, base):
            return finish("context")
        who = rules.may(kind, it["author_id"], t["requester_id"], cfg.approvers, cfg.bot_user_id)
        if who == "ignore":
            return finish("ignored")
        if edited:
            return finish("rejected:edited", NOTICE_EDITED)
        if who == "notice":
            allowed = (["the requester"] if stage == "business" else []) + names
            return finish("rejected:author", "Not accepted: only %s can close %s requirements." % (_or(allowed), stage))
        with ctx.store.transaction() as c:  # a valid closure is a material-candidate input (§5.2)
            gen = c.execute("SELECT input_gen FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()[0] + 1
            c.execute("UPDATE tasks SET input_gen=? WHERE clickup_task_id=?", (gen, tid))
            c.execute("UPDATE items SET gen=?, command_result=? WHERE task=? AND kind=? AND id=?",
                      (gen, "closure_" + stage, tid, it["kind"], it["id"]))
            revoke_live_tickets(c, tid, "new_input", ctx.now)
        return

    # approve / vague: NEW unedited top-level comment after publication, in an approval phase
    n = t["published_rev"]
    if it["kind"] != "comment" or t["phase"] not in APPROVAL_PHASES or n is None:
        return finish("context")
    base = _posted(ctx, tid, "clickup-dp:%s:pub:r%d:%s:%%:comment" % (tid, n, (t["published_sha256"] or "")[:12]))
    if base is None:
        return
    if not rules.is_new(it, base):
        return finish("context")
    who = rules.may("approve", it["author_id"], t["requester_id"], cfg.approvers, cfg.bot_user_id)
    if who == "ignore":
        return finish("ignored")
    if edited:
        return finish("rejected:edited", NOTICE_EDITED)
    ag = t["published_accounts_gen"]
    dup = ctx.c.execute("SELECT author_name FROM approvals WHERE task=? AND rev=? AND accounts_gen=?",
                        (tid, n, ag)).fetchone()
    if kind == "approve" and rev == n and dup:
        return finish("duplicate", "Already approved by %s." % dup[0])
    if who == "notice":
        return finish("rejected:author", "Not accepted: only %s can approve development." % _or(names))
    if kind == "vague":
        return finish("rejected:vague", NOTICE_VAGUE % n)
    if rev != n or t["scope_gen"] > ag:
        return finish("rejected:stale", NOTICE_STALE % (rev, n))
    state = "held" if t["classified_gen"] != t["input_gen"] else "recorded"
    with ctx.store.transaction() as c:
        aid = c.execute(
            "INSERT INTO approvals(task, rev, accounts_gen, comment_id, author_id, author_name, comment_date,"
            " text_hash, plan_sha256, state) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, n, ag, it["id"], it["author_id"], cfg.approvers[it["author_id"]], it["date_ms"], it["cur_hash"],
             t["published_sha256"], state)).lastrowid
        c.execute("UPDATE approvals SET state='revoked', revoked_reason='superseded', revoked_ms=?"
                  " WHERE task=? AND rev<? AND state IN (%s)" % ",".join("?" * len(ACTIVE)),
                  (ctx.now, tid, n) + ACTIVE)
        c.execute("UPDATE items SET command_result=? WHERE task=? AND kind=? AND id=?",
                  ("approved:%d" % aid, tid, it["kind"], it["id"]))


def _close_blockers(ctx, tid, it, kind, stage, edited, finish):
    """r6 §4.5: outside ``awaiting_<stage>`` a closure applies to the task's ``asked`` blockers of that stage whose
    question post is observed, with the unchanged NEW/author/edited checks and notices. A valid closure bumps
    input_gen as today and closes every such blocker whose question precedes it."""
    t, cfg = ctx.task(tid), ctx.cfg
    asked = [dict(b) for b in ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state='asked' AND stage=?"
                                            " ORDER BY id", (tid, stage))] if _cutover_ms(ctx) is not None else []
    if not asked:
        return finish("context")
    posts = [(b, ctx.item(tid, "comment", ctx.ref(b["question_key"]))) for b in asked]
    if not any(p for _, p in posts):
        return  # question post not observed yet
    closing = [b for b, p in posts if p and rules.is_new(it, p)]
    if not closing:
        return finish("context")
    who = rules.may(kind, it["author_id"], t["requester_id"], cfg.approvers, cfg.bot_user_id)
    if who == "ignore":
        return finish("ignored")
    if edited:
        return finish("rejected:edited", NOTICE_EDITED)
    if who == "notice":
        allowed = (["the requester"] if stage == "business" else []) + list(cfg.approvers.values())
        return finish("rejected:author", "Not accepted: only %s can close %s requirements." % (_or(allowed), stage))
    with ctx.store.transaction() as c:
        gen = c.execute("SELECT input_gen FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()[0] + 1
        c.execute("UPDATE tasks SET input_gen=? WHERE clickup_task_id=?", (gen, tid))
        c.execute("UPDATE items SET gen=?, command_result=? WHERE task=? AND kind=? AND id=?",
                  (gen, "closure_" + stage, tid, it["kind"], it["id"]))
        revoke_live_tickets(c, tid, "new_input", ctx.now)
        for b in closing:
            c.execute("UPDATE blockers SET state='closed', closure_item=?, closure_gen=?, updated_ms=? WHERE id=?",
                      (it["id"], gen, ctx.now, b["id"]))


# ---- planner outputs (§8.1, §5.3) ----------------------------------------------------------

def _outputs(ctx, tid):
    for card, in ctx.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq",
                               (tid,)).fetchall():
        for cm in (ctx.show(card) or {}).get("comments") or []:
            if not isinstance(cm, dict) or cm.get("author") != "planner":
                continue
            parsed = rules.parse_out(cm.get("body"))
            if parsed is None:
                continue
            key = _sha(json.dumps([card, cm.get("author"), cm.get("created_at"), cm.get("body")]))
            if ctx.c.execute("SELECT 1 FROM kanban_seen WHERE key=?", (key,)).fetchone():
                continue
            _consume(ctx, tid, card, key, *parsed)


def _consume(ctx, tid, card, key, hdr, text):
    kind, h12 = hdr["kind"], key[:12]
    with ctx.store.transaction() as c:
        c.execute("INSERT INTO kanban_seen(key, card, consumed_ms) VALUES (?,?,?)", (key, card, ctx.now))
        if kind == "blocker_internal":
            for b in _relayed_blockers(ctx, tid, card, h12, hdr):
                c.execute("UPDATE blockers SET state='internal', updated_ms=? WHERE id=?", (ctx.now, b["id"]))
                ctx.alert("blocker:%d:internal" % b["id"], "question card %s for %s was classified internal; resolve"
                          " within existing authority by completing %s with the guidance"
                          % (b["question_card"], b["card"], b["question_card"]), tid)
            return
        if kind == "classification":
            rel = c.execute("SELECT * FROM relays WHERE relay_ref=? AND task=? AND card=?",
                            (hdr["relay"], tid, card)).fetchone()
            if rel is None or rel["gen"] != hdr["gen"] or rel["state"] == "staged":
                ctx.alert("classification:" + h12, "ignored classification on card %s: unknown relay or gen" % card,
                          tid)
                return
            g = hdr["gen"]
            c.execute("UPDATE relays SET state='classified', classification=? WHERE relay_ref=?",
                      (hdr["result"], hdr["relay"]))
            c.execute("UPDATE tasks SET classified_gen=MAX(classified_gen, ?) WHERE clickup_task_id=?", (g, tid))
            if hdr["result"] == "material":
                c.execute("UPDATE tasks SET scope_gen=MAX(scope_gen, ?) WHERE clickup_task_id=?", (g, tid))
                revoke_live_tickets(c, tid, "material", ctx.now)
            _published_after_classification(ctx, tid)
            return
        if kind == "resolution":
            return _resolution(ctx, tid, card, h12, hdr, text)
        if not text:
            return
        out = "clickup-dp:%s:out:%s" % (tid, h12)
        if kind in ("business_questions", "technical_questions") and "blocker" in hdr:
            return _ask(ctx, tid, card, h12, hdr, kind.split("_")[0], text)
        if kind in ("business_questions", "technical_questions"):
            stage = kind.split("_")[0]
            qk = "clickup-dp:%s:q:%s:%s" % (tid, stage, h12)
            ctx.comment(tid, qk, text + "\n\n" + _footer(ctx.cfg, stage))
            if ctx.task(tid)["phase"] not in AMENDMENT_PHASES + ("approval_queued",):
                ctx.set_status(tid, qk + ":status", stage + " requirements", depends=(qk,),
                               phases=("awaiting_" + stage,))
                ctx.set_phase(tid, "awaiting_" + stage)
        elif kind == "answer" and hdr.get("reply_to"):
            root = _root(ctx, tid, hdr["reply_to"])
            if root is None:
                ctx.alert("reply_to:" + h12, "answer on card %s names a reply_to that is not an observed comment of"
                          " task %s; not posted" % (card, tid), tid)
            else:
                ctx.reply(tid, out, root, text)
        else:
            ctx.comment(tid, out, text)


RESOLUTION_MAX = 1600  # r9 §4.3: fits the native parent-result and rendered comment caps


def _resolution(ctx, tid, card, h12, hdr, text):
    """r6 §4.5 (r9 §4.3/§4.5), inside the consuming transaction: accept a resolution only when it is bound to the
    relay and generation that carried the blocker; anything else is ignored with one alert."""
    rel = ctx.c.execute("SELECT * FROM relays WHERE relay_ref=? AND task=? AND card=?",
                        (hdr["relay"], tid, card)).fetchone()
    ids = _relayed_blocker_ids(hdr)
    b = len(ids) == 1 and ctx.c.execute("SELECT * FROM blockers WHERE id=? AND task=?", (ids[0], tid)).fetchone()
    why = None
    if rel is None or rel["gen"] != hdr["gen"] or rel["state"] == "staged":
        why = "unknown relay or gen"
    elif not b or b["state"] not in POST_CLOSURE or b["pending_relay"] != hdr["relay"]:
        why = "blocker %s is unknown, foreign, final or not carried by relay %s" % (hdr.get("blocker"), hdr["relay"])
    elif hdr["gen"] <= (b["resolution_gen"] or 0):
        why = "duplicate or older generation"
    elif hdr.get("authority") != "unchanged":
        why = "authority=unchanged is missing"
    elif not text or len(text) > RESOLUTION_MAX or rules.blocker_leak(text) or render.check_outbound(text):
        why = "text is empty, over %d characters or failed the leak guard" % RESOLUTION_MAX
    if why:
        ctx.alert("resolution:" + h12, "ignored resolution on card %s: %s" % (card, why), tid)
        return
    ctx.c.execute("UPDATE blockers SET resolution_relay=?, resolution_card=?, resolution_gen=?, resolution_body=?,"
                  " resolution_authority='unchanged', updated_ms=? WHERE id=?",
                  (hdr["relay"], card, hdr["gen"], text, ctx.now, b["id"]))
    _derive(ctx, b["id"])


RESTORE_STATUS = {"development": "implementing", "implemented": "implemented", "promotion_window": "implemented",
                  "promotion_held": "implemented", "ready_for_deployment": "ready for deployment",
                  "approval_queued": "waiting for approval"}  # blocker plan r6 §4.4


def _ask(ctx, tid, card, h12, hdr, stage, text):
    """r6 §4.4 ask, inside the consuming transaction: each relayed blocker named by a question output becomes
    ``asked`` with its own ClickUp question (plus the stage's closure footer) and stage status; tasks.phase is
    unchanged. Leaking or oversized text is not posted: the blockers stay ``relayed`` with one alert (r6 §4.3)."""
    blockers = _question_blockers(ctx, tid, card, h12, hdr)
    if not blockers:
        return
    leak = rules.blocker_leak(text) or render.check_outbound(text)
    if leak:
        for b in blockers:
            ctx.alert("blocker:%d:leak:%s" % (b["id"], h12), "question for blocker %d not posted: the planner text on"
                      " card %s failed the leak guard (%s); the blocker stays %s" % (b["id"], card, leak, b["state"]), tid)
        return
    boundary = 0 if stage == "business" or hdr.get("boundary") == "none" else 1  # missing/invalid => security
    body = text + "\n\n" + _footer(ctx.cfg, stage)
    for b in blockers:
        prev = b["question_key"] and ctx.c.execute("SELECT body FROM outbox WHERE idem_key=?",
                                                   (b["question_key"],)).fetchone()
        if prev and json.loads(prev[0]).get("text") == body:
            ctx.alert("blocker:%d:repeat:%s" % (b["id"], h12), "question for blocker %d on card %s not repeated: it"
                      " is unchanged from the question already asked" % (b["id"], card), tid)
            continue
        qk = "clickup-dp:%s:bq:%s:%d:%s" % (tid, stage, b["id"], h12)
        ctx.c.execute("UPDATE blockers SET state='asked', stage=?, boundary=?, question_key=?, restore_status=?,"
                      " updated_ms=? WHERE id=?",
                      (stage, boundary, qk, RESTORE_STATUS.get(b["phase_at_detect"]), ctx.now, b["id"]))
        ctx.comment(tid, qk, body, blocker_id=b["id"])
        ctx.set_status(tid, qk + ":status", stage + " requirements", depends=(qk,), blocker_id=b["id"],
                       phases=(b["phase_at_detect"],))


def _question_blockers(ctx, tid, card, h12, hdr):
    """Blockers a question output may ask: relayed by this triage relay (r6 §4.3), or post-closure blockers carried
    by this classified input relay when the answer was insufficient (r6 §4.5). Others alert and are ignored."""
    rel = ctx.c.execute("SELECT 1 FROM relays WHERE relay_ref=? AND task=? AND card=? AND state<>'staged'",
                        (hdr["relay"], tid, card)).fetchone()
    if not rel:
        return _relayed_blockers(ctx, tid, card, h12, hdr)
    out = []
    for i in _relayed_blocker_ids(hdr) or [None]:
        b = i is not None and ctx.c.execute("SELECT * FROM blockers WHERE id=? AND task=? AND pending_relay=? AND"
                                            " state IN (%s)" % _in(POST_CLOSURE), (i, tid, hdr["relay"])).fetchone()
        if b:
            out.append(dict(b))
        else:
            ctx.alert("blocker-out:%s:%s" % (h12, i), "ignored blocker output on card %s: blocker %s is unknown,"
                      " foreign or final for relay %s" % (card, i, hdr["relay"]), tid)
    return out


def _relayed_blocker_ids(hdr):
    try:
        return [int(x) for x in hdr.get("blocker", "").split(",")]
    except ValueError:
        return []


def _relayed_blockers(ctx, tid, card, h12, hdr):
    """Blocker rows a triage output names (blocker plan r6 §4.3): relayed by this relay to this request card.
    An unknown, foreign or final id raises one alert and is ignored."""
    ids = _relayed_blocker_ids(hdr)
    out = []
    for i in ids or [None]:
        b = i is not None and ctx.c.execute(
            "SELECT * FROM blockers WHERE id=? AND task=? AND state='relayed' AND relay_ref=? AND request_card=?",
            (i, tid, hdr["relay"], card)).fetchone()
        if b:
            out.append(dict(b))
        else:
            ctx.alert("blocker-out:%s:%s" % (h12, i), "ignored blocker output on card %s: blocker %s is unknown,"
                      " foreign or final for relay %s" % (card, i, hdr["relay"]), tid)
    return out


def _published_after_classification(ctx, tid):
    """A question exchange after publication never strands an unreleased rN (§5.3, §6, §8.4):
    ``material`` -> revising + notice; ``answer_only`` with current(rN) -> approvable again."""
    t = ctx.task(tid)
    n = t["published_rev"]
    if n is None or t["phase"] not in ("awaiting_approval", "awaiting_business", "awaiting_technical"):
        return
    if t["scope_gen"] > (t["published_accounts_gen"] or 0):
        if ctx.state("clickup-dp:%s:notice:revising:r%d" % (tid, n)) is None:
            _revise(ctx, tid, n)
    elif t["phase"] != "awaiting_approval" and rules.current(t["published_accounts_gen"], t):
        ctx.set_phase(tid, "awaiting_approval")
        ctx.set_status(tid, "clickup-dp:%s:status:approvable:r%d:g%d" % (tid, n, t["classified_gen"]),
                       "waiting for approval", phases=("awaiting_approval",))


def _footer(cfg, stage):
    names = list(cfg.approvers.values())
    who = _or((["the requester"] if stage == "business" else []) + names)
    return ("Add your answers as comments. When ready, add a NEW comment with the line: %s requirements closed\n"
            "(%s may post it. The phrase triggers reevaluation, not a bypass; quoted or formatted copies of the "
            "phrase do not count.)" % (stage.capitalize(), who[0].upper() + who[1:]))


# ---- request cards, relays, publication (§8.1-§8.4) ----------------------------------------

def _requests(ctx, tid):
    for key, card in ctx.c.execute("SELECT idem_key, result_ref FROM outbox WHERE task=? AND kind='kb_create'"
                                   " AND state='done' AND idem_key LIKE ?", (tid, "clickup-dp:%s:req:%%" % tid)).fetchall():
        if card:
            ctx.c.execute("INSERT OR IGNORE INTO cards(card_id, task, role, seq, created_key) VALUES (?,?,?,?,?)",
                          (card, tid, "request", int(key.rsplit(":", 1)[1]), key))
    _progress_relays(ctx, tid)
    _unclassified(ctx, tid)
    reqs = [dict(r) for r in ctx.c.execute("SELECT * FROM cards WHERE task=? AND role='request' ORDER BY seq", (tid,))]
    if reqs and ctx.task(tid)["phase"] in EARLY_PHASES and rules.status(ctx.show(reqs[-1]["card_id"])) == "review":
        ctx.set_phase(tid, "in_review")
    _publication(ctx, tid, reqs)
    _revising(ctx, tid)
    _relay_new(ctx, tid, reqs)
    _blocker_relay(ctx, tid, reqs)


def _progress_relays(ctx, tid):
    for rel in [dict(r) for r in ctx.c.execute("SELECT * FROM relays WHERE task=? AND state='staged'", (tid,))]:
        k = "clickup-dp:%s:relay:%s" % (tid, rel["relay_ref"])
        st = ctx.state(k + ":unblock")
        with ctx.store.transaction() as c:
            if st == "done":
                c.execute("UPDATE relays SET state='delivered' WHERE relay_ref=?", (rel["relay_ref"],))
                for kind, iid in json.loads(rel["item_ids"]):
                    c.execute("UPDATE items SET delivered_relay=? WHERE task=? AND kind=? AND id=?",
                              (rel["relay_ref"], tid, kind, iid))
                c.execute("UPDATE cards SET wait_run_id=NULL WHERE card_id=?", (rel["card"],))
                if ctx.task(tid)["phase"] == "new_quiet":  # pickup: the request itself defines the scope
                    c.execute("UPDATE tasks SET phase='planning', classified_gen=MAX(classified_gen, ?),"
                              " scope_gen=MAX(scope_gen, ?) WHERE clickup_task_id=?", (rel["gen"], rel["gen"], tid))
            elif st in FAILED:  # not delivered: inputs stay pending for a fresh relay
                c.execute("DELETE FROM relays WHERE relay_ref=?", (rel["relay_ref"],))


def _unclassified(ctx, tid):
    """§5.3/§11: a request card that parked after a delivered relay without classifying it -> one alert per
    relay. Approval and tickets stay suspended; no classification is inferred and nothing is woken."""
    t = ctx.task(tid)
    for ref, card, gen in ctx.c.execute("SELECT relay_ref, card, gen FROM relays WHERE task=? AND state='delivered'"
                                        " AND gen>?", (tid, t["classified_gen"])).fetchall():
        show, m = ctx.show(card), re.search(r"-w(\d+)$", ref)
        run = rules.latest_run(show)
        if rules.status(show) == "scheduled" and run and run.get("outcome") == "scheduled" \
                and run["id"] > (int(m.group(1)) if m else 0):
            ctx.alert("unclassified:" + ref, "request card %s parked without classifying relay %s (gen %d);"
                      " approval and tickets stay suspended" % (card, ref, gen), tid)


def _relay_new(ctx, tid, reqs):
    t = ctx.task(tid)
    if not ctx.barrier(t):
        return
    if _wake_in_flight(ctx, tid, ":blocker:"):
        return  # blocker plan r6 §4.3: one in-flight wake per task
    staged = ctx.c.execute("SELECT * FROM relays WHERE task=? AND state='staged'", (tid,)).fetchone()
    if staged:
        _rebatch(ctx, tid, dict(staged))
        return
    relayed = ctx.c.execute("SELECT MAX(gen) FROM relays WHERE task=?", (tid,)).fetchone()[0]
    if relayed is not None and t["input_gen"] <= relayed:
        return
    if not reqs:
        if t["phase"] == "new_quiet":
            _create_request(ctx, tid, 1, None)
        return
    last = reqs[-1]
    card = last["card_id"]
    show = ctx.show(card)
    st = rules.status(show)
    fresh = not (ctx.c.execute("SELECT 1 FROM relays WHERE card=?", (card,)).fetchone()
                 or _blocker_relayed(ctx, card))
    if fresh and st == "blocked" and not rules.runs(show):
        _relay(ctx, tid, last, "blocked", None, fresh)
    elif st == "scheduled" and not fresh:
        run = rules.latest_run(show)
        if run and run.get("outcome") == "scheduled":
            _relay(ctx, tid, last, "scheduled", run["id"], fresh)
    elif st == "done":
        _create_request(ctx, tid, last["seq"] + 1, card)
    elif st in ("blocked", "triage", "archived"):
        ctx.alert("held:%s:%s" % (card, st), "ClickUp input for task %s is held: request card %s is %s"
                  % (tid, card, st), tid)


def _wake_in_flight(ctx, tid, kind=None):
    """A non-final ``:relay:*:unblock`` or ``:blocker:*:unblock`` row of the task (blocker plan r6 §4.3)."""
    kinds = (kind,) if kind else (":relay:", ":blocker:")
    return any(ctx.c.execute(
        "SELECT 1 FROM outbox WHERE task=? AND kind='kb_unblock' AND idem_key LIKE ? AND idem_key LIKE '%%:unblock'"
        " AND state NOT IN (%s)" % _in(("done",) + FAILED), (tid, "clickup-dp:%s%s%%" % (tid, k))).fetchone()
        for k in kinds)


def _blocker_relayed(ctx, card):
    """A blocker relay was staged to this request card (blocker plan r6 §4.3 freshness)."""
    return _cutover_ms(ctx) is not None and ctx.c.execute(
        "SELECT 1 FROM blockers WHERE request_card=?", (card,)).fetchone() is not None


def _blocker_relay(ctx, tid, reqs):
    """Blocker plan r6 §4.3 (r9 §4.3): relay detected question cards to the planner request card for triage."""
    if _cutover_ms(ctx) is None:
        return
    for b in map(dict, ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state='relayed'", (tid,)).fetchall()):
        st = ctx.state("clickup-dp:%s:blocker:%s:unblock" % (tid, b["relay_ref"]))
        if st in FAILED:  # not delivered: stage again
            ctx.c.execute("UPDATE blockers SET state='detected', updated_ms=? WHERE id=?", (ctx.now, b["id"]))
            continue
        show = ctx.show(b["request_card"])  # r6 §4.3: the planner parked without answering this blocker
        run = rules.latest_run(show)
        if st == "done" and rules.status(show) == "scheduled" and run and run.get("outcome") == "scheduled" \
                and run["id"] > int(re.search(r"-w(\d+)$", b["relay_ref"]).group(1)):
            ctx.alert("blocker:%d:unanswered:%s" % (b["id"], b["relay_ref"]), "request card %s parked without"
                      " answering blocker %d (question card %s for %s, relay %s); nothing was asked on ClickUp and %s"
                      " keeps waiting" % (b["request_card"], b["id"], b["question_card"], b["card"], b["relay_ref"],
                                          b["card"]), tid)
    bl = [dict(r) for r in ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state='detected' ORDER BY id",
                                         (tid,))]
    if not bl or not reqs or _wake_in_flight(ctx, tid):
        return
    last = reqs[-1]
    card = last["card_id"]
    show = ctx.show(card)
    st = rules.status(show)
    fresh = not (ctx.c.execute("SELECT 1 FROM relays WHERE card=?", (card,)).fetchone()
                 or _blocker_relayed(ctx, card))
    if st == "done":
        _create_request(ctx, tid, last["seq"] + 1, card)
        return
    if fresh and st == "blocked" and not rules.runs(show):
        expect, wait = "blocked", None
    elif st == "scheduled" and not fresh and (rules.latest_run(show) or {}).get("outcome") == "scheduled":
        expect, wait = "scheduled", rules.latest_run(show)["id"]
    else:
        if st in ("blocked", "triage", "archived"):
            ctx.alert("held:%s:%s" % (card, st), "ClickUp input for task %s is held: request card %s is %s"
                      % (tid, card, st), tid)
        return
    items = []
    for b in bl:
        q = ctx.show(b["question_card"])
        if q is None:
            return  # Q's text is read from this scan's show when relayed (r9 §4.2)
        items.append({"id": b["id"], "question_card": b["question_card"], "card": b["card"], "run_id": b["run_id"],
                      "reason": (q.get("task") or {}).get("body") or ""})
    ids = [b["id"] for b in bl]
    ref = "%s-s%d-k%s-w%d" % (tid, last["seq"], ".".join(map(str, ids)), wait or 0)
    t = ctx.task(tid)
    k = "clickup-dp:%s:blocker:%s" % (tid, ref)
    with ctx.store.transaction() as c:
        c.execute("UPDATE blockers SET state='relayed', relay_ref=?, request_card=?, updated_ms=? WHERE id IN (%s)"
                  % ",".join("?" * len(ids)), [ref, card, ctx.now] + ids)
        c.execute("UPDATE cards SET wait_run_id=? WHERE card_id=?", (wait, card))
        ctx.wake(tid, k + ":comment", "kb_comment", card, {
            "template": "blocker_relay", "text": "CLICKUP-BLOCKER v1 relay=%s blockers=%s" % (ref, ",".join(map(str, ids))),
            "relay": ref, "task": tid, "blockers": items, "context": "clickup-context-%s.md" % ref,
            "plan_rev": t["published_rev"],
            "plan_sha256": t["published_sha256"]})
        ctx.wake(tid, k + ":attach", "kb_attach", card, {"template": "context", "task": tid, "gen": t["input_gen"],
                                                         "name": "clickup-context-%s.md" % ref,  # = "context"
                                                         "quiet_until_ms": t["quiet_until_ms"]})
        deps = [k + ":comment", k + ":attach"]
        if fresh and ctx.cfg.notify_mode == "notify":
            deps.append(_subscribe(ctx, tid, card, ctx.wake))
        ctx.wake(tid, k + ":unblock", "kb_unblock", card, {"expect_status": expect, "wait_run_id": wait}, deps,
                 wait_run_id=wait)


def _create_request(ctx, tid, seq, prev):
    key = "clickup-dp:%s:req:%d" % (tid, seq)
    ctx.wake(tid, key, "kb_create", None, {
        "role": "request", "seq": seq, "title": "ClickUp %s: request %d" % (tid, seq), "template": "request_card",
        "params": {"task": tid, "seq": seq, "previous_card": prev}, "assignee": "planner",
        "skills": list(REQUEST_SKILLS), "initial_status": "blocked", "idempotency_key": key})


def _rebatch(ctx, tid, rel):
    """§4.4/§5.1/§8.2/§8.3: a staged relay whose wake was never sent is re-staged at the current barrier when
    activity was observed after it was staged (quiet moved), so the run gets one batch and a context snapshot of
    everything up to that barrier: a card's FIRST relay (blocked, never run) or a LATER relay to a card still parked
    in its recorded wait run (same wait_run guard). Once its wake may have been sent, nothing is re-staged."""
    k = "clickup-dp:%s:relay:%s" % (tid, rel["relay_ref"])
    att = ctx.c.execute("SELECT body FROM outbox WHERE idem_key=?", (k + ":attach",)).fetchone()
    quiet = ctx.task(tid)["quiet_until_ms"]
    if att is None or json.loads(att[0]).get("quiet_until_ms", quiet) == quiet:
        return
    unsent = ("pending", "retry_wait")
    if ctx.state(k + ":unblock") not in unsent or any(ctx.state(k + s) not in unsent + ("done",)
                                                       for s in (":comment", ":attach")):
        return
    row = ctx.c.execute("SELECT * FROM cards WHERE card_id=? AND task=? AND role='request'",
                        (rel["card"], tid)).fetchone()
    show = ctx.show(rel["card"])
    run = rules.latest_run(show)
    wait = int(re.search(r"-w(\d+)$", rel["relay_ref"]).group(1)) or None
    if row is None:
        return
    if wait is None and rules.status(show) == "blocked" and not rules.runs(show):
        _relay(ctx, tid, dict(row), "blocked", None, True, old=rel)
    elif wait is not None and rules.status(show) == "scheduled" and run and run.get("outcome") == "scheduled" \
            and run["id"] == wait:
        _relay(ctx, tid, dict(row), "scheduled", wait, False, old=rel)


def _relay(ctx, tid, card_row, expect, wait, fresh, old=None):
    t, card, seq = ctx.task(tid), card_row["card_id"], card_row["seq"]
    g = t["input_gen"]
    b = ""
    if old:  # re-staged unsent wake: new ref and a new (never overwritten) context file name
        m = re.search(r"-b(\d+)-w\d+$", old["relay_ref"])
        b = "-b%d" % (int(m.group(1)) + 1 if m else 2)
    ref = "%s-s%d-g%d%s-w%d" % (tid, seq, g, b, wait or 0)
    if ctx.c.execute("SELECT 1 FROM relays WHERE relay_ref=?", (ref,)).fetchone():
        return
    rows = ctx.c.execute("SELECT kind, id, command_result FROM items WHERE task=? AND delivered_relay IS NULL"
                         " AND (author_id IS NULL OR author_id<>?) AND (gen IS NULL OR gen<=?)"
                         " ORDER BY date_ms, id", (tid, ctx.cfg.bot_user_id, g)).fetchall()
    items = [[r[0], r[1]] for r in rows]
    closures = [[r[0], r[1]] for r in rows if (r[2] or "").startswith("closure_")]
    note = None
    if seq > 1:
        prev = ctx.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' AND seq=?",
                             (tid, seq - 1)).fetchone()
        ev = prev and rules.pass_evidence(ctx.show(prev[0]), ctx.obs.attachments.get(prev[0]), tid)
        if ev and t["published_rev"] != ev["rev"]:
            note = REVIEW_NOTE % ev["rev"]
    ok = old and "clickup-dp:%s:relay:%s" % (tid, old["relay_ref"])
    if old and ctx.state(ok + ":comment") == "done":
        note = (note + "; " if note else "") + "this relay supersedes relay %s (its batch predates later input)" \
            % old["relay_ref"]
    k = "clickup-dp:%s:relay:%s" % (tid, ref)
    with ctx.store.transaction() as c:
        note = _bind_blockers(ctx, tid, ref, note)
        if old:  # the unsent rows of the superseded relay never go out; its items return to the new batch
            c.execute("UPDATE outbox SET state='cancelled_stale', outcome='SUPERSEDED' WHERE idem_key IN (?,?,?)"
                      " AND state IN ('pending', 'retry_wait')", (ok + ":comment", ok + ":attach", ok + ":unblock"))
            c.execute("DELETE FROM relays WHERE relay_ref=?", (old["relay_ref"],))
        c.execute("INSERT INTO relays(relay_ref, task, card, gen, item_ids, state) VALUES (?,?,?,?,?,'staged')",
                  (ref, tid, card, g, json.dumps(items)))
        c.execute("UPDATE cards SET wait_run_id=? WHERE card_id=?", (wait, card))
        ctx.wake(tid, k + ":comment", "kb_comment", card, {
            "template": "relay", "text": "CLICKUP-IN v1 relay=%s gen=%d" % (ref, g), "relay": ref, "gen": g,
            "task": tid, "items": items, "closures": closures, "note": note})
        ctx.wake(tid, k + ":attach", "kb_attach", card, {"template": "context", "task": tid, "gen": g,
                                                         "name": "clickup-context-g%d%s.md" % (g, b),
                                                         "quiet_until_ms": t["quiet_until_ms"]})
        deps = [k + ":comment", k + ":attach"]
        if fresh and ctx.cfg.notify_mode == "notify":
            deps.append(_subscribe(ctx, tid, card, ctx.wake))
        ctx.wake(tid, k + ":unblock", "kb_unblock", card, {"expect_status": expect, "wait_run_id": wait}, deps,
                 wait_run_id=wait)


POST_CLOSURE = ("closed", "resolved", "releasing", "delivered")  # r9 §4.3 / r11 §3.5 whitelist


def _effect_open(ctx, b):
    """A non-final ``complete Q`` or correction row of blocker ``b`` exists (r9 §4.3 / r11 §3.5 derived state 1)."""
    return ctx.c.execute("SELECT 1 FROM outbox WHERE task=? AND (idem_key=? OR idem_key LIKE ?) AND state NOT IN (%s)"
                         % _in(("done",) + FAILED),
                         (b["task"], b["release_key"] or "", "clickup-dp:%s:blocker:%d:corr:%%" % (b["task"], b["id"]))
                         ).fetchone() is not None


def _derive(ctx, bid):
    """The one derived-state helper (r11 §3.5) for a post-closure non-final blocker; returns the new state."""
    b = dict(ctx.c.execute("SELECT * FROM blockers WHERE id=?", (bid,)).fetchone())
    if b["state"] not in POST_CLOSURE:
        return b["state"]
    if _effect_open(ctx, b):
        state = "releasing"
    elif (b["resolution_body"] is not None and b["resolution_relay"] == b["pending_relay"]
          and (b["delivered_gen"] is None or _sha(b["resolution_body"]) != b["delivered_sha256"])):
        state = "resolved"
    elif b["delivered_gen"] is not None:
        state = "delivered"
    else:
        state = "closed"
    ctx.c.execute("UPDATE blockers SET state=?, updated_ms=? WHERE id=?", (state, ctx.now, bid))
    return state


WAKE_ENDED = ("scheduled", "blocked", "done", "archived")


def _deliver(ctx, tid):
    """r9 §4.4 after ``_outputs``: confirm applied answer rows, then queue (a) the first delivery of a due
    answer as one ``complete Q`` row with the frozen r6 §4.6(b) predicates."""
    if _cutover_ms(ctx) is None:
        return
    t = ctx.task(tid)
    for b in map(dict, ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state IN (%s) ORDER BY id"
                                     % _in(PRE_CLOSURE + POST_CLOSURE), (tid,)).fetchall()):
        if b["state"] in PRE_CLOSURE:
            with ctx.store.transaction():
                _confirm(ctx, tid, b)  # r6 §4.5 re-ask while releasing: the kept answer row is reconciled first
                if not _effect_open(ctx, b):
                    _dispose(ctx, tid, dict(ctx.c.execute("SELECT * FROM blockers WHERE id=?", (b["id"],)).fetchone()))
            continue
        with ctx.store.transaction():
            _confirm(ctx, tid, b)
            state = _derive(ctx, b["id"])
            b = dict(ctx.c.execute("SELECT * FROM blockers WHERE id=?", (b["id"],)).fetchone())
            if state == "releasing" and _cancel_corrections(ctx, tid, b):
                state = _derive(ctx, b["id"])
                b = dict(ctx.c.execute("SELECT * FROM blockers WHERE id=?", (b["id"],)).fetchone())
            if state != "releasing" and _dispose(ctx, tid, b) is not False:
                continue  # final, or None: it waits for Q's observation (no lower-precedence path decides it)
            rel = ctx.c.execute("SELECT * FROM relays WHERE relay_ref=?", (b["pending_relay"],)).fetchone()
            if state != "releasing" and rel and rel["state"] == "classified" and rel["classification"] == "material":
                if ctx.show(b["card"]) is not None or ctx.show(b["question_card"]) is not None:
                    _scope_change(ctx, tid, b, rel["gen"])  # SPEC-R6-B1/B2: a pass observing neither W nor Q (the
                continue  # progress pass, failed reads) waits: W may be done/archived, whose disposition precedes
            if state == "delivered" and _settled(ctx, t, b):
                _recorded(ctx, tid, b)
            if state != "resolved" or not _due(ctx, t, b):
                continue
            (_first_delivery if b["delivered_gen"] is None else _correction)(ctx, tid, t, b)
            _derive(ctx, b["id"])


PRE_CLOSURE = ("detected", "relayed", "asked")


def _confirm(ctx, tid, b):
    """r11 §3.6 confirmation: the highest confirmed (done) answer or correction row sets delivered_gen/sha256."""
    done = [json.loads(r[0]) for r in ctx.c.execute(
        "SELECT body FROM outbox WHERE task=? AND state='done' AND (idem_key=? OR idem_key LIKE ?)",
        (tid, b["release_key"] or "", "clickup-dp:%s:blocker:%d:corr:%%" % (tid, b["id"])))]
    top = max(done, key=lambda p: p["gen"]) if done else None
    if top and top["gen"] != b["delivered_gen"]:  # a confirmed delivery (its readback applied)
        ctx.c.execute("UPDATE blockers SET delivered_gen=?, delivered_sha256=?, updated_ms=? WHERE id=?",
                      (top["gen"], top["answer_sha256"], ctx.now, b["id"]))


def _disabled_blockers(ctx, tid):
    """r9 §7 / r6 §7 after ``blockers disable``: the kept rows of a ``releasing`` blocker are still confirmed and the
    blocker re-derived (nothing is queued again); once it is no longer ``releasing``, the disable alert for the
    result is queued: the correction alert if an answer was delivered, else the open-question alert. Both read only
    the outbox, so the writer's progress pass keeps that alert (NA1, ``main.Glue.progress_pass``)."""
    if _cutover_ms(ctx) is not None or not ctx.c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='blockers'").fetchone():
        return
    for b in map(dict, ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state='releasing' ORDER BY id",
                                     (tid,)).fetchall()):
        with ctx.store.transaction():
            _confirm(ctx, tid, b)
            if _derive(ctx, b["id"]) != "releasing":
                b = dict(ctx.c.execute("SELECT * FROM blockers WHERE id=?", (b["id"],)).fetchone())
                _disabled_alert(ctx, b, b["delivered_gen"] is not None)


def _disabled_alert(ctx, b, delivered):
    if delivered:
        ctx.alert("blocker:%d:disabled:delivered" % b["id"], "ClickUp relay disabled: later ClickUp corrections to the"
                  " answer for %s (question %s) are no longer delivered and its resumption is not reported; comment"
                  " any correction on %s, or re-enable." % (b["card"], b["question_card"], b["card"]), b["task"])
    else:
        ctx.alert("blocker:%d:disabled:open" % b["id"], "ClickUp relay disabled: answer %s by completing it with the"
                  " decision as its summary, or re-enable." % b["question_card"], b["task"])


def _fences(ctx, tid):
    """r11 §3.3 engine side of the answer-fence release (parks: r12 §3.3). An open fence of W is released by one
    ``complete F`` row once all input is classified, W, F and each non-final blocker's Q are observed in this pass, and
    every non-final blocker of W whose Q is done is ``delivered`` and settled, behind the fence's barrier (none when it
    protects no answered question)."""
    enabled = _cutover_ms(ctx) is not None  # r12 §9: after disable, recorded parks and releasing rows go on
    if not enabled and not ctx.c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fences'").fetchone():
        return  # never enabled in this store: no relay tables to read
    t = ctx.task(tid)
    for f in map(dict, ctx.c.execute("SELECT * FROM fences WHERE task=? AND state IN ('open', 'releasing')"
                                     " ORDER BY id", (tid,)).fetchall()):
        with ctx.store.transaction():
            state = None
            if f["state"] == "releasing":
                row = ctx.state(f["release_key"])
                state = "released" if row == "done" else "open" if row == "cancelled_stale" else None
                if state == "open" and f["kind"] == "park" and \
                        rules.status(ctx.show(f["fence_card"])) in ("done", "archived"):
                    state = "owner_released"  # r12 §3.3: P already done/archived at send
            elif rules.status(ctx.show(f["card"])) == "archived":
                state = "abandoned"
            elif rules.status(ctx.show(f["fence_card"])) in (("done", "archived") if f["kind"] == "park" else ("done",)):
                state = "owner_released"
            if state:
                ctx.c.execute("UPDATE fences SET state=?, updated_ms=? WHERE id=?", (state, ctx.now, f["id"]))
            if state is not None or f["state"] == "releasing":
                continue
            if f["kind"] == "park":  # r12 §3.3: due when P is a parent of W and W has no current run
                w = ctx.show(f["card"])
                if w is None or f["fence_card"] not in rules.parents(w) or kp.open_run_ids(w):
                    continue
                key = "clickup-dp:%s:park:%s:release" % (tid, f["fence_card"])
                ctx.c.execute("UPDATE fences SET state='releasing', release_key=?, updated_ms=? WHERE id=?",
                              (key, ctx.now, f["id"]))
                ctx.kb(tid, key, "kb_complete", f["fence_card"],  # a refused row is sent again (rearm)
                       {"summary": "CLICKUP-PARK v1 card=%s released" % f["card"]}, rearm=True)
                continue
            if not enabled:  # r11 §9: a disable release refused at send (never sent) is offered again
                _disable_release(ctx, tid, f)
                continue
            if t["terminal"] or t["classified_gen"] != t["input_gen"]:
                continue  # r11 §3.3: t is not held or terminal
            open_ = [dict(b) for b in ctx.c.execute("SELECT * FROM blockers WHERE card=? AND state NOT IN (%s)"
                                                    " ORDER BY id" % _in(BLOCKER_FINAL), (f["card"],))]
            if any(ctx.show(c) is None for c in [f["card"], f["fence_card"]] + [b["question_card"] for b in open_]):
                continue  # E916 P1: a card unobserved in this pass (writer progress pass, failed read) is not "not done"
            answered = [b for b in open_ if rules.status(ctx.show(b["question_card"])) == "done"]
            if any(b["state"] != "delivered" or not _settled(ctx, t, b) for b in answered):
                continue
            anchor = f["anchor_ms"] if answered else None
            if anchor is not None and not ctx.barrier(t, anchor):
                continue
            key = "clickup-dp:%s:fence:%s:release:g%d" % (tid, f["fence_card"], t["input_gen"])
            ctx.c.execute("UPDATE fences SET state='releasing', release_key=?, snapshot=?, updated_ms=? WHERE id=?",
                          (key, json.dumps({b["question_card"]: b["delivered_gen"] for b in answered}, sort_keys=True),
                           ctx.now, f["id"]))
            ctx.kb(tid, key, "kb_complete", f["fence_card"],
                   {"summary": "CLICKUP-FENCE v1 card=%s released input_gen=%d" % (f["card"], t["input_gen"])},
                   rearm=True, anchor=anchor, input_gen=t["input_gen"])
    for f in map(dict, ctx.c.execute("SELECT * FROM fences WHERE task=? AND kind='park' AND state='owner_released'"
                                     " ORDER BY id", (tid,)).fetchall()):
        w = ctx.show(f["card"])  # r12 §3.5 override row: one alert once W is sticky after an operator-completed P
        last = ([e for e in kp._events(w) if e.get("kind") == "blocked"] or [{}])[-1] if w else {}
        p = last.get("payload") if isinstance(last.get("payload"), dict) else {}
        if rules.status(w) == "blocked" and p.get("rekind_reason") == "no_open_parent" \
                and f["fence_card"] in str(p.get("reason") or ""):
            with ctx.store.transaction():
                ctx.alert("park:%s:override" % f["fence_card"], "%s is sticky needs_input: its park card %s was"
                          " completed outside the intake before the run blocked (no open parent); an operator"
                          " decides how %s continues" % (f["card"], f["fence_card"], f["card"]), tid)


DISABLED_PARK_ALERT = ("ClickUp relay disabled: an operator releases a park card by completing it only when"
                       " `kanban show <its card>` shows no current run.")


def _disable_release(ctx, tid, f):
    """r11 §9 barrier-free ``complete F`` of a disabled relay; a refused (never sent) row is re-armed."""
    key = "clickup-dp:%s:fence:%s:release:disable" % (tid, f["fence_card"])
    ctx.c.execute("UPDATE fences SET state='releasing', release_key=?, updated_ms=? WHERE id=?",
                  (key, ctx.now, f["id"]))
    ctx.kb(tid, key, "kb_complete", f["fence_card"],
           {"summary": "CLICKUP-FENCE v1 card=%s released (relay disabled)" % f["card"]}, rearm=True)


def disable_blockers(store, now, config=None):
    """r6/r9 §7, r11 §9, r12 §9 ``blockers disable``, in one transaction: delete the cutover; cancel unsent
    (``pending``/``retry_wait``) blocker rows except ``complete Q``; queue one barrier-free ``complete F`` for each
    open or releasing fence (parks are left to ``_fences``); queue the operator alerts."""
    ctx = _Ctx(store, now, Observation(), config or Config())
    with store.transaction() as c:
        c.execute("DELETE FROM meta WHERE key=?", (CUTOVER_KEY,))
        c.execute("UPDATE outbox SET state='cancelled_stale' WHERE state IN ('pending', 'retry_wait')"
                  " AND kind != 'alert' AND (idem_key LIKE 'clickup-dp:%:blocker:%'"
                  " OR idem_key LIKE 'clickup-dp:%:bq:%')"
                  " AND NOT (kind='kb_complete' AND idem_key LIKE 'clickup-dp:%:blocker:%:answer:%')")
        for f in map(dict, c.execute("SELECT * FROM fences WHERE kind='fence' AND state IN ('open', 'releasing')"
                                     " ORDER BY id").fetchall()):
            if f["state"] == "releasing":
                if ctx.state(f["release_key"]) not in ("pending", "retry_wait", "cancelled_stale"):
                    continue  # possibly sent or settled: the existing readback finishes it
                c.execute("UPDATE outbox SET state='cancelled_stale' WHERE idem_key=?", (f["release_key"],))
            _disable_release(ctx, f["task"], f)
        for b in map(dict, c.execute("SELECT * FROM blockers WHERE state NOT IN (%s) ORDER BY id"
                                     % _in(BLOCKER_FINAL)).fetchall()):
            if b["state"] != "releasing":  # a releasing blocker is alerted once it settles (_disabled_blockers)
                _disabled_alert(ctx, b, b["state"] == "delivered")
        ctx.alert("blockers:disabled:%d" % now, DISABLED_PARK_ALERT, "")
    return ctx.new


def protocol_comments(store, now, targets, config=None):
    """r9 §7 D1-A: queue one trusted ``kb_comment`` "CLICKUP-PROTOCOL v1" with the protocol text on each
    ``(task, card)`` target (an existing effect kind; idempotent by key; no status write)."""
    ctx = _Ctx(store, now, Observation(), config or Config())
    text = "CLICKUP-PROTOCOL v1\n\n" + render.question_protocol().strip()
    with store.transaction():
        for tid, card in targets:
            ctx.kb(tid, "clickup-dp:%s:protocol:%s" % (tid, card), "kb_comment", card, {"text": text})
    return ctx.new


def _dispose(ctx, tid, b):
    """r9 §4.4 "Other outcomes" and r11 §3.5 final dispositions, taken only when the blocker has no non-final
    effect row (the caller skips ``releasing``); True when ``b`` became final. W's status is this scan's read;
    a watched W already cached as closed (r6 §4.2) is taken as archived, so no resumption is ever claimed.
    NA2: the terminal/superseded outcome and its open-Q audit alert are decided only on Q's status observed
    in this pass; an absent Q (progress pass, failed read) is not "open": None, the blocker waits for this pass.
    SPEC-R6-B2: an undelivered blocker of a gone approval is superseded whatever W's status, so it needs no W.
    NA3: a delivered one first needs W's consumption ACK; the writer shows that W (``_observe_watched``)
    and an absent W (progress pass, failed read) is None: the blocker waits, no lower-precedence path decides it.
    E936-F1: so does a delivered blocker whose ACK is undecided while its fence's release row is possibly sent."""
    w = ctx.show(b["card"])
    a = b["approval_id"] is not None and ctx.approval(b["approval_id"])
    gone = not a or a["state"] == "stale" or (a["state"] == "revoked" and a["revoked_reason"] == "superseded")
    delivered = b["delivered_gen"] is not None
    if w is None and b["card"] not in json.loads((ctx.c.execute(
            "SELECT value FROM meta WHERE key='blocker_closed_cards'").fetchone() or ["[]"])[0]) \
            and (delivered or not gone):
        return None if gone else False  # W unknown in this scan: no decision (gone: wait for W's read)
    ws = rules.status(w) if w is not None else "archived"
    qs = rules.status(ctx.show(b["question_card"]))
    linked = w is not None and b["question_card"] in rules.parents(w)
    posted = bool(b["question_key"]) and ctx.state(b["question_key"]) == "done"
    key = "clickup-dp:%s:blocker:%d:" % (tid, b["id"])
    ack = _ack(ctx, b, w) if delivered and w is not None else None
    if ack == UNDECIDED:
        return None  # E936-F1: its fence's release row is possibly sent; nothing lower decides it in this pass
    if ack == "valid":  # r11 §3.5: the first valid consumption ACK
        state = "resumed"
        ctx.reply(tid, key + "resumed", _closure_root(ctx, tid, b), "The blocked step has resumed with your answer.")
    elif ack:  # slip ACK
        state = "resumed_unconfirmed"
        ctx.reply(tid, key + "resumed", _closure_root(ctx, tid, b), "The blocked step has resumed.")
        ctx.alert("blocker:%d:unconfirmed" % b["id"], "%s (question card %s): %s" % (b["card"], b["question_card"], ack),
                  tid)
    elif gone or ws in ("done", "archived"):
        if not gone and ws == "done" and delivered:
            state = "resumed_unconfirmed"
            ctx.reply(tid, key + "resumed", _closure_root(ctx, tid, b), "The blocked step has resumed.")
            ctx.alert("blocker:%d:unconfirmed" % b["id"], "%s (question card %s): %s finished without a valid answer"
                      " acknowledgement" % (b["card"], b["question_card"], b["card"]), tid)
        else:
            if ctx.show(b["question_card"]) is None:
                return None  # NA2: an unobserved Q is not "open"; the writer shows it (_observe_watched)
            state = "superseded" if gone else "terminal"
            if posted and not delivered:
                ctx.reply(tid, key + "superseded", ctx.ref(b["question_key"]),
                          "No longer needed: this was resolved without your input")
            if qs not in ("done", "archived"):
                ctx.alert("blocker:%d:audit" % b["id"], "question card %s for %s stays open as an audit record;"
                          " blocker %d is %s" % (b["question_card"], b["card"], b["id"], state), tid)
    elif not delivered and (qs in ("done", "archived") or not linked
                            or any(r > (b["run_id"] or 0) for r in kp.claimed_runs(w))):
        state = "released_by_owner"
        if posted:
            ctx.reply(tid, key + "owner", ctx.ref(b["question_key"]), "This question was closed by its work owner.")
        ctx.alert("blocker:%d:owner" % b["id"], "question card %s for %s (blocked run %s) was released by its work"
                  " owner before the intake delivered an answer" % (b["question_card"], b["card"], b["run_id"]), tid)
    elif delivered and (qs == "archived" or not linked):
        state = "released_by_owner"
        ctx.alert("blocker:%d:owner" % b["id"], "delivered answer gen %d for %s left its context: question card %s was"
                  " unlinked or archived by its work owner" % (b["delivered_gen"], b["card"], b["question_card"]), tid)
    else:
        return False
    ctx.c.execute("UPDATE blockers SET state=?, updated_ms=? WHERE id=?", (state, ctx.now, b["id"]))
    _restore(ctx, tid, b)
    return True


POSSIBLY_SENT = ("sending", "ambiguous", "held_ambiguous")
UNDECIDED = "undecided"


def _ack(ctx, b, w):
    """r11 §3.5 consumption ACK of blocker ``b`` from W's show, first deciding comment in order: ``"valid"``, a
    slip ACK's alert case text, or None. Ignored: author clickup-intake, wrong question/card, run <= R or without
    a ``claimed`` event, a lane other than origin_lane, a fence not of W. E936-F1 (r11 §8, §3.3 "Reconcile before
    refusal"): an ACK of a delivered gen naming a fence created during its run that is still ``releasing`` with a
    possibly-sent release row is ``UNDECIDED`` until the executor's readback settles that row; then as above."""
    claimed = kp.claimed_runs(w)
    started = {r["id"]: r.get("started_at") for r in rules.runs(w)}
    pre = "clickup-dp:%s:blocker:%d:" % (b["task"], b["id"])
    sent = {json.loads(r[0])["gen"] for r in ctx.c.execute(
        "SELECT body FROM outbox WHERE task=? AND state='done' AND (idem_key LIKE ? OR idem_key LIKE ?)",
        (b["task"], pre + "answer:%", pre + "corr:%"))}
    for cm in kp._comments(w):
        hdr = rules.parse_ack(str(cm.get("body") or "").split("\n", 1)[0])
        if (not hdr or cm.get("author") == kp.AUTHOR or hdr["question"] != b["question_card"]
                or hdr["card"] != b["card"]):
            continue
        r, n = hdr["run"], hdr["gen"]
        if r <= (b["run_id"] or 0) or claimed.get(r) != (b["origin_lane"] or "work"):
            continue
        f = ctx.c.execute("SELECT * FROM fences WHERE fence_card=? AND card=? AND kind='fence'",
                          (hdr["fence"], b["card"])).fetchone()
        if f is None:
            continue
        if n not in sent:
            ctx.alert("blocker:%d:ack-undelivered:%d" % (b["id"], n), "%s run %d acknowledged gen %d of question card"
                      " %s, which the intake never delivered" % (b["card"], r, n, b["question_card"]), b["task"])
            return "run %d acknowledged gen %d, which the intake never delivered" % (r, n)
        row = ctx.state(f["release_key"]) if f["state"] == "releasing" else None
        released = f["state"] == "released" or row == "done"
        during = started.get(r) is not None and f["created_s"] >= started[r]
        if not released and during and row in POSSIBLY_SENT:
            return UNDECIDED  # E936-F1: the release may be applied; the executor's readback settles it first
        if not released or not during:
            return "run %d acknowledged without an intake-released answer fence created during that run" % r
        if n < json.loads(f["snapshot"] or "{}").get(b["question_card"], 0):
            return "run %d acknowledged gen %d below the answer delivered before its fence was released" % (r, n)
        return "valid"
    return None


def _cancel_corrections(ctx, tid, b):
    """r11 §3.5 ``resumed``: a valid consumption ACK cancels the blocker's pending correction rows (input posted
    after the consuming claim is ordinary task input). Only when every non-final effect row is an unsent
    correction (pending/retry_wait; a possibly-sent row is reconciled by the executor first). True if cancelled."""
    w = ctx.show(b["card"])
    pre = "clickup-dp:%s:blocker:%d:" % (tid, b["id"])
    rows = ctx.c.execute("SELECT idem_key, state FROM outbox WHERE task=? AND (idem_key=? OR idem_key LIKE ?) AND"
                         " state NOT IN (%s)" % _in(("done",) + FAILED),
                         (tid, b["release_key"] or "", pre + "corr:%")).fetchall()
    if (not rows or w is None or any(not k.startswith(pre + "corr:") or s not in ("pending", "retry_wait")
                                     for k, s in rows) or _ack(ctx, b, w) != "valid"):
        return False
    ctx.c.executemany("UPDATE outbox SET state='cancelled_stale' WHERE idem_key=?", [(k,) for k, _ in rows])
    return True


def _restore(ctx, tid, b):
    """r6 status restore: restore_status, only while the phase is phase_at_detect and no other blocker of the task
    is ``asked``. The row carries no blocker_id (it is not an ask row)."""
    if b["restore_status"] and not ctx.c.execute("SELECT 1 FROM blockers WHERE task=? AND state='asked' AND id<>?",
                                                 (tid, b["id"])).fetchone():
        ctx.set_status(tid, "clickup-dp:%s:blocker:%d:restore" % (tid, b["id"]), b["restore_status"],
                       phases=(b["phase_at_detect"],))


def _scope_change(ctx, tid, b, g):
    """r9 §4.5: material input for a post-closure blocker with no non-final effect row -> ``scope_change``
    (final); the task-level scope notice/revocation/revision path governs. Q stays open; W is not held."""
    ctx.c.execute("UPDATE blockers SET state='scope_change', updated_ms=? WHERE id=?", (ctx.now, b["id"]))
    if b["delivered_gen"] is None:
        text = ("%s waits on %s after a material ClickUp change; after the revised plan is approved, an operator"
                " completes %s with the approved decision or archives %s." % (b["card"], b["question_card"],
                                                                                b["question_card"], b["card"]))
    else:
        text = ("delivered answer gen %d for %s was superseded by material ClickUp input gen %d; %s may resume with"
                " gen %d before the revision is approved — the scope notice and revision path govern."
                % (b["delivered_gen"], b["card"], g, b["card"], b["delivered_gen"]))
    ctx.alert("blocker:%d:scope_change" % b["id"], text, tid)
    _restore(ctx, tid, b)  # r6 §4.6(a): status is restored


def _settled(ctx, t, b):
    """r6 ``settled`` (r9 §4.4): all input classified and the blocker's pending relay classified at input_gen
    with its planner wake ended in this scan's snapshot (the derived state is checked by the caller)."""
    rel = ctx.c.execute("SELECT * FROM relays WHERE relay_ref=?", (b["pending_relay"],)).fetchone()
    return (t["classified_gen"] == t["input_gen"] and rel is not None and rel["state"] == "classified"
            and rel["gen"] == t["input_gen"] and rules.status(ctx.show(rel["card"])) in WAKE_ENDED)


def _recorded(ctx, tid, b):
    """r9 §4.4 / r11 §3.5 "Answer recorded." reply to the closure, once per delivered generation."""
    text = "Answer recorded." + (" The security/containment boundary is unchanged; any new authority will come back"
                                 " as a plan revision for approval." if b["boundary"] else "")
    ctx.reply(tid, "clickup-dp:%s:blocker:%d:recorded:g%d" % (tid, b["id"], b["delivered_gen"]),
              _closure_root(ctx, tid, b), text)


def _closure_root(ctx, tid, b):
    """r6 §4.6 replies go "to the closure": its thread root when the closure was itself a thread reply."""
    return _root(ctx, tid, b["closure_item"]) or b["closure_item"]


def _due(ctx, t, b):
    """r9 §4.4 due trigger (the task is neither held nor terminal here: ``evaluate`` skips those)."""
    rel = ctx.c.execute("SELECT * FROM relays WHERE relay_ref=?", (b["pending_relay"],)).fetchone()
    a = b["approval_id"] is not None and ctx.approval(b["approval_id"])
    return (t["classified_gen"] == t["input_gen"] and rel is not None and rel["state"] == "classified"
            and rel["gen"] == t["input_gen"] == b["resolution_gen"] and rel["classification"] == "answer_only"
            and rules.status(ctx.show(rel["card"])) in WAKE_ENDED
            and bool(a) and a["state"] in WATCHED_STATES and rules.current(a["accounts_gen"], t)
            and b["resolution_authority"] == "unchanged")


def _boundary(b):
    return "security" if b["boundary"] else "none"


def _frozen(t, b, a):
    """r9 §4.4 frozen predicates of every answer effect row (r6 §4.6(b) form)."""
    return dict(approval_id=a["id"], approval_states=(a["state"],), scope_gen=t["scope_gen"],
                input_gen=b["resolution_gen"], blocker_id=b["id"], question_card=b["question_card"])


DELIVERY_MAX = 2000  # r9 §4.4 (a)/(b): the whole summary/correction text (native 4,096 and rendered 2,048 caps)


def _over_length(ctx, tid, b, text):
    """r9 Q10: an answer text over 2,000 characters is not delivered; one alert per generation."""
    if len(text) <= DELIVERY_MAX:
        return False
    ctx.alert("blocker:%d:overlength:g%d" % (b["id"], b["resolution_gen"]), "answer gen %d for %s (question card %s)"
              " is over 2,000 characters and was not delivered" % (b["resolution_gen"], b["card"], b["question_card"]),
              tid)
    return True


def _first_delivery(ctx, tid, t, b):
    g, q, a = b["resolution_gen"], b["question_card"], ctx.approval(b["approval_id"])
    key = "clickup-dp:%s:blocker:%d:answer:g%d" % (tid, b["id"], g)
    summary = ("CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s\n%s\nThis answer grants no new authority;"
               " boundary: %s — unchanged." % (q, b["card"], g, tid, b["resolution_body"], _boundary(b)))
    if _over_length(ctx, tid, b, summary):
        return
    ctx.c.execute("UPDATE blockers SET release_key=?, release_gen=?, updated_ms=? WHERE id=?",
                  (key, g, ctx.now, b["id"]))
    ctx.kb(tid, key, "kb_complete", q, {"summary": summary, "gen": g, "answer_sha256": _sha(b["resolution_body"])},
           rearm=True, **_frozen(t, b, a))  # a refused row is re-derived and sent again (r9 §4.4)


def _correction(ctx, tid, t, b):
    """r9 §4.4(b): a changed answer after delivery is one ``CLICKUP-ANSWER ... supersedes=`` comment on W."""
    g, d = b["resolution_gen"], b["delivered_gen"]
    text = ("CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s supersedes=g%d\n%s\nThis answer replaces gen %d for"
            " this question and grants no new authority; boundary: %s — unchanged."
            % (b["question_card"], b["card"], g, tid, d, b["resolution_body"], d, _boundary(b)))
    if _over_length(ctx, tid, b, text):
        return
    ctx.kb(tid, "clickup-dp:%s:blocker:%d:corr:g%d" % (tid, b["id"], g), "kb_comment", b["card"],
           {"text": text, "gen": g, "answer_sha256": _sha(b["resolution_body"])}, rearm=True,
           **_frozen(t, b, ctx.approval(b["approval_id"])))


def _bind_blockers(ctx, tid, ref, note):
    """r6 §4.5/§4.6 newer input (r9 §4.3 whitelist and wording), in the staging transaction of input relay ``ref``:
    every post-closure non-final blocker gets pending_relay = ref and is re-derived; the note lists them."""
    if _cutover_ms(ctx) is None:
        return note
    need, done = [], []
    for b in ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state IN (%s) ORDER BY id" % _in(POST_CLOSURE),
                           (tid,)).fetchall():
        ctx.c.execute("UPDATE blockers SET pending_relay=? WHERE id=?", (ref, b["id"]))
        _derive(ctx, b["id"])
        if b["delivered_gen"] is None:
            need.append(str(b["id"]))
        else:
            done.append("blocker %d: answer delivered at gen %d and not yet consumed; output a superseding resolution"
                        " only if this input changes it" % (b["id"], b["delivered_gen"]))
    lines = ([note] if note else []) + (
        ["blockers needing a resolution for this generation: " + ",".join(need)] if need else []) + done
    return "; ".join(lines) or None


def _subscribe(ctx, tid, card, put=None):
    """D3: passive notify subscription of a ClickUp-origin card to the configured Discord channel (never wake)."""
    key = "clickup-dp:%s:subscribe:%s" % (tid, card)
    (put or ctx.kb)(tid, key, "kb_subscribe", card, {"chat_id": ctx.cfg.discord_chat, "delivery_mode": "notify"})
    return key


def _pass_source(ctx, tid, rev, sha):
    for card, in ctx.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC", (tid,)):
        ev = rules.pass_evidence(ctx.show(card), ctx.obs.attachments.get(card), tid)
        if ev and ev["rev"] == rev and ev["sha256"] == sha:
            return card, ev
    return None


def _publication(ctx, tid, reqs):
    done = [r["card_id"] for r in reqs if rules.status(ctx.show(r["card_id"])) == "done"]
    if not done:
        return
    card = done[-1]
    ev = rules.pass_evidence(ctx.show(card), ctx.obs.attachments.get(card), tid)
    if ev is None:
        ctx.alert("nopass:" + card, "request card %s is done without valid PASS evidence" % card, tid)
        return
    delivered = ctx.c.execute("SELECT MAX(gen) FROM relays WHERE card=? AND state IN ('delivered', 'classified')",
                              (card,)).fetchone()[0]
    if ev["accounts_gen"] != delivered:
        ctx.alert("accounts:" + card, "request card %s PASS has accounts_gen %s, delivered %s"
                  % (card, ev["accounts_gen"], delivered), tid)
        return
    t, n, sha = ctx.task(tid), ev["rev"], ev["sha256"]
    if t["published_rev"] == n and t["published_sha256"] == sha:
        return
    amend = t["phase"] in AMENDMENT_PHASES
    if _posted(ctx, tid, "clickup-dp:%s:pub:r%d:%s:%%:comment" % (tid, n, sha[:12])):
        with ctx.store.transaction() as c:
            c.execute("UPDATE tasks SET published_rev=?, published_sha256=?, published_accounts_gen=?"
                      " WHERE clickup_task_id=?", (n, sha, ev["accounts_gen"], tid))
            if not amend:
                ctx.set_phase(tid, "awaiting_approval")
        return
    if not rules.current(ev["accounts_gen"], t) or not ctx.barrier(t):
        return  # unpublished; pending input reaches request <seq+1> with REVIEW_NOTE
    base = "clickup-dp:%s:pub:r%d:%s:i%d" % (tid, n, sha[:12], t["input_gen"])
    if ctx.state(base + ":comment") is not None:
        return
    pre = {"input_gen": t["input_gen"], "scope_gen": t["scope_gen"]}
    names = list(ctx.cfg.approvers.values())
    text = ("{summary}\n\nPlan r%d, SHA-256 %s\nIndependent review passed; this is NOT approval.\n"
            "To approve, %s may post it as a new plain comment:\nApprove plan r%d for development"
            % (n, sha, _or(names), n))
    with ctx.store.transaction():  # publication is quiet-expired: every send/retry rechecks the barrier (§4.4)
        ctx.cu(tid, base + ":attach", "cu_attach", {"card": card, "attachment": ev["attachment"], "sha256": sha,
                                                    "filename": "plan-r%d-%s-%s.md" % (n, sha[:12], _sha(base)[:6])},
               anchor=0, **pre)
        ctx.cu(tid, base + ":comment", "cu_comment", {"text": text, "summary_from": {"card": card,
                                                                                    "attachment": ev["attachment"]}},
               (base + ":attach",), anchor=0, **pre)
        if not amend:  # an amendment published during development keeps the status (§8.6)
            ctx.set_status(tid, base + ":status", "waiting for approval", (base + ":comment",), anchor=0, **pre)


def _revising(ctx, tid):
    t = ctx.task(tid)
    if t["published_rev"] is None or t["scope_gen"] <= (t["published_accounts_gen"] or 0):
        return
    if t["phase"] == "awaiting_approval":
        with ctx.store.transaction():
            _revise(ctx, tid, t["published_rev"])


def _revise(ctx, tid, rev):
    ctx.set_phase(tid, "revising")
    ctx.set_status(tid, "clickup-dp:%s:status:revising:r%d" % (tid, rev), "reviewing feature")
    ctx.comment(tid, "clickup-dp:%s:notice:revising:r%d" % (tid, rev), NOTICE_REVISING % rev)


# ---- approval lifecycle and promotion ticket (§8.4-§8.6) -----------------------------------

def _approvals(ctx, tid):
    handlers = {"held": _held, "recorded": _stage, "staged": _release, "released": _start,
                "started": _implement, "implemented": _issue, "ticketed": _ticketed, "revoked": _revoked}
    for aid, in ctx.c.execute("SELECT id FROM approvals WHERE task=? ORDER BY id", (tid,)).fetchall():
        for _ in range(8):
            a = ctx.approval(aid)
            if a["state"] not in handlers:
                break
            handlers[a["state"]](ctx, tid, a)
            if ctx.approval(aid)["state"] == a["state"]:
                break
        a = ctx.approval(aid)
        _scope_notice(ctx, tid, a)
        _superseded_gate(ctx, tid, a)


def _keys(tid, a):
    tail = "r%d:g%d" % (a["rev"], a["accounts_gen"])
    return "clickup-dp:%s:gate:%s" % (tid, tail), "clickup-dp:%s:orch:%s" % (tid, tail)


def _gates(a):
    return json.loads(a["clickup_gates"]) if a["clickup_gates"] else None


def _held(ctx, tid, a):
    t = ctx.task(tid)
    if t["classified_gen"] == t["input_gen"]:
        state = "recorded" if rules.current(a["accounts_gen"], t) else "stale"
        ctx.c.execute("UPDATE approvals SET state=? WHERE id=? AND state='held'", (state, a["id"]))


def _stage(ctx, tid, a):
    gk, ok = _keys(tid, a)
    n, sha, pre = a["rev"], a["plan_sha256"], {"approval_id": a["id"], "approval_states": ("recorded",)}
    gate = ctx.ref(gk)
    if gate is None:
        ctx.kb(tid, gk, "kb_create", None, {
            "role": "gate", "title": "ClickUp %s: approval gate r%d" % (tid, n), "template": "gate_card",
            "params": {"task": tid, "rev": n, "sha256": sha, "approval_id": a["id"]}, "assignee": None,
            "skills": [], "initial_status": "blocked", "idempotency_key": gk}, **pre)
        return
    orch = ctx.ref(ok)
    if orch is None:
        lineage = [{"approval_id": o["id"], "rev": o["rev"], "gate_card": o["gate_card"],
                    "clickup_gates": _gates(o)}
                   for o in map(dict, ctx.c.execute("SELECT * FROM approvals WHERE task=? AND id<? AND"
                                                     " clickup_gates IS NOT NULL ORDER BY id", (tid, a["id"])))]
        ctx.kb(tid, ok, "kb_create", None, {
            "role": "orchestrator", "title": "ClickUp %s: orchestrate %s r%d"
                                             % (tid, "amendment" if lineage else "approved plan", n),
            "template": "orchestrator_card",
            "params": {"task": tid, "rev": n, "sha256": sha, "gate": gate, "approval_id": a["id"],
                       "marker": rules.marker(gate, tid, n, sha), "lineage": lineage},
            "assignee": "orchestrator", "skills": [], "initial_status": "blocked", "idempotency_key": ok}, **pre)
        return
    src = _pass_source(ctx, tid, n, sha)
    if src is None:
        ctx.alert("source:a%d" % a["id"], "plan r%d bytes for task %s are not observable on a PASSed request card"
                  % (n, tid), tid)
        return
    keys = [ok + ":attach:plan", ok + ":attach:approval"]
    with ctx.store.transaction() as c:
        c.execute("UPDATE approvals SET gate_card=?, orchestrator_card=? WHERE id=?", (gate, orch, a["id"]))
        for card, role, key in ((gate, "gate", gk), (orch, "orchestrator", ok)):
            c.execute("INSERT OR IGNORE INTO cards(card_id, task, role, rev, approval_id, created_key)"
                      " VALUES (?,?,?,?,?,?)", (card, tid, role, n, a["id"], key))
        ctx.kb(tid, keys[0], "kb_attach", orch, {"source_card": src[0], "attachment": src[1]["attachment"],
                                                 "sha256": sha, "name": "clickup-plan-r%d.md" % n}, **pre)
        ctx.kb(tid, keys[1], "kb_attach", orch, {
            "template": "approval_record", "name": "clickup-approval-r%d.md" % n,
            "params": {"task": tid, "rev": n, "sha256": sha, "accounts_gen": a["accounts_gen"],
                       "comment_id": a["comment_id"], "author_id": a["author_id"], "author_name": a["author_name"],
                       "comment_date": a["comment_date"]}}, **pre)
        if ctx.cfg.notify_mode == "notify":
            keys += [_subscribe(ctx, tid, orch), _subscribe(ctx, tid, gate)]
        keys += _card_context(ctx, tid, a, gate, orch, ("recorded",))
    if all(ctx.state(k) == "done" for k in keys):
        with ctx.store.transaction() as c:  # both cards created and verified -> staged + queued reply
            c.execute("UPDATE approvals SET state='staged' WHERE id=? AND state='recorded'", (a["id"],))
            if ctx.task(tid)["phase"] == "awaiting_approval":
                ctx.set_phase(tid, "approval_queued")
            ctx.reply(tid, "clickup-dp:%s:reply:queued:a%d" % (tid, a["id"]), a["comment_id"],
                      "Approval of r%d by %s recorded; queued for development; not started" % (n, a["author_name"]))


def _card_context(ctx, tid, a, gate, orch, states):
    """§8.2 on the approval's own cards: the complete context snapshot and the ledger for the task's CURRENT
    input_gen, attached (and read back) on the orchestrator and the gate. Returns the row keys; staging and
    release wait until all are done, so a new generation gets its own files before the cards move on. Both names
    are generation-specific: the CLI never overwrites an attachment, so a reused name would be stored renamed and
    never pass the exact readback (§10.2)."""
    g = ctx.task(tid)["input_gen"]
    keys = []
    for card, role, base in ((orch, "orchestrator", _keys(tid, a)[1]), (gate, "gate", _keys(tid, a)[0])):
        for template, name in (("context", "clickup-context-g%d.md" % g), ("ledger", "clickup-ledger-g%d.md" % g)):
            key = "%s:attach:%s:g%d" % (base, template, g)
            ctx.kb(tid, key, "kb_attach", card, {"template": template, "task": tid, "gen": g, "name": name,
                                                 "card_role": role},
                   approval_id=a["id"], approval_states=states, input_gen=g)
            keys.append(key)
    return keys


def _stale(ctx, tid, a, notice):
    with ctx.store.transaction() as c:
        c.execute("UPDATE approvals SET state='stale' WHERE id=? AND state IN ('recorded', 'staged')", (a["id"],))
        for card in (a["orchestrator_card"], a["gate_card"]):
            if card:
                ctx.kb(tid, "clickup-dp:%s:archive:%s" % (tid, card), "kb_archive", card)
        if notice:
            _revise(ctx, tid, a["rev"])


def _release(ctx, tid, a):
    t = ctx.task(tid)
    if t["classified_gen"] != t["input_gen"]:
        return
    if not rules.current(a["accounts_gen"], t):
        _stale(ctx, tid, a, notice=True)
        return
    if not ctx.cfg.handoff_unblock:
        return
    uk = _keys(tid, a)[1] + ":unblock"
    if ctx.state(uk) == "done":
        ctx.c.execute("UPDATE approvals SET state='released' WHERE id=? AND state='staged'", (a["id"],))
        return
    with ctx.store.transaction():
        context = _card_context(ctx, tid, a, a["gate_card"], a["orchestrator_card"], ("staged",))
    if any(ctx.state(k) != "done" for k in context):
        return  # §8.2: the current generation's context + ledger precede release
    # §10.1: an unsent release cancelled as stale (e.g. input arrived during its retry) is replanned once this
    # revalidation passes again; only for the approval's own registered orchestrator. Rows that may have been
    # sent (held_ambiguous/failed) are never re-armed, and the send-time readback keeps it a single unblock.
    own = ctx.c.execute("SELECT 1 FROM cards WHERE card_id=? AND task=? AND role='orchestrator' AND approval_id=?"
                        " AND created_key=?", (a["orchestrator_card"], tid, a["id"], _keys(tid, a)[1])).fetchone()
    ctx.kb(tid, uk, "kb_unblock", a["orchestrator_card"], {"expect_status": "blocked", "wait_run_id": None},
           rearm=own is not None, approval_id=a["id"], approval_states=("staged",), input_gen=t["input_gen"],
           scope_gen=t["scope_gen"])


def _start(ctx, tid, a):
    show = ctx.show(a["orchestrator_card"])
    if rules.status(show) != "done":
        return
    g = rules.gates_of(show, a["rev"], a["plan_sha256"])
    if g is None:
        ctx.alert("gates:" + a["orchestrator_card"], "invalid clickup_gates on orchestrator card %s (task %s r%d)"
                  % (a["orchestrator_card"], tid, a["rev"]), tid)
        return
    promo = ctx.show(g["promotion"])
    if promo is None:
        return
    if not rules.guarded(promo, a["gate_card"], tid, a["rev"], a["plan_sha256"]):
        ctx.alert("unguarded:" + g["promotion"], "promotion unguarded: card %s lacks gate parent %s or its"
                  " ticket-check marker; no ticket will be issued" % (g["promotion"], a["gate_card"]), tid)
        return
    with ctx.store.transaction() as c:
        c.execute("UPDATE approvals SET state='started', clickup_gates=? WHERE id=? AND state='released'",
                  (json.dumps(g), a["id"]))
        ctx.set_phase(tid, "development")
        ctx.set_status(tid, "clickup-dp:%s:status:started:a%d" % (tid, a["id"]), "implementing")
        ctx.reply(tid, "clickup-dp:%s:reply:started:a%d" % (tid, a["id"]), a["comment_id"],
                  "Development started for r%d" % a["rev"])


def _implement(ctx, tid, a):
    if rules.status(ctx.show(_gates(a)["final_e2e"])) != "done":
        return
    with ctx.store.transaction() as c:
        c.execute("UPDATE approvals SET state='implemented' WHERE id=? AND state='started'", (a["id"],))
        ctx.set_phase(tid, "implemented")
        ctx.set_status(tid, "clickup-dp:%s:status:implemented:a%d" % (tid, a["id"]), "implemented")


def _issue(ctx, tid, a):
    """Ticket issue + audit stamp + gate completion (§8.5), quiet-expired, barrier anchored at final_e2e done."""
    if a["state"] == "revoked" and a["revoked_reason"] not in REISSUE_REASONS:
        return
    t, g = ctx.task(tid), _gates(a)
    if not g or not rules.current(a["accounts_gen"], t) or t["terminal"] or t["hold_reason"]:
        return
    fe = ctx.show(g["final_e2e"])
    anchor = rules.done_ms(fe)
    if rules.status(fe) != "done" or anchor is None or not ctx.barrier(t, anchor):
        return
    i, gate = t["input_gen"], a["gate_card"]
    with ctx.store.transaction() as c:
        cur = c.execute(
            "UPDATE approvals SET state='ticketed', ticket_epoch=ticket_epoch+1, ticket_input_gen=?,"
            " ticket_renewed_ms=?, revoked_reason=NULL, revoked_ms=NULL WHERE id=? AND state=? AND EXISTS"
            " (SELECT 1 FROM tasks t WHERE t.clickup_task_id=approvals.task AND t.input_gen=? AND"
            " t.classified_gen=? AND t.scope_gen<=approvals.accounts_gen AND t.terminal=0 AND"
            " (t.hold_reason IS NULL OR t.hold_reason=''))", (i, ctx.now, a["id"], a["state"], i, i))
        if cur.rowcount != 1:
            return  # replanned next scan
        e = a["ticket_epoch"] + 1
        ctx.set_phase(tid, "promotion_window")
        pre = {"approval_id": a["id"], "approval_states": ("ticketed",), "input_gen": i}
        sk = "clickup-dp:%s:stamp:a%d:e%d:valid" % (tid, a["id"], e)
        ctx.kb(tid, sk, "kb_comment", gate, {"text": "CLICKUP-GATE v1 audit=valid task=%s rev=%d sha=%s epoch=%d"
                                                     " input_gen=%d" % (tid, a["rev"], a["plan_sha256"], e, i)},
               anchor=anchor, **pre)
        if rules.status(ctx.show(gate)) not in ("done", "archived"):
            if ctx.cfg.auto_gate:  # release is quiet-expired: each send/retry waits for the anchored barrier (§4.4)
                ctx.kb(tid, "clickup-dp:%s:gate-complete:a%d:e%d" % (tid, a["id"], e), "kb_complete", gate,
                       {"summary": "ClickUp approval r%d current at input_gen %d" % (a["rev"], i)}, (sk,),
                       anchor=anchor, **pre)
            else:
                ctx.alert("gate:%s:e%d" % (gate, e), "r%d ticket valid; the operator may complete gate %s" % (a["rev"], gate),
                          tid)
        if a["state"] == "revoked" and rules.status(ctx.show(g["promotion"])) == "blocked":
            ctx.alert("valid-again:%s:e%d" % (g["promotion"], e),
                      "r%d ticket valid again; promotion card %s is blocked by an earlier INVALID check; unblock if"
                      " appropriate" % (a["rev"], g["promotion"]), tid)


def _promoted(ctx, tid, a):
    g = _gates(a)
    promo = ctx.show(g["promotion"]) if g else None
    st = rules.status(promo)
    if st == "done":
        sha = rules.promoted_sha(promo)
        if sha is None:
            ctx.alert("promo-sha:" + g["promotion"], "promotion card %s is done without a valid promoted_sha; status"
                      " unchanged" % g["promotion"], tid)
            return False
        with ctx.store.transaction() as c:
            c.execute("UPDATE approvals SET state='promoted', promoted_sha=? WHERE id=? AND state IN"
                      " ('ticketed', 'revoked')", (sha, a["id"]))
            ctx.set_phase(tid, "ready_for_deployment")
            ctx.set_status(tid, "clickup-dp:%s:status:promoted:a%d" % (tid, a["id"]), "ready for deployment")
        return True
    if st == "archived" and a["revoked_reason"] != "superseded":
        ctx.alert("promo-archived:" + g["promotion"], "promotion card %s was archived; status unchanged"
                  % g["promotion"], tid)
    return False


def _ticketed(ctx, tid, a):
    if _promoted(ctx, tid, a):
        return
    t = ctx.task(tid)
    if ctx.now - a["ticket_renewed_ms"] >= ctx.cfg.ticket_renew_seconds * 1000 \
            and ctx.barrier(t, a["ticket_renewed_ms"]):
        ctx.store.renew_ticket(a["id"], a["ticket_epoch"], ctx.now)  # local CAS; posts nothing


def _revoked(ctx, tid, a):
    if a["ticket_epoch"] >= 1 and _promoted(ctx, tid, a):
        return
    if a["ticket_epoch"] >= 1:
        _issue(ctx, tid, a)


def _revocations(ctx, tid):
    """Protective follow-ups of revocations already committed (§8.5): audit stamp, alert, held notice."""
    t = ctx.task(tid)
    for a in map(dict, ctx.c.execute("SELECT * FROM approvals WHERE task=? AND ticket_epoch>=1 ORDER BY id", (tid,))):
        e, reason = a["ticket_epoch"], a["revoked_reason"]
        vk = "clickup-dp:%s:stamp:a%d:e%d:valid" % (tid, a["id"], e)
        rk = "clickup-dp:%s:stamp:a%d:e%d:revoked" % (tid, a["id"], e)
        stamp = "revoked" if ctx.state(rk) == "done" else "valid" if ctx.state(vk) == "done" else a["audit_stamp_state"]
        if stamp != a["audit_stamp_state"]:
            ctx.c.execute("UPDATE approvals SET audit_stamp_state=? WHERE id=?", (stamp, a["id"]))
        if a["state"] != "revoked":
            continue
        if ctx.state(vk) not in (None, "cancelled_stale"):
            ctx.kb(tid, rk, "kb_comment", a["gate_card"], {"text": "CLICKUP-GATE v1 audit=revoked task=%s rev=%d"
                                                                   " epoch=%d reason=%s" % (tid, a["rev"], e, reason)})
        if ctx.state(rk) in ("failed", "held_ambiguous"):
            ctx.alert("stamp:" + rk, "revocation audit stamp could not be posted on gate %s (task %s r%d epoch %d);"
                      " the ticket is INVALID regardless" % (a["gate_card"], tid, a["rev"], e), tid)
        if reason == "superseded":
            continue
        ctx.alert("revoked:a%d:e%d" % (a["id"], e), "ticket revoked: task %s r%d epoch %d reason %s"
                  % (tid, a["rev"], e, reason), tid)
        if t["phase"] == "promotion_window":
            ctx.set_phase(tid, "promotion_held")
        if reason == "new_input" and not t["terminal"] and tid in ctx.obs.confirmed \
                and ctx.now >= (t["quiet_until_ms"] or 0):
            ctx.comment(tid, "clickup-dp:%s:notice:held:a%d:e%d" % (tid, a["id"], e), NOTICE_HELD % a["rev"])


def _scope_notice(ctx, tid, a):
    """§8.6: material after release while rN is not promoted -> one notice; status unchanged."""
    if a["state"] not in ("released", "started", "implemented", "revoked") or a["revoked_reason"] == "superseded":
        return
    t = ctx.task(tid)
    if t["scope_gen"] > a["accounts_gen"] and ctx.now >= (t["quiet_until_ms"] or 0):
        ctx.comment(tid, "clickup-dp:%s:notice:scope:a%d" % (tid, a["id"]), NOTICE_SCOPE % a["rev"])


def _superseded_gate(ctx, tid, a):
    """Archive an old gate only after its promotion card is archived (an archived parent releases children)."""
    g = _gates(a)
    if a["state"] != "revoked" or a["revoked_reason"] != "superseded" or not g:
        return
    if rules.status(ctx.show(g["promotion"])) == "archived" and rules.status(ctx.show(a["gate_card"])) != "archived":
        ctx.kb(tid, "clickup-dp:%s:archive:%s" % (tid, a["gate_card"]), "kb_archive", a["gate_card"])


def _closed_task_blockers(ctx, tid):
    """r6 §4.7 ("the task is terminal") read with r9 §4.4 / r11 §3.5: each non-final blocker of a terminal task
    becomes ``terminal`` once it has no non-final effect row. No ClickUp write is queued (existing prechecks cancel
    unsent ClickUp writes of such a task); an open Q stays as an audit record with one alert. A held task is not
    terminal: owner V1 decision the approved temporary-hold rule makes a temporary hold a pause (``evaluate`` skips it).
    NA1: decided only on Q's status observed in this pass (the writer shows it, ``_observe_watched``); an
    absent Q (progress pass, failed read, held task) is not "open" and the blocker waits, so its alert is queued
    in the full pass with the transition."""
    if _cutover_ms(ctx) is None:
        return
    for b in map(dict, ctx.c.execute("SELECT * FROM blockers WHERE task=? AND state NOT IN (%s) ORDER BY id"
                                     % _in(BLOCKER_FINAL), (tid,)).fetchall()):
        with ctx.store.transaction():
            if ctx.c.execute("SELECT 1 FROM outbox WHERE task=? AND (idem_key LIKE ? OR idem_key LIKE ?) AND state"
                             " NOT IN (%s)" % _in(("done",) + FAILED),
                             (tid, "clickup-dp:%s:blocker:%d:%%" % (tid, b["id"]),
                              "clickup-dp:%s:bq:%%:%d:%%" % (tid, b["id"]))).fetchone():
                continue
            q = ctx.show(b["question_card"])
            if q is None:
                continue
            ctx.c.execute("UPDATE blockers SET state='terminal', updated_ms=? WHERE id=?", (ctx.now, b["id"]))
            if rules.status(q) not in ("done", "archived"):
                ctx.alert("blocker:%d:audit" % b["id"], "question card %s for %s stays open as an audit record;"
                          " blocker %d is terminal" % (b["question_card"], b["card"], b["id"]), tid)


def _terminal(ctx, tid):
    """Terminal task: archive parked own request cards and never-released own cards; no ClickUp writes."""
    for card, role in ctx.c.execute("SELECT card_id, role FROM cards WHERE task=?", (tid,)).fetchall():
        if role == "request" and rules.status(ctx.show(card)) in ("scheduled", "blocked"):
            ctx.kb(tid, "clickup-dp:%s:archive:%s" % (tid, card), "kb_archive", card)
    for a in map(dict, ctx.c.execute("SELECT * FROM approvals WHERE task=? AND state IN ('recorded', 'staged')",
                                     (tid,))):
        _stale(ctx, tid, a, notice=False)
