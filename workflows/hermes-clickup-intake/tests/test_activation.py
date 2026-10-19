"""ClickUp human-blocker relay activation, disable and rollback compatibility (blocker plan r12/r11/r9/r6 §7)."""

import dataclasses
import hashlib
import importlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import unittest

from clickup_intake import engine
from clickup_intake import main as glue_main
from clickup_intake.contracts import KanbanResult, Outcome
from test_s4_composition import TMP_ROOT, Lifecycle, World

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_SHA = "b665a985434e296f5b3fd796dc34357d0d6464ba"  # sanitized fixture source revision
BASELINE_ROOT = os.path.join(REPO, "tests", "fixtures", "rollback_base")


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


class WriterCreatesRelayTablesTest(unittest.TestCase):
    def test_a_scan_creates_the_relay_tables_in_the_existing_state_store(self):
        w = World(self)
        rc, _ = w.run()
        self.assertEqual(rc, 0)
        self.assertTrue({"blockers", "fences"} <= tables(w.state_path))


class BlockersCommandTest(unittest.TestCase):
    """r6 §4.1/§7, r9 §7: ``blockers enable|disable`` take intake.lock; enable writes the current time as the
    cutover, disable deletes it (engine.disable_blockers) and queues the operator alerts."""

    def cutover(self, w):
        rows = w.q("SELECT value FROM meta WHERE key='blocker_cutover_ms'")
        return json.loads(rows[0]["value"]) if rows else None

    def test_enable_writes_the_current_time_and_disable_deletes_it(self):
        w = World(self)
        self.assertEqual(w.run()[0], 0)
        self.assertIsNone(self.cutover(w))
        rc, out = w.main("blockers", "enable")
        self.assertEqual((rc, self.cutover(w)), (0, w.clock()))
        self.assertEqual(out, ["blockers enabled; cutover %d" % w.clock()])
        w.clock.advance(60)
        rc, out = w.main("blockers", "disable")
        self.assertEqual((rc, out, self.cutover(w)), (0, ["blockers disabled"], None))
        alerts = [json.loads(r["body"])["text"] for r in w.q("SELECT body FROM outbox WHERE kind='alert'")]
        self.assertIn(engine.DISABLED_PARK_ALERT, alerts)

    def test_protocol_comment_requires_enable(self):
        w = World(self)
        w.run()
        self.assertEqual(w.main("blockers", "protocol-comment"), (1, ["blockers not enabled"]))
        w.main("blockers", "enable")
        self.assertEqual(w.main("blockers", "protocol-comment"), (0, ["protocol comments queued: 0"]))

    def test_a_held_lock_refuses_the_command(self):
        w = World(self)
        w.run()
        lock = glue_main._lock(w.runtime)
        try:
            self.assertEqual(w.main("blockers", "enable")[0], 1)
        finally:
            lock.close()
        self.assertIsNone(self.cutover(w))


class RollbackPreflightTest(unittest.TestCase):
    """r6 §7 step 3, r9 §7, r11 §9, r12 §9 and the r6 §6.2 Compatibility case: the full code rollback
    preflight refuses while the feature is enabled, while any ``:blocker:``/``:bq:`` row (or mirror) is not
    ``done``/``cancelled_stale``, while a blocker is ``releasing``, or while a fence or park is ``open`` or
    ``releasing``. Read-only; unrelated rows (the global INST preflight's concern) do not affect it."""

    def setUp(self):
        self.w = World(self)
        self.assertEqual(self.w.run()[0], 0)

    def sql(self, statement, *args):
        conn = sqlite3.connect(self.w.state_path)
        try:
            conn.execute(statement, args)
            conn.commit()
        finally:
            conn.close()

    def row(self, key, state, kind="kb_comment"):
        self.sql("INSERT INTO outbox(idem_key, task, kind, state) VALUES (?, '86b7t', ?, ?)", key, kind, state)

    def blocker(self, state, q="t_q1"):
        self.sql("INSERT INTO blockers(task, question_card, card, state) VALUES ('86b7t', ?, 't_w1', ?)", q, state)

    def fence(self, kind, state, f="t_f1"):
        self.sql("INSERT INTO fences(task, card, fence_card, kind, state) VALUES ('86b7t', 't_w1', ?, ?, ?)",
                 f, kind, state)

    def preflight(self):
        return self.w.main("blockers", "rollback-preflight")

    def test_a_clean_disabled_store_passes(self):
        self.assertEqual(self.preflight(), (0, ["rollback preflight OK"]))

    def test_enabled_feature_refuses(self):
        self.w.main("blockers", "enable")
        self.assertEqual(self.preflight(), (1, ["rollback preflight refused: blockers enabled"]))

    def test_final_rows_blockers_and_fences_pass_and_unrelated_rows_are_ignored(self):
        self.row("clickup-dp:86b7t:blocker:1:answer:g1", "done", "kb_complete")
        self.row("clickup-dp:86b7t:bq:t_w1:1:ask", "cancelled_stale", "cu_comment")
        self.row("clickup-dp:86b7t:bq:t_w1:1:ask#cu", "done", "clickup_comment")
        self.row("clickup-dp:86b7t:req:1", "failed", "kb_create")  # unrelated: the global INST preflight's
        self.blocker("resumed")
        self.blocker("superseded", "t_q2")
        self.fence("fence", "released")
        self.fence("park", "owner_released", "t_p1")
        self.assertEqual(self.preflight(), (0, ["rollback preflight OK"]))

    def test_each_non_final_blocker_row_state_refuses(self):
        for i, state in enumerate(("pending", "retry_wait", "sending", "ambiguous", "held_ambiguous", "failed")):
            with self.subTest(state=state):
                key = "clickup-dp:86b7t:blocker:%d:answer:g1" % i
                self.row(key, state, "kb_complete")
                self.assertEqual(self.preflight(), (1, [
                    "rollback preflight refused: blocker rows not done or cancelled_stale: 1"]))
                self.sql("UPDATE outbox SET state='done' WHERE idem_key=?", key)

    def test_a_bq_mirror_row_refuses(self):
        self.row("clickup-dp:86b7t:bq:t_w1:1:ask", "done", "cu_comment")
        self.row("clickup-dp:86b7t:bq:t_w1:1:ask#cu", "held_ambiguous", "clickup_comment")
        self.assertEqual(self.preflight(), (1, [
            "rollback preflight refused: blocker rows not done or cancelled_stale: 1"]))

    def test_a_releasing_blocker_refuses(self):
        self.blocker("releasing")
        self.blocker("delivered", "t_q2")  # delivered alone does not refuse: disable already alerted it
        self.assertEqual(self.preflight(), (1, ["rollback preflight refused: blockers releasing: 1"]))

    def test_open_or_releasing_fences_and_parks_refuse(self):
        self.fence("fence", "open")
        self.fence("fence", "releasing", "t_f2")
        self.fence("park", "open", "t_p1")
        self.assertEqual(self.preflight(), (1, [
            "rollback preflight refused: fences open or releasing: 2; parks open or releasing: 1"]))

    def test_the_preflight_writes_nothing(self):
        self.w.main("blockers", "enable")
        self.fence("fence", "open")
        before = self.w.q("SELECT idem_key, state FROM outbox ORDER BY idem_key")
        self.preflight()
        self.assertEqual(self.w.q("SELECT idem_key, state FROM outbox ORDER BY idem_key"), before)
        self.assertEqual(self.w.q("SELECT state FROM fences"), [{"state": "open"}])


class _Outcomes:
    """Hands the baseline package results carrying its OWN ``Outcome`` members (it compares them with ``is``);
    the shared fakes were imported against the candidate's ``contracts``."""

    def __init__(self, inner, outcome):
        self._inner, self._outcome = inner, outcome

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            r = attr(*a, **k)
            if dataclasses.is_dataclass(r) and hasattr(r, "outcome"):
                r = dataclasses.replace(r, outcome=self._outcome(r.outcome.value))
            return r
        return call


class BaselineReadsCandidateDatabaseTest(unittest.TestCase):
    """r6 §6.2 Compatibility (R4), the populated old-reader rollback test: the baseline package (the delivered
    master this branch starts from) runs ``run`` and ``ticket-check`` against a candidate-written DB holding
    ordinary unsent rows, finalized blocker rows (blocker-shaped preconditions) and the relay tables. Expected:
    no TypeError, and the ordinary rows execute."""

    def baseline_main(self, w):
        # The public checkout does not contain the private source repository's history.
        # Use its shipped, hash-checked sanitized baseline rather than a stub or a newer reader.
        with open(os.path.join(BASELINE_ROOT, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["source_revision"], BASELINE_SHA)
        for entry in manifest["files"]:
            with open(os.path.join(BASELINE_ROOT, entry["path"]), "rb") as f:
                data = f.read()
            self.assertEqual(len(data), entry["size"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        root = os.path.join(w.dir, "baseline")
        shutil.copytree(BASELINE_ROOT, root, ignore=shutil.ignore_patterns("manifest.json", "__pycache__"))
        names = lambda: [k for k in sys.modules if k == "clickup_intake" or k.startswith("clickup_intake.")]  # noqa
        saved = {k: sys.modules.pop(k) for k in names()}
        sys.path.insert(0, root)

        def restore():
            sys.path.remove(root)
            for k in names():
                del sys.modules[k]
            sys.modules.update(saved)
        self.addCleanup(restore)
        base = importlib.import_module("clickup_intake.main")
        self.assertEqual(os.path.dirname(os.path.dirname(os.path.abspath(base.__file__))), root)
        outcome = importlib.import_module("clickup_intake.contracts").Outcome

        def run(*argv):
            buf = io.StringIO()
            rc = base.main(list(argv), root=w.root, transport=_Outcomes(w.transport, outcome),
                           port=_Outcomes(w.board, outcome), clock=w.clock, opener=w.opener, out=buf)
            return rc, buf.getvalue().splitlines()
        return run

    def test_baseline_runs_and_ticket_checks_a_populated_candidate_database(self):
        w = World(self)
        life = Lifecycle(w)
        w.main("blockers", "enable")
        real, attempted = w.board.create, []
        w.board.create = lambda *a, **k: attempted.append(a) or KanbanResult(Outcome.DEFINITE_FAIL,
                                                                             {"error": "transient"})
        w.step(lambda: bool(attempted))
        w.board.create = real
        unsent = [r["idem_key"] for r in w.q("SELECT idem_key FROM outbox WHERE state IN ('pending', 'retry_wait')"
                                             " AND kind != 'alert'")]
        self.assertTrue(any(":req:" in k for k in unsent), unsent)
        pre = json.dumps(dict(json.loads(w.q("SELECT preconditions FROM outbox WHERE idem_key=?", unsent[0])[0]
                                         ["preconditions"] or "{}"), blocker_id=1, question_card="t_q1"))
        conn = sqlite3.connect(w.state_path)
        try:
            conn.execute("INSERT INTO blockers(task, question_card, card, state) VALUES (?, 't_q1', 't_w1',"
                         " 'resumed')", (life.tid,))
            conn.execute("INSERT INTO fences(task, card, fence_card, state) VALUES (?, 't_w1', 't_f1', 'released')",
                         (life.tid,))
            for key, kind, state in (("blocker:1:answer:g1", "kb_complete", "done"),
                                     ("blocker:1:corr:g2", "kb_comment", "cancelled_stale"),
                                     ("fence:t_f1:release:g1", "kb_complete", "done")):
                conn.execute("INSERT INTO outbox(idem_key, task, kind, target, preconditions, state) VALUES"
                             " (?, ?, ?, 't_q1', ?, ?)", ("clickup-dp:%s:%s" % (life.tid, key), life.tid, kind,
                                                          pre, state))
            conn.commit()
        finally:
            conn.close()
        base = self.baseline_main(w)
        for _ in range(3):  # past the retry backoff
            w.clock.advance(1861)
            self.assertEqual(base("run")[0], 0)
        states = {r["idem_key"]: r["state"] for r in w.q("SELECT idem_key, state FROM outbox")}
        self.assertEqual({k: states[k] for k in unsent if ":req:" in k}, {k: "done" for k in unsent if ":req:" in k})
        rc, lines = base("ticket-check", "--gate", "t_nogate", "--task", life.tid, "--rev", "1", "--sha", "0" * 64)
        self.assertEqual((rc, len(lines)), (3, 1))
        self.assertTrue(lines[0].startswith("INVALID"), lines)


if __name__ == "__main__":
    unittest.main()
