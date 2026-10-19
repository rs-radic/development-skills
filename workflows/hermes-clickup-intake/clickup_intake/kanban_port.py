"""Kanban side of the glue (plan r5 §8, §10.2, §10.3).

``KanbanCli`` implements the S0 ``KanbanPort`` over the installed ``hermes kanban`` CLI, always with
``--board <board>`` and the ``clickup-intake`` author. The pure helpers read ``show --json`` /
``attachments --json`` / ``notify-list --json`` for exact readback, the wake guard, PASS evidence and
``clickup_gates``. ``run_kanban_effect`` executes one Kanban outbox effect with readback-before-resend.
"""

import ctypes
import hashlib
import json
import os
import re
import subprocess
from typing import Any, NamedTuple, Optional

from .contracts import KanbanResult, Outcome, RowState

AUTHOR = "clickup-intake"
DISCORD_GUILD_ID = "222222222222222222"
NOTIFIER_PROFILE = "default"
DELIVERY_MODE = "notify"  # D3: passive only, never wake
CHAT_TYPE = "group"
MAX_COMMENT_CHARS = 30000  # stays under the Windows 32,767-character command line

CREATE, COMMENT, ATTACH, UNBLOCK = "kanban_create", "kanban_comment", "kanban_attach", "kanban_unblock"
COMPLETE, ARCHIVE, SUBSCRIBE = "kanban_complete", "kanban_archive", "kanban_subscribe"
KINDS = (CREATE, COMMENT, ATTACH, UNBLOCK, COMPLETE, ARCHIVE, SUBSCRIBE)

_CARD_ID = re.compile(r"^t_[0-9a-f]{8,}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------- child processes
class RunOutput(NamedTuple):
    rc: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False


def process_start_time(pid):
    """Creation time of a live process as a string, or None when it is gone/unknown (Windows)."""
    if os.name != "nt" or not pid:
        return None
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k32.CloseHandle(handle)


def child_alive(pid, start_time):
    """True only if ``pid`` is running AND is the same process (creation time) as recorded."""
    return bool(pid and start_time) and process_start_time(pid) == str(start_time)


def subprocess_runner(argv, env, timeout, on_spawn):
    """Run one CLI child; ``on_spawn(pid, start_time)`` is called before waiting (outbox journaling)."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, encoding="utf-8", errors="replace")
    if on_spawn:
        on_spawn(proc.pid, process_start_time(proc.pid))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return RunOutput(None, out or "", err or "", True)
    return RunOutput(proc.returncode, out or "", err or "")


# --------------------------------------------------------------------------- CLI port
class KanbanCli:
    """``KanbanPort`` over ``<hermes_exe> kanban --board <board> ...``.

    Outcomes: launch failure or a CLI error exit -> DEFINITE_FAIL; timeout, a crash traceback or an
    unparseable ``--json`` result -> AMBIGUOUS (readback decides); exit 0 -> OK.
    """

    def __init__(self, hermes_exe, board, *, discord_chat, env=None, timeout=120, runner=None):
        self.hermes_exe = str(hermes_exe)
        self.board = board
        self.discord_chat = str(discord_chat)
        self.env = env
        self.timeout = timeout
        self.runner = runner or subprocess_runner
        self.on_spawn = None  # set by the executor to record child pid + start time

    def _run(self, args, json_out=False):
        env = None
        if self.env is not None:
            env = dict(self.env)
            env.setdefault("PYTHONIOENCODING", "utf-8")
        argv = [self.hermes_exe, "kanban", "--board", self.board] + list(args)
        try:
            out = self.runner(argv, env, self.timeout, self.on_spawn)
        except OSError:
            return KanbanResult(Outcome.DEFINITE_FAIL)
        if out.timed_out or out.rc is None:
            return KanbanResult(Outcome.AMBIGUOUS)
        if out.rc != 0:
            ambiguous = "Traceback (most recent call last)" in out.stderr
            return KanbanResult(Outcome.AMBIGUOUS if ambiguous else Outcome.DEFINITE_FAIL, {"error": out.stderr.strip()})
        if not json_out:
            return KanbanResult(Outcome.OK, {"stdout": out.stdout.strip()})
        try:
            return KanbanResult(Outcome.OK, json.loads(out.stdout))
        except ValueError:
            return KanbanResult(Outcome.AMBIGUOUS)

    def create(self, title, *, body_file, idempotency_key, assignee=None, skills=(), initial_status="blocked"):
        args = ["create", "--body-file", str(body_file), "--idempotency-key", idempotency_key,
                "--created-by", AUTHOR, "--initial-status", initial_status]
        if assignee:
            args += ["--assignee", assignee]
        for skill in skills:
            args += ["--skill", skill]
        return self._run(args + ["--json", "--", title], json_out=True)

    def comment(self, card_id, body):
        if len(body) > MAX_COMMENT_CHARS:
            return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "comment too long"})
        return self._run(["comment", card_id, "--author", AUTHOR, "--", body])

    def attach(self, card_id, path, name):
        return self._run(["attach", card_id, str(path), "--name", name, "--author", AUTHOR])

    def attachments(self, card_id):
        return self._run(["attachments", card_id, "--json"], json_out=True)

    def show(self, card_id):
        return self._run(["show", card_id, "--json"], json_out=True)

    def unblock(self, card_id):
        return self._run(["unblock", card_id])

    def complete(self, card_id, summary, metadata=None):
        args = ["complete", card_id, "--summary=" + summary]
        if metadata is not None:
            args.append("--metadata=" + json.dumps(metadata))
        return self._run(args)

    def archive(self, card_id):
        return self._run(["archive", card_id])

    def subscribe(self, card_id):
        """D3: passive notify subscription of an integration-created card to the configured Discord channel."""
        return self._run(["notify-subscribe", card_id, "--platform", "discord", "--chat-id", self.discord_chat,
                          "--guild-id", DISCORD_GUILD_ID, "--chat-type", CHAT_TYPE,
                          "--notifier-profile", NOTIFIER_PROFILE, "--delivery-mode", DELIVERY_MODE])

    def subscriptions(self, card_id):
        return self._run(["notify-list", card_id, "--json"], json_out=True)


# --------------------------------------------------------------------------- readback helpers (show --json etc.)
def card_status(show):
    try:
        return show["task"]["status"]
    except (KeyError, TypeError):
        return None


def _runs(show):
    runs = show.get("runs") if isinstance(show, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def latest_run(show):
    runs = _runs(show)
    return max(runs, key=lambda r: r.get("id") or 0) if runs else None


def latest_run_id(show):
    run = latest_run(show)
    return run.get("id") if run else None


def parked_wait_run_id(show):
    """The parked run id when the card is ``scheduled`` and its latest run ended ``scheduled`` (§10.3)."""
    run = latest_run(show)
    if card_status(show) == "scheduled" and run and run.get("outcome") == "scheduled":
        return run.get("id")
    return None


def _comments(show):
    comments = show.get("comments") if isinstance(show, dict) else None
    return [c for c in comments if isinstance(c, dict)] if isinstance(comments, list) else []


# --------------------------------------------------------------------------- question relay readers (exported fields)
def _events(show):
    events = show.get("events") if isinstance(show, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def open_run_ids(show):
    """Runs without ``ended_at``. ``show --json`` exports no ``current_run_id``; native ``_end_run`` sets
    ``ended_at`` and clears ``current_run_id`` in one transaction, so an empty list means no current run."""
    return [r.get("id") for r in _runs(show) if r.get("ended_at") is None]


def claimed_runs(show):
    """{run_id: lane} from ``claimed`` events: ``review`` when the payload carries ``source_status: review``."""
    out = {}
    for e in _events(show):
        p = e.get("payload")
        if e.get("kind") == "claimed" and isinstance(p, dict) and isinstance(p.get("run_id"), int):
            out[p["run_id"]] = "review" if p.get("source_status") == "review" else "work"
    return out


def answer_gate(q_show, w_show, q, w, run_id):
    """r9 §4.4(a) ``question_card`` send gate, delivery form: ``go``; ``children`` when another card linked
    itself to Q; else ``stale`` (Q not a fresh blocked intake card, Q not a parent of W, or a claim > R)."""
    task = (q_show or {}).get("task") or {}
    parents = {p.get("id") if isinstance(p, dict) else p for p in (w_show or {}).get("parents") or []}
    if (task.get("status") != "blocked" or _runs(q_show) or task.get("assignee") != AUTHOR or q not in parents
            or any(r > run_id for r in claimed_runs(w_show))):
        return "stale"
    return "go" if q_show.get("children") == [w] else "children"


def fence_gate(f_show, w):
    """r11 §3.3 fence send gate: F ``blocked``, no runs, assignee ``clickup-intake``, children(F) == [W]."""
    task = (f_show or {}).get("task") or {}
    ok = (task.get("status") == "blocked" and not _runs(f_show) and task.get("assignee") == AUTHOR
          and f_show.get("children") == [w])
    return "go" if ok else "stale"


def correction_gate(q_show, w_show, q):
    """r11 §3.6(b) ``question_card`` send gate, correction form: Q done, Q a parent of W, W not done/archived
    (the blocker-not-final check is the writer's). No claim, run, ACK or fence condition."""
    parents = {p.get("id") if isinstance(p, dict) else p for p in (w_show or {}).get("parents") or []}
    ok = card_status(q_show) == "done" and q in parents and card_status(w_show) not in (None, "done", "archived")
    return "go" if ok else "stale"


def sentinel_defect(show, w, cutover_ms):
    """None when ``show`` is a fresh protocol card created by worker card ``w``: assignee ``clickup-intake``,
    ``blocked`` with no runs, newest blocked/unblocked event its ``initial_status`` block, ``created`` payload
    ``creator_task_id == w``, created at/after the cutover. Otherwise the first failed check's name."""
    task = show.get("task") if isinstance(show, dict) else None
    if not isinstance(task, dict) or task.get("assignee") != AUTHOR:
        return "assignee"
    if task.get("status") != "blocked":
        return "status"
    if _runs(show):
        return "runs"
    events = _events(show)
    created = [e for e in events if e.get("kind") == "created"]
    if len(created) != 1 or not isinstance(created[0].get("payload"), dict) \
            or created[0]["payload"].get("creator_task_id") != w:
        return "creator"
    if not isinstance(task.get("created_at"), int) or task["created_at"] * 1000 < cutover_ms:
        return "pre_cutover"
    last = ([e for e in events if e.get("kind") in ("blocked", "unblocked")] or [{}])[-1]
    if last.get("kind") != "blocked" or (last.get("payload") or {}).get("reason") != "initial_status":
        return "unblocked"
    return None


PROTOCOL_TITLES = (("CLICKUP-QUESTION v1 ", "question"), ("CLICKUP-FENCE v1 ", "fence"), ("CLICKUP-PARK v1 ", "park"))


def protocol_parent(w, w_show, p_show, cutover_ms):
    """Examine an unrecorded parent P of watched card ``w`` (blocker plan r9 §4.2, r11 §3.2, r12 §3.2).

    (None, None) for a non-protocol parent. A question: ``record`` (W waits in ``todo``), ``wait`` (W not yet
    ``todo``; re-examined next scan) or ``alert:<why>`` (identity failure or a sticky/terminal child; no row).
    A fence or park: ``record``, or ``refused:<why>`` (recorded refused, one alert); a fence failing an identity
    check is ``ignored:<why>`` (r11 §3.2: not recorded, no alert; only children != [W] refuses it)."""
    task = p_show.get("task") if isinstance(p_show, dict) else None
    title = task.get("title") if isinstance(task, dict) else None
    hit = [(p, k) for p, k in PROTOCOL_TITLES if isinstance(title, str) and title.startswith(p)]
    if not hit:
        return None, None
    prefix, kind = hit[0]
    defect = sentinel_defect(p_show, w, cutover_ms)
    if defect is None and kind != "question" and title != prefix + w:
        defect = "title"
    if kind == "fence" and defect is not None:
        return kind, "ignored:" + defect  # r11 §3.2: recorded only if its identity holds
    if defect is None and p_show.get("children") != [w]:
        defect = "children"
    if kind != "question":
        return kind, "record" if defect is None else "refused:" + defect
    if defect is not None:
        return kind, "alert:" + defect
    status = (w_show.get("task") or {}).get("status") if isinstance(w_show, dict) else None
    if status == "todo":
        return kind, "record"
    if status == "blocked":
        return kind, "alert:sticky"
    if status in ("done", "archived"):
        return kind, "alert:orphan"
    return kind, "wait"


def has_comment(show, body, author=AUTHOR):
    """Exact readback of a comment we posted (the CLI stores the body stripped)."""
    want = body.strip()
    return any(c.get("author") == author and str(c.get("body", "")).strip() == want for c in _comments(show))


def has_comment_line(show, first_line, author=None):
    """A comment whose first line is exactly ``first_line`` (optionally by ``author``)."""
    for c in _comments(show):
        lines = str(c.get("body", "")).splitlines()
        if lines and lines[0] == first_line and (author is None or c.get("author") == author):
            return True
    return False


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def attachment_matches(attachments, name, local_path, uploaded_by=AUTHOR):
    """Attachment readback by stored name, size and SHA-256 of the stored bytes (§8.1, §10.2)."""
    try:
        size, digest = os.path.getsize(local_path), _sha256_file(local_path)
    except OSError:
        return False
    for a in attachments if isinstance(attachments, list) else ():
        if not isinstance(a, dict) or a.get("filename") != name or a.get("size") != size:
            continue
        if uploaded_by is not None and a.get("uploaded_by") != uploaded_by:
            continue
        try:
            if _sha256_file(a["stored_path"]) == digest:
                return True
        except (KeyError, OSError, TypeError):
            continue
    return False


def wake_decision(show, wait_run_id, relay_line):
    """§10.3 wake guard: ``send`` | ``done`` (the earlier unblock took effect) | ``stale``."""
    run_id = latest_run_id(show)
    if not isinstance(run_id, int) or not isinstance(wait_run_id, int):
        return "stale"
    if run_id > wait_run_id:
        return "done"
    if run_id != wait_run_id:
        return "stale"
    status = card_status(show)
    if status == "scheduled" and parked_wait_run_id(show) == wait_run_id and has_comment_line(show, relay_line, AUTHOR):
        return "send"
    if status in ("ready", "todo"):
        return "done"
    return "stale"


def pass_evidence(show, attachments):
    """§8.4 PASS evidence: done, final run by ``plan-reviewer``, and the latest review request naming an
    attached plan whose stored bytes hash to ``plan_sha256``. Returns that review metadata, else None."""
    runs = sorted(_runs(show), key=lambda r: r.get("id") or 0)
    if card_status(show) != "done" or not runs:
        return None
    final = runs[-1]
    if final.get("profile") != "plan-reviewer" or final.get("outcome") != "completed":
        return None
    reviews = [r for r in runs if r.get("outcome") == "review_requested"]
    meta = reviews[-1].get("metadata") if reviews else None
    if not isinstance(meta, dict):
        return None
    if not (isinstance(meta.get("plan_revision"), int) and isinstance(meta.get("accounts_gen"), int)
            and isinstance(meta.get("plan_attachment"), str) and _SHA256.match(str(meta.get("plan_sha256", "")))
            and isinstance(meta.get("clickup_task"), str)):
        return None
    for a in attachments if isinstance(attachments, list) else ():
        if isinstance(a, dict) and a.get("filename") == meta["plan_attachment"]:
            try:
                if _sha256_file(a["stored_path"]) == meta["plan_sha256"]:
                    return meta
            except (KeyError, OSError, TypeError):
                pass
    return None


def _completed_metadata(show):
    done = [r for r in _runs(show) if r.get("outcome") == "completed"]
    if card_status(show) != "done" or not done:
        return None
    meta = max(done, key=lambda r: r.get("id") or 0).get("metadata")
    return meta if isinstance(meta, dict) else None


def clickup_gates(show):
    """Validated ``clickup_gates`` from the orchestrator's completion metadata (§8.4 step 4), else None."""
    gates = (_completed_metadata(show) or {}).get("clickup_gates")
    if not isinstance(gates, dict):
        return None
    ids = [gates.get(k) for k in ("final_e2e", "promotion", "cleanup")]
    lineage = gates.get("lineage")
    if (isinstance(gates.get("plan_revision"), int) and not isinstance(gates.get("plan_revision"), bool)
            and _SHA256.match(str(gates.get("plan_sha256", "")))
            and all(isinstance(i, str) and _CARD_ID.match(i) for i in ids)
            and isinstance(lineage, list) and all(isinstance(i, str) and _CARD_ID.match(i) for i in lineage)):
        return gates
    return None


def promotion_guarded(show, gate_id, marker_line):
    """The promotion card has the gate as a parent and the exact marker line in its body."""
    parents = show.get("parents") if isinstance(show, dict) else None
    body = (show.get("task") or {}).get("body") if isinstance(show, dict) else None
    return (isinstance(parents, list) and gate_id in parents and isinstance(body, str)
            and marker_line in body.splitlines())


def promoted_sha(show):
    """40-hex ``promoted_sha`` of a done (not archived) promotion card, else None (§6)."""
    sha = (_completed_metadata(show) or {}).get("promoted_sha")
    return sha if isinstance(sha, str) and _SHA1.match(sha) else None


def has_passive_subscription(subs, chat_id):
    """D3 readback: a passive notify subscription to the configured Discord channel owned by the default notifier."""
    for s in subs if isinstance(subs, list) else ():
        meta = s.get("delivery_metadata") if isinstance(s, dict) else None
        if (isinstance(s, dict) and s.get("platform") == "discord" and str(s.get("chat_id")) == str(chat_id)
                and s.get("delivery_mode") == DELIVERY_MODE and s.get("notifier_profile") == NOTIFIER_PROFILE
                and s.get("chat_type") == CHAT_TYPE and isinstance(meta, dict)
                and meta.get("guild_id") == DISCORD_GUILD_ID):
            return True
    return False


# --------------------------------------------------------------------------- effect execution (§10.2, §10.3)
class EffectResult(NamedTuple):
    state: RowState  # DONE | RETRY_WAIT | AMBIGUOUS | CANCELLED_STALE | SENDING (deferred: child still alive)
    json: Any = None


def _failed(res):
    return EffectResult(RowState.AMBIGUOUS if res.outcome is Outcome.AMBIGUOUS else RowState.RETRY_WAIT, res.json)


def _question_done(show, p):
    """r9 §4.4(a) ``complete Q`` readback (A2): Q ``done`` with exactly the sent summary."""
    return card_status(show) == "done" and show.get("latest_summary") == p["summary"]


def _readback(port, intent):
    """``_applied`` plus, for a ``complete Q`` row, the confirming ``show`` of Q (r9 L4 post-release children
    readback); (applied | None when the read failed, that show or None)."""
    if intent.kind == COMPLETE and getattr(intent.preconditions, "question_card", None):
        res = port.show(intent.target)
        if res.outcome is not Outcome.OK:
            return None, None
        return _question_done(res.json, intent.payload), res.json
    return _applied(port, intent), None


def _applied(port, intent):
    """Readback: True if the effect is visible, False if not, None if the readback itself failed."""
    kind, card, p = intent.kind, intent.target, intent.payload
    if kind == ATTACH:
        res = port.attachments(card)
        return None if res.outcome is not Outcome.OK else attachment_matches(res.json, p["name"], p["path"])
    if kind == SUBSCRIBE:
        res = port.subscriptions(card)
        return None if res.outcome is not Outcome.OK else has_passive_subscription(res.json, port.discord_chat)
    res = port.show(card)
    if res.outcome is not Outcome.OK:
        return None
    if kind == COMMENT:
        return has_comment(res.json, p["body"])
    if kind == COMPLETE:
        done = card_status(res.json) == "done"
        if getattr(intent.preconditions, "question_card", None):  # r9 §4.4(a) readback (A2)
            return _question_done(res.json, p)
        return done
    if kind == ARCHIVE:
        return card_status(res.json) == "archived"
    if kind == UNBLOCK:
        wait = intent.preconditions.wait_run_id
        return wake_decision(res.json, wait, p["relay_line"]) == "done"
    raise ValueError(kind)


def _send(port, intent):
    kind, card, p = intent.kind, intent.target, intent.payload
    if kind == COMMENT:
        return port.comment(card, p["body"])
    if kind == ATTACH:
        return port.attach(card, p["path"], p["name"])
    if kind == UNBLOCK:
        return port.unblock(card)
    if kind == COMPLETE:
        return port.complete(card, p["summary"], p.get("metadata"))
    if kind == ARCHIVE:
        return port.archive(card)
    return port.subscribe(card)


def run_kanban_effect(port, intent, *, prior_child=None, alive=child_alive):
    """Execute one Kanban outbox effect (S0 ``EffectIntent``) with readback (§10.2) and the wake guard (§10.3).

    ``prior_child`` is the (pid, start_time) recorded for an earlier attempt of this row. While that
    child is alive nothing is sent (state stays SENDING). After it is gone, the effect is read back
    before any resend. Creates are idempotent by key and never need readback.
    """
    if intent.kind not in KINDS:
        raise ValueError(f"not a Kanban effect: {intent.kind}")
    if prior_child is not None and alive(*prior_child):
        return EffectResult(RowState.SENDING)
    if intent.kind == CREATE:
        p = intent.payload
        res = port.create(p["title"], body_file=p["body_file"], idempotency_key=p["idempotency_key"],
                          assignee=p.get("assignee"), skills=tuple(p.get("skills") or ()),
                          initial_status=p.get("initial_status", "blocked"))
        if res.outcome is Outcome.OK and isinstance(res.json, dict) and res.json.get("id"):
            return EffectResult(RowState.DONE, res.json)
        return _failed(res)
    if intent.kind == UNBLOCK:
        shown = port.show(intent.target)
        if shown.outcome is not Outcome.OK:
            return EffectResult(RowState.RETRY_WAIT)
        decision = wake_decision(shown.json, intent.preconditions.wait_run_id, intent.payload["relay_line"])
        if decision == "done":
            return EffectResult(RowState.DONE, shown.json)
        if decision == "stale":
            return EffectResult(RowState.CANCELLED_STALE, shown.json)
    elif prior_child is not None:
        seen, shown = _readback(port, intent)
        if seen is None:
            return EffectResult(RowState.RETRY_WAIT)
        if seen:
            return EffectResult(RowState.DONE, shown)
    res = _send(port, intent)
    if res.outcome is not Outcome.OK:
        return _failed(res)
    if intent.kind == UNBLOCK:
        shown = port.show(intent.target)
        status = card_status(shown.json) if shown.outcome is Outcome.OK else None
        landed = status in ("ready", "todo", "running") or (
            shown.outcome is Outcome.OK and (latest_run_id(shown.json) or 0) > intent.preconditions.wait_run_id)
        return EffectResult(RowState.DONE if landed else RowState.AMBIGUOUS, shown.json)
    seen, shown = _readback(port, intent)  # a complete-Q readback returns its confirming show of Q (r9 L4)
    return EffectResult(RowState.DONE if seen else RowState.AMBIGUOUS, res.json if shown is None else shown)
