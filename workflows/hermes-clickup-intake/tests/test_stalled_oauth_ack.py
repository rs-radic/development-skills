"""Sanitized OAuth stop/ACK replay through main -> scan -> progress/evaluate -> execute.
Native row shapes and binding relationships use public identities and projected policy prose.
Provider responses and clock are explicit fakes; this is not live acceptance evidence.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest import mock

from clickup_intake import engine, main, stalled, kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome
from test_recovery import RecoveryBoard, store_conn
import test_s4_composition as comp

FIXTURE = Path(__file__).parent / 'fixtures/oauth_diagnostic_ack.json'
CARD, QUESTION, TID = 't_ab00001b', 't_ab00003f', '86pub00003'
ACK_SHA = 'ce1e2a3b7681de9756cfaa0f364a2b00cc16df45bbf272de7367a8c6d1bd85ae'


class OAuthDiagnosticAckTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids real subprocess/network'))
            p.start()
            self.addCleanup(p.stop)
        self.f = json.loads(FIXTURE.read_bytes())
        self.w = comp.World(self)
        self.w.clock.now_ms = 1792853919761  # historical post-ACK scan time, fake clock, not a live measurement
        self.w.write_config(cutover_ms=1790000000000, mode='shadow')
        self.w.board = RecoveryBoard(str(Path(self.w.dir) / 'board'), self.w.clock, self.w.inj)
        self.b = self.w.board
        self.w.cu.add_task(task_id=TID, status='reviewing feature', description='Offline provider substrate, not live content.',
                           created_ms=1792780000000)
        self.assertEqual(self.w.run()[0], 0)
        self.w.write_config(mode='live')
        self.w.cu.set_status(TID, 'implementing')
        conn = store_conn(self.w)
        substrate = dict(conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (TID,)).fetchone())
        conn.execute('DELETE FROM outbox')
        task = dict(self.f['task'])
        # Provider content and observation times belong to the fake provider; actual authority/obligations do not.
        for key in ('content_hash', 'attach_ids_hash', 'list_fp', 'full_cursor', 'full_started_ms'):
            task[key] = substrate[key]
        for key in ('last_light_ms', 'last_full_ms', 'last_task_get_ms', 'barrier_ok_ms'):
            task[key] = self.w.clock() - 301000
        task['quiet_until_ms'] = self.w.clock() - 1000
        conn.execute('UPDATE tasks SET %s WHERE clickup_task_id=?' % ','.join(k+'=?' for k in task if k!='clickup_task_id'),
                     [v for k,v in task.items() if k!='clickup_task_id']+[TID])
        def insert(table, row):
            conn.execute('INSERT INTO %s(%s) VALUES (%s)' % (table, ','.join(row), ','.join('?' for _ in row)), list(row.values()))
        for table in ('approvals', 'cards', 'blockers', 'fences'):
            for row in self.f[table]:
                insert(table, row)
        insert('recoveries', self.f['incident'])
        for key, value in self.f['meta'].items():
            conn.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, json.dumps(value)))
        conn.close()
        for cid, shown in self.f['shows'].items():
            self.b.cards[cid] = dict(copy.deepcopy(shown), atts=[], subs=[])
        for cid, plan in self.f['plans'].items():
            data = base64.b64decode(plan['bytes_b64'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), self.f['incident']['plan_sha256'])
            p = Path(self.w.dir) / plan['name']
            p.write_bytes(data)
            self.b.cards[cid]['atts'] = [{'filename': plan['name'], 'stored_path': str(p)}]
        self.before = self.snapshot()
        self.assertEqual((self.before['incident']['id'], self.before['incident']['run_id'], self.before['incident']['attempts']), (8,1263,2))
        self.assertEqual(hashlib.sha256(self.b.cards[CARD]['comments'][0]['body'].encode()).hexdigest(), ACK_SHA)
        self.evidence = {'provenance': self.f['provenance'], 'cycles': [], 'before': self.before}

    def notices(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")

    def snapshot(self):
        return {'incident': self.w.q('SELECT * FROM recoveries WHERE id=8')[0], 'notices': self.notices(),
                'blocker': self.w.q('SELECT * FROM blockers WHERE id=6')[0],
                'waits': self.w.q("SELECT key,value FROM meta WHERE key LIKE 'diagnostic_wait:%'"),
                'source': copy.deepcopy(self.b.cards.get(CARD)), 'mutations': list(self.b.mutations())}

    def cycle(self, n=1):
        for _ in range(n):
            self.w.clock.advance(301)
            calls = len(self.b.calls)
            trace = []
            evaluate = engine.evaluate
            def traced(store, now, obs=None, config=None):
                found = stalled.candidate(dict(store.conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?',(TID,)).fetchone()),
                    dict(store.conn.execute('SELECT * FROM approvals WHERE id=20').fetchone()), CARD,
                    obs.cards, stalled.context(store.conn,TID),obs.attachments) if obs else None
                trace.append({'cards':sorted(obs.cards), 'attachments':sorted(obs.attachments), 'candidate':found})
                return evaluate(store,now,obs,config)
            with mock.patch.object(engine, 'evaluate', side_effect=traced):
                self.assertEqual(self.w.run()[0], 0)
            self.evidence['cycles'].append({'trace':trace,'native_calls':self.b.calls[calls:], 'snapshot':self.snapshot()})

    def assert_preserved(self):
        state = self.snapshot()
        self.assertEqual(state['blocker'], self.before['blocker'])
        self.assertEqual(state['source'], self.before['source'])
        self.assertEqual([m for m in self.b.mutations() if m[1] in (CARD,QUESTION)], [])
        for key in ('id','run_id','reason_sha256','approval_id','attempts','abs_ms','detected_ms'):
            self.assertEqual(state['incident'][key], self.before['incident'][key], key)

    def test_actual_coordinator_cannot_act_ACK_delivers_precise_verified_origin_notice_once(self):
        self.assert_delivered()

    def test_only_default_actor_substitution_restores_existing_delivery(self):
        self.b.cards[CARD]['comments'][0]['author'] = 'default'
        self.before = self.snapshot()
        self.assert_delivered()

    def assert_delivered(self):
        first = len(self.evidence['cycles'])
        self.cycle(3)
        full = self.evidence['cycles'][first]['trace'][-1]
        self.assertEqual(full['candidate']['reason_sha256'], self.before['incident']['reason_sha256'],
                         'fixture must select the actual unchanged stop, not repair its fingerprint')
        ack = kp.recovery_ack(self.b.show(CARD).json,CARD,1263,1792785381)
        self.assertEqual(ack['final'], 'cannot_act', 'unchanged public ACK parser must recognize actual input')
        self.assertEqual(len(self.notices()), 1,
                         'REAL_OAUTH_ACK: current coordinator cannot_act must queue and deliver the originating technical escalation')
        row = self.notices()[0]
        self.assertEqual((row['state'],row['attempts']), ('done',1))
        self.assertEqual(row['target'], TID)
        text = json.loads(row['body'])['text']
        self.assertIn('approved continuation is missing a supported workflow transition', text)
        self.assertIn('read-only state and approval inspection', text)
        self.assertIn('separately authorized and independently reviewed integration repair', text)
        self.assertNotIn('Required operator action: repair the structured', text, 'never relay raw ACK prose')
        self.assertEqual(self.snapshot()['incident']['state'],'escalated')
        self.assertTrue(any(c['id']==row['result_ref'] for c in self.w.bot_comments(TID)))
        self.assertEqual(self.w.cu.count('POST','/v2/task/%s/comment'%TID),1)
        self.assertEqual(self.snapshot()['waits'], [], 'technical inability is not a fabricated human decision')
        before = self.notices()
        self.cycle(3)
        self.assertEqual(self.notices(),before)
        self.assert_preserved()

    def assert_deferred_then_recovers(self, target, fault):
        real = getattr(self.b, target)
        with mock.patch.object(self.b, target, side_effect=lambda cid: fault(cid, real)):
            self.cycle(2)
        self.assertEqual(self.notices(), [], 'unavailable current evidence cannot finalize the ACK')
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.assert_preserved()
        self.assert_delivered()

    def test_unreadable_native_parent_defers_then_ordinary_observation_recovers(self):
        self.assert_deferred_then_recovers('show', lambda cid, real:
            KanbanResult(Outcome.DEFINITE_FAIL) if cid == 't_ab000066' else real(cid))

    def test_unreadable_question_defers_then_ordinary_observation_recovers(self):
        self.assert_deferred_then_recovers('show', lambda cid, real:
            KanbanResult(Outcome.DEFINITE_FAIL) if cid == QUESTION else real(cid))

    def test_unreadable_plan_source_defers_then_ordinary_observation_recovers(self):
        self.assert_deferred_then_recovers('show', lambda cid, real:
            KanbanResult(Outcome.DEFINITE_FAIL) if cid == 't_ab00009b' else real(cid))

    def test_unavailable_plan_attachments_defers_then_ordinary_observation_recovers(self):
        self.assert_deferred_then_recovers('attachments', lambda cid, real:
            KanbanResult(Outcome.DEFINITE_FAIL) if cid == 't_ab00009b' else real(cid))

    def test_unreadable_exact_plan_bytes_defers_then_recovers(self):
        att = self.b.cards['t_ab00009b']['atts'][0]
        path = att['stored_path']
        att['stored_path'] = str(Path(self.w.dir) / 'absent-plan.md')
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        att['stored_path'] = path
        self.assert_delivered()

    def test_different_reviewed_plan_evidence_does_not_finalize_the_ACK(self):
        runs = self.b.cards['t_ab00009b']['runs']
        original = copy.deepcopy(runs)
        for run in runs:
            if isinstance(run.get('metadata'),dict) and 'plan_revision' in run['metadata']:
                run['metadata']['plan_revision'] = 99
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.b.cards['t_ab00009b']['runs'] = original
        self.assert_delivered()

    def test_unavailable_provider_observation_defers_then_real_caller_recovers(self):
        for _ in range(2):
            self.w.cu.inject('GET','/v2/task/%s$'%TID,Outcome.DEFINITE_FAIL,status=503)
        self.cycle(2)
        self.assertEqual(self.w.cu.faults, [], 'fault must reach the actual provider boundary')
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.assert_delivered()

    def test_healthy_source_worker_is_not_escalated_by_the_ACK(self):
        self.b.cards[CARD]['task']['status'] = 'running'
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.b.cards[CARD]['task']['status'] = 'todo'
        self.assert_delivered()

    def test_genuine_new_dependency_does_not_become_an_ACK_escalation(self):
        parent = self.b._new('Offline genuine dependency', status='blocked')
        self.b.cards[CARD]['parents'].append(parent)
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.b.cards[CARD]['parents'].remove(parent)
        self.assert_delivered()

    def test_revoked_authority_cannot_finalize_the_ACK(self):
        conn=store_conn(self.w)
        conn.execute("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=20")
        conn.close()
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'superseded')
        self.assert_preserved()

    def test_material_change_cannot_finalize_the_ACK(self):
        conn=store_conn(self.w)
        conn.execute('UPDATE tasks SET input_gen=5,classified_gen=5,scope_gen=5 WHERE clickup_task_id=?',(TID,))
        conn.close()
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'superseded')
        self.assert_preserved()

    def test_unclassified_input_defers_without_treating_authority_as_revoked(self):
        conn=store_conn(self.w)
        conn.execute('UPDATE tasks SET input_gen=5 WHERE clickup_task_id=?',(TID,))
        conn.close()
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')
        self.assert_preserved()

    def test_foreign_worker_cannot_use_the_new_technical_ACK_path(self):
        self.b.cards[CARD]['comments'][0]['author'] = 'unrelated-worker'
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['waits'], [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')

    def test_intake_cannot_attest_technical_inability(self):
        self.b.cards[CARD]['comments'][0]['author'] = kp.AUTHOR
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['incident']['state'], 'open')

    def test_wrong_run_or_stale_coordinator_ACK_cannot_finalize(self):
        cm=self.b.cards[CARD]['comments'][0]
        original=copy.deepcopy(cm)
        for change in ({'body':cm['body'].replace('run=1263','run=0')}, {'created_at':1792785380}):
            cm.update(original)
            cm.update(change)
            self.cycle()
            self.assertEqual(self.notices(), [])
            self.assertEqual(self.snapshot()['incident']['state'], 'open')
        cm.update(original)
        self.assert_delivered()

    def test_default_owner_still_requires_exact_structured_question_and_verified_target_wait(self):
        import test_stalled_remediation as remediation
        self.tid=TID
        self.b.cards[CARD]['comments']=[]
        q=remediation.RemediationTest.question_ack(self,CARD,1263)
        self.before=self.snapshot()
        self.cycle(3)
        [row]=self.notices()
        self.assertEqual((row['target'],row['state'],row['attempts']),(TID,'done',1))
        wait=json.loads(self.snapshot()['waits'][0]['value'])
        self.assertEqual(wait['state'],'waiting')
        self.assertEqual(wait['root'],row['result_ref'])
        self.assertEqual(wait['question'],q)
        self.assertIn(q['decision'],json.loads(row['body'])['text'])
        self.assertEqual(self.snapshot()['incident']['state'],'open')
        self.w.clock.now_ms=self.before['incident']['abs_ms']+1
        self.cycle(2)
        self.assertEqual(self.notices(),[row])
        self.assertEqual(self.w.main('recovery-check','--card',CARD,'--run','1263'),
                         (3,['STALE diagnostic_human_wait']))
        self.assert_preserved()

    def test_operator_alias_does_not_gain_default_owner_decision_authority(self):
        import test_stalled_remediation as remediation
        self.tid=TID
        self.b.cards[CARD]['comments']=[]
        remediation.RemediationTest.question_ack(self,CARD,1263)
        self.b.cards[CARD]['comments'][-1]['author']='coordinator'
        self.cycle(2)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.snapshot()['waits'], [])
        self.assertEqual(self.snapshot()['incident']['state'],'open')
