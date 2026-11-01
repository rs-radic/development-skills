"""SPEC-R3-1: a failed successor does not hide the same-work linked owner."""
import copy
import json
import unittest
from unittest import mock

import test_general_stall_continuation as continuation
import test_general_stall_wake as original
from clickup_intake import main, progress


class LinkedOwnerTest(unittest.TestCase):
    setUp = continuation.GeneralStallContinuationTest.setUp
    scans = continuation.GeneralStallContinuationTest.scans
    invalid_plan = continuation.GeneralStallContinuationTest.invalid_plan
    carriers = continuation.GeneralStallContinuationTest.carriers
    notices = continuation.GeneralStallContinuationTest.notices
    check = continuation.GeneralStallContinuationTest.check
    failed_successor = continuation.GeneralStallContinuationTest.failed_successor
    assert_retained = continuation.GeneralStallContinuationTest.assert_retained
    assert_delivered = continuation.GeneralStallContinuationTest.assert_delivered

    def prepare(self, logical=False):
        if logical:
            original.GeneralStallWakeTest.test_missing_request_owner_uses_logical_anchor_without_native_lookup(self)
        self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        row = self.failed_successor(inc)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.check(inc), 0)
        self.saved_source = copy.deepcopy(self.b.cards[self.life.req_cards()[-1]])
        self.saved_history = self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%' ORDER BY key")
        return inc, row

    def reviewer(self):
        source = self.life.req_cards()[-1]
        child = self.b.worker_create('Review same planning handoff', parents=(source,), assignee='plan-reviewer')
        self.b.claim(child, 'plan-reviewer')
        self.assertEqual(self.b.cards[source]['task']['status'], 'done')
        self.assertIn(source, self.b.cards[child]['parents'])
        self.assertIsNotNone(self.b.cards[child]['runs'][-1]['worker_pid'])
        self.assertIsNone(self.b.cards[child]['runs'][-1]['ended_at'])
        return child

    def retained(self, inc, row):
        self.assert_retained(inc)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', row['idem_key']), [row])
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + row['idem_key'])
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%' ORDER BY key"),
                         self.saved_history)
        self.assertEqual(self.b.cards[self.life.req_cards()[-1]], self.saved_source)

    def test_failed_successor_live_child_refuses_public_admission_and_deadline_notice(self):
        inc, row = self.prepare()
        child = self.reviewer()
        self.assertEqual(self.check(inc), 3)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.b.cards[child]['task']['status'], 'running')
        self.retained(inc, row)

    def test_failed_successor_live_child_does_not_spend_next_wake(self):
        inc, row = self.prepare()
        self.reviewer()
        before = copy.deepcopy([self.b.cards[c] for c in self.carriers()])
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(25)
        self.assertEqual([self.b.cards[c] for c in self.carriers()], before)
        self.assertEqual(self.notices(), [])
        self.retained(inc, row)

    def test_pending_notice_rechecks_child_before_spending_send(self):
        inc, row = self.prepare()
        self.w.clock.now_ms = inc['abs_ms'] + 1
        clickup = main.Glue.clickup
        def suspend(glue, item):
            return False if json.loads(item['body']).get('recovery_notice') else clickup(glue, item)
        with mock.patch.object(main.Glue, 'clickup', suspend):
            self.scans(5)
        [pending] = self.notices()
        self.assertEqual((pending['state'], pending['attempts']), ('pending', 0))
        self.reviewer()
        comments = self.w.bot_comments(self.tid)
        self.scans(8)
        [after] = self.notices()
        self.assertNotEqual(after['state'], 'done')
        self.assertEqual((after['attempts'], after['first_send_ms']), (0, None))
        self.assertEqual(self.w.bot_comments(self.tid), comments)
        self.retained(inc, row)

    def test_failed_successor_without_child_keeps_original_deadline_delivery(self):
        inc, row = self.prepare()
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        self.retained(inc, row)

    def test_failed_successor_finished_child_keeps_original_deadline_delivery(self):
        inc, row = self.prepare()
        child = self.reviewer()
        self.b.finish(child, 'plan-reviewer', {})
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        self.retained(inc, row)

    def test_failed_successor_unknown_child_closure_defers_projection(self):
        inc, row = self.prepare()
        child = self.reviewer()
        snap = main.load_recovery_snapshot(self.w.state_path, self.tid, inc['card'], inc['run_id'])
        reader = main._PreworkerReads(self.b, self.w.clock)
        try:
            shows, atts, complete = main._progress_collect(snap, reader)
            self.assertTrue(complete)
            self.assertIn(child, shows[self.life.req_cards()[-1]]['children'])
            self.assertIn(child, shows)
            # Complete collection above proves these are missing observations, not no-child fixtures.
            for missing in ('child', 'child_runs', 'source_children'):
                with self.subTest(missing=missing):
                    observed = copy.deepcopy(shows)
                    if missing == 'child':
                        observed.pop(child)
                    elif missing == 'child_runs':
                        observed[child].pop('runs')
                    else:
                        observed[self.life.req_cards()[-1]].pop('children')
                    self.assertEqual(progress.projection(snap['task'], snap, observed, atts,
                        self.w.clock(), snap['task']['last_light_ms'], admitted=True), ('unknown', None))
        finally:
            if reader.proof:
                reader.proof.close()
        self.retained(inc, row)

    def test_logical_incident_failed_successor_live_child_keeps_original_anchor_quiet(self):
        inc, row = self.prepare(logical=True)
        self.assertEqual(inc['card'], 'progress-' + self.tid + '-request')
        self.assertEqual(inc['run_id'], 0)
        self.reviewer()
        self.assertEqual(self.check(inc), 3)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assertEqual(self.notices(), [])
        self.retained(inc, row)
