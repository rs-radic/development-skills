"""Two continuation regressions: retained obligations, never expanded recovery authority."""
import copy
import json
import unittest
from unittest import mock

import test_general_stall_wake as original
import test_general_stall_corrections as corrections
import test_recovery as recovery
import test_s4_composition as composition
from clickup_intake import main
from clickup_intake.contracts import KanbanResult, Outcome


class GeneralStallContinuationTest(unittest.TestCase):
    setUp = original.GeneralStallWakeTest.setUp
    scans = original.GeneralStallWakeTest.scans
    carriers = original.GeneralStallWakeTest.carriers
    invalid_plan = original.GeneralStallWakeTest.invalid_plan
    notices = corrections.GeneralStallCorrectionsTest.notices

    def approved_incident(self):
        original.GeneralStallWakeTest.test_completed_approved_producer_missing_handoff_uses_same_diagnostic(self)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['attempts'], 1)
        self.assertIsNotNone(inc['approval_id'])
        self.assertIsNone(self.b.cards[inc['card']]['runs'][-1]['worker_pid'])
        self.assertIsNone(self.b.cards[inc['card']]['task'].get('worker_pid'))
        return inc

    def check(self, inc):
        return self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id']))[0]

    def assert_retained(self, before):
        [after] = self.w.q('SELECT * FROM recoveries')
        for key in ('id', 'task', 'card', 'run_id', 'approval_id', 'approval_rev',
                    'plan_sha256', 'detected_ms', 'abs_ms', 'attempts'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['state'], 'open')
        return after

    def assert_delivered(self):
        rows = self.notices()
        self.assertEqual(len(rows), 1, 'original obligation must deliver one current explanation')
        [notice] = rows
        self.assertEqual((notice['target'], notice['state']), (self.tid, 'done'))
        text = json.loads(notice['body'])['text']
        self.assertTrue(any(text in c['comment_text'] for c in self.w.bot_comments(self.tid)))

    def test_approved_admitted_unowned_queue_or_review_keeps_deadline_and_admission(self):
        inc = self.approved_incident()
        card = inc['card']
        runs = copy.deepcopy(self.b.cards[card]['runs'])
        for status in ('ready', 'todo', 'scheduled', 'review'):
            with self.subTest(status=status):
                self.b.cards[card]['task']['status'] = status
                self.assertEqual(self.check(inc), 0, 'admitted unowned producer remains diagnosable')
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        self.assert_retained(inc)
        self.assertEqual(self.b.cards[card]['runs'], runs)
        self.assertEqual(self.b.cards[card]['task']['status'], 'review')
        self.assertEqual(len(self.carriers()), 1)

    def failed_successor(self, inc):
        self.life.say(composition.REQ, 'Please clarify the planning handoff.', mention=composition.BOT)
        create = self.b.create
        def fail(*args, **kwargs):
            if ':req:' in kwargs.get('idempotency_key', ''):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(*args, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=fail):
            self.scans(100)
        [row] = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND state='failed' AND idem_key LIKE '%:req:%'")
        self.assertEqual((row['attempts'], row['outcome'], row['result_ref']), (6, 'retry_wait', None))
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + row['idem_key'])
        self.assertEqual(json.loads(row['body'])['params']['previous_card'], self.life.req_cards()[-1])
        return row

    def test_native_publication_incident_follows_definite_failed_successor_without_new_identity(self):
        self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        source = copy.deepcopy(self.b.cards[inc['card']])
        row = self.failed_successor(inc)
        [continued] = self.w.q('SELECT * FROM recoveries')
        self.assertGreaterEqual(continued['attempts'], inc['attempts'])
        self.assertLessEqual(continued['attempts'], 3)
        for key in ('id', 'card', 'run_id', 'detected_ms', 'abs_ms'):
            self.assertEqual(continued[key], inc[key])
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        after = self.assert_retained(continued)
        self.assertNotEqual(after['reason_sha256'], inc['reason_sha256'])
        self.assertEqual(self.check(after), 0)
        self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id'] + 1))[0], 0)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', row['idem_key']), [row])
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + row['idem_key'])
        self.assertEqual(self.b.cards[inc['card']], source)
        self.assertEqual(len(self.carriers()), continued['attempts'])
        self.assertTrue(self.w.q("SELECT 1 FROM meta WHERE key LIKE 'diagnostic_history:%'"))
        self.assertFalse(any(card == 'progress-' + self.tid + '-request' for _, card in self.b.calls))

    def approved_deadline(self, status):
        inc = self.approved_incident()
        self.b.cards[inc['card']]['task']['status'] = status
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        self.assert_retained(inc)
        self.assertEqual(self.b.cards[inc['card']]['task']['status'], status)

    def test_approved_ready_delivers_through_effect_boundary(self):
        self.approved_deadline('ready')

    def test_approved_todo_delivers_through_effect_boundary(self):
        self.approved_deadline('todo')

    def test_approved_scheduled_delivers_through_effect_boundary(self):
        self.approved_deadline('scheduled')

    def test_initial_approved_queues_and_review_labels_stay_quiet(self):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.life.say(composition.APPROVER_A, 'Approve plan r1 for development')
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        card = self.life.orch()
        for status in ('ready', 'todo', 'scheduled', 'review'):
            with self.subTest(status=status):
                self.b.cards[card]['task']['status'] = status
                self.scans(18)
                self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])
                self.assertEqual(self.carriers(), [])
                self.assertEqual(self.notices(), [])

    def test_admitted_approved_active_worker_or_reviewer_stays_quiet(self):
        inc = self.approved_incident()
        self.b.claim(inc['card'], 'orchestrator')
        self.w.clock.now_ms = inc['abs_ms'] + 1
        for status in ('running', 'review', 'ready'):
            with self.subTest(status=status):
                self.b.cards[inc['card']]['task']['status'] = status
                self.assertNotEqual(self.check(inc), 0)
                self.scans(10)
                self.assertEqual(self.notices(), [])
                self.assert_retained(inc)

    def test_admitted_approved_unknown_source_stays_unavailable(self):
        inc = self.approved_incident()
        self.b.cards[inc['card']]['task']['status'] = 'ready'
        self.w.clock.now_ms = inc['abs_ms'] + 1
        show = self.b.show
        with mock.patch.object(self.b, '_preworker_snapshot', return_value=None) as unavailable, \
                mock.patch.object(self.b, 'show', side_effect=lambda cid: KanbanResult(Outcome.DEFINITE_FAIL)
                                  if cid == inc['card'] else show(cid)):
            self.assertEqual(self.check(inc), 2)
            self.scans(15)
            self.assertTrue(unavailable.called)
        self.assertEqual(self.notices(), [])
        self.assert_retained(inc)

    def test_logical_incident_keeps_anchor_when_native_successor_create_fails(self):
        original.GeneralStallWakeTest.test_missing_request_owner_uses_logical_anchor_without_native_lookup(self)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.invalid_plan(); self.scans(18)
        row = self.failed_successor(inc)
        [continued] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((continued['id'], continued['card'], continued['run_id'], continued['abs_ms']),
                         (inc['id'], inc['card'], 0, inc['abs_ms']))
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(25)
        self.assert_delivered()
        self.assert_retained(continued)
        self.assertEqual(self.check(continued), 0)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', row['idem_key']), [row])
        self.assertFalse(any(card == inc['card'] for _, card in self.b.calls))

    def test_failed_successor_binding_rejects_unrelated_or_malformed_transition_at_effect(self):
        self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        row = self.failed_successor(inc)
        [current] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.check(current), 0)
        body = json.loads(row['body'])
        corruptions = [dict(body='not-json'), dict(body=json.dumps(dict(body, params=dict(body['params'], previous_card='t_foreign')))),
                       dict(body=json.dumps(dict(body, params=dict(body['params'], task='other')))),
                       dict(body=json.dumps(dict(body, params=dict(body['params'], seq=99)))),
                       dict(outcome='ambiguous'), dict(kind='kb_comment')]
        c = recovery.store_conn(self.w)
        try:
            for changes in corruptions:
                with self.subTest(changes=changes):
                    for field, value in changes.items():
                        c.execute('UPDATE outbox SET '+field+'=? WHERE idem_key=?', (value, row['idem_key']))
                    self.assertNotEqual(self.check(current), 0)
                    for field in changes:
                        c.execute('UPDATE outbox SET '+field+'=? WHERE idem_key=?', (row[field], row['idem_key']))
            self.assertEqual(self.check(current), 0)
            self.w.clock.now_ms = inc['abs_ms'] + 1
            clickup = main.Glue.clickup
            def suspend(glue, item):
                return False if json.loads(item['body']).get('recovery_notice') else clickup(glue, item)
            with mock.patch.object(main.Glue, 'clickup', suspend):
                self.scans(5)
            [notice] = self.notices()
            self.assertEqual((notice['state'], notice['attempts']), ('pending', 0))
            c.execute('UPDATE outbox SET body=? WHERE idem_key=?', (corruptions[1]['body'], row['idem_key']))
            self.scans(8)
            self.assertEqual(self.notices()[0]['state'], 'cancelled_stale')
            self.assertEqual(self.notices()[0]['attempts'], 0)
            self.assertEqual(self.w.bot_comments(self.tid)[-1]['comment_text'].count('no verified publication'), 0)
        finally:
            c.close()

    def test_failed_successor_running_owner_and_unknown_reads_refuse_admission(self):
        self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.failed_successor(inc)
        [current] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.check(current), 0)
        card = self.life.req_cards()[-1]
        self.b.claim(card, 'planner')
        self.assertNotEqual(self.check(current), 0)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(10)
        self.assertEqual(self.notices(), [])
        with mock.patch.object(self.b, '_preworker_snapshot', return_value=None) as unavailable:
            self.assertEqual(self.check(current), 2)
            self.scans(8)
            self.assertTrue(unavailable.called)
        self.assertEqual(self.notices(), [])
