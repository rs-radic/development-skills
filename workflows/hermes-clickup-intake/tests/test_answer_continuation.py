"""Accepted human input stays owned by the bounded AI route until verified progress or verified escalation.

Real main/scan/reconcile/evaluate/execute; native board and ClickUp are offline fakes with synthetic content.
"""
import json
import unittest
from unittest import mock

import test_general_stall_wake as general
import test_preworker_recovery as pre
import test_stalled_fallback as fallback
import test_stalled_remediation as remediation
import test_s4_composition as comp
from clickup_intake import engine, kanban_port as kp
from clickup_intake.contracts import KanbanResult, Outcome
from test_recovery import store_conn


def carrier_key(board, cid):
    [key] = [k for k, c in board.keys.items() if c == cid and k.endswith(':create')]
    return key[:-len(':create')]


def carrier_row(w, board, cid, suffix):
    rows = w.q('SELECT state FROM outbox WHERE idem_key=?', carrier_key(board, cid) + suffix)
    return rows[0]['state'] if rows else None


def delivered_row(w, board, cid):
    """State of the escalation carrier's delivery-verification row (None when there is none)."""
    return carrier_row(w, board, cid, ':delivered')


def settled_row(w, board, cid):
    """State of the escalation's final accountability check (None before its window has passed)."""
    return carrier_row(w, board, cid, ':settled')


def gateway_settles(board, cid):
    """Fixture stand-in for the gateway notifier settling a completed carrier's legs: the passive ping cursor
    (``last_ping_event_id``) and the delivered wake cursor (``last_event_id``)."""
    n = 1000 + len(board.cards[cid]['events'])
    for s in board.cards[cid]['subs']:
        s.update(last_event_id=n, **({} if s['delivery_mode'] == 'wake' else {'last_ping_event_id': n}))


def verify_delivery(tc, w, board, cid):
    """A completed carrier is not delivery: the row stays unverified until the cursors show both legs settled."""
    settled = board.__dict__.setdefault('settled_carriers', set())
    if cid not in settled:
        tc.assertIn(delivered_row(w, board, cid), ('pending', 'retry_wait', 'sending', 'ambiguous'),
                    'ANSWER_ESCALATION: delivery is verified from the notification cursors, never assumed')
        gateway_settles(board, cid)
        settled.add(cid)
        w.step(lambda: delivered_row(w, board, cid) == 'done', limit=40)
    tc.assertEqual(delivered_row(w, board, cid), 'done')


class AnswerContinuationTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'socket.create_connection', 'socket.socket.connect',
                       'urllib.request.OpenerDirector.open'):
            p = mock.patch(target, side_effect=AssertionError('offline fixture forbids real subprocess/network'))
            p.start()
            self.addCleanup(p.stop)
        remediation.RemediationTest.setUp(self)

    scan_minutes = remediation.RemediationTest.scan_minutes
    notice_rows = remediation.RemediationTest.notice_rows
    diagnostic_wait = remediation.RemediationTest.diagnostic_wait

    ANSWER = 'Use the existing provider A within unchanged authority and continue the same approved work.'

    def carriers(self):
        prefix = 'clickup-dp:%s:rcv:' % self.tid
        return {key: cid for key, cid in self.b.keys.items() if key.startswith(prefix)}

    def incident(self):
        # The fixture's independent whole-workflow watcher may own its own progress_stall incident.
        [inc] = self.w.q("SELECT * FROM recoveries WHERE class != 'progress_stall'")
        return inc

    def question_ack(self, card, run, **changes):
        inc = self.incident()
        q = {'task': self.tid, 'card': card, 'run': run, 'approval': inc['approval_id'],
             'revision': inc['approval_rev'], 'sha256': inc['plan_sha256'], 'fingerprint': inc['reason_sha256'],
             'failed': 'The approved identity provider cannot be inferred from the accepted requirements.',
             'attempted': 'Read the accepted requirements and inspected the existing integration capabilities.',
             'decision': 'Which approved identity provider must this integration use: existing provider A or B?',
             'next_action': 'Reply in this question thread and mention the intake bot; choose A or B.'}
        q.update(changes)
        self.b.worker_comment(card, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\n'
                              'CLICKUP-RECOVERY-QUESTION v1 %s' % (card, run, json.dumps(q, sort_keys=True)))
        return q

    def stopped_with_question(self):
        card = self.life.fe
        run = self.b.claim(card, 'e2e-tester')
        self.b.block(card, 'needs_input', 'A genuine requirements choice remains unresolved.')
        self.w.step(lambda: self.w.q('SELECT * FROM recoveries'))
        self.question_ack(card, run)
        self.scan_minutes(6)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        return card, run, self.incident()

    def answer(self, inc, text=None, authority='unchanged'):
        wait = self.diagnostic_wait()
        self.life.say(comp.APPROVER_A, 'Provider A is fine.', mention=comp.BOT, reply_to=wait['root'])
        def script(cid, ref, gen):
            self.b.park(cid, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, text or self.ANSWER,
                                 gen=gen, diagnostic=inc['id'], authority=authority))
        return script

    def accept(self, inc):
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'answered',
                    world=self.life.planner(self.answer(inc)))

    def continuations(self, before, answer=True):
        return {k: c for k, c in self.carriers().items()
                if k not in before and k.endswith(':create') and not k.endswith('-owner:create')
                and (':answer-g' in k) == answer}

    def test_answer_after_expired_original_deadline_wakes_same_owner_once_with_the_answer(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60_000  # the original deadline expired before the reply
        self.scan_minutes(3)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting', 'a verified human wait stays quiet')
        self.accept(inc)
        self.scan_minutes(30)  # many passes, each a fresh process: one continuation only
        new = self.continuations(before)
        self.assertEqual(len(new), 1, 'ANSWER_CONTINUATION: an accepted answer must wake the capable owner once')
        [carrier] = new.values()
        self.assertIn(self.ANSWER, self.b.cards[carrier]['task']['body'], 'the wake must carry the accepted answer')
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'done', 'the wake was actually delivered')
        after = self.incident()
        for field in ('id', 'state', 'run_id', 'attempts', 'abs_ms', 'detected_ms', 'approval_id'):
            self.assertEqual(after[field], inc[field], field)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked', 'an answer is not automatic native release')
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.assertEqual(len(self.notice_rows()), 1, 'no repeated question for the same answer')

    def answered_after_deadline(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60_000
        self.accept(inc)
        self.scan_minutes(5)
        [carrier] = self.continuations(before).values()
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'done')
        return card, run, inc, before

    def alerts(self, card):
        return [line for line in self.w.outputs if 'accepted answer' in line and card in line]

    def answer_wakes(self, before):
        """Wake-row states of the answer-bound wakes in queue order; each carries the accepted answer."""
        out = []
        for key, cid in sorted(self.continuations(before).items(), key=lambda kc: self.w.q(
                'SELECT rowid FROM outbox WHERE idem_key=?', kc[0])[0]['rowid']):
            self.assertIn(self.ANSWER, self.b.cards[cid]['task']['body'], 'every continuation wake carries the answer')
            out.append(self.w.q('SELECT state FROM outbox WHERE idem_key=?', key[:-len(':create')] + ':wake')[0]['state'])
        return out

    def unchanged_history(self, inc):
        after = self.incident()
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms', 'approval_id'):
            self.assertEqual(after[field], inc[field], field)
        return after

    TAKEOVER = 'Operator takeover'

    def operator_owned(self, before, card):
        """No verified progress through the bounded answer wakes: one final takeover wake of the main-chat operator
        through the existing carrier route, never a ClickUp operator-choice question, notice or human wait."""
        self.scan_minutes(30)
        self.assertEqual(self.answer_wakes(before), ['done'], 'the owner gets its progress window first')
        self.w.clock.advance(engine.RECOVERY_RETRY_MIN[1] * 60)
        self.scan_minutes(3)
        self.assertEqual(self.answer_wakes(before), ['done', 'done'],
                         'ANSWER_OWNERSHIP: no progress after the answer wake re-wakes the capable AI owner')
        self.w.clock.advance(engine.RECOVERY_RETRY_MIN[2] * 60)
        self.scan_minutes(3)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.RECOVERY_ATTEMPTS)
        self.assertNotIn(self.TAKEOVER, self.b.cards[self.last_continuation(before)]['task']['body'])
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['done'] * (engine.RECOVERY_ATTEMPTS + 1),
                         'ANSWER_OPERATOR: exhausted answer wakes place the obligation with the main-chat operator')
        self.assertIn(self.TAKEOVER, self.b.cards[self.last_continuation(before)]['task']['body'])
        self.operator_route_only(card)

    def operator_route_only(self, card):
        self.assertEqual(len(self.notice_rows()), 1, 'ANSWER_OPERATOR: no ClickUp operator-choice question or notice')
        self.assertFalse(any('authorized operator can take over' in c['comment_text']
                             for c in self.w.bot_comments(self.tid)), 'no human is asked to choose an operator')
        self.assertEqual([l for l in self.w.outputs if 'No further AI wake' in l], [], 'never a passive terminal alert')
        self.assertEqual(self.diagnostic_wait()['state'], 'answered', 'ANSWER_OPERATOR: never a quiet human wait')
        self.assertEqual(self.incident()['state'], 'open', 'the obligation stays owned and observed')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def last_continuation(self, before):
        return max(self.continuations(before).items(), key=lambda kc: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', kc[0])[0]['rowid'])[1]

    ESCALATION = 'Operator escalation'

    def escalated(self, before, card, notices=1):
        """The precise verified escalation after an unaccepted/unprogressed takeover: one carrier on the existing
        notify+wake route to the configured Discord channel (a visible ping plus a wake of the main operator), whose delivery is
        verified from the notification cursors, never a ClickUp question/notice, a stdout alert alone or a repeat
        of the answered question."""
        cid = self.last_continuation(before)
        task = self.b.cards[cid]['task']
        self.assertIn(self.ESCALATION, task['body'], 'ANSWER_ESCALATION: the escalation carrier states the obligation')
        self.assertIn(self.ANSWER, task['body'])
        self.assertEqual(task['status'], 'done', 'ANSWER_ESCALATION: the escalation was actually sent')
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[cid]['subs']], ['notify+wake'],
                         'ANSWER_ESCALATION: a visible ping and a wake of the main operator, not the silent wake only')
        verify_delivery(self, self.w, self.b, cid)
        [summary] = [e['payload']['summary'] for e in self.b.cards[cid]['events'] if e['kind'] == 'completed']
        head = summary.splitlines()[0]
        self.assertTrue(head.startswith('CLICKUP-RECOVERY ESCALATION'), head)
        self.assertLessEqual(len(head), 200, 'the visible notification line carries the whole precise headline')
        self.assertIn(card, head)
        self.assertEqual(len(self.notice_rows()), notices, 'never a ClickUp operator-choice question or notice')
        self.assertFalse(any('authorized operator can take over' in c['comment_text']
                             for c in self.w.bot_comments(self.tid)), 'no human is asked to choose an operator')
        self.assertTrue(any('ESCALATION' in l and card in l for l in self.w.outputs), 'the operator alert names it')
        self.assertEqual(self.diagnostic_wait()['state'], 'answered', 'never a quiet human wait')
        self.assertEqual(self.incident()['state'], 'open', 'the obligation stays visibly unresolved')
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')
        return cid

    def test_silent_takeover_is_not_placed_and_reaches_one_delivered_visible_escalation(self):
        card, run, inc, before = self.answered_after_deadline()
        old = self.diagnostic_wait()
        self.operator_owned(before, card)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0,
                         'the woken operator is admitted: the obligation is still current')
        self.w.clock.advance(60 * 60)
        self.scan_minutes(5)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 1, 'the takeover gets its window')
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['done'] * (engine.RECOVERY_ATTEMPTS + 2),
                         'ANSWER_ESCALATION: a delivered but unacknowledged takeover is not placed ownership')
        self.escalated(before, card)
        wait = self.diagnostic_wait()
        self.assertEqual((wait['root'], wait['resolution']), (old['root'], old['resolution']),
                         'the answered question is never repeated or rebound')
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scan_minutes(60)  # many further passes, each a fresh process
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2, 'finite and deduplicated')
        self.assigned_after_window()
        self.unchanged_history(inc)

    RECEIPT = 'CLICKUP-RECOVERY-ACCEPT v1 incident=%d answer=g%d relay=%s carrier=%s'

    def receipt(self, card, run, carrier, author='default', on=None, **changes):
        """The receiving operator's bound acceptance: the exact working ACK first line plus the generation- and
        carrier-bound receipt line, written by the real receiver after its CURRENT admission."""
        wait, inc = self.diagnostic_wait(), self.incident()
        f = dict(incident=inc['id'], gen=wait['gen'], relay=wait['relay'], carrier=carrier)
        f.update(changes)
        self.b.worker_comment(on or card, author, 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working\n' % (card, run)
                              + self.RECEIPT % (f['incident'], f['gen'], f['relay'], f['carrier']))

    def test_takeover_carrier_instructs_the_receiver_to_post_its_bound_receipt(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        wait, cid = self.diagnostic_wait(), self.last_continuation(before)
        body = self.b.cards[cid]['task']['body']
        self.assertIn(self.RECEIPT % (inc['id'], wait['gen'], wait['relay'], '<this carrier id>'), body,
                      'ANSWER_RECEIPT: the takeover names the exact generation/carrier-bound receipt to write')
        self.assertIn('CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, run), body)

    def test_operator_receipt_after_takeover_without_progress_still_reaches_the_escalation(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        self.w.clock.advance(60 * 60)
        self.receipt(card, run, self.last_continuation(before))
        self.w.clock.advance((engine.RECOVERY_DEFER_MIN - 30) * 60)  # past the takeover's own window, inside the receipt's
        self.scan_minutes(5)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 1,
                         'ANSWER_RECEIPT: a bound receipt of the receiving operator accepts the takeover')
        self.w.clock.advance(60 * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['done'] * (engine.RECOVERY_ATTEMPTS + 2),
                         'ANSWER_ESCALATION: operator acceptance without verified progress is not completion')
        self.escalated(before, card)
        self.unchanged_history(inc)

    def test_unbound_stale_or_foreign_receipts_never_accept_the_takeover(self):
        card, run, inc, before = self.answered_after_deadline()
        earlier = self.continuations(before)
        self.operator_owned(before, card)
        takeover, wait = self.last_continuation(before), self.diagnostic_wait()
        self.w.clock.advance(60 * 60)
        self.b.worker_comment(card, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, run))
        self.receipt(card, run, sorted(earlier.values())[0])  # an earlier answer wake's carrier
        self.receipt(card, run, takeover, gen=wait['gen'] + 1)
        self.receipt(card, run, takeover, incident=inc['id'] + 1)
        self.receipt(card, run, takeover, relay=wait['relay'] + 'x')
        self.receipt(card, run, takeover, author='coordinator')
        self.receipt(card, run + 1, takeover)
        self.w.clock.advance((engine.RECOVERY_DEFER_MIN - 60) * 60 + 60)  # just past the takeover's own window
        self.scan_minutes(5)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2,
                         'ANSWER_RECEIPT: an unbound, stale, wrong-generation or foreign ACK is not accepted ownership')
        self.unchanged_history(inc)

    def test_failed_takeover_is_not_consumed_and_is_redelivered_after_release(self):
        card, run, inc, before = self.answered_after_deadline()
        self.scan_minutes(30)
        for gap in (engine.RECOVERY_RETRY_MIN[1], engine.RECOVERY_RETRY_MIN[2]):
            self.w.clock.advance(gap * 60)
            self.scan_minutes(3)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.RECOVERY_ATTEMPTS)
        complete = self.b.complete
        def failing_takeover(cid, summary, metadata=None):
            if self.TAKEOVER in self.b.cards[cid]['task']['body']:
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return complete(cid, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=failing_takeover):
            self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
            self.scan_minutes(90)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.RECOVERY_ATTEMPTS + ['failed'])
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold.startswith('outbox:') and hold.endswith(':wake'), 'the existing delivery hold applies')
        self.assertEqual(self.w.main('release', self.tid)[0], 0)  # existing supported operator release
        self.scan_minutes(5)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.RECOVERY_ATTEMPTS + ['failed', 'done'],
                         'ANSWER_TAKEOVER: a failed takeover is not consumed; it is delivered after release')
        self.assertIn(self.TAKEOVER, self.b.cards[self.last_continuation(before)]['task']['body'])
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.RECOVERY_ATTEMPTS + ['failed', 'done', 'done'],
                         'ANSWER_ESCALATION: the silent redelivered takeover still reaches the escalation')
        self.escalated(before, card, notices=2)  # plus only the existing verified delivery-hold notice
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scan_minutes(20)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 3, 'finite')
        self.unchanged_history(inc)

    def test_undelivered_escalation_is_held_then_redelivered_after_release(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['done'] * (engine.RECOVERY_ATTEMPTS + 2))
        first = self.last_continuation(before)
        self.scan_minutes(90)  # the gateway never settles the ping/wake cursors of this carrier
        self.assertEqual(delivered_row(self.w, self.b, first), 'failed',
                         'ANSWER_ESCALATION: an unverified delivery is a recorded failure, never assumed delivered')
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold.startswith('outbox:') and hold.endswith(':delivered'), hold)
        held = [r for r in self.notice_rows() if json.loads(r['body']).get('recovery_delivery_hold') == hold]
        self.assertEqual([r['state'] for r in held], ['done'], 'the existing delivery-hold notice is delivered')
        self.assertEqual(self.incident()['state'], 'open', 'the obligation stays visibly unresolved')
        self.assertEqual(self.w.main('release', self.tid)[0], 0)  # existing supported operator release
        self.scan_minutes(5)
        self.assertNotEqual(self.last_continuation(before), first, 'the unverified escalation is not consumed')
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 3)
        self.escalated(before, card, notices=2)
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scan_minutes(20)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 3, 'finite and deduplicated')
        self.unchanged_history(inc)

    def refuse_escalation_wakes(self, times):
        """The existing recovery send gate refuses (``stale``) the escalation's wake ``times`` times: never sent."""
        from clickup_intake import main as glue
        real, refused = glue.Glue.recovery_gate, []
        def gate(glue_self, row, p):
            if (p.get('params', {}).get('answer') or {}).get('escalation') and len(refused) < times:
                refused.append(row['idem_key'])
                return 'stale'
            return real(glue_self, row, p)
        return mock.patch.object(glue.Glue, 'recovery_gate', autospec=True, side_effect=gate), refused

    def test_refused_unsent_escalation_is_not_consumed_and_is_queued_again(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        patch, refused = self.refuse_escalation_wakes(1)
        with patch:
            self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
            self.scan_minutes(10)
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.answer_wakes(before)[:engine.RECOVERY_ATTEMPTS + 2],
                         ['done'] * (engine.RECOVERY_ATTEMPTS + 1) + ['cancelled_stale'])
        self.assertEqual(self.answer_wakes(before)[engine.RECOVERY_ATTEMPTS + 2:], ['done'],
                         'ANSWER_ESCALATION: a refused, never-sent escalation is not consumed; it is queued again')
        self.escalated(before, card)
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scan_minutes(20)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 3, 'finite and deduplicated')
        self.unchanged_history(inc)

    def test_persistently_refused_escalation_is_bounded_and_named(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        patch, refused = self.refuse_escalation_wakes(100)
        with patch:
            self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
            self.scan_minutes(30)
            self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
            self.scan_minutes(20)
        self.assertEqual(len(refused), engine.RECOVERY_ATTEMPTS + 1, 'ANSWER_ESCALATION: refusals never loop unboundedly')
        self.assertEqual(len([l for l in self.w.outputs if 'escalation refused' in l and card in l]), 1,
                         'the exhausted refusal is named once for the operator')
        self.assertEqual(self.incident()['state'], 'open')
        self.unchanged_history(inc)

    def test_unverified_escalation_check_stops_once_verified_progress_closes_the_incident(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(5)
        cid = self.last_continuation(before)
        self.assertIn(delivered_row(self.w, self.b, cid), ('pending', 'retry_wait'))
        self.b.unblock(card)  # the operator performs the existing supported resume; the same assignee runs live
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(70)  # the gateway never settles this ping
        self.assertEqual(self.unchanged_history(inc)['state'], 'resumed')
        self.assertEqual(delivered_row(self.w, self.b, cid), 'cancelled_stale',
                         'ANSWER_ESCALATION: a settled incident no longer needs its escalation read back')
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'never a delivery hold for a settled incident')

    def final_window(self):
        """A delivered escalation whose recipient has not yet settled anything: (card, run, inc, before, carrier,
        escalation queue time)."""
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        cid = self.escalated(before, card)
        return card, run, inc, before, cid, self.diagnostic_wait()['continuation']['last_ms']

    def assigned_after_window(self):
        inc = self.incident()
        return self.assigned_owner(inc['card'], inc['run_id'], inc)

    def assigned_recoveries(self):
        return [cid for cid, shown in self.b.cards.items()
                if shown['task'].get('title', '').startswith('CLICKUP-RECOVERY-OWNER v1 ')]

    def assigned_owner(self, card, run, inc):
        owners = self.assigned_recoveries()
        self.assertEqual(len(owners), 1, 'ASSIGNED_RECOVERY: exhausted wakes must create one assigned task')
        [cid] = owners
        task = self.b.cards[cid]['task']
        self.assertEqual(task['assignee'], 'integrator')
        self.assertEqual(task['status'], 'ready', 'ordinary dispatcher owns pickup; no human release')
        self.assertEqual(self.b.cards[cid]['parents'], [], 'the stopped source cannot gate its recovery')
        self.assertIn(self.ANSWER, task['body'])
        self.assertIn('worker', task['body'])
        self.assertIn('operator handoff', task['body'])
        self.assertIn('recovery-check --card %s --run %d' % (card, run), task['body'])
        self.assertIn('incident=%d' % inc['id'], task['body'])
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'elapsed time is not a human-only blocker')
        self.assertEqual(self.incident()['state'], 'open', 'creating a recovery task does not settle the source')
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        return cid

    def test_delivered_final_escalation_without_receipt_dispatches_one_assigned_recovery(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + engine.RECOVERY_DEFER_MIN * 60_000 + 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(60)
        self.assertEqual(self.assigned_recoveries(), [owner], 'duplicate polls/restarts reuse the assigned task')
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)
        self.assertEqual(len(self.notice_rows()), 1, 'no manufactured hold notice or repeated question')
        self.assertIsNone(settled_row(self.w, self.b, cid), 'no intentionally failing check')
        self.unchanged_history(inc)

    def test_assigned_recovery_pickup_requires_a_live_matching_native_worker(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        key = carrier_key(self.b, owner)
        self.b.claim(owner, 'integrator')
        self.scan_minutes(3)
        self.assertFalse(self.w.q('SELECT value FROM meta WHERE key=?', 'recovery_pickup:' + key),
                         'a queued status or unsupported claim is not real pickup')
        task, worker_run = self.b.cards[owner]['task'], self.b.cards[owner]['runs'][-1]
        task.update(current_run_id=worker_run['id'], worker_pid=4243, last_heartbeat_at=self.w.clock() // 1000)
        worker_run['worker_pid'] = 4243
        self.b._event(owner, 'claimed', run_id=worker_run['id'])
        start = str((worker_run['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4243 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.assertIsNotNone(kp.worker_progress(self.b.show(owner).json, self.w.clock()), self.b.show(owner).json)
            self.scan_minutes(3)
        rows = self.w.q('SELECT value FROM meta WHERE key=?', 'recovery_pickup:' + key)
        self.assertEqual(len(rows), 1, 'ASSIGNED_PICKUP: native matching live worker pickup is observed')
        proof = json.loads(rows[0]['value'])
        self.assertEqual((proof['run'], proof['owner']), (worker_run['id'], 'integrator'))
        self.assertEqual(self.incident()['state'], 'open', 'pickup is not source progress')

    def test_premature_recovery_completion_routes_exact_same_task_operator_handoff(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        self.b.finish(owner, 'integrator')
        self.scan_minutes(5)
        handoff = self.last_continuation(before)
        self.assertEqual(self.b.cards[owner]['task']['status'], 'done', 'preserve the failed outcome for same-task correction')
        self.assertNotEqual(handoff, cid, 'FALSE_COMPLETION: an exact supported operator handoff must wake')
        body = self.b.cards[handoff]['task']['body']
        self.assertIn('Unresolved premature completion', body)
        self.assertIn(owner, body)
        self.assertIn('update_task', body)
        self.assertIn('same-card correction', body)
        verify_delivery(self, self.w, self.b, handoff)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner], 'never replacement recovery tasks')
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 3, 'one correction wake only')
        self.assertEqual(self.incident()['state'], 'open', 'false completion or handoff is not source recovery')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.assertTrue(any('premature completion' in c['body'] for c in self.b.cards[owner]['comments']))
        self.unchanged_history(inc)

    def test_failed_correction_handoff_is_not_consumed_and_is_delivered_after_release(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        self.b.finish(owner, 'integrator')
        complete = self.b.complete
        def failing(cid2, summary, metadata=None):
            if 'Unresolved premature completion' in self.b.cards[cid2]['task']['body']:
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return complete(cid2, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=failing):
            self.scan_minutes(120)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.ANSWER_STEPS + ['failed'])
        self.released_after_hold(':wake')
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.ANSWER_STEPS + ['failed', 'done'],
                         'FALSE_COMPLETION: a failed correction handoff is not consumed; it is delivered after release')
        handoff = self.last_continuation(before)
        self.assertIn('Unresolved premature completion', self.b.cards[handoff]['task']['body'])
        verify_delivery(self, self.w, self.b, handoff)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(len(self.answer_wakes(before)), engine.ANSWER_STEPS + 2, 'finite and deduplicated')
        self.assertEqual(self.assigned_recoveries(), [owner], 'never replacement recovery tasks')
        self.assertEqual(len([c for c in self.b.cards[owner]['comments'] if 'premature completion' in c['body']]), 1)
        self.assertEqual(self.incident()['state'], 'open', 'a delivered handoff is not source recovery')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.unchanged_history(inc)

    def test_operator_resend_of_failed_correction_handoff_is_delivered_once(self):
        card, run, inc, before, owner = self.prematurely_completed_owner()
        complete = self.b.complete
        def failing(cid2, summary, metadata=None):
            if 'Unresolved premature completion' in self.b.cards[cid2]['task']['body']:
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return complete(cid2, summary, metadata)
        with mock.patch.object(self.b, 'complete', side_effect=failing):
            self.scan_minutes(120)
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold.endswith(':wake'), hold)
        self.assertEqual(self.w.main('outbox', 'resend', hold[len('outbox:'):])[0], 0)
        self.scan_minutes(10)
        handoff = self.last_continuation(before)
        self.assertIn('Unresolved premature completion', self.b.cards[handoff]['task']['body'])
        verify_delivery(self, self.w, self.b, handoff)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.answer_wakes(before), ['done'] * (engine.ANSWER_STEPS + 1),
                         'DUPLICATE_HANDOFF: an operator-resent handoff is read back, never delivered again')
        sent = [k for k, c in self.continuations(before).items() if c == handoff][0][:-len(':create')]
        self.assertEqual(self.w.q('SELECT state FROM outbox WHERE idem_key=?', sent + ':delivered')[0]['state'],
                         'done', 'UNVERIFIED_HANDOFF: the resent handoff ping is read back')
        self.assertEqual(self.assigned_recoveries(), [owner], 'never replacement recovery tasks')
        self.assertEqual(self.incident()['state'], 'open', 'a delivered handoff is not source recovery')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.unchanged_history(inc)

    def refuse_correction_handoff(self, times):
        from clickup_intake import main as glue
        real, refused = glue.Glue.recovery_gate, []
        def gate(glue_self, row, p):
            if (p.get('params', {}).get('answer') or {}).get('correction') and len(refused) < times:
                refused.append(row['idem_key'])
                return 'stale'
            return real(glue_self, row, p)
        return mock.patch.object(glue.Glue, 'recovery_gate', autospec=True, side_effect=gate), refused

    def prematurely_completed_owner(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        self.b.finish(owner, 'integrator')
        return card, run, inc, before, owner

    def test_refused_unsent_correction_handoff_is_not_consumed_and_is_queued_again(self):
        card, run, inc, before, owner = self.prematurely_completed_owner()
        patch, refused = self.refuse_correction_handoff(1)
        with patch:
            self.scan_minutes(10)
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.answer_wakes(before), ['done'] * engine.ANSWER_STEPS + ['cancelled_stale', 'done'],
                         'FALSE_COMPLETION: a refused, never-sent handoff is queued again')
        verify_delivery(self, self.w, self.b, self.last_continuation(before))
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(len(self.answer_wakes(before)), engine.ANSWER_STEPS + 2, 'finite and deduplicated')
        self.assertEqual(self.assigned_recoveries(), [owner])
        self.unchanged_history(inc)

    def test_persistently_refused_correction_handoff_is_bounded_and_named(self):
        card, run, inc, before, owner = self.prematurely_completed_owner()
        patch, refused = self.refuse_correction_handoff(100)
        with patch:
            self.scan_minutes(30)
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
        self.assertEqual(len(refused), engine.RECOVERY_ATTEMPTS + 1, 'refusals never loop unboundedly')
        self.assertEqual(len([l for l in self.w.outputs if 'correction handoff refused' in l and card in l]), 1,
                         'the exhausted refusal is named once for the operator')
        self.assertEqual(self.incident()['state'], 'open')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.unchanged_history(inc)

    def owner_rows(self):
        return [(r['idem_key'].rsplit(':', 1)[1], r['state']) for r in self.w.q(
            "SELECT idem_key, state FROM outbox WHERE idem_key LIKE '%:rcv:%-owner:%' ORDER BY rowid")]

    def released_after_hold(self, suffix):
        """The existing delivery hold of a failed recovery effect, then its supported operator release."""
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold.startswith('outbox:') and hold.endswith(suffix), hold)
        rows = lambda: [tuple(r) for r in self.w.q('SELECT idem_key, state FROM outbox ORDER BY rowid')]
        held = rows()
        self.scan_minutes(30)
        self.assertEqual(rows(), held,
                         'nothing is redone while the existing hold stands')
        self.assertEqual(self.w.main('release', self.tid)[0], 0)
        self.scan_minutes(5)

    def test_failed_assigned_owner_creation_is_not_consumed_and_is_created_after_release(self):
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def failing(title, **kwargs):
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(title, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failing):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        self.assertEqual(self.assigned_recoveries(), [])
        self.released_after_hold('-owner:create')
        owner = self.assigned_owner(card, run, inc)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner], 'ASSIGNED_REDO: one owner, finite and deduplicated')
        self.assertEqual(self.owner_rows(), [('create', 'failed'), ('subscribe', 'cancelled_stale'),
                                             ('release', 'cancelled_stale'), ('create', 'done'),
                                             ('subscribe', 'done'), ('release', 'done')],
                         'the failed step is preserved as history and redone exactly once')
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[owner]['subs']], ['notify+wake'])
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.unchanged_history(inc)

    def test_ambiguous_owner_creation_redone_after_release_reuses_the_possibly_created_owner(self):
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def ambiguous(title, **kwargs):
            result = create(title, **kwargs)  # applied, but the outcome is unknown to the intake
            return KanbanResult(Outcome.AMBIGUOUS) if title.startswith('CLICKUP-RECOVERY-OWNER v1 ') else result
        with mock.patch.object(self.b, 'create', side_effect=ambiguous):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        [created] = self.assigned_recoveries()
        self.released_after_hold('-owner:create')
        self.assertEqual(self.assigned_owner(card, run, inc), created,
                         'ASSIGNED_REDO: the native idempotency key of the one owner is reused, never a duplicate')
        self.unchanged_history(inc)

    def ambiguous_owner_hold(self):
        """The owner create lands natively but its outcome stays unknown: the existing hold, no owner ref."""
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def ambiguous(title, **kwargs):
            result = create(title, **kwargs)
            return KanbanResult(Outcome.AMBIGUOUS) if title.startswith('CLICKUP-RECOVERY-OWNER v1 ') else result
        with mock.patch.object(self.b, 'create', side_effect=ambiguous):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        [created] = self.assigned_recoveries()
        return card, run, inc, created

    def test_operator_archived_owner_of_an_unknown_create_is_named_and_never_replaced(self):
        card, run, inc, created = self.ambiguous_owner_hold()
        self.b.archive(created)  # the operator disposes of the owner the intake never learned about
        with self.hermes_create():
            self.released_after_hold('-owner:create')
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
            reads = self.b.calls.count(('list', None))
            self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [created],
                         'ASSIGNED_REDO: an operator-archived owner is never replaced, known ref or not')
        self.assertEqual([r for r in self.owner_rows() if r[0] == 'create'], [('create', 'failed')],
                         'no create is redone for a disposed owner')
        disposed = [l for l in self.w.outputs if 'recovery owner disposed' in l and card in l]
        self.assertEqual(len(disposed), 1, 'the operator disposition of the owner is named once')
        self.assertIn(created, disposed[0])
        self.assertEqual(self.b.calls.count(('list', None)), reads, 'the named disposition ends the lookups')
        self.assertEqual(self.incident()['state'], 'open', 'disposing of the owner is not source recovery')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.unchanged_history(inc)

    def test_unreadable_owner_lookup_takes_no_step_and_adopts_the_owner_once_read(self):
        card, run, inc, created = self.ambiguous_owner_hold()
        with mock.patch.object(self.b, 'list_tasks', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.released_after_hold('-owner:create')
            self.w.clock.advance(6 * 60 * 60)
            self.scan_minutes(30)
            self.assertEqual(self.owner_rows(), [('create', 'failed'), ('subscribe', 'cancelled_stale'),
                                                 ('release', 'cancelled_stale')],
                             'an unreadable lookup is unknown: no create is redone')
            self.assertFalse(self.w.task(self.tid)['hold_reason'], 'an unknown read is not a park')
            self.assertEqual(self.b.cards[created]['task']['status'], 'blocked', 'nor is it a success')
        with self.hermes_create():
            self.scan_minutes(10)
        self.assertEqual(self.assigned_owner(card, run, inc), created, 'the live owner is adopted, not replaced')
        self.unchanged_history(inc)

    def test_operator_archive_between_unknown_create_attempts_is_named_and_never_replaced(self):
        card, run, inc, before, cid, sent = self.final_window()
        create, sent_creates = self.b.create, []
        def racing(title, **kwargs):
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                sent_creates.append(title)
                if len(sent_creates) == 1:  # applied natively, outcome unknown; disposed before the next attempt
                    self.b.archive(create(title, **kwargs).json['id'])
                    return KanbanResult(Outcome.AMBIGUOUS)
                known = self.b.keys.get(kwargs['idempotency_key'])
                if known and self.b.cards[known]['task']['status'] == 'archived':
                    self.b.keys.pop(kwargs['idempotency_key'])  # Hermes create idempotency ignores archived tasks
            return create(title, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=racing):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            with mock.patch.object(self.b, 'list_tasks', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
                self.scan_minutes(30)
            self.assertEqual(len(sent_creates), 1, 'an unreadable lookup sends nothing again')
            self.assertFalse(self.w.task(self.tid)['hold_reason'], 'and is not a park')
            self.scan_minutes(120)
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
        owners = self.assigned_recoveries()
        self.assertEqual(len(owners), 1, 'ASSIGNED_REDO: a create retry never replaces an operator-archived owner')
        [created] = owners
        self.assertEqual(self.b.cards[created]['task']['status'], 'archived')
        self.assertEqual(len(sent_creates), 1, 'the possibly applied create is resolved before it is sent again')
        disposed = [l for l in self.w.outputs if 'recovery owner disposed' in l and card in l]
        self.assertEqual(len(disposed), 1, 'the operator disposition of the owner is named once')
        self.assertIn(created, disposed[0])
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.incident()['state'], 'open')
        self.unchanged_history(inc)

    def test_owner_lookup_ends_with_its_incident(self):
        card, run, inc, created = self.ambiguous_owner_hold()
        with mock.patch.object(self.b, 'list_tasks', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.released_after_hold('-owner:create')
        conn = store_conn(self.w)
        conn.execute("UPDATE recoveries SET state='superseded' WHERE id=?", (inc['id'],))
        conn.close()
        reads = self.b.calls.count(('list', None))
        self.scan_minutes(30)
        self.assertEqual(self.b.calls.count(('list', None)), reads, 'no lookup outlives its incident')
        self.assertEqual(self.assigned_recoveries(), [created])

    def test_persistently_refused_owner_create_is_named_once_and_ends_the_lookups(self):
        card, run, inc, before, cid, sent = self.final_window()
        from clickup_intake import main as glue
        real, refused = glue.Glue.recovery_gate, []
        def gate(glue_self, row, p):
            if p.get('role') == 'recovery_owner':
                refused.append(row['idem_key'])
                return 'stale'
            return real(glue_self, row, p)
        with mock.patch.object(glue.Glue, 'recovery_gate', autospec=True, side_effect=gate):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(30)
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
            reads = self.b.calls.count(('list', None))
            self.scan_minutes(30)
        self.assertEqual(len(refused), engine.RECOVERY_ATTEMPTS + 1)
        self.assertEqual(len([l for l in self.w.outputs if 'recovery owner refused' in l and card in l]), 1)
        self.assertEqual(self.b.calls.count(('list', None)), reads, 'a named refusal ends the lookups')
        self.assertEqual(self.assigned_recoveries(), [])
        self.unchanged_history(inc)

    def test_operator_abandoned_owner_create_is_neither_looked_up_nor_redone(self):
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def failing(title, **kwargs):
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(title, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failing):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        [key] = [r['idem_key'] for r in self.w.q("SELECT idem_key FROM outbox WHERE idem_key LIKE '%-owner:create'")]
        self.assertEqual(self.w.main('outbox', 'abandon', key)[0], 0)
        reads = self.b.calls.count(('list', None))
        self.scan_minutes(30)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.b.calls.count(('list', None)), reads, 'an abandoned owner create is not looked up')
        self.assertEqual(self.assigned_recoveries(), [], 'nor redone')
        self.unchanged_history(inc)

    def test_failed_assigned_owner_release_reuses_the_same_native_owner_after_release(self):
        card, run, inc, before, cid, sent = self.final_window()
        unblock = self.b.unblock
        def failing(cid2):
            if self.b.cards[cid2]['task'].get('title', '').startswith('CLICKUP-RECOVERY-OWNER v1 '):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return unblock(cid2)
        with mock.patch.object(self.b, 'unblock', side_effect=failing):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        [created] = self.assigned_recoveries()
        self.assertEqual(self.b.cards[created]['task']['status'], 'blocked')
        self.released_after_hold('-owner:release')
        self.assertEqual(self.assigned_owner(card, run, inc), created, 'ASSIGNED_REDO: the same native owner')
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [created], 'never a replacement recovery task')
        self.assertEqual([s for _, s in self.owner_rows()], ['done', 'done', 'failed', 'done', 'done'],
                         'only the unfinished steps are redone for the existing owner')
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)
        self.unchanged_history(inc)

    def refuse_owner_release(self, times):
        """The existing recovery send gate refuses (``stale``) the owner's release ``times`` times: never sent."""
        from clickup_intake import main as glue
        real, refused = glue.Glue.recovery_gate, []
        def gate(glue_self, row, p):
            if p.get('assigned_recovery') and len(refused) < times:
                refused.append(row['idem_key'])
                return 'stale'
            return real(glue_self, row, p)
        return mock.patch.object(glue.Glue, 'recovery_gate', autospec=True, side_effect=gate), refused

    def test_refused_unsent_assigned_owner_release_is_not_consumed_and_is_queued_again(self):
        card, run, inc, before, cid, sent = self.final_window()
        patch, refused = self.refuse_owner_release(1)
        with patch:
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(10)
        self.assertEqual(len(refused), 1)
        owner = self.assigned_owner(card, run, inc)
        self.assertEqual([s for _, s in self.owner_rows()], ['done', 'done', 'cancelled_stale', 'done', 'done'],
                         'ASSIGNED_REDO: a refused, never-sent release is queued again for the same owner')
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner])
        self.assertEqual(len(self.owner_rows()), 5, 'finite and deduplicated')
        self.unchanged_history(inc)

    def test_persistently_refused_assigned_owner_release_is_bounded_and_named(self):
        card, run, inc, before, cid, sent = self.final_window()
        patch, refused = self.refuse_owner_release(100)
        with patch:
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(30)
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
        self.assertEqual(len(refused), engine.RECOVERY_ATTEMPTS + 1, 'ASSIGNED_REDO: refusals never loop unboundedly')
        self.assertEqual(len([l for l in self.w.outputs if 'recovery owner refused' in l and card in l]), 1,
                         'the exhausted refusal is named once for the operator')
        self.assertEqual(len(self.assigned_recoveries()), 1, 'never a replacement recovery task')
        self.assertEqual(self.incident()['state'], 'open')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.unchanged_history(inc)

    def hermes_create(self):
        """Hermes ``create --idempotency-key`` matches only non-archived tasks (the shared fake ignores status)."""
        create = self.b.create
        def hermes(title, **kwargs):
            known = self.b.keys.get(kwargs['idempotency_key'])
            if known and self.b.cards[known]['task']['status'] == 'archived':
                self.b.keys.pop(kwargs['idempotency_key'])
            return create(title, **kwargs)
        return mock.patch.object(self.b, 'create', side_effect=hermes)

    def test_operator_archived_unreleased_owner_is_named_and_never_replaced(self):
        card, run, inc, before, cid, sent = self.final_window()
        from clickup_intake import main as glue
        real = glue.Glue.recovery_gate
        def defer(glue_self, row, p):  # e.g. the stopped source is momentarily unreadable: nothing is sent
            return 'defer' if p.get('assigned_recovery') else real(glue_self, row, p)
        with self.hermes_create():
            with mock.patch.object(glue.Glue, 'recovery_gate', autospec=True, side_effect=defer):
                self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
                self.scan_minutes(1)
                [owner] = self.assigned_recoveries()
                self.b.archive(owner)  # the operator disposes of the never-released owner natively
            self.scan_minutes(30)
            self.w.clock.advance(72 * 60 * 60)
            self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner], 'ASSIGNED_REDO: never a replacement native owner')
        self.assertEqual(len([l for l in self.w.outputs if 'recovery owner disposed' in l and card in l]), 1,
                         'the operator disposition of the owner is named once')
        self.assertEqual(self.incident()['state'], 'open', 'disposing of the owner is not source recovery')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.unchanged_history(inc)

    def test_resent_failed_owner_create_completes_the_same_owner_without_spending_a_refusal(self):
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def failing(title, **kwargs):
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(title, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failing):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(120)
        hold = self.w.task(self.tid)['hold_reason']
        self.assertEqual(self.w.main('outbox', 'resend', hold[len('outbox:'):])[0], 0)  # supported operator resend
        self.scan_minutes(10)
        owner = self.assigned_owner(card, run, inc)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner])
        self.assertEqual(self.diagnostic_wait()['continuation']['owner']['refused'], 0,
                         'an operator resend is not a send-gate refusal')
        self.assertEqual([s for _, s in self.owner_rows()],
                         ['done', 'cancelled_stale', 'cancelled_stale', 'done', 'done'],
                         'the resent create completes the one owner; only its cancelled dependents are queued again')
        self.unchanged_history(inc)

    def test_failed_correction_comment_is_posted_on_the_same_owner_after_release(self):
        card, run, inc, before, owner = self.prematurely_completed_owner()
        comment = self.b.comment
        def failing(card2, body):
            if 'Unresolved premature completion' in body:
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return comment(card2, body)
        with mock.patch.object(self.b, 'comment', side_effect=failing):
            self.scan_minutes(10)
            verify_delivery(self, self.w, self.b, self.last_continuation(before))
            self.scan_minutes(120)
        self.released_after_hold(':correction')
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(len([c for c in self.b.cards[owner]['comments'] if 'premature completion' in c['body']]), 1,
                         'FALSE_COMPLETION: a failed same-task correction is not consumed; it is posted after release')
        self.assertEqual(len(self.answer_wakes(before)), engine.ANSWER_STEPS + 1, 'one delivered handoff only')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.incident()['state'], 'open')
        self.unchanged_history(inc)

    def test_recovery_workers_genuine_capability_failure_stays_on_the_same_assigned_task(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        owner = self.assigned_owner(card, run, inc)
        self.b.claim(owner, 'integrator')
        reason = 'CLICKUP-RECOVERY-REQUEST native cross-card resume %s; CURRENT source/run read back, worker fence refuses it' % card
        self.b.block(owner, 'capability', reason)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(30)
        self.assertEqual(self.assigned_recoveries(), [owner], 'no replacement recovery graph')
        self.assertEqual((self.b.cards[owner]['task']['assignee'], self.b.cards[owner]['task']['status']),
                         ('integrator', 'blocked'))
        self.assertEqual([s['delivery_mode'] for s in self.b.cards[owner]['subs']], ['notify+wake'])
        self.assertEqual(self.b.cards[owner]['events'][-1]['payload']['reason'], reason)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)
        self.assertEqual(self.incident()['state'], 'open', 'a genuine native failure is still an owed obligation')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0)
        self.unchanged_history(inc)

    def test_new_answer_generation_at_release_refuses_the_old_assigned_task(self):
        card, run, inc, before, cid, sent = self.final_window()
        create = self.b.create
        def changed_answer(title, **kwargs):
            result = create(title, **kwargs)
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                conn = store_conn(self.w)
                row = conn.execute('SELECT value FROM meta WHERE key=?', ('diagnostic_wait:%d' % inc['id'],)).fetchone()
                wait = json.loads(row[0])
                wait.update(gen=wait['gen'] + 1, relay=wait['relay'] + '-new')
                conn.execute('UPDATE meta SET value=? WHERE key=?',
                             (json.dumps(wait), 'diagnostic_wait:%d' % inc['id']))
                conn.close()
            return result
        with mock.patch.object(self.b, 'create', side_effect=changed_answer):
            self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
            self.scan_minutes(1)
        [owner] = self.assigned_recoveries()
        self.assertEqual(self.b.cards[owner]['task']['status'], 'blocked',
                         'ANSWER_BINDING: a new classified answer cannot release a task carrying the old answer')
        self.assertEqual(carrier_row(self.w, self.b, owner, ':release'), 'cancelled_stale')
        self.assertEqual(self.incident()['state'], 'open')

    def test_stale_takeover_receipt_does_not_extend_the_final_assigned_fallback(self):
        card, run, inc, before, cid, sent = self.final_window()
        takeover = sorted(self.continuations(before).items(), key=lambda kc: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', kc[0])[0]['rowid'])[-2][1]
        self.w.clock.now_ms = sent + 30 * 60_000
        self.receipt(card, run, takeover)
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN - 10) * 60_000
        self.scan_minutes(5)
        self.assertEqual(self.assigned_recoveries(), [], 'the final recipient first gets its bounded window')
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        self.assigned_owner(card, run, inc)
        self.assertEqual(len(self.notice_rows()), 1)
        self.unchanged_history(inc)

    def test_final_recipients_bound_receipt_without_progress_still_dispatches_the_assigned_owner(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + 60 * 60_000
        self.receipt(card, run, cid)
        received = self.w.clock()
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 30) * 60_000
        self.scan_minutes(5)
        self.assertEqual(self.assigned_recoveries(), [], 'the bound final receipt extends its window, not forever')
        self.w.clock.now_ms = received + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(5)
        self.assigned_owner(card, run, inc)
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)
        self.unchanged_history(inc)

    def test_verified_progress_settles_the_final_accountability_check_without_a_hold(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + engine.RECOVERY_DEFER_MIN * 60_000 + 60_000
        self.scan_minutes(3)
        self.assigned_after_window()
        self.b.unblock(card)  # the operator performs the existing supported resume; the same assignee runs live
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(70)
        self.assertEqual(self.unchanged_history(inc)['state'], 'resumed')
        self.assertIsNone(settled_row(self.w, self.b, cid), 'no intentionally failing check')
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'never a hold for a settled obligation')
        self.assertEqual(len(self.notice_rows()), 1)

    def test_new_owner_question_after_final_escalation_settles_the_check_as_a_human_wait(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.w.clock.now_ms = sent + engine.RECOVERY_DEFER_MIN * 60_000 + 60_000
        self.scan_minutes(3)
        self.assigned_after_window()
        self.question_ack(card, run, decision='The answer leaves one new choice: should option C or D be used?')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        self.scan_minutes(70)
        self.assertIsNone(settled_row(self.w, self.b, cid))
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'a verified genuine human wait is not held')
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting')
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 2)

    def unclaimed_answer_handoff(self, expired):
        from preworker_snapshot_fixture import install
        install(self.b, self.w.dir)
        card = self.life.fe
        run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        self.b.block(card, 'unrecognized_stop', 'The approved provider choice needs clarification.')
        self.scan_minutes(18)
        inc = self.incident()
        self.question_ack(card, run)
        self.scan_minutes(6)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        before = self.carriers()
        if expired:
            self.w.clock.now_ms = inc['abs_ms'] + 60_000
        self.accept(inc)
        self.b.finish(self.diagnostic_wait()['resolution_card'], 'planner')
        self.scan_minutes(5)
        return card, run, inc, before

    def test_late_answer_unclaimed_source_release_keeps_the_assigned_obligation(self):
        card, run, inc, before = self.unclaimed_answer_handoff(expired=True)
        self.b.unblock(card)  # supported operator release is not capable-owner pickup
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        mark = len(self.b.mutations())
        self.scan_minutes(3)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered',
                         'UNCLAIMED_ANSWER: release must not replace the accepted answer with an operator-choice question')
        self.assertEqual(self.incident()['state'], 'open')
        for _ in range(engine.ANSWER_STEPS + 2):
            self.w.clock.advance(4 * 60 * 60)
            self.scan_minutes(3)
            for cid in self.continuations(before).values():
                if delivered_row(self.w, self.b, cid) in ('pending', 'retry_wait'):
                    verify_delivery(self, self.w, self.b, cid)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered',
                         'UNCLAIMED_ANSWER: no manufactured human wait after the original deadline')
        [owner] = self.assigned_recoveries()
        self.assertEqual((self.b.cards[owner]['task']['assignee'], self.b.cards[owner]['task']['status']),
                         ('integrator', 'ready'), 'the original approved assigned role owns diagnosis')
        self.assertEqual(self.b.cards[owner]['parents'], [])
        self.assertNotEqual(self.w.main('recovery-check', '--card', card, '--run', str(run))[0], 0,
                            'carrier admission must not broaden stopped-source mutation admission')
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        self.assertEqual([m for m in self.b.mutations()[mark:] if m[1] == card], [],
                         'handoff admission must never release or mutate the source')
        self.assertEqual(len([r for r in self.notice_rows()
                              if json.loads(r['body']).get('recovery_notice') == inc['id']]), 1)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered')
        self.assertEqual(len(self.answer_wakes(before)), engine.ANSWER_STEPS)
        self.w.clock.advance(72 * 60 * 60)
        self.scan_minutes(3)
        self.assertEqual(self.assigned_recoveries(), [owner])
        self.unchanged_history(inc)
        owner_run = self.b.claim(owner, 'integrator')
        owner_task = self.b.cards[owner]['task']
        owner_task.update(current_run_id=owner_run, worker_pid=4243, last_heartbeat_at=self.w.clock() // 1000)
        self.b.cards[owner]['runs'][-1]['worker_pid'] = 4243
        owner_start = str((self.b.cards[owner]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def owner_probe(pid):
            owner_task['last_heartbeat_at'] = self.w.clock() // 1000
            return owner_start if pid == 4243 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=owner_probe):
            self.scan_minutes(6)
        pickup = self.w.q('SELECT value FROM meta WHERE key=?', 'recovery_pickup:' + carrier_key(self.b, owner))
        self.assertEqual(json.loads(pickup[0]['value'])['run'], owner_run)
        self.assertEqual(self.incident()['state'], 'open', 'assigned pickup is not original-owner progress')
        rc, lines = self.w.main('recovery-check', '--card', card, '--run', str(run))
        self.assertNotEqual(rc, 0)
        self.b.worker_comment(owner, 'integrator',
                              'Read source %s run %d: ready without pickup; no source transition. Admission: %s'
                              % (card, run, lines))
        source_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        source_task = self.b.cards[card]['task']
        source_task.update(current_run_id=source_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        source_start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def source_probe(pid):
            source_task['last_heartbeat_at'] = self.w.clock() // 1000
            return source_start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=source_probe):
            self.scan_minutes(6)
        self.assertEqual(self.unchanged_history(inc)['state'], 'resumed')
        self.assertEqual(self.assigned_recoveries(), [owner])

    def test_early_answer_unclaimed_source_release_stays_owned_past_original_expiry(self):
        card, run, inc, before = self.unclaimed_answer_handoff(expired=False)
        self.assertLess(self.w.clock(), inc['abs_ms'])
        self.b.unblock(card)
        self.scan_minutes(6)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered')
        self.w.clock.now_ms = inc['abs_ms'] + 6 * 60_000
        self.scan_minutes(6)
        self.assertEqual(self.diagnostic_wait()['state'], 'answered',
                         'UNCLAIMED_ANSWER: later expiry must not manufacture an operator-choice question')
        self.assertEqual(self.incident()['state'], 'open')
        for _ in range(engine.ANSWER_STEPS + 2):
            self.w.clock.advance(4 * 60 * 60)
            self.scan_minutes(3)
            for cid in self.continuations(before).values():
                if delivered_row(self.w, self.b, cid) in ('pending', 'retry_wait'):
                    verify_delivery(self, self.w, self.b, cid)
        [owner] = self.assigned_recoveries()
        self.assertEqual((self.b.cards[owner]['task']['assignee'], self.b.cards[owner]['task']['status']),
                         ('integrator', 'ready'))
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        self.assertEqual(self.incident()['state'], 'open', 'delivery and ready source are not recovery')
        self.unchanged_history(inc)

    def test_unclaimed_answer_handoff_effect_refuses_a_new_native_dependency(self):
        card, run, inc, before = self.unclaimed_answer_handoff(expired=True)
        self.b.unblock(card)
        parent = self.b._new('New unfinished fixture prerequisite', status='blocked')
        def add_dependency():
            self.b.cards[card]['parents'].append(parent)
            # Project the actor's native graph commit, invalidating any earlier read proof.
            proof = self.b._preworker_snapshot({card, parent}, lambda: True, self.w.clock)
            self.assertIsNotNone(proof)
            proof.close()
        self.b.on_subscribe = add_dependency
        self.w.clock.advance(4 * 60 * 60)
        self.scan_minutes(6)
        self.assertIn(parent, self.b.cards[card]['parents'])
        self.assertEqual(self.answer_wakes(before), ['done', 'cancelled_stale'],
                         'fresh native prerequisite must refuse an already-queued handoff wake')
        self.w.clock.advance(48 * 60 * 60)
        self.scan_minutes(6)
        self.assertEqual(self.assigned_recoveries(), [])
        self.assertEqual(self.diagnostic_wait()['state'], 'answered')
        self.assertEqual(self.b.cards[card]['task']['status'], 'ready')
        self.unchanged_history(inc)

    def test_unclaimed_answer_handoff_refuses_unknown_source_observation(self):
        card, run, inc, before = self.unclaimed_answer_handoff(expired=True)
        self.b.unblock(card)
        real = self.b._preworker_snapshot
        def unavailable(roots, take, clock):
            return None if card in roots else real(roots, take, clock)
        with mock.patch.object(self.b, '_preworker_snapshot', side_effect=unavailable):
            self.w.clock.advance(48 * 60 * 60)
            self.scan_minutes(6)
        self.assertEqual(self.answer_wakes(before), ['done'])
        self.assertEqual(self.assigned_recoveries(), [])
        self.assertEqual(self.diagnostic_wait()['state'], 'answered')
        self.unchanged_history(inc)

    def test_operator_handoff_during_the_final_check_settles_it_without_a_hold(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.accept(inc)  # before the original deadline: the whole bounded route ends before it
        self.scan_minutes(5)
        self.operator_owned(before, card)
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        cid = self.escalated(before, card)
        sent = self.diagnostic_wait()['continuation']['last_ms']
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(3)
        self.assertLess(self.w.clock(), inc['abs_ms'])
        self.assigned_after_window()
        self.b.unblock(card)  # the operator performs the supported resume; no worker has started yet
        self.scan_minutes(90)
        self.assertEqual(self.incident()['state'], 'handed_off', 'the existing handed-off route owns it now')
        self.assertIsNone(settled_row(self.w, self.b, cid), 'no final artificial hold')
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(len(self.notice_rows()), 1)

    def test_explicit_operator_hold_pauses_assigned_dispatch_and_is_preserved(self):
        card, run, inc, before, cid, sent = self.final_window()
        self.assertEqual(self.w.main('hold', self.tid)[0], 0)
        self.w.clock.now_ms = sent + (engine.RECOVERY_DEFER_MIN + 1) * 60_000
        self.scan_minutes(90)
        self.assertEqual(self.w.task(self.tid)['hold_reason'], 'operator')
        self.assertEqual(self.assigned_recoveries(), [], 'an explicit hold is not bypassed to launch recovery')
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(self.w.main('release', self.tid)[0], 0)
        self.scan_minutes(5)
        self.assigned_owner(card, run, inc)
        self.unchanged_history(inc)

    def test_operator_working_ack_then_verified_progress_closes_the_placed_obligation(self):
        card, run, inc, before = self.answered_after_deadline()
        self.operator_owned(before, card)
        self.b.worker_comment(card, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, run))
        self.w.clock.advance(60 * 60)
        self.scan_minutes(10)
        self.operator_route_only(card)  # an ACK alone is not progress, and is not a new wake
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 1)
        self.b.unblock(card)  # the operator performs the existing supported resume; the same assignee runs live
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(6)
        self.assertEqual(self.unchanged_history(inc)['state'], 'resumed', 'verified same-owner progress settles it')
        self.w.clock.advance((engine.RECOVERY_DEFER_MIN + engine.RECOVERY_ABS_MIN) * 60)
        self.scan_minutes(10)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 1, 'no escalation after progress')

    def test_operator_new_owner_question_after_takeover_becomes_the_legitimate_human_wait(self):
        card, run, inc, before = self.answered_after_deadline()
        old = self.diagnostic_wait()
        self.operator_owned(before, card)
        q = self.question_ack(card, run, decision='The answer leaves one new choice: should option C or D be used?')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertEqual(wait['question'], q)
        self.assertNotEqual(wait['root'], old['root'])
        self.assertTrue(any(q['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)
                            if c['id'] == wait['root']), 'the new question is actually delivered and read back')
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scan_minutes(20)
        self.assertEqual(self.diagnostic_wait()['state'], 'waiting', 'a verified genuine human wait stays quiet')
        self.assertEqual(len(self.answer_wakes(before)), engine.RECOVERY_ATTEMPTS + 1)
        self.assertEqual(len(self.notice_rows()), 2)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_working_ack_after_answer_wake_defers_the_next_wake_but_not_forever(self):
        card, run, inc, before = self.answered_after_deadline()
        self.w.clock.advance((engine.RECOVERY_RETRY_MIN[1] - 10) * 60)
        self.b.worker_comment(card, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working' % (card, run))
        self.scan_minutes(30)
        self.assertEqual(len(self.answer_wakes(before)), 1, 'a fresh working ACK from the woken owner defers')
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(3)
        self.assertEqual(self.answer_wakes(before), ['done', 'done'], 'an ACK alone is not progress')
        self.assertEqual(self.alerts(card), [])

    def test_cannot_act_after_answer_wake_keeps_the_obligation_with_the_bounded_ai_route(self):
        card, run, inc, before = self.answered_after_deadline()
        self.w.clock.advance(5 * 60)
        self.b.worker_comment(card, 'coordinator',
                              'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act' % (card, run))
        self.scan_minutes(3)
        self.w.clock.advance(engine.RECOVERY_RETRY_MIN[1] * 60)
        self.scan_minutes(3)
        self.assertEqual(self.answer_wakes(before), ['done', 'done'],
                         'ANSWER_OWNERSHIP: a technical inability is intermediate; the AI route keeps the obligation')
        self.assertEqual([l for l in self.w.outputs if 'No further AI wake' in l], [])
        self.assertEqual(len(self.notice_rows()), 1, 'never a fabricated product question')
        self.unchanged_history(inc)

    def test_unknown_source_observation_defers_the_answer_wake_until_actually_observed(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        show = self.b.show
        def unavailable(cid):
            return KanbanResult(Outcome.DEFINITE_FAIL, {'error': 'unavailable'}) if cid == card else show(cid)
        with mock.patch.object(self.b, 'show', side_effect=unavailable):
            self.accept(inc)
            self.scan_minutes(10)
        self.assertEqual(self.continuations(before), {}, 'an unknown source is never a basis for a wake')
        self.assertEqual(self.diagnostic_wait()['state'], 'answered')
        self.scan_minutes(5)
        self.assertEqual(len(self.continuations(before)), 1, 'the observed source then gets its single answer wake')

    def test_owner_new_question_after_answer_wake_is_published_and_then_stays_a_quiet_human_wait(self):
        card, run, inc, before = self.answered_after_deadline()
        old = self.diagnostic_wait()
        q = self.question_ack(card, run, decision='The answer leaves one new choice: should option C or D be used?')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertEqual(wait['question'], q)
        self.assertNotEqual(wait['root'], old['root'], 'a new question must not be bound to the old answered thread')
        self.assertEqual([json.loads(r['value']) for r in self.w.q(
            "SELECT value FROM meta WHERE key=?", 'diagnostic_wait_history:%d:%s' % (inc['id'], old['key']))], [old],
            'the answered question, resolution and continuation remain history')
        self.assertTrue(any(q['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)
                            if c['id'] == wait['root']), 'the new question must actually be delivered and read back')
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(30)
        self.assertEqual(self.alerts(card), [], 'a verified genuine human wait stays quiet')
        self.assertEqual(len(self.continuations(before)), 1)
        self.assertEqual(self.w.main('recovery-check', '--card', card, '--run', str(run)),
                         (3, ['STALE diagnostic_human_wait']))

    def test_actual_same_owner_progress_after_answer_wake_closes_without_escalation(self):
        card, run, inc, before = self.answered_after_deadline()
        # The woken coordinator performs the existing supported resume; the same assignee starts a live run.
        self.b.unblock(card)
        new_run = self.b.claim(card, self.b.cards[card]['task']['assignee'])
        task = self.b.cards[card]['task']
        task.update(current_run_id=new_run, worker_pid=4242, last_heartbeat_at=self.w.clock() // 1000)
        start = str((self.b.cards[card]['runs'][-1]['started_at'] + 11644473600) * 10000000)
        def probe(pid):
            task['last_heartbeat_at'] = self.w.clock() // 1000
            return start if pid == 4242 else None
        with mock.patch.object(kp, 'process_start_time', side_effect=probe):
            self.scan_minutes(6)
            self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
            self.scan_minutes(6)
        after = self.incident()
        self.assertEqual(after['state'], 'resumed', 'verified live same-owner progress settles the obligation')
        for field in ('id', 'run_id', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], inc[field], field)
        self.assertEqual(self.alerts(card), [])
        self.assertEqual(len(self.continuations(before)), 1)

    def test_answer_before_original_deadline_wakes_at_once_without_spending_remaining_attempts(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.accept(inc)
        self.assertLess(self.w.clock(), inc['abs_ms'])
        self.scan_minutes(3)
        self.assertEqual(len(self.continuations(before)), 1, 'an accepted answer wakes the owner without delay')
        self.w.clock.now_ms = inc['detected_ms'] + 200 * 60_000  # past every remaining attempt's due time
        self.scan_minutes(3)
        self.assertEqual(len(self.answer_wakes(before)), 2, 'one bounded answer re-wake after the gap, never a burst')
        self.assertEqual(self.continuations(before, answer=False), {}, 'no answerless attempts compete with it')
        after = self.incident()
        for field in ('state', 'attempts', 'abs_ms', 'detected_ms'):
            self.assertEqual(after[field], inc[field], field)

    def test_refused_resolution_is_not_accepted_input_and_the_question_stays_quietly_unfinished(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.w.step(lambda: self.w.task(self.tid)['classified_gen'] == self.w.task(self.tid)['input_gen'] > 3,
                    world=self.life.planner(self.answer(inc, authority='changed')))
        self.scan_minutes(10)
        self.w.clock.now_ms = inc['abs_ms'] + engine.RECOVERY_DEFER_MIN * 60_000
        self.scan_minutes(10)
        self.assertEqual(self.diagnostic_wait()['state'], 'classifying')
        self.assertEqual(self.continuations(before), {})
        self.assertEqual(self.alerts(card), [])
        self.assertEqual(len(self.notice_rows()), 1)

    def test_revoked_authority_after_answer_wake_supersedes_without_escalation_or_wake(self):
        card, run, inc, before = self.answered_after_deadline()
        conn = store_conn(self.w)
        conn.execute("UPDATE approvals SET state='revoked',revoked_reason='security' WHERE id=?", (inc['approval_id'],))
        conn.close()
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.incident()['state'], 'superseded')
        self.assertEqual(self.alerts(card), [])
        self.assertEqual(len(self.continuations(before)), 1)
        self.assertEqual(self.b.cards[card]['task']['status'], 'blocked')

    def test_failed_answer_wake_takes_verified_delivery_hold_then_resumes_the_bounded_route_after_release(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60_000
        with mock.patch.object(self.b, 'complete', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.accept(inc)
            self.scan_minutes(90)
        [create] = [k for k in self.continuations(before)]
        self.assertEqual(self.answer_wakes(before), ['failed'], 'bounded delivery retries end in a recorded failure')
        hold = self.w.task(self.tid)['hold_reason']
        self.assertEqual(hold, 'outbox:' + create[:-len(':create')] + ':wake', 'the failed delivery holds the task')
        held = [r for r in self.notice_rows() if json.loads(r['body']).get('recovery_delivery_hold') == hold]
        self.assertEqual(len(held), 1, 'ANSWER_OWNERSHIP: the existing delivery-hold escalation is actually queued')
        self.assertEqual(held[0]['state'], 'done', 'and delivered')
        self.assertTrue(any(c['id'] == held[0]['result_ref'] for c in self.w.bot_comments(self.tid)), 'and read back')
        self.assertEqual(len(self.continuations(before)), 1, 'no AI wake while the delivery route is held')
        self.assertEqual(self.incident()['state'], 'open')
        self.assertEqual(self.w.main('release', self.tid)[0], 0)  # existing supported operator release
        self.scan_minutes(5)
        self.assertEqual(self.answer_wakes(before), ['failed', 'done'],
                         'ANSWER_OWNERSHIP: after release the bounded answer route continues')
        for gap in (engine.RECOVERY_RETRY_MIN[2], engine.RECOVERY_DEFER_MIN, engine.RECOVERY_ABS_MIN):
            self.w.clock.advance(gap * 60)
            self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['failed'] + ['done'] * (engine.RECOVERY_ATTEMPTS + 1),
                         'ANSWER_OWNERSHIP: the failed delivery is not consumed; the full bounded route follows')
        self.assertIn(self.TAKEOVER, self.b.cards[self.last_continuation(before)]['task']['body'])
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scan_minutes(10)
        self.assertEqual(self.answer_wakes(before), ['failed'] + ['done'] * (engine.RECOVERY_ATTEMPTS + 2), 'finite')
        self.escalated(before, card, notices=2)  # only the verified delivery-hold notice; no operator-choice question
        self.unchanged_history(inc)

    def test_owner_question_after_failed_answer_wake_hold_is_its_own_delivered_human_wait(self):
        card, run, inc = self.stopped_with_question()
        before = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60_000
        with mock.patch.object(self.b, 'complete', return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.accept(inc)
            self.scan_minutes(90)
        [held] = [r for r in self.notice_rows() if json.loads(r['body']).get('recovery_delivery_hold')]
        self.assertEqual(self.w.main('release', self.tid)[0], 0)
        self.scan_minutes(5)
        self.assertEqual(self.answer_wakes(before), ['failed', 'done'])
        old = self.diagnostic_wait()
        self.w.clock.advance(60)
        q = self.question_ack(card, run, decision='The answer leaves one new choice: should option C or D be used?')
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        wait = self.diagnostic_wait()
        self.assertEqual(wait['question'], q)
        self.assertNotIn(wait['root'], (old['root'], held['result_ref']),
                         'a new owner question is never bound to the answered question or the delivery-hold notice')
        self.assertTrue(any(q['decision'] in c['comment_text'] for c in self.w.bot_comments(self.tid)
                            if c['id'] == wait['root']), 'the new question is actually delivered and read back')
        self.assertEqual(len(self.notice_rows()), 3)

    def test_diagnostic_class_answer_uses_the_same_continuation(self):
        card, run = fallback.StalledFallbackTest.unknown_stop(self)
        inc = self.incident()
        self.assertEqual(inc['class'], 'diagnostic')
        self.question_ack(card, run)
        self.w.step(lambda: (self.diagnostic_wait() or {}).get('state') == 'waiting')
        before = self.carriers()
        self.w.clock.now_ms = inc['abs_ms'] + 60_000
        self.accept(inc)
        self.scan_minutes(5)
        [carrier] = self.continuations(before).values()
        self.assertIn(self.ANSWER, self.b.cards[carrier]['task']['body'])
        self.assertEqual(self.b.cards[carrier]['task']['status'], 'done')
        self.assertEqual(self.incident()['attempts'], inc['attempts'])


class ProgressAnswerContinuationTest(unittest.TestCase):
    """The whole-workflow (progress_stall) diagnostic uses the same accepted-answer continuation."""
    setUp = general.GeneralStallWakeTest.setUp
    scans = general.GeneralStallWakeTest.scans
    invalid_plan = general.GeneralStallWakeTest.invalid_plan
    wait = general.GeneralStallWakeTest.wait

    def test_answered_workflow_question_rewakes_boundedly_then_takeover_then_visible_escalation(self):
        self.workflow_to_escalation(receipt=False)

    def test_answered_workflow_takeover_receipt_on_its_carrier_accepts_but_is_not_progress(self):
        self.workflow_to_escalation(receipt=True)

    def workflow_to_escalation(self, receipt):
        self.invalid_plan()
        self.scans(18)
        [inc] = self.w.q('SELECT * FROM recoveries')
        [first] = self.b.by_key('clickup-dp:%s:rcv:' % self.tid)
        question = dict(task=self.tid, card=inc['card'], run=inc['run_id'], approval=None, revision=None, sha256=None,
                        fingerprint=inc['reason_sha256'], failed='The planning recipient is ambiguous.',
                        attempted='Read current task and handoff.',
                        decision='Which existing planning contact should receive the clarification, A or B?',
                        next_action='Reply with the contact, without credentials.')
        self.w.clock.advance(2)
        self.b.worker_comment(first, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=owner\n'
                              'CLICKUP-RECOVERY-QUESTION v1 %s' % (inc['card'], inc['run_id'], json.dumps(question)))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        self.w.clock.now_ms = inc['abs_ms'] + 60_000
        self.scans(3)
        before = {k for k in self.b.keys}
        self.life.say(comp.APPROVER_A, 'Use existing contact A.', mention=comp.BOT, reply_to=self.wait()['root'])
        def answer(card, ref, gen):
            self.b.park(card, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, 'Use existing contact A for the clarification.',
                                 gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(answer))
        self.scans(10)
        new = [k for k in self.b.keys if k not in before and ':rcv:' in k and k.endswith(':create')]
        self.assertEqual(len(new), 1, 'ANSWER_CONTINUATION: the accepted workflow answer must wake the owner once')
        self.assertIn(':answer-g', new[0])
        carrier = self.b.cards[self.b.keys[new[0]]]
        self.assertIn('Use existing contact A for the clarification.', carrier['task']['body'])
        self.assertEqual(carrier['task']['status'], 'done')
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['state'], after['attempts'], after['abs_ms']), ('open', inc['attempts'], inc['abs_ms']))
        notices = lambda: self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'"
                                   " ORDER BY rowid")
        published = len(notices())
        wakes = lambda: [k for k in self.b.keys if k not in before and ':rcv:' in k and k.endswith(':create')
                         and not k.endswith('-owner:create')]
        for gap in (engine.RECOVERY_RETRY_MIN[1], engine.RECOVERY_RETRY_MIN[2]):
            self.w.clock.advance(gap * 60)
            self.scans(3)
        self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS,
                         'ANSWER_OWNERSHIP: no progress re-wakes the capable AI owner within a finite budget')
        for key in wakes():
            self.assertIn('Use existing contact A for the clarification.', self.b.cards[self.b.keys[key]]['task']['body'])
        self.assertEqual(len(notices()), published)
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scans(20)
        self.assertEqual([l for l in self.w.outputs if 'No further AI wake' in l], [], 'never a passive terminal alert')
        self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS + 1,
                         'ANSWER_OPERATOR: exhausted answer wakes place the workflow with the main-chat operator')
        takeover = self.b.cards[self.b.keys[max(wakes(), key=lambda k: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', k)[0]['rowid'])]]
        self.assertIn('Operator takeover', takeover['task']['body'])
        self.assertIn('Use existing contact A for the clarification.', takeover['task']['body'])
        self.assertEqual(takeover['task']['status'], 'done', 'the takeover wake was actually delivered')
        self.assertEqual(len(notices()), published, 'ANSWER_OPERATOR: no passive ClickUp notice as the terminal step')
        self.assertEqual(self.wait()['state'], 'answered', 'never a quiet human wait')
        if receipt:  # the receiving operator accepts on this recovery carrier, where whole-workflow ACKs are posted
            self.w.clock.advance(40 * 60)
            wait = self.wait()
            self.b.worker_comment(takeover['task']['id'], 'default',
                                  'CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=working\n' % (inc['card'], inc['run_id'])
                                  + AnswerContinuationTest.RECEIPT % (inc['id'], wait['gen'], wait['relay'],
                                                                      takeover['task']['id']))
            self.w.clock.advance((engine.RECOVERY_DEFER_MIN - 30) * 60)  # past the takeover window, inside the receipt's
            show, hidden = self.b.show, takeover['task']['id']
            def unreadable(cid):
                return KanbanResult(Outcome.DEFINITE_FAIL, {'error': 'unavailable'}) if cid == hidden else show(cid)
            with mock.patch.object(self.b, 'show', side_effect=unreadable):
                self.scans(3)
            self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS + 1,
                             'ANSWER_RECEIPT: an unreadable receipt location is unknown, never "no receipt"')
            self.scans(5)
            self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS + 1, 'ANSWER_RECEIPT: the bound receipt accepts it')
        self.w.clock.advance(engine.RECOVERY_DEFER_MIN * 60)
        self.scans(20)
        self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS + 2,
                         'ANSWER_ESCALATION: an unaccepted or unprogressed takeover reaches the visible escalation')
        escalation = self.b.cards[self.b.keys[max(wakes(), key=lambda k: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', k)[0]['rowid'])]]
        self.assertIn('Operator escalation', escalation['task']['body'])
        self.assertIn('Use existing contact A for the clarification.', escalation['task']['body'])
        self.assertEqual(escalation['task']['status'], 'done', 'the escalation was actually sent')
        self.assertEqual([s['delivery_mode'] for s in escalation['subs']], ['notify+wake'], 'visible ping plus wake')
        if not receipt:  # new ClickUp input while the delivery is still unverified never cancels its verification
            self.life.say(comp.APPROVER_A, 'Any update on this?')
            self.scans(3)
        verify_delivery(self, self.w, self.b, self.b.keys[max(wakes(), key=lambda k: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', k)[0]['rowid'])])
        [summary] = [e['payload']['summary'] for e in escalation['events'] if e['kind'] == 'completed']
        self.assertTrue(summary.startswith('CLICKUP-RECOVERY ESCALATION'), summary)
        self.assertEqual(len(notices()), published, 'never a passive ClickUp notice')
        self.assertEqual(self.wait()['state'], 'answered')
        self.w.clock.advance(engine.RECOVERY_ABS_MIN * 60)
        self.scans(60)
        self.assertEqual(len(wakes()), engine.RECOVERY_ATTEMPTS + 2, 'finite and deduplicated')
        owners = [self.b.cards[c] for c in self.b.keys.values()
                  if self.b.cards[c]['task']['title'].startswith('CLICKUP-RECOVERY-OWNER v1 ')]
        self.assertEqual(len(owners), 1, 'one assigned owner for ordinary whole-workflow no-progress too')
        self.assertEqual((owners[0]['task']['assignee'], owners[0]['task']['status']), ('integrator', 'ready'))
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'no human release required for fallback launch')
        self.assertEqual(len(notices()), published, 'no manufactured hold notice')
        self.assertEqual(self.wait()['state'], 'answered')
        [after] = self.w.q('SELECT * FROM recoveries')
        self.assertEqual((after['state'], after['attempts'], after['abs_ms']), ('open', inc['attempts'], inc['abs_ms']))


class StagingAnswerContinuationTest(unittest.TestCase):
    """The approval-staging diagnostic (a logical approval anchor, not a native card) uses the same
    accepted-answer continuation, whether the stage writer keeps failing or the stage reaches READY unclaimed."""
    setUp = pre.PreworkerRecoveryTest.setUp
    applications = pre.PreworkerRecoveryTest.applications
    incidents = pre.PreworkerRecoveryTest.incidents
    carriers = pre.PreworkerRecoveryTest.carriers
    scans = pre.PreworkerRecoveryTest.scans
    stall = pre.PreworkerRecoveryTest.stall
    stopped_stage = pre.PreworkerRecoveryTest.stopped_stage
    wait = pre.PreworkerRecoveryTest.wait
    question = pre.PreworkerRecoveryTest.question

    ANSWER = 'Use existing approved account A for the notification.'

    def answered(self, late, stopped, failing=None):
        """A genuine staging question with its accepted answer, before or after the original expiry. ``stopped``
        keeps the injected stage-writer failure for the rest of the test; otherwise the normal stage path runs.
        ``failing`` instead makes every gate/orchestrator create genuinely fail while ``failing['on']``."""
        stop = self.stopped_stage()
        stop.start()
        self.scans(18)
        [inc] = self.incidents()
        self.question(inc)
        self.w.step(lambda: (self.wait() or {}).get('state') == 'waiting', limit=15)
        if not stopped:
            stop.stop()
        else:
            self.addCleanup(stop.stop)
        if failing is not None:
            create = self.b.create
            def flaky(title, **kwargs):
                if failing['on'] and (':gate:' in kwargs['idempotency_key'] or ':orch:' in kwargs['idempotency_key']):
                    return KanbanResult(Outcome.DEFINITE_FAIL, {'error': 'synthetic stage failure'})
                return create(title, **kwargs)
            patch = mock.patch.object(self.b, 'create', side_effect=flaky)
            patch.start()
            self.addCleanup(patch.stop)
        if late:
            self.w.clock.now_ms = inc['abs_ms'] + 60_000
        before = set(self.carriers())
        self.life.say(comp.APPROVER_A, 'Account A.', mention=comp.BOT, reply_to=self.wait()['root'])
        def response(card, ref, gen):
            self.b.park(card, comp.classify(ref, gen, 'answer_only'),
                        comp.out('resolution', ref, self.ANSWER, gen=gen, diagnostic=inc['id'], authority='unchanged'))
        self.w.step(lambda: (self.wait() or {}).get('state') == 'answered', world=self.life.planner(response))
        return inc, before

    def wakes(self, before):
        keys = [k for k in self.b.keys if k.startswith('clickup-dp:%s:rcv:' % self.tid) and k not in before
                and k.endswith(':create') and not k.endswith('-owner:create') and ':answer-g' in k]
        return [self.b.keys[k] for k in sorted(keys, key=lambda k: self.w.q(
            'SELECT rowid FROM outbox WHERE idem_key=?', k)[0]['rowid'])]

    def unchanged(self, inc):
        [after] = self.incidents()
        self.assertEqual((after['class'], after['attempts'], after['abs_ms'], after['detected_ms']),
                         (inc['class'], inc['attempts'], inc['abs_ms'], inc['detected_ms']),
                         'spent budget and original deadline are never refunded')
        return after

    def test_late_staging_answer_under_persistent_stage_failure_wakes_once_with_the_answer(self):
        inc, before = self.answered(late=True, stopped=True)
        self.scans(5)
        self.assertEqual(self.wakes(before), [], 'the normal stage path continues the accepted answer first')
        self.scans(7)
        wakes = self.wakes(before)
        self.assertEqual(len(wakes), 1, 'ANSWER_CONTINUATION: the accepted staging answer wakes its owner once')
        [carrier] = wakes
        shown = self.b.cards[carrier]['task']
        self.assertEqual(shown['status'], 'done', 'the answer wake was actually sent')
        self.assertIn('CLICKUP-RECOVERY approval staging v1', shown['body'], 'its existing staging template')
        self.assertIn(self.ANSWER, shown['body'], 'the accepted answer is carried to the woken owner')
        self.scans(10)
        self.assertEqual(self.wakes(before), [carrier], 'deduplicated across passes')
        self.assertEqual(self.unchanged(inc)['state'], 'open')
        self.assertEqual(self.wait()['state'], 'answered')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)

    def owners(self):
        return [cid for cid, shown in self.b.cards.items()
                if shown['task'].get('title', '').startswith('CLICKUP-RECOVERY-OWNER v1 ')]

    def route_to_owner(self, inc, before):
        """No verified stage progress: the bounded answer route ends in one assigned integrator owner."""
        for _ in range(engine.ANSWER_STEPS + 2):
            self.w.clock.advance(4 * 60 * 60)
            self.scans(3)
            for cid in self.wakes(before):
                if delivered_row(self.w, self.b, cid) in ('pending', 'retry_wait'):
                    verify_delivery(self, self.w, self.b, cid)
        wakes = self.wakes(before)
        self.assertEqual(len(wakes), engine.ANSWER_STEPS,
                         'ANSWER_OWNERSHIP: answer wakes, operator takeover and one visible escalation')
        for cid in wakes:  # an answer wake left unsent by a hold is superseded (archived) by the next one
            self.assertIn(self.b.cards[cid]['task']['status'], ('done', 'archived') if cid in wakes[:-2] else ('done',))
            self.assertIn(self.ANSWER, self.b.cards[cid]['task']['body'])
        self.assertEqual(self.b.cards[wakes[-3]]['task']['status'], 'done', 'the last answer wake was delivered')
        takeover, escalation = (self.b.cards[c] for c in wakes[-2:])
        self.assertIn('Operator takeover', takeover['task']['body'])
        self.assertIn('post on this recovery carrier', takeover['task']['body'],
                      'a logical approval anchor is never the receipt location')
        self.assertIn('Operator escalation', escalation['task']['body'])
        self.assertEqual([s['delivery_mode'] for s in escalation['subs']], ['notify+wake'])
        self.assertEqual(delivered_row(self.w, self.b, wakes[-1]), 'done')
        owners = self.owners()
        self.assertEqual(len(owners), 1, 'ASSIGNED_RECOVERY: one assigned integrator task')
        task = self.b.cards[owners[0]]['task']
        self.assertEqual((task['assignee'], task['status']), ('integrator', 'ready'))
        self.assertEqual(self.b.cards[owners[0]]['parents'], [])
        self.assertIn(self.ANSWER, task['body'])
        self.assertIn('recovery-check --card %s --run 0' % inc['card'], task['body'])
        self.assertFalse(self.w.task(self.tid)['hold_reason'], 'elapsed time is not a human-only blocker')
        self.assertEqual(self.wait()['state'], 'answered', 'never a manufactured human wait')
        self.assertEqual(self.unchanged(inc)['state'], 'open', 'an assigned owner does not settle the stage')
        self.assertEqual(self.w.main('recovery-check', '--card', inc['card'], '--run', '0')[0], 0)
        self.w.clock.advance(72 * 60 * 60)
        self.scans(10)
        self.assertEqual((self.wakes(before), self.owners()), (wakes, owners), 'finite and deduplicated')
        return owners[0]

    def test_late_staging_answer_under_persistent_stage_failure_stays_owned_to_one_assigned_owner(self):
        inc, before = self.answered(late=True, stopped=True)
        self.route_to_owner(inc, before)
        self.assertEqual(self.applications()[0]['state'], 'recorded')

    def test_early_staging_answer_under_persistent_stage_failure_stays_owned_past_original_expiry(self):
        inc, before = self.answered(late=False, stopped=True)
        self.assertLess(self.w.clock(), inc['abs_ms'])
        self.scans(12)
        [carrier] = self.wakes(before)
        self.assertIn(self.ANSWER, self.b.cards[carrier]['task']['body'], 'woken before expiry with the answer')
        self.unchanged(inc)  # the remaining ordinary attempts are neither spent nor refunded
        self.route_to_owner(inc, before)
        self.assertGreater(self.w.clock(), inc['abs_ms'])

    def ready_without_pickup(self, late):
        inc, before = self.answered(late=late, stopped=False)
        self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        self.assertEqual(self.b.cards[self.life.orch()]['runs'], [])
        return inc, before

    def test_late_staging_answer_reaching_ready_without_pickup_stays_owned_to_one_assigned_owner(self):
        inc, before = self.ready_without_pickup(late=True)
        self.route_to_owner(inc, before)
        orch = self.b.cards[self.life.orch()]
        self.assertEqual((orch['task']['status'], orch['runs']), ('ready', []), 'READY is never progress')

    def test_early_staging_answer_reaching_ready_without_pickup_stays_owned_past_original_expiry(self):
        inc, before = self.ready_without_pickup(late=False)
        self.route_to_owner(inc, before)
        self.assertGreater(self.w.clock(), inc['abs_ms'])
        self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'ready')

    def test_actual_orchestration_after_the_staging_answer_wake_settles_without_further_wakes(self):
        inc, before = self.ready_without_pickup(late=True)
        self.scans(12)
        woken = self.wakes(before)
        self.assertEqual(len(woken), 1)
        self.life.orchestrate()
        self.assertEqual(self.incidents()[0]['state'], 'resumed')
        self.assertTrue(self.b.cards[self.life.orch()]['runs'])
        self.w.clock.advance(72 * 60 * 60)
        self.scans(10)
        self.assertEqual((self.wakes(before), self.owners()), (woken, []), 'verified progress ends the route')

    def test_bound_receipt_on_the_staging_takeover_carrier_accepts_it_but_is_not_progress(self):
        inc, before = self.answered(late=True, stopped=True)
        self.scans(12)
        for gap in (engine.RECOVERY_RETRY_MIN[1], engine.RECOVERY_RETRY_MIN[2], engine.RECOVERY_DEFER_MIN):
            self.w.clock.advance(gap * 60)
            self.scans(3)
        takeover = self.wakes(before)[-1]
        self.assertEqual(len(self.wakes(before)), engine.RECOVERY_ATTEMPTS + 1)
        self.w.clock.advance(40 * 60)
        wait = self.wait()
        self.b.worker_comment(takeover, 'default', 'CLICKUP-RECOVERY-ACK v1 card=%s run=0 disposition=working\n'
                              % inc['card'] + AnswerContinuationTest.RECEIPT % (inc['id'], wait['gen'], wait['relay'],
                                                                                 takeover))
        self.w.clock.advance((engine.RECOVERY_DEFER_MIN - 30) * 60)  # past the takeover window, inside the receipt's
        self.scans(5)
        self.assertEqual(len(self.wakes(before)), engine.RECOVERY_ATTEMPTS + 1,
                         'ANSWER_RECEIPT: the bound receipt on its actual carrier accepts the takeover')
        self.w.clock.advance(60 * 60)
        self.scans(5)
        self.assertEqual(len(self.wakes(before)), engine.ANSWER_STEPS, 'acceptance without progress escalates')
        self.assertEqual(self.unchanged(inc)['state'], 'open')

    def genuinely_failing_stage(self, late, held):
        """After the accepted answer every stage create really fails and is retried. ``held``: it keeps failing
        into the existing hold, then the supported release; otherwise it clears while still retrying and the
        normal stage path reaches READY unclaimed."""
        failing = {'on': True}
        inc, before = self.answered(late=late, stopped=False, failing=failing)
        for _ in range(12 if held else 2):
            self.w.clock.advance(30 * 60)
            self.scans(3)
            if self.w.task(self.tid)['hold_reason']:
                break
        self.assertEqual(self.unchanged(inc)['state'], 'open', 'a failing stage writer is not a new obligation')
        failing['on'] = False
        if held:
            hold = self.w.task(self.tid)['hold_reason']
            self.assertTrue(hold and ':gate:' in hold, hold)
            failing['on'] = True  # the writer keeps failing; the failed row is not blindly redone
            self.assertEqual(self.w.main('release', self.tid)[0], 0)
            self.scans(5)
        else:
            self.w.step(lambda: self.life.orch() and self.b.cards[self.life.orch()]['task']['status'] == 'ready')
        return inc, before

    def test_late_answer_then_stage_failing_into_the_hold_and_release_stays_owned_to_one_assigned_owner(self):
        inc, before = self.genuinely_failing_stage(late=True, held=True)
        self.route_to_owner(inc, before)

    def test_early_answer_then_retried_stage_failure_reaching_ready_without_pickup_stays_owned(self):
        inc, before = self.genuinely_failing_stage(late=False, held=False)
        self.route_to_owner(inc, before)
        self.assertGreater(self.w.clock(), inc['abs_ms'])
        self.assertEqual(self.b.cards[self.life.orch()]['task']['status'], 'ready')

    def test_late_staging_answer_then_independent_delivery_failure_and_resend_stays_owned(self):
        from clickup_intake.store import Store
        inc, before = self.answered(late=True, stopped=True)
        key = 'clickup-dp:' + self.tid + ':independent-delivery-lifecycle'
        store = Store(self.w.state_path, self.w.clock)
        try:  # an unrelated outbound ClickUp comment of the same task genuinely fails into the existing hold
            ctx = engine._Ctx(store, self.w.clock(), engine.Observation(), engine.Config())
            ctx.comment(self.tid, key, 'Independent delivery lifecycle notification.')
        finally:
            store.close()
        for _ in range(6):
            self.w.cu.inject('POST', '/v2/task/' + self.tid + '/comment', Outcome.DEFINITE_FAIL, status=503)
        for _ in range(20):
            self.scans(1)
            [row] = self.w.q('SELECT * FROM outbox WHERE idem_key=?', key)
            if row['state'] == 'failed':
                break
            if row['next_attempt_ms'] and row['next_attempt_ms'] > self.w.clock():
                self.w.clock.now_ms = row['next_attempt_ms'] + 1000
        self.assertEqual(row['state'], 'failed')
        self.scans(12)
        self.assertTrue(self.w.task(self.tid)['hold_reason'])
        self.assertEqual(self.w.main('outbox', 'resend', key)[0], 0)
        self.scans(10)
        self.assertFalse(self.w.task(self.tid)['hold_reason'])
        self.route_to_owner(inc, before)

    def test_failed_staging_owner_creation_is_created_once_after_the_supported_release(self):
        inc, before = self.answered(late=True, stopped=True)
        self.scans(12)
        for gap in (engine.RECOVERY_RETRY_MIN[1], engine.RECOVERY_RETRY_MIN[2], engine.RECOVERY_DEFER_MIN,
                    engine.RECOVERY_DEFER_MIN):
            self.w.clock.advance(gap * 60)
            self.scans(3)
        self.assertEqual(len(self.wakes(before)), engine.ANSWER_STEPS)
        verify_delivery(self, self.w, self.b, self.wakes(before)[-1])
        create = self.b.create
        def failing(title, **kwargs):
            if title.startswith('CLICKUP-RECOVERY-OWNER v1 '):
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return create(title, **kwargs)
        with mock.patch.object(self.b, 'create', side_effect=failing):
            self.w.clock.advance((engine.RECOVERY_DEFER_MIN + 1) * 60)
            self.scans(120)
        self.assertEqual(self.owners(), [])
        hold = self.w.task(self.tid)['hold_reason']
        self.assertTrue(hold.startswith('outbox:') and hold.endswith('-owner:create'), hold)
        self.assertEqual(self.w.main('release', self.tid)[0], 0)
        self.scans(10)
        self.assertEqual(len(self.owners()), 1, 'ASSIGNED_REDO: the failed owner step is redone after release')
        owner = self.owners()[0]
        self.assertEqual(self.b.cards[owner]['task']['status'], 'ready')
        self.w.clock.advance(72 * 60 * 60)
        self.scans(10)
        self.assertEqual(self.owners(), [owner], 'finite and deduplicated')
        self.assertEqual(self.unchanged(inc)['state'], 'open')


if __name__ == '__main__':
    unittest.main()
