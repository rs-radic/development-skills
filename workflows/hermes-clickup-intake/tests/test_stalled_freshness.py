"""Diagnostic freshness under real scan ordering; fake ports advance time, never sleep or call live services."""
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import Outcome
from test_recovery import store_conn
import test_stalled_fallback as fallback
import test_stalled_remediation as remediation
import test_s4_composition as comp


class DiagnosticFreshnessTest(unittest.TestCase):
    setUp = fallback.StalledFallbackTest.setUp
    carriers = fallback.StalledFallbackTest.carriers
    notice_rows = fallback.StalledFallbackTest.notice_rows
    scan_minutes = fallback.StalledFallbackTest.scan_minutes
    unknown_stop = fallback.StalledFallbackTest.unknown_stop
    queue_notice = fallback.StalledFallbackTest.queue_notice

    def slow_scan(self, seconds=962, during=None):
        """The first native read follows provider observation, as in the actual scheduled scan."""
        show, advanced = self.b.show, []
        def slow(cid):
            result = show(cid)
            if not advanced:
                advanced.append(cid)
                self.w.clock.advance(seconds)
                if during:
                    during()
            return result
        self.w.clock.advance(301)  # the configured 300s light cadence is due on this ordinary subsequent pass
        start = self.w.clock()
        provider_start, native_start = len(self.w.cu.calls), len(self.b.calls)
        with mock.patch.object(self.b, 'show', side_effect=slow):
            self.assertEqual(self.w.run()[0], 0)
        assert advanced
        self.evidence = getattr(self, 'evidence', []) + [{
            'scan_start': start, 'end': self.w.clock(),
            'last_light': self.w.task(self.tid)['last_light_ms'],
            'provider_calls': self.w.cu.calls[provider_start:], 'native_calls': self.b.calls[native_start:],
            'recoveries': self.w.q('SELECT * FROM recoveries'),
            'effects': self.w.q("SELECT idem_key,kind,state,attempts,outcome FROM outbox WHERE idem_key LIKE '%:rcv:%' OR idem_key LIKE '%:recovery-notice:%'"),
            'carriers': [(c, self.b.cards[c]['task']['status']) for c in self.carriers()]}]

    def test_slow_global_scan_eventually_delivers_one_diagnostic_wake(self):
        card = self.life.fe
        run = self.b.claim(card, 'tester')
        self.b.block(card, 'new_technical_stop', 'A previously unseen technical stop.')
        before = list(self.b.mutations())
        for _ in range(3):
            self.w.clock.advance(61)
            self.slow_scan()
            if self.carriers() and self.b.cards[self.carriers()[0]]['task']['status'] == 'done':
                break
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((inc['card'], inc['run_id'], inc['class']), (card, run, 'diagnostic'))
        self.assertEqual(len(self.carriers()), 1)
        carrier = self.carriers()[0]
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'done',
                         'unchanged approved work must actually wake, not accumulate a permanently deferred carrier')
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[carrier]['subs']], ['wake'])
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual([m for m in self.b.mutations()[len(before):] if m[1] == card], [])
        # Public admission consumes only the latest genuinely committed observation and native reads.
        mutations = list(self.b.mutations())
        with mock.patch.object(main, 'Store', side_effect=AssertionError('public admission must not initialize Store')):
            rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertEqual(rc, 0, lines)
        self.assertEqual(self.b.mutations(), mutations)

    def test_slow_global_scan_delivers_and_verifies_queued_origin_notice(self):
        card, run, key = self.queue_notice()
        path = '/v2/task/%s/comment' % self.tid
        before = self.w.cu.count('POST', path)
        for _ in range(3):
            self.w.clock.advance(61)
            self.slow_scan()
        [notice] = self.notice_rows()
        self.assertEqual(notice['idem_key'], key)
        self.assertEqual(notice['state'], 'done', 'a queued diagnostic escalation must not starve on every normal long scan')
        self.assertEqual(self.w.cu.count('POST', path) - before, 1)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'escalated')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)

    def assert_unsent(self, key, posts):
        [notice] = self.notice_rows()
        self.assertEqual(notice['idem_key'], key)
        self.assertEqual(notice['attempts'], 0)
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid), posts)
        return notice

    def test_new_addressed_input_during_slow_scan_cancels_old_notice_and_classifies_normally(self):
        card, run, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        before = self.w.task(self.tid)['input_gen']
        self.slow_scan(during=lambda: self.life.say(comp.APPROVER_A, 'Please clarify the status.', mention=comp.BOT))
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'cancelled_stale')
        task = self.w.task(self.tid)
        self.assertGreater(task['input_gen'], before)
        self.assertLess(task['classified_gen'], task['input_gen'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.out('answer', ref, 'Still safely stopped.'), comp.classify(ref, gen, 'answer_only'))
        self.w.step(lambda: self.w.task(self.tid)['classified_gen'] == self.w.task(self.tid)['input_gen'],
                    world=self.life.planner(script))
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_unaddressed_activity_preserves_generation_but_defers_until_real_quiet_barrier(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        gen = self.w.task(self.tid)['input_gen']
        self.slow_scan(during=lambda: self.life.say(comp.APPROVER_A, 'An unaddressed status note.'))
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(self.w.task(self.tid)['input_gen'], gen)
        self.assertGreater(self.w.task(self.tid)['quiet_until_ms'], self.w.clock())
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'done')
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid) - posts, 1)

    def test_changed_description_during_slow_scan_is_new_input_not_fresh_approval(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        gen = self.w.task(self.tid)['input_gen']
        self.slow_scan(during=lambda: self.w.cu.set_description(self.tid, 'A new material requirement.'))
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'cancelled_stale')
        self.assertGreater(self.w.task(self.tid)['input_gen'], gen)

    def test_failed_target_refresh_defers_without_timestamp_renewal_or_send_then_recovers(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        start = self.w.clock()
        def fail():
            self.w.cu.inject('GET', '/v2/task/%s$' % self.tid, Outcome.DEFINITE_FAIL, status=503)
        self.slow_scan(during=fail)
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], self.evidence[-1]['scan_start'])
        self.assertEqual(len(self.w.cu.faults), 0, 'the target provider refresh must actually have been attempted')
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'done')
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid) - posts, 1)

    def test_shared_scan_budget_exhaustion_cannot_buy_a_refresh_or_send(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        light = self.w.task(self.tid)['last_light_ms']
        count = self.w.cu.count()
        self.w.cu.rate_remaining = 0  # actual preflight consumes the remaining reserve and stops this guard
        self.slow_scan()
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], light)
        self.assertEqual(self.w.cu.count() - count, 1, 'renewal must not allocate an independent request budget')
        self.w.cu.rate_remaining = None
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'done')

    def test_target_provider_IO_over_five_minutes_still_expires_without_refresh_loop(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        request, late = self.w.transport.request, []
        start = self.w.clock()
        def slow(method, path, **kw):
            result = request(method, path, **kw)
            if method == 'GET' and path == '/v2/task/%s' % self.tid and self.w.clock() - start >= 962_000:
                late.append(self.w.clock())
                self.w.clock.advance(601)
            return result
        with mock.patch.object(self.w.transport, 'request', side_effect=slow):
            self.slow_scan()
        self.assertEqual(len(late), 1, 'only one real bounded refresh, no renew-until-current loop')
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], late[0], 'timestamp must precede its actual provider I/O')
        self.assertGreater(self.w.clock() - self.w.task(self.tid)['last_light_ms'], 300_000)

    def test_diagnostic_native_IO_after_refresh_still_expires_fail_closed(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        reads = main._diagnostic_reads
        def slow(*a):
            reads(*a)
            self.w.clock.advance(601)
        with mock.patch.object(main, '_diagnostic_reads', side_effect=slow):
            self.slow_scan()
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertGreater(self.w.clock() - self.w.task(self.tid)['last_light_ms'], 300_000)

    def test_new_parent_during_target_provider_refresh_is_required_before_notice_send(self):
        card, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        request = self.w.transport.request
        start = self.w.clock()
        def change(method, path, **kw):
            result = request(method, path, **kw)
            if method == 'GET' and path == '/v2/task/%s' % self.tid and self.w.clock() - start >= 962_000:
                self.b.cards[card]['parents'].append('t_deadbeef')
            return result
        with mock.patch.object(self.w.transport, 'request', side_effect=change):
            self.slow_scan()
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertIn(('show', 't_deadbeef'), self.b.calls)

    def test_current_hold_during_final_native_IO_cannot_escape_refreshed_notice(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        reads = main._diagnostic_reads
        def hold(*a):
            reads(*a)
            conn = store_conn(self.w)
            conn.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
            conn.close()
        with mock.patch.object(main, '_diagnostic_reads', side_effect=hold):
            self.slow_scan()
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'operator')

    def test_genuine_unanswered_native_question_stays_quiet_across_long_scans(self):
        fallback.StalledFallbackTest.incorporated_question(self, marker=False)
        for _ in range(3):
            self.w.clock.advance(61)
            self.slow_scan()
        self.assertEqual(self.carriers(), [])
        self.assertEqual(self.notice_rows(), [])

    def test_public_admission_never_renews_expired_committed_provider_proof(self):
        card, run = self.unknown_stop()
        self.w.clock.advance(301)
        before = self.w.task(self.tid)
        calls, mutations = self.w.cu.count(), list(self.b.mutations())
        with mock.patch.object(main, 'Store', side_effect=AssertionError('public command cannot initialize Store')):
            rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertEqual((rc, lines), (2, ['UNAVAILABLE diagnostic_input_barrier']))
        self.assertEqual(self.w.task(self.tid), before)
        self.assertEqual(self.w.cu.count(), calls)
        self.assertEqual(self.b.mutations(), mutations)

    def test_new_input_during_long_scan_cannot_release_a_queued_diagnostic_wake(self):
        with mock.patch.object(main.Glue, 'recovery_gate', return_value='defer'):
            card, run = self.unknown_stop()
        [carrier] = self.carriers()
        key = 'clickup-dp:%s:rcv:%s:%d:1:wake' % (self.tid, card, run)
        gen = self.w.task(self.tid)['input_gen']
        self.slow_scan(during=lambda: self.life.say(comp.APPROVER_A, 'New requirements need classification.', mention=comp.BOT))
        [row] = self.w.q('SELECT state,attempts FROM outbox WHERE idem_key=?', key)
        self.assertEqual((row['state'], row['attempts']), ('cancelled_stale', 0))
        self.assertGreater(self.w.task(self.tid)['input_gen'], gen)
        self.assertNotEqual(self.b.cards[carrier]['task']['status'], 'done')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_target_refresh_requires_current_home_list_proof_not_scan_start_confirmation(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        def unknown_list():
            task = dict(self.w.cu.tasks[self.tid])
            task.pop('list')
            self.w.cu.inject('GET', '/v2/task/%s$' % self.tid, Outcome.OK, status=200, json=task)
        self.slow_scan(during=unknown_list)
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        self.assertEqual(len(self.w.cu.faults), 0, 'new target response must actually be observed')

    def test_incomplete_comment_refresh_cannot_reuse_a_successful_task_get(self):
        _, _, key = self.queue_notice()
        posts = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        def fail():
            self.w.cu.inject('GET', '/v2/task/%s/comment$' % self.tid, Outcome.DEFINITE_FAIL, status=503)
        self.slow_scan(during=fail)
        self.assertEqual(self.assert_unsent(key, posts)['state'], 'pending')
        task = self.w.task(self.tid)
        self.assertEqual(len(self.w.cu.faults), 0)
        self.assertGreater(task['last_task_get_ms'], task['last_light_ms'])
        self.assertGreater(self.w.clock() - task['last_light_ms'], 300_000)

    def test_slow_own_hold_informational_notice_refreshes_without_clearing_execution_hold(self):
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        card, run = remediation.RemediationTest.exhausted_carrier(self)
        [notice] = self.notice_rows()
        self.assertEqual(notice['attempts'], 1)
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold)
        self.w.clock.advance(600)
        self.slow_scan()
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'done')
        self.assertEqual(self.notice_rows()[0]['attempts'], 2)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], hold)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)

    def test_queued_wake_needs_exact_current_plan_evidence_even_with_fresh_provider_lease(self):
        with mock.patch.object(main.Glue, 'recovery_gate', return_value='defer'):
            card, _ = self.unknown_stop()
        [carrier] = self.carriers()
        for run in self.b.cards[self.life.req_cards()[-1]]['runs']:
            if 'plan_revision' in (run.get('metadata') or {}):
                run['metadata']['plan_revision'] = 99
        self.scan_minutes(2)
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'blocked', 'wrong plan bytes/review revision cannot admit a wake')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
