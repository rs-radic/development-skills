"""Pure promotion-ticket verdict (plan r5 §8.5).

Shared by the poller and the read-only ``ticket-check`` subcommand. Validity is
recomputed from the local approval/task rows; gate-card audit stamps are never
required or trusted. Everything not positively VALID is INVALID.
"""

import json

from .contracts import TicketVerdict

INVALID = TicketVerdict.INVALID


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not an int")
    return value


def _gate_status(gate_show_json, gate_id):
    data = gate_show_json
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError:
            return None, "gate_unparseable"
    task = data.get("task") if isinstance(data, dict) else None
    if not isinstance(task, dict) or "id" not in task or "status" not in task:
        return None, "gate_unparseable"
    if task["id"] != gate_id:
        return None, "gate_mismatch"
    return task["status"], None


def ticket_verdict(snapshot, gate_show_json, task, rev, plan_sha, gate_id, now):
    """Return VALID(epoch) only if every §8.5 condition holds; ``now`` is epoch ms."""
    if snapshot is None:
        return INVALID("state_unavailable")
    try:
        row = snapshot.task
        if not row or row.get("clickup_task_id") != task:
            return INVALID("task_mismatch")
        mine = [a for a in snapshot.approvals if a.get("task") == task]
        if not mine:
            return INVALID("no_approval")
        cands = [a for a in mine if a.get("rev") == rev]
        if not cands:
            return INVALID("rev_mismatch")
        cands = [a for a in cands if a.get("plan_sha256") == plan_sha]
        if not cands:
            return INVALID("sha_mismatch")
        cands = [a for a in cands if a.get("gate_card") == gate_id]
        if not cands:
            return INVALID("gate_mismatch")
        if len(cands) != 1:
            return INVALID("ambiguous_approval")
        appr = cands[0]

        if appr.get("state") == "revoked":
            return INVALID(appr.get("revoked_reason") or "revoked")
        if appr.get("state") != "ticketed":
            return INVALID("not_ticketed")
        if any(_int(a.get("rev")) > rev for a in mine):
            return INVALID("superseded")
        if _int(row.get("terminal")) != 0:
            return INVALID("terminal")
        if row.get("hold_reason"):
            return INVALID("hold")
        input_gen = _int(row.get("input_gen"))
        if _int(appr.get("ticket_input_gen")) != input_gen:
            return INVALID("new_input")
        if _int(row.get("classified_gen")) != input_gen:
            return INVALID("unclassified")
        if _int(appr.get("accounts_gen")) < _int(row.get("scope_gen")):
            return INVALID("not_current")
        epoch = _int(appr.get("ticket_epoch"))
        if epoch < 1:
            return INVALID("malformed")
        if _int(now) - _int(appr.get("ticket_renewed_ms")) > _int(snapshot.lease_seconds) * 1000:
            return INVALID("lease_expired")
    except (AttributeError, TypeError, ValueError):
        return INVALID("malformed")

    status, err = _gate_status(gate_show_json, gate_id)
    if err:
        return INVALID(err)
    if status != "done":
        return INVALID("gate_not_done")
    return TicketVerdict.VALID(epoch)
