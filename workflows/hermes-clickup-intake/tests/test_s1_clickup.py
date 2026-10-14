"""S1 ClickUp-side tests: T1, T3, T4, T5, T11, T12, T15 (plan r5 §4, §5, §9, §10, §13.1)."""

import ctypes
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error

from clickup_intake import clickup_api, clickup_effects, observe
from clickup_intake.config import Config, from_dict
from clickup_intake.contracts import Outcome, TransportResult
from clickup_intake.store import Store
from fakes.fake_clickup import BOT, CORRECT_STATUSES, LIST_ID, FakeClickUp
from fakes.fake_clock import FakeClock

APPROVER_A, APPROVER_B, COWORKER = 900000101, 900000102, 555
T0 = 1_800_000_000_000
TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(T0)
        self.cfg = Config(cutover_ms=T0 - 3_600_000)
        self.fake = FakeClickUp(self.clock)
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(os.path.join(self.dir, "state.sqlite3"), self.clock)
        self.addCleanup(self.store.close)

    def task(self, task_id):
        row = self.store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def items(self, task_id, kind=None):
        q = "SELECT * FROM items WHERE task=?" + (" AND kind=?" if kind else "")
        return {r["id"]: dict(r) for r in self.store.conn.execute(q, (task_id, kind) if kind else (task_id,))}

    def scan(self, **kw):
        """One observation scan (discovery + task passes) at the fake clock's time."""
        return observe.observe_scan(self.fake, self.store, self.cfg, self.clock(), **kw)


# ---------------------------------------------------------------------------------------------- T3
class PreflightTest(Base):
    def run_preflight(self, statuses):
        self.fake.statuses[LIST_ID] = [list(p) for p in statuses]
        return observe.preflight_statuses(self.fake, self.cfg)

    def replace(self, old, new):
        return [new if p == old else p for p in CORRECT_STATUSES]

    def test_old_spellings_mismatch(self):
        statuses = self.replace(("implementing", "custom"), ("implimenting", "custom"))
        statuses = [("implimented", "done") if p == ("implemented", "done") else p for p in statuses]
        r = self.run_preflight(statuses)
        self.assertEqual(r.status, "MISMATCH")
        self.assertEqual(set(r.missing), {("implementing", "custom"), ("implemented", "done")})
        self.assertEqual(set(r.unexpected), {("implimenting", "custom"), ("implimented", "done")})

    def test_type_change_mismatch(self):
        r = self.run_preflight(self.replace(("implemented", "done"), ("implemented", "custom")))
        self.assertEqual((r.status, r.missing, r.unexpected),
                         ("MISMATCH", (("implemented", "done"),), (("implemented", "custom"),)))

    def test_missing_status_mismatch(self):
        r = self.run_preflight([p for p in CORRECT_STATUSES if p[0] != "deployed"])
        self.assertEqual((r.status, r.missing, r.unexpected), ("MISMATCH", (("deployed", "closed"),), ()))

    def test_extra_status_mismatch(self):
        r = self.run_preflight(list(CORRECT_STATUSES) + [("on hold", "custom")])
        self.assertEqual((r.status, r.missing, r.unexpected), ("MISMATCH", (), (("on hold", "custom"),)))

    def test_case_or_trailing_space_mismatch(self):
        for bad in (("Deployed", "closed"), ("deployed ", "closed")):
            r = self.run_preflight(self.replace(("deployed", "closed"), bad))
            self.assertEqual((r.status, r.missing, r.unexpected), ("MISMATCH", (("deployed", "closed"),), (bad,)))

    def test_exact_set_any_order_ok(self):
        r = self.run_preflight(list(reversed(CORRECT_STATUSES)))
        self.assertEqual(r.status, "OK")
        self.assertTrue(r.ok)
        self.assertEqual(self.fake.calls, [("GET", f"/v2/list/{LIST_ID}", {})])

    def test_transport_failure_is_error_not_ok(self):
        self.fake.inject("GET", "/v2/list/", Outcome.AUTH_FAIL, status=401)
        r = observe.preflight_statuses(self.fake, self.cfg)
        self.assertEqual(r.status, "ERROR")
        self.assertFalse(r.ok)

    def test_malformed_body_is_error(self):
        self.fake.inject("GET", "/v2/list/", Outcome.OK, status=200, json={"statuses": [{"status": 1}]})
        self.assertEqual(observe.preflight_statuses(self.fake, self.cfg).status, "ERROR")

    def test_describe_lists_pairs_only(self):
        r = self.run_preflight(self.replace(("implemented", "done"), ("implimented", "done")))
        self.assertIn("missing implemented/done", r.describe())
        self.assertIn("unexpected implimented/done", r.describe())


INTAKE = ["reviewing feature", "business requirements", "technical requirements", "waiting for approval"]


def list_calls(fake):
    return [c for c in fake.calls if c[1] == f"/v2/list/{LIST_ID}/task"]


def task_get_calls(fake, task_id):
    return [c for c in fake.calls if c[1].startswith(f"/v2/task/{task_id}")]


class DiscoveryTest(Base):
    def set_phase(self, task_id, phase):
        self.store.observe_commit(task_id, [], {"phase": phase})

    def test_request_carries_exactly_the_four_intake_statuses(self):
        self.fake.add_task("t1")
        self.scan()
        self.assertEqual(list_calls(self.fake)[0][2], {
            "archived": "false", "subtasks": "false", "order_by": "created", "statuses[]": INTAKE, "page": 0})
        self.assertNotIn("include_closed", list_calls(self.fake)[0][2])

    def test_admissible_task_gets_new_quiet_row_with_quiet_from_creation(self):
        self.fake.add_task("t1", created_ms=T0 - 10_000, creator=COWORKER)
        r = self.scan()
        row = self.task("t1")
        self.assertEqual((row["phase"], row["eligible"], row["requester_id"]), ("new_quiet", 1, COWORKER))
        self.assertEqual(row["quiet_until_ms"], T0 - 10_000 + 180_000)
        self.assertEqual(r.discovery.admitted, ["t1"])
        self.assertTrue(r.discovery.complete)

    def test_multi_page(self):
        self.fake.page_size = 2
        for i in range(5):
            self.fake.add_task(f"t{i}", created_ms=T0 - 1000 + i)
        r = self.scan()
        self.assertEqual([c[2]["page"] for c in list_calls(self.fake)], [0, 1, 2])
        self.assertEqual(sorted(r.discovery.admitted), [f"t{i}" for i in range(5)])

    def test_task_inserted_during_pagination_is_seen_next_scan(self):
        self.fake.page_size = 2
        for i in range(4):
            self.fake.add_task(f"t{i}", created_ms=T0 - 1000 + i)

        def insert(page):
            if page == 1 and "early" not in self.fake.tasks:
                self.fake.add_task("early", created_ms=T0 - 5000)  # shifts every later task by one
        self.fake.on_list_page = insert
        self.scan()
        self.assertIsNone(self.task("early"))
        self.clock.advance(60)
        self.scan()
        self.assertEqual(self.task("early")["phase"], "new_quiet")
        for i in range(4):
            self.assertIsNotNone(self.task(f"t{i}"), i)

    def test_ten_page_cap_alerts_and_marks_incomplete(self):
        self.fake.page_size = 1
        for i in range(12):
            self.fake.add_task(f"t{i}", created_ms=T0 - 1000 + i)
        r = self.scan()
        self.assertEqual(len(list_calls(self.fake)), 10)
        self.assertFalse(r.discovery.complete)
        self.assertTrue(any("10 pages" in a for a in r.alerts))

    def test_a6_unfiltered_fallback_gives_same_admission(self):
        statuses = INTAKE + ["implementing", "implemented", "ready for deployment"]
        for i, s in enumerate(statuses):
            self.fake.add_task(f"t{i}", status=s, created_ms=T0 - 1000 + i)
        r = self.scan(filtered=False)
        self.assertEqual(list_calls(self.fake)[0][2], {"archived": "false", "subtasks": "false",
                                                        "order_by": "created", "include_closed": "false", "page": 0})
        self.assertEqual(sorted(r.discovery.admitted), [f"t{i}" for i in range(4)])

    def test_tracked_task_missing_from_discovery_is_not_terminal_and_keeps_passes(self):
        self.fake.add_task("t1", created_ms=T0 - 600_000)
        self.scan()
        self.set_phase("t1", "development")
        self.fake.set_status("t1", "implementing")
        self.clock.advance(3600)
        before = len(task_get_calls(self.fake, "t1"))
        self.scan()
        row = self.task("t1")
        self.assertEqual((row["terminal"], row["phase"], row["last_status_seen"]), (0, "development", "implementing"))
        self.assertGreater(len(task_get_calls(self.fake, "t1")), before)

    def test_terminal_only_from_own_get_task(self):
        cases = {"deployed": "deployed", "archived": "stopped", "moved": "stopped", "deleted": "stopped"}
        for case, phase in cases.items():
            tid = "t_" + case
            self.fake.add_task(tid, created_ms=T0 - 600_000)
            self.scan()
            self.set_phase(tid, "development")
            if case == "deployed":
                self.fake.set_status(tid, "deployed")
            elif case == "archived":
                self.fake.archive(tid)
            elif case == "moved":
                self.fake.move(tid, "999")
            else:
                self.fake.delete_task(tid)
            self.clock.advance(3600)
            self.scan()
            row = self.task(tid)
            self.assertEqual((row["terminal"], row["phase"]), (1, phase), case)
            n = len(task_get_calls(self.fake, tid))
            self.clock.advance(3600)
            self.scan()
            self.assertEqual(len(task_get_calls(self.fake, tid)), n, f"{case}: terminal task is not read again")

    def test_missing_archived_or_list_field_is_not_a_terminal_fact(self):
        self.fake.add_task("t1", created_ms=T0 - 600_000)
        self.scan()
        self.set_phase("t1", "development")
        for key in ("archived", "list"):
            body = {k: v for k, v in self.fake.tasks["t1"].items() if k != key}
            self.fake.inject("GET", "^/v2/task/t1$", Outcome.OK, status=200, json=body)
            self.clock.advance(3600)
            self.scan()
            self.assertEqual((self.task("t1")["terminal"], self.task("t1")["phase"]), (0, "development"), key)


# --------------------------------------------------------------------------------------------- T11
class AdmissionTest(Base):
    def test_first_seen_in_non_intake_status_gets_no_row_and_no_reads(self):
        self.fake.ignore_filters = True
        for s in ("implemented", "ready for deployment", "implementing", "deployed"):
            self.fake.add_task("t_" + s.replace(" ", "_"), status=s, created_ms=T0 - 1000)
        self.scan()
        self.clock.advance(600)
        self.scan()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual([c for c in self.fake.calls if c[1].startswith("/v2/task/")], [])

    def test_archived_other_list_subtask_bot_created_get_no_row(self):
        self.fake.ignore_filters = True
        self.fake.add_task("arch", archived=True, created_ms=T0 - 1000)
        self.fake.add_task("other", list_id="999", created_ms=T0 - 1000)
        self.fake.add_task("sub", parent="arch", created_ms=T0 - 1000)
        self.fake.add_task("bot", creator=BOT, created_ms=T0 - 1000)
        self.scan()
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual([c for c in self.fake.calls if c[1].startswith("/v2/task/")], [])

    def test_pre_cutover_task_is_baseline_and_never_read(self):
        self.fake.add_task("old", created_ms=self.cfg.cutover_ms - 1)
        self.scan()
        self.clock.advance(3600)
        self.scan()
        self.assertEqual((self.task("old")["phase"], self.task("old")["eligible"]), ("baseline", 0))
        self.assertEqual(task_get_calls(self.fake, "old"), [])

    def test_no_cutover_means_baseline(self):
        self.cfg = Config(cutover_ms=None)
        self.fake.add_task("t1")
        self.scan()
        self.assertEqual(self.task("t1")["phase"], "baseline")

    def test_moved_to_done_during_initial_quiet_becomes_not_admitted(self):
        for s in ("implemented", "ready for deployment"):
            tid = "t_" + s.replace(" ", "_")
            self.fake.add_task(tid, created_ms=self.clock())
            self.scan()
            self.assertEqual(self.task(tid)["phase"], "new_quiet")
            self.clock.advance(100)
            self.fake.set_status(tid, s)
            self.clock.advance(81)  # quiet expired: the barrier GET Task sees the move
            self.scan()
            self.assertEqual(self.task(tid)["phase"], "not_admitted", s)
            self.assertFalse(observe.barrier_confirmed(self.task(tid), self.clock()))
            n = len(task_get_calls(self.fake, tid))
            self.clock.advance(600)
            self.scan()
            self.assertEqual(len(task_get_calls(self.fake, tid)), n, "not_admitted is not read")

    def test_not_admitted_moved_back_returns_to_new_quiet_with_fresh_quiet(self):
        self.fake.add_task("t1", created_ms=self.clock())
        self.scan()
        self.fake.set_status("t1", "implemented")
        self.clock.advance(30)
        self.scan()
        self.assertEqual(self.task("t1")["phase"], "not_admitted")
        self.clock.advance(3600)
        self.fake.set_status("t1", "reviewing feature")
        self.scan()
        row = self.task("t1")
        self.assertEqual(row["phase"], "new_quiet")
        self.assertEqual(row["quiet_until_ms"], self.clock() + 180_000)

    def test_admit_refuses_done_type_and_implementing_accepts_intake(self):
        for s in ("implemented", "ready for deployment", "implementing", "deployed"):
            tid = "old_" + s.replace(" ", "_")
            self.fake.add_task(tid, status=s, created_ms=self.cfg.cutover_ms - 1)
            ok, reason = observe.admit_task(self.fake, self.store, self.cfg, tid, self.clock())
            self.assertFalse(ok, s)
            self.assertNotEqual(self.task(tid) and self.task(tid)["phase"], "new_quiet")
        self.fake.add_task("old_ok", status="business requirements", created_ms=self.cfg.cutover_ms - 1)
        self.scan()
        self.assertEqual(self.task("old_ok")["phase"], "baseline")
        ok, _ = observe.admit_task(self.fake, self.store, self.cfg, "old_ok", self.clock())
        self.assertTrue(ok)
        row = self.task("old_ok")
        self.assertEqual((row["phase"], row["eligible"], row["quiet_until_ms"]),
                         ("new_quiet", 1, self.clock() + 180_000))

    def test_admissible_predicate(self):
        base = {"list": {"id": LIST_ID}, "archived": False, "parent": None, "status": {"status": "reviewing feature"}}
        self.assertTrue(observe.admissible(base, self.cfg))
        for change in ({"list": {"id": "x"}}, {"archived": True}, {"parent": "p1"},
                       {"status": {"status": "implementing"}}, {"status": {"status": "Reviewing feature"}}):
            self.assertFalse(observe.admissible({**base, **change}, self.cfg), change)


# ---------------------------------------------------------------------------------------------- T1
class QuietTest(Base):
    def track(self, tid="t1", phase=None):
        """Create a tracked task whose first-sight F and barrier are done."""
        self.fake.add_task(tid, created_ms=self.clock() - 600_000)
        self.scan()
        if phase:
            self.store.observe_commit(tid, [], {"phase": phase})
        return tid

    def test_no_barrier_at_179s_then_barrier_at_180s(self):
        self.fake.add_task("t1", created_ms=self.clock())
        self.scan()
        self.clock.advance(179)
        self.scan()
        self.assertFalse(observe.barrier_confirmed(self.task("t1"), self.clock()))
        self.clock.advance(1)
        self.scan()
        self.assertTrue(observe.barrier_confirmed(self.task("t1"), self.clock()))

    def test_each_activity_restarts_quiet_and_clears_barrier(self):
        tid = self.track(phase="planning")
        old = self.fake.add_comment(tid, COWORKER, "old")
        att = self.fake.add_attachment(tid, COWORKER, "a.txt")
        self.clock.advance(1000)
        self.scan()
        changes = {
            "comment": lambda: self.fake.add_comment(tid, COWORKER, "new one"),
            "reply": lambda: self.fake.add_comment(tid, COWORKER, "a reply", reply_to=old),
            "edit": lambda: self.fake.edit_comment(old, "old, edited"),
            "description": lambda: self.fake.set_description(tid, "changed"),
            "name": lambda: self.fake.set_field(tid, "name", "Renamed"),
            "attachment add": lambda: self.fake.add_attachment(tid, COWORKER, "b.txt"),
            "attachment remove": lambda: self.fake.remove_attachment(tid, att),
            "deletion": lambda: self.fake.delete_comment(old),
        }
        for label, change in changes.items():
            self.clock.advance(1000)  # L and F both due
            self.scan()
            self.assertTrue(observe.barrier_confirmed(self.task(tid), self.clock()), label + " (baseline)")
            change()
            self.clock.advance(1000)
            self.scan()
            row = self.task(tid)
            self.assertEqual(row["quiet_until_ms"], self.clock() + 180_000, label)
            self.assertFalse(observe.barrier_confirmed(row, self.clock()), label)

    def test_unchanged_bot_items_status_and_fields_do_not_restart_quiet(self):
        tid = self.track()  # new_quiet: L every scan
        self.clock.advance(60)
        self.scan()
        quiet = self.task(tid)["quiet_until_ms"]
        changes = [lambda: None,
                   lambda: self.fake.add_comment(tid, BOT, "bot post"),
                   lambda: self.fake.add_attachment(tid, BOT, "plan-r1.md"),
                   lambda: self.fake.set_status(tid, "business requirements"),
                   lambda: self.fake.set_field(tid, "assignees", [{"id": COWORKER}])]
        for i, change in enumerate(changes):
            change()
            self.clock.advance(60 if i % 2 else 1000)
            self.scan()
            row = self.task(tid)
            self.assertEqual(row["quiet_until_ms"], quiet, i)
            self.assertTrue(observe.barrier_confirmed(row, self.clock()), i)

    def test_five_item_burst_is_one_batch(self):
        self.fake.add_task("t1", created_ms=self.clock())
        self.scan()
        confirmations = []
        for i in range(5):
            self.clock.advance(60)
            self.fake.add_comment("t1", COWORKER, f"part {i}", mention=BOT if i == 2 else None)
            self.scan()
            confirmations.append(observe.barrier_confirmed(self.task("t1"), self.clock()))
        self.clock.advance(179)
        self.scan()
        confirmations.append(observe.barrier_confirmed(self.task("t1"), self.clock()))
        self.clock.advance(1)
        self.scan()
        confirmations.append(observe.barrier_confirmed(self.task("t1"), self.clock()))
        self.assertEqual(confirmations, [False] * 6 + [True])
        items = self.items("t1", "comment")
        self.assertEqual(len(items), 5)
        self.assertEqual(sum(1 for i in items.values() if i["gen"] is not None), 1)  # only the addressed one
        self.assertEqual(self.task("t1")["input_gen"], 1)

    def test_barrier_requires_light_pass_in_the_dispatching_scan(self):
        tid = self.track(phase="planning")
        self.clock.advance(1000)
        self.scan()
        self.assertTrue(observe.barrier_confirmed(self.task(tid), self.clock()))
        self.clock.advance(60)  # active cadence: no L due this scan
        self.scan()
        self.assertFalse(observe.barrier_confirmed(self.task(tid), self.clock()))

    def test_barrier_predicate_full_start_and_anchor(self):
        now = self.clock()
        row = {"phase": "planning", "terminal": 0, "hold_reason": None, "quiet_until_ms": now - 1000,
               "barrier_ok_ms": now - 500, "last_light_ms": now}
        self.assertTrue(observe.barrier_confirmed(row, now))
        self.assertFalse(observe.barrier_confirmed({**row, "barrier_ok_ms": now - 1001}, now), "F began inside quiet")
        self.assertFalse(observe.barrier_confirmed(row, now, anchor_ms=now - 499), "F began before the anchor")
        self.assertTrue(observe.barrier_confirmed(row, now, anchor_ms=now - 500))
        self.assertFalse(observe.barrier_confirmed({**row, "quiet_until_ms": now + 1}, now))
        for change in ({"hold_reason": "caps"}, {"terminal": 1}, {"phase": "not_admitted"}, {"barrier_ok_ms": None}):
            self.assertFalse(observe.barrier_confirmed({**row, **change}, now), change)


# ---------------------------------------------------------------------------------------------- T4
class HistoryTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        self.ids = [self.fake.add_comment("t1", COWORKER, f"c{i}", date_ms=self.clock() - 500_000 + i)
                    for i in range(30)]
        self.scan()
        self.store.observe_commit("t1", [], {"phase": "planning"})

    def comment_calls(self):
        return [c for c in self.fake.calls if c[1] == "/v2/task/t1/comment"]

    def test_more_than_25_comments_via_start_and_start_id(self):
        calls = self.comment_calls()
        self.assertEqual(len(calls), 2)
        oldest_first_page = self.fake._find(self.ids[5])  # newest-first: page 1 holds c29..c5
        self.assertEqual(calls[1][2], {"start": int(oldest_first_page["date"]), "start_id": self.ids[5]})
        self.assertEqual(len(self.items("t1", "comment")), 30)

    def next_full(self):
        self.clock.advance(15 * 60)
        self.scan()

    def test_old_comment_edit_detected_by_next_full_not_by_light(self):
        self.clock.advance(1000)
        self.scan()
        self.fake.edit_comment(self.ids[1], "c1 edited")
        self.clock.advance(5 * 60)  # L due, F not yet
        self.scan()
        self.assertEqual(self.items("t1")[self.ids[1]]["cur_hash"], self.items("t1")[self.ids[1]]["first_hash"])
        self.next_full()
        it = self.items("t1")[self.ids[1]]
        self.assertNotEqual(it["cur_hash"], it["first_hash"])
        self.assertEqual(self.task("t1")["quiet_until_ms"], self.clock() + 180_000)

    def test_new_reply_and_mention_reply_under_old_thread(self):
        r1 = self.fake.add_comment("t1", COWORKER, "plain reply", reply_to=self.ids[0])
        self.next_full()
        self.assertIn(r1, self.items("t1", "reply"))
        self.assertIsNone(self.items("t1", "reply")[r1]["gen"])
        gen = self.task("t1")["input_gen"]
        r2 = self.fake.add_comment("t1", APPROVER_A, " question?", mention=BOT, reply_to=self.ids[2])
        self.next_full()
        rep = self.items("t1", "reply")[r2]
        self.assertEqual((rep["parent_id"], rep["cur_mentions_bot"], rep["gen"]), (self.ids[2], 1, gen + 1))
        self.assertEqual(self.task("t1")["input_gen"], gen + 1)

    def test_reply_edit_and_deletions(self):
        r1 = self.fake.add_comment("t1", COWORKER, "reply", reply_to=self.ids[3])
        self.next_full()
        self.fake.edit_comment(r1, "reply edited")
        self.next_full()
        rep = self.items("t1", "reply")[r1]
        self.assertNotEqual(rep["cur_hash"], rep["first_hash"])
        self.fake.delete_comment(r1)
        self.fake.delete_comment(self.ids[4])
        self.next_full()
        self.assertEqual(self.items("t1", "reply")[r1]["deleted"], 1)
        self.assertEqual(self.items("t1", "comment")[self.ids[4]]["deleted"], 1)
        self.assertEqual(self.task("t1")["quiet_until_ms"], self.clock() + 180_000)

    def test_mention_added_by_edit_is_material(self):
        gen = self.task("t1")["input_gen"]
        self.fake.edit_comment(self.ids[6], " now asking", mention=BOT)
        self.next_full()
        self.assertEqual(self.items("t1")[self.ids[6]]["gen"], gen + 1)

    def test_comment_page_cap_puts_task_on_hold(self):
        self.cfg = from_dict({"cutover_ms": self.cfg.cutover_ms, "budgets": {"comment_pages_cap": 1}})
        self.next_full()
        row = self.task("t1")
        self.assertEqual(row["hold_reason"], "caps")
        n = len(task_get_calls(self.fake, "t1"))
        self.next_full()
        self.assertEqual(len(task_get_calls(self.fake, "t1")), n, "held task is not polled")


# ---------------------------------------------------------------------------------------------- T5
class AttachmentObservationTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        self.scan()
        self.store.observe_commit("t1", [], {"phase": "planning"})

    def step(self):
        self.clock.advance(16 * 60)
        self.scan()
        return self.task("t1")

    def test_human_add_remove_is_material_bot_upload_ignored(self):
        gen = self.task("t1")["input_gen"]
        a = self.fake.add_attachment("t1", COWORKER, "spec.pdf")
        row = self.step()
        self.assertEqual(row["input_gen"], gen + 1)
        self.assertEqual(self.items("t1", "attachment")[a]["gen"], gen + 1)
        h = row["attach_ids_hash"]
        self.fake.add_attachment("t1", BOT, "plan-r1-abc.md")
        row = self.step()
        self.assertEqual((row["input_gen"], row["attach_ids_hash"]), (gen + 1, h))
        self.assertNotEqual(row["quiet_until_ms"], self.clock() + 180_000)
        self.fake.remove_attachment("t1", a)
        row = self.step()
        self.assertEqual(row["input_gen"], gen + 2)
        self.assertEqual(self.items("t1", "attachment")[a]["deleted"], 1)

    def test_hidden_or_deleted_flag_counts_as_removal(self):
        for flag in ("hidden", "deleted"):
            a = self.fake.add_attachment("t1", COWORKER, flag + ".txt")
            self.step()
            gen = self.task("t1")["input_gen"]
            next(x for x in self.fake.tasks["t1"]["attachments"] if x["id"] == a)[flag] = True
            row = self.step()
            self.assertEqual(row["input_gen"], gen + 1, flag)
            self.assertEqual(self.items("t1", "attachment")[a]["deleted"], 1, flag)


class _Resp:
    def __init__(self, data=b"{}", status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, responses=None):
        self.requests, self.responses = [], list(responses or [])

    def open(self, req, timeout=None):
        self.requests.append(req)
        r = self.responses.pop(0) if self.responses else _Resp()
        if isinstance(r, BaseException):
            raise r
        return r


class TransportAndStagingTest(Base):
    TOKEN = "pk_test_FAKE_TOKEN_VALUE"

    def test_token_sent_only_to_api_host(self):
        opener = _Opener([_Resp(b'{"id": "t"}', headers={"X-RateLimit-Remaining": "88"})])
        t = clickup_api.HttpTransport(self.TOKEN, opener=opener)
        r = t.request("GET", "/v2/task/abc", params={"statuses[]": ["a b", "c"], "page": 0})
        self.assertEqual((r.outcome, r.json, r.headers["x-ratelimit-remaining"]), (Outcome.OK, {"id": "t"}, "88"))
        req = opener.requests[0]
        self.assertEqual(req.full_url, "https://api.clickup.com/api/v2/task/abc?statuses%5B%5D=a+b&statuses%5B%5D=c&page=0")
        self.assertEqual(req.get_header("Authorization"), self.TOKEN)
        for bad in ("https://evil.example/v2/task/x", "//evil.example/v2/task/x", "/v2/../evil", "/v3/task/x",
                    "/v2/task/x?y=1", "/v2/task/x@evil.example", "v2/task/x", None):
            self.assertEqual(t.request("GET", bad).outcome, Outcome.DEFINITE_FAIL, bad)
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(self.TOKEN, repr(t))

    def test_redirects_are_never_followed(self):
        opener = clickup_api.build_api_opener()
        self.assertTrue(any(isinstance(h, clickup_api._NoRedirect) for h in opener.handlers))
        self.assertIsNone(clickup_api._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x.example/"))
        err = urllib.error.HTTPError("https://api.clickup.com/api/v2/task/a", 302, "Found",
                                     {"Location": "https://evil.example/"}, None)
        t = clickup_api.HttpTransport(self.TOKEN, opener=_Opener([err]))
        self.assertEqual(t.request("POST", "/v2/task/a/comment", body={"comment_text": "x"}).outcome,
                         Outcome.AMBIGUOUS)

    def test_outcome_classification(self):
        import socket

        def outcome(exc_or_resp):
            return clickup_api.HttpTransport(self.TOKEN, opener=_Opener([exc_or_resp])).request("GET", "/v2/task/a")

        def http(code):
            return urllib.error.HTTPError("u", code, "m", {}, None)
        self.assertEqual(outcome(http(401)).outcome, Outcome.AUTH_FAIL)
        self.assertEqual(outcome(http(403)).outcome, Outcome.AUTH_FAIL)
        self.assertEqual(outcome(http(429)).outcome, Outcome.RATE_LIMITED)
        for code in (408, 409, 500, 502, 503):
            self.assertEqual(outcome(http(code)).outcome, Outcome.AMBIGUOUS, code)
        for code in (400, 404):
            r = outcome(http(code))
            self.assertEqual((r.outcome, r.status), (Outcome.DEFINITE_FAIL, code))
        for reason in (ConnectionRefusedError(), socket.gaierror(), TimeoutError()):
            self.assertEqual(outcome(urllib.error.URLError(reason)).outcome, Outcome.DEFINITE_FAIL, reason)
        self.assertEqual(outcome(urllib.error.URLError(ConnectionResetError())).outcome, Outcome.AMBIGUOUS)
        self.assertEqual(outcome(TimeoutError("read timed out")).outcome, Outcome.AMBIGUOUS)
        self.assertEqual(outcome(_Resp(b"<html>")).outcome, Outcome.AMBIGUOUS)

    def test_stage_attachments_caps_https_and_no_token(self):
        mb = 1024 * 1024
        atts = [
            {"id": 1, "title": "a.txt", "size": 5, "user": {"id": COWORKER}, "url": "https://t1.example/a"},
            {"id": 2, "title": "big.bin", "size": 11 * mb, "user": {"id": COWORKER}, "url": "https://t1.example/b"},
            {"id": 3, "title": "liar.bin", "size": 1, "user": {"id": COWORKER}, "url": "https://t1.example/c"},
            {"id": 4, "title": "h.txt", "size": 1, "user": {"id": COWORKER}, "url": "https://t1.example/d", "hidden": True},
            {"id": 5, "title": "d.txt", "size": 1, "user": {"id": COWORKER}, "url": "https://t1.example/e", "deleted": True},
            {"id": 6, "title": "plan.md", "size": 1, "user": {"id": BOT}, "url": "https://t1.example/f"},
            {"id": 7, "title": "http.txt", "size": 1, "user": {"id": COWORKER}, "url": "http://t1.example/g"},
            {"id": 8, "title": "nine.bin", "size": 9 * mb, "user": {"id": COWORKER}, "url": "https://t1.example/h"},
            {"id": 9, "title": "nine2.bin", "size": 9 * mb, "user": {"id": COWORKER}, "url": "https://t1.example/i"},
            {"id": 10, "title": "three.bin", "size": 3 * mb, "user": {"id": COWORKER}, "url": "https://t1.example/j"},
        ]
        opener = _Opener([_Resp(b"hello"), _Resp(b"x" * (10 * mb + 1)), _Resp(b"y" * 9 * mb), _Resp(b"z" * 9 * mb)])
        out = clickup_api.stage_attachments(atts, self.dir, BOT, opener=opener)
        reasons = {r["id"]: r["reason"] for r in out}
        self.assertEqual(reasons, {"1": "staged", "2": "too_large", "3": "too_large", "4": "hidden", "5": "deleted",
                                   "6": "bot", "7": "not_https", "8": "staged", "9": "staged", "10": "total_cap"})
        self.assertEqual(open(os.path.join(self.dir, "1-a.txt"), "rb").read(), b"hello")
        self.assertEqual(len(opener.requests), 4)
        for req in opener.requests:
            self.assertIsNone(req.get_header("Authorization"))
            self.assertTrue(req.full_url.startswith("https://"))

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_dpapi_hex_round_trip_in_memory(self):
        # Synthetic dummy in the installed credential's stored form: UTF-16LE plaintext (E2E-F1).
        token = "pk_12345678_SYNTHETICDUMMYNOTAREALTOKEN0"
        secret = token.encode("utf-16-le")
        blob_in = clickup_api._Blob(len(secret), ctypes.cast(ctypes.create_string_buffer(secret, len(secret)),
                                                             ctypes.POINTER(ctypes.c_char)))
        blob_out = clickup_api._Blob()
        self.assertTrue(ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                                               ctypes.byref(blob_out)))
        hexblob = ctypes.string_at(blob_out.pbData, blob_out.cbData).hex()
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        decoded = clickup_api.unprotect_hex(hexblob)
        self.assertNotIn("\x00", decoded)
        self.assertRegex(decoded, r"\Apk_[0-9]+_[A-Za-z0-9]+\Z")
        self.assertEqual(decoded, token)
        with self.assertRaises(OSError):
            clickup_api.unprotect_hex("00" * 16)


# --------------------------------------------------------------------------------------------- T12
class BudgetTest(Base):
    def cfg_with(self, **budgets):
        return from_dict({"cutover_ms": self.cfg.cutover_ms, "budgets": budgets})

    def guarded_scan(self, cfg=None):
        cfg = cfg or self.cfg
        guard = clickup_api.ApiGuard(self.store, cfg, self.clock())
        pre = observe.preflight_statuses(guard.transport(self.fake), cfg)
        obs = observe.observe_scan(self.fake, self.store, cfg, self.clock(), guard=guard) if pre.ok else None
        return guard, pre, obs

    def track(self, tid, phase="planning", **fields):
        self.fake.add_task(tid, created_ms=self.clock() - 600_000)
        self.scan()
        self.store.observe_commit(tid, [], {"phase": phase, **fields})

    def test_40_request_cap_counts_the_preflight(self):
        for i in range(30):
            self.fake.add_task(f"t{i:02}", created_ms=self.clock() - 600_000 + i)
        guard, pre, obs = self.guarded_scan()
        self.assertEqual(len(self.fake.calls), 40)
        self.assertEqual(guard.used, 40)
        self.assertTrue(obs.deferred)
        self.assertEqual(self.fake.calls[0][1], f"/v2/list/{LIST_ID}")

    def test_rate_limit_remaining_reserve_stops_the_scan(self):
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        self.fake.rate_remaining = 19
        self.guarded_scan()
        self.assertEqual(len(self.fake.calls), 1)  # preflight only; P and passes wait for the next scan

    def test_429_stops_until_reset_time(self):
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        reset = str(self.clock() // 1000 + 120)
        self.fake.inject("GET", "/task$", Outcome.RATE_LIMITED, status=429, headers={"x-ratelimit-reset": reset})
        self.guarded_scan()
        n = len(self.fake.calls)
        self.clock.advance(60)
        guard, pre, obs = self.guarded_scan()
        self.assertEqual(len(self.fake.calls), n)
        self.assertEqual(pre.status, "ERROR")
        self.clock.advance(61)
        self.guarded_scan()
        self.assertGreater(len(self.fake.calls), n)

    def test_per_task_cap_resumes_full_pass_from_cursor(self):
        self.fake.add_task("big", created_ms=self.clock() - 600_000)
        for i in range(20 * 25):
            self.fake.add_comment("big", COWORKER, f"c{i}", date_ms=self.clock() - 590_000 + i)
        self.fake.add_task("small", created_ms=self.clock() - 500_000)
        self.scan()
        big_calls = [c for c in self.fake.calls if c[1].startswith("/v2/task/big")]
        self.assertEqual(len(big_calls), 15)
        self.assertIsNotNone(self.task("big")["full_cursor"])
        self.assertIsNone(self.task("big")["last_full_ms"])
        self.assertIsNotNone(self.task("small")["last_full_ms"], "cap is per task: others still served")
        self.clock.advance(60)
        self.scan()
        resumed = [c for c in self.fake.calls if c[1] == "/v2/task/big/comment"][14]
        self.assertIn("start_id", resumed[2])
        self.assertIsNone(self.task("big")["full_cursor"])
        self.assertEqual(len(self.items("big", "comment")), 500)

    def test_comment_first_stored_during_a_multi_scan_full_pass_is_not_deleted(self):
        self.fake.add_task("big", created_ms=self.clock() - 600_000)
        for i in range(20 * 25):
            self.fake.add_comment("big", COWORKER, f"c{i}", date_ms=self.clock() - 590_000 + i)
        self.scan()
        self.store.observe_commit("big", [], {"phase": "promotion_window", "full_cursor": None, "last_full_ms": None})
        self.store.conn.execute("INSERT INTO approvals(task, rev, accounts_gen, state, ticket_epoch, ticket_input_gen,"
                                " ticket_renewed_ms) VALUES ('big', 1, 0, 'ticketed', 1, 0, ?)", (self.clock(),))
        self.clock.advance(60)
        self.scan()  # live ticket: L, then an F that the per-task cap cuts short
        self.assertIsNotNone(self.task("big")["full_cursor"])
        new = self.fake.add_comment("big", COWORKER, "arrived mid-F")
        self.clock.advance(60)
        self.scan()  # L stores the new comment first, then the F completes without having paged it
        self.assertIsNone(self.task("big")["full_cursor"])
        self.assertEqual(self.items("big", "comment")[new]["deleted"], 0)

    def test_live_ticket_task_goes_first(self):
        self.track("old", last_light_ms=self.clock() - 10_000_000)
        self.track("live", phase="promotion_window")
        self.store.conn.execute("INSERT INTO approvals(task, rev, accounts_gen, state, ticket_epoch, ticket_input_gen,"
                                " ticket_renewed_ms) VALUES ('live', 1, 0, 'ticketed', 1, 0, ?)", (self.clock(),))
        self.clock.advance(400)
        self.fake.calls.clear()
        self.guarded_scan(self.cfg_with(scan_requests=4))
        touched = [c[1] for c in self.fake.calls if c[1].startswith("/v2/task/")]
        self.assertTrue(touched and all(p.startswith("/v2/task/live") for p in touched), touched)

    def test_oldest_due_light_pass_first(self):
        for tid in ("a", "b", "c"):
            self.track(tid)
        for tid, age in (("a", 400), ("b", 900), ("c", 600)):
            self.store.observe_commit(tid, [], {"last_light_ms": self.clock() - age * 1000,
                                                "last_full_ms": self.clock()})
        self.fake.calls.clear()
        cfg = self.cfg_with(scan_requests=4)  # preflight + P + exactly one L (GET Task + newest page)
        order = []
        for _ in range(3):
            before = len(self.fake.calls)
            self.guarded_scan(cfg)
            order += [c[1].split("/")[3] for c in self.fake.calls[before:] if c[1].startswith("/v2/task/")][:1]
        self.assertEqual(order, ["b", "c", "a"])

    def test_get_5xx_backoff_alert_after_15_minutes_and_one_recovery_line(self):
        alerts = []
        expected_gaps = [1, 2, 5, 10, 30]
        for gap in expected_gaps:
            self.fake.inject("GET", f"/v2/list/{LIST_ID}$", Outcome.AMBIGUOUS, status=503)
            guard, pre, _ = self.guarded_scan()
            alerts += guard.alerts
            self.assertEqual(pre.status, "ERROR")
            n = len(self.fake.calls)
            self.clock.advance(gap * 60 - 1)
            guard, _, _ = self.guarded_scan()
            self.assertEqual(len(self.fake.calls), n, f"still backing off after {gap} min - 1 s")
            alerts += guard.alerts
            self.clock.advance(1)
        self.assertEqual(alerts, ["ClickUp API failing (5xx/timeout) for 15+ minutes; backing off"])
        guard, pre, _ = self.guarded_scan()
        self.assertTrue(pre.ok)
        self.assertEqual(guard.alerts, ["ClickUp API recovered"])

    def test_401_alert_once_silent_30_minute_retry_and_recovery(self):
        self.fake.inject("GET", f"/v2/list/{LIST_ID}$", Outcome.AUTH_FAIL, status=401)
        guard, _, _ = self.guarded_scan()
        self.assertEqual(len(guard.alerts), 1)
        self.assertIn("401/403", guard.alerts[0])
        n = len(self.fake.calls)
        for _ in range(29):
            self.clock.advance(60)
            guard, _, _ = self.guarded_scan()
            self.assertEqual((len(self.fake.calls), guard.alerts), (n, []))
        self.clock.advance(60)
        self.fake.inject("GET", f"/v2/list/{LIST_ID}$", Outcome.AUTH_FAIL, status=403)
        guard, _, _ = self.guarded_scan()
        self.assertEqual((len(self.fake.calls), guard.alerts), (n + 1, []))  # one silent probe
        self.clock.advance(30 * 60)
        guard, pre, _ = self.guarded_scan()
        self.assertTrue(pre.ok)
        self.assertEqual(guard.alerts, ["ClickUp authentication recovered"])

    def test_budget_exhausted_before_barrier_completion_confirms_nothing(self):
        self.fake.add_task("t1", created_ms=self.clock())
        for i in range(60):
            self.fake.add_comment("t1", COWORKER, f"c{i}", date_ms=self.clock() - 1000 + i)
        self.scan()
        self.clock.advance(181)
        self.store.observe_commit("t1", [], {"barrier_ok_ms": None})
        cfg = self.cfg_with(scan_requests=4)  # preflight + P + GET Task + 1 page: F cannot complete
        guard, _, obs = self.guarded_scan(cfg)
        self.assertTrue(obs.deferred)
        self.assertFalse(observe.barrier_confirmed(self.task("t1"), self.clock()))

    def test_deferral_alert_after_30_minutes(self):
        for i in range(30):
            self.fake.add_task(f"t{i:02}", created_ms=self.clock() - 600_000 + i)
        alerts = []
        cfg = self.cfg_with(scan_requests=3)
        for _ in range(31):
            guard, _, _ = self.guarded_scan(cfg)
            alerts += guard.alerts
            self.clock.advance(60)
        self.assertEqual(alerts, ["ClickUp request budget exhausted for 30+ minutes; observation is delayed"])


# --------------------------------------------------------------------------------------------- T15
class EffectsTest(Base):
    def setUp(self):
        super().setUp()
        self.fake.add_task("t1", created_ms=self.clock() - 600_000)
        self.root = self.fake.add_comment("t1", COWORKER, "question")

    def enqueue(self, key, kind, target, body, task="t1"):
        self.store.conn.execute("INSERT INTO outbox(idem_key, task, kind, target, body) VALUES (?,?,?,?,?)",
                                (key, task, kind, target, json.dumps(body)))

    def row(self, key):
        return dict(self.store.conn.execute("SELECT * FROM outbox WHERE idem_key=?", (key,)).fetchone())

    def run_effect(self, key, precheck=None, transport=None):
        return clickup_effects.execute(transport or self.fake, self.store, self.cfg, key, self.clock(),
                                       precheck=precheck or (lambda row: True))

    def bot_comments(self):
        return [c for c in self.fake.comments["t1"] if c["user"]["id"] == BOT]

    def posts(self, pattern):
        return self.fake.count("POST", pattern)

    def test_comment_carries_ref_footer_and_notify_all_false(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "Hello"})
        r = self.run_effect("k1")
        self.assertEqual(r.state, "done")
        body = [b for c, b in zip(self.fake.calls, self.fake.bodies) if c[0] == "POST"]
        self.assertEqual(len(body), 1)
        self.assertIs(body[0]["notify_all"], False)
        posted = self.bot_comments()[0]["comment_text"]
        self.assertEqual(posted, "Hello\n\nref: " + clickup_effects.effect_ref("k1"))
        self.assertEqual(self.row("k1")["result_ref"], self.bot_comments()[0]["id"])

    def test_comment_timeout_committed_after_first_reconciliation_read_is_found_later_without_resend(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "Answer"})
        self.fake.inject("POST", "/v2/task/t1/comment$", Outcome.AMBIGUOUS, commit=True,
                         commit_at_ms=self.clock() + 5 * 60_000)
        self.assertEqual(self.run_effect("k1").state, "ambiguous")
        self.clock.advance(2 * 60)
        self.assertEqual(self.run_effect("k1").state, "ambiguous")  # first read: not there yet
        self.assertEqual(self.bot_comments(), [])
        self.clock.advance(4 * 60)
        self.assertEqual(self.run_effect("k1").state, "ambiguous")  # not due before +10 min
        self.clock.advance(4 * 60)
        r = self.run_effect("k1")
        self.assertEqual(r.state, "done")
        self.assertEqual(self.posts("/comment$"), 1)
        self.assertEqual(len(self.bot_comments()), 1)
        self.assertEqual(self.row("k1")["result_ref"], self.bot_comments()[0]["id"])

    def test_never_found_is_held_ambiguous_with_alert_hold_and_no_resend(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "Answer"})
        self.fake.inject("POST", "/comment$", Outcome.AMBIGUOUS)
        self.run_effect("k1")
        for minutes in (2, 8, 20):
            self.clock.advance(minutes * 60)
            r = self.run_effect("k1")
        self.assertEqual(r.state, "held_ambiguous")
        self.assertEqual(len(r.alerts), 1)
        self.assertIn("k1", r.alerts[0])
        self.assertTrue(self.task("t1")["hold_reason"])
        self.clock.advance(3600)
        self.assertEqual(self.run_effect("k1").state, "held_ambiguous")
        self.assertEqual(self.posts("/comment$"), 1)

    def test_reply_ambiguous_reconciled_from_root_replies(self):
        self.enqueue("k2", "clickup_reply", self.root, {"text": "Thread answer"})
        self.fake.inject("POST", f"/v2/comment/{self.root}/reply$", Outcome.AMBIGUOUS, commit=True)
        self.assertEqual(self.run_effect("k2").state, "ambiguous")
        self.clock.advance(120)
        self.assertEqual(self.run_effect("k2").state, "done")
        self.assertEqual(self.posts("/reply$"), 1)
        self.assertEqual(len(self.fake.replies[self.root]), 1)

    def test_attachment_ambiguous_reconciled_by_ref6_in_filename(self):
        path = os.path.join(self.dir, "plan.md")
        with open(path, "wb") as f:
            f.write(b"# plan")
        self.enqueue("k3", "clickup_attachment", "t1", {"filename": "plan-r1-0123456789ab.md", "path": path})
        self.fake.inject("POST", "/attachment$", Outcome.AMBIGUOUS, commit=True)
        self.assertEqual(self.run_effect("k3").state, "ambiguous")
        self.clock.advance(120)
        self.assertEqual(self.run_effect("k3").state, "done")
        title = self.fake.tasks["t1"]["attachments"][0]["title"]
        self.assertIn(clickup_effects.effect_ref("k3")[3:9], title)
        self.assertEqual(self.posts("/attachment$"), 1)

    def test_connect_failure_retries_with_backoff_then_fails_with_hold(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "x"})
        self.fake.inject("POST", "/comment$", Outcome.DEFINITE_FAIL)
        self.assertEqual(self.run_effect("k1").state, "retry_wait")
        self.assertEqual(self.row("k1")["next_attempt_ms"], self.clock() + 60_000)
        self.clock.advance(60)
        self.assertEqual(self.run_effect("k1").state, "done")
        self.assertEqual(len(self.bot_comments()), 1)

        self.enqueue("k9", "clickup_comment", "t1", {"text": "y"})
        gaps = [1, 2, 5, 10, 30]
        for i in range(6):
            self.fake.inject("POST", "/comment$", Outcome.DEFINITE_FAIL)
            r = self.run_effect("k9")
            if i < 5:
                self.assertEqual((r.state, self.row("k9")["next_attempt_ms"]), ("retry_wait", self.clock() + gaps[i] * 60_000))
                self.clock.advance(gaps[i] * 60 - 1)
                self.assertEqual(self.run_effect("k9").state, "retry_wait")  # not due yet: nothing sent
                self.clock.advance(1)
        self.assertEqual((r.state, self.row("k9")["attempts"]), ("failed", 6))
        self.assertEqual(len(r.alerts), 1)
        self.assertTrue(self.task("t1")["hold_reason"])
        self.assertEqual(self.posts("/comment$"), 8)

    def test_ambiguous_status_put_is_verified_by_get(self):
        self.enqueue("s1", "clickup_status", "t1", {"status": "business requirements"})
        self.fake.inject("PUT", "/v2/task/t1$", Outcome.AMBIGUOUS, commit=True)
        self.assertEqual(self.run_effect("s1").state, "done")
        self.assertEqual(self.fake.count("PUT"), 1)

        self.enqueue("s2", "clickup_status", "t1", {"status": "technical requirements"})
        self.fake.inject("PUT", "/v2/task/t1$", Outcome.AMBIGUOUS)
        self.assertEqual(self.run_effect("s2").state, "retry_wait")
        self.assertEqual(self.run_effect("s2").state, "done")  # retried after a passing precondition recheck
        self.assertEqual(self.fake.tasks["t1"]["status"]["status"], "technical requirements")

        self.enqueue("s3", "clickup_status", "t1", {"status": "waiting for approval"})
        self.fake.inject("PUT", "/v2/task/t1$", Outcome.AMBIGUOUS)
        self.run_effect("s3")
        self.assertEqual(self.run_effect("s3", precheck=lambda row: False).state, "cancelled_stale")
        self.assertEqual(self.fake.count("PUT"), 4)

    def test_failed_precheck_cancels_without_sending(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "x"})
        self.assertEqual(self.run_effect("k1", precheck=lambda row: False).state, "cancelled_stale")
        self.assertEqual(self.fake.writes(), [])

    def test_status_outside_mapping_or_deployed_is_never_written(self):
        for i, status in enumerate(("deployed", "implimented", "Implemented")):
            self.enqueue(f"s{i}", "clickup_status", "t1", {"status": status})
            r = self.run_effect(f"s{i}")
            self.assertEqual(r.state, "failed", status)
        self.assertEqual(self.fake.writes(), [])

    def test_crash_while_sending_is_reconciled_not_resent(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "x"})
        sent = self.clock()
        self.store.conn.execute("UPDATE outbox SET state='sending', attempts=1, next_attempt_ms=? WHERE idem_key='k1'",
                                (sent,))
        self.fake.request("POST", "/v2/task/t1/comment",
                          body={"comment_text": "x\n\nref: " + clickup_effects.effect_ref("k1")})
        self.clock.advance(120)
        self.assertEqual(self.run_effect("k1").state, "done")
        self.assertEqual(self.posts("/comment$"), 1)

    def test_budget_blocked_reconciliation_read_defers(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "x"})
        self.fake.inject("POST", "/comment$", Outcome.AMBIGUOUS, commit=True)
        self.run_effect("k1")
        self.clock.advance(120)
        guard = clickup_api.ApiGuard(self.store, from_dict({"budgets": {"scan_requests": 1}}), self.clock())
        guard.used = 1
        before = self.row("k1")
        self.assertEqual(self.run_effect("k1", transport=guard.transport(self.fake)).state, "ambiguous")
        self.assertEqual(self.row("k1"), before)
        self.assertEqual(self.run_effect("k1").state, "done")

    def test_due_rows_lists_only_due_clickup_rows(self):
        self.enqueue("k1", "clickup_comment", "t1", {"text": "x"})
        self.enqueue("k2", "clickup_comment", "t1", {"text": "y"})
        self.enqueue("kb", "kanban_comment", "card", {"text": "z"})
        self.store.conn.execute("UPDATE outbox SET state='retry_wait', next_attempt_ms=? WHERE idem_key='k2'",
                                (self.clock() + 60_000,))
        self.assertEqual(clickup_effects.due_rows(self.store, self.clock()), ["k1"])


if __name__ == "__main__":
    unittest.main()
