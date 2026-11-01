"""SPEC-2: retain the naturally admitted non-staging diagnostic owner."""
import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import test_general_stall_wake as general_fixture
import test_recovery as recovery
from clickup_intake.contracts import KanbanResult, Outcome


class ProgressContinuationTest(unittest.TestCase):
    setUp = general_fixture.GeneralStallWakeTest.setUp
    scans = general_fixture.GeneralStallWakeTest.scans
    carriers = general_fixture.GeneralStallWakeTest.carriers
    invalid_plan = general_fixture.GeneralStallWakeTest.invalid_plan

    def state(self):
        return dict(incidents=self.w.q('SELECT * FROM recoveries'),
                    wakes=self.w.q("SELECT idem_key,state,attempts FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%'"),
                    carriers=self.carriers(),
                    notices=self.w.q("SELECT idem_key,state,attempts FROM outbox WHERE kind LIKE 'cu_%' AND idem_key LIKE '%:recovery-notice:%'"))

    def admission(self, inc):
        return self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id']))

    def change(self, sql, *args):
        conn = recovery.store_conn(self.w)
        try:
            conn.execute(sql, args)
        finally:
            conn.close()

    def record(self, value):
        if os.environ.get('R3_OBSERVATIONS'):
            with Path(os.environ['R3_OBSERVATIONS']).open('a', encoding='utf8') as output:
                output.write(json.dumps(dict(id=self.id(), **value)) + '\n')

    def test_nonstaging_incapable_incident_keeps_owner_wake_and_deadline(self):
        # Exact spec-r2-probes.generic sequence: real invalid plan and 18 scans.
        source = self.invalid_plan()
        self.scans(18)
        before = self.state()
        [inc] = before['incidents']
        self.assertEqual((inc['class'], inc['card'], inc['event_kind']),
                         ('progress_stall', source, 'progress_stall'))
        before['admission'] = self.admission(inc)
        self.assertEqual(before['admission'][0], 0, before)
        self.assertEqual(before['wakes'][0]['state'], 'done')
        self.assertEqual(inc['attempts'], 1)
        original_cards = {k: copy.deepcopy(v) for k, v in self.b.cards.items() if k not in self.carriers()}
        mutations_before = len(self.b.mutations())
        source_rows = self.w.q("SELECT idem_key,state,attempts FROM outbox WHERE idem_key NOT LIKE '%:rcv:%' AND idem_key NOT LIKE '%:recovery-notice:%' AND kind<>'alert'")
        self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(25)
        after = self.state()
        after['admission'] = self.admission(inc)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(20)
        deadline = self.state()
        deadline['admission'] = self.admission(inc)
        self.record(dict(before=before, after=after, after_absolute_deadline=deadline,
                         source=source, task=self.w.task(self.tid),
                         mutations=self.b.mutations()[mutations_before:],
                         history=self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%'")))
        self.assertEqual({k: v for k, v in self.b.cards.items() if k in original_cards}, original_cards)
        self.assertEqual(set(self.b.cards) - set(original_cards), set(self.carriers()))
        self.assertTrue(all(card in self.carriers() for _, card in self.b.mutations()[mutations_before:]))
        self.assertEqual(self.w.q("SELECT idem_key,state,attempts FROM outbox WHERE idem_key NOT LIKE '%:rcv:%' AND idem_key NOT LIKE '%:recovery-notice:%' AND kind<>'alert'"), source_rows)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'internal_stage_wait')
        self.assertIsNone(self.w.task(self.tid)['published_rev'])
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])
        self.assertEqual(after['admission'][0], 0, after)
        self.assertGreater(after['incidents'][0]['attempts'], inc['attempts'])
        self.assertEqual(after['wakes'][-1]['state'], 'done')
        for state in (after, deadline):
            self.assertEqual(len(state['incidents']), 1)
            for key in ('id', 'task', 'card', 'run_id', 'event_kind', 'event_created_s', 'block_kind',
                        'approval_id', 'approval_rev', 'plan_sha256', 'detected_ms', 'abs_ms', 'adopted'):
                self.assertEqual(state['incidents'][0][key], inc[key], key)
            self.assertEqual(state['incidents'][0]['state'], 'open', 'a notice is not recovery')
            self.assertLessEqual(state['incidents'][0]['attempts'], 3)
        self.assertEqual(len(deadline['wakes']), len(after['wakes']), 'deadline cannot refund a wake')
        self.assertEqual(len(deadline['notices']), 1)
        self.assertEqual(deadline['notices'][0]['state'], 'done')
        self.assertTrue(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%'"))
        self.scans(4)
        self.assertEqual(self.state(), {k: v for k, v in deadline.items() if k != 'admission'})

    def test_different_hold_keeps_prior_failed_carrier_spend(self):
        source = self.invalid_plan()
        original = self.b.complete
        with mock.patch.object(self.b, 'complete', side_effect=lambda card, *args, **kwargs:
                KanbanResult(Outcome.DEFINITE_FAIL) if card in self.carriers() else original(card, *args, **kwargs)):
            self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['attempts'], 1)
        first_carrier = self.carriers()[0]
        [spent] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%'")
        self.assertGreater(spent['attempts'], 0)
        self.assertIsNotNone(spent['first_send_ms'])
        self.assertNotEqual(spent['state'], 'done')
        source_before = copy.deepcopy(self.b.cards[source])
        self.change("UPDATE tasks SET hold_reason='another_technical_wait' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(4)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['id'], after['attempts'], after['abs_ms']), (inc['id'], 2, inc['abs_ms']))
        [retired] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', spent['idem_key'])
        for field in ('attempts', 'first_send_ms', 'payload_sha256'):
            self.assertEqual(retired[field], spent[field], field)
        self.assertEqual(retired['state'], 'cancelled_stale')
        self.assertEqual(self.b.cards[first_carrier]['task']['status'], 'archived')
        self.assertEqual(self.state()['wakes'][-1]['state'], 'done')
        self.assertEqual(self.admission(after)[0], 0)
        self.assertEqual(self.b.cards[source], source_before)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'another_technical_wait')
        self.record(dict(before=inc, after=after, spent=spent, retired=retired, state=self.state()))

    def test_unavailable_source_continues_same_diagnostic_truthfully(self):
        source = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        source_before = copy.deepcopy(self.b.cards[source])
        original = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                KanbanResult(Outcome.DEFINITE_FAIL) if card == source else original(card)), \
                mock.patch.object(self.b, '_preworker_snapshot', return_value=None):
            self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
            self.scans(6)
            [after] = self.w.q('SELECT * FROM recoveries')
            self.assertEqual((after['id'], after['attempts'], after['abs_ms']), (inc['id'], 2, inc['abs_ms']))
            self.assertEqual(self.state()['wakes'][-1]['state'], 'done')
            self.assertEqual(self.admission(after)[0], 0)
            self.assertIn('unavailable', self.b.cards[self.carriers()[-1]]['task']['body'])
        self.assertEqual(self.b.cards[source], source_before)
        self.assertIsNone(self.w.task(self.tid)['published_rev'])
        self.record(dict(before=inc, after=after, state=self.state()))

    def test_operator_hold_prevents_nonstaging_continuation(self):
        self.invalid_plan()
        self.scans(18)
        before = self.state()
        [inc] = before['incidents']
        self.change("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(25)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.admission(inc)[0], 3)
        self.assertEqual(self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_continuation:%'"), [])

    def test_continued_native_obligation_closes_only_on_verified_publication(self):
        source = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(4)
        [continued] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(continued['attempts'], 2)
        original = copy.deepcopy(self.b.cards[source]['runs'][-2]['metadata'])
        self.change('UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?', self.tid)
        self.b.claim(source, 'planner')
        digest = self.b.request_review(source, self.tid, 3, general_fixture.composition.PLAN_R1,
                                       self.w.task(self.tid)['classified_gen'])
        self.b.claim(source, 'plan-reviewer')
        self.scans(12)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.b.finish(source, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
            plan_attachment='clickup-plan-r3.md', plan_sha256=digest,
            accounts_gen=self.w.task(self.tid)['classified_gen'], review_outcome='PASS', reviewed_sha256=digest))
        self.w.step(lambda: self.w.task(self.tid)['published_rev'] == 3)
        self.scans(5)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.record(dict(before=inc, continued=continued, after=after))
        self.assertEqual(after['state'], 'closed')
        for key in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[key], continued[key], key)
        self.assertEqual(self.b.cards[source]['runs'][2]['metadata'], original)
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])

    def test_verified_human_wait_prevents_nonstaging_continuation(self):
        self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        question = dict(task=self.tid, card=inc['card'], run=inc['run_id'], approval=None,
                        revision=None, sha256=None, fingerprint=inc['reason_sha256'],
                        failed='The planning recipient is ambiguous.', attempted='Read current task and handoff.',
                        decision='Which existing planning contact should receive the clarification, A or B?',
                        next_action='Reply with the contact, without credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\nCLICKUP-RECOVERY-QUESTION v1 %s'
            % (inc['card'], inc['run_id'], json.dumps(question)))
        def wait():
            rows = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait:%'")
            return json.loads(rows[0]['value']) if rows else {}
        self.w.step(lambda: wait().get('state') == 'waiting', limit=15)
        self.assertTrue(wait()['root'])
        before = self.state()
        self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(20)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.admission(inc)[0], 3)
        self.assertEqual(self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_continuation:%'"), [])

    def test_continued_approved_native_incident_retains_current_approval_guard(self):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.life.say(general_fixture.composition.APPROVER_A, 'Approve plan r1 for development')
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        source = self.life.orch()
        self.b.claim(source, 'orchestrator')
        self.b.finish(source, 'orchestrator', {})
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((inc['card'], inc['event_kind']), (source, 'progress_stall'))
        self.assertIsNotNone(inc['approval_id'])
        self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(4)
        self.assertEqual(self.admission(inc)[0], 0)
        self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', 'f' * 64, self.tid)
        self.scans(20)
        self.record(dict(before=inc, after=self.state(), admission=self.admission(inc)))
        self.assertEqual(self.admission(inc)[0], 3)

    def test_approval_change_before_first_continuation_stays_stale(self):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.life.say(general_fixture.composition.APPROVER_A, 'Approve plan r1 for development')
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        source = self.life.orch()
        self.b.claim(source, 'orchestrator')
        self.b.finish(source, 'orchestrator', {})
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertIsNotNone(inc['approval_id'])
        self.assertEqual(self.admission(inc)[0], 0)
        native_before = copy.deepcopy(self.b.cards[source])
        self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', 'f' * 64, self.tid)
        self.change("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", self.tid)
        self.assertNotEqual(self.admission(inc)[0], 0)
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(4)
        self.assertNotEqual(self.admission(inc)[0], 0)
        [after] = self.w.q('SELECT * FROM recoveries')
        for key in ('id', 'card', 'run_id', 'approval_id', 'approval_rev', 'plan_sha256', 'attempts', 'abs_ms'):
            self.assertEqual(after[key], inc[key], key)
        self.assertEqual(self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_continuation:%'"), [])
        self.assertEqual(self.b.cards[source], native_before)
