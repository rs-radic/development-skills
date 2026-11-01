"""SPEC correction regressions; real offline Glue path, no inherited test discovery."""
import json
import unittest
from unittest import mock

import test_general_stall_wake as original
import test_recovery as recovery
import test_s4_composition as composition
from clickup_intake import main, kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome


class GeneralStallCorrectionsTest(unittest.TestCase):
    setUp = original.GeneralStallWakeTest.setUp
    scans = original.GeneralStallWakeTest.scans
    carriers = original.GeneralStallWakeTest.carriers
    invalid_plan = original.GeneralStallWakeTest.invalid_plan
    wait = original.GeneralStallWakeTest.wait

    def notices(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")

    def rerun_invalid(self, card, claim=True):
        if claim:
            self.b.claim(card, 'planner')
        gen = self.w.task(self.tid)['input_gen']
        sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1, gen)
        self.b.cards[card]['runs'][-1]['metadata']['plan_sha256'] = 'ff7ec9d4placeholder'
        self.b.claim(card, 'plan-reviewer')
        self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
            plan_attachment='clickup-plan-r3.md', plan_sha256=sha, accounts_gen=gen,
            review_outcome='PASS', reviewed_sha256=sha))

    def successor(self, valid=False):
        before = len(self.life.req_cards())
        self.life.say(composition.REQ, 'Please clarify the planning handoff.', mention=composition.BOT)
        def finish(card, ref, gen):
            self.b.worker_comment(card, 'planner', composition.classify(ref, gen))
            if not valid:
                self.rerun_invalid(card, claim=False)
                return
            sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1, gen)
            self.b.claim(card, 'plan-reviewer')
            self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
                plan_attachment='clickup-plan-r3.md', plan_sha256=sha, accounts_gen=gen,
                review_outcome='PASS', reviewed_sha256=sha))
        self.w.step(lambda: len(self.life.req_cards()) > before and
                    self.b.cards[self.life.req_cards()[-1]]['task']['status'] == 'done',
                    world=self.life.planner(finish))
        return self.life.req_cards()[-1]

    def test_successor_keeps_original_incident_identity_deadline_and_wake_budget(self):
        card = self.invalid_plan(); self.scans(18)
        [before] = self.w.q('SELECT * FROM recoveries')
        original_runs = json.dumps(self.b.cards[card]['runs'], sort_keys=True)
        self.successor()
        self.scans(20)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        after = rows[0]
        for key in ('id', 'card', 'run_id', 'detected_ms', 'abs_ms', 'attempts'):
            self.assertEqual(after[key], before[key], key)
        self.assertNotEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(json.dumps(self.b.cards[card]['runs'], sort_keys=True), original_runs)
        self.assertTrue(self.w.q("SELECT 1 FROM meta WHERE key LIKE 'diagnostic_history:%'"))

    def test_verified_successor_publication_settles_original_not_just_done(self):
        self.invalid_plan(); self.scans(18)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.successor(valid=True)
        self.w.step(lambda: self.w.task(self.tid)['published_rev'] == 3)
        self.scans(6)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'closed')
        for key in ('id', 'card', 'run_id', 'attempts', 'abs_ms'):
            self.assertEqual(after[key], before[key])

    def test_known_unsent_notice_refreshes_without_rewriting_history(self):
        card = self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        clickup = main.Glue.clickup
        def suspend(glue, row):
            return False if json.loads(row['body']).get('recovery_notice') else clickup(glue, row)
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (card, inc['run_id']))
        with mock.patch.object(main.Glue, 'clickup', suspend):
            self.scans(6)
        [old] = self.notices()
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('pending', 0, None))
        self.rerun_invalid(card); self.scans(24)
        notices = self.notices()
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[0]['body'], old['body'])
        self.assertEqual(notices[0]['state'], 'cancelled_stale')
        self.assertEqual(notices[1]['state'], 'done')
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['attempts'], after['abs_ms']), (inc['attempts'], inc['abs_ms']))
        self.assertTrue(self.w.q("SELECT 1 FROM meta WHERE key LIKE 'diagnostic_history:%'"))

    def test_expired_deadline_survives_repeated_genuine_context_churn(self):
        card = self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.w.clock.now_ms = inc['abs_ms'] + 1
        for _ in range(4):
            self.rerun_invalid(card); self.scans(8)
        self.assertTrue(self.notices())
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['id'], after['abs_ms'], after['attempts']),
                         (inc['id'], inc['abs_ms'], inc['attempts']))

    def test_admitted_ready_without_pickup_reaches_original_deadline_but_running_is_quiet(self):
        card = self.invalid_plan(); self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.b.cards[card]['task']['status'] = 'running'
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(8)
        self.assertEqual(self.notices(), [])
        self.b.cards[card]['task']['status'] = 'ready'
        self.scans(15)
        self.assertEqual(len(self.notices()), 1, 'admitted READY must still reach its fixed deadline')
        [notice] = self.notices()
        self.assertEqual(notice['state'], 'done')
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        self.assertEqual(len(self.carriers()), 1)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['abs_ms'], after['attempts']), (inc['abs_ms'], inc['attempts']))

    def test_wake_retry_revalidates_after_negative_readback_before_resend(self):
        card = self.invalid_plan()
        complete = self.b.complete
        def ambiguous(cid, summary, metadata=None):
            if cid in self.carriers():
                return KanbanResult(Outcome.AMBIGUOUS)
            return complete(cid, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=ambiguous):
            self.w.step(lambda: bool(self.w.q("SELECT 1 FROM outbox WHERE idem_key LIKE '%:rcv:%:wake' AND state='ambiguous'")), limit=30)
        readback = kp._readback
        seen = []
        def race(port, intent):
            result = readback(port, intent)
            if intent.kind == kp.COMPLETE and intent.target in self.carriers() and not seen:
                seen.append(result[0])
                self.b.cards[card]['task']['status'] = 'running'
            return result
        before = len([c for c in self.b.calls if c[0] == 'complete'])
        with mock.patch.object(kp, '_readback', side_effect=race):
            self.scans(8)
        self.assertEqual(seen, [False])
        self.assertEqual(len([c for c in self.b.calls if c[0] == 'complete']), before)
        self.assertFalse(self.w.q("SELECT 1 FROM outbox WHERE idem_key LIKE '%:rcv:%:wake' AND state='done'"))

    def test_generated_create_failure_wakes_for_diagnosis_without_clearing_hold_or_retrying_source(self):
        create = self.b.create
        def failure(*args, **kwargs):
            if ':req:' in kwargs.get('idempotency_key', ''):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(*args, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failure):
            self.scans(100)
        [row] = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND idem_key LIKE '%:req:%'")
        self.assertEqual((row['state'], row['attempts']), ('failed', 6))
        self.assertEqual(self.life.req_cards(), [])
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + row['idem_key'])
        self.assertEqual(len(self.carriers()), 1)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['run_id'], 0)
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.assertFalse(any(card == inc['card'] for _, card in self.b.calls))
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(8)
        self.assertEqual(self.notices()[0]['state'], 'done')
        [after] = self.w.q("SELECT * FROM outbox WHERE idem_key=?", row['idem_key'])
        self.assertEqual(after, row)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + row['idem_key'])

    def test_answered_wait_allows_current_context_continuation_with_cumulative_notice_sends(self):
        original.GeneralStallWakeTest.test_preapproval_question_uses_json_null_and_verified_answer_lifecycle(self)
        [before] = self.w.q('SELECT * FROM recoveries')
        old = self.notices()[0]
        self.assertEqual(self.wait()['state'], 'answered')
        self.rerun_invalid(before['card']); self.scans(18)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertNotEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(after['abs_ms'], before['abs_ms'])
        self.assertEqual(after['attempts'], before['attempts'] + 1)
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[-1], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (after['card'], after['run_id']))
        self.scans(8)
        rows = self.notices()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['body'], old['body'])
        self.assertEqual(rows[1]['state'], 'done')
        self.assertEqual(rows[1]['attempts'], old['attempts'] + 1)
        self.assertEqual(rows[1]['first_send_ms'], old['first_send_ms'])

    def test_ambiguous_or_operator_failure_holds_never_authorize_diagnostic_effects(self):
        create = self.b.create
        def failure(*args, **kwargs):
            if ':req:' in kwargs.get('idempotency_key', ''):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(*args, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failure):
            self.w.step(lambda: bool(self.w.task(self.tid)['hold_reason']), limit=150)
        [row] = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND idem_key LIKE '%:req:%'")
        self.assertEqual(row['state'], 'failed')
        c = recovery.store_conn(self.w)
        c.execute("UPDATE outbox SET outcome='ambiguous' WHERE idem_key=?", (row['idem_key'],))
        c.close()
        self.scans(24)
        self.assertEqual(self.carriers(), [])
        c = recovery.store_conn(self.w)
        c.execute("UPDATE outbox SET outcome='retry_wait' WHERE idem_key=?", (row['idem_key'],))
        c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
        c.close()
        self.scans(24)
        self.assertEqual(self.carriers(), [])

    def test_ambiguous_notice_context_change_never_rearms_or_refunds(self):
        original.GeneralStallWakeTest.test_ambiguous_origin_delivery_is_retained_without_repeated_sends(self)
        [old] = self.notices()
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.rerun_invalid(inc['card']); self.scans(24)
        [after] = self.notices()
        self.assertEqual(after, old)
        [current] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(current['attempts'], inc['attempts'])
        self.assertEqual(current['abs_ms'], inc['abs_ms'])

    def test_logical_incident_survives_native_request_creation(self):
        original.GeneralStallWakeTest.test_missing_request_owner_uses_logical_anchor_without_native_lookup(self)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.invalid_plan(); self.scans(18)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['id'], rows[0]['card'], rows[0]['abs_ms'], rows[0]['attempts']),
                         (inc['id'], inc['card'], inc['abs_ms'], inc['attempts']))
        self.assertNotEqual(rows[0]['reason_sha256'], inc['reason_sha256'])

    def test_fresh_wake_keeps_the_existing_single_admission_read_budget(self):
        gate = main.Glue.recovery_gate
        calls = []
        def check(glue, row, payload):
            calls.append(row['idem_key'])
            return gate(glue, row, payload)
        with mock.patch.object(main.Glue, 'recovery_gate', new=check):
            self.invalid_plan(); self.scans(18)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(len(calls), 1, 'post-readback guard is for retries, not a duplicate fresh-send observation')

    def test_unresolved_logical_anchor_never_reads_native_even_when_state_unavailable(self):
        before = list(self.b.calls)
        for result in (None, {'incident': None, 'task': None, 'approvals': [], 'orch': {}}):
            with self.subTest(snapshot=result):
                with mock.patch.object(main, 'load_recovery_snapshot', return_value=result):
                    code, _ = self.w.main('recovery-check', '--card', 'progress-t1000-request', '--run', '999')
                self.assertNotEqual(code, 0)
                self.assertEqual(self.b.calls, before)
