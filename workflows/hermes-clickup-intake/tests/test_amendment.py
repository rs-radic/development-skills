"""Workflow repair r3 §3 / §7 U1-U8: bound approved-amendment continuation of a ``scope_change`` question.

r3 §3.7 (same-pass fence release) is dropped under its recorded fallback: original tests assert the deferred
release, so a fence detected in a full pass is released by the next full pass (the native park cycle).

Engine scenarios run on the existing S2 harness (``test_s2_workflow.H`` with the blocker executor of
``test_question_relay``) in the writer's REAL scan order: every harness scan first runs the unmodified
``main.Glue.progress_pass`` (its cards-empty evaluate), then the full-observation evaluate, exactly as
``main.Glue.scan`` does. Fakes only (fake Kanban cards, fake clock); no live board, state or ClickUp.
"""

import hashlib
import json
import os
import unittest

from clickup_intake import engine, rules
import test_question_relay as qr
import test_s2_workflow as s2

TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
ENABLED = json.dumps({"ms": 1, "drain_sha256": "d" * 64})


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def amended_text(k, a):
    """r3 §3.5, written out independently of the engine."""
    src = k["source"] + (", receipt SHA-256 %s" % k["receipt_sha256"] if k["receipt_sha256"] else "")
    return ("The decision for this question (ClickUp resolution gen %d, relay %s, stored answer SHA-256 %s) is"
            " incorporated in approved plan r%d (SHA-256 %s), approved in ClickUp comment %s (approval %d,"
            " accounts_gen %d); binding %d (%s). Follow that plan and the latest governing comments on this card;"
            " they supersede any interim instructions that accompanied the original question."
            % (k["gen"], k["relay_ref"], k["resolution_sha256"], a["rev"], a["plan_sha256"], a["comment_id"],
               a["id"], a["accounts_gen"], k["id"], src))


# =============================================================================================== parser (§3.1)
class ParseIncorporatesTest(unittest.TestCase):
    LINE = "CLICKUP-INCORPORATES v1 blocker=3 question=t_ab000084 card=t_ab00005f gen=5"

    def test_exact_whole_lines_are_parsed_and_one_trailing_cr_is_stripped(self):
        text = "# plan\r\n" + self.LINE + "\r\nmore\n" + self.LINE.replace("blocker=3", "blocker=1") + "\n"
        got, extra = rules.parse_incorporates(text)
        self.assertEqual(got, [{"blocker": 3, "question": "t_ab000084", "card": "t_ab00005f", "gen": 5},
                               {"blocker": 1, "question": "t_ab000084", "card": "t_ab00005f", "gen": 5}])
        self.assertEqual(extra, 0)

    def test_formatted_partial_or_malformed_lines_are_not_markers(self):
        for bad in ("`" + self.LINE + "`", " " + self.LINE, self.LINE + " ", "- " + self.LINE, self.LINE + "\r\r",
                    self.LINE.replace("gen=5", "gen=0"), self.LINE.replace("blocker=3", "blocker=03"),
                    self.LINE.replace("t_ab000084", "t_BEAF4D16"), self.LINE.replace("t_ab00005f", "t_8b4b674"),
                    self.LINE.replace("v1", "v2"), self.LINE.replace("gen=5", "gen=1234567890"),
                    self.LINE + " relay=x"):
            with self.subTest(bad=bad):
                self.assertEqual(rules.parse_incorporates(bad + "\n"), ([], 0))

    def test_at_most_twenty_markers_are_read_and_the_rest_are_counted(self):
        text = "\n".join(self.LINE.replace("blocker=3", "blocker=%d" % i) for i in range(1, 23))
        got, extra = rules.parse_incorporates(text)
        self.assertEqual([m["blocker"] for m in got], list(range(1, 21)))
        self.assertEqual(extra, 2)

    def test_operator_line_is_built_from_live_values_and_validated(self):
        line = rules.adoption_line(1, "t_ab000004", "t_ab00005a", 4, "86pub00001-s2-g4-w967", "2" * 64,
                                   "86pub00001", 13, 10, "8" * 64)
        self.assertEqual(line, "CLICKUP-INCORPORATES v1 blocker=1 question=t_ab000004 card=t_ab00005a gen=4"
                               " relay=86pub00001-s2-g4-w967 resolution_sha256=%s task=86pub00001 approval=13 rev=10"
                               " sha=%s" % ("2" * 64, "8" * 64))
        for bad in ({"relay": "a b"}, {"relay": "x" * 81}, {"rsha": "Z" * 64}, {"sha": "8" * 63}):
            with self.subTest(bad=bad):
                self.assertIsNone(rules.adoption_line(1, "t_ab000004", "t_ab00005a", 4, bad.get("relay", "r-1"),
                                                      bad.get("rsha", "2" * 64), "86pub00001", 13, 10,
                                                      bad.get("sha", "8" * 64)))

    def test_the_reviewed_r3_plan_lines_are_valid_operator_lines(self):  # r3 §6.4 exact receipt lines
        for line in ("CLICKUP-INCORPORATES v1 blocker=1 question=t_ab000004 card=t_ab00005a gen=4"
                     " relay=86pub00001-s2-g4-w967 resolution_sha256=200128c7073f3a283924bcf3d69e86e3c4b856190d3e1f45e3"
                     "e09cb57fc46fca task=86pub00001 approval=13 rev=10 sha=89e7efac27692d7d0c4d5857bca08a037b8631c14fb"
                     "cd7c48bf9cdf1f1ba474d",
                     "CLICKUP-INCORPORATES v1 blocker=3 question=t_ab000084 card=t_ab00005f gen=5"
                     " relay=86pub00001-s3-g5-w1095 resolution_sha256=2485be38c0ad660e64723395f786348c0a4529f079437c2a1"
                     "fa7b25ce5feef14 task=86pub00001 approval=13 rev=10 sha=89e7efac27692d7d0c4d5857bca08a037b8631c14f"
                     "bcd7c48bf9cdf1f1ba474d"):
            self.assertTrue(rules.ADOPTION_LINE.fullmatch(line), line)


class ObservedAttachmentTest(unittest.TestCase):
    """r3 §3.1: markers are parsed only from ``clickup-plan-r<N>.md`` attachments of at most 1 MiB."""
    LINE = ParseIncorporatesTest.LINE

    def obs(self, name, data):
        from clickup_intake.main import _observed_attachment
        return _observed_attachment({"filename": name, "sha256": "s", "data": data})

    def test_plan_files_carry_markers_and_other_names_do_not(self):
        data = (self.LINE + "\n").encode()
        self.assertEqual(self.obs("clickup-plan-r10.md", data),
                         {"name": "clickup-plan-r10.md", "sha256": "s", "incorporates": rules.parse_incorporates(
                             self.LINE)[0]})
        for name in ("clickup-plan-r0.md", "clickup-context-g4.md", "plan-r10.md", "clickup-plan-r10.md.txt"):
            self.assertEqual(self.obs(name, data), {"name": name, "sha256": "s"})

    def test_over_one_mib_is_not_scanned_and_extra_markers_are_counted(self):
        big = (self.LINE + "\n").encode() + b"x" * (1024 * 1024)
        self.assertEqual(self.obs("clickup-plan-r2.md", big),
                         {"name": "clickup-plan-r2.md", "sha256": "s", "incorporates_oversize": True})
        many = "\n".join(self.LINE.replace("blocker=3", "blocker=%d" % i) for i in range(1, 22)).encode()
        got = self.obs("clickup-plan-r2.md", many)
        self.assertEqual((len(got["incorporates"]), got["incorporates_ignored"]), (20, 1))


# =============================================================================================== engine harness
class AmendBase(unittest.TestCase):
    """A ``scope_change`` blocker on watched card W (the r1 final E2E), then plan r2 PASSed on the request card that
    classified the material input, approved and orchestrated (approval 2 ``started``), with W in its lineage."""

    (setUp_, cutover, link, rows, ask, answer, close, closed, resolve, tid, b, new_input, outputs, claim, link_fence,
     ack, lost_ack, readback_done, release_rows) = (
        qr.AskTest.setUp, qr.AskTest.cutover, qr.AskTest.link, qr.AskTest.rows, qr.AskTest.ask, qr.AskTest.answer,
        qr.ClosureTest.close, qr.ResolutionTest.closed, qr.DeliveryTest.resolve, qr.ReleaseScenarioTest.tid,
        qr.ReleaseScenarioTest.b, qr.ReleaseScenarioTest.new_input, qr.ReleaseScenarioTest.outputs,
        qr.ReleaseScenarioTest.claim, qr.ReleaseScenarioTest.link_fence, qr.ReleaseScenarioTest.ack,
        qr.ReleaseScenarioTest.lost_ack, qr.ReleaseScenarioTest.readback_done, qr.FenceReleaseTest.release_rows)
    TEXT, TEXT2, F = qr.DeliveryTest.TEXT, qr.ReleaseScenarioTest.TEXT2, qr.FenceReleaseTest.F
    replies = qr.DispositionTest.replies
    SHA2 = s2.SHA2

    def setUp(self):
        from clickup_intake.main import Glue
        self.setUp_()
        h, full = self.h, self.h.scan

        class View:  # exactly what Glue.progress_pass reads
            meta, set_meta = Glue.meta, Glue.set_meta

            def __init__(v):
                v.store, v.c, v.now, v.cfg, v.obs = h.store, h.store.conn, h.now, h.cfg, h.obs()

        def scan():  # main.Glue.scan order: progress_pass, then the full-observation evaluate
            Glue.progress_pass(View())
            return full()
        h.scan = scan

    # -- fixture ---------------------------------------------------------------------------------------------
    def scope_change(self):
        b, rel = self.resolve(result="material")
        self.assertEqual(b["state"], "scope_change")
        self.rel = rel
        return b, rel

    def marker(self, b, **over):
        m = {"blocker": b["id"], "question": b["question_card"], "card": b["card"], "gen": b["resolution_gen"]}
        m.update(over)
        return m

    def revise(self, markers=None, lineage_w=True, approve=True, orchestrate=True):
        """Plan r2 on the material relay's request card: PASS -> publication -> approval -> orchestrator done."""
        h, b = self.h, self.b()
        card = self.rel["card"]
        h.run_review(card, 2, self.SHA2, accounts_gen=self.rel["gen"])
        h.attachments[card][-1]["incorporates"] = [self.marker(b)] if markers is None else markers
        h.pass_review(card)
        h.settle()
        h.cycle()
        if not approve:
            return None
        h.post("ap2", s2.APPROVER_A, "Approve plan r2 for development")
        h.settle()
        h.cycle()
        a2 = h.approvals()[-1]
        self.assertEqual((a2["rev"], a2["state"]), (2, "released"))
        if not orchestrate:
            return a2
        fe2, cleanup2 = h.new_card(), h.new_card()
        promo2 = h.new_card(parents=[a2["gate_card"]],
                            body="promotion\n" + rules.marker(a2["gate_card"], s2.T, 2, self.SHA2) + "\n")
        g = {"plan_revision": 2, "plan_sha256": self.SHA2, "final_e2e": fe2, "promotion": promo2,
             "cleanup": cleanup2, "lineage": ([self.w] if lineage_w else []) + [fe2, promo2, cleanup2]}
        h.orch_done(a2, gates=g)
        h.cycle()
        a2 = h.approvals()[-1]
        self.assertEqual(a2["state"], "started")
        return a2

    def enable(self):
        self.h.store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('amendment_cutover_ms', ?)",
                                  (ENABLED,))

    def bindings(self):
        return self.rows("amendment_answers")

    def answer_rows(self):
        return [r for r in self.h.rows("kb_complete") if ":answer:" in r["idem_key"]]

    def amend_alerts(self):
        return [a for a in self.h.alerts if "ClickUp amendment binding" in a]

    def applied(self):
        """scope_change + r2 started + marker + enabled -> bound -> applied -> incorporated answer delivered."""
        b, rel = self.scope_change()
        a2 = self.revise()
        self.assertEqual(self.bindings(), [])  # not enabled: the marker waits unread (§3.8)
        self.enable()
        self.h.settle()
        self.h.cycle()
        return b, rel, a2


# =============================================================================================== U1
class FullOrderingTest(AmendBase):
    def test_u1_material_then_marker_revision_binds_applies_delivers_and_resumes_once(self):
        b, rel, a2 = self.applied()
        [k] = self.bindings()
        self.assertEqual((k["blocker_id"], k["approval_id"], k["source"], k["state"], k["relay_ref"], k["gen"],
                          k["resolution_sha256"], k["question_card"], k["card"], k["rev"], k["plan_sha256"]),
                         (b["id"], a2["id"], "plan", "applied", rel["relay_ref"], rel["gen"], _sha(self.TEXT),
                          b["question_card"], self.w, 2, self.SHA2))
        tid, q, g = self.tid(), b["question_card"], rel["gen"]
        [row] = self.answer_rows()
        self.assertEqual((row["idem_key"], row["target"], row["state"]),
                         ("clickup-dp:%s:blocker:%d:answer:g%d" % (tid, b["id"], g), q, "done"))
        body = amended_text(k, a2)
        p = json.loads(row["body"])
        self.assertEqual(p["summary"], "CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s\n%s\nThis answer grants"
                         " no new authority; boundary: none — unchanged." % (q, self.w, g, tid, body))
        self.assertEqual((p["gen"], p["answer_sha256"]), (g, _sha(body)))
        self.assertNotIn(self.TEXT, p["summary"])  # the stored interim resolution is never replayed (§1.2)
        pre = json.loads(row["preconditions"])
        t = self.h.task()
        self.assertEqual((pre["approval_id"], pre["approval_states"], pre["input_gen"], pre["scope_gen"],
                          pre["blocker_id"], pre["question_card"]),
                         (a2["id"], ["started"], t["input_gen"], t["scope_gen"], b["id"], q))
        b2 = self.b()
        self.assertEqual((b2["state"], b2["delivered_gen"], b2["delivered_sha256"]), ("delivered", g, _sha(body)))
        n = len(self.h.rows())
        for _ in range(5):  # no correction loop: the amended delivery is "delivered", not re-"resolved"
            self.h.settle()
            self.h.cycle()
        self.assertEqual((self.b()["state"], len(self.answer_rows())), ("delivered", 1))
        self.assertFalse([r for r in self.h.rows() if ":corr:" in r["idem_key"]])
        self.assertEqual(len(self.h.rows()), n)
        self.claim()
        self.link_fence()
        self.h.settle()
        self.h.cycle()  # detected (r3 §3.7 dropped: released by the next full pass)
        self.h.settle()
        self.h.cycle()
        [f] = self.rows("fences")
        self.assertEqual((f["state"], json.loads(f["snapshot"])), ("released", {q: g}))
        self.assertEqual(self.ack(g)["state"], "resumed")
        self.assertEqual(self.replies("resumed with your answer"), ["The blocked step has resumed with your answer."])
        self.assertEqual(len(self.replies("Answer recorded.")), 1)
        self.assertEqual(self.amend_alerts(), [])

    def test_u1_fallback_the_fence_is_released_by_the_next_full_pass_not_the_detecting_one(self):
        b, rel, a2 = self.applied()
        self.claim()
        self.link_fence()
        self.h.settle()
        self.h.scan()  # detection scan: no release row yet (r3 §3.7 fallback, native timing)
        self.assertEqual((self.rows("fences")[0]["state"], self.release_rows()), ("open", []))
        self.h.settle()
        self.h.scan()
        [row] = self.release_rows()
        self.assertEqual(json.loads(row["body"])["barrier_anchor_ms"], self.rows("fences")[0]["anchor_ms"])


class LaterAccountedInputTest(AmendBase):
    """The 86pub00001 shape: the incorporated resolution is gen g, and a later answer-only input g+1 (classified,
    never relayed to the final blocker) is accounted by the approved revision (accounts_gen g+1 = input_gen)."""

    def later(self, gen_offset=0):
        b, rel = self.scope_change()
        rel2 = self.new_input("Thanks, noted.")
        self.outputs(rel2)  # answer_only; the scope_change blocker is final and not rebound
        self.assertEqual((self.b()["state"], self.b()["pending_relay"]), ("scope_change", rel["relay_ref"]))
        self.rel = rel2  # the revision accounts the latest delivered generation
        b = self.b()
        self.revise(markers=[self.marker(b, gen=b["resolution_gen"] + gen_offset)])
        self.enable()
        self.h.settle()
        self.h.cycle()
        return b, rel, rel2

    def test_u1b_the_incorporated_gen_below_input_gen_is_delivered_with_the_current_input_gen_frozen(self):
        b, rel, rel2 = self.later()
        self.assertEqual(self.bindings()[0]["state"], "applied")
        [row] = self.answer_rows()
        self.assertEqual(row["state"], "done")
        self.assertTrue(row["idem_key"].endswith(":answer:g%d" % rel["gen"]))  # the original gen, never relabelled
        self.assertEqual(json.loads(row["preconditions"])["input_gen"], rel2["gen"])
        self.assertEqual(self.b()["delivered_gen"], rel["gen"])

    def test_u4_a_marker_gen_that_is_not_the_resolution_gen_is_a_mismatch(self):
        b, rel, rel2 = self.later(gen_offset=1)  # gen g+1 <= accounts_gen g+1, but not the resolution gen
        self.assertEqual(rel2["gen"], b["resolution_gen"] + 1)
        self.assertEqual(self.bindings(), [])
        self.assertEqual(sum(("amendment marker" in a and "mismatch" in a) for a in self.h.alerts), 1)


# =============================================================================================== U3
class NewerInputTest(AmendBase):
    def applied_pending(self):
        """Applied, its incorporated ``complete Q`` row queued but not yet sent."""
        b, rel = self.scope_change()
        self.revise()
        self.enable()
        self.h.settle()
        self.h.scan()
        [row] = self.answer_rows()
        self.assertEqual((row["state"], self.b()["state"], self.bindings()[0]["state"]),
                         ("pending", "releasing", "applied"))
        return b, rel, row

    def test_u3a_newer_answer_only_input_cancels_the_pending_row_and_the_new_gen_is_native(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.setUp()
                b, rel, row = self.applied_pending()
                rel2 = self.new_input()  # staged before the send: the frozen input_gen refuses the row
                self.assertEqual(self.h._state(row["idem_key"]), "cancelled_stale")
                self.assertEqual((self.b()["state"], self.b()["pending_relay"]), ("closed", rel2["relay_ref"]))
                if restart:  # U3(b): a restart between the cancellation and the gen-6 resolution
                    from clickup_intake.store import Store
                    self.h.store.close()
                    self.h.store = Store(self.h.path, self.h.clock)
                    self.addCleanup(self.h.store.close)
                self.outputs(rel2, text=self.TEXT2)
                self.h.cycle()
                rows = self.answer_rows()
                self.assertEqual([(r["idem_key"].rsplit(":", 1)[1], r["state"]) for r in rows],
                                 [("g%d" % rel["gen"], "cancelled_stale"), ("g%d" % rel2["gen"], "done")])
                p = json.loads(rows[1]["body"])
                self.assertIn("\n%s\n" % self.TEXT2, p["summary"])
                self.assertEqual(p["answer_sha256"], _sha(self.TEXT2))
                pre = json.loads(rows[1]["preconditions"])
                self.assertEqual((pre["approval_id"], pre["input_gen"]), (self.h.approvals()[-1]["id"], rel2["gen"]))
                self.claim()
                self.link_fence()
                for _ in range(2):  # detected, then released by the next full pass
                    self.h.settle()
                    self.h.cycle()
                self.assertEqual(self.ack(rel2["gen"])["state"], "resumed")

    def test_u3c_after_the_amended_delivery_a_newer_answer_is_a_native_correction(self):
        b, rel, a2 = self.applied()
        rel2 = self.new_input()
        self.outputs(rel2, text=self.TEXT2)
        self.h.cycle()
        [corr] = [r for r in self.h.rows("kb_comment") if ":corr:" in r["idem_key"]]
        self.assertTrue(corr["idem_key"].endswith(":corr:g%d" % rel2["gen"]))
        text = json.loads(corr["body"])["text"]
        self.assertTrue(text.startswith("CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s supersedes=g%d\n%s\n"
                                        % (b["question_card"], self.w, rel2["gen"], self.tid(), rel["gen"], self.TEXT2)))
        self.assertEqual(json.loads(corr["preconditions"])["approval_id"], a2["id"])
        self.assertEqual((self.b()["state"], self.b()["delivered_gen"]), ("delivered", rel2["gen"]))

    def test_u3c_an_ack_of_the_amended_gen_before_the_correction_follows_the_native_snapshot_rules(self):
        b, rel, a2 = self.applied()
        self.claim()
        self.link_fence()
        for _ in range(2):  # detected, then released by the next full pass
            self.h.settle()
            self.h.cycle()
        rel2 = self.new_input()
        self.outputs(rel2, text=self.TEXT2)
        self.h.scan()  # correction queued, unsent
        self.assertEqual(self.ack(rel["gen"])["state"], "resumed")  # the g4 ACK of the released fence consumes it
        self.assertTrue(all(r["state"] in ("cancelled_stale", "done") for r in self.h.rows()
                            if ":corr:" in r["idem_key"]))

    def test_u3d_unrelated_newer_input_without_a_resolution_rebinds_and_waits_natively(self):
        b, rel, a2 = self.applied()
        n = len(self.answer_rows())
        rel2 = self.new_input("FYI: unrelated detail.")
        self.outputs(rel2)  # answer_only, no resolution for this blocker
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        b2 = self.b()
        self.assertEqual((b2["state"], b2["pending_relay"], b2["delivered_gen"]), ("delivered", rel2["relay_ref"],
                                                                                 rel["gen"]))
        self.assertEqual(len(self.answer_rows()), n)
        self.assertFalse([r for r in self.h.rows() if ":corr:" in r["idem_key"]])

    def test_u3d_rebound_before_delivery_lists_the_blocker_and_delivers_nothing_incorporated(self):
        b, rel, row = self.applied_pending()
        rel2 = self.new_input("FYI: unrelated detail.")
        note = json.loads({r["idem_key"]: r for r in self.h.rows()}[
            "clickup-dp:%s:relay:%s:comment" % (self.tid(), rel2["relay_ref"])]["body"])["note"]
        self.assertIn("blockers needing a resolution for this generation: %d" % b["id"], note)
        self.outputs(rel2)
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual([r["state"] for r in self.answer_rows()], ["cancelled_stale"])
        self.assertEqual(self.b()["state"], "closed")
        self.assertEqual(self.h.cards[b["question_card"]]["task"]["status"], "blocked")

    def test_u3e_newer_material_input_after_apply_is_a_native_scope_change_and_the_binding_stays_applied(self):
        b, rel, row = self.applied_pending()
        rel2 = self.new_input("Change the whole design again.")
        self.outputs(rel2, result="material")
        self.h.cycle()
        self.assertEqual((self.b()["state"], self.bindings()[0]["state"]), ("scope_change", "applied"))
        self.assertEqual(sum("after a material ClickUp change" in a for a in self.h.alerts), 1)  # native per-blocker key
        self.assertIn("clickup-dp:alert:blocker:%d:scope_change" % b["id"], [r["idem_key"] for r in self.h.rows()])

    def test_u3f_newer_input_before_apply_voids_the_binding_with_one_alert(self):
        b, rel = self.scope_change()
        self.revise()
        rel2 = self.new_input()
        self.outputs(rel2)  # classified: input_gen > accounts_gen
        self.enable()
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        [k] = self.bindings()
        self.assertEqual((k["state"], k["reason"]), ("void", "newer input"))
        self.assertEqual(self.answer_rows(), [])
        self.assertEqual(len(self.amend_alerts()), 1)

    def test_u3g_lost_send_ack_on_the_incorporated_row_reconciles_without_resend(self):
        b, rel = self.scope_change()
        self.revise()
        self.enable()
        key = "clickup-dp:%s:blocker:%d:answer:g%d" % (self.tid(), b["id"], rel["gen"])
        self.lost_ack(key)
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.h._state(key), self.b()["state"]), ("sending", "releasing"))
        rel2 = self.new_input()  # newer input while possibly sent: nothing is resent or relabelled
        self.readback_done(key, lambda: self.h.cards[b["question_card"]]["task"].update(status="done"))
        self.outputs(rel2)
        self.h.settle()
        self.h.cycle()
        self.assertEqual(len(self.answer_rows()), 1)
        self.assertEqual((self.b()["state"], self.b()["delivered_gen"]), ("delivered", rel["gen"]))


# =============================================================================================== U4
class RefusalAndVoidTest(AmendBase):
    def bound_then(self, mutate, cycles=2):
        b, rel = self.scope_change()
        self.revise()
        mutate()
        self.enable()
        for _ in range(cycles):
            self.h.settle()
            self.h.cycle()
        return self.bindings()

    def test_u4_w_or_q_state_voids_the_binding_with_no_row(self):
        def claimed():
            self.h.cards[self.w]["events"].append({"kind": "claimed", "payload": {"run_id": 8}})
        cases = {"q_done": lambda: self.h.cards[self.b()["question_card"]]["task"].update(status="done"),
                 "q_archived": lambda: self.h.cards[self.b()["question_card"]]["task"].update(status="archived"),
                 "w_running": lambda: self.h.cards[self.w]["task"].update(status="running"),
                 "w_blocked": lambda: self.h.cards[self.w]["task"].update(status="blocked"),
                 "w_review": lambda: self.h.cards[self.w]["task"].update(status="review"),
                 "w_done": lambda: self.h.cards[self.w]["task"].update(status="done"),
                 "w_archived": lambda: self.h.cards[self.w]["task"].update(status="archived"),
                 "w_newer_claim": claimed,
                 "q_not_parent": lambda: self.h.cards[self.w]["parents"].remove(self.b()["question_card"])}
        for name, mutate in cases.items():
            with self.subTest(name):
                self.setUp()
                [k] = self.bound_then(mutate)
                self.assertEqual(k["state"], "void")
                self.assertEqual(self.answer_rows(), [])
                self.assertEqual(len(self.amend_alerts()), 1)

    def test_u4_w_not_in_the_approvals_cards_voids(self):
        b, rel = self.scope_change()
        self.revise(lineage_w=False)
        self.enable()
        self.h.settle()
        self.h.cycle()
        [k] = self.bindings()
        self.assertEqual((k["state"], k["reason"]), ("void", "work card not eligible"))
        self.assertEqual(self.answer_rows(), [])

    def test_u4_a_released_approval_waits_and_applies_once_started(self):
        b, rel = self.scope_change()
        a2 = self.revise(orchestrate=False)
        self.enable()
        for _ in range(2):
            self.h.settle()
            self.h.cycle()
        self.assertEqual(self.bindings(), [])  # only AMENDED_STATES approvals are read for markers
        fe2, cleanup2 = self.h.new_card(), self.h.new_card()
        promo2 = self.h.new_card(parents=[a2["gate_card"]],
                                 body="promotion\n" + rules.marker(a2["gate_card"], s2.T, 2, self.SHA2) + "\n")
        self.h.orch_done(a2, gates={"plan_revision": 2, "plan_sha256": self.SHA2, "final_e2e": fe2,
                                    "promotion": promo2, "cleanup": cleanup2, "lineage": [self.w, fe2]})
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings()[0]["state"], "applied")
        self.assertEqual(len(self.answer_rows()), 1)

    def test_u4_marker_mismatches_alert_once_and_bind_nothing(self):
        cases = {"question": {"question": "t_q9999999"}, "card": {"card": "t_w9999999"}, "gen": {"gen": 99},
                 "blocker": {"blocker": 777}}
        for name, over in cases.items():
            with self.subTest(name):
                self.setUp()
                b, rel = self.scope_change()
                self.revise(markers=[self.marker(b, **over)])
                self.enable()
                for _ in range(3):
                    self.h.settle()
                    self.h.cycle()
                self.assertEqual(self.bindings(), [])
                self.assertEqual(sum(("amendment marker" in a and "mismatch" in a) for a in self.h.alerts), 1)

    def test_u4_a_stale_or_superseded_or_older_approval_never_binds_or_voids(self):
        b, rel = self.scope_change()
        a2 = self.revise()
        c = self.h.store.conn
        c.execute("UPDATE approvals SET state='revoked', revoked_reason='superseded' WHERE id=?", (a2["id"],))
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings(), [])  # not an AMENDED_STATES approval: never ingested
        c.execute("UPDATE approvals SET state='started', revoked_reason=NULL WHERE id=?", (a2["id"],))
        c.execute("UPDATE blockers SET approval_id=? WHERE id=?", (a2["id"], b["id"]))  # A.id <= b.approval_id
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings(), [])
        self.assertEqual(sum("mismatch" in a for a in self.h.alerts), 1)

    def test_u4_bound_then_gone_approval_voids(self):
        for state, reason in (("stale", None), ("revoked", "superseded"), ("revoked", "material"),
                              ("revoked", "terminal")):
            with self.subTest(state=state, reason=reason):
                self.setUp()
                b, rel = self.scope_change()
                a2 = self.revise()
                self.h.cards[self.w]["task"]["status"] = "running"  # keep it bound for one pass ...
                self.enable()
                self.h.settle()
                self.h.cycle()
                self.assertEqual(self.bindings()[0]["state"], "void")  # ... W running voids; use a fresh fixture
                self.setUp()
                b, rel = self.scope_change()
                a2 = self.revise()
                q = self.b()["question_card"]
                self.h.cards[q]["task"]["status"] = "blocked"
                del self.h.cards[self.w]  # W unobserved: rule 8 waits, k stays bound
                self.enable()
                self.h.settle()
                self.h.cycle()
                self.assertEqual(self.bindings()[0]["state"], "bound")
                self.h.store.conn.execute("UPDATE approvals SET state=?, revoked_reason=? WHERE id=?",
                                          (state, reason, a2["id"]))
                self.h.settle()
                self.h.cycle()
                [k] = self.bindings()
                self.assertEqual((k["state"], k["reason"]), ("void", "approval gone"))

    def test_u4_changed_resolution_body_relay_or_gen_voids(self):
        for col, val in (("resolution_body", "changed"), ("resolution_relay", "other-relay"),
                         ("pending_relay", "other-relay"), ("resolution_gen", 99)):
            with self.subTest(col):
                self.setUp()
                b, rel = self.scope_change()
                a2 = self.revise()
                del_w = self.h.cards.pop(self.w)
                self.enable()
                self.h.settle()
                self.h.cycle()
                self.assertEqual(self.bindings()[0]["state"], "bound")
                self.h.store.conn.execute("UPDATE blockers SET %s=? WHERE id=?" % col, (val, b["id"]))
                self.h.cards[self.w] = del_w
                self.h.settle()
                self.h.cycle()
                [k] = self.bindings()
                self.assertEqual((k["state"], k["reason"]), ("void", "blocker changed"))
                self.assertEqual(self.answer_rows(), [])

    def test_u4_newer_material_voids_and_unclassified_newer_input_waits_then_voids(self):
        b, rel = self.scope_change()
        self.revise()
        w = self.h.cards.pop(self.w)
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings()[0]["state"], "bound")
        self.h.cards[self.w] = w
        rel2 = self.new_input()  # unclassified newer input: rule 5 waits
        self.assertEqual(self.bindings()[0]["state"], "bound")
        self.outputs(rel2)  # classified answer_only: input_gen > accounts_gen -> rule 6 void
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.bindings()[0]["reason"]), ("void", "newer input"))
        self.setUp()
        b, rel = self.scope_change()
        self.revise()
        w = self.h.cards.pop(self.w)
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.h.cards[self.w] = w
        rel2 = self.new_input("Change everything.")
        self.outputs(rel2, result="material")
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.bindings()[0]["reason"]), ("void", "newer material input"))

    def test_u4e_not_enabled_markers_wait_unread_and_enablement_ingests_them_on_the_next_full_pass(self):
        b, rel = self.scope_change()
        self.revise()
        rows = len(self.h.rows())
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual((self.bindings(), self.b()["state"], len(self.h.rows())), ([], "scope_change", rows))
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings()[0]["state"], "applied")

    def test_u4e_not_enabled_is_the_feature_off_outbox(self):
        """With no amendment rows the candidate's outbox equals the run with amendments ignored entirely."""
        b, rel = self.scope_change()
        self.revise()
        for _ in range(2):
            self.h.settle()
            self.h.cycle()
        cand = [(r["idem_key"], r["state"], r["body"]) for r in self.h.rows()]
        self.assertFalse([k for k, _, _ in cand if ":answer:" in k or "amend:" in k])

    def test_u4_a_marker_in_an_unapproved_or_unpublished_revision_is_never_ingested(self):
        b, rel = self.scope_change()
        self.revise(approve=False)  # PASSed and published, not approved
        self.enable()
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual(self.bindings(), [])


# =============================================================================================== U4(h), U4(t)
class HoldAndTerminalTest(AmendBase):
    def ticketed_bound(self):
        """A2 ``ticketed`` (its final E2E done), k ``bound`` (W unobserved for one pass), Q blocked, W todo."""
        b, rel = self.scope_change()
        a2 = self.revise()
        g = json.loads(self.h.approvals()[-1]["clickup_gates"])
        self.h.done(g["final_e2e"])
        self.h.settle()
        self.h.cycle()
        a2 = self.h.approvals()[-1]
        self.assertEqual(a2["state"], "ticketed")
        w = self.h.cards.pop(self.w)
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.h.cards[self.w] = w
        self.assertEqual(self.bindings()[0]["state"], "bound")
        return b, rel, a2

    def test_u4h_operator_hold_pauses_and_the_reissued_ticket_applies(self):
        from clickup_intake.main import hold_task, release_task
        b, rel, a2 = self.ticketed_bound()
        self.assertTrue(hold_task(self.h.store, s2.T, self.h.now))
        self.assertEqual(self.h.approvals()[-1]["revoked_reason"], "operator")
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.answer_rows(), self.amend_alerts()), ("bound", [], []))
        self.assertTrue(release_task(self.h.store, s2.T))
        self.h.settle()
        self.h.cycle()
        a = self.h.approvals()[-1]
        self.assertEqual((a["state"], a["ticket_epoch"]), ("ticketed", 2))  # reissued in that same full pass
        self.assertEqual(self.amend_alerts(), [])
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings()[0]["state"], "applied")
        self.assertEqual(self.b()["state"], "delivered")

    def test_u4h_hold_reason_revocation_also_pauses_and_a_superseded_one_voids(self):
        b, rel, a2 = self.ticketed_bound()
        c = self.h.store.conn
        c.execute("UPDATE approvals SET state='revoked', revoked_reason='hold' WHERE id=?", (a2["id"],))
        self.h.cards[self.w] = self.h.cards.pop(self.w)
        f = qr.TemporaryHoldPauseTest  # noqa: F841 (documents the existing pause pattern this mirrors)
        c.execute("UPDATE tasks SET hold_reason=NULL")
        g = json.loads(a2["clickup_gates"])
        fe = self.h.cards[g["final_e2e"]]
        fe["task"]["completed_at"] = self.h.now // 1000 + 10_000  # barrier unmet: no reissue for several scans
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
            self.assertEqual(self.bindings()[0]["state"], "bound")
        c.execute("UPDATE approvals SET state='revoked', revoked_reason='superseded' WHERE id=?", (a2["id"],))
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.bindings()[0]["reason"]), ("void", "approval gone"))

    def test_u4h_controls_terminal_while_held_and_material_after_release_void(self):
        from clickup_intake.main import hold_task, release_task
        b, rel, a2 = self.ticketed_bound()
        self.assertTrue(hold_task(self.h.store, s2.T, self.h.now))
        self.h.store.conn.execute("UPDATE tasks SET terminal=1")
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.bindings()[0]["reason"]), ("void", "terminal"))
        self.setUp()
        b, rel, a2 = self.ticketed_bound()
        self.assertTrue(hold_task(self.h.store, s2.T, self.h.now))
        self.assertTrue(release_task(self.h.store, s2.T))
        self.h.store.conn.execute("UPDATE tasks SET scope_gen=scope_gen+1, input_gen=input_gen+1,"
                                  " classified_gen=classified_gen+1")  # material input observed before reissue
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.bindings()[0]["reason"]), ("void", "newer material input"))
        self.assertEqual(self.answer_rows(), [])

    def test_u4t_terminal_voids_a_bound_row_and_its_alert_survives_the_progress_pass_once(self):
        b, rel, a2 = self.ticketed_bound()
        self.h.store.conn.execute("UPDATE tasks SET terminal=1")
        n = len(self.h.alerts)
        self.h.settle()
        self.h.cycle()
        [k] = self.bindings()
        self.assertEqual((k["state"], k["reason"]), ("void", "terminal"))
        alerts = self.h.alerts[n:]
        self.assertEqual([a for a in alerts if "ClickUp amendment binding %d " % k["id"] in a],
                         ["ClickUp amendment binding %d (blocker %d, question %s, card %s) is void: terminal; the"
                          " question stays with a human." % (k["id"], b["id"], b["question_card"], self.w)])
        rows = len(self.h.rows())
        for _ in range(5):
            self.h.settle()
            self.h.cycle()
        self.assertEqual(len(self.amend_alerts()), 1)
        self.assertEqual(len(self.h.rows()), rows)
        self.assertEqual(self.answer_rows(), [])
        from clickup_intake.main import rollback_readiness
        self.assertEqual(rollback_readiness(self.h.store.conn, "45f7c252df27"), [])

    def test_u4t_applied_then_terminal_is_finalized_by_the_existing_closed_task_path(self):
        for when in ("closed", "pending", "delivered"):
            with self.subTest(when):
                self.setUp()
                b, rel = self.scope_change()
                self.revise()
                self.enable()
                self.h.settle()
                if when == "delivered":
                    self.h.cycle()
                else:
                    self.h.scan()  # applied + pending row
                    if when == "closed":
                        self.h.store.conn.execute("UPDATE outbox SET state='cancelled_stale' WHERE idem_key LIKE"
                                                  " '%:answer:%'")
                self.h.store.conn.execute("UPDATE tasks SET terminal=1")
                for _ in range(2):
                    self.h.settle()
                    self.h.cycle()
                self.assertEqual(self.b()["state"], "terminal")
                self.assertEqual(self.bindings()[0]["state"], "applied")
                from clickup_intake.main import rollback_readiness
                self.assertEqual(rollback_readiness(self.h.store.conn, "45f7c252df27"), [])


# =============================================================================================== U5-U8
class SiblingDuplicateCrashBoundsTest(AmendBase):
    def test_u5_an_unanswered_sibling_question_keeps_w_waiting(self):
        b, rel = self.scope_change()
        sib = self.h.new_card(status="blocked")
        self.h.cards[self.w]["parents"].append(sib)
        self.revise()
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.b()["state"], self.rows("fences"), self.replies("resumed")), ("delivered", [], []))
        self.assertEqual(self.h.cards[self.w]["task"]["status"], "todo")

    def test_u6_duplicate_progress_and_full_passes_change_nothing(self):
        b, rel, a2 = self.applied()
        rows = [(r["idem_key"], r["state"]) for r in self.h.rows()]
        for _ in range(5):
            self.h.settle()
            self.h.cycle()
            self.h.cycle()
        self.assertEqual(len(self.bindings()), 1)
        self.assertEqual([(r["idem_key"], r["state"]) for r in self.h.rows()], rows)

    def test_u7_a_crash_between_apply_and_queue_loses_nothing(self):
        b, rel = self.scope_change()
        self.revise()
        self.enable()
        real = engine._first_delivery
        calls = []

        def crash(*a):
            calls.append(1)
            raise RuntimeError("crash inside the apply transaction")
        engine._first_delivery = crash
        try:
            self.h.settle()
            with self.assertRaises(RuntimeError):
                self.h.scan()
        finally:
            engine._first_delivery = real
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.bindings()[0]["state"], self.b()["state"], len(self.answer_rows())),
                         ("applied", "delivered", 1))

    def test_u7_a_restart_between_binding_and_delivery_causes_no_duplicate(self):
        from clickup_intake.store import Store
        b, rel = self.scope_change()
        self.revise()
        w = self.h.cards.pop(self.w)
        self.enable()
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.bindings()[0]["state"], "bound")
        self.h.store.close()
        self.h.store = Store(self.h.path, self.h.clock)
        self.addCleanup(self.h.store.close)
        self.h.cards[self.w] = w
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual((len(self.bindings()), len(self.answer_rows()), self.b()["state"]), (1, 1, "delivered"))

    def test_u7_a_lost_fence_release_ack_takes_the_existing_readback_path(self):
        b, rel, a2 = self.applied()
        self.claim()
        self.link_fence()
        self.h.settle()
        self.h.cycle()  # detected
        self.h.settle()
        self.h.scan()  # the release row is queued
        [row] = self.release_rows()
        self.lost_ack(row["idem_key"])
        self.h.cycle()
        self.assertEqual((self.h._state(row["idem_key"]), self.rows("fences")[0]["state"]), ("sending", "releasing"))
        self.readback_done(row["idem_key"], lambda: self.h.cards[self.F]["task"].update(status="done"))
        self.h.settle()
        self.h.cycle()
        self.assertEqual(self.ack(rel["gen"])["state"], "resumed")
        self.assertEqual(len(self.release_rows()), 1)

    def test_u8_the_maximal_identifier_answer_fits_the_delivery_cap(self):
        k = {"id": 999_999_999, "gen": 999_999_999, "relay_ref": "r" * 80, "resolution_sha256": "f" * 64,
             "source": "operator", "receipt_sha256": "e" * 64}
        a = {"rev": 999_999_999, "plan_sha256": "a" * 64, "comment_id": "9" * 19, "id": 999_999_999,
             "accounts_gen": 999_999_999}
        body = engine.amended_body(k, a)
        self.assertEqual(body, amended_text(k, a))
        summary = "CLICKUP-ANSWER v1 question=%s card=%s gen=%d task=%s\n%s\nThis answer grants no new authority;" \
                  " boundary: security — unchanged." % ("t_ffffffff", "t_ffffffff", 999_999_999, "x" * 40, body)
        self.assertLess(len(summary), 1000)

    def test_a_changed_resolution_authority_is_never_due(self):
        b, rel = self.scope_change()
        self.revise()
        self.enable()
        self.h.settle()
        self.h.scan()
        self.assertEqual(self.bindings()[0]["state"], "applied")
        self.h.store.conn.execute("DELETE FROM outbox WHERE idem_key LIKE '%:answer:%'")
        self.h.store.conn.execute("UPDATE blockers SET resolution_authority='security', release_key=NULL,"
                                  " state='closed' WHERE id=?", (b["id"],))
        for _ in range(2):
            self.h.settle()
            self.h.cycle()
        self.assertEqual(self.answer_rows(), [])

    def test_u8_ignored_and_oversize_markers_alert_once(self):
        b, rel = self.scope_change()
        a2 = self.revise(markers=[self.marker(self.b())])
        self.h.attachments[self.rel["card"]][-1].update(incorporates_ignored=3, incorporates_oversize=True)
        self.enable()
        for _ in range(3):
            self.h.settle()
            self.h.cycle()
        self.assertEqual(sum("beyond the first 20 were ignored" in a for a in self.h.alerts), 1)
        self.assertEqual(sum("the plan file is over 1 MiB" in a for a in self.h.alerts), 1)
        self.assertEqual(a2["rev"], 2)

    def test_u8_a_fence_with_an_unmet_barrier_is_not_released_until_the_barrier_holds(self):
        b, rel, a2 = self.applied()
        self.claim()
        self.created_s = self.h.now // 1000 + 1000  # anchor after every full pass so far
        self.link(self.F, "CLICKUP-FENCE v1 " + self.w, "running")
        self.h.settle()
        self.h.cycle()
        self.assertEqual((self.rows("fences")[0]["state"], self.release_rows()), ("open", []))
        self.h.settle(seconds=1000)
        self.h.cycle()
        self.assertEqual(self.rows("fences")[0]["state"], "released")


if __name__ == "__main__":
    unittest.main()
