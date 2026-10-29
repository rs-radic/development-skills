"""RR1: real main scan/progress/evaluate/execute; only native/provider ports are fake."""
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import Outcome
import test_stalled_remediation as rem
import test_s4_composition as comp


class QuestionCancellationTest(unittest.TestCase):
    setUp = rem.RemediationTest.setUp
    carriers = rem.RemediationTest.carriers
    scan_minutes = rem.RemediationTest.scan_minutes
    notice_rows = rem.RemediationTest.notice_rows
    unknown_stop = rem.RemediationTest.unknown_stop
    question_ack = rem.RemediationTest.question_ack
    diagnostic_wait = rem.RemediationTest.diagnostic_wait
    answered_status_input = rem.RemediationTest.answered_status_input

    def snapshot(self):
        return {'incident': self.w.q('SELECT * FROM recoveries')[0],
                'wait': self.diagnostic_wait(), 'notices': self.notice_rows(),
                'carriers': len(self.carriers()), 'operator_lines': self.w.outputs[:],
                'input_gen': self.w.task(self.tid)['input_gen'],
                'classified_gen': self.w.task(self.tid)['classified_gen']}

    def finish_current_question(self, card, run, before, old, evidence):
        self.scan_minutes(18)
        rebound = self.snapshot()
        q = self.question_ack(card, run, decision='The current dependency is satisfied. Should the already-approved option A or B be used?')
        self.scan_minutes(10)
        corrected = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        final = self.snapshot()
        self.evidence = dict(evidence, rebound=rebound, corrected=corrected, final=final)
        self.assertNotEqual(rebound['incident']['reason_sha256'], before['reason_sha256'])
        self.assertTrue(any(q['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)),
                        'a known-unsent cancelled question must not remain publishing forever and ignore a valid current-bound correction')
        for field in ('id', 'approval_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(rebound['incident'][field], before[field], field)
        self.assertEqual(final['wait']['state'], 'waiting')
        self.assertTrue(final['wait']['root'])
        self.assertEqual(final['wait']['question'], q)
        self.assertNotEqual(final['wait']['key'], old['idem_key'])
        self.assertEqual(final['notices'][0], old, 'failed/cancelled outbox history is immutable')
        histories = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait_history:%'")
        self.assertTrue(any(json.loads(r['value'])['key'] == old['idem_key'] and
                            json.loads(r['value'])['state'] == 'publishing' for r in histories))
        self.assertEqual(final['carriers'], 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)
        self.assertEqual(len([c for c in self.w.bot_comments(self.tid) if q['decision'] in c['comment_text']]), 1)
        old_text = json.loads(old['body'])['text']
        self.assertFalse(any(old_text in c['comment_text'] for c in self.w.bot_comments(self.tid)))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_queued_question_cancelled_by_changed_parent_accepts_current_correction(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        self.question_ack(card, run)
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        queued = self.snapshot()
        self.assertEqual((queued['notices'][0]['state'], queued['notices'][0]['attempts']), ('pending', 0))
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        parent = self.b._new('Current dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        cancelled = self.snapshot()
        old = cancelled['notices'][0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('cancelled_stale', 0, None))
        self.assertEqual(self.w.cu.count('POST', path), count)
        self.b.cards[parent]['task']['status'] = 'done'
        self.finish_current_question(card, run, before, old, {'queued': queued, 'cancelled': cancelled})

    def test_actual_503_then_changed_parent_accepts_current_correction(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        failed = self.snapshot()
        self.assertEqual(self.w.cu.count('POST', path) - count, 1, 'actual fake-transport POST, not an executor stub')
        self.assertEqual((failed['notices'][0]['attempts'], failed['notices'][0]['result_ref']), (1, None))
        parent = self.b._new('Newly known current dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        cancelled = self.snapshot()
        old = cancelled['notices'][0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('cancelled_stale', 1, None))
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.b.cards[parent]['task']['status'] = 'done'
        self.finish_current_question(card, run, before, old, {'failed_send': failed, 'cancelled': cancelled})

    def test_actual_503_then_real_answer_only_input_accepts_current_correction(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        failed = self.snapshot()
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual((failed['notices'][0]['attempts'], failed['notices'][0]['result_ref']), (1, None))
        self.assertEqual((failed['input_gen'], failed['classified_gen']), (3, 3))
        self.answered_status_input()  # actual addressed top-level request and existing planner classification
        self.scan_minutes(5)
        cancelled = self.snapshot()
        self.assertEqual((cancelled['input_gen'], cancelled['classified_gen']), (4, 4))
        old = cancelled['notices'][0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('cancelled_stale', 1, None))
        self.finish_current_question(card, run, before, old, {'failed_send': failed, 'cancelled': cancelled})

    def test_unpublished_question_is_not_reported_as_a_verified_human_wait(self):
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        self.assertEqual(self.diagnostic_wait()['state'], 'publishing')
        self.assertFalse(self.diagnostic_wait().get('root'))
        before = list(self.b.mutations())
        admission = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.evidence = {'publishing': self.snapshot(), 'admission': admission}
        self.assertEqual(admission, (3, ['STALE diagnostic_notice_unfinished']))
        self.assertEqual(self.b.mutations(), before)
        parent = self.b._new('Current dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(18)
        self.assertEqual(self.diagnostic_wait()['state'], 'superseded')
        self.assertFalse(self.diagnostic_wait().get('root'))
        self.assertNotEqual(self.diagnostic_wait()['key'], self.notice_rows()[0]['idem_key'].split(':ctx:')[0] +
                            ':ctx:' + self.snapshot()['incident']['reason_sha256'])
        admission = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertNotIn('STALE diagnostic_human_wait', admission[1])

    def test_ambiguous_applied_question_reconciles_after_context_change_without_resend(self):
        card, run = self.unknown_stop()
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=True)
        q = self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'ambiguous')
        publishing = self.snapshot()
        parent = self.b._new('Dependency observed after applied POST', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(10)
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(18)
        self.evidence = {'publishing': publishing, 'reconciled': self.snapshot()}
        [notice] = self.notice_rows()
        self.assertEqual((notice['state'], notice['attempts']), ('done', 1))
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.assertEqual(self.diagnostic_wait()['root'], notice['result_ref'])
        self.assertEqual(self.diagnostic_wait()['question'], q)
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_wait_history:%'"), [])
        self.assertEqual(len(self.carriers()), 1)

    def test_uncertain_unapplied_question_is_not_retired_or_resent_on_context_turnover(self):
        card, run = self.unknown_stop()
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'ambiguous')
        original = self.diagnostic_wait()
        parent = self.b._new('Dependency while delivery is uncertain', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(35)
        self.evidence = {'final': self.snapshot()}
        [notice] = self.notice_rows()
        self.assertEqual((notice['state'], notice['attempts']), ('held_ambiguous', 1))
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual(self.diagnostic_wait(), original)
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_wait_history:%'"), [])
        self.assertTrue(any('still ambiguous' in line for line in self.w.outputs))

    def test_explicit_question_abandonment_survives_real_input_and_context_turnover(self):
        card, run = self.unknown_stop()
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'held_ambiguous')
        [notice] = self.notice_rows()
        self.assertEqual(self.w.main('outbox', 'abandon', notice['idem_key'])[0], 0)
        original = self.snapshot()
        self.answered_status_input()
        self.w.clock.now_ms = original['incident']['abs_ms'] + 1
        self.scan_minutes(18)
        self.evidence = {'abandoned': original, 'after_turnover': self.snapshot()}
        self.assertEqual(self.notice_rows()[0]['outcome'], 'ABANDONED')
        self.assertEqual(self.diagnostic_wait(), original['wait'])
        self.assertEqual(self.snapshot()['incident'], original['incident'])
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE operator_abandoned']))

    def test_verified_waiting_and_classifying_question_stays_quiet_past_deadline(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        root = self.diagnostic_wait()['root']
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.life.say(comp.APPROVER_A, 'Please clarify the allowed choices.', mention=comp.BOT, reply_to=root)
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'))  # no sufficient resolution emitted
        self.w.step(lambda: self.w.task(self.tid)['input_gen'] > 3 and
                    self.w.task(self.tid)['classified_gen'] == self.w.task(self.tid)['input_gen'],
                    world=self.life.planner(script))
        self.scan_minutes(10)
        self.evidence = {'classifying': self.snapshot()}
        self.assertEqual(self.diagnostic_wait()['state'], 'classifying')
        self.assertEqual(self.diagnostic_wait()['root'], root)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(self.snapshot()['incident']['abs_ms'], before['abs_ms'])
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_answered_question_reenters_bounded_wake_and_original_deadline_escalation(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        root = self.diagnostic_wait()['root']
        self.life.say(comp.APPROVER_A, 'Use existing provider A.', mention=comp.BOT, reply_to=root)
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'Existing provider A is within unchanged authority.',
                                 gen=gen, diagnostic=before['id'], authority='unchanged'))
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'answered',
                    world=self.life.planner(script))
        self.w.clock.now_ms = before['detected_ms'] + 62 * 60_000
        self.scan_minutes(6)
        retried = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        final = self.snapshot()
        self.evidence = {'retried': retried, 'final': final}
        self.assertEqual(retried['carriers'], 2)
        self.assertEqual(final['incident']['state'], 'escalated')
        for field in ('id', 'abs_ms', 'detected_ms'):
            self.assertEqual(final['incident'][field], before[field], field)
        self.assertEqual(final['wait']['state'], 'answered')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(final['notices'][-1]['state'], 'done')
        text = json.loads(final['notices'][-1]['body'])['text']
        self.assertTrue(any(text in c['comment_text'] for c in self.w.bot_comments(self.tid)))
