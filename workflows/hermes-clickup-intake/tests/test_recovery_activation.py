"""ClickUp technical-blocker recovery activation, disable, rollback and previous-reader compatibility (plan r3 §8,
§11, §10.2 #15): ``recovery enable|disable|adopt|status|rollback-check`` take intake.lock, ``recovery-check`` takes
none and only reads; the 2d53206 release reads a candidate-written DB with the blocker relay enabled; feature-off
scans are behaviorally identical to the 2d53206 release.
"""

import hashlib
import importlib
import io
import json
import os
import sqlite3
import shutil
import sys
import unittest

from clickup_intake import engine
from clickup_intake import main as glue_main
from clickup_intake.contracts import KanbanResult, Outcome
from test_activation import _Outcomes
from test_s4_composition import TMP_ROOT, Lifecycle, World

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "2d53206cb907ae7d1f2e3e8ca0ce0e5170f217fd"  # source release represented by the sanitized fixture
PUBLIC_FIXTURE_BASE = "532c102bdaa5bf2f7238f37973ce9e0563ddf0a9"
BASELINE_ROOT = os.path.join(REPO, "tests", "fixtures", "recovery_base")
BUSY = ["busy: another clickup-intake instance holds the lock; retry"]


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def sql(w, statement, *args):
    conn = sqlite3.connect(w.state_path)
    try:
        conn.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


def state_bytes(path):
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    wal = path + "-wal"
    return digest, os.path.getsize(wal) if os.path.exists(wal) else 0


def baseline_package(tc, dest):
    """The hash-checked public equivalent of 2d53206, imported until cleanup; never the candidate reader."""
    with open(os.path.join(BASELINE_ROOT, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    tc.assertEqual(manifest["source_revision"], BASE)
    tc.assertEqual(manifest["public_fixture_base"], PUBLIC_FIXTURE_BASE)
    for entry in manifest["files"]:
        with open(os.path.join(BASELINE_ROOT, entry["path"]), "rb") as f:
            data = f.read()
        tc.assertEqual(len(data), entry["size"])
        tc.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
    shutil.copytree(BASELINE_ROOT, dest, ignore=shutil.ignore_patterns("manifest.json", "__pycache__"))
    names = lambda: [k for k in sys.modules if k == "clickup_intake" or k.startswith("clickup_intake.")]  # noqa
    saved = {k: sys.modules.pop(k) for k in names()}
    sys.path.insert(0, dest)

    def restore():
        sys.path.remove(dest)
        for k in names():
            del sys.modules[k]
        sys.modules.update(saved)
    tc.addCleanup(restore)
    base = importlib.import_module("clickup_intake.main")
    tc.assertEqual(os.path.dirname(os.path.dirname(os.path.abspath(base.__file__))), dest)
    return base, importlib.import_module("clickup_intake.contracts").Outcome


def baseline_runner(w, base, outcome):
    def run(*argv):
        buf = io.StringIO()
        rc = base.main(list(argv), root=w.root, transport=_Outcomes(w.transport, outcome),
                       port=_Outcomes(w.board, outcome), clock=w.clock, opener=w.opener, out=buf)
        lines = buf.getvalue().splitlines()
        w.outputs.extend(lines)
        return rc, lines
    return run


class RecoveryCommandsTest(unittest.TestCase):
    def setUp(self):
        self.w = World(self)
        self.assertEqual(self.w.run()[0], 0)

    def cutover(self):
        rows = self.w.q("SELECT value FROM meta WHERE key=?", engine.RECOVERY_KEY)
        return json.loads(rows[0]["value"]) if rows else None

    def test_enable_status_disable(self):
        w = self.w
        self.assertEqual(w.main("recovery", "status"), (0, ["recovery: disabled", "incidents: none"]))
        self.assertEqual(w.main("recovery", "enable"), (0, ["recovery enabled; cutover %d" % w.clock()]))
        self.assertEqual(self.cutover(), w.clock())
        self.assertEqual(w.main("recovery", "status"), (0, ["recovery: enabled; cutover %d" % w.clock(),
                                                            "incidents: none"]))
        w.clock.advance(60)
        self.assertEqual(w.main("recovery", "disable"), (0, ["recovery disabled"]))
        self.assertIsNone(self.cutover())
        self.assertEqual(self.w.q("SELECT value FROM meta WHERE key=?", engine.CUTOVER_KEY), [])  # relay untouched

    def test_adopt_needs_card_and_run(self):
        self.assertEqual(self.w.main("recovery", "adopt"), (2, ["recovery adopt requires --card and --run"]))

    def test_a_held_lock_refuses_every_recovery_subcommand_and_recovery_check_only_reads(self):
        w = self.w
        w.main("recovery", "enable")
        card = w.board._new("W", assignee="orchestrator", status="blocked")
        before = state_bytes(w.state_path)
        lock = glue_main._lock(w.runtime)
        try:
            for argv in (("enable",), ("disable",), ("adopt", "--card", card, "--run", "1"), ("status",),
                         ("rollback-check",)):
                with self.subTest(argv):
                    self.assertEqual(w.main("recovery", *argv), (1, BUSY))
            self.assertEqual(w.main("recovery-check", "--card", card, "--run", "1"), (3, ["STALE no_incident"]))
            self.assertEqual(w.main("recovery-check", "--card", "t_00000bad", "--run", "1"),
                             (2, ["UNAVAILABLE show_unavailable"]))
        finally:
            lock.close()
        self.assertEqual(state_bytes(w.state_path), before)
        self.assertIsNotNone(self.cutover())

    def test_recovery_check_without_a_readable_state_or_config_is_unavailable(self):
        w = World(self)
        card = w.board._new("W", status="blocked")
        self.assertEqual(w.main("recovery-check", "--card", card, "--run", "1"), (2, ["UNAVAILABLE state_unavailable"]))
        self.assertFalse(os.path.exists(w.state_path))  # the read-only opener never creates it
        os.remove(os.path.join(w.runtime, "config.json"))
        self.assertEqual(w.main("recovery-check", "--card", card, "--run", "1"), (2, ["UNAVAILABLE config_unavailable"]))


class RecoveryRollbackCheckTest(unittest.TestCase):
    """§8: ``recovery rollback-check`` refuses only for recovery state; it is read-only."""

    def setUp(self):
        self.w = World(self)
        self.assertEqual(self.w.run()[0], 0)

    def check(self):
        return self.w.main("recovery", "rollback-check")

    def row(self, key, state, kind="kb_create"):
        sql(self.w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES (?, '86b7t', ?, ?)", key, kind, state)

    def incident(self, state, run=1):
        sql(self.w, "INSERT INTO recoveries(task, card, run_id, state, attempts) VALUES ('86b7t', 't_w1', ?, ?, 1)",
            run, state)

    def test_a_clean_disabled_store_passes(self):
        self.assertEqual(self.check(), (0, ["recovery rollback-check OK"]))

    def test_enabled_refuses(self):
        self.w.main("recovery", "enable")
        self.assertEqual(self.check(), (1, ["recovery rollback-check refused: recovery enabled"]))

    def test_each_unsent_or_possibly_sent_rcv_row_refuses_and_final_rows_pass(self):
        self.row("clickup-dp:86b7t:rcv:t_w1:1:1:create", "done")
        self.row("clickup-dp:86b7t:rcv:t_w1:1:1:wake", "cancelled_stale", "kb_complete")
        self.row("clickup-dp:86b7t:req:1", "failed")  # unrelated rows are not this check's concern
        self.assertEqual(self.check(), (0, ["recovery rollback-check OK"]))
        for i, state in enumerate(("pending", "retry_wait", "sending", "ambiguous", "held_ambiguous", "failed")):
            with self.subTest(state):
                key = "clickup-dp:86b7t:rcv:t_w1:%d:1:subscribe" % (i + 2)
                self.row(key, state, "kb_subscribe")
                self.assertEqual(self.check(), (1, ["recovery rollback-check refused: :rcv: rows not done or"
                                                    " cancelled_stale: 1"]))
                sql(self.w, "UPDATE outbox SET state='done' WHERE idem_key=?", key)

    def test_open_or_handed_off_incidents_refuse_and_final_ones_pass(self):
        for i, state in enumerate(("resumed", "gated", "closed", "escalated", "superseded", "disabled")):
            self.incident(state, i + 1)
        self.assertEqual(self.check(), (0, ["recovery rollback-check OK"]))
        self.incident("open", 20)
        self.incident("handed_off", 21)
        self.assertEqual(self.check(), (1, ["recovery rollback-check refused: open incidents: 2"]))

    def test_the_check_writes_nothing(self):
        self.w.main("recovery", "enable")
        self.incident("open")
        before = state_bytes(self.w.state_path)
        self.check()
        self.assertEqual(state_bytes(self.w.state_path), before)


class BlockersEnabledRollbackCompatibilityTest(unittest.TestCase):
    """§8/§11 (R4): with the blocker relay enabled and open/releasing blocker, fence and park rows, recovery disabled
    and every recovery row final, ``recovery rollback-check`` passes, ``blockers rollback-preflight`` keeps its
    unchanged ``blockers enabled`` refusal, and the 2d53206 release runs ordinary scans without sending a ``:rcv:``
    row (previous-reader compatibility)."""

    def test_rollback_target_reads_the_candidate_database(self):
        w = World(self)
        life = Lifecycle(w)
        w.main("blockers", "enable")
        w.main("recovery", "enable")
        real, attempted = w.board.create, []
        w.board.create = lambda *a, **k: attempted.append(a) or KanbanResult(Outcome.DEFINITE_FAIL, {"error": "x"})
        w.step(lambda: bool(attempted))
        w.board.create = real
        unsent = [r["idem_key"] for r in w.q("SELECT idem_key FROM outbox WHERE state IN ('pending', 'retry_wait')"
                                             " AND kind != 'alert'")]
        self.assertTrue(any(":req:" in k for k in unsent), unsent)
        tid = life.tid
        for q, state in (("t_q1", "detected"), ("t_q2", "releasing")):
            sql(w, "INSERT INTO blockers(task, question_card, card, state) VALUES (?, ?, 't_w1', ?)", tid, q, state)
        for f, kind, state in (("t_f1", "fence", "open"), ("t_p1", "park", "releasing")):
            sql(w, "INSERT INTO fences(task, card, fence_card, kind, state) VALUES (?, 't_w1', ?, ?, ?)", tid, f, kind,
                state)
        pre = json.dumps({"task_id": tid, "list_id": None, "phases": list(engine.AMENDMENT_PHASES), "scope_gen": None,
                          "input_gen": None, "published_rev": None, "approval_id": 1,
                          "approval_states": list(engine.WATCHED_STATES), "wait_run_id": None})
        k = "clickup-dp:%s:rcv:t_w1:7:1" % tid
        final = ((k + ":create", "kb_create", "done", "t_cdead001"), (k + ":subscribe", "kb_subscribe", "done", None),
                 (k + ":wake", "kb_complete", "cancelled_stale", None),
                 (k + ":archive", "kb_archive", "done", None))
        for key, kind, state, ref in final:
            sql(w, "INSERT INTO outbox(idem_key, task, kind, target, body, preconditions, state, result_ref)"
                   " VALUES (?,?,?,?,?,?,?,?)", key, tid, kind, None, json.dumps({"card": None}), pre, state, ref)
        for i, state in enumerate(("resumed", "escalated", "superseded")):
            sql(w, "INSERT INTO recoveries(task, card, run_id, state, attempts) VALUES (?, 't_w1', ?, ?, 1)", tid,
                i + 7, state)
        w.main("recovery", "disable")
        self.assertEqual(w.main("recovery", "rollback-check"), (0, ["recovery rollback-check OK"]))
        self.assertEqual(w.main("blockers", "rollback-preflight"),
                         (1, ["rollback preflight refused: blockers enabled; blockers releasing: 1; fences open or"
                              " releasing: 1; parks open or releasing: 1"]))
        rcv = w.q("SELECT idem_key, state FROM outbox WHERE idem_key LIKE '%:rcv:%' ORDER BY idem_key")
        mark = len(w.board.calls)
        base, outcome = baseline_package(self, os.path.join(w.dir, "baseline"))
        run = baseline_runner(w, base, outcome)
        for _ in range(3):  # past the retry backoff
            w.clock.advance(1861)
            self.assertEqual(run("run")[0], 0)
        states = {r["idem_key"]: r["state"] for r in w.q("SELECT idem_key, state FROM outbox")}
        self.assertEqual({k: states[k] for k in unsent if ":req:" in k}, {k: "done" for k in unsent if ":req:" in k})
        self.assertEqual(w.q("SELECT idem_key, state FROM outbox WHERE idem_key LIKE '%:rcv:%' ORDER BY idem_key"),
                         rcv)
        self.assertFalse([c for c in w.board.calls[mark:] if c[1] == "t_cdead001" and c[0] != "show"])
        self.assertEqual(run("blockers", "rollback-preflight")[0], 1)


class FeatureOffEquivalenceTest(unittest.TestCase):
    """§10.2 #15: with recovery off the candidate is behaviorally identical to the 2d53206 release: same outbox and
    same board effects for the full T19 lifecycle (blocker relay enabled, so its watched observation runs too)."""

    def lifecycle(self, w):
        w.main("blockers", "enable")
        Lifecycle(w).full()
        rows = w.q("SELECT idem_key, task, kind, target, body, preconditions, depends_on, state, outcome, result_ref"
                   " FROM outbox ORDER BY rowid")
        text = json.dumps([rows, w.board.mutations(), w.outputs])
        for d in (w.dir.replace("\\", "\\\\\\\\"), w.dir.replace("\\", "\\\\"), w.dir):  # staging paths
            text = text.replace(d, "<world>")
        return json.loads(text)

    def test_full_lifecycle_matches_the_baseline_release(self):
        cand = self.lifecycle(World(self))
        w = World(self)
        base, outcome = baseline_package(self, os.path.join(w.dir, "baseline"))
        w.main = baseline_runner(w, base, outcome)
        old = self.lifecycle(w)
        self.assertEqual(len(cand[0]), len(old[0]))
        for a, b in zip(cand[0], old[0]):
            self.assertEqual(a, b)
        self.assertEqual(cand[1], old[1])
        self.assertEqual(cand[2], old[2])


if __name__ == "__main__":
    unittest.main()
