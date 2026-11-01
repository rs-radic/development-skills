"""General stall-to-AI contract (sanitized owner-request regression).

Use the real scan/effect path with fake external ports. The malformed token below is the
retained malformed regression value, not a repaired or normalized approximation.
"""
import os
import unittest
from unittest import mock

from clickup_intake import rules
import test_recovery as recovery
import test_s4_composition as composition


class GeneralStallWakeTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            patcher = mock.patch(target, side_effect=AssertionError('offline-only test boundary'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.w = composition.World(self)
        self.w.board = recovery.RecoveryBoard(os.path.join(self.w.dir, 'board'), self.w.clock, self.w.inj)
        self.b = self.w.board
        from preworker_snapshot_fixture import install
        install(self.b, self.w.dir)
        self.life = composition.Lifecycle(self.w)
        self.tid = self.life.tid
        self.assertEqual(self.w.main('recovery', 'enable')[0], 0)
        self.w.clock.advance(5)

    def scans(self, count):
        for _ in range(count):
            self.w.clock.advance(61)
            self.w.run()

    def carriers(self):
        return self.b.by_key('clickup-dp:%s:rcv:' % self.tid)

    def test_completed_plan_with_invalid_handoff_wakes_ai_before_human_approval(self):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.say(composition.APPROVER_A, 'Technical requirements closed')
        source = []

        def planner(card, ref, gen):
            self.b.worker_comment(card, 'planner', composition.classify(ref, gen))
            actual_sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1, gen)
            request = self.b.cards[card]['runs'][-1]
            request['metadata']['plan_sha256'] = 'ff7ec9d4placeholder'
            self.b.claim(card, 'plan-reviewer')
            self.b.finish(card, 'plan-reviewer', {
                'clickup_task': self.tid, 'plan_revision': 3, 'plan_attachment': 'clickup-plan-r3.md',
                'plan_sha256': actual_sha, 'accounts_gen': gen,
                'review_outcome': 'PASS', 'reviewed_sha256': actual_sha,
            })
            source.append(card)

        self.w.step(lambda: bool(source) and any('without valid PASS evidence' in line for line in self.w.outputs),
                    world=self.life.planner(planner))
        self.assertEqual(self.w.q('SELECT * FROM approvals WHERE task=?', self.tid), [])
        self.assertEqual(self.b.cards[source[0]]['task']['status'], 'done')
        self.assertEqual(self.w.task(self.tid)['published_rev'], None)
        self.scans(18)
        self.assertEqual(len(self.carriers()), 1,
                         'a stalled completed-plan handoff must wake AI without requiring product approval')
        carrier = self.b.cards[self.carriers()[0]]
        self.assertEqual(carrier['task']['status'], 'done')
        self.assertEqual([s['delivery_mode'] for s in carrier['subs']], ['wake'])
        self.assertEqual(self.b.cards[source[0]]['task']['status'], 'done',
                         'mechanical detection must not reopen or rewrite the source')
        self.scans(10)
        self.assertEqual(len(self.carriers()), 1, 'unchanged incident must not create repeated AI wakes')

    def invalid_plan(self, missing_attachment=False):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.say(composition.APPROVER_A, 'Technical requirements closed')
        sources = []
        def planner(card, ref, gen):
            self.b.worker_comment(card, 'planner', composition.classify(ref, gen))
            actual_sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1, gen)
            self.b.cards[card]['runs'][-1]['metadata']['plan_sha256'] = 'ff7ec9d4placeholder'
            self.b.claim(card, 'plan-reviewer')
            self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
                plan_attachment='clickup-plan-r3.md', plan_sha256=actual_sha, accounts_gen=gen,
                review_outcome='PASS', reviewed_sha256=actual_sha))
            if missing_attachment:
                self.b.cards[card]['runs'][-2]['metadata']['plan_sha256'] = actual_sha
                self.b.cards[card]['atts'] = []
            sources.append(card)
        self.w.step(lambda: bool(sources) and any('without valid PASS evidence' in l for l in self.w.outputs),
                    world=self.life.planner(planner))
        return sources[0]

    def test_three_attempts_and_deadline_keep_technical_notice_unresolved(self):
        import json
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['attempts'], 1)
        for minute in (61, 181):
            self.w.clock.now_ms = inc['detected_ms'] + minute * 60000
            self.scans(6)
        self.assertEqual(len(self.carriers()), 3)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(7)
        notices = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['target'], self.tid)
        text = json.loads(notices[0]['body'])['text']
        self.assertIn('integration operator', text)
        self.assertNotIn('Approved plan', text)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.scans(12)
        self.assertEqual(len(self.carriers()), 3)
        self.assertEqual(len([c for c in self.w.bot_comments(self.tid) if text in c['comment_text']]), 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'done')

    def test_completed_approved_producer_missing_handoff_uses_same_diagnostic(self):
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.life.say(composition.APPROVER_A, 'Approve plan r1 for development')
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        card = self.life.orch()
        run = self.b.claim(card, 'orchestrator')
        self.b.finish(card, 'orchestrator', {})
        before = set(self.b.cards)
        self.scans(18)
        self.assertEqual(len(self.carriers()), 1)
        [inc] = self.w.q('SELECT * FROM recoveries')
        [approval] = self.w.q('SELECT * FROM approvals')
        self.assertEqual((inc['card'], inc['run_id'], inc['approval_id']), (card, run, approval['id']))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.assertEqual(set(self.b.cards) - before, set(self.carriers()), 'never synthesize replacement product graph')

    def test_real_same_request_rereview_and_publication_close_only_after_transition(self):
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        original = self.b.cards[card]['runs'][-2]['metadata'].copy()
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.b.claim(card, 'planner')
        sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1,
                                    self.w.task(self.tid)['classified_gen'])
        self.b.claim(card, 'plan-reviewer')
        self.scans(12)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open',
                         'an active reviewer does not itself fulfill publication')
        self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
            plan_attachment='clickup-plan-r3.md', plan_sha256=sha,
            accounts_gen=self.w.task(self.tid)['classified_gen'], review_outcome='PASS', reviewed_sha256=sha))
        self.w.step(lambda: self.w.task(self.tid)['published_rev'] == 3)
        self.scans(5)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'closed')
        self.assertEqual(self.b.cards[card]['runs'][2]['metadata'], original)
        self.assertEqual(original['plan_sha256'], 'ff7ec9d4placeholder')
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])
        self.assertEqual(self.w.q('SELECT attempts,abs_ms FROM recoveries')[0],
                         {'attempts': inc['attempts'], 'abs_ms': inc['abs_ms']})

    def test_repeated_failed_handoff_retains_incident_budget_and_history(self):
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.b.claim(card, 'planner')
        sha = self.b.request_review(card, self.tid, 3, composition.PLAN_R1, self.w.task(self.tid)['input_gen'])
        self.b.cards[card]['runs'][-1]['metadata']['plan_sha256'] = 'ff7ec9d4placeholder'
        self.b.claim(card, 'plan-reviewer')
        self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=3,
            plan_attachment='clickup-plan-r3.md', plan_sha256=sha, accounts_gen=self.w.task(self.tid)['input_gen'],
            review_outcome='PASS', reviewed_sha256=sha))
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(8)
        self.assertEqual(self.w.q('SELECT attempts FROM recoveries')[0]['attempts'], 1,
                         'a changed structural context must itself pass stable grace')
        self.scans(10)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1, 'same unfulfilled publication obligation cannot buy another incident budget')
        self.assertEqual(rows[0]['attempts'], 2)
        self.assertEqual((rows[0]['run_id'], rows[0]['abs_ms']), (inc['run_id'], inc['abs_ms']))
        self.assertNotEqual(rows[0]['reason_sha256'], inc['reason_sha256'])
        self.assertTrue(self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_history:%'"))

    def test_missing_request_owner_uses_logical_anchor_without_native_lookup(self):
        from clickup_intake import main
        original = main.Glue.kanban
        with mock.patch.object(main.Glue, 'kanban', lambda g, r: False if ':req:' in r['idem_key'] else original(g, r)):
            self.scans(25)
            self.assertEqual(self.life.req_cards(), [])
            self.assertEqual(len(self.carriers()), 1)
            [inc] = self.w.q('SELECT * FROM recoveries')
            self.assertEqual(inc['run_id'], 0)
            self.assertTrue(inc['card'].startswith('progress-'))
            self.assertIsNone(inc['approval_id'])
            self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
            self.assertFalse(any(card == inc['card'] for _, card in self.b.calls))
            self.assertFalse(any(c['runs'] for c in self.b.cards.values()))

    def test_different_structural_missing_attachment_uses_identical_fallback(self):
        self.invalid_plan(missing_attachment=True)
        self.scans(18)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.w.q('SELECT class FROM recoveries')[0]['class'], 'progress_stall')
        self.assertIsNone(self.w.task(self.tid)['published_rev'])

    def test_unknown_observation_breaks_stable_grace_without_a_wake(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        card = self.invalid_plan()
        self.scans(5)
        original = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda c:
                KanbanResult(Outcome.DEFINITE_FAIL) if c == card else original(c)):
            self.scans(15)
        self.assertEqual(self.carriers(), [])
        self.scans(5)
        self.assertEqual(self.carriers(), [])
        self.scans(12)
        self.assertEqual(len(self.carriers()), 1)

    def test_current_readonly_admission_and_post_io_changed_authority(self):
        from clickup_intake import main
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        before = list(self.b.mutations())
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(inc['run_id']))[0], 0)
        original = self.b._preworker_snapshot
        def changed(*args):
            proof = original(*args)
            c = recovery.store_conn(self.w)
            c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
            c.close()
            return proof
        with mock.patch.object(self.b, '_preworker_snapshot', side_effect=changed):
            self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(inc['run_id']))[0], 3)
        self.assertEqual(self.b.mutations(), before)
        c = recovery.store_conn(self.w)
        c.execute('UPDATE tasks SET hold_reason=NULL WHERE clickup_task_id=?', (self.tid,))
        c.close()
        with mock.patch.object(self.b, '_preworker_snapshot', return_value=None):
            self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(inc['run_id']))[0], 2)

    def test_null_approval_incident_cap_does_not_buy_a_fourth_wake(self):
        card = self.invalid_plan()
        c = recovery.store_conn(self.w)
        for run in (1, 2, 3):
            c.execute("INSERT INTO recoveries(task,card,run_id,event_kind,event_created_s,block_kind,class,"
                      "reason_sha256,approval_id,approval_rev,plan_sha256,adopted,state,attempts,abs_ms,detected_ms,updated_ms)"
                      " VALUES (?,?,?,'progress_stall',1,'progress_stall','progress_stall',?,NULL,NULL,NULL,0,'closed',3,1,1,1)",
                      (self.tid, card, run, '0'*64))
        c.close()
        self.scans(18)
        self.assertEqual(self.carriers(), [])
        rows = self.w.q("SELECT * FROM recoveries WHERE state='open'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempts'], 0)
        self.assertTrue(self.w.q("SELECT 1 FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'"))

    def test_verified_unanswered_planning_question_is_quiet_even_if_producer_is_done(self):
        self.life.pickup_to_business()
        card = self.life.req_cards()[-1]
        self.b.cards[card]['task']['status'] = 'done'
        self.scans(18)
        self.assertEqual(self.carriers(), [])

    def test_completed_dependency_closure_is_observed_before_wake_and_admission(self):
        card = self.invalid_plan()
        grandparent = self.b._new('Settled prerequisite', status='done')
        parent = self.b._new('Settled parent', status='done', parents=(grandparent,))
        self.b.cards[card]['parents'] = [parent]
        self.scans(18)
        self.assertEqual(len(self.carriers()), 1)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(inc['run_id']))[0], 0)
        self.b.cards[grandparent]['task']['status'] = 'running'
        self.assertNotEqual(self.w.main('recovery-check', '--card', card, '--run', str(inc['run_id']))[0], 0)

    def test_working_ack_defers_retry_but_never_absolute_deadline(self):
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, inc['run_id']))
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60000
        self.scans(5)
        self.assertEqual(len(self.carriers()), 1)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(8)
        self.assertEqual(len(self.carriers()), 1)
        self.assertTrue(self.w.q("SELECT 1 FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'"))

    def test_cannot_act_delivers_factual_notice_without_spurious_human_question(self):
        import json
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act\nraw private detail' % (card, inc['run_id']))
        self.scans(8)
        notices = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(len(notices), 1)
        self.assertNotIn('raw private', json.loads(notices[0]['body'])['text'])
        self.assertNotIn('Decision needed', json.loads(notices[0]['body'])['text'])
        self.assertIsNone(self.wait())
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')

    def test_stale_source_after_subscribe_never_completes_a_wake(self):
        card = self.invalid_plan()
        original = self.b.subscribe
        def subscribe(cid, mode='notify'):
            result = original(cid, mode)
            if mode == 'wake':
                self.b.cards[card]['task']['status'] = 'running'
            return result
        with mock.patch.object(self.b, 'subscribe', side_effect=subscribe):
            self.scans(18)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.b.cards[self.carriers()[0]]['task']['status'], 'archived')
        self.assertFalse(any(c[0] == 'complete' and c[1] == self.carriers()[0] for c in self.b.calls))

    def test_absolute_cutover_excludes_old_completed_stalls(self):
        import json
        self.invalid_plan()
        c = recovery.store_conn(self.w)
        c.execute("UPDATE meta SET value=? WHERE key='recovery_cutover_ms'", (json.dumps(self.w.clock()+1000),))
        c.close()
        self.scans(18)
        self.assertEqual(self.carriers(), [])

    def test_read_allowance_exhaustion_is_unavailable_not_authority(self):
        from clickup_intake import main, store
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        reader = main._PreworkerReads(self.b, self.w.clock)
        reader.remaining = 1
        before = list(self.b.mutations())
        result = main._progress_check(lambda: store.load_recovery_snapshot(self.w.state_path,
            self.tid, card, inc['run_id']), self.b, card, inc['run_id'], self.w.clock, reader=reader)
        self.assertEqual(result.code, 2)
        self.assertEqual(reader.remaining, 0)
        self.assertEqual(self.b.mutations(), before)

    def test_ambiguous_origin_delivery_is_retained_without_repeated_sends(self):
        from clickup_intake.contracts import Outcome
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        before = self.w.cu.count('POST', path)
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (card, inc['run_id']))
        self.scans(40)
        [notice] = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(notice['state'], 'held_ambiguous')
        self.assertEqual(self.w.cu.count('POST', path)-before, 1)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertEqual(len(self.carriers()), 1)

    def test_failed_carrier_delivery_keeps_origin_explanation_deliverable(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        card = self.invalid_plan()
        original = self.b.complete
        def complete(cid, summary, metadata=None):
            if cid in self.carriers():
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return original(cid, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=complete):
            self.scans(18)
            [inc] = self.w.q('SELECT * FROM recoveries')
            self.w.clock.advance(2)
            self.b.worker_comment(self.carriers()[0], 'default',
                'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, inc['run_id']))
            self.scans(85)
        self.assertTrue(self.w.q("SELECT 1 FROM outbox WHERE kind='kb_complete' AND state='failed'"
                                 " AND idem_key LIKE '%:rcv:%'"), self.w.q("SELECT idem_key,state,attempts,outcome FROM outbox WHERE idem_key LIKE '%:rcv:%'") + self.w.outputs)
        notices = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['state'], 'done')
        self.assertEqual(notices[0]['target'], self.tid)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertTrue(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.b.cards[card]['task']['status'], 'done')

    def test_definite_origin_delivery_failure_retains_pending_responsibility(self):
        from clickup_intake.contracts import Outcome
        card = self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        path = '/v2/task/%s/comment' % self.tid
        for _ in range(6):
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (card, inc['run_id']))
        self.scans(90)
        [notice] = self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual((notice['state'], notice['attempts']), ('failed', 6))
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertEqual(len(self.carriers()), 1)
        self.assertTrue(any('failed after 6 attempts' in line for line in self.w.outputs))

    def wait(self):
        import json
        rows = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait:%'")
        return json.loads(rows[0]['value']) if rows else None

    def test_preapproval_question_uses_json_null_and_verified_answer_lifecycle(self):
        import json
        self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        body = self.b.cards[self.carriers()[0]]['task']['body']
        qline = next(l for l in body.splitlines() if l.startswith('`CLICKUP-RECOVERY-QUESTION'))
        example = json.loads(qline[len('`CLICKUP-RECOVERY-QUESTION v1 '):-1])
        self.assertIsNone(example['approval'])
        self.assertIsNone(example['revision'])
        self.assertIsNone(example['sha256'])
        question = dict(task=self.tid, card=inc['card'], run=inc['run_id'], approval=None,
                        revision=None, sha256=None, fingerprint=inc['reason_sha256'],
                        failed='The planning recipient is ambiguous.', attempted='Read current task and handoff.',
                        decision='Which existing planning contact should receive the clarification, A or B?',
                        next_action='Reply with the contact, without credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[0], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\nCLICKUP-RECOVERY-QUESTION v1 %s'
            % (inc['card'], inc['run_id'], json.dumps(question)))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        self.assertTrue(self.wait()['root'])
        self.scans(70)
        self.assertEqual(len(self.carriers()), 1, 'verified unanswered decision suppresses more wakes')
        self.life.say(composition.APPROVER_A, 'Use existing contact A.', mention=composition.BOT,
                      reply_to=self.wait()['root'])
        def answer(card, ref, gen):
            self.b.park(card, composition.classify(ref, gen, 'answer_only'),
                        composition.out('resolution', ref, 'Use existing contact A.',
                                        gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['attempts'], after['abs_ms']), (inc['attempts'], inc['abs_ms']))
