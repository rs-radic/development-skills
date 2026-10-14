"""S0 foundation tests: T0 (parse, ticket verdict, store) and schema tests (plan r5 §2.2, §3, §13.1)."""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from clickup_intake.config import Config, load_config
from clickup_intake.contracts import (ApprovalState, EffectIntent, Item, KanbanResult, Outcome, Phase, Preconditions,
                                      RowState, TicketSnapshot, TransportResult)
from clickup_intake.parse import PLAIN, QUOTED, UNKNOWN, parse_comment
from clickup_intake.ports import ClickUpTransport, KanbanPort
from clickup_intake.store import SCHEMA_VERSION, Store, load_ticket_snapshot, open_readonly
from clickup_intake.ticket import ticket_verdict
from fakes.fake_clock import FakeClock
from fakes.fake_kanban import FakeKanban
from fakes.fake_transport import FakeTransport

BOT = 900000001


def text(s, **attrs):
    part = {"text": s}
    if attrs:
        part["attributes"] = attrs
    return part


def tag(user_id, s="@Developer AI"):
    return {"type": "tag", "text": s, "user": {"id": user_id, "username": "Developer AI"}}


class ParseCommentTest(unittest.TestCase):
    def test_plain_text_is_plain(self):
        p = parse_comment([text("Approve plan r4 for development")], "Approve plan r4 for development", [])
        self.assertEqual(p.shape, PLAIN)
        self.assertEqual(p.norm_text, "approve plan r4 for development")
        self.assertEqual(p.mentions, set())
        self.assertEqual(p.nontext_parts, 0)

    def test_tag_plus_plain_is_plain_with_mention(self):
        parts = [tag(BOT), text(" what is the timeline?")]
        p = parse_comment(parts, "@Developer AI what is the timeline?", [])
        self.assertEqual(p.shape, PLAIN)
        self.assertEqual(p.mentions, {BOT})
        self.assertEqual(p.norm_text, "what is the timeline?")
        self.assertEqual(p.nontext_parts, 1)

    def test_blockquote_attribute_is_quoted(self):
        parts = [text("Approve plan r4 for development", blockquote={})]
        p = parse_comment(parts, "Approve plan r4 for development", [])
        self.assertEqual(p.shape, QUOTED)

    def test_code_attribute_is_quoted(self):
        for key in ("code-block", "code"):
            p = parse_comment([text("Approve plan r4 for development", **{key: True})],
                              "Approve plan r4 for development", [])
            self.assertEqual(p.shape, QUOTED, key)

    def test_gt_prefixed_line_is_quoted(self):
        body = "> Approve plan r4 for development\nlooks good"
        p = parse_comment([text(body)], body, [])
        self.assertEqual(p.shape, QUOTED)

    def test_quote_attribute_with_unknown_attribute_is_quoted(self):
        for extra in ({"block-id": "b1"}, {"bold": True}):
            parts = [text("Approve plan r4 for development", blockquote={}, **extra)]
            p = parse_comment(parts, "Approve plan r4 for development", [])
            self.assertEqual(p.shape, QUOTED, extra)

    def test_quote_attribute_with_unknown_part_type_is_quoted(self):
        parts = [text("Approve plan r4 for development", blockquote={}), {"type": "emoticon", "emoticon": {}}]
        p = parse_comment(parts, "Approve plan r4 for development", [])
        self.assertEqual(p.shape, QUOTED)

    def test_gt_line_with_unknown_part_type_is_quoted(self):
        body = "> Approve plan r4 for development\n"
        parts = [text(body), {"type": "emoticon", "emoticon": {"code": "1f600"}}]
        p = parse_comment(parts, body, [])
        self.assertEqual(p.shape, QUOTED)

    def test_unknown_part_type_is_unknown(self):
        parts = [text("Approve plan r4 for development"), {"type": "emoticon", "emoticon": {"code": "1f600"}}]
        p = parse_comment(parts, "Approve plan r4 for development", [])
        self.assertEqual(p.shape, UNKNOWN)
        self.assertEqual(p.nontext_parts, 1)

    def test_unknown_attribute_is_unknown(self):
        p = parse_comment([text("Approve plan r4 for development", bold=True)],
                          "Approve plan r4 for development", [])
        self.assertEqual(p.shape, UNKNOWN)

    def test_comment_text_disagreeing_with_parts_is_unknown(self):
        p = parse_comment([text("Approve plan r4 for development")],
                          "Approve plan r4 for development (edited elsewhere)", [])
        self.assertEqual(p.shape, UNKNOWN)

    def test_allowlisted_neutral_key_stays_plain(self):
        parts = [text("Approve plan r4 for development", **{"block-id": "block-1"})]
        self.assertEqual(parse_comment(parts, "Approve plan r4 for development", []).shape, UNKNOWN)
        self.assertEqual(parse_comment(parts, "Approve plan r4 for development", ["block-id"]).shape, PLAIN)

    def test_normalization(self):
        p = parse_comment([text("  Approve   PLAN r4\tfor development. ")], "Approve PLAN r4 for development.", [])
        self.assertEqual(p.shape, PLAIN)
        self.assertEqual(p.norm_text, "approve plan r4 for development")

    def test_lines_are_normalized_individually(self):
        body = "Thanks for the answers.\n  Business   requirements closed.\n\n"
        p = parse_comment([text(body)], body, [])
        self.assertEqual(p.lines, ["thanks for the answers", "business requirements closed"])

    def test_malformed_inputs_are_unknown(self):
        self.assertEqual(parse_comment("nope", "nope", []).shape, UNKNOWN)
        self.assertEqual(parse_comment([text("x")], None, []).shape, UNKNOWN)
        self.assertEqual(parse_comment([{"type": "tag", "user": {}}], "", []).shape, UNKNOWN)
        self.assertEqual(parse_comment([{"text": 5}], "5", []).shape, UNKNOWN)
        self.assertEqual(parse_comment(["x"], "x", []).shape, UNKNOWN)

    def test_shape_sig_tracks_part_types_and_attribute_keys(self):
        a = parse_comment([text("a")], "a", []).shape_sig
        b = parse_comment([text("b")], "b", []).shape_sig
        c = parse_comment([text("a", bold=True)], "a", []).shape_sig
        d = parse_comment([tag(BOT), text("a")], "@Developer AI a", []).shape_sig
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertRegex(a, r"^[0-9a-f]{64}$")


NOW = 1_800_000_000_000
SHA = "a" * 64
TASK = "86abc"
GATE = "t_gate01"


def snap(task_over=None, approval_over=None, extra=(), lease=1500, task_row=True):
    task = {"clickup_task_id": TASK, "input_gen": 4, "classified_gen": 4, "scope_gen": 3,
            "terminal": 0, "hold_reason": None}
    task.update(task_over or {})
    appr = {"id": 1, "task": TASK, "rev": 2, "accounts_gen": 3, "plan_sha256": SHA, "gate_card": GATE,
            "state": "ticketed", "ticket_epoch": 1, "ticket_input_gen": 4,
            "ticket_renewed_ms": NOW - 60_000, "revoked_reason": None}
    appr.update(approval_over or {})
    return TicketSnapshot(task=task if task_row else None, approvals=[appr, *extra], lease_seconds=lease)


def gate_json(status="done", gate_id=GATE, comments=()):
    return {"task": {"id": gate_id, "status": status}, "parents": [], "children": [],
            "comments": list(comments), "events": [], "runs": []}


def verdict(s, g=None, task=TASK, rev=2, sha=SHA, gate=GATE, now=NOW):
    return ticket_verdict(s, gate_json() if g is None else g, task, rev, sha, gate, now)


class TicketVerdictTest(unittest.TestCase):
    def assertInvalid(self, v, reason):
        self.assertFalse(v.valid, str(v))
        self.assertEqual(v.reason, reason)
        self.assertEqual(str(v), f"INVALID {reason}")

    def test_issued_and_fresh_is_valid(self):
        v = verdict(snap())
        self.assertTrue(v.valid)
        self.assertEqual(v.epoch, 1)
        self.assertEqual(str(v), "VALID epoch=1")

    def test_gate_show_json_text_is_accepted(self):
        import json
        self.assertTrue(verdict(snap(), json.dumps(gate_json())).valid)

    def test_input_gen_above_ticket_input_gen(self):
        self.assertInvalid(verdict(snap({"input_gen": 5, "classified_gen": 5})), "new_input")

    def test_unclassified_gen(self):
        s = snap({"input_gen": 5, "classified_gen": 4}, {"ticket_input_gen": 5})
        self.assertInvalid(verdict(s), "unclassified")

    def test_revoked_reports_revocation_reason(self):
        self.assertInvalid(verdict(snap(approval_over={"state": "revoked", "revoked_reason": "new_input"})),
                           "new_input")
        self.assertInvalid(verdict(snap(approval_over={"state": "revoked"})), "revoked")

    def test_not_ticketed(self):
        self.assertInvalid(verdict(snap(approval_over={"state": "implemented"})), "not_ticketed")

    def test_superseded_by_later_revision(self):
        later = {"id": 2, "task": TASK, "rev": 3, "accounts_gen": 4, "plan_sha256": "b" * 64,
                 "gate_card": "t_gate02", "state": "recorded"}
        self.assertInvalid(verdict(snap(extra=[later])), "superseded")

    def test_not_current_when_scope_moved_past_accounts_gen(self):
        self.assertInvalid(verdict(snap({"scope_gen": 4})), "not_current")

    def test_terminal_and_hold(self):
        self.assertInvalid(verdict(snap({"terminal": 1})), "terminal")
        self.assertInvalid(verdict(snap({"hold_reason": "operator"})), "hold")

    def test_lease(self):
        self.assertTrue(verdict(snap(approval_over={"ticket_renewed_ms": NOW - 1_500_000})).valid)
        self.assertInvalid(verdict(snap(approval_over={"ticket_renewed_ms": NOW - 1_500_001})), "lease_expired")
        self.assertInvalid(verdict(snap(approval_over={"ticket_renewed_ms": NOW - 2_000}, lease=1)), "lease_expired")

    def test_rev_sha_task_gate_mismatch(self):
        self.assertInvalid(verdict(snap(), rev=3), "rev_mismatch")
        self.assertInvalid(verdict(snap(), sha="c" * 64), "sha_mismatch")
        self.assertInvalid(verdict(snap(), task="86other"), "task_mismatch")
        self.assertInvalid(verdict(snap(), gate="t_other", g=gate_json(gate_id="t_other")), "gate_mismatch")
        self.assertInvalid(verdict(snap(), g=gate_json(gate_id="t_other")), "gate_mismatch")

    def test_gate_archived_or_blocked(self):
        for status in ("archived", "blocked", "todo", "ready", "running"):
            self.assertInvalid(verdict(snap(), g=gate_json(status=status)), "gate_not_done")

    def test_missing_rows_and_state(self):
        self.assertInvalid(verdict(None), "state_unavailable")
        self.assertInvalid(verdict(snap(task_row=False)), "task_mismatch")
        self.assertInvalid(verdict(TicketSnapshot(task=snap().task, approvals=[], lease_seconds=1500)),
                           "no_approval")

    def test_garbage_gate_json(self):
        for g in ("{not json", "[]", "null", {"task": "x"}, {"nope": 1}, 42):
            self.assertInvalid(verdict(snap(), g=g), "gate_unparseable")

    def test_malformed_snapshot_values(self):
        self.assertInvalid(verdict(snap(approval_over={"ticket_epoch": None})), "malformed")
        self.assertInvalid(verdict(snap({"input_gen": "x"})), "malformed")

    def test_audit_stamps_are_neither_trusted_nor_required(self):
        stamp = {"author": "clickup-intake", "created_at": NOW,
                 "body": f"CLICKUP-GATE v1 audit=valid task={TASK} rev=2 sha={SHA} epoch=1 input_gen=4"}
        revoked = snap(approval_over={"state": "revoked", "revoked_reason": "new_input"})
        self.assertInvalid(verdict(revoked, g=gate_json(comments=[stamp])), "new_input")
        self.assertTrue(verdict(snap(), g=gate_json(comments=[])).valid)


TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")

SCHEMA = {
    "meta": ["key", "value"],
    "tasks": ["clickup_task_id", "eligible", "requester_id", "phase", "last_status_seen", "list_fp",
              "content_hash", "attach_ids_hash", "quiet_until_ms", "input_gen", "classified_gen", "scope_gen",
              "published_rev", "published_sha256", "published_accounts_gen", "next_rev", "last_status_written",
              "last_light_ms", "last_full_ms", "last_task_get_ms", "full_cursor", "full_started_ms",
              "barrier_ok_ms", "hold_reason", "terminal"],
    "items": ["task", "kind", "id", "parent_id", "author_id", "date_ms", "first_hash", "cur_hash",
              "first_mentions_bot", "cur_mentions_bot", "shape", "shape_sig", "reply_count", "first_seen_ms",
              "deleted", "gen", "command_result", "delivered_relay"],
    "cards": ["card_id", "task", "role", "seq", "rev", "approval_id", "last_run_id", "wait_run_id", "created_key"],
    "relays": ["relay_ref", "task", "card", "gen", "item_ids", "state", "classification"],
    "kanban_seen": ["key", "card", "consumed_ms"],
    "approvals": ["id", "task", "rev", "accounts_gen", "comment_id", "author_id", "author_name", "comment_date",
                  "text_hash", "plan_sha256", "state", "orchestrator_card", "gate_card", "clickup_gates",
                  "ticket_epoch", "ticket_input_gen", "ticket_renewed_ms", "revoked_reason", "revoked_ms",
                  "audit_stamp_state", "promoted_sha"],
    "outbox": ["idem_key", "task", "kind", "target", "payload_sha256", "body", "preconditions", "depends_on",
               "state", "outcome", "attempts", "next_attempt_ms", "first_send_ms", "child_pid", "child_start_time",
               "result_ref"],
}


def comment(cid, h="h1", material=False, mentions=False, author=555):
    return Item(kind="comment", id=cid, parent_id=None, author_id=author, date_ms=NOW, hash=h,
                mentions_bot=mentions, shape="PLAIN", shape_sig="s", reply_count=0, material=material)


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        self.path = os.path.join(self.dir, "state.sqlite3")
        self.clock = FakeClock(NOW)
        self.store = Store(self.path, self.clock)
        self._conns = [self.store]

    def tearDown(self):
        for c in self._conns:
            c.close()
        shutil.rmtree(self.dir)

    def ro(self):
        conn = open_readonly(self.path)
        self._conns.append(conn)
        return conn

    def seed_ticket(self, state="ticketed", epoch=1, gen=4, classified=None, scope=3):
        """Task at input_gen ``gen`` with one approval row holding a ticket issued at ``gen``."""
        with self.store.transaction() as c:
            c.execute("INSERT INTO tasks(clickup_task_id, phase, input_gen, classified_gen, scope_gen, content_hash)"
                      " VALUES (?,?,?,?,?,?)", (TASK, "promotion_window", gen,
                                                gen if classified is None else classified, scope, "c0"))
            cur = c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, gate_card,"
                            " ticket_epoch, ticket_input_gen, ticket_renewed_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                            (TASK, 2, 3, SHA, state, GATE, epoch, gen, NOW))
        return cur.lastrowid

    def approval(self, approval_id):
        return dict(self.store.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def task_row(self):
        return dict(self.store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (TASK,)).fetchone())

    def check(self, now=None):
        s = load_ticket_snapshot(self.path, TASK, 1500)
        return ticket_verdict(s, gate_json(), TASK, 2, SHA, GATE, NOW if now is None else now)


class StoreSchemaTest(StoreTestBase):
    def test_schema_matches_plan_section_3(self):
        conn = self.store.conn
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                             " AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(tables, set(SCHEMA))
        for table, cols in SCHEMA.items():
            self.assertEqual([r[1] for r in conn.execute(f"PRAGMA table_info({table})")], cols, table)

    def test_wal_mode_and_schema_version(self):
        self.assertEqual(self.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                         str(SCHEMA_VERSION))

    def test_reopen_is_idempotent(self):
        self.seed_ticket()
        self.store.close()
        self.store = Store(self.path, self.clock)
        self._conns = [self.store]
        self.assertEqual(self.task_row()["input_gen"], 4)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM meta WHERE key='schema_version'")
                         .fetchone()[0], 1)

    def test_newer_schema_is_refused(self):
        with self.store.transaction() as c:
            c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
        self.store.close()
        self._conns = []
        with self.assertRaises(RuntimeError):
            Store(self.path, self.clock)
        self.assertIsNone(load_ticket_snapshot(self.path, TASK, 1500))

    def test_approval_unique_task_rev_accounts_gen(self):
        self.seed_ticket()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as c:
                c.execute("INSERT INTO approvals(task, rev, accounts_gen, state) VALUES (?,?,?,?)",
                          (TASK, 2, 3, "recorded"))

    def test_enum_columns_are_constrained(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as c:
                c.execute("INSERT INTO approvals(task, rev, accounts_gen, state) VALUES (?,?,?,?)",
                          (TASK, 9, 9, "bogus"))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as c:
                c.execute("INSERT INTO outbox(idem_key, task, kind, target, state) VALUES ('k','t','x','y','bogus')")

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(ZeroDivisionError):
            with self.store.transaction() as c:
                c.execute("INSERT INTO tasks(clickup_task_id) VALUES ('x1')")
                1 / 0
        self.assertIsNone(self.store.conn.execute("SELECT 1 FROM tasks WHERE clickup_task_id='x1'").fetchone())


class ObserveCommitTest(StoreTestBase):
    def test_first_sight_creates_task_row_without_raising_gen(self):
        res = self.store.observe_commit("t_new", [comment("c1")], {"phase": "new_quiet", "content_hash": "c0"})
        self.assertEqual(res.input_gen, 0)
        row = self.store.conn.execute("SELECT phase, input_gen FROM tasks WHERE clickup_task_id='t_new'").fetchone()
        self.assertEqual(tuple(row), ("new_quiet", 0))
        item = self.store.conn.execute("SELECT first_hash, cur_hash, gen, first_seen_ms FROM items"
                                       " WHERE task='t_new' AND kind='comment' AND id='c1'").fetchone()
        self.assertEqual(tuple(item), ("h1", "h1", None, NOW))

    def test_material_items_and_content_change_stamp_increasing_gens(self):
        self.store.observe_commit("t1", [], {"content_hash": "c0"})
        res = self.store.observe_commit("t1", [comment("c1", material=True, mentions=True), comment("c2"),
                                               comment("c3", material=True, mentions=True)],
                                        {"content_hash": "c1"})
        self.assertEqual(res.input_gen, 3)
        gens = dict(self.store.conn.execute("SELECT id, gen FROM items WHERE task='t1'").fetchall())
        self.assertEqual(gens, {"c1": 2, "c2": None, "c3": 3})

    def test_int_requester_id_round_trips_as_int(self):
        self.store.observe_commit("t1", [], {"requester_id": 900000101})
        val = self.store.conn.execute("SELECT requester_id FROM tasks WHERE clickup_task_id='t1'").fetchone()[0]
        self.assertIs(type(val), int)
        self.assertEqual(val, 900000101)

    def test_edit_keeps_first_hash(self):
        self.store.observe_commit("t1", [comment("c1", h="h1")], {})
        self.clock.advance(60)
        self.store.observe_commit("t1", [comment("c1", h="h2", mentions=True, material=True)], {})
        row = self.store.conn.execute("SELECT first_hash, cur_hash, first_mentions_bot, cur_mentions_bot, gen,"
                                      " first_seen_ms FROM items WHERE id='c1'").fetchone()
        self.assertEqual(tuple(row), ("h1", "h2", 0, 1, 1, NOW))

    def test_store_managed_fields_are_refused(self):
        for key in ("input_gen", "classified_gen", "scope_gen", "clickup_task_id", "nope"):
            with self.assertRaises(ValueError):
                self.store.observe_commit("t1", [], {key: 1})

    def test_raises_input_gen_and_revokes_ticket_in_one_transaction(self):
        aid = self.seed_ticket()
        self.assertTrue(self.check().valid)
        self.clock.advance(5)
        res = self.store.observe_commit(TASK, [comment("c9", material=True, mentions=True)], {})
        self.assertEqual(res.input_gen, 5)
        self.assertEqual(res.revoked, [aid])
        a = self.approval(aid)
        self.assertEqual((a["state"], a["revoked_reason"], a["revoked_ms"]), ("revoked", "new_input", NOW + 5000))
        self.assertEqual(str(self.check()), "INVALID new_input")

    def test_non_material_observation_keeps_ticket(self):
        aid = self.seed_ticket()
        res = self.store.observe_commit(TASK, [comment("c9")], {"last_light_ms": NOW})
        self.assertEqual(res.revoked, [])
        self.assertEqual(self.approval(aid)["state"], "ticketed")
        self.assertTrue(self.check().valid)

    def test_terminal_and_hold_revoke(self):
        aid = self.seed_ticket()
        self.store.observe_commit(TASK, [], {"terminal": 1, "phase": "stopped"})
        self.assertEqual(self.approval(aid)["revoked_reason"], "terminal")
        self.assertFalse(self.check().valid)

        self.tearDown()
        self.setUp()
        aid = self.seed_ticket()
        self.store.observe_commit(TASK, [], {"hold_reason": "comment_page_cap"})
        self.assertEqual(self.approval(aid)["revoked_reason"], "hold")
        self.assertFalse(self.check().valid)

    def test_non_ticketed_approvals_untouched(self):
        aid = self.seed_ticket(state="implemented")
        self.store.observe_commit(TASK, [comment("c9", material=True, mentions=True)], {})
        self.assertEqual(self.approval(aid)["state"], "implemented")

    def test_failure_inside_transaction_rolls_back_everything(self):
        aid = self.seed_ticket()
        with mock.patch("clickup_intake.store.revoke_live_tickets", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.store.observe_commit(TASK, [comment("c9", material=True, mentions=True)], {})
        self.assertEqual(self.task_row()["input_gen"], 4)
        self.assertIsNone(self.store.conn.execute("SELECT 1 FROM items WHERE id='c9'").fetchone())
        self.assertEqual(self.approval(aid)["state"], "ticketed")

    def test_kill_right_after_commit_leaves_ticket_invalid(self):
        aid = self.seed_ticket()
        self.store.close()
        self._conns = []
        script = (
            "import os, sys\n"
            "from clickup_intake.contracts import Item\n"
            "from clickup_intake.store import Store\n"
            f"s = Store({self.path!r}, lambda: {NOW + 1000})\n"
            "s.observe_commit(%r, [Item(kind='comment', id='c9', parent_id=None, author_id=555, date_ms=1,"
            " hash='h', mentions_bot=True, material=True)], {})\n"
            "os._exit(9)\n" % TASK)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 9, proc.stderr)
        self.assertEqual(str(self.check()), "INVALID new_input")
        self.store = Store(self.path, self.clock)
        self._conns = [self.store]
        self.assertEqual(self.approval(aid)["state"], "revoked")
        self.assertEqual(self.task_row()["input_gen"], 5)


class RenewTicketTest(StoreTestBase):
    def test_renew_current_epoch(self):
        aid = self.seed_ticket()
        self.assertTrue(self.store.renew_ticket(aid, 1, NOW + 600_000))
        self.assertEqual(self.approval(aid)["ticket_renewed_ms"], NOW + 600_000)
        self.assertTrue(self.check(now=NOW + 2_000_000).valid)

    def test_stale_epoch_updates_nothing(self):
        aid = self.seed_ticket(epoch=2)
        self.assertFalse(self.store.renew_ticket(aid, 1, NOW + 600_000))
        self.assertEqual(self.approval(aid)["ticket_renewed_ms"], NOW)

    def test_lower_ticket_input_gen_updates_nothing(self):
        aid = self.seed_ticket()
        with self.store.transaction() as c:  # newer input with the ticket row not (yet) revoked
            c.execute("UPDATE tasks SET input_gen=5, classified_gen=5 WHERE clickup_task_id=?", (TASK,))
        self.assertFalse(self.store.renew_ticket(aid, 1, NOW + 600_000))
        self.assertEqual(self.approval(aid)["ticket_renewed_ms"], NOW)

    def test_cannot_outlive_revocation(self):
        aid = self.seed_ticket()
        self.store.observe_commit(TASK, [comment("c9", material=True, mentions=True)], {})
        self.assertFalse(self.store.renew_ticket(aid, 1, NOW + 1))
        self.assertEqual(self.approval(aid)["state"], "revoked")

    def test_unclassified_terminal_or_hold_updates_nothing(self):
        for setup in ("classified_gen=3", "terminal=1", "hold_reason='operator'"):
            aid = self.seed_ticket()
            with self.store.transaction() as c:
                c.execute(f"UPDATE tasks SET {setup} WHERE clickup_task_id=?", (TASK,))
            self.assertFalse(self.store.renew_ticket(aid, 1, NOW + 1), setup)
            with self.store.transaction() as c:
                c.execute("DELETE FROM approvals")
                c.execute("DELETE FROM tasks")


class ReadOnlySnapshotTest(StoreTestBase):
    def test_reader_sees_last_committed_snapshot_during_concurrent_write(self):
        self.seed_ticket()
        ro = self.ro()
        self.store.conn.execute("BEGIN IMMEDIATE")
        self.store.conn.execute("UPDATE tasks SET input_gen=99 WHERE clickup_task_id=?", (TASK,))
        started = time.monotonic()
        self.assertEqual(ro.execute("SELECT input_gen FROM tasks").fetchone()[0], 4)
        self.assertTrue(self.check().valid)
        self.assertLess(time.monotonic() - started, 2.0)
        self.store.conn.execute("COMMIT")
        self.assertEqual(ro.execute("SELECT input_gen FROM tasks").fetchone()[0], 99)
        self.assertEqual(str(self.check()), "INVALID new_input")

    def test_reader_cannot_write(self):
        self.seed_ticket()
        with self.assertRaises(sqlite3.OperationalError):
            self.ro().execute("UPDATE tasks SET input_gen=1")

    def test_reader_after_writer_closed(self):
        self.seed_ticket()
        self.store.close()
        self._conns = []
        self.assertTrue(self.check().valid)

    def test_busy_timeout_is_five_seconds(self):
        self.assertEqual(self.ro().execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_missing_or_foreign_file_gives_no_snapshot(self):
        self.assertIsNone(load_ticket_snapshot(os.path.join(self.dir, "absent.sqlite3"), TASK, 1500))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "absent.sqlite3")))
        foreign = os.path.join(self.dir, "foreign.sqlite3")
        c = sqlite3.connect(foreign)
        c.execute("CREATE TABLE x(y)")
        c.commit()
        c.close()
        self.assertIsNone(load_ticket_snapshot(foreign, TASK, 1500))

    def test_snapshot_reads_only_this_task(self):
        self.seed_ticket()
        self.store.observe_commit("other", [], {})
        s = load_ticket_snapshot(self.path, TASK, 1500)
        self.assertEqual(s.task["clickup_task_id"], TASK)
        self.assertEqual({a["task"] for a in s.approvals}, {TASK})
        self.assertEqual(s.lease_seconds, 1500)


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


class ConfigTest(unittest.TestCase):
    EIGHT = [("reviewing feature", "open"), ("business requirements", "unstarted"),
             ("technical requirements", "unstarted"), ("waiting for approval", "unstarted"),
             ("implementing", "custom"), ("implemented", "done"), ("ready for deployment", "done"),
             ("deployed", "closed")]

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, data):
        import json
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def test_defaults_match_plan(self):
        c = Config()
        self.assertEqual(c.expected_statuses, tuple(self.EIGHT))
        self.assertEqual(c.intake_statuses, ("reviewing feature", "business requirements",
                                             "technical requirements", "waiting for approval"))
        self.assertEqual((c.workspace_id, c.space_id, c.folder_id, c.list_id, c.bot_user_id),
                         ("11111111111", "22222222222", "3333333333333333", "4444444444444444", 900000001))
        self.assertEqual(c.approvers, {900000101: "Approver One", 900000102: "Approver Two",
                                       900000103: "Approver Three"})
        self.assertEqual((c.quiet_seconds, c.ticket_renew_seconds, c.ticket_lease_seconds), (180, 600, 1500))
        self.assertEqual(c.plain_attr_allowlist, ())
        self.assertEqual((c.mode, c.enabled, c.cutover_ms), ("shadow", False, None))
        self.assertEqual((c.board, c.discord_chat, c.notify_mode), ("example-project", "111111111111111111",
                                                                     "notify"))
        self.assertEqual((c.handoff_unblock, c.auto_gate), (True, True))
        self.assertEqual(c.intervals, {"active_light_seconds": 300, "active_full_seconds": 900,
                                       "promotion_full_seconds": 300, "late_light_seconds": 900,
                                       "late_full_seconds": 3600})
        self.assertEqual(c.budgets, {"scan_requests": 40, "rate_limit_reserve": 20, "per_task_requests": 15,
                                     "discovery_max_pages": 10, "comment_pages_cap": 40, "threaded_roots_cap": 200})
        self.assertEqual(c.state_path,
                         os.path.join("C:/Hermes/integrations/"
                                      "clickup-dedicated-panel", "state.sqlite3"))
        self.assertNotIn("implimenting", {n for n, _ in c.expected_statuses})

    def test_load_overrides(self):
        c = load_config(self.write({"mode": "live", "enabled": True, "cutover_ms": NOW, "handoff_unblock": False,
                                    "notify_mode": "off", "plain_attr_allowlist": ["block-id"],
                                    "approvers": {"900000101": "Approver One"}, "runtime_dir": self.dir,
                                    "intervals": {"late_full_seconds": 1800}}))
        self.assertEqual((c.mode, c.enabled, c.cutover_ms, c.handoff_unblock, c.notify_mode),
                         ("live", True, NOW, False, "off"))
        self.assertEqual(c.plain_attr_allowlist, ("block-id",))
        self.assertEqual(c.approvers, {900000101: "Approver One"})
        self.assertEqual(c.intervals["late_full_seconds"], 1800)
        self.assertEqual(c.intervals["active_light_seconds"], 300)
        self.assertEqual(c.state_path, os.path.join(self.dir, "state.sqlite3"))

    def test_invalid_configs_are_refused(self):
        bad = [
            {"nope": 1},
            {"mode": "yolo"},
            {"enabled": "yes"},
            {"notify_mode": "wake"},
            {"quiet_seconds": -1},
            {"expected_statuses": [["implementing", "custom"], ["implementing", "custom"]]},
            {"expected_statuses": [["x"]]},
            {"intake_statuses": ["implementing"]},
            {"intervals": {"bogus_seconds": 1}},
            {"budgets": {"scan_requests": 0}},
            {"approvers": {"abc": "x"}},
            {"cutover_ms": "now"},
        ]
        for data in bad:
            with self.assertRaises(ValueError, msg=data):
                load_config(self.write(data))
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        with self.assertRaises(ValueError):
            load_config(path)


class PortsAndFakesTest(unittest.TestCase):
    def test_fakes_satisfy_ports(self):
        self.assertIsInstance(FakeTransport(), ClickUpTransport)
        self.assertIsInstance(FakeKanban(), KanbanPort)

    def test_fake_transport_scripted_results_and_call_log(self):
        t = FakeTransport([TransportResult(Outcome.OK, 200, {"statuses": []}, {"X-RateLimit-Remaining": "99"})])
        r = t.request("GET", "/v2/list/4444444444444444", {"a": 1})
        self.assertEqual((r.outcome, r.status, r.json), (Outcome.OK, 200, {"statuses": []}))
        self.assertEqual(t.calls, [("GET", "/v2/list/4444444444444444", {"a": 1}, None, None)])
        self.assertEqual(t.request("GET", "/x").outcome, Outcome.DEFINITE_FAIL)  # script exhausted

    def test_fake_kanban_records_calls(self):
        k = FakeKanban()
        r = k.create("ClickUp 86abc: request 1", body_file="b.md", idempotency_key="k1", assignee="planner",
                     skills=("plan",), initial_status="blocked")
        self.assertEqual(r.outcome, Outcome.OK)
        card = r.json["id"]
        self.assertEqual(k.create("again", body_file="b.md", idempotency_key="k1").json["id"], card)
        k.comment(card, "hello")
        k.unblock(card)
        self.assertEqual([c[0] for c in k.calls], ["create", "create", "comment", "unblock"])
        self.assertEqual(k.show(card).json["task"]["status"], "ready")
        k.respond("show", KanbanResult(Outcome.AMBIGUOUS))
        self.assertEqual(k.show(card).outcome, Outcome.AMBIGUOUS)


class ContractsTest(unittest.TestCase):
    def test_enums_match_plan(self):
        self.assertEqual({o.name for o in Outcome}, {"OK", "DEFINITE_FAIL", "AMBIGUOUS", "RATE_LIMITED", "AUTH_FAIL"})
        self.assertEqual({s.value for s in ApprovalState},
                         {"held", "recorded", "staged", "released", "started", "implemented", "ticketed",
                          "promoted", "revoked", "stale"})
        self.assertEqual({s.value for s in RowState},
                         {"pending", "sending", "retry_wait", "ambiguous", "held_ambiguous", "done",
                          "cancelled_stale", "failed"})
        self.assertEqual({p.value for p in Phase},
                         {"baseline", "not_admitted", "new_quiet", "planning", "awaiting_business",
                          "awaiting_technical", "in_review", "awaiting_approval", "revising", "approval_queued",
                          "development", "implemented", "promotion_window", "promotion_held",
                          "ready_for_deployment", "deployed", "stopped", "hold"})

    def test_effect_intent_and_preconditions_fields(self):
        from dataclasses import fields
        self.assertEqual([f.name for f in fields(EffectIntent)],
                         ["idem_key", "kind", "target", "payload", "preconditions", "depends_on"])
        self.assertEqual([f.name for f in fields(Preconditions)],
                         ["task_id", "list_id", "phases", "scope_gen", "input_gen", "published_rev",
                          "approval_id", "approval_states", "wait_run_id"])


if __name__ == "__main__":
    unittest.main()
