"""Pure S2 workflow rules (plan r5 §4.4, §5.3, §6, §7, §8.4-§8.5, §10).

No I/O: every function decides from rows, parsed comments or ``show --json`` dicts.
"""

import re

from .parse import PLAIN, QUOTED, normalize, parse_comment

APPROVE = re.compile(r"approve plan r(\d+) for development")
VAGUE = re.compile(r"(i )?approved?( the)?( plan)?( r\d+)?( for development)?")
CLOSE_BUSINESS = "business requirements closed"
CLOSE_TECHNICAL = "technical requirements closed"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
OUT_KINDS = ("business_questions", "technical_questions", "answer", "notice", "classification")
# Workflow phases in which a queued wake (request create, relay, unblock) may still go out (§4.2-§4.4, §10.1).
LIVE_PHASES = ("new_quiet", "planning", "awaiting_business", "awaiting_technical", "in_review", "awaiting_approval",
               "revising", "approval_queued", "development", "implemented", "promotion_window", "promotion_held",
               "ready_for_deployment")


def _command_text(norm_text, lines):
    m = APPROVE.fullmatch(norm_text)
    if m:
        return "approve", int(m.group(1))
    if CLOSE_BUSINESS in lines:
        return "close_business", None
    if CLOSE_TECHNICAL in lines:
        return "close_technical", None
    if VAGUE.fullmatch(norm_text):
        return "vague", None
    return None, None


def command_of(parts, comment_text, allowlist):
    """Command candidacy of one comment (§7.1, §7.3): (kind, rev, Parsed).

    kind is approve | close_business | close_technical | vague for a PLAIN shape;
    ``quoted`` for command-like QUOTED text (context only); ``unknown`` for command-like
    text in any other shape (rejected with a notice); None otherwise. There is no
    text-equality fallback: only PLAIN can carry a command.
    """
    p = parse_comment(parts, comment_text, allowlist)
    if p.shape == PLAIN:
        kind, rev = _command_text(p.norm_text, p.lines)
        return kind, rev, p
    text = comment_text if isinstance(comment_text, str) else ""
    raw = [ln.lstrip().lstrip(">") for ln in text.splitlines()]
    lines = [ln for ln in (normalize(x) for x in raw) if ln]
    kind, _ = _command_text(" ".join(lines), lines)
    if kind is None and p.norm_text:
        kind, _ = _command_text(p.norm_text, p.lines)
    if kind is None:
        return None, None, p
    return ("quoted" if p.shape == QUOTED else "unknown"), None, p


def may(action, author_id, requester_id, approvers, bot_id):
    """Author matrix §7.2 by API user id: 'yes', 'notice' or 'ignore'."""
    if author_id is None or author_id == bot_id:
        return "ignore"
    named = author_id in approvers
    if action == "ask":
        return "yes"
    if action == "close_business":
        return "yes" if named or author_id == requester_id else "notice"
    return "yes" if named else "notice"  # close_technical, approve


def is_new(item, base):
    """NEW (§7.3): first seen no earlier than the baseline post and dated after it."""
    return (item["first_seen_ms"] is not None and base["first_seen_ms"] is not None
            and item["first_seen_ms"] >= base["first_seen_ms"]
            and item["date_ms"] is not None and base["date_ms"] is not None and item["date_ms"] > base["date_ms"])


def current(accounts_gen, task):
    """``current(rN)`` (§5.3): accounts_gen >= scope_gen and classified_gen == input_gen."""
    return (accounts_gen is not None and accounts_gen >= task["scope_gen"]
            and task["classified_gen"] == task["input_gen"])


def barrier_ok(task, anchor_ms, now, scan_started_ms):
    """§4.4 barrier: quiet expired; a complete F started at/after max(quiet_until, anchor);
    an L in this scan, after that F, found no new activity (any activity restarts quiet)."""
    quiet = task["quiet_until_ms"] or 0
    started, light = task["full_started_ms"], task["last_light_ms"]
    return (now >= quiet and not task["full_cursor"] and started is not None and started >= max(quiet, anchor_ms or 0)
            and light is not None and light >= scan_started_ms and light >= (task["last_full_ms"] or 0)
            and light >= quiet)


def send_barrier_ok(task, payload, now, scan_started_ms):
    """§4.4 rechecked at every send and retry (QSR F2). A quiet-expired row carries ``barrier_anchor_ms`` in
    its payload and may go out only while the current quiet has expired and the barrier anchored there holds
    in this scan. False means DEFER (keep the row pending/retrying), never cancel, so the row still completes
    idempotently once the barrier holds. Rows without the key (revocation stamps, archives, notices, alerts)
    are protective or not quiet-expired and never wait."""
    if not isinstance(payload, dict) or "barrier_anchor_ms" not in payload:
        return True
    return task is not None and barrier_ok(task, payload["barrier_anchor_ms"], now, scan_started_ms)


def parse_out(body):
    """Parse a planner ``CLICKUP-OUT v1`` comment: (header dict, text) or None."""
    if not isinstance(body, str):
        return None
    first, _, rest = body.partition("\n")
    tokens = first.split()
    if tokens[:2] != ["CLICKUP-OUT", "v1"]:
        return None
    hdr = {}
    for tok in tokens[2:]:
        k, sep, v = tok.partition("=")
        if not sep or not v:
            return None
        hdr[k] = v
    if hdr.get("kind") not in OUT_KINDS or "relay" not in hdr:
        return None
    if hdr["kind"] == "classification":
        if hdr.get("result") not in ("answer_only", "material") or not hdr.get("gen", "").isdigit():
            return None
        hdr["gen"] = int(hdr["gen"])
    return hdr, rest.strip()


def runs(show):
    out = [r for r in (show or {}).get("runs") or [] if isinstance(r, dict) and isinstance(r.get("id"), int)]
    return sorted(out, key=lambda r: r["id"])


def latest_run(show):
    rs = runs(show)
    return rs[-1] if rs else None


def status(show):
    task = (show or {}).get("task")
    return task.get("status") if isinstance(task, dict) else None


def done_ms(show):
    task = (show or {}).get("task") or {}
    v = task.get("completed_at")
    return int(v) * 1000 if isinstance(v, int) and not isinstance(v, bool) else None


def parents(show):
    out = set()
    for p in (show or {}).get("parents") or []:
        out.add(p.get("id") if isinstance(p, dict) else p)
    return out


def _metadata(run):
    md = run.get("metadata")
    return md if isinstance(md, dict) else {}


def pass_evidence(show, attachments, task_id):
    """§8.4 PASS evidence of a request card, or None.

    Card done; final run by plan-reviewer; the latest review_requested metadata names
    an attached plan whose stored bytes hash to plan_sha256.
    """
    rs = runs(show)
    if status(show) != "done" or not rs or rs[-1].get("profile") != "plan-reviewer":
        return None
    req = [r for r in rs if r.get("outcome") == "review_requested"]
    if not req:
        return None
    md = _metadata(req[-1])
    rev, sha, name, ag = (md.get("plan_revision"), md.get("plan_sha256"), md.get("plan_attachment"),
                          md.get("accounts_gen"))
    if md.get("clickup_task") != task_id or not _pos_int(rev) or not _int(ag) or not isinstance(name, str) \
            or not isinstance(sha, str) or not HEX64.fullmatch(sha):
        return None
    if not any(a.get("name") == name and a.get("sha256") == sha for a in attachments or [] if isinstance(a, dict)):
        return None
    return {"rev": rev, "sha256": sha, "attachment": name, "accounts_gen": ag}


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _pos_int(v):
    return _int(v) and v > 0


def gates_of(show, rev, sha):
    """Validated ``clickup_gates`` from the orchestrator's latest completed run metadata, or None."""
    done = [r for r in runs(show) if r.get("outcome") == "completed"]
    if status(show) != "done" or not done:
        return None
    g = _metadata(done[-1]).get("clickup_gates")
    if not isinstance(g, dict) or g.get("plan_revision") != rev or g.get("plan_sha256") != sha:
        return None
    if not all(isinstance(g.get(k), str) and g.get(k) for k in ("final_e2e", "promotion", "cleanup")):
        return None
    if not isinstance(g.get("lineage"), list):
        return None
    return {k: g[k] for k in ("plan_revision", "plan_sha256", "final_e2e", "promotion", "cleanup", "lineage")}


def promoted_sha(show):
    """Full 40-hex promoted commit of the latest completed run, or None.

    Canonical: top-level ``promoted_sha``. When present it governs: malformed, or contradicted
    by a structured ``promoted.dev_after``, gives None, never a fallback. Without it, a completed
    structured receipt is accepted only when ``candidate.sha`` and ``promoted.dev_after`` are
    both full 40-hex and equal.
    """
    done = [r for r in runs(show) if r.get("outcome") == "completed"]
    md = _metadata(done[-1]) if done else {}
    after = md["promoted"].get("dev_after") if isinstance(md.get("promoted"), dict) else None
    if "promoted_sha" in md:
        sha = md["promoted_sha"]
        return sha if isinstance(sha, str) and HEX40.fullmatch(sha) and after in (None, sha) else None
    cand = md["candidate"].get("sha") if isinstance(md.get("candidate"), dict) else None
    return cand if isinstance(cand, str) and HEX40.fullmatch(cand) and cand == after else None


def marker(gate, task, rev, sha):
    return f"CLICKUP-TICKET-CHECK v1 gate={gate} task={task} rev={rev} sha={sha}"


def guarded(show, gate, task, rev, sha):
    """Promotion card parented on the gate with the exact marker line in its body (§8.4 step 4)."""
    body = ((show or {}).get("task") or {}).get("body") or ""
    return gate in parents(show) and marker(gate, task, rev, sha) in [ln.strip() for ln in body.splitlines()]


def preconditions_ok(task, approvals, pre, kind, card_show=None, confirmed_list_id=None):
    """§10.1 recheck immediately before a send/retry; False means ``cancelled_stale``.

    ``approvals`` maps id -> row; ``confirmed_list_id`` is the home list id the GET Task of
    this scan confirmed (None when not confirmed).
    """
    if task is None:
        return False
    if task["terminal"] and (kind.startswith("cu_") or pre.phases):
        return False  # phase-guarded rows are workflow rows; protective rows carry no phases
    if pre.list_id is not None and confirmed_list_id != pre.list_id:
        return False
    if pre.phases and task["phase"] not in pre.phases:
        return False
    for f in ("scope_gen", "input_gen", "published_rev"):
        v = getattr(pre, f)
        if v is not None and task[f] != v:
            return False
    if pre.approval_id is not None:
        a = approvals.get(pre.approval_id)
        if a is None or (pre.approval_states and a["state"] not in pre.approval_states):
            return False
    if pre.wait_run_id is not None:
        run = latest_run(card_show)
        if status(card_show) != "scheduled" or not run or run["id"] != pre.wait_run_id:
            return False
    return True
