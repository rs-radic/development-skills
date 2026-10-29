"""v4 regressions through actual intake callers; native reads stay offline."""
import unittest
import test_preworker_recovery as fixture


class PreworkerSnapshotTest(unittest.TestCase):
    def case(self, incident=False):
        case = fixture.PreworkerRecoveryTest('test_zero_native_stage_wakes_with_truthful_readonly_admission')
        case.setUp()
        self.addCleanup(case.doCleanups)
        with case.stopped_stage():
            case.scans(18 if incident else 5)
        return case

    def test_changed_cached_parent_and_renewal_edge_refuse_real_effect(self):
        from unittest import mock
        for mode in ('between_scans', 'during_renewal'):
            with self.subTest(mode=mode):
                c = self.case()
                source = c.life.req_cards()[0]
                parents = ([c.b._new('Required parent %d' % i, status='done') for i in range(70)]
                           if mode == 'between_scans' else sorted(c.required_slow_cards() - {source}))
                if mode == 'between_scans':
                    c.b.cards[source]['parents'] = parents
                    c.scan_before_global(stop_at_entry=True)
                    self.assertIsNone(c.life.gate())
                blocked = c.b._new('New transitive obligation', status='blocked')
                def change():
                    c.b.cards[parents[0]]['task']['status'] = 'blocked'
                    c.b.cards[parents[0]]['parents'].append(blocked)
                if mode == 'between_scans':
                    change()
                    c.scan_before_global(stop_at_entry=True)
                else:
                    request = c.w.transport.request
                    changed = []
                    start = c.w.clock()
                    def renew(method, path, **kw):
                        out = request(method, path, **kw)
                        if method == 'GET' and path == '/v2/task/' + c.tid and c.w.clock() - start > 600000:
                            change(); changed.append(True)
                        return out
                    def slow(card, result):
                        if card in set(parents + [source]): c.w.clock.advance(45)
                        return result
                    with mock.patch.object(c.w.transport, 'request', side_effect=renew):
                        c.scan_before_global(slow, stop_at_entry=True)
                    self.assertEqual(changed, [True])
                self.assertIsNone(c.life.gate(), mode)
                c.doCleanups()

    def test_large_stable_closure_finishes_under_original_scan_bound(self):
        c = self.case()
        source = c.life.req_cards()[0]
        parents = [c.b._new('Required parent %d' % i, status='done') for i in range(130)]
        c.b.cards[source]['parents'] = parents
        all_reads = []
        for _ in range(8):
            before = len(c.b.calls)
            _, reads = c.scan_before_global(stop_at_entry=True)
            all_reads.extend(reads)
            self.assertLessEqual(len(c.b.calls) - before, 64)
            if c.life.gate(): break
        self.assertIsNotNone(c.life.gate())
        self.assertTrue(set(parents) <= set(all_reads))
        self.assertEqual(len(c.b.by_key('clickup-dp:%s:gate:' % c.tid)), 1)
        c.scans(2)
        self.assertEqual(len(c.b.by_key('clickup-dp:%s:gate:' % c.tid)), 1)

    def test_public_large_closure_current_and_changed_parent_refuses(self):
        c = self.case(incident=True)
        inc = c.incidents()[0]
        source = c.life.req_cards()[0]
        parents = [c.b._new('Required parent %d' % i, status='done') for i in range(70)]
        c.b.cards[source]['parents'] = parents
        mutations, provider = list(c.b.mutations()), c.w.cu.count()
        check = lambda: c.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0]
        self.assertEqual(check(), 0)
        blocked = c.b._new('New blocked ancestor', status='blocked')
        mutations = list(c.b.mutations())
        c.b.cards[parents[0]]['parents'].append(blocked)
        self.assertEqual(check(), 2)
        c.b.cards[blocked]['task']['status'] = 'done'
        self.assertEqual(check(), 0)
        self.assertEqual(c.b.mutations(), mutations)
        self.assertEqual(c.w.cu.count(), provider)

    def direct_port(self, c):
        from clickup_intake import kanban_port as kp, preworker_snapshot
        from pathlib import Path
        # Populate exact offline schema once; admission itself cannot write this file.
        proof = c.b._preworker_snapshot({c.life.req_cards()[0]}, lambda: True, c.w.clock)
        self.assertIsNotNone(proof)
        path = Path(c.w.dir) / 'native-read-fixture.db'
        proof.close()
        port = kp.KanbanCli('fixture/hermes.exe', 'fixture', discord_chat='fixture',
                            runner=lambda *a: self.fail('direct current proof must not launch the CLI'))
        port._preworker_snapshot = lambda roots, take, clock: preworker_snapshot.acquire(path, roots, take, clock)
        return port, path

    def test_direct_native_public_closure_uses_fixed_queries_and_readonly_file(self):
        from clickup_intake import main
        from pathlib import Path
        c = self.case(incident=True)
        source = c.life.req_cards()[0]
        c.b.cards[source]['parents'] = [c.b._new('Required native parent %d' % i, status='done') for i in range(130)]
        port, path = self.direct_port(c)
        before = path.read_bytes()
        calls = []
        original = port._preworker_snapshot
        def snapshot(roots, take, clock):
            def counted():
                calls.append(True)
                return take()
            return original(roots, counted, clock)
        port._preworker_snapshot = snapshot
        inc = c.incidents()[0]
        for _ in range(2):
            rc = main.main(['recovery-check', '--card', inc['card'], '--run', '0'], root=c.w.root,
                           port=port, clock=c.w.clock, out=__import__('io').StringIO())
            self.assertEqual(rc, 0)
        self.assertLessEqual(len(calls), 24)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(path.name + '-journal')), [])

    def test_actual_admission_refuses_expired_and_concurrently_changed_native_proof(self):
        from clickup_intake import main
        import sqlite3
        import io
        for mode in ('expired', 'changed'):
            with self.subTest(mode=mode):
                c = self.case(incident=True)
                port, path = self.direct_port(c)
                original = port._preworker_snapshot
                def snapshot(roots, take, clock):
                    proof = original(roots, take, clock)
                    self.assertIsNotNone(proof)
                    if mode == 'expired':
                        c.w.clock.advance(601)
                    else:
                        with sqlite3.connect(path) as writer:
                            writer.execute("UPDATE tasks SET status='blocked' WHERE id=?", (c.life.req_cards()[0],))
                    return proof
                port._preworker_snapshot = snapshot
                inc = c.incidents()[0]
                rc = main.main(['recovery-check', '--card', inc['card'], '--run', '0'], root=c.w.root,
                               port=port, clock=c.w.clock, out=io.StringIO())
                self.assertEqual(rc, 2)
                c.doCleanups()

    def test_native_snapshot_inflight_commit_is_not_a_mixed_current_proof(self):
        from clickup_intake import preworker_snapshot
        import sqlite3
        c = self.case()
        port, path = self.direct_port(c)
        with sqlite3.connect(path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
        reads = []
        def take():
            reads.append(True)
            if len(reads) == 4:  # tasks selected, links and remaining rows not yet read
                with sqlite3.connect(path) as writer:
                    writer.execute("UPDATE tasks SET status='blocked' WHERE id=?", (c.life.req_cards()[0],))
            return True
        self.assertIsNone(preworker_snapshot.acquire(path, {c.life.req_cards()[0]}, take, c.w.clock))
        self.assertEqual(len(reads), 9)

    def test_direct_native_stage_effect_rechecks_proof_after_intervening_change(self):
        from clickup_intake import main
        from unittest import mock
        import sqlite3
        import io
        for changed in (False, True):
            with self.subTest(changed=changed):
                c = self.case()
                source = c.life.req_cards()[0]
                c.b.cards[source]['parents'] = [c.b._new('Native parent %d' % i, status='done') for i in range(130)]
                port, path = self.direct_port(c)
                for name in ('create', 'comment', 'attach', 'unblock', 'complete', 'archive', 'subscribe',
                             'subscriptions', 'show', 'attachments'):
                    setattr(port, name, getattr(c.b, name))
                execute = main.Glue.execute
                intervened = []
                def before_effect(glue, *args, **kw):
                    if changed and not intervened:
                        with sqlite3.connect(path) as writer:
                            writer.execute("UPDATE tasks SET status='blocked' WHERE id=?", (source,))
                        intervened.append(True)
                    return execute(glue, *args, **kw)
                class Stop(BaseException): pass
                c.w.clock.advance(301)
                with mock.patch.object(main.Glue, 'execute', new=before_effect), \
                     mock.patch.object(main.Glue, 'observe_cards', side_effect=Stop), self.assertRaises(Stop):
                    main.main(['run'], root=c.w.root, transport=c.w.transport, port=port, clock=c.w.clock,
                              opener=c.w.opener, out=io.StringIO())
                if changed:
                    self.assertEqual(intervened, [True])
                    self.assertIsNone(c.life.gate())
                else:
                    self.assertIsNotNone(c.life.gate())
                c.doCleanups()

    def test_installed_adapter_refuses_custody_and_pending_update_before_native_read(self):
        from clickup_intake import main, kanban_port as kp
        from unittest import mock
        from pathlib import Path
        import sys
        root = str(Path(main.HERMES_EXE).parents[2])
        sys.path.insert(0, root)
        try:
            import hermes_cli.update_lock as locks
            import hermes_cli.venv_sync as sync
            c = self.case()
            marker = Path(c.w.dir) / 'pending-update'
            marker.write_text('fixture', encoding='utf-8')
            port = kp.KanbanCli(main.HERMES_EXE, 'fixture', discord_chat='fixture')
            for held in (True, False):
                with mock.patch.object(locks, 'update_in_progress', return_value=held), \
                     mock.patch.object(sync, 'completion_pending_path', return_value=marker), \
                     mock.patch('clickup_intake.preworker_snapshot.acquire', side_effect=AssertionError('must not read')):
                    self.assertIsNone(port._preworker_snapshot({'t_deadbeef'}, lambda: True, c.w.clock))
        finally:
            sys.path.remove(root)

    def test_installed_adapter_on_native_schema_and_bound_fixture_database(self):
        from clickup_intake import main, kanban_port as kp
        from unittest import mock
        from pathlib import Path
        import ast
        import sqlite3
        import sys
        import io
        c = self.case(incident=True)
        c.b.cards[c.life.req_cards()[0]]['parents'] = [c.b._new('Parent %d' % i, status='done') for i in range(70)]
        _, old = self.direct_port(c)
        native_root = Path(main.HERMES_EXE).parents[2]
        module = ast.parse((native_root / 'hermes_cli/kanban_db.py').read_text(encoding='utf-8'))
        schema = next(ast.literal_eval(n.value) for n in module.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_SQL' for t in n.targets))
        path = Path(c.w.dir) / 'native-schema.db'
        with sqlite3.connect(path) as out, sqlite3.connect(old) as src:
            out.executescript(schema)
            src.row_factory = sqlite3.Row
            for table in ('tasks','task_links','task_runs','task_comments','task_events','task_attachments'):
                for row in src.execute('SELECT * FROM ' + table):
                    values = dict(row)
                    if table == 'tasks':
                        # Creation time of this NEW offline schema fixture row; not a historical claim/run.
                        values['created_at'] = c.w.clock() // 1000
                    if table in ('task_comments','task_events','task_attachments'):
                        values.pop('id')
                    if table == 'task_attachments' and values['created_at'] is None:
                        values['created_at'] = c.w.clock() // 1000  # new fixture attachment-row creation
                    keys = list(values)
                    out.execute('INSERT INTO ' + table + ' (' + ','.join(keys) + ') VALUES (' +
                                ','.join('?' for _ in keys) + ')', [values[k] for k in keys])
        sys.path.insert(0, str(native_root))
        try:
            import hermes_cli.kanban_db as native
            import hermes_cli.update_lock as locks
            import hermes_cli.venv_sync as sync
            port = kp.KanbanCli(main.HERMES_EXE, 'fixture', discord_chat='fixture',
                                runner=lambda *a: self.fail('no CLI process'))
            before = path.read_bytes()
            inc = c.incidents()[0]
            with mock.patch.object(native, 'kanban_db_path', return_value=path) as resolver, \
                 mock.patch.object(locks, 'update_in_progress', return_value=False), \
                 mock.patch.object(sync, 'completion_pending_path', return_value=Path(c.w.dir) / 'absent'):
                rc = main.main(['recovery-check','--card',inc['card'],'--run','0'], root=c.w.root,
                               port=port, clock=c.w.clock, out=io.StringIO())
                self.assertEqual(rc, 0)
                resolver.assert_called_once_with(board='fixture')
            self.assertEqual(path.read_bytes(), before)
        finally:
            sys.path.remove(str(native_root))

    def test_completed_current_reviewer_is_required_at_actual_stage_effect(self):
        for mode in ('completed', 'changes_requested', 'wrong_hash', 'wrong_revision',
                     'wrong_reviewer', 'missing_attachment'):
            with self.subTest(mode=mode):
                c = self.case()
                source = c.b.cards[c.life.req_cards()[0]]
                request = next(r for r in reversed(source['runs']) if r['outcome'] == 'review_requested')
                if mode == 'changes_requested':
                    source['runs'][-1]['outcome'] = mode
                elif mode == 'wrong_hash':
                    request['metadata']['plan_sha256'] = 'a' * 64
                elif mode == 'wrong_revision':
                    request['metadata']['plan_revision'] = 2
                elif mode == 'wrong_reviewer':
                    source['runs'][-1]['profile'] = 'planner'
                elif mode == 'missing_attachment':
                    source['atts'] = []
                c.scan_before_global(stop_at_entry=True)
                if mode == 'completed':
                    self.assertIsNotNone(c.life.gate())
                    self.assertEqual(len(c.b.by_key('clickup-dp:%s:gate:' % c.tid)), 1)
                else:
                    self.assertIsNone(c.life.gate(), mode)
                c.doCleanups()
