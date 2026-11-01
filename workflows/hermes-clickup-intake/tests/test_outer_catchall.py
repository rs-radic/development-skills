"""Outer inactivity fallback: real Glue scan order, offline external boundaries.

The incident is a sanitized persisted/native field projection, not an API replay.
Uncaptured ClickUp text and old cards are synthetic; no product plan bytes or
native history are invented to make a PASS. The direct causal assertion uses the
source-equivalent reviewer metadata and a synthetic attachment digest, separately
from fake file IO. Identities and titles are sanitized, not exact captured bytes.
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from clickup_intake import engine, main, preworker, progress
from clickup_intake.store import Store
import test_recovery as recovery
import test_s4_composition as composition
from preworker_snapshot_fixture import install

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/outer_catchall.json').read_text())


class OuterCatchallTest(unittest.TestCase):
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
        self.tid = FIXTURE['task']['clickup_task_id']
        self.w.clock.now_ms = FIXTURE['task']['last_full_ms'] + 1
        self.w.cu.add_task(task_id=self.tid)
        self.w.run()  # synthetic API content has its own honest hashes
        self.w.cu.set_status(self.tid, 'implementing')
        self.w.main('recovery', 'enable')
        c = recovery.store_conn(self.w)
        try:
            c.execute('DELETE FROM outbox')
            c.execute('DELETE FROM cards')
            c.execute('UPDATE items SET delivered_relay=?', (FIXTURE['blocker']['relay_ref'],))
            fields = {k: v for k, v in FIXTURE['task'].items() if k not in
                      ('clickup_task_id', 'content_hash', 'attach_ids_hash', 'list_fp',
                       'last_light_ms', 'last_full_ms', 'last_task_get_ms', 'full_started_ms')}
            c.execute('UPDATE tasks SET ' + ','.join(k+'=?' for k in fields) + ' WHERE clickup_task_id=?',
                      [*fields.values(), self.tid])
            for a in FIXTURE['approvals']:
                c.execute('INSERT INTO approvals('+','.join(a)+') VALUES ('+','.join('?' for _ in a)+')', list(a.values()))
            b = FIXTURE['blocker']
            c.execute('INSERT INTO blockers('+','.join(b)+') VALUES ('+','.join('?' for _ in b)+')', list(b.values()))
            c.execute("INSERT INTO cards(card_id,task,role,seq) VALUES (?,?, 'request',2)",
                      ('t_e0000016', self.tid))
            for relay in FIXTURE['relays']:
                c.execute('INSERT INTO relays('+','.join(relay)+') VALUES ('+
                          ','.join('?' for _ in relay)+')', list(relay.values()))
            pending = FIXTURE['pending']
            c.execute('INSERT INTO outbox('+','.join(pending)+') VALUES ('+
                      ','.join('?' for _ in pending)+')', list(pending.values()))
        finally:
            c.close()
        for t in FIXTURE['tasks']:
            card = t['id']
            self.b.cards[card] = dict(task=copy.deepcopy(t), parents=[], runs=[copy.deepcopy(r)
                for r in FIXTURE['runs'] if r['task_id'] == card], children=[], events=[], comments=[], atts=[], subs=[])
        # Retain the source graph with mapped IDs; unprojected endpoints are synthetic inert stubs.
        for edge in FIXTURE['links']:
            for card in edge.values():
                if card not in self.b.cards:
                    self.b.cards[card] = dict(task=dict(id=card, status='done', current_run_id=None,
                        worker_pid=None), parents=[], runs=[], events=[], comments=[], atts=[], subs=[])
            self.b.cards[edge['child_id']]['parents'].append(edge['parent_id'])
        self.assertEqual(self.b.cards['t_e0000016']['parents'], [])

    def scans(self, count):
        for _ in range(count):
            self.w.clock.advance(61)
            self.assertEqual(self.w.run()[0], 0)

    def carriers(self):
        return self.b.by_key('clickup-dp:%s:rcv:' % self.tid)

    def test_heartbeat_and_polling_do_not_reset_inactivity(self):
        for _ in range(25):
            self.b.cards['t_e0000016']['task']['last_heartbeat_at'] = self.w.clock() // 1000
            self.scans(1)
        self.assertEqual(len(self.carriers()), 1)

    def test_meaningful_native_movement_restarts_grace(self):
        self.scans(8)
        self.b.worker_comment('t_e0000016', 'plan-reviewer', 'Verified the current handoff; working on owner routing.')
        self.scans(8)
        self.assertEqual(self.carriers(), [])
        self.scans(15)
        self.assertEqual(len(self.carriers()), 1)

    def test_delivered_current_planning_question_stays_quiet(self):
        c = recovery.store_conn(self.w)
        c.execute("UPDATE tasks SET phase='awaiting_business' WHERE clickup_task_id=?", (self.tid,))
        c.close()
        question = self.w.cu.add_comment(self.tid, composition.BOT, 'Which existing group should receive this feature?')
        c = recovery.store_conn(self.w)
        row = dict(FIXTURE['pending'], idem_key='clickup-dp:'+self.tid+':q:business:fixture',
                   kind='cu_comment', target=self.tid, state='done', outcome='OK', attempts=1,
                   result_ref=question, body=json.dumps({'text': 'Which existing group should receive this feature?'}))
        c.execute('INSERT INTO outbox('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', list(row.values()))
        c.close()
        self.scans(25)
        self.assertEqual(self.carriers(), [])

    def test_delivered_current_native_question_stays_quiet_while_internally_held(self):
        question = self.w.cu.add_comment(self.tid, composition.BOT, 'Which current approved account, A or B?')
        c = recovery.store_conn(self.w)
        c.execute("UPDATE tasks SET hold_reason='internal_stage_wait' WHERE clickup_task_id=?", (self.tid,))
        c.execute("UPDATE blockers SET state='asked',approval_id=33,card='t_e0000016',run_id=1551 WHERE id=5")
        row = dict(FIXTURE['pending'], idem_key=FIXTURE['blocker']['question_key'], kind='cu_comment',
                   target=self.tid, state='done', outcome='OK', attempts=1, result_ref=question,
                   body=json.dumps({'text':'Which current approved account, A or B?'}))
        c.execute('INSERT INTO outbox('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')', list(row.values()))
        c.close()
        self.scans(25)
        self.assertEqual(self.carriers(), [])

    def test_failed_wake_is_not_success_and_does_not_duplicate_incident(self):
        from clickup_intake.contracts import KanbanResult, Outcome
        original = self.b.complete
        def complete(card, summary, metadata=None):
            if card in self.carriers():
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return original(card, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=complete):
            self.scans(25)
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(inc['state'], 'open')
        self.assertEqual(inc['attempts'], 1)
        self.assertEqual(len(self.carriers()), 1)
        self.assertNotEqual(self.b.cards[self.carriers()[0]]['task']['status'], 'done')
        [wake] = self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE '%:rcv:%'")
        self.assertNotEqual(wake['state'], 'done')
        self.assertGreater(wake['attempts'], 0)

    def test_movement_after_wake_keeps_the_existing_recovery_budget(self):
        self.scans(20)
        [before] = self.w.q('SELECT * FROM recoveries')
        self.b.worker_comment('t_e0000016', 'plan-reviewer', 'Verified owner routing; next transition still pending.')
        self.scans(20)
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(after['state'], 'open')
        self.assertEqual((after['id'], after['attempts'], after['abs_ms']),
                         (before['id'], before['attempts'], before['abs_ms']))
        self.assertNotEqual(after['reason_sha256'], before['reason_sha256'])
        self.assertEqual(len(self.carriers()), 1)

    def test_lease_renewal_is_not_meaningful_progress(self):
        self.scans(6)
        with contextlib.closing(Store(self.w.state_path, self.w.clock).conn) as conn:
            state = progress.context(conn, self.tid)
            task = dict(conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
        shows = {card: self.b.show(card).json for card in self.b.cards}
        atts = {'t_e0000016': []}
        first = progress.outer(task, state, shows, atts, self.w.clock(), task['last_light_ms'])[1]
        self.assertIsNotNone(first)
        state['approvals'][-1]['ticket_renewed_ms'] = self.w.clock()
        second = progress.outer(task, state, shows, atts, self.w.clock(), task['last_light_ms'])[1]
        self.assertEqual(first['progress'], second['progress'])

    def test_explicit_operator_pause_stays_quiet(self):
        self.assertEqual(self.w.main('hold', self.tid)[0], 0)
        self.scans(25)
        self.assertEqual(self.carriers(), [])

    def test_unknown_internal_hold_still_wakes_without_releasing_work(self):
        c = recovery.store_conn(self.w)
        c.execute("UPDATE tasks SET phase='hold', hold_reason='internal_stage_wait' WHERE clickup_task_id=?",
                  (self.tid,))
        c.close()
        self.scans(25)
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.b.cards[self.carriers()[0]]['task']['status'], 'done')
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'internal_stage_wait')
        self.assertEqual(self.w.q('SELECT state FROM approvals WHERE id=33')[0]['state'], 'recorded')

    def test_captured_superseded_question_cannot_veto_outer_wake(self):
        with contextlib.closing(Store(self.w.state_path, self.w.clock).conn) as conn:
            state = preworker.context(conn, self.tid)
            a = dict(conn.execute('SELECT * FROM approvals WHERE id=33').fetchone())
            t = dict(conn.execute('SELECT * FROM tasks WHERE clickup_task_id=?', (self.tid,)).fetchone())
            shows = {card: self.b.show(card).json for card in self.b.cards}
            atts = {'t_e0000016': [FIXTURE['attachment']]}
            self.assertIsNotNone(preworker.source_evidence(shows['t_e0000016'], atts['t_e0000016'], self.tid))
            self.assertIsNone(preworker.inspect(t, a, state, shows, atts, self.w.clock(), self.w.clock()))
            self.assertEqual(progress.prerequisite_status('t_e0000016', state, shows), 'wait')
        self.scans(18)
        self.assertEqual(len(self.carriers()), 1, 'outer inactivity must reach a real diagnostic wake')
        carrier = self.b.cards[self.carriers()[0]]
        self.assertEqual(carrier['task']['status'], 'done')
        self.assertEqual([s['delivery_mode'] for s in carrier['subs']], ['wake'])
        [inc] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', str(inc['run_id']))[0], 0)
        self.assertEqual(self.w.q('SELECT state FROM blockers WHERE id=5')[0]['state'], 'superseded')
        self.assertEqual(self.b.cards['t_e000000c']['task']['status'], 'blocked')
        self.assertEqual(self.w.q('SELECT state FROM approvals WHERE id=33')[0]['state'], 'recorded')
        self.assertIsNone(self.w.q('SELECT gate_card FROM approvals WHERE id=33')[0]['gate_card'])
        self.scans(15)
        self.assertEqual(len(self.carriers()), 1, 'the existing recovery owns the unchanged stall')
