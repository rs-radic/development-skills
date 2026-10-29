"""Workflow repair r3 §4.1 / §7 U10 (Panel instance): the intake templates through the actual renderer."""

import unittest

from clickup_intake import render

MARKER_RULE = ("When a later plan revision fully settles a blocker whose resolution generation was classified"
               " `material`, add to that revision's plan file one line per settled blocker, exactly"
               " `CLICKUP-INCORPORATES v1 blocker=<id> question=<question card> card=<work card> gen=<that resolution's"
               " gen>` on its own line without formatting. After the revision is approved and its orchestration"
               " finishes, the integration answers that question itself, bound to the approved revision; a later"
               " answer still goes through normal triage. Omit the line when the revision does not settle that"
               " decision; the question then stays with a human. Never complete a question card yourself.")
REQUEST_SENTENCE = ("Treat freshly observed `dev` as a moving integration target: do not write a clause that stops work"
                    " or requires another revision merely because another approved lane later adds a migration; plan"
                    " the existing reconciliation instead (verify that lane's accepted handback names the served SHA,"
                    " required runtime DLL provenance and the migration's read-only postconditions, and keep verifying"
                    " those postconditions wherever the target contains it; never rerun its SQL). Unknown schema"
                    " state, destructive operations and unapproved behavior still stop on the same card.")
ORCH_SENTENCE = ("Treat freshly observed `dev` as a moving integration target: reconcile another approved lane's"
                 " promoted change through the existing integrator and SAME cards (verified handback, runtime"
                 " provenance and postconditions; no SQL replay); do not add a frozen-baseline stop.")


def _orch():
    return render.orchestrator_card_body("86task1", 2, "clickup-plan-r2.md", 10, "a" * 64, 3, "t_gate0001",
                                         {"author_name": "Approver One", "author_id": "1", "comment_id": "9",
                                          "date_ms": 0}, True)


class CurrentDevV2Test(unittest.TestCase):
    def test_blocker_triage_rules_carry_the_incorporates_marker_rule(self):
        self.assertIn(MARKER_RULE, render.blocker_triage_rules("r1"))
        self.assertIn(MARKER_RULE, render.request_card_body("86task1", 2, 0, 2))

    def test_request_card_is_current_dev_v2_with_the_moving_target_sentence(self):
        body = render.request_card_body("86task1", 2, 0, 2)
        [line] = [ln for ln in body.splitlines() if "current-dev policy" in ln]
        self.assertTrue(line.startswith("- Configured project current-dev policy v2: "))
        self.assertTrue(line.endswith(" " + REQUEST_SENTENCE))
        self.assertNotIn("current-dev policy v1", body)

    def test_orchestrator_requirement_1_is_current_dev_v2_with_the_moving_target_sentence(self):
        body = _orch()
        [line] = [ln for ln in body.splitlines() if ln.startswith("1. ")]
        self.assertIn("Configured project current-dev policy v2: ", line)
        self.assertTrue(line.endswith(" " + ORCH_SENTENCE))
        self.assertNotIn("current-dev policy v1", body)


if __name__ == "__main__":
    unittest.main()
