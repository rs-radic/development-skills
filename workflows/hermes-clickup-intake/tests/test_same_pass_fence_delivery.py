"""Newly observed protocol cards must use the existing guarded path in the same full scan.

Captured CPU substrate, real Glue.scan ordering, offline native/provider adapters.
"""
import test_three_native_waits as native
from unittest import mock
from clickup_intake.contracts import KanbanResult, Outcome
from test_question_relay import sentinel_show


class SamePassSentinelTest(native.NativeWaitCase):
    setUp = native.CpuNativeParkTest.setUp

    def new_worker(self):
        self.cycles(3)
        self.assertEqual(self.b.cards[self.park]['task']['status'], 'done')
        return self.b.claim(self.source, 'implementor')

    def new_sentinel(self, card, kind):
        self.w.clock.advance(1)
        self.b.cards[card] = dict(sentinel_show(
            card=card, title='CLICKUP-%s v1 %s' % (kind, self.source),
            w=self.source, created_s=self.w.clock() // 1000), atts=[], subs=[])
        self.b.cards[self.source]['parents'].append(card)
        self.assertEqual(self.w.q('SELECT * FROM fences WHERE fence_card=?', card), [])
        return card

    def test_new_fence_is_delivered_in_its_first_complete_scan(self):
        run = self.new_worker()
        fence = self.new_sentinel('t_f00df101', 'FENCE')
        created_ms = self.w.clock()
        self.w.clock.advance(901)  # a complete stable-task observation is actually due
        self.assertEqual(self.w.run()[0], 0)
        self.assertGreater(self.w.task(self.tid)['last_full_ms'], created_ms)
        self.assertEqual(self.b.cards[fence]['task']['status'], 'done',
                         'A newly detected fence must not spend another full poll queued without a release')
        self.assertEqual(self.b.mutations().count(('complete', fence)), 1)
        self.assertEqual(self.blocker(self.question)['state'], 'delivered',
                         'Fence delivery alone is not an authentic worker answer ACK')
        self.b.worker_comment(self.source, 'implementor',
            'CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=5 run=%d fence=%s' %
            (self.question, self.source, run, fence))
        self.cycles(2)
        self.assertEqual(self.blocker(self.question)['state'], 'resumed')
        self.assertEqual(self.b.mutations().count(('complete', fence)), 1)

    def test_new_park_is_delivered_in_its_first_complete_scan_after_worker_exit(self):
        self.new_worker()
        park = self.new_sentinel('t_f00df102', 'PARK')
        self.b.block(self.source, 'needs_input', 'Waiting on the existing intake-owned park')
        self.cycles(1)
        self.assertEqual(self.b.cards[park]['task']['status'], 'done',
                         'A newly detected park with no open worker must use its existing guarded release now')
        self.assertEqual(self.b.mutations().count(('complete', park)), 1)
        self.cycles(2)
        self.assertEqual(self.b.mutations().count(('complete', park)), 1)

    def test_new_fence_does_not_release_on_light_only_observation(self):
        self.new_worker()
        fence = self.new_sentinel('t_f00df103', 'FENCE')
        created_ms = self.w.clock()
        self.cycles(1)
        self.assertLess(self.w.task(self.tid)['last_full_ms'], created_ms)
        self.assertEqual(self.b.cards[fence]['task']['status'], 'blocked')
        self.assertNotIn(('complete', fence), self.b.mutations())

    def test_new_fence_missing_question_observation_does_not_release(self):
        self.new_worker()
        fence = self.new_sentinel('t_f00df104', 'FENCE')
        read = self.b.show
        with mock.patch.object(self.b, 'show', side_effect=lambda card:
                               KanbanResult(Outcome.DEFINITE_FAIL) if card == self.question else read(card)):
            self.w.clock.advance(901)
            self.assertEqual(self.w.run()[0], 0)
        self.assertEqual(self.b.cards[fence]['task']['status'], 'blocked')
        self.assertNotIn(('complete', fence), self.b.mutations())

    def test_new_park_with_open_worker_does_not_release(self):
        self.new_worker()
        park = self.new_sentinel('t_f00df105', 'PARK')
        self.w.clock.advance(901)
        self.assertEqual(self.w.run()[0], 0)
        self.assertEqual(self.b.cards[park]['task']['status'], 'blocked')
        self.assertNotIn(('complete', park), self.b.mutations())
