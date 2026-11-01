"""Focused offline regressions: real World.main/Glue/effect admission."""
import json
import unittest
from unittest import mock

import test_outer_catchall as outer_fixture
import test_preworker_recovery as stage_fixture
from clickup_intake import engine, kanban_port as kp, main, progress
from clickup_intake.contracts import KanbanResult, Outcome


class UnavailableDeliveryTest(unittest.TestCase):
    setUp = outer_fixture.OuterCatchallTest.setUp
    scans = outer_fixture.OuterCatchallTest.scans
    carriers = outer_fixture.OuterCatchallTest.carriers

    def test_local_progress_during_native_failure_restarts_grace(self):
        self.scans(8)
        real = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                KanbanResult(Outcome.DEFINITE_FAIL) if card == 't_e0000016' else real(card)), \
                mock.patch.object(self.b, '_preworker_snapshot', return_value=None):
            self.scans(1)
            before = json.loads(self.w.q("SELECT value FROM meta WHERE key=?", 'outer_observed:' + self.tid)[0]['value'])
            conn = outer_fixture.recovery.store_conn(self.w)
            conn.execute("UPDATE tasks SET phase='implemented' WHERE clickup_task_id=?", (self.tid,))
            conn.close()
            self.scans(5)
            after = json.loads(self.w.q("SELECT value FROM meta WHERE key=?", 'outer_observed:' + self.tid)[0]['value'])
            self.assertGreater(after['first_ms'], before['first_ms'])
            self.assertEqual(self.carriers(), [])

    def test_failed_native_reads_keep_clock_and_deliver_truthful_diagnosis(self):
        self.scans(8)
        [clock] = self.w.q("SELECT value FROM meta WHERE key=?", 'outer_observed:' + self.tid)
        first_ms = json.loads(clock['value'])['first_ms']
        real = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                KanbanResult(Outcome.DEFINITE_FAIL) if card == 't_e0000016' else real(card)), \
                mock.patch.object(self.b, '_preworker_snapshot', return_value=None):
            self.scans(20)
            clocks = self.w.q("SELECT value FROM meta WHERE key=?", 'outer_observed:' + self.tid)
            self.assertEqual(len(clocks), 1, 'native failure must retain diagnostic responsibility')
            self.assertEqual(json.loads(clocks[0]['value'])['first_ms'], first_ms)
            [inc] = self.w.q('SELECT * FROM recoveries')
            self.assertEqual(inc['attempts'], 1)
            [wake] = self.w.q("SELECT state,attempts FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%'")
            self.assertEqual(wake, dict(state='done', attempts=1))
            carrier = self.b.cards[self.carriers()[0]]
            self.assertIn('unavailable', carrier['task']['body'])
            self.assertEqual([s['delivery_mode'] for s in carrier['subs']], ['wake'])
            self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
            conn = outer_fixture.recovery.store_conn(self.w)
            conn.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
            conn.close()
            self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
        self.assertEqual(self.w.q('SELECT state FROM approvals WHERE id=33')[0]['state'], 'recorded')
        [stage] = self.w.q('SELECT state,attempts FROM outbox WHERE idem_key=?', outer_fixture.FIXTURE['pending']['idem_key'])
        self.assertEqual(stage, dict(state='pending', attempts=0))
        self.assertEqual(self.b.cards['t_e0000016']['parents'], [])
        self.assertEqual(self.b.cards['t_e000000c']['task']['status'], 'blocked')


class CatchallDeliveryTest(unittest.TestCase):
    setUp = stage_fixture.PreworkerRecoveryTest.setUp
    applications = stage_fixture.PreworkerRecoveryTest.applications
    incidents = stage_fixture.PreworkerRecoveryTest.incidents
    carriers = stage_fixture.PreworkerRecoveryTest.carriers
    scans = stage_fixture.PreworkerRecoveryTest.scans
    change = stage_fixture.PreworkerRecoveryTest.change
    stall = stage_fixture.PreworkerRecoveryTest.stall
    stopped_stage = stage_fixture.PreworkerRecoveryTest.stopped_stage
    wait = stage_fixture.PreworkerRecoveryTest.wait
    question = stage_fixture.PreworkerRecoveryTest.question

    def delivered(self, attempt=1):
        [inc] = self.incidents()
        self.assertEqual(inc['attempts'], attempt)
        key = engine._rcv_key(self.tid, inc['card'], inc['run_id'], attempt)
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key + ':wake')
        self.assertEqual((wake['state'], wake['attempts']), ('done', 1))
        [create] = self.w.q('SELECT result_ref FROM outbox WHERE idem_key=?', key + ':create')
        carrier = self.b.cards[create['result_ref']]
        self.assertEqual(carrier['task']['status'], 'done')
        self.assertEqual([s['delivery_mode'] for s in carrier['subs']], ['wake'])
        rc, lines = self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id']))
        self.assertEqual(rc, 0, lines)
        return inc

    def test_existing_stage_incident_continues_diagnosis_under_technical_hold(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            self.assertEqual(before['class'], 'approval_staging')
            self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
            self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
            self.scans(4)
            after = self.delivered(attempt=2)
            for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'event_created_s',
                          'block_kind', 'approval_id', 'approval_rev', 'plan_sha256', 'abs_ms', 'detected_ms'):
                self.assertEqual(after[field], before[field], field)
            self.assertEqual(after['class'], 'progress_stall')
            self.assertEqual(len(self.carriers()), 2)
            self.assertEqual(self.w.task(self.tid)['hold_reason'], 'internal_stage_wait')
            self.assertIsNone(self.life.gate())
            self.assertIsNone(self.life.orch())
            self.assertTrue(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%'"))
            self.scans(4)
            self.assertEqual(len(self.carriers()), 2)
            self.w.clock.now_ms = before['detected_ms'] + 181 * 60000
            self.scans(4)
            self.delivered(attempt=3)
            self.w.clock.now_ms = before['abs_ms'] + 1
            self.scans(4)
            self.assertEqual(self.incidents()[0]['attempts'], 3)
            self.assertEqual(len(self.carriers()), 3)

    def test_existing_incident_native_failure_reuses_budget_and_identity(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            source = self.life.req_cards()[0]
            real = self.b.show
            with mock.patch.object(self.b, 'show', side_effect=lambda card:
                    KanbanResult(Outcome.DEFINITE_FAIL) if card == source else real(card)), \
                    mock.patch.object(self.b, '_preworker_snapshot', return_value=None):
                self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
                self.scans(4)
                after = self.delivered(attempt=2)
                self.assertEqual((after['id'], after['abs_ms']), (before['id'], before['abs_ms']))
                self.assertIsNone(self.life.gate())
                self.assertIn('unavailable', self.b.cards[self.carriers()[-1]]['task']['body'])

    def test_revocation_allows_first_diagnosis_but_no_old_stage_work(self):
        with self.stopped_stage():
            self.scans(6)
            self.change("UPDATE approvals SET state='revoked',revoked_reason='new_input' WHERE task=?", self.tid)
            self.scans(18)
            self.delivered()
        self.scans(4)
        self.assertIsNone(self.life.orch())
        self.assertEqual(self.applications()[0]['state'], 'revoked')

    def test_unchanged_live_writer_allows_first_diagnosis_without_replacement(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            key = 'clickup-dp:%s:gate:r1:g%d' % (self.tid, a['accounts_gen'])
            self.change("UPDATE outbox SET state='sending',child_pid=4321,child_start_time='123' WHERE idem_key=?", key)
            with mock.patch.object(kp, 'child_alive', return_value=True):
                self.scans(18)
                self.delivered()
            [row] = self.w.q('SELECT state,child_pid,child_start_time FROM outbox WHERE idem_key=?', key)
            self.assertEqual(row, dict(state='sending', child_pid=4321, child_start_time='123'))
            self.assertIsNone(self.life.gate())

    def test_operator_pause_prevents_existing_incident_takeover(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            self.assertEqual(self.w.main('hold', self.tid)[0], 0)
            self.w.clock.now_ms = before['detected_ms'] + 181 * 60000
            self.scans(4)
            self.assertEqual(self.incidents()[0]['attempts'], 1)
            self.assertEqual(self.incidents()[0]['class'], 'approval_staging')
            self.assertEqual(len(self.carriers()), 1)
            self.assertEqual(self.w.main('recovery-check', '--card', before['card'], '--run', '0')[0], 3)

    def test_changed_approval_during_continuation_observation_refuses_admission(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            self.change("UPDATE tasks SET hold_reason='another_technical_wait' WHERE clickup_task_id=?", self.tid)
            self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
            self.scans(4)
            after = self.delivered(attempt=2)
            real = self.b._preworker_snapshot
            def changed(*args):
                proof = real(*args)
                self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', 'f' * 64, self.tid)
                return proof
            with mock.patch.object(self.b, '_preworker_snapshot', side_effect=changed):
                self.assertEqual(self.w.main('recovery-check', '--card', after['card'], '--run', '0')[0], 3)
            self.assertIsNone(self.life.gate())

    def test_continued_incident_still_observes_actual_owner_work(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
            self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
            self.scans(4)
            after = self.delivered(attempt=2)
            self.change('UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?', self.tid)
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.incidents()[0]['state'], 'open', 'READY is not proven work')
        self.life.orchestrate()
        last = self.incidents()[0]
        self.assertEqual(last['state'], 'resumed')
        for field in ('id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(last[field], after[field])

    def test_continued_diagnostic_question_still_pauses_ordinary_stage_work(self):
        with self.stopped_stage():
            self.scans(18)
            before = self.delivered()
            self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
            self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
            self.scans(4)
            inc = self.delivered(attempt=2)
            self.question(inc)
            self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
            self.change('UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?', self.tid)
        self.scans(6)
        self.assertIsNone(self.life.gate(), 'a current delivered decision still bars ordinary source work')

    def test_first_idle_ready_wake_completes_without_retry_or_authority(self):
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        orch = self.life.orch()
        self.assertEqual(self.b.cards[orch]['runs'], [])
        self.scans(18)
        self.delivered()
        self.assertEqual(self.b.cards[orch]['task']['status'], 'ready')
        self.assertEqual(self.b.cards[orch]['runs'], [])
        self.assertEqual(len(self.carriers()), 1)
