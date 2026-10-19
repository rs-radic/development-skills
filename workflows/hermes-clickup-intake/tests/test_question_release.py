"""ClickUp human-blocker question release (plan r12 over r11/r9/r6): fence cadence and release-side behaviour."""

import json
import os
import shutil
import tempfile
import unittest

from clickup_intake import engine, observe
from clickup_intake.config import Config
from clickup_intake.contracts import Preconditions
from clickup_intake.store import Store
from fakes.fake_clickup import FakeClickUp
from fakes.fake_clock import FakeClock

T0 = 1_800_000_000_000
TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


class FenceCadenceTest(unittest.TestCase):
    """r11 §3.3 / F18: a task with an ``open`` or ``releasing`` fence joins the ``live`` cadence set (a light
    pass every scan, a full pass every 300 s); r12 §3.3: parks do not."""

    def setUp(self):
        self.clock = FakeClock(T0)
        self.cfg = Config(cutover_ms=T0 - 3_600_000)
        self.fake = FakeClickUp(self.clock)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(os.path.join(self.dir, "state.sqlite3"), self.clock)
        self.addCleanup(self.store.close)
        self.store.ensure_relay_tables()
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        self.scan()  # admitted: quiet starts
        self.store.observe_commit("t1", [], {"phase": "planning"})
        self.clock.advance(200)  # quiet expired
        self.scan()  # barrier F + L: nothing is due afterwards for 300 s (light) / 900 s (full)
        self.clock.advance(60)

    def scan(self):
        return observe.observe_scan(self.fake, self.store, self.cfg, self.clock())

    def fence(self, state, kind="fence"):
        self.store.conn.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, state, updated_ms)"
                                " VALUES ('t1', 'W', 'F1', ?, 1, ?, 1)", (kind, state))

    def task_calls(self):
        self.fake.calls.clear()
        self.scan()
        return [c[1] for c in self.fake.calls if c[1].startswith("/v2/task/t1")]

    def test_control_no_fence_no_pass_before_the_light_interval(self):
        self.assertEqual(self.task_calls(), [])

    def test_open_fence_gets_a_light_pass_every_scan(self):
        self.fence("open")
        self.assertTrue(self.task_calls())

    def test_releasing_fence_gets_a_light_pass_every_scan(self):
        self.fence("releasing")
        self.assertTrue(self.task_calls())

    def test_released_fence_and_park_do_not_join_the_live_set(self):
        self.fence("released")
        self.store.conn.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, state, updated_ms)"
                                " VALUES ('t1', 'W', 'P1', 'park', 1, 'open', 1)")
        self.assertEqual(self.task_calls(), [])

    def test_open_fence_full_pass_at_300_seconds_not_900(self):
        self.fence("open")
        self.clock.advance(250)  # 310 s after the barrier full pass
        self.fake.calls.clear()
        self.scan()
        comments = [c for c in self.fake.calls if c[1] == "/v2/task/t1/comment"]
        self.assertGreaterEqual(len(comments), 2, self.fake.calls)  # L newest page + F paging

    def test_store_without_relay_tables_still_scans(self):
        s = Store(os.path.join(self.dir, "plain.sqlite3"), self.clock)
        self.addCleanup(s.close)
        observe.observe_scan(self.fake, s, self.cfg, self.clock())


class DisableTest(unittest.TestCase):
    """r9 §7 / r11 §9 / r12 §9 ``blockers disable`` (engine side): the cutover is deleted; unsent blocker rows
    except ``complete Q`` are cancelled; every open or releasing fence gets a barrier-free ``complete F``; parks
    are not cancelled and keep being evaluated; operator alerts per open Q and per delivered blocker."""

    from test_question_relay import FenceReleaseTest as _F, ParkReleaseTest as _P
    setUp, cutover, link, rows, ask, answer, close, closed, resolve, fence, release_rows = (
        _F.setUp, _F.cutover, _F.link, _F.rows, _F.ask, _F.answer, _F.close, _F.closed, _F.resolve, _F.fence,
        _F.release_rows)
    TEXT, F, P = _F.TEXT, _F.F, _P.P
    park = _P.park

    def disable(self):
        new = engine.disable_blockers(self.h.store, self.h.now)
        self.h.intents.extend(new)
        return new

    def meta(self):
        return self.h.store.conn.execute("SELECT value FROM meta WHERE key='blocker_cutover_ms'").fetchone()

    def alerts(self):
        return [json.loads(r["body"])["text"] for r in self.h.rows("alert")]

    def test_an_open_fence_is_released_without_a_barrier_and_the_cutover_is_deleted(self):
        self.resolve()
        f = self.fence(ahead_s=1000)  # the barrier is not met yet
        self.assertEqual((f["state"], self.release_rows()), ("open", []))
        self.disable()
        self.assertIsNone(self.meta())
        tid = self.h.task()["clickup_task_id"]
        [row] = self.release_rows()
        self.assertEqual(row["idem_key"], "clickup-dp:%s:fence:%s:release:disable" % (tid, self.F))
        self.assertNotIn("barrier_anchor_ms", json.loads(row["body"]))
        self.assertIsNone(json.loads(row["preconditions"])["input_gen"])
        self.assertEqual(self.rows("fences")[0]["state"], "releasing")
        self.h.cycle()
        self.assertEqual(self.rows("fences")[0]["state"], "released")
        self.assertEqual(self.h.cards[self.F]["task"]["status"], "done")

    def releasing(self):
        self.resolve()
        self.fence(ahead_s=1000)
        self.h.settle(seconds=1000)
        self.h.scan()  # evaluate only: the barriered release row is queued but not sent
        [f] = self.rows("fences")
        self.assertEqual((f["state"], self.h.store.conn.execute("SELECT state FROM outbox WHERE idem_key=?",
                                                                (f["release_key"],)).fetchone()[0]),
                         ("releasing", "pending"))
        return f

    def test_an_unsent_barriered_release_is_replaced_by_the_barrier_free_one(self):
        f = self.releasing()
        self.disable()
        states = {r["idem_key"]: r["state"] for r in self.release_rows()}
        tid = self.h.task()["clickup_task_id"]
        key = "clickup-dp:%s:fence:%s:release:disable" % (tid, self.F)
        self.assertEqual(states, {f["release_key"]: "cancelled_stale", key: "pending"})
        self.assertEqual(self.rows("fences")[0]["release_key"], key)

    def test_a_possibly_sent_release_is_left_to_the_readback(self):
        f = self.releasing()
        self.h.store.conn.execute("UPDATE outbox SET state='sending' WHERE idem_key=?", (f["release_key"],))
        self.disable()
        self.assertEqual([r["idem_key"] for r in self.release_rows()], [f["release_key"]])
        self.assertEqual(self.rows("fences")[0], f)

    def test_a_refused_disable_release_is_sent_again_while_disabled(self):  # r11 §9, r6 §7; B2
        from clickup_intake import main as glue_main
        self.resolve()
        self.fence(ahead_s=1000)  # the barrier is never met here: the disable release needs none
        self.disable()
        [row] = self.release_rows()
        self.h.store.conn.execute("UPDATE outbox SET state='cancelled_stale' WHERE idem_key=?", (row["idem_key"],))
        self.h.scan()  # evaluate only: the refusal is re-evaluated, the fence is open again
        self.assertEqual(self.rows("fences")[0]["state"], "open")
        for _ in range(2):
            self.h.cycle()  # offered again without a barrier, sent once and confirmed
        self.assertEqual((self.rows("fences")[0]["state"], self.h.cards[self.F]["task"]["status"]),
                         ("released", "done"))
        self.assertEqual([(r["idem_key"], r["state"]) for r in self.release_rows()], [(row["idem_key"], "done")])
        self.assertFalse([r for r in glue_main.rollback_preflight(self.h.store) if "fence" in r])

    def test_disable_again_re_arms_a_refused_disable_release(self):  # r11 §9, B2 (writer)
        for reopened in (True, False):
            with self.subTest(reopened=reopened):
                self.setUp()
                self.resolve()
                self.fence(ahead_s=1000)
                self.disable()
                [row] = self.release_rows()
                self.h.store.conn.execute("UPDATE outbox SET state='cancelled_stale' WHERE idem_key=?",
                                          (row["idem_key"],))
                self.h.store.conn.commit()
                if reopened:
                    self.h.scan()  # evaluate only: the refusal reopened the fence
                    self.assertEqual(self.rows("fences")[0]["state"], "open")
                self.disable()
                [again] = self.release_rows()
                self.assertEqual((again["idem_key"], again["state"]), (row["idem_key"], "pending"))
                self.assertEqual((self.rows("fences")[0]["state"], self.rows("fences")[0]["release_key"]),
                                 ("releasing", row["idem_key"]))
                self.h.cycle()
                self.assertEqual(self.rows("fences")[0]["state"], "released")

    def test_a_delivered_blocker_gets_the_correction_alert(self):
        b, _ = self.resolve()
        self.disable()
        self.assertIn("ClickUp relay disabled: later ClickUp corrections to the answer for %s (question %s) are no"
                      " longer delivered and its resumption is not reported; comment any correction on %s, or"
                      " re-enable." % (b["card"], b["question_card"], b["card"]), self.alerts())

    def test_an_open_question_gets_the_operator_answer_alert(self):
        b = self.answer(self.ask())
        self.disable()
        self.assertIn("ClickUp relay disabled: answer %s by completing it with the decision as its summary, or"
                      " re-enable." % b["question_card"], self.alerts())

    def test_the_disable_alert_repeats_the_park_sentence(self):
        self.cutover()
        self.disable()
        self.assertIn("ClickUp relay disabled: an operator releases a park card by completing it only when"
                      " `kanban show <its card>` shows no current run.", self.alerts())

    def test_unsent_blocker_rows_are_cancelled_except_complete_q(self):
        self.cutover()
        tid = self.h.task()["clickup_task_id"]
        ctx = engine._Ctx(self.h.store, self.h.now, engine.Observation(), self.h.cfg)
        keys = {"clickup-dp:%s:blocker:9:answer:g4" % tid: ("kb_complete", "pending", "pending"),
                "clickup-dp:%s:blocker:9:corr:g5" % tid: ("kb_comment", "pending", "cancelled_stale"),
                "clickup-dp:%s:bq:technical:9:abcdef012345" % tid: ("cu_comment", "retry_wait", "cancelled_stale"),
                "clickup-dp:%s:bq:technical:9:abcdef012345#cu" % tid: ("cu_comment", "pending", "cancelled_stale"),
                "clickup-dp:%s:blocker:9:x" % tid: ("kb_comment", "sending", "sending"),
                "clickup-dp:%s:out:1" % tid: ("kb_comment", "pending", "pending"),
                "clickup-dp:alert:blocker:9:x": ("alert", "pending", "pending")}
        with self.h.store.transaction():
            for k, (kind, state, _) in keys.items():
                ctx.put(k, kind, "t_x", {"text": "x"}, Preconditions(task_id=tid))
                self.h.store.conn.execute("UPDATE outbox SET state=? WHERE idem_key=?", (state, k))
        self.disable()
        got = {r["idem_key"]: r["state"] for r in self.h.rows() if r["idem_key"] in keys}
        self.assertEqual(got, {k: v[2] for k, v in keys.items()})

    def test_an_open_park_is_not_cancelled_and_is_released_later(self):
        f, rows = self.park(open_run=True)
        self.assertEqual((f["state"], rows), ("open", []))
        self.disable()
        self.assertEqual(self.rows("fences")[0]["state"], "open")
        self.h.cards[self.w]["runs"][-1].update(status="blocked", ended_at=self.h.now // 1000)
        self.h.cycle()
        self.assertEqual(self.rows("fences")[0]["state"], "released")

    def kept_answer_row(self):
        b, rel = self.closed()
        self.h.park(rel["card"], "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=answer_only"
                    % (rel["relay_ref"], rel["gen"]),
                    "CLICKUP-OUT v1 kind=resolution blocker=%d relay=%s gen=%d authority=unchanged\n%s"
                    % (b["id"], rel["relay_ref"], rel["gen"], self.TEXT))
        self.h.scan()  # the complete-Q row is queued, not yet sent
        b = self.rows("blockers")[0]
        self.assertEqual((b["state"], self.h._state(b["release_key"])), ("releasing", "pending"))
        return b, rel

    def test_a_kept_complete_q_row_sent_after_disable_finishes_the_delivery_with_its_alert(self):  # r9 §7
        from clickup_intake import main as glue_main
        b, rel = self.kept_answer_row()
        self.disable()
        self.assertEqual(self.h._state(b["release_key"]), "pending")
        self.h.cycle()
        b2 = self.rows("blockers")[0]
        self.assertEqual((self.h._state(b["release_key"]), b2["state"], b2["delivered_gen"]),
                         ("done", "delivered", rel["gen"]))
        self.assertEqual(self.h.cards[b["question_card"]]["task"]["status"], "done")
        self.assertEqual(sum("later ClickUp corrections to the answer for %s" % b["card"] in a
                             for a in self.alerts()), 1)
        self.assertEqual(glue_main.rollback_preflight(self.h.store), [])

    def test_a_kept_complete_q_row_refused_after_disable_leaves_an_open_question_alert(self):
        b, _ = self.kept_answer_row()
        self.disable()
        self.h.fail[b["release_key"]] = "cancelled_stale"
        for _ in range(2):
            self.h.cycle()
        self.assertEqual((self.h._state(b["release_key"]), self.rows("blockers")[0]["state"]),
                         ("cancelled_stale", "resolved"))  # not re-sent while disabled
        self.assertEqual(sum("answer %s by completing it" % b["question_card"] in a for a in self.alerts()), 1)

    def test_a_correction_cancelled_by_disable_leaves_the_delivered_answer_with_its_alert(self):
        from test_question_relay import ReleaseScenarioTest
        b, rel = self.resolve()
        rel2 = ReleaseScenarioTest.new_input(self)
        self.h.park(rel2["card"], "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=answer_only"
                    % (rel2["relay_ref"], rel2["gen"]),
                    "CLICKUP-OUT v1 kind=resolution blocker=%d relay=%s gen=%d authority=unchanged\nUse US."
                    % (b["id"], rel2["relay_ref"], rel2["gen"]))
        self.h.scan()
        self.assertEqual(self.rows("blockers")[0]["state"], "releasing")
        self.disable()
        self.h.cycle()
        b2 = self.rows("blockers")[0]
        self.assertEqual((b2["state"], b2["delivered_gen"]), ("resolved", rel["gen"]))  # the delivered answer stands
        self.assertEqual(sum("later ClickUp corrections to the answer for %s" % b["card"] in a
                             for a in self.alerts()), 1)

    def test_a_terminal_tasks_disable_fence_release_and_park_are_finished(self):  # r11 §9, r12 §3.3/§9
        self.resolve()
        self.fence(ahead_s=1000)
        self.link(self.P, "CLICKUP-PARK v1 " + self.w, "todo")
        self.h.cards[self.w]["runs"].append({"id": 8, "status": "running", "ended_at": None})
        self.h.cycle()
        self.assertEqual({f["fence_card"]: f["state"] for f in self.rows("fences")}, {self.F: "open", self.P: "open"})
        self.h.store.conn.execute("UPDATE tasks SET terminal=1, phase='stopped'")
        self.h.store.conn.commit()
        self.disable()
        self.h.cards[self.w]["runs"][-1].update(status="blocked", ended_at=self.h.now // 1000)
        for _ in range(2):
            self.h.cycle()
        self.assertEqual({f["fence_card"]: f["state"] for f in self.rows("fences")},
                         {self.F: "released", self.P: "released"})
        self.assertEqual((self.h.cards[self.F]["task"]["status"], self.h.cards[self.P]["task"]["status"]),
                         ("done", "done"))


class GlueDisabledFenceRetryTest(unittest.TestCase):
    """B2 at the writer (r11 §9, r6 §7, r11 §3.3): a disable release refused at its send gate by one failed
    read is offered again while disabled, completed exactly once, and the rollback preflight clears."""

    from test_question_relay import GlueFenceReleaseTest as _G
    setUp, world, _cleanup = _G.setUp, _G.world, _G._cleanup
    F, W, SUMMARY = _G.F, _G.W, _G.SUMMARY

    def refused_once(self):
        import dataclasses
        from clickup_intake import kanban_port as kp
        self.world()
        self.store.conn.execute("UPDATE tasks SET phase='development'")
        self.store.conn.commit()
        engine.disable_blockers(self.store, self.glue.now, self.glue.cfg)
        self.key = self.store.conn.execute("SELECT release_key FROM fences").fetchone()[0]
        show = self.glue.port.show
        self.glue.port.show = lambda card: kp.KanbanResult(kp.Outcome.AMBIGUOUS, None)
        self.glue.kanban(self.glue.row(self.key))  # one transient failed read at the send gate
        self.glue.port.show = show
        self.assertEqual((self.glue.state(self.key), self.completes), ("cancelled_stale", []))
        self.obs = dataclasses.replace(self.glue.obs, confirmed=frozenset({"tk"}))

    def scans(self, n, start=1):
        for i in range(n):
            engine.evaluate(self.store, self.glue.now + start + i, self.obs, self.glue.cfg)
            self.glue.execute()

    def released_once(self):
        from clickup_intake import main as glue_main
        f = self.store.conn.execute("SELECT state FROM fences").fetchone()[0]
        self.assertEqual((self.shows[self.F]["task"]["status"], f, self.glue.state(self.key)),
                         ("done", "released", "done"))
        self.assertEqual(self.completes, [(self.F, "CLICKUP-FENCE v1 card=%s released (relay disabled)" % self.W)])
        self.assertEqual(glue_main.rollback_preflight(self.store), [])

    def test_a_disable_release_refused_by_one_failed_read_is_completed_by_later_scans(self):
        self.refused_once()
        self.scans(3)  # reopened, offered again and sent, then confirmed
        self.released_once()
        self.scans(2, start=4)  # nothing is sent again
        self.assertEqual(len(self.completes), 1)

    def test_a_second_disable_re_arms_it(self):
        self.refused_once()
        engine.disable_blockers(self.store, self.glue.now + 1, self.glue.cfg)
        self.assertEqual(self.glue.state(self.key), "pending")
        self.glue.execute()
        self.scans(1, start=2)
        self.released_once()


class GlueDisabledParkObservationTest(unittest.TestCase):
    """r12 §9: after disable (no cutover), scans still ``show`` the work and park cards of recorded open or
    releasing parks so they keep being evaluated; nothing else is watched."""

    from test_question_relay import GlueBlockerRowTest as _G
    setUp, _cleanup, _watched_world, tearDown_world = _G.setUp, _G._cleanup, _G._watched_world, _G.tearDown_world

    def test_open_parks_keep_their_cards_observed_without_a_cutover(self):
        shows, calls = self._watched_world()
        c = self.store.conn
        c.execute("DELETE FROM meta WHERE key='blocker_cutover_ms'")
        c.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, state, updated_ms)"
                  " VALUES ('tk', 't_l0000001', 't_p0000001', 'park', 1, 'open', 1),"
                  " ('tk', 't_l0000001', 't_p0000002', 'park', 1, 'released', 1)")
        c.commit()
        shows["t_p0000001"] = shows["t_p0000002"] = {"task": {"status": "blocked"}, "parents": [], "children": []}
        self.glue.observe_cards()
        self.assertEqual(sorted(set(calls) & {"t_l0000001", "t_p0000001", "t_p0000002", "t_c0000001", "t_q0000001",
                                              "t_o0000001"}), ["t_l0000001", "t_p0000001"])
        self.assertIn("t_p0000001", self.glue.obs.cards)

    def test_a_held_task_park_is_not_observed(self):
        shows, calls = self._watched_world(hold="outbound_filter")
        c = self.store.conn
        c.execute("DELETE FROM meta WHERE key='blocker_cutover_ms'")
        c.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, state, updated_ms)"
                  " VALUES ('tk', 't_l0000001', 't_p0000001', 'park', 1, 'open', 1)")
        c.commit()
        self.glue.observe_cards()
        self.assertFalse({"t_l0000001", "t_p0000001"} & set(calls), calls)

    def test_a_terminal_tasks_open_fences_and_parks_keep_their_cards_observed(self):  # r11 §9, r12 §3.3/§9
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.tearDown_world()
                shows, calls = self._watched_world(terminal=1)
                c = self.store.conn
                c.execute("DELETE FROM fences")
                if not enabled:
                    c.execute("DELETE FROM meta WHERE key='blocker_cutover_ms'")
                c.execute("INSERT INTO fences(task, card, fence_card, kind, created_s, state, updated_ms)"
                          " VALUES ('tk', 't_l0000001', 't_p0000001', 'park', 1, 'open', 1),"
                          " ('tk', 't_l0000001', 't_f0000001', 'fence', 1, 'releasing', 1)")
                c.commit()
                shows["t_p0000001"] = {"task": {"status": "blocked"}, "parents": [], "children": []}
                calls.clear()
                self.glue.observe_cards()
                self.assertEqual(sorted({"t_l0000001", "t_p0000001", "t_f0000001"} & set(self.glue.obs.cards)),
                                 ["t_f0000001", "t_l0000001", "t_p0000001"])

    def test_a_releasing_blockers_question_and_work_cards_stay_observed_after_disable(self):  # r9 §7, r6 §7
        for terminal in (0, 1):  # a terminal task's kept answer row is never sent (r9 §4.4 due trigger)
            with self.subTest(terminal=terminal):
                self.tearDown_world()
                shows, calls = self._watched_world(terminal=terminal)
                c = self.store.conn
                c.execute("DELETE FROM blockers")
                c.execute("DELETE FROM meta WHERE key='blocker_cutover_ms'")
                c.execute("INSERT INTO blockers(task, question_card, card, run_id, created_ms, state, updated_ms)"
                          " VALUES ('tk', 't_q0000001', 't_l0000001', 7, 1, 'releasing', 1)")
                c.commit()
                self.glue.obs = engine.Observation()
                self.glue.observe_cards()
                self.assertEqual(sorted({"t_l0000001", "t_q0000001"} & set(self.glue.obs.cards)),
                                 [] if terminal else ["t_l0000001", "t_q0000001"])

    def test_the_disable_park_sentence_is_printed_by_the_writer(self):  # r12 §9
        self._watched_world()
        engine.disable_blockers(self.store, 1_792_000_000_000)
        self.glue.execute()
        self.assertIn(engine.DISABLED_PARK_ALERT, self.glue.out)


class OrchestratorRequirement8Test(unittest.TestCase):
    """r9 §4.1 / Q13: once enabled, the orchestrator card gains requirement 8 and the protocol verbatim; with the
    feature off the body is byte-identical to the baseline rendering."""

    from test_s3_kanban import TemplatesTest as _T
    orch = _T.orch
    REQ8 = ("8. Every card you create for this plan, and every card created by a later amendment of it, must include"
            " verbatim the ClickUp question protocol below.")

    def test_enabled_body_has_requirement_8_and_the_protocol_verbatim(self):
        from clickup_intake import render
        body = self.orch(protocol=True)
        self.assertIn(self.REQ8, body.splitlines())
        self.assertIn(render.question_protocol().strip(), body)
        self.assertLess(body.index(self.REQ8), body.index(render.question_protocol().strip()))
        self.assertNotRegex(body, r"\$\{?[a-z_]+")

    def test_disabled_body_is_unchanged(self):
        self.assertEqual(self.orch(protocol=False), self.orch())
        self.assertNotIn("ClickUp question protocol", self.orch())


class GlueOrchestratorProtocolTest(unittest.TestCase):
    """r9 §7: new orchestrations render the protocol from the cutover on (the body is rendered at send time)."""

    from test_question_relay import GlueBlockerRowTest as _G
    setUp, _cleanup = _G.setUp, _G._cleanup

    def body_kwargs(self, enabled):
        from unittest import mock
        from clickup_intake import render
        self.store.ensure_relay_tables()
        c = self.store.conn
        c.execute("INSERT INTO tasks(clickup_task_id) VALUES ('tk')")
        if enabled:
            c.execute("INSERT INTO meta(key, value) VALUES ('blocker_cutover_ms', '1')")
        c.commit()
        self.glue.approval_view = lambda aid: ({"accounts_gen": 1}, {})
        self.glue.plan_bytes = lambda tid, sha: b"plan"
        with mock.patch.object(render, "orchestrator_card_body", return_value="body") as m:
            self.glue.card_body("tk", {"template": "orchestrator_card",
                                       "params": {"rev": 1, "sha256": "a" * 64, "gate": "t_g0000001",
                                                  "approval_id": 1}})
        return m.call_args

    def test_protocol_is_rendered_only_while_enabled(self):
        self.assertEqual(self.body_kwargs(True).kwargs.get("protocol"), True)
        self._cleanup()
        self.setUp()
        self.assertEqual(self.body_kwargs(False).kwargs.get("protocol"), False)


class ProtocolCommentTest(unittest.TestCase):
    """r9 §7 D1-A ``blockers protocol-comment``: one trusted ``kb_comment`` "CLICKUP-PROTOCOL v1" with the protocol
    text on each non-terminal watched card of live ClickUp-origin graphs; no status write; idempotent."""

    from test_question_relay import GlueBlockerRowTest as _G
    setUp, _cleanup, _watched_world, tearDown_world = _G.setUp, _G._cleanup, _G._watched_world, _G.tearDown_world

    def test_targets_are_the_open_watched_cards_of_unheld_live_tasks_never_the_gate(self):
        shows, calls = self._watched_world()
        shows["t_c0000001"]["task"]["status"] = "done"
        shows["t_m0000001"]["task"]["status"] = "archived"
        self.assertEqual(sorted(self.glue.protocol_targets()),
                         [("tk", "t_e0000001"), ("tk", "t_l0000001"), ("tk", "t_o0000001")])
        self.assertNotIn("t_g0000001", calls)

    def test_held_or_terminal_tasks_have_no_targets(self):
        for kw in ({"hold": "outbound_filter"}, {"terminal": 1}):
            with self.subTest(**kw):
                self.tearDown_world()
                self._watched_world(**kw)
                self.assertEqual(self.glue.protocol_targets(), [])

    def test_one_idempotent_protocol_comment_per_target(self):
        from clickup_intake import render
        self.store.ensure_relay_tables()
        self.store.conn.execute("INSERT INTO tasks(clickup_task_id) VALUES ('tk')")
        self.store.conn.commit()
        new = engine.protocol_comments(self.store, 1, [("tk", "t_o0000001"), ("tk", "t_l0000001")])
        self.assertEqual([(i.idem_key, i.kind, i.target) for i in new],
                         [("clickup-dp:tk:protocol:t_o0000001", "kb_comment", "t_o0000001"),
                          ("clickup-dp:tk:protocol:t_l0000001", "kb_comment", "t_l0000001")])
        self.assertEqual(new[0].payload["text"], "CLICKUP-PROTOCOL v1\n\n" + render.question_protocol().strip())
        self.assertEqual(engine.protocol_comments(self.store, 2, [("tk", "t_o0000001")]), [])
        rows = self.store.conn.execute("SELECT kind FROM outbox").fetchall()
        self.assertEqual([r[0] for r in rows], ["kb_comment", "kb_comment"])


class WriterScanOrderDisableAlertTest(unittest.TestCase):
    """B1/NA1 (r9 §7, r11 §9, r12 §9): after ``blockers disable`` a releasing blocker whose kept row settles
    (applied by readback, refused or never applied, or its pending correction cancelled) gets its ONE per-question
    alert, delivered or open as its derived state says, in the writer's REAL scan order (``main.Glue.progress_pass``
    before the full evaluate), across unchanged scans, restarts and a crash inside the progress pass. That decision
    reads only the outbox, so the progress pass keeps the alert it queues with it. Fakes only."""

    from test_question_relay import WriterScanOrderFenceTest as _W, ReleaseScenarioTest as _R, writer_scan
    setUp_, setUp = DisableTest.setUp, _W.setUp
    cutover, link, rows, ask, answer, close, closed, resolve, fence, release_rows, disable, alerts, kept_answer_row = (
        DisableTest.cutover, DisableTest.link, DisableTest.rows, DisableTest.ask, DisableTest.answer,
        DisableTest.close, DisableTest.closed, DisableTest.resolve, DisableTest.fence, DisableTest.release_rows,
        DisableTest.disable, DisableTest.alerts, DisableTest.kept_answer_row)
    lost_ack, readback_done = _R.lost_ack, _R.readback_done
    TEXT = DisableTest.TEXT

    def delivered_text(self, b):
        return ("ClickUp relay disabled: later ClickUp corrections to the answer for %s (question %s) are no longer"
                " delivered and its resumption is not reported; comment any correction on %s, or re-enable."
                % (b["card"], b["question_card"], b["card"]))

    def open_text(self, b):
        return ("ClickUp relay disabled: answer %s by completing it with the decision as its summary, or re-enable."
                % b["question_card"])

    def per_question(self, g=None):
        """(per-question disable alert rows, those lines the writer printed)."""
        rows = [json.loads(r["body"])["text"] for r in self.h.rows("alert")
                if r["idem_key"].startswith("clickup-dp:alert:blocker:") and ":disabled:" in r["idem_key"]]
        return rows, [x for x in (g.out if g else []) if x.startswith("ClickUp relay disabled: ") and "park" not in x]

    def kept(self, outcome):
        """The kept ``complete Q`` row is confirmed applied by its readback, or refused (never applied)."""
        b, rel = self.kept_answer_row()
        self.disable()
        if outcome == "done":
            self.h.cards[b["question_card"]]["task"].update(status="done")
        self.h.store.conn.execute("UPDATE outbox SET state=? WHERE idem_key=?", (outcome, b["release_key"]))
        self.h.store.conn.commit()
        return b, rel

    def cancelled_correction(self):
        """A delivered answer whose pending correction row is cancelled by disable."""
        b, rel = self.resolve()
        rel2 = self._R.new_input(self)
        self.h.park(rel2["card"], "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=answer_only"
                    % (rel2["relay_ref"], rel2["gen"]),
                    "CLICKUP-OUT v1 kind=resolution blocker=%d relay=%s gen=%d authority=unchanged\nUse US."
                    % (b["id"], rel2["relay_ref"], rel2["gen"]))
        self.h.scan()
        self.assertEqual(self.rows("blockers")[0]["state"], "releasing")
        self.disable()
        return b, rel

    def corrections(self, b):
        return [(r["idem_key"], r["state"]) for r in self.h.rows() if ":blocker:%d:corr:" % b["id"] in r["idem_key"]]

    def test_na1_a_kept_row_sent_after_disable_alerts_once_in_the_writer_order(self):
        DisableTest.test_a_kept_complete_q_row_sent_after_disable_finishes_the_delivery_with_its_alert(self)

    def test_na1_a_kept_row_refused_after_disable_alerts_once_in_the_writer_order(self):
        DisableTest.test_a_kept_complete_q_row_refused_after_disable_leaves_an_open_question_alert(self)

    def test_na1_a_correction_cancelled_by_disable_alerts_once_in_the_writer_order(self):
        DisableTest.test_a_correction_cancelled_by_disable_leaves_the_delivered_answer_with_its_alert(self)

    def test_na1_a_possibly_sent_kept_row_is_reconciled_before_its_one_alert(self):
        for applied in (True, False):
            with self.subTest(applied=applied):
                self.setUp()
                b, rel = self.kept_answer_row()
                self.disable()
                self.lost_ack(b["release_key"])
                self.h.cycle()  # sent, its ack lost: possibly sent, not yet settled
                self.assertEqual((self.h._state(b["release_key"]), self.rows("blockers")[0]["state"]),
                                 ("sending", "releasing"))
                self.assertEqual(self.per_question(), ([], []))
                if applied:
                    self.readback_done(b["release_key"], lambda: self.h.cards[b["question_card"]]["task"].update(
                        status="done"))
                else:  # the readback finds it not applied
                    self.h.store.conn.execute("UPDATE outbox SET state='cancelled_stale' WHERE idem_key=?",
                                              (b["release_key"],))
                    self.h.store.conn.commit()
                for _ in range(2):
                    self.h.cycle()
                b2 = self.rows("blockers")[0]
                self.assertEqual((b2["state"], b2["delivered_gen"], self.h._state(b["release_key"])),
                                 ("delivered", rel["gen"], "done") if applied else
                                 ("resolved", None, "cancelled_stale"))  # never re-sent while disabled
                want = self.delivered_text(b) if applied else self.open_text(b)
                self.assertEqual(self.per_question()[0], [want])

    def test_na1_the_real_writer_prints_each_per_question_alert_once(self):
        for case in ("done", "cancelled_stale", "correction"):
            with self.subTest(case):
                self.setUp()
                b, rel = self.cancelled_correction() if case == "correction" else self.kept(case)
                corr = self.corrections(b)
                g, _ = self.writer_scan()
                b2 = self.rows("blockers")[0]
                want = [self.open_text(b) if case == "cancelled_stale" else self.delivered_text(b)]
                self.assertEqual(self.per_question(g), (want, want))
                self.assertEqual((b2["state"], b2["delivered_gen"]), {
                    "done": ("delivered", rel["gen"]), "cancelled_stale": ("resolved", None),
                    "correction": ("resolved", rel["gen"])}[case])
                for _ in range(2):  # unchanged scans, each a fresh process
                    g, _ = self.writer_scan()
                    self.assertEqual(self.per_question(g), (want, []))
                self.assertEqual(self.rows("blockers")[0], b2)
                self.assertEqual(self.corrections(b), corr)  # a cancelled correction is not sent again
                self.assertTrue(all(s == "cancelled_stale" for _, s in corr))

    def test_na1_a_crash_inside_the_progress_pass_keeps_the_only_alert_row(self):
        from clickup_intake import main as glue_main
        b, rel = self.kept("done")
        g = glue_main.Glue(self.h.cfg, self.h.store, None, None, self.h.now)
        g.set_meta(glue_main.PREPASS_KEY, self.h.store.conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM outbox").fetchone()[0])  # progress_pass's mark ...
        engine.evaluate(self.h.store, self.h.now, engine.Observation(scan_started_ms=self.h.now,
                                                                     confirmed=frozenset(self.h.confirmed)), self.h.cfg)
        self.assertEqual(self.rows("blockers")[0]["state"], "delivered")  # ... its cards-empty evaluate; then a crash
        g, _ = self.writer_scan()  # the rerun's progress_pass drops by the kept mark
        self.assertEqual(self.per_question(g), ([self.delivered_text(b)], [self.delivered_text(b)]))
        g, _ = self.writer_scan()
        self.assertEqual(self.per_question(g), ([self.delivered_text(b)], []))


if __name__ == "__main__":
    unittest.main()
