"""RR1-roundtrip: real scan/progress/evaluate/execute, fake native and ClickUp ports."""
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import Outcome
import test_stalled_question_cancellation as cancellation


class NoticeRoundtripTest(unittest.TestCase):
    setUp = cancellation.QuestionCancellationTest.setUp
    carriers = cancellation.QuestionCancellationTest.carriers
    scan_minutes = cancellation.QuestionCancellationTest.scan_minutes
    notice_rows = cancellation.QuestionCancellationTest.notice_rows
    unknown_stop = cancellation.QuestionCancellationTest.unknown_stop
    question_ack = cancellation.QuestionCancellationTest.question_ack
    diagnostic_wait = cancellation.QuestionCancellationTest.diagnostic_wait
    snapshot = cancellation.QuestionCancellationTest.snapshot

    def test_cancelled_question_same_fingerprint_accepts_fresh_current_question(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        obsolete = self.question_ack(card, run)
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        queued = self.snapshot()
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        parent = self.b._new('A newly observed current dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        cancelled = self.snapshot()
        old = cancelled['notices'][0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('cancelled_stale', 0, None))
        self.assertEqual(self.w.cu.count('POST', path), count)
        self.b.cards[card]['parents'].remove(parent)  # existing work-owner dependency input
        self.scan_minutes(18)
        restored = self.snapshot()
        self.assertEqual(restored['incident']['reason_sha256'], before['reason_sha256'])
        self.assertEqual(restored['wait']['state'], 'superseded')
        self.assertFalse(restored['wait'].get('root'))
        current = self.question_ack(card, run, decision='The temporary dependency was removed. Use approved option A or B?')
        self.scan_minutes(10)
        corrected = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        final = self.snapshot()
        self.evidence = dict(queued=queued, cancelled=cancelled, restored=restored, corrected=corrected, final=final)
        self.assertTrue(any(current['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)),
                        'RR1-roundtrip: the occupied cancelled key must not strand a fresh current-bound question')
        self.assertEqual(final['notices'][0], old, 'cancelled delivery history must remain immutable')
        self.assertEqual(len(final['notices']), 2)
        self.assertEqual(final['notices'][1]['state'], 'done')
        self.assertNotEqual(final['notices'][1]['idem_key'], old['idem_key'])
        self.assertEqual(final['wait']['state'], 'waiting')
        self.assertEqual(final['wait']['question'], current)
        self.assertEqual(final['wait']['key'], final['notices'][1]['idem_key'])
        self.assertEqual(final['wait']['root'], final['notices'][1]['result_ref'])
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertFalse(any(obsolete['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)))
        for field in ('id', 'approval_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(final['incident'][field], before[field], field)
        histories = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait_history:%'")
        self.assertTrue(any(json.loads(r['value'])['key'] == old['idem_key'] for r in histories))
        self.assertEqual(final['carriers'], 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_definite_503_roundtrip_preserves_spent_transport_budget_on_current_question(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        failed = self.snapshot()
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual(failed['notices'][0]['attempts'], 1)
        parent = self.b._new('Temporary dependency after definite failure', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        old = self.notice_rows()[0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']), ('cancelled_stale', 1, None))
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(18)
        self.assertEqual(self.snapshot()['incident']['reason_sha256'], before['reason_sha256'])
        current = self.question_ack(card, run, decision='The temporary dependency is gone. Choose existing approved option A or B.')
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(lambda: len(self.notice_rows()) == 2)
        pending = self.snapshot()
        self.evidence = dict(failed=failed, pending=pending)
        self.assertEqual(pending['notices'][1]['attempts'], 1,
                         'a new publication must retain the definite spent attempt, not acquire a fresh transport budget')
        self.assertEqual(pending['notices'][1]['next_attempt_ms'], old['next_attempt_ms'])
        self.assertEqual(pending['notices'][1]['first_send_ms'], old['first_send_ms'])
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')  # new Glue/Store per scan
        final = self.snapshot()
        self.evidence['final'] = final
        self.assertEqual(len(final['notices']), 2)
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][1]['state'], final['notices'][1]['attempts']), ('done', 2))
        [mirror] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', final['notices'][1]['idem_key'] + '#cu')
        self.assertEqual(mirror['attempts'], 2)
        self.assertEqual(final['wait']['question'], current)
        self.assertEqual(final['wait']['state'], 'waiting')
        self.assertEqual(self.w.cu.count('POST', path) - count, 2)
        for field in ('id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(final['incident'][field], before[field], field)
        self.assertEqual(final['carriers'], 1)

    def test_definite_503_mirror_committed_before_outer_crash_retains_actual_spent_attempt(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.question_ack(card, run)
        real_update = main.Glue.update
        class AfterMirrorCommit(BaseException):
            pass
        def update(glue, key, **fields):
            if ':recovery-notice:' in key and fields.get('state') == 'retry_wait':
                raise AfterMirrorCommit('fake crash after definite-failure mirror persistence, before outer copy')
            return real_update(glue, key, **fields)
        with mock.patch.object(main.Glue, 'update', update):
            with self.assertRaises(AfterMirrorCommit):
                self.scan_minutes(6)
        [outer] = self.notice_rows()
        [mirror] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', outer['idem_key'] + '#cu')
        self.assertEqual((outer['attempts'], mirror['attempts']), (0, 1))
        parent = self.b._new('Temporary dependency before resuming mirror/outer copy', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        old = self.notice_rows()[0]
        self.assertEqual(old['state'], 'cancelled_stale')
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(18)
        self.assertEqual(self.snapshot()['incident']['reason_sha256'], before['reason_sha256'])
        self.question_ack(card, run, decision='Current restored context after definite failure: choose approved option A or B.')
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(lambda: len(self.notice_rows()) == 2)
        pending = self.snapshot()
        self.evidence = dict(old_outer=old, old_mirror=mirror, pending=pending)
        self.assertEqual(pending['notices'][1]['attempts'], 1,
                         'the actual transport mirror, not a lagging outer copy, owns the spent budget')
        self.assertEqual(pending['notices'][1]['next_attempt_ms'], mirror['next_attempt_ms'])
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        final = self.snapshot()
        self.evidence['final'] = final
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][1]['state'], final['notices'][1]['attempts']), ('done', 2))
        self.assertEqual(self.w.cu.count('POST', path) - count, 2)
        self.assertEqual(final['incident']['abs_ms'], before['abs_ms'])

    def cancelled_roundtrip(self, definite_failure=False):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        question = self.question_ack(card, run)
        if definite_failure:
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
            self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        else:
            with mock.patch.object(main.Glue, 'clickup', return_value=False):
                self.w.step(self.notice_rows)
        parent = self.b._new('Temporary dependency to restore unchanged context', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        old = self.notice_rows()[0]
        self.assertEqual((old['state'], old['attempts'], old['result_ref']),
                         ('cancelled_stale', int(definite_failure), None))
        self.assertEqual(self.w.cu.count('POST', path) - count, int(definite_failure))
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(18)
        restored = self.snapshot()
        self.assertEqual(restored['incident']['reason_sha256'], before['reason_sha256'])
        self.assertEqual(restored['wait']['state'], 'superseded')
        self.assertNotIn('STALE diagnostic_human_wait',
                         self.w.main('recovery-check', '--card', card, '--run', str(run))[1])
        return card, run, before, old, question, path, count

    def deadline_after_roundtrip(self, definite_failure):
        card, run, before, old, question, path, count = self.cancelled_roundtrip(definite_failure)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.w.step(lambda: self.snapshot()['incident']['state'] == 'escalated')
        final = self.snapshot()
        self.scan_minutes(10)
        self.evidence = dict(old=old, final=final, unchanged=self.snapshot())
        self.assertEqual(len(final['notices']), 2,
                         'the original absolute deadline must reach a new technical publication despite the historical occupied key')
        self.assertEqual(final['notices'][0], old)
        notice = final['notices'][1]
        self.assertEqual((notice['state'], notice['attempts']), ('done', 1 + int(definite_failure)))
        self.assertTrue(notice['result_ref'])
        text = json.loads(notice['body'])['text']
        self.assertIn('not resumed after 1 AI wakes', text)
        self.assertIn('integration operator', text)
        self.assertIn('Next action:', text)
        self.assertNotIn(question['decision'], text, 'a cancelled ACK must not be republished as the deadline fallback')
        self.assertEqual(len([c for c in self.w.bot_comments(self.tid) if text in c['comment_text']]), 1)
        self.assertEqual(self.w.cu.count('POST', path) - count, 1 + int(definite_failure))
        self.assertEqual(self.notice_rows(), final['notices'])
        self.assertEqual(final['wait']['state'], 'superseded')
        self.assertFalse(final['wait'].get('root'))
        for field in ('id', 'approval_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(final['incident'][field], before[field], field)
        self.assertEqual(final['carriers'], 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)

    def test_cancelled_before_first_send_roundtrip_reaches_original_deadline_technical_notice(self):
        self.deadline_after_roundtrip(False)

    def test_definite_503_roundtrip_reaches_original_deadline_technical_notice(self):
        self.deadline_after_roundtrip(True)

    def test_cancelled_technical_notice_without_question_republishes_after_same_fingerprint_roundtrip(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        self.b.worker_comment(card, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (card, run))
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        self.assertIsNone(self.diagnostic_wait())
        parent = self.b._new('Temporary dependency before technical retry', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        old = self.notice_rows()[0]
        self.assertEqual((old['state'], old['attempts']), ('cancelled_stale', 1))
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(18)
        self.w.step(lambda: self.snapshot()['incident']['state'] == 'escalated')
        final = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.evidence = dict(final=final, unchanged=self.snapshot())
        self.assertEqual(final['incident']['reason_sha256'], before['reason_sha256'])
        self.assertEqual(len(final['notices']), 2)
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][1]['state'], final['notices'][1]['attempts']), ('done', 2))
        self.assertEqual(self.notice_rows(), final['notices'])
        self.assertEqual(self.w.cu.count('POST', path) - count, 2)
        self.assertIsNone(final['wait'])
        self.assertEqual(final['incident']['attempts'], before['attempts'])
        self.assertEqual(final['incident']['abs_ms'], before['abs_ms'])
        self.assertEqual(final['carriers'], 1)

    def replacement_queued(self):
        values = self.cancelled_roundtrip()
        card, run = values[:2]
        question = self.question_ack(card, run, decision='After restoring current context, choose approved existing option A or B.')
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(lambda: len(self.notice_rows()) == 2)
        self.assertEqual(self.notice_rows()[-1]['state'], 'pending')
        return values, question

    def test_replacement_ambiguous_applied_reconciles_once_and_verified_wait_remains_quiet(self):
        (card, run, before, old, _, path, count), question = self.replacement_queued()
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=True)
        self.w.step(lambda: self.notice_rows()[-1]['state'] == 'ambiguous')
        pending = self.snapshot()
        parent = self.b._new('Dependency after accepted replacement POST', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        self.b.cards[card]['parents'].remove(parent)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        delivered = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(10)
        final = self.snapshot()
        self.evidence = dict(pending=pending, delivered=delivered, final=final)
        self.assertEqual(len(final['notices']), 2)
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][1]['state'], final['notices'][1]['attempts']), ('done', 1))
        self.assertEqual(final['wait'], delivered['wait'])
        self.assertEqual(final['wait']['question'], question)
        self.assertEqual(final['wait']['root'], final['notices'][1]['result_ref'])
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual(final['carriers'], 1)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_replacement_uncertain_unapplied_is_not_replaced_after_roundtrip_or_deadline(self):
        (card, _, before, old, _, path, count), _ = self.replacement_queued()
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.w.step(lambda: self.notice_rows()[-1]['state'] == 'ambiguous')
        wait = self.diagnostic_wait()
        parent = self.b._new('Dependency while replacement outcome is unknown', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(35)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        final = self.snapshot()
        self.evidence = dict(final=final)
        self.assertEqual(len(final['notices']), 2)
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][1]['state'], final['notices'][1]['attempts']), ('held_ambiguous', 1))
        self.assertEqual(final['wait'], wait)
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertTrue(any('still ambiguous' in line for line in self.w.outputs))

    def test_replacement_explicit_abandonment_survives_roundtrip_and_deadline(self):
        (card, run, before, old, _, path, count), _ = self.replacement_queued()
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.w.step(lambda: self.notice_rows()[-1]['state'] == 'held_ambiguous')
        key = self.notice_rows()[-1]['idem_key']
        self.assertEqual(self.w.main('outbox', 'abandon', key)[0], 0)
        abandoned = self.snapshot()
        parent = self.b._new('Dependency after explicit abandon', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(3)
        self.b.cards[card]['parents'].remove(parent)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(10)
        final = self.snapshot()
        self.evidence = dict(abandoned=abandoned, final=final)
        self.assertEqual(final['notices'], abandoned['notices'])
        self.assertEqual(final['notices'][0], old)
        self.assertEqual(final['notices'][-1]['outcome'], 'ABANDONED')
        self.assertEqual(final['wait'], abandoned['wait'])
        self.assertEqual(final['incident'], abandoned['incident'])
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE operator_abandoned']))

    def test_repeated_same_fingerprint_turnover_exhausts_original_six_send_budget_without_rearming_failure(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.question_ack(card, run)
        cancelled = []
        for spent in range(1, 6):
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
            self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[-1]['state'] == 'retry_wait'
                        and self.notice_rows()[-1]['attempts'] == spent)
            self.assertEqual(self.w.cu.count('POST', path) - count, spent)
            parent = self.b._new('Temporary dependency turnover %d' % spent, status='blocked')
            self.b.cards[card]['parents'].append(parent)
            self.w.step(lambda: self.notice_rows()[-1]['state'] == 'cancelled_stale')
            old = self.notice_rows()[-1]
            cancelled.append(old)
            self.b.cards[card]['parents'].remove(parent)
            self.scan_minutes(2)
            self.assertEqual(self.snapshot()['incident']['reason_sha256'], before['reason_sha256'])
            self.question_ack(card, run, decision='Current restored context, correction %d: choose approved option A or B.' % spent)
            with mock.patch.object(main.Glue, 'clickup', return_value=False):
                self.w.step(lambda: len(self.notice_rows()) == spent + 1)
            successor = self.notice_rows()[-1]
            self.assertEqual(successor['attempts'], spent)
            self.assertEqual(successor['next_attempt_ms'], old['next_attempt_ms'])
            self.assertEqual(successor['first_send_ms'], cancelled[0]['first_send_ms'])
            self.assertEqual(self.notice_rows()[:-1], cancelled)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.step(lambda: self.notice_rows()[-1]['state'] == 'failed')
        exhausted = self.snapshot()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.question_ack(card, run, decision='A later ACK must not reset the exhausted transport budget.')
        self.scan_minutes(10)
        final = self.snapshot()
        self.evidence = dict(exhausted=exhausted, final=final)
        self.assertEqual(len(final['notices']), 6)
        self.assertEqual(final['notices'][:-1], cancelled)
        self.assertEqual(final['notices'], exhausted['notices'])
        self.assertEqual((final['notices'][-1]['state'], final['notices'][-1]['attempts']), ('failed', 6))
        self.assertEqual(self.w.cu.count('POST', path) - count, 6)
        self.assertTrue(any('failed after 6 attempts' in line for line in self.w.outputs))
        self.assertEqual(final['incident']['state'], 'open')
        self.assertEqual(final['incident']['abs_ms'], before['abs_ms'])
        self.assertGreaterEqual(final['incident']['attempts'], before['attempts'])
        self.assertLessEqual(final['incident']['attempts'], 3)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        histories = [json.loads(r['value']) for r in
                     self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait_history:%'")]
        self.assertEqual({h['key'] for h in histories}, {r['idem_key'] for r in cancelled})
