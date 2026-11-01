"""Current dependency visibility through the real Glue caller, with offline ports only.

The first regression retains the two-hop graph, actor categories, lifecycle events,
parent links and approval binding with synthetic identities and question prose.
Unrelated absent approved roots are explicit terminal fixture nodes; this is a
sanitized structural projection, not exact private captured bytes.
"""
import copy
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from clickup_intake import main, kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome
import test_s4_composition as comp
import test_recovery as recovery

FIXTURE = Path(__file__).parent / 'fixtures/dynamic_dependencies.json'


class DynamicDependenciesTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids external I/O'))
            p.start()
            self.addCleanup(p.stop)
        self.f = json.loads(FIXTURE.read_bytes())
        self.tid = self.f['task_id']
        self.quality, self.repair, self.question = [self.f[k] for k in ('quality_card', 'repair_card', 'question_card')]
        self.w = comp.World(self, mode='shadow', cutover_ms=1790000000000)
        self.w.clock.now_ms = 1793371860000
        self.w.board = recovery.RecoveryBoard(os.path.join(self.w.dir, 'board'), self.w.clock, self.w.inj)
        self.b = self.w.board
        self.w.cu.add_task(task_id=self.tid, status='reviewing feature', description='Offline provider substrate.',
                           created_ms=1790000000001)
        self.assertEqual(self.w.run()[0], 0)
        self.w.cu.set_status(self.tid, 'implementing')
        self.w.write_config(mode='live')
        c = recovery.store_conn(self.w)
        task = dict(self.f['task'])
        substrate = dict(c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
        for field in ('content_hash', 'attach_ids_hash', 'list_fp', 'full_cursor', 'full_started_ms'):
            task[field] = substrate[field]
        for field in ('last_light_ms', 'last_full_ms', 'last_task_get_ms', 'barrier_ok_ms'):
            task[field] = self.w.clock() - 3700000
        task['quiet_until_ms'] = self.w.clock() - 1000
        c.execute('UPDATE tasks SET %s WHERE clickup_task_id=?' % ','.join(k+'=?' for k in task if k!='clickup_task_id'),
                  [v for k,v in task.items() if k!='clickup_task_id'] + [self.tid])
        c.execute('DELETE FROM outbox')
        a = self.f['approval']
        c.execute('INSERT INTO approvals(%s) VALUES (%s)' % (','.join(a), ','.join('?' for _ in a)), list(a.values()))
        for key in ('blocker_cutover_ms', 'recovery_cutover_ms'):
            c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, '1790000000000'))
        c.close()
        for cid, shown in self.f['cards'].items():
            self.b.cards[cid] = dict(copy.deepcopy(shown), comments=[], atts=[], subs=[])
        gates = json.loads(a['clickup_gates'])
        for cid in set(gates['lineage'] + [a['gate_card']]):
            if cid not in self.b.cards:
                self.b.cards[cid] = dict(task=dict(id=cid, title='Unrelated terminal fixture root', status='done',
                    assignee='orchestrator', created_by='orchestrator', created_at=1793362327),
                    parents=[], runs=[], events=[], comments=[], atts=[], subs=[])
        self.b.cards[gates['final_e2e']]['task']['status'] = 'todo'
        self.b.cards[gates['final_e2e']]['parents'] = [self.quality]
        self.b.cards[gates['promotion']]['task']['status'] = 'blocked'
        self.b.cards[gates['promotion']]['parents'] = [a['gate_card']]
        self.b.cards[gates['cleanup']]['task']['status'] = 'todo'
        self.b.cards[gates['cleanup']]['parents'] = [gates['promotion']]
        self.b._run = 1600
        request = self.b._new('Offline parked planning source', assignee='planner', status='scheduled')
        self.b.cards[request]['runs'] = [dict(id=1490, profile='planner', status='done', outcome='scheduled',
            started_at=1793360000, ended_at=1793360100)]
        c = recovery.store_conn(self.w)
        c.execute("INSERT INTO cards(card_id,task,role,seq,created_key,wait_run_id) VALUES (?,?,'request',1,?,1490)",
                  (request, self.tid, 'clickup-dp:%s:req:1' % self.tid))
        c.execute("INSERT INTO relays(relay_ref,task,card,gen,item_ids,state,classification) VALUES (?,?,?,2,'[]','classified','answer_only')",
                  ('offline-prior-relay', self.tid, request))
        c.close()
        # The retained projection omits these two fields. Their exact values follow
        # from the parent's verified protocol_parent(...)=('question','record')
        # fact and the unchanged sentinel_defect checks; no guard is bypassed.
        for event in self.b.cards[self.question]['events']:
            if event['kind'] == 'created':
                event['payload']['creator_task_id'] = self.repair
            if event['kind'] == 'blocked':
                event['payload']['reason'] = 'initial_status'
        self.snapshot_calls = []
        self.install_snapshot()

    def install_snapshot(self):
        # Real read-only Snapshot acquisition over a disposable native-schema fixture.
        def snapshot(roots, take, clock):
            from clickup_intake.preworker_snapshot import acquire
            self.snapshot_calls.append(tuple(sorted(roots)))
            path = Path(self.w.dir) / 'native.db'
            c = sqlite3.connect(path)
            with c:
                for table in ('tasks','task_links','task_runs','task_comments','task_events','task_attachments'):
                    c.execute('DROP TABLE IF EXISTS ' + table)
                c.executescript('''
                CREATE TABLE tasks(id TEXT PRIMARY KEY, body TEXT, title TEXT, status TEXT, assignee TEXT,
                    created_by TEXT, created_at INTEGER, current_run_id INTEGER, worker_pid INTEGER,
                    last_heartbeat_at INTEGER);
                CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
                CREATE TABLE task_runs(id INTEGER, task_id TEXT, profile TEXT, status TEXT, outcome TEXT,
                    metadata TEXT, summary TEXT, worker_pid INTEGER, started_at INTEGER, ended_at INTEGER);
                CREATE TABLE task_comments(id INTEGER, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
                CREATE TABLE task_events(id INTEGER, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
                CREATE TABLE task_attachments(id INTEGER, task_id TEXT, filename TEXT, stored_path TEXT,
                    size INTEGER, uploaded_by TEXT, created_at INTEGER);
                ''')
                for cid, s in self.b.cards.items():
                    c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)', tuple(s['task'].get(k) for k in
                        ('id','body','title','status','assignee','created_by','created_at','current_run_id',
                         'worker_pid','last_heartbeat_at')))
                    c.executemany('INSERT INTO task_links VALUES (?,?)', [(p,cid) for p in s.get('parents',[])])
                    for r in s.get('runs',[]):
                        c.execute('INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (r.get('id'),cid,r.get('profile'),r.get('status'),r.get('outcome'),json.dumps(r.get('metadata')),
                             r.get('summary'),r.get('worker_pid'),r.get('started_at'),r.get('ended_at')))
                    for i,r in enumerate(s.get('comments',[])):
                        c.execute('INSERT INTO task_comments VALUES (?,?,?,?,?)',
                                  (i,cid,r.get('author'),r.get('body'),r.get('created_at')))
                    for i,r in enumerate(s.get('events',[])):
                        c.execute('INSERT INTO task_events VALUES (?,?,?,?,?,?)',
                                  (i,cid,r.get('run_id'),r.get('kind'),json.dumps(r.get('payload')),r.get('created_at')))
                    for i,r in enumerate(s.get('atts', [])):
                        c.execute('INSERT INTO task_attachments VALUES (?,?,?,?,?,?,?)',
                                  (i,cid,r.get('filename'),r.get('stored_path'),r.get('size'),r.get('uploaded_by'),r.get('created_at')))
            c.close()
            proof = acquire(path, roots, take, clock)
            if proof:
                original = copy.deepcopy(self.b.cards)
                proof.native_stable = lambda: self.b.cards == original
            return proof
        self.b._preworker_snapshot = snapshot

    def scans(self, n=1):
        for _ in range(n):
            self.w.clock.advance(301)
            self.assertEqual(self.w.run()[0], 0)

    def blockers(self):
        return self.w.q('SELECT * FROM blockers WHERE question_card=?', self.question)

    def wakes(self):
        return [x for x in self.b.cards if self.b.cards[x]['task'].get('key') and ':rcv:' in self.b.cards[x]['task']['key']]

    def deliver(self):
        self.scans(4)
        rows = self.blockers()
        self.assertTrue(rows, 'actual two-hop shape: deliveries=%d wakes=%d blockers=%d' %
                        (len(self.w.bot_comments(self.tid)), len(self.wakes()), len(rows)))
        b = rows[0]
        self.assertIsNotNone(b['request_card'])
        self.b.claim(b['request_card'], 'planner')
        self.b.park(b['request_card'], comp.out('technical_questions', b['relay_ref'],
            self.b.cards[self.question]['task']['body'], blocker=b['id'], boundary='security'))
        self.scans(4)
        b = self.blockers()[0]
        [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', b['question_key'])
        self.assertEqual(row['state'], 'done')
        self.assertTrue(row['result_ref'])
        visible = [x for x in self.w.bot_comments(self.tid) if x['id'] == row['result_ref']]
        self.assertEqual(len(visible), 1)
        self.assertIn(self.b.cards[self.question]['task']['body'], visible[0]['comment_text'])
        self.assertEqual((b['card'], b['run_id'], b['approval_id'], b['state']), (self.repair, 1540, 32, 'asked'))
        return b, row

    def test_two_hop_native_question_delivers_with_readback(self):
        self.assertEqual(kp.protocol_parent(self.repair, self.b.show(self.repair).json,
                         self.b.show(self.question).json, 1790000000000), ('question', 'record'))
        self.assertEqual(self.blockers(), [])
        original = self.w.q('SELECT clickup_gates FROM approvals')
        b, row = self.deliver()
        self.scans(12)
        self.assertEqual(self.w.q('SELECT clickup_gates FROM approvals'), original)
        self.assertEqual(self.blockers()[0]['question_key'], row['idem_key'])
        self.assertEqual(self.wakes(), [])

    def test_late_chain_after_closed_cache_delivers(self):
        original = self.b.cards[self.quality]['parents'][:]
        self.b.cards[self.quality]['parents'] = [original[0]]
        self.scans(2)
        c = recovery.store_conn(self.w)
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('blocker_closed_cards',?)", (json.dumps([self.quality]),))
        c.close()
        self.b.cards[self.quality]['parents'] = original
        self.deliver()

    def test_third_required_level_is_visible(self):
        bridge = self.b._new('Additional required repair', assignee='implementor', status='todo', parents=[self.repair])
        self.b.cards[self.quality]['parents'] = [bridge]
        self.deliver()

    def test_foreign_consumer_prevents_adopting_repair(self):
        self.b._new('Foreign task consumes repair', status='todo', parents=[self.repair])
        self.scans(5)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_foreign_consumer_of_intermediate_chain_prevents_deeper_adoption(self):
        bridge = self.b._new('Shared intermediate repair', status='todo', parents=[self.repair])
        self.b.cards[self.quality]['parents'] = [bridge]
        self.b._new('Foreign consumer', status='todo', parents=[bridge])
        self.scans(4)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_missing_required_node_fails_closed(self):
        del self.b.cards[self.repair]
        self.scans(5)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_cycle_fails_closed(self):
        self.b.cards[self.repair]['parents'].append(self.quality)
        self.scans(5)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_detached_chain_cannot_send_queued_question(self):
        original = main.Glue.clickup
        def hold_question(glue, row):
            if ':bq:' in row['idem_key']:
                return False
            return original(glue, row)
        with mock.patch.object(main.Glue, 'clickup', hold_question):
            self.scans(4)
            b = self.blockers()[0]
            self.b.claim(b['request_card'], 'planner')
            self.b.park(b['request_card'], comp.out('technical_questions', b['relay_ref'],
                self.b.cards[self.question]['task']['body'], blocker=b['id'], boundary='security'))
            self.scans(4)
        self.assertTrue(self.blockers()[0]['question_key'])
        self.b.cards[self.quality]['parents'].remove(self.repair)
        self.scans(4)
        key = self.blockers()[0]['question_key']
        row = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)[0]
        self.assertNotEqual(row['state'], 'done')
        self.assertFalse(any(self.b.cards[self.question]['task']['body'] in x['comment_text']
                             for x in self.w.bot_comments(self.tid)))

    def test_native_snapshot_refuses_oversize_closure_within_shared_budget(self):
        parent = self.repair
        for _ in range(513):
            parent = self.b._new('Bounded required ancestor', status='done', parents=[parent])
        self.b.cards[self.quality]['parents'] = [parent]
        reader = main._PreworkerReads(self.b, self.w.clock)
        proof = self.b._preworker_snapshot({self.quality}, reader.take, self.w.clock)
        if proof is not None:
            proof.close()
        self.assertIsNone(proof)
        self.assertGreaterEqual(reader.remaining, 50)
        self.scans(2)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_shared_approval_root_is_not_assigned_arbitrarily(self):
        c = recovery.store_conn(self.w)
        c.execute("INSERT INTO tasks(clickup_task_id,phase) VALUES ('foreign-task','development')")
        a = dict(self.f['approval'], id=33, task='foreign-task', comment_id='other-approval')
        c.execute('INSERT INTO approvals(%s) VALUES (%s)' % (','.join(a), ','.join('?' for _ in a)), list(a.values()))
        c.close()
        self.scans(4)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_revoked_approval_cannot_authorize_new_required_repair(self):
        c = recovery.store_conn(self.w)
        c.execute("UPDATE approvals SET state='revoked',revoked_reason='new_input'")
        c.close()
        self.scans(4)
        self.assertEqual(self.blockers(), [])
        self.assertEqual(self.wakes(), [])

    def test_two_current_approvals_cannot_claim_the_same_dynamic_chain(self):
        c = recovery.store_conn(self.w)
        a = dict(self.f['approval'], id=33, rev=4, comment_id='other-revision')
        gates = json.loads(a['clickup_gates'])
        gates['plan_revision'] = 4
        a['clickup_gates'] = json.dumps(gates)
        c.execute('INSERT INTO approvals(%s) VALUES (%s)' % (','.join(a), ','.join('?' for _ in a)), list(a.values()))
        from clickup_intake import engine
        roots, forbidden = engine.dependency_scope(c, self.tid)
        shows = {cid: self.b.show(cid).json for cid in self.b.cards}
        members = engine.dependency_members(roots, shows, forbidden)
        c.close()
        self.assertNotIn(self.repair, members)

    def test_typed_stall_of_new_required_repair_wakes_ai(self):
        self.b.cards[self.repair]['parents'].remove(self.question)
        self.b.cards[self.repair]['task']['status'] = 'blocked'
        self.b.cards[self.repair]['events'][-2].update(kind='blocked', payload=dict(
            reason='Required capability is unavailable.', kind='capability', source_status='ready'))
        self.scans(4)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['card'], rows[0]['run_id'], rows[0]['approval_id']), (self.repair, 1540, 32))
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.b.cards[self.wakes()[0]]['task']['status'], 'done')
        self.assertEqual(self.w.main('recovery-check', '--card', self.repair, '--run', '1540')[0], 0)


class DynamicDeliveryRecoveryTest(unittest.TestCase):
    install_snapshot = DynamicDependenciesTest.install_snapshot
    scans = DynamicDependenciesTest.scans
    wakes = DynamicDependenciesTest.wakes

    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids external I/O'))
            p.start()
            self.addCleanup(p.stop)
        self.w, self.life = recovery.developing_world(self)
        self.b, self.tid = self.w.board, self.life.tid
        self.snapshot_calls = []
        self.install_snapshot()
        c = recovery.store_conn(self.w)
        for key in ('blocker_cutover_ms', 'recovery_cutover_ms'):
            c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, str(self.w.clock())))
        c.close()
        self.w.clock.advance(2)
        self.repair = self.b.worker_create('Required later repair', assignee='implementor')
        self.b.cards[self.life.fe]['parents'] = [self.repair]
        run = self.b.claim(self.repair, 'implementor')
        self.b._end(self.repair, 'blocked')
        self.b.cards[self.repair]['task']['status'] = 'todo'
        self.b._ev(self.repair, 'dependency_wait', run, source_status='ready')
        from test_question_relay import sentinel_show
        self.question = 't_abcd1234'
        self.b.cards[self.question] = dict(sentinel_show(card=self.question,
            title='CLICKUP-QUESTION v1 '+self.repair, w=self.repair, created_s=self.w.clock()//1000),
            atts=[], subs=[])
        self.b.cards[self.question]['task']['body'] = 'Which supported route should this repair use?'
        self.b.cards[self.repair]['parents'] = [self.question]

    def queue_question(self, row_change=None):
        original = main.Glue.clickup
        def hold(glue, row):
            if ':bq:' in row['idem_key'] and row['kind'] == 'cu_comment':
                if row_change:
                    row_change(glue, row)
                return False
            return original(glue, row)
        self.scans(3)
        b = self.w.q('SELECT * FROM blockers')[0]
        self.b.claim(b['request_card'], 'planner')
        self.b.park(b['request_card'], comp.out('technical_questions', b['relay_ref'],
            self.b.cards[self.question]['task']['body'], blocker=b['id'], boundary='security'))
        return mock.patch.object(main.Glue, 'clickup', hold)

    def test_pending_unpublished_question_wakes_instead_of_human_wait(self):
        with self.queue_question():
            self.scans(6)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['card'], self.repair)
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.b.cards[self.wakes()[0]]['task']['status'], 'done')

    def test_failed_question_delivery_gets_bounded_ai_without_clearing_hold(self):
        def fail(glue, row):
            glue.update(row['idem_key'], state='failed', outcome='retry_wait', attempts=5,
                        first_send_ms=self.w.clock(), result_ref=None)
            glue.hold(self.tid, 'outbox:' + row['idem_key'], 'offline definite failure')
        with self.queue_question(fail):
            self.scans(8)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.b.cards[self.wakes()[0]]['task']['status'], 'done')
        self.assertTrue(self.w.task(self.tid)['hold_reason'].startswith('outbox:'))
        self.assertEqual(self.w.q("SELECT state FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:bq:%'")[0]['state'], 'failed')

    def test_future_backoff_is_quiet(self):
        def future(glue, row):
            glue.update(row['idem_key'], state='retry_wait', next_attempt_ms=self.w.clock()+3600000)
        with self.queue_question(future):
            self.scans(6)
        self.assertEqual(self.wakes(), [])

    def test_healthy_planner_is_quiet(self):
        self.scans(3)
        b = self.w.q('SELECT * FROM blockers')[0]
        self.b.claim(b['request_card'], 'planner')
        self.scans(6)
        self.assertEqual(self.wakes(), [])

    def test_detached_repair_does_not_spend_another_wake(self):
        self.b.cards[self.question]['task']['assignee'] = 'wrong-route'
        self.scans(6)
        before = self.w.q('SELECT * FROM recoveries')[0]
        self.b.cards[self.life.fe]['parents'] = []
        self.w.clock.now_ms = before['detected_ms'] + 61 * 60000
        self.scans(3)
        after = self.w.q('SELECT * FROM recoveries')[0]
        self.assertEqual(after['attempts'], before['attempts'])
        self.assertEqual(after['abs_ms'], before['abs_ms'])
        self.assertEqual(len(self.wakes()), 1)

    def test_invalid_question_route_is_owned_by_bounded_ai_not_human_wait(self):
        self.b.cards[self.question]['task']['assignee'] = 'wrong-route'
        self.scans(6)
        rows = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['card'], self.repair)
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual(self.b.cards[self.wakes()[0]]['task']['status'], 'done')
        self.assertEqual(self.w.main('recovery-check','--card',self.repair,'--run',str(rows[0]['run_id']))[0], 0)
        self.assertEqual(self.w.q('SELECT * FROM blockers'), [])
