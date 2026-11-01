"""SPEC-R2 regressions: real intake callers with inert provider/native ports."""
import contextlib
import json
import unittest
from unittest import mock

from clickup_intake import main
from clickup_intake.contracts import Outcome
import test_dynamic_dependencies as fixture
import test_s4_composition as comp
import test_recovery as recovery


@contextlib.contextmanager
def world(kind='native'):
    cls = fixture.DynamicDependenciesTest if kind == 'native' else fixture.DynamicDeliveryRecoveryTest
    case = cls('runTest')
    original = comp.World.__init__
    def short_world(w, *args, **kwargs):
        original(w, *args, **kwargs)
        w.root = w.dir
        w.runtime = __import__('os').path.join(w.root, 'integrations', 'clickup-dedicated-panel')
        w.cfg['runtime_dir'] = w.runtime
        w.write_config()
    with mock.patch.object(comp.World, '__init__', short_world):
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()


def queued(t):
    original = main.Glue.clickup
    def hold(g, row):
        if ':bq:' in row['idem_key'] and row['kind'] == 'cu_comment':
            return False
        return original(g, row)
    with mock.patch.object(main.Glue, 'clickup', hold):
        t.scans(3)
        b = t.w.q('SELECT * FROM blockers')[0]
        t.b.claim(b['request_card'], 'planner')
        t.b.park(b['request_card'], comp.out('technical_questions', b['relay_ref'],
            t.b.cards[t.question]['task']['body'], blocker=b['id'], boundary='security'))
        t.scans(2)
    b = t.w.q('SELECT * FROM blockers')[0]
    t.assertTrue(b['question_key'])
    t.assertEqual(row(t, b['question_key'])['state'], 'pending')
    return b


def row(t, key):
    return t.w.q('SELECT * FROM outbox WHERE idem_key=?', key)[0]


def pre_send_mirror(t, b):
    original = main.clickup_effects.execute
    reached = []
    def crash(*args, **kwargs):
        if args[3] == b['question_key'] + '#cu':
            reached.append(args[3])
            raise comp.Crash('review-before-provider')
        return original(*args, **kwargs)
    with mock.patch.object(main.clickup_effects, 'execute', crash):
        t.scans(1)  # World retries a simulated process crash before returning.
    t.assertTrue(reached, 'pre-send mirror boundary was not reached')
    mirror = row(t, b['question_key'] + '#cu')
    t.assertEqual((mirror['state'], mirror['attempts']), ('pending', 0))
    return mirror


class DynamicDependencyReviewTest(unittest.TestCase):
    def handoff(self, t, queue_notice=True):
        parent, ancestor = self.ancestors(t)
        t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
        t.scans(6)
        [incident] = t.w.q('SELECT * FROM recoveries')
        self.assertTrue(t.wakes(), 'the original blockage must reach a capable AI')
        t.b.finish(t.question, 'coordinator')
        t.b.unblock(t.repair)
        t.scans(2)
        [handed] = t.w.q('SELECT * FROM recoveries')
        self.assertEqual(handed['state'], 'handed_off')
        self.assertEqual(t.b.cards[t.repair]['task']['status'], 'ready')
        self.same_incident(incident, handed)
        if not queue_notice:
            return incident, parent, ancestor, None
        t.w.clock.now_ms = incident['abs_ms'] + 1000
        original = main.Glue.clickup
        def hold(g, r):
            if json.loads(r['body']).get('recovery_notice'):
                return False
            return original(g, r)
        with mock.patch.object(main.Glue, 'clickup', hold):
            t.scans(2)
        [notice] = t.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
        self.assertEqual((notice['state'], notice['attempts']), ('pending', 0))
        [meta] = t.w.q('SELECT value FROM meta WHERE key=?', 'diagnostic_wait:' + str(incident['id']))
        wait = json.loads(meta['value'])
        self.assertTrue(wait['takeover'])
        self.assertEqual(wait['state'], 'publishing')
        for key, value in (('task', t.tid), ('card', t.repair), ('run', incident['run_id']),
                           ('approval', incident['approval_id'])):
            self.assertEqual(wait['question'][key], value)
        return incident, parent, ancestor, notice

    def same_incident(self, before, after):
        for field in ('id', 'attempts', 'detected_ms', 'abs_ms'):
            self.assertEqual(after[field], before[field])

    def test_handoff_running_ancestor_blocks_takeover_selection(self):
        with world('delivery') as t:
            incident, parent, ancestor, _ = self.handoff(t, queue_notice=False)
            t.b.claim(ancestor, 'implementor')
            self.assertEqual(t.b.cards[parent]['task']['status'], 'done')
            t.w.clock.now_ms = incident['abs_ms'] + 1000
            t.scans(2)
            self.assertEqual(t.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'"), [])
            self.assertNotEqual(t.w.main('recovery-check', '--card', t.repair,
                                        '--run', str(incident['run_id']))[0], 0)
            self.same_incident(incident, t.w.q('SELECT * FROM recoveries')[0])

    def test_handoff_completed_ancestor_delivers_takeover_once(self):
        with world('delivery') as t:
            incident, parent, ancestor, before = self.handoff(t)
            self.assertEqual(t.b.cards[parent]['task']['status'], 'done')
            self.assertEqual(t.b.cards[ancestor]['task']['status'], 'done')
            t.scans(3)
            after = row(t, before['idem_key'])
            mirror = row(t, before['idem_key'] + '#cu')
            self.assertEqual((after['state'], after['attempts']), ('done', 1))
            self.assertEqual((mirror['state'], mirror['attempts']), ('done', 1))
            self.assertEqual(len([c for c in t.w.bot_comments(t.tid) if c['id'] == after['result_ref']]), 1)
            self.same_incident(incident, t.w.q('SELECT * FROM recoveries')[0])
            t.scans(2)
            self.assertEqual(row(t, before['idem_key']), after)

    def test_handoff_running_ancestor_defers_queued_takeover_until_completed(self):
        self.handoff_notice_obligation('running')

    def test_handoff_missing_ancestor_defers_same_takeover_until_restored(self):
        self.handoff_notice_obligation('missing')

    def handoff_notice_obligation(self, mode):
        with world('delivery') as t:
            incident, parent, ancestor, before = self.handoff(t)
            saved = t.b.cards[ancestor]
            if mode == 'running':
                t.b.claim(ancestor, 'implementor')
            else:
                del t.b.cards[ancestor]
            comments = t.w.bot_comments(t.tid)
            gates, proofs = [], []
            original = main.Glue.recovery_notice_gate
            def gate(g, *args, **kwargs):
                result = original(g, *args, **kwargs)
                reader = g.preworker_reader(t.tid)
                native = getattr(reader, 'dependencies', None)
                if native:
                    proof = native[1]
                    current = proof.shows.get(ancestor)
                    proofs.append((proof.valid(), reader.remaining,
                                   (current or {}).get('task', {}).get('status'),
                                   main.kp.open_run_ids(current) if current else []))
                gates.append(result)
                return result
            with mock.patch.object(main.Glue, 'recovery_notice_gate', gate):
                t.scans(2)
            self.assertTrue(gates, 'the queued notice must reach its final effect gate')
            if mode == 'running':
                self.assertTrue(any(valid and remaining > 0 and status == 'running' and runs
                                    for valid, remaining, status, runs in proofs), proofs)
            self.assertEqual(set(gates), {'defer'})
            self.assertEqual(row(t, before['idem_key']), before)
            self.assertEqual(t.w.bot_comments(t.tid), comments)
            self.assertNotEqual(t.w.main('recovery-check', '--card', t.repair,
                                        '--run', str(incident['run_id']))[0], 0)
            self.same_incident(incident, t.w.q('SELECT * FROM recoveries')[0])
            if mode == 'running':
                t.b.finish(ancestor, 'implementor')
            else:
                t.b.cards[ancestor] = saved
            t.scans(4)
            after = row(t, before['idem_key'])
            self.assertEqual((after['state'], after['attempts']), ('done', 1))
            self.assertEqual(len([c for c in t.w.bot_comments(t.tid) if c['id'] == after['result_ref']]), 1)
            self.same_incident(incident, t.w.q('SELECT * FROM recoveries')[0])
            t.scans(2)
            self.assertEqual(row(t, before['idem_key']), after)

    def test_due_retry_mirror_cancels_without_refunding_attempt(self):
        with world() as t:
            b = queued(t)
            t.w.cu.inject('POST', '/v2/task/%s/comment' % t.tid, Outcome.DEFINITE_FAIL, status=503)
            t.scans(1)
            before = row(t, b['question_key'] + '#cu')
            self.assertEqual((before['state'], before['attempts']), ('retry_wait', 1))
            t.b.cards[t.quality]['parents'].remove(t.repair)
            t.w.clock.now_ms = before['next_attempt_ms']
            t.scans(2)
            after = row(t, b['question_key'] + '#cu')
            self.assertEqual(after['state'], 'cancelled_stale')
            for field in ('attempts', 'first_send_ms', 'next_attempt_ms'):
                self.assertEqual(after[field], before[field])
            self.assertFalse(any(t.b.cards[t.question]['task']['body'] in c['comment_text']
                                 for c in t.w.bot_comments(t.tid)))

    def test_possibly_applied_mirror_reconciles_after_detachment(self):
        with world() as t:
            b = queued(t)
            original = main.Glue.update
            class AfterApplied(BaseException):
                pass
            def update(g, key, **fields):
                if key == b['question_key'] and fields.get('state') == 'done':
                    raise AfterApplied()
                return original(g, key, **fields)
            with mock.patch.object(main.Glue, 'update', update):
                with self.assertRaises(AfterApplied):
                    t.scans(1)
            applied = row(t, b['question_key'] + '#cu')
            self.assertEqual((applied['state'], applied['attempts']), ('done', 1))
            c = recovery.store_conn(t.w)
            c.execute("UPDATE outbox SET state='sending', result_ref=NULL WHERE idem_key=?",
                      (b['question_key'] + '#cu',))
            c.close()  # provider commit before durable mirror acknowledgement
            t.b.cards[t.quality]['parents'].remove(t.repair)
            t.scans(3)
            after = row(t, b['question_key'])
            self.assertEqual((after['state'], after['result_ref'], after['attempts']),
                             ('done', applied['result_ref'], 1))
            self.assertEqual(len([c for c in t.w.bot_comments(t.tid) if c['id'] == applied['result_ref']]), 1)

    def test_existing_mirror_delivers_when_current(self):
        with world() as t:
            b = queued(t)
            pre_send_mirror(t, b)
            t.scans(3)
            after = row(t, b['question_key'])
            self.assertEqual((after['state'], after['attempts']), ('done', 1))
            self.assertEqual(len([c for c in t.w.bot_comments(t.tid) if c['id'] == after['result_ref']]), 1)

    def test_unknown_existing_mirror_defers_then_delivers_same_key(self):
        self.unknown_question(mirror=True)

    def test_exhausted_closure_defers_then_delivers_same_key(self):
        self.unknown_question(exhausted=True)

    def test_missing_native_node_defers_original_question_until_restored(self):
        with world() as t:
            b = queued(t)
            original = t.b.cards.pop(t.repair)
            before = row(t, b['question_key'])
            t.scans(2)
            self.assertEqual(row(t, b['question_key']), before)
            t.b.cards[t.repair] = original
            t.scans(4)
            after = row(t, b['question_key'])
            self.assertEqual((after['state'], after['attempts']), ('done', 1))

    def test_public_admission_rechecks_recursive_prerequisite(self):
        with world('delivery') as t:
            parent, ancestor = self.ancestors(t)
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            t.scans(6)
            [inc] = t.w.q('SELECT * FROM recoveries')
            args = ('recovery-check', '--card', t.repair, '--run', str(inc['run_id']))
            self.assertEqual(t.w.main(*args)[0], 0)
            t.b.claim(ancestor, 'implementor')
            self.assertEqual(t.w.main(*args)[0], 2)
            t.b.finish(ancestor, 'implementor')
            self.assertEqual(t.w.main(*args)[0], 0)
            del t.b.cards[ancestor]
            self.assertEqual(t.w.main(*args)[0], 2)

    def test_effect_gate_rechecks_recursive_prerequisite_and_resumes(self):
        with world('delivery') as t:
            parent, ancestor = self.ancestors(t)
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            original = main.Glue.recovery_gate
            verdicts = []
            def gate(g, r, p):
                if p['params'].get('class') == 'diagnostic':
                    if not verdicts:
                        t.b.claim(ancestor, 'implementor')
                    result = original(g, r, p)
                    verdicts.append(result)
                    return result
                return original(g, r, p)
            with mock.patch.object(main.Glue, 'recovery_gate', gate):
                t.scans(3)
            self.assertTrue(verdicts)
            self.assertEqual(set(verdicts), {'defer'})
            before = t.w.q('SELECT * FROM recoveries')[0]
            self.assertTrue(all(r['state'] != 'done' for r in
                t.w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:rcv:%:wake'")))
            t.b.finish(ancestor, 'implementor')
            t.scans(3)
            after = t.w.q('SELECT * FROM recoveries')[0]
            self.assertTrue(any(r['state'] == 'done' for r in
                t.w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:rcv:%:wake'")))
            for field in ('id', 'detected_ms', 'abs_ms'):
                self.assertEqual(before[field], after[field])

    def test_question_ancestor_is_not_exempted(self):
        with world('delivery') as t:
            ancestor = t.b.worker_create('Question prerequisite', assignee='implementor')
            t.b.claim(ancestor, 'implementor')
            t.b.cards[t.question]['parents'] = [ancestor]
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            t.scans(6)
            self.assertEqual(t.wakes(), [])

    def test_exhausted_current_input_does_not_acquire_native_snapshot(self):
        self.exhausted_effect(stale=False)

    def test_repeated_legacy_diagnostic_reads_revalidate_parent(self):
        with world('delivery') as t:
            parent = t.b.worker_create('Current required parent', assignee='implementor')
            t.b.finish(parent, 'implementor')
            t.b.cards[t.repair]['parents'] = [parent]
            w = t.b.show(t.repair).json
            reader = main._PreworkerReads(t.b, t.w.clock)
            snap = dict(requests=[], blockers=[], fences=[], approvals=[])
            with mock.patch.object(t.b, '_preworker_snapshot', None):
                main._diagnostic_reads(snap, t.b, t.repair, w, reader)
                self.assertEqual(snap['shows'][parent]['task']['status'], 'done')
                t.b.claim(parent, 'implementor')
                main._diagnostic_reads(snap, t.b, t.repair, w, reader)
            self.assertEqual(snap['shows'][parent]['task']['status'], 'running')

    def test_notice_exhaustion_defers_without_io_then_delivers_same_row(self):
        with world('delivery') as t:
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            t.scans(6)
            before_inc = t.w.q('SELECT * FROM recoveries')[0]
            t.w.clock.now_ms = before_inc['abs_ms'] + 1000
            clickup = main.Glue.clickup
            def hold(g, r):
                if json.loads(r['body']).get('recovery_notice'):
                    return False
                return clickup(g, r)
            with mock.patch.object(main.Glue, 'clickup', hold):
                t.scans(2)
            notices = t.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")
            self.assertEqual(len(notices), 1)
            before = notices[0]
            self.assertEqual(before['state'], 'pending')
            original = main.Glue.recovery_notice_gate
            request, show, snapshot = t.w.transport.request, t.b.show, t.b._preworker_snapshot
            active, events, verdicts = [False], [], []
            def req(*args, **kwargs):
                if active[0]: events.append(('provider', args[:2]))
                return request(*args, **kwargs)
            def native(*args, **kwargs):
                if active[0]: events.append(('native', args))
                return show(*args, **kwargs)
            def snap(*args, **kwargs):
                if active[0]: events.append(('snapshot',))
                return snapshot(*args, **kwargs)
            def gate(g, iid, *args, **kwargs):
                reader = g.preworker_reader(t.tid)
                reader.remaining = 0
                g.c.execute('UPDATE tasks SET last_light_ms=? WHERE clickup_task_id=?',
                            (t.w.clock() - 301000, t.tid))
                active[0] = True
                try:
                    result = original(g, iid, *args, **kwargs)
                finally:
                    active[0] = False
                verdicts.append(result)
                return result
            with mock.patch.object(main.Glue, 'recovery_notice_gate', gate), \
                 mock.patch.object(t.w.transport, 'request', req), \
                 mock.patch.object(t.b, 'show', native), \
                 mock.patch.object(t.b, '_preworker_snapshot', snap):
                t.scans(2)
            self.assertTrue(verdicts)
            self.assertEqual(set(verdicts), {'defer'})
            self.assertEqual(events, [])
            self.assertEqual(row(t, before['idem_key']), before)
            t.scans(4)
            after = row(t, before['idem_key'])
            self.assertEqual((after['state'], after['attempts']), ('done', 1))
            self.assertTrue(after['result_ref'])
            self.assertTrue(any(c['id'] == after['result_ref'] for c in t.w.bot_comments(t.tid)))
            after_inc = t.w.q('SELECT * FROM recoveries')[0]
            for field in ('id', 'attempts', 'detected_ms', 'abs_ms'):
                self.assertEqual(after_inc[field], before_inc[field])

    def test_within_budget_renewal_charges_actual_reads(self):
        with world('delivery') as t:
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            original = main.Glue.recovery_gate
            request = t.w.transport.request
            active, seen, calls = [None], [], []
            def track(*args, **kwargs):
                if active[0] is not None:
                    calls.append((args[0], args[1], active[0].remaining))
                    self.assertGreaterEqual(active[0].remaining, 0)
                return request(*args, **kwargs)
            def gate(g, r, p):
                if p['params'].get('class') != 'diagnostic' or seen:
                    return original(g, r, p)
                reader = g.preworker_reader(t.tid)
                before = reader.remaining
                g.c.execute('UPDATE tasks SET last_light_ms=? WHERE clickup_task_id=?',
                            (t.w.clock() - 301000, t.tid))
                active[0] = reader
                try:
                    result = original(g, r, p)
                finally:
                    active[0] = None
                seen.append((before, reader.remaining, result))
                return result
            with mock.patch.object(main.Glue, 'recovery_gate', gate), \
                 mock.patch.object(t.w.transport, 'request', track):
                t.scans(6)
            self.assertTrue(seen)
            self.assertGreaterEqual(len(calls), 2)
            self.assertGreaterEqual(seen[0][0] - seen[0][1], len(calls))
            self.assertEqual(seen[0][2], 'go')
            self.assertTrue(any(r['state'] == 'done' for r in
                t.w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:rcv:%:wake'")))

    def ancestors(self, t):
        parent = t.b.worker_create('Completed direct prerequisite', assignee='implementor')
        t.b.finish(parent, 'implementor')
        ancestor = t.b.worker_create('Completed recursive prerequisite', assignee='implementor')
        t.b.finish(ancestor, 'implementor')
        t.b.cards[parent]['parents'] = [ancestor]
        t.b.cards[t.repair]['parents'].append(parent)
        return parent, ancestor

    def test_exhausted_effect_reader_performs_no_provider_or_native_reads(self):
        self.exhausted_effect(stale=True)

    def exhausted_effect(self, stale):
        with world('delivery') as t:
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            original = main.Glue.recovery_gate
            request, show, snapshot = t.w.transport.request, t.b.show, t.b._preworker_snapshot
            events, verdicts = [], []
            armed = [False]
            def track_request(*args, **kwargs):
                if armed[0]:
                    events.append(('provider', args[0], args[1]))
                return request(*args, **kwargs)
            def track_show(card):
                if armed[0]:
                    events.append(('native', card))
                return show(card)
            def track_snapshot(*args, **kwargs):
                if armed[0]:
                    events.append(('snapshot',))
                return snapshot(*args, **kwargs)
            def gate(g, r, p):
                if p['params'].get('class') != 'diagnostic':
                    return original(g, r, p)
                reader = g.preworker_reader(t.tid)
                reader.remaining = 0
                if stale:
                    g.c.execute('UPDATE tasks SET last_light_ms=? WHERE clickup_task_id=?',
                                (t.w.clock() - 301000, t.tid))
                if getattr(reader, 'dependencies', None):
                    reader.dependencies[1].close()
                armed[0] = True
                try:
                    result = original(g, r, p)
                finally:
                    armed[0] = False
                verdicts.append(result)
                self.assertEqual(reader.remaining, 0)
                return result
            with mock.patch.object(main.Glue, 'recovery_gate', gate), \
                 mock.patch.object(t.w.transport, 'request', track_request), \
                 mock.patch.object(t.b, 'show', track_show), \
                 mock.patch.object(t.b, '_preworker_snapshot', track_snapshot):
                t.scans(3)
            self.assertTrue(verdicts, 'actual effect gate not reached')
            self.assertEqual(set(verdicts), {'defer'})
            self.assertEqual(events, [])
            wakes = t.w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:rcv:%:wake'")
            self.assertTrue(wakes)
            self.assertTrue(all(r['state'] != 'done' for r in wakes))

    def test_running_recursive_prerequisite_blocks_undelivered_question_wake(self):
        with world('delivery') as t:
            parent = t.b.worker_create('Completed direct prerequisite', assignee='implementor')
            t.b.finish(parent, 'implementor')
            ancestor = t.b.worker_create('Running recursive prerequisite', assignee='implementor')
            t.b.claim(ancestor, 'implementor')
            t.b.cards[parent]['parents'] = [ancestor]
            t.b.cards[t.repair]['parents'].append(parent)
            t.b.cards[t.question]['task']['assignee'] = 'wrong-route'
            t.scans(6)
            self.assertTrue(t.b.cards[ancestor]['runs'])
            self.assertEqual(t.b.cards[ancestor]['task']['status'], 'running')
            self.assertEqual(t.wakes(), [])
            self.assertEqual(t.w.q('SELECT * FROM recoveries'), [])

    def test_unknown_closure_preserves_original_unsent_question(self):
        self.unknown_question()

    def unknown_question(self, mirror=False, exhausted=False):
        with world() as t:
            b = queued(t)
            if mirror:
                pre_send_mirror(t, b)
            before = row(t, b['question_key'])
            gates = []
            original = main.Glue.send_gate
            def unavailable(g, r, *args, **kwargs):
                if r['idem_key'] != b['question_key']:
                    return original(g, r, *args, **kwargs)
                reader = g.preworker_reader(t.tid)
                if getattr(reader, 'dependencies', None):
                    reader.dependencies[1].close()
                if exhausted:
                    reader.remaining = 0
                with mock.patch.object(t.b, '_preworker_snapshot', return_value=None):
                    result = original(g, r, *args, **kwargs)
                gates.append(result)
                return result
            with mock.patch.object(main.Glue, 'send_gate', unavailable):
                t.scans(2)
            self.assertTrue(gates)
            self.assertEqual(set(gates), {'defer'})
            during = row(t, b['question_key'])
            self.assertEqual(during, before)
            t.scans(4)
            after = row(t, b['question_key'])
            self.assertEqual(after['state'], 'done')
            self.assertEqual(after['idem_key'], before['idem_key'])
            self.assertEqual(after['attempts'], 1)
            self.assertEqual(len([c for c in t.w.bot_comments(t.tid) if c['id'] == after['result_ref']]), 1)
            t.scans(2)
            self.assertEqual(row(t, b['question_key']), after)

    def test_existing_unsent_mirror_honors_confirmed_detachment(self):
        with world() as t:
            b = queued(t)
            before = pre_send_mirror(t, b)
            t.b.cards[t.quality]['parents'].remove(t.repair)
            gates = []
            original = main.Glue.send_gate
            def gate(g, r, *args, **kwargs):
                verdict = original(g, r, *args, **kwargs)
                if r['idem_key'] == b['question_key']:
                    gates.append(verdict)
                return verdict
            with mock.patch.object(main.Glue, 'send_gate', gate):
                t.scans(3)
            self.assertIn('cancel', gates)
            self.assertEqual(row(t, b['question_key'])['state'], 'cancelled_stale')
            after = row(t, b['question_key'] + '#cu')
            self.assertEqual(after['attempts'], before['attempts'])
            self.assertFalse(any(t.b.cards[t.question]['task']['body'] in c['comment_text']
                                 for c in t.w.bot_comments(t.tid)))
