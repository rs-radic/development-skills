"""Native-shape regression: actual Error native-question discovery and CPU PARK continuation.

Sanitized DB projections retain types, nulls, actor categories and matching relations.
Public identities and policy prose replace operational history; provider/clock/worker ports are explicit fakes.
No status, run or authority normalization, preseeded new blocker, or live owner answer.
"""
import base64
import copy
import datetime
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from clickup_intake import engine, main, kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome
from test_recovery import RecoveryBoard, store_conn
import test_s4_composition as comp
from test_question_relay import sentinel_show

FIXTURE = Path(__file__).parent / 'fixtures/three_native_waits.json'


class NativeWaitCase(unittest.TestCase):
    def load_case(self, name):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids native subprocess/network'))
            p.start()
            self.addCleanup(p.stop)
        self.f = json.loads(FIXTURE.read_bytes())
        case = self.f['cases'][name]
        self.tid, self.source = case['task'], case['source']
        self.w = comp.World(self)
        self.w.clock.now_ms = int(datetime.datetime.fromisoformat('2026-10-24T18:24:07.202177+00:00').timestamp() * 1000)
        self.w.write_config(cutover_ms=1790000000000, mode='shadow')
        self.w.board = RecoveryBoard(str(Path(self.w.dir) / 'board'), self.w.clock, self.w.inj)
        self.b = self.w.board
        self.w.cu.add_task(task_id=self.tid, status='reviewing feature', description='Explicit offline provider substrate.',
                           created_ms=1790000000001)
        self.assertEqual(self.w.run()[0], 0)
        self.w.write_config(mode='live')
        state = self.f['intake'][self.tid]
        captured = state['tasks'][0]
        self.w.cu.set_status(self.tid, captured['last_status_seen'])
        conn = store_conn(self.w)
        substrate = dict(conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
        conn.execute('DELETE FROM outbox')
        task = dict(captured)
        for field in ('content_hash', 'attach_ids_hash', 'list_fp', 'full_cursor', 'full_started_ms'):
            task[field] = substrate[field]
        for field in ('last_light_ms', 'last_full_ms', 'last_task_get_ms', 'barrier_ok_ms'):
            task[field] = self.w.clock() - 3_700_000
        task['quiet_until_ms'] = self.w.clock() - 1000
        conn.execute('UPDATE tasks SET %s WHERE clickup_task_id=?' % ','.join(k+'=?' for k in task if k!='clickup_task_id'),
                     [v for k,v in task.items() if k!='clickup_task_id'] + [self.tid])
        for table in ('approvals', 'cards', 'relays', 'blockers', 'fences', 'amendment_answers'):
            for row in state[table]:
                conn.execute('INSERT INTO %s(%s) VALUES (%s)' % (table, ','.join(row), ','.join('?' for _ in row)), list(row.values()))
        for row in state.get('done_answer_outbox', []):
            conn.execute('INSERT INTO outbox(%s) VALUES (%s)' % (','.join(row), ','.join('?' for _ in row)), list(row.values()))
        for key,value in self.f['meta'].items():
            conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key,json.dumps(value)))
        conn.close()
        for cid, shown in self.f['native'].items():
            self.b.cards[cid] = dict(copy.deepcopy(shown), atts=[], subs=[])
        for row in state['cards']:
            if row['card_id'] in self.b.cards and row['created_key']:
                self.b.keys[row['created_key']] = row['card_id']
                self.b.cards[row['card_id']]['task']['key'] = row['created_key']
        for cid,plan in self.f['plans'].items():
            data = base64.b64decode(plan['bytes_b64'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), plan['sha256'])
            path = Path(self.w.dir) / plan['name']
            path.write_bytes(data)
            self.b.cards[cid]['atts'] = [{'filename':plan['name'],'stored_path':str(path),'size':len(data),'uploaded_by':'planner'}]
        self.b._run = max(r['id'] for c in self.b.cards.values() for r in c.get('runs', []))
        show = self.b.show
        def readback(cid):
            result = show(cid)
            if isinstance(result.json,dict):
                complete = [e for e in result.json.get('events',[]) if e.get('kind')=='completed']
                if complete:
                    result.json['latest_summary'] = complete[-1]['payload'].get('summary')
                elif result.json.get('runs') and result.json['task']['status']=='done':
                    result.json['latest_summary'] = result.json['runs'][-1].get('summary')
            return result
        self.b.show = readback
        self.life = object.__new__(comp.Lifecycle)
        self.life.w,self.life.b,self.life.cu,self.life.tid = self.w,self.b,self.w.cu,self.tid
        self.life.planner_turns,self.life.plan_sha = 0,None

    def cycles(self, n=1):
        for _ in range(n):
            self.w.clock.advance(301)
            self.assertEqual(self.w.run()[0],0)

    def blocker(self, question):
        rows = self.w.q('SELECT * FROM blockers WHERE question_card=?', question)
        return rows[0] if rows else None


class ErrorNativeQuestionTest(NativeWaitCase):
    def setUp(self):
        self.load_case('errors')
        self.question = 't_ab00009d'
        self.assertIsNone(self.blocker(self.question), 'the actual Error question has no intake blocker yet')
        self.assertEqual(self.b.cards[self.source]['task']['status'], 'todo')
        self.assertEqual(self.b.cards[self.source]['runs'][-1]['id'],1327)
        self.assertEqual(self.b.cards[self.source]['events'][-1]['kind'], 'dependency_wait')

    def ask_actual_question(self):
        self.w.step(lambda: bool(self.blocker(self.question)) and self.blocker(self.question)['state']=='relayed')
        b = self.blocker(self.question)
        self.b.claim(b['request_card'],'planner')
        # Sanitized captured-shape question, not a chosen live answer.
        self.b.park(b['request_card'], comp.out('technical_questions',b['relay_ref'],
                    self.b.cards[self.question]['task']['body'],blocker=b['id'],boundary='security'))
        self.w.step(lambda: bool(self.blocker(self.question)['question_key']) and
                    any(r['state']=='done' for r in self.w.q('SELECT state FROM outbox WHERE idem_key=?',
                                                           self.blocker(self.question)['question_key'])))
        self.cycles(2)
        b = self.blocker(self.question)
        [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?',b['question_key'])
        [visible] = [c for c in self.w.bot_comments(self.tid) if c['id']==row['result_ref']]
        self.assertIn(self.b.cards[self.question]['task']['body'],visible['comment_text'])
        self.assertEqual((b['state'],b['boundary']),( 'asked',1))
        self.assertEqual(self.b.cards[self.question]['task']['status'],'blocked')
        return b,row

    def test_actual_question_delivers_bound_human_wait_then_material_reply_uses_revision_not_waiver(self):
        b,row = self.ask_actual_question()
        text = json.loads(row['body'])['text']
        for exact in ('Example catalog','A) Accept narrowly.','B) Require a formal plan amendment','C) Stop'):
            self.assertIn(exact,text)
        self.assertEqual(self.b.cards[self.source]['task']['status'],'todo')
        # Explicitly synthetic human B reply. No live acceptance or exception is inferred.
        self.life.say(comp.APPROVER_A,'B) Require a formal plan amendment before testing.',mention=comp.BOT,reply_to=row['result_ref'])
        self.life.say(comp.APPROVER_A,'Technical requirements closed')
        self.w.step(lambda: self.blocker(self.question)['state']=='closed' and self.blocker(self.question)['pending_relay'])
        b=self.blocker(self.question)
        rel=self.w.q('SELECT * FROM relays WHERE relay_ref=?',b['pending_relay'])[0]
        self.w.step(lambda: self.b.cards[rel['card']]['task']['status']=='ready')
        self.b.claim(rel['card'],'planner')
        self.b.park(rel['card'],comp.classify(rel['relay_ref'],rel['gen'],'material'))
        self.w.step(lambda: self.blocker(self.question)['state']=='scope_change')
        self.assertEqual(self.b.cards[self.question]['task']['status'],'blocked')
        self.assertIsNone(self.blocker(self.question)['delivered_gen'])
        self.assertNotIn(self.w.task(self.tid)['phase'],('development','ready_for_deployment'))
        self.assertEqual(self.w.task(self.tid)['scope_gen'],rel['gen'])
        self.assertTrue(all(a['state']=='revoked' for a in self.w.q('SELECT state FROM approvals WHERE id=17')))
        # Simulated, independently reviewed r12 and explicit fresh development approval: never a live waiver.
        original = base64.b64decode(self.f['plans']['t_ab00009f']['bytes_b64']).decode('utf-8')
        revised = (original + '\n\n## Offline test-only amendment r12\nFormal baseline decision review required; '
                   'no implicit acceptance of option A.\nCLICKUP-INCORPORATES v1 blocker=%d question=%s card=%s gen=%d\n' %
                   (b['id'],self.question,self.source,rel['gen']))
        self.b.claim(rel['card'],'planner')
        sha = self.b.request_review(rel['card'],self.tid,12,revised,rel['gen'])
        self.b.claim(rel['card'],'plan-reviewer')
        self.b.finish(rel['card'],'plan-reviewer')
        self.w.step(lambda: self.w.task(self.tid)['published_rev']==12)
        self.life.say(comp.APPROVER_A,'Approve plan r12 for development')
        self.w.step(lambda: bool(self.w.q('SELECT * FROM approvals WHERE rev=12')) and
                    self.w.q('SELECT * FROM approvals WHERE rev=12')[0]['orchestrator_card'] is not None)
        a=self.w.q('SELECT * FROM approvals WHERE rev=12')[0]
        self.w.step(lambda: self.b.cards[a['orchestrator_card']]['task']['status']=='ready')
        self.b.claim(a['orchestrator_card'],'orchestrator')
        # Synthetic approved amendment owner output, through the same existing orchestration protocol.
        from clickup_intake import render
        promo=self.b.worker_create('Offline amended promotion',parents=[a['gate_card']],assignee='integrator',
                                   body=render.promotion_block(a['gate_card'],self.tid,12,sha))
        cleanup=self.b.worker_create('Offline amended cleanup',parents=[promo])
        self.b.finish(a['orchestrator_card'],'orchestrator',{'clickup_gates':{'plan_revision':12,
                      'plan_sha256':sha,'final_e2e':self.source,'promotion':promo,'cleanup':cleanup,
                      'lineage':[self.source,promo,cleanup]}})
        self.w.step(lambda: self.blocker(self.question)['delivered_gen']==rel['gen'])
        self.assertEqual(self.b.cards[self.question]['task']['status'],'done')
        self.assertEqual(self.b.cards[self.source]['task']['status'],'ready')
        run=self.b.claim(self.source,'e2e-tester')
        self.w.clock.advance(1)
        fence='t_f00df012'
        self.b.cards[fence]=dict(sentinel_show(card=fence,title='CLICKUP-FENCE v1 '+self.source,w=self.source,
                                              created_s=self.w.clock()//1000),atts=[],subs=[])
        self.b.cards[self.source]['parents'].append(fence)
        self.cycles(3)
        self.assertEqual(self.b.cards[fence]['task']['status'],'done')
        self.b.worker_comment(self.source,'e2e-tester',
              'CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=%d run=%d fence=%s' %
              (self.question,self.source,rel['gen'],run,fence))
        self.cycles(2)
        self.assertEqual(self.blocker(self.question)['state'],'resumed')

    def test_cached_source_read_failure_defers_then_real_observation_discovers_once(self):
        read=self.b.show
        with mock.patch.object(self.b,'show',side_effect=lambda card:
                               KanbanResult(Outcome.DEFINITE_FAIL) if card==self.source else read(card)):
            self.cycles(3)
        self.assertIsNone(self.blocker(self.question))
        self.cycles(3)
        b=self.blocker(self.question)
        self.assertIsNotNone(b)
        self.cycles(3)
        self.assertEqual(len(self.w.q('SELECT id FROM blockers WHERE question_card=?',self.question)),1)

    def test_cached_source_probe_does_not_borrow_revoked_authority(self):
        conn=store_conn(self.w)
        conn.execute("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=17")
        conn.close()
        self.cycles(3)
        self.assertIsNone(self.blocker(self.question))
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.question)],[])

    def test_cached_source_operator_hold_preserves_no_discovery(self):
        conn=store_conn(self.w)
        conn.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?",(self.tid,))
        conn.close()
        self.cycles(3)
        self.assertIsNone(self.blocker(self.question))
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.question)],[])

    def test_cached_source_terminal_task_preserves_no_discovery(self):
        conn=store_conn(self.w)
        conn.execute('UPDATE tasks SET terminal=1 WHERE clickup_task_id=?',(self.tid,))
        conn.close()
        self.cycles(3)
        self.assertIsNone(self.blocker(self.question))
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.question)],[])

    def test_invalid_creator_cannot_turn_cached_source_parent_into_a_question(self):
        for event in self.b.cards[self.question]['events']:
            if event['kind']=='created':event['payload']['creator_task_id']='t_deadbeef'
        self.cycles(3)
        self.assertIsNone(self.blocker(self.question))
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.question)],[])

    def test_cached_reopened_current_source_gets_one_status_probe_per_full_scan(self):
        counts=[]
        for _ in range(3):
            start=len(self.b.calls)
            self.cycles()
            counts.append(self.b.calls[start:].count(('show',self.source)))
        self.assertTrue(all(c<=2 for c in counts),counts)  # observer plus any actual guarded send readback
        self.assertIsNotNone(self.blocker(self.question))

    def test_actual_reopened_E_native_question_is_discovered_despite_historical_closed_cache(self):
        for key in ('blocker_closed_cards','recovery_closed_cards'):
            self.assertIn(self.source,self.f['meta'][key])
        self.cycles(4)
        b = self.blocker(self.question)
        self.assertIsNotNone(b,
                             'ERROR_NATIVE_QUESTION: existing TODO E behind genuine new Q cannot be hidden forever by a historical closed-card cache')
        self.assertEqual((b['card'],b['run_id'],b['approval_id']),(self.source,1327,17))
        self.assertIn(b['state'],('detected','relayed'))
        self.assertEqual(self.b.cards[self.question]['task']['status'],'blocked')
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.question)], [])


class CpuNativeParkTest(NativeWaitCase):
    def setUp(self):
        self.load_case('cpu')
        self.park,self.fence,self.question = 't_ab0000b3','t_ab000029','t_ab000073'

    def test_cpu_open_worker_run_defers_original_park_release(self):
        self.b.claim(self.source,'implementor')
        self.cycles(3)
        self.assertEqual(self.b.cards[self.park]['task']['status'],'blocked')
        self.assertEqual([m for m in self.b.mutations() if m==('complete',self.park)],[])

    def test_cpu_foreign_park_children_refuse_release(self):
        actual=self.b.show
        def read(card):
            result=actual(card)
            if card==self.park and isinstance(result.json,dict):
                result.json['children'].append('t_deadbeef')
            return result
        with mock.patch.object(self.b,'show',side_effect=read):
            self.cycles(3)
        self.assertEqual(self.b.cards[self.park]['task']['status'],'blocked')
        self.assertEqual([m for m in self.b.mutations() if m==('complete',self.park)],[])

    def test_actual_done_fence_and_open_park_release_normally_and_new_run_keeps_answer_fence(self):
        self.assertEqual(self.b.cards[self.source]['runs'][-1]['id'],1331)
        self.assertEqual(self.b.cards[self.source]['events'][-1]['kind'],'dependency_wait')
        self.assertEqual(self.b.cards[self.fence]['task']['status'],'done')
        self.assertEqual(self.b.cards[self.park]['task']['status'],'blocked')
        self.assertEqual(self.blocker(self.question)['delivered_gen'],5)
        self.cycles(3)
        self.assertEqual(self.b.cards[self.park]['task']['status'],'done',
                         'CPU_PARK: normal intake must release the actual already-recorded PARK after worker exit')
        [release] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', 'clickup-dp:%s:park:%s:release' % (self.tid,self.park))
        self.assertEqual(release['state'],'done')
        self.assertEqual(len([m for m in self.b.mutations() if m==('complete',self.park)]),1)
        self.assertEqual([m for m in self.b.mutations() if m[1] in (self.source,self.fence,self.question)], [])
        self.assertEqual(self.b.cards[self.source]['task']['status'],'ready')
        # A normal worker claim, then a NEW fence; the old fence cannot attest the consuming run.
        run = self.b.claim(self.source,'implementor')
        self.w.clock.advance(1)
        new_fence = 't_f00df001'
        native = sentinel_show(card=new_fence,title='CLICKUP-FENCE v1 '+self.source,w=self.source,
                               created_s=self.w.clock()//1000)
        self.b.cards[new_fence] = dict(native,atts=[],subs=[])
        self.b.cards[self.source]['parents'].append(new_fence)
        self.cycles(3)
        self.assertEqual(self.b.cards[new_fence]['task']['status'],'done')
        self.assertEqual(self.blocker(self.question)['state'],'delivered', 'fence release is not the worker ACK')
        self.b.worker_comment(self.source,'implementor',
                               'CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=5 run=%d fence=%s' %
                              (self.question,self.source,run,new_fence))
        self.cycles(2)
        self.assertEqual(self.blocker(self.question)['state'],'resumed',
                         'CPU normal answer consumption must finish only after the new-run trusted ACK')
        self.assertEqual(self.b.cards[self.source]['runs'][-1]['id'],run)
