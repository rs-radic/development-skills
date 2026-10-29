"""Native CLI boundary of the assembled lease retry; isolated board, fake ClickUp, no dispatch.

The existing full lifecycle fixture supplies the approved local state. Only its gate and promotion
are replaced with real CLI fixture cards before the stop, so native run/event/unblock semantics
are exercised without building a second planner harness or changing an existing fake.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_cli_contract as cc
import test_s4_composition as comp
from test_recovery import developing_world
from test_ticket_lease_recovery import DIAGNOSTIC_EXPIRED
from clickup_intake import kanban_port as kp, render
from clickup_intake.contracts import KanbanResult, Outcome
from clickup_intake.store import Store


class NativeTicketLeaseRecoveryTest(unittest.TestCase):
    def test_native_blocked_run_is_continued_once_after_renewal_and_lost_ack(self):
        staging = Path(__file__).resolve().parents[1] / 'staging' / 'lease-native'
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as home, \
                mock.patch.object(comp, 'TMP_ROOT', str(staging / 'world')):
            board = 'ticket-lease-offline'
            env = cc.isolated_env(HERMES_HOME=home, HERMES_KANBAN_HOME=home)

            def cli(*args, worker=None):
                child_env = dict(env)
                if worker:
                    child_env.update(HERMES_KANBAN_TASK=worker[0], HERMES_KANBAN_RUN_ID=str(worker[1]),
                                     HERMES_KANBAN_DB=str(Path(home) / 'kanban' / 'boards' / board / 'kanban.db'))
                rc, out, err = cc.cli(*args, board=board, env=child_env)
                self.assertEqual(rc, 0, err)
                return out

            created = cli('boards', 'create', board)
            self.assertIn(home.replace('\\', '/').lower(), created.replace('\\', '/').lower())
            port = kp.KanbanCli(cc.HERMES_EXE, board, discord_chat=cc.CHAT, env=env)
            w, life = developing_world(self)
            life.e2e_done_to_ticketed()
            gate = json.loads(cli('create', '--json', '--', 'isolated approval gate'))['id']
            cli('complete', gate, '--summary', 'fixture approval gate is done')
            body = Path(home) / 'promotion.md'
            body.write_text(render.promotion_block(gate, life.tid, 1, life.plan_sha), encoding='utf-8')
            promo = json.loads(cli('create', '--assignee', 'integrator', '--parent', gate,
                                   '--body-file', str(body), '--json', '--', 'isolated promotion'))['id']
            # Replace only the test fixture binding; never a live intake or native board SQL write.
            store = Store(w.state_path, w.clock)
            try:
                approval = dict(store.conn.execute('SELECT * FROM approvals').fetchone())
                gates = json.loads(approval['clickup_gates'])
                gates['promotion'] = promo
                gates['lineage'] = [promo if x == life.promo else x for x in gates['lineage']]
                with store.transaction() as c:
                    c.execute('UPDATE approvals SET gate_card=?,clickup_gates=? WHERE id=?',
                              (gate, json.dumps(gates), approval['id']))
            finally:
                store.close()
            w.clock.advance(1801)
            self.assertEqual(w.main('ticket-check', '--gate', gate, '--task', life.tid, '--rev', '1',
                                    '--sha', life.plan_sha)[1], ['INVALID lease_expired'])
            cli('claim', promo)
            run = kp.latest_run_id(port.show(promo).json)
            reason = (DIAGNOSTIC_EXPIRED.replace('t_ab000039', gate).replace('86pub00002', life.tid)
                      .replace('rev 3,', 'rev 1,'))
            cli('block', '--kind', 'needs_input', promo, reason, worker=(promo, run))
            stopped = port.show(promo).json
            self.assertIsNotNone(kp.ticket_lease_stop(stopped), stopped)
            history = copy.deepcopy(stopped['runs'])
            fake_show = w.board.show
            fake_unblock = w.board.unblock
            sends = []

            def show(card):
                return port.show(card) if card in (promo, gate) else fake_show(card)

            def unblock(card):
                if card != promo:
                    return fake_unblock(card)
                sends.append(card)
                port.on_spawn = w.board.on_spawn
                try:
                    result = port.unblock(card)
                finally:
                    port.on_spawn = None
                self.assertEqual(result.outcome, Outcome.OK, result.json)
                return KanbanResult(Outcome.AMBIGUOUS)  # applied CLI write, lost parent ACK

            with mock.patch.object(w.board, 'show', show), mock.patch.object(w.board, 'unblock', unblock):
                w.clock.advance(61)
                w.run()
                w.run()  # a fresh writer instance reconciles the same durable row
                w.clock.advance(61)
                w.run()
                self.assertEqual(w.main('ticket-check', '--gate', gate, '--task', life.tid, '--rev', '1',
                                        '--sha', life.plan_sha), (0, ['VALID epoch=1']))
            final = port.show(promo).json
            self.assertEqual(final['task']['status'], 'ready')
            self.assertEqual(final['runs'], history)
            self.assertEqual(sends, [promo])
            self.assertEqual(sum(e['kind'] == 'unblocked' for e in final['events']), 1)
            rows = w.q("SELECT state FROM outbox WHERE idem_key LIKE '%:ticket-lease:%'")
            self.assertEqual(rows, [{'state': 'done'}])
        self.assertFalse(Path(home).exists())


if __name__ == '__main__':
    unittest.main()
