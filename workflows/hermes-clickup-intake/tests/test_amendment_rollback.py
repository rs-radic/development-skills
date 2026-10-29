"""Workflow repair rollback tests using a hash-pinned public pre-amendment reader.

The fixture is extracted from public Git blobs, never the changing candidate worktree.
"""

import json
import os
import sqlite3
import unittest

from clickup_intake import engine
from test_recovery_activation import baseline_package, baseline_runner, sql
from test_s4_composition import TMP_ROOT, Lifecycle, World

TARGET = "45f7c252df27451248047b25218faf9f8b334417"
SOURCE_TARGET = "149042d7edbfd2b279f00164d66e685d30da8119"
BASELINE_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "tests", "fixtures", "amendment_base")
DRAIN = "e" * 64


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def dump(w, tables=("blockers", "fences", "amendment_answers")):
    conn = sqlite3.connect(w.state_path)
    try:
        return {t: conn.execute("SELECT * FROM %s ORDER BY rowid" % t).fetchall() for t in tables}
    finally:
        conn.close()


def binding(w, bid, state, approval=2, reason=None):
    sql(w, "INSERT INTO amendment_answers(task, blocker_id, approval_id, rev, plan_sha256, question_card, card, gen,"
           " relay_ref, resolution_sha256, source, state, reason, created_ms, updated_ms)"
           " VALUES ('86b7t', ?, ?, 2, ?, 't_q0000001', 't_w0000001', 4, 'r-1', ?, 'plan', ?, ?, 1, 1)",
        bid, approval, "a" * 64, "b" * 64, state, reason)


def blocker(w, state, task="86b7t"):
    conn = sqlite3.connect(w.state_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM blockers").fetchone()[0] + 1
        cur = conn.execute("INSERT INTO blockers(task, question_card, card, state) VALUES (?, ?, 't_w0000001', ?)",
                           (task, "t_q%07d" % n, state))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class ReadinessMatrixTest(unittest.TestCase):
    """``amendment-status`` and ``rollback-prepare`` refuse exactly the bound, non-final applied and in-flight cases,
    without closing admission or writing anything; the ready cases close admission."""

    CASES = [  # (name, setup, refused reason or None)
        ("bound", lambda w: binding(w, blocker(w, "scope_change"), "bound"), "amendment bindings bound: 1"),
        ("bound held", lambda w: (binding(w, blocker(w, "scope_change"), "bound"),
                                  sql(w, "UPDATE tasks SET hold_reason='operator'")), "amendment bindings bound: 1"),
        ("applied closed", lambda w: binding(w, blocker(w, "closed"), "applied"),
         "applied amendment blockers not final: 1"),
        ("applied resolved", lambda w: binding(w, blocker(w, "resolved"), "applied"),
         "applied amendment blockers not final: 1"),
        ("applied releasing pending", lambda w: (binding(w, blocker(w, "releasing"), "applied"), sql(
            w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES ('clickup-dp:86b7t:blocker:1:answer:g4',"
               " '86b7t', 'kb_complete', 'pending')")), "applied amendment blockers not final: 1"),
        ("applied releasing sending", lambda w: (binding(w, blocker(w, "releasing"), "applied"), sql(
            w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES ('clickup-dp:86b7t:blocker:1:answer:g4',"
               " '86b7t', 'kb_complete', 'sending')")),
         "applied amendment blockers not final: 1; outbox rows sending or ambiguous: 1"),
        ("applied delivered", lambda w: binding(w, blocker(w, "delivered"), "applied"),
         "applied amendment blockers not final: 1"),
        ("applied resumed", lambda w: binding(w, blocker(w, "resumed"), "applied"), None),
        ("void", lambda w: binding(w, blocker(w, "scope_change"), "void", reason="newer input"), None),
        ("void terminal", lambda w: (binding(w, blocker(w, "terminal"), "void", reason="terminal"),
                                     binding(w, blocker(w, "terminal"), "applied", approval=3)), None),
        ("applied then scope_change", lambda w: binding(w, blocker(w, "scope_change"), "applied"), None),
        ("ambiguous row", lambda w: sql(w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES"
                                           " ('clickup-dp:86b7t:x', '86b7t', 'kb_comment', 'ambiguous')"),
         "outbox rows sending or ambiguous: 1"),
        ("failed and held rows", lambda w: (
            sql(w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES ('clickup-dp:86b7t:f', '86b7t',"
                   " 'kb_comment', 'failed')"),
            sql(w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES ('clickup-dp:86b7t:h', '86b7t',"
                   " 'kb_comment', 'held_ambiguous')")), None),
        ("ordinary pending rows", lambda w: sql(w, "INSERT INTO outbox(idem_key, task, kind, state) VALUES"
                                                   " ('clickup-dp:86b7t:p', '86b7t', 'kb_comment', 'pending')"), None),
    ]

    def test_matrix_with_and_without_the_enablement_key(self):
        for enabled in (False, True):
            for name, setup, refused in self.CASES:
                with self.subTest(name=name, enabled=enabled):
                    w = World(self)
                    self.assertEqual(w.run()[0], 0)
                    w.main("blockers", "enable")
                    sql(w, "INSERT OR IGNORE INTO tasks(clickup_task_id) VALUES ('86b7t')")
                    if enabled:
                        self.assertEqual(w.main("blockers", "amendment-enable", "--drain-sha", DRAIN)[0], 0)
                    setup(w)
                    before = (dump(w), w.q("SELECT idem_key, state FROM outbox ORDER BY rowid"),
                              w.q("SELECT key, value FROM meta ORDER BY key"))
                    status = w.main("blockers", "amendment-status")[1][-1]
                    rc, out = w.main("blockers", "rollback-prepare", "--target", "45f7c252df27")
                    if refused:
                        self.assertEqual(status, "rollback-target 45f7c252df27: refused (%s)" % refused)
                        self.assertEqual((rc, out), (1, ["rollback refused: " + refused]))
                        after = (dump(w), w.q("SELECT idem_key, state FROM outbox ORDER BY rowid"),
                                 w.q("SELECT key, value FROM meta ORDER BY key"))
                        self.assertEqual(after, before)
                    else:
                        self.assertEqual(status, "rollback-target 45f7c252df27: ready")
                        self.assertEqual((rc, out), (0, ["rollback ready; admission closed"]))
                        self.assertEqual(dump(w), before[0])
                        self.assertEqual(w.q("SELECT idem_key, state FROM outbox ORDER BY rowid"), before[1])


class TargetReadsCandidateDatabaseTest(unittest.TestCase):
    """The actual target completes full scans of a candidate-written database holding every ready-state binding,
    with blocker/binding/fence rows and failed/held rows unchanged and no new board writes."""

    def test_target_scans_preserve_rows(self):
        w = World(self)
        life = Lifecycle(w)
        w.main("blockers", "enable")
        w.main("blockers", "amendment-enable", "--drain-sha", DRAIN)
        life.pickup_to_business()
        tid = life.tid
        for state, kstate, reason in (("resumed", "applied", None), ("scope_change", "void", "newer input"),
                                      ("terminal", "void", "terminal"), ("scope_change", "applied", None)):
            bid = blocker(w, state, task=tid)
            sql(w, "INSERT INTO amendment_answers(task, blocker_id, approval_id, rev, plan_sha256, question_card,"
                   " card, gen, relay_ref, resolution_sha256, source, state, reason, created_ms, updated_ms)"
                   " VALUES (?, ?, 99, 2, ?, 't_q0000001', 't_w0000001', 4, 'r-1', ?, 'plan', ?, ?, 1, 1)",
                tid, bid, "a" * 64, "b" * 64, kstate, reason)
        for key, state in (("clickup-dp:%s:f" % tid, "failed"), ("clickup-dp:%s:h" % tid, "held_ambiguous")):
            sql(w, "INSERT INTO outbox(idem_key, task, kind, target, body, state) VALUES (?, ?, 'kb_comment', 't_q1',"
                   " '{}', ?)", key, tid, state)
        self.assertEqual(w.main("blockers", "rollback-prepare", "--target", "45f7c252df27"),
                         (0, ["rollback ready; admission closed"]))
        before = dump(w)
        dead = w.q("SELECT idem_key, state, attempts FROM outbox WHERE state IN ('failed', 'held_ambiguous')"
                   " ORDER BY rowid")
        base, outcome = _target_package(self, os.path.join(w.dir, "baseline"))
        run = baseline_runner(w, base, outcome)
        mark = len(w.board.calls)
        for _ in range(3):
            w.clock.advance(181)
            self.assertEqual(run("run")[0], 0)
        self.assertEqual(dump(w), before)
        self.assertEqual(w.q("SELECT idem_key, state, attempts FROM outbox WHERE state IN ('failed',"
                             " 'held_ambiguous') ORDER BY rowid"), dead)
        self.assertFalse([c for c in w.board.calls[mark:] if c[1] in ("t_q0000001", "t_w0000001") and c[0] != "show"])
        self.assertEqual(json.loads(w.q("SELECT value FROM meta WHERE key=?", engine.ADMISSION_KEY)[0]["value"])
                         ["target"], "45f7c252df27")  # inert to the target, kept in the state database


class FeatureOffEquivalenceTest(unittest.TestCase):
    """With no bindings the candidate (enablement key absent or present) and the 45f7c25 target produce the same
    outbox, board effects and output for the full lifecycle (blocker relay enabled): the old code running after the
    new code equals the feature being off."""

    def lifecycle(self, w, enable_key):
        w.main("blockers", "enable")
        if enable_key:
            sql(w, "INSERT INTO meta(key, value) VALUES (?, ?)", engine.AMEND_KEY,
                json.dumps({"ms": 1, "drain_sha256": DRAIN}))
        Lifecycle(w).full()
        rows = w.q("SELECT idem_key, task, kind, target, body, preconditions, depends_on, state, outcome, result_ref"
                   " FROM outbox ORDER BY rowid")
        text = json.dumps([rows, w.board.mutations(), w.outputs])
        for d in (w.dir.replace("\\", "\\\\\\\\"), w.dir.replace("\\", "\\\\"), w.dir):
            text = text.replace(d, "<world>")
        return json.loads(text)

    def test_candidate_feature_off_and_target_are_identical(self):
        w = World(self)
        base, outcome = _target_package(self, os.path.join(w.dir, "baseline"))
        w.main = baseline_runner(w, base, outcome)
        old = self.lifecycle(w, True)  # the target with the key present: it never reads it
        for key in (False, True):
            with self.subTest(enable_key=key):
                cand = self.lifecycle(World(self), key)
                self.assertEqual(len(cand[0]), len(old[0]))
                for a, b in zip(cand[0], old[0]):
                    self.assertEqual(a, b)
                self.assertEqual(cand[1], old[1])
                self.assertEqual(cand[2], old[2])


def _target_package(tc, dest):
    """Use the hash-pinned source/public pair without mutating the legacy fixture."""
    import test_recovery_activation as tra
    saved = tra.BASE, tra.PUBLIC_FIXTURE_BASE, tra.BASELINE_ROOT
    tra.BASE, tra.PUBLIC_FIXTURE_BASE, tra.BASELINE_ROOT = SOURCE_TARGET, TARGET, BASELINE_ROOT
    try:
        return baseline_package(tc, dest)
    finally:
        tra.BASE, tra.PUBLIC_FIXTURE_BASE, tra.BASELINE_ROOT = saved



if __name__ == "__main__":
    unittest.main()
