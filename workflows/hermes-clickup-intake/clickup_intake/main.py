"""S4 composition: scan loop, outbox executor, lock, alerts and operator subcommands (plan r5 §3, §4.1, §8, §10, §11).

One ``run`` is one cron scan in the §4.1 order: lock + config, status-set preflight (a mismatch ends the run
with no task reads, no evaluation, no outbox execution and no renewal), discovery and task passes (S1),
Kanban observation (``show --json``), engine evaluation (S2), outbox execution (S1 ClickUp effects, S3 Kanban
effects), then deduped operator alerts on stdout. ``ticket-check`` is read-only: it opens the state file of
its own installation and never loads the ClickUp token.

The executor adapts S2 outbox rows to the S1/S3 executors without changing either:
- ``cu_*`` rows run through ``clickup_effects.execute`` on a mirror row ``<key>#cu`` (S1 kinds and body);
  the S2 row copies the mirror's state and ``result_ref``.
- ``kb_*`` rows run through ``kanban_port.run_kanban_effect`` with the S3 kind and payload. A ``kb_unblock``
  of a blocked, never-run card (initial release, ``expect_status=blocked``) uses ``port.unblock`` plus a
  ready/todo/running readback; relay wakes keep the §10.3 wake guard.
- Before every send and retry (``send_gate``): ``rules.preconditions_ok`` (False cancels), a reply root must
  be an observed top-level comment of the row's own task (else cancelled + alert), then
  ``rules.send_barrier_ok`` for quiet-expired rows (False defers; not an attempt).
- Context snapshots are read resumably across scans (``read_snapshot``), and orchestrator/gate ``card_role``
  rows attach exactly one context or ledger file each.
"""

import argparse
import dataclasses
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import time

from . import clickup_api, clickup_effects, engine, observe, render, rules
from . import kanban_port as kp
from .config import load_config
from .contracts import EffectIntent, Outcome, Preconditions, RowState
from .parse import parse_comment
from .store import Store, load_ticket_snapshot, open_readonly, revoke_live_tickets
from .ticket import ticket_verdict

HERMES_EXE = "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe"
CREDENTIAL = "C:/Hermes/secrets/dedicated-panel-clickup/api-key.dpapi"
CONFIG_REL = ("integrations", "clickup-dedicated-panel", "config.json")

FINAL = ("done", "cancelled_stale", "failed", "held_ambiguous")
DEAD = ("cancelled_stale", "failed", "held_ambiguous")
CU_KINDS = {"cu_comment": "clickup_comment", "cu_reply": "clickup_reply", "cu_status": "clickup_status",
            "cu_attach": "clickup_attachment"}
KB_KINDS = {"kb_create": kp.CREATE, "kb_comment": kp.COMMENT, "kb_attach": kp.ATTACH, "kb_unblock": kp.UNBLOCK,
            "kb_complete": kp.COMPLETE, "kb_archive": kp.ARCHIVE, "kb_subscribe": kp.SUBSCRIBE}
KB_BACKOFF_MIN = clickup_effects.SEND_BACKOFF_MIN
MAX_ATTEMPTS = clickup_effects.MAX_ATTEMPTS
PREFLIGHT_KEY = "incident_preflight"
OPEN_ALERTS_KEY = "open_alerts"
CLOSED_CARDS_KEY = "closed_cards"
PREPASS_KEY = "prepass_outbox_mark"
COMMENT_PAGE = 25


def _sha(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _ok(r):
    return r is not None and r.outcome is Outcome.OK


# ----------------------------------------------------------------------------------------- capture
class _Capture:
    """ClickUp transport view that records this scan's GET results for the engine (no persistence)."""

    def __init__(self, inner, glue):
        self.inner, self.glue = inner, glue

    def request(self, method, path, params=None, body=None, multipart=None):
        r = self.inner.request(method, path, params=params, body=body, multipart=multipart)
        if method == "GET" and r.outcome is Outcome.OK and isinstance(r.json, dict):
            self.glue.capture(path, r.json)
        return r


# ----------------------------------------------------------------------------------------- the glue
class Glue:
    def __init__(self, cfg, store, transport, port, now, *, secrets=(), opener=None, log=None):
        self.cfg, self.store, self.c, self.inner, self.port, self.now = cfg, store, store.conn, transport, port, now
        self.secrets = tuple(s for s in secrets if s)
        self.opener = opener
        self.log = log or logging.getLogger("clickup_intake.null")
        self.guard = clickup_api.ApiGuard(store, cfg, now)
        self.staging = os.path.join(cfg.runtime_dir, "staging")
        self.out = []          # stdout lines (operator alerts)
        self.alerts = []       # incident alerts of this scan (deduped across scans)
        self.comments, self.confirmed, self.roots = {}, set(), {}
        self.obs = engine.Observation(scan_started_ms=now)
        self._att = {}

    # -- meta
    def meta(self, key, default=None):
        r = self.c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        with self.store.transaction() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # -- capture of this scan's reads
    def capture(self, path, data):
        m = re.fullmatch(r"/v2/task/([^/]+)", path)
        if m:
            lst = data.get("list")
            if isinstance(lst, dict) and str(lst.get("id")) == str(self.cfg.list_id):
                self.confirmed.add(m.group(1))
            return
        m = re.fullmatch(r"/v2/task/([^/]+)/comment", path)
        if m and isinstance(data.get("comments"), list):
            for cm in data["comments"]:
                if isinstance(cm, dict) and cm.get("id") is not None:
                    self.comments[(m.group(1), "comment", str(cm["id"]))] = (cm.get("comment"), cm.get("comment_text"))
                    self.roots[str(cm["id"])] = m.group(1)
            return
        m = re.fullmatch(r"/v2/comment/([^/]+)/reply", path)
        if m and isinstance(data.get("comments"), list):
            root = m.group(1)
            tid = self.roots.get(root)
            if tid is None:
                r = self.c.execute("SELECT task FROM items WHERE kind='comment' AND id=?", (root,)).fetchone()
                tid = r[0] if r else None
            if tid is None:
                return
            for cm in data["comments"]:
                if isinstance(cm, dict) and cm.get("id") is not None:
                    self.comments[(tid, "reply", str(cm["id"]))] = (cm.get("comment"), cm.get("comment_text"))

    # ------------------------------------------------------------------------------------- §4.1 scan
    def preflight(self):
        """§4.1 step 2. Returns True only on an exact status-set match."""
        pf = observe.preflight_statuses(self.guard.transport(self.inner), self.cfg)
        incident = self.meta(PREFLIGHT_KEY)
        if pf.status == "MISMATCH":
            text = "ClickUp intake stopped: " + pf.describe() + "; no task polling and no writes until it matches"
            if incident != text:
                self.out.append(text)
                self.set_meta(PREFLIGHT_KEY, text)
            self.log.info(json.dumps({"event": "preflight", "status": "MISMATCH"}))
        elif pf.ok and incident is not None:
            self.out.append("ClickUp intake: status-set preflight matches again; polling resumed")
            self.set_meta(PREFLIGHT_KEY, None)
        if not pf.ok:
            self.alerts.extend(self.guard.alerts)
        return pf.ok

    def scan(self, shadow=False):
        if not self.preflight():
            self.finish()
            return
        res = observe.observe_scan(_Capture(self.inner, self), self.store, self.cfg, self.now, guard=self.guard)
        n_guard = len(self.guard.alerts)
        self.alerts.extend(res.alerts)
        self.observe_cards()
        self.progress_pass()
        engine.evaluate(self.store, self.now, self.obs, self.cfg)
        if shadow:
            self.shadow_report(res)
        else:
            self.execute()
        self.alerts.extend(self.guard.alerts[n_guard:])
        self.close_cards()
        self.sweep_staging()
        self.finish()

    def progress_pass(self):
        """S2 consumes worker outputs before it records delivered relays. A first pass without cards or
        comments records this run's earlier outbox results, so a worker that parked since then is matched
        to its relay. Without cards that pass can raise false alerts (e.g. a plan source "not observable"),
        so the alert rows it added are dropped; the full pass queues every genuine one again. The outbox
        rowid mark is kept in meta until the drop commits, so a crash in between drops them on the rerun."""
        mark = self.meta(PREPASS_KEY)
        if mark is None:
            mark = self.c.execute("SELECT COALESCE(MAX(rowid), 0) FROM outbox").fetchone()[0]
            self.set_meta(PREPASS_KEY, mark)
        engine.evaluate(self.store, self.now,
                        dataclasses.replace(self.obs, comments={}, cards={}, attachments={}), self.cfg)
        with self.store.transaction() as c:
            c.execute("DELETE FROM outbox WHERE kind='alert' AND state='pending' AND rowid>?", (mark,))
            c.execute("DELETE FROM meta WHERE key=?", (PREPASS_KEY,))

    def finish(self):
        """Deduped incident alerts: an alert repeated by consecutive scans is printed once."""
        prev = set(self.meta(OPEN_ALERTS_KEY, []))
        cur = list(dict.fromkeys(self.alerts))
        self.out.extend(a for a in cur if a not in prev)
        if set(cur) != prev:
            self.set_meta(OPEN_ALERTS_KEY, cur or None)

    def shadow_report(self, res):
        rows = self.c.execute("SELECT idem_key, kind, target FROM outbox WHERE state NOT IN (%s) AND kind<>'alert'"
                              " AND kind NOT LIKE 'clickup_%%' ORDER BY rowid" % ",".join("?" * len(FINAL)),
                              FINAL).fetchall()
        for key, kind, target in rows:
            self.log.info(json.dumps({"event": "would", "kind": kind, "target": target, "key": key}))
        self.execute(alerts_only=True)
        d = res.discovery
        self.out.append("shadow: preflight OK; discovery pages=%d complete=%s admitted=%d; task passes=%d; "
                        "would-effects=%d" % (d.pages, d.complete, len(d.admitted), len(res.passes), len(rows)))

    # ------------------------------------------------------------------------------- §4.1 step 5
    def _show(self, card):
        r = self.port.show(card)
        return r.json if _ok(r) and isinstance(r.json, dict) else None

    def observe_cards(self):
        """``show --json`` of glue-owned cards and ``clickup_gates`` ids; ``attachments`` of done request cards."""
        c = self.c
        terminal = {r[0] for r in c.execute("SELECT clickup_task_id FROM tasks WHERE terminal=1")}
        closed = set(self.meta(CLOSED_CARDS_KEY, []))
        want = {}
        for card, task, role in c.execute("SELECT card_id, task, role FROM cards ORDER BY rowid"):
            if task in terminal and (role != "request" or card in closed):
                continue
            want[card] = role
        for key, task, ref in c.execute("SELECT idem_key, task, result_ref FROM outbox WHERE kind='kb_create'"
                                        " AND state='done' AND result_ref IS NOT NULL ORDER BY rowid"):
            if ref and task not in terminal:
                want.setdefault(ref, "request" if ":req:" in key else "other")
        cards = {}
        for card in want:
            shown = self._show(card)
            if shown is not None:
                cards[card] = shown
        for a in map(dict, c.execute("SELECT * FROM approvals ORDER BY id")):
            if a["task"] in terminal:
                continue
            g = json.loads(a["clickup_gates"]) if a["clickup_gates"] else None
            if g is None and a["state"] == "released" and a["orchestrator_card"] in cards:
                g = rules.gates_of(cards[a["orchestrator_card"]], a["rev"], a["plan_sha256"])
            for card in ((g or {}).get("final_e2e"), (g or {}).get("promotion")):
                if card and card not in cards:
                    shown = self._show(card)
                    if shown is not None:
                        cards[card] = shown
        atts = {}
        for card, role in want.items():
            if role == "request" and rules.status(cards.get(card)) == "done":
                atts[card] = [{"name": a.get("filename"), "sha256": a.get("sha256")} for a in self.attachments(card)]
        self.obs = engine.Observation(scan_started_ms=self.now, comments=dict(self.comments), cards=cards,
                                      attachments=atts, confirmed=frozenset(self.confirmed))

    def attachments(self, card):
        """``attachments --json`` of a card with the SHA-256 of each stored file (cached per scan)."""
        if card not in self._att:
            r = self.port.attachments(card)
            out = []
            for a in r.json if _ok(r) and isinstance(r.json, list) else ():
                if isinstance(a, dict):
                    try:
                        with open(a["stored_path"], "rb") as f:
                            data = f.read()
                    except (KeyError, OSError, TypeError):
                        continue
                    out.append(dict(a, sha256=_sha(data), data=data))
            self._att[card] = out
        return self._att[card]

    def close_cards(self):
        """Request cards of terminal tasks that are done/archived need no further ``show``."""
        closed = set(self.meta(CLOSED_CARDS_KEY, []))
        add = {card for card, task in self.c.execute(
            "SELECT card_id, task FROM cards WHERE role='request' AND task IN"
            " (SELECT clickup_task_id FROM tasks WHERE terminal=1)")
            if rules.status(self.obs.cards.get(card)) in ("done", "archived")}
        if add - closed:
            self.set_meta(CLOSED_CARDS_KEY, sorted(closed | add))

    # ------------------------------------------------------------------------------- §4.1 step 7
    def row(self, key):
        r = self.c.execute("SELECT * FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def state(self, key):
        r = self.c.execute("SELECT state FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return r[0] if r else None

    def update(self, key, **cols):
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET %s WHERE idem_key=?" % ", ".join(k + "=?" for k in cols),
                      [*cols.values(), key])
        if "state" in cols:
            self.log.info(json.dumps({"event": "outbox", "key": key, "state": cols["state"]}))
        return True

    def hold(self, task, reason, alert):
        self.alerts.append(alert)
        if task:
            self.store.observe_commit(task, [], {"hold_reason": reason})

    def execute(self, alerts_only=False):
        """Run outbox rows in order (revocation audit stamps first), dependencies and preconditions first."""
        for _ in range(50):
            keys = [r[0] for r in self.c.execute(
                "SELECT idem_key FROM outbox WHERE state NOT IN (%s) AND kind NOT LIKE 'clickup_%%'"
                " ORDER BY (idem_key LIKE '%%:stamp:%%:revoked') DESC, rowid" % ",".join("?" * len(FINAL)), FINAL)]
            progressed = False
            for key in keys:
                row = self.row(key)
                if alerts_only and row["kind"] != "alert":
                    continue
                progressed = self.step(row) or progressed
            if not progressed:
                break

    def step(self, row):
        key = row["idem_key"]
        if row["state"] in FINAL:
            return False
        deps = [self.state(d) for d in json.loads(row["depends_on"] or "[]")]
        if any(s in DEAD for s in deps):
            return self.update(key, state="cancelled_stale", outcome="DEPENDENCY")
        if any(s != "done" for s in deps):
            return False
        kind = row["kind"]
        if kind == "alert":
            if not self.precheck(row):
                return self.update(key, state="cancelled_stale")
            self.out.append(json.loads(row["body"])["text"])
            return self.update(key, state="done", outcome="OK")
        if kind in CU_KINDS:
            return self.clickup(row)
        if kind in KB_KINDS:
            return self.kanban(row)
        return False

    def precheck(self, row, drop_wait=False):
        """§10.1 recheck against this scan's state (``rules.preconditions_ok``)."""
        pre = Preconditions(**json.loads(row["preconditions"]))
        if drop_wait:
            pre = dataclasses.replace(pre, wait_run_id=None)
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (pre.task_id,)).fetchone()
        apps = {a["id"]: dict(a) for a in self.c.execute("SELECT * FROM approvals WHERE task=?", (pre.task_id,))}
        card = self.obs.cards.get(json.loads(row["body"] or "{}").get("card"))
        confirmed = self.cfg.list_id if pre.task_id in self.obs.confirmed else None
        return rules.preconditions_ok(dict(t) if t else None, apps, pre, row["kind"], card, confirmed)

    def send_gate(self, row, drop_wait=False):
        """Immediately before every send and retry: the §10.1 precondition recheck ('cancel'), the final
        reply-destination check ('foreign': a reply root must be an observed top-level comment of this task,
        §7.1/§8.3/§9), then the §4.4 barrier of a quiet-expired row ('defer': not sent, not an attempt)."""
        if not self.precheck(row, drop_wait):
            return "cancel"
        p = json.loads(row["body"] or "{}")
        tid = json.loads(row["preconditions"])["task_id"]
        if row["kind"] == "cu_reply" and not self.c.execute(
                "SELECT 1 FROM items WHERE task=? AND kind='comment' AND id=?", (tid, str(p.get("root")))).fetchone():
            return "foreign"
        t = self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()
        if not rules.send_barrier_ok(dict(t) if t else None, p, self.now, self.obs.scan_started_ms):
            return "defer"
        return "go"

    def refuse(self, row, gate):
        """Apply a non-'go' send gate to an S2 row and its ClickUp mirrors; a deferral changes nothing."""
        if gate == "defer":
            return False
        key = row["idem_key"]
        if gate == "foreign":
            tid = json.loads(row["preconditions"])["task_id"]
            self.alerts.append("ClickUp reply outbox %s targets a thread that is not an observed comment of task %s;"
                               " not sent" % (key, tid))
        with self.store.transaction() as c:
            c.execute("UPDATE outbox SET state='cancelled_stale', outcome=? WHERE idem_key IN (?,?,?) AND state NOT IN"
                      " (%s)" % ",".join("?" * len(FINAL)),
                      ("FOREIGN_ROOT" if gate == "foreign" else "STALE", key, key + "#cu", key + "#cu-att") + FINAL)
        return True

    # -- staging (transient, per outbox key)
    def stage_dir(self, key):
        d = os.path.join(self.staging, _sha(key)[:16])
        os.makedirs(d, exist_ok=True)
        return d

    def stage(self, key, name, data, overwrite=False):
        """Write ``data`` once; retries reuse the same bytes (nonce envelopes stay stable for readback)."""
        path = os.path.join(self.stage_dir(key), name)
        if overwrite or not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            os.replace(tmp, path)
        return path

    def sweep_staging(self):
        """Remove staging dirs whose outbox rows are resolved (§2.1 ``staging/`` is transient). A ``failed`` or
        ``held_ambiguous`` row of a live task can still be resent (§10.2, §11), so it keeps its rendered bytes
        until it is done or abandoned (R8-F1): files already attached must read back unchanged."""
        if not os.path.isdir(self.staging):
            return
        keys = [r[0] for r in self.c.execute(
            "SELECT idem_key FROM outbox WHERE state NOT IN (%s) OR (state IN ('failed', 'held_ambiguous') AND task"
            " IN (SELECT clickup_task_id FROM tasks WHERE terminal=0))" % ",".join("?" * len(FINAL)), FINAL)]
        # a kept orchestrator/gate context row keeps the shared snapshot staging alive (card_context), and a kept
        # relay comment/attach row keeps its relay's context bundle (context_files, staged under the unblock key)
        live = {_sha(k)[:16] for k in keys} | {_sha(shared_context_key(k))[:16] for k in keys} \
            | {_sha(k.rsplit(":", 1)[0] + ":unblock")[:16] for k in keys if ":relay:" in k}
        for name in os.listdir(self.staging):
            if name not in live:
                shutil.rmtree(os.path.join(self.staging, name), ignore_errors=True)

    # ------------------------------------------------------------------------- ClickUp effects (S1)
    def clickup(self, row):
        key, p = row["idem_key"], json.loads(row["body"])
        pre = Preconditions(**json.loads(row["preconditions"]))
        tid = pre.task_id
        mirrors = [k for k in (key + "#cu-att", key + "#cu") if self.state(k) is not None]
        if not mirrors:
            if pre.list_id is not None and tid not in self.obs.confirmed:
                return False  # home list not confirmed this scan: defer, do not cancel
            gate = self.send_gate(row)
            if gate != "go":
                return self.refuse(row, gate)
            built = self.build_clickup(row, p)
            if built is None:
                return False
            if isinstance(built, str):  # outbound filter rejection (§9)
                self.update(key, state="failed", outcome="FILTERED")
                self.hold(tid, "outbound_filter", built)
                return True
            with self.store.transaction() as c:
                for mkey, kind, target, body in built:
                    c.execute("INSERT OR IGNORE INTO outbox(idem_key, task, kind, target, payload_sha256, body,"
                              " preconditions, depends_on) VALUES (?,?,?,?,?,?,?,?)",
                              (mkey, tid, kind, target, _sha(json.dumps(body, sort_keys=True)),
                               json.dumps(body, sort_keys=True), row["preconditions"], "[]"))
            mirrors = [m[0] for m in built]
        cur = next((m for m in mirrors if self.state(m) != "done"), mirrors[-1])
        mrow = self.row(cur)
        if mrow["state"] in ("pending", "retry_wait") and pre.list_id is not None and tid not in self.obs.confirmed:
            return False
        if mrow["state"] == "pending" or (mrow["state"] == "retry_wait" and (mrow["next_attempt_ms"] or 0) <= self.now):
            gate = self.send_gate(row)  # the next step is a send or retry
            if gate in ("defer", "foreign"):
                return self.refuse(row, gate)
        before = (mrow["state"], mrow["attempts"], mrow["next_attempt_ms"])
        if mrow["state"] not in FINAL:
            res = clickup_effects.execute(self.guard.transport(self.inner, task=tid), self.store, self.cfg, cur,
                                          self.now, lambda _r: self.precheck(row))
            self.alerts.extend(res.alerts)
        mrow = self.row(cur)
        if mrow["state"] == "done" and cur != mirrors[-1]:
            return True  # next mirror (the comment after its long-text attachment) runs in the next pass
        self.update(key, state=mrow["state"], outcome=mrow["outcome"], attempts=mrow["attempts"],
                    next_attempt_ms=mrow["next_attempt_ms"], first_send_ms=mrow["first_send_ms"],
                    result_ref=mrow["result_ref"])
        return (mrow["state"], mrow["attempts"], mrow["next_attempt_ms"]) != before

    def build_clickup(self, row, p):
        """Mirror rows (key, S1 kind, target, body) for one S2 ClickUp row; None defers; str = filter alert."""
        key, kind, tid = row["idem_key"], row["kind"], p["task"]
        mkey = key + "#cu"
        if kind == "cu_status":
            return [(mkey, "clickup_status", tid, {"status": p["status"]})]
        if kind == "cu_attach":
            data = self.card_file(p["card"], p["attachment"], p["sha256"])
            if data is None:
                self.alerts.append("plan %s for task %s is not readable on card %s; publication deferred"
                                   % (p["attachment"], tid, p["card"]))
                return None
            if render.check_outbound(data.decode("utf-8", "replace"), self.secrets):
                return "ClickUp outbound filter hold: task %s (plan attachment); nothing was posted" % tid
            rev = int(re.match(r"plan-r(\d+)-", p["filename"]).group(1))
            name = render.plan_attachment_name(rev, p["sha256"], mkey)
            return [(mkey, "clickup_attachment", tid, {"filename": name, "path": self.stage(mkey, name, data)})]
        text = p["text"]
        if "summary_from" in p:
            src = p["summary_from"]
            data = self.card_file(src["card"], src["attachment"], None)
            if data is None:
                return None
            text = text.replace("{summary}", plan_summary(data.decode("utf-8", "replace")))
        post = render.prepare_clickup_post(text, mkey, task_id=tid, secrets=self.secrets)
        if not post.ok:
            return post.alert
        mk, target = ("clickup_reply", p["root"]) if kind == "cu_reply" else ("clickup_comment", tid)
        if post.attachment_name is None:
            return [(mkey, mk, target, {"text": post.comment})]
        akey = key + "#cu-att"
        aname = clickup_effects.attachment_name(post.attachment_name, akey)
        apath = self.stage(akey, aname, post.attachment_bytes)
        return [(akey, "clickup_attachment", tid, {"filename": aname, "path": apath}),
                (mkey, mk, target, {"text": post.comment.replace(post.attachment_name, aname)})]

    def card_file(self, card, name, sha):
        for a in self.attachments(card):
            if a.get("filename") == name and (sha is None or a["sha256"] == sha):
                return a["data"]
        return None

    def plan_bytes(self, task, sha):
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC",
                                    (task,)).fetchall():
            for a in self.attachments(card):
                if a["sha256"] == sha:
                    return a["data"]
        return None

    # -------------------------------------------------------------------------- Kanban effects (S3)
    def kanban(self, row):
        key, kind, p = row["idem_key"], row["kind"], json.loads(row["body"])
        if row["state"] in ("retry_wait", "ambiguous") and (row["next_attempt_ms"] or 0) > self.now:
            return False
        gate = self.send_gate(row, drop_wait=kind == "kb_unblock")
        if gate != "go":
            return self.refuse(row, gate)
        intent = self.kb_intent(row, p)
        if intent is None:
            return False
        possibly = row["state"] in ("sending", "ambiguous")
        prior = (row["child_pid"], row["child_start_time"]) if possibly else None
        attempts = row["attempts"] + 1
        self.update(key, state="sending", attempts=attempts, first_send_ms=row["first_send_ms"] or self.now)
        self.port.on_spawn = lambda pid, st: self.update(key, child_pid=pid, child_start_time=st)
        try:
            if kind == "kb_unblock" and p.get("expect_status") == "blocked":
                res = self.release(intent.target, prior)
            elif kind == "kb_attach" and p.get("template") == "context" and not p.get("card_role"):
                res = self.attach_all(intent, prior)
            else:
                res = kp.run_kanban_effect(self.port, intent, prior_child=prior)
        finally:
            self.port.on_spawn = None
        st = res.state
        if st is RowState.DONE:
            ref = res.json.get("id") if kind == "kb_create" and isinstance(res.json, dict) else None
            return self.update(key, state="done", outcome="OK", result_ref=ref)
        if st is RowState.CANCELLED_STALE:
            return self.update(key, state="cancelled_stale", outcome="STALE")
        if st is RowState.SENDING:  # the earlier child is still alive: nothing sent, decide next scan
            return self.update(key, attempts=row["attempts"])
        if attempts >= MAX_ATTEMPTS:
            self.update(key, state="failed", outcome=st.value)
            self.hold(json.loads(row["preconditions"])["task_id"], "outbox:" + key,
                      "Kanban %s outbox %s failed after %d attempts; task on hold" % (kind, key, attempts))
            return True
        nxt = self.now + KB_BACKOFF_MIN[attempts - 1] * 60_000 if st is RowState.RETRY_WAIT else self.now
        return self.update(key, state=st.value, outcome=st.value, next_attempt_ms=nxt)

    def release(self, card, prior=None):
        """Initial release of a blocked, never-run own card (§8.1 request pickup, §8.4 step 3): ``unblock``
        then a ready/todo/running readback. A card already past ``blocked`` was released earlier. While the
        recorded child of an earlier attempt is alive nothing is sent (§10.2); after it exits, the card is read."""
        if prior is not None and kp.child_alive(*prior):
            return kp.EffectResult(RowState.SENDING)
        shown = self.port.show(card)
        if not _ok(shown):
            return kp.EffectResult(RowState.RETRY_WAIT)
        status, runs = kp.card_status(shown.json), (shown.json.get("runs") or [])
        if status in (None, "archived", "triage"):
            return kp.EffectResult(RowState.CANCELLED_STALE, shown.json)
        if status != "blocked" or runs:
            return kp.EffectResult(RowState.DONE, shown.json)
        res = self.port.unblock(card)
        if not _ok(res):
            return kp.EffectResult(RowState.AMBIGUOUS if res.outcome is Outcome.AMBIGUOUS else RowState.RETRY_WAIT)
        back = self.port.show(card)
        landed = _ok(back) and kp.card_status(back.json) in ("ready", "todo", "running")
        return kp.EffectResult(RowState.DONE if landed else RowState.AMBIGUOUS, back.json)

    def attach_all(self, intent, prior):
        """§8.1/§8.2 context step: context snapshot, ledger, latest passed plan and staged files, each read back."""
        for name, path in intent.payload["files"]:
            one = EffectIntent(intent.idem_key, kp.ATTACH, intent.target, {"path": path, "name": name},
                               intent.preconditions)
            res = kp.run_kanban_effect(self.port, one, prior_child=prior or (None, None))
            if res.state is not RowState.DONE:
                return res
        return kp.EffectResult(RowState.DONE)

    def kb_intent(self, row, p):
        """S3 ``EffectIntent`` for one S2 Kanban row (payload rendering; None defers)."""
        key, kind, card = row["idem_key"], row["kind"], row["target"]
        pre = Preconditions(**json.loads(row["preconditions"]))
        tid = pre.task_id
        if kind == "kb_create":
            body = self.card_body(tid, p)
            if body is None:
                return None
            payload = {"title": p["title"], "body_file": self.stage(key, "body.md", body),
                       "idempotency_key": p["idempotency_key"], "assignee": p.get("assignee"),
                       "skills": p.get("skills") or [], "initial_status": p.get("initial_status", "blocked")}
        elif kind == "kb_comment":
            if p.get("template") == "relay":
                body = self.relay_body(row, p)
                if body is None:
                    return None
            else:
                body = p["text"]
            payload = {"body": body}
        elif kind == "kb_attach":
            if p.get("card_role"):  # §8.2 on the orchestrator/gate: exactly one file per row
                path = self.card_context(row, p)
                if path is None:
                    return None
                payload = {"path": path, "name": p["name"]}
            elif p.get("template") == "context":
                files = self.context_files(row, p)
                if files is None:
                    return None
                payload = {"files": files}
            elif p.get("template") == "approval_record":
                a = self.c.execute("SELECT * FROM approvals WHERE id=?", (pre.approval_id,)).fetchone()
                q = p["params"]
                text = render.approval_record(q["task"], q["rev"], q["sha256"], q["accounts_gen"], a["gate_card"], {
                    "author_name": q["author_name"], "author_id": q["author_id"], "comment_id": q["comment_id"],
                    "comment_date_ms": q["comment_date"]}, self.cfg.auto_gate)
                payload = {"path": self.stage(key, p["name"], text), "name": p["name"]}
            else:
                data = self.card_file(p["source_card"], p["attachment"], p["sha256"])
                if data is None:
                    self.alerts.append("plan %s is not readable on card %s; handoff deferred"
                                       % (p["attachment"], p["source_card"]))
                    return None
                payload = {"path": self.stage(key, p["name"], data), "name": p["name"]}
        elif kind == "kb_unblock":
            line = None
            for d in json.loads(row["depends_on"] or "[]"):
                if d.endswith(":comment"):
                    line = json.loads(self.row(d)["body"]).get("text")
            payload = {"relay_line": line}
        elif kind == "kb_complete":
            payload = {"summary": p["summary"], "metadata": p.get("metadata")}
        else:
            payload = {}
        return EffectIntent(key, KB_KINDS[kind], card, payload, pre, ())

    # -- rendering
    def approval_view(self, approval_id):
        a = dict(self.c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
        return a, {"author_name": a["author_name"], "author_id": a["author_id"], "comment_id": a["comment_id"],
                   "comment_date_ms": a["comment_date"]}

    def latest_pass(self, tid):
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC",
                                    (tid,)).fetchall():
            ev = rules.pass_evidence(self.obs.cards.get(card), self.obs.attachments.get(card), tid)
            if ev:
                return card, ev
        return None

    def card_body(self, tid, p):
        q = p["params"]
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        if p["template"] == "request_card":
            last = self.latest_pass(tid)
            rev = max(last[1]["rev"] if last else 0, t["published_rev"] or 0)
            return render.request_card_body(tid, q["seq"], t["input_gen"], rev + 1, q.get("previous_card"),
                                            "clickup-plan-r%d.md" % last[1]["rev"] if last else None)
        a, view = self.approval_view(q["approval_id"])
        if p["template"] == "gate_card":
            return render.gate_card_body(tid, q["rev"], q["sha256"], a["accounts_gen"], view, self.cfg.auto_gate)
        data = self.plan_bytes(tid, q["sha256"])
        if data is None:
            self.alerts.append("plan r%d bytes for task %s are not readable; handoff deferred" % (q["rev"], tid))
            return None
        prev = None
        if q.get("lineage"):
            last = q["lineage"][-1]
            prev = dict(last.get("clickup_gates") or {}, gate=last.get("gate_card"))
        return render.orchestrator_card_body(tid, q["rev"], "clickup-plan-r%d.md" % q["rev"], len(data), q["sha256"],
                                             a["accounts_gen"], q["gate"], view, self.cfg.auto_gate, prev)

    def read_snapshot(self, tid, d):
        """Complete current task content (GET Task, every comment page, replies of threaded roots); None defers.

        The reads are resumable (QSR F3): progress is kept in ``d/snapshot.json`` and continues in later scans
        within this task's per-scan request share (§4.5), so a supported history larger than one scan's
        budget still completes, untruncated. Partial progress is bound to the task's current quiet window:
        any newly observed activity moves ``quiet_until_ms`` and restarts the reads, so a completed snapshot
        was read entirely after the last activity (§4.4). The progress file is removed once complete."""
        prog = os.path.join(d, "snapshot.json")
        quiet = self.c.execute("SELECT quiet_until_ms FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone()[0] or 0
        st = None
        if os.path.exists(prog):
            with open(prog, encoding="utf-8") as f:
                st = json.load(f)
        if not st or st["quiet"] != quiet:
            st = {"quiet": quiet, "task": None, "roots": [], "pages": 0, "next": None, "paged": False, "replies": {}}
        t, complete = self.guard.transport(self.inner, task=tid), False
        try:
            if st["task"] is None:
                r = t.request("GET", "/v2/task/%s" % tid, params={"include_markdown_description": "true"})
                if not _ok(r) or not isinstance(r.json, dict):
                    return None
                st["task"] = r.json
            while not st["paged"]:
                if st["pages"] >= self.cfg.budgets["comment_pages_cap"]:
                    return None
                params = {} if st["next"] is None else {"start": st["next"][0], "start_id": st["next"][1]}
                r = t.request("GET", "/v2/task/%s/comment" % tid, params=params)
                if not _ok(r) or not isinstance(r.json, dict) or not isinstance(r.json.get("comments"), list):
                    return None
                page = [cm for cm in r.json["comments"] if isinstance(cm, dict)]
                st["roots"] += page
                st["pages"] += 1
                if len(r.json["comments"]) < COMMENT_PAGE:
                    st["paged"] = True
                else:
                    st["next"] = [page[-1].get("date"), page[-1].get("id")]
            for cm in st["roots"]:
                if int(cm.get("reply_count") or 0) > 0 and str(cm["id"]) not in st["replies"]:
                    r = t.request("GET", "/v2/comment/%s/reply" % cm["id"])
                    if not _ok(r) or not isinstance(r.json, dict) or not isinstance(r.json.get("comments"), list):
                        return None
                    st["replies"][str(cm["id"])] = [x for x in r.json["comments"] if isinstance(x, dict)]
            complete = True
            return st["task"], st["roots"], st["replies"]
        finally:
            if complete:
                if os.path.exists(prog):
                    os.remove(prog)
            else:
                tmp = prog + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f)
                os.replace(tmp, prog)

    def snapshot(self, tid, gen, snap, staged):
        """(§8.2 context snapshot dict, item views) from a complete ``read_snapshot`` result."""
        task, roots, replies = snap
        comments = []
        for raw in sorted(roots, key=lambda x: (observe._int(x.get("date"), 0), str(x.get("id")))):
            comments.append(self._item_view(tid, "comment", raw, None))
            for rep in replies.get(str(raw["id"]), []):
                comments.append(self._item_view(tid, "reply", rep, str(raw["id"])))
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        return {"task_id": tid, "gen": gen, "name": task.get("name"),
                "description": task.get("markdown_description") or task.get("description"),
                "requester_id": t["requester_id"], "requester_name": self.cfg.approvers.get(t["requester_id"]),
                "attachments": [{"id": s["id"], "title": s["title"], "size": s["size"], "author_id": s["user_id"],
                                 "date_ms": observe._int(s["date"]), "staged": s["staged"]} for s in staged],
                "comments": comments, "notices": []}, comments

    def card_context(self, row, p):
        """§8.2 file for an orchestrator/gate ``kb_attach`` row (``card_role``): ``clickup-ledger-g<G>.md`` or the
        complete ``clickup-context-g<G>.md`` snapshot (attachment metadata only; one file per row). Both cards
        of one approval share the generation's snapshot, staged under the gate's context row key and kept until
        both rows are final (``sweep_staging``)."""
        tid, gen = p["task"], p["gen"]
        if p["template"] == "ledger":
            return self.stage(row["idem_key"], p["name"], self.ledger(tid))
        skey = shared_context_key(row["idem_key"])
        d = self.stage_dir(skey)
        path = os.path.join(d, p["name"])
        if not os.path.exists(path):
            snap = self.read_snapshot(tid, d)
            if snap is None:
                return None
            meta = [{"id": str(x.get("id")), "title": x.get("title"), "size": x.get("size"),
                     "user_id": (x.get("user") or {}).get("id"), "date": x.get("date"), "staged": None}
                    for x in snap[0].get("attachments") or [] if isinstance(x, dict)]
            self.stage(skey, p["name"], render.context_file(self.snapshot(tid, gen, snap, meta)[0]))
        return path

    def _item_view(self, tid, kind, raw, parent):
        it = self.c.execute("SELECT * FROM items WHERE task=? AND kind=? AND id=?", (tid, kind, str(raw["id"]))).fetchone()
        parsed = parse_comment(raw.get("comment"), raw.get("comment_text"), self.cfg.plain_attr_allowlist)
        cr = (it["command_result"] if it else None) or ""
        uid = (raw.get("user") or {}).get("id")
        return {"kind": kind, "id": str(raw["id"]), "parent_id": parent, "author_id": uid,
                "date_ms": observe._int(raw.get("date")), "addressed": self.cfg.bot_user_id in parsed.mentions,
                "shape": parsed.shape, "closure": cr[len("closure_"):] if cr.startswith("closure_") else None,
                "text": raw.get("comment_text") or ""}

    def relay_tag(self, att, ctx_name):
        """Name suffix for one relay's ledger, plan copy and staged files on its request card (E2E-F2): the
        installed CLI stores a repeated attachment name as ``name (n).ext``, so the exact-name readback (§10.2)
        needs names no other relay on the card uses. The card's first relay keeps the plain names its body
        references; any later relay (an earlier relay attach row exists for the card) uses its own context
        file's stem, ``-g<G>[-b<k>]``. Decided by outbox insertion order, so it is stable across retries."""
        earlier = att and self.c.execute(
            "SELECT 1 FROM outbox WHERE kind='kb_attach' AND target=? AND idem_key LIKE '%:relay:%:attach'"
            " AND rowid < (SELECT rowid FROM outbox WHERE idem_key=?)", (att["target"], att["idem_key"])).fetchone()
        return "-" + os.path.splitext(ctx_name)[0].replace("clickup-context-", "", 1) if earlier else ""

    def context_files(self, row, p):
        """[(name, path)] for the request-card context step, rendered once per relay (stable bytes). The context
        file carries the relay attach row's payload ``name`` (a re-staged first wake gets a new, never overwritten
        name) and the relay comment names that same file. The ledger, plan copy and staged files are attached
        under this relay's own names (``relay_tag``), and the context file names the staged files that way."""
        tid, gen = p["task"], p["gen"]
        base = row["idem_key"].rsplit(":", 1)[0]
        ctx_key = base + ":unblock"
        att = self.row(base + ":attach")
        name = (json.loads(att["body"]).get("name") if att else None) or render.context_name(gen)
        tag = self.relay_tag(att, name)

        def own(n):
            root, ext = os.path.splitext(n)
            return root + tag + ext
        d = self.stage_dir(ctx_key)
        ctx = os.path.join(d, name)
        ledger = os.path.join(d, "clickup-ledger.md")
        files = []
        if not os.path.exists(ctx):
            snap = self.read_snapshot(tid, d)
            if snap is None:
                return None
            atts = [a for a in snap[0].get("attachments") or [] if isinstance(a, dict)]
            staged = clickup_api.stage_attachments(atts, d, self.cfg.bot_user_id, opener=self.opener)
            snapshot, comments = self.snapshot(tid, gen, snap, [dict(s, staged=s["staged"] and own(s["staged"]))
                                                                 for s in staged])
            # the context file is written last: its presence means the whole bundle is complete
            self.stage(ctx_key, "items.json", json.dumps(comments), overwrite=True)
            self.stage(ctx_key, "staged.json", json.dumps([s["staged"] for s in staged]), overwrite=True)
            self.stage(ctx_key, "clickup-ledger.md", self.ledger(tid), overwrite=True)
            self.stage(ctx_key, name, render.context_file(snapshot))
        files.append((name, ctx))
        files.append((own("clickup-ledger.md"), ledger))
        last = self.latest_pass(tid)
        if last:
            data = self.card_file(last[0], last[1]["attachment"], last[1]["sha256"])
            if data is None:
                return None
            name = own("clickup-plan-r%d.md" % last[1]["rev"])
            files.append((name, self.stage(row["idem_key"], name, data)))
        with open(os.path.join(d, "staged.json"), encoding="utf-8") as f:
            for s in json.load(f):
                if s:
                    files.append((own(s), os.path.join(d, s)))
        return files

    def ledger(self, tid):
        t = dict(self.c.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (tid,)).fetchone())
        revs = []
        for card, in self.c.execute("SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq",
                                    (tid,)).fetchall():
            ev = rules.pass_evidence(self.obs.cards.get(card), self.obs.attachments.get(card), tid)
            if ev:
                revs.append({"rev": ev["rev"], "plan_sha256": ev["sha256"], "pass_ref": card,
                             "published": ev["rev"] == t["published_rev"] and ev["sha256"] == t["published_sha256"]})
        apps = [dict(a) for a in self.c.execute("SELECT * FROM approvals WHERE task=? ORDER BY id", (tid,))]
        gates = next((json.loads(a["clickup_gates"]) for a in reversed(apps) if a["clickup_gates"]), None)
        return render.ledger_file({
            "task_id": tid, "phase": t["phase"], "input_gen": t["input_gen"], "classified_gen": t["classified_gen"],
            "scope_gen": t["scope_gen"], "published_rev": t["published_rev"], "revisions": revs,
            "approvals": [{"id": a["id"], "rev": a["rev"], "author_name": a["author_name"],
                           "comment_date_ms": a["comment_date"], "state": a["state"]} for a in apps],
            "clickup_gates": gates})

    def relay_body(self, row, p):
        """``CLICKUP-IN v1`` relay comment (§8.2), rendered once with the relay's context file SHA-256."""
        path = os.path.join(self.stage_dir(row["idem_key"]), "body.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        files = self.context_files(row, p)
        if files is None:
            return None
        d = os.path.dirname(files[0][1])
        with open(files[0][1], "rb") as f:
            ctx_sha = _sha(f.read())
        with open(os.path.join(d, "items.json"), encoding="utf-8") as f:
            known = {(i["kind"], i["id"]): i for i in json.load(f)}
        closures = {tuple(x) for x in p.get("closures") or []}
        items = []
        for kind, iid in p.get("items") or []:
            view = dict(known.get((kind, iid)) or {"kind": kind, "id": iid, "text": "(not present in the snapshot)"})
            if (kind, iid) in closures and not view.get("closure"):
                view["closure"] = "yes"
            items.append(view)
        body = render.relay_comment(p["relay"], p["gen"], items, files[0][0], ctx_sha, p.get("note"))
        self.stage(row["idem_key"], "body.md", body)
        with open(path, encoding="utf-8") as f:
            return f.read()


def shared_context_key(key):
    """Staging key of the snapshot shared by an approval's orchestrator and gate context rows for one gen:
    the gate row's key (``clickup-dp:<task>:gate:r<N>:g<A>:attach:context:g<G>``). Other keys map to themselves."""
    if ":attach:context:" not in key:
        return key
    return key.replace(":orch:", ":gate:", 1)


def plan_summary(text, cap=2000):
    """The ``## ClickUp summary`` section of a plan revision (§8.1), at most ``cap`` characters."""
    m = re.search(r"^## ClickUp summary[ \t]*\r?\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return (m.group(1).strip() if m else "")[:cap]


# ------------------------------------------------------------------------------------------- operator
def _lock(runtime_dir):
    """Exclusive ``msvcrt.locking`` on ``intake.lock``; None when another instance holds it."""
    import msvcrt
    os.makedirs(runtime_dir, exist_ok=True)
    f = open(os.path.join(runtime_dir, "intake.lock"), "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def _logger(runtime_dir):
    os.makedirs(os.path.join(runtime_dir, "logs"), exist_ok=True)
    log = logging.Logger("clickup_intake.run")
    h = logging.handlers.RotatingFileHandler(os.path.join(runtime_dir, "logs", "intake.jsonl"),
                                             maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8")
    h.setFormatter(logging.Formatter('{"ts": "%(asctime)s", "line": %(message)s}'))
    log.addHandler(h)
    return log


def hold_task(store, task, now):
    """``hold <task>``: operator hold; a live ticket is revoked in the same transaction (§8.5, §11)."""
    with store.transaction() as c:
        if c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (task,)).rowcount != 1:
            return False
        revoke_live_tickets(c, task, "operator", now)
    return True


def release_task(store, task):
    with store.transaction() as c:
        return c.execute("UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?", (task,)).rowcount == 1


def outbox_command(store, action, key):
    """``outbox resend|abandon <id>`` for a held_ambiguous/failed row (S2 key or its ``#cu`` mirror)."""
    base = key.split("#", 1)[0]
    keys = [k for k in (base, base + "#cu-att", base + "#cu")]
    rows = {k: store.conn.execute("SELECT task, state FROM outbox WHERE idem_key=?", (k,)).fetchone() for k in keys}
    rows = {k: r for k, r in rows.items() if r}
    if not rows or not any(r["state"] in ("held_ambiguous", "failed") for r in rows.values()):
        return False
    task = next(iter(rows.values()))["task"]
    with store.transaction() as c:
        for k, r in rows.items():
            if r["state"] == "done":
                continue
            if action == "resend":
                c.execute("UPDATE outbox SET state='pending', outcome=NULL, attempts=0, next_attempt_ms=NULL,"
                          " child_pid=NULL, child_start_time=NULL WHERE idem_key=?", (k,))
            else:
                c.execute("UPDATE outbox SET state='cancelled_stale', outcome='ABANDONED' WHERE idem_key=?", (k,))
        c.execute("UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=? AND hold_reason IN (%s)"
                  % ",".join("?" * len(keys)), [task] + ["outbox:" + k for k in keys])
    return True


def status_lines(state_path):
    conn = open_readonly(state_path)
    try:
        phases = conn.execute("SELECT COALESCE(phase, '-'), COUNT(*) FROM tasks GROUP BY 1 ORDER BY 1").fetchall()
        holds = conn.execute("SELECT COUNT(*) FROM tasks WHERE hold_reason IS NOT NULL AND hold_reason<>''").fetchone()
        rows = conn.execute("SELECT state, COUNT(*) FROM outbox WHERE kind NOT LIKE 'clickup_%' GROUP BY 1 ORDER BY 1"
                            ).fetchall()
        tickets = conn.execute("SELECT COUNT(*) FROM approvals WHERE state='ticketed'").fetchone()
        incident = conn.execute("SELECT 1 FROM meta WHERE key=?", (PREFLIGHT_KEY,)).fetchone()
    finally:
        conn.close()
    return ["tasks: " + (", ".join("%s=%d" % (p, n) for p, n in phases) or "none") + "; holds=%d" % holds[0],
            "outbox: " + (", ".join("%s=%d" % (s, n) for s, n in rows) or "empty") + "; live tickets=%d" % tickets[0],
            "preflight incident: " + ("open" if incident else "none")]


def ticket_check(cfg, port, gate, task, rev, sha, now):
    """§8.5 read-only verdict: local state snapshot (read-only URI) + ``show --json`` of the gate."""
    snapshot = load_ticket_snapshot(cfg.state_path, task, cfg.ticket_lease_seconds)
    shown = port.show(gate)
    return ticket_verdict(snapshot, shown.json if _ok(shown) else "", task, rev, sha, gate, now)


def _parser():
    p = argparse.ArgumentParser(prog="clickup_dedicated_panel_intake")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=("shadow",))
    sub.add_parser("status")
    for name in ("admit", "hold", "release"):
        sub.add_parser(name).add_argument("task")
    ob = sub.add_parser("outbox")
    ob.add_argument("action", choices=("resend", "abandon"))
    ob.add_argument("id")
    tc = sub.add_parser("ticket-check")
    tc.add_argument("--gate", required=True)
    tc.add_argument("--task", required=True)
    tc.add_argument("--rev", required=True, type=int)
    tc.add_argument("--sha", required=True)
    return p


def default_root():
    """The Hermes root of this installation: ``<root>/scripts/clickup_intake/main.py``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None, root=None, *, transport=None, port=None, clock=None, opener=None, out=None):
    args = _parser().parse_args(argv)
    out = out or sys.stdout
    clock = clock or (lambda: int(time.time() * 1000))
    config_path = os.path.join(root or default_root(), *CONFIG_REL)

    def say(*lines):
        for line in lines:
            out.write(line + "\n")
        out.flush()

    try:
        cfg = load_config(config_path)
    except (OSError, ValueError):
        if args.cmd == "ticket-check":
            say("INVALID config_unavailable")
            return 3
        say("ClickUp intake: config unavailable or invalid")
        return 2
    port = port or kp.KanbanCli(HERMES_EXE, cfg.board, discord_chat=cfg.discord_chat)

    if args.cmd == "ticket-check":
        verdict = ticket_check(cfg, port, args.gate, args.task, args.rev, args.sha, clock())
        say(str(verdict))
        return 0 if verdict.valid else 3
    if args.cmd == "status":
        try:
            say(*status_lines(cfg.state_path))
        except Exception:
            say("state unavailable")
            return 2
        return 0
    if args.cmd == "run" and not cfg.enabled:
        return 0

    lock = _lock(cfg.runtime_dir)
    if lock is None:
        if args.cmd != "run":
            say("busy: another clickup-intake instance holds the lock; retry")
            return 1
        return 0
    try:
        store = Store(cfg.state_path, clock)
        try:
            now = clock()
            if args.cmd == "hold":
                ok = hold_task(store, args.task, now)
                say("held" if ok else "unknown task")
                return 0 if ok else 1
            if args.cmd == "release":
                ok = release_task(store, args.task)
                say("released" if ok else "unknown task")
                return 0 if ok else 1
            if args.cmd == "outbox":
                ok = outbox_command(store, args.action, args.id)
                say(args.action + (" done" if ok else ": no held_ambiguous/failed row with that id"))
                return 0 if ok else 1
            secrets = ()
            if transport is None:
                token = clickup_api.load_token(CREDENTIAL)
                transport, secrets = clickup_api.HttpTransport(token), (token,)
            log = _logger(cfg.runtime_dir)
            try:
                glue = Glue(cfg, store, transport, port, now, secrets=secrets, opener=opener, log=log)
                if args.cmd == "admit":
                    if not glue.preflight():
                        glue.finish()
                        say(*glue.out)
                        return 1
                    ok, why = observe.admit_task(glue.guard.transport(transport), store, cfg, args.task, now)
                    say(*glue.out, ("admitted" if ok else "refused: " + why))
                    return 0 if ok else 1
                try:
                    glue.scan(shadow=args.mode == "shadow" or cfg.mode == "shadow")
                except Exception as e:  # never echo content; class name only
                    log.info(json.dumps({"event": "error", "class": e.__class__.__name__}))
                    say(*glue.out, "ClickUp intake run failed (%s); see logs" % e.__class__.__name__)
                    return 1
                say(*glue.out)
                return 0
            finally:
                for h in list(log.handlers):
                    h.close()
                    log.removeHandler(h)
        finally:
            store.close()
    finally:
        lock.close()
