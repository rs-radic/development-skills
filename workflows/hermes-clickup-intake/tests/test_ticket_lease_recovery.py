"""Offline regression of lease-expired promotion continuation, through the real scan/outbox executor."""
import copy

from pathlib import Path
import unittest
from unittest import mock

from clickup_intake import main as glue_main
from clickup_intake import kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome, TransportResult
from clickup_intake.store import Store
import test_s4_composition as comp
from test_recovery import developing_world
from test_recovery import worker_at

EXPIRED = "ClickUp approval ticket INVALID: lease_expired"
# Sanitized native block-event shape; reason classifier and timestamp are retained.
NATIVE_STOP_EVENT = {
    'kind': 'blocked',
    'payload': {
        'reason': (EXPIRED + ' (ticket-check gate t_ab000039 task 86pub00002 rev 3, exit 3 at '
                   '2026-10-22T23:22:51Z). dev not written, ZIP not delivered; verified ZIP and build '
                   'gates retained for resume (see fixture continuation note).'),
        'kind': 'needs_input', 'recurrences': 1, 'source_status': 'ready',
    },
    'created_at': 1792711416,
    'run_id': 1183,
}
DIAGNOSTIC_EXPIRED = NATIVE_STOP_EVENT['payload']['reason']


class TicketLeaseRecoveryTest(unittest.TestCase):
    def setUp(self):
        staging = str(Path(__file__).resolve().parents[1] / 'staging' / 'lease-tests')
        patch = mock.patch.object(comp, 'TMP_ROOT', staging)
        patch.start()
        self.addCleanup(patch.stop)

    def stopped(self, reason=EXPIRED):
        w, life = developing_world(self)
        life.e2e_done_to_ticketed()
        w.clock.advance(1801)
        self.assertEqual(life.ticket_check(), (3, ['INVALID lease_expired']))
        run = w.board.claim(life.promo, 'integrator')
        w.board.block(life.promo, 'needs_input', reason)
        return w, life, run

    def retry_rows(self, w):
        return w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:ticket-lease:%'")

    def pending(self):
        w, life, run = self.stopped()
        with mock.patch.object(glue_main.Glue, 'execute', return_value=None):
            w.clock.advance(61)
            w.run()
        [row] = self.retry_rows(w)
        self.assertEqual(row['state'], 'pending')
        return w, life, run, row

    def sql(self, w, statement, *args):
        store = Store(w.state_path, w.clock)
        try:
            with store.transaction() as c:
                c.execute(statement, args)
        finally:
            store.close()

    def at_send(self, w, mutate):
        execute = glue_main.Glue.execute
        injected = []

        def run(glue, *args, **kw):
            if not injected:
                mutate()
                injected.append(True)
            return execute(glue, *args, **kw)

        with mock.patch.object(glue_main.Glue, 'execute', run), \
                mock.patch.object(w.board, 'unblock', wraps=w.board.unblock) as unblock:
            w.run()
        self.assertTrue(injected)
        return unblock.call_args_list

    def test_expired_ticket_waits_for_observation_then_continues_same_card_once(self):
        w, life, run = self.stopped()
        history = copy.deepcopy(w.board.cards[life.promo]['runs'])
        original = w.transport.request

        def unavailable(method, path, **kw):
            if method == 'GET' and path == '/v2/task/%s/comment' % life.tid:
                return TransportResult(Outcome.DEFINITE_FAIL, 503)
            return original(method, path, **kw)

        with mock.patch.object(w.transport, 'request', unavailable):
            w.run()
            w.clock.advance(61)
            w.run()
        self.assertEqual(life.ticket_check(), (3, ['INVALID lease_expired']))
        self.assertEqual(w.board.cards[life.promo]['task']['status'], 'blocked')
        self.assertEqual(w.board.cards[life.promo]['runs'], history)
        w.step(lambda: w.board.cards[life.promo]['task']['status'] == 'ready', limit=12)
        self.assertEqual(life.ticket_check(), (0, ['VALID epoch=1']))
        self.assertEqual(w.board.cards[life.promo]['runs'], history)
        for _ in range(3):
            w.clock.advance(61)
            w.run()  # new Glue/Store every scan: restart safety
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)
        resumed = w.board.claim(life.promo, 'integrator')
        self.assertGreater(resumed, run)
        w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)


    def test_diagnostic_expiry_continues_same_card_after_renewal(self):
        w, life, _ = self.stopped(DIAGNOSTIC_EXPIRED)
        history = copy.deepcopy(w.board.cards[life.promo]['runs'])
        w.step(lambda: w.board.cards[life.promo]['task']['status'] == 'ready', limit=12)
        self.assertEqual(life.ticket_check(), (0, ['VALID epoch=1']))
        w.clock.advance(61)
        w.run()
        self.assertEqual(w.board.cards[life.promo]['runs'], history)
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)

    def test_card_changed_during_parent_read_never_attempts_unblock(self):
        w, life, _ = self.stopped()
        original_show = w.board.show
        armed = []
        original_retry = glue_main.Glue.retry_ticket_lease

        def retry(glue, *args):
            armed.append(True)
            return original_retry(glue, *args)

        def show(card):
            value = original_show(card)
            if armed and card == life.gate():
                w.board.claim(life.promo, 'integrator')
                armed.clear()
            return value

        with mock.patch.object(glue_main.Glue, 'retry_ticket_lease', retry), \
                mock.patch.object(w.board, 'show', show), \
                mock.patch.object(w.board, 'unblock', wraps=w.board.unblock) as unblock:
            w.clock.advance(61)
            w.run()
        self.assertNotIn(mock.call(life.promo), unblock.call_args_list)

    def test_lost_ack_and_crash_before_local_commit_do_not_repeat_unblock(self):
        for failure in ('ambiguous', 'process_death', 'before_done_commit'):
            with self.subTest(failure=failure):
                w, life, _, row = self.pending()
                history = copy.deepcopy(w.board.cards[life.promo]['runs'])
                original = w.board.unblock
                failed = []

                def unblock(card):
                    result = original(card)
                    if card == life.promo and not failed and failure != 'before_done_commit':
                        failed.append(True)
                        if failure == 'process_death':
                            raise comp.Crash('after native unblock')
                        if failure == 'ambiguous':
                            return KanbanResult(Outcome.AMBIGUOUS)
                    return result

                update = glue_main.Glue.update

                def before_commit(glue, key, **cols):
                    if key == row['idem_key'] and cols.get('state') == 'done' and not failed:
                        failed.append(True)
                        raise comp.Crash('before done commit')
                    return update(glue, key, **cols)

                with mock.patch.object(w.board, 'unblock', unblock), \
                        mock.patch.object(glue_main.Glue, 'update', before_commit if
                                          failure == 'before_done_commit' else update):
                    w.run()
                for _ in range(2):
                    w.clock.advance(61)
                    w.run()
                self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)
                self.assertEqual(failed, [True])
                self.assertEqual(self.retry_rows(w)[0]['state'], 'done')
                self.assertEqual(w.board.cards[life.promo]['runs'], history)

    def test_ticket_and_task_authority_are_rechecked_after_queueing(self):
        cases = {
            'revoked': ("UPDATE approvals SET state='revoked', revoked_reason='operator'", ()),
            'not_ticketed': ("UPDATE approvals SET state='implemented'", ()),
            'revision': ("UPDATE approvals SET rev=rev+1", ()),
            'digest': ("UPDATE approvals SET plan_sha256=?", ('f' * 64,)),
            'epoch': ("UPDATE approvals SET ticket_epoch=ticket_epoch+1", ()),
            'zero_epoch': ("UPDATE approvals SET ticket_epoch=0", ()),
            'ticket_generation': ("UPDATE approvals SET ticket_input_gen=ticket_input_gen+1", ()),
            'input_generation': ("UPDATE tasks SET input_gen=input_gen+1", ()),
            'unclassified': ("UPDATE tasks SET classified_gen=0", ()),
            'scope_generation': ("UPDATE tasks SET scope_gen=(SELECT MAX(accounts_gen)+1 FROM approvals)", ()),
            'hold': ("UPDATE tasks SET hold_reason='operator'", ()),
            'terminal': ("UPDATE tasks SET terminal=1", ()),
            'gate': ("UPDATE approvals SET gate_card='t_12345678'", ()),
            'superseded': ("INSERT INTO approvals(task,rev,comment_id,comment_date,author_id,author_name,"
                           "plan_sha256,accounts_gen,state) SELECT task,rev+1,'new',comment_date,author_id,"
                           "author_name,plan_sha256,accounts_gen,'recorded' FROM approvals", ()),
        }
        for name, (sql, args) in cases.items():
            with self.subTest(name=name):
                w, life, _, _ = self.pending()
                calls = self.at_send(w, lambda: self.sql(w, sql, *args))
                self.assertNotIn(mock.call(life.promo), calls)
                self.assertEqual(self.retry_rows(w)[0]['state'], 'cancelled_stale')

    def test_only_exact_lease_expiration_typed_stops_can_queue_a_retry(self):
        for reason in ('ClickUp approval ticket INVALID: revoked',
                       'ClickUp approval ticket INVALID: new_input',
                       'ClickUp approval ticket INVALID: superseded',
                       'ClickUp approval ticket INVALID: gate_not_done',
                       EXPIRED + ' ', EXPIRED + '\nextra', EXPIRED + ': revoked',
                       'prefix ' + EXPIRED,
                       DIAGNOSTIC_EXPIRED.replace('lease_expired', 'revoked'),
                       DIAGNOSTIC_EXPIRED.replace('lease_expired', 'lease_expired_stale'),
                       DIAGNOSTIC_EXPIRED.replace('exit 3', 'exit 0'),
                       DIAGNOSTIC_EXPIRED.replace('(ticket-check gate', '(other gate'),
                       DIAGNOSTIC_EXPIRED + '\nrevoked'):
            with self.subTest(reason=reason):
                w, life, run = self.stopped()
                event = w.board.cards[life.promo]['events'][-1]
                event['payload']['reason'] = reason
                w.clock.advance(61)
                w.run()
                self.assertEqual(self.retry_rows(w), [])
                self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
                if reason.startswith(kp.TICKET_INVALID_PREFIX):
                    self.assertIsNone(kp.recovery_incident(w.board.show(life.promo).json))

    def test_changed_card_or_stop_cannot_inherit_a_queued_retry(self):
        def change_run(w, life):
            w.board.claim(life.promo, 'integrator')

        cases = {
            'card_identity': lambda s: s['task'].update(id='t_12345678'),
            'active_worker': lambda s: s['runs'][-1].update(worker_pid=42),
            'open_run': lambda s: s['runs'][-1].update(ended_at=None),
            'terminal_card': lambda s: s['task'].update(status='done'),
            'archived_card': lambda s: s['task'].update(status='archived'),
            'changed_event': lambda s: s['events'][-1].update(created_at=s['events'][-1]['created_at'] + 1),
            'changed_kind': lambda s: s['events'][-1]['payload'].update(kind='capability'),
            'changed_reason': lambda s: s['events'][-1]['payload'].update(reason=EXPIRED + ' '),
            'missing_claim': lambda s: s.update(events=[e for e in s['events'] if e['kind'] != 'claimed']),
            'marker': lambda s: s['task'].update(body='No ticket marker'),
            'no_gate_parent': lambda s: s.update(parents=[]),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                w, life, _, _ = self.pending()
                calls = self.at_send(w, lambda: mutate(w.board.cards[life.promo]))
                self.assertNotIn(mock.call(life.promo), calls)
        w, life, _, _ = self.pending()
        calls = self.at_send(w, lambda: change_run(w, life))
        self.assertNotIn(mock.call(life.promo), calls)

    def test_unsatisfied_native_parent_waits_then_reuses_one_retry(self):
        w, life, _, row = self.pending()
        extra = w.board.worker_create('Still required native check')
        w.board.cards[life.promo]['parents'].append(extra)
        for _ in range(2):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)
        w.board.finish(extra, 'tester')
        w.clock.advance(61)
        w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)
        self.assertEqual([r['idem_key'] for r in self.retry_rows(w)], [row['idem_key']])

    def test_lease_expiring_during_parent_read_is_not_accepted(self):
        w, life, _, _ = self.pending()
        read = w.board.show
        expired = []
        original = glue_main.Glue.retry_ticket_lease
        active = []

        def retry(glue, *args):
            active.append(True)
            return original(glue, *args)

        def show(card):
            result = read(card)
            if active and card == life.gate() and not expired:
                w.clock.advance(1801)
                expired.append(True)
            return result

        with mock.patch.object(glue_main.Glue, 'retry_ticket_lease', retry), \
                mock.patch.object(w.board, 'show', show):
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)
        self.assertEqual(life.ticket_check(), (3, ['INVALID lease_expired']))
        w.step(lambda: w.board.cards[life.promo]['task']['status'] == 'ready', limit=12)
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)

    def test_failed_parent_read_defers_without_spending_attempts(self):
        w, life, _, _ = self.pending()
        show = w.board.show

        def unavailable(card):
            return KanbanResult(Outcome.DEFINITE_FAIL) if card == life.gate() else show(card)

        with mock.patch.object(w.board, 'show', unavailable):
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)
        w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)

    def test_operator_abandon_is_not_automatically_rearmed(self):
        w, life, _, row = self.pending()
        # The existing operator command accepts only terminal failed/ambiguous rows.
        self.sql(w, "UPDATE outbox SET state='failed' WHERE idem_key=?", row['idem_key'])
        self.assertEqual(w.main('outbox', 'abandon', row['idem_key']), (0, ['abandon done']))
        for _ in range(2):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['outcome'], 'ABANDONED')

    def test_gate_status_and_identity_are_revalidated(self):
        for change in ({'status': 'todo'}, {'status': 'archived'}, {'id': 't_12345678'}):
            with self.subTest(change=change):
                w, life, _, _ = self.pending()
                calls = self.at_send(w, lambda: w.board.cards[life.gate()]['task'].update(change))
                self.assertNotIn(mock.call(life.promo), calls)

    def test_incomplete_observation_defers_a_queued_retry(self):
        w, life, _, _ = self.pending()
        calls = self.at_send(w, lambda: self.sql(w, "UPDATE tasks SET full_cursor='incomplete'"))
        self.assertNotIn(mock.call(life.promo), calls)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)

    def test_readonly_state_unavailable_defers_a_queued_retry(self):
        w, life, _, _ = self.pending()
        with mock.patch.object(glue_main, 'load_ticket_snapshot', return_value=None):
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)
        w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)

    def test_surviving_retry_child_blocks_even_reconciliation_reads(self):
        w, life, _, row = self.pending()
        self.sql(w, "UPDATE outbox SET state='sending',child_pid=123,child_start_time='same' WHERE idem_key=?",
                 row['idem_key'])
        original = glue_main.Glue.retry_ticket_lease

        def retry(glue, *args):
            with mock.patch.object(glue.port, 'show', side_effect=AssertionError('live child read')):
                return original(glue, *args)

        with mock.patch.object(kp, 'child_alive', return_value=True), \
                mock.patch.object(glue_main.Glue, 'retry_ticket_lease', retry):
            w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 0)
        self.assertEqual(self.retry_rows(w)[0]['attempts'], 0)
        w.run()
        self.assertEqual(w.board.mutations().count(('unblock', life.promo)), 1)


class TicketLeaseIdentityTest(unittest.TestCase):
    def test_exact_retained_native_event_is_recognized_without_rewriting_it(self):
        show, worker, _ = worker_at(t=NATIVE_STOP_EVENT['created_at'], card='t_ab000081')
        worker.claim()
        worker.block('needs_input', DIAGNOSTIC_EXPIRED)
        show['runs'][-1]['id'] = NATIVE_STOP_EVENT['run_id']
        show['events'][0]['run_id'] = NATIVE_STOP_EVENT['run_id']
        show['events'][0]['payload']['run_id'] = NATIVE_STOP_EVENT['run_id']
        show['events'][-1] = copy.deepcopy(NATIVE_STOP_EVENT)
        before = copy.deepcopy(show)
        stop = kp.ticket_lease_stop(show)
        self.assertIsNotNone(stop)
        self.assertEqual((stop['run_id'], stop['event_created_s'], stop['reason']),
                         (1183, 1792711416, DIAGNOSTIC_EXPIRED))
        self.assertIsNone(kp.recovery_incident(show))
        self.assertEqual(show, before)

    def test_malformed_run_identity_never_becomes_a_ticket_stop(self):
        show, worker, _ = worker_at(card='t_12345678')
        worker.claim()
        worker.block('needs_input', EXPIRED)
        for run in show['runs']:
            run['id'] = 1
        for event in show['events']:
            event['run_id'] = 1
            if event['kind'] == 'claimed':
                event['payload']['run_id'] = 1
        self.assertIsNotNone(kp.ticket_lease_stop(show))
        malformed = copy.deepcopy(show)
        malformed['events'][-1]['run_id'] = True  # bool is not native run 1
        self.assertIsNone(kp.ticket_lease_stop(malformed))


if __name__ == '__main__':
    unittest.main()
