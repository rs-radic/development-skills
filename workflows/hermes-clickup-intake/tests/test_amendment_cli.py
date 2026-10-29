"""Workflow repair r3 §3.8/§3.9 and §7 U2, U4(e), U4 (receipt/admission): the amendment CLI.

``World`` runs the real ``main.main`` (lock, store, admission flag, parser) with the fake board and fake ClickUp;
the operator-source scenario (U2) runs ``main.adopt_amendment`` on the S2 harness of ``test_amendment`` with a port
over the harness cards, then the real writer scan order; ``AdoptRealBoardReadsTest`` checks that the adoption's
W/Q readers accept the installed ``hermes kanban show --json`` shape on an isolated board.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import unittest

from clickup_intake import engine, rules
from clickup_intake import kanban_port as kp
from clickup_intake import main as glue_main
from clickup_intake.contracts import KanbanResult, Outcome
from test_amendment import AmendBase, amended_text
from test_s4_composition import TMP_ROOT, World

BUSY = ["busy: another clickup-intake instance holds the lock; retry"]
DRAIN = "e" * 64
CLOSED = ["admission closed (rollback to 45f7c252df27)"]


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def sql(w, statement, *args):
    conn = sqlite3.connect(w.state_path)
    try:
        conn.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


def dump(w):
    """Every row of every table (the 'writes nothing' oracle; WAL checkpoints may change file bytes)."""
    conn = sqlite3.connect(w.state_path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: conn.execute("SELECT * FROM %s ORDER BY rowid" % n).fetchall() for n in names}
    finally:
        conn.close()


class AmendmentCommandsTest(unittest.TestCase):
    def setUp(self):
        self.w = World(self)
        self.assertEqual(self.w.run()[0], 0)

    def status(self):
        return self.w.main("blockers", "amendment-status")

    def test_status_enable_and_idempotent_repeat(self):
        w = self.w
        self.assertEqual(self.status(), (0, ["amendment continuation: disabled", "admission: open",
                                             "rollback-target 45f7c252df27: ready"]))
        self.assertEqual(w.main("blockers", "amendment-enable", "--drain-sha", DRAIN),
                         (1, ["amendment-enable refused: blockers not enabled"]))
        for bad in ("E" * 64, "e" * 63, None):
            argv = ("blockers", "amendment-enable") + (("--drain-sha", bad) if bad else ())
            self.assertEqual(w.main(*argv), (1, ["amendment-enable refused: --drain-sha must be 64 lowercase hex"
                                                 " characters"]))
        w.main("blockers", "enable")
        first = w.clock()
        self.assertEqual(w.main("blockers", "amendment-enable", "--drain-sha", DRAIN),
                         (0, ["amendment continuation enabled %d drain %s" % (first, DRAIN[:12])]))
        w.clock.advance(60)
        self.assertEqual(w.main("blockers", "amendment-enable", "--drain-sha", "f" * 64),
                         (0, ["amendment continuation enabled %d drain %s (existing)" % (first, DRAIN[:12])]))
        [row] = w.q("SELECT value FROM meta WHERE key=?", engine.AMEND_KEY)
        self.assertEqual(json.loads(row["value"]), {"ms": first, "drain_sha256": DRAIN})  # the shape the engine reads
        self.assertEqual(self.status()[1][0], "amendment continuation: enabled %d drain %s" % (first, DRAIN[:12]))

    def test_adopt_needs_all_arguments_and_refuses_before_enablement_writing_nothing(self):
        w = self.w
        self.assertEqual(w.main("blockers", "adopt-amendment", "--blocker", "1"),
                         (2, ["blockers adopt-amendment requires --blocker, --approval, --receipt and --receipt-sha"]))
        w.main("blockers", "enable")
        before = dump(w)
        self.assertEqual(w.main("blockers", "adopt-amendment", "--blocker", "1", "--approval", "2", "--receipt",
                                "x.md", "--receipt-sha", "a" * 64),
                         (1, ["adopt refused: amendment continuation not enabled"]))
        self.assertEqual(dump(w), before)

    def test_rollback_prepare_refuses_the_wrong_target_and_a_bound_row_writing_nothing(self):
        w = self.w
        self.assertEqual(w.main("blockers", "rollback-prepare", "--target", "aa3cbe67c998"),
                         (1, ["target must be 45f7c252df27"]))
        sql(w, "INSERT INTO amendment_answers(task, blocker_id, approval_id, rev, plan_sha256, question_card, card,"
               " gen, relay_ref, resolution_sha256, source, state, created_ms, updated_ms)"
               " VALUES ('t', 1, 2, 2, ?, 't_q0000001', 't_w0000001', 4, 'r-1', ?, 'plan', 'bound', 1, 1)",
            "a" * 64, "b" * 64)
        sql(w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES ('clickup-dp:t:x', 't', 'kb_comment',"
               " 'ambiguous')")
        before = dump(w)
        self.assertEqual(w.main("blockers", "rollback-prepare", "--target", "45f7c252df27"),
                         (1, ["rollback refused: amendment bindings bound: 1; outbox rows sending or ambiguous: 1"]))
        self.assertEqual(self.status()[1][-2:], ["admission: open", "rollback-target 45f7c252df27: refused (amendment"
                                                 " bindings bound: 1; outbox rows sending or ambiguous: 1)"])
        self.assertEqual(dump(w), before)

    def test_closed_admission_makes_the_candidate_inert_until_admission_open(self):
        w = self.w
        w.main("blockers", "enable")
        self.assertEqual(w.main("blockers", "rollback-prepare", "--target", "45f7c252df27"),
                         (0, ["rollback ready; admission closed"]))
        [row] = w.q("SELECT value FROM meta WHERE key=?", engine.ADMISSION_KEY)
        self.assertEqual(json.loads(row["value"]), {"target": "45f7c252df27", "ms": w.clock()})
        before, calls = dump(w), (len(w.board.calls), len(w.cu.calls))
        w.clock.advance(600)
        self.assertEqual(w.run(), (0, []))  # no output, so the scheduler sends no notice
        self.assertEqual((dump(w), (len(w.board.calls), len(w.cu.calls))), (before, calls))
        with open(os.path.join(w.runtime, "logs", "intake.jsonl"), encoding="utf-8") as f:
            self.assertIn('"event": "admission_closed"', f.read())
        for argv in (("hold", "t1"), ("release", "t1"), ("admit", "t1"), ("blockers", "enable"),
                     ("blockers", "disable"), ("blockers", "protocol-comment"), ("blockers", "rollback-prepare",
                     "--target", "45f7c252df27"), ("blockers", "amendment-enable", "--drain-sha", DRAIN),
                     ("blockers", "adopt-amendment", "--blocker", "1", "--approval", "2", "--receipt", "x",
                      "--receipt-sha", "a" * 64), ("recovery", "enable"), ("outbox", "resend", "k")):
            with self.subTest(argv):
                self.assertEqual(w.main(*argv), (1, CLOSED))
        self.assertEqual(dump(w), before)
        self.assertEqual(w.main("blockers", "rollback-preflight")[0], 1)  # read-only commands still run
        self.assertEqual(w.main("recovery", "status")[0], 0)
        self.assertEqual(self.status()[1][-2], "admission: closed (rollback to 45f7c252df27)")
        self.assertEqual(w.main("blockers", "admission-open"), (0, ["admission open"]))
        self.assertEqual(w.q("SELECT value FROM meta WHERE key=?", engine.ADMISSION_KEY), [])
        self.assertEqual(w.run()[0], 0)

    def test_a_held_lock_refuses_every_new_lock_taking_command_and_status_only_reads(self):
        w = self.w
        w.main("blockers", "enable")
        lock = glue_main._lock(w.runtime)
        try:
            for argv in (("amendment-enable", "--drain-sha", DRAIN), ("rollback-prepare", "--target", "45f7c252df27"),
                         ("admission-open",), ("adopt-amendment", "--blocker", "1", "--approval", "2", "--receipt",
                                               "x", "--receipt-sha", "a" * 64)):
                with self.subTest(argv):
                    self.assertEqual(w.main("blockers", *argv), (1, BUSY))
            self.assertEqual(self.status()[0], 0)
        finally:
            lock.close()


# ======================================================================== U2: the operator source end to end
class _Port:
    def __init__(self, h):
        self.h = h

    def show(self, card):
        shown = self.h.cards.get(card)
        return KanbanResult(Outcome.OK, json.loads(json.dumps(shown))) if shown else KanbanResult(
            Outcome.DEFINITE_FAIL, {"error": "unknown"})


class OperatorAdoptionTest(AmendBase):
    def adopt(self, receipt=None, sha=None, blocker=None, approval=None):
        b, a = self.b(), self.h.approvals()[-1]
        if receipt is None:
            line = rules.adoption_line(b["id"], b["question_card"], b["card"], b["resolution_gen"],
                                       b["resolution_relay"], hashlib.sha256(b["resolution_body"].encode()).hexdigest(),
                                       self.tid(), a["id"], a["rev"], a["plan_sha256"])
            receipt = ("# reviewed plan\r\n\r\n```\r\n" + line + "\r\n```\r\n").encode()
        path = os.path.join(self.h.dir, "receipt.md")
        with open(path, "wb") as f:
            f.write(receipt)
        return glue_main.adopt_amendment(self.h.store, _Port(self.h), blocker or b["id"], approval or a["id"], path,
                                         sha or _sha(receipt), self.h.now)

    def hexify(self):
        """Give Q and W real-shaped Kanban ids (``t_`` + 8 hex; the receipt line requires them) everywhere the
        harness and the state hold them; the harness's own ``t_x005``/``t_q0000001`` ids are not hex."""
        b = self.b()
        ren = {b["question_card"]: "t_0000a0a1", b["card"]: "t_0000b0b2"}
        for old, new in ren.items():
            self.h.cards = json.loads(re.sub(re.escape(old) + r"(?![0-9a-z])", new, json.dumps(self.h.cards)))
            c = self.h.store.conn
            for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                for col in c.execute("PRAGMA table_info(%s)" % table).fetchall():
                    if col["type"].upper() == "TEXT":
                        c.execute("UPDATE %s SET %s=REPLACE(%s, ?, ?)" % (table, col["name"], col["name"]),
                                  (old, new))
        self.w = self.fe = ren[b["card"]]

    def prepared(self):
        b, rel = self.scope_change()
        self.hexify()
        a2 = self.revise(markers=[])  # an older revision: no plan marker
        self.enable()
        return b, rel, a2

    def test_u2_adopt_binds_then_the_writer_applies_delivers_and_resumes(self):
        b, rel, a2 = self.prepared()
        ok, line = self.adopt()
        self.assertTrue(ok, line)
        [k] = self.bindings()
        self.assertEqual(line, "binding %d blocker %d approval %d bound" % (k["id"], b["id"], a2["id"]))
        self.assertEqual((k["source"], k["state"], k["relay_ref"], k["gen"]), ("operator", "bound", rel["relay_ref"],
                                                                               rel["gen"]))
        self.assertEqual(self.adopt(), (True, "binding blocker %d approval %d (existing)" % (b["id"], a2["id"])))
        self.h.settle()
        self.h.cycle()
        [k] = self.bindings()
        self.assertEqual(k["state"], "applied")
        [row] = self.answer_rows()
        p = json.loads(row["body"])
        body = amended_text(k, a2)
        self.assertIn(", receipt SHA-256 %s)." % k["receipt_sha256"], body)
        self.assertIn("\n%s\n" % body, p["summary"])
        self.assertEqual((row["state"], p["answer_sha256"]), ("done", hashlib.sha256(body.encode()).hexdigest()))
        self.claim()
        self.link_fence()
        for _ in range(2):  # detected, then released by the next full pass (r3 §3.7 fallback)
            self.h.settle()
            self.h.cycle()
        self.assertEqual(self.rows("fences")[0]["state"], "released")
        self.assertEqual(self.ack(rel["gen"])["state"], "resumed")

    def test_adopt_refusals_write_nothing(self):
        self.prepared()
        b, a2 = self.b(), self.h.approvals()[-1]
        q, w = b["question_card"], self.w
        cases = [
            ("receipt sha", lambda: None, dict(sha="0" * 64), "receipt SHA-256 mismatch"),
            ("no line", lambda: None, dict(receipt=b"# plan without the line\n"),
             "the receipt has no exact line for the live blocker and approval"),
            ("altered line", lambda: None, dict(receipt=b"CLICKUP-INCORPORATES v1 blocker=%d question=%s card=%s gen=9"
                                                % (b["id"], q.encode(), w.encode())),
             "the receipt has no exact line for the live blocker and approval"),
            ("unknown blocker", lambda: None, dict(blocker=999), "unknown blocker 999"),
            ("q done", lambda: self.h.cards[q]["task"].update(status="done"), {},
             "question card %s is not blocked" % q),
            ("w running", lambda: self.h.cards[w]["task"].update(status="running"), {},
             "work card %s is not eligible" % w),
            ("w claimed", lambda: self.h.cards[w]["events"].append({"kind": "claimed", "payload": {"run_id": 99}}), {},
             "work card %s is not eligible" % w),
            ("held", lambda: self.h.store.conn.execute("UPDATE tasks SET hold_reason='operator'"), {},
             "task %s is held" % self.tid()),
            ("terminal", lambda: self.h.store.conn.execute("UPDATE tasks SET terminal=1"), {},
             "task %s is terminal" % self.tid()),
            ("not amended", lambda: self.h.store.conn.execute("UPDATE approvals SET state='released' WHERE id=?",
                                                              (a2["id"],)), {}, "approval %d is released" % a2["id"]),
            ("newer input", lambda: self.h.store.conn.execute("UPDATE tasks SET input_gen=input_gen+1"), {},
             "approval %d is not current" % a2["id"]),
            ("admission closed", lambda: self.h.store.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, '{}')", (engine.ADMISSION_KEY,)), {}, "admission closed"),
        ]
        for name, mutate, kw, why in cases:
            with self.subTest(name):
                self.setUp()
                self.prepared()
                mutate()
                self.assertEqual(self.adopt(**kw), (False, why))
                self.assertEqual(self.bindings(), [])

    def test_adopt_of_a_delivered_or_other_blocker_state_refuses(self):
        b, rel, a2 = self.prepared()
        self.h.store.conn.execute("UPDATE blockers SET state='closed' WHERE id=?", (b["id"],))
        self.assertEqual(self.adopt()[0], False)
        self.assertEqual(self.bindings(), [])


# ======================================================================== the installed show --json shape
HERMES_EXE = os.environ.get("CLICKUP_INTAKE_TEST_HERMES_EXE", "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe")
HOME = os.path.join(TMP_ROOT, "amend-cli-home")
BOARD = "clickup-amend-cli"


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HERMES_HOME=HOME, HERMES_KANBAN_HOME=HOME, HERMES_DISABLE_LAZY_INSTALLS="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


class AdoptRealBoardReadsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(HOME):
            raise RuntimeError("stale temporary home %s; remove it first" % HOME)
        os.makedirs(HOME)
        try:
            p = subprocess.run([HERMES_EXE, "kanban", "boards", "create", BOARD], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=_env(), timeout=300)
            if p.returncode != 0:
                raise RuntimeError("board create failed: %s %s" % (p.stdout, p.stderr))
        except BaseException:
            shutil.rmtree(HOME)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(HOME)
        if os.path.exists(HOME):
            raise AssertionError("temporary home still present: %s" % HOME)

    def test_blocked_question_and_todo_work_card_parented_on_it_are_read_as_adoptable(self):
        port = kp.KanbanCli(HERMES_EXE, BOARD, discord_chat="111111111111111111", env=_env())
        path = os.path.join(HOME, "body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("body")
        q = port.create("Q", body_file=path, idempotency_key="amend-q", assignee="planner").json["id"]
        p = subprocess.run([HERMES_EXE, "kanban", "--board", BOARD, "create", "--body-file", path, "--assignee",
                            "implementor", "--parent", q, "--json", "--", "W"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_env(), timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        w = json.loads(p.stdout)["id"]
        qs, ws = port.show(q).json, port.show(w).json
        self.assertEqual(rules.status(qs), "blocked")
        self.assertEqual((rules.status(ws), rules.parents(ws), kp.open_run_ids(ws), kp.claimed_runs(ws)),
                         ("todo", {q}, [], {}))

if __name__ == "__main__":
    unittest.main()
