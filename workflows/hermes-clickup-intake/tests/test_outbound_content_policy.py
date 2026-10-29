"""Owner-authorized retirement of send-time content filtering (Discord 9900000000000052).

All values below are synthetic examples, never deployed credentials. Transport, destination,
input-generation, review and approval checks are not part of the removed content policy.
"""
import hashlib
import unittest
from unittest import mock

from clickup_intake import main, render
import test_s4_composition as composition


class OutboundContentPolicyTest(unittest.TestCase):
    def test_content_is_not_rejected_by_the_retired_filter(self):
        examples = (
            "https://E2E_USER_MARKER:E2E_PASS_MARKER@127.0.0.1:8088/test",
            "pk_SYNTHETIC_EXAMPLE_1234567890",
            "-----BEGIN PRIVATE KEY-----\nSYNTHETIC EXAMPLE\n-----END PRIVATE KEY-----",
            "Server=example.invalid;Password=Synthetic123!;",
            "Password: Synthetic123!",
            r"example hermes\secrets\example.dpapi",
            "synthetic-in-memory-secret",
        )
        for text in examples:
            with self.subTest(example=text):
                self.assertIsNone(render.check_outbound(text, ("synthetic-in-memory-secret",)))
                post = render.prepare_clickup_post(text, "example-key", task_id="example-task",
                                                  secrets=("synthetic-in-memory-secret",))
                self.assertTrue(post.ok)
                self.assertFalse(post.hold)
                self.assertIsNone(post.alert)
                self.assertEqual(post.comment, text + "\n\n" + render.ref_tag("example-key"))

                # Exercise the other real send-time caller: a reviewed plan attachment.
                data = ("## ClickUp summary\nA synthetic test plan.\n\n" + text).encode("utf-8")
                digest = hashlib.sha256(data).hexdigest()
                staged = {}
                glue = object.__new__(main.Glue)
                glue.secrets = ("synthetic-in-memory-secret",)
                glue.card_file = lambda card, name, sha: data if sha == digest else None
                glue.stage = lambda key, name, payload: staged.setdefault(name, payload) and "example-staged.md"
                result = glue.build_clickup(
                    {"idem_key": "example-key", "kind": "cu_attach"},
                    {"task": "example-task", "card": "t_example", "attachment": "plan.md",
                     "sha256": digest, "filename": "plan-r2-example.md"},
                )
                self.assertIsInstance(result, list)
                self.assertEqual(result[0][1:3], ("clickup_attachment", "example-task"))
                self.assertEqual(list(staged.values()), [data])

    def test_planner_example_is_delivered_without_feedback_or_duplicate(self):
        world = composition.World(self)
        life = composition.Lifecycle(world)
        question = "Should the test reject https://E2E_USER_MARKER:E2E_PASS_MARKER@example.invalid/test?"

        def planner(card, ref, gen):
            world.board.park(card, composition.out("business_questions", ref, question),
                             composition.classify(ref, gen))

        world.step(lambda: life.bot_q(question) and world.statuses_written(), world=life.planner(planner))
        self.assertIsNone(world.task(life.tid)["hold_reason"])
        self.assertEqual(life.planner_turns, 1)
        self.assertEqual(composition.feedback_comments(world.board, life.req_cards()[0]), [])
        for _ in range(3):
            world.clock.advance(61)
            world.run()
        self.assertEqual(sum(question in c["comment_text"] for c in world.bot_comments(life.tid)), 1)
        self.assertIsNone(life.orch())

    def test_reviewed_example_plan_publishes_but_still_requires_human_approval(self):
        world = composition.World(self)
        life = composition.Lifecycle(world)
        plan = ("## ClickUp summary\nReject credential-style URLs in the product under test.\n\n"
                "## Plan\nUse https://E2E_USER_MARKER:E2E_PASS_MARKER@example.invalid/test.\n")
        with mock.patch.object(composition, "PLAN_R1", plan):
            life.pickup_to_business()
            life.business_to_technical()
            life.technical_to_published()
        self.assertEqual(world.task(life.tid)["published_sha256"], hashlib.sha256(plan.encode()).hexdigest())
        self.assertEqual(life.phase(), "awaiting_approval")
        self.assertIsNone(world.task(life.tid)["hold_reason"])
        self.assertIsNone(life.orch())
        self.assertIsNone(life.gate())
        self.assertEqual(composition.feedback_comments(world.board, life.req_cards()[0]), [])
