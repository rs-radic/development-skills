"""Offline real-Glue caller regression for a fence arriving after poll start.

Native/provider adapters are fixtures; observation, barriers and send/readback are
production code. No fabricated human answer or ACK is added to the substrate.
"""
import json
from pathlib import Path
from unittest import mock

from clickup_intake import engine, main, observe, rules
from clickup_intake.contracts import KanbanResult, Outcome, TransportResult
import test_same_pass_fence_delivery as samepass
import test_three_native_waits as native
import test_s4_composition as comp


class PostAnchorFenceObservationTest(native.NativeWaitCase):
    new_worker = samepass.SamePassSentinelTest.new_worker
    new_sentinel = samepass.SamePassSentinelTest.new_sentinel

    def setUp(self):
        homes = Path(__file__).resolve().parents[2] / 'staging/remediation-12-post-anchor-refresh/testhomes'
        homes.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(comp, 'TMP_ROOT', str(homes))
        patcher.start()
        self.addCleanup(patcher.stop)
        native.CpuNativeParkTest.setUp(self)
        self.run_id = self.new_worker()
        self.events = []
        self.fresh = 't_f00da120'

    def run_arriving_fence(self, mode=None):
        """Create F during the first real provider pass, not before Glue's clock."""
        self.w.clock.advance(901)
        self.poll_start = self.w.clock()
        request = self.w.transport.request
        full, light = observe.full_pass, observe.light_pass
        complete, show, observe_cards = self.b.complete, self.b.show, main.Glue.observe_cards
        evaluate = engine.evaluate
        created = False
        self.active = None
        self.operation_guards = []

        def provider(method, path, **kwargs):
            nonlocal created
            if not created and path.endswith('/task') and '/list/' in path:
                created = True
                if mode == 'same-second':
                    self.b.cards[self.fresh] = dict(samepass.sentinel_show(
                        card=self.fresh, title='CLICKUP-FENCE v1 ' + self.source,
                        w=self.source, created_s=self.w.clock() // 1000), atts=[], subs=[])
                    self.b.cards[self.source]['parents'].append(self.fresh)
                else:
                    self.w.clock.advance(6)
                    self.new_sentinel(self.fresh, 'FENCE')
                self.anchor = (self.b.cards[self.fresh]['task']['created_at'] + 1) * 1000
                self.events.append(('created', self.w.clock()))
                if mode == 'two-fences':
                    self.new_sentinel('t_f00da121', 'FENCE')
                if mode != 'before-anchor':
                    self.w.clock.advance(2)
            self.w.clock.advance(ms=10)
            if self.active and path == '/v2/task/%s/comment' % self.tid:
                if mode == self.active + '-failure':
                    return TransportResult(Outcome.DEFINITE_FAIL, 503)
                if mode == self.active + '-activity':
                    self.w.cu.add_comment(self.tid, comp.APPROVER_A, 'New scope input during observation')
                    self.active = None
            return request(method, path, **kwargs)

        def operation(kind, fn, *args, **kwargs):
            tid, stamp = args[3:5]
            self.operation_guards.append(args[0].guard)
            self.events.append((kind + '-start', self.w.clock(), stamp, tid))
            self.active = kind if stamp >= getattr(self, 'anchor', float('inf')) else None
            result = fn(*args, **kwargs)
            self.active = None
            self.w.clock.advance(ms=10)
            self.events.append((kind + '-end', self.w.clock(), result.complete, tid))
            return result

        def observed(glue):
            observe_cards(glue)
            self.glue = glue
            self.before = dict(glue.c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
            ctx = engine._Ctx(glue.store, self.w.clock(), glue.obs, glue.cfg)
            b = dict(glue.c.execute('SELECT * FROM blockers WHERE question_card=?', (self.question,)).fetchone())
            self.initial_settled = engine._settled(ctx, self.before, b)
            self.native_seen = all(glue.obs.cards.get(c) is not None for c in (self.source, self.fresh, self.question))
            if mode == 'old-cursor':
                cursor = dict(started_ms=self.poll_start, start=None, start_id=None, pages=0,
                              seen=[], roots=[], paged=False)
                glue.store.observe_commit(self.tid, [], {'full_cursor': json.dumps(cursor)})
            if mode == 'hold':
                glue.store.observe_commit(self.tid, [], {'hold_reason': 'operator'})
            if mode == 'terminal':
                glue.store.observe_commit(self.tid, [], {'terminal': 1})
            if mode == 'per-task-budget':
                glue.guard.task_cap = glue.guard.per_task.get(self.tid, 0) + 2
            if mode == 'scan-budget':
                glue.guard.scan_cap = glue.guard.used + 2

        def evaluated(store, now, obs=None, config=None):
            result = evaluate(store, now, obs, config)
            if obs and obs.cards:
                if mode == 'unclassified':
                    store.conn.execute('UPDATE tasks SET classified_gen=input_gen-1 WHERE clickup_task_id=?',
                                       (self.tid,))
                if mode == 'unsettled':
                    store.conn.execute("UPDATE relays SET state='staged' WHERE task=?", (self.tid,))
                    store.conn.execute('UPDATE approvals SET accounts_gen=0 WHERE task=?', (self.tid,))
                    ctx = engine._Ctx(store, now, obs, config)
                    b = dict(store.conn.execute('SELECT * FROM blockers WHERE question_card=?',
                                                (self.question,)).fetchone())
                    self.post_settled = engine._settled(ctx, ctx.task(self.tid), b)
            return result

        def read(card):
            if mode == 'missing-W' and card == self.source or mode == 'missing-F' and card == self.fresh \
                    or mode == 'missing-Q' and card == self.question:
                return KanbanResult(Outcome.DEFINITE_FAIL)
            result = show(card)
            if mode == 'foreign-children' and card == self.fresh and result.json:
                result.json['children'].append('t_deadbeef')
            if mode == 'wrong-creator' and card == self.fresh and result.json:
                for event in result.json.get('events', []):
                    if event.get('kind') == 'created':
                        event['payload']['creator_task_id'] = 't_deadbeef'
            if mode == 'owner-released' and card == self.fresh and result.json:
                result.json['task']['status'] = 'done'
            return result

        def delivered(card, *args, **kwargs):
            if card == self.fresh:
                self.events.append(('send', self.w.clock()))
            return complete(card, *args, **kwargs)

        with mock.patch.object(self.w.transport, 'request', side_effect=provider), \
             mock.patch.object(observe, 'full_pass', side_effect=lambda *a, **k: operation('F', full, *a, **k)), \
             mock.patch.object(observe, 'light_pass', side_effect=lambda *a, **k: operation('L', light, *a, **k)), \
             mock.patch.object(main.Glue, 'observe_cards', new=observed), \
             mock.patch.object(engine, 'evaluate', side_effect=evaluated), \
             mock.patch.object(self.b, 'show', side_effect=read), \
             mock.patch.object(self.b, 'complete', side_effect=delivered):
            self.assertEqual(self.w.run()[0], 0, self.w.outputs[-5:])
        self.assertTrue(created)
        if mode == 'same-second':
            self.assertLessEqual(self.b.cards[self.fresh]['task']['created_at'] * 1000, self.poll_start)
        else:
            self.assertGreater(self.b.cards[self.fresh]['task']['created_at'] * 1000, self.poll_start)
        self.assertLess(self.poll_start, self.anchor)
        self.assertTrue(all(g is self.glue.guard for g in self.operation_guards))

    def test_arriving_fence_gets_real_post_anchor_full_then_light_before_send(self):
        self.run_arriving_fence()
        task = self.w.task(self.tid)
        self.assertTrue(self.initial_settled)
        self.assertTrue(self.native_seen)
        self.assertLess(self.before['full_started_ms'], self.anchor)
        self.assertFalse(rules.barrier_ok(self.before, self.anchor, self.w.clock(), self.poll_start))
        self.assertEqual(self.glue.obs.scan_started_ms, self.poll_start)
        self.assertEqual(task['classified_gen'], task['input_gen'])
        self.assertEqual(self.blocker(self.question)['state'], 'delivered')
        self.assertEqual(self.blocker(self.question)['delivered_gen'], 5)
        self.assertEqual(self.b.cards[self.question]['task']['status'], 'done')
        self.assertEqual(self.b.cards[self.source]['task']['status'], 'running')
        self.assertGreater(self.w.clock(), self.anchor)
        self.assertIsNone(task['hold_reason'])
        self.assertEqual(task['terminal'], 0)
        self.assertEqual(self.b.cards[self.fresh]['task']['status'], 'done',
                         'POST_ANCHOR_GAP: eligible answered fence must release in this actual Glue.scan')
        real_full = [e for e in self.events if e[0] == 'F-start' and e[2] >= self.anchor]
        self.assertEqual(len(real_full), 1, self.events)
        f = self.events.index(real_full[0])
        self.assertEqual(self.events[f+1][0], 'F-end')
        self.assertTrue(self.events[f+1][2])
        self.assertEqual(self.events[f+2][0], 'L-start')
        self.assertGreaterEqual(self.events[f+2][2], self.events[f+1][1])
        self.assertTrue(any(e[0] == 'send' for e in self.events[f+3:]), self.events)
        self.assertEqual(task['full_started_ms'], real_full[0][2])
        self.assertGreaterEqual(task['last_light_ms'], task['last_full_ms'])
        self.assertTrue(rules.barrier_ok(task, self.anchor, self.w.clock(), self.poll_start))
        self.assertEqual(self.b.mutations().count(('complete', self.fresh)), 1)
        # Completion/readback is not a worker-authored consumption ACK.
        self.cycles(1)
        self.assertEqual(self.blocker(self.question)['state'], 'delivered')
        self.b.worker_comment(self.source, 'implementor',
            'CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=5 run=%d fence=%s' %
            (self.question, self.source, self.run_id, self.fresh))
        self.cycles(2)
        self.assertEqual(self.blocker(self.question)['state'], 'resumed')
        self.assertEqual(self.b.mutations().count(('complete', self.fresh)), 1)

    def assert_deferred(self):
        self.assertNotIn(('complete', self.fresh), self.b.mutations())
        self.assertNotEqual(self.blocker(self.question)['state'], 'resumed')

    def refresh_starts(self, kind):
        return [e for e in self.events if e[0] == kind + '-start' and e[2] >= self.anchor]

    def test_same_second_creation_requires_and_gets_post_anchor_full(self):
        self.run_arriving_fence('same-second')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(len(self.refresh_starts('L')), 1)
        self.assertEqual(self.b.mutations().count(('complete', self.fresh)), 1)
        self.assertEqual(self.glue.obs.scan_started_ms, self.poll_start)

    def test_before_integer_second_anchor_does_not_refresh_or_release(self):
        self.run_arriving_fence('before-anchor')
        self.assertLess(self.w.clock(), self.anchor)
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_resumed_pre_anchor_cursor_keeps_original_start_and_cannot_confirm(self):
        self.run_arriving_fence('old-cursor')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(self.refresh_starts('L'), [])
        self.assertEqual(self.w.task(self.tid)['full_started_ms'], self.poll_start)
        self.assertIsNone(self.w.task(self.tid)['full_cursor'])
        self.assert_deferred()

    def test_incomplete_real_full_keeps_cursor_and_skips_confirming_light(self):
        self.run_arriving_fence('F-failure')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(self.refresh_starts('L'), [])
        self.assertIsNotNone(self.w.task(self.tid)['full_cursor'])
        self.assertLess(self.w.task(self.tid)['full_started_ms'], self.anchor)
        self.assert_deferred()

    def test_failed_real_light_cannot_reuse_earlier_light_before_full(self):
        self.run_arriving_fence('L-failure')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(len(self.refresh_starts('L')), 1)
        task = self.w.task(self.tid)
        self.assertGreaterEqual(task['full_started_ms'], self.anchor)
        self.assertLess(task['last_light_ms'], task['last_full_ms'])
        self.assert_deferred()

    def test_activity_during_real_full_restarts_quiet_without_light_or_release(self):
        self.run_arriving_fence('F-activity')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(self.refresh_starts('L'), [])
        self.assertGreater(self.w.task(self.tid)['quiet_until_ms'], self.w.clock())
        self.assert_deferred()

    def test_activity_during_confirming_light_invalidates_release(self):
        self.run_arriving_fence('L-activity')
        self.assertEqual(len(self.refresh_starts('L')), 1)
        self.assertGreater(self.w.task(self.tid)['quiet_until_ms'], self.w.clock())
        self.assert_deferred()

    def test_target_budget_is_shared_with_prior_poll_reads(self):
        self.run_arriving_fence('per-task-budget')
        self.assertEqual(self.glue.guard.per_task[self.tid], self.glue.guard.task_cap)
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assert_deferred()

    def test_scan_budget_is_shared_with_prior_poll_reads(self):
        self.run_arriving_fence('scan-budget')
        self.assertEqual(self.glue.guard.used, self.glue.guard.scan_cap)
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assert_deferred()

    def test_two_fences_for_one_target_do_not_multiply_refresh_budget(self):
        self.run_arriving_fence('two-fences')
        self.assertEqual(len(self.refresh_starts('F')), 1)
        self.assertEqual(len(self.refresh_starts('L')), 1)
        for card in (self.fresh, 't_f00da121'):
            self.assertEqual(self.b.mutations().count(('complete', card)), 1)

    def test_missing_worker_observation_does_not_refresh_or_release(self):
        self.run_arriving_fence('missing-W')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_missing_fence_observation_does_not_refresh_or_release(self):
        self.run_arriving_fence('missing-F')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_missing_question_observation_does_not_refresh_or_release(self):
        self.run_arriving_fence('missing-Q')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_unclassified_input_does_not_refresh_or_release(self):
        self.run_arriving_fence('unclassified')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_unsettled_answer_does_not_refresh_or_release(self):
        self.run_arriving_fence('unsettled')
        self.assertTrue(self.initial_settled)
        self.assertFalse(self.post_settled)
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_held_task_does_not_refresh_or_release(self):
        self.run_arriving_fence('hold')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_terminal_task_does_not_refresh_or_release(self):
        self.run_arriving_fence('terminal')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_foreign_fence_children_refuse_refresh_and_release(self):
        self.run_arriving_fence('foreign-children')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_wrong_native_creator_refuses_refresh_and_release(self):
        self.run_arriving_fence('wrong-creator')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()

    def test_owner_completed_fence_does_not_trigger_observation_or_complete_again(self):
        self.run_arriving_fence('owner-released')
        self.assertEqual(self.refresh_starts('F'), [])
        self.assert_deferred()
