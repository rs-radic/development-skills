"""Approved pre-worker recovery via real scan/prepass/evaluate/executor; all ports offline."""
import json
import os
import unittest
from unittest import mock

from clickup_intake import engine, main, kanban_port as kp
import test_s4_composition as composition
import test_recovery as recovery
from clickup_intake.contracts import KanbanResult, Outcome, TransportResult


class PreworkerRecoveryTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            guard = mock.patch(target, side_effect=AssertionError('offline-only test boundary'))
            guard.start()
            self.addCleanup(guard.stop)
        self.w = composition.World(self)
        self.w.board = recovery.RecoveryBoard(os.path.join(self.w.dir, 'board'), self.w.clock, self.w.inj)
        self.life = composition.Lifecycle(self.w)
        self.b, self.tid = self.w.board, self.life.tid
        from preworker_snapshot_fixture import install
        install(self.b, self.w.dir)
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.w.main('recovery', 'enable')
        self.life.say(composition.APPROVER_A, 'Approve plan r1 for development')

    def applications(self):
        return self.w.q('SELECT * FROM approvals WHERE task=? ORDER BY id', self.tid)

    def incidents(self):
        return self.w.q('SELECT * FROM recoveries WHERE task=?', self.tid)

    def carriers(self):
        return self.b.by_key('clickup-dp:%s:rcv:' % self.tid)

    def scans(self, count):
        for _ in range(count):
            self.w.clock.advance(61)
            self.w.run()

    def stall(self, row):
        return ':gate:' in row['idem_key'] or ':orch:' in row['idem_key']

    def stopped_stage(self):
        real = main.Glue.kanban
        return mock.patch.object(main.Glue, 'kanban',
                                 lambda glue, row: False if self.stall(row) else real(glue, row))

    def test_zero_native_stage_wakes_with_truthful_readonly_admission(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            self.assertEqual(a['state'], 'recorded')
            self.assertIsNone(a['gate_card'])
            self.assertIsNone(a['orchestrator_card'])
            self.assertIsNone(self.life.gate())
            self.assertIsNone(self.life.orch())
            self.scans(13)
            self.assertEqual(len(self.carriers()), 1,
                             'eligible approval with no stage progress must reach the existing wake path')
            [inc] = self.incidents()
            self.assertEqual((inc['class'], inc['event_kind'], inc['run_id']),
                             ('approval_staging', 'approval_staging', 0))
            self.assertEqual(inc['approval_id'], a['id'])
            self.assertEqual(inc['event_created_s'], a['comment_date'] // 1000)
            self.assertEqual(inc['card'], 'approval-%d' % a['id'])
            before = list(self.b.mutations())
            rc, lines = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
            self.assertEqual(rc, 0, lines)
            self.assertIn('class=approval_staging', lines[0])
            self.assertEqual(self.b.mutations(), before)
            self.assertFalse(any(card == inc['card'] for _, card in self.b.calls))
            carrier = self.b.cards[self.carriers()[0]]
            self.assertEqual(carrier['task']['status'], 'done')
            self.assertIn('No native card or run is implied', carrier['task']['body'])
            self.assertIn('normal intake poll', carrier['task']['body'])
            self.assertFalse(any(c['runs'] for c in self.b.cards.values()
                                 if c['task']['id'] != self.life.req_cards()[0]))
        # Removing the injected stalled writer lets unchanged normal lifecycle continue.
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:gate:' % self.tid)), 1)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        self.assertEqual(self.incidents()[0]['state'], 'open', 'READY without owner action is not success')
        self.life.orchestrate()
        self.assertEqual(self.incidents()[0]['state'], 'resumed')
        self.assertTrue(self.b.cards[self.life.orch()]['runs'])

    def test_unavailable_plan_attachment_observation_cannot_become_a_stall(self):
        source = self.life.req_cards()[0]
        real = self.b.attachments
        with self.stopped_stage(), mock.patch.object(self.b, 'attachments', side_effect=lambda card:
                KanbanResult(Outcome.DEFINITE_FAIL) if card == source else real(card)):
            self.scans(20)
            self.assertEqual(self.applications()[0]['state'], 'recorded')
            self.assertEqual(self.carriers(), [], 'a failed read is not evidence of a stalled approval')
        with self.stopped_stage():
            self.scans(18)
            self.assertEqual(len(self.carriers()), 1)

    def wait(self):
        rows = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait:%'")
        return json.loads(rows[0]['value']) if rows else None

    def question(self, inc, decision='Which existing approved account should receive the notification, A or B?'):
        q = dict(task=self.tid, card=inc['card'], run=0, approval=inc['approval_id'],
                 revision=inc['approval_rev'], sha256=inc['plan_sha256'], fingerprint=inc['reason_sha256'],
                 failed='The already approved account choice is ambiguous.', attempted='Read the existing approved plan.',
                 decision=decision, next_action='Reply here with the approved account; do not post credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[-1], 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=0 disposition=owner\n'
                              'CLICKUP-RECOVERY-QUESTION v1 %s' % (inc['card'], json.dumps(q)))
        return q

    def test_no_native_human_question_readback_answer_and_normal_stage_continuation(self):
        with self.stopped_stage():
            self.scans(18)
            [inc] = self.incidents()
            question = self.question(inc)
            self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        wait = self.wait()
        [notice] = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(wait['question'], question)
        self.assertEqual(wait['root'], notice['result_ref'])
        self.assertTrue(wait['verified_ms'])
        self.assertTrue(any(c['id'] == wait['root'] and question['decision'] in c['comment_text']
                            for c in self.w.bot_comments(self.tid)))
        self.scans(4)
        self.assertIsNone(self.life.gate(), 'a verified human wait pauses the ordinary stage path')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
        self.life.say(composition.APPROVER_A, 'Use existing approved account A.', mention=composition.BOT,
                      reply_to=wait['root'])
        def answer(card, ref, gen):
            self.b.park(card, composition.classify(ref, gen, 'answer_only'),
                        composition.out('resolution', ref, 'Use existing approved account A.',
                                        gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        self.assertEqual(self.w.q('SELECT * FROM blockers'), [])
        self.assertEqual(self.incidents()[0]['abs_ms'], inc['abs_ms'])
        self.assertEqual(self.incidents()[0]['attempts'], inc['attempts'])

    def change(self, sql, *args):
        conn = recovery.store_conn(self.w)
        try:
            conn.execute(sql, args)
        finally:
            conn.close()

    def test_stale_approval_cannot_continue_an_already_queued_stage_effect(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            self.assertEqual(a['state'], 'recorded')
        self.change('UPDATE tasks SET scope_gen=? WHERE clickup_task_id=?', a['accounts_gen'] + 1, self.tid)
        self.assertGreater(self.w.task(self.tid)['scope_gen'], a['accounts_gen'])
        self.scans(5)
        self.assertIsNone(self.life.gate(), 'a stale scope must not authorize even the initial queued create')
        self.assertEqual(self.carriers(), [])

    def test_gate_created_unbound_continues_without_duplicate_or_fabricated_run(self):
        with mock.patch.object(self, 'stall', side_effect=lambda row: ':orch:' in row['idem_key']):
            with self.stopped_stage():
                self.scans(24)
                [a] = self.applications()
                gate = self.life.gate()
                self.assertIsNotNone(gate)
                self.assertIsNone(a['gate_card'])
                self.assertIsNone(a['orchestrator_card'])
                self.assertEqual(self.w.q('SELECT * FROM cards WHERE card_id=?', gate), [])
                self.assertEqual(len(self.carriers()), 1)
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.life.gate(), gate)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:gate:' % self.tid)), 1)
        self.assertEqual(len(self.w.q('SELECT * FROM cards WHERE approval_id=?', a['id'])), 2)
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])

    def test_orchestrator_created_unbound_and_partial_attachment_registration_resume(self):
        self.w.step(lambda: self.life.orch() is not None)
        [a] = self.applications()
        self.assertIsNone(a['orchestrator_card'])
        orch = self.life.orch()
        with mock.patch.object(self, 'stall', side_effect=lambda row: row['idem_key'].endswith(':attach:approval')):
            with self.stopped_stage():
                self.scans(5)
                [a] = self.applications()
                self.assertEqual(a['orchestrator_card'], orch)
                self.assertEqual(a['state'], 'recorded')
                self.assertTrue(self.b.cards[orch]['atts'])
                self.change('DELETE FROM cards WHERE card_id=?', orch)
                self.scans(18)
                self.assertEqual(len(self.carriers()), 1)
                self.assertEqual(len(self.w.q('SELECT * FROM cards WHERE card_id=?', orch)), 1)
        self.w.step(lambda: self.b.cards[orch]['task']['status'] == 'ready')
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)
        attachments = self.b.cards[orch]['atts']
        self.assertEqual(len(attachments), len({a['filename'] for a in attachments}))

    def test_healthy_stage_progress_and_ordinary_ready_queue_are_not_new_incidents(self):
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.scans(18)
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.carriers(), [])

    def test_staged_release_stall_uses_same_guarded_continuation(self):
        with mock.patch.object(self, 'stall', side_effect=lambda row: row['kind'] == 'kb_unblock'):
            with self.stopped_stage():
                self.w.step(lambda: self.applications()[0]['state'] == 'staged' if self.applications() else False)
                self.scans(20)
                self.assertEqual(len(self.carriers()), 1)
                self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'blocked')
        self.w.step(lambda: self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.b.cards[self.life.gate()]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])

    def test_holds_revocation_and_unknown_native_observation_fail_closed(self):
        with mock.patch.object(self, 'stall', side_effect=lambda row: ':orch:' in row['idem_key']):
            with self.stopped_stage():
                self.scans(6)
                gate = self.life.gate()
                self.assertIsNotNone(gate)
                self.change("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", self.tid)
                self.scans(15)
                self.assertEqual(self.carriers(), [])
                self.change('UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?', self.tid)
                real = self.b.show
                with mock.patch.object(self.b, 'show', side_effect=lambda card:
                        KanbanResult(Outcome.DEFINITE_FAIL) if card == gate else real(card)):
                    self.scans(15)
                    self.assertEqual(self.carriers(), [])
                self.change("UPDATE approvals SET state='revoked',revoked_reason='new_input' WHERE task=?", self.tid)
                self.scans(15)
                self.assertEqual(self.carriers(), [])

    def test_live_writer_and_future_retry_are_not_stalls_then_expired_writer_recovers(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            key = 'clickup-dp:%s:gate:r1:g%d' % (self.tid, a['accounts_gen'])
            self.change("UPDATE outbox SET state='sending',child_pid=4321,child_start_time='123' WHERE idem_key=?", key)
            with mock.patch.object(kp, 'child_alive', return_value=True):
                self.scans(18)
            self.assertEqual(self.carriers(), [])
            self.change("UPDATE outbox SET state='retry_wait',child_pid=NULL,next_attempt_ms=? WHERE idem_key=?",
                        self.w.clock() + 60 * 60_000, key)
            self.scans(18)
            self.assertEqual(self.carriers(), [])
            self.change("UPDATE outbox SET state='pending',next_attempt_ms=NULL WHERE idem_key=?", key)
            self.scans(18)
            self.assertEqual(len(self.carriers()), 1)

    def test_changed_context_retry_and_restart_preserve_one_logical_budget(self):
        with self.stopped_stage():
            self.scans(18)
            [first] = self.incidents()
            self.assertEqual(first['attempts'], 1)
            # Every World.run creates a fresh Store/Glue, exercising persisted restart behavior.
            self.refresh_context()
            self.w.clock.now_ms = first['detected_ms'] + 61 * 60_000
            self.scans(4)
            self.assertEqual(self.incidents()[0]['attempts'], 2)
            self.w.clock.now_ms = first['detected_ms'] + 181 * 60_000
            self.scans(6)
            self.assertEqual(self.incidents()[0]['attempts'], 3)
            self.b.worker_comment(self.carriers()[-1], 'default',
                                  'CLICKUP-RECOVERY-ACK v1 card=%s run=0 disposition=cannot_act' % first['card'])
            self.w.clock.now_ms = first['abs_ms'] + 1
            self.scans(4)
            [last] = self.incidents()
            self.assertEqual(len(self.carriers()), 3)
            for key in ('id', 'approval_id', 'event_created_s', 'reason_sha256', 'abs_ms', 'detected_ms'):
                self.assertEqual(last[key], first[key], key)
            self.assertEqual(last['state'], 'open')
            self.assertEqual(self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'"), [])

    def test_approval_older_than_recovery_cutover_is_observed_without_history_rewrite(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            self.w.main('recovery', 'disable')
            self.w.clock.advance(5)
            self.w.main('recovery', 'enable')
            [cut] = self.w.q("SELECT value FROM meta WHERE key='recovery_cutover_ms'")
            self.assertLess(a['comment_date'], json.loads(cut['value']))
            self.scans(18)
            [inc] = self.incidents()
            self.assertEqual(inc['event_created_s'], a['comment_date'] // 1000)
            self.assertEqual(len(self.carriers()), 1)

    def test_question_replacement_preserves_spent_delivery_budget_and_exact_binding(self):
        with self.stopped_stage():
            self.scans(18)
            [inc] = self.incidents()
            self.w.cu.inject('POST', '/v2/task/%s/comment' % self.tid, Outcome.DEFINITE_FAIL, status=503)
            self.question(inc)
            rows = lambda: self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
            self.w.step(lambda: rows() and rows()[0]['state'] == 'retry_wait')
            [old] = rows()
            self.assertEqual(old['attempts'], 1)
            self.refresh_context()
            self.scans(5)
            [cancelled] = rows()
            self.assertEqual(cancelled['state'], 'cancelled_stale')
            replacement = self.question(inc, 'After the clarified context, choose approved account A or B?')
            with mock.patch.object(main.Glue, 'clickup', return_value=False):
                self.w.step(lambda: len(rows()) == 2)
            new = rows()[1]
            for name in ('attempts', 'first_send_ms', 'next_attempt_ms'):
                self.assertEqual(new[name], old[name], name)
            self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting')
            self.assertEqual(self.wait()['question'], replacement)
            self.assertEqual(rows()[0], cancelled)
            self.assertEqual(rows()[1]['attempts'], 2)
            self.assertEqual(self.incidents()[0]['abs_ms'], inc['abs_ms'])

    def test_newer_approval_blocks_old_stage_and_old_recovery_admission(self):
        with self.stopped_stage():
            self.scans(18)
            [old] = self.applications()
            [inc] = self.incidents()
            self.change("INSERT INTO approvals(task,rev,accounts_gen,comment_id,author_id,author_name,comment_date,"
                        "text_hash,plan_sha256,state) SELECT task,rev+1,accounts_gen,comment_id||'-new',author_id,"
                        "author_name,comment_date+1,text_hash,plan_sha256,'released' FROM approvals WHERE id=?", old['id'])
            self.scans(5)
            self.assertEqual(self.incidents()[0]['state'], 'superseded')
        self.scans(5)
        self.assertEqual(self.b.by_key('clickup-dp:%s:gate:r1:' % self.tid), [])
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)

    def refresh_context(self):
        old = self.w.task(self.tid)['input_gen']
        self.life.say(composition.APPROVER_A, 'Clarification within existing approved scope.', mention=composition.BOT)
        def classify(card, ref, gen):
            self.b.worker_comment(card, 'planner', composition.classify(ref, gen, 'answer_only'))
            self.b.finish(card, 'planner')
        self.w.step(lambda: self.w.task(self.tid)['input_gen'] > old and
                    self.w.task(self.tid)['input_gen'] == self.w.task(self.tid)['classified_gen'],
                    world=self.life.planner(classify))

    def test_changed_published_plan_refuses_old_approval_identity(self):
        with self.stopped_stage():
            self.scans(18)
            [inc] = self.incidents()
            self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', 'f' * 64, self.tid)
            rc, lines = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
            self.assertEqual(rc, 3, lines)
            self.assertIsNone(self.life.gate())
            self.assertEqual(self.incidents()[0]['approval_id'], inc['approval_id'])

    def test_intentionally_disabled_handoff_is_quiet_not_a_stall(self):
        self.w.write_config(handoff_unblock=False)
        self.w.step(lambda: self.applications() and self.applications()[0]['state'] == 'staged')
        self.scans(20)
        self.assertEqual(self.incidents(), [])
        self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'blocked')

    def test_incident_and_first_wake_are_atomic_across_interrupted_scan(self):
        with self.stopped_stage():
            self.scans(5)
            with mock.patch.object(engine, '_attempt', side_effect=RuntimeError('offline injected interruption')):
                self.scans(13)
            self.assertEqual(self.incidents(), [], 'interrupted admission must roll back its unsent incident')
            self.scans(4)
            self.assertEqual(len(self.carriers()), 1)
            self.assertEqual(self.incidents()[0]['attempts'], 1)

    def exhaust(self):
        self.scans(18)
        [inc] = self.incidents()
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60_000
        self.scans(4)
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60_000
        self.scans(6)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(4)
        self.assertEqual(self.incidents()[0]['attempts'], 3)
        return self.incidents()[0]

    def test_exhausted_stage_requires_owner_work_not_ready_ack_or_bare_done(self):
        with self.stopped_stage():
            inc = self.exhaust()
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.b.worker_comment(self.carriers()[-1], 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=0 disposition=cannot_act' % inc['card'])
        self.scans(4)
        self.assertEqual(self.incidents()[0]['state'], 'open', 'READY and generic cannot_act are not recovery')
        self.b.finish(self.life.orch(), 'orchestrator')  # completion without the authorized child work
        self.scans(5)
        self.assertEqual(self.incidents()[0]['state'], 'open', 'bare DONE is not verified capable-owner work')
        self.life.orchestrate()
        last = self.incidents()[0]
        self.assertEqual(last['state'], 'resumed')
        self.assertTrue(self.b.cards[self.life.promo]['parents'])
        for name in ('id', 'attempts', 'abs_ms', 'detected_ms', 'event_created_s', 'reason_sha256'):
            self.assertEqual(last[name], inc[name], name)

    def historical_card(self):
        old = self.b._new('Unrelated historical gate')
        self.change("INSERT INTO cards(card_id,task,role,created_key) VALUES (?,?,'gate','historical')",
                    old, 'unrelated')
        return old

    def test_scan_continues_exhausted_stage_and_monitors_work_before_global_reads(self):
        with self.stopped_stage():
            inc = self.exhaust()
        old = self.historical_card()
        real = self.b.show

        class StopCollection(BaseException):
            pass

        def show(card):
            if card == old:
                raise StopCollection()
            return real(card)

        with mock.patch.object(self.b, 'show', side_effect=show):
            for _ in range(12):
                self.w.clock.advance(301)
                with self.assertRaises(StopCollection):
                    self.w.run()
                if self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready':
                    break
            self.assertIsNotNone(self.life.orch())
            self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'ready',
                             'supported staging must finish without waiting for the unrelated collector')
            self.assertEqual(self.incidents()[0]['state'], 'open')
            orch, gate = self.life.orch(), self.life.gate()
            self.b.claim(orch, 'orchestrator')
            fe = self.b.worker_create('actual final E2E')
            promo = self.b.worker_create('actual promotion', parents=[gate], body=
                main.render.promotion_block(gate, self.tid, 1, self.life.plan_sha))
            cleanup = self.b.worker_create('actual cleanup', parents=[promo])
            self.b.finish(orch, 'orchestrator', {'clickup_gates': {'plan_revision': 1,
                'plan_sha256': self.life.plan_sha, 'final_e2e': fe, 'promotion': promo,
                'cleanup': cleanup, 'lineage': [fe, promo, cleanup]}})
            self.w.clock.advance(301)
            with self.assertRaises(StopCollection):
                self.w.run()
        last = self.incidents()[0]
        self.assertEqual(last['state'], 'resumed', 'continued work must be monitored before the global collector')
        for name in ('id', 'attempts', 'abs_ms', 'detected_ms', 'event_created_s', 'reason_sha256'):
            self.assertEqual(last[name], inc[name], name)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:gate:' % self.tid)), 1)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)
        self.assertEqual(len(self.carriers()), 3, 'supported continuation must not refund spent wakes')
        self.scans(5)
        self.assertEqual(self.incidents()[0]['state'], 'resumed')

    def test_exhausted_bound_question_delivers_before_unrelated_collection(self):
        with self.stopped_stage():
            inc = self.exhaust()
            question = self.question(inc)
            old = self.historical_card()
            real = self.b.show

            class StopCollection(BaseException):
                pass

            def show(card):
                if card == old:
                    raise StopCollection()
                return real(card)

            with mock.patch.object(self.b, 'show', side_effect=show):
                for _ in range(3):
                    self.w.clock.advance(301)
                    with self.assertRaises(StopCollection):
                        self.w.run()
            wait = self.wait()
            self.assertIsNotNone(wait)
            self.assertEqual(wait['state'], 'waiting', 'a genuine decision must reach its originating task')
            self.assertEqual(wait['question'], question)
            self.assertTrue(wait['root'])
            self.assertTrue(wait['verified_ms'])
            self.assertTrue(any(c['id'] == wait['root'] and question['decision'] in c['comment_text']
                                for c in self.w.bot_comments(self.tid)))
            self.assertEqual(self.incidents()[0]['attempts'], inc['attempts'])
            self.assertEqual(self.incidents()[0]['abs_ms'], inc['abs_ms'])

    def test_scan_delivers_before_unrelated_show_blocks(self):
        import threading
        with self.stopped_stage():
            self.scans(5)
        old = self.historical_card()
        real = self.b.show
        entered, release = threading.Event(), threading.Event()
        errors = []

        def show(card):
            if card == old:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test failed to release its bounded collector')
            return real(card)

        def run():
            try:
                self.w.run()
            except BaseException as e:
                errors.append(e)

        self.w.clock.advance(301)
        with mock.patch.object(self.b, 'show', side_effect=show):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), 'the actual global collector must be blocked')
                self.assertIsNotNone(self.life.gate(), 'the guarded create must be delivered before unrelated show')
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])

    def test_scan_priority_path_refuses_unconfirmed_stale_revoked_held_and_expired_inputs(self):
        with self.stopped_stage():
            self.scans(5)
        [a] = self.applications()
        old = self.historical_card()
        real_show, real_request = self.b.show, self.w.transport.request

        class StopCollection(BaseException):
            pass

        def show(card):
            if card == old:
                raise StopCollection()
            return real_show(card)

        cases = ('held', 'stale', 'revoked', 'source_failed', 'expired', 'unconfirmed')
        for case in cases:
            with self.subTest(case=case):
                self.change('UPDATE tasks SET hold_reason=NULL,published_sha256=? WHERE clickup_task_id=?',
                            a['plan_sha256'], self.tid)
                self.change("UPDATE approvals SET state='recorded' WHERE id=?", a['id'])
                if case == 'held':
                    self.change("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", self.tid)
                if case == 'stale':
                    self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', 'f' * 64, self.tid)
                if case == 'revoked':
                    self.change("UPDATE approvals SET state='revoked' WHERE id=?", a['id'])
                source = self.life.req_cards()[0]
                advanced = []

                def observed(card):
                    result = show(card)
                    if case == 'expired' and card == source and not advanced:
                        self.w.clock.advance(301)  # real fresh read, then simulated native I/O latency
                        advanced.append(True)
                    return result

                def request(method, path, **kw):
                    if case == 'unconfirmed' and method == 'GET' and path == '/v2/task/' + self.tid:
                        return TransportResult(Outcome.DEFINITE_FAIL, 503)
                    return real_request(method, path, **kw)

                real_atts = self.b.attachments
                def atts(card):
                    if case == 'source_failed' and card == source:
                        return KanbanResult(Outcome.DEFINITE_FAIL)
                    return real_atts(card)

                self.w.clock.advance(301)
                with mock.patch.object(self.b, 'show', side_effect=observed), \
                        mock.patch.object(self.b, 'attachments', side_effect=atts), \
                        mock.patch.object(self.w.transport, 'request', side_effect=request), \
                        self.assertRaises(StopCollection):
                    self.w.run()
                self.assertIsNone(self.life.gate(), 'priority collection cannot bypass ' + case)
                self.assertEqual(self.carriers(), [])

    def test_scan_reconciles_current_stage_before_unrelated_show_raises(self):
        with mock.patch.object(self, 'stall', side_effect=lambda row: ':orch:' in row['idem_key']):
            with self.stopped_stage():
                self.scans(5)
        self.assertIsNotNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        old = self.historical_card()
        real = self.b.show
        observed = []

        class StopCollection(BaseException):
            pass

        def show(card):
            if card == old:
                observed.append((self.life.orch(), self.w.task(self.tid)['last_light_ms']))
                raise StopCollection()
            return real(card)

        self.w.clock.advance(301)  # let the normal task-local light cadence become due
        scan_start = self.w.clock()
        with mock.patch.object(self.b, 'show', side_effect=show), self.assertRaises(StopCollection):
            self.w.run()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][1], scan_start, 'the real ClickUp pass must confirm this scan')
        self.assertIsNotNone(observed[0][0], 'current orchestrator creation must precede unrelated native I/O')
        # Fresh Store/Glue each time: continue the full production caller after interruption.
        self.w.step(lambda: self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.scans(5)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)
        self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'ready')

    def test_released_incident_retries_then_dead_claim_question_answers_and_resumes(self):
        with self.stopped_stage():
            self.scans(18)
        [first] = self.incidents()
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.scans(1)
        self.assertEqual(self.applications()[0]['state'], 'released')
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        for minutes, attempts in ((61, 2), (181, 3)):
            self.w.clock.now_ms = first['detected_ms'] + minutes * 60000
            self.scans(4)
            self.assertEqual(self.incidents()[0]['attempts'], attempts,
                             'normal release must preserve remaining continuation allowance')
            self.assertEqual(self.w.main('recovery-check', '--card', first['card'], '--run', '0')[0], 0)
        self.w.clock.now_ms = first['abs_ms'] + 1
        run = self.b.claim(self.life.orch(), 'orchestrator')
        self.b.cards[self.life.orch()]['task'].update(current_run_id=run, worker_pid=4242,
                                                      last_heartbeat_at=self.w.clock() // 1000)
        with mock.patch.object(kp, 'process_start_time', return_value=None):
            self.scans(4)
            self.assertEqual(self.incidents()[0]['state'], 'open', 'a dead claim is not owner progress')
            question = self.question(first, 'Which existing authorized operator can restore this worker capability?')
            self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        wait = self.wait()
        self.assertEqual(wait['question'], question)
        self.assertTrue(wait['verified_ms'])
        self.assertTrue(any(c['id'] == wait['root'] and question['decision'] in c['comment_text']
                            for c in self.w.bot_comments(self.tid)))
        self.life.say(composition.APPROVER_A, 'Use existing authorized operator A.', mention=composition.BOT,
                      reply_to=wait['root'])
        def answer(card, ref, gen):
            self.b.park(card, composition.classify(ref, gen, 'answer_only'),
                        composition.out('resolution', ref, 'Use existing authorized operator A.',
                                        gen=gen, diagnostic=first['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        self.life.orchestrate()
        [last] = self.incidents()
        self.assertEqual(last['state'], 'resumed')
        self.assertEqual(last['attempts'], 3)
        for name in ('id', 'abs_ms', 'detected_ms', 'event_created_s', 'reason_sha256'):
            self.assertEqual(last[name], first[name], name)
        self.assertEqual(len(self.carriers()), 3)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:gate:' % self.tid)), 1)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)

    def extra_requests(self, count):
        cards = []
        for i in range(count):
            card = self.b._new('Past completed request %d' % i, status='done')
            self.change("INSERT INTO cards(card_id,task,role,seq,created_key) VALUES (?,?,'request',?,?)",
                        card, self.tid, 100 + i, 'v3-history-' + str(i))
            cards.append(card)
        return cards

    def scan_before_global(self, observe=None, stop_at_entry=False):
        old, real = self.historical_card(), self.b.show
        calls = []
        class StopCollection(BaseException):
            pass
        def show(card):
            if card == old:
                raise StopCollection()
            calls.append(card)
            result = real(card)
            return observe(card, result) if observe else result
        collect = main.Glue.observe_cards
        def collector(glue):
            if stop_at_entry:
                raise StopCollection()
            return collect(glue)
        self.w.clock.advance(301)
        start = self.w.clock()
        with mock.patch.object(self.b, 'show', side_effect=show), \
                mock.patch.object(main.Glue, 'observe_cards', new=collector), self.assertRaises(StopCollection):
            self.w.run()
        return start, calls

    def test_large_request_history_resolves_exact_source_before_unrelated_collection(self):
        with self.stopped_stage():
            self.scans(5)
        self.extra_requests(32)
        calls = []
        for _ in range(3):
            start, observed = self.scan_before_global()
            calls.extend(observed)
            if self.life.gate():
                break
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], start)
        self.assertIsNotNone(self.life.gate(), '33 source candidates must not disable task-local progress')
        self.assertIn(self.life.req_cards()[0], calls)
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:gate:' % self.tid)), 1)

    def test_large_dependency_closure_is_complete_before_effect_and_refuses_missing_parent(self):
        with self.stopped_stage():
            self.scans(5)
        source = self.life.req_cards()[0]
        parents = [self.b._new('Required completed dependency %d' % i, status='done') for i in range(32)]
        self.b.cards[source]['parents'] = parents
        missing = parents[-1]
        def unavailable(card, result):
            return KanbanResult(Outcome.DEFINITE_FAIL) if card == missing else result
        _, first_calls = self.scan_before_global(unavailable)
        self.assertIsNone(self.life.gate(), 'a missing required parent must not be truncated')
        _, calls = self.scan_before_global()
        self.assertTrue(set(parents) <= set(first_calls + calls))
        self.assertIn(missing, calls, 'failed reads must be retried, never cached as satisfied')
        self.assertIsNotNone(self.life.gate(), 'complete 33-object dependency closure must make progress')

    def required_slow_cards(self):
        source = self.life.req_cards()[0]
        parents = [self.b._new('Required slow source parent %d' % i, status='done') for i in range(6)]
        self.b.cards[source]['parents'].extend(parents)
        return set(parents + [source])

    def test_slow_complete_required_reads_renew_before_real_stage_effect(self):
        with self.stopped_stage():
            self.scans(5)
        slow = self.required_slow_cards()
        before = self.w.cu.count('GET', '/v2/task/' + self.tid)
        def latency(card, result):
            if card in slow:
                self.w.clock.advance(45)
            return result
        start, calls = self.scan_before_global(latency)
        self.assertTrue(slow <= set(calls))
        self.assertGreater(self.w.clock() - start, 300000)
        self.assertGreater(self.w.task(self.tid)['last_light_ms'], start,
                           'a real late target-local observation must renew the expired input')
        self.assertGreaterEqual(self.w.cu.count('GET', '/v2/task/' + self.tid) - before, 2)
        self.assertIsNotNone(self.life.gate(), 'complete slow reads must reach the guarded real effect')

    def slow_refresh_refusal(self, mode):
        with self.stopped_stage():
            self.scans(5)
        slow = self.required_slow_cards()
        source = self.life.req_cards()[0]
        injected = []
        def latency(card, result):
            if card in slow:
                self.w.clock.advance(45)
            if card == source and not injected:
                injected.append(True)
                if mode == 'activity':
                    self.life.say(composition.APPROVER_A, 'New requirements need classification.', mention=composition.BOT)
                elif mode == 'partial':
                    self.w.cu.inject('GET', '/v2/task/%s/comment$' % self.tid,
                                     Outcome.DEFINITE_FAIL, status=503)
                elif mode == 'omitted_list':
                    task = dict(self.w.cu.tasks[self.tid])
                    task.pop('list')
                    self.w.cu.inject('GET', '/v2/task/%s$' % self.tid, Outcome.OK, status=200, json=task)
                elif mode == 'budget':
                    self.w.cu.rate_remaining = 0
            return result
        start, _ = self.scan_before_global(latency)
        self.assertEqual(injected, [True])
        self.assertIsNone(self.life.gate(), 'renewal must fail closed: ' + mode)
        self.assertEqual(self.carriers(), [])
        if mode in ('partial', 'budget'):
            self.assertEqual(self.w.task(self.tid)['last_light_ms'], start)

    def test_slow_required_reads_refuse_changed_activity(self):
        self.slow_refresh_refusal('activity')

    def test_slow_required_reads_refuse_partial_renewal(self):
        self.slow_refresh_refusal('partial')

    def test_slow_required_reads_refuse_current_list_omission(self):
        self.slow_refresh_refusal('omitted_list')

    def test_slow_required_reads_preserve_shared_request_budget(self):
        self.slow_refresh_refusal('budget')

    def test_executor_and_public_preworker_admission_ignore_revoked_orchestrators(self):
        with self.stopped_stage():
            self.scans(18)
        [inc], [approval] = self.incidents(), self.applications()
        revoked = []
        for i in range(7):
            card = self.b._new('Revoked historical orchestrator %d' % i, status='done')
            self.change("INSERT INTO approvals(task,rev,accounts_gen,comment_date,plan_sha256,state,"
                        "orchestrator_card,revoked_reason) VALUES (?,?,?,?,?,'revoked',?,'superseded')",
                        self.tid, 100 + i, approval['accounts_gen'], self.w.clock(), approval['plan_sha256'], card)
            revoked.append(card)
        def latency(card, result):
            if card in revoked:
                self.w.clock.advance(45)
            return result
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        with self.stopped_stage():
            _, calls = self.scan_before_global(latency, stop_at_entry=True)
        self.assertFalse(set(revoked) & set(calls), 'preworker executor must not expand to revoked history')
        [wake] = self.w.q('SELECT state,attempts FROM outbox WHERE idem_key=?',
                         engine._rcv_key(self.tid, inc['card'], 0, 2) + ':wake')
        self.assertEqual(wake, {'state': 'done', 'attempts': 1})
        self.assertEqual(self.b.cards[self.carriers()[-1]]['task']['status'], 'done')
        calls, real, mutations, provider = [], self.b.show, list(self.b.mutations()), self.w.cu.count()
        def public_show(card):
            calls.append(card)
            return latency(card, real(card))
        with mock.patch.object(self.b, 'show', side_effect=public_show), \
                mock.patch.object(main, 'Store', side_effect=AssertionError('public admission is read-only')):
            rc, lines = self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
        self.assertEqual(rc, 0, lines)
        self.assertFalse(set(revoked) & set(calls))
        self.assertEqual(self.b.mutations(), mutations)
        self.assertEqual(self.w.cu.count(), provider)

    def test_required_context_continues_bounded_across_scans_and_rechecks_roots(self):
        with self.stopped_stage():
            self.scans(5)
        source = self.life.req_cards()[0]
        parents = [self.b._new('Large required parent %d' % i, status='done') for i in range(70)]
        self.b.cards[source]['parents'] = parents
        attachments, real = [], self.b.attachments
        def atts(card):
            attachments.append(card)
            return real(card)
        with mock.patch.object(self.b, 'attachments', side_effect=atts):
            _, first = self.scan_before_global(stop_at_entry=True)
        self.assertIsNone(self.life.gate(), 'an incomplete bounded page is never full evidence')
        self.assertLessEqual(len(first) + len(attachments), 64)
        new_parent = self.b._new('New required parent during continuation', status='done')
        self.b.cards[source]['parents'].append(new_parent)
        def missing(card, result):
            return KanbanResult(Outcome.DEFINITE_FAIL) if card == new_parent else result
        _, second = self.scan_before_global(missing, stop_at_entry=True)
        self.assertIn(source, second, 'source root must be genuinely renewed on continuation')
        self.assertIn(new_parent, second, 'changed parent membership must expand the required set')
        self.assertIsNone(self.life.gate(), 'a newly unobserved obligation must defer')
        third = []
        for _ in range(3):
            _, observed = self.scan_before_global(stop_at_entry=True)
            third.extend(observed)
            if self.life.gate():
                break
        self.assertIn(new_parent, third)
        self.assertIsNotNone(self.life.gate())
        self.assertTrue(set(parents) <= set(first + second + third))

    def test_public_preworker_admission_preserves_required_parent_source_and_revocation(self):
        with self.stopped_stage():
            self.scans(18)
        [inc] = self.incidents()
        source = self.life.req_cards()[0]
        parent = self.b._new('Required current source parent', status='blocked')
        self.b.cards[source]['parents'].append(parent)
        check = lambda: self.w.main('recovery-check', '--card', inc['card'], '--run', '0')
        self.assertEqual(check()[0], 2)
        self.b.finish(parent, 'default')
        self.assertEqual(check()[0], 0)
        real = self.b.attachments
        with mock.patch.object(self.b, 'attachments', side_effect=lambda c:
                KanbanResult(Outcome.DEFINITE_FAIL) if c == source else real(c)):
            self.assertEqual(check()[0], 2)
        real_show = self.b.show
        def revoke(card):
            result = real_show(card)
            if card == source:
                self.change("UPDATE approvals SET state='revoked' WHERE id=?", inc['approval_id'])
            return result
        with mock.patch.object(self.b, 'show', side_effect=revoke):
            self.assertEqual(check()[0], 3)

    def test_refresh_rechecks_new_native_parent_before_effect(self):
        with self.stopped_stage():
            self.scans(5)
        source = self.life.req_cards()[0]
        slow = self.required_slow_cards()
        parent = self.b._new('Added during genuine provider renewal', status='blocked')
        request, renewed = self.w.transport.request, []
        start = self.w.clock()
        def changed(method, path, **kw):
            result = request(method, path, **kw)
            if method == 'GET' and path == '/v2/task/' + self.tid and self.w.clock() - start > 600000:
                self.b.cards[source]['parents'].append(parent)
                renewed.append(True)
            return result
        def latency(card, result):
            if card in slow:
                self.w.clock.advance(45)
            return result
        with mock.patch.object(self.w.transport, 'request', side_effect=changed):
            _, calls = self.scan_before_global(latency, stop_at_entry=True)
        self.assertEqual(renewed, [True])
        self.assertIn(parent, calls)
        self.assertIsNone(self.life.gate())

    def test_unrelated_request_failures_do_not_hide_bound_published_source(self):
        with self.stopped_stage():
            self.scans(5)
        history = set(self.extra_requests(32))
        def unavailable(card, result):
            return KanbanResult(Outcome.DEFINITE_FAIL) if card in history else result
        _, calls = self.scan_before_global(unavailable, stop_at_entry=True)
        self.assertIsNotNone(self.life.gate(), 'unrelated history is not a required current source')
        self.assertFalse(history & set(calls))

    def test_unobserved_native_question_is_not_proof_of_satisfied_obligation(self):
        with self.stopped_stage():
            self.scans(5)
            [a] = self.applications()
            self.change("INSERT INTO blockers(task,question_card,card,run_id,created_ms,approval_id,state,closure_gen,"
                        "origin_lane) VALUES (?,?,?,0,?,?,'scope_change',?,'work')", self.tid, 't_missingq1',
                        't_previous1', self.w.clock(), a['id'] - 1, a['accounts_gen'])
            self.scans(18)
            self.assertEqual(self.carriers(), [], 'unknown question must not be treated as incorporated or closed')
