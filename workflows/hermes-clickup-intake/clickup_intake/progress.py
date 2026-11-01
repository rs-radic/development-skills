"""Structural progress diagnostics, never publication or development authority."""
import hashlib
import json
import re

from . import kanban_port as kp, rules, stalled

CLASS = 'progress_stall'
PHASES = ('new_quiet', 'planning', 'awaiting_business', 'awaiting_technical', 'in_review', 'revising',
          'awaiting_approval', 'approval_queued', 'development', 'implemented',
          'promotion_window', 'promotion_held', 'ready_for_deployment')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def failure_hold(conn, task):
    """Exact integration-owned definite failure only; never an operator/security/ambiguous hold."""
    hold = task['hold_reason'] or ''
    if not hold.startswith('outbox:'):
        return None
    row = conn.execute('SELECT * FROM outbox WHERE idem_key=? AND task=?',
                       (hold[7:], task['clickup_task_id'])).fetchone()
    prefixes = tuple('clickup-dp:%s:%s:' % (task['clickup_task_id'], stage)
                     for stage in ('req', 'pub', 'relay', 'bq'))
    if (not row or not row['idem_key'].startswith(prefixes) or row['state'] != 'failed'
            or row['outcome'] != 'retry_wait' or not row['attempts'] or row['result_ref'] is not None
            or row['kind'] not in ('kb_create', 'kb_attach', 'kb_comment', 'kb_unblock',
                                   'cu_comment', 'cu_reply', 'cu_attach', 'cu_status')):
        return None
    if ':bq:' in row['idem_key'] and not conn.execute(
            "SELECT 1 FROM blockers WHERE task=? AND question_key=? AND state='asked'",
            (task['clickup_task_id'], row['idem_key'])).fetchone():
        return None
    return dict(row)


def held_tasks(conn):
    if not conn.execute("SELECT 1 FROM meta WHERE key='recovery_cutover_ms'").fetchone():
        return set()
    return {t['clickup_task_id'] for t in conn.execute(
        "SELECT clickup_task_id FROM tasks WHERE terminal=0 AND hold_reason IS NOT NULL AND hold_reason<>'operator'")}


def context(conn, tid):
    from .engine import dependency_scope
    return dict(stalled.context(conn, tid), dependency_scope=dependency_scope(conn, tid),
        **delivery_context(conn, tid),
        approvals=[dict(r) for r in conn.execute(
        'SELECT * FROM approvals WHERE task=? ORDER BY id', (tid,))], requests=[r[0] for r in conn.execute(
        "SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC", (tid,))],
        transitions=[dict(r) for r in conn.execute("SELECT * FROM outbox WHERE task=? AND "
            "(idem_key LIKE ? OR idem_key LIKE ?)", (tid, 'clickup-dp:%s:pub:%%' % tid,
                                                       'clickup-dp:%s:req:%%' % tid))],
        relays=[dict(r) for r in conn.execute('SELECT * FROM relays WHERE task=?', (tid,))],
        stage_rows=[dict(r) for r in conn.execute("SELECT * FROM outbox WHERE task=? AND "
            "(idem_key LIKE ? OR idem_key LIKE ?)", (tid, 'clickup-dp:%s:gate:%%' % tid,
                                                    'clickup-dp:%s:orch:%%' % tid))],
        created_ms=conn.execute('SELECT MIN(first_seen_ms) FROM items WHERE task=?', (tid,)).fetchone()[0],
        human_wait=human_wait(conn, tid),
        diagnostic_waits=[json.loads(r[0]) for r in conn.execute(
            "SELECT m.value FROM meta m JOIN recoveries r ON m.key='diagnostic_wait:'||r.id "
            "WHERE r.task=? AND r.state IN ('open','handed_off')", (tid,))],
        completed_incidents=[int(r[0]) for r in conn.execute(
            'SELECT value FROM meta WHERE key=?', ('completed_workflow:' + tid,))],
        continued_incidents=[json.loads(r[0]) for r in conn.execute(
            'SELECT value FROM meta WHERE key=?', ('diagnostic_continuation:' + tid,))],
        deliveries=[dict(r) for r in conn.execute("SELECT idem_key,payload_sha256,result_ref FROM outbox "
            "WHERE task=? AND kind LIKE 'cu_%' AND state='done' AND idem_key NOT LIKE '%:recovery-notice:%' "
            "ORDER BY rowid", (tid,))],
        delivery_failures=[dict(r) for r in conn.execute("SELECT idem_key,kind,body,preconditions,payload_sha256,state,outcome"
            " FROM outbox WHERE task=? AND kind IN ('cu_comment','cu_reply','cu_attach','cu_status')"
            " AND (state='failed' OR (state='cancelled_stale' AND outcome='FOREIGN_ROOT'))"
            " AND idem_key NOT LIKE '%:recovery-notice:%' ORDER BY idem_key", (tid,))],
        failure=failure_hold(conn, conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone()))


def verified_delivery_wait(conn, tid, wait, inc):
    """An independent diagnostic may share only an unchanged, read-back human wait."""
    if not isinstance(wait, dict) or wait.get('state') != 'waiting' or not wait.get('root') or not wait.get('verified_ms'):
        return False
    question = wait.get('question')
    if not inc or not isinstance(question, dict) or any(question.get(q) != inc[k] for q, k in
            (('task', 'task'), ('card', 'card'), ('run', 'run_id'), ('approval', 'approval_id'),
             ('revision', 'approval_rev'), ('sha256', 'plan_sha256'), ('fingerprint', 'reason_sha256'))):
        return False
    row = conn.execute("SELECT result_ref,payload_sha256 FROM outbox WHERE task=? AND idem_key=? "
                       "AND kind='cu_comment' AND state='done'", (tid, wait.get('key'))).fetchone()
    mirror = conn.execute("SELECT result_ref FROM outbox WHERE task=? AND idem_key=? AND state='done'",
                          (tid, str(wait.get('key')) + '#cu')).fetchone()
    return bool(row and mirror and row['result_ref'] == mirror['result_ref'] == wait['root']
                and conn.execute("SELECT 1 FROM items WHERE task=? AND kind='comment' AND id=? AND deleted=0",
                                 (tid, wait['root'])).fetchone())


def delivery_context(conn, tid):
    row = conn.execute('SELECT value FROM meta WHERE key=?', ('diagnostic_delivery:' + tid,)).fetchone()
    role = json.loads(row[0]) if row else None
    wait = conn.execute('SELECT value FROM meta WHERE key=?',
                        ('diagnostic_wait:%s' % role.get('incident'),)).fetchone() if isinstance(role, dict) else None
    wait = json.loads(wait[0]) if wait else None
    inc = conn.execute('SELECT * FROM recoveries WHERE id=?', (role.get('incident'),)).fetchone() if isinstance(role, dict) else None
    return dict(delivery_role=role, delivery_wait_current=bool(isinstance(role, dict)
                and role.get('wait_sha256') == digest(wait) and verified_delivery_wait(conn, tid, wait, inc)))


def delivery_current(inc, state, found):
    role = state.get('delivery_role')
    return bool(isinstance(role, dict) and role.get('incident') == inc['id'] and state.get('delivery_wait_current')
                and found and found.get('delivery_only') and role.get('progress') == found['reason_sha256'])


def retire_delivery(ctx, tid, inc):
    """Return a temporary delivery owner to its original, still-guarded lifecycle."""
    from . import engine
    state = context(ctx.c, tid)
    role = state.get('delivery_role')
    if not isinstance(role, dict) or role.get('incident') != inc['id']:
        return inc
    wait = engine._diagnostic_wait(ctx, inc['id']) or {}
    transitioned = wait.get('state') in ('classifying', 'answered', 'superseded')
    row = ctx.c.execute('SELECT value FROM meta WHERE key=?',
                       ('diagnostic_history:%d:%s' % (inc['id'], inc['reason_sha256']),)).fetchone()
    original = json.loads(row[0]) if row else None
    if not isinstance(original, dict) or any(original.get(k) != inc[k] for k in
            ('id', 'task', 'card', 'run_id', 'approval_id', 'approval_rev', 'plan_sha256',
             'event_kind', 'event_created_s', 'reason_sha256', 'detected_ms', 'abs_ms')):
        return inc
    from . import preworker
    task = ctx.task(tid)
    approval = next((a for a in state['approvals'] if a['id'] == inc['approval_id']), None)
    staging_owner = bool(approval and original['class'] == preworker.CLASS
                         and inc['card'] == preworker.anchor(approval) and inc['run_id'] == 0)
    source_owner = bool(approval and original['class'] == CLASS and inc['event_kind'] == CLASS
                        and inc['card'] == approval['orchestrator_card'])
    # A newer active approval is positive supersession evidence, not a failed current check.
    superseded = bool((staging_owner or source_owner) and state.get('delivery_wait_current')
        and task and not task['terminal'] and not task['hold_reason']
        and task['classified_gen'] == task['input_gen'] and approval
        and rules.barrier_ok(task, 0, ctx.now, ctx.obs.scan_started_ms)
        and approval['rev'] == inc['approval_rev'] and approval['plan_sha256'] == inc['plan_sha256']
        and (approval['state'] != 'staged' or ctx.cfg.handoff_unblock)
        and any(a['id'] > approval['id'] and a['state'] in engine.ACTIVE + engine.WATCHED_STATES
                for a in state['approvals']))
    if not transitioned and not superseded and (state.get('delivery_failures') or not state.get('delivery_wait_current')):
        return inc  # Missing proof is not supersession or permission to resume source work.
    ctx.c.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                  ('diagnostic_delivery_history:%d:%s' % (inc['id'], digest(role)),
                   json.dumps(dict(role=role, retired_ms=ctx.now, original_class=original['class']), sort_keys=True)))
    engine._retire_attempts(ctx, tid, inc['card'], inc['run_id'])
    changes = {'class': original['class']}
    if superseded and source_owner:
        changes['state'] = 'superseded'
    engine._recovery_update(ctx, inc, **changes)
    ctx.c.execute('DELETE FROM meta WHERE key=?', ('diagnostic_delivery:' + tid,))
    return dict(ctx.c.execute('SELECT * FROM recoveries WHERE id=?', (inc['id'],)).fetchone())


def human_wait(conn, tid):
    task = conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (tid,)).fetchone()
    for b in conn.execute("SELECT * FROM blockers WHERE task=? AND state='asked'", (tid,)):
        if b['approval_id'] is not None:
            a = conn.execute('SELECT * FROM approvals WHERE id=?', (b['approval_id'],)).fetchone()
            if (not a or a['state'] == 'revoked' or not rules.current(a['accounts_gen'], task)
                    or a['rev'] != task['published_rev'] or a['plan_sha256'] != task['published_sha256']):
                continue
        if conn.execute("SELECT 1 FROM outbox o JOIN items i ON i.task=o.task AND i.id=o.result_ref "
                        "AND i.kind='comment' AND i.deleted=0 WHERE o.task=? AND o.idem_key=? "
                        "AND o.state='done' AND o.kind='cu_comment'", (tid, b['question_key'])).fetchone():
            return True
    stage = task['phase'].removeprefix('awaiting_') if task and task['phase'] else ''
    if stage not in ('business', 'technical'):
        return False
    row = conn.execute("SELECT result_ref FROM outbox WHERE task=? AND state='done' AND kind='cu_comment'"
                       " AND idem_key LIKE ? ORDER BY rowid DESC LIMIT 1",
                       (tid, 'clickup-dp:%s:q:%s:%%' % (tid, stage))).fetchone()
    if not row:
        return None
    base = conn.execute("SELECT * FROM items WHERE task=? AND kind='comment' AND id=? AND deleted=0",
                        (tid, row[0])).fetchone()
    if not base:
        return None
    for answer in conn.execute('SELECT * FROM items WHERE task=? AND deleted=0', (tid,)):
        if not rules.is_new(answer, base):
            continue
        if answer['command_result'] == 'closure_' + stage:
            return False
        if (answer['parent_id'] == row[0] and answer['delivered_relay'] and answer['gen'] is not None
                and answer['gen'] <= task['classified_gen']):
            return False
    return True


def binding(task, state):
    # Retry attempts, leases, poll timestamps and unrelated comments are not progress.
    return digest([{k: task[k] for k in ('clickup_task_id', 'input_gen', 'classified_gen', 'scope_gen',
                                        'published_rev', 'published_sha256', 'published_accounts_gen')},
                   state['requests'],
                   [[{k: v for k, v in row.items() if k != 'updated_ms'} for row in state[name]]
                    for name in ('blockers', 'fences')],
                   [(r['relay_ref'], r['gen'], r['state']) for r in state['relays']],
                   state.get('failure')])


def question_authority(task, state):
    return digest([{k: task[k] for k in ('scope_gen', 'published_rev', 'published_sha256', 'published_accounts_gen')},
                   [(a['id'], a['rev'], a['plan_sha256'], a['state']) for a in state['approvals']]])


def child_progress(w, shows):
    """A live successor owns the handoff, including a reviewer on another card."""
    if not isinstance(w, dict) or not isinstance(w.get('children'), list):
        return 'unknown'
    for child in w['children']:
        c = shows.get(child)
        if not isinstance(c, dict) or not isinstance(c.get('runs'), list):
            return 'unknown'
        if kp.card_status(c) not in ('done', 'archived') or kp.open_run_ids(c):
            return 'progress'
    return None


def prerequisite_status(card, state, shows, internal=()):
    """Every actual prerequisite and recorded human/fence obligation stays authoritative."""
    pending = {b['question_card'] for b in state['blockers']}
    pending |= {f['fence_card'] for f in state['fences'] if f['state'] in ('open', 'releasing', 'abandoned', 'refused')}
    if card is not None:
        w = shows.get(card)
        if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
                or not isinstance(w.get('parents'), list)):
            return 'unknown'
        pending.update(w['parents'])
    seen = set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        p = shows.get(parent)
        if (not isinstance(p, dict) or (p.get('task') or {}).get('id') != parent
                or not isinstance(p.get('parents'), list) or not isinstance(p.get('runs'), list)):
            return 'unknown'
        if (kp.card_status(p) not in ('done', 'archived') and parent not in internal) or kp.open_run_ids(p):
            return 'wait'
        pending.update(p['parents'])
    return None


def failed_question(state, shows):
    """Only a bound failed publish is internal work; no human-wait or fence bypass."""
    row = state.get('failure')
    if not row or ':bq:' not in row['idem_key'] or not state.get('dependency_scope'):
        return None
    from .engine import dependency_members
    roots, forbidden = state['dependency_scope']
    members = dependency_members(roots, shows, forbidden)
    b = next((b for b in state['blockers'] if b.get('question_key') == row['idem_key']), None)
    if not b or members.get(b['card'], {}).get('id') != b['approval_id']:
        return None
    w, q = shows.get(b['card']), shows.get(b['question_card'])
    if (not w or not q or kp.latest_run_id(w) != b['run_id'] or kp.open_run_ids(w)
            or kp.protocol_parent(b['card'], w, q, 0) != ('question', 'record')):
        return None
    return b['question_card']


def projection(task, state, shows, attachments, now, scan, admitted=False):
    """Return (classification, candidate). Unknown is not an observed empty workflow."""
    if not task or task['terminal'] or task['phase'] not in PHASES:
        return 'wait', None
    if task['hold_reason'] and not state.get('failure'):
        return 'wait', None
    if state.get('human_wait') is True:
        return 'wait', None
    if 'human_wait' in state and state['human_wait'] is None:
        return 'unknown', None
    if not rules.barrier_ok(task, 0, now, scan):
        return 'defer', None  # normal F/L scheduling is not native progress or an unknown native read
    if state.get('failure'):
        question = failed_question(state, shows)
        kind = prerequisite_status(state['requests'][0] if state['requests'] else None, state, shows,
                                   (question,) if question else ())
        if kind:
            return kind, None
        if state['requests']:
            kind = child_progress(shows.get(state['requests'][0]), shows)
            if kind:
                return kind, None
        row = state['failure']
        at = row['first_send_ms']
        if type(at) is not int:
            return 'unknown', None
        fingerprint = digest([binding(task, state), row['idem_key'], row['payload_sha256']])
        return 'actionable', dict(card='progress-' + task['clickup_task_id'] + '-request', run_id=0,
            event_kind=CLASS, event_created_s=at // 1000, block_kind=CLASS, **{'class': CLASS},
            reason_sha256=fingerprint, progress=fingerprint, approval=None, source_status=None,
            reason='An integration-owned transition exhausted definite-failure retries. The failure hold remains '
                   'in force. No native run is bound; native-object absence is not asserted. Diagnose only; '
                   'do not clear the hold, retry the source operation or recreate work.')
    if not state['requests']:
        return missing_request(task, state)
    if task['classified_gen'] != task['input_gen']:
        return 'wait', None
    card = state['requests'][0]
    w = shows.get(card)
    if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
            or not isinstance(w.get('runs'), list) or not isinstance(w.get('parents'), list)
            or not isinstance(w.get('children'), list) or not isinstance(w.get('events'), list)):
        return 'unknown', None
    queued = kp.card_status(w) in ('ready', 'todo', 'scheduled')
    if kp.card_status(w) in ('running', 'review') or kp.open_run_ids(w) or (queued and not admitted):
        return 'progress', None
    no_run = (w['runs'] == [] and all(k in w['task'] and w['task'][k] is None
              for k in ('current_run_id', 'worker_pid')) and not kp.claimed_runs(w))
    if kp.card_status(w) != 'done' and not (admitted and queued) and not (
            no_run and kp.card_status(w) in ('blocked', 'triage')):
        return 'wait', None
    if card not in attachments:
        return 'unknown', None
    run = kp.latest_run(w)
    if not run and no_run:
        at = w['task'].get('created_at')
    elif run and type(run.get('id')) is int:
        at = run.get('ended_at')
    else:
        at = None
    if type(at) is not int:
        return 'unknown', None
    if (run and run.get('worker_pid') is not None) or w['task'].get('worker_pid') is not None:
        return 'unknown', None
    kind = prerequisite_status(card, state, shows)
    if kind:
        return kind, None
    ev = rules.pass_evidence(w, attachments[card], task['clickup_task_id'])
    if (ev and task['published_rev'] == ev['rev'] and task['published_sha256'] == ev['sha256']
            and task['published_accounts_gen'] == ev['accounts_gen']):
        return execution_projection(task, state, shows, attachments, admitted=admitted)
    if any(r['state'] in ('pending', 'retry_wait', 'sending', 'ambiguous') for r in state['transitions']):
        return 'wait', None
    kind = child_progress(w, shows)
    if kind:
        return kind, None
    return 'actionable', candidate(task, state, card, w, attachments[card], None, at)


def missing_request(task, state):
    """Only an explicitly never-sent owned create can prove there is no native source."""
    rows = [r for r in state['transitions'] if r['kind'] == 'kb_create']
    if (task['phase'] != 'new_quiet' or state['approvals'] or state['blockers'] or state['fences']
            or len(rows) != 1 or not isinstance(state.get('created_ms'), int)):
        return 'unknown', None
    row = rows[0]
    if (row['attempts'] != 0 or row['result_ref'] is not None or row['first_send_ms'] is not None
            or row['child_pid'] is not None):
        return 'unknown', None  # possibly issued, never equate failed/ambiguous lookup with absence
    if row['state'] != 'pending':
        return 'wait', None
    anchor = 'progress-' + task['clickup_task_id'] + '-request'
    fingerprint = digest([binding(task, state), row['idem_key'], row['payload_sha256']])
    return 'actionable', dict(card=anchor, run_id=0, event_kind=CLASS,
        event_created_s=state['created_ms'] // 1000, block_kind=CLASS, **{'class': CLASS},
        reason_sha256=fingerprint, progress=fingerprint, approval=None, source_status=None,
        reason='The initial owned planning request has never been sent. No native source card or run is '
               'implied by this logical anchor; diagnose the existing intake transition, not native resume.')


def candidate(task, state, card, w, attachments, approval, at):
    observed = [binding(task, state), question_authority(task, state), card, sorted(w['parents']), sorted(w['children']),
                [{k: r.get(k) for k in ('id', 'profile', 'status', 'outcome', 'metadata', 'ended_at')} for r in w['runs']],
                sorted((a.get('name'), a.get('sha256')) for a in attachments
                       if re.fullmatch(r'clickup-plan-r[1-9][0-9]*\.md', str(a.get('name', ''))))]
    fingerprint = digest(observed)
    run = kp.latest_run(w)
    return dict(card=card, run_id=run['id'] if run else 0, event_kind=CLASS, event_created_s=at,
        block_kind=CLASS, **{'class': CLASS}, reason_sha256=fingerprint, progress=fingerprint,
        reason='A settled workflow has no verified required transition or owned continuation. Diagnose the current '
               'handoff through its existing owners; rejected metadata is not PASS or approval.',
        source_status=kp.card_status(w), approval=approval)


def execution_projection(task, state, shows, attachments, admitted=False):
    """Completed approved producer without its handoff; ordinary eligible queues stay quiet."""
    from . import engine
    active = [a for a in state['approvals'] if a['state'] in engine.WATCHED_STATES]
    if not active:
        return 'progress', None  # published, unanswered exact-plan approval
    a = active[-1]
    if not rules.current(a['accounts_gen'], task) or a['state'] != 'released':
        return 'wait', None
    card = a['orchestrator_card']
    w = shows.get(card)
    if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
            or not all(isinstance(w.get(k), list) for k in ('runs', 'parents', 'children', 'events'))):
        return 'unknown', None
    unowned_continuation = admitted and kp.card_status(w) in ('ready', 'todo', 'scheduled', 'review')
    if (kp.card_status(w) != 'done' and not unowned_continuation) or kp.open_run_ids(w):
        return 'progress', None
    if rules.gates_of(w, a['rev'], a['plan_sha256']) is not None:
        return 'progress', None
    run = kp.latest_run(w)
    if (not run or type(run.get('ended_at')) is not int or run.get('worker_pid') is not None
            or w['task'].get('worker_pid') is not None):
        return 'unknown', None
    for p in w['parents'] + w['children']:
        s = shows.get(p)
        if not isinstance(s, dict) or not isinstance(s.get('runs'), list):
            return 'unknown', None
        if kp.card_status(s) not in ('done', 'archived') or kp.open_run_ids(s):
            return 'wait', None
    return 'actionable', candidate(task, state, card, w, [], a, run['ended_at'])


def outer_anchor(tid):
    return 'progress-' + tid + '-workflow'


def workflow_anchor(card, tid):
    return bool(re.fullmatch(re.escape(outer_anchor(tid)) + r'(?:-after-[1-9][0-9]*)?', card))


def outer_incident(inc, state=None):
    # A continued diagnostic retains its original event and anchor identities.
    return (workflow_anchor(inc['card'], inc['task']) or inc['event_kind'] != CLASS
            or inc['id'] in (state or {}).get('continued_incidents', ()))


def continuation_current(task, state, inc):
    if inc['event_kind'] == CLASS:
        if inc['approval_id'] is None:
            return True
        from . import engine, preworker
        a = next((a for a in state['approvals'] if a['id'] == inc['approval_id']), None)
        return bool(a and a['state'] in engine.WATCHED_STATES
                    and a['rev'] == inc['approval_rev'] and a['plan_sha256'] == inc['plan_sha256']
                    and preworker.current_plan(task, a))
    from . import preworker
    _, why = preworker.authority(dict(state, enabled=True,
                                     incident=dict(inc, **{'class': preworker.CLASS}),
                                     task=dict(task, hold_reason=None)), inc['card'], inc['run_id'])
    return why is None


def continue_incident(ctx, tid, task, inc):
    """Reuse an incapable specialist's diagnostic, never its source-operation authority."""
    from . import engine
    if (not ctx.obs.progress_scan or engine.recovery_abandoned(ctx.c, inc)
            or ctx.now - inc['updated_ms'] < stalled.GRACE_MS):
        return
    state = dict(context(ctx.c, tid), handoff_unblock=ctx.cfg.handoff_unblock)
    if state.get('delivery_role'):
        return  # its own step revalidates the exact obligation; never convert it again
    if not continuation_current(task, state, inc):
        return
    _, found = outer(task, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms)
    if not found:
        return
    if found.get('delivery_only') and any(
            json.loads(row[0])['role']['progress'] == found['reason_sha256'] for row in ctx.c.execute(
                'SELECT value FROM meta WHERE key LIKE ?', ('diagnostic_delivery_history:%d:%%' % inc['id'],))):
        return  # This delivery was already diagnosed; preserve the original owner's lifecycle and budget.
    wait = engine._reconcile_cancelled_question(ctx, inc)
    if found.get('delivery_only') and verified_delivery_wait(ctx.c, tid, wait, inc):
        # Transfer responsibility, not product authority or the original question binding.
        capped = ctx.c.execute('SELECT COUNT(*) FROM recoveries WHERE task=? AND card=? AND approval_id IS ?',
                               (tid, inc['card'], inc['approval_id'])).fetchone()[0] > engine.RECOVERY_CAP
        if (inc['state'] != 'open' or state.get('delivery_role') or capped
                or inc['attempts'] >= engine.RECOVERY_ATTEMPTS or ctx.now >= inc['abs_ms']):
            return
        ctx.c.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                      ('diagnostic_history:%d:%s' % (inc['id'], inc['reason_sha256']), json.dumps(inc, sort_keys=True)))
        ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                      ('diagnostic_delivery:' + tid, json.dumps(dict(incident=inc['id'],
                       progress=found['reason_sha256'], wait_sha256=digest(wait)))))
        engine._recovery_update(ctx, inc, **{'class': CLASS})
        inc = dict(ctx.c.execute('SELECT * FROM recoveries WHERE id=?', (inc['id'],)).fetchone())
        engine._attempt(ctx, tid, inc, dict(id=inc['approval_id'], rev=inc['approval_rev'],
                                          plan_sha256=inc['plan_sha256']), found)
        return
    if engine._notice_pending(ctx, tid, inc):
        return
    if wait and wait['state'] in ('publishing', 'waiting', 'classifying'):
        return
    ctx.c.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                  ('diagnostic_history:%d:%s' % (inc['id'], inc['reason_sha256']), json.dumps(inc, sort_keys=True)))
    ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                  ('diagnostic_continuation:' + tid, json.dumps(inc['id'])))
    engine._retire_attempts(ctx, tid, inc['card'], inc['run_id'])
    engine._recovery_update(ctx, inc, **{'class': CLASS, 'reason_sha256': found['reason_sha256']})
    step(ctx, tid, task, dict(ctx.c.execute('SELECT * FROM recoveries WHERE id=?', (inc['id'],)).fetchone()))


def outer_roots(state):
    """Current owners, not historical blockers; observation grants no execution authority."""
    active = [a for a in state['approvals'] if a['state'] != 'revoked']
    roots = set(state['requests'][:1])
    if active:
        a = active[-1]
        roots.update(c for c in (a['gate_card'], a['orchestrator_card']) if c)
        from . import preworker
        roots.update(r['result_ref'] for r in preworker.rows_for(a['task'], a, state)
                     if r['kind'] == 'kb_create' and r['state'] == 'done' and r['result_ref'])
        gates = json.loads(a['clickup_gates']) if a['clickup_gates'] else {}
        roots.update(gates.get('lineage', []))
        roots.update(gates[k] for k in ('final_e2e', 'promotion', 'cleanup') if gates.get(k))
    return roots


def completed_workflow(task, state, shows, attachments, observed):
    """Positive current native completion AND reported effects, never a phase/status shortcut."""
    from . import engine
    if (task['hold_reason'] or state.get('human_wait') is not False
            or any(w.get('state') in ('publishing', 'waiting', 'classifying')
                   for w in state.get('diagnostic_waits', ()))
            or not state['requests'] or not state['approvals']
            or any(b['state'] not in engine.BLOCKER_FINAL for b in state['blockers'])
            or any(f['state'] not in ('released', 'owner_released') for f in state['fences'])
            or any(r['state'] != 'classified' for r in state['relays'])):
        return False
    a = state['approvals'][-1]
    if (a['state'] != 'promoted' or not rules.current(a['accounts_gen'], task)
            or (a['rev'], a['plan_sha256'], a['accounts_gen']) !=
               (task['published_rev'], task['published_sha256'], task['published_accounts_gen'])):
        return False
    source = state['requests'][0]
    ev = rules.pass_evidence(shows.get(source), attachments.get(source), task['clickup_task_id'])
    if not ev or (ev['rev'], ev['sha256'], ev['accounts_gen']) != (
            a['rev'], a['plan_sha256'], a['accounts_gen']):
        return False
    g = rules.gates_of(shows.get(a['orchestrator_card']), a['rev'], a['plan_sha256'])
    if (not g or not a['clickup_gates'] or g != json.loads(a['clickup_gates'])
            or not rules.guarded(shows.get(g['promotion']), a['gate_card'], a['task'], a['rev'], a['plan_sha256'])
            or not a['promoted_sha'] or rules.promoted_sha(shows.get(g['promotion'])) != a['promoted_sha']):
        return False
    for card in observed:
        w = shows[card]
        if (kp.card_status(w) not in (('done', 'archived') if card == a['gate_card'] else ('done',))
                or any(k not in w['task'] or w['task'][k] is not None for k in ('current_run_id', 'worker_pid'))
                or kp.open_run_ids(w) or any(r.get('worker_pid') is not None for r in w['runs'])):
            return False
    effects = {r['idem_key']: r for r in state['outbox'] if r['kind'] != 'alert'
               and ':rcv:' not in r['idem_key'] and ':recovery-notice:' not in r['idem_key']}
    if any(r['state'] not in ('done', 'cancelled_stale') or r['outcome'] == 'FOREIGN_ROOT'
           for r in effects.values()):
        return False
    key = 'clickup-dp:%s:status:promoted:a%d' % (a['task'], a['id'])
    status, mirror = effects.get(key), effects.get(key + '#cu')
    return bool(status and mirror and status['kind'] == 'cu_status' and mirror['kind'] == 'clickup_status'
                and status['state'] == mirror['state'] == 'done'
                and json.loads(status['body']).get('status') == 'ready for deployment'
                and json.loads(status['body']).get('task') == task['clickup_task_id'])


def outer(task, state, shows, attachments, now, scan):
    """Last resort: unchanged observed work, not a business-readiness decision."""
    if (not task or task['terminal'] or not task['eligible']
            or task['phase'] in (None, 'baseline', 'not_admitted') or task['hold_reason'] == 'operator'
            or (state.get('human_wait') is True and not state.get('delivery_failures'))
            or (state.get('handoff_unblock') is False and any(a['state'] == 'staged' for a in state['approvals']))):
        return 'wait', None
    if not rules.barrier_ok(task, 0, now, scan):
        return 'defer', None
    failures = []
    for row in state.get('delivery_failures', []):
        try:
            own = (row['idem_key'].startswith('clickup-dp:' + task['clickup_task_id'] + ':')
                   and json.loads(row['body']).get('task') == task['clickup_task_id']
                   and json.loads(row['preconditions']).get('task_id') == task['clickup_task_id'])
        except (ValueError, TypeError, AttributeError):
            own = False
        if own:
            failures.append((row['idem_key'], row['payload_sha256'], row['state'], row['outcome']))
    if failures:
        fingerprint = digest(failures)
        return 'actionable', dict(card=outer_anchor(task['clickup_task_id']), run_id=0, event_kind=CLASS,
            event_created_s=now // 1000, block_kind=CLASS, **{'class': CLASS}, reason_sha256=fingerprint,
            progress=fingerprint, approval=None, source_status=None, delivery_only=True,
            reason='ClickUp notification delivery failed: ' + ', '.join(r[0] for r in failures) + '. '
                   'This is a current local failure observation, not a claim that native work stopped. '
                   'Diagnose delivery only. Preserve the destination refusal and failure history; do not retry '
                   'the source operation, redirect a reply, clear an operator hold or resume native work. '
                   'Use only the supported delivery recovery after validating its current binding.')
    pending, seen, observed = outer_roots(state), set(), []
    unavailable = state.get('native_unavailable', False)
    while pending:
        card = pending.pop()
        if card in seen:
            continue
        seen.add(card)
        w = shows.get(card)
        if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
                or not all(isinstance(w.get(k), list) for k in ('parents', 'children', 'runs', 'comments'))):
            unavailable = True
            continue
        pending.update(w['parents'] + w['children'])
        if card in state['requests'][:1] and kp.card_status(w) == 'done' and card not in attachments:
            unavailable = True
        observed.append([card, kp.card_status(w), w['parents'], w['children'],
            [{k: r.get(k) for k in ('id', 'profile', 'status', 'outcome', 'metadata')} for r in w['runs']],
            [(c.get('author'), c.get('body')) for c in w['comments']],
            sorted((a.get('name'), a.get('sha256')) for a in attachments.get(card, [])
                   if re.fullmatch(r'clickup-plan-r[1-9][0-9]*\.md', str(a.get('name', ''))))])
    if not unavailable and completed_workflow(task, state, shows, attachments, seen):
        return 'complete', None
    local_progress = digest([binding(task, dict(state, failure=None)), task['phase'], task['hold_reason'],
        [[a[k] for k in ('id', 'rev', 'accounts_gen', 'plan_sha256', 'state', 'gate_card', 'orchestrator_card')]
         for a in state['approvals']],
        [(r['idem_key'], r['payload_sha256'], r['result_ref']) for r in state['deliveries']]])
    fingerprint = digest([local_progress, None if unavailable else sorted(observed)])
    return 'actionable', dict(card=outer_anchor(task['clickup_task_id']), run_id=0, event_kind=CLASS,
        event_created_s=now // 1000, block_kind=CLASS, **{'class': CLASS}, reason_sha256=fingerprint,
        progress=fingerprint, approval=None, source_status=None, native_unavailable=unavailable,
        local_progress=local_progress,
        reason=('Native observations are unavailable; native state and movement remain unknown. '
                'Diagnose the observation failure only; do not infer absence or perform source operations. '
                if unavailable else
                'No meaningful workflow movement or current ClickUp delivery was observed through the grace period. ')
               +
               'This is a diagnostic workflow anchor, not a native run. Inspect the existing owners and restore '
               'their already-authorized workflow through supported owner operations. Business readiness and '
               'historical questions do not veto diagnosis; every current product, approval and scope gate '
               'still applies to any actual operation. Do not stop at a warning or fabricate completion.')


def detect(ctx, tid, fallback=True):
    from . import engine
    live = ctx.c.execute("SELECT * FROM recoveries WHERE task=? AND state IN ('open','handed_off')", (tid,)).fetchall()
    if live:
        if len(live) == 1 and context(ctx.c, tid).get('delivery_failures'):
            continue_incident(ctx, tid, ctx.task(tid), dict(live[0]))
        return  # never create a competing owner or refund its budget
    task, state = ctx.task(tid), dict(context(ctx.c, tid), handoff_unblock=ctx.cfg.handoff_unblock)
    delivery = bool(state.get('delivery_failures'))
    observe = outer if delivery else projection
    kind, found = observe(task, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms)
    key = ('outer_observed:' if delivery else 'progress_observed:') + tid
    if ctx.obs.progress_scan and kind not in ('actionable', 'defer'):
        ctx.c.execute('DELETE FROM meta WHERE key=?', (key,))
    if kind != 'actionable' and fallback and ctx.obs.progress_scan:
        kind, found = outer(task, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms)
        key = 'outer_observed:' + tid
    if kind != 'actionable':
        if ctx.obs.progress_scan and kind != 'defer':  # a progress-only pass is not a complete native observation
            ctx.c.execute('DELETE FROM meta WHERE key=?', (key,))
        return
    if found['event_created_s'] * 1000 < engine._recovery_cutover(ctx):
        return
    card = found['card']
    if card == outer_anchor(tid) and state.get('completed_incidents'):
        # A new obligation AFTER verified completion gets a new logical identity.
        # Fingerprint churn within an unfinished episode never creates a fresh budget.
        card += '-after-' + str(state['completed_incidents'][-1])
    if ctx.c.execute("SELECT 1 FROM recoveries WHERE card=? AND (run_id=? OR (class=? AND state='open'))",
                     (card, found['run_id'], CLASS)).fetchone():
        return
    row = ctx.c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    previous = json.loads(row[0]) if row else None
    if not previous or previous['progress'] != found['progress']:
        keep_clock = (previous and found.get('native_unavailable') and
                      previous.get('local_progress', found.get('local_progress')) == found.get('local_progress'))
        previous = dict(progress=found['progress'], local_progress=found.get('local_progress'),
                        first_ms=previous['first_ms'] if keep_clock else ctx.now)
        ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                      (key, json.dumps(previous)))
    if ctx.now - previous['first_ms'] >= stalled.GRACE_MS:
        engine.open_incident(ctx, tid, card, found.get('approval') or
                             dict(id=None, rev=None, plan_sha256=None), found, adopted=0)


def notice_refreshable(ctx, tid, inc):
    """Only a proven never-sent cancellation permits replacement, not uncertain delivery."""
    from . import engine
    key = engine._notice_key(tid, inc, ctx.c)
    rows = ctx.c.execute('SELECT * FROM outbox WHERE idem_key IN (?,?)', (key, key + '#cu')).fetchall()
    wait = engine._diagnostic_wait(ctx, inc['id'])
    if (wait and wait.get('key') == key and wait.get('root') and wait.get('state') in ('answered', 'superseded')
            and rows and all(r['state'] == 'done' and r['result_ref'] for r in rows)):
        return True
    return bool(rows and engine._cancelled_notice(ctx, key) and all(
        r['attempts'] == 0 and r['first_send_ms'] is None and r['child_pid'] is None for r in rows))


def same_obligation(inc, state, found, shows):
    """Keep native identity; only an exact current successor-create failure may bridge to a logical observation."""
    if outer_incident(inc, state) and found['card'] == outer_anchor(inc['task']):
        return True
    requests = state['requests']
    anchor = 'progress-' + inc['task'] + '-request'
    lineage = inc['approval_id'] is None and requests and (inc['card'] == anchor or inc['card'] in requests)
    source = requests[0] if lineage else inc['card']
    if found['card'] == source:
        return True
    row = state.get('failure')
    if (inc['card'] == found['card'] == anchor and inc['run_id'] == found['run_id'] == 0
            and row and ':bq:' in row['idem_key'] and failed_question(state, shows)):
        return True
    if not lineage or found['card'] != anchor or found['run_id'] != 0 or not row or row['kind'] != 'kb_create':
        return False
    try:
        body = json.loads(row['body'])
        params = body['params']
        seq = params['seq']
        previous = next(r for r in state['transitions'] if r['kind'] == 'kb_create'
                        and r['state'] == 'done' and r['result_ref'] == source)
        linked = (type(seq) is int and seq > 1 and body['seq'] == seq and body['role'] == 'request'
                  and params['task'] == inc['task'] and params['previous_card'] == source
                  and row['idem_key'] == 'clickup-dp:%s:req:%d' % (inc['task'], seq)
                  and body['idempotency_key'] == row['idem_key']
                  and previous['idem_key'] == 'clickup-dp:%s:req:%d' % (inc['task'], seq - 1))
    except (KeyError, TypeError, ValueError, StopIteration):
        return False
    w = shows.get(source)
    return bool(linked and isinstance(w, dict) and (w.get('task') or {}).get('id') == source
                and isinstance(w.get('runs'), list) and kp.card_status(w) != 'running'
                and not kp.open_run_ids(w) and w['task'].get('worker_pid') is None
                and all(r.get('worker_pid') is None for r in w['runs']))


def step(ctx, tid, task, inc):
    from . import engine
    if engine.recovery_abandoned(ctx.c, inc):
        return
    state = dict(context(ctx.c, tid), handoff_unblock=ctx.cfg.handoff_unblock)
    if outer_incident(inc, state) and not ctx.obs.progress_scan:
        return
    if not continuation_current(task, state, inc):
        return
    if state.get('delivery_role'):
        _, found = outer(task, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms)
        if (not delivery_current(inc, state, found) or inc['state'] != 'open'
                or ctx.now >= inc['abs_ms'] or inc['attempts'] >= engine.RECOVERY_ATTEMPTS):
            return
        due = inc['detected_ms'] + engine.RECOVERY_RETRY_MIN.get(inc['attempts'], 0) * engine._MIN_MS
        if ctx.now >= due:
            engine._attempt(ctx, tid, inc, dict(id=inc['approval_id'], rev=inc['approval_rev'],
                                              plan_sha256=inc['plan_sha256']), found)
        return  # the original question and every native operation remain independently gated
    if inc['event_kind'] != CLASS:
        a = next(a for a in state['approvals'] if a['id'] == inc['approval_id'])
        owner = ctx.obs.cards.get(a['orchestrator_card'])
        if (a['orchestrator_card'] in ctx.obs.workers or
                (a['state'] in ('started', 'implemented', 'ticketed', 'promoted')
                 and rules.gates_of(owner, a['rev'], a['plan_sha256']) is not None)):
            engine._recovery_update(ctx, inc, state='resumed')
            return
    # Request rows are the supported successor lineage. Keep the incident anchor immutable,
    # but verify the outstanding publication against the current legitimate request.
    source_card = (state['requests'][0] if inc['approval_id'] is None and state['requests']
                   and (inc['card'].startswith('progress-') or inc['card'] in state['requests']) else inc['card'])
    source = ctx.obs.cards.get(source_card)
    if inc['approval_id'] is None:
        ev = rules.pass_evidence(source, ctx.obs.attachments.get(source_card), tid)
        completed = bool(ev and task['published_rev'] == ev['rev'] and task['published_sha256'] == ev['sha256']
                         and task['published_accounts_gen'] == ev['accounts_gen'])
    else:
        a = next((a for a in state['approvals'] if a['id'] == inc['approval_id']), None)
        completed = bool(a and a['state'] in ('started', 'implemented', 'ticketed', 'promoted')
                         and rules.gates_of(source, a['rev'], a['plan_sha256']) is not None)
    whole_workflow = outer_incident(inc, state)
    # A continued native obligation still settles only through its verified source contract.
    if completed and inc['event_kind'] == CLASS and not workflow_anchor(inc['card'], tid):
        engine._retire_attempts(ctx, tid, inc['card'], inc['run_id'])
        engine._recovery_update(ctx, inc, state='closed')
        return
    observe = outer if whole_workflow else projection
    kind, found = observe(task, state, ctx.obs.cards, ctx.obs.attachments,
                          ctx.now, ctx.obs.scan_started_ms, **({} if whole_workflow else {'admitted': True}))
    if whole_workflow and kind == 'complete':
        engine._retire_attempts(ctx, tid, inc['card'], inc['run_id'])
        engine._recovery_update(ctx, inc, state='closed')
        ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                      ('completed_workflow:' + tid, str(inc['id'])))
        return
    if not found or not same_obligation(inc, state, found, ctx.obs.cards):
        if not found and not whole_workflow:
            continue_incident(ctx, tid, task, inc)
        return
    if engine._notice_pending(ctx, tid, inc):
        return
    wait = engine._reconcile_cancelled_question(ctx, inc)
    if wait and wait['state'] in ('publishing', 'waiting', 'classifying'):
        return
    if found['reason_sha256'] != inc['reason_sha256']:
        key = 'progress_observed:' + tid
        row = ctx.c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        previous = json.loads(row[0]) if row else None
        if ctx.now < inc['abs_ms']:
            if not previous or previous['progress'] != found['progress']:
                ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                              (key, json.dumps(dict(progress=found['progress'], first_ms=ctx.now))))
                return
            if ctx.now - previous['first_ms'] < stalled.GRACE_MS:
                return
        a = found.get('approval') or {}
        if a.get('id') != inc['approval_id'] or (ctx.c.execute(
                "SELECT 1 FROM outbox WHERE idem_key=?", (engine._notice_key(tid, inc, ctx.c),)).fetchone()
                and not notice_refreshable(ctx, tid, inc)):
            return  # possibly delivered explanation cannot manufacture a new budget
        ctx.c.execute('INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)',
                      ('diagnostic_history:%d:%s' % (inc['id'], inc['reason_sha256']), json.dumps(inc, sort_keys=True)))
        engine._retire_attempts(ctx, tid, inc['card'], inc['run_id'])
        engine._recovery_update(ctx, inc, reason_sha256=found['reason_sha256'])
        inc = dict(ctx.c.execute('SELECT * FROM recoveries WHERE id=?', (inc['id'],)).fetchone())
    defer = 0
    for row in ctx.c.execute("SELECT result_ref FROM outbox WHERE task=? AND kind='kb_create' AND state='done'"
                             " AND idem_key LIKE ? ORDER BY rowid DESC",
                             (tid, engine._rcv_key(tid, inc['card'], inc['run_id'], 0)[:-1] + '%')):
        ack, comment = stalled.diagnostic_ack(ctx.obs.cards.get(row[0]), inc,
            wait.get('ack_s', -1) if wait and wait['state'] in ('answered', 'superseded') else -1)
        if ack['working_s'] is not None:
            defer = max(defer, ack['working_s'] * 1000 + engine.RECOVERY_DEFER_MIN * engine._MIN_MS)
        if ack['final'] == 'cannot_act' and (notice_refreshable(ctx, tid, inc) or not ctx.c.execute(
                'SELECT 1 FROM outbox WHERE idem_key=?', (engine._notice_key(tid, inc, ctx.c),)).fetchone()):
            engine._escalate(ctx, tid, inc, 'coordinator could not establish a supported continuation')
            return
        if ack['final'] == 'owner':
            question = stalled.question_projection(comment, inc)
            if question:
                engine._escalate(ctx, tid, inc, 'coordinator: owner', question=question, ack_s=ack['final_s'])
                return
    n, detected = inc['attempts'], inc['detected_ms']
    capped = ctx.c.execute('SELECT COUNT(*) FROM recoveries WHERE task=? AND card=? AND approval_id IS ?',
                          (tid, inc['card'], inc['approval_id'])).fetchone()[0] > engine.RECOVERY_CAP
    if n == 0 and capped:
        engine._escalate(ctx, tid, inc, 'incident cap reached')
        return
    if ctx.now >= inc['abs_ms'] or (n >= engine.RECOVERY_ATTEMPTS and
            ctx.now >= max(detected + engine.RECOVERY_ESCALATE_MIN * engine._MIN_MS, defer)):
        engine._escalate(ctx, tid, inc, 'no verified publication after %d AI wakes' % n)
        return
    if n < engine.RECOVERY_ATTEMPTS:
        due = min(max(detected + engine.RECOVERY_RETRY_MIN.get(n, 0) * engine._MIN_MS, defer), inc['abs_ms'])
        if ctx.now >= due:
            engine._attempt(ctx, tid, inc, dict(id=inc['approval_id'], rev=inc['approval_rev'],
                                              plan_sha256=inc['plan_sha256']), found)



def verdict(snapshot, card, run, notice=False):
    if not isinstance(snapshot, dict):
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    inc, task = snapshot.get('incident'), snapshot.get('task')
    if (notice and task and task['hold_reason'] and task['hold_reason'] == snapshot.get('delivery_hold')):
        task = dict(task, hold_reason=None)  # exact failed-own-carrier notice only; never native work
    if not snapshot.get('enabled') or not inc or inc['class'] != CLASS or inc['state'] != 'open' or snapshot.get('abandoned'):
        return kp.RecoveryVerdict('STALE', 'incident_not_open')
    whole_workflow = task and outer_incident(inc, snapshot)
    if task and not continuation_current(task, snapshot, inc):
        return kp.RecoveryVerdict('STALE', 'approval_changed')
    delivery_hold = bool(task and snapshot.get('delivery_role') and any(
        task['hold_reason'] in ('outbox:' + row['idem_key'], 'outbox:' + row['idem_key'] + '#cu')
        for row in snapshot.get('delivery_failures', ())))
    if inc['card'] != card or inc['run_id'] != run or not task or task['terminal'] or (
            task['hold_reason'] and not snapshot.get('failure') and not whole_workflow and not delivery_hold):
        return kp.RecoveryVerdict('STALE', 'authority_changed')
    if snapshot.get('delivery_role'):
        if snapshot['now_ms'] >= inc['abs_ms']:
            return kp.RecoveryVerdict('STALE', 'delivery_deadline')
        if snapshot['now_ms'] - (task['last_light_ms'] or 0) > 300000:
            return kp.RecoveryVerdict('UNAVAILABLE', 'input_observation')
        _, found = outer(task, snapshot, snapshot.get('shows', {}), snapshot.get('attachments', {}),
                         snapshot['now_ms'], snapshot['scan_started_ms'])
        if not delivery_current(inc, snapshot, found):
            return kp.RecoveryVerdict('STALE', 'delivery_binding_changed')
        return kp.RecoveryVerdict('CURRENT', 'incident=%d card=%s run=%d scope=delivery-only' % (inc['id'], card, run))
    wait = snapshot.get('diagnostic_wait') or {}
    if not notice and wait.get('state') in ('publishing', 'waiting', 'classifying'):
        return kp.RecoveryVerdict('STALE', 'diagnostic_human_wait')
    if snapshot['now_ms'] - (task['last_light_ms'] or 0) > 300000:
        return kp.RecoveryVerdict('UNAVAILABLE', 'input_observation')
    whole_workflow = outer_incident(inc, snapshot)
    observe = outer if whole_workflow else projection
    kind, found = observe(task, snapshot, snapshot.get('shows', {}), snapshot.get('attachments', {}),
                          snapshot['now_ms'], snapshot['scan_started_ms'],
                          **({} if whole_workflow else {'admitted': True}))
    if kind in ('unknown', 'defer'):
        return kp.RecoveryVerdict('UNAVAILABLE', 'progress_observation')
    if (not found or not same_obligation(inc, snapshot, found, snapshot.get('shows', {}))
            or found['reason_sha256'] != inc['reason_sha256']):
        return kp.RecoveryVerdict('STALE', 'progress_changed')
    return kp.RecoveryVerdict('CURRENT', 'incident=%d card=%s run=%d approval=%s revision=%s class=%s' %
                             (inc['id'], card, run, json.dumps(inc['approval_id']), json.dumps(inc['approval_rev']), CLASS))
