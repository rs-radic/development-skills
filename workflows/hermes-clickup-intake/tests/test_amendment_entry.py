"""Workflow repair r3 §3.10 and §7 U1/U2/U4(h) through the REAL entry.

Every step runs the unmodified ``main.main`` (lock, store, parser, ``Glue.scan`` with ``observe_cards`` ->
``_observe_watched`` -> ``progress_pass`` -> full evaluate -> executor) over the S4 ``World``: fake ClickUp, the
in-memory fake Kanban board as the port (no ``hermes`` CLI, no live board, state or ClickUp) and the fake clock.
The blocker is created, relayed, asked, closed and classified ``material`` by the same simulated humans/workers as
the existing relay tests; the revision, its approval and orchestration go through the real lifecycle; the
operator commands (``blockers enable``, ``blockers amendment-enable``, ``blockers adopt-amendment``, ``hold``,
``release``, ``blockers amendment-status``) are the real CLI.

These scenarios are the only ones in which Q and W are observed by ``Glue`` itself: Q of a ``scope_change``
blocker is a recorded (final) question, so only the r3 §3.10 bound-pair observation shows it.
"""

import hashlib
import json
import os
import re
import unittest

from clickup_intake import render
from clickup_intake.contracts import Outcome
from test_question_relay import sentinel_show
from test_s4_composition import APPROVER_A, TMP_ROOT, FakeBoard, Lifecycle, World

DRAIN = "d" * 64
ANSWER = "Use the EU region."
RESOLUTION = "Use the EU region for every new rack; stop until the plan is revised."
PLAN_R2 = "## ClickUp summary\nDark mode, EU region.\n\n## Plan\nAdd a theme toggle; host in the EU region.\n"


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class EntryBase(unittest.TestCase):
    """r1 developing (W = the r1 final E2E card) -> blockers enabled through the CLI -> W asks question Q -> asked on
    ClickUp -> Approver One answers and closes -> the planner classifies the closure relay ``material`` with a bound
    resolution -> ``scope_change``."""

    def setUp(self):
        w = World(self)
        # Isolation before the first fixture write: the World's root is under tests/.tmp and the Kanban port is the
        # in-memory fake board, so no Hermes home, kanban.db, state DB or ClickUp outside the temporary root is used.
        self.assertTrue(os.path.abspath(w.root).startswith(os.path.abspath(TMP_ROOT) + os.sep), w.root)
        self.assertIsInstance(w.board, FakeBoard)
        self.w, self.b = w, w.board
        board_show = w.board.show

        def show(card):  # the native ``show --json`` field the ``complete Q`` readback reads (r9 §4.4(a) A2)
            r = board_show(card)
            if r.outcome is Outcome.OK:
                done = [e for e in w.board.cards[card]["events"] if e["kind"] == "completed"]
                r.json["latest_summary"] = done[-1]["payload"].get("summary") if done else None
            return r
        w.board.show = show
        self.life = life = Lifecycle(w)
        self.tid = life.tid
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        life.approve_to_released()
        life.orchestrate()
        self.W = life.fe
        rc, lines = w.main("blockers", "enable")
        self.assertEqual(rc, 0, lines)

    # -- helpers -------------------------------------------------------------------------------------------------
    def blocker(self):
        rows = self.w.q("SELECT * FROM blockers ORDER BY id")
        return rows[0] if rows else {"state": None}

    def bindings(self):
        return self.w.q("SELECT * FROM amendment_answers ORDER BY id")

    def approval(self, rev):
        return self.w.q("SELECT * FROM approvals WHERE task=? AND rev=?", self.tid, rev)[0]

    def answer_rows(self):
        return self.w.q("SELECT * FROM outbox WHERE kind='kb_complete' AND idem_key LIKE ? ORDER BY rowid",
                        "%:blocker:%:answer:%")

    def shows(self, card):
        return sum(1 for c, k in self.b.calls if c == "show" and k == card)

    def no_failed_runs(self):
        self.assertEqual([x for x in self.w.outputs if "run failed" in x], [])

    def amend_alerts(self):
        return [x for x in self.w.outputs if "ClickUp amendment binding" in x]

    # -- fixture -------------------------------------------------------------------------------------------------
    def question(self):
        """W ends its run on a new CLICKUP-QUESTION card (a real Kanban id shape) and waits in ``todo``."""
        b, W = self.b, self.W
        self.w.clock.advance(5)
        self.run_id = b.claim(W, "implementor")
        b._end(W, "blocked")
        b.cards[W]["runs"][-1]["ended_at"] = b._now()
        self.Q = q = "t_%08x" % 0x0bee0001
        b.cards[q] = dict(sentinel_show(card=q, title="CLICKUP-QUESTION v1 " + W, w=W, created_s=b._now()),
                          atts=[], subs=[])
        b.cards[q]["task"]["body"] = "Which region?"
        b.cards[W]["parents"].append(q)
        b.cards[W]["task"]["status"] = "todo"
        self.w.step(lambda: self.blocker()["state"] == "relayed", limit=30)

    def ask(self):
        bl = self.blocker()
        self.b.claim(bl["request_card"], "planner")
        self.b.park(bl["request_card"], "CLICKUP-OUT v1 kind=technical_questions relay=%s blocker=%d boundary=none\n"
                    "Which region should we use?" % (bl["relay_ref"], bl["id"]))
        self.w.step(lambda: self.life.bot_q("Which region should we use?"), limit=30)

    def close(self):
        self.life.say(APPROVER_A, ANSWER)
        self.life.say(APPROVER_A, "Technical requirements closed")
        self.w.step(lambda: self.blocker()["state"] == "closed" and self.blocker()["pending_relay"] and self.w.q(
            "SELECT state FROM outbox WHERE kind='kb_unblock' AND target=(SELECT card FROM relays WHERE relay_ref=?)"
            " ORDER BY rowid DESC LIMIT 1", self.blocker()["pending_relay"])[0]["state"] == "done", limit=30)
        self.rel = self.w.q("SELECT * FROM relays WHERE relay_ref=?", self.blocker()["pending_relay"])[0]

    def material(self):
        bl, rel = self.blocker(), self.rel
        self.b.claim(rel["card"], "planner")
        self.b.park(rel["card"],
                    "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=material" % (rel["relay_ref"], rel["gen"]),
                    "CLICKUP-OUT v1 kind=resolution blocker=%d relay=%s gen=%d authority=unchanged\n%s"
                    % (bl["id"], rel["relay_ref"], rel["gen"], RESOLUTION))
        self.w.step(lambda: self.blocker()["state"] == "scope_change", limit=30)
        bl = self.blocker()
        self.assertEqual((bl["resolution_relay"], bl["pending_relay"], bl["resolution_gen"], bl["resolution_body"],
                          bl["delivered_gen"], bl["release_key"]),
                         (rel["relay_ref"], rel["relay_ref"], rel["gen"], RESOLUTION, None, None))
        return bl

    def marker(self):
        bl = self.blocker()
        return "CLICKUP-INCORPORATES v1 blocker=%d question=%s card=%s gen=%d" % (
            bl["id"], bl["question_card"], bl["card"], bl["resolution_gen"])

    def revise(self, markers=True):
        """Plan r2 (a real ``clickup-plan-r2.md`` attachment) PASSed -> published -> approved -> orchestrated with
        W in its lineage -> approval r2 ``started``."""
        b, rel, life = self.b, self.rel, self.life
        text = PLAN_R2 + ("\n" + self.marker() + "\n" if markers else "")
        b.claim(rel["card"], "planner")
        self.sha2 = b.request_review(rel["card"], self.tid, 2, text, rel["gen"])
        b.claim(rel["card"], "plan-reviewer")
        b.finish(rel["card"], "plan-reviewer")
        self.w.step(lambda: life.bot_q("Plan r2, SHA-256"), limit=40)
        life.say(APPROVER_A, "Approve plan r2 for development")
        orch1 = life.orch()
        self.w.step(lambda: [c for c in b.by_key("clickup-dp:%s:orch:" % self.tid) if c != orch1
                             and b.cards[c]["task"]["status"] == "ready"], limit=40)
        a2 = self.approval(2)
        orch2, gate2 = a2["orchestrator_card"], a2["gate_card"]
        b.claim(orch2, "orchestrator")
        self.fe2 = b.worker_create("final E2E r2")
        promo2 = b.worker_create("dev promotion r2", parents=[gate2], assignee="integrator",
                                 body="Promote.\n\n" + render.promotion_block(gate2, self.tid, 2, self.sha2))
        cleanup2 = b.worker_create("cleanup r2", parents=[promo2])
        b.finish(orch2, "orchestrator", {"clickup_gates": {
            "plan_revision": 2, "plan_sha256": self.sha2, "final_e2e": self.fe2, "promotion": promo2,
            "cleanup": cleanup2, "lineage": [self.W, self.fe2, promo2, cleanup2]}})
        self.w.step(lambda: self.approval(2)["state"] == "started", limit=40)
        return self.approval(2)

    def to_bound(self, markers=True):
        self.question()
        self.ask()
        self.close()
        self.material()
        a2 = self.revise(markers)
        for _ in range(3):  # not enabled: a valid marker of a started approval waits unread (r3 §3.8)
            self.w.clock.advance(61)
            self.w.run()
        self.assertEqual(self.bindings(), [])
        self.assertEqual(self.blocker()["state"], "scope_change")
        return a2

    def enable(self):
        rc, lines = self.w.main("blockers", "amendment-enable", "--drain-sha", DRAIN)
        self.assertEqual(rc, 0, lines)

    def first_scan_binds(self):
        """The first confirmed full pass after enablement ingests the marker. Q is not observed in that pass (no
        bound row existed when ``observe_cards`` ran, and a ``scope_change`` question is otherwise never shown), so
        apply rule 8 waits and nothing is queued."""
        q_shows = self.shows(self.Q)
        self.w.step(lambda: self.bindings(), limit=15)
        [k] = self.bindings()
        self.assertEqual((k["state"], k["source"]), ("bound", "plan"))
        self.assertEqual(self.shows(self.Q), q_shows)
        self.assertEqual(self.answer_rows(), [])
        return k

    def until_applied(self, k):
        """Later scans: r3 §3.10 shows Q and W of the bound pair; the next confirmed full pass applies (rule 10)."""
        q_shows = self.shows(self.Q)
        self.w.step(lambda: self.bindings()[0]["state"] != "bound", limit=15)
        self.assertGreater(self.shows(self.Q), q_shows)
        self.assertEqual([(x["id"], x["state"]) for x in self.bindings()], [(k["id"], "applied")])
        # the existing ``_confirm`` of a later confirmed pass records the delivered generation
        self.w.step(lambda: self.blocker()["state"] == "delivered", limit=15)

    def check_incorporated_delivery(self, a, source="plan", receipt=None):
        [k] = self.bindings()
        bl, rel, W, q = self.blocker(), self.rel, self.W, self.Q
        self.assertEqual((k["state"], k["source"], k["receipt_sha256"], k["blocker_id"], k["approval_id"],
                          k["question_card"], k["card"], k["gen"], k["relay_ref"], k["resolution_sha256"]),
                         ("applied", source, receipt, bl["id"], a["id"], q, W, rel["gen"], rel["relay_ref"],
                          _sha(RESOLUTION)))
        [row] = self.answer_rows()
        g = rel["gen"]
        self.assertEqual((row["idem_key"], row["target"], row["state"]),
                         ("clickup-dp:%s:blocker:%d:answer:g%d" % (self.tid, bl["id"], g), q, "done"))
        src = source + (", receipt SHA-256 %s" % receipt if receipt else "")
        body = ("The decision for this question (ClickUp resolution gen %d, relay %s, stored answer SHA-256 %s) is"
                " incorporated in approved plan r2 (SHA-256 %s), approved in ClickUp comment %s (approval %d,"
                " accounts_gen %d); binding %d (%s). Follow that plan and the latest governing comments on this card;"
                " they supersede any interim instructions that accompanied the original question."
                % (g, rel["relay_ref"], _sha(RESOLUTION), self.sha2, a["comment_id"], a["id"], a["accounts_gen"],
                   k["id"], src))
        summary = "CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s\n%s\nThis answer grants no new authority;" \
                  " boundary: none — unchanged." % (q, W, g, self.tid, body)
        p = json.loads(row["body"])
        self.assertEqual((p["summary"], p["gen"], p["answer_sha256"]), (summary, g, _sha(body)))
        pre = json.loads(row["preconditions"])
        t = self.w.task(self.tid)
        self.assertEqual((pre["approval_id"], pre["input_gen"], pre["scope_gen"], pre["blocker_id"]),
                         (a["id"], t["input_gen"], t["scope_gen"], bl["id"]))
        # Q was completed through the fake board with exactly that summary; the stored resolution is not replayed.
        self.assertEqual(self.b.cards[q]["task"]["status"], "done")
        [ev] = [e for e in self.b.cards[q]["events"] if e["kind"] == "completed"]
        self.assertEqual(ev["payload"]["summary"], summary)
        self.assertNotIn(RESOLUTION, summary)
        self.assertEqual((bl["state"], bl["delivered_gen"], bl["delivered_sha256"]), ("delivered", g, _sha(body)))
        return g

    def resume(self, g):
        """W is claimed again, creates its answer fence (released by a later full pass), then ACKs ``gen=g``."""
        b, W, q = self.b, self.W, self.Q
        self.w.clock.advance(5)
        run = b.claim(W, "implementor")
        b._event(W, "claimed", run_id=run)  # the native claim event (FakeBoard.claim records only the run)
        f = "t_%08x" % 0x0bee00f1
        b.cards[f] = dict(sentinel_show(card=f, title="CLICKUP-FENCE v1 " + W, w=W, created_s=b._now()),
                          atts=[], subs=[])
        b.cards[f]["task"]["body"] = "Answer fence."
        b.cards[W]["parents"].append(f)
        self.w.step(lambda: self.w.q("SELECT state FROM fences")[:1] == [{"state": "released"}], limit=10)
        b.worker_comment(W, "implementor", "CLICKUP-ANSWER-ACK v1 question=%s card=%s gen=%d run=%d fence=%s\n"
                         "Continuing." % (q, W, g, run, f))
        self.w.step(lambda: self.blocker()["state"] == "resumed", limit=10)
        replies = [c["comment_text"] for c in self.w.bot_comments(self.tid)]
        self.assertEqual(sum("The blocked step has resumed with your answer." in x for x in replies), 1)
        self.assertEqual(sum(x.startswith("Answer recorded.") for x in replies), 1)


class EntryPlanMarkerTest(EntryBase):
    def test_u1_marker_revision_is_bound_observed_via_3_10_applied_and_delivered_through_the_real_entry(self):
        a2 = self.to_bound()
        self.enable()
        k = self.first_scan_binds()
        self.until_applied(k)  # the incorporated row is queued and sent by the same scan's executor
        g = self.check_incorporated_delivery(a2)
        n = len(self.w.q("SELECT * FROM outbox"))
        for _ in range(5):  # no correction loop, no duplicate
            self.w.clock.advance(61)
            self.w.run()
        self.assertEqual((self.blocker()["state"], len(self.answer_rows())), ("delivered", 1))
        self.assertEqual(len(self.w.q("SELECT * FROM outbox")), n)
        self.resume(g)
        status = self.w.main("blockers", "amendment-status")[1]
        self.assertIn("amendment continuation: enabled %d drain %s" % (
            json.loads(self.w.q("SELECT value FROM meta WHERE key='amendment_cutover_ms'")[0]["value"])["ms"],
            DRAIN[:12]), status)
        self.assertIn("rollback-target 45f7c252df27: ready", status)
        self.assertEqual(self.amend_alerts(), [])
        self.no_failed_runs()

    def test_u4h_operator_hold_and_release_through_the_cli_pause_then_the_reissued_ticket_applies(self):
        a2 = self.to_bound()
        b = self.b
        b.finish(self.fe2, "e2e-tester")  # r2 final E2E done -> approval r2 ticketed
        self.w.step(lambda: self.approval(2)["state"] == "ticketed", limit=30)
        epoch = self.approval(2)["ticket_epoch"]
        self.assertEqual(self.bindings(), [])
        self.enable()
        k = self.first_scan_binds()
        self.assertEqual(self.w.main("hold", self.tid), (0, ["held"]))
        a = self.approval(2)
        self.assertEqual((a["state"], a["revoked_reason"]), ("revoked", "operator"))
        q_shows = self.shows(self.Q)
        for _ in range(3):  # held: the bound pair is not observed (§3.10 unheld only), no apply/void/row/alert
            self.w.clock.advance(61)
            self.w.run()
        # W (the r1 final E2E) is still shown by the native approval-gate observation; Q only via §3.10
        self.assertEqual(self.shows(self.Q), q_shows)
        self.assertEqual([(x["id"], x["state"]) for x in self.bindings()], [(k["id"], "bound")])
        self.assertEqual(self.answer_rows(), [])
        self.assertEqual(self.amend_alerts(), [])
        self.assertEqual(self.w.main("release", self.tid), (0, ["released"]))
        seen = []

        def observe():
            st = self.approval(2)["state"], self.bindings()[0]["state"]
            seen.append(st)
            self.assertNotEqual(st[1], "void")
        self.w.step(lambda: self.bindings()[0]["state"] == "applied", limit=20, world=observe)
        self.w.step(lambda: self.blocker()["state"] == "delivered", limit=15)
        # rule 7: the confirmed pass whose native ``_approvals`` reissued the ticket still waited (``_amend`` runs
        # before ``_approvals``); a later pass applied, never while the ticket was revoked
        self.assertEqual(next(s for s in seen if s[0] != "revoked"), ("ticketed", "bound"), seen)
        self.assertTrue(all(s[0] == "ticketed" for s in seen if s[1] == "applied"), seen)
        a = self.approval(2)
        self.assertEqual(a["state"], "ticketed")
        self.assertGreater(a["ticket_epoch"], epoch)
        g = self.check_incorporated_delivery(a)
        self.resume(g)
        self.assertEqual(self.amend_alerts(), [])
        self.no_failed_runs()


class EntryOperatorAdoptionTest(EntryBase):
    def test_u2_adopt_amendment_through_the_cli_then_real_scans_observe_apply_and_deliver(self):
        a2 = self.to_bound(markers=False)
        self.enable()
        bl = self.blocker()
        line = ("CLICKUP-INCORPORATES v1 blocker=%d question=%s card=%s gen=%d relay=%s resolution_sha256=%s task=%s"
                " approval=%d rev=2 sha=%s" % (bl["id"], self.Q, self.W, bl["resolution_gen"], bl["resolution_relay"],
                                               _sha(RESOLUTION), self.tid, a2["id"], self.sha2))
        receipt = os.path.join(self.w.dir, "receipt.md")
        with open(receipt, "w", encoding="utf-8", newline="\n") as f:
            f.write("# reviewed owner receipt\n\n" + line + "\n")
        with open(receipt, "rb") as f:
            rsha = hashlib.sha256(f.read()).hexdigest()
        argv = ("blockers", "adopt-amendment", "--blocker", str(bl["id"]), "--approval", str(a2["id"]),
                "--receipt", receipt)
        self.assertEqual(self.w.main(*argv, "--receipt-sha", "0" * 64), (1, ["adopt refused: receipt SHA-256 mismatch"]))
        self.assertEqual(self.bindings(), [])
        rc, lines = self.w.main(*argv, "--receipt-sha", rsha)
        self.assertEqual(rc, 0, lines)
        [k] = self.bindings()
        self.assertEqual(lines, ["binding %d blocker %d approval %d bound" % (k["id"], bl["id"], a2["id"])])
        self.assertEqual((k["state"], k["source"], k["receipt_sha256"]), ("bound", "operator", rsha))
        self.assertEqual(self.w.main(*argv, "--receipt-sha", rsha),
                         (0, ["binding blocker %d approval %d (existing)" % (bl["id"], a2["id"])]))
        status = self.w.main("blockers", "amendment-status")[1]
        self.assertTrue(any(x.startswith("rollback-target 45f7c252df27: refused (") for x in status), status)
        self.until_applied(k)  # r3 §3.10 observes Q and W of the operator binding -> apply -> delivery
        g = self.check_incorporated_delivery(a2, "operator", rsha)
        self.resume(g)
        self.assertIn("rollback-target 45f7c252df27: ready", self.w.main("blockers", "amendment-status")[1])
        self.no_failed_runs()


if __name__ == "__main__":
    unittest.main()
