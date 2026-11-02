"""Verified initial approval waits through the real World.main scan/effect order."""
import copy
import json
import os
import unittest
from unittest import mock

from clickup_intake import engine, progress, rules
from clickup_intake.contracts import Outcome, Preconditions
from clickup_intake.store import Store
import test_recovery as recovery
import test_s4_composition as composition
from preworker_snapshot_fixture import install


class ApprovalWaitTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            patcher = mock.patch(target, side_effect=AssertionError('offline-only boundary'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.w = composition.World(self)
        self.w.board = recovery.RecoveryBoard(os.path.join(self.w.dir, 'board'), self.w.clock, self.w.inj)
        self.b = self.w.board
        install(self.b, self.w.dir)
        self.tid = self.w.cu.add_task(task_id='86pubappr01', name='Synthetic approval wait', creator=composition.REQ)
        for text in ('Requirement one.', 'Requirement two.', 'Requirement three.'):
            self.w.cu.add_comment(self.tid, composition.REQ, text)
        self.assertEqual(self.w.main('recovery', 'enable')[0], 0)
        self.w.clock.advance(5)
        self.assertEqual(self.w.run()[0], 0)
        # Synthetic preexisting material-input ledger; publication itself is real.
        c = recovery.store_conn(self.w)
        try:
            c.execute('UPDATE tasks SET input_gen=4,scope_gen=4 WHERE clickup_task_id=?', (self.tid,))
        finally:
            c.close()
        self.source = None

        def planner():
            for card in self.b.by_key('clickup-dp:%s:req:' % self.tid):
                if self.b.cards[card]['task']['status'] == 'ready':
                    self.source = card
                    self.b.claim(card, 'planner')
                    ref, gen = composition.relay_of(self.b, card)
                    self.b.worker_comment(card, 'planner', composition.classify(ref, gen))
                    self.sha = self.b.request_review(card, self.tid, 4, composition.PLAN_R1, gen)
                    self.b.claim(card, 'plan-reviewer')
                    self.b.finish(card, 'plan-reviewer')
        self.w.step(lambda: self.w.task(self.tid)['phase'] == 'awaiting_approval', world=planner)
        self.scans(3)
        t = self.w.task(self.tid)
        self.assertEqual((t['input_gen'], t['classified_gen'], t['scope_gen'], t['published_accounts_gen']),
                         (4, 4, 4, 4), t)
        self.assertEqual(t['published_rev'], 4)
        self.pub = self.w.q("SELECT * FROM outbox WHERE task=? AND kind='cu_comment' AND idem_key LIKE '%:pub:%'",
                            self.tid)[0]
        self.assertEqual(self.pub['state'], 'done')
        self.root = self.pub['result_ref']
        self.assertTrue(self.w.q("SELECT 1 FROM items WHERE task=? AND kind='comment' AND id=? AND deleted=0",
                                 self.tid, self.root))
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])
        # A settled benign later generation, as in the observed 5/5, scope=4 case.
        self.w.cu.add_comment(self.tid, composition.BOT, 'Synthetic benign bot bookkeeping.')
        c = recovery.store_conn(self.w)
        try:
            c.execute('UPDATE tasks SET input_gen=5,classified_gen=5 WHERE clickup_task_id=?', (self.tid,))
            c.execute("INSERT INTO relays(relay_ref,task,card,gen,item_ids,state,classification) "
                      "VALUES ('synthetic-benign-settlement',?,?,5,'[]','classified','answer_only')",
                      (self.tid, self.source))
        finally:
            c.close()
        self.scans(2)
        t = self.w.task(self.tid)
        self.assertEqual((t['input_gen'], t['classified_gen'], t['scope_gen'], t['published_accounts_gen']),
                         (5, 5, 4, 4))
        self.assertTrue(rules.current(4, t))

    def scans(self, count, seconds=61):
        for _ in range(count):
            self.w.clock.advance(seconds)
            rc, lines = self.w.run()
            self.assertEqual(rc, 0, lines)

    def carriers(self):
        return self.b.by_key('clickup-dp:%s:rcv:' % self.tid)

    def seed_incident(self, attempts=3, notice=True):
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            detected = self.w.clock() - 13 * 60 * 60 * 1000
            inc = dict(id=29, task=self.tid, card=progress.outer_anchor(self.tid), run_id=0,
                       event_kind=progress.CLASS, event_created_s=detected // 1000,
                       block_kind=progress.CLASS, reason_sha256='1' * 64, approval_id=None,
                       approval_rev=None, plan_sha256=None, adopted=0, state='open', attempts=attempts,
                       detected_ms=detected, abs_ms=detected + 24 * 60 * 60 * 1000, updated_ms=detected)
            inc['class'] = progress.CLASS
            s.conn.execute('INSERT INTO recoveries(' + ','.join(inc) + ') VALUES (' + ','.join('?' for _ in inc) + ')', list(inc.values()))
            if notice:
                key = engine._notice_key(self.tid, inc)
                ctx.cu(self.tid, key, 'cu_comment', dict(text='Synthetic previously delivered technical notice.', recovery_notice=29))
                row = s.conn.execute('SELECT * FROM outbox WHERE idem_key=?', (key,)).fetchone()
                ref = self.w.cu.add_comment(self.tid, composition.BOT, json.loads(row['body'])['text'])
                ctx.put(key + '#cu', 'clickup_comment', self.tid,
                        dict(task=self.tid, text=json.loads(row['body'])['text']),
                        Preconditions(task_id=self.tid))
                s.conn.execute("UPDATE outbox SET state='done',outcome='OK',attempts=1,result_ref=? WHERE idem_key IN (?,?)",
                               (ref, key, key + '#cu'))
                # No cached verification: normal main must observe the already-delivered root.
                self.notice_key = key
            return inc
        finally:
            s.close()

    def test_verified_wait_quiets_existing_spent_incident_without_completing_work(self):
        before = self.seed_incident()
        old_effects = self.w.q("SELECT * FROM outbox WHERE state='done' ORDER BY idem_key")
        native = copy.deepcopy(self.b.cards)
        self.scans(4)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.scans(4)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertFalse(any('default coordinator retains' in line for line in self.w.outputs),
                         'verified plan approval wait must not emit the old delivered-notice owner warning')
        self.assertEqual(after['state'], 'open', 'waiting is not completed work')
        for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'reason_sha256', 'attempts', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], before[field], field)
        for row in old_effects:
            self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', row['idem_key']), [row])
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key=?", 'completed_workflow:' + self.tid), [])
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])
        self.assertEqual(self.carriers(), [])
        self.assertEqual(self.b.cards, native)
        self.assertEqual(self.w.task(self.tid)['phase'], 'awaiting_approval')

    def wait_state(self):
        s = Store(self.w.state_path, self.w.clock)
        try:
            return progress.human_wait(s.conn, self.tid)
        finally:
            s.close()

    def fail_independent_notice(self):
        key = 'clickup-dp:' + self.tid + ':independent-status-notice'
        s = Store(self.w.state_path, self.w.clock)
        try:
            engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config()).comment(
                self.tid, key, 'Independent workflow notification.')
        finally:
            s.close()
        for _ in range(6):
            self.w.cu.inject('POST', '/v2/task/' + self.tid + '/comment', Outcome.DEFINITE_FAIL, status=503)
        for _ in range(10):
            self.scans(1, seconds=1801)
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
            if row['state'] == 'failed':
                break
        self.assertEqual((row['state'], row['attempts']), ('failed', 6))
        return key

    def test_initial_verified_wait_has_no_new_recovery_after_deadline(self):
        self.assertIs(self.wait_state(), True)
        self.scans(8, seconds=14401)
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])
        self.assertEqual(self.carriers(), [])

    def test_delivered_notice_callback_is_quiet_only_during_verified_wait(self):
        inc = self.seed_incident()
        self.scans(3)
        s = Store(self.w.state_path, self.w.clock)
        try:
            row = s.conn.execute('SELECT * FROM outbox WHERE idem_key=?', (self.notice_key,)).fetchone()
            root = next(c for c in self.w.bot_comments(self.tid) if c['id'] == row['result_ref'])
            obs = engine.Observation(comments={(self.tid, 'comment', root['id']): (None, root['comment_text'])})
            ctx = engine._Ctx(s, self.w.clock(), obs, engine.Config())
            self.assertFalse(engine._notice_pending(ctx, self.tid, inc))
            self.assertEqual(s.conn.execute("SELECT 1 FROM outbox WHERE idem_key='clickup-dp:alert:rcv-owner:29'").fetchall(), [])
            s.conn.execute('UPDATE tasks SET scope_gen=6 WHERE clickup_task_id=?', (self.tid,))
            self.assertFalse(engine._notice_pending(ctx, self.tid, inc))
            self.assertTrue(s.conn.execute("SELECT 1 FROM outbox WHERE idem_key='clickup-dp:alert:rcv-owner:29'").fetchone())
        finally:
            s.close()

    def test_later_independent_failure_reuses_spent_owner_and_remains_current(self):
        before = self.seed_incident()
        self.scans(4)
        self.assertIs(self.wait_state(), True)
        key = self.fail_independent_notice()
        self.scans(14)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['attempts'], 3, 'the wait does not refund spent wakes')
        self.assertEqual(after['abs_ms'], before['abs_ms'])
        admission = self.w.main('recovery-check', '--card', after['card'], '--run', '0')
        self.assertEqual(admission[0], 0, 'later delivery failure must have a CURRENT diagnostic owner: %r' % (admission,))
        self.assertNotEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(self.carriers(), [])
        self.assertTrue(any('failed after 6 attempts' in line and key in line for line in self.w.outputs))

    def test_independent_failure_during_wait_still_gets_delivered_diagnostic(self):
        self.assertIs(self.wait_state(), True)
        key = self.fail_independent_notice()
        incidents = []
        for _ in range(4):
            self.scans(1, seconds=1801)
            incidents = self.w.q('SELECT * FROM recoveries')
            if incidents:
                break
        self.assertEqual(len(incidents), 1, incidents)
        [inc] = incidents
        self.assertEqual(inc['attempts'], 1)
        [carrier] = self.carriers()
        self.assertIn(key, self.b.cards[carrier]['task']['body'])
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'done')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])

    def test_queued_owner_warning_waits_but_unrelated_alert_is_not_silenced(self):
        inc = self.seed_incident()
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            ctx.alert('rcv-owner:29', 'default coordinator retains the same repair', self.tid)
            ctx.alert('rcv:' + inc['card'] + ':0', 'Originating-task technical notice queued.', self.tid)
            ctx.alert('unrelated-approval-control', 'Unrelated integration warning.', self.tid)
        finally:
            s.close()
        self.scans(3)
        self.assertNotIn('default coordinator retains the same repair', self.w.outputs)
        self.assertNotIn('Originating-task technical notice queued.', self.w.outputs)
        self.assertIn('Unrelated integration warning.', self.w.outputs)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)[0]['state'], 'done')
        self.fail_independent_notice()
        self.scans(2)
        self.assertEqual(self.w.outputs.count('default coordinator retains the same repair'), 1)
        self.assertEqual(self.w.outputs.count('Originating-task technical notice queued.'), 1)

    def test_queued_recovery_notice_and_wake_are_refused_during_wait(self):
        inc = self.seed_incident(attempts=0, notice=False)
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            engine._attempt(ctx, self.tid, inc, dict(id=None, rev=None, plan_sha256=None),
                            dict(card=inc['card'], source_status=None, reason='Synthetic pre-wait queued recovery.'))
            key = engine._notice_key(self.tid, inc)
            ctx.cu(self.tid, key, 'cu_comment', dict(text='Synthetic unsent technical notice.', recovery_notice=29))
        finally:
            s.close()
        sent = self.w.cu.count('POST', '/v2/task/' + self.tid + '/comment')
        self.scans(4)
        self.assertTrue(all(self.b.cards[card]['task']['status'] != 'done' for card in self.carriers()))
        self.assertEqual(self.w.q("SELECT 1 FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%' AND state='done'"), [])
        self.assertEqual(self.w.cu.count('POST', '/v2/task/' + self.tid + '/comment'), sent)
        self.assertNotEqual(self.w.q('SELECT state FROM outbox WHERE idem_key=?', key)[0]['state'], 'done')
        self.assertEqual(self.w.q('SELECT attempts FROM recoveries')[0]['attempts'], 1)
        self.assertNotEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)

    def test_unobserved_approval_request_stays_diagnosable(self):
        self.w.cu.delete_comment(self.root)
        c = recovery.store_conn(self.w)
        try:
            c.execute('DELETE FROM items WHERE task=? AND id=?', (self.tid, self.root))
        finally:
            c.close()
        self.assertIs(self.wait_state(), False)
        self.scans(4, seconds=1801)
        self.assertTrue(self.carriers(), 'unobserved publication must not inherit human-wait silence')

    def test_failed_publication_status_stays_diagnosable(self):
        c = recovery.store_conn(self.w)
        key = self.pub['idem_key'].removesuffix(':comment') + ':status'
        try:
            c.execute("UPDATE outbox SET state='failed',outcome='retry_wait',attempts=6,result_ref=NULL WHERE idem_key IN (?,?)",
                      (key, key + '#cu'))
        finally:
            c.close()
        self.assertIs(self.wait_state(), False)
        self.scans(4, seconds=1801)
        self.assertTrue(self.carriers(), 'failed publication is not an approval wait')
        [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
        self.assertEqual((row['state'], row['attempts']), ('failed', 6))

    def test_publication_binding_rejects_obsolete_foreign_and_pending_proofs(self):
        c = recovery.store_conn(self.w)
        try:
            task = dict(c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
            for column, value in (('published_rev', 5), ('published_sha256', self.sha[:12] + 'f' * 52),
                                  ('scope_gen', 6), ('classified_gen', 4)):
                with self.subTest(column=column):
                    c.execute('UPDATE tasks SET ' + column + '=? WHERE clickup_task_id=?', (value, self.tid))
                    self.assertIs(progress.approval_wait(c, self.tid), False)
                    c.execute('UPDATE tasks SET ' + column + '=? WHERE clickup_task_id=?', (task[column], self.tid))
            for state in ('pending', 'retry_wait', 'ambiguous', 'failed'):
                with self.subTest(state=state):
                    c.execute('UPDATE outbox SET state=? WHERE idem_key=?', (state, self.pub['idem_key']))
                    self.assertIs(progress.approval_wait(c, self.tid), False)
            c.execute("UPDATE outbox SET state='done' WHERE idem_key=?", (self.pub['idem_key'],))
            body = json.loads(self.pub['body'])
            for field, value in (('task', 'another-task'), ('summary_from', dict(card='foreign-source', attachment='x'))):
                with self.subTest(field=field):
                    altered = dict(body, **{field: value})
                    c.execute('UPDATE outbox SET body=? WHERE idem_key=?', (json.dumps(altered), self.pub['idem_key']))
                    self.assertIs(progress.approval_wait(c, self.tid), False)
            c.execute('UPDATE outbox SET body=? WHERE idem_key=?', (self.pub['body'], self.pub['idem_key']))
            self.assertIs(progress.approval_wait(c, self.tid), True)
        finally:
            c.close()

    def test_accepted_approval_exits_wait_and_releases_normal_guarded_work(self):
        self.w.clock.advance(5)
        self.w.cu.add_comment(self.tid, composition.APPROVER_A, 'Approve plan r4 for development')
        self.w.step(lambda: bool(self.w.q("SELECT 1 FROM approvals WHERE state='released'")))
        self.assertIs(self.wait_state(), False)
        [a] = self.w.q('SELECT * FROM approvals')
        self.assertEqual((a['rev'], a['plan_sha256']), (4, self.sha))
        self.assertEqual(self.b.cards[a['orchestrator_card']]['task']['status'], 'ready')

    def test_meaningful_input_resumes_classification_and_revision(self):
        self.w.clock.advance(5)
        self.w.cu.add_comment(self.tid, composition.REQ, 'Change the required behavior.', mention=composition.BOT)
        classified = []
        def planner():
            for card in self.b.by_key('clickup-dp:%s:req:' % self.tid):
                if self.b.cards[card]['task']['status'] == 'ready':
                    self.b.claim(card, 'planner')
                    ref, gen = composition.relay_of(self.b, card)
                    self.b.worker_comment(card, 'planner', composition.classify(ref, gen, 'material'))
                    self.b.finish(card, 'planner')
                    classified.append(card)
        self.w.step(lambda: self.w.task(self.tid)['phase'] == 'revising', world=planner)
        self.assertTrue(classified)
        self.assertGreater(self.w.task(self.tid)['scope_gen'], 4)
        self.assertIs(self.wait_state(), False)
        self.assertEqual(self.w.q('SELECT * FROM approvals'), [])

    def test_revoked_approval_only_blocks_initial_wait_for_its_exact_plan(self):
        self.assertIs(self.wait_state(), True)
        s = Store(self.w.state_path, self.w.clock)
        try:
            s.conn.execute("INSERT INTO approvals(id,task,rev,accounts_gen,plan_sha256,state,revoked_reason,revoked_ms) "
                           "VALUES (990001,?,4,4,?,'revoked','new_input',?)",
                           (self.tid, self.sha, self.w.clock()))
            for reason in ('new_input', 'hold', 'operator'):
                with self.subTest(reason=reason):
                    s.conn.execute('UPDATE approvals SET revoked_reason=? WHERE id=990001', (reason,))
                    self.assertIs(progress.approval_wait(s.conn, self.tid), False)
            s.conn.execute('UPDATE approvals SET rev=3 WHERE id=990001')
            self.assertIs(progress.approval_wait(s.conn, self.tid), True)
            s.conn.execute('UPDATE approvals SET rev=4,plan_sha256=? WHERE id=990001', ('f' * 64,))
            self.assertIs(progress.approval_wait(s.conn, self.tid), True)
            s.conn.execute('DELETE FROM approvals WHERE id=990001')
        finally:
            s.close()
        self.scans(4)
        self.assertIs(self.wait_state(), True)
        self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])

    def assert_old_incident_preserved(self, before, notice):
        [after] = self.w.q('SELECT * FROM recoveries')
        for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'reason_sha256',
                      'attempts', 'detected_ms', 'abs_ms', 'state'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key), [notice])
        self.assertEqual(self.w.q('SELECT * FROM meta WHERE key=?', 'completed_workflow:' + self.tid), [])

    def test_postapproval_stall_after_spent_deadline_still_qualifies_warning(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        s = Store(self.w.state_path, self.w.clock)
        try:
            engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config()).alert(
                'rcv-owner:29', 'Old coordinator warning', self.tid)
        finally:
            s.close()
        self.scans(2)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.assertNotIn('Old coordinator warning', self.w.outputs)
        self.scans(12)
        self.assertEqual(self.w.outputs.count('Old coordinator warning'), 1,
                         'a new stable postapproval stall must remain diagnosable after the old deadline')
        self.scans(2)
        self.assertEqual(self.w.outputs.count('Old coordinator warning'), 1)
        self.assert_old_incident_preserved(before, notice)

    def test_postapproval_independent_delivery_failure_keeps_old_owner_usable(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        self.scans(2)
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.assertFalse(any('default coordinator retains' in line for line in self.w.outputs))
        key = self.fail_independent_notice()
        # Allow the same normal observation/grace cadence as the existing delivery control.
        self.scans(14)
        self.assertTrue(any('default coordinator retains' in line for line in self.w.outputs))
        self.assertTrue(any('failed after 6 attempts' in line and key in line for line in self.w.outputs))
        admission = self.w.main('recovery-check', '--card', before['card'], '--run', '0')
        self.assertEqual(admission[0], 0, (admission, self.w.q('SELECT * FROM recoveries'),
                                         self.w.task(self.tid)))
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['id'], after['attempts'], after['abs_ms']),
                         (before['id'], before['attempts'], before['abs_ms']))
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key), [notice])

    def test_healthy_approval_does_not_replay_queued_old_warnings(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            ctx.alert('rcv-owner:29', 'Old coordinator warning', self.tid)
            ctx.alert('rcv:' + before['card'] + ':0', 'Old escalation warning', self.tid)
        finally:
            s.close()
        self.scans(2)
        self.assertNotIn('Old coordinator warning', self.w.outputs)
        self.assertNotIn('Old escalation warning', self.w.outputs)
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.scans(2)
        self.assertNotIn('Old coordinator warning', self.w.outputs,
                         'a healthy accepted approval is not a current technical failure')
        self.assertNotIn('Old escalation warning', self.w.outputs)
        self.assertEqual(self.w.q("SELECT * FROM outbox WHERE state='failed'"), [])
        self.assert_old_incident_preserved(before, notice)

    def test_healthy_approval_does_not_replay_old_delivered_notice_callback(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        self.scans(2)
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.scans(2)
        self.assertFalse(any('default coordinator retains' in line for line in self.w.outputs),
                         'the delivered old notice is not evidence of a new failure')
        self.assert_old_incident_preserved(before, notice)

    def test_healthy_approval_does_not_replay_source_generated_escalation(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            engine._escalate(ctx, self.tid, before, 'not resumed after 3 AI wakes')
            key = 'clickup-dp:alert:rcv:' + before['card'] + ':0'
            [alert] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
            text = json.loads(alert['body'])['text']
        finally:
            s.close()
        self.scans(2)
        self.assertNotIn(text, self.w.outputs)
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.scans(2)
        self.assertNotIn(text, self.w.outputs,
                         'an actual old escalation must not replay on healthy approval')
        self.assertEqual(self.w.q("SELECT * FROM outbox WHERE state='failed'"), [])
        self.assert_old_incident_preserved(before, notice)


    def test_approved_plan_unsettled_comment_does_not_replay_old_warnings(self):
        before = self.seed_incident()
        [notice] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.notice_key)
        s = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(s, self.w.clock(), engine.Observation(), engine.Config())
            ctx.alert('rcv-owner:29', 'Old coordinator warning', self.tid)
            ctx.alert('rcv:' + before['card'] + ':0', 'Old escalation warning', self.tid)
        finally:
            s.close()
        self.scans(2)
        self.test_accepted_approval_exits_wait_and_releases_normal_guarded_work()
        self.scans(2)
        self.assertNotIn('Old coordinator warning', self.w.outputs)
        self.assertNotIn('Old escalation warning', self.w.outputs)
        self.w.clock.advance(5)
        self.w.cu.add_comment(self.tid, composition.REQ, 'Thanks, one small question later.',
                              mention=composition.BOT)
        start = len(self.w.outputs)
        for _ in range(3):
            self.scans(1)
            # The warning oracle is meaningful only in the healthy unsettled window.
            t = self.w.task(self.tid)
            self.assertEqual((t['input_gen'], t['classified_gen'], t['scope_gen'], t['phase']),
                             (6, 5, 4, 'approval_queued'), t)
            [a] = self.w.q('SELECT * FROM approvals')
            self.assertEqual((a['task'], a['rev'], a['accounts_gen'], a['plan_sha256'], a['state']),
                             (self.tid, 4, 4, self.sha, 'released'))
            self.assertEqual((t['published_rev'], t['published_sha256'], t['published_accounts_gen']),
                             (4, self.sha, 4))
            self.assertEqual(self.b.cards[a['orchestrator_card']]['task']['status'], 'ready')
            self.assertEqual(self.w.q("SELECT * FROM outbox WHERE state='failed'"), [])
        recent = self.w.outputs[start:]
        self.assertNotIn('Old coordinator warning', recent,
                         'unsettled input alone is not a new technical obligation after approval')
        self.assertNotIn('Old escalation warning', recent)
        self.assertFalse(any('default coordinator retains' in line for line in recent), recent)
        self.assert_old_incident_preserved(before, notice)
        self.assertEqual(self.carriers(), [])
        self.assertEqual({r['state'] for r in self.w.q(
            "SELECT state FROM outbox WHERE idem_key IN (?,?)",
            'clickup-dp:alert:rcv-owner:29', 'clickup-dp:alert:rcv:' + before['card'] + ':0')},
            {'pending'}, 'old alerts stay deferred, not cancelled or consumed')
