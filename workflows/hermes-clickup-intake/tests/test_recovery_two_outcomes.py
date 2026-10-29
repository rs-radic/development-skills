"""Current owner contract: no technical ACK or gate label is a completed recovery.

Real main/scan/progress/evaluate/execute; native/provider boundaries are offline fakes.
"""
import copy
import json
import unittest
from unittest import mock

import test_stalled_oauth_ack as oauth
import test_stalled_remediation as remediation
import test_s4_composition as comp
from clickup_intake import engine, kanban_port as kp
from test_recovery import store_conn


class LegacyQuestionTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids real subprocess/network'))
            p.start()
            self.addCleanup(p.stop)
        remediation.RemediationTest.setUp(self)

    scan_minutes = remediation.RemediationTest.scan_minutes
    carriers = remediation.RemediationTest.carriers
    notice_rows = remediation.RemediationTest.notice_rows
    question_ack = remediation.RemediationTest.question_ack
    diagnostic_wait = remediation.RemediationTest.diagnostic_wait

    def test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route(self):
        card = self.life.fe
        run = self.b.claim(card, 'e2e-tester')
        self.b.block(card, 'needs_input', 'A genuine requirements choice remains unresolved.')
        self.w.step(lambda: self.w.q('SELECT * FROM recoveries'))
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['class'], 'needs_input')
        question = self.question_ack(card, run)
        self.scan_minutes(6)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        [notice] = self.notice_rows()
        self.assertIn(question['decision'], json.loads(notice['body'])['text'],
                      'TWO_OUTCOME: legacy owner ACK must deliver the precise question, not a generic notice')
        self.assertEqual(notice['state'], 'done')
        wait = self.diagnostic_wait()
        self.assertEqual(wait['state'], 'waiting')
        self.assertEqual(wait['root'], notice['result_ref'])
        self.assertTrue(any(c['id'] == wait['root'] for c in self.w.bot_comments(self.tid)))
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.life.say(comp.APPROVER_A, 'Use existing provider A.', mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'Use existing provider A within unchanged authority.',
                                 gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'answered', world=self.life.planner(script))
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked', 'classification is not native release')
        self.assertEqual(self.w.q('SELECT id FROM blockers'), [])


class ActualCannotActTest(unittest.TestCase):
    setUp = oauth.OAuthDiagnosticAckTest.setUp
    cycle = oauth.OAuthDiagnosticAckTest.cycle
    snapshot = oauth.OAuthDiagnosticAckTest.snapshot
    notices = oauth.OAuthDiagnosticAckTest.notices

    def test_later_working_ack_retains_original_defer_without_refunding_deadline(self):
        self.tid = oauth.TID
        self.b.worker_comment(oauth.CARD, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=1263 disposition=working' % oauth.CARD)
        self.w.clock.now_ms = self.before['incident']['next_ms'] + 1
        self.cycle(3)
        inc = self.snapshot()['incident']
        self.assertEqual(inc['state'], 'open')
        self.assertEqual(inc['attempts'], 2, 'an intermediate cannot_act must not discard a later working defer')
        self.assertEqual(inc['abs_ms'], self.before['incident']['abs_ms'])

    def test_actual_cannot_act_keeps_same_work_owned_and_uses_remaining_original_wake(self):
        self.cycle(3)
        self.assertEqual(self.snapshot()['incident']['state'], 'open',
                         'TWO_OUTCOME: technical inability cannot finalize the actual OAuth recovery')
        self.assertEqual(self.notices(), [], 'a canned technical notice is not a genuine human question')
        self.w.clock.now_ms = self.before['incident']['next_ms'] + 1
        self.cycle(3)
        inc = self.snapshot()['incident']
        self.assertEqual(inc['attempts'], 3, 'cannot_act must not suppress the remaining approved owner wake')
        for field in ('id', 'run_id', 'approval_id', 'abs_ms', 'detected_ms'):
            self.assertEqual(inc[field], self.before['incident'][field])
        carriers = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND idem_key LIKE '%:rcv:%'")
        self.assertTrue(any(r['state'] == 'done' and json.loads(r['body'])['params']['attempt'] == 3 for r in carriers))
        [wake] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%:3:wake'")
        self.assertEqual(wake['state'], 'done', 'the real final gate must actually deliver the remaining owner wake')
        self.assertFalse(any(m[1] in (oauth.CARD, oauth.QUESTION) for m in self.b.mutations()))


class OwnedContinuationTest(LegacyQuestionTest):
    # Do not inherit the question test into this fixture's test inventory.
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None

    def stopped(self):
        card = self.life.fe
        run = self.b.claim(card, 'e2e-tester')
        self.b.block(card, 'capability', 'Existing approved setup requires the main operator.')
        self.w.step(lambda: self.w.q('SELECT * FROM recoveries'))
        return card, run

    def test_completed_new_run_is_observed_closed_not_left_waiting_for_a_dead_process(self):
        card, run = self.stopped()
        self.b.unblock(card)
        self.b.claim(card, self.b.cards[card]['task']['assignee'])
        self.b.finish(card, self.b.cards[card]['task']['assignee'])
        self.scan_minutes(6)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['state'], 'closed', 'a completed actual successor run is native completion, not a live-worker claim')

    def test_gate_only_without_capable_owner_cannot_complete_recovery(self):
        card, run = self.stopped()
        parent = self.b._new('Existing approved dependency', assignee='integrator', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.b.unblock(card)
        self.scan_minutes(6)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertIn(inc['state'], ('open', 'handed_off'),
                      'TWO_OUTCOME: TODO behind a gate is not actual owned continuation')
        self.assertNotEqual(inc['state'], 'gated')
        self.assertEqual(self.notice_rows(), [])

    def test_new_claim_with_dead_generation_cannot_complete_recovery(self):
        card, run = self.stopped()
        self.b.unblock(card)
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        with mock.patch.object(kp, 'process_start_time', return_value=None) as failed_probe:
            self.scan_minutes(6)
        self.assertTrue(failed_probe.called, 'exercise failed native process proof, not merely an old claim label')
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertIn(inc['state'], ('open', 'handed_off'),
                      'TWO_OUTCOME: a recorded claim is not a live process generation')
        self.assertEqual(self.notice_rows(), [], 'normal pickup grace must not ask prematurely')
        self.w.clock.now_ms = inc['abs_ms'] + 1
        with mock.patch.object(kp, 'process_start_time', return_value=None):
            self.scan_minutes(6)
        wait = self.diagnostic_wait()
        self.assertIsNotNone(wait, 'SPEC8-B1b: a dead successor must not escape the original deadline')
        [notice] = self.notice_rows()
        self.assertEqual((wait['state'], notice['state'], wait['root']),
                         ('waiting', 'done', notice['result_ref']))
        self.assertEqual(wait['question']['run'], new_run, 'question must name the observed claim, not stopped old run')
        self.assertIn('run %d' % new_run, wait['question']['decision'])
        self.assertIn(wait['question']['decision'], next(c['comment_text'] for c in self.w.bot_comments(self.tid)
                                                       if c['id'] == wait['root']))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(new_run)),
                         (3, ['STALE diagnostic_human_wait']))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE newer_run']))
        self.scan_minutes(6)
        self.assertEqual(len(self.notice_rows()), 1)
        original_approval = self.w.q('SELECT * FROM approvals WHERE id=?', inc['approval_id'])[0]
        self.life.say(comp.APPROVER_A, 'The existing authorized operator can inspect this failed worker and take over.',
                      mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'The existing authorized operator can take over.',
                                 gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: self.diagnostic_wait()['state'] == 'answered', world=self.life.planner(script))
        self.assertEqual(self.b.cards[card]['task']['current_run_id'], new_run, 'reply does not clear a native claim')
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(new_run)),
                         (3, ['STALE open_run']))
        self.assertEqual(self.w.q('SELECT * FROM approvals WHERE id=?', inc['approval_id'])[0]['state'],
                         original_approval['state'])
        self.b.block(card, 'capability', 'Operator stops the failed simulated worker before approved takeover.')
        self.b.unblock(card)
        next_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task.update(current_run_id=next_run, worker_pid=4242)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        with mock.patch.object(kp, 'process_start_time', return_value=start):
            self.w.clock.advance(301)
            task['last_heartbeat_at'] = self.w.clock() // 1000
            self.assertEqual(self.w.run()[0], 0)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'resumed')
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], inc[field], field)

    def test_fresh_same_owner_process_generation_is_actual_continuation(self):
        card, run = self.stopped()
        self.b.unblock(card)
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        started = self.b.cards[card]['runs'][-1]['started_at']
        # Existing native Windows process-probe boundary, not manufactured engine observations.
        native_start = str((started + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return native_start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(6)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['state'], 'resumed')
        self.assertEqual(self.notice_rows(), [])


class ActualApprovedIncorporationTest(unittest.TestCase):
    cycle = oauth.OAuthDiagnosticAckTest.cycle
    snapshot = oauth.OAuthDiagnosticAckTest.snapshot
    notices = oauth.OAuthDiagnosticAckTest.notices

    def setUp(self):
        oauth.OAuthDiagnosticAckTest.setUp(self)
        # The original capture omitted relays. These identifying fields are independently captured
        # in oauth-resolution-investigation-7.json; item_ids is unused, not a synthesized resolution.
        conn = store_conn(self.w)
        conn.execute("INSERT INTO relays(relay_ref,task,card,gen,state,classification) VALUES (?,?,?,?,?,?)",
                     ('86pub00003-s3-g4-w1271', oauth.TID, 't_ab00009b', 4, 'classified', 'material'))
        conn.close()
        show = self.b.show
        def native_readback(card):
            result = show(card)
            if isinstance(result.json, dict):
                completed = [e for e in result.json.get('events', []) if e['kind'] == 'completed']
                if completed:
                    result.json['latest_summary'] = completed[-1]['payload']['summary']
            return result
        self.b.show = native_readback

    def test_actual_material_without_resolution_uses_exact_approved_plan_and_normal_delivery(self):
        before = self.w.q('SELECT * FROM blockers WHERE id=6')[0]
        self.assertTrue(all(before[k] is None for k in ('resolution_relay', 'resolution_card', 'resolution_gen',
                                                        'resolution_body', 'resolution_authority')))
        self.cycle(4)
        rows = self.w.q('SELECT * FROM amendment_answers WHERE blocker_id=6')
        self.assertEqual(len(rows), 1,
                         'OAUTH_APPROVED_INCORPORATION: exact current approved marker must own missing bookkeeping')
        self.assertEqual(rows[0]['state'], 'applied')
        after = self.w.q('SELECT * FROM blockers WHERE id=6')[0]
        self.assertEqual(after['approval_id'], 18, 'preserve original blocker provenance')
        self.assertEqual((after['resolution_gen'], after['resolution_card'], after['resolution_relay']),
                         (4, 't_ab00009b', '86pub00003-s3-g4-w1271'))
        self.assertEqual(after['resolution_authority'], 'unchanged')
        self.assertEqual(after['delivered_gen'], 4)
        for token in (oauth.TID, oauth.CARD, oauth.QUESTION, 'approval=20', 'r6',
                      self.before['incident']['plan_sha256'], '9900000000000037', 't_ab00009b'):
            self.assertIn(token, after['resolution_body'])
        self.assertEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')
        [answer] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND target=?", oauth.QUESTION)
        self.assertEqual(answer['state'], 'done', 'normal exact-summary readback must succeed')
        done = [m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)]
        self.assertEqual(len(done), 1)
        self.assertEqual(self.notices(), [], 'already approved decisions must not become another human question')
        # Normal capable-owner pickup is an explicit fake native worker event, not a poller write.
        self.b._run = 1263
        self.b.claim(oauth.CARD, 'implementor')
        task = self.b.cards[oauth.CARD]['task']
        task.update(current_run_id=1264, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        started = self.b.cards[oauth.CARD]['runs'][-1]['started_at']
        with mock.patch.object(kp, 'process_start_time', return_value=str((started + 11644473600) * 10000000)):
            self.w.clock.advance(301)
            task['last_heartbeat_at'] = self.w.clock() // 1000
            self.assertEqual(self.w.run()[0], 0)
        self.assertEqual(self.snapshot()['incident']['state'], 'resumed')
        self.cycle(2)
        self.assertEqual(self.w.q('SELECT * FROM amendment_answers WHERE blocker_id=6'), rows)
        self.assertEqual(len([m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)]), 1)


class ReadyHandoffMonitorTest(ActualApprovedIncorporationTest):
    test_actual_material_without_resolution_uses_exact_approved_plan_and_normal_delivery = None

    def test_actual_run1263_does_not_admit_arbitrary_zero_run_probe(self):
        before = self.snapshot()['incident']
        self.assertEqual(before['run_id'], 1263)
        self.assertEqual(self.w.main('recovery-check', '--card', oauth.CARD, '--run', '0'),
                         (3, ['STALE no_incident']))
        self.assertEqual(self.snapshot()['incident'], before)
        self.assertEqual(self.notices(), [])

    def ready_from_actual_question(self):
        self.cycle(4)
        self.assertEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')
        self.assertEqual(self.b.cards[oauth.CARD]['task']['status'], 'ready')
        self.assertEqual(self.b.cards[oauth.CARD]['runs'][-1]['id'], 1263)
        self.assertEqual(self.w.q('SELECT delivered_gen FROM blockers WHERE id=6')[0]['delivered_gen'], 4)
        [answer] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND target=?", oauth.QUESTION)
        self.assertEqual(answer['state'], 'done')
        self.assertEqual(self.notices(), [])
        return self.snapshot()['incident']

    def assert_bound_resource_wait(self, before, run):
        waits = self.snapshot()['waits']
        self.assertEqual(len(waits), 1,
                         'SPEC8-B1a: actual completed-Q READY without pickup must reach a genuine resource question')
        wait = json.loads(waits[0]['value'])
        [notice] = self.notices()
        self.assertEqual((wait['state'], notice['state'], wait['root']),
                         ('waiting', 'done', notice['result_ref']))
        self.assertEqual(wait['question']['run'], run)
        self.assertEqual(wait['question']['card'], oauth.CARD)
        self.assertIn('existing authorized operator', wait['question']['decision'])
        self.assertIn(wait['question']['decision'], next(c['comment_text'] for c in self.w.bot_comments(oauth.TID)
                                                       if c['id'] == wait['root']))
        after = self.snapshot()['incident']
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(len(self.w.q('SELECT * FROM recoveries')), 1)
        return wait

    def test_ready_handoff_keeps_first_grace_and_original_deadline(self):
        before = self.ready_from_actual_question()
        self.assertEqual(before['state'], 'handed_off')
        grace = before['escalate_ms']
        self.assertLessEqual(grace, before['abs_ms'])
        self.cycle(2)
        self.assertEqual(self.snapshot()['incident']['escalate_ms'], grace)
        self.assertEqual(self.notices(), [])
        self.w.clock.now_ms = grace + 1
        self.cycle(3)
        self.assert_bound_resource_wait(before, 1263)

    def test_ready_unknown_parent_beyond_deadline_defers_then_normal_observation_delivers(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        before = self.ready_from_actual_question()
        self.w.clock.now_ms = before['abs_ms'] + 1
        read = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                               KanbanResult(Outcome.DEFINITE_FAIL) if card == 't_ab000066' else read(card)):
            self.cycle(3)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['abs_ms'], before['abs_ms'])
        self.cycle(3)
        self.assert_bound_resource_wait(before, 1263)

    def test_ready_queued_notice_cannot_ignore_new_parent(self):
        from clickup_intake import main
        before = self.ready_from_actual_question()
        self.w.clock.now_ms = before['abs_ms'] + 1
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.cycle(2)
        self.assertEqual(len(self.notices()), 1)
        parent = self.b._new('Required dependency', status='blocked')
        self.b.cards[oauth.CARD]['parents'].append(parent)
        self.cycle(3)
        self.assertFalse(any(n['state'] == 'done' for n in self.notices()))
        self.assertFalse(any(json.loads(w['value']).get('root') for w in self.snapshot()['waits']))

    def test_ready_operator_abandon_prevents_resend_after_deadline(self):
        from clickup_intake.contracts import Outcome
        before = self.ready_from_actual_question()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.w.cu.inject('POST', '/v2/task/%s/comment' % oauth.TID, Outcome.AMBIGUOUS, commit=False)
        self.cycle(8)
        [notice] = self.notices()
        self.assertEqual(notice['state'], 'held_ambiguous')
        self.assertEqual(self.w.main('outbox', 'abandon', notice['idem_key'])[0], 0)
        self.cycle(4)
        self.assertEqual(self.notices()[0]['outcome'], 'ABANDONED')
        self.assertFalse(self.notices()[0]['result_ref'])
        self.assertEqual(self.snapshot()['incident']['attempts'], before['attempts'])

    def test_ready_foreign_claim_cannot_receive_takeover_question(self):
        before = self.ready_from_actual_question()
        self.b._run = 1263
        run = self.b.claim(oauth.CARD, 'foreign-owner')
        self.b.cards[oauth.CARD]['task'].update(current_run_id=run, worker_pid=4242)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(4)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'handed_off')

    def test_ready_revoked_authority_cannot_receive_takeover_question(self):
        before = self.ready_from_actual_question()
        conn = store_conn(self.w)
        conn.execute("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=20")
        conn.close()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(4)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'superseded')

    def test_ready_unclassified_input_cannot_receive_takeover_question(self):
        before = self.ready_from_actual_question()
        conn = store_conn(self.w)
        conn.execute('UPDATE tasks SET input_gen=5 WHERE clickup_task_id=?', (oauth.TID,))
        conn.close()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(4)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'handed_off')

    def test_takeover_question_rebinds_new_unverified_claim_without_budget_refund(self):
        before = self.ready_from_actual_question()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(3)
        old = self.assert_bound_resource_wait(before, 1263)
        self.b._run = 1263
        run = self.b.claim(oauth.CARD, 'implementor')
        self.b.cards[oauth.CARD]['task'].update(current_run_id=run, worker_pid=4242)
        self.cycle(4)
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual(wait['question']['run'], run, 'a superseded question cannot exempt the successor from monitoring')
        self.assertEqual(wait['state'], 'waiting')
        self.assertNotEqual(wait['root'], old['root'])
        self.assertEqual(self.snapshot()['incident']['abs_ms'], before['abs_ms'])
        self.assertEqual(self.snapshot()['incident']['attempts'], before['attempts'])
        self.assertEqual(self.w.main('recovery-check', '--card', oauth.CARD, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_actual_ready_no_pickup_beyond_original_deadline_delivers_and_accepts_bound_reply(self):
        before = self.ready_from_actual_question()
        original_comments = list(self.b.cards[oauth.CARD]['comments'])
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(4)  # real main, fresh fake provider/native observations, no injected worker/ACK
        wait = self.assert_bound_resource_wait(before, 1263)
        self.assertEqual(self.b.cards[oauth.CARD]['comments'], original_comments)
        self.cycle(2)
        self.assertEqual(len(self.notices()), 1)
        life = object.__new__(comp.Lifecycle)
        life.w, life.b, life.cu, life.tid = self.w, self.b, self.w.cu, oauth.TID
        life.planner_turns, life.plan_sha = 0, None
        approval = self.w.q('SELECT * FROM approvals WHERE id=20')[0]
        life.say(comp.APPROVER_A, 'The existing authorized operator is available to pick up this approved work.',
                 mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'The existing authorized operator is available.',
                                 gen=gen, diagnostic=before['id'], authority='unchanged'))
        self.w.step(lambda: json.loads(self.snapshot()['waits'][0]['value'])['state'] == 'answered',
                    world=life.planner(script))
        self.assertEqual(self.w.q('SELECT * FROM approvals WHERE id=20')[0]['state'], approval['state'])
        self.assertEqual(self.b.cards[oauth.CARD]['task']['status'], 'ready', 'reply never releases/claims work')
        self.b._run = 1263
        new_run = self.b.claim(oauth.CARD, 'implementor')  # explicit fake operator pickup, not intake mutation
        task = self.b.cards[oauth.CARD]['task']
        task.update(current_run_id=new_run, worker_pid=4242)
        start = str((self.b.cards[oauth.CARD]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        with mock.patch.object(kp, 'process_start_time', return_value=start):
            for _ in range(2):
                self.w.clock.advance(301)
                task['last_heartbeat_at'] = self.w.clock() // 1000
                self.assertEqual(self.w.run()[0], 0)
        self.assertEqual(self.snapshot()['incident']['state'], 'resumed')
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.snapshot()['incident']['abs_ms'], before['abs_ms'])


class ReviewHandoffAnchorTest(LegacyQuestionTest):
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None

    def released_review(self):
        card = self.life.fe
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        self.b.cards[card]['events'][-1]['payload']['source_status'] = 'review'
        self.b.block(card, 'unknown_review_stop', 'Review worker needs the existing recovery operator.')
        self.b.cards[card]['events'][-1]['payload']['source_status'] = 'review'
        self.scan_minutes(18)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((before['class'], before['run_id'], before['attempts']), ('diagnostic', run, 1))
        # Explicit fake operator action. Native unblock restores its recorded review lane;
        # the shared fake otherwise only models work-lane READY, so project that native result.
        self.assertEqual(self.b.unblock(card).outcome.value, 'OK')
        self.b.cards[card]['task']['status'] = 'review'
        shown = self.b.show(card).json
        self.assertEqual(kp.recovery_outcome(shown, run, before['event_created_s'])[0], 'handed_off')
        self.assertEqual(kp.claimed_runs(shown)[run], 'review')
        self.evidence = {'admitted': copy.deepcopy(before), 'released_native': copy.deepcopy(shown)}
        return card, run, before

    def assert_resource_wait(self, card, run, before):
        wait = self.diagnostic_wait()
        self.assertIsNotNone(wait, 'SPEC9-B1c/B1d: admitted handoff must remain bounded past original deadline')
        [notice] = self.notice_rows()
        self.assertEqual((wait['state'], notice['state'], wait['root']), ('waiting', 'done', notice['result_ref']))
        self.assertEqual((wait['question']['card'], wait['question']['run']), (card, run))
        self.assertIn('existing authorized operator', wait['question']['decision'])
        self.assertIn(wait['question']['decision'], next(c['comment_text'] for c in self.w.bot_comments(self.tid)
                                                       if c['id'] == wait['root']))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))
        [after] = self.w.q('SELECT * FROM recoveries')
        for field in ('id', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.evidence['wait'] = copy.deepcopy(wait)
        return wait

    def answer_then_pickup(self, card, before, wait, dead_run=None):
        native = copy.deepcopy(self.b.show(card).json)
        approval = self.w.q('SELECT * FROM approvals WHERE id=?', before['approval_id'])[0]
        self.life.say(comp.APPROVER_A, 'The existing authorized operator is available for this approved work.',
                      mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'The existing authorized operator is available.',
                                 gen=gen, diagnostic=before['id'], authority='unchanged'))
        self.w.step(lambda: self.diagnostic_wait()['state'] == 'answered', world=self.life.planner(script))
        self.assertEqual(self.b.show(card).json, native, 'reply must not clear claims or release native work')
        self.assertEqual(self.w.q('SELECT * FROM approvals WHERE id=?', before['approval_id'])[0]['state'], approval['state'])
        if dead_run is not None:
            self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(dead_run)),
                             (3, ['STALE open_run']))
            self.b.block(card, 'capability', 'Explicit fake operator stops the failed process before takeover.')
            self.b.unblock(card)
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        with mock.patch.object(kp, 'process_start_time', return_value=start):
            self.w.clock.advance(301)
            task['last_heartbeat_at'] = self.w.clock() // 1000
            self.assertEqual(self.w.run()[0], 0)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'resumed')
        for field in ('id', 'run_id', 'attempts', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], before[field], field)
        self.evidence['resumed'] = copy.deepcopy(after)

    def test_review_handoff_preserves_original_grace(self):
        card, run, before = self.released_review()
        self.scan_minutes(6)
        [observed] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(observed['state'], 'handed_off')
        self.assertLessEqual(observed['escalate_ms'], before['abs_ms'])
        self.scan_minutes(6)
        self.assertEqual(self.w.q('SELECT escalate_ms FROM recoveries')[0]['escalate_ms'], observed['escalate_ms'])
        self.assertEqual(self.notice_rows(), [])
        self.w.clock.now_ms = observed['escalate_ms'] + 1
        self.scan_minutes(6)
        self.assert_resource_wait(card, run, before)

    def test_review_without_native_release_is_not_a_handoff(self):
        card, run, before = self.released_review()
        self.b.cards[card]['events'] = [e for e in self.b.cards[card]['events'] if e['kind'] != 'unblocked']
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [])
        self.assertIsNone(self.diagnostic_wait())

    def test_review_handoff_past_original_deadline_has_verified_question_reply_and_real_pickup(self):
        card, run, before = self.released_review()
        native = copy.deepcopy(self.b.show(card).json)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        wait = self.assert_resource_wait(card, run, before)
        self.assertEqual(self.b.show(card).json, native, 'monitoring may not release or assign REVIEW')
        self.scan_minutes(6)
        self.assertEqual(len(self.notice_rows()), 1)
        self.answer_then_pickup(card, before, wait)


class ZeroRunHandoffAnchorTest(LegacyQuestionTest):
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None
    assert_resource_wait = ReviewHandoffAnchorTest.assert_resource_wait
    answer_then_pickup = ReviewHandoffAnchorTest.answer_then_pickup

    def missing_worker(self, created_event=True):
        import test_stalled_fallback as fallback
        card, question, _ = fallback.StalledFallbackTest.incorporated_question(self)
        # Exactly the existing accepted missing-worker fixture: no native claimed run zero.
        created = self.w.clock() // 1000
        self.b.cards[card]['runs'] = []
        self.b.cards[card]['task']['created_at'] = created
        self.b.cards[card]['events'] = ([{'kind': 'created', 'created_at': created,
                                         'run_id': None, 'payload': {'status': 'todo'}}] if created_event else [])
        self.scan_minutes(18)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((before['class'], before['run_id'], before['attempts']), ('diagnostic', 0, 1))
        self.assertEqual(before['event_created_s'], created)
        self.assertEqual(self.b.cards[card]['runs'], [])
        self.assertEqual(kp.claimed_runs(self.b.show(card).json), {})
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', '0')[0], 0)
        self.assertEqual(self.b.cards[question]['task']['status'], 'blocked')
        self.evidence = {'admitted': copy.deepcopy(before), 'no_worker_native': copy.deepcopy(self.b.show(card).json)}
        # Explicit fake authorized operator completes the already-approved dependency, using its
        # ordinary completion/regating. The intake never creates a claimed run or clears a claim.
        self.assertEqual(self.b.complete(question, 'Existing approved decision is available.').outcome.value, 'OK')
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        self.assertEqual(self.b.cards[card]['runs'], [])
        self.evidence['ready_native'] = copy.deepcopy(self.b.show(card).json)
        return card, question, before

    def test_zero_run_healthy_first_worker_is_progress_without_question(self):
        card, question, before = self.missing_worker()
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        with mock.patch.object(kp, 'process_start_time', return_value=start):
            self.w.clock.advance(301)
            task['last_heartbeat_at'] = self.w.clock() // 1000
            self.assertEqual(self.w.run()[0], 0)
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'resumed')
        self.assertEqual(self.notice_rows(), [])

    def test_zero_run_foreign_claim_stays_fail_closed_after_deadline(self):
        card, question, before = self.missing_worker()
        run = self.b.claim(card, 'foreign-owner')
        self.b.cards[card]['task'].update(current_run_id=run, worker_pid=4242)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [])
        self.assertIsNone(self.diagnostic_wait())
        self.assertEqual(self.w.q('SELECT abs_ms FROM recoveries')[0]['abs_ms'], before['abs_ms'])

    def test_zero_run_changed_creation_anchor_stays_fail_closed_after_deadline(self):
        card, question, before = self.missing_worker()
        self.b.cards[card]['task']['created_at'] += 1
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [])
        self.assertIsNone(self.diagnostic_wait())

    def test_zero_run_unknown_parent_defers_then_observation_reaches_original_deadline(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        card, question, before = self.missing_worker()
        self.w.clock.now_ms = before['abs_ms'] + 1
        read = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda cid:
                               KanbanResult(Outcome.DEFINITE_FAIL) if cid == question else read(cid)):
            self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [])
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        self.assert_resource_wait(card, 0, before)

    def test_zero_run_queued_question_rechecks_new_parent(self):
        from clickup_intake import main
        card, question, before = self.missing_worker()
        self.w.clock.now_ms = before['abs_ms'] + 1
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.scan_minutes(6)
        self.assertEqual(len(self.notice_rows()), 1)
        parent = self.b._new('Required dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.scan_minutes(6)
        self.assertFalse(any(n['state'] == 'done' for n in self.notice_rows()))
        self.assertFalse(self.diagnostic_wait().get('root'))

    def test_zero_run_delivered_wait_rebinds_first_dead_claim_with_spent_send_history(self):
        card, question, before = self.missing_worker()
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        old = self.assert_resource_wait(card, 0, before)
        [first] = self.notice_rows()
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        self.b.cards[card]['task'].update(current_run_id=run, worker_pid=4242,
                                         last_heartbeat_at=self.w.clock() // 1000)
        with mock.patch.object(kp, 'process_start_time', return_value=None):
            self.w.step(lambda: self.diagnostic_wait().get('state') == 'waiting'
                        and self.diagnostic_wait()['question']['run'] == run)
        wait = self.diagnostic_wait()
        self.assertEqual((wait['state'], wait['question']['run']), ('waiting', run))
        self.assertNotEqual(wait['root'], old['root'])
        notices = self.notice_rows()
        self.assertEqual([n['attempts'] for n in notices], [1, 2])
        self.assertEqual([n['first_send_ms'] for n in notices], [first['first_send_ms']] * 2)
        history = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait_history:%'")
        self.assertTrue(any(json.loads(h['value']).get('root') == old['root'] for h in history))
        [after] = self.w.q('SELECT * FROM recoveries')
        for field in ('id', 'run_id', 'attempts', 'detected_ms', 'abs_ms', 'reason_sha256'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', '0'), (3, ['STALE newer_run']))
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))
        self.evidence['old_wait'] = copy.deepcopy(old)
        self.evidence['rebound_wait'] = copy.deepcopy(wait)
        self.evidence['notices'] = copy.deepcopy(notices)

    def test_zero_run_observed_empty_events_uses_the_admitted_creation_anchor(self):
        card, question, before = self.missing_worker(created_event=False)
        self.assertEqual(self.b.cards[card]['events'], [])
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        self.assert_resource_wait(card, 0, before)

    def test_zero_run_first_dead_claim_past_deadline_binds_observed_run_and_never_clears_claim(self):
        card, question, before = self.missing_worker()
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        self.assertGreater(run, 0)
        self.assertEqual(len(self.b.cards[card]['runs']), 1)
        self.evidence['first_claim'] = copy.deepcopy(self.b.show(card).json)
        with mock.patch.object(kp, 'process_start_time', return_value=None) as failed_probe:
            self.assertEqual(self.w.run()[0], 0)
        self.assertTrue(failed_probe.called, 'the actual current first claim has failed process-generation proof')
        self.assertEqual(self.notice_rows(), [], 'first claim retains its pickup grace')
        self.w.clock.now_ms = before['abs_ms'] + 1
        with mock.patch.object(kp, 'process_start_time', return_value=None):
            self.scan_minutes(6)
        wait = self.assert_resource_wait(card, run, before)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', '0'),
                         (3, ['STALE newer_run']))
        self.assertEqual(self.b.show(card).json, self.evidence['first_claim'])
        self.answer_then_pickup(card, before, wait, dead_run=run)

    def test_zero_run_ready_past_original_deadline_has_verified_question_reply_and_first_pickup(self):
        card, question, before = self.missing_worker()
        native = copy.deepcopy(self.b.show(card).json)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scan_minutes(6)
        wait = self.assert_resource_wait(card, 0, before)
        self.assertEqual(self.b.show(card).json, native)
        self.scan_minutes(6)
        self.assertEqual(len(self.notice_rows()), 1)
        self.answer_then_pickup(card, before, wait)


class DeadlineOwnershipTest(unittest.TestCase):
    setUp = oauth.OAuthDiagnosticAckTest.setUp
    cycle = oauth.OAuthDiagnosticAckTest.cycle
    snapshot = oauth.OAuthDiagnosticAckTest.snapshot
    notices = oauth.OAuthDiagnosticAckTest.notices

    def test_later_owner_ack_cannot_replace_delivered_exhaustion_question(self):
        self.w.clock.now_ms = self.before['incident']['abs_ms'] + 1
        self.cycle(4)
        [old] = self.notices()
        self.assertEqual(old['state'], 'done')
        wait_before = json.loads(self.snapshot()['waits'][0]['value'])
        self.tid = oauth.TID
        remediation.RemediationTest.question_ack(
            self, oauth.CARD, 1263,
            failed='The authorized credential capability is unavailable to the existing operator.',
            attempted='Inspected the supported secure credential capability metadata without reading secret values.',
            decision='The credential owner must provide access to the existing approved identity-provider capability.',
            next_action='Provision that capability through the existing secure channel, never in this task, then confirm here.')
        self.cycle(4)
        self.assertEqual(self.notices(), [old], 'a delivered unanswered question cannot be replaced by a later ACK')
        self.assertEqual(json.loads(self.snapshot()['waits'][0]['value']), wait_before)
        self.assertEqual(self.snapshot()['incident']['abs_ms'], self.before['incident']['abs_ms'])
        self.assertEqual(self.snapshot()['incident']['attempts'], self.before['incident']['attempts'])

    def test_deadline_delivery_failure_is_not_a_human_wait_and_retries_same_budget(self):
        from clickup_intake.contracts import Outcome
        path = '/v2/task/%s/comment' % oauth.TID
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.clock.now_ms = self.before['incident']['abs_ms'] + 1
        self.cycle()
        [notice] = self.notices()
        self.assertEqual((notice['state'], notice['attempts']), ('retry_wait', 1))
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual(wait['state'], 'publishing')
        self.assertFalse(wait.get('root'), 'a failed POST is not an answerable human wait')
        self.cycle(5)
        [notice] = self.notices()
        self.assertEqual((notice['state'], notice['attempts']), ('done', 2))
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual((wait['state'], wait['root']), ('waiting', notice['result_ref']))
        self.assertEqual(self.snapshot()['incident']['attempts'], self.before['incident']['attempts'])
        self.assertEqual(self.snapshot()['incident']['abs_ms'], self.before['incident']['abs_ms'])

    def test_deadline_all_sends_fail_honestly_without_claiming_wait_or_new_wakes(self):
        from clickup_intake.contracts import Outcome
        path = '/v2/task/%s/comment' % oauth.TID
        for _ in range(6):
            self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.clock.now_ms = self.before['incident']['abs_ms'] + 1
        self.cycle(14)
        [notice] = self.notices()
        self.assertEqual((notice['state'], notice['attempts']), ('failed', 6))
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual(wait['state'], 'publishing')
        self.assertFalse(wait.get('root'))
        self.assertTrue(any('failed after 6 attempts' in line for line in self.w.outputs))
        before = self.w.cu.count('POST', path)
        self.cycle(4)
        self.assertEqual(self.w.cu.count('POST', path), before)
        self.assertEqual(self.notices(), [notice])
        self.assertEqual(self.snapshot()['incident']['attempts'], self.before['incident']['attempts'])

    def test_deadline_ambiguous_delivery_and_operator_abandon_are_not_automatic_resend(self):
        from clickup_intake.contracts import Outcome
        path = '/v2/task/%s/comment' % oauth.TID
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.w.clock.now_ms = self.before['incident']['abs_ms'] + 1
        self.cycle(8)
        [notice] = self.notices()
        self.assertEqual(notice['state'], 'held_ambiguous')
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual(wait['state'], 'publishing')
        self.assertFalse(wait.get('root'))
        self.assertEqual(self.w.main('outbox', 'abandon', notice['idem_key'])[0], 0)
        before = self.w.cu.count('POST', path)
        self.cycle(4)
        self.assertEqual(self.w.cu.count('POST', path), before)
        self.assertEqual(self.notices()[0]['outcome'], 'ABANDONED')
        self.assertEqual(self.snapshot()['incident']['attempts'], self.before['incident']['attempts'])

    def test_deadline_technical_notice_never_finalizes_without_a_genuine_question_or_work(self):
        # SPEC7-B1 supersedes the old OPEN+stdout oracle. No injected owner ACK is allowed.
        comments = list(self.b.cards[oauth.CARD]['comments'])
        self.w.clock.now_ms = self.before['incident']['abs_ms'] + 1
        self.cycle(4)
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.assertEqual(len(self.snapshot()['waits']), 1,
                         'SPEC7-B1: deadline must obtain a delivered, answerable resource handoff, not OPEN+stdout')
        wait = json.loads(self.snapshot()['waits'][0]['value'])
        [notice] = self.notices()
        self.assertEqual((wait['state'], notice['state']), ('waiting', 'done'))
        self.assertEqual(wait['root'], notice['result_ref'])
        delivered = next(c for c in self.w.bot_comments(oauth.TID) if c['id'] == wait['root'])
        self.assertIn(wait['question']['decision'], delivered['comment_text'])
        self.assertIn(oauth.CARD, wait['question']['decision'])
        self.assertIn('existing authorized operator', wait['question']['decision'])
        self.assertIn('No new product approval', delivered['comment_text'])
        self.assertEqual(self.b.cards[oauth.CARD]['comments'], comments, 'intake cannot forge an owner ACK')
        after = self.snapshot()['incident']
        self.cycle(4)
        self.assertEqual(self.snapshot()['incident'], after)
        self.assertEqual(len(self.notices()), 1)
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], self.before['incident'][field], field)
        self.assertEqual(self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%'"), [])


class ExhaustionRouteTest(LegacyQuestionTest):
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None
    stopped = OwnedContinuationTest.stopped

    def test_unowned_handoff_grace_exhaustion_delivers_bound_resource_question(self):
        card = self.life.fe
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        self.b.block(card, 'capability', 'Existing approved setup requires the main operator.')
        self.w.step(lambda: self.w.q('SELECT * FROM recoveries'))
        self.b.unblock(card)
        self.scan_minutes(6)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(before['state'], 'handed_off')
        released_s = next(e['created_at'] for e in self.b.cards[card]['events'] if e['kind'] == 'unblocked')
        self.assertEqual(before['escalate_ms'], released_s * 1000 + engine.RECOVERY_DEFER_MIN * 60_000,
                         'monitoring must retain the original observed release grace')
        self.w.clock.now_ms = before['escalate_ms'] + 1
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertIn(card, wait['question']['decision'])
        self.assertEqual(wait['root'], self.notice_rows()[0]['result_ref'])
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)
        [after] = self.w.q('SELECT * FROM recoveries')
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], before[field], field)

    def test_handoff_question_waits_for_fresh_required_parent_before_dispatch(self):
        from clickup_intake import main
        card, run = self.stopped()
        self.b.unblock(card)
        self.scan_minutes(6)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.w.clock.now_ms = inc['escalate_ms'] + 1
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        count = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        self.b.cards[card]['parents'].append('t_deadbeef')
        self.scan_minutes(6)
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid), count)
        self.assertFalse(self.diagnostic_wait().get('root'))
        self.b.cards[card]['parents'].remove('t_deadbeef')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid), count + 1)
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)

    def test_finite_exhaustion_handoff_reply_resumes_via_existing_authorized_path(self):
        card, run = self.stopped()
        [before] = self.w.q('SELECT * FROM recoveries')
        # Ordinary scans deliver all three finite wakes; nobody supplies a synthetic owner ACK.
        self.scan_minutes(190)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['attempts'], engine.RECOVERY_ATTEMPTS)
        self.w.clock.now_ms = inc['escalate_ms'] + 1
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertIsNotNone(wait, 'SPEC7-B1: exhausted wakes must deliver an actual resource handoff question')
        self.assertEqual(wait['state'], 'waiting')
        [notice] = self.notice_rows()
        self.assertEqual((notice['state'], notice['result_ref']), ('done', wait['root']))
        self.assertIn(wait['question']['decision'], next(c['comment_text'] for c in self.w.bot_comments(self.tid)
                                                      if c['id'] == wait['root']))
        original_approval = self.w.q('SELECT * FROM approvals WHERE id=?', inc['approval_id'])[0]
        # Explicit synthetic human resource response, processed through real input/relay/classification.
        self.life.say(comp.APPROVER_A, 'The existing authorized operator is available for this same approved work.',
                      mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'The existing authorized operator can continue the same work.',
                                 gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'answered', world=self.life.planner(script))
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked', 'an answer is not permission to release')
        self.assertEqual(self.w.q('SELECT * FROM approvals WHERE id=?', inc['approval_id'])[0], original_approval)
        self.scan_minutes(5)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered', 'do not republish the same answered takeover')
        self.assertEqual(len(self.notice_rows()), 1)
        rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertEqual(rc, 0, lines)
        # The actual supported native operation is performed by the simulated operator, not the intake.
        self.b.unblock(card)
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(6)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'resumed')
        for field in ('id', 'run_id', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after['attempts'], engine.RECOVERY_ATTEMPTS)


class LegacyCompatibilityTest(LegacyQuestionTest):
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None

    def typed_stop(self):
        card = self.life.fe
        run = self.b.claim(card, 'e2e-tester')
        self.b.block(card, 'needs_input', 'An actual owner decision is required.')
        self.w.step(lambda: self.w.q('SELECT * FROM recoveries'))
        return card, run

    def test_old_legacy_carrier_plain_precise_question_remains_supported(self):
        card, run = self.typed_stop()
        question = 'Which existing approved identity provider should be selected: provider A or B?'
        self.b.worker_comment(card, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\n%s' % (card, run, question))
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertEqual(wait['question']['decision'], question,
                         'unchanged Marketing legacy instructions cannot silently gain a new required schema')
        [delivered] = [c for c in self.w.bot_comments(self.tid) if c['id'] == wait['root']]
        self.assertIn(question, delivered['comment_text'])

    def test_legacy_republication_abandonment_stays_bound_to_same_logical_stop(self):
        from clickup_intake.contracts import Outcome
        card, run = self.typed_stop()
        self.question_ack(card, run)
        path = '/v2/task/%s/comment' % self.tid
        self.w.cu.inject('POST', path, Outcome.DEFINITE_FAIL, status=503)
        self.w.step(lambda: bool(self.notice_rows()) and self.notice_rows()[0]['state'] == 'retry_wait')
        parent = self.b._new('Temporary required dependency', status='blocked')
        self.b.cards[card]['parents'].append(parent)
        self.w.step(lambda: self.notice_rows()[0]['state'] == 'cancelled_stale')
        old = self.notice_rows()[0]
        self.b.cards[card]['parents'].remove(parent)
        self.scan_minutes(3)
        self.question_ack(card, run, decision='Current context restored: choose existing provider A or B?')
        self.w.cu.inject('POST', path, Outcome.AMBIGUOUS, commit=False)
        self.w.step(lambda: len(self.notice_rows()) == 2 and self.notice_rows()[1]['state'] == 'held_ambiguous')
        latest = self.notice_rows()[1]
        self.assertGreaterEqual(latest['attempts'], old['attempts'])
        self.assertEqual(self.w.main('outbox', 'abandon', latest['idem_key'])[0], 0)
        count = self.w.cu.count('POST', path)
        self.scan_minutes(8)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE operator_abandoned']))
        self.assertEqual(self.w.cu.count('POST', path), count)
        self.assertEqual(self.notice_rows()[0], old)

    def test_nonquestion_owner_ack_cannot_create_fake_human_wait_or_suppress_remaining_wake(self):
        card, run = self.typed_stop()
        self.b.worker_comment(card, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\ndetails' % (card, run))
        self.scan_minutes(4)
        self.assertEqual(self.notice_rows(), [], 'legacy compatibility is for a precise question, not arbitrary owner prose')
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.w.clock.now_ms = inc['detected_ms'] + 61 * 60_000
        self.scan_minutes(3)
        [later] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(later['attempts'], 2)
        [wake] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%:2:wake'")
        self.assertEqual(wake['state'], 'done', 'an invalid owner payload is an intermediate repair signal')
        self.assertIsNone(self.diagnostic_wait())

    def test_legacy_verified_question_is_readonly_stale_admission(self):
        card, run = self.typed_stop()
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        mutations = list(self.b.mutations())
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))
        self.assertEqual(self.b.mutations(), mutations)

    def test_legacy_unsafe_or_foreign_structured_question_cannot_downgrade_to_prose(self):
        card, run = self.typed_stop()
        for change in ({'task': 'foreign'}, {'decision': 'Use API key sk-' + 'a' * 40},
                       {'fingerprint': '0' * 64}, {'revision': 99}):
            with self.subTest(change=change):
                self.question_ack(card, run, **change)
                self.scan_minutes(3)
                self.assertEqual(self.notice_rows(), [])
                self.assertIsNone(self.diagnostic_wait())
        self.b.worker_comment(card, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\nRead C:/private/secret.txt?' % (card, run))
        self.scan_minutes(3)
        self.assertEqual(self.notice_rows(), [])

    def assert_displaced_structured_rejected(self, payload):
        card, run = self.typed_stop()
        self.b.worker_comment(card, 'default',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\n%s' % (card, run, payload))
        self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [], 'SPEC7-B2: reserved structured input cannot acquire a prose binding')
        self.assertIsNone(self.diagnostic_wait())
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['attempts'], 1)
        [wake] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%:1:wake'")
        self.assertEqual(wake['state'], 'done', 'malformed owner text cannot suppress the supported automatic path')

    def test_displaced_leading_space_structured_question_cannot_downgrade(self):
        self.assert_displaced_structured_rejected(
            ' CLICKUP-RECOVERY-QUESTION v1 {"task":"foreign","decision":"Which existing provider?"}')

    def test_displaced_blank_line_structured_question_cannot_downgrade(self):
        self.assert_displaced_structured_rejected(
            '\nCLICKUP-RECOVERY-QUESTION v1 {"task":"foreign","decision":"Which existing provider?"}')

    def test_displaced_formatted_malformed_structured_question_cannot_downgrade(self):
        self.assert_displaced_structured_rejected(
            'Quoted decision:\n> CLICKUP-RECOVERY-QUESTION v1 {"decision":"Which provider?"}\n')

    def test_displaced_code_fence_structured_question_cannot_downgrade(self):
        self.assert_displaced_structured_rejected(
            '```\nCLICKUP-RECOVERY-QUESTION v1 not-json Which provider?\n```')

    def test_legacy_queued_question_defers_on_unreadable_required_parent(self):
        from clickup_intake import main
        from clickup_intake.contracts import KanbanResult, Outcome
        card, run = self.typed_stop()
        self.question_ack(card, run)
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        self.b.cards[card]['parents'].append('t_deadbeef')
        count = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        self.scan_minutes(6)
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid), count,
                         'legacy question must retain complete current native observation at final send')
        self.b.cards[card]['parents'].remove('t_deadbeef')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')


class ApprovedIncorporationGuardsTest(ActualApprovedIncorporationTest):
    test_actual_material_without_resolution_uses_exact_approved_plan_and_normal_delivery = None

    def no_delivery(self):
        self.cycle(3)
        self.assertNotEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')
        self.assertEqual([m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)], [])

    def change(self, sql, args=()):
        conn = store_conn(self.w)
        conn.execute(sql, args)
        conn.close()

    def test_partial_resolution_tuple_is_never_overwritten(self):
        for field, value in (('resolution_relay', 'different-relay'), ('resolution_card', 't_ab00009b'),
                             ('resolution_gen', 4), ('resolution_body', 'Existing partial guidance.'),
                             ('resolution_authority', 'unchanged')):
            with self.subTest(field=field):
                self.setUp()
                self.change('UPDATE blockers SET %s=? WHERE id=6' % field, (value,))
                self.no_delivery()
                row = self.w.q('SELECT * FROM blockers WHERE id=6')[0]
                self.assertEqual(row[field], value)
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])

    def test_wrong_material_relay_identity_generation_or_classification_never_seeds(self):
        for field, value in (('card', 't_deadbeef'), ('task', 'other'), ('gen', 3),
                             ('state', 'delivered'), ('classification', 'answer_only')):
            with self.subTest(field=field):
                self.setUp()
                self.change('UPDATE relays SET %s=?' % field, (value,))
                self.no_delivery()
                self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])

    def test_current_authority_and_enablement_are_required_for_derivation(self):
        cases = [
            ("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=20", ()),
            ("UPDATE tasks SET input_gen=5 WHERE clickup_task_id=?", (oauth.TID,)),
            ("UPDATE tasks SET input_gen=5,classified_gen=5,scope_gen=5 WHERE clickup_task_id=?", (oauth.TID,)),
            ("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (oauth.TID,)),
            ("UPDATE tasks SET terminal=1 WHERE clickup_task_id=?", (oauth.TID,)),
            ("DELETE FROM meta WHERE key='amendment_cutover_ms'", ()),
            ("DELETE FROM meta WHERE key='blocker_cutover_ms'", ()),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql):
                self.setUp()
                self.change(sql, args)
                self.no_delivery()
                self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])

    def test_verified_but_foreign_marker_identity_or_generation_never_seeds(self):
        from pathlib import Path
        import hashlib
        literal = 'CLICKUP-INCORPORATES v1 blocker=6 question=t_ab00003f card=t_ab00001b gen=4'
        for change in ('blocker=7', 'question=t_deadbeef', 'card=t_deadbeef', 'gen=3'):
            with self.subTest(marker=change):
                self.setUp()
                field = change.split('=')[0]
                replacement = ' '.join(change if part.startswith(field + '=') else part for part in literal.split(' '))
                path = Path(self.b.cards['t_ab00009b']['atts'][0]['stored_path'])
                data = path.read_bytes().replace(literal.encode(), replacement.encode())
                path.write_bytes(data)
                sha = hashlib.sha256(data).hexdigest()
                # Synthetic exact-byte approved negative, not the captured positive plan.
                for run in self.b.cards['t_ab00009b']['runs']:
                    if isinstance(run.get('metadata'), dict) and 'plan_sha256' in run['metadata']:
                        run['metadata']['plan_sha256'] = sha
                self.change('UPDATE approvals SET plan_sha256=? WHERE id=20', (sha,))
                self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?', (sha, oauth.TID))
                self.no_delivery()
                self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])

    def test_superseding_active_approval_prevents_old_plan_bookkeeping(self):
        row = self.w.q('SELECT * FROM approvals WHERE id=20')[0]
        row.update(id=21, state='held', rev=7)
        conn = store_conn(self.w)
        conn.execute('INSERT INTO approvals(%s) VALUES (%s)' % (','.join(row), ','.join('?' for _ in row)), list(row.values()))
        conn.close()
        self.no_delivery()
        self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])

    def test_unverified_plan_bytes_never_derive_missing_resolution(self):
        from pathlib import Path
        plan = self.b.cards['t_ab00009b']['atts'][0]['stored_path']
        Path(plan).write_bytes(Path(plan).read_bytes() + b'\nUnreviewed bytes.\n')
        self.no_delivery()
        self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])
        self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])

    def test_missing_question_observation_leaves_bound_work_then_normal_scan_delivers_once(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        real = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                               KanbanResult(Outcome.DEFINITE_FAIL) if card == oauth.QUESTION else real(card)):
            self.cycle(3)
        self.assertEqual(len(self.w.q('SELECT * FROM amendment_answers')), 1)
        [bound] = self.w.q('SELECT * FROM amendment_answers')
        self.assertEqual(bound['state'], 'bound')
        self.assertNotEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')
        self.cycle(3)
        self.assertEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')
        self.assertEqual(len([m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)]), 1)

    def test_contrary_native_work_or_question_is_not_released(self):
        cases = [('question_done', lambda: self.b.cards[oauth.QUESTION]['task'].update(status='archived')),
                 ('work_busy', lambda: self.b.claim(oauth.CARD, 'implementor')),
                 ('missing_parent', lambda: self.b.cards[oauth.CARD]['parents'].remove(oauth.QUESTION)),
                 ('wrong_lane', lambda: self.b.cards[oauth.CARD]['task'].update(status='blocked'))]
        for label, change in cases:
            with self.subTest(case=label):
                self.setUp()
                change()
                self.cycle(3)
                self.assertEqual([m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)], [])
                self.assertTrue(all(r['state'] == 'void' for r in self.w.q('SELECT * FROM amendment_answers')))

    def test_missing_plan_attachment_keeps_progress_pass_inert_until_full_observation(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        real = self.b.attachments
        with mock.patch.object(self.b, 'attachments', side_effect=lambda card:
                               KanbanResult(Outcome.DEFINITE_FAIL) if card == 't_ab00009b' else real(card)):
            self.cycle(3)
        self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])
        self.assertIsNone(self.w.q('SELECT resolution_body FROM blockers WHERE id=6')[0]['resolution_body'])
        self.cycle(3)
        self.assertEqual(self.b.cards[oauth.QUESTION]['task']['status'], 'done')

    def test_applied_native_completion_reconciles_after_real_executor_crash_without_duplicate(self):
        from test_s4_composition import Crash
        real = self.b.complete
        crashed = [False]
        def complete(card, summary, metadata=None):
            result = real(card, summary, metadata)
            if card == oauth.QUESTION and not crashed[0]:
                crashed[0] = True
                raise Crash('applied native completion before intake journal commit')
            return result
        with mock.patch.object(self.b, 'complete', side_effect=complete):
            self.cycle(4)  # World.main restarts the real entry after its Crash boundary.
        self.assertTrue(crashed[0], 'the injected post-apply crash must actually occur')
        self.cycle(4)
        self.assertEqual(self.w.q('SELECT delivered_gen FROM blockers WHERE id=6')[0]['delivered_gen'], 4)
        self.assertEqual(len([m for m in self.b.mutations() if m == ('complete', oauth.QUESTION)]), 1)


class PreciseQuestionFreshnessTest(LegacyQuestionTest):
    """Retain the exact safety assertions under a genuine-question (not obsolete cannot_act) fixture."""
    test_legacy_owner_question_delivers_exact_wait_and_answer_uses_normal_route = None
    import test_stalled_freshness as _prior
    slow_scan = _prior.DiagnosticFreshnessTest.slow_scan
    assert_unsent = _prior.DiagnosticFreshnessTest.assert_unsent
    unknown_stop = remediation.RemediationTest.unknown_stop

    def queue_notice(self):
        from clickup_intake import main
        card, run = self.unknown_stop()
        self.question_ack(card, run)
        with mock.patch.object(main.Glue, 'clickup', return_value=False):
            self.w.step(self.notice_rows)
        return card, run, self.notice_rows()[0]['idem_key']

    test_current_question_changed_description_requires_classification = _prior.DiagnosticFreshnessTest.test_changed_description_during_slow_scan_is_new_input_not_fresh_approval
    test_current_question_hold_during_native_IO_cannot_escape = _prior.DiagnosticFreshnessTest.test_current_hold_during_final_native_IO_cannot_escape_refreshed_notice
    test_current_question_post_native_IO_expiry_fails_closed = _prior.DiagnosticFreshnessTest.test_diagnostic_native_IO_after_refresh_still_expires_fail_closed
    test_current_question_failed_refresh_defers_then_recovers = _prior.DiagnosticFreshnessTest.test_failed_target_refresh_defers_without_timestamp_renewal_or_send_then_recovers
    test_current_question_incomplete_refresh_cannot_reuse_task_get = _prior.DiagnosticFreshnessTest.test_incomplete_comment_refresh_cannot_reuse_a_successful_task_get
    test_current_question_new_addressed_input_cancels_and_classifies = _prior.DiagnosticFreshnessTest.test_new_addressed_input_during_slow_scan_cancels_old_notice_and_classifies_normally
    test_current_question_new_parent_at_refresh_is_required = _prior.DiagnosticFreshnessTest.test_new_parent_during_target_provider_refresh_is_required_before_notice_send
    test_current_question_shared_budget_cannot_buy_refresh = _prior.DiagnosticFreshnessTest.test_shared_scan_budget_exhaustion_cannot_buy_a_refresh_or_send
    test_current_question_post_provider_IO_expiry_cannot_loop = _prior.DiagnosticFreshnessTest.test_target_provider_IO_over_five_minutes_still_expires_without_refresh_loop
    test_current_question_refresh_requires_home_list_proof = _prior.DiagnosticFreshnessTest.test_target_refresh_requires_current_home_list_proof_not_scan_start_confirmation
    test_current_question_unaddressed_activity_keeps_quiet_barrier = _prior.DiagnosticFreshnessTest.test_unaddressed_activity_preserves_generation_but_defers_until_real_quiet_barrier

    def test_current_question_slow_scan_delivers_verified_wait_once(self):
        card, run, key = self.queue_notice()
        count = self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid)
        for _ in range(3):
            self.w.clock.advance(61)
            self.slow_scan()
        [notice] = self.notice_rows()
        self.assertEqual((notice['idem_key'], notice['state']), (key, 'done'))
        self.assertEqual(self.w.cu.count('POST', '/v2/task/%s/comment' % self.tid) - count, 1)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.assertEqual(self.diagnostic_wait()['root'], notice['result_ref'])
        self.assertEqual(self.w.q('SELECT state FROM recoveries')[0]['state'], 'open')
        self.assertEqual(self.b.cards[card]['runs'][-1]['id'], run)
