"""Read-only generic diagnostic selection. This module grants no recovery operation authority."""
import hashlib
import json
import re

from . import kanban_port as kp, rules, render

GRACE_MS = 10 * 60_000
CLASS = "diagnostic"

FAILURES = {
    "missing_transition": "The approved continuation is missing a supported workflow transition.",
    "ticket_guard": "A current ticket guard stopped the work; diagnosis does not override that guard.",
    "unsupported_operation": "The required operation is outside the supported recovery operation set.",
    "missing_credential": "A required credential capability is unavailable; no credential value is requested here.",
}
ATTEMPTED = {"inspection": "read-only state and approval inspection", "supported_operations":
             "already-authorized supported recovery operations", "ticket_recheck": "read-only ticket recheck"}
NEXT = {
    "integration_repair": "The integration operator must arrange a separately authorized and independently reviewed "
                          "integration repair, then revalidate before supported same-work recovery.",
    "ticket_renewal": "The integration operator must restore the existing ticket through its supported renewal "
                      "path and recheck it before any resume; no repeat development approval is requested.",
    "secure_credential": "The authorized credential owner must provision the required capability through the "
                         "existing secure credential channel; never post a credential value to this task.",
}
_DETAIL = re.compile(r"CLICKUP-RECOVERY-DETAIL v1 failed=([a-z_]+) attempted=([a-z_]+) next=([a-z_]+)")


def notice_detail(show, inc):
    """Project only fixed allowlisted diagnosis codes from the first final ACK. Never relay raw ACK prose."""
    ack = kp.recovery_ack(show, inc["card"], inc["run_id"], inc["event_created_s"])
    for cm in kp._comments(show):
        lines = str(cm.get("body", "")).splitlines()
        if (cm.get("author") == kp.AUTHOR or cm.get("created_at") != ack["final_s"] or len(lines) < 2
                or lines[0] != "CLICKUP-RECOVERY-ACK v1 card=%s run=%s disposition=%s"
                % (inc["card"], inc["run_id"], ack["final"])):
            continue
        match = _DETAIL.fullmatch(lines[1])
        if match and match[1] in FAILURES and match[2] in ATTEMPTED and match[3] in NEXT:
            return "%s Attempted: %s. Next action: %s" % (FAILURES[match[1]], ATTEMPTED[match[2]], NEXT[match[3]])
    return ""


def diagnostic_ack(show, inc, after_s=-1):
    """Default coordinator decisions; the named operator may attest technical inability, never an owner wait."""
    cannot_act = 'CLICKUP-RECOVERY-ACK v1 card=%s run=%s disposition=cannot_act' % (inc['card'], inc['run_id'])
    comments = [c for c in kp._comments(show) if (c.get('author') == 'default'
                or (c.get('author') == 'coordinator' and str(c.get('body', '')).splitlines()[:1] == [cannot_act]))
                and type(c.get('created_at')) is int and c['created_at'] > after_s]
    final = []
    for c in comments:
        a = kp.recovery_ack({'comments': [c]}, inc['card'], inc['run_id'], inc['event_created_s'])
        if a['final']:
            final.append((c, a))
    if final:
        c, ack = max(final, key=lambda ca: ca[0]['created_at'])
        ack['working_s'] = kp.recovery_ack({'comments': comments}, inc['card'], inc['run_id'],
                                          inc['event_created_s'])['working_s']
        return ack, c
    return kp.recovery_ack({'comments': comments}, inc['card'], inc['run_id'], inc['event_created_s']), None


def question_projection(comment, inc):
    """Reject, never redact or truncate, a strict bounded structured decision. Raw ACK prose stays private."""
    lines = str((comment or {}).get('body', '')).splitlines()
    prefix = 'CLICKUP-RECOVERY-QUESTION v1 '
    if len(lines) < 2 or not lines[1].startswith(prefix) or len(lines[1]) > 6000:
        return None
    try:
        q = json.loads(lines[1][len(prefix):])
    except (ValueError, TypeError):
        return None
    binding = {'task': inc['task'], 'card': inc['card'], 'run': inc['run_id'], 'approval': inc['approval_id'],
               'revision': inc['approval_rev'], 'sha256': inc['plan_sha256'], 'fingerprint': inc['reason_sha256']}
    fields = ('failed', 'attempted', 'decision', 'next_action')
    if (not isinstance(q, dict) or set(q) != set(binding) | set(fields)
            or any(type(q.get(k)) is not type(v) or q[k] != v for k, v in binding.items())):
        return None
    if any(not isinstance(q[k], str) or not q[k].strip() or len(q[k]) > 800
           or any(ord(c) < 32 for c in q[k]) for k in fields):
        return None
    text = '\n'.join(q[k] for k in fields)
    # This new free-text projection rejects credential-shaped keys as well as the existing outbound rules.
    if re.search(r'\bsk-[A-Za-z0-9_-]{20,}', text):
        return None
    return None if rules.blocker_leak(text) or render.check_outbound(text) else q


def legacy_question_projection(comment, inc):
    """The unchanged legacy carrier asks default for a precise prose question, not the new JSON protocol."""
    lines = str((comment or {}).get('body', '')).splitlines()
    if (inc['class'] == CLASS or (comment or {}).get('author') != 'default' or len(lines) < 2
            or lines[0] != 'CLICKUP-RECOVERY-ACK v1 card=%s run=%s disposition=owner' %
               (inc['card'], inc['run_id']) or any('CLICKUP-RECOVERY-QUESTION' in line for line in lines[1:])):
        return None  # malformed/foreign structured questions must never downgrade to prose
    decision = '\n'.join(lines[1:]).strip()
    if (not decision or '?' not in decision or len(decision) > 800 or any(ord(c) < 32 and c != '\n' for c in decision)
            or re.search(r'\bsk-[A-Za-z0-9_-]{20,}', decision)
            or rules.blocker_leak(decision) or render.check_outbound(decision)):
        return None
    return dict(task=inc['task'], card=inc['card'], run=inc['run_id'], approval=inc['approval_id'],
                revision=inc['approval_rev'], sha256=inc['plan_sha256'], fingerprint=inc['reason_sha256'],
                failed='Approved work is stopped at the recorded source run.',
                attempted='The existing coordinator reported an owner-only decision for this run.',
                decision=decision, next_action='Answer this precise question in this thread and mention the intake bot; '
                'the answer follows normal classification and authority checks.')


def held_notice_tasks(conn):
    """Only exact own-carrier-failure notices may keep read-only observation alive under that hold."""
    if not conn.execute("SELECT 1 FROM meta WHERE key='recovery_cutover_ms'").fetchone():
        return set()
    tasks = set()
    for row in conn.execute("SELECT o.*, t.hold_reason FROM outbox o JOIN tasks t ON t.clickup_task_id=o.task"
                            " WHERE o.kind='cu_comment' AND t.terminal=0 AND t.hold_reason LIKE 'outbox:%'"
                            " AND o.state IN ('pending','retry_wait','sending','ambiguous','done')"):
        p = json.loads(row['body'])
        hold = p.get('recovery_delivery_hold')
        if not hold or hold != row['hold_reason']:
            continue
        inc = conn.execute("SELECT * FROM recoveries WHERE id=? AND task=? AND state='open'",
                           (p.get('recovery_notice'), row['task'])).fetchone()
        if not inc:
            continue
        from .engine import _notice_key
        expected = _notice_key(row['task'], inc, conn)
        if row['idem_key'] != expected:
            continue
        prefix = 'clickup-dp:%s:rcv:%s:%d:' % (row['task'], inc['card'], inc['run_id'])
        failed = conn.execute("SELECT state FROM outbox WHERE idem_key=?", (hold[7:],)).fetchone()
        if (not hold[7:].startswith(prefix) or not failed or failed[0] != 'failed'
                or conn.execute("SELECT 1 FROM outbox WHERE outcome='ABANDONED' AND (idem_key LIKE ? OR idem_key=?)",
                                (prefix + '%', row['idem_key'])).fetchone()):
            continue
        tasks.add(row['task'])
    return tasks


def context(conn, tid):
    """Read-only obligation/plan-source projection, shared by selection and fresh admission."""
    return {"blockers": [dict(r) for r in conn.execute("SELECT * FROM blockers WHERE task=?", (tid,))],
            "fences": [dict(r) for r in conn.execute("SELECT * FROM fences WHERE task=?", (tid,))],
            "requests": [r[0] for r in conn.execute("SELECT card_id FROM cards WHERE task=? AND role='request'",
                                                   (tid,))]}


def plan_markers(task, approval, shows, attachments, state):
    """Only exact reviewed/approved bytes count; a marker is diagnostic evidence, never a release."""
    for card in state.get("requests", []):
        ev = rules.pass_evidence(shows.get(card), attachments.get(card), task["clickup_task_id"])
        if ev and ev["rev"] == approval["rev"] and ev["sha256"] == approval["plan_sha256"]:
            for att in attachments.get(card, []):
                if att.get("name") == ev["attachment"] and att.get("sha256") == ev["sha256"]:
                    return att.get("incorporates") or []
    return []


def handoff(task, approval, inc, shows, state, attachments):
    """Observe an existing incident's unowned handoff, never select a new stalled queue item."""
    card = inc['card']
    w = shows.get(card)
    if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
            or task['terminal'] or task['hold_reason'] or not rules.current(approval['accounts_gen'], task)
            or kp.card_status(w) not in ('ready', 'review', 'running', 'blocked', 'triage', 'todo')
            or not isinstance(w.get('parents'), list)
            or not isinstance(w.get('events'), list) or not isinstance(w.get('runs'), list)):
        return None
    run = kp.latest_run(w)
    if inc['run_id'] == 0:
        # The admitted missing-worker stop is anchored to creation, not an invented claim/event 0.
        if (inc['class'] != CLASS or inc['event_kind'] != 'stalled_work'
                or w['task'].get('created_at') != inc['event_created_s']
                or any(type(r.get('id')) is not int or r['id'] <= 0
                       or type(r.get('started_at')) is not int or r['started_at'] < inc['event_created_s']
                       for r in w['runs'])):
            return None
    if not run:
        if (inc['run_id'] != 0 or w['runs'] != [] or kp.claimed_runs(w) or kp.card_status(w) != 'ready'
                or w['task'].get('current_run_id') is not None or w['task'].get('worker_pid') is not None):
            return None
    else:
        if (run.get('id', -1) < inc['run_id'] or run['id'] not in kp.claimed_runs(w)
                or run.get('profile') != w['task'].get('assignee') or not run.get('profile')
                or (inc['run_id'] != 0 and not any(r.get('id') == inc['run_id'] for r in w['runs']))):
            return None
        if run['id'] == inc['run_id']:
            if (kp.card_status(w) not in ('ready', 'review') or run.get('ended_at') is None
                    or run.get('worker_pid') is not None or kp.open_run_ids(w)
                    or (kp.card_status(w) == 'review' and
                        kp.recovery_outcome(w, inc['run_id'], inc['event_created_s'])[0] != 'handed_off')):
                return None
        elif (w['task'].get('current_run_id') not in (None, run['id'])
              or (kp.open_run_ids(w) and (kp.open_run_ids(w) != [run['id']]
                  or w['task'].get('current_run_id') != run['id']
                  or run.get('worker_pid') != w['task'].get('worker_pid')))):
            return None
        if inc['run_id'] != 0 and not any(
                kp._event_run(e) == inc['run_id'] and e.get('created_at') == inc['event_created_s']
                and e.get('kind') in ('blocked', 'block_loop_detected', 'dependency_wait') for e in w['events']):
            return None
    # Every known obligation must be observed satisfied, including native questions and reviewed plan bytes.
    required = set(w['parents']) | set(state['requests']) | {b['question_card'] for b in state['blockers']}
    if any(not isinstance(shows.get(p), dict) or (shows[p].get('task') or {}).get('id') != p
           or kp.card_status(shows[p]) not in ('done', 'archived') or kp.open_run_ids(shows[p]) for p in required):
        return None
    if any(f['state'] in ('open', 'releasing', 'abandoned', 'refused') for f in state['fences']):
        return None
    if not any((ev := rules.pass_evidence(shows.get(r), attachments.get(r), task['clickup_task_id']))
               and ev['rev'] == approval['rev'] and ev['sha256'] == approval['plan_sha256'] for r in state['requests']):
        return None
    return run['id'] if run else 0


def candidate(task, approval, card, shows, state=None, attachments=None):
    """A fully observed stopped worker with satisfied parents; unknown reads are never empty obligations."""
    w = shows.get(card)
    if not isinstance(w, dict) or (w.get("task") or {}).get("id") != card:
        return None
    if task["terminal"] or task["hold_reason"] or not rules.current(approval["accounts_gen"], task):
        return None
    if kp.card_status(w) not in ("blocked", "triage", "todo"):
        return None
    run = kp.latest_run(w)
    if run and (run.get("id") not in kp.claimed_runs(w) or run.get("ended_at") is None
            or run.get("worker_pid") is not None or kp.open_run_ids(w)):
        return None
    parents = w.get("parents")
    if not isinstance(parents, list):
        return None
    state = state or {}
    markers = plan_markers(task, approval, shows, attachments or {}, state)
    incorporated = set()
    for b in state.get("blockers", []):
        q = b["question_card"]
        qs = shows.get(q)
        if (not isinstance(qs, dict) or (qs.get("task") or {}).get("id") != q
                or kp.card_status(qs) not in ("blocked", "done", "archived") or kp.open_run_ids(qs)):
            return None
        if kp.card_status(qs) in ("done", "archived"):
            continue
        matched = any(m == {"blocker": b["id"], "question": q, "card": b["card"], "gen": b["closure_gen"]}
                      and m["gen"] <= approval["accounts_gen"] for m in markers)
        if not (b["state"] == "scope_change" and matched and approval["id"] > (b["approval_id"] or 0)):
            return None
        incorporated.add(q)
    if any(f["state"] in ("open", "releasing", "abandoned", "refused") for f in state.get("fences", [])):
        return None
    observed = []
    for p in parents:
        s = shows.get(p)
        if not isinstance(s, dict) or (s.get("task") or {}).get("id") != p:
            return None
        status = kp.card_status(s)
        if status not in ("done", "archived") and p not in incorporated:
            return None
        observed.append((p, status))
    if kp.card_status(w) == "todo" and not incorporated.intersection(parents):
        return None  # a normal ready/todo queue or dependency is not a stalled decision transition
    if kp.card_status(w) == "todo" and kp.card_status(shows.get(approval["orchestrator_card"])) != "done":
        return None  # an active coordinator, not a missing transition
    if run:
        events = [e for e in w.get("events", []) if isinstance(e, dict) and e.get("kind") in
                  ("blocked", "block_loop_detected", "dependency_wait") and kp._event_run(e) == run["id"]]
        if not events or run.get("outcome") != "blocked":
            return None
        event = events[-1]
    else:
        if w.get("runs") != [] or kp.card_status(w) != "todo":
            return None
        event = {"kind": "missing_worker", "created_at": (w.get("task") or {}).get("created_at")}
    if type(event.get("created_at")) is not int:
        return None
    rid = run["id"] if run else 0  # explicit no-worker generation, never a synthetic claimed run
    binding = [approval["id"], approval["rev"], approval["plan_sha256"], task["input_gen"],
               task["scope_gen"], card, rid, kp.card_status(w), event, sorted(observed)]
    fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return {"run_id": rid, "event_kind": "stalled_work", "event_created_s": event["created_at"],
            "block_kind": "diagnostic", "class": CLASS, "reason_sha256": fingerprint,
            "reason": "Approved work remains stopped; inspect current authority and supported recovery options.",
            "source_status": (event.get("payload") or {}).get("source_status")}
