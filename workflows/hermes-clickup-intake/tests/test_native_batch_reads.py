"""Routine card observation through one bounded native read, not one CLI process per card.

Approved source scope: reuse the integration-local read-only native snapshot for
the routine collection of cards that currently tracked ClickUp tasks require; actual writes and fresh
effect-time checks stay on the supported CLI. Offline: disposable native-schema fixture databases only.
"""
import ast
import contextlib
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from clickup_intake import main, kanban_port as kp, preworker_snapshot
from clickup_intake.config import load_config
from clickup_intake.store import Store
import test_s4_composition as composition

NATIVE_ROOT = Path(main.HERMES_EXE).parents[2]


def native_schema():
    module = ast.parse((NATIVE_ROOT / 'hermes_cli/kanban_db.py').read_text(encoding='utf-8'))
    return next(ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_SQL' for t in n.targets))


def write_native(board, path, created_s):
    """The fake board's cards as rows of the installed native schema (new offline fixture rows)."""
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')  # as the live board
        db.executescript(native_schema())
        for cid, c in board.cards.items():
            t = c['task']
            db.execute('INSERT INTO tasks (id, title, body, assignee, status, created_by, created_at, completed_at,'
                       ' skills) VALUES (?,?,?,?,?,?,?,?,?)',
                       (cid, t['title'], t.get('body') or '', t.get('assignee'), t['status'], t.get('created_by'),
                        created_s, t.get('completed_at'), json.dumps(t.get('skills') or [])))
            db.executemany('INSERT INTO task_links (parent_id, child_id) VALUES (?,?)',
                           [(p, cid) for p in c['parents']])
            for r in c['runs']:
                db.execute('INSERT INTO task_runs (id, task_id, profile, status, outcome, metadata, summary,'
                           ' started_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?)',
                           (r['id'], cid, r.get('profile'), r.get('status'), r.get('outcome'),
                            None if r.get('metadata') is None else json.dumps(r['metadata']), r.get('summary'),
                            r.get('started_at') or created_s, r.get('ended_at')))
            for x in c['comments']:
                db.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)',
                           (cid, x['author'], x['body'], x['created_at']))
            for e in c['events']:
                db.execute('INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)',
                           (cid, e['kind'], json.dumps(e.get('payload')) if e.get('payload') else None,
                            e['created_at']))
            for a in c['atts']:
                db.execute('INSERT INTO task_attachments (task_id, filename, stored_path, size, uploaded_by,'
                           ' created_at) VALUES (?,?,?,?,?,?)',
                           (cid, a['filename'], a['stored_path'], a['size'], a.get('uploaded_by'), created_s))


def essential(show):
    """Fields the observation consumers read, independent of fake-board-only keys and list order."""
    task = show['task']
    return dict(status=task['status'], title=task['title'], assignee=task.get('assignee'),
                body=task.get('body') or '', parents=sorted(show['parents']), children=sorted(show['children']),
                runs=[(r['id'], r.get('profile'), r.get('status'), r.get('outcome'), r.get('metadata'))
                      for r in show['runs']],
                comments=[(x['author'], x['body']) for x in show['comments']],
                events=[(e['kind'], e.get('payload') or None) for e in show['events']])


class NativeBatchReadsTest(unittest.TestCase):
    def setUp(self):
        self.w = composition.World(self)
        self.life = composition.Lifecycle(self.w)
        self.life.pickup_to_business()
        self.life.business_to_technical()
        self.life.technical_to_published()
        self.life.approve_to_released()
        self.life.orchestrate()
        for feature in ('blockers', 'recovery'):  # both observation features are enabled live
            self.assertEqual(self.w.main(feature, 'enable')[0], 0)
        b = self.w.board
        # A completed prerequisite that still supports the unfinished workflow.
        self.ancestor = b.worker_create('required completed prerequisite')
        b.finish(self.ancestor, 'implementor')
        b.cards[self.life.fe]['parents'].append(self.ancestor)
        # A retired workflow: terminal intake task whose request card was already observed closed.
        self.retired = [b.worker_create('retired request'), b.worker_create('retired orchestrator')]
        for card in self.retired:
            b.finish(card, 'planner')
        conn = Store(self.w.state_path, self.w.clock).conn
        try:
            conn.execute("INSERT INTO tasks (clickup_task_id, terminal) VALUES ('retired1', 1)")
            conn.execute("INSERT INTO cards (card_id, task, role, seq, created_key) VALUES (?, 'retired1',"
                         " 'request', 1, 'retired-req')", (self.retired[0],))
            conn.execute("INSERT INTO cards (card_id, task, role, seq, created_key) VALUES (?, 'retired1',"
                         " 'orchestrator', 2, 'retired-orch')", (self.retired[1],))
            closed = json.loads((conn.execute("SELECT value FROM meta WHERE key=?", (main.CLOSED_CARDS_KEY,))
                                 .fetchone() or ['[]'])[0])
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (main.CLOSED_CARDS_KEY, json.dumps(sorted(set(closed) | {self.retired[0]}))))
        finally:
            conn.close()
        self.native = Path(self.w.dir) / 'native-board.db'
        write_native(b, self.native, self.w.clock() // 1000)
        from preworker_snapshot_fixture import install
        install(b, self.w.dir)  # the legacy port uses the same snapshot path for dependency evidence
        self.launches, self.roots = [], []
        acquire = preworker_snapshot.acquire

        def recorded(path, roots, *a, **kw):
            self.roots.append(set(roots))
            return acquire(path, roots, *a, **kw)
        self.enterContext(mock.patch.object(preworker_snapshot, 'acquire', side_effect=recorded))

    def runner(self, argv, env, timeout, on_spawn):
        """CLI boundary: every launch is counted; read verbs answer from the same fixture data."""
        self.launches.append(argv[4:])
        verb, card = argv[4], argv[5]
        res = getattr(self.w.board, verb)(card)
        ok = res.outcome is kp.Outcome.OK
        return kp.RunOutput(0 if ok else 1, json.dumps(res.json) if ok else '', '' if ok else 'no such task')

    def native_port(self):
        import sys
        sys.path.insert(0, str(NATIVE_ROOT))
        self.addCleanup(sys.path.remove, str(NATIVE_ROOT))
        import hermes_cli.kanban_db as native
        import hermes_cli.update_lock as locks
        import hermes_cli.venv_sync as sync
        self.enterContext(mock.patch.object(native, 'kanban_db_path', return_value=self.native))
        self.enterContext(mock.patch.object(locks, 'update_in_progress', return_value=False))
        self.enterContext(mock.patch.object(sync, 'completion_pending_path',
                                            return_value=Path(self.w.dir) / 'absent-marker'))
        return kp.KanbanCli(main.HERMES_EXE, 'fixture', discord_chat=composition.CHAT, runner=self.runner)

    def glue(self, port, label):
        """A Glue over a private copy of the same intake state, as the run command builds it."""
        runtime = Path(self.w.dir) / label
        runtime.mkdir()
        with contextlib.closing(sqlite3.connect(self.w.state_path)) as src, \
                contextlib.closing(sqlite3.connect(runtime / 'state.sqlite3')) as dst:
            src.backup(dst)
        import dataclasses
        cfg = dataclasses.replace(load_config(os.path.join(self.w.runtime, 'config.json')), runtime_dir=str(runtime))
        store = Store(cfg.state_path, self.w.clock)
        self.addCleanup(store.close)
        return main.Glue(cfg, store, None, port, self.w.clock())

    def test_routine_observation_launches_no_cli_and_observes_equivalent_cards(self):
        legacy = self.glue(self.w.board, 'legacy')
        legacy.observe_cards()
        native = self.glue(self.native_port(), 'native')
        self.roots.clear()
        native.observe_cards()
        self.assertEqual(self.launches, [], 'routine observation must not start one CLI process per card')
        self.assertEqual(sorted(native.obs.cards), sorted(legacy.obs.cards))
        for card in legacy.obs.cards:
            self.assertEqual(essential(native.obs.cards[card]), essential(legacy.obs.cards[card]), card)
        self.assertEqual({c: [(a['name'], a['sha256']) for a in v] for c, v in native.obs.attachments.items()},
                         {c: [(a['name'], a['sha256']) for a in v] for c, v in legacy.obs.attachments.items()})
        for card in (self.life.orch(), self.life.gate(), self.life.fe, self.life.promo, self.life.req_cards()[0],
                     self.ancestor):
            self.assertIn(card, native.obs.cards)
        self.assertEqual(native.obs.cards[self.ancestor]['task']['status'], 'done')
        read = set().union(*self.roots)
        self.assertFalse(read & set(self.retired), 'a retired workflow is not part of routine collection')
        self.assertNotIn(self.retired[0], native.obs.cards)
        self.assertNotIn(self.retired[1], native.obs.cards)

    def want(self):
        return {self.life.orch(), self.life.gate(), self.life.fe, self.life.promo, self.life.req_cards()[0]}

    def rewrite_native(self):
        self.native.unlink()
        write_native(self.w.board, self.native, self.w.clock() // 1000)

    def test_unavailable_native_read_is_unknown_never_empty_success_or_cli_fallback(self):
        for mode in ('missing_database', 'update_in_progress', 'custody_marker'):
            with self.subTest(mode=mode):
                port = self.native_port()
                import hermes_cli.kanban_db as native
                import hermes_cli.update_lock as locks
                import hermes_cli.venv_sync as sync
                patch = {'missing_database': (native, 'kanban_db_path', Path(self.w.dir) / 'absent.db'),
                         'update_in_progress': (locks, 'update_in_progress', True),
                         'custody_marker': (sync, 'completion_pending_path', self.native)}[mode]
                glue = self.glue(port, mode)
                closed = {k: glue.meta(k) for k in (main.CLOSED_CARDS_KEY, main.WATCHED_CLOSED_KEY,
                                                     main.RECOVERY_CLOSED_KEY)}
                self.launches.clear()
                with mock.patch.object(patch[0], patch[1], return_value=patch[2]):
                    glue.observe_cards()
                self.assertEqual(self.launches, [], mode)
                self.assertFalse(self.want() & set(glue.obs.cards), mode)
                self.assertNotIn(self.life.req_cards()[0], glue.obs.attachments)
                self.assertEqual({k: glue.meta(k) for k in closed}, closed, 'unknown is not observed closure')
                self.assertIsNone(glue._batch)

    def test_oversized_or_corrupt_closure_is_unknown_only_for_the_cards_it_covers(self):
        b = self.w.board
        legacy = self.glue(b, 'legacy')
        legacy.observe_cards()
        old = [b.worker_create('old done %d' % i) for i in range(600)]
        # The existing closure follows parents and, for a completed orchestrator, its promotion card.
        for mode, unknown in (('oversized', {self.life.fe}),
                              ('corrupt', {self.life.gate(), self.life.promo, self.life.orch()})):
            with self.subTest(mode=mode):
                b.cards[self.life.fe]['parents'] = [self.ancestor] + (old if mode == 'oversized' else [])
                self.rewrite_native()
                if mode == 'corrupt':
                    with contextlib.closing(sqlite3.connect(self.native)) as db, db:
                        db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES"
                                   " (?, 'commented', '{not json', 1)", (self.life.gate(),))
                native = self.glue(self.native_port(), mode)
                self.launches.clear()
                native.observe_cards()
                self.assertEqual(self.launches, [])
                self.assertFalse(unknown & set(native.obs.cards), mode)
                for card in self.want() - unknown:
                    self.assertIn(card, native.obs.cards)
                    self.assertEqual(essential(native.obs.cards[card]), essential(legacy.obs.cards[card]), card)
                self.assertEqual(
                    [(a['name'], a['sha256']) for a in native.obs.attachments[self.life.req_cards()[0]]],
                    [(a['name'], a['sha256']) for a in legacy.obs.attachments[self.life.req_cards()[0]]])

    def test_concurrent_native_commit_discards_the_read_and_reacquires_current_state(self):
        port = self.native_port()
        glue = self.glue(port, 'changed')
        acquire, changed = preworker_snapshot.acquire, []

        def committing(path, roots, take, clock, render=None):
            def change_then_render(conn, card):
                if not changed:  # one native commit while the first (prefetch) read transaction is open
                    with contextlib.closing(sqlite3.connect(path)) as db, db:
                        db.execute("UPDATE tasks SET title='changed during read' WHERE id=?", (self.life.orch(),))
                    changed.append(card)
                return render(conn, card)
            return acquire(path, roots, take, clock, change_then_render if render else None)
        with mock.patch.object(preworker_snapshot, 'acquire', side_effect=committing):
            glue.observe_cards()
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.launches, [])
        self.assertTrue(self.want() <= set(glue.obs.cards))
        self.assertEqual(glue.obs.cards[self.life.orch()]['task']['title'], 'changed during read')

    def test_snapshot_ends_with_observation_so_effect_time_reads_are_fresh_cli_reads(self):
        port = self.native_port()
        glue = self.glue(port, 'fresh')
        glue.observe_cards()
        self.assertIsNone(glue._batch)
        self.assertEqual(self.launches, [])
        gate = self.life.gate()
        self.w.board.cards[gate]['task']['status'] = 'done'  # a later native change; the snapshot is not reused
        self.assertEqual(glue._show(gate)['task']['status'], 'done')
        self.assertEqual(self.launches, [['show', gate, '--json']])
        failing = self.glue(port, 'failing')
        with mock.patch.object(main.Glue, '_observe_recovery', side_effect=RuntimeError('observation failed')), \
                self.assertRaises(RuntimeError):
            failing.observe_cards()
        self.assertIsNone(failing._batch)
        failing._show(gate)
        self.assertEqual(self.launches[-1], ['show', gate, '--json'])
        inside = []
        with mock.patch.object(main.Glue, '_observe_recovery',
                               lambda g, cards, terminal: inside.append((g._show(gate), g._show(gate)))):
            self.glue(port, 'copies').observe_cards()
        first, second = inside[0]
        self.assertEqual(first, second)
        first['task']['status'] = 'mutated by a consumer'
        self.assertNotEqual(second['task']['status'], first['task']['status'])  # independent parsed objects

    def test_custom_child_environment_keeps_the_per_card_cli_reads(self):
        self.native_port()
        port = kp.KanbanCli(main.HERMES_EXE, 'fixture', discord_chat=composition.CHAT, env={}, runner=self.runner)
        glue = self.glue(port, 'env')
        glue.observe_cards()
        self.assertTrue({('show', c) for c in self.want()} <= {tuple(x[:2]) for x in self.launches})
        self.assertTrue(self.want() <= set(glue.obs.cards))


class NativeShowEquivalenceTest(unittest.TestCase):
    """The installed CLI's own ``show --json`` / ``attachments --json`` on an isolated native board equal the
    routine read of the same database, including ordering/normalization edge cases and the bounded closure."""

    BOARD = 'clickup-native-equivalence'

    def env(self, home):
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_')}
        env.update(HERMES_HOME=home, HERMES_KANBAN_HOME=home, HERMES_DISABLE_LAZY_INSTALLS='1',
                   PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        return env

    def test_installed_cli_output_equals_native_routine_read_of_the_same_board(self):
        import shutil
        import subprocess
        import sys
        import tempfile
        home = tempfile.mkdtemp(prefix='native-equivalence-')
        self.addCleanup(shutil.rmtree, home, True)
        env = self.env(home)
        done = subprocess.run([main.HERMES_EXE, 'kanban', 'boards', 'create', self.BOARD], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', env=env, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        db_path = Path(home) / 'kanban' / 'boards' / self.BOARD / 'kanban.db'
        self.assertTrue(db_path.exists())
        stored = Path(home) / 'stored.bin'
        stored.write_bytes(b'plan bytes')
        a, c, d, e, f = 't_eeee000a', 't_eeee000c', 't_eeee000d', 't_eeee000e', 't_eeee000f'
        gates = {'clickup_gates': {'plan_revision': 1, 'plan_sha256': 'a' * 64, 'final_e2e': 't_eeee0011',
                                   'promotion': e, 'cleanup': 't_eeee0012', 'lineage': [e]}}
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            for cid, status, extra in ((a, 'running', dict(skills='["plan", "", "qa"]', model_override='')),
                                       (c, 'done', dict(created_at='1800000000')),
                                       (d, 'done', {}), (e, 'todo', {}), (f, 'done', {})):
                row = dict(dict(id=cid, title='Card %s \u2014 \u00e9' % cid, body='body', assignee='implementor',
                                status=status, created_at=1800000001, workspace_kind='scratch'), **extra)
                db.execute('INSERT INTO tasks (%s) VALUES (%s)' % (','.join(row), ','.join('?' * len(row))),
                           list(row.values()))
            db.execute('UPDATE tasks SET body=? WHERE id=?', (b'lossy \xff body', c))
            db.execute('UPDATE tasks SET current_run_id=9 WHERE id=?', (a,))  # consistent claim: no init backfill
            db.executemany('INSERT INTO task_links (parent_id, child_id) VALUES (?,?)',
                           [(f, a), (c, a), (f, e)])
            runs = [(7, a, 'implementor', 'done', 1800000300, 1800000400, 'completed', 'later id, earlier start',
                     '{"k": 1}'),
                    (5, a, 'implementor', 'done', 1800000500, None, 'blocked', '', None),
                    (9, a, 'implementor', 'running', 1800000600, None, None, None, None),
                    (3, d, 'orchestrator', 'done', 1800000100, 1800000200, 'completed', 'orchestrated',
                     json.dumps(gates))]
            db.executemany('INSERT INTO task_runs (id, task_id, profile, status, started_at, ended_at, outcome,'
                           ' summary, metadata) VALUES (?,?,?,?,?,?,?,?,?)', runs)
            db.executemany('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)',
                           [(a, 'w', 'second same second', 1800000010), (a, 'w', 'first \u2713', 1800000010),
                            (a, 'w', 'earlier', 1800000005)])
            db.executemany('INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES'
                           ' (?,?,?,?,?)', [(a, 9, 'claimed', '{"run_id": 9}', 1800000600),
                                            (a, None, 'created', None, 1800000001),
                                            (a, 5, 'blocked', '{"kind": "needs_input", "reason": "r"}', 1800000500)])
            db.executemany('INSERT INTO task_attachments (task_id, filename, stored_path, content_type, size,'
                           ' uploaded_by, created_at) VALUES (?,?,?,?,?,?,?)',
                           [(a, 'b.md', str(stored), 'text/markdown', 10, 'planner', 1800000020),
                            (a, 'a.md', str(stored), None, 10, None, 1800000020)])
        cli = kp.KanbanCli(main.HERMES_EXE, self.BOARD, discord_chat='equivalence', env=env)
        sys.path.insert(0, str(NATIVE_ROOT))
        self.addCleanup(sys.path.remove, str(NATIVE_ROOT))
        import hermes_cli.kanban_db as native
        import hermes_cli.update_lock as locks
        import hermes_cli.venv_sync as sync
        port = kp.KanbanCli(main.HERMES_EXE, self.BOARD, discord_chat='equivalence',
                            runner=lambda *a: self.fail('the routine read starts no CLI process'))
        with mock.patch.object(native, 'kanban_db_path', return_value=db_path), \
                mock.patch.object(locks, 'update_in_progress', return_value=False), \
                mock.patch.object(sync, 'completion_pending_path', return_value=Path(home) / 'absent'):
            shows, atts = port.show_batch({a, d}, lambda: 1800000700000)
            self.assertEqual(set(shows), {a, c, d, e, f})  # roots, DONE ancestors and the orchestrator's promotion
            for card in sorted(shows):
                shown, listed = cli.show(card), cli.attachments(card)
                self.assertEqual(shown.json, shows[card], card)
                self.assertEqual(listed.json, atts[card], card)
            self.assertEqual(port.show_batch({a, d}, lambda: 1800000700000), (shows, atts))  # same data throughout
        self.assertEqual([r['id'] for r in shows[a]['runs']], [7, 5, 9])
        self.assertEqual(shows[a]['latest_summary'], 'later id, earlier start')
        self.assertEqual(shows[a]['task']['skills'], ['plan', 'qa'])


if __name__ == '__main__':
    unittest.main()
