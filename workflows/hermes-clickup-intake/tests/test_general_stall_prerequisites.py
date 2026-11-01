"""QUALITY-R4-1: failed transitions retain native prerequisite authority."""
import copy
import json
import unittest
from unittest import mock

import test_general_stall_linked_owner as linked
import test_progress_projection as projection_fixture
import test_recovery as recovery
from clickup_intake import main, progress, kanban_port as kp


class FailurePrerequisiteProjectionTest(unittest.TestCase):
    def setUp(self):
        projection_fixture.ProgressProjectionTest.setUp(self)
        self.task['hold_reason'] = 'outbox:failed'
        self.state['failure'] = dict(first_send_ms=self.now - 1000,
                                    idem_key='failed', payload_sha256='a' * 64)

    project = projection_fixture.ProgressProjectionTest.project

    def parent(self, name, status='done', parents=()):
        self.shows[name] = dict(task=dict(id=name, status=status), parents=list(parents), runs=[])
        return self.shows[name]

    def test_required_parents_recurse_and_active_ancestor_waits(self):
        self.source['parents'] = ['parent']
        self.parent('parent', parents=('ancestor',))
        self.parent('ancestor', 'running')
        self.assertEqual(self.project(), ('wait', None))

    def test_missing_required_ancestor_is_unknown(self):
        self.source['parents'] = ['parent']
        self.parent('parent', parents=('absent',))
        self.assertEqual(self.project(), ('unknown', None))

    def test_known_source_requires_observed_parent_list_and_identity(self):
        for field in ('parents', 'identity'):
            with self.subTest(field=field):
                source = copy.deepcopy(self.source)
                if field == 'parents':
                    self.source.pop('parents')
                else:
                    self.source['task']['id'] = 'other'
                self.assertEqual(self.project(), ('unknown', None))
                self.source.clear()
                self.source.update(source)

    def test_recorded_question_and_its_ancestors_remain_authoritative(self):
        self.state['blockers'] = [dict(question_card='question')]
        self.parent('question', parents=('ancestor',))
        self.parent('ancestor', 'running')
        self.assertEqual(self.project(), ('wait', None))
        del self.shows['ancestor']
        self.assertEqual(self.project(), ('unknown', None))

    def test_all_guarded_fence_states_preserve_recursive_observation(self):
        for state in ('open', 'releasing', 'abandoned', 'refused'):
            with self.subTest(state=state):
                self.state['fences'] = [dict(fence_card='fence', state=state)]
                self.parent('fence', parents=('ancestor',))
                self.parent('ancestor', 'running')
                self.assertEqual(self.project(), ('wait', None))
                del self.shows['ancestor']
                self.assertEqual(self.project(), ('unknown', None))

    def test_completed_and_archived_closure_allows_failure_diagnosis(self):
        self.source['parents'] = ['parent']
        self.parent('parent', 'archived', parents=('ancestor',))
        self.parent('ancestor')
        self.assertEqual(self.project()[0], 'actionable')

    def test_no_native_source_is_not_invented_for_a_definite_failure(self):
        self.state['requests'] = []
        self.shows.clear()
        kind, found = self.project()
        self.assertEqual(kind, 'actionable')
        self.assertEqual(found['run_id'], 0)
        self.assertEqual(found['card'], 'progress-t_fixture-request')
        self.assertEqual(self.shows, {})

    def test_no_native_source_still_respects_recorded_obligations(self):
        self.state['requests'] = []
        self.state['blockers'] = [dict(question_card='question')]
        self.parent('question', 'running')
        self.assertEqual(self.project(), ('wait', None))
        del self.shows['question']
        self.assertEqual(self.project(), ('unknown', None))


class FailurePrerequisiteWorkflowTest(unittest.TestCase):
    setUp = linked.LinkedOwnerTest.setUp
    scans = linked.LinkedOwnerTest.scans
    invalid_plan = linked.LinkedOwnerTest.invalid_plan
    carriers = linked.LinkedOwnerTest.carriers
    notices = linked.LinkedOwnerTest.notices
    check = linked.LinkedOwnerTest.check
    failed_successor = linked.LinkedOwnerTest.failed_successor
    assert_retained = linked.LinkedOwnerTest.assert_retained
    assert_delivered = linked.LinkedOwnerTest.assert_delivered
    prepare = linked.LinkedOwnerTest.prepare

    def parent(self, done=False, transitive=False):
        source = self.life.req_cards()[-1]
        ancestor = self.b.worker_create('Existing upstream prerequisite', assignee='specialist')
        self.b.claim(ancestor, 'specialist')
        if done:
            self.b.finish(ancestor, 'specialist', {})
        parent = ancestor
        if transitive:
            parent = self.b.worker_create('Settled direct dependency', parents=(ancestor,), assignee='specialist')
            self.b.claim(parent, 'specialist')
            self.b.finish(parent, 'specialist', {})
        self.b.cards[source]['parents'].append(parent)
        return source, parent, ancestor

    def retained(self, inc, failed):
        self.assert_retained(inc)
        self.assertEqual(self.w.q('SELECT * FROM outbox WHERE idem_key=?', failed['idem_key']), [failed])
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'outbox:' + failed['idem_key'])
        self.assertEqual(self.w.q("SELECT * FROM meta WHERE key LIKE 'diagnostic_history:%' ORDER BY key"),
                         self.saved_history)

    def test_actual_admission_rejects_running_transitive_parent(self):
        inc, failed = self.prepare()
        source, parent, ancestor = self.parent(transitive=True)
        self.assertIn(parent, self.b.cards[source]['parents'])
        self.assertIn(ancestor, self.b.cards[parent]['parents'])
        self.assertTrue(kp.open_run_ids(self.b.cards[ancestor]))
        self.assertEqual(self.check(inc), 3)
        self.retained(inc, failed)

    def test_actual_admission_missing_ancestor_is_unavailable(self):
        inc, failed = self.prepare()
        source, parent, ancestor = self.parent(done=True, transitive=True)
        factory = self.b._preworker_snapshot
        hits = []
        def missing(*args):
            proof = factory(*args)
            if proof:
                self.assertIn(ancestor, proof.shows[parent]['parents'])
                self.assertIn(ancestor, proof.shows)
                proof.shows.pop(ancestor)
                hits.append(ancestor)
            return proof
        with mock.patch.object(self.b, '_preworker_snapshot', side_effect=missing):
            self.assertEqual(self.check(inc), 2)
        self.assertTrue(hits)
        self.retained(inc, failed)

    def test_active_prerequisite_does_not_spend_the_next_wake(self):
        inc, failed = self.prepare()
        self.parent()
        carriers = copy.deepcopy([self.b.cards[c] for c in self.carriers()])
        self.w.clock.now_ms = inc['detected_ms'] + 181 * 60000
        self.scans(12)
        self.assertEqual([self.b.cards[c] for c in self.carriers()], carriers)
        self.assertEqual(self.notices(), [])
        self.retained(inc, failed)

    def test_active_prerequisite_suppresses_deadline_notice(self):
        inc, failed = self.prepare()
        self.parent()
        comments = self.w.bot_comments(self.tid)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(12)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.w.bot_comments(self.tid), comments)
        self.retained(inc, failed)

    def test_prequeued_notice_rechecks_prerequisite_before_send(self):
        inc, failed = self.prepare()
        self.w.clock.now_ms = inc['abs_ms'] + 1
        clickup = main.Glue.clickup
        def suspend(glue, item):
            return False if json.loads(item['body']).get('recovery_notice') else clickup(glue, item)
        with mock.patch.object(main.Glue, 'clickup', suspend):
            self.scans(5)
        [before] = self.notices()
        self.assertEqual((before['state'], before['attempts'], before['first_send_ms']), ('pending', 0, None))
        self.parent()
        comments = self.w.bot_comments(self.tid)
        self.scans(8)
        [after] = self.notices()
        self.assertEqual((after['attempts'], after['first_send_ms']), (0, None))
        self.assertNotEqual(after['state'], 'done')
        self.assertEqual(self.w.bot_comments(self.tid), comments)
        self.retained(inc, failed)

    def test_completed_prerequisite_preserves_original_deadline_delivery(self):
        inc, failed = self.prepare()
        self.parent(done=True, transitive=True)
        self.assertEqual(self.check(inc), 0)
        self.w.clock.now_ms = inc['abs_ms'] + 1
        self.scans(12)
        self.assert_delivered()
        self.retained(inc, failed)

    def test_actual_admission_checks_recorded_question_and_fence(self):
        inc, failed = self.prepare()
        source = self.life.req_cards()[-1]
        question = self.b.worker_create('Recorded upstream question', assignee='specialist')
        self.b.claim(question, 'specialist')
        conn = recovery.store_conn(self.w)
        try:
            conn.execute("INSERT INTO blockers(task, question_card, card, state) VALUES (?, ?, ?, 'internal')",
                         (self.tid, question, source))
            self.assertEqual(self.check(inc), 3)
            conn.execute('DELETE FROM blockers WHERE question_card=?', (question,))
            conn.execute("INSERT INTO fences(task, card, fence_card, kind, state) VALUES (?, ?, ?, 'fence', 'open')",
                         (self.tid, source, question))
            self.assertEqual(self.check(inc), 3)
        finally:
            conn.close()
        self.retained(inc, failed)
