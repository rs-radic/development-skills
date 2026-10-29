"""Approval-staging diagnostics. An approval anchor is NOT a native card or run."""
import hashlib
import json

from . import kanban_port as kp, rules, stalled

CLASS = 'approval_staging'
STATES = ('recorded', 'staged')
PHASES = ('awaiting_approval', 'approval_queued', 'development', 'implemented',
          'promotion_window', 'promotion_held', 'ready_for_deployment')


def anchor(approval):
    return 'approval-%d' % approval['id']


def current_plan(task, approval):
    return (rules.current(approval['accounts_gen'], task) and approval['rev'] == task['published_rev']
            and approval['plan_sha256'] == task['published_sha256'])


def context(conn, tid):
    return dict(stalled.context(conn, tid), requests=[r[0] for r in conn.execute(
        "SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq DESC", (tid,))],
        sources=[json.loads(r[0]) for r in conn.execute(
            "SELECT body FROM outbox WHERE task=? AND kind='cu_attach' AND state='done' AND idem_key LIKE ?"
            " ORDER BY rowid DESC", (tid, 'clickup-dp:%s:pub:%%:attach' % tid))],
        stage_rows=[dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE task=? AND (idem_key LIKE ? OR idem_key LIKE ?)",
        (tid, 'clickup-dp:%s:gate:%%' % tid, 'clickup-dp:%s:orch:%%' % tid))])


def rows_for(tid, approval, state):
    bases = ['clickup-dp:%s:%s:r%d:g%d' % (tid, role, approval['rev'], approval['accounts_gen'])
             for role in ('gate', 'orch')]
    return [r for r in state['stage_rows'] if any(r['idem_key'] == b or r['idem_key'].startswith(b + ':')
                                                for b in bases)]


def targets(approval, state):
    return {c for c in (approval['gate_card'], approval['orchestrator_card']) if c} | {
        r['result_ref'] for r in rows_for(approval['task'], approval, state)
        if r['kind'] == 'kb_create' and r['state'] == 'done' and r['result_ref']}


def source_evidence(show, attachments, task):
    """Early staging retains the full completed current-review PASS contract."""
    final = kp.latest_run(show)
    if not final or final.get('profile') != 'plan-reviewer' or final.get('outcome') != 'completed':
        return None
    return rules.pass_evidence(show, attachments, task)


def inspect(task, approval, state, shows, attachments, now, scan, admitted=False):
    """Return an observed stage, or None for unknown/quiet inputs. No fabricated native observations."""
    if (task['terminal'] or task['hold_reason'] or task['phase'] not in PHASES
            or (not admitted and approval['state'] not in STATES) or not current_plan(task, approval)
            or not rules.barrier_ok(task, 0, now, scan)):
        return None
    source = next((r for r in state['requests'] if (ev := source_evidence(
        shows.get(r), attachments.get(r), task['clickup_task_id'])) and ev['rev'] == approval['rev']
        and ev['sha256'] == approval['plan_sha256']), None)
    if source is None:
        return None
    # Historical requests are source candidates, not obligations. Every actual
    # ancestor of the source and current stage targets remains an obligation.
    pending, seen = {source}, set()
    for card in targets(approval, state):
        w = shows.get(card)
        continuing = admitted and card == approval['orchestrator_card']
        if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
                or not isinstance(w.get('runs'), list) or not isinstance(w.get('parents'), list)
                or any(not isinstance(p, str) for p in w['parents'])
                or kp.card_status(w) not in (('blocked', 'todo', 'ready', 'done', 'running', 'review')
                                            if continuing else ('blocked', 'todo', 'ready', 'done'))
                or (kp.open_run_ids(w) and not continuing)):
            return None
        pending.update(w['parents'])
    # A failed read is absent, not an empty attachment list or a missing native object.
    while pending:
        card = pending.pop()
        if card in seen:
            continue
        seen.add(card)
        w = shows.get(card)
        if (not isinstance(w, dict) or (w.get('task') or {}).get('id') != card
                or not isinstance(w.get('parents'), list) or kp.open_run_ids(w)
                or any(not isinstance(p, str) for p in w['parents'])
                or kp.card_status(w) not in ('done', 'archived')):
            return None
        pending.update(w['parents'])
    markers = stalled.plan_markers(task, approval, shows, attachments, state)
    for b in state['blockers']:
        q = shows.get(b['question_card'])
        if (not isinstance(q, dict) or (q.get('task') or {}).get('id') != b['question_card']
                or kp.open_run_ids(q)):
            return None
        if kp.card_status(q) in ('done', 'archived'):
            continue
        incorporated = {'blocker': b['id'], 'question': b['question_card'], 'card': b['card'],
                        'gen': b['closure_gen']}
        if (kp.card_status(q) != 'blocked' or b['state'] != 'scope_change'
                or approval['id'] <= (b['approval_id'] or 0) or b['closure_gen'] is None
                or b['closure_gen'] > approval['accounts_gen'] or incorporated not in markers):
            return None
    if any(f['state'] in ('open', 'releasing', 'abandoned', 'refused') for f in state['fences']):
        return None
    rows = rows_for(task['clickup_task_id'], approval, state)
    if any((r['state'] == 'retry_wait' and (r['next_attempt_ms'] or 0) > now)
           or (r['state'] == 'sending' and r['child_pid']
               and kp.child_alive(r['child_pid'], r['child_start_time'])) for r in rows):
        return None
    binding = [approval['id'], approval['rev'], approval['accounts_gen'], approval['plan_sha256']]
    fingerprint = hashlib.sha256(json.dumps(binding).encode()).hexdigest()
    progress = [approval['state'], approval['gate_card'], approval['orchestrator_card'],
                sorted((r['idem_key'], r['result_ref']) for r in rows if r['state'] == 'done')]
    return dict(run_id=0, event_kind=CLASS, event_created_s=approval['comment_date'] // 1000,
                block_kind=CLASS, **{'class': CLASS}, reason_sha256=fingerprint,
                reason='Approved intake staging has made no verified progress; use the normal intake poll, '
                       'not a native worker resume. No native card or run is implied.', source_status=None,
                progress=hashlib.sha256(json.dumps(progress).encode()).hexdigest())


def authority(snapshot, card, run):
    """Same approval identity throughout retries; changed context never creates a new budget."""
    from . import engine
    inc, task = snapshot.get('incident'), snapshot.get('task')
    if not snapshot.get('enabled') or not inc or inc['class'] != CLASS:
        return None, 'no_incident'
    a = next((a for a in snapshot['approvals'] if a['id'] == inc['approval_id']), None)
    if (inc['card'] != card or run != 0 or not a or anchor(a) != card
            or a['rev'] != inc['approval_rev'] or a['plan_sha256'] != inc['plan_sha256']):
        return None, 'approval_changed'
    if (not task or task['terminal'] or task['phase'] not in PHASES
            or a['state'] not in STATES + engine.WATCHED_STATES
            or not current_plan(task, a)
            or any(x['id'] > a['id'] and x['state'] in engine.ACTIVE + engine.WATCHED_STATES
                   for x in snapshot['approvals'])):
        return None, 'approval_not_current'
    if task['hold_reason']:
        return None, 'held'
    if a['state'] == 'staged' and not snapshot.get('handoff_unblock', True):
        return None, 'handoff_disabled'
    return a, None


def verdict(snapshot, card, run, notice=False):
    if not isinstance(snapshot, dict):
        return kp.RecoveryVerdict('UNAVAILABLE', 'state_unavailable')
    a, why = authority(snapshot, card, run)
    if why:
        return kp.RecoveryVerdict('STALE', why)
    inc = snapshot['incident']
    if inc['state'] != 'open' or snapshot.get('abandoned'):
        return kp.RecoveryVerdict('STALE', 'incident_not_open')
    wait = snapshot.get('diagnostic_wait') or {}
    if not notice and wait.get('state') in ('publishing', 'waiting', 'classifying'):
        return kp.RecoveryVerdict('STALE', 'diagnostic_human_wait' if wait.get('root') else 'diagnostic_notice_unfinished')
    found = inspect(snapshot['task'], a, snapshot, snapshot.get('shows', {}), snapshot.get('attachments', {}),
                    snapshot['now_ms'], snapshot['scan_started_ms'], admitted=True)
    if found is None or snapshot['now_ms'] - (snapshot['task']['last_light_ms'] or 0) > 5 * 60_000:
        return kp.RecoveryVerdict('UNAVAILABLE', 'staging_observation')
    if found['reason_sha256'] != inc['reason_sha256']:
        return kp.RecoveryVerdict('STALE', 'approval_changed')
    return kp.RecoveryVerdict('CURRENT', 'incident=%d card=%s run=0 approval=%d rev=%d class=%s' %
                            (inc['id'], card, a['id'], a['rev'], CLASS))


def pending_question(ctx, approval_id):
    from . import engine
    if engine._recovery_cutover(ctx) is None:
        return False  # additive recovery tables need not exist while the feature is disabled
    for inc in map(dict, ctx.c.execute("SELECT * FROM recoveries WHERE approval_id=? AND class=? AND state='open'",
                                       (approval_id, CLASS))):
        wait = engine._reconcile_cancelled_question(ctx, inc)
        if wait and wait['state'] in ('publishing', 'waiting', 'classifying'):
            return True
    return False


def detect(ctx, tid):
    from . import engine
    task, state = ctx.task(tid), context(ctx.c, tid)
    approvals = [dict(r) for r in ctx.c.execute('SELECT * FROM approvals WHERE task=? ORDER BY id', (tid,))]
    for a in approvals:
        if a['state'] == 'staged' and not ctx.cfg.handoff_unblock:
            continue
        if a['state'] not in STATES or any(x['id'] > a['id'] and x['state'] in engine.ACTIVE for x in approvals):
            continue
        found = inspect(task, a, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms)
        if found is None:
            continue
        key = 'stage_observed:' + tid + ':' + anchor(a)
        row = ctx.c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        previous = json.loads(row[0]) if row else None
        if not previous or previous['progress'] != found['progress']:
            ctx.c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                          (key, json.dumps(dict(progress=found['progress'], first_ms=ctx.now))))
            continue
        if ctx.now - previous['first_ms'] < stalled.GRACE_MS:
            continue
        if not ctx.c.execute('SELECT 1 FROM recoveries WHERE card=? AND run_id=0', (anchor(a),)).fetchone():
            engine.open_incident(ctx, tid, anchor(a), a, found, adopted=0)


def step(ctx, tid, task, inc):
    from . import engine
    state = context(ctx.c, tid)
    apps = [dict(r) for r in ctx.c.execute('SELECT * FROM approvals WHERE task=?', (tid,))]
    snap = dict(state, task=task, incident=inc, approvals=apps, enabled=True,
                handoff_unblock=ctx.cfg.handoff_unblock)
    a, why = authority(snap, inc['card'], 0)
    if why:
        if why not in ('held', 'handoff_disabled') and task['classified_gen'] == task['input_gen']:
            engine._recovery_update(ctx, inc, state='superseded')
        return
    if engine.recovery_abandoned(ctx.c, inc):
        return
    w = ctx.obs.cards.get(a['orchestrator_card'])
    # Continue the original incident across release/claim. Only proven worker progress or
    # the normal completed orchestration contract settles it, never READY or a dead claim.
    if (a['orchestrator_card'] in ctx.obs.workers or
            (a['state'] in ('started', 'implemented', 'ticketed', 'promoted')
             and rules.gates_of(w, a['rev'], a['plan_sha256']) is not None)):
        engine._recovery_update(ctx, inc, state='resumed')
        return
    found = inspect(task, a, state, ctx.obs.cards, ctx.obs.attachments, ctx.now, ctx.obs.scan_started_ms,
                    admitted=True)
    if found is None:
        return
    if engine._notice_pending(ctx, tid, inc):
        return
    wait = engine._reconcile_cancelled_question(ctx, inc)
    if wait and wait['state'] in ('publishing', 'waiting', 'classifying'):
        return
    # ACKs belong to the real carrier, never to the logical approval anchor.
    for row in ctx.c.execute("SELECT result_ref FROM outbox WHERE task=? AND kind='kb_create' AND state='done'"
                             " AND idem_key LIKE ? ORDER BY rowid DESC", (tid, engine._rcv_key(tid, inc['card'], 0, 0)[:-1] + '%')):
        show = ctx.obs.cards.get(row[0])
        ack, comment = stalled.diagnostic_ack(show, inc, wait.get('ack_s', -1) if wait else -1)
        if ack['final'] == 'owner':
            question = stalled.question_projection(comment, inc)
            if question:
                engine._escalate(ctx, tid, inc, 'coordinator: owner', question=question, ack_s=ack['final_s'])
                return
    if ctx.now >= inc['abs_ms'] or inc['attempts'] >= engine.RECOVERY_ATTEMPTS:
        return  # still unfinished: never manufacture a human question or a completed recovery
    due = inc['detected_ms'] + engine.RECOVERY_RETRY_MIN.get(inc['attempts'], 0) * engine._MIN_MS
    if ctx.now >= due:
        engine._attempt(ctx, tid, inc, a, found)
