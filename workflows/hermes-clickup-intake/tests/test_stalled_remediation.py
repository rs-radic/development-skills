"""R1-R5 regression cases: real scan caller order, isolated fake provider/native ports."""
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import KanbanResult, Outcome
import test_stalled_fallback as fallback
import test_s4_composition as comp
from test_recovery import store_conn


class RemediationTest(unittest.TestCase):
    setUp = fallback.StalledFallbackTest.setUp
    carriers = fallback.StalledFallbackTest.carriers
    scan_minutes = fallback.StalledFallbackTest.scan_minutes
    notice_rows = fallback.StalledFallbackTest.notice_rows
    unknown_stop = fallback.StalledFallbackTest.unknown_stop
    queue_notice = fallback.StalledFallbackTest.queue_notice

    def exhausted_carrier(self):
        card = self.life.fe
        run = self.b.claim(card, 'tester')
        self.b.block(card, 'unknown_stop', 'A technical stop with no matching parser')
        with mock.patch.object(self.b, 'complete', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.scan_minutes(70)
        return card, run

    def test_R1_own_carrier_hold_retries_transient_notice_then_verifies_without_release(self):
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        card, run = self.exhausted_carrier()
        self.scan_minutes(40)
        [notice] = self.notice_rows()
        self.assertEqual(notice['state'], 'done', 'the own informational notice must remain retryable while execution stays held')
        self.assertEqual(notice['attempts'], 2)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'escalated')
        self.assertTrue(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)
        self.assertEqual(len(self.carriers()), 1)

    def test_R2_current_revision_turnover_rebinds_same_stop_without_fresh_budgets(self):
        self.unknown_stop()
        [before] = self.w.q('SELECT * FROM recoveries')
        for revision in (2, 3):
            self.next_approval(revision)
            self.scan_minutes(18)
            [after] = self.w.q('SELECT * FROM recoveries')
            self.assertEqual(after['approval_rev'], revision, 'a historical same-stop row must not permanently exclude current authority')
            for field in ('id', 'attempts', 'abs_ms', 'detected_ms'):
                self.assertEqual(after[field], before[field], field)
        self.assertEqual(len(self.carriers()), 1)
        self.assertGreaterEqual(len(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%'")), 2)

    def question_ack(self, card, run, **changes):
        [inc] = self.w.q('SELECT * FROM recoveries')
        q = {'task': self.tid, 'card': card, 'run': run, 'approval': inc['approval_id'],
             'revision': inc['approval_rev'], 'sha256': inc['plan_sha256'], 'fingerprint': inc['reason_sha256'],
             'failed': 'The approved identity provider cannot be inferred from the accepted requirements.',
             'attempted': 'Read the accepted requirements and inspected the existing integration capabilities.',
             'decision': 'Which approved identity provider must this integration use: existing provider A or B?',
             'next_action': 'Reply in this question thread and mention the intake bot; choose A or B and explain constraints.'}
        q.update(changes)
        self.b.worker_comment(card, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\n'
                              'CLICKUP-RECOVERY-QUESTION v1 %s' % (card, run, json.dumps(q, sort_keys=True)))
        return q

    def diagnostic_wait(self):
        rows = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait:%'")
        return json.loads(rows[0]['value']) if rows else None

    def test_R3_precise_question_verified_wait_then_targeted_answer_uses_normal_classification(self):
        card, run = self.unknown_stop()
        [before] = self.w.q('SELECT * FROM recoveries')
        q = self.question_ack(card, run)
        self.scan_minutes(6)
        [notice] = self.notice_rows()
        text = json.loads(notice['body'])['text']
        self.assertIn(q['decision'], text, 'a fixed generic notice is not the actual missing decision')
        self.assertIn(q['next_action'], text)
        wait = self.diagnostic_wait()
        self.assertEqual(wait['state'], 'waiting')
        self.assertEqual(wait['root'], notice['result_ref'])
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.life.say(comp.APPROVER_A, 'Use existing provider A.', mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'The answer selects existing provider A within unchanged authority.',
                                 gen=gen, diagnostic=before['id'], authority='unchanged'))
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'answered', world=self.life.planner(script))
        self.scan_minutes(6)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'open')
        for field in ('attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked', 'an answer is not automatic native release')
        self.assertEqual(self.w.q('SELECT id FROM blockers'), [], 'do not fabricate or close a native question')

    def test_R4_queued_notice_waits_for_complete_parent_observation(self):
        card, run, key = self.queue_notice()
        path = '/v2/task/%s/comment' % self.tid
        before = self.w.cu.count('POST', path)
        self.b.cards[card]['parents'].append('t_deadbeef')
        self.scan_minutes(5)
        self.assertGreater(self.b.calls.count(('show', 't_deadbeef')), 0)
        self.assertEqual(self.w.cu.count('POST', path), before, 'unknown obligation must defer an already queued notice')
        self.assertEqual(self.notice_rows()[0]['state'], 'pending')
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.b.cards[card]['parents'].remove('t_deadbeef')
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows()[0]['state'], 'done')
        self.assertEqual(self.w.cu.count('POST', path) - before, 1)

    def test_R5_public_admission_uses_post_IO_clock_for_five_minute_lease(self):
        card, run = self.unknown_stop()
        real, advanced = self.b.show, [False]
        def slow_show(cid):
            value = real(cid)
            if not advanced[0]:
                advanced[0] = True
                self.w.clock.advance(601)
            return value
        before = list(self.b.mutations())
        with mock.patch.object(self.b, 'show', side_effect=slow_show):
            rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertGreater(self.w.clock() - self.w.task(self.tid)['last_light_ms'], 300_000)
        self.assertEqual(rc, 2, lines)
        self.assertEqual(lines, ['UNAVAILABLE diagnostic_input_barrier'])
        self.assertEqual(self.b.mutations(), before)

    def test_R4_changed_parent_cancels_unsent_notice_then_replans_only_current_context(self):
        card, run, key = self.queue_notice()
        path = '/v2/task/%s/comment' % self.tid
        before = self.w.cu.count('POST', path)
        parent = self.b._new('A real current dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(5)
        [old] = self.notice_rows()
        self.assertEqual(old['state'], 'cancelled_stale')
        self.assertEqual(self.w.cu.count('POST', path), before)
        self.b.cards[parent]['task']['status'] = 'done'
        self.scan_minutes(18)
        rows = self.notice_rows()
        self.assertEqual(len(rows), 2, 'known-unsent stale context must be replaceable without rewriting its history')
        self.assertEqual(rows[0]['state'], 'cancelled_stale')
        self.assertEqual(rows[1]['state'], 'done')
        self.assertNotEqual(rows[0]['idem_key'], rows[1]['idem_key'])
        self.assertEqual(self.w.cu.count('POST', path) - before, 1)

    def test_R1_own_hold_notice_exhaustion_alerts_and_remains_unfinished(self):
        path = '/v2/task/%s/comment' % self.tid
        for _ in range(6):
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        card, _ = self.exhausted_carrier()
        self.scan_minutes(60)
        [notice] = self.notice_rows()
        self.assertEqual((notice['state'], notice['attempts']), ('failed', 6))
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertTrue(any('ClickUp' in line and 'failed after 6 attempts' in line for line in self.w.outputs))
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_R1_own_hold_ambiguous_applied_notice_reconciles_without_resend(self):
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=True)
        self.exhausted_carrier()
        self.scan_minutes(15)
        [notice] = self.notice_rows()
        self.assertEqual((notice['state'], notice['attempts']), ('done', 1))
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'escalated')
        self.assertTrue(self.w.task(self.tid)['hold_reason'])

    def test_R1_true_operator_hold_never_borrows_informational_observation(self):
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.exhausted_carrier()
        self.assertEqual(self.w.main('hold', self.tid)[0], 0)
        before = self.w.task(self.tid)['last_light_ms']
        count = self.w.cu.count('POST', path)
        self.scan_minutes(15)
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], before)
        self.assertEqual(self.w.cu.count('POST', path), count)
        self.assertEqual(self.notice_rows()[0]['attempts'], 1)

    def test_R1_foreign_hold_binding_cannot_make_a_held_notice_observable(self):
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.exhausted_carrier()
        [notice] = self.notice_rows()
        p = json.loads(notice['body'])
        p['recovery_delivery_hold'] = 'outbox:clickup-dp:foreign:rcv:t_deadbeef:1:1:wake'
        conn = store_conn(self.w)
        conn.execute('UPDATE outbox SET body=? WHERE idem_key=?', (json.dumps(p), notice['idem_key']))
        conn.close()
        before = self.w.task(self.tid)['last_light_ms']
        count = self.w.cu.count('POST', path)
        self.scan_minutes(15)
        self.assertEqual(self.w.task(self.tid)['last_light_ms'], before)
        self.assertEqual(self.w.cu.count('POST', path), count)

    def test_R2_pending_classification_is_not_revocation_or_permission_to_escalate(self):
        self.unknown_stop()
        [before] = self.w.q('SELECT * FROM recoveries')
        self.life.say(comp.APPROVER_A, 'Please explain the current status.', mention=comp.BOT)
        self.scan_minutes(10)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(5)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'open')
        self.assertEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(self.notice_rows(), [])
        self.assertEqual(len(self.carriers()), 1)

    def test_R2_explicit_abandon_survives_real_answer_classification(self):
        card, run = self.unknown_stop()
        [before] = self.w.q('SELECT * FROM recoveries')
        key = 'clickup-dp:%s:rcv:%s:%d:1:wake' % (self.tid, card, run)
        conn = store_conn(self.w)
        conn.execute("UPDATE outbox SET state='failed' WHERE idem_key=?", (key,))
        conn.close()
        self.assertEqual(self.w.main('outbox', 'abandon', key)[0], 0)
        self.answered_status_input()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(self.w.q('SELECT outcome FROM outbox WHERE idem_key=?', key)[0]['outcome'], 'ABANDONED')
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.notice_rows(), [])

    def test_R3_invalid_or_foreign_decisions_stay_unfinished_until_safe_correction(self):
        card, run = self.unknown_stop()
        for change in ({'decision': 'Read C:/private/secret.txt'}, {'task': 'foreign'}, {'decision': 'API key: sk-' + 'a' * 40}):
            self.question_ack(card, run, **change)
            self.scan_minutes(4)
            self.assertEqual(self.notice_rows(), [])
            self.assertIsNone(self.diagnostic_wait())
            self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertTrue(any('required human escalation remains unfinished' in line for line in self.w.outputs))
        q = self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.assertIn(q['decision'], json.loads(self.notice_rows()[0]['body'])['text'])
        self.assertNotIn('C:/private', json.loads(self.notice_rows()[0]['body'])['text'])

    def test_R3_foreign_actor_ack_cannot_create_a_question_or_end_monitoring(self):
        card, run = self.unknown_stop()
        q = self.question_ack(card, run)
        self.b.cards[card]['comments'][-1]['author'] = 'unrelated-worker'
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [])
        self.assertIsNone(self.diagnostic_wait())
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')

    def test_R3_unrelated_classified_input_and_resolution_cannot_satisfy_target_wait(self):
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        self.scan_minutes(6)
        self.life.say(comp.APPROVER_A, 'Unrelated status request.', mention=comp.BOT)
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen), comp.out('resolution', ref, 'A plausible but unrelated answer.',
                        gen=gen, diagnostic=1, authority='unchanged'))
        self.w.step(lambda: self.w.task(self.tid)['classified_gen'] == self.w.task(self.tid)['input_gen'],
                    world=self.life.planner(script))
        self.scan_minutes(5)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.assertTrue(any('Diagnostic question resolution refused' in line for line in self.w.outputs))
        self.w.clock.now_ms = self.w.q('SELECT abs_ms FROM recoveries')[0]['abs_ms'] + 1
        self.scan_minutes(5)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.carriers()), 1, 'a genuine delivered human wait remains quiet beyond diagnostic deadline')

    def test_R3_material_answer_uses_normal_scope_and_approval_lifecycle(self):
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        self.scan_minutes(6)
        wait = self.diagnostic_wait()
        old_gen = self.w.task(self.tid)['input_gen']
        self.life.say(comp.APPROVER_A, 'Use a new provider outside the current plan.', mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'material'), comp.out('resolution', ref, 'Not unchanged authority.',
                        gen=gen, diagnostic=1, authority='unchanged'))
        self.w.step(lambda: self.w.task(self.tid)['scope_gen'] > old_gen, world=self.life.planner(script))
        self.scan_minutes(6)
        t = self.w.task(self.tid)
        self.assertEqual(t['scope_gen'], t['input_gen'])
        self.assertNotEqual(self.diagnostic_wait()['state'], 'answered')
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'superseded')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_R4_ambiguous_applied_notice_reconciles_even_if_parent_now_unreadable(self):
        card, _, key = self.queue_notice()
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=True)
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'ambiguous')
        count = self.w.cu.count('POST', path)
        self.b.cards[card]['parents'].append('t_deadbeef')
        self.scan_minutes(10)
        self.assertEqual(self.notice_rows()[0]['state'], 'done')
        self.assertEqual(self.w.cu.count('POST', path), count)

    def test_R4_final_native_IO_cannot_hide_a_new_operator_hold(self):
        self.queue_notice()
        real = main._diagnostic_reads
        def reads(*args):
            real(*args)
            conn = store_conn(self.w)
            conn.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
            conn.close()
        path = '/v2/task/%s/comment' % self.tid
        before = self.w.cu.count('POST', path)
        with mock.patch.object(main, '_diagnostic_reads', side_effect=reads):
            self.scan_minutes(5)
        self.assertEqual(self.w.cu.count('POST', path), before)
        self.assertEqual(self.notice_rows()[0]['state'], 'pending')

    def test_R5_five_minute_boundary_preserves_current_stale_unknown_distinctions(self):
        card, run = self.unknown_stop()
        light = self.w.task(self.tid)['last_light_ms']
        self.w.clock.now_ms = light + 300_000
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.w.clock.advance(.001)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 2)
        self.scan_minutes(6)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.b.cards[card]['task']['status'] = 'running'
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 2)

    def test_R3_new_approved_revision_supersedes_only_old_bound_wait(self):
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        old = self.diagnostic_wait()
        self.next_approval(2)
        self.scan_minutes(20)
        self.assertEqual(self.w.q('SELECT approval_rev FROM recoveries')[0]['approval_rev'], 2)
        self.assertEqual(self.diagnostic_wait()['state'], 'superseded')
        self.assertEqual(self.diagnostic_wait()['root'], old['root'])
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_R4_queued_notice_requires_current_approved_plan_evidence(self):
        self.queue_notice()
        req = self.life.req_cards()[-1]
        for run in self.b.cards[req]['runs']:
            md = run.get('metadata') or {}
            if 'plan_revision' in md:
                md['plan_revision'] = 99
        path = '/v2/task/%s/comment' % self.tid
        count = self.w.cu.count('POST', path)
        self.scan_minutes(6)
        [notice] = self.notice_rows()
        text = json.loads(notice['body'])['text']
        self.assertFalse(any(text in c['comment_text'] for c in self.w.bot_comments(self.tid)),
                         'different accepted revision is not current plan observation')
        self.assertEqual(notice['state'], 'pending')

    def test_R3_verified_human_wait_blocks_diagnostic_admission(self):
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        self.scan_minutes(6)
        before = list(self.b.mutations())
        rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertEqual((rc, lines), (3, ['STALE diagnostic_human_wait']))
        self.assertEqual(self.b.mutations(), before)

    def next_approval(self, revision):
        conn = store_conn(self.w)
        a = dict(conn.execute('SELECT * FROM approvals ORDER BY id DESC LIMIT 1').fetchone())
        conn.execute("UPDATE approvals SET state='revoked',revoked_reason='superseded' WHERE id=?", (a['id'],))
        a.pop('id')
        # Frozen approved-revision turnover uses fake typed review evidence, not production approval writes.
        req = self.b.worker_create('Accepted revision %d' % revision, assignee='planner')
        self.b.claim(req, 'planner')
        sha = self.b.request_review(req, self.tid, revision, '## ClickUp summary\nUnchanged authorized scope.\n', a['accounts_gen'])
        self.b.finish(req, 'plan-reviewer')
        seq = conn.execute("SELECT MAX(seq)+1 FROM cards WHERE task=? AND role='request'", (self.tid,)).fetchone()[0]
        conn.execute("INSERT INTO cards(card_id,task,role,seq) VALUES (?,?,'request',?)", (req, self.tid, seq))
        gates = json.loads(a['clickup_gates'])
        gates.update(plan_revision=revision, plan_sha256=sha)
        a.update(rev=revision, plan_sha256=sha, clickup_gates=json.dumps(gates), state='released',
                 revoked_reason=None, revoked_ms=None)
        conn.execute('INSERT INTO approvals(%s) VALUES (%s)' % (','.join(a), ','.join('?' for _ in a)), list(a.values()))
        conn.close()

    def test_R2_new_revision_after_verified_escalation_preserves_history_and_spent_budget(self):
        self.queue_notice()
        self.w.step(lambda: self.w.q('SELECT state FROM recoveries')[0]['state'] == 'escalated')
        [before] = self.w.q('SELECT * FROM recoveries')
        [old_notice] = self.notice_rows()
        self.next_approval(2)
        self.w.step(lambda: len(self.notice_rows()) == 2)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['approval_rev'], 2)
        self.assertEqual(after['abs_ms'], before['abs_ms'])
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.notice_rows()[0], old_notice, 'verified old delivery is history, not a reusable payload')
        self.assertEqual(len(self.notice_rows()), 2)

    def answered_status_input(self):
        before = self.w.task(self.tid)['input_gen']
        self.life.say(comp.APPROVER_A, 'Please give a status update.', mention=comp.BOT)
        def script(cid, ref, gen):
            self.b.park(cid, comp.out('answer', ref, 'Still safely stopped.'), comp.classify(ref, gen, 'answer_only'))
        self.w.step(lambda: self.w.task(self.tid)['input_gen'] > before and
                    self.w.task(self.tid)['classified_gen'] == self.w.task(self.tid)['input_gen'],
                    world=self.life.planner(script))

    def test_R2_answer_only_same_stop_keeps_budget_and_eventual_escalation(self):
        card, run = self.unknown_stop()
        [before] = self.w.q('SELECT * FROM recoveries')
        self.answered_status_input()
        self.scan_minutes(6)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'open', 'pending classification must not permanently supersede monitoring')
        for field in ('id', 'approval_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], before[field], field)
        self.assertNotEqual(after['reason_sha256'], before['reason_sha256'])
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'escalated')
        self.assertEqual(len(self.carriers()), 1)
