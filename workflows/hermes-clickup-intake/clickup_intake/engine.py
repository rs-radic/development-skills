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

from . import rules
from .config import Config
from .contracts import EffectIntent, Preconditions
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
        self.put(key, kind, tid, payload, Preconditions(task_id=tid, list_id=self.cfg.list_id, **pre), depends)

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
        self.put(key, kind, card, payload, Preconditions(task_id=tid, **pre), depends, rearm)

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
        if t["terminal"]:
            _terminal(ctx, tid)
            continue
        if t["hold_reason"] or tid not in ctx.obs.confirmed:
            continue
        _outputs(ctx, tid)
        _commands(ctx, tid)
        _requests(ctx, tid)
        _approvals(ctx, tid)
    return ctx.new


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
            return finish("context")
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
        if not text:
            return
        out = "clickup-dp:%s:out:%s" % (tid, h12)
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
    fresh = not ctx.c.execute("SELECT 1 FROM relays WHERE card=?", (card,)).fetchone()
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


def _terminal(ctx, tid):
    """Terminal task: archive parked own request cards and never-released own cards; no ClickUp writes."""
    for card, role in ctx.c.execute("SELECT card_id, role FROM cards WHERE task=?", (tid,)).fetchall():
        if role == "request" and rules.status(ctx.show(card)) in ("scheduled", "blocked"):
            ctx.kb(tid, "clickup-dp:%s:archive:%s" % (tid, card), "kb_archive", card)
    for a in map(dict, ctx.c.execute("SELECT * FROM approvals WHERE task=? AND state IN ('recorded', 'staged')",
                                     (tid,))):
        _stale(ctx, tid, a, notice=False)
