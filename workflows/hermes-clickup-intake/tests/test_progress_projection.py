"""Pure structural projection contracts; no live services or original test mutation."""
import copy
import unittest
from clickup_intake import progress


class ProgressProjectionTest(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000000
        self.task = dict(clickup_task_id='t_fixture', phase='in_review', terminal=0, hold_reason=None,
            input_gen=3, classified_gen=3, scope_gen=2, published_rev=None, published_sha256=None,
            published_accounts_gen=None, quiet_until_ms=0, full_cursor=None, full_started_ms=self.now,
            last_light_ms=self.now, last_full_ms=self.now)
        self.state = dict(requests=['t_source'], blockers=[], fences=[], transitions=[], relays=[], approvals=[])
        self.source = dict(task=dict(id='t_source', status='done', current_run_id=None, worker_pid=None,
                                    created_at=1799999000), parents=[], children=[], events=[], comments=[],
            runs=[dict(id=12, profile='plan-reviewer', status='done', outcome='completed',
                       ended_at=1799999990, worker_pid=None, metadata={})])
        self.shows = {'t_source': self.source}
        self.atts = {'t_source': []}

    def project(self):
        return progress.projection(self.task, self.state, self.shows, self.atts, self.now, self.now)

    def test_settled_missing_publication_is_structural_not_an_error_regex(self):
        kind, found = self.project()
        self.assertEqual(kind, 'actionable')
        self.assertEqual(found['run_id'], 12)
        self.assertNotIn('nopass', found['reason'])

    def test_explicit_no_worker_run_is_distinct_from_unreadable_runs(self):
        self.source['task']['status'] = 'blocked'
        self.source['runs'] = []
        kind, found = self.project()
        self.assertEqual(kind, 'actionable')
        self.assertEqual(found['run_id'], 0)
        self.assertEqual(found['event_created_s'], self.source['task']['created_at'])
        for missing in ('runs', 'events'):
            with self.subTest(missing=missing):
                snapshot = self.source.pop(missing)
                self.assertEqual(self.project()[0], 'unknown')
                self.source[missing] = snapshot

    def test_healthy_queue_reviewer_and_explicit_wait_are_quiet(self):
        for status in ('ready', 'todo', 'running', 'review', 'scheduled', 'archived'):
            with self.subTest(status=status):
                self.source['task']['status'] = status
                self.assertNotEqual(self.project()[0], 'actionable')

    def test_terminal_held_new_input_and_open_human_obligation_are_quiet(self):
        for field, value in (('terminal', 1), ('hold_reason', 'operator'), ('classified_gen', 2)):
            old = self.task[field]
            self.task[field] = value
            self.assertNotEqual(self.project()[0], 'actionable')
            self.task[field] = old
        self.state['blockers'] = [dict(question_card='t_question')]
        self.shows['t_question'] = dict(task=dict(id='t_question', status='blocked'), parents=[], runs=[])
        self.assertEqual(self.project()[0], 'wait')
        del self.shows['t_question']
        self.assertEqual(self.project()[0], 'unknown')

    def test_incomplete_attachment_or_dependency_observation_is_unknown(self):
        self.atts.clear()
        self.assertEqual(self.project()[0], 'unknown')
        self.atts['t_source'] = []
        self.source['parents'] = ['t_unobserved']
        self.assertEqual(self.project()[0], 'unknown')

    def test_settled_parent_identity_changes_context_but_not_heartbeat_noise(self):
        first = self.project()[1]['reason_sha256']
        self.source['parents'] = ['t_parent']
        self.shows['t_parent'] = dict(task=dict(id='t_parent', status='done'), parents=[], runs=[])
        self.assertNotEqual(first, self.project()[1]['reason_sha256'])

    def test_progress_summary_has_explicit_nullable_authority(self):
        from clickup_intake import render
        params = dict(task='t_fixture', card='t_source', run_id=12, event_kind='progress_stall',
            event_created_s=1, block_kind='progress_stall', **{'class':'progress_stall'}, reason_sha256='a'*64,
            approval_id=None, rev=None, plan_sha256=None, attempt=1, adopted=0)
        summary = render.recovery_summary(params, 't_carrier')
        self.assertNotIn('rNone', summary)
        self.assertIn('approval=null', summary)

    def test_incomplete_settled_parent_run_observation_is_unknown(self):
        self.source['parents'] = ['t_parent']
        self.shows['t_parent'] = dict(task=dict(id='t_parent', status='done'), parents=[])
        self.assertEqual(self.project()[0], 'unknown')

    def test_no_native_anchor_rejects_any_possibly_sent_creation(self):
        self.task['phase'] = 'new_quiet'
        self.state.update(requests=[], created_ms=self.now-1000, transitions=[dict(kind='kb_create',
            attempts=1, result_ref=None, first_send_ms=self.now, child_pid=None, state='pending',
            idem_key='request:1', payload_sha256='a'*64)])
        self.assertEqual(self.project()[0], 'unknown')

    def test_ledger_observation_timestamp_is_not_meaningful_progress(self):
        self.state['blockers'] = [dict(question_card='t_question', updated_ms=1)]
        self.shows['t_question'] = dict(task=dict(id='t_question', status='done'), parents=[], runs=[])
        first = self.project()[1]['reason_sha256']
        self.state['blockers'][0]['updated_ms'] = 1000
        self.assertEqual(first, self.project()[1]['reason_sha256'])

    def test_noise_and_retry_counters_do_not_change_fingerprint(self):
        first = self.project()[1]['reason_sha256']
        self.source['events'].append(dict(kind='heartbeat', created_at=1800000000, payload=dict(noise='new')))
        self.source['task']['last_heartbeat_at'] = 1800000000
        self.assertEqual(first, self.project()[1]['reason_sha256'])
        self.source['runs'][0]['metadata']['literal'] = 'ff7ec9d4placeholder'
        self.assertNotEqual(first, self.project()[1]['reason_sha256'])
