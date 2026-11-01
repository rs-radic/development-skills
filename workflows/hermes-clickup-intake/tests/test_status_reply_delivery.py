"""Sanitized NULL-root structural projection, real main/Glue, fake provider only."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

import test_superseded_approved_incorporation as captured
from clickup_intake import engine, main
from clickup_intake.store import Store

TID, WORK, QUESTION = captured.TID, captured.WORK, captured.QUESTION
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/null_root_status_delivery.json').read_bytes())
ROOT = FIXTURE['root_comment']['id']
KEYS = [row['idem_key'] for row in FIXTURE['outbox'][:2]]


class StatusReplyDeliveryTest(unittest.TestCase):
    setUp = captured.SupersededApprovedIncorporationTest.setUp
    change = captured.SupersededApprovedIncorporationTest.change
    blocker = captured.SupersededApprovedIncorporationTest.blocker
    def cycle(self, n=1):
        try:
            return captured.SupersededApprovedIncorporationTest.cycle(self, n)
        except AssertionError:
            for path in Path(self.w.dir).rglob('intake.jsonl'):
                print('FIXTURE_LOG ' + str(path) + '\n' + path.read_text(errors='replace')[-12000:], flush=True)
            raise
    answer_rows = captured.SupersededApprovedIncorporationTest.answer_rows

    def seed_root(self):
        row = FIXTURE['outbox'][2]
        old = self.w.cu.add_comment(TID, captured.comp.BOT, json.loads(row['body'])['text'],
                                  date_ms=FIXTURE['root_comment']['date_ms'])
        comment = next(c for c in self.w.cu.comments[TID] if c['id'] == old)
        comment['id'] = ROOT
        self.w.cu.replies[ROOT] = self.w.cu.replies.pop(old)
        store = Store(self.w.state_path, self.w.clock)
        try:
            self.insert(store.conn, 'outbox', row)
            self.insert(store.conn, 'items', FIXTURE['root_comment'])
        finally:
            store.close()

    @staticmethod
    def insert(conn, table, row):
        conn.execute('INSERT INTO %s(%s) VALUES (%s)' %
                     (table, ','.join(row), ','.join('?' for _ in row)), list(row.values()))

    def seed_failed(self):
        self.seed_root()
        store = Store(self.w.state_path, self.w.clock)
        try:
            b = FIXTURE['blocker']
            a = FIXTURE['approval']
            store.conn.execute('UPDATE approvals SET %s WHERE id=33' %
                ','.join(k+'=?' for k in a if k != 'id'), [v for k,v in a.items() if k != 'id'])
            store.conn.execute('UPDATE blockers SET %s WHERE id=5' %
                ','.join(k+'=?' for k in b if k != 'id'), [v for k,v in b.items() if k != 'id'])
            for row in FIXTURE['outbox'][:2]:
                self.insert(store.conn, 'outbox', row)
            for row in FIXTURE['bindings']:
                self.insert(store.conn, 'amendment_answers', row)
        finally:
            store.close()
        self.b.cards[QUESTION]['task']['status'] = 'done'
        self.b._run = 1566
        self.b.claim(WORK, 'implementor')
        self.before_native = copy.deepcopy(self.b.cards)
        self.before_blocker = self.blocker()
        self.before_rows = self.rows()
        self.assertIsNone(self.before_blocker['closure_item'])
        self.assertEqual([(r['state'], r['outcome'], r['attempts'], r['result_ref']) for r in self.before_rows],
                         [('cancelled_stale', 'FOREIGN_ROOT', 0, None)] * 2)

    def rows(self):
        return self.w.q('SELECT * FROM outbox WHERE idem_key IN (?,?) ORDER BY idem_key', *KEYS)

    def assert_delivery(self):
        readback = self.w.cu.request('GET', '/v2/comment/%s/reply' % ROOT)
        self.assertIs(readback.outcome, captured.Outcome.OK)
        replies = readback.json['comments']
        self.assertEqual(len(replies), 2)
        for key, expected in zip(KEYS, FIXTURE['outbox'][:2]):
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
            self.assertEqual(row['state'], 'done')
            self.assertEqual(json.loads(row['body'])['root'], ROOT)
            text = json.loads(expected['body'])['text']
            [reply] = [r for r in replies if text in r['comment_text']]
            self.assertIn(text, reply['comment_text'])
            self.assertEqual(reply['user']['id'], captured.comp.BOT)
        posts = [path for method,path,_ in self.w.cu.calls if method == 'POST' and '/reply' in path]
        self.assertEqual(posts, ['/v2/comment/%s/reply' % ROOT] * 2)

    def test_new_recorded_and_resumed_replies_use_observed_question_root(self):
        self.seed_root()
        captured.SupersededApprovedIncorporationTest.test_superseded_null_capture_delivers_bound_answer_then_normal_fence_ack_once(self)
        self.assert_delivery()
        self.cycle(2)
        self.assert_delivery()

    def test_supported_resend_recovers_captured_never_sent_rows_after_restart(self):
        self.seed_failed()
        for key in KEYS:
            rc, lines = self.w.main('outbox', 'resend', key)
            self.assertEqual(rc, 0, lines)
        self.cycle(3)
        self.assert_delivery()
        self.cycle(3)
        self.assert_delivery()
        self.assertEqual(self.blocker(), self.before_blocker)
        self.assertEqual(self.b.cards, self.before_native)
        self.assertEqual(self.answer_rows(), [])
        for original, row in zip(self.before_rows, self.rows()):
            [history] = self.w.q('SELECT value FROM meta WHERE key=?', 'delivery_repair:' + row['idem_key'])
            self.assertEqual(json.loads(history['value']), original)
        print('STATUS_DELIVERY_RESULT ' + json.dumps(dict(rows=self.rows(), roots=list(self.w.cu.replies),
            blocker=self.blocker(), native_mutations=self.b.mutations()), sort_keys=True), flush=True)

    def test_repaired_http_commit_before_journal_restart_does_not_duplicate(self):
        self.seed_failed()
        for key in KEYS:
            self.assertEqual(self.w.main('outbox', 'resend', key)[0], 0)
        request = self.w.cu.request
        crashed = []
        def crash_after_reply(method, path, *args, **kwargs):
            result = request(method, path, *args, **kwargs)
            if method == 'POST' and path.endswith('/reply') and not crashed:
                crashed.append(path)
                raise captured.comp.Crash('fake provider committed; journal not yet updated')
            return result
        with mock.patch.object(self.w.cu, 'request', side_effect=crash_after_reply):
            self.cycle(6)
        self.assertEqual(crashed, ['/v2/comment/%s/reply' % ROOT])
        self.assert_delivery()
        self.cycle(2)
        self.assert_delivery()
        self.assertEqual(self.blocker(), self.before_blocker)
        self.assertEqual(self.b.cards, self.before_native)
        for key in KEYS:
            self.assertEqual(self.w.main('outbox', 'resend', key)[0], 1)

    def test_historical_resend_does_not_claim_current_external_wait_resumed(self):
        self.seed_failed()
        self.b.cards[WORK]['task']['status'] = 'blocked'
        self.b.cards[WORK]['runs'][-1].update(status='blocked', outcome='blocked')
        gate = self.b.worker_create('Synthetic later external resource wait', assignee='implementor')
        self.b.cards[gate]['task']['status'] = 'blocked'
        self.b.cards[WORK]['parents'].append(gate)
        native_before = copy.deepcopy(self.b.cards)
        for key in KEYS:
            self.assertEqual(self.w.main('outbox', 'resend', key)[0], 0)
        self.cycle(3)
        self.assert_delivery()
        for row in self.rows():
            text = json.loads(row['body'])['text']
            self.assertIn('Historical update for earlier question blocker 5:', text)
            self.assertIn('not a statement of current work status', text)
        self.assertEqual(self.b.cards, native_before)
        self.assertEqual(self.blocker(), self.before_blocker)

    def reset_failed(self):
        self.doCleanups()
        self.setUp()
        self.seed_failed()

    def test_resend_refuses_unobserved_missing_foreign_or_wrong_root_bindings(self):
        cases = [
            ("DELETE FROM items WHERE id=?", (ROOT,)),
            ("UPDATE items SET task='other' WHERE id=?", (ROOT,)),
            ("UPDATE items SET deleted=1 WHERE id=?", (ROOT,)),
            ("UPDATE items SET parent_id='foreign' WHERE id=?", (ROOT,)),
            ("DELETE FROM outbox WHERE idem_key=?", (FIXTURE['outbox'][2]['idem_key'],)),
            ("UPDATE outbox SET result_ref='missing' WHERE idem_key=?", (FIXTURE['outbox'][2]['idem_key'],)),
            ("UPDATE amendment_answers SET question_card='t_deadbeef'", ()),
            ("UPDATE amendment_answers SET card='t_deadbeef'", ()),
            ("UPDATE amendment_answers SET task='other'", ()),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql):
                self.reset_failed()
                self.change(sql, *args)
                before = self.rows()
                for key in KEYS:
                    self.assertEqual(self.w.main('outbox', 'resend', key)[0], 1)
                self.assertEqual(self.rows(), before)
                self.assertEqual(self.w.cu.replies[ROOT], [])

    def test_resend_refuses_stale_authority_new_generation_or_changed_notification(self):
        cases = [
            ("UPDATE approvals SET state='stale' WHERE id=33", ()),
            ("UPDATE tasks SET input_gen=10,classified_gen=10,scope_gen=10", ()),
            ("UPDATE approvals SET plan_sha256=? WHERE id=33", ('f'*64,)),
            ("UPDATE blockers SET resolution_gen=10 WHERE id=5", ()),
            ("UPDATE blockers SET closure_item='wrong' WHERE id=5", ()),
            ("UPDATE outbox SET body=replace(body,'null','\"wrong\"') WHERE idem_key IN (?,?)", tuple(KEYS)),
        ]
        for sql, args in cases:
            with self.subTest(sql=sql):
                self.reset_failed()
                self.change(sql, *args)
                before = self.rows()
                for key in KEYS:
                    self.assertEqual(self.w.main('outbox', 'resend', key)[0], 1)
                self.assertEqual(self.rows(), before)

    def test_resend_leaves_sent_ambiguous_abandoned_and_possible_send_untouched(self):
        cases = [
            ("state='done', outcome='OK'", ()),
            ("state='held_ambiguous'", ()), # special NULL-root recovery; ordinary explicit resend is unchanged
            ("outcome='ABANDONED'", ()),
            ("attempts=1", ()),
            ("first_send_ms=123", ()),
            ("result_ref='remote'", ()),
            ("child_pid=123", ()),
            ("child_start_time='123'", ()),
        ]
        for assignment, args in cases:
            with self.subTest(assignment=assignment):
                self.reset_failed()
                self.change('UPDATE outbox SET ' + assignment + ' WHERE idem_key IN (?,?)', *args, *KEYS)
                before = self.rows()
                for key in KEYS:
                    self.assertEqual(self.w.main('outbox', 'resend', key)[0], 1)
                self.assertEqual(self.rows(), before)

    def test_repaired_send_rechecks_generation_before_any_http_post(self):
        self.seed_failed()
        for key in KEYS:
            self.assertEqual(self.w.main('outbox', 'resend', key)[0], 0)
        self.change('UPDATE tasks SET input_gen=10,classified_gen=10,scope_gen=10')
        self.cycle(2)
        self.assertEqual(self.w.cu.replies[ROOT], [])
        self.assertEqual([r['state'] for r in self.rows()], ['cancelled_stale'] * 2)

class DeliveryDiagnosticTest(unittest.TestCase):
    def setUp(self):
        import test_recovery as recovery
        from preworker_snapshot_fixture import install
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline-only effects'))
            p.start()
            self.addCleanup(p.stop)
        self.w = captured.comp.World(self, mode='shadow')
        self.w.board = self.b = recovery.RecoveryBoard(str(Path(self.w.dir) / 'native'), self.w.clock)
        install(self.b, self.w.dir)
        self.tid = self.w.cu.add_task()
        self.assertEqual(self.w.run()[0], 0)
        self.w.write_config(mode='live')
        self.work = self.b.worker_create('Synthetic healthy ongoing owner', assignee='planner')
        self.b.claim(self.work, 'planner')
        store = Store(self.w.state_path, self.w.clock)
        try:
            store.conn.execute('DELETE FROM outbox')
            store.conn.execute("INSERT INTO cards(card_id,task,role,seq) VALUES (?,?,'request',1)",
                               (self.work, self.tid))
            store.conn.execute("UPDATE tasks SET phase='planning',classified_gen=input_gen,quiet_until_ms=0")
            store.conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (engine.RECOVERY_KEY, '1'))
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            self.key = 'clickup-dp:' + self.tid + ':blocker:1:resumed'
            ctx.reply(self.tid, self.key, 'not-an-observed-root', 'The blocked step has resumed.')
        finally:
            store.close()

    def cycle(self, count=1):
        for n in range(count):
            self.w.clock.advance(61)
            self.b.worker_comment(self.work, 'planner', 'Healthy authorized work %d' % self.w.clock())
            self.assertEqual(self.w.run()[0], 0, self.w.outputs[-4:])

    def incident(self):
        return self.w.q("SELECT * FROM recoveries WHERE class='progress_stall'")[0]

    def test_delivery_diagnostic_keeps_budget_deadline_and_truthful_escalation(self):
        self.cycle(22)
        before = self.incident()
        for minutes, expected in ((61, 2), (181, 3), (361, 3)):
            self.w.clock.now_ms = before['detected_ms'] + minutes * 60000
            self.cycle()
            after = self.incident()
            self.assertEqual((after['id'], after['detected_ms'], after['abs_ms'], after['attempts']),
                             (before['id'], before['detected_ms'], before['abs_ms'], expected))
        notices = self.w.q("SELECT body,state FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['state'], 'done')
        text = json.loads(notices[0]['body'])['text']
        self.assertIn('notification', text)
        self.assertIn(self.key, text)
        self.assertNotIn('Work remains safely stopped', text)
        self.assertNotIn('workflow remains stopped', text)
        self.w.clock.now_ms = before['abs_ms'] + 1
        self.cycle(2)
        self.assertEqual(self.incident()['attempts'], 3)
        self.assertEqual(self.b.cards[self.work]['task']['status'], 'running')
        self.assertEqual(len(self.b.by_key('clickup-dp:' + self.tid + ':rcv:')), 3)

    def test_non_failure_and_abandoned_notifications_do_not_open_delivery_diagnostics(self):
        for state, outcome in (('done','OK'), ('cancelled_stale','STALE'),
                               ('cancelled_stale','ABANDONED'), ('held_ambiguous','AMBIGUOUS')):
            with self.subTest(state=state, outcome=outcome):
                self.doCleanups()
                self.setUp()
                store = Store(self.w.state_path, self.w.clock)
                try:
                    store.conn.execute('UPDATE outbox SET state=?,outcome=? WHERE idem_key=?',
                                       (state, outcome, self.key))
                finally:
                    store.close()
                self.cycle(22)
                self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])

    def test_operator_hold_and_disabled_recovery_prevent_delivery_wake(self):
        for blocked in ('operator', 'disabled'):
            with self.subTest(blocked=blocked):
                self.doCleanups()
                self.setUp()
                self.cycle()
                store = Store(self.w.state_path, self.w.clock)
                try:
                    if blocked == 'operator':
                        store.conn.execute("UPDATE tasks SET hold_reason='operator'")
                    else:
                        store.conn.execute('DELETE FROM meta WHERE key=?', (engine.RECOVERY_KEY,))
                finally:
                    store.close()
                self.cycle(22)
                self.assertEqual(self.w.q('SELECT * FROM recoveries'), [])
                self.assertEqual(self.b.cards[self.work]['task']['status'], 'running')

    def test_current_product_wait_does_not_hide_delivery_without_other_incident(self):
        from clickup_intake import progress
        self.cycle()
        root = self.w.cu.add_comment(self.tid, captured.comp.BOT, 'Which required external capacity is available?')
        store = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            key = 'clickup-dp:' + self.tid + ':q:business:delivery-control'
            ctx.comment(self.tid, key, 'Which required external capacity is available?')
            store.conn.execute("UPDATE outbox SET state='done',outcome='OK',attempts=1,result_ref=? WHERE idem_key=?", (root, key))
            store.conn.execute("UPDATE tasks SET phase='awaiting_business'")
        finally:
            store.close()
        self.b.cards[self.work]['task']['status'] = 'blocked'
        self.b.cards[self.work]['runs'][-1].update(status='blocked', outcome='blocked')
        before = copy.deepcopy(self.b.cards[self.work])
        self.cycle(22)
        inc = self.incident()
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        [create] = self.w.q("SELECT body FROM outbox WHERE kind='kb_create' AND idem_key LIKE '%:rcv:%'")
        self.assertIn(self.key, create['body'])
        store = Store(self.w.state_path, self.w.clock)
        try:
            self.assertIs(progress.human_wait(store.conn, self.tid), True)
        finally:
            store.close()
        for field in ('task', 'runs', 'parents'):
            self.assertEqual(self.b.cards[self.work][field], before[field])

    def test_foreign_refusal_reaches_bounded_diagnostic_with_healthy_work(self):
        self.cycle(22)
        [refused] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.key)
        self.assertEqual((refused['state'], refused['outcome'], refused['attempts']),
                         ('cancelled_stale', 'FOREIGN_ROOT', 0))
        self.assertEqual(self.b.cards[self.work]['task']['status'], 'running')
        incidents = self.w.q("SELECT * FROM recoveries WHERE class='progress_stall'")
        self.assertEqual(len(incidents), 1, 'FOREIGN_ROOT needs AI ownership even while the owner is progressing')
        [inc] = incidents
        self.assertEqual(inc['attempts'], 1)
        base = engine._rcv_key(self.tid, inc['card'], inc['run_id'], 1)
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', base + ':wake')
        self.assertEqual(wake['state'], 'done')
        [create] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', base + ':create')
        carrier = self.b.cards[create['result_ref']]
        self.assertEqual([s['delivery_mode'] for s in carrier['subs']], ['wake'])
        self.assertIn(self.key, carrier['task']['body'])
        self.assertIn('Diagnose', carrier['task']['body'])
        rc, lines = self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id']))
        self.assertEqual(rc, 0, lines)
        self.cycle(2)
        [after] = self.w.q('SELECT * FROM recoveries WHERE id=?', inc['id'])
        self.assertEqual((after['attempts'], after['detected_ms'], after['abs_ms']),
                         (inc['attempts'], inc['detected_ms'], inc['abs_ms']))
        self.assertEqual(self.b.cards[self.work]['task']['status'], 'running')
        self.assertFalse([p for m,p,_ in self.w.cu.calls if m == 'POST' and '/reply' in p])
        print('DELIVERY_DIAGNOSTIC_RESULT ' + json.dumps(dict(incident=after, refused=refused,
            carrier=create['result_ref'], wake=wake, admission=lines), sort_keys=True), flush=True)


class ExistingHumanWaitDeliveryTest(unittest.TestCase):
    import test_preworker_recovery as stage
    setUp = stage.PreworkerRecoveryTest.setUp
    applications = stage.PreworkerRecoveryTest.applications
    incidents = stage.PreworkerRecoveryTest.incidents
    carriers = stage.PreworkerRecoveryTest.carriers
    scans = stage.PreworkerRecoveryTest.scans
    change = stage.PreworkerRecoveryTest.change
    stall = stage.PreworkerRecoveryTest.stall
    stopped_stage = stage.PreworkerRecoveryTest.stopped_stage
    wait = stage.PreworkerRecoveryTest.wait
    question = stage.PreworkerRecoveryTest.question

    def verified_wait(self):
        self.scans(18)
        [inc] = self.incidents()
        self.assertEqual((inc['class'], inc['state'], inc['attempts']), ('approval_staging', 'open', 1))
        self.question(inc)
        self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        wait = copy.deepcopy(self.wait())
        self.assertTrue(wait['root'])
        self.assertTrue(wait['verified_ms'])
        self.assertTrue(any(c['id'] == wait['root'] and wait['question']['decision'] in c['comment_text']
                            for c in self.w.bot_comments(self.tid)))
        return inc, wait

    def queue_failure(self):
        self.key = 'clickup-dp:' + self.tid + ':blocker:999:resumed'
        store = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            ctx.reply(self.tid, self.key, 'not-an-observed-root', 'The blocked step has resumed.')
        finally:
            store.close()

    def delivery_carriers(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND idem_key LIKE '%:rcv:%' AND body LIKE ?",
                        '%' + self.key + '%')

    def assert_same_incident(self, before):
        [after] = self.incidents()
        for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'event_created_s', 'block_kind',
                      'approval_id', 'approval_rev', 'plan_sha256', 'reason_sha256', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], before[field], field)
        self.assertLessEqual(after['attempts'], engine.RECOVERY_ATTEMPTS)
        return after

    def test_open_verified_wait_transfers_only_delivery_diagnosis_with_same_budget(self):
        with self.stopped_stage():
            before, wait = self.verified_wait()
            self.queue_failure()
            self.scans(22)
        [failed] = self.w.q('SELECT state,outcome,attempts FROM outbox WHERE idem_key=?', self.key)
        self.assertEqual(failed, dict(state='cancelled_stale', outcome='FOREIGN_ROOT', attempts=0))
        after = self.assert_same_incident(before)
        self.assertEqual(self.wait(), wait)
        carriers = self.delivery_carriers()
        self.assertEqual(len(carriers), 1, 'OPEN human wait must not suppress independent delivery ownership')
        self.assertEqual(after['attempts'], before['attempts'] + 1)
        [create] = carriers
        self.assertEqual(create['state'], 'done')
        [wake] = self.w.q('SELECT state FROM outbox WHERE idem_key=?', create['idem_key'][:-6] + 'wake')
        self.assertEqual(wake['state'], 'done')
        body = self.b.cards[create['result_ref']]['task']['body']
        self.assertIn('Diagnose delivery only', body)
        self.assertIn('do not', body)
        rc, lines = self.w.main('recovery-check', '--card', after['card'], '--run', '0')
        self.assertEqual(rc, 0, lines)
        self.assertIn('scope=delivery-only', lines[0])
        # Remove the synthetic writer stop: the actual question gate must still pause product work.
        self.scans(5)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        self.assertEqual(self.wait(), wait)
        self.assertEqual(self.delivery_carriers(), carriers)
        self.assertEqual(self.assert_same_incident(before)['attempts'], after['attempts'])
        print('OPEN_WAIT_DELIVERY_RESULT ' + json.dumps(dict(before=before, after=after, wait=wait,
              failed=failed, carrier=create['result_ref'], admission=lines), sort_keys=True), flush=True)


    def converted(self):
        with self.stopped_stage():
            before, wait = self.verified_wait()
            self.queue_failure()
            self.scans(22)
        self.assertEqual(len(self.delivery_carriers()), 1)
        self.assertEqual(self.w.main('recovery-check', '--card', before['card'], '--run', '0')[0], 0)
        return self.assert_same_incident(before), wait

    def test_original_question_answer_still_classifies_and_releases_only_normal_stage(self):
        inc, wait = self.converted()
        self.life.say(self.stage.composition.APPROVER_A, 'Use existing approved account A.',
                      mention=self.stage.composition.BOT, reply_to=wait['root'])
        def answer(card, ref, gen):
            self.b.park(card, self.stage.composition.classify(ref, gen, 'answer_only'),
                        self.stage.composition.out('resolution', ref, 'Use existing approved account A.',
                            gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.wait()['question'], wait['question'])
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        self.assert_same_incident(inc)

    def test_resolved_delivery_revokes_role_without_new_wakes_or_product_release(self):
        inc, wait = self.converted()
        carriers = self.delivery_carriers()
        self.change("UPDATE outbox SET state='done',outcome='OK' WHERE idem_key=?", self.key)
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(5)
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
        self.assertEqual(self.delivery_carriers(), carriers)
        self.assertEqual(self.assert_same_incident(inc)['attempts'], inc['attempts'])
        self.assertEqual(self.wait(), wait)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())

    def test_delivery_role_admission_revalidates_binding_and_original_wait(self):
        inc, wait = self.converted()
        store = Store(self.w.state_path, self.w.clock)
        original = dict(store.conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
        approval = dict(store.conn.execute('SELECT * FROM approvals WHERE id=?', (inc['approval_id'],)).fetchone())
        store.close()
        cases = [
            ("UPDATE tasks SET scope_gen=?", (approval['accounts_gen'] + 1,), "UPDATE tasks SET scope_gen=?", (original['scope_gen'],)),
            ("UPDATE approvals SET state='revoked'", (), "UPDATE approvals SET state=?", (approval['state'],)),
            ("UPDATE tasks SET hold_reason='operator'", (), "UPDATE tasks SET hold_reason=NULL", ()),
            ("UPDATE items SET deleted=1 WHERE id=?", (wait['root'],), "UPDATE items SET deleted=0 WHERE id=?", (wait['root'],)),
            ("UPDATE outbox SET state='held_ambiguous' WHERE idem_key=?", (wait['key'],), "UPDATE outbox SET state='done' WHERE idem_key=?", (wait['key'],)),
            ("UPDATE recoveries SET state='closed'", (), "UPDATE recoveries SET state='open'", ()),
            ("UPDATE outbox SET outcome='ABANDONED' WHERE idem_key=?", (self.delivery_carriers()[0]['idem_key'],),
             "UPDATE outbox SET outcome='OK' WHERE idem_key=?", (self.delivery_carriers()[0]['idem_key'],)),
        ]
        for sql, args, restore, values in cases:
            with self.subTest(sql=sql):
                self.change(sql, *args)
                self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
                self.change(restore, *values)
                self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.assertEqual(self.w.main('recovery', 'disable')[0], 0)
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)

    def test_unverified_or_foreign_question_cannot_transfer_delivery_responsibility(self):
        for field, value in (('task', 'other-task'), ('fingerprint', 'f' * 64), ('approval', 999)):
            with self.subTest(field=field):
                self.doCleanups()
                self.setUp()
                with self.stopped_stage():
                    inc, wait = self.verified_wait()
                    altered = copy.deepcopy(wait)
                    altered['question'][field] = value
                    self.change('UPDATE meta SET value=? WHERE key=?', json.dumps(altered), 'diagnostic_wait:%d' % inc['id'])
                    self.queue_failure()
                    self.scans(22)
                self.assertEqual(self.delivery_carriers(), [], 'foreign question binding is not delivery-only authority')
                self.assertEqual(self.incidents()[0]['attempts'], inc['attempts'])
                self.assertEqual(self.wait(), altered)
                self.assertIsNone(self.life.orch())

    def test_missing_proof_exhaustion_and_cap_cannot_transfer_or_refund(self):
        for guard in ('unverified', 'ambiguous', 'exhausted', 'deadline', 'cap', 'operator', 'disabled'):
            with self.subTest(guard=guard):
                self.doCleanups()
                self.setUp()
                with self.stopped_stage():
                    inc, wait = self.verified_wait()
                    expected = inc['attempts']
                    if guard == 'unverified':
                        changed = dict(wait, verified_ms=None)
                        self.change('UPDATE meta SET value=? WHERE key=?', json.dumps(changed), 'diagnostic_wait:%d' % inc['id'])
                    elif guard == 'ambiguous':
                        self.change("UPDATE outbox SET state='held_ambiguous' WHERE idem_key=?", wait['key'] + '#cu')
                    elif guard == 'exhausted':
                        expected = engine.RECOVERY_ATTEMPTS
                        self.change('UPDATE recoveries SET attempts=? WHERE id=?', expected, inc['id'])
                    elif guard == 'deadline':
                        self.change('UPDATE recoveries SET abs_ms=? WHERE id=?', self.w.clock(), inc['id'])
                    elif guard == 'cap':
                        store = Store(self.w.state_path, self.w.clock)
                        try:
                            for run in (101, 102, 103):
                                row = dict(inc, run_id=run, state='closed')
                                row.pop('id')
                                StatusReplyDeliveryTest.insert(store.conn, 'recoveries', row)
                        finally:
                            store.close()
                    elif guard == 'operator':
                        self.change("UPDATE tasks SET hold_reason='operator'")
                    else:
                        self.assertEqual(self.w.main('recovery', 'disable')[0], 0)
                    self.queue_failure()
                    self.scans(22)
                self.assertEqual(self.delivery_carriers(), [])
                self.assertEqual(self.incidents()[0]['attempts'], expected)
                self.assertIsNone(self.life.orch())

    def test_original_deadline_and_remaining_attempt_are_not_refunded(self):
        inc, wait = self.converted()
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(5)
        after = self.assert_same_incident(inc)
        self.assertEqual(after['attempts'], 3)
        self.assertEqual(len(self.delivery_carriers()), 2)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(5)
        self.assertEqual(self.assert_same_incident(inc)['attempts'], 3)
        self.assertEqual(len(self.delivery_carriers()), 2)
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
        self.assertEqual(self.wait(), wait)
        self.assertIsNone(self.life.orch())


    def supported_failure(self):
        """Persist real fake-HTTP failures, rather than fabricating delivery state."""
        from clickup_intake.contracts import Outcome
        self.key = 'clickup-dp:' + self.tid + ':independent-delivery-lifecycle'
        store = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            ctx.comment(self.tid, self.key, 'Independent delivery lifecycle notification.')
        finally:
            store.close()
        for _ in range(6):
            self.w.cu.inject('POST', '/v2/task/' + self.tid + '/comment', Outcome.DEFINITE_FAIL, status=503)
        for _ in range(20):
            self.scans(1)
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.key)
            if row['state'] == 'failed':
                break
            if row['next_attempt_ms'] and row['next_attempt_ms'] > self.w.clock():
                self.w.clock.now_ms = row['next_attempt_ms'] + 1000
        self.assertEqual((row['state'], row['attempts']), ('failed', 6))
        return row

    def supported_lifecycle(self, complete_before_answer):
        from clickup_intake import progress
        with self.stopped_stage():
            initial, wait = self.verified_wait()
            failure = self.supported_failure()
            self.scans(22)
        converted = self.assert_same_incident(initial)
        self.assertEqual(converted['attempts'], initial['attempts'] + 1)
        [carrier] = self.delivery_carriers()
        self.assertEqual(carrier['state'], 'done')
        [active_role] = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid)
        active_role = json.loads(active_role['value'])
        self.assertIn('scope=delivery-only', self.w.main(
            'recovery-check', '--card', initial['card'], '--run', '0')[1][0])
        self.scans(3)
        self.assertEqual(self.wait(), wait)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())

        def resend():
            rc, lines = self.w.main('outbox', 'resend', self.key)
            self.assertEqual(rc, 0, lines)

        def deliver():
            self.w.step(lambda: self.w.q('SELECT state FROM outbox WHERE idem_key=?', self.key)[0]['state'] == 'done')
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', self.key)
            self.assertEqual(row['outcome'], 'OK')
            response = self.w.cu.request('GET', '/v2/task/' + self.tid + '/comment')
            self.assertTrue(any(c['id'] == row['result_ref'] and
                'Independent delivery lifecycle notification.' in c['comment_text']
                for c in response.json['comments']))
            return row

        resend()
        if complete_before_answer:
            delivered = deliver()
            self.scans(3)
            self.assertEqual(self.wait(), wait)
            self.assertIsNone(self.life.gate())
            self.assertIsNone(self.life.orch())
        self.life.say(self.stage.composition.APPROVER_A, 'Use existing approved account A.',
                      mention=self.stage.composition.BOT, reply_to=wait['root'])
        def answer(card, ref, gen):
            self.b.park(card, self.stage.composition.classify(ref, gen, 'answer_only'),
                        self.stage.composition.out('resolution', ref, 'Use existing approved account A.',
                            gen=gen, diagnostic=initial['id'], authority='unchanged'))
        real = main.Glue.clickup
        # The supported resend removes its failure hold. Delay only this payload
        # at the fake transport boundary to exercise answer-before-delivery.
        with mock.patch.object(main.Glue, 'clickup', lambda glue, row:
                False if not complete_before_answer and row['idem_key'] in (self.key, self.key + '#cu')
                else real(glue, row)):
            self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        if not complete_before_answer:
            self.assertNotEqual(self.w.q('SELECT state FROM outbox WHERE idem_key=?', self.key)[0]['state'], 'done')
            delivered = deliver()
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        ready = self.assert_same_incident(initial)
        self.assertEqual(ready['state'], 'open', 'READY without actual owner work is not recovery')
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        self.life.orchestrate()
        self.scans(6)
        [settled] = self.incidents()
        store = Store(self.w.state_path, self.w.clock)
        try:
            context = progress.context(store.conn, self.tid)
            history = store.conn.execute('SELECT value FROM meta WHERE key=?',
                ('diagnostic_history:%d:%s' % (initial['id'], initial['reason_sha256']),)).fetchone()
        finally:
            store.close()
        self.assertTrue(history)
        self.assertEqual(json.loads(history[0]), initial)
        self.assertEqual(self.wait()['question'], wait['question'])
        self.assertEqual(self.wait()['resolution'], 'Use existing approved account A.')
        owner = self.b.cards[self.life.orch()]
        self.assertTrue(owner['runs'])
        self.assertIsNotNone(self.stage.kp.latest_run(owner)['metadata']['clickup_gates'])

        # A later independent refused root must obtain a fresh usable AI owner.
        self.queue_failure()
        self.scans(22)
        incidents = self.incidents()
        later = self.delivery_carriers()
        checks = [self.w.main('recovery-check', '--card', i['card'], '--run', str(i['run_id'])) for i in incidents]
        print('SUPPORTED_ROLE_LIFECYCLE ' + json.dumps(dict(before=initial, converted=converted,
            ready=ready, settled=settled, failure=failure, delivered=delivered,
            complete_before_answer=complete_before_answer, role=context.get('delivery_role'),
            later_incidents=incidents, later_carriers=later, checks=checks), sort_keys=True), flush=True)
        self.assertEqual(settled['state'], 'resumed', 'stale delivery role masks verified normal owner progress')
        self.assertIsNone(context.get('delivery_role'))
        [retired] = self.w.q('SELECT value FROM meta WHERE key LIKE ?',
                            'diagnostic_delivery_history:%d:%%' % initial['id'])
        retired = json.loads(retired['value'])
        self.assertEqual(retired['role'], active_role)
        self.assertEqual(retired['original_class'], initial['class'])
        for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'event_created_s', 'block_kind',
                      'approval_id', 'approval_rev', 'plan_sha256', 'reason_sha256', 'detected_ms', 'abs_ms'):
            self.assertEqual(settled[field], initial[field], field)
        self.assertEqual(settled['attempts'], converted['attempts'])
        self.assertEqual(len(incidents), 2)
        self.assertEqual(len(later), 1, 'later independent failure needs its own diagnostic carrier')
        self.assertEqual(later[0]['state'], 'done')
        self.assertEqual(checks[0][0], 3)
        self.assertEqual(checks[1][0], 0, checks[1])

    def test_invalid_delivery_wait_is_not_retirement_or_operation_permission(self):
        inc, wait = self.converted()
        before = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid)
        comments = self.w.cu.comments[self.tid]
        root = next(c for c in comments if c['id'] == wait['root'])
        comments.remove(root)
        self.change('UPDATE items SET deleted=1 WHERE id=?', wait['root'])
        self.scans(3)
        self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), before)
        self.assertEqual(self.assert_same_incident(inc)['class'], inc['class'])
        self.assertEqual(self.wait(), wait)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 3)
        comments.append(root)
        self.change('UPDATE items SET deleted=0 WHERE id=?', wait['root'])
        self.scans(1)
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)

    def test_answer_transition_retires_role_with_unresolved_foreign_root_without_refunding(self):
        inc, wait = self.converted()
        self.life.say(self.stage.composition.APPROVER_A, 'Use existing approved account A.',
                      mention=self.stage.composition.BOT, reply_to=wait['root'])
        def answer(card, ref, gen):
            self.b.park(card, self.stage.composition.classify(ref, gen, 'answer_only'),
                        self.stage.composition.out('resolution', ref, 'Use existing approved account A.',
                            gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), [])
        self.assertEqual(self.assert_same_incident(inc)['attempts'], inc['attempts'])
        self.assertEqual(self.wait()['question'], wait['question'])
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        [failed] = self.w.q('SELECT state,outcome,attempts FROM outbox WHERE idem_key=?', self.key)
        self.assertEqual(failed, dict(state='cancelled_stale', outcome='FOREIGN_ROOT', attempts=0))

    def newer_approval(self, inc):
        # Exact transition from the unchanged preworker newer-approval regression.
        self.change("INSERT INTO approvals(task,rev,accounts_gen,comment_id,author_id,author_name,comment_date,"
                    "text_hash,plan_sha256,state) SELECT task,rev+1,accounts_gen,comment_id||'-new',author_id,"
                    "author_name,comment_date+1,text_hash,plan_sha256,'released' FROM approvals WHERE id=?",
                    inc['approval_id'])

    def test_newer_approval_supersedes_delivery_role_and_later_failure_is_current(self):
        inc, wait = self.converted()
        [active_role] = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid)
        native_before = copy.deepcopy(self.b.cards)
        old_key = self.key
        self.newer_approval(inc)
        self.scans(5)
        before_expiry = copy.deepcopy(self.incidents())
        later_key = 'clickup-dp:' + self.tid + ':later-independent-refusal'
        store = Store(self.w.state_path, self.w.clock)
        try:
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            ctx.reply(self.tid, later_key, 'other-unobserved-root', 'Later independent notification.')
        finally:
            store.close()
        self.scans(22)
        after_later = copy.deepcopy(self.incidents())
        roles = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid)
        later = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND body LIKE ?", '%' + later_key + '%')
        checks = [self.w.main('recovery-check', '--card', i['card'], '--run', str(i['run_id']))
                  for i in after_later]
        self.w.clock.now_ms = inc['abs_ms'] + 60000
        self.scans(5)
        after_expiry = self.incidents()
        print('SUPERSEDED_DELIVERY_RESULT ' + json.dumps(dict(before=inc, wait_before=wait,
            wait_after=self.wait(), before_expiry=before_expiry, after_later=after_later,
            after_expiry=after_expiry, retained_role=roles, later_carriers=later, checks=checks),
            sort_keys=True), flush=True)
        self.assertEqual(after_expiry[0]['state'], 'superseded',
                         'Obsolete converted owner persists and suppresses new diagnostics')
        self.assertEqual(before_expiry[0]['state'], 'superseded')
        self.assertEqual(roles, [])
        self.assertEqual(self.wait(), wait)
        for field in ('id', 'task', 'card', 'run_id', 'event_kind', 'event_created_s', 'block_kind',
                      'approval_id', 'approval_rev', 'plan_sha256', 'reason_sha256', 'detected_ms',
                      'abs_ms', 'attempts'):
            self.assertEqual(after_expiry[0][field], inc[field], field)
        self.assertEqual(after_expiry[0]['class'], 'approval_staging')
        [history] = self.w.q('SELECT value FROM meta WHERE key LIKE ?',
                            'diagnostic_delivery_history:%d:%%' % inc['id'])
        self.assertEqual(json.loads(history['value'])['role'], json.loads(active_role['value']))
        self.assertEqual(len(after_later), 2)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]['state'], 'done')
        [wake] = self.w.q('SELECT state FROM outbox WHERE idem_key=?', later[0]['idem_key'][:-6] + 'wake')
        self.assertEqual(wake['state'], 'done')
        self.assertEqual(checks[0][0], 3)
        self.assertEqual(checks[1][0], 0, checks[1])
        self.assertEqual(self.w.q('SELECT state,outcome,attempts FROM outbox WHERE idem_key=?', old_key),
                         [dict(state='cancelled_stale', outcome='FOREIGN_ROOT', attempts=0)])
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        for card, before in native_before.items():
            self.assertEqual(self.b.cards[card], before)

    def test_supersession_requires_positive_identity_and_available_authority(self):
        for guard in ('current', 'revoked_successor', 'missing_approval', 'changed_approval',
                      'missing_history', 'changed_history', 'missing_wait', 'unavailable_read',
                      'operator', 'disabled'):
            with self.subTest(guard=guard):
                self.doCleanups()
                self.setUp()
                inc, wait = self.converted()
                role = self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid)
                carriers = copy.deepcopy(self.delivery_carriers())
                native = copy.deepcopy(self.b.cards)
                if guard != 'current':
                    self.newer_approval(inc)
                history_key = 'diagnostic_history:%d:%s' % (inc['id'], inc['reason_sha256'])
                if guard == 'revoked_successor':
                    self.change("UPDATE approvals SET state='revoked' WHERE id>?", inc['approval_id'])
                elif guard == 'missing_approval':
                    self.change('DELETE FROM approvals WHERE id=?', inc['approval_id'])
                elif guard == 'changed_approval':
                    self.change('UPDATE approvals SET plan_sha256=? WHERE id=?', 'f' * 64, inc['approval_id'])
                elif guard == 'missing_history':
                    self.change('DELETE FROM meta WHERE key=?', history_key)
                elif guard == 'changed_history':
                    [history] = self.w.q('SELECT value FROM meta WHERE key=?', history_key)
                    changed = dict(json.loads(history['value']), detected_ms=0)
                    self.change('UPDATE meta SET value=? WHERE key=?', json.dumps(changed), history_key)
                elif guard == 'missing_wait':
                    self.w.cu.comments[self.tid] = [c for c in self.w.cu.comments[self.tid] if c['id'] != wait['root']]
                    self.change('UPDATE items SET deleted=1 WHERE id=?', wait['root'])
                elif guard == 'operator':
                    self.change("UPDATE tasks SET hold_reason='operator'")
                elif guard == 'disabled':
                    self.assertEqual(self.w.main('recovery', 'disable')[0], 0)
                expected = copy.deepcopy(self.incidents())
                if guard == 'disabled':
                    self.assertEqual(expected[0]['state'], 'disabled')
                else:
                    self.assertEqual(expected, [inc])
                request = self.w.cu.request
                unavailable_reads = []
                def unavailable(method, path, *args, **kwargs):
                    if guard == 'unavailable_read' and method == 'GET' and path == '/v2/task/' + self.tid + '/comment':
                        unavailable_reads.append(path)
                        return self.stage.TransportResult(self.stage.Outcome.DEFINITE_FAIL, 503)
                    return request(method, path, *args, **kwargs)
                with mock.patch.object(self.w.cu, 'request', side_effect=unavailable):
                    self.scans(3)
                if guard == 'unavailable_read':
                    self.assertTrue(unavailable_reads, 'the failed observation must actually be requested')
                self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), role)
                self.assertEqual(self.incidents(), expected)
                self.assertEqual(self.delivery_carriers(), carriers)
                self.assertEqual(self.wait(), wait)
                self.assertIsNone(self.life.gate())
                self.assertIsNone(self.life.orch())
                self.assertEqual(self.b.cards, native)
                if guard == 'unavailable_read':
                    self.scans(5)
                    self.assertEqual(self.incidents()[0]['state'], 'superseded')
                    self.assertEqual(self.incidents()[0]['attempts'], inc['attempts'])
                    self.assertEqual(self.incidents()[0]['abs_ms'], inc['abs_ms'])
                    self.assertEqual(self.wait(), wait)
                    self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), [])

    def test_definite_supersession_after_exhaustion_preserves_spent_budget_and_deadline(self):
        for exhausted in ('deadline', 'attempts'):
            with self.subTest(exhausted=exhausted):
                self.doCleanups()
                self.setUp()
                inc, wait = self.converted()
                if exhausted == 'deadline':
                    self.w.clock.now_ms = inc['abs_ms'] + 60000
                else:
                    self.change('UPDATE recoveries SET attempts=? WHERE id=?', engine.RECOVERY_ATTEMPTS, inc['id'])
                    inc['attempts'] = engine.RECOVERY_ATTEMPTS
                self.newer_approval(inc)
                self.scans(5)
                old = self.incidents()[0]
                self.assertEqual(old['state'], 'superseded')
                self.assertEqual(old['class'], 'approval_staging')
                for field in ('id', 'approval_id', 'approval_rev', 'plan_sha256', 'reason_sha256',
                              'detected_ms', 'abs_ms', 'attempts'):
                    self.assertEqual(old[field], inc[field], field)
                self.assertEqual(self.wait(), wait)
                self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), [])
                self.assertIsNone(self.life.gate())
                self.assertIsNone(self.life.orch())

    def test_supported_delivery_completion_before_answer_retires_role_and_later_failure_is_current(self):
        self.supported_lifecycle(True)

    def test_supported_delivery_completion_after_answer_retires_role_and_later_failure_is_current(self):
        self.supported_lifecycle(False)


import test_general_stall_wake as _general_stall


class SourceProgressDeliveryTest(unittest.TestCase):
    setUp = _general_stall.GeneralStallWakeTest.setUp
    scans = _general_stall.GeneralStallWakeTest.scans
    carriers = _general_stall.GeneralStallWakeTest.carriers
    invalid_plan = _general_stall.GeneralStallWakeTest.invalid_plan
    supported_failure = ExistingHumanWaitDeliveryTest.supported_failure

    def wait(self):
        rows = self.w.q("SELECT value FROM meta WHERE key LIKE 'diagnostic_wait:%' ORDER BY key")
        return json.loads(rows[0]['value']) if rows else None

    def source_delivery(self, approved, *, transport=True):
        if approved:
            self.life.pickup_to_business()
            self.life.business_to_technical()
            self.life.technical_to_published()
            self.life.say(_general_stall.composition.APPROVER_A, 'Approve plan r1 for development')
            self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
            source = self.life.orch()
            self.b.claim(source, 'orchestrator')
            self.b.finish(source, 'orchestrator', {})
        else:
            source = self.invalid_plan()
        self.scans(18)
        [initial] = self.w.q('SELECT * FROM recoveries ORDER BY id')
        self.assertEqual(initial['class'], 'progress_stall')
        self.assertEqual(initial['approval_id'] is not None, approved)
        question = dict(task=self.tid, card=initial['card'], run=initial['run_id'],
            approval=initial['approval_id'], revision=initial['approval_rev'],
            sha256=initial['plan_sha256'], fingerprint=initial['reason_sha256'],
            failed='The planning recipient is ambiguous.', attempted='Read current task and handoff.',
            decision='Which existing planning contact should receive the clarification, A or B?',
            next_action='Reply with the contact, without credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(self.carriers()[-1], 'default',
            'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\nCLICKUP-RECOVERY-QUESTION v1 %s'
            % (initial['card'], initial['run_id'], json.dumps(question)))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        wait = copy.deepcopy(self.wait())
        self.assertTrue(wait['verified_ms'])
        self.assertTrue(wait['root'])
        source_before = copy.deepcopy(self.b.cards[source])
        if transport:
            self.supported_failure()
        else:
            ExistingHumanWaitDeliveryTest.queue_failure(self)
        self.scans(22)
        [after] = self.w.q('SELECT * FROM recoveries ORDER BY id')
        for field in ('id', 'card', 'run_id', 'approval_id', 'approval_rev', 'plan_sha256',
                      'reason_sha256', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], initial[field], field)
        self.assertEqual(after['attempts'], initial['attempts'] + 1)
        self.assertEqual(self.wait(), wait)
        self.assertEqual(self.b.cards[source], source_before)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + self.key + '#cu' if transport else None)
        key = engine._rcv_key(self.tid, after['card'], after['run_id'], after['attempts']) + ':wake'
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
        self.assertEqual(wake['state'], 'done', (wake['state'], wake['outcome']))
        self.assertEqual(wake['attempts'], 1)
        check = self.w.main('recovery-check', '--card', after['card'], '--run', str(after['run_id']))
        self.assertEqual(check[0], 0, check)
        self.assertIn('scope=delivery-only', check[1][0])
        return after, wait

    def test_unbound_source_delivery_failure_wakes_without_clearing_hold(self):
        self.source_delivery(False)

    def test_approved_source_delivery_failure_wakes_without_clearing_hold(self):
        self.source_delivery(True)

    def test_source_delivery_role_does_not_admit_other_holds(self):
        incident, wait = self.source_delivery(False)
        original = self.w.task(self.tid)['hold_reason']
        for hold in ('operator', 'unknown-manual-hold', 'outbox:unrelated-effect#cu'):
            with self.subTest(hold=hold):
                store = Store(self.w.state_path, self.w.clock)
                try:
                    store.conn.execute('UPDATE tasks SET hold_reason=? WHERE clickup_task_id=?', (hold, self.tid))
                finally:
                    store.close()
                check = self.w.main('recovery-check', '--card', incident['card'], '--run', str(incident['run_id']))
                self.assertNotEqual(check[0], 0, check)
                self.assertEqual(self.w.task(self.tid)['hold_reason'], hold)
                self.assertEqual(self.wait(), wait)
        store = Store(self.w.state_path, self.w.clock)
        try:
            store.conn.execute('UPDATE tasks SET hold_reason=? WHERE clickup_task_id=?', (original, self.tid))
        finally:
            store.close()
        self.assertEqual(self.w.main('recovery-check', '--card', incident['card'], '--run', str(incident['run_id']))[0], 0)

    def test_source_delivery_transport_hold_still_requires_exact_wait_binding(self):
        incident, wait = self.source_delivery(False)
        store = Store(self.w.state_path, self.w.clock)
        try:
            role = json.loads(store.conn.execute('SELECT value FROM meta WHERE key=?',
                              ('diagnostic_delivery:' + self.tid,)).fetchone()[0])
            role['wait_sha256'] = 'changed'
            store.conn.execute('UPDATE meta SET value=? WHERE key=?',
                               (json.dumps(role), 'diagnostic_delivery:' + self.tid))
        finally:
            store.close()
        check = self.w.main('recovery-check', '--card', incident['card'], '--run', str(incident['run_id']))
        self.assertNotEqual(check[0], 0, check)
        self.assertEqual(self.wait(), wait)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + self.key + '#cu')

    def test_source_delivery_resend_and_original_answer_retire_scoped_role(self):
        incident, wait = self.source_delivery(False)
        self.assertEqual(self.w.main('outbox', 'resend', self.key)[0], 0)
        self.w.step(lambda: self.w.q('SELECT state FROM outbox WHERE idem_key=?', self.key)[0]['state'] == 'done')
        self.life.say(_general_stall.composition.APPROVER_A, 'Use existing planning contact A.',
                      mention=_general_stall.composition.BOT, reply_to=wait['root'])
        def planner(card, ref, gen):
            self.b.park(card, _general_stall.composition.classify(ref, gen, 'answer_only'),
                _general_stall.composition.out('resolution', ref, 'Use existing planning contact A.',
                    gen=gen, diagnostic=incident['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(planner))
        self.scans(3)
        self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), [])
        [after] = self.w.q('SELECT * FROM recoveries WHERE id=?', incident['id'])
        for field in ('id', 'card', 'run_id', 'attempts', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], incident[field], field)
        self.assertEqual(self.wait()['question'], wait['question'])
        self.assertIsNone(self.w.task(self.tid)['hold_reason'])


    def test_newer_real_approval_retires_source_role_and_allows_later_delivery(self):
        incident, wait = self.source_delivery(True, transport=False)
        self.life.say(_general_stall.composition.APPROVER_A,
            'Please revise the plan to add a light-mode option in addition to dark mode.',
            mention=_general_stall.composition.BOT)
        observed = []
        plan = '## ClickUp summary\nAdd dark and light modes.\n\n## Plan\nAdd an explicit light-mode option.\n'
        def publish(card, ref, gen):
            observed.append((card, ref, gen))
            self.b.worker_comment(card, 'planner', _general_stall.composition.classify(ref, gen, 'material'))
            sha = self.b.request_review(card, self.tid, 2, plan, gen)
            self.b.claim(card, 'plan-reviewer')
            self.b.finish(card, 'plan-reviewer', dict(clickup_task=self.tid, plan_revision=2,
                plan_attachment='clickup-plan-r2.md', plan_sha256=sha, accounts_gen=gen,
                review_outcome='PASS', reviewed_sha256=sha))
        self.w.step(lambda: self.w.task(self.tid)['published_rev'] == 2,
                    world=self.life.planner(publish), limit=45)
        self.assertTrue(observed)
        self.assertEqual(self.w.task(self.tid)['published_accounts_gen'], observed[-1][2])
        self.life.say(_general_stall.composition.APPROVER_A, 'Approve plan r2 for development')
        self.w.step(lambda: bool(self.w.q('SELECT id FROM approvals WHERE task=? AND rev=2', self.tid)), limit=40)
        self.scans(5)
        [old] = self.w.q('SELECT * FROM recoveries WHERE id=?', incident['id'])
        self.assertEqual(old['state'], 'superseded')
        self.assertEqual(self.wait(), wait)
        self.assertEqual(self.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_delivery:' + self.tid), [])
        for field in ('id', 'card', 'run_id', 'approval_id', 'approval_rev', 'plan_sha256',
                      'reason_sha256', 'detected_ms', 'abs_ms', 'attempts'):
            self.assertEqual(old[field], incident[field], field)
        later_key = 'clickup-dp:' + self.tid + ':later-independent-source-delivery'
        store = Store(self.w.state_path, self.w.clock)
        try:
            engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config()).reply(
                self.tid, later_key, 'other-unobserved-root', 'Later independent notification.')
        finally:
            store.close()
        self.scans(22)
        [failure] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', later_key)
        self.assertEqual((failure['state'], failure['outcome']), ('cancelled_stale', 'FOREIGN_ROOT'))
        carriers = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND body LIKE ?", '%' + later_key + '%')
        if not carriers:
            current = self.w.q("SELECT * FROM recoveries WHERE state='open' ORDER BY id DESC")[0]
            due = current['detected_ms'] + engine.RECOVERY_RETRY_MIN[current['attempts']] * 60000
            self.w.clock.now_ms = max(self.w.clock.now_ms, due + 1000)
            self.w.step(lambda: bool(self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND body LIKE ?", '%' + later_key + '%')), limit=20)
            carriers = self.w.q("SELECT * FROM outbox WHERE kind='kb_create' AND body LIKE ?", '%' + later_key + '%')
        self.assertEqual(len(carriers), 1)
        [wake] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', carriers[0]['idem_key'].removesuffix(':create') + ':wake')
        self.assertEqual(wake['state'], 'done')
        after = self.w.q('SELECT * FROM recoveries ORDER BY id')
        self.assertEqual(len(after), 2)
        self.assertEqual(self.w.main('recovery-check', '--card', after[-1]['card'], '--run', str(after[-1]['run_id']))[0], 0)
        self.w.clock.now_ms = incident['abs_ms'] + 60000
        self.scans(5)
        [old] = self.w.q('SELECT * FROM recoveries WHERE id=?', incident['id'])
        self.assertEqual((old['state'], old['attempts'], old['abs_ms']),
                         ('superseded', incident['attempts'], incident['abs_ms']))


if __name__ == '__main__':
    unittest.main()
