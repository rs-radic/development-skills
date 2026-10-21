"""ClickUp observation (S1, plan r5 §4, §5.1-5.2, §6 admission, §11 admit).

Status-set preflight, discovery pass P, light/full task passes, due/fairness scheduling and the
dispatch-barrier record. Every task pass ends in exactly one ``Store.observe_commit``.

Barrier record (§4.4), written only here and read by the engine through ``barrier_confirmed``:
- ``tasks.full_started_ms`` = start time of the latest *completed* full pass F (an F in progress keeps
  its own start in ``full_cursor``);
- ``tasks.barrier_ok_ms`` = set to ``full_started_ms`` by a clean light pass L that ran after that F
  completed; cleared by any new activity, which also moves ``quiet_until_ms`` past it;
- ``tasks.last_light_ms`` = time of the latest completed L.
"""

import hashlib
import json
from dataclasses import dataclass, field

from .clickup_api import ApiGuard
from .contracts import Item, Outcome, Phase
from .parse import parse_comment

COMMENT_PAGE = 25
NO_READ_PHASES = {Phase.BASELINE.value, Phase.NOT_ADMITTED.value, Phase.DEPLOYED.value, Phase.STOPPED.value}
ACTIVE_PHASES = {p.value for p in (Phase.PLANNING, Phase.IN_REVIEW, Phase.AWAITING_BUSINESS, Phase.AWAITING_TECHNICAL,
                                   Phase.AWAITING_APPROVAL, Phase.REVISING, Phase.APPROVAL_QUEUED, Phase.DEVELOPMENT,
                                   Phase.IMPLEMENTED)}


# ------------------------------------------------------------------------------------- preflight
@dataclass(frozen=True)
class PreflightResult:
    """``OK``, ``MISMATCH`` (exact diff) or ``ERROR`` (request failed / unparseable). Only OK may poll."""
    status: str
    missing: tuple = ()
    unexpected: tuple = ()
    outcome: object = None

    @property
    def ok(self):
        return self.status == "OK"

    def describe(self):
        """Names/types only (no task content), for the deduped operator alert."""
        if self.status == "ERROR":
            return f"status-set preflight failed ({getattr(self.outcome, 'value', self.outcome)})"
        parts = [f"missing {n}/{t}" for n, t in self.missing] + [f"unexpected {n}/{t}" for n, t in self.unexpected]
        return "status-set preflight " + self.status + ("" if not parts else ": " + ", ".join(parts))


def preflight_statuses(transport, config):
    """§4.1 step 2: GET the target list and compare its live (status, type) set by exact string equality."""
    r = transport.request("GET", f"/v2/list/{config.list_id}")
    if r.outcome != Outcome.OK:
        return PreflightResult("ERROR", outcome=r.outcome)
    statuses = r.json.get("statuses") if isinstance(r.json, dict) else None
    if not isinstance(statuses, list):
        return PreflightResult("ERROR", outcome=Outcome.AMBIGUOUS)
    live = set()
    for s in statuses:
        if not (isinstance(s, dict) and isinstance(s.get("status"), str) and isinstance(s.get("type"), str)):
            return PreflightResult("ERROR", outcome=Outcome.AMBIGUOUS)
        live.add((s["status"], s["type"]))
    expected = set(tuple(p) for p in config.expected_statuses)
    if live == expected:
        return PreflightResult("OK")
    return PreflightResult("MISMATCH", missing=tuple(sorted(expected - live)), unexpected=tuple(sorted(live - expected)))


# ------------------------------------------------------------------------------------- helpers
def _int(v, default=None):
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _status_name(task):
    st = task.get("status")
    return st.get("status") if isinstance(st, dict) else None


def admissible(task, config):
    """§4.2 code predicate: home list is the target, not archived, no parent, literal status in intake_statuses."""
    lst = task.get("list") if isinstance(task, dict) else None
    return (isinstance(lst, dict) and str(lst.get("id")) == str(config.list_id)
            and task.get("archived") is False and not task.get("parent")
            and _status_name(task) in config.intake_statuses)


def list_fp(task):
    return _sha([task.get("name"), task.get("description"), _status_name(task), str(task.get("date_updated"))])


def content_hash(task):
    desc = task.get("markdown_description")
    if desc is None:
        desc = task.get("description")
    return _sha([task.get("name"), desc])


def _closed_names(config):
    return {n for n, t in config.expected_statuses if t == "closed"}


def _done_names(config):
    return {n for n, t in config.expected_statuses if t == "done"}


def _row(store, task_id):
    r = store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def barrier_confirmed(task_row, now_ms, anchor_ms=0):
    """§4.4 for scan time ``now_ms``: quiet expired, an F that started at or after max(quiet_until, anchor)
    completed, and a clean L later in this same scan. Any activity clears it."""
    if not task_row or task_row.get("terminal") or task_row.get("hold_reason"):
        return False
    if task_row.get("phase") in NO_READ_PHASES:
        return False
    quiet_until = task_row.get("quiet_until_ms") or 0
    ok = task_row.get("barrier_ok_ms")
    return (ok is not None and now_ms >= quiet_until and ok >= max(quiet_until, anchor_ms or 0)
            and task_row.get("last_light_ms") == now_ms)


# ------------------------------------------------------------------------------------- discovery
@dataclass
class DiscoveryResult:
    complete: bool = False
    pages: int = 0
    admitted: list = field(default_factory=list)
    hints: dict = field(default_factory=dict)  # tracked task id -> list_fp from the listing
    alerts: list = field(default_factory=list)
    outcome: object = None


def discover(transport, store, config, now_ms, filtered=True):
    """§4.2 discovery pass P: relist intake-status tasks (no watermark), admit new ones, hint tracked ones."""
    res = DiscoveryResult()
    params = {"archived": "false", "subtasks": "false", "order_by": "created"}
    if filtered:
        params["statuses[]"] = list(config.intake_statuses)
    else:
        params["include_closed"] = "false"  # A6 fallback: the code predicate alone decides
    max_pages = config.budgets["discovery_max_pages"]
    page = 0
    while True:
        if res.pages >= max_pages:
            res.alerts.append(f"ClickUp discovery exceeded {max_pages} pages; pass incomplete")
            break
        r = transport.request("GET", f"/v2/list/{config.list_id}/task", params={**params, "page": page})
        if r.outcome != Outcome.OK or not isinstance(r.json, dict) or not isinstance(r.json.get("tasks"), list):
            res.outcome = r.outcome
            break
        res.pages += 1
        for t in r.json["tasks"]:
            if isinstance(t, dict):
                _discovered(store, config, t, now_ms, res)
        if r.json.get("last_page") is True:
            res.complete = True
            break
        page += 1
    return res


def _discovered(store, config, t, now, res):
    tid = str(t.get("id"))
    row = _row(store, tid)
    ok = admissible(t, config)
    if row is None:
        if not ok:
            return  # no row, no further reads
        creator = _int((t.get("creator") or {}).get("id"))
        created = _int(t.get("date_created"))
        if creator is None or created is None or creator == config.bot_user_id:
            return
        fields = {"requester_id": creator, "last_status_seen": _status_name(t), "list_fp": list_fp(t)}
        if config.cutover_ms is None or created < config.cutover_ms:
            fields.update(phase=Phase.BASELINE.value, eligible=0)
        else:
            fields.update(phase=Phase.NEW_QUIET.value, eligible=1, quiet_until_ms=created + config.quiet_seconds * 1000)
            res.admitted.append(tid)
        store.observe_commit(tid, [], fields)
    elif row["phase"] == Phase.NOT_ADMITTED.value and ok and not row["terminal"]:
        store.observe_commit(tid, [], {"phase": Phase.NEW_QUIET.value, "eligible": 1, "last_status_seen": _status_name(t),
                                       "quiet_until_ms": now + config.quiet_seconds * 1000, "last_full_ms": None,
                                       "full_started_ms": None, "full_cursor": None, "barrier_ok_ms": None})
        res.admitted.append(tid)
    elif not row["terminal"] and row["phase"] not in NO_READ_PHASES:
        res.hints[tid] = list_fp(t)


# ------------------------------------------------------------------------------------- task passes
@dataclass
class PassResult:
    task_id: str
    kind: str  # L | F
    complete: bool = False
    activity: bool = False
    terminal: bool = False
    not_admitted: bool = False
    hold: object = None
    alerts: list = field(default_factory=list)


class _Pass:
    def __init__(self, transport, store, config, task_id, now, kind):
        self.t, self.store, self.cfg, self.now = transport, store, config, now
        self.row = _row(store, task_id) or {}
        self.res = PassResult(task_id, kind)
        self.id = task_id
        self.known = {(r["kind"], r["id"]): dict(r)
                      for r in store.conn.execute("SELECT * FROM items WHERE task=?", (task_id,))}
        self.out = {}
        self.fields = {}

    # -- reads
    def get(self, path, params=None):
        r = self.t.request("GET", path, params=params)
        return r if r.outcome == Outcome.OK and isinstance(r.json, dict) else None, r

    def task(self):
        """GET Task; returns False when the pass must stop (failure, terminal, not admitted)."""
        ok, r = self.get(f"/v2/task/{self.id}", {"include_markdown_description": "true"})
        if ok is None:
            if r.status == 404:
                self._terminal(Phase.STOPPED)
            return False
        task = ok.json
        self.fields.update(last_task_get_ms=self.now, last_status_seen=_status_name(task), list_fp=list_fp(task))
        if self.row.get("phase") == Phase.NEW_QUIET.value:
            if not admissible(task, self.cfg):  # admission recheck while no card exists
                self.fields["phase"] = Phase.NOT_ADMITTED.value
                self.res.not_admitted = True
                self.commit()
                return False
        else:
            st = task.get("status") or {}
            closed = st.get("status") in _closed_names(self.cfg) or st.get("type") == "closed"
            lst = task.get("list")
            moved = isinstance(lst, dict) and lst.get("id") is not None and str(lst["id"]) != self.cfg.list_id
            if closed or task.get("archived") is True or moved:  # terminal only on positive evidence
                self._terminal(Phase.DEPLOYED if closed and not task.get("archived") else Phase.STOPPED)
                return False
        ch = content_hash(task)
        if self.row.get("content_hash") is not None and self.row["content_hash"] != ch:
            self._activity()
        self.fields["content_hash"] = ch
        self._attachments(task.get("attachments") or [])
        return True

    def _terminal(self, phase):
        self.fields.update(terminal=1, phase=phase.value, last_task_get_ms=self.now)
        self.res.terminal = True
        self.commit()

    def _attachments(self, atts):
        bot, seen, human_visible = self.cfg.bot_user_id, set(), []
        for a in atts:
            if not isinstance(a, dict) or a.get("id") is None:
                continue
            aid, user = str(a["id"]), _int((a.get("user") or {}).get("id"))
            human, visible = user != bot, not a.get("deleted") and not a.get("hidden")
            seen.add(aid)
            prev = self.known.get(("attachment", aid))
            h = _sha([a.get("title"), a.get("size")])
            if visible:
                if human:
                    human_visible.append(aid)
                if prev is None or prev["deleted"]:
                    self._emit(Item("attachment", aid, None, user, _int(a.get("date")), h, material=human))
                    if human:
                        self._activity()
            elif prev is not None and not prev["deleted"]:
                self._remove_attachment(prev)
        for (kind, aid), prev in self.known.items():
            if kind == "attachment" and aid not in seen and not prev["deleted"]:
                self._remove_attachment(prev)
        self.fields["attach_ids_hash"] = _sha(sorted(human_visible))

    def _remove_attachment(self, prev):
        human = prev["author_id"] != self.cfg.bot_user_id
        self._emit(Item("attachment", prev["id"], None, prev["author_id"], prev["date_ms"], prev["cur_hash"],
                        deleted=True, material=human))
        if human:
            self._activity()

    def comment_page(self, start=None, start_id=None):
        params = {} if start is None else {"start": start, "start_id": start_id}
        ok, _ = self.get(f"/v2/task/{self.id}/comment", params)
        if ok is None or not isinstance(ok.json.get("comments"), list):
            return None
        return ok.json["comments"]

    def replies(self, root_id):
        """GET replies of one root; stores changes and detects reply deletions. False on failure."""
        ok, _ = self.get(f"/v2/comment/{root_id}/reply")
        if ok is None or not isinstance(ok.json.get("comments"), list):
            return False
        seen = set()
        for raw in ok.json["comments"]:
            if not self.comment(raw, "reply", root_id):
                return False
            seen.add(str(raw["id"]))
        for (kind, rid), prev in self.known.items():
            if kind == "reply" and prev["parent_id"] == root_id and rid not in seen and not prev["deleted"]:
                self._delete_comment(prev)
        return True

    # -- items
    def comment(self, raw, kind, parent_id, reply_count=None):
        """Observe one comment/reply; False (task on hold) if it does not have the comment schema (A4)."""
        if not (isinstance(raw, dict) and raw.get("id") is not None and isinstance(raw.get("user"), dict)
                and _int(raw["user"].get("id")) is not None and isinstance(raw.get("comment"), list)
                and isinstance(raw.get("comment_text"), str)):
            self._hold("schema", f"ClickUp {kind} schema drift on task {self.id}; task on hold")
            return False
        cid, author = str(raw["id"]), _int(raw["user"]["id"])
        parsed = parse_comment(raw["comment"], raw["comment_text"], self.cfg.plain_attr_allowlist)
        mentions = self.cfg.bot_user_id in parsed.mentions
        h = _sha([raw["comment"], raw["comment_text"]])
        human = author != self.cfg.bot_user_id
        prev = self.known.get((kind, cid))
        if reply_count is None:
            reply_count = prev["reply_count"] if prev else 0
        material = False
        if prev is None or prev["deleted"]:
            changed, material = True, human and mentions
        elif prev["cur_hash"] != h:
            changed, material = True, human and mentions and not prev["cur_mentions_bot"]
        else:
            changed = False
        if changed and human:
            self._activity()
        if changed or (prev is not None and prev["reply_count"] != reply_count):
            self._emit(Item(kind, cid, parent_id, author, _int(raw.get("date")), h, mentions_bot=mentions,
                            shape=parsed.shape, shape_sig=parsed.shape_sig, reply_count=reply_count,
                            material=material))
        return True

    def set_reply_count(self, cid, n):
        key = ("comment", cid)
        if key in self.out:
            it = self.out[key]
            self.out[key] = Item(it.kind, it.id, it.parent_id, it.author_id, it.date_ms, it.hash, it.mentions_bot,
                                 it.shape, it.shape_sig, n, it.deleted, it.material)
        elif key in self.known and self.known[key]["reply_count"] != n:
            p = self.known[key]
            self._emit(Item("comment", cid, p["parent_id"], p["author_id"], p["date_ms"], p["cur_hash"],
                            bool(p["cur_mentions_bot"]), p["shape"], p["shape_sig"], n))

    def _delete_comment(self, prev):
        human = prev["author_id"] != self.cfg.bot_user_id
        self._emit(Item(prev["kind"], prev["id"], prev["parent_id"], prev["author_id"], prev["date_ms"],
                        prev["cur_hash"], bool(prev["cur_mentions_bot"]), prev["shape"], prev["shape_sig"],
                        prev["reply_count"], deleted=True, material=human and bool(prev["cur_mentions_bot"])))
        if human:
            self._activity()

    def _emit(self, item):
        self.out[(item.kind, item.id)] = item

    def _activity(self):
        self.res.activity = True
        self.fields.update(quiet_until_ms=self.now + self.cfg.quiet_seconds * 1000, barrier_ok_ms=None)

    def _hold(self, reason, alert):
        self.fields["hold_reason"] = reason
        self.res.hold = reason
        self.res.alerts.append(alert)

    def commit(self):
        self.store.observe_commit(self.id, list(self.out.values()), self.fields)
        return self.res


def light_pass(transport, store, config, task_id, now_ms):
    """§4.3 L: GET Task, newest comment page (older pages only until a known id), replies of roots whose
    ``reply_count`` changed. Ends in one observe_commit."""
    p = _Pass(transport, store, config, task_id, now_ms, "L")
    if not p.task():
        return p.res
    known_roots = {i for k, i in p.known if k == "comment"}
    start = start_id = None
    pages, roots = 0, []
    while True:
        page = p.comment_page(start, start_id)
        if page is None:
            return p.commit()
        pages += 1
        hit = False
        for raw in page:
            if not p.comment(raw, "comment", None):
                return p.commit()
            cid = str(raw["id"])
            hit = hit or cid in known_roots
            prev = p.known.get(("comment", cid))
            rc = _int(raw.get("reply_count"), 0)
            if rc != (prev["reply_count"] if prev else 0):
                roots.append((cid, rc))
        if hit or len(page) < COMMENT_PAGE:
            break
        if pages >= config.budgets["comment_pages_cap"]:
            p._hold("caps", f"ClickUp task {task_id} exceeds {config.budgets['comment_pages_cap']} comment pages; on hold")
            return p.commit()
        last = page[-1]
        start, start_id = _int(last.get("date")), str(last.get("id"))
    for cid, rc in roots:
        if not p.replies(cid):
            return p.commit()
        p.set_reply_count(cid, rc)
    p.res.complete = True
    p.fields["last_light_ms"] = now_ms
    if not p.res.activity and p.row.get("full_started_ms") is not None:
        p.fields["barrier_ok_ms"] = p.row["full_started_ms"]
    return p.commit()


def full_pass(transport, store, config, task_id, now_ms):
    """§4.3 F: GET Task, every comment page, replies of every threaded root; resumable via ``full_cursor``.
    Root deletions are detected only when the F completes. Ends in one observe_commit."""
    p = _Pass(transport, store, config, task_id, now_ms, "F")
    cur = json.loads(p.row["full_cursor"]) if p.row.get("full_cursor") else \
        {"started_ms": now_ms, "start": None, "start_id": None, "pages": 0, "seen": [], "roots": [], "paged": False}
    if not p.task():
        return p.res
    b = config.budgets

    def save():
        p.fields["full_cursor"] = json.dumps(cur)
        return p.commit()

    while not cur["paged"]:
        page = p.comment_page(cur["start"], cur["start_id"])
        if page is None:
            return save()
        for raw in page:
            if not p.comment(raw, "comment", None):
                return save()
        cur["pages"] += 1
        for raw in page:
            cid = str(raw["id"])
            cur["seen"].append(cid)
            rc, prev = _int(raw.get("reply_count"), 0), p.known.get(("comment", cid))
            if rc > 0 or (prev is not None and prev["reply_count"] > 0):  # a drop to 0 still needs the reply check
                cur["roots"].append([cid, rc])
            else:
                p.set_reply_count(cid, 0)
        if len(page) < COMMENT_PAGE:
            cur["paged"] = True
        else:
            cur["start"], cur["start_id"] = _int(page[-1].get("date")), str(page[-1].get("id"))
        if cur["pages"] > b["comment_pages_cap"] or len(cur["roots"]) > b["threaded_roots_cap"]:
            p._hold("caps", f"ClickUp task {task_id} exceeds the comment page/thread caps; on hold")
            p.fields["full_cursor"] = None
            return p.commit()
    while cur["roots"]:
        cid, rc = cur["roots"][0]
        if not p.replies(cid):
            return save()
        p.set_reply_count(cid, rc)
        cur["roots"].pop(0)
    seen = set(cur["seen"])
    for (kind, cid), prev in p.known.items():
        if kind == "comment" and cid not in seen and not prev["deleted"] \
                and (prev["first_seen_ms"] or 0) < cur["started_ms"]:
            p._delete_comment(prev)
    p.res.complete = True
    p.fields.update(full_cursor=None, last_full_ms=now_ms, full_started_ms=cur["started_ms"])
    return p.commit()


# ------------------------------------------------------------------------------------- admit
def admit_task(transport, store, config, task_id, now_ms):
    """§11 ``admit <task>``: admit a pre-cutover task only if its GET Task passes ``admissible()``."""
    row = _row(store, task_id)
    if row is not None and row["phase"] not in (Phase.BASELINE.value, None):
        return False, f"already tracked ({row['phase']})"
    r = transport.request("GET", f"/v2/task/{task_id}", params={"include_markdown_description": "true"})
    if r.outcome != Outcome.OK or not isinstance(r.json, dict):
        return False, "task unavailable"
    if not admissible(r.json, config):
        return False, f"not admissible (status {_status_name(r.json)!r})"
    store.observe_commit(task_id, [], {
        "phase": Phase.NEW_QUIET.value, "eligible": 1, "requester_id": _int((r.json.get("creator") or {}).get("id")),
        "last_status_seen": _status_name(r.json), "list_fp": list_fp(r.json),
        "quiet_until_ms": now_ms + config.quiet_seconds * 1000, "last_full_ms": None, "full_started_ms": None,
        "full_cursor": None, "barrier_ok_ms": None})
    return True, "admitted"


# ------------------------------------------------------------------------------------- scheduling
@dataclass
class ScanObservation:
    discovery: DiscoveryResult
    passes: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    deferred: bool = False


def _cadence(row, config, live):
    iv = config.intervals
    phase = row["phase"]
    if live or phase == Phase.PROMOTION_WINDOW.value:
        return 0, iv["promotion_full_seconds"]
    if phase == Phase.NEW_QUIET.value:
        return 0, None
    if phase in ACTIVE_PHASES:
        return iv["active_light_seconds"], iv["active_full_seconds"]
    if phase == Phase.READY_FOR_DEPLOYMENT.value or row["last_status_seen"] in _done_names(config):
        return iv["late_light_seconds"], iv["late_full_seconds"]
    return iv["active_light_seconds"], iv["active_full_seconds"]


def _due(last_ms, seconds, now):
    return seconds is not None and (last_ms is None or now - last_ms >= seconds * 1000)


def observe_scan(transport, store, config, now_ms, filtered=True, guard=None):
    """§4.1 steps 3-4 after a passing preflight: discovery P, then L/F passes in §4.5 priority order within
    the budget (preflight is counted in the same ``guard``)."""
    guard = guard or ApiGuard(store, config, now_ms)
    disc = discover(guard.transport(transport), store, config, now_ms, filtered)
    obs = ScanObservation(disc, alerts=list(disc.alerts))
    live = {r[0] for r in store.conn.execute("SELECT DISTINCT task FROM approvals WHERE state='ticketed'")}
    if store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fences'").fetchone():
        # r11 §3.3: an open or releasing fence joins the live cadence; r12 §3.3: parks read no ClickUp data.
        live |= {r[0] for r in store.conn.execute(
            "SELECT DISTINCT task FROM fences WHERE kind='fence' AND state IN ('open', 'releasing')")}
    rows = [dict(r) for r in store.conn.execute(
        "SELECT * FROM tasks WHERE terminal=0 AND (hold_reason IS NULL OR hold_reason='') AND phase IS NOT NULL")
        if r["phase"] not in NO_READ_PHASES]

    plan = []  # (priority, sort key, task id, ops)
    for row in rows:
        tid, now = row["clickup_task_id"], now_ms
        light_s, full_s = _cadence(row, config, tid in live)
        quiet_until = row["quiet_until_ms"] or 0
        need_f = row["last_full_ms"] is None or _due(row["last_full_ms"], full_s, now)
        need_l = (now < quiet_until or disc.hints.get(tid, row["list_fp"]) != row["list_fp"]
                  or _due(row["last_light_ms"], light_s, now))
        barrier = now >= quiet_until and (row["barrier_ok_ms"] is None or row["barrier_ok_ms"] < quiet_until)
        if tid in live:
            plan.append((3, 0, tid, ["L"] + (["F"] if need_f else [])))
        elif row["last_full_ms"] is None or barrier:
            f_first = row["last_full_ms"] is None or (row["full_started_ms"] or 0) < quiet_until \
                or row["full_cursor"] is not None
            ops = (["F"] if f_first else []) + (["L"] if barrier or not f_first else [])
            plan.append((4, quiet_until, tid, ops))
        else:
            if need_l:
                plan.append((5, row["last_light_ms"] or 0, tid, ["L"]))
            if need_f:
                plan.append((6, row["last_full_ms"] or 0, tid, ["F"]))
    plan.sort(key=lambda p: (p[0], p[1], p[2]))

    full_done = set()
    for _, _, tid, ops in plan:
        for op in ops:
            if guard.exhausted:
                obs.deferred = True
                break
            if op == "L" and tid in full_done and len(ops) == 1:
                continue  # an F this scan already covered the due L
            fn = full_pass if op == "F" else light_pass
            res = fn(guard.transport(transport, task=tid), store, config, tid, now_ms)
            obs.passes.append(res)
            obs.alerts.extend(res.alerts)
            if not res.complete:
                if not (res.terminal or res.not_admitted or res.hold):
                    obs.deferred = True
                break
            if op == "F":
                full_done.add(tid)
                if res.activity:
                    break  # quiet restarted: a barrier L now cannot confirm anything
    guard.note_deferred(obs.deferred)
    obs.alerts.extend(guard.alerts)
    return obs
