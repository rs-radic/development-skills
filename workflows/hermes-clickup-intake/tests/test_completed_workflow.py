"""Completed/reporting proof through real World.main scans; external ports are fake."""
import copy
import json
import unittest
from unittest import mock

import test_general_stall_wake as fixture
from clickup_intake import engine, main, progress, rules
from clickup_intake.contracts import KanbanResult, Outcome
from clickup_intake.store import Store


class CompletedWorkflowTest(unittest.TestCase):
    carriers = fixture.GeneralStallWakeTest.carriers

    def setUp(self):
        fixture.GeneralStallWakeTest.setUp(self)
        native_snapshot = self.b._preworker_snapshot

        def snapshot(*args):
            proof = native_snapshot(*args)
            if proof:
                # The shared native-schema fixture omits completed_at. Preserve
                # the timestamp produced by the same FakeBoard.finish lifecycle.
                for card, shown in proof.shows.items():
                    shown['task']['completed_at'] = self.b.cards[card]['task'].get('completed_at')
            return proof

        self.b._preworker_snapshot = snapshot

    def scans(self, count, seconds=1801):
        for _ in range(count):
            self.w.clock.advance(seconds)
            rc, lines = self.w.run()
            self.assertEqual(rc, 0, lines)

    def promoted(self):
        try:
            self.life.full()
        except AssertionError:
            print('SETUP_STATE ' + json.dumps(dict(tasks=self.w.q('SELECT * FROM tasks'),
                approvals=self.w.q('SELECT * FROM approvals'),
                gates={k: v for k, v in self.b.cards.items() if k in (self.life.gate(), self.life.promo)},
                pending=self.w.q("SELECT idem_key,state,outcome FROM outbox WHERE state<>'done'")), default=str))
            raise
        [a] = self.w.q('SELECT * FROM approvals WHERE task=?', self.tid)
        self.assertEqual(a['state'], 'promoted')
        self.cleanup = json.loads(a['clickup_gates'])['cleanup']
        self.assertNotEqual(self.b.cards[self.cleanup]['task']['status'], 'done')
        return a

    def completed(self):
        a = self.promoted()
        self.b.finish(self.cleanup, 'implementor')
        self.scans(3)
        key = 'clickup-dp:%s:status:promoted:a%d' % (self.tid, a['id'])
        rows = self.w.q('SELECT * FROM outbox WHERE idem_key IN (?,?)', key, key + '#cu')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['state'] == 'done' for row in rows))
        response = self.w.cu.request('GET', '/v2/task/' + self.tid)
        self.assertIs(response.outcome, Outcome.OK)
        self.assertEqual(response.json['status']['status'], 'ready for deployment')
        return a

    def nested_cleanup(self, depth=2, ready_leaf=False):
        parent, descendants = self.cleanup, []
        for level in range(depth):
            card = self.b.worker_create('Nested cleanup work %d' % level, parents=[parent])
            descendants.append(card)
            if not ready_leaf or level < depth - 1:
                self.b.finish(card, 'implementor')
            parent = card
        return descendants

    def test_initial_nested_completion_without_incident_stays_quiet(self):
        self.promoted()
        descendants = self.nested_cleanup(depth=3)
        self.b.finish(self.cleanup, 'implementor')
        native = copy.deepcopy(self.b.cards)
        self.assertTrue(all(native[c]['task']['status'] == 'done' for c in descendants))
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])
        self.scans(10)
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [],
                         'initial nested completion must be observed before any retirement marker exists')
        self.assertEqual(self.carriers(), [])
        self.assertEqual(self.b.cards, native)

    def test_initial_nested_completion_retires_without_refunding_incident(self):
        before = self.incomplete_incident()
        self.nested_cleanup()
        self.b.finish(self.cleanup, 'implementor')
        native = copy.deepcopy(self.b.cards)
        self.scans(3)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'closed', 'finished descendants must retire the initial incident')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.assertNotEqual(self.w.main('recovery-check', '--card', after['card'], '--run', '0')[0], 0)
        carriers = self.carriers()
        self.w.clock.now_ms = before['abs_ms'] + 60000
        self.scans(10)
        self.assertEqual(self.carriers(), carriers)
        self.assertEqual(self.w.q('SELECT * FROM recoveries')[0], after)
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)

    def test_later_nested_ready_work_is_observed_then_retires(self):
        before = self.retired()
        descendants = self.nested_cleanup(ready_leaf=True)
        leaf = descendants[-1]
        native = copy.deepcopy(self.b.cards)
        self.assertEqual(native[leaf]['task']['status'], 'ready')
        self.scans(6)
        old, inc = self.w.q('SELECT * FROM recoveries ORDER BY id')
        self.assertEqual(old['state'], 'closed')
        self.assertEqual(inc['state'], 'open')
        self.assertEqual(inc['card'], progress.outer_anchor(self.tid) + '-after-' + str(before['id']))
        key = engine._rcv_key(self.tid, inc['card'], 0, 0)[:-1] + '%'
        carriers = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND kind='kb_create' ORDER BY rowid", key)
        self.assertTrue(carriers)
        with self.subTest(phase='diagnosis'):
            self.assertNotIn('Native observations are unavailable', carriers[-1]['body'])
            self.assertEqual(carriers[-1]['state'], 'done')
            wakes = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND idem_key LIKE '%:wake'", key)
            self.assertTrue(any(w['state'] == 'done' for w in wakes))
            check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
            self.assertEqual(check[0], 0, check)
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)
        self.b.finish(leaf, 'implementor')
        self.scans(3)
        old, after = self.w.q('SELECT * FROM recoveries ORDER BY id')
        with self.subTest(phase='completion'):
            self.assertEqual(after['state'], 'closed', 'later nested work must retire once actually finished')
            for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
                self.assertEqual(after[field], inc[field], field)
            self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(old[field], before[field], field)
        print('NESTED_COMPLETION ' + json.dumps(dict(descendants=descendants, before=inc, after=after)))

    def assert_nested_unknown(self, before):
        native = copy.deepcopy(self.b.cards)
        self.scans(3)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'open')
        self.assertEqual(after['abs_ms'], before['abs_ms'])
        self.assertGreaterEqual(after['attempts'], before['attempts'])
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key=?", 'completed_workflow:' + self.tid), [])
        key = engine._rcv_key(self.tid, after['card'], 0, 0)[:-1] + '%'
        carriers = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND idem_key LIKE ? ORDER BY rowid", key)
        self.assertTrue(carriers)
        self.assertIn('Native observations are unavailable', carriers[-1]['body'])
        self.assertIn('do not infer absence or perform source operations', carriers[-1]['body'])
        check = self.w.main('recovery-check', '--card', after['card'], '--run', '0')
        self.assertEqual(check[0], 0, check)  # CURRENT diagnostic only, never source-operation authority
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)

    def test_nested_descendant_read_failure_stays_diagnostic_until_restored(self):
        before = self.incomplete_incident()
        descendants = self.nested_cleanup()
        self.b.finish(self.cleanup, 'implementor')
        snapshot = self.b._preworker_snapshot
        failed = []
        def unavailable(roots, take, clock):
            if descendants[-1] in roots:
                failed.append(True)
                return None
            return snapshot(roots, take, clock)
        with mock.patch.object(self.b, '_preworker_snapshot', side_effect=unavailable):
            self.assert_nested_unknown(before)
        self.assertTrue(failed, 'the failed descendant read must actually be requested')
        self.scans(3)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'closed')

    def test_nested_descendant_node_bound_stays_diagnostic(self):
        from clickup_intake.preworker_snapshot import MAX_DEPENDENCY_NODES
        before = self.incomplete_incident()
        parent = self.nested_cleanup(depth=1)[0]
        for _ in range(MAX_DEPENDENCY_NODES):
            card = self.b.worker_create('Completed bounded descendant', parents=[parent])
            self.b.finish(card, 'implementor')
        self.b.finish(self.cleanup, 'implementor')
        self.assert_nested_unknown(before)

    def test_nested_descendant_read_bound_stays_diagnostic(self):
        before = self.incomplete_incident()
        self.nested_cleanup(depth=8)
        self.b.finish(self.cleanup, 'implementor')
        take = main._PreworkerReads.take
        attempts = []
        def counted(reader):
            result = take(reader)
            attempts.append((reader.remaining, reader.exhausted))
            return result
        with mock.patch.object(main._PreworkerReads, 'take', counted):
            self.assert_nested_unknown(before)
        self.assertTrue(any(exhausted for _, exhausted in attempts))
        self.assertTrue(all(0 <= remaining <= main.PREWORKER_READ_LIMIT for remaining, _ in attempts))

    def test_nested_descendant_acquisition_time_bound_stays_unknown(self):
        self.completed()
        self.nested_cleanup()
        store = Store(self.w.state_path, self.w.clock)
        try:
            state = progress.context(store.conn, self.tid)
        finally:
            store.close()
        snapshot = self.b._preworker_snapshot
        elapsed = [0]
        calls = []
        def slow(roots, take, clock):
            proof = snapshot(roots, take, clock)
            calls.append(set(roots))
            elapsed[0] += 180001  # each acquisition fits; the complete closure does not
            return proof
        reader = main._PreworkerReads(self.b, lambda: self.w.clock() + elapsed[0])
        with mock.patch.object(self.b, '_preworker_snapshot', side_effect=slow):
            shows, atts, complete = main._progress_collect(state, reader)
        self.assertGreater(len(calls), 1)
        self.assertGreater(elapsed[0], 300000)
        self.assertEqual((shows, atts, complete), ({}, {}, False))
        self.assertIsNotNone(reader.proof)
        self.assertFalse(reader.proof.valid())
        reader.proof.close()

    def test_completed_delivered_workflow_stays_quiet_across_future_scans(self):
        self.completed()
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])
        self.scans(25)
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [],
                         'complete native lineage plus delivered promotion must not create an outer stall')
        self.assertEqual(self.carriers(), [])

    def retired(self):
        self.promoted()
        self.scans(2)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(before['card'], progress.outer_anchor(self.tid))
        self.assertEqual((before['state'], before['attempts']), ('open', 1))
        self.assertEqual(self.w.main('recovery-check', '--card', before['card'], '--run', '0')[0], 0)
        self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
        self.scans(1)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(before['attempts'], 2, 'retirement must preserve an already-spent retry')
        self.b.finish(self.cleanup, 'implementor')
        self.scans(3)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'closed', 'outer diagnostic must settle on positive completed proof')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.assertNotEqual(self.w.main('recovery-check', '--card', before['card'], '--run', '0')[0], 0)
        carriers = self.carriers()
        self.w.clock.now_ms = before['abs_ms'] + 60000
        self.scans(25)
        self.assertEqual(self.carriers(), carriers)
        self.assertEqual(self.w.q('SELECT * FROM recoveries')[0]['attempts'], before['attempts'])
        return before

    def test_open_outer_incident_retires_only_after_cleanup_and_preserves_budget(self):
        self.retired()

    def queue_notice(self, suffix='independent-completion-notice'):
        key = 'clickup-dp:' + self.tid + ':' + suffix
        store = Store(self.w.state_path, self.w.clock)
        try:
            engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config()).comment(
                self.tid, key, 'Independent workflow notification.')
        finally:
            store.close()
        return key

    def fail_notice(self):
        key = self.queue_notice()
        for _ in range(6):
            self.w.cu.inject('POST', '/v2/task/' + self.tid + '/comment', Outcome.DEFINITE_FAIL, status=503)
        for _ in range(10):
            self.scans(1)
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
            if row['state'] == 'failed':
                break
        self.assertEqual((row['state'], row['attempts']), ('failed', 6))
        return key

    def test_later_independent_http_failure_has_new_delivered_current_owner(self):
        before = self.retired()
        native = copy.deepcopy(self.b.cards)
        key = self.fail_notice()
        # The pending notification can already own an inactivity diagnostic.
        # Let its unchanged bounded retry cadence publish the actual failure.
        for _ in range(8):
            if self.w.q("SELECT 1 FROM outbox WHERE kind='kb_create' AND body LIKE ?", '%' + key + '%'):
                break
            self.scans(1)
        incidents = self.w.q('SELECT * FROM recoveries ORDER BY id')
        self.assertEqual(len(incidents), 2, 'closed completion must not permanently suppress new failures')
        old, new = incidents
        for field in ('id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(old[field], before[field], field)
        self.assertEqual(old['state'], 'closed')
        self.assertEqual(new['state'], 'open')
        self.assertIn(new['attempts'], (1, 2, 3))
        self.assertGreater(new['detected_ms'], before['abs_ms'])
        self.assertGreater(new['abs_ms'], new['detected_ms'])
        [carrier] = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND body LIKE ? ORDER BY rowid DESC LIMIT 1", '%' + key + '%')
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', carrier['idem_key'].removesuffix(':create') + ':wake')
        self.assertEqual((carrier['state'], wake['state']), ('done', 'done'))
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[carrier['result_ref']]['subs']], ['wake'])
        check = self.w.main('recovery-check', '--card', new['card'], '--run', str(new['run_id']))
        self.assertEqual(check[0], 0, check)
        self.assertNotEqual(self.w.main('recovery-check', '--card', old['card'], '--run', '0')[0], 0)
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)
        print('COMPLETION_LATER_FAILURE ' + json.dumps(dict(old=old, new=new,
            failure_key=key, carrier=carrier['result_ref'], wake_state=wake['state'], admission=check)))


    def test_later_linked_ready_work_has_delivered_current_owner_before_deadline(self):
        before = self.retired()
        child = self.b.worker_create('Independent post-completion verification', parents=[self.cleanup])
        self.assertEqual(self.b.cards[child]['task']['status'], 'ready')
        self.assertIn(self.cleanup, self.b.cards[child]['parents'])
        native = copy.deepcopy(self.b.cards)
        checks = []
        for scan in range(12):
            self.scans(1)
            rows = self.w.q('SELECT * FROM recoveries ORDER BY id')
            if len(rows) == 2:
                old, inc = rows
                key = engine._rcv_key(self.tid, inc['card'], 0, 0)[:-1] + '%'
                wakes = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND idem_key LIKE '%:wake'", key)
                check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
                checks.append(dict(scan=scan + 1, now=self.w.clock(), incident=inc, admission=check,
                                   wakes=[dict(key=w['idem_key'], state=w['state']) for w in wakes]))
        self.assertEqual(len(rows), 2)
        old, inc = rows
        self.assertEqual(old['state'], 'closed')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(old[field], before[field], field)
        self.assertEqual(inc['card'], progress.outer_anchor(self.tid) + '-after-' + str(before['id']))
        self.assertIn(inc['attempts'], (1, 2, 3))
        self.assertEqual(inc['state'], 'open')
        print('COMPLETION_LINKED_WORK ' + json.dumps(dict(child=child, checks=checks)))
        usable = [c for c in checks if c['now'] < c['incident']['abs_ms'] and c['admission'][0] == 0
                  and any(w['state'] == 'done' for w in c['wakes'])]
        self.assertTrue(usable, 'no delivered CURRENT owner before original absolute deadline')
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)
        delivered = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND kind='kb_create' AND state='done'", key)
        self.assertTrue(delivered)
        self.assertTrue(any([s['delivery_mode'] for s in self.b.cards[r['result_ref']]['subs']] == ['wake']
                            for r in delivered))

    def test_later_linked_work_recovers_same_owner_after_unavailable_observation(self):
        before = self.retired()
        child = self.b.worker_create('Independent post-completion verification', parents=[self.cleanup])
        self.assertEqual(self.b.cards[child]['task']['status'], 'ready')
        original_show = self.b.show
        with mock.patch.object(self.b, '_preworker_snapshot', return_value=None), \
                mock.patch.object(self.b, 'show', side_effect=lambda card:
                    KanbanResult(Outcome.DEFINITE_FAIL) if card == child else original_show(card)):
            self.scans(2)
            old, unknown = self.w.q('SELECT * FROM recoveries ORDER BY id')
            self.assertEqual((old['state'], unknown['state']), ('closed', 'open'))
            self.assertEqual(unknown['attempts'], 1)
            key = engine._rcv_key(self.tid, unknown['card'], 0, 0)[:-1] + '%'
            [carrier] = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND kind='kb_create'", key)
            self.assertIn('Native observations are unavailable', carrier['body'])
            self.assertEqual(self.w.main('recovery-check', '--card', unknown['card'], '--run', '0')[0], 0)
        checks = []
        for _ in range(6):
            self.scans(1)
            old, inc = self.w.q('SELECT * FROM recoveries ORDER BY id')
            check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
            wakes = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND idem_key LIKE '%:wake'", key)
            checks.append(dict(now=self.w.clock(), incident=inc, admission=check,
                               wakes=[dict(key=w['idem_key'], state=w['state']) for w in wakes]))
        for field in ('id', 'card', 'run_id', 'detected_ms', 'abs_ms'):
            self.assertEqual(inc[field], unknown[field], field)
        self.assertGreaterEqual(inc['attempts'], unknown['attempts'])
        self.assertLessEqual(inc['attempts'], 3)
        self.assertNotEqual(inc['reason_sha256'], unknown['reason_sha256'])
        self.assertEqual((old['state'], inc['state']), ('closed', 'open'))
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(old[field], before[field], field)
        history = self.w.q('SELECT value FROM meta WHERE key=?',
                           'diagnostic_history:%d:%s' % (inc['id'], unknown['reason_sha256']))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0]['value'])['attempts'], unknown['attempts'])
        current = [c for c in checks if c['now'] < unknown['abs_ms'] and c['admission'][0] == 0
                   and any(w['state'] == 'done' and not w['key'].endswith(':1:wake') for w in c['wakes'])]
        self.assertTrue(current, 'restored observation must yield a delivered CURRENT retry without a new budget')
        self.assertEqual(self.b.cards[child]['task']['status'], 'ready')
        print('COMPLETION_READ_RECOVERY ' + json.dumps(dict(unknown=unknown, checks=checks)))

    def incomplete_incident(self):
        self.promoted()
        self.scans(2)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['state'], 'open')
        return inc

    def test_incomplete_cleanup_remains_actionable(self):
        inc = self.incomplete_incident()
        self.assertEqual(self.b.cards[self.cleanup]['task']['status'], 'ready')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.scans(1)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')

    def test_unavailable_native_proof_cannot_retire_existing_incident(self):
        inc = self.incomplete_incident()
        self.b.finish(self.cleanup, 'implementor')
        original_show = self.b.show
        with mock.patch.object(self.b, '_preworker_snapshot', return_value=None), \
                mock.patch.object(self.b, 'show', side_effect=lambda card:
                    KanbanResult(Outcome.DEFINITE_FAIL) if card == self.cleanup else original_show(card)):
            self.scans(2)
            self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.scans(2)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'closed')
        self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)

    def test_pending_required_reporting_keeps_incident_until_actual_delivery(self):
        self.incomplete_incident()
        self.b.finish(self.cleanup, 'implementor')
        key = self.queue_notice('pending-completion-report')
        deliver = main.Glue.clickup
        with mock.patch.object(main.Glue, 'clickup', lambda glue, row:
                False if row['idem_key'] in (key, key + '#cu') else deliver(glue, row)):
            self.scans(3)
            self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
            self.assertNotEqual(self.w.q('SELECT state FROM outbox WHERE idem_key=?', key)[0]['state'], 'done')
        self.scans(3)
        [reported] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
        self.assertEqual(reported['state'], 'done')
        self.assertTrue(any(c['id'] == reported['result_ref'] for c in self.w.cu.request(
            'GET', '/v2/task/' + self.tid + '/comment').json['comments']))
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'closed')

    def test_current_diagnostic_question_prevents_completion_retirement(self):
        inc = self.incomplete_incident()
        question = dict(task=self.tid, card=inc['card'], run=inc['run_id'],
            approval=inc['approval_id'], revision=inc['approval_rev'], sha256=inc['plan_sha256'],
            fingerprint=inc['reason_sha256'], failed='A required completion check remains unresolved.',
            attempted='Read the current workflow.', decision='Which existing operator owns the final check?',
            next_action='Identify the existing operator without credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[-1], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=0 disposition=owner\nCLICKUP-RECOVERY-QUESTION v1 %s'
            % (inc['card'], json.dumps(question)))
        def waiting():
            rows = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_wait:%d' % inc['id'])
            return bool(rows and json.loads(rows[0]['value']).get('state') == 'waiting')
        self.w.step(waiting, seconds=1801, limit=8)
        self.b.finish(self.cleanup, 'implementor')
        self.scans(3)
        self.assertTrue(waiting())
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open',
                         'a delivered current question is still an unresolved workflow obligation')

    def test_completion_requires_current_authority_and_complete_observed_lineage(self):
        self.completed()
        store = Store(self.w.state_path, self.w.clock)
        try:
            task = dict(store.conn.execute('SELECT * FROM tasks').fetchone())
            state = progress.context(store.conn, self.tid)
        finally:
            store.close()
        proof = self.b._preworker_snapshot(progress.outer_roots(state), lambda: True, self.w.clock)
        self.assertIsNotNone(proof)
        shows = copy.deepcopy(proof.shows)
        proof.close()
        source = state['requests'][0]
        atts = {source: [dict(name='clickup-plan-r1.md', sha256=task['published_sha256'])]}
        self.assertEqual(progress.outer(task, state, shows, atts, self.w.clock(), task['last_light_ms'])[0], 'complete')
        for guard in ('phase_only', 'missing_source', 'missing_cleanup', 'active_cleanup', 'missing_pid_proof',
                      'stale_authority', 'fresh_input', 'hold', 'unobserved_human_wait', 'missing_report'):
            with self.subTest(guard=guard):
                t, s, native, attachments = copy.deepcopy((task, state, shows, atts))
                if guard == 'phase_only':
                    s['approvals'][-1]['state'] = 'ticketed'
                elif guard == 'missing_source':
                    attachments.clear()
                elif guard == 'missing_cleanup':
                    native.pop(self.cleanup)
                elif guard == 'active_cleanup':
                    native[self.cleanup]['task']['status'] = 'running'
                elif guard == 'missing_pid_proof':
                    native[self.cleanup]['task'].pop('worker_pid')
                elif guard == 'stale_authority':
                    t['scope_gen'] = s['approvals'][-1]['accounts_gen'] + 1
                elif guard == 'fresh_input':
                    t['input_gen'] += 1
                elif guard == 'hold':
                    t['hold_reason'] = 'internal_stage_wait'
                elif guard == 'unobserved_human_wait':
                    s['human_wait'] = None
                else:
                    s['outbox'] = [r for r in s['outbox'] if ':status:promoted:' not in r['idem_key']]
                self.assertNotEqual(progress.outer(t, s, native, attachments, self.w.clock(), t['last_light_ms'])[0],
                                    'complete')


if __name__ == '__main__':
    unittest.main()


class SharedReadBudgetCompletionTest(unittest.TestCase):
    """Real scan -> first eligible wake -> fresh public admission, with fake ports."""
    setUp = CompletedWorkflowTest.setUp
    scans = CompletedWorkflowTest.scans
    promoted = CompletedWorkflowTest.promoted
    nested_cleanup = CompletedWorkflowTest.nested_cleanup
    carriers = CompletedWorkflowTest.carriers
    retired = CompletedWorkflowTest.retired
    incomplete_incident = CompletedWorkflowTest.incomplete_incident

    def test_active_depth4_first_wake_is_delivered_current(self):
        self.promoted()
        descendants = self.nested_cleanup(depth=4, ready_leaf=True)
        native = copy.deepcopy(self.b.cards)
        self.assertEqual(native[descendants[-1]]['task']['status'], 'ready')
        self.scans(1)
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [], 'the original grace must apply')
        self.scans(1)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((inc['state'], inc['attempts']), ('open', 1))
        self.assertLess(self.w.clock(), inc['abs_ms'])
        key = engine._rcv_key(self.tid, inc['card'], 0, 1)
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key + ':wake')
        check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
        print('SHARED_READ_FIRST_WAKE ' + json.dumps(dict(depth=4, incident=inc,
            wake=dict(state=wake['state'], outcome=wake['outcome']), admission=check)))
        self.assertEqual(wake['state'], 'done', 'the first eligible wake must not be cancelled by scan budget use')
        self.assertEqual(check[0], 0, check)
        [carrier] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key + ':create')
        self.assertNotIn('Native observations are unavailable', carrier['body'])
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)

    def _nested_first_owner(self, depth, later=False):
        before = self.retired() if later else None
        if not later:
            self.promoted()
        descendants = self.nested_cleanup(depth=depth, ready_leaf=True)
        native = copy.deepcopy(self.b.cards)
        self.assertEqual(native[descendants[-1]]['task']['status'], 'ready')
        self.scans(1)
        self.assertEqual(len(self.w.q('SELECT * FROM recoveries')), 1 if later else 0)
        self.scans(1)
        incidents = self.w.q('SELECT * FROM recoveries ORDER BY id')
        inc = incidents[-1]
        self.assertEqual((inc['state'], inc['attempts']), ('open', 1))
        self.assertLess(self.w.clock(), inc['abs_ms'])
        key = engine._rcv_key(self.tid, inc['card'], 0, 1)
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key + ':wake')
        check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
        print('SHARED_READ_OWNER ' + json.dumps(dict(depth=depth, later=later, incident=inc,
            wake=dict(state=wake['state'], outcome=wake['outcome']), admission=check)))
        self.assertEqual(wake['state'], 'done', 'the first eligible wake must be delivered')
        self.assertEqual(check[0], 0, check)
        [carrier] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key + ':create')
        self.assertEqual(carrier['state'], 'done')
        self.assertNotIn('Native observations are unavailable', carrier['body'])
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[carrier['result_ref']]['subs']], ['wake'])
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)
        if later:
            self.assertEqual(inc['card'], progress.outer_anchor(self.tid) + '-after-' + str(before['id']))
            for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
                self.assertEqual(incidents[0][field], before[field], field)
        self.b.finish(descendants[-1], 'implementor')
        if not later:
            self.b.finish(self.cleanup, 'implementor')
        self.scans(2)
        after = self.w.q('SELECT * FROM recoveries ORDER BY id')[-1]
        self.assertEqual(after['state'], 'closed', 'actual completion must retire the same incident')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], inc[field], field)
        self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        carriers = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60000
        self.scans(2)
        self.assertEqual(self.carriers(), carriers)

    def test_active_depth5_first_wake_is_delivered_current_then_retires(self):
        self._nested_first_owner(5)

    def test_later_ready_depth4_first_wake_is_delivered_current_then_retires(self):
        self._nested_first_owner(4, later=True)

    def test_later_ready_depth5_first_wake_is_delivered_current_then_retires(self):
        self._nested_first_owner(5, later=True)

    def _finished_nested_retires(self, depth):
        before = self.incomplete_incident()
        self.nested_cleanup(depth=depth)
        self.b.finish(self.cleanup, 'implementor')
        native = copy.deepcopy(self.b.cards)
        self.scans(2)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'closed', 'finished nested work must retire, not change fingerprints')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.assertNotEqual(self.w.main('recovery-check', '--card', after['card'], '--run', '0')[0], 0)
        for card, shown in native.items():
            self.assertEqual(self.b.cards[card], shown)
        carriers = self.carriers()
        self.w.clock.now_ms = before['abs_ms'] + 60000
        self.scans(2)
        self.assertEqual(self.carriers(), carriers)

    def test_finished_depth4_retires_original_incident(self):
        self._finished_nested_retires(4)

    def test_finished_depth5_retires_original_incident(self):
        self._finished_nested_retires(5)

    def test_depth6_exhaustion_keeps_delivered_current_unknown_diagnosis(self):
        before = self.incomplete_incident()
        self.nested_cleanup(depth=6)
        self.b.finish(self.cleanup, 'implementor')
        self.scans(2)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['state'], 'open', 'insufficient shared reads must never prove completion')
        self.assertEqual(inc['id'], before['id'])
        self.assertEqual(inc['abs_ms'], before['abs_ms'])
        self.assertLessEqual(inc['attempts'], 3)
        key = engine._rcv_key(self.tid, inc['card'], 0, 0)[:-1] + '%'
        carriers = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND kind='kb_create' ORDER BY rowid", key)
        self.assertIn('Native observations are unavailable', carriers[-1]['body'])
        check = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
        print('SHARED_READ_UNKNOWN ' + json.dumps(dict(incident=inc, admission=check)))
        self.assertEqual(check[0], 0, check)
        wakes = self.w.q("SELECT * FROM outbox WHERE idem_key LIKE ? AND idem_key LIKE '%:wake'", key)
        self.assertEqual(wakes[-1]['state'], 'done')

    def test_depth4_send_reobserves_completion_after_scan(self):
        self.promoted()
        descendants = self.nested_cleanup(depth=4, ready_leaf=True)
        self.scans(1)
        gate = main.Glue.recovery_gate
        changed = []
        def complete_before_send(glue, row, payload):
            if payload['params'].get('class') == progress.CLASS and not changed:
                self.b.finish(descendants[-1], 'implementor')
                self.b.finish(self.cleanup, 'implementor')
                changed.append(row['idem_key'])
            return gate(glue, row, payload)
        with mock.patch.object(main.Glue, 'recovery_gate', complete_before_send):
            self.scans(1)
        self.assertEqual(len(changed), 1, 'the real wake gate must be reached')
        [inc] = self.w.q('SELECT * FROM recoveries')
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', changed[0])
        self.assertEqual(wake['state'], 'cancelled_stale', 'completed work cannot use the scan-time active proof')
        self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.scans(2)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'closed')
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], inc[field], field)
