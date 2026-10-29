"""RR3: real scan/progress/evaluate/execute and measured fake-transport POSTs."""
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import Outcome
from test_recovery import store_conn
import test_stalled_question_cancellation as cancellation


class TransportContextTest(unittest.TestCase):
    setUp = cancellation.QuestionCancellationTest.setUp
    carriers = cancellation.QuestionCancellationTest.carriers
    scan_minutes = cancellation.QuestionCancellationTest.scan_minutes
    notice_rows = cancellation.QuestionCancellationTest.notice_rows
    unknown_stop = cancellation.QuestionCancellationTest.unknown_stop
    question_ack = cancellation.QuestionCancellationTest.question_ack
    diagnostic_wait = cancellation.QuestionCancellationTest.diagnostic_wait
    answered_status_input = cancellation.QuestionCancellationTest.answered_status_input
    snapshot = cancellation.QuestionCancellationTest.snapshot

    # Retain the reviewer's two exact production-caller sequences and measured POST oracle.
    test_actual_503_then_changed_parent_accepts_current_correction = (
        cancellation.QuestionCancellationTest.test_actual_503_then_changed_parent_accepts_current_correction)
    test_actual_503_then_real_answer_only_input_accepts_current_correction = (
        cancellation.QuestionCancellationTest.test_actual_503_then_real_answer_only_input_accepts_current_correction)

    def finish_current_question(self, card, run, before, old, evidence):
        path = '/v2/task/%s/comment' % self.tid
        post_count_before = self.w.cu.count('POST', path)
        cancellation.QuestionCancellationTest.finish_current_question(self, card, run, before, old, evidence)
        final = self.snapshot()
        measured_delta = self.w.cu.count('POST', path) - post_count_before
        self.evidence['measured_replacement_POST_count'] = measured_delta
        self.evidence['original_definite_failure_POST_count_previously_asserted'] = old['attempts']
        self.evidence['total_measured_notice_POSTs'] = old['attempts'] + measured_delta
        self.assertEqual(measured_delta, 1, 'Exactly one actual replacement POST must reach the fake transport')
        self.assertEqual(final['notices'][-1]['attempts'], old['attempts'] + 1,
                         'Same incident across new fingerprint refunded an actual definite-503 transport attempt')
        self.assertEqual(final['notices'][-1]['first_send_ms'], old['first_send_ms'],
                         'Same incident across new fingerprint restarted the first-send deadline')

    def test_context_turnover_and_old_fingerprint_return_exhaust_original_six_send_budget(self):
        card, run = self.unknown_stop()
        before = self.snapshot()['incident']
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.question_ack(card, run)
        parent = self.b._new('Dependency whose completion changes current context', status='blocked')
        cancelled, fingerprints, backoff_checks = [], [before['reason_sha256']], 0
        for spent in range(1, 6):
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
            self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[-1]['state'] == 'retry_wait'
                        and self.notice_rows()[-1]['attempts'] == spent)
            self.assertEqual(self.w.cu.count('POST', path) - count, spent)
            self.b.cards[parent]['task']['status'] = 'done'
            if spent % 2:
                self.b.cards[card]['parents'].append(parent)  # newly observed completed parent: F -> G
            else:
                self.b.cards[card]['parents'].remove(parent)  # G -> original F
            self.w.step(lambda: self.notice_rows()[-1]['state'] == 'cancelled_stale'
                        and self.snapshot()['incident']['reason_sha256'] != fingerprints[-1])
            old = self.notice_rows()[-1]
            cancelled.append(old)
            current = self.snapshot()['incident']
            fingerprints.append(current['reason_sha256'])
            self.assertNotEqual(fingerprints[-1], fingerprints[-2])
            self.assertEqual(current['reason_sha256'] == before['reason_sha256'], spent % 2 == 0)
            self.question_ack(card, run, decision='Current context correction %d: choose approved option A or B.' % spent)
            with mock.patch.object(main.Glue, 'clickup', return_value=False):
                self.w.step(lambda: len(self.notice_rows()) == spent + 1)
            successor = self.notice_rows()[-1]
            self.assertEqual(successor['attempts'], spent)
            self.assertEqual(successor['next_attempt_ms'], old['next_attempt_ms'])
            self.assertEqual(successor['first_send_ms'], cancelled[0]['first_send_ms'])
            self.assertEqual(self.notice_rows()[:-1], cancelled)
            # An unchanged real caller pass before the inherited backoff cannot perform a POST.
            if self.w.clock() < old['next_attempt_ms']:
                backoff_checks += 1
                self.w.run()
                self.assertEqual(self.w.cu.count('POST', path) - count, spent)
        self.assertGreater(backoff_checks, 0, 'exercise a still-future inherited backoff, not only copied timestamps')
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.step(lambda: self.notice_rows()[-1]['state'] == 'failed')
        exhausted = self.snapshot()
        self.b.cards[parent]['task']['status'] = 'blocked'
        self.scan_minutes(3)
        self.b.cards[card]['parents'].remove(parent)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.question_ack(card, run, decision='A later current ACK cannot grant another transport allowance.')
        self.scan_minutes(10)
        final = self.snapshot()
        self.evidence = dict(exhausted=exhausted, final=final, fingerprints=fingerprints,
                             still_future_backoff_checks=backoff_checks)
        self.assertEqual(len(final['notices']), 6)
        self.assertEqual(final['notices'][:-1], cancelled)
        self.assertEqual(final['notices'], exhausted['notices'])
        self.assertEqual((final['notices'][-1]['state'], final['notices'][-1]['attempts']), ('failed', 6))
        self.assertEqual(self.w.cu.count('POST', path) - count, 6)
        self.assertTrue(any('failed after 6 attempts' in line for line in self.w.outputs))
        for field in ('id', 'approval_id', 'abs_ms', 'detected_ms'):
            self.assertEqual(final['incident'][field], before[field], field)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)

    def test_mirror_before_outer_crash_carries_real_attempt_to_changed_fingerprint(self):
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
                raise AfterMirrorCommit('after real transport mirror persistence, before outer copy')
            return real_update(glue, key, **fields)
        with mock.patch.object(main.Glue, 'update', update):
            with self.assertRaises(AfterMirrorCommit):
                self.scan_minutes(6)
        [outer] = self.notice_rows()
        [mirror] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', outer['idem_key'] + '#cu')
        self.assertEqual((outer['attempts'], mirror['attempts']), (0, 1))
        self.assertEqual(self.w.cu.count('POST', path) - count, 1)
        parent = self.b._new('Dependency changed after mirror commit', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        old = self.notice_rows()[0]
        self.assertEqual(old['state'], 'cancelled_stale')
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(18)
        self.assertNotEqual(self.snapshot()['incident']['reason_sha256'], before['reason_sha256'])
        self.question_ack(card, run, decision='Current changed context after crash: choose approved option A or B.')
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(lambda: len(self.notice_rows()) == 2)
        pending = self.snapshot()
        self.evidence = dict(old_outer=old, old_mirror=mirror, pending=pending)
        successor = pending['notices'][-1]
        self.assertEqual(successor['attempts'], 1)
        self.assertEqual(successor['next_attempt_ms'], mirror['next_attempt_ms'])
        self.assertEqual(successor['first_send_ms'], mirror['first_send_ms'])
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        final = self.snapshot()
        self.evidence['final'] = final
        self.assertEqual(final['notices'][0], old)
        self.assertEqual((final['notices'][-1]['state'], final['notices'][-1]['attempts']), ('done', 2))
        self.assertEqual(self.w.cu.count('POST', path) - count, 2)
        self.assertEqual(final['incident']['abs_ms'], before['abs_ms'])

    def test_verified_answered_question_is_not_counted_twice_in_a_new_publication(self):
        cancellation.QuestionCancellationTest.test_answered_question_reenters_bounded_wake_and_original_deadline_escalation(self)
        notices = self.snapshot()['notices']
        self.assertEqual(len(notices), 2)
        self.assertEqual([(n['state'], n['attempts']) for n in notices], [('done', 1), ('done', 1)])
        self.assertGreater(notices[-1]['first_send_ms'], notices[0]['first_send_ms'])

    def test_foreign_task_card_and_run_cannot_supply_same_stop_transport_accounting(self):
        card, run = self.unknown_stop()
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.question_ack(card, run)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        parent = self.b._new('Changed dependency for accounting isolation', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        [old] = self.notice_rows()
        self.assertEqual((old['state'], old['attempts']), ('cancelled_stale', 1))
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(18)
        # These synthetic durable histories are deliberately newer and have larger budgets.
        # One even has the identical key prefix but belongs to another originating task.
        foreign = []
        for task, key in [('foreign-task', old['idem_key'] + ':publication:999'),
                          (self.tid, old['idem_key'].replace(card, card.replace('_', 'X'))),
                          (self.tid, old['idem_key'].replace(':%d:ctx:' % run, ':%d0:ctx:' % run))]:
            row = dict(old, task=task, idem_key=key, attempts=5,
                       first_send_ms=old['first_send_ms'] - 100_000,
                       next_attempt_ms=self.w.clock() + 90 * 60_000)
            body = json.loads(row['body'])
            body['recovery_notice'] = 999
            row['body'] = json.dumps(body)
            foreign.append(row)
        conn = store_conn(self.w)
        for row in foreign:
            conn.execute('INSERT INTO outbox(%s) VALUES (%s)' % (','.join(row), ','.join('?' for _ in row)),
                         list(row.values()))
        conn.close()
        self.question_ack(card, run, decision='Current isolated context: choose approved option A or B.')
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(lambda: len(self.notice_rows()) == 5)
        successor = self.notice_rows()[-1]
        self.assertEqual(successor['attempts'], 1)
        self.assertEqual(successor['first_send_ms'], old['first_send_ms'])
        self.assertEqual(successor['next_attempt_ms'], old['next_attempt_ms'])
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        final = self.snapshot()
        self.evidence = dict(foreign=foreign, final=final)
        self.assertEqual((final['notices'][-1]['state'], final['notices'][-1]['attempts']), ('done', 2))
        self.assertEqual(self.w.cu.count('POST', path) - count, 2)
        self.assertEqual(final['notices'][0], old)
        for row in foreign:
            [actual] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', row['idem_key'])
            self.assertEqual(actual, row)
