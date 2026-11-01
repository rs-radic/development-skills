"""Sanitized superseded/NULL bookkeeping through World -> main -> Glue -> native effects.

The JSON preserves the native/store field relationships, actor categories, run
kinds, relative timing and NULL bookkeeping, with synthetic identities, prose
and artifact fingerprints. It is not exact private captured bytes or a full
ClickUp API replay. The gzip contains a minimal synthetic approved plan with
the source-equivalent incorporation marker and unchanged authority boundary;
the private plan and project history are not included. No live transport is available.
"""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

import test_s4_composition as comp
from test_question_relay import sentinel_show
from clickup_intake import engine, main, rules
from clickup_intake.contracts import Outcome
from clickup_intake.store import Store

FIXTURES = Path(__file__).parent / 'fixtures'
TID, WORK, QUESTION, SOURCE = 'fixturetaska', 't_e0000001', 't_e000000c', 't_e0000016'
RELAY = 'fixturetaska-s2-g9-w1320'
NULLS = ('closure_gen', 'pending_relay', 'resolution_relay', 'resolution_card',
         'resolution_gen', 'resolution_body', 'resolution_authority', 'delivered_gen', 'release_key')


class CapturedBoard(comp.FakeBoard):
    def show(self, card):
        result = super().show(card)
        if result.outcome is Outcome.OK:
            result.json['children'] = list(self.cards[card].get('children', result.json['children']))
            done = [e for e in self.cards[card]['events'] if e['kind'] == 'completed']
            result.json['latest_summary'] = (done[-1]['payload']['summary'] if done else
                                              self.cards[card].get('latest_summary'))
        return result

    def _regate(self):
        # This is a scoped native projection: absent unrelated ancestors are unknown,
        # not made-up DONE cards. The simulated worker starts separately below.
        for c in self.cards.values():
            if c['task']['status'] == 'todo' and all(
                    p in self.cards and self.cards[p]['task']['status'] in ('done', 'archived')
                    for p in c['parents']):
                c['task']['status'] = 'ready'


class SupersededApprovedIncorporationTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline projection forbids real effects'))
            p.start()
            self.addCleanup(p.stop)
        self.capture = json.loads((FIXTURES / 'superseded_approved_incorporation.json').read_bytes())
        self.w = comp.World(self)
        self.w.clock.now_ms = self.capture['tasks'][0]['last_light_ms'] + 301000
        self.w.write_config(mode='shadow', cutover_ms=1790000000000)
        self.w.board = self.b = CapturedBoard(str(Path(self.w.dir) / 'native'), self.w.clock)
        self.w.cu.add_task(task_id=TID, status='reviewing feature', description='Offline provider substrate only.',
                           created_ms=1790000000000)
        self.assertEqual(self.w.run()[0], 0)
        self.w.write_config(mode='live')
        self.w.cu.set_status(TID, 'implementing')
        store = Store(self.w.state_path, self.w.clock)
        try:
            c = store.conn
            substrate = dict(c.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (TID,)).fetchone())
            task = dict(self.capture['tasks'][0])
            for field in ('content_hash', 'attach_ids_hash', 'list_fp', 'full_cursor'):
                task[field] = substrate[field]
            c.execute('DELETE FROM outbox')
            c.execute('UPDATE tasks SET %s WHERE clickup_task_id=?' % ','.join(k+'=?' for k in task if k != 'clickup_task_id'),
                      [v for k,v in task.items() if k != 'clickup_task_id'] + [TID])
            for table in ('approvals', 'blockers', 'relays'):
                for row in self.capture[table]:
                    c.execute('INSERT INTO %s(%s) VALUES (%s)' % (table, ','.join(row), ','.join('?' for _ in row)),
                              list(row.values()))
            c.execute("INSERT INTO cards(card_id,task,role,seq) VALUES (?,?,'request',2)", (SOURCE,TID))
            for key,value in ((engine.CUTOVER_KEY,1790000000000),(engine.AMEND_KEY,{'ms':1,'drain_sha256':'d'*64})):
                c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key,json.dumps(value)))
        finally:
            store.close()
        for cid,shown in self.capture['shows'].items():
            self.b.cards[cid] = dict(copy.deepcopy(shown), atts=[], subs=[])
        data = gzip.decompress((FIXTURES / 'superseded-approved-plan-r6.md.gz').read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.capture['approvals'][1]['plan_sha256'])
        self.plan = Path(self.w.dir) / 'clickup-plan-r6.md'
        self.plan.write_bytes(data)
        self.b.cards[SOURCE]['atts'] = [{'filename':'clickup-plan-r6.md','stored_path':str(self.plan)}]
        self.before = self.blocker()
        self.assertEqual(self.before, self.capture['blockers'][0])
        self.assertEqual(self.before['state'], 'superseded')
        self.assertTrue(all(self.before[f] is None for f in NULLS))
        self.assertEqual(self.b.cards[QUESTION]['task']['created_by'], 'implementor')
        self.assertEqual(self.b.cards[WORK]['parents'], self.capture['shows'][WORK]['parents'])
        self.assertEqual(self.b.cards[WORK]['runs'], self.capture['shows'][WORK]['runs'])
        self.assertEqual(self.w.q('SELECT * FROM amendment_answers'), [])
        self.trace = []

    def blocker(self):
        return self.w.q('SELECT * FROM blockers WHERE id=5')[0]

    def change(self, sql, *args):
        store = Store(self.w.state_path, self.w.clock)
        try:
            store.conn.execute(sql, args)
        finally:
            store.close()

    def cycle(self, n=1):
        for _ in range(n):
            self.w.clock.advance(301)
            real = engine.evaluate
            def record(store, now, obs=None, config=None):
                result = real(store, now, obs, config)
                self.trace.append({'cards': sorted(obs.cards), 'attachments': sorted(obs.attachments),
                                   'source_proof': rules.pass_evidence(obs.cards.get(SOURCE), obs.attachments.get(SOURCE), TID),
                                   'bindings': [dict(r) for r in store.conn.execute('SELECT * FROM amendment_answers')],
                                   'answers': [dict(r) for r in store.conn.execute("SELECT * FROM outbox WHERE kind='kb_complete'")]})
                return result
            with mock.patch.object(engine, 'evaluate', side_effect=record):
                self.assertEqual(self.w.run()[0], 0, self.w.outputs[-5:])

    def answer_rows(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND target=?", QUESTION)

    def test_superseded_null_capture_delivers_bound_answer_then_normal_fence_ack_once(self):
        self.cycle(4)
        task = self.w.task(TID)
        self.assertEqual((task['input_gen'], task['classified_gen'], task['scope_gen'], task['published_rev']), (9,9,9,6))
        self.assertEqual(self.w.q('SELECT state FROM approvals WHERE id=33'), [{'state':'started'}])
        self.assertTrue(any(x['source_proof'] == {'rev':6, 'sha256':task['published_sha256'],
                        'attachment':'clickup-plan-r6.md', 'accounts_gen':9} for x in self.trace), self.trace)
        rows = self.w.q('SELECT * FROM amendment_answers')
        self.assertEqual(len(rows), 1, 'SUPERSEDED_NULL: current approved marker must recover missing bookkeeping')
        [bound] = rows
        self.assertEqual((bound['state'],bound['approval_id'],bound['rev'],bound['gen'],bound['relay_ref'],bound['source']),
                         ('applied',33,6,9,RELAY,'plan'))
        [answer] = self.answer_rows()
        self.assertEqual(answer['state'], 'done')
        self.assertEqual(answer['idem_key'], 'clickup-dp:fixturetaska:blocker:5:answer:g9')
        pre = json.loads(answer['preconditions'])
        self.assertEqual((pre['approval_id'],pre['input_gen'],pre['scope_gen'],pre['question_card']), (33,9,9,QUESTION))
        summary = json.loads(answer['body'])['summary']
        self.assertTrue(summary.startswith('CLICKUP-ANSWER v1 question=%s card=%s gen=9 task=%s\n' % (QUESTION,WORK,TID)))
        self.assertIn('approved plan r6',summary)
        self.assertIn('boundary: security — unchanged.',summary)
        self.assertEqual(self.b.show(QUESTION).json['latest_summary'],summary)
        self.assertEqual(self.b.cards[QUESTION]['task']['status'],'done')
        self.assertEqual(self.blocker()['delivered_gen'],9)
        for field in ('id','task','question_card','card','run_id','created_ms','body_sha256','approval_id',
                      'phase_at_detect','stage','boundary','relay_ref','request_card','question_key','closure_item','origin_lane'):
            self.assertEqual(self.blocker()[field],self.before[field],field)
        self.assertEqual(self.b.cards[WORK]['parents'],self.capture['shows'][WORK]['parents'])
        self.assertEqual(self.b.cards['t_e0000006']['task']['status'],'done')
        self.assertTrue(any(x['answers'] and x['answers'][0]['state']=='pending' for x in self.trace),
                        'must witness queued answer before native effect, not only final state')
        # Each World.run reopens the store: unchanged rescans are process-restart equivalents.
        self.cycle(3)
        self.assertEqual(len(self.answer_rows()),1)
        self.assertEqual(self.b.mutations().count(('complete',QUESTION)),1)
        self.b._run = 1561
        run = self.b.claim(WORK,'implementor')
        self.b._event(WORK,'claimed',run_id=run)
        fence = 't_0bee00f1'
        self.b.cards[fence] = dict(sentinel_show(card=fence,title='CLICKUP-FENCE v1 '+WORK,
                                               w=WORK,created_s=self.b._now()),atts=[],subs=[])
        self.b.cards[WORK]['parents'].append(fence)
        self.cycle(3)
        [f] = self.w.q('SELECT * FROM fences')
        self.assertEqual(f['state'],'released')
        self.assertEqual(json.loads(f['snapshot']),{QUESTION:9})
        self.assertEqual(self.b.cards[fence]['task']['status'],'done')
        self.b.worker_comment(WORK,'implementor',
            'CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=9 run=%d fence=%s\nContinuing approved r6.' %
            (QUESTION,WORK,run,fence))
        self.cycle(2)
        self.assertEqual(self.blocker()['state'],'resumed')
        self.assertEqual(self.b.mutations().count(('complete',QUESTION)),1)
        print('OFFLINE_INCORPORATION_RESULT ' + json.dumps({
            'before': self.before, 'binding': bound, 'answer': answer,
            'after': self.blocker(), 'fence': f, 'mutations': self.b.mutations(),
            'limit': 'real main/Glue/engine/effect path; sanitized native field projection, synthetic approved plan and simulated worker/provider'},
            sort_keys=True), flush=True)

    def assert_no_answer(self):
        self.cycle(3)
        self.assertEqual(self.b.mutations().count(('complete',QUESTION)), 0)
        self.assertNotEqual(self.b.cards[QUESTION]['task']['status'], 'done')
        self.assertEqual(self.answer_rows(), [])

    def synthetic_plan(self, old, new):
        """Explicit exact-byte negative; never changes the synthetic approved fixture."""
        data = self.plan.read_bytes().replace(old.encode(),new.encode())
        self.assertNotEqual(data,self.plan.read_bytes())
        self.plan.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        for run in self.b.cards[SOURCE]['runs']:
            md = run.get('metadata') or {}
            if md.get('plan_revision') == 6 and 'plan_sha256' in md:
                md['plan_sha256'] = digest
        self.change('UPDATE approvals SET plan_sha256=? WHERE id=33',digest)
        self.change('UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?',digest,TID)

    def test_no_marker_or_wrong_identity_never_creates_resolution(self):
        marker = 'CLICKUP-INCORPORATES v1 blocker=5 question=%s card=%s gen=9' % (QUESTION,WORK)
        for replacement in ('', marker.replace('blocker=5','blocker=500'),
                            marker.replace(QUESTION,'t_deadbeef'), marker.replace(WORK,'t_deadbeef'),
                            marker.replace('gen=9','gen=8')):
            with self.subTest(marker=replacement):
                if replacement != '':
                    self.doCleanups()
                    self.setUp()
                self.synthetic_plan(marker,replacement)
                self.assert_no_answer()
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])
                self.assertIsNone(self.blocker()['resolution_body'])

    def test_current_genuine_question_and_partial_bookkeeping_are_never_reinterpreted(self):
        changes = [('state','asked'),('closure_item','an-existing-closure'),('closure_gen',9),('pending_relay',RELAY),
                   ('release_gen',9),('delivered_sha256','1'*64),
                   ('resolution_relay',RELAY),('resolution_card',SOURCE),('resolution_gen',9),
                   ('resolution_body','Existing human answer.'),('resolution_authority','unchanged'),
                   ('delivered_gen',8),('release_key','existing-release')]
        for i,(field,value) in enumerate(changes):
            with self.subTest(field=field):
                if i:
                    self.doCleanups()
                    self.setUp()
                self.change('UPDATE blockers SET %s=? WHERE id=5' % field,value)
                if field == 'state':
                    self.change('UPDATE blockers SET approval_id=33 WHERE id=5')
                self.assert_no_answer()
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])
                self.assertEqual(self.blocker()[field],value)

    def test_stale_wrong_rev_hash_gen_or_held_authority_never_seeds(self):
        changes = [
            ("UPDATE approvals SET state='stale' WHERE id=33",()),
            ("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=33",()),
            ("UPDATE approvals SET state='held' WHERE id=33",()),
            ("UPDATE approvals SET rev=7 WHERE id=33",()),
            ("UPDATE approvals SET plan_sha256=? WHERE id=33",('0'*64,)),
            ("UPDATE approvals SET accounts_gen=10 WHERE id=33",()),
            ("UPDATE tasks SET input_gen=10,classified_gen=10,scope_gen=10 WHERE clickup_task_id=?",(TID,)),
            ("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?",(TID,)),
            ("UPDATE approvals SET revoked_reason='security' WHERE id=16",()),
            ("DELETE FROM meta WHERE key='amendment_cutover_ms'",()),
            ("DELETE FROM meta WHERE key='blocker_cutover_ms'",())]
        for i,(sql,args) in enumerate(changes):
            with self.subTest(sql=sql):
                if i:
                    self.doCleanups()
                    self.setUp()
                self.change(sql,*args)
                self.assert_no_answer()
                self.assertIsNone(self.blocker()['resolution_body'])
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])

    def test_changed_stale_or_ambiguous_material_relay_never_seeds(self):
        changes = [('task','other'),('card','t_deadbeef'),('gen',8),('state','delivered'),
                   ('classification','answer_only')]
        for i,(field,value) in enumerate(changes):
            with self.subTest(field=field):
                if i:
                    self.doCleanups()
                    self.setUp()
                self.change('UPDATE relays SET %s=? WHERE relay_ref=?' % field,value,RELAY)
                self.assert_no_answer()
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])
        for gen in (9,10):
            with self.subTest(extra_gen=gen):
                self.doCleanups()
                self.setUp()
                self.change("INSERT INTO relays(relay_ref,task,card,gen,state,classification) VALUES (?,?,?,?,'classified','material')",
                            RELAY+'-new',TID,SOURCE,gen)
                self.assert_no_answer()
                self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])

    def test_unverified_plan_bytes_or_unavailable_source_cannot_seed(self):
        self.plan.write_bytes(self.plan.read_bytes()+b'\nUnreviewed.\n')
        self.assert_no_answer()
        self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])
        for operation in ('show','attachments'):
            with self.subTest(operation=operation):
                self.doCleanups()
                self.setUp()
                real = getattr(self.b,operation)
                from clickup_intake.contracts import KanbanResult
                with mock.patch.object(self.b,operation,side_effect=lambda card:
                    KanbanResult(Outcome.DEFINITE_FAIL) if card==SOURCE else real(card)):
                    self.assert_no_answer()
                    self.assertEqual(self.w.q('SELECT * FROM amendment_answers'),[])
                self.cycle(3)
                self.assertEqual(self.blocker()['delivered_gen'],9)

    def test_unavailable_question_or_work_leaves_bound_then_delivers_once(self):
        from clickup_intake.contracts import KanbanResult
        for i,target in enumerate((QUESTION,WORK)):
            with self.subTest(target=target):
                if i:
                    self.doCleanups()
                    self.setUp()
                real = self.b.show
                with mock.patch.object(self.b,'show',side_effect=lambda card:
                    KanbanResult(Outcome.DEFINITE_FAIL) if card==target else real(card)):
                    self.assert_no_answer()
                    self.assertEqual(self.w.q('SELECT state FROM amendment_answers'),[{'state':'bound'}])
                self.cycle(3)
                self.assertEqual(self.blocker()['delivered_gen'],9)
                self.assertEqual(self.b.mutations().count(('complete',QUESTION)),1)

    def test_native_owner_gate_still_refuses_busy_detached_or_shared_question(self):
        for i,case in enumerate(('busy','detached','shared','wrong_assignee')):
            with self.subTest(case=case):
                if i:
                    self.doCleanups()
                    self.setUp()
                if case == 'busy':
                    self.b._run = 1561
                    self.b.claim(WORK,'implementor')
                elif case == 'detached':
                    self.b.cards[WORK]['parents'].remove(QUESTION)
                elif case == 'shared':
                    self.b.cards[QUESTION]['children'].append('t_deadbeef')
                else:
                    self.b.cards[QUESTION]['task']['assignee']='other'
                self.cycle(3)
                self.assertEqual(self.b.mutations().count(('complete',QUESTION)),0)
                self.assertNotEqual(self.b.cards[QUESTION]['task']['status'],'done')

    def test_queued_answer_keeps_current_generation_gate_at_send(self):
        with mock.patch.object(main.Glue,'execute',return_value=None):
            self.cycle(3)
        [answer] = self.answer_rows()
        self.assertEqual(answer['state'],'pending')
        self.change('UPDATE tasks SET input_gen=10,classified_gen=10,scope_gen=10 WHERE clickup_task_id=?',TID)
        self.cycle(2)
        self.assertEqual(self.b.mutations().count(('complete',QUESTION)),0)
        self.assertNotEqual(self.b.cards[QUESTION]['task']['status'],'done')
        self.assertEqual(self.answer_rows()[0]['state'],'cancelled_stale')

    def test_post_complete_restart_reconciles_without_duplicate_answer(self):
        real = self.b.complete
        crashed = []
        def complete(card,summary,metadata=None):
            result = real(card,summary,metadata)
            if card==QUESTION and not crashed:
                crashed.append(True)
                raise comp.Crash('native answer applied before journal commit')
            return result
        with mock.patch.object(self.b,'complete',side_effect=complete):
            self.cycle(4)
        self.assertEqual(crashed,[True])
        self.cycle(3)
        self.assertEqual(self.blocker()['delivered_gen'],9)
        self.assertEqual(self.b.mutations().count(('complete',QUESTION)),1)


if __name__ == '__main__':
    unittest.main()
