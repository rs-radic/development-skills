"""Offline stalled-work regressions through real main.scan/progress_pass/evaluate/execute.

All ports are fakes; temporary databases/files stay under tests/.tmp in the isolated lane.
"""
import hashlib
import json
import unittest
from unittest import mock

from clickup_intake import engine, main
from clickup_intake.contracts import KanbanResult, Outcome
from clickup_intake.config import load_config
from clickup_intake.store import Store
from test_recovery import developing_world, store_conn
from test_s4_composition import Crash


class StalledFallbackTest(unittest.TestCase):
    def setUp(self):
        self.w, self.life = developing_world(self)
        self.b, self.tid = self.w.board, self.life.tid
        self.w.main("recovery", "enable")
        self.w.clock.advance(5)

    def carriers(self):
        return self.b.by_key("clickup-dp:%s:rcv:" % self.tid)

    def scan_minutes(self, minutes):
        for _ in range(minutes):
            self.w.clock.advance(61)
            self.w.run()

    def test_captured_ticket_diagnostic_shape_wakes_without_relaxing_ticket_retry(self):
        self.life.e2e_done_to_ticketed()
        card = self.life.promo
        run = self.b.claim(card, "integrator")
        # Real captured event shape: free prose does not match the narrow typed lease parser.
        reason = ("ClickUp approval ticket INVALID: lease_expired (ticket-check for gate %s, task %s, rev 1 "
                  "returned exit 3). Renew the existing ticket and resume this same card; no dev promotion, "
                  "IIS/database changes or release delivery occurred. Preflight evidence is attached and "
                  "owned staging removed." % (self.life.gate(), self.tid))
        self.b.block(card, "needs_input", reason)
        conn = store_conn(self.w)
        conn.execute("UPDATE tasks SET phase='promotion_held' WHERE clickup_task_id=?", (self.tid,))
        conn.close()
        mark = len(self.b.mutations())
        self.scan_minutes(13)
        self.assertEqual(len(self.carriers()), 1, "persistently stranded current approval must wake diagnostics")
        carrier = self.b.cards[self.carriers()[0]]
        self.assertEqual(carrier["task"]["status"], "done")
        self.assertIsNone(carrier["task"]["assignee"])
        self.assertEqual([s["delivery_mode"] for s in carrier["subs"]], ["wake"])
        self.assertIn("diagnostic", carrier["task"]["body"].lower())
        self.assertEqual(self.b.cards[card]["task"]["status"], "blocked")
        self.assertEqual(self.b.cards[card]["runs"][-1]["id"], run)
        self.assertEqual([m for m in self.b.mutations()[mark:] if m[1] == card], [])
        self.scan_minutes(10)
        self.assertEqual(len(self.carriers()), 1)

    def unknown_stop(self):
        card = self.life.fe
        run = self.b.claim(card, "e2e-tester")
        self.b.block(card, "new_technical_stop", "A previously unseen technical stop.")
        self.scan_minutes(18)
        return card, run

    def test_generic_check_is_fresh_readonly_and_carrier_does_not_expand_operator_authority(self):
        card, run = self.unknown_stop()
        self.assertEqual(len(self.carriers()), 1)
        before = list(self.b.mutations())
        rc, lines = self.w.main("recovery-check", "--card", card, "--run", str(run))
        self.assertEqual(rc, 0, lines)
        self.assertIn("class=diagnostic", lines[0])
        body = self.b.cards[self.carriers()[0]]["task"]["body"]
        self.assertIn("Diagnostic admission is NOT approval", body)
        self.assertIn("never complete a question", body)
        self.assertIn("unsupported code repair", body)
        self.b.cards[card]["task"]["status"] = "running"
        self.assertNotEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)
        self.assertEqual(self.b.mutations(), before)

    def incorporated_question(self, marker=True):
        """Frozen accepted-state shape: material closure incorporated by approval, no unchanged resolution.
        This is fixture state, not a simulated approval bypass or live recovery.
        """
        card = self.life.fe
        run = self.b.claim(card, "implementor")
        self.b._end(card, "blocked")
        self.b.cards[card]["task"].update(status="todo", block_kind="dependency")
        question = self.b._new("CLICKUP-QUESTION v1 " + card, status="blocked", assignee="clickup-intake")
        self.b.cards[card]["parents"].append(question)
        self.b._ev(card, "dependency_wait", run, reason="CLICKUP-QUESTION " + question,
                   kind="dependency", source_status="ready")
        conn = store_conn(self.w)
        a = dict(conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 1").fetchone())
        gen = a["accounts_gen"]
        conn.execute("INSERT INTO blockers(id,task,question_card,card,run_id,created_ms,approval_id,state,closure_gen,"
                     " pending_relay,origin_lane) VALUES (6,?,?,?,?,?,?,'scope_change',?,'material-relay','work')",
                     (self.tid, question, card, run, self.w.clock(), 0, gen))
        req = self.life.req_cards()[-1]
        plan = "## ClickUp summary\nFixture approved amendment\n"
        if marker:
            plan += "CLICKUP-INCORPORATES v1 blocker=6 question=%s card=%s gen=%d\n" % (question, card, gen)
        sha = hashlib.sha256(plan.encode()).hexdigest()
        for att in self.b.cards[req]["atts"]:
            if att["filename"] == "clickup-plan-r1.md":
                with open(att["stored_path"], "w", encoding="utf-8", newline="\n") as f:
                    f.write(plan)
        for r in self.b.cards[req]["runs"]:
            if isinstance(r.get("metadata"), dict) and "plan_sha256" in r["metadata"]:
                r["metadata"]["plan_sha256"] = sha
        g = json.loads(a["clickup_gates"])
        g["plan_sha256"] = sha
        conn.execute("UPDATE approvals SET plan_sha256=?,clickup_gates=? WHERE id=?", (sha, json.dumps(g), a["id"]))
        conn.execute("UPDATE tasks SET published_sha256=? WHERE clickup_task_id=?", (sha, self.tid))
        conn.close()
        return card, question, run

    def test_approved_material_question_missing_transition_wakes_without_completing_question(self):
        card, question, run = self.incorporated_question()
        before = list(self.b.mutations())
        self.scan_minutes(18)
        self.assertEqual(len(self.carriers()), 1, "accepted incorporation is not still awaiting a human")
        self.assertEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)
        self.assertEqual(self.b.cards[question]["task"]["status"], "blocked")
        self.assertEqual(self.b.cards[card]["task"]["status"], "todo")
        self.assertEqual([m for m in self.b.mutations()[len(before):] if m[1] in (card, question)], [])
        self.assertIsNone(self.w.q("SELECT resolution_body FROM blockers WHERE id=6")[0]["resolution_body"])

    def test_genuinely_unanswered_question_is_quiet(self):
        self.incorporated_question(marker=False)
        self.scan_minutes(18)
        self.assertEqual(self.carriers(), [])

    def notice_rows(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='cu_comment' AND idem_key LIKE '%:recovery-notice:%'")

    def test_cannot_act_is_unfinished_until_same_task_clickup_readback_then_deduped(self):
        card, run = self.unknown_stop()
        self.b.worker_comment(card, "default", "CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act\n"
                              "raw secret path C:/private/credential.dpapi host.example.test 192.0.2.1" % (card, run))
        self.w.step(self.notice_rows)
        [notice] = self.notice_rows()
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open",
                         "a successful POST is not verified comment delivery")
        self.assertEqual(notice["target"], self.tid)
        text = json.loads(notice["body"])["text"]
        for unsafe in ("C:/", "192.0.2.1", "host.example", "credential.dpapi"):
            self.assertNotIn(unsafe, text)
        self.assertIn("No new product approval", text)
        self.assertIn("integration operator", text)
        self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")
        posted = [c for c in self.w.bot_comments(self.tid) if text in c["comment_text"]]
        self.assertEqual(len(posted), 1)
        self.assertEqual(self.notice_rows()[0]["result_ref"], posted[0]["id"])
        self.scan_minutes(15)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.carriers()), 1)


    def test_missing_observation_at_escalation_deadline_defers_all_effects(self):
        card, question, run = self.incorporated_question()
        self.scan_minutes(18)
        [inc] = self.w.q("SELECT * FROM recoveries")
        mark = len(self.b.mutations())
        self.w.clock.now_ms = inc["abs_ms"] + 1
        real = self.b.show
        def show(cid):
            return KanbanResult(Outcome.DEFINITE_FAIL) if cid == question else real(cid)
        with mock.patch.object(self.b, "show", side_effect=show):
            self.scan_minutes(6)
        self.assertEqual(self.notice_rows(), [], "unknown obligations must not become an owner escalation")
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open")
        self.assertEqual(len(self.b.mutations()), mark)

    def test_admission_rereads_current_authority_after_native_reads(self):
        card, run = self.unknown_stop()
        real = self.b.show
        changed = [False]
        def show(cid):
            result = real(cid)
            if not changed[0]:
                changed[0] = True
                conn = store_conn(self.w)
                conn.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (self.tid,))
                conn.close()
            return result
        with mock.patch.object(self.b, "show", side_effect=show):
            rc, lines = self.w.main("recovery-check", "--card", card, "--run", str(run))
        self.assertNotEqual(rc, 0, lines)

    def test_heartbeat_noise_does_not_reset_the_incident_or_bypass_cooldown(self):
        card = self.life.fe
        run = self.b.claim(card, "tester")
        self.b.block(card, "unrecognized_kind", "An unknown stop, not a message pattern.")
        for i in range(20):
            self.b._ev(card, "heartbeat", run, note="changing heartbeat %d" % i)
            self.scan_minutes(1)
        self.assertEqual(len(self.carriers()), 1)
        [inc] = self.w.q("SELECT * FROM recoveries")
        self.w.clock.now_ms = inc["detected_ms"] + 59 * 60_000
        self.w.run()
        self.assertEqual(len(self.carriers()), 1)
        self.w.clock.now_ms = inc["detected_ms"] + 62 * 60_000
        self.scan_minutes(5)
        self.assertEqual(len(self.carriers()), 2)
        self.w.clock.now_ms = inc["detected_ms"] + 182 * 60_000
        self.scan_minutes(5)
        self.assertEqual(len(self.carriers()), 3)
        self.w.clock.now_ms = inc["detected_ms"] + 362 * 60_000
        self.scan_minutes(5)
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.carriers()), 3)


    def queue_notice(self, typed=False):
        if typed:
            card = self.life.fe
            run = self.b.claim(card, "tester")
            self.b.block(card, "capability", "Owner-only operation cannot be completed by this worker.")
            self.scan_minutes(6)
        else:
            card, run = self.unknown_stop()
        self.b.worker_comment(card, "default", "CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act" %
                              (card, run))
        with mock.patch.object(main.Glue, "clickup", return_value=False):
            self.w.step(self.notice_rows)
        return card, run, self.notice_rows()[0]["idem_key"]

    def test_existing_typed_recovery_uses_same_originating_verified_result(self):
        self.queue_notice(typed=True)
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open")
        self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")
        self.assertEqual(len(self.notice_rows()), 1)

    def test_post_send_crash_reconciles_once_and_requires_observed_delivery(self):
        _, _, key = self.queue_notice()
        real = self.w.transport.request
        crashed = [False]
        path = "/v2/task/%s/comment" % self.tid
        before = self.w.cu.count("POST", path)
        def request(method, target, **kw):
            result = real(method, target, **kw)
            if method == "POST" and target == path and not crashed[0]:
                crashed[0] = True
                raise Crash("after originating notice POST, before local acknowledgment")
            return result
        with mock.patch.object(self.w.transport, "request", side_effect=request):
            self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")
        self.assertTrue(crashed[0])
        self.assertEqual(self.w.cu.count("POST", path) - before, 1)
        self.assertEqual(self.notice_rows()[0]["state"], "done")

    def test_ambiguous_unsent_notice_remains_unfinished_alerted_and_not_resent(self):
        _, _, key = self.queue_notice()
        path = "/v2/task/%s/comment" % self.tid
        before = self.w.cu.count("POST", path)
        self.w.cu.inject("POST", path, Outcome.AMBIGUOUS, commit=False)
        self.w.step(lambda: self.notice_rows()[0]["state"] == "ambiguous")
        self.scan_minutes(35)
        self.assertEqual(self.notice_rows()[0]["state"], "held_ambiguous")
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open")
        self.assertEqual(self.w.cu.count("POST", path) - before, 1)
        self.assertTrue(any("still ambiguous" in line for line in self.w.outputs))
        self.assertEqual(len(self.carriers()), 1)
        self.assertEqual(self.w.main("outbox", "abandon", key)[0], 0)
        self.scan_minutes(10)
        self.assertEqual(self.notice_rows()[0]["outcome"], "ABANDONED")
        self.assertEqual(self.w.cu.count("POST", path) - before, 1)

    def test_definite_delivery_failures_preserve_unfinished_notice_and_discord_fallback(self):
        self.queue_notice()
        path = "/v2/task/%s/comment" % self.tid
        for _ in range(6):
            self.w.cu.inject("POST", path, Outcome.DEFINITE_FAIL, status=503)
        self.scan_minutes(65)
        self.assertEqual(self.notice_rows()[0]["state"], "failed")
        self.assertEqual(self.notice_rows()[0]["attempts"], 6)
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open")
        self.assertTrue(any("failed after 6 attempts" in line for line in self.w.outputs))
        self.assertEqual(len(self.carriers()), 1)

    def test_abandoned_wake_is_not_replaced_by_later_automatic_attempt(self):
        card, run = self.unknown_stop()
        key = "clickup-dp:%s:rcv:%s:%d:1:wake" % (self.tid, card, run)
        conn = store_conn(self.w)
        conn.execute("UPDATE outbox SET state='failed' WHERE idem_key=?", (key,))
        conn.close()
        self.assertEqual(self.w.main("outbox", "abandon", key)[0], 0)
        self.w.clock.advance(70 * 60)
        self.scan_minutes(6)
        self.assertEqual(len(self.carriers()), 1, "abandon is an operator stop, not permission for a new key")
        self.assertNotEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)


    def test_pending_newer_input_and_quiet_window_cannot_admit_diagnostic_action(self):
        card, run = self.unknown_stop()
        conn = store_conn(self.w)
        conn.execute("UPDATE tasks SET quiet_until_ms=? WHERE clickup_task_id=?", (self.w.clock() + 180_000, self.tid))
        conn.close()
        rc, lines = self.w.main("recovery-check", "--card", card, "--run", str(run))
        self.assertNotEqual(rc, 0, lines)
        conn = store_conn(self.w)
        conn.execute("UPDATE tasks SET input_gen=input_gen+1 WHERE clickup_task_id=?", (self.tid,))
        conn.close()
        self.assertNotEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)

    def test_stale_wake_after_subscribe_never_wakes_the_coordinator(self):
        card = self.life.fe
        self.b.claim(card, "tester")
        self.b.block(card, "unknown_stop", "New unknown technical failure")
        self.b.on_subscribe = lambda: self.b.cards[card]["task"].update(status="review")
        self.scan_minutes(18)
        [carrier] = self.carriers()
        self.assertFalse([e for e in self.b.cards[carrier]["events"] if e["kind"] == "completed"])

    def test_normal_queue_running_review_and_missing_native_inputs_stay_quiet(self):
        card = self.life.fe
        original = self.b.show(card).json
        for status in ("todo", "ready", "running", "review", "done", "archived"):
            with self.subTest(status=status):
                self.b.cards[card].update({k: json.loads(json.dumps(v)) for k, v in original.items()})
                self.b.cards[card]["task"]["status"] = status
                self.scan_minutes(18)
                self.assertEqual(self.carriers(), [])


    def test_failed_carrier_delivery_queues_originating_notice_without_releasing_source(self):
        card = self.life.fe
        run = self.b.claim(card, "tester")
        self.b.block(card, "unknown_stop", "A technical stop with no matching error pattern")
        with mock.patch.object(self.b, "complete", return_value=KanbanResult(Outcome.DEFINITE_FAIL)):
            self.scan_minutes(70)
        self.assertEqual(len(self.notice_rows()), 1, "exhausted AI wake delivery is itself an unfinished escalation")
        self.assertEqual(self.b.cards[card]["task"]["status"], "blocked")
        self.assertTrue(self.w.task(self.tid)["hold_reason"], "preserve the existing transport-failure hold")
        self.w.step(lambda: any("Approved plan r1 remains stopped" in c["comment_text"] for c in self.w.bot_comments(self.tid)))
        self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")
        self.assertTrue(self.w.task(self.tid)["hold_reason"], "verified notice never clears execution hold")
        self.assertEqual(len(self.carriers()), 1)


    def test_notice_retry_rechecks_native_work_instead_of_sending_stale_escalation(self):
        card, run, key = self.queue_notice()
        path = "/v2/task/%s/comment" % self.tid
        self.w.cu.inject("POST", path, Outcome.DEFINITE_FAIL, status=503)
        self.w.step(lambda: self.notice_rows()[0]["state"] == "retry_wait")
        count = self.w.cu.count("POST", path)
        self.b.cards[card]["task"]["status"] = "running"
        self.scan_minutes(8)
        self.assertEqual(self.w.cu.count("POST", path), count, "healthy work cannot inherit a queued escalation")
        self.assertNotEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "escalated")

    def test_generic_admission_preserves_terminal_hold_revocation_and_newer_approval(self):
        card, run = self.unknown_stop()
        changes = ["UPDATE tasks SET terminal=1", "UPDATE tasks SET hold_reason='operator'",
                   "UPDATE approvals SET state='revoked',revoked_reason='security'",
                   "UPDATE tasks SET scope_gen=100", "UPDATE tasks SET classified_gen=0"]
        conn = store_conn(self.w)
        original_task = dict(conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (self.tid,)).fetchone())
        original_a = dict(conn.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 1").fetchone())
        for sql in changes:
            with self.subTest(sql=sql):
                conn.execute(sql)
                self.assertNotEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)
                conn.execute("UPDATE tasks SET terminal=?,hold_reason=?,scope_gen=?,classified_gen=? WHERE clickup_task_id=?",
                             tuple(original_task[k] for k in ("terminal", "hold_reason", "scope_gen", "classified_gen")) + (self.tid,))
                conn.execute("UPDATE approvals SET state=?,revoked_reason=? WHERE id=?",
                             (original_a["state"], original_a["revoked_reason"], original_a["id"]))
        conn.execute("INSERT INTO approvals(task,rev,accounts_gen,plan_sha256,state) VALUES (?,2,100,?,'recorded')",
                     (self.tid, "a" * 64))
        self.assertNotEqual(self.w.main("recovery-check", "--card", card, "--run", str(run))[0], 0)
        conn.close()


    def test_missing_worker_behind_approved_question_is_diagnostic_not_a_normal_queue(self):
        card, question, _ = self.incorporated_question()
        self.b.cards[card]["runs"] = []
        self.b.cards[card]["task"]["created_at"] = self.w.clock() // 1000
        self.b.cards[card]["events"] = [{"kind": "created", "created_at": self.w.clock() // 1000,
                                         "run_id": None, "payload": {"status": "todo"}}]
        self.scan_minutes(18)
        self.assertEqual(len(self.carriers()), 1)
        [inc] = self.w.q("SELECT * FROM recoveries")
        self.assertEqual(inc["run_id"], 0)
        self.assertEqual(self.w.main("recovery-check", "--card", card, "--run", "0")[0], 0)
        self.assertEqual(self.b.cards[question]["task"]["status"], "blocked")

    def test_incomplete_question_observation_cannot_be_inferred_from_an_approved_marker(self):
        card, question, run = self.incorporated_question()
        self.scan_minutes(18)
        self.b.cards[question]["task"]["status"] = None
        rc, lines = self.w.main("recovery-check", "--card", card, "--run", str(run))
        self.assertNotEqual(rc, 0, lines)


    def test_precise_allowlisted_diagnosis_reaches_clickup_without_raw_ack_prose(self):
        card, run = self.unknown_stop()
        ack = ("CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=cannot_act\n"
               "CLICKUP-RECOVERY-DETAIL v1 failed=missing_transition attempted=inspection next=integration_repair\n"
               "Never relay raw C:/private/log.txt 192.0.2.10 customer=synthetic-value" % (card, run))
        self.b.worker_comment(card, "default", ack)
        self.w.step(self.notice_rows)
        text = json.loads(self.notice_rows()[0]["body"])["text"]
        self.assertIn("approved continuation is missing a supported workflow transition", text)
        self.assertIn("read-only state and approval inspection", text)
        self.assertIn("separately authorized and independently reviewed integration repair", text)
        self.assertNotIn("synthetic-value", text)
        self.assertNotIn("192.0.2.10", text)
        self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")


    def test_rollback_refuses_ambiguous_originating_notice_even_after_recovery_disable(self):
        self.queue_notice()
        path = "/v2/task/%s/comment" % self.tid
        self.w.cu.inject("POST", path, Outcome.AMBIGUOUS)
        self.w.step(lambda: self.notice_rows()[0]["state"] == "ambiguous")
        self.assertEqual(self.w.main("recovery", "disable")[0], 0)
        rc, lines = self.w.main("recovery", "rollback-check")
        self.assertNotEqual(rc, 0, lines)
        self.assertTrue(any("notice" in line for line in lines))

    def test_failed_readback_of_accepted_post_is_unfinished_until_observed_and_never_resent(self):
        self.queue_notice()
        self.w.step(lambda: self.notice_rows()[0]["state"] == "done")
        path = "/v2/task/%s/comment" % self.tid
        count = self.w.cu.count("POST", path)
        for _ in range(12):
            self.w.cu.inject("GET", path, Outcome.DEFINITE_FAIL, status=503)
        self.scan_minutes(8)
        self.assertEqual(self.w.q("SELECT state FROM recoveries")[0]["state"], "open")
        self.assertEqual(self.w.cu.count("POST", path), count)
        self.w.cu.faults.clear()
        self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] == "escalated")
        self.assertEqual(self.w.cu.count("POST", path), count)


    def test_a_known_healthy_interval_resets_persistent_stall_grace(self):
        card = self.life.fe
        self.b.claim(card, "tester")
        self.b.block(card, "unknown_stop", "Technical stop")
        self.scan_minutes(6)
        self.b.cards[card]["task"]["status"] = "ready"
        self.scan_minutes(8)
        self.assertEqual(self.carriers(), [])
        self.b.cards[card]["task"]["status"] = "blocked"
        self.scan_minutes(6)
        self.assertEqual(self.carriers(), [], "a healthy interval is not continuous stall evidence")
        self.scan_minutes(12)
        self.assertEqual(len(self.carriers()), 1)

    def test_notice_send_rejects_a_newer_current_approval(self):
        _, _, key = self.queue_notice()
        store = Store(self.w.state_path, self.w.clock)
        self.addCleanup(store.close)
        with store.transaction():
            store.conn.execute("INSERT INTO approvals(task,rev,accounts_gen,plan_sha256,state) VALUES (?,2,100,?,'recorded')",
                               (self.tid, "a" * 64))
        cfg = load_config(self.w.runtime + "/config.json")
        glue = main.Glue(cfg, store, self.w.transport, self.b, self.w.clock())
        self.assertNotEqual(glue.recovery_notice_gate(self.w.q("SELECT id FROM recoveries")[0]["id"]), "go")


if __name__ == "__main__":
    unittest.main()
