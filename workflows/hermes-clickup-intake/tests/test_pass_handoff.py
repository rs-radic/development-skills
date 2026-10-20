"""Quick-fix regression: recover a bound reviewer receipt, never infer a PASS."""
import copy
import unittest

from clickup_intake import rules
from test_s2_workflow import H, T, SHA1


class PassHandoffTest(unittest.TestCase):
    def handoff(self):
        h = H(self)
        card = h.picked_up()
        h.run_review(card, 4, SHA1)
        accounts_gen = h.cards[card]["runs"][-1]["metadata"]["accounts_gen"]
        h.cards[card]["runs"][-1]["metadata"] = {
            "plan_revision": "r4", "attachment": "clickup-plan-r4.md", "sha256": SHA1,
        }
        h.pass_review(card)
        h.cards[card]["runs"][-1]["metadata"] = {
            "review_outcome": "PASS", "reviewed_sha256": SHA1, "clickup_task": T,
            "plan_revision": 4, "plan_attachment": "clickup-plan-r4.md",
            "plan_sha256": SHA1, "accounts_gen": accounts_gen,
        }
        return h, card

    def test_bound_reviewer_receipt_recovers_publication_once_without_execution(self):
        h, card = self.handoff()
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["published_rev"], 4)
        self.assertEqual(h.task()["published_sha256"], SHA1)
        self.assertEqual(h.task()["phase"], "awaiting_approval")
        self.assertEqual(h.approvals(), [])
        publication = [r["idem_key"] for r in h.rows() if ":pub:r4:" in r["idem_key"]]
        self.assertEqual(len(publication), 3)
        h.cycle()
        self.assertEqual(publication, [r["idem_key"] for r in h.rows() if ":pub:r4:" in r["idem_key"]])

    def test_unbound_or_invalid_review_cannot_recover_a_malformed_request(self):
        h, card = self.handoff()
        original = h.cards[card]
        def check(label, mutate):
            shown = copy.deepcopy(original)
            mutate(shown)
            with self.subTest(label=label):
                self.assertIsNone(rules.pass_evidence(shown, h.attachments[card], T))
        check("not done", lambda s: s["task"].update(status="review"))
        check("wrong reviewer", lambda s: s["runs"][-1].update(profile="planner"))
        check("review not completed", lambda s: s["runs"][-1].update(outcome="changes_requested"))
        check("no explicit pass", lambda s: s["runs"][-1]["metadata"].pop("review_outcome"))
        check("review rejected", lambda s: s["runs"][-1]["metadata"].update(review_outcome="BLOCK"))
        check("wrong task", lambda s: s["runs"][-1]["metadata"].update(clickup_task="another-task"))
        check("wrong reviewed hash", lambda s: s["runs"][-1]["metadata"].update(reviewed_sha256="b" * 64))
        check("wrong attachment", lambda s: s["runs"][-1]["metadata"].update(plan_attachment="other.md"))
        check("string revision", lambda s: s["runs"][-1]["metadata"].update(plan_revision="4"))
        check("bool revision", lambda s: s["runs"][-1]["metadata"].update(plan_revision=True))
        check("missing generation", lambda s: s["runs"][-1]["metadata"].pop("accounts_gen"))
        check("string generation", lambda s: s["runs"][-1]["metadata"].update(accounts_gen="1"))
        check("negative generation", lambda s: s["runs"][-1]["metadata"].update(accounts_gen=-1))
        check("request hash differs", lambda s: s["runs"][-2]["metadata"].update(sha256="b" * 64))
        check("request name differs", lambda s: s["runs"][-2]["metadata"].update(attachment="older.md"))
        check("request revision differs", lambda s: s["runs"][-2]["metadata"].update(plan_revision="r5"))
        check("request task contradicts", lambda s: s["runs"][-2]["metadata"].update(clickup_task="another-task"))
        check("request generation contradicts", lambda s: s["runs"][-2]["metadata"].update(accounts_gen=99))
        check("canonical hash contradicts alias", lambda s: s["runs"][-2]["metadata"].update(plan_sha256="b" * 64))
        check("canonical name contradicts alias", lambda s: s["runs"][-2]["metadata"].update(plan_attachment="older.md"))
        check("no request artifact binding", lambda s: s["runs"][-2]["metadata"].pop("sha256"))
        check("request author not planner", lambda s: s["runs"][-2].update(profile="implementor"))
        with self.subTest(label="missing stored attachment"):
            self.assertIsNone(rules.pass_evidence(original, [], T))

    def test_current_generation_checks_still_prevent_stale_publication(self):
        h, card = self.handoff()
        h.describe()
        h.settle()
        h.cycle()
        self.assertIsNone(h.task()["published_rev"])
        self.assertEqual([r for r in h.rows() if ":pub:r4:" in r["idem_key"]], [])


if __name__ == "__main__":
    unittest.main()
