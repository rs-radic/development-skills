"""ClickUp write effects and uncertain-write recovery (S1, plan r5 §9 write scope, §10.1-10.2).

Outbox rows for ClickUp kinds (written by the executor before the call, body = JSON payload):
- ``clickup_comment``    target = task id,          body ``{"text": ...}``
- ``clickup_reply``      target = root comment id,  body ``{"text": ...}`` (``task`` = task id)
- ``clickup_status``     target = task id,          body ``{"status": ...}``
- ``clickup_attachment`` target = task id,          body ``{"filename": ..., "path": <staged file>}``

Every comment/reply carries the footer ``ref: cu-<sha12(idem_key)>`` and every upload name contains
``<ref6>``, so an ambiguous write can be found again. Ambiguous comment/reply/attachment writes are
never resent automatically; an idempotent status PUT is verified by GET and may be retried.
"""

import hashlib
import json
from dataclasses import dataclass, field

from .contracts import Outcome, RowState

KINDS = ("clickup_comment", "clickup_reply", "clickup_status", "clickup_attachment")
SEND_BACKOFF_MIN = (1, 2, 5, 10, 30, 30)
MAX_ATTEMPTS = 6
RECONCILE_AT_MIN = (2, 10, 30)
_FINAL = {RowState.DONE.value, RowState.CANCELLED_STALE.value, RowState.FAILED.value, RowState.HELD_AMBIGUOUS.value}


def effect_ref(idem_key):
    return "cu-" + hashlib.sha256(idem_key.encode("utf-8")).hexdigest()[:12]


def with_footer(text, idem_key):
    foot = "ref: " + effect_ref(idem_key)
    return text if foot in text else f"{text}\n\n{foot}"


def attachment_name(filename, idem_key):
    ref6 = effect_ref(idem_key)[3:9]
    if ref6 in filename:
        return filename
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}-{ref6}.{ext}" if dot else f"{filename}-{ref6}"


@dataclass
class EffectResult:
    state: str
    alerts: list = field(default_factory=list)


def due_rows(store, now_ms):
    """ClickUp outbox rows to execute now: pending, crashed mid-send, or retry/reconcile time reached."""
    marks = ",".join("?" * len(KINDS))
    return [r[0] for r in store.conn.execute(
        f"SELECT idem_key FROM outbox WHERE kind IN ({marks}) AND (state IN ('pending', 'sending')"
        " OR (state IN ('retry_wait', 'ambiguous') AND (next_attempt_ms IS NULL OR next_attempt_ms <= ?)))"
        " ORDER BY rowid", (*KINDS, now_ms))]


class _Effect:
    def __init__(self, transport, store, config, key, now, precheck):
        self.t, self.store, self.cfg, self.key, self.now, self.precheck = transport, store, config, key, now, precheck
        self.row = self._load()
        self.payload = json.loads(self.row["body"] or "{}")
        self.alerts = []

    def _load(self):
        r = self.store.conn.execute("SELECT * FROM outbox WHERE idem_key=?", (self.key,)).fetchone()
        if r is None or r["kind"] not in KINDS:
            raise ValueError(f"not a ClickUp outbox row: {self.key}")
        return dict(r)

    def update(self, **cols):
        with self.store.transaction() as c:
            c.execute(f"UPDATE outbox SET {', '.join(k + '=?' for k in cols)} WHERE idem_key=?",
                      [*cols.values(), self.key])
        self.row.update(cols)
        return EffectResult(self.row["state"], self.alerts)

    def hold(self, state, alert, **cols):
        self.alerts.append(alert)
        res = self.update(state=state, **cols)
        if self.row["task"]:
            self.store.observe_commit(self.row["task"], [], {"hold_reason": f"outbox:{self.key}"})
        return res

    # -- send
    def send(self):
        kind, p = self.row["kind"], self.payload
        if not self.precheck(self.row):
            return self.update(state=RowState.CANCELLED_STALE.value)
        if kind == "clickup_status":
            allowed = {n for n, t in self.cfg.expected_statuses if t != "closed"}
            if p.get("status") not in allowed:
                return self.hold(RowState.FAILED.value, f"ClickUp status write refused for outbox {self.key} "
                                 "(not a writable mapped status); task on hold", outcome="REFUSED")
        attempts = self.row["attempts"] + 1
        prev_state = self.row["state"]
        self.update(state=RowState.SENDING.value, attempts=attempts, next_attempt_ms=self.now,
                    first_send_ms=self.row["first_send_ms"] or self.now)
        r = self._request()
        if r is None:
            return self.hold(RowState.FAILED.value, f"ClickUp {kind} outbox {self.key}: staged file unavailable; "
                             "task on hold", outcome="DEFINITE_FAIL")
        if r.outcome == Outcome.OK and (kind in ("clickup_reply", "clickup_status")
                                        or (isinstance(r.json, dict) and r.json.get("id") is not None)):
            ref = "" if kind in ("clickup_reply", "clickup_status") else str(r.json["id"])
            return self.update(state=RowState.DONE.value, outcome="OK", result_ref=ref)
        if r.outcome in (Outcome.RATE_LIMITED, Outcome.AUTH_FAIL):  # rejected or never sent: not applied
            return self.update(state=prev_state, attempts=attempts - 1, outcome=r.outcome.value)
        if r.outcome == Outcome.DEFINITE_FAIL:
            if attempts >= MAX_ATTEMPTS:
                return self.hold(RowState.FAILED.value, f"ClickUp {kind} outbox {self.key} failed after "
                                 f"{attempts} attempts; task on hold", outcome="DEFINITE_FAIL")
            return self.update(state=RowState.RETRY_WAIT.value, outcome="DEFINITE_FAIL",
                               next_attempt_ms=self.now + SEND_BACKOFF_MIN[attempts - 1] * 60_000)
        # AMBIGUOUS (or OK without the expected id): it may have committed
        if kind == "clickup_status":
            return self.verify_status()
        return self.update(state=RowState.AMBIGUOUS.value, outcome=f"AMBIGUOUS@{self.now}",
                           next_attempt_ms=self.now + RECONCILE_AT_MIN[0] * 60_000)

    def _request(self):
        kind, p, target = self.row["kind"], self.payload, self.row["target"]
        if kind == "clickup_comment":
            return self.t.request("POST", f"/v2/task/{target}/comment",
                                  body={"comment_text": with_footer(p["text"], self.key), "notify_all": False})
        if kind == "clickup_reply":
            return self.t.request("POST", f"/v2/comment/{target}/reply",
                                  body={"comment_text": with_footer(p["text"], self.key), "notify_all": False})
        if kind == "clickup_status":
            return self.t.request("PUT", f"/v2/task/{target}", body={"status": p["status"]})
        try:
            with open(p["path"], "rb") as f:
                content = f.read()
        except (OSError, KeyError, TypeError):
            return None
        return self.t.request("POST", f"/v2/task/{target}/attachment",
                              multipart={"filename": attachment_name(p["filename"], self.key), "content": content})

    # -- reconcile
    def verify_status(self):
        r = self.t.request("GET", f"/v2/task/{self.row['target']}")
        if r.outcome != Outcome.OK or not isinstance(r.json, dict):
            return self.update(state=RowState.AMBIGUOUS.value, outcome=f"AMBIGUOUS@{self.now}",
                               next_attempt_ms=self.now)
        if (r.json.get("status") or {}).get("status") == self.payload.get("status"):
            return self.update(state=RowState.DONE.value, outcome="OK", result_ref="")
        return self.update(state=RowState.RETRY_WAIT.value, outcome="NOT_APPLIED", next_attempt_ms=self.now)

    def reconcile(self):
        kind = self.row["kind"]
        if kind == "clickup_status":
            return self.verify_status()
        found, complete = self._find()
        if not complete:
            return EffectResult(self.row["state"], self.alerts)  # read deferred (budget/failure): try later
        if found is not None:
            return self.update(state=RowState.DONE.value, outcome="OK", result_ref=found)
        sent = _sent_ms(self.row)
        for minutes in RECONCILE_AT_MIN:
            if sent + minutes * 60_000 > self.now:
                return self.update(next_attempt_ms=sent + minutes * 60_000)
        return self.hold(RowState.HELD_AMBIGUOUS.value,
                         f"ClickUp {kind} outbox {self.key} still ambiguous after 30 min; not resent; task on hold "
                         f"(operator: outbox resend|abandon {self.key})")

    def _find(self):
        """(result_ref or None, read complete?) for the bot-authored write carrying this row's ref."""
        kind, target, bot = self.row["kind"], self.row["target"], self.cfg.bot_user_id
        foot = "ref: " + effect_ref(self.key)

        def mine(c):
            return isinstance(c, dict) and _uid(c) == bot and foot in str(c.get("comment_text", ""))
        if kind == "clickup_attachment":
            r = self.t.request("GET", f"/v2/task/{target}")
            if r.outcome != Outcome.OK or not isinstance(r.json, dict):
                return None, False
            ref6 = effect_ref(self.key)[3:9]
            for a in r.json.get("attachments") or []:
                if isinstance(a, dict) and ref6 in str(a.get("title", "")) and _uid(a) in (bot, None):
                    return str(a.get("id")), True
            return None, True
        if kind == "clickup_reply":
            r = self.t.request("GET", f"/v2/comment/{target}/reply")
            if r.outcome != Outcome.OK or not isinstance(r.json, dict):
                return None, False
            hit = next((c for c in r.json.get("comments") or [] if mine(c)), None)
            return (str(hit["id"]) if hit else None), True
        params = {}
        for _ in range(self.cfg.budgets["comment_pages_cap"]):  # complete walk of the task's comments
            r = self.t.request("GET", f"/v2/task/{target}/comment", params=params)
            if r.outcome != Outcome.OK or not isinstance(r.json, dict) or not isinstance(r.json.get("comments"), list):
                return None, False
            page = r.json["comments"]
            hit = next((c for c in page if mine(c)), None)
            if hit:
                return str(hit["id"]), True
            if len(page) < 25:
                return None, True
            params = {"start": page[-1].get("date"), "start_id": page[-1].get("id")}
        return None, False


def _uid(obj):
    user = obj.get("user")
    try:
        return int(user.get("id")) if isinstance(user, dict) else None
    except (TypeError, ValueError):
        return None


def _sent_ms(row):
    out = row.get("outcome") or ""
    if out.startswith("AMBIGUOUS@"):
        try:
            return int(out.split("@", 1)[1])
        except ValueError:
            pass
    return row.get("next_attempt_ms") or row.get("first_send_ms") or 0


def execute(transport, store, config, idem_key, now_ms, precheck):
    """Advance one ClickUp outbox row by at most one send or one reconciliation step.

    ``precheck(row) -> bool`` is the executor's precondition recheck against this scan's state (§10.1);
    it runs before every send and retry. Returns the row's new state and any alerts.
    """
    e = _Effect(transport, store, config, idem_key, now_ms, precheck)
    state = e.row["state"]
    if state in _FINAL:
        return EffectResult(state)
    if state == RowState.SENDING.value:  # the process died during the call: it may have committed
        sent = e.row["next_attempt_ms"] or e.row["first_send_ms"] or now_ms
        e.update(state=RowState.AMBIGUOUS.value, outcome=f"AMBIGUOUS@{sent}",
                 next_attempt_ms=sent if e.row["kind"] == "clickup_status" else sent + RECONCILE_AT_MIN[0] * 60_000)
        state = RowState.AMBIGUOUS.value
    if (e.row["next_attempt_ms"] or 0) > now_ms and state != RowState.PENDING.value:
        return EffectResult(state)
    if state == RowState.AMBIGUOUS.value:
        return e.reconcile()
    return e.send()
