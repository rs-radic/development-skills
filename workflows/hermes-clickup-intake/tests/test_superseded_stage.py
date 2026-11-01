"""Current stage gates ignore retained superseded audit obligations, not real parents."""
import copy
import unittest

import test_preworker_recovery as stage
import test_recovery as recovery


class SupersededStageTest(unittest.TestCase):
    setUp = stage.PreworkerRecoveryTest.setUp
    scans = stage.PreworkerRecoveryTest.scans
    stall = stage.PreworkerRecoveryTest.stall
    stopped_stage = stage.PreworkerRecoveryTest.stopped_stage

    def add_audit(self, state='superseded'):
        with self.stopped_stage():
            self.scans(5)
        [approval] = self.w.q('SELECT * FROM approvals')
        source = self.life.req_cards()[-1]
        question = 't_c0de9911'
        old_work = 't_c0de9912'
        for card, status in ((question, 'blocked'), (old_work, 'todo')):
            value = copy.deepcopy(self.b.cards[source])
            value['task'].update(id=card, status=status, current_run_id=None,
                                 worker_pid=None, assignee='clickup-intake')
            value.update(parents=[], children=[], runs=[], events=[], comments=[], atts=[], subs=[])
            self.b.cards[card] = value
        self.b.cards[old_work]['parents'] = [question]
        c = recovery.store_conn(self.w)
        try:
            c.execute('INSERT INTO blockers(task,question_card,card,run_id,created_ms,approval_id,state,origin_lane) '
                      "VALUES (?,?,?,0,?,?,?,'work')", (self.tid, question, old_work,
                                                     self.w.clock(), approval['id'], state))
        finally:
            c.close()
        return approval, source, question, old_work

    def test_superseded_audit_does_not_block_normal_stage_release(self):
        approval, source, question, old_work = self.add_audit()
        before = copy.deepcopy(self.b.cards[question])
        self.assertEqual(self.b.cards[source]['parents'], [])
        self.w.step(lambda: self.life.orch() and
                    self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertIsNotNone(self.life.orch())
        self.assertIsNotNone(self.life.gate())
        self.assertEqual(self.b.cards[question], before)
        self.assertEqual(self.b.cards[old_work]['parents'], [question])
        self.assertEqual(self.w.q('SELECT state FROM blockers'), [{'state': 'superseded'}])
        self.assertEqual(len(self.b.by_key('clickup-dp:%s:orch:' % self.tid)), 1)

    def test_current_question_still_blocks_stage(self):
        approval, source, question, old_work = self.add_audit('asked')
        self.scans(20)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        self.assertEqual(self.b.cards[question]['task']['status'], 'blocked')

    def test_actual_open_parent_blocks_even_if_audit_is_superseded(self):
        approval, source, question, old_work = self.add_audit()
        self.b.cards[source]['parents'] = [question]
        self.scans(20)
        self.assertIsNone(self.life.gate())
        self.assertIsNone(self.life.orch())
        self.assertEqual(self.b.cards[question]['task']['status'], 'blocked')
