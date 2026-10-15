"""SCHEMA-F1: neutral ClickUp editor metadata on a plain closure or approval.

The observed representation has an ordinary text part followed by a newline part
with a ``block-id`` attribute. Only that neutral key is calibrated; parser code,
quote/code/unknown-shape rejection, author checks and revision controls are unchanged.

This regression compares the historical empty allowlist with ["block-id"]. Task,
comment and account identifiers from the source observation are deliberately omitted.
The block value is synthetic; the signature describes field names/types, not contents.
"""

import os
import unittest

from clickup_intake.parse import PLAIN, QUOTED, UNKNOWN, parse_comment
from clickup_intake.rules import command_of
from test_s4_composition import PLAN_R1, APPROVER_A, REQ, TMP_ROOT, Lifecycle, World, classify, operator_lines, out

BLOCK_ID = "block-0f3a9c2e7d"  # synthetic fixture value; no observed content retained
OBSERVED_SIG = "b407b372d97fb68daa37fa6d6e0470a1c55de94e2bca80e30a0c92ccd2f7add6"
BASELINE_ALLOWLIST = []            # historical pre-calibration configuration (fail closed)
CORRECTED_ALLOWLIST = ["block-id"]  # SCHEMA-F1 config correction
OTHER = 555777  # a human who is neither the requester nor a named approver
CLOSE_B, CLOSE_T, APPROVE_R1 = "Business requirements closed", "Technical requirements closed", \
    "Approve plan r1 for development"


def observed(text, **newline_attrs):
    """The observed ClickUp representation of a one-line plain comment, plus optional extra newline attributes."""
    return ([{"text": text, "attributes": {}}, {"text": "\n", "attributes": dict({"block-id": BLOCK_ID},
                                                                               **newline_attrs)}], text + "\n")


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


class ObservedShapeParseTest(unittest.TestCase):
    def test_baseline_empty_allowlist_rejects_the_observed_closure_with_the_shape_signature(self):
        parts, ct = observed(CLOSE_B)
        p = parse_comment(parts, ct, BASELINE_ALLOWLIST)
        self.assertEqual((p.shape, p.shape_sig), (UNKNOWN, OBSERVED_SIG))
        self.assertEqual(command_of(parts, ct, BASELINE_ALLOWLIST)[0], "unknown")

    def test_block_id_allowlisted_observed_closures_and_approval_are_plain_commands(self):
        for text, kind, rev in ((CLOSE_B, "close_business", None), (CLOSE_T, "close_technical", None),
                                (APPROVE_R1, "approve", 1)):
            parts, ct = observed(text)
            k, n, p = command_of(parts, ct, CORRECTED_ALLOWLIST)
            self.assertEqual((p.shape, k, n, p.shape_sig), (PLAIN, kind, rev, OBSERVED_SIG), text)

    def test_quote_and_code_markers_with_block_id_stay_quoted(self):
        cases = [observed(APPROVE_R1, blockquote={}), observed(APPROVE_R1, **{"code-block": "plain"}),
                 observed(APPROVE_R1, quote=True),
                 ([{"text": APPROVE_R1, "attributes": {"code": True}}, observed("")[0][1]], APPROVE_R1 + "\n"),
                 observed("> " + APPROVE_R1)]
        for parts, ct in cases:
            k, _, p = command_of(parts, ct, CORRECTED_ALLOWLIST)
            self.assertEqual((p.shape, k), (QUOTED, "quoted"), parts)

    def test_unknown_parts_attributes_mismatch_and_malformed_input_with_block_id_stay_unknown(self):
        parts, ct = observed(APPROVE_R1)
        cases = [
            observed(APPROVE_R1, bold=True),                                     # unknown attribute
            (parts + [{"type": "emoticon", "emoticon": {"code": "1f600"}}], ct),  # unknown part type
            (parts, "Approve plan r2 for development\n"),                        # comment_text disagrees
            ([{"text": APPROVE_R1, "attributes": {"block-id": BLOCK_ID, "Block-Id": "x"}}], ct),  # other key
            ([{"text": APPROVE_R1, "attributes": "block-id"}], ct),            # non-dict attributes
            ([{"text": 5, "attributes": {"block-id": BLOCK_ID}}], ct),          # non-str text
            ([{"attributes": {"block-id": BLOCK_ID}}], ct),                     # text missing
        ]
        for parts_, ct_ in cases:
            k, _, p = command_of(parts_, ct_, CORRECTED_ALLOWLIST)
            self.assertEqual((p.shape, k), (UNKNOWN, "unknown"), parts_)
        self.assertEqual(parse_comment(tuple(parts), ct, CORRECTED_ALLOWLIST).shape, UNKNOWN)
        self.assertEqual(parse_comment(parts, None, CORRECTED_ALLOWLIST).shape, UNKNOWN)


# ------------------------------------------------------------------------- composed (real main, fakes)
def results(w, tid):
    return {r["id"]: r["command_result"] for r in w.q("SELECT id, command_result FROM items WHERE task=?", tid)}


def say_observed(life, user, text, reply_to=None, **newline_attrs):
    parts, ct = observed(text, **newline_attrs)
    return life.say(user, ct, parts=parts, reply_to=reply_to)


def question_post(w, life, text):
    """The bot's question comment; the live closure was a reply in such a thread."""
    return [c for c in w.bot_comments(life.tid) if text in c["comment_text"]][0]["id"]


def notices(w, tid):
    return [c["comment_text"] for c in w.bot_comments(tid) if c["comment_text"].startswith("Not accepted")]


class ObservedShapeCompositionTest(unittest.TestCase):
    """Real ``main`` scans with config.json written per case, FakeClickUp, fake board and fake clock."""

    def to_awaiting_business(self, allowlist):
        w = World(self, plain_attr_allowlist=allowlist)
        life = Lifecycle(w)
        life.pickup_to_business()
        return w, life

    def test_observed_business_closure_is_rejected_under_the_baseline_config(self):
        w, life = self.to_awaiting_business(BASELINE_ALLOWLIST)
        life.say(REQ, "All panel users.")
        cid = say_observed(life, REQ, CLOSE_B, reply_to=question_post(w, life, "Which users"))
        w.step(lambda: results(w, life.tid).get(cid) == "rejected:unknown" and notices(w, life.tid))
        self.assertEqual(life.phase(), "awaiting_business")
        self.assertEqual(len(notices(w, life.tid)), 1)
        self.assertTrue(any(OBSERVED_SIG[:12] in x for x in operator_lines(w)), operator_lines(w))

    def test_observed_approval_is_rejected_under_the_baseline_config(self):
        w, life = self.to_awaiting_business(BASELINE_ALLOWLIST)
        life.business_to_technical()  # attribute-free closures (existing path)
        life.technical_to_published()
        cid = say_observed(life, APPROVER_A, APPROVE_R1)
        w.step(lambda: results(w, life.tid).get(cid) == "rejected:unknown" and notices(w, life.tid))
        self.assertEqual(w.q("SELECT * FROM approvals WHERE task=?", life.tid), [])
        self.assertIsNone(life.orch())

    def test_observed_closures_and_approval_pass_every_control_with_block_id_allowlisted(self):
        w, life = self.to_awaiting_business(CORRECTED_ALLOWLIST)
        tid, b = life.tid, w.board
        life.say(REQ, "All panel users.")
        early_approval = say_observed(life, APPROVER_A, APPROVE_R1)    # wrong stage -> context
        early_technical = say_observed(life, APPROVER_A, CLOSE_T)      # wrong stage -> context
        business = say_observed(life, REQ, CLOSE_B, reply_to=question_post(w, life, "Which users"))  # live: reply

        def tech_q(card, ref, gen):
            b.park(card, out("technical_questions", ref, "Should it follow the OS theme?"), classify(ref, gen))
        w.step(lambda: life.phase() == "awaiting_technical" and life.bot_q("OS theme"), world=life.planner(tech_q))
        r = results(w, tid)
        self.assertEqual((r[early_approval], r[early_technical], r[business]),
                         ("context", "context", "closure_business"))

        life.say(APPROVER_A, "Yes, follow the OS theme.")
        wrong_actor = say_observed(life, REQ, CLOSE_T)            # requester is not a named approver
        technical = say_observed(life, APPROVER_A, CLOSE_T)

        def review(card, ref, gen):
            b.worker_comment(card, "planner", classify(ref, gen))
            life.plan_sha = b.request_review(card, tid, 1, PLAN_R1, gen)
            b.claim(card, "plan-reviewer")
            b.finish(card, "plan-reviewer")
        w.step(lambda: life.phase() == "awaiting_approval" and life.bot_q("Plan r1, SHA-256"),
               world=life.planner(review))
        r = results(w, tid)
        self.assertEqual((r[wrong_actor], r[technical]), ("rejected:author", "closure_technical"))

        pub_ms = int([c for c in w.bot_comments(tid) if "Plan r1, SHA-256" in c["comment_text"]][0]["date"])
        edited = say_observed(life, APPROVER_A, APPROVE_R1)
        w.clock.advance(1)
        w.run()  # first sight as an approval candidate, inside the quiet window
        self.assertEqual(results(w, tid)[edited], "cand:approve:1")
        parts, ct = observed(APPROVE_R1 + " now")
        w.cu._find(edited).update(comment=parts, comment_text=ct)
        into_command = say_observed(life, APPROVER_A, "Looks good")
        w.clock.advance(1)
        w.run()  # first sight as a non-command
        parts, ct = observed(APPROVE_R1)
        w.cu._find(into_command).update(comment=parts, comment_text=ct)
        cases = {
            "author": say_observed(life, OTHER, APPROVE_R1),
            "requester": say_observed(life, REQ, APPROVE_R1),
            "stale_rev": say_observed(life, APPROVER_A, "Approve plan r2 for development"),
            "blockquote": say_observed(life, APPROVER_A, APPROVE_R1, blockquote={}),
            "code_block": say_observed(life, APPROVER_A, APPROVE_R1, **{"code-block": "plain"}),
            "bold": say_observed(life, APPROVER_A, APPROVE_R1, bold=True),
            "old_date": life.cu.add_comment(tid, APPROVER_A, observed(APPROVE_R1)[1], parts=observed(APPROVE_R1)[0],
                                            date_ms=pub_ms - 1),
        }
        valid = say_observed(life, APPROVER_A, APPROVE_R1)
        w.step(lambda: life.orch() and b.cards[life.orch()]["task"]["status"] == "ready")
        r = results(w, tid)
        self.assertEqual({k: r[v] for k, v in cases.items()},
                         {"author": "rejected:author", "requester": "rejected:author", "stale_rev": "rejected:stale",
                          "blockquote": "context", "code_block": "context", "bold": "rejected:unknown",
                          "old_date": "context"})
        self.assertEqual((r[edited], r[into_command]), ("rejected:edited", "none"))
        apps = w.q("SELECT comment_id, author_id, rev, state FROM approvals WHERE task=?", tid)
        self.assertEqual([(a["comment_id"], a["author_id"], a["rev"]) for a in apps], [(valid, APPROVER_A, 1)])
        self.assertEqual(r[valid], "approved:%d" % w.q("SELECT id FROM approvals WHERE task=?", tid)[0]["id"])
        self.assertEqual(len(b.by_key("clickup-dp:%s:orch:" % tid)), 1)
        self.assertEqual(len(b.by_key("clickup-dp:%s:gate:" % tid)), 1)
        # Notices: wrong-actor technical closure, two wrong-author approvals, stale, bold-unknown and edited.
        self.assertEqual(len(notices(w, tid)), 6, notices(w, tid))
        # The neutral observed shape raises no drift alert; only the bold+block-id shape does.
        drift = [x for x in operator_lines(w) if "schema drift" in x]
        self.assertEqual(len(drift), 1, drift)
        self.assertNotIn(OBSERVED_SIG[:12], drift[0])


if __name__ == "__main__":
    unittest.main()
