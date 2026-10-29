"""Explicit same-publication retry approved in Discord 9900000000000053."""
import json
import unittest
from unittest import mock

from clickup_intake import render
from clickup_intake.store import Store
import test_s4_composition as composition


class FilteredPublicationRetryTest(unittest.TestCase):
    def setUp(self):
        self.world = w = composition.World(self)
        self.life = life = composition.Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.say(composition.APPROVER_A, "Yes, follow the OS theme.")
        life.say(composition.APPROVER_A, "Technical requirements closed")
        self.plan = ("## ClickUp summary\nA reviewed test plan.\n\n## Plan\n"
                     "Reject https://TEST_USER:TEST_PASS@example.invalid/test.\n")

        def planner(card, ref, gen):
            w.board.worker_comment(card, "planner", composition.classify(ref, gen))
            life.plan_sha = w.board.request_review(card, life.tid, 1, self.plan, gen)
            w.board.claim(card, "plan-reviewer")
            w.board.finish(card, "plan-reviewer")

        # Use the actual producer/executor with its retired policy to create the observed pre-state.
        with mock.patch.object(render, "check_outbound", side_effect=lambda text, secrets=():
                               "credential_url" if "TEST_USER:TEST_PASS@" in text else None):
            w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", world=life.planner(planner))
            w.run()
        self.rows = w.q("SELECT * FROM outbox WHERE task=? AND idem_key LIKE ? ORDER BY idem_key",
                        life.tid, "clickup-dp:%:pub:%")
        self.assertEqual(len(self.rows), 3)
        self.root = next(r["idem_key"] for r in self.rows if r["kind"] == "cu_attach")
        self.assertEqual([(r["state"], r["outcome"]) for r in self.rows],
                         [("failed", "FILTERED"), ("cancelled_stale", "DEPENDENCY"),
                          ("cancelled_stale", "DEPENDENCY")])
        self.assertTrue(all(r["attempts"] == 0 and r["first_send_ms"] is None for r in self.rows))

    def publication(self):
        return self.world.q("SELECT * FROM outbox WHERE task=? AND idem_key LIKE ? ORDER BY idem_key",
                            self.life.tid, "clickup-dp:%:pub:%")

    def test_explicit_retry_restores_same_never_sent_chain_and_publishes_once(self):
        w, life = self.world, self.life
        before_cards = set(w.board.cards)
        rc, output = w.main("outbox", "resend", self.root)
        self.assertEqual((rc, output), (0, ["resend done"]))
        resumed = self.publication()
        self.assertEqual([r["state"] for r in resumed], ["pending"] * 3)
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        mutable = {"state", "outcome"}
        self.assertEqual([{k: v for k, v in r.items() if k not in mutable} for r in resumed],
                         [{k: v for k, v in r.items() if k not in mutable} for r in self.rows])
        w.step(lambda: life.phase() == "awaiting_approval" and life.bot_q("Plan r1, SHA-256"))
        self.assertEqual(w.task(life.tid)["published_sha256"], life.plan_sha)
        self.assertEqual(set(w.board.cards), before_cards)
        self.assertIsNone(life.orch())
        count = len(w.bot_comments(life.tid))
        self.assertEqual(w.main("outbox", "resend", self.root)[0], 1)
        for _ in range(3):
            w.clock.advance(61)
            w.run()
        self.assertEqual(len(w.bot_comments(life.tid)), count)

    def test_existing_transport_mirror_blocks_rearming(self):
        store = Store(self.world.state_path, self.world.clock)
        try:
            mirror = dict(self.rows[0], idem_key=self.root + "#cu", kind="clickup_attachment")
            columns = list(mirror)
            with store.transaction() as conn:
                conn.execute("INSERT INTO outbox (" + ",".join(columns) + ") VALUES ("
                             + ",".join("?" for _ in columns) + ")", [mirror[k] for k in columns])
            before = [dict(r) for r in store.conn.execute("SELECT * FROM outbox ORDER BY idem_key")]
        finally:
            store.close()
        self.assertEqual(self.world.main("outbox", "resend", self.root)[0], 1)
        store = Store(self.world.state_path, self.world.clock)
        try:
            self.assertEqual([dict(r) for r in store.conn.execute("SELECT * FROM outbox ORDER BY idem_key")], before)
        finally:
            store.close()

    def test_unsafe_or_changed_chains_are_refused_without_partial_updates(self):
        w, life = self.world, self.life
        comment = self.root[:-len(":attach")] + ":comment"
        status = self.root[:-len(":attach")] + ":status"
        original_task = w.task(life.tid)
        cases = [
            ("attempted", "UPDATE outbox SET attempts=1 WHERE idem_key=?", (comment,)),
            ("abandoned", "UPDATE outbox SET outcome='ABANDONED' WHERE idem_key=?", (comment,)),
            ("live-child", "UPDATE outbox SET child_pid=4242 WHERE idem_key=?", (comment,)),
            ("delivered", "UPDATE outbox SET result_ref='existing-result' WHERE idem_key=?", (comment,)),
            ("foreign-dependency", "UPDATE outbox SET depends_on='[\"foreign\"]' WHERE idem_key=?", (comment,)),
            ("foreign-task", "UPDATE outbox SET task='foreign' WHERE idem_key=?", (comment,)),
            ("operator-hold", "UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (life.tid,)),
            ("new-generation", "UPDATE tasks SET input_gen=input_gen+1 WHERE clickup_task_id=?", (life.tid,)),
            ("malformed", "UPDATE outbox SET preconditions='[]' WHERE idem_key=?", (self.root,)),
            ("missing-descendant", "DELETE FROM outbox WHERE idem_key=?", (status,)),
        ]
        for name, sql, parameters in cases:
            with self.subTest(case=name):
                store = Store(w.state_path, w.clock)
                try:
                    with store.transaction() as conn:
                        conn.execute(sql, parameters)
                    before = [dict(r) for r in store.conn.execute("SELECT * FROM outbox ORDER BY idem_key")]
                    before_task = dict(store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?",
                                                          (life.tid,)).fetchone())
                finally:
                    store.close()
                self.assertEqual(w.main("outbox", "resend", self.root)[0], 1)
                store = Store(w.state_path, w.clock)
                try:
                    self.assertEqual([dict(r) for r in store.conn.execute("SELECT * FROM outbox ORDER BY idem_key")],
                                     before)
                    self.assertEqual(dict(store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?",
                                                              (life.tid,)).fetchone()), before_task)
                    with store.transaction() as conn:
                        for row in self.rows:
                            columns = list(row)
                            conn.execute("INSERT OR REPLACE INTO outbox (" + ",".join(columns) + ") VALUES ("
                                         + ",".join("?" for _ in columns) + ")", [row[k] for k in columns])
                        conn.execute("UPDATE tasks SET hold_reason=?,input_gen=? WHERE clickup_task_id=?",
                                     (original_task["hold_reason"], original_task["input_gen"], life.tid))
                finally:
                    store.close()
