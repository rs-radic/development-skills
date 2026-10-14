"""S2 workflow tests: T2, T6, T7, T8, T10, T16, T20 (plan r5 §5-§8, §10, §13.1).

The harness drives the real S0 Store and the S2 engine. A minimal in-test executor
plays the S4 role: it runs pending outbox rows in order, rechecks preconditions with
``rules.preconditions_ok`` and simulates the ClickUp/Kanban effects in memory.
"""

import copy
import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from clickup_intake import rules
from clickup_intake.config import Config
from clickup_intake.contracts import Item, Preconditions
from clickup_intake.engine import Observation, evaluate
from clickup_intake.parse import parse_comment
from clickup_intake.store import load_ticket_snapshot, revoke_live_tickets, Store
from clickup_intake.ticket import ticket_verdict
from fakes.fake_clock import FakeClock

TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
BOT, APPROVER_A, APPROVER_B, APPROVER_C, REQ, OTHER = 900000001, 900000101, 900000102, 900000103, 555001, 777001
T = "86task1"
SHA1 = "a" * 64
SHA2 = "b" * 64
PROMOTED = "c" * 40
STATUS_NAMES = {n for n, _ in Config().expected_statuses}
WAKES = ("kb_create", "kb_comment", "kb_unblock")


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _h(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def tag(uid=BOT):
    return {"type": "tag", "text": "@Developer AI", "user": {"id": uid}}


class H:
    """Store + engine + in-memory executor for one test."""

    def __init__(self, tc, **cfg):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = FakeClock()
        self.path = os.path.join(self.dir, "state.sqlite3")
        self.store = Store(self.path, self.clock)
        tc.addCleanup(self.store.close)
        self.cfg = Config(**cfg)
        self.cards, self.attachments, self.comments = {}, {}, {}
        self.confirmed = {T}
        self.intents, self.effects, self.alerts, self.statuses = [], [], [], []
        self.fail = {}
        self._n = 0
        self._run = 0

    # ---- time / observation -------------------------------------------------
    @property
    def now(self):
        return self.clock()

    def seed(self, task=T, phase="new_quiet", requester=REQ):
        self.store.observe_commit(task, [], {"phase": phase, "requester_id": requester, "eligible": 1,
                                             "content_hash": "c0", "quiet_until_ms": self.now + 180_000})

    def post(self, cid, author, body, task=T, parent=None, mention=False, parts=None, comment_text=None,
             material=None):
        self.clock.advance(1)
        kind = "reply" if parent else "comment"
        if parts is None:
            parts = ([tag()] if mention else []) + [{"text": body}]
        ct = comment_text if comment_text is not None else ("@Developer AI " if mention else "") + body
        if mention and parts and parts[-1].get("text") == body:
            parts[-1] = {"text": " " + body}
        p = parse_comment(parts, ct, self.cfg.plain_attr_allowlist)
        mat = (mention and author != BOT) if material is None else material
        item = Item(kind, cid, parent, author, self.now, _h(json.dumps([parts, ct])), mentions_bot=mention,
                    shape=p.shape, shape_sig=p.shape_sig, material=mat)
        self.store.observe_commit(task, [item], {"quiet_until_ms": self.now + 180_000})
        self.comments[(task, kind, cid)] = (parts, ct)

    def edit(self, cid, author, body, task=T, parent=None, mention=False):
        old = self.store.conn.execute("SELECT cur_mentions_bot FROM items WHERE task=? AND id=?",
                                      (task, cid)).fetchone()
        added = mention and not old[0]
        self.post(cid, author, body, task, parent, mention, material=added)

    def describe(self, task=T, h="c-new"):
        self.store.observe_commit(task, [], {"content_hash": h, "quiet_until_ms": self.now + 180_000})

    def settle(self, task=T, seconds=181):
        """Quiet expires, then a complete F followed by a clean L in the dispatching scan (§4.4)."""
        self.clock.advance(seconds)
        self.store.observe_commit(task, [], {"full_started_ms": self.now, "last_full_ms": self.now,
                                             "last_light_ms": self.now + 1, "full_cursor": None})
        self.clock.advance(ms=2)

    def light(self, task=T, seconds=60):
        self.clock.advance(seconds)
        self.store.observe_commit(task, [], {"last_light_ms": self.now})
        self.clock.advance(ms=1)

    def obs(self):
        return Observation(scan_started_ms=self.now - 2, comments=dict(self.comments),
                           cards=copy.deepcopy(self.cards), attachments=copy.deepcopy(self.attachments),
                           confirmed=frozenset(self.confirmed))

    def scan(self):
        new = evaluate(self.store, self.now, self.obs(), self.cfg)
        self.intents.extend(new)
        return new

    def cycle(self, rounds=10):
        """Evaluate and execute until nothing new is queued (S4 executor stand-in)."""
        out = []
        for _ in range(rounds):
            new = self.scan()
            out.extend(new)
            ran = self.execute()
            if not new and not ran:
                break
        return out

    # ---- state readers ------------------------------------------------------
    def task(self, task=T):
        return dict(self.store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (task,)).fetchone())

    def approvals(self, task=T):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM approvals WHERE task=? ORDER BY id",
                                                         (task,))]

    def rows(self, kind=None):
        q = "SELECT * FROM outbox" + (" WHERE kind=?" if kind else "") + " ORDER BY rowid"
        return [dict(r) for r in self.store.conn.execute(q, (kind,) if kind else ())]

    def request_cards(self, task=T):
        return [r[0] for r in self.store.conn.execute(
            "SELECT card_id FROM cards WHERE task=? AND role='request' ORDER BY seq", (task,))]

    def relays(self, task=T):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM relays WHERE task=? ORDER BY rowid",
                                                         (task,))]

    def eff(self, kind):
        return [e for e in self.effects if e[0] == kind]

    def verdict(self, a=None, task=T):
        a = a or self.approvals(task)[-1]
        snap = load_ticket_snapshot(self.path, task, self.cfg.ticket_lease_seconds)
        return ticket_verdict(snap, self.cards.get(a["gate_card"], {"task": {}}), task, a["rev"], a["plan_sha256"],
                              a["gate_card"], self.now)

    # ---- executor -----------------------------------------------------------
    def _state(self, key):
        r = self.store.conn.execute("SELECT state FROM outbox WHERE idem_key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set(self, key, state, ref=None):
        self.store.conn.execute("UPDATE outbox SET state=?, result_ref=COALESCE(?, result_ref), attempts=attempts+1,"
                                " first_send_ms=COALESCE(first_send_ms, ?) WHERE idem_key=?",
                                (state, ref, self.now, key))

    def execute(self):
        ran = False
        progressed = True
        retry = {r["idem_key"] for r in self.rows() if r["state"] == "retry_wait"}  # backoff: one retry per execute
        while progressed:
            progressed = False
            for r in self.rows():
                if r["state"] != "pending" and not (r["state"] == "retry_wait" and r["idem_key"] in retry):
                    continue
                deps = [self._state(d) for d in json.loads(r["depends_on"] or "[]")]
                if any(s in ("cancelled_stale", "failed", "held_ambiguous") for s in deps):
                    self._set(r["idem_key"], "cancelled_stale")
                    progressed = True
                    continue
                if any(s != "done" for s in deps):
                    continue
                pre = Preconditions(**json.loads(r["preconditions"]))
                task = self.store.conn.execute("SELECT * FROM tasks WHERE clickup_task_id=?", (pre.task_id,)).fetchone()
                apps = {a["id"]: dict(a) for a in self.store.conn.execute("SELECT * FROM approvals")}
                payload = json.loads(r["body"])
                card = self.cards.get(payload.get("card"))
                ok = rules.preconditions_ok(dict(task) if task else None, apps, pre, r["kind"], card,
                                            self.cfg.list_id if pre.task_id in self.confirmed else None)
                if not ok:
                    self._set(r["idem_key"], "cancelled_stale")
                    progressed = True
                    continue
                if not rules.send_barrier_ok(dict(task), payload, self.now, self.now - 2):
                    continue  # quiet-expired row: deferred, not cancelled (§4.4 at send/retry time)
                retry.discard(r["idem_key"])
                if r["idem_key"] in self.fail:
                    self._set(r["idem_key"], self.fail.pop(r["idem_key"]))
                    progressed = True
                    continue
                if r["kind"] == "kb_attach" and self._collides(payload):
                    self._set(r["idem_key"], "held_ambiguous")  # exact name+SHA readback can never match
                    progressed = True
                    continue
                self._set(r["idem_key"], "done", self._apply(r["kind"], payload))
                ran = progressed = True
        return ran

    def _collides(self, p):
        """Real ``hermes kanban attach`` semantics (kanban_db._collision_free_path): a requested name already on the
        card is stored as ``<stem> (n)<ext>``, so the exact-name readback (§10.2) cannot verify the new bytes."""
        atts = self.attachments[p["card"]]
        existing = {a["name"] for a in atts}
        if p["name"] not in existing:
            return False
        stem, dot, ext = p["name"].partition(".")
        n = 1
        while "%s (%d)%s%s" % (stem, n, dot, ext) in existing:
            n += 1
        stored = "%s (%d)%s%s" % (stem, n, dot, ext)
        self.effects.append(("kb_attach", dict(p, name=stored)))
        atts.append({"name": stored, "sha256": p.get("sha256")})
        return True

    def _apply(self, kind, p):
        self.effects.append((kind, p))
        if kind == "kb_create":
            for cid, c in self.cards.items():
                if c["task"].get("key") == p["idempotency_key"]:
                    return cid
            self._n += 1
            cid = "t_c%03d" % self._n
            self.cards[cid] = {"task": {"id": cid, "status": p.get("initial_status", "blocked"), "title": p["title"],
                                        "assignee": p.get("assignee"), "key": p["idempotency_key"], "body": ""},
                               "parents": [], "comments": [], "runs": [], "events": []}
            self.attachments[cid] = []
            return cid
        if kind == "kb_comment":
            self.cards[p["card"]]["comments"].append({"author": "clickup-intake", "body": p.get("text", ""),
                                                      "created_at": self.now // 1000})
        elif kind == "kb_attach":
            self.attachments[p["card"]].append({"name": p["name"], "sha256": p.get("sha256")})
        elif kind == "kb_unblock":
            self.cards[p["card"]]["task"]["status"] = "ready"
        elif kind == "kb_complete":
            self.cards[p["card"]]["task"].update(status="done", completed_at=self.now // 1000)
        elif kind == "kb_archive":
            self.cards[p["card"]]["task"]["status"] = "archived"
        elif kind == "cu_comment":
            self._n += 1
            cid = "cu%03d" % self._n
            self.store.observe_commit(p["task"], [Item("comment", cid, None, BOT, self.now, _h(p["text"]))], {})
            return cid
        elif kind == "cu_status":
            self.statuses.append(p["status"])
        elif kind == "alert":
            self.alerts.append(p["text"])
        return ""

    # ---- simulated workers --------------------------------------------------
    def _add_run(self, card, **run):
        self._run += 1
        run.setdefault("id", self._run)
        self.cards[card]["runs"].append(run)
        return run["id"]

    def park(self, card, *outputs):
        for body in outputs:
            self.cards[card]["comments"].append({"author": "planner", "body": body, "created_at": self.now // 1000})
        self._add_run(card, profile="planner", status="done", outcome="scheduled")
        self.cards[card]["task"]["status"] = "scheduled"

    def classify(self, card, result="answer_only", ref=None):
        rel = [r for r in self.relays() if r["card"] == card][-1] if ref is None else \
            [r for r in self.relays() if r["relay_ref"] == ref][0]
        self.park(card, "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=%s" %
                  (rel["relay_ref"], rel["gen"], result))

    def run_review(self, card, rev, sha=SHA1, accounts_gen=None):
        if accounts_gen is None:
            accounts_gen = max([r["gen"] for r in self.relays() if r["card"] == card and r["state"] != "staged"])
        name = "clickup-plan-r%d.md" % rev
        self._add_run(card, profile="planner", status="done", outcome="review_requested",
                      metadata={"clickup_task": T, "plan_revision": rev, "plan_attachment": name,
                                "plan_sha256": sha, "accounts_gen": accounts_gen})
        self.attachments[card].append({"name": name, "sha256": sha})
        self.cards[card]["task"]["status"] = "review"

    def pass_review(self, card):
        self._add_run(card, profile="plan-reviewer", status="done", outcome="completed")
        self.cards[card]["task"].update(status="done", completed_at=self.now // 1000)

    def new_card(self, status="todo", parents=(), body=""):
        self._n += 1
        cid = "t_x%03d" % self._n
        self.cards[cid] = {"task": {"id": cid, "status": status, "body": body}, "parents": list(parents),
                           "comments": [], "runs": [], "events": []}
        return cid

    def orch_done(self, a, marker=True, parent=True, gates=None):
        gate, orch = a["gate_card"], a["orchestrator_card"]
        body = "promotion\n" + (rules.marker(gate, T, a["rev"], a["plan_sha256"]) + "\n" if marker else "")
        fe = self.new_card()
        promo = self.new_card(parents=[gate] if parent else [], body=body)
        cleanup = self.new_card()
        g = gates or {"plan_revision": a["rev"], "plan_sha256": a["plan_sha256"], "final_e2e": fe,
                      "promotion": promo, "cleanup": cleanup, "lineage": [fe, promo, cleanup]}
        self._add_run(orch, profile="orchestrator", status="done", outcome="completed", metadata={"clickup_gates": g})
        self.cards[orch]["task"].update(status="done", completed_at=self.now // 1000)
        return fe, promo

    def done(self, card, sha=None):
        if sha is not None:
            self._add_run(card, profile="integrator", status="done", outcome="completed",
                          metadata={"promoted_sha": sha})
        self.cards[card]["task"].update(status="done", completed_at=self.now // 1000)

    # ---- canned flows -------------------------------------------------------
    def picked_up(self):
        self.seed()
        self.settle()
        self.cycle()
        card = self.request_cards()[-1]
        self.cards[card]["task"]["status"] = "running"
        return card

    def published(self, rev=1, sha=SHA1):
        card = self.picked_up()
        self.run_review(card, rev, sha)
        self.pass_review(card)
        self.settle()
        self.cycle()
        return card

    def approve(self, cid="ap1", author=APPROVER_A, rev=1):
        self.post(cid, author, "Approve plan r%d for development" % rev)
        self.settle()
        self.cycle()

    def developing(self, **kw):
        self.published()
        self.approve()
        a = self.approvals()[-1]
        fe, promo = self.orch_done(a, **kw)
        self.cycle()
        return fe, promo

    def ticketed(self):
        fe, promo = self.developing()
        self.done(fe)
        self.settle()
        self.cycle()
        return fe, promo


class T2IdleAndExclusionsTest(unittest.TestCase):
    def test_idle_scans_and_unaddressed_comments_wake_nothing(self):
        h = H(self)
        card = h.published()
        before = len(h.intents)
        for i in range(200):
            if i % 25 == 0:
                h.post("cw%d" % i, OTHER, "coworker note %d" % i)
            h.settle(seconds=61)
            h.cycle()
        later = [i for i in h.intents[before:] if i.kind in WAKES]
        self.assertEqual(later, [])
        self.assertEqual(h.task()["phase"], "awaiting_approval")
        # The next addressed input relays them as context (to request 2, since request 1 PASSed).
        h.post("m1", REQ, "what about exports?", mention=True)
        h.settle()
        h.cycle()
        rel = h.relays()[-1]
        ids = {i[1] for i in json.loads(rel["item_ids"])}
        self.assertTrue({"cw0", "cw175", "m1"} <= ids)
        self.assertNotEqual(rel["card"], card)

    def test_idle_in_every_glue_phase(self):
        for phase in ("planning", "awaiting_business", "awaiting_technical", "in_review", "awaiting_approval",
                      "revising", "approval_queued", "development", "implemented", "promotion_window",
                      "promotion_held", "ready_for_deployment"):
            with self.subTest(phase=phase):
                h = H(self)
                h.seed(phase=phase)
                for i in range(20):
                    h.post("u%d" % i, OTHER, "unaddressed")
                    h.settle()
                    h.cycle()
                self.assertEqual([i for i in h.intents if i.kind in WAKES], [])

    def test_baseline_and_not_admitted_rows_yield_no_intents(self):
        for phase in ("baseline", "not_admitted"):
            with self.subTest(phase=phase):
                h = H(self)
                h.seed(phase=phase)
                h.post("m", APPROVER_A, "please plan", mention=True)
                h.post("c", APPROVER_A, "Business requirements closed")
                h.post("a", APPROVER_A, "Approve plan r1 for development")
                h.settle()
                self.assertEqual(h.cycle(), [])
                self.assertEqual(h.task()["phase"], phase)


class PickupTest(unittest.TestCase):
    def test_pickup_waits_for_barrier_and_confirmed_home_list(self):
        h = H(self)
        h.seed()
        h.clock.advance(179)
        self.assertEqual(h.cycle(), [])
        h.confirmed.clear()
        h.settle()
        self.assertEqual(h.cycle(), [])
        h.confirmed.add(T)
        h.settle()
        h.cycle()
        card = h.request_cards()[0]
        self.assertEqual(h.cards[card]["task"]["status"], "ready")
        kinds = [k for k, _ in h.effects]
        self.assertEqual(kinds.count("kb_create"), 1)
        self.assertEqual(kinds.count("kb_unblock"), 1)
        self.assertIn("kb_subscribe", kinds)
        self.assertEqual(h.task()["phase"], "planning")
        create = h.eff("kb_create")[0][1]
        self.assertEqual(create["idempotency_key"], "clickup-dp:%s:req:1" % T)
        self.assertEqual(create["assignee"], "planner")

    def test_pickup_requires_complete_F_after_quiet(self):
        h = H(self)
        h.seed()
        h.clock.advance(200)
        h.store.observe_commit(T, [], {"full_started_ms": h.now - 100_000, "last_light_ms": h.now,
                                       "full_cursor": None})
        self.assertEqual(h.cycle(), [])
        h.store.observe_commit(T, [], {"full_started_ms": h.now, "full_cursor": '{"start": 1}'})
        self.assertEqual(h.cycle(), [])


class T6MentionTest(unittest.TestCase):
    def parked(self, h):
        card = h.picked_up()
        h.park(card, "CLICKUP-OUT v1 kind=business_questions relay=x\nWhich users need it?")
        h.cycle()
        return card

    def test_tag_triggers_and_plain_at_text_does_not(self):
        h = H(self)
        card = self.parked(h)
        h.post("p1", REQ, "@Developer AI is this ok?")  # plain text, no tag part
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "scheduled")
        h.post("m1", REQ, "is this ok?", mention=True)
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "ready")
        rel = h.relays()[-1]
        self.assertEqual(rel["state"], "delivered")
        self.assertEqual({i[1] for i in json.loads(rel["item_ids"])}, {"p1", "m1"})
        comment = [p for k, p in h.effects if k == "kb_comment"][-1]
        self.assertEqual(comment["relay"], rel["relay_ref"])

    def test_threaded_mention_answer_goes_to_same_thread(self):
        h = H(self)
        card = self.parked(h)
        h.post("root1", OTHER, "old thread")
        h.post("r1", REQ, "in thread?", parent="root1", mention=True)
        h.settle()
        h.cycle()
        h.park(card, "CLICKUP-OUT v1 kind=answer relay=x reply_to=r1\nYes, in this thread.")
        h.cycle()
        reply = h.eff("cu_reply")[-1][1]
        self.assertEqual(reply["root"], "root1")
        self.assertIn("Yes, in this thread.", reply["text"])

    def test_unknown_or_foreign_reply_to_is_never_a_destination(self):
        """QSR F1: an answer's reply_to must be an observed comment/reply of THIS task (§7.1, §8.3, §9)."""
        for target in ("nope", "f1", "fr1", "orph"):
            with self.subTest(target=target):
                h = H(self)
                card = self.parked(h)
                h.seed(task="86task2")  # another tracked task with its own thread
                h.post("f1", OTHER, "foreign root", task="86task2")
                h.post("fr1", OTHER, "foreign reply", task="86task2", parent="f1")
                h.post("orph", OTHER, "reply whose root is not an observed comment of this task", parent="f1")
                h.cards[card]["task"]["status"] = "running"
                h.park(card, "CLICKUP-OUT v1 kind=answer relay=x reply_to=%s\nSecret answer." % target)
                h.cycle()
                posts = [p for k, p in h.effects if k in ("cu_reply", "cu_comment") and "Secret answer." in p["text"]]
                self.assertEqual(posts, [])
                self.assertEqual(sum("reply_to" in x for x in h.alerts), 1)
                self.assertNotIn(target, " ".join(h.alerts))  # planner-supplied ids are not echoed

    def test_mention_added_by_edit_asks_but_never_commands(self):
        h = H(self)
        card = self.parked(h)
        h.post("e1", REQ, "Business requirements closed and one question")
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "scheduled")
        h.edit("e1", REQ, "Business requirements closed", mention=True)
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "ready")  # the question is relayed
        rel = h.relays()[-1]
        self.assertEqual([p for k, p in h.effects if k == "kb_comment"][-1]["closures"], [])
        self.assertEqual(h.task()["phase"], "awaiting_business")
        self.assertTrue(rel["state"] == "delivered")


class T7AuthorMatrixTest(unittest.TestCase):
    AUTHORS = {"requester": REQ, "approver_a": APPROVER_A, "approver_b": APPROVER_B, "approver_c": APPROVER_C, "other": OTHER, "bot": BOT}

    def closure(self, kind, who, requester=REQ):
        h = H(self)
        h.seed(requester=requester)
        h.settle()
        h.cycle()
        card = h.request_cards()[0]
        h.cards[card]["task"]["status"] = "running"
        q = "business_questions" if kind == "business" else "technical_questions"
        h.park(card, "CLICKUP-OUT v1 kind=%s relay=x\nQ?" % q)
        h.cycle()
        g0 = h.task()["input_gen"]
        n0 = len(h.eff("cu_reply"))
        h.post("cl", who, "%s requirements closed" % kind.capitalize())
        h.settle()
        h.cycle()
        accepted = h.task()["input_gen"] == g0 + 1 and h.cards[card]["task"]["status"] == "ready"
        noticed = len(h.eff("cu_reply")) > n0
        return "yes" if accepted else ("notice" if noticed else "ignored")

    def approval(self, who):
        h = H(self)
        h.published()
        n0 = len(h.eff("cu_reply"))
        h.approve(author=who)
        if h.approvals():
            return "yes"
        return "notice" if len(h.eff("cu_reply")) > n0 else "ignored"

    def test_matrix(self):
        expected = {
            "business": {"requester": "yes", "approver_a": "yes", "approver_b": "yes", "approver_c": "yes", "other": "notice",
                         "bot": "ignored"},
            "technical": {"requester": "notice", "approver_a": "yes", "approver_b": "yes", "approver_c": "yes", "other": "notice",
                          "bot": "ignored"},
            "approval": {"requester": "notice", "approver_a": "yes", "approver_b": "yes", "approver_c": "yes", "other": "notice",
                         "bot": "ignored"},
        }
        for action, row in expected.items():
            for who, want in row.items():
                with self.subTest(action=action, who=who):
                    uid = self.AUTHORS[who]
                    got = self.approval(uid) if action == "approval" else self.closure(action, uid)
                    self.assertEqual(got, want)

    def test_requester_who_is_also_named_approver(self):
        self.assertEqual(self.closure("technical", APPROVER_B, requester=APPROVER_B), "yes")
        h = H(self)
        h.published()
        h.store.observe_commit(T, [], {"requester_id": APPROVER_C})
        h.approve(author=APPROVER_C)
        self.assertEqual(len(h.approvals()), 1)

    def test_mention_question_any_human_bot_ignored(self):
        for who, relayed in ((OTHER, True), (APPROVER_B, True), (BOT, False)):
            with self.subTest(who=who):
                h = H(self)
                card = h.picked_up()
                h.park(card)
                h.post("q", who, "question?", mention=True)
                h.settle()
                h.cycle()
                self.assertEqual(h.cards[card]["task"]["status"] == "ready", relayed)


class T8CommandTest(unittest.TestCase):
    def outcome(self, h, n0):
        return len(h.approvals()), len(h.eff("cu_reply")) - n0

    def test_rejections(self):
        cases = [
            ("wrong rev", "Approve plan r2 for development", None, (0, 1)),
            ("vague", "Approved", None, (0, 1)),
            ("prose", "I think we can approve plan r1 for development soon", None, (0, 0)),
            ("whole quote", "Approve plan r1 for development", [{"text": "Approve plan r1 for development",
                                                                "attributes": {"blockquote": {}}}], (0, 0)),
            ("gt quote", "> Approve plan r1 for development", None, (0, 0)),
        ]
        for name, body, parts, want in cases:
            with self.subTest(case=name):
                h = H(self)
                h.published()
                n0 = len(h.eff("cu_reply"))
                h.post("x", APPROVER_A, body, parts=parts, comment_text=body)
                h.settle()
                h.cycle()
                self.assertEqual(self.outcome(h, n0), want)
                self.assertNotIn("waiting for approval", h.statuses[1:])

    def test_older_rev_after_new_publication(self):
        h = H(self)
        card = h.published()
        h.post("m", REQ, "change scope", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        self.assertNotEqual(card, card2)
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        self.assertEqual(h.task()["phase"], "revising")
        h.cards[card2]["task"]["status"] = "running"
        h.run_review(card2, 2, SHA2)
        h.pass_review(card2)
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["published_rev"], 2)
        n0 = len(h.eff("cu_reply"))
        h.approve("old", APPROVER_A, rev=1)
        self.assertEqual(self.outcome(h, n0), (0, 1))
        h.approve("new", APPROVER_A, rev=2)
        self.assertEqual([a["rev"] for a in h.approvals()], [2])

    def test_before_publication_is_context(self):
        h = H(self)
        card = h.picked_up()
        h.post("early", APPROVER_A, "Approve plan r1 for development")
        h.settle()
        h.cycle()
        h.run_review(card, 1)
        h.pass_review(card)
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["phase"], "awaiting_approval")
        self.assertEqual(h.approvals(), [])
        self.assertEqual(h.eff("cu_reply"), [])

    def test_edited_and_edited_into_command(self):
        h = H(self)
        h.published()
        h.post("ed", APPROVER_A, "Approve plan r1 for development")
        h.edit("ed", APPROVER_A, "Approve plan r1 for development.")
        h.post("into", APPROVER_A, "nice plan")
        h.settle()
        h.cycle()
        h.edit("into", APPROVER_A, "Approve plan r1 for development")
        h.settle()
        h.cycle()
        self.assertEqual(h.approvals(), [])
        self.assertEqual(len(h.eff("cu_reply")), 1)  # one edited notice for 'ed'

    def test_duplicate_consumed_once(self):
        h = H(self)
        h.published()
        h.approve("a1", APPROVER_A)
        creates = len(h.eff("kb_create"))
        h.approve("a2", APPROVER_B)
        self.assertEqual(len(h.approvals()), 1)
        self.assertEqual(len(h.eff("kb_create")), creates)
        self.assertIn("Already approved by Approver One.", h.eff("cu_reply")[-1][1]["text"])

    def test_bot_text_and_drag_are_not_approvals(self):
        h = H(self)
        h.published()
        h.post("b", BOT, "Approve plan r1 for development")
        h.store.observe_commit(T, [], {"last_status_seen": "implementing"})
        h.settle()
        h.cycle()
        self.assertEqual(h.approvals(), [])
        self.assertEqual(h.eff("cu_reply"), [])

    def test_quoted_closure_is_context(self):
        h = H(self)
        card = h.picked_up()
        h.park(card, "CLICKUP-OUT v1 kind=business_questions relay=x\nQ?")
        h.cycle()
        g0 = h.task()["input_gen"]
        h.post("qc", REQ, "> Business requirements closed", comment_text="> Business requirements closed")
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["input_gen"], g0)
        self.assertEqual(h.eff("cu_reply"), [])

    def test_unknown_shape_exact_text_rejected_with_notice_and_drift_alert(self):
        h = H(self)
        h.published()
        parts = [{"text": "Approve plan r1 for development", "attributes": {"bold": True}}]
        h.post("u1", APPROVER_A, "", parts=parts, comment_text="Approve plan r1 for development")
        h.post("u2", APPROVER_B, "", parts=parts, comment_text="Approve plan r1 for development")
        h.settle()
        h.cycle()
        self.assertEqual(h.approvals(), [])
        texts = [p["text"] for _, p in h.eff("cu_reply")]
        self.assertEqual(texts.count(
            "Not accepted: post the command as a new plain-text comment without quotes or formatting"), 2)
        self.assertEqual(len([a for a in h.alerts if "shape" in a]), 1)

    def test_held_while_classification_pending_then_valid_or_stale(self):
        for result, final in (("answer_only", "released"), ("material", "stale")):
            with self.subTest(result=result):
                h = H(self)
                h.published()
                h.post("m", REQ, "question", mention=True)
                h.approve("a1", APPROVER_A)
                self.assertEqual(h.approvals()[0]["state"], "held")
                self.assertEqual(h.eff("kb_create")[-1][1]["role"], "request")
                card2 = h.request_cards()[-1]
                h.cards[card2]["task"]["status"] = "running"
                h.classify(card2, result)
                h.settle()
                h.cycle()
                self.assertEqual(h.approvals()[0]["state"], final)


class T10StatusTest(unittest.TestCase):
    def test_staged_with_handoff_unblock_false(self):
        h = H(self, handoff_unblock=False)
        h.published()
        h.approve()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "staged")
        self.assertEqual(h.cards[a["orchestrator_card"]]["task"]["status"], "blocked")
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")
        self.assertIsNone(h.cards[a["gate_card"]]["task"]["assignee"])
        self.assertEqual(h.statuses, ["waiting for approval"])
        self.assertEqual(h.eff("cu_reply")[-1][1]["text"],
                         "Approval of r1 by Approver One recorded; queued for development; not started")
        self.assertEqual(h.task()["phase"], "approval_queued")
        g = h.task()["input_gen"]
        names = [p["name"] for k, p in h.effects if k == "kb_attach" and p["card"] == a["orchestrator_card"]]
        self.assertEqual(sorted(names), ["clickup-approval-r1.md", "clickup-context-g%d.md" % g,
                                         "clickup-ledger-g%d.md" % g, "clickup-plan-r1.md"])  # §8.2: context + ledger
        names = [p["name"] for k, p in h.effects if k == "kb_attach" and p["card"] == a["gate_card"]]
        self.assertEqual(sorted(names), ["clickup-context-g%d.md" % g, "clickup-ledger-g%d.md" % g])
        h.settle()
        self.assertEqual([i for i in h.cycle() if i.kind != "alert"], [])

    def test_staging_waits_for_context_and_ledger_on_both_cards(self):
        """QSR F5: staged (and its queued reply) only after the generation-bound context + ledger are attached."""
        h = H(self, handoff_unblock=False)
        h.published()
        t = h.task()
        lk = "clickup-dp:%s:gate:r1:g%d:attach:ledger:g%d" % (T, t["published_accounts_gen"], t["input_gen"])
        h.fail[lk] = "retry_wait"
        h.post("ap1", APPROVER_A, "Approve plan r1 for development")
        h.settle()
        for _ in range(5):
            h.scan()
            h.execute()
            if h._state(lk) == "retry_wait":
                break
        self.assertEqual(h._state(lk), "retry_wait")
        h.scan()
        self.assertEqual(h.approvals()[0]["state"], "recorded")
        self.assertEqual(h.eff("cu_reply"), [])
        h.cycle()
        self.assertEqual(h.approvals()[0]["state"], "staged")
        self.assertEqual(h.eff("cu_reply")[-1][1]["text"],
                         "Approval of r1 by Approver One recorded; queued for development; not started")

    def test_release_attaches_context_for_the_current_generation_first(self):
        """QSR F5: input classified after staging -> the new generation's context + ledger precede release."""
        h = H(self, handoff_unblock=False)
        h.published()
        h.approve()
        a = h.approvals()[0]
        h.post("m", REQ, "one more question", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2)
        h.cycle()
        g = h.task()["input_gen"]
        self.assertEqual(h.task()["classified_gen"], g)
        ck = "clickup-dp:%s:orch:r1:g%d:attach:context:g%d" % (T, a["accounts_gen"], g)
        h.fail[ck] = "retry_wait"
        h.cfg = dataclasses.replace(h.cfg, handoff_unblock=True)
        h.scan()
        h.execute()
        self.assertEqual(h._state(ck), "retry_wait")
        h.scan()
        h.execute()  # the context attach is retried; release is not queued before it is done
        self.assertNotIn(a["orchestrator_card"], [p["card"] for k, p in h.effects if k == "kb_unblock"])
        h.cycle()
        self.assertEqual(h.approvals()[0]["state"], "released")
        order = [(k, p.get("name")) for k, p in h.effects if p.get("card") == a["orchestrator_card"]]
        rel = order.index(("kb_unblock", None))
        self.assertIn(("kb_attach", "clickup-context-g%d.md" % g), order[:rel])
        gate = [p["name"] for k, p in h.effects if k == "kb_attach" and p["card"] == a["gate_card"]]
        self.assertIn("clickup-context-g%d.md" % g, gate)

    def test_started_only_with_valid_gates_parent_and_marker(self):
        for kw, alert in (({"marker": False}, "promotion unguarded"), ({"parent": False}, "promotion unguarded")):
            with self.subTest(kw=kw):
                h = H(self)
                h.developing(**kw)
                a = h.approvals()[0]
                self.assertEqual(a["state"], "released")
                self.assertNotIn("implementing", h.statuses)
                self.assertTrue(any(alert in x for x in h.alerts))
        h = H(self)
        h.published()
        h.approve()
        a = h.approvals()[0]
        h.orch_done(a, gates={"plan_revision": 1, "plan_sha256": SHA2, "final_e2e": "x", "promotion": "y",
                              "cleanup": "z", "lineage": []})
        h.cycle()
        self.assertEqual(h.approvals()[0]["state"], "released")
        self.assertTrue(any("clickup_gates" in x for x in h.alerts))

    def test_status_sequence_through_ready(self):
        h = H(self)
        fe, promo = h.developing()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "started")
        self.assertEqual(h.statuses, ["waiting for approval", "implementing"])
        self.assertEqual(h.eff("cu_reply")[-1][1]["text"], "Development started for r1")
        early = h.new_card(status="done")  # an early slice E2E / release card: not in clickup_gates
        h.cycle()
        self.assertEqual(h.approvals()[0]["state"], "started")
        h.done(fe)
        h.settle()
        h.cycle()
        self.assertEqual(h.statuses[-1], "implemented")
        self.assertEqual(h.task()["phase"], "promotion_window")
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "done")
        h.cycle()
        self.assertNotIn("ready for deployment", h.statuses)  # gate done alone
        h.done(promo, sha=PROMOTED)
        h.cycle()
        self.assertEqual(h.statuses[-1], "ready for deployment")
        self.assertEqual(h.approvals()[0]["state"], "promoted")
        self.assertEqual(h.approvals()[0]["promoted_sha"], PROMOTED)
        self.assertEqual(h.task()["phase"], "ready_for_deployment")
        self.assertTrue(set(h.statuses) <= STATUS_NAMES)
        self.assertNotIn("deployed", h.statuses)
        del early

    def test_promotion_archived_or_without_sha_changes_nothing(self):
        for how in ("archived", "nosha"):
            with self.subTest(how=how):
                h = H(self)
                fe, promo = h.ticketed()
                if how == "archived":
                    h.cards[promo]["task"]["status"] = "archived"
                else:
                    h.done(promo, sha="not-a-sha")
                h.cycle()
                self.assertEqual(h.approvals()[0]["state"], "ticketed")
                self.assertNotIn("ready for deployment", h.statuses)
                self.assertTrue(any("promot" in x for x in h.alerts))

    def test_status_writes_come_from_expected_statuses(self):
        h = H(self)
        card = h.picked_up()
        h.park(card, "CLICKUP-OUT v1 kind=business_questions relay=x\nQ?")
        h.cycle()
        h.park(card, "CLICKUP-OUT v1 kind=technical_questions relay=x\nT?")
        h.cycle()
        self.assertEqual(h.statuses, ["business requirements", "technical requirements"])
        self.assertTrue(set(h.statuses) <= STATUS_NAMES)
        q = h.eff("cu_comment")[0][1]["text"]
        self.assertIn("Business requirements closed", q)
        with self.assertRaises(ValueError):
            from clickup_intake.engine import _Ctx
            _Ctx(h.store, h.now, h.obs(), h.cfg).set_status(T, "k", "deployed")


class T16GenerationTest(unittest.TestCase):
    def test_description_change_after_pass_before_publication(self):
        h = H(self)
        card = h.picked_up()
        h.run_review(card, 1)
        h.pass_review(card)
        h.describe()
        h.settle()
        h.cycle()
        self.assertIsNone(h.task()["published_rev"])
        card2 = h.request_cards()[-1]
        self.assertNotEqual(card2, card)
        relay_comment = [p for k, p in h.effects if k == "kb_comment" and p["card"] == card2][0]
        self.assertEqual(relay_comment["note"], "r1 passed review but is unpublished; classify")
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "answer_only")
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["published_rev"], 1)
        self.assertEqual(h.task()["phase"], "awaiting_approval")

    def test_publication_retry_waits_for_new_quiet_and_barrier(self):
        """QSR F2: publication is quiet-expired; a retried step waits for the current quiet + barrier."""
        h = H(self)
        card = h.picked_up()
        h.run_review(card, 1)
        h.pass_review(card)
        h.settle()
        ak = [i.idem_key for i in h.scan() if i.kind == "cu_attach"][0]
        h.fail[ak] = "retry_wait"
        h.execute()
        h.post("u", OTHER, "an unrelated note")
        h.light()
        h.cycle()
        self.assertEqual(h._state(ak), "retry_wait")
        self.assertEqual(h.eff("cu_attach"), [])
        self.assertIsNone(h.task()["published_rev"])
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["published_rev"], 1)
        self.assertEqual(len(h.eff("cu_attach")), 1)

    def test_attachment_change_after_publication_revising(self):
        h = H(self)
        h.published()
        h.store.observe_commit(T, [Item("attachment", "f1", None, REQ, h.now, "h", material=True)],
                               {"quiet_until_ms": h.now + 180_000})
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        self.assertEqual(h.task()["phase"], "revising")
        self.assertEqual(h.statuses[-1], "reviewing feature")
        self.assertIn("Approval of r1 is no longer valid; plan being revised", h.eff("cu_comment")[-1][1]["text"])
        h.approve("late", APPROVER_A)
        self.assertEqual(h.approvals(), [])

    def test_answer_only_creates_no_revision(self):
        h = H(self)
        h.published()
        h.post("m", REQ, "how long?", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2)
        h.cycle()
        self.assertEqual(h.task()["phase"], "awaiting_approval")
        self.assertEqual(h.task()["published_rev"], 1)
        h.approve()
        self.assertEqual(h.approvals()[0]["state"], "released")

    def test_material_after_staged_before_release_archives_own_cards(self):
        h = H(self, handoff_unblock=False)
        h.published()
        h.approve()
        a = h.approvals()[0]
        h.post("m", REQ, "new scope", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "stale")
        self.assertEqual(h.cards[a["orchestrator_card"]]["task"]["status"], "archived")
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "archived")
        self.assertEqual(h.task()["phase"], "revising")
        texts = [p["text"] for k, p in h.effects if k == "cu_comment"]
        self.assertEqual(sum("no longer valid; plan being revised" in t for t in texts), 1)

    def test_material_after_release_before_final_e2e_is_never_ticketed(self):
        h = H(self)
        fe, promo = h.developing()
        h.post("m", REQ, "scope change", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        h.done(fe)
        h.settle()
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "implemented")
        self.assertEqual(a["ticket_epoch"], 0)
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")
        texts = [p["text"] for k, p in h.effects if k == "cu_comment"]
        self.assertIn("Promotion of r1 held: scope change; revised plan required", texts)

    def test_amendment_published_in_development_keeps_status_and_is_approvable(self):
        h = H(self)
        fe, promo = h.developing()
        h.post("m", REQ, "scope change", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        h.cards[card2]["task"]["status"] = "running"
        h.run_review(card2, 2, SHA2)
        h.pass_review(card2)
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["published_rev"], 2)
        self.assertEqual(h.task()["phase"], "development")
        self.assertEqual(h.statuses, ["waiting for approval", "implementing"])
        h.approve("a2", APPROVER_B, rev=2)
        apps = h.approvals()
        self.assertEqual([(a["rev"], a["state"]) for a in apps], [(1, "revoked"), (2, "released")])
        self.assertEqual(apps[0]["revoked_reason"], "superseded")
        orch = [p for k, p in h.effects if k == "kb_create" and p["role"] == "orchestrator"][-1]
        self.assertEqual(orch["title"], "ClickUp %s: orchestrate amendment r2" % T)
        self.assertEqual(orch["params"]["lineage"][0]["gate_card"], apps[0]["gate_card"])

    def test_auto_gate_false_never_completes_gate(self):
        h = H(self, auto_gate=False)
        fe, promo = h.developing()
        h.done(fe)
        h.settle()
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "ticketed")
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")
        self.assertEqual(h.eff("kb_complete"), [])
        self.assertTrue(any("the operator may complete gate" in x for x in h.alerts))


class PostPublicationQuestionsTest(unittest.TestCase):
    """SPEC r1 finding 1: a question exchange after publication must not strand rN (§5.3, §6, §8.4)."""

    def asked(self, h, same_park_result=None):
        h.published()
        h.post("m", REQ, "who needs exports?", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        q = "CLICKUP-OUT v1 kind=business_questions relay=x\nWhich users need exports?"
        if same_park_result is None:
            h.park(card2, q)
            h.cycle()
            self.assertEqual(h.task()["phase"], "awaiting_business")
            self.assertEqual(h.statuses[-1], "business requirements")
        else:
            rel = [r for r in h.relays() if r["card"] == card2][-1]
            h.park(card2, q, "CLICKUP-OUT v1 kind=classification relay=%s gen=%d result=%s"
                   % (rel["relay_ref"], rel["gen"], same_park_result))
            h.cycle()
        return card2

    def test_answer_only_after_questions_restores_approvable_rn(self):
        for same_park in (False, True):
            with self.subTest(same_park=same_park):
                h = H(self)
                card2 = self.asked(h, "answer_only" if same_park else None)
                if not same_park:
                    h.cards[card2]["task"]["status"] = "running"
                    h.classify(card2, "answer_only")
                    h.cycle()
                self.assertEqual(h.task()["phase"], "awaiting_approval")
                self.assertEqual(h.statuses[-1], "waiting for approval")
                if same_park:  # the superseded question status is never written (truthful projection)
                    self.assertNotIn("business requirements", h.statuses)
                self.assertEqual(h.task()["published_rev"], 1)
                h.approve("ap-after", APPROVER_A)
                self.assertEqual([(a["rev"], a["state"]) for a in h.approvals()], [(1, "released")])

    def test_material_after_questions_applies_revising_status_and_notice(self):
        h = H(self)
        card2 = self.asked(h)
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        self.assertEqual(h.task()["phase"], "revising")
        self.assertEqual(h.statuses[-1], "reviewing feature")
        texts = [p["text"] for k, p in h.effects if k == "cu_comment"]
        self.assertEqual(texts.count("Approval of r1 is no longer valid; plan being revised"), 1)
        h.approve("late", APPROVER_A)
        self.assertEqual(h.approvals(), [])
        # later questions for r2 keep their phase: a further material classification does not re-revise r1
        h.park(card2, "CLICKUP-OUT v1 kind=business_questions relay=x\nWhich regions?")
        h.cycle()
        self.assertEqual(h.task()["phase"], "awaiting_business")
        h.post("cl", REQ, "Business requirements closed")
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card2]["task"]["status"], "ready")
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        self.assertEqual(h.task()["phase"], "awaiting_business")
        texts = [p["text"] for k, p in h.effects if k == "cu_comment"]
        self.assertEqual(texts.count("Approval of r1 is no longer valid; plan being revised"), 1)


class MissingClassificationTest(unittest.TestCase):
    """QSR F4: a card parked after a delivered relay it never classified -> one deduped alert (§5.3, §11)."""

    def test_parked_without_classification_alerts_once_and_wakes_nothing(self):
        h = H(self)
        h.published()
        h.post("m", REQ, "Please explain one detail", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.cycle()
        self.assertEqual(h.alerts, [])  # delivered but not yet parked: no alert
        h.park(card2, "CLICKUP-OUT v1 kind=answer relay=x\nThe requested explanation")
        unblocks = len(h.eff("kb_unblock"))
        for _ in range(5):
            h.settle()
            h.cycle()
        t = h.task()
        self.assertGreater(t["input_gen"], t["classified_gen"])  # nothing inferred
        self.assertEqual(sum("classif" in x for x in h.alerts), 1)
        self.assertEqual(len(h.eff("kb_unblock")), unblocks)  # no AI woken
        self.assertEqual(h.cards[card2]["task"]["status"], "scheduled")
        self.assertEqual(h.relays()[-1]["state"], "delivered")

    def test_pickup_relay_and_classified_relays_raise_no_alert(self):
        h = H(self)
        card = h.picked_up()
        h.park(card, "CLICKUP-OUT v1 kind=business_questions relay=x\nWhich users?")
        h.cycle()
        h.post("m", REQ, "all of them", mention=True)
        h.settle()
        h.cycle()
        h.cards[card]["task"]["status"] = "running"
        h.classify(card)
        h.cycle()
        self.assertEqual(h.task()["classified_gen"], h.task()["input_gen"])
        self.assertEqual([x for x in h.alerts if "classif" in x], [])


class WakeGuardTest(unittest.TestCase):
    """SPEC r1 finding 2: queued wakes never fire for terminal or not_admitted tasks (§4.2-§4.4, §10.1)."""

    def kinds_done(self, h):
        return [k for k, _ in h.effects]

    def test_pending_pickup_rows_cancelled_after_not_admitted(self):
        for stage in ("create", "unblock"):
            with self.subTest(stage=stage):
                h = H(self)
                h.seed()
                h.settle()
                h.scan()
                if stage == "unblock":
                    h.execute()  # request card created in scan N
                    queued = h.scan()  # relay + unblock queued for scan N+1
                    self.assertIn("kb_unblock", [i.kind for i in queued])
                h.store.observe_commit(T, [], {"phase": "not_admitted"})  # S1: barrier GET Task not admissible
                h.execute()
                self.assertNotIn("kb_unblock", self.kinds_done(h))
                wake_rows = [r for r in h.rows() if r["kind"] in ("kb_create", "kb_comment", "kb_attach",
                                                                   "kb_subscribe", "kb_unblock")
                             and r["state"] != "done"]
                self.assertTrue(wake_rows)
                self.assertEqual({r["state"] for r in wake_rows}, {"cancelled_stale"})
                self.assertTrue(all(c["task"]["status"] == "blocked" for c in h.cards.values()))
                self.assertEqual(h.cycle(), [])  # not_admitted: nothing planned
                # moved back to an intake status: new_quiet with fresh quiet, then a normal pickup
                h.store.observe_commit(T, [], {"phase": "new_quiet", "quiet_until_ms": h.now + 180_000})
                h.settle()
                h.cycle()
                card = h.request_cards()[0]
                self.assertEqual(h.cards[card]["task"]["status"], "ready")
                self.assertEqual(self.kinds_done(h).count("kb_create"), 1)
                self.assertEqual(self.kinds_done(h).count("kb_unblock"), 1)
                self.assertEqual(h.task()["phase"], "planning")

    def test_pending_relay_unblock_cancelled_after_terminal_and_card_archived(self):
        h = H(self)
        card = h.picked_up()
        h.park(card)
        unblocks = self.kinds_done(h).count("kb_unblock")
        h.post("q", REQ, "question?", mention=True)
        h.settle()
        self.assertIn("kb_unblock", [i.kind for i in h.scan()])
        h.store.observe_commit(T, [], {"terminal": 1})  # terminal fact from its own GET Task
        h.execute()
        self.assertEqual(self.kinds_done(h).count("kb_unblock"), unblocks)
        relay_rows = [r for r in h.rows() if ":relay:" in r["idem_key"] and r["state"] != "done"]
        self.assertTrue(relay_rows)
        self.assertEqual({r["state"] for r in relay_rows}, {"cancelled_stale"})
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "archived")  # protective archive still runs
        self.assertEqual(self.kinds_done(h).count("kb_unblock"), unblocks)

    def test_relay_unblock_retry_waits_for_new_quiet_and_barrier(self):
        """QSR F2: a queued wake retried after a definite failure waits for the current quiet + barrier."""
        h = H(self)
        card = h.picked_up()
        h.park(card)
        h.post("q", REQ, "question?", mention=True)
        h.settle()
        uk = [i.idem_key for i in h.scan() if i.kind == "kb_unblock"][0]
        h.fail[uk] = "retry_wait"
        h.execute()
        unblocks = len(h.eff("kb_unblock"))
        h.post("u", OTHER, "an unrelated note")
        h.light()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "scheduled")
        self.assertEqual(h._state(uk), "retry_wait")
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "ready")
        self.assertEqual(len(h.eff("kb_unblock")), unblocks + 1)

    def test_cancelled_initial_release_is_replanned_after_answer_only(self):
        """QSR-619 R2-F1: release fails, new input cancels the unsent retry, answer_only restores current(rN) ->
        the same orchestrator is released once (§5.3, §8.4 step 3, §10.1 'the engine replans'). A possibly-sent
        (held_ambiguous) release is never replanned."""
        for first in ("retry_wait", "held_ambiguous"):
            with self.subTest(first=first):
                h = H(self)
                h.published()
                uk = "clickup-dp:%s:orch:r1:g%d:unblock" % (T, h.task()["published_accounts_gen"])
                h.fail[uk] = first
                h.post("ap1", APPROVER_A, "Approve plan r1 for development")
                h.settle()
                for _ in range(10):
                    h.scan()
                    h.execute()
                    if h._state(uk) == first:
                        break
                a = h.approvals()[0]
                self.assertEqual((a["state"], h._state(uk)), ("staged", first))
                h.post("m", REQ, "one more question", mention=True)
                h.settle()
                h.cycle()
                card2 = h.request_cards()[-1]
                if first == "retry_wait":
                    self.assertEqual(h._state(uk), "cancelled_stale")  # unsent retry: stale input_gen
                h.cards[card2]["task"]["status"] = "running"
                h.classify(card2, "answer_only")
                for _ in range(5):
                    h.settle()
                    h.cycle()
                orch = a["orchestrator_card"]
                released = [p for k, p in h.effects if k == "kb_unblock" and p["card"] == orch]
                if first == "retry_wait":
                    self.assertEqual(h.approvals()[0]["state"], "released")
                    self.assertEqual(h.cards[orch]["task"]["status"], "ready")
                    self.assertEqual(len(released), 1)
                    g = h.task()["input_gen"]
                    order = [(k, p.get("name")) for k, p in h.effects if p.get("card") == orch]
                    self.assertIn(("kb_attach", "clickup-context-g%d.md" % g), order[:order.index(("kb_unblock", None))])
                else:
                    self.assertEqual(h.approvals()[0]["state"], "staged")
                    self.assertEqual((h._state(uk), released), ("held_ambiguous", []))

    def test_new_generation_ledger_on_approval_cards_never_collides(self):
        """QSR643 R7-F1: failed release -> @mention -> answer_only -> the new generation's context + ledger are
        attached to the SAME orchestrator/gate under new immutable names. With real CLI collision semantics a reused
        name would be stored renamed and never verify (§8.2, §8.4 step 3, §10.2); here the release completes once."""
        h = H(self)
        h.published()
        uk = "clickup-dp:%s:orch:r1:g%d:unblock" % (T, h.task()["published_accounts_gen"])
        h.fail[uk] = "retry_wait"
        h.post("ap1", APPROVER_A, "Approve plan r1 for development")
        h.settle()
        for _ in range(10):
            h.scan()
            h.execute()
            if h._state(uk) == "retry_wait":
                break
        a = h.approvals()[0]
        g0 = h.task()["input_gen"]
        self.assertEqual((a["state"], h._state(uk)), ("staged", "retry_wait"))
        h.post("m", APPROVER_B, "does this cover mobile?", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "answer_only")
        for _ in range(5):
            h.settle()
            h.cycle()
        t = h.task()
        g = t["input_gen"]
        self.assertEqual((g, t["classified_gen"]), (g0 + 1, g0 + 1))
        self.assertEqual([r["idem_key"] for r in h.rows() if r["state"] in ("held_ambiguous", "failed")], [])
        orch, gate = a["orchestrator_card"], a["gate_card"]
        self.assertEqual((h.approvals()[0]["state"], h.cards[orch]["task"]["status"]), ("released", "ready"))
        self.assertEqual(len([p for k, p in h.effects if k == "kb_unblock" and p["card"] == orch]), 1)
        for card in (orch, gate):
            names = [x["name"] for x in h.attachments[card]]
            self.assertEqual(len(names), len(set(names)), names)
            self.assertNotIn(" (", "".join(names))  # nothing stored under a CLI-renamed name
            for gen in (g0, g):
                self.assertIn("clickup-context-g%d.md" % gen, names)
                self.assertIn("clickup-ledger-g%d.md" % gen, names)
        order = [(k, p.get("name")) for k, p in h.effects if p.get("card") == orch]
        self.assertIn(("kb_attach", "clickup-ledger-g%d.md" % g), order[:order.index(("kb_unblock", None))])

    def test_first_wake_batch_rebound_to_activity_during_its_retry(self):
        """QSR-619 R2-F2 (S2 part): the first relay's comment + context land, its unblock fails, then an unaddressed
        comment restarts quiet -> at the new barrier the unsent first wake is re-staged as ONE batch with a fresh
        context file that includes it (§4.4, §5.1, §8.2); still exactly one wake, none for the comment alone."""
        h = H(self)
        h.seed()
        h.settle()
        uk = None
        for _ in range(5):
            for i in h.scan():
                if i.kind == "kb_unblock":
                    uk = i.idem_key
                    h.fail[uk] = "retry_wait"
            h.execute()
            if uk and h._state(uk) == "retry_wait":
                break
        card = h.request_cards()[0]
        g = h.task()["input_gen"]
        self.assertEqual([p["name"] for k, p in h.effects if k == "kb_attach"], ["clickup-context-g%d.md" % g])
        h.post("c1002", OTHER, "only enterprise customers need this")
        h.light()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "blocked")  # quiet restarted: no wake yet
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "ready")
        self.assertEqual([p["card"] for k, p in h.effects if k == "kb_unblock"], [card])
        self.assertEqual(h._state(uk), "cancelled_stale")
        rels = h.relays()
        self.assertEqual(len(rels), 1)
        rel = rels[0]
        self.assertEqual((rel["state"], rel["gen"]), ("delivered", g))
        self.assertIn(["comment", "c1002"], json.loads(rel["item_ids"]))
        it = h.store.conn.execute("SELECT delivered_relay FROM items WHERE id='c1002'").fetchone()
        self.assertEqual(it[0], rel["relay_ref"])
        order = [(k, p.get("name") or p.get("relay")) for k, p in h.effects if p.get("card") == card]
        wake = order.index(("kb_unblock", None))
        self.assertEqual(order[wake - 2:wake], [("kb_comment", rel["relay_ref"]),
                                                ("kb_attach", "clickup-context-g%d-b2.md" % g)])
        relay = [p for k, p in h.effects if k == "kb_comment" and p.get("relay") == rel["relay_ref"]][0]
        self.assertIn(["comment", "c1002"], relay["items"])
        self.assertIn("supersedes", relay["note"])
        self.assertEqual(h.task()["phase"], "planning")
        h.cards[card]["task"]["status"] = "running"
        h.post("c1003", OTHER, "another note")
        h.settle()
        h.cycle()
        self.assertEqual(len(h.eff("kb_unblock")), 1)  # an unaddressed comment alone never wakes

    def test_later_relay_batch_rebound_to_activity_during_its_retry(self):
        """QSR-624 R3-F1: a LATER scheduled relay (wait>0) whose comment + context landed but whose unblock failed is
        re-staged at the new barrier when unaddressed input restarts quiet: one batch + fresh context file including
        it, the SAME recorded wait run, still one wake, none for the unaddressed input alone (§4.4, §5.1, §8.2, §8.3)."""
        h = H(self)
        card = h.picked_up()
        h.park(card, "CLICKUP-OUT v1 kind=business_questions relay=x\nWhich users need it?")
        h.cycle()
        run = h.cards[card]["runs"][-1]["id"]
        h.post("q2", REQ, "Please clarify the licensed users", mention=True)
        h.settle()
        uk = None
        for _ in range(5):
            for i in h.scan():
                if i.kind == "kb_unblock":
                    uk = i.idem_key
                    h.fail[uk] = "retry_wait"
            h.execute()
            if uk and h._state(uk) == "retry_wait":
                break
        g = h.task()["input_gen"]
        old = h.relays()[-1]["relay_ref"]
        self.assertEqual(old, "%s-s1-g%d-w%d" % (T, g, run))
        ok = "clickup-dp:%s:relay:%s" % (T, old)
        self.assertEqual((h._state(ok + ":comment"), h._state(ok + ":attach")), ("done", "done"))
        unblocks = len(h.eff("kb_unblock"))
        h.post("c1002", OTHER, "only enterprise customers need this")
        h.light()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "scheduled")  # quiet restarted: no wake yet
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "ready")
        self.assertEqual(len(h.eff("kb_unblock")), unblocks + 1)
        self.assertEqual(h._state(uk), "cancelled_stale")
        rel = h.relays()[-1]
        ref = "%s-s1-g%d-b2-w%d" % (T, g, run)
        self.assertEqual((rel["relay_ref"], rel["state"], rel["gen"]), (ref, "delivered", g))
        self.assertEqual(h.task()["input_gen"], g)
        self.assertIn(["comment", "c1002"], json.loads(rel["item_ids"]))
        for iid in ("q2", "c1002"):
            it = h.store.conn.execute("SELECT delivered_relay FROM items WHERE id=?", (iid,)).fetchone()
            self.assertEqual(it[0], ref)
        order = [(k, p.get("name") or p.get("relay")) for k, p in h.effects if p.get("card") == card]
        wake = len(order) - 1 - order[::-1].index(("kb_unblock", None))
        self.assertEqual(order[wake - 2:wake], [("kb_comment", ref), ("kb_attach", "clickup-context-g%d-b2.md" % g)])
        relay = [p for k, p in h.effects if k == "kb_comment" and p.get("relay") == ref][0]
        self.assertIn("supersedes relay %s" % old, relay["note"])
        wake_row = [r for r in h.rows("kb_unblock") if r["idem_key"] == "clickup-dp:%s:relay:%s:unblock" % (T, ref)][0]
        self.assertEqual(json.loads(wake_row["body"])["wait_run_id"], run)  # the original wait_run guard
        self.assertEqual(json.loads(wake_row["preconditions"])["wait_run_id"], run)
        h.cards[card]["task"]["status"] = "running"
        h.post("c1003", OTHER, "another note")
        h.settle()
        h.cycle()
        self.assertEqual(len(h.eff("kb_unblock")), unblocks + 1)  # an unaddressed comment alone never wakes

    def test_abandoned_outbox_action_is_never_rearmed(self):
        """QSR-624 R3-F2: ``outbox abandon`` (cancelled_stale/ABANDONED) of a failed initial orchestrator release or
        relay wake is operator intent: confirming scans never re-arm it; explicit resend stays distinct (§10.2, §11)."""
        abandon = "UPDATE outbox SET state='cancelled_stale', outcome='ABANDONED' WHERE idem_key=?"  # main abandon
        resend = "UPDATE outbox SET state='pending', outcome=NULL, attempts=0, next_attempt_ms=NULL WHERE idem_key=?"
        with self.subTest(row="release"):
            h = H(self)
            h.published()
            uk = "clickup-dp:%s:orch:r1:g%d:unblock" % (T, h.task()["published_accounts_gen"])
            h.fail[uk] = "failed"
            h.post("ap1", APPROVER_A, "Approve plan r1 for development")
            h.settle()
            for _ in range(10):
                h.scan()
                h.execute()
                if h._state(uk) == "failed":
                    break
            a = h.approvals()[0]
            orch = a["orchestrator_card"]
            self.assertEqual((a["state"], h._state(uk)), ("staged", "failed"))
            h.store.conn.execute(abandon, (uk,))
            for _ in range(5):
                h.settle()
                h.cycle()
            row = [r for r in h.rows() if r["idem_key"] == uk][0]
            self.assertEqual((row["state"], row["outcome"]), ("cancelled_stale", "ABANDONED"))
            self.assertEqual((h.approvals()[0]["state"], h.cards[orch]["task"]["status"]), ("staged", "blocked"))
            self.assertEqual([p for k, p in h.effects if k == "kb_unblock" and p["card"] == orch], [])
            h.store.conn.execute(resend, (uk,))  # main resend: an explicit operator retry still works
            h.settle()
            h.cycle()
            self.assertEqual((h.approvals()[0]["state"], h.cards[orch]["task"]["status"]), ("released", "ready"))
            self.assertEqual(len([p for k, p in h.effects if k == "kb_unblock" and p["card"] == orch]), 1)
        with self.subTest(row="relay wake"):
            h = H(self)
            card = h.picked_up()
            h.park(card)
            h.post("q", REQ, "question?", mention=True)
            h.settle()
            uk = [i.idem_key for i in h.scan() if i.kind == "kb_unblock"][0]
            h.fail[uk] = "failed"
            h.execute()
            unblocks = len(h.eff("kb_unblock"))
            h.store.conn.execute(abandon, (uk,))
            for _ in range(3):
                h.settle()
                h.cycle()
            row = [r for r in h.rows() if r["idem_key"] == uk][0]
            self.assertEqual((row["state"], row["outcome"]), ("cancelled_stale", "ABANDONED"))
            self.assertEqual((h.cards[card]["task"]["status"], len(h.eff("kb_unblock"))), ("scheduled", unblocks))

    def test_terminal_still_posts_revocation_audit_stamp(self):
        h = H(self)
        h.ticketed()
        h.store.observe_commit(T, [], {"terminal": 1})
        h.cycle()
        a = h.approvals()[0]
        self.assertIn("CLICKUP-GATE v1 audit=revoked task=%s rev=1 epoch=1 reason=terminal" % T,
                      [c["body"] for c in h.cards[a["gate_card"]]["comments"]])


class T20TicketTest(unittest.TestCase):
    def test_issue_stamp_then_gate_and_valid_verdict(self):
        h = H(self)
        fe, promo = h.ticketed()
        a = h.approvals()[0]
        self.assertEqual((a["state"], a["ticket_epoch"], a["ticket_input_gen"]),
                         ("ticketed", 1, h.task()["input_gen"]))
        kinds = [(k, p.get("card")) for k, p in h.effects if p.get("card") == a["gate_card"]]
        self.assertEqual([k for k, _ in kinds][-2:], ["kb_comment", "kb_complete"])
        stamp = h.cards[a["gate_card"]]["comments"][-1]["body"]
        self.assertEqual(stamp, "CLICKUP-GATE v1 audit=valid task=%s rev=1 sha=%s epoch=1 input_gen=%d"
                         % (T, SHA1, a["ticket_input_gen"]))
        self.assertTrue(h.verdict().valid)

    def test_a_scope_input_after_gate_before_promotion(self):
        h = H(self)
        h.ticketed()
        h.post("m", REQ, "wait, one more thing", mention=True)  # observe_commit revokes
        self.assertEqual(str(h.verdict()), "INVALID new_input")
        h.settle()
        h.cycle()
        self.assertEqual(h.task()["phase"], "promotion_held")
        self.assertNotIn("ready for deployment", h.statuses)
        self.assertIn("Promotion of r1 to dev is held: new input is being reviewed",
                      [p["text"] for k, p in h.effects if k == "cu_comment"])
        a = h.approvals()[0]
        self.assertIn("CLICKUP-GATE v1 audit=revoked task=%s rev=1 epoch=1 reason=new_input" % T,
                      [c["body"] for c in h.cards[a["gate_card"]]["comments"]])

    def test_b_scope_input_while_promotion_runs(self):
        h = H(self)
        fe, promo = h.ticketed()
        h.cards[promo]["task"]["status"] = "running"
        h.describe()
        self.assertEqual(str(h.verdict()), "INVALID new_input")
        h.done(promo, sha=PROMOTED)  # a card that pushed anyway would have failed its check; nothing is undone
        self.assertFalse(h.verdict().valid)

    def test_c_old_history_not_yet_reached_when_final_e2e_finishes(self):
        h = H(self)
        fe, promo = h.developing()
        h.clock.advance(30)
        h.done(fe)
        h.light()  # only an L after the done time; the last complete F started before it
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual((a["state"], a["ticket_epoch"]), ("implemented", 0))
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")
        h.post("old", REQ, "old thread question", parent="root0", mention=True)  # found by the next F
        h.settle()
        h.cycle()
        self.assertEqual(h.approvals()[0]["ticket_epoch"], 0)
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")

    def test_d_amendment_after_promotion_committed(self):
        h = H(self)
        fe, promo = h.ticketed()
        h.done(promo, sha=PROMOTED)
        h.cycle()
        h.post("m", REQ, "follow-on idea", mention=True)
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "material")
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual(a["state"], "promoted")
        self.assertIsNone(a["revoked_reason"])
        self.assertEqual(h.statuses[-1], "ready for deployment")

    def test_e_poller_stopped_lease_expires(self):
        h = H(self)
        h.ticketed()
        h.clock.advance(1500)
        self.assertTrue(h.verdict().valid)
        h.clock.advance(ms=1)
        self.assertEqual(str(h.verdict()), "INVALID lease_expired")

    def test_renewal_is_barrier_gated_and_local(self):
        h = H(self)
        h.ticketed()
        a = h.approvals()[0]
        h.clock.advance(601)
        h.cycle()  # no F after the previous renewal: not renewed
        self.assertEqual(h.approvals()[0]["ticket_renewed_ms"], a["ticket_renewed_ms"])
        n = len(h.effects)
        h.settle()
        h.cycle()
        self.assertGreater(h.approvals()[0]["ticket_renewed_ms"], a["ticket_renewed_ms"])
        self.assertEqual(len(h.effects), n)
        h.clock.advance(1400)
        self.assertTrue(h.verdict().valid)

    def test_f_revoke_then_answer_only_new_epoch_and_blocked_promotion_alert(self):
        h = H(self)
        fe, promo = h.ticketed()
        h.post("m", REQ, "quick question", mention=True)
        h.settle()
        h.cycle()
        h.cards[promo]["task"]["status"] = "blocked"  # it ran its check and blocked itself
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2, "answer_only")
        h.settle()
        h.cycle()
        a = h.approvals()[0]
        self.assertEqual((a["state"], a["ticket_epoch"]), ("ticketed", 2))
        self.assertTrue(h.verdict().valid)
        self.assertEqual(h.task()["phase"], "promotion_window")
        self.assertTrue(any("ticket valid again; promotion card %s is blocked" % promo in x for x in h.alerts))
        self.assertNotIn(("kb_unblock", promo), [(k, p.get("card")) for k, p in h.effects])

    def test_gate_completion_retry_waits_for_new_quiet_and_barrier(self):
        """QSR F2: a gate completion that failed definitely is retried only after the current quiet expired and
        the barrier anchored at final_e2e holds again; it then completes exactly once (§4.4, §8.5, §10.1)."""
        h = H(self)
        fe, promo = h.developing()
        h.done(fe)
        h.settle()
        a = h.approvals()[0]
        gk = "clickup-dp:%s:gate-complete:a%d:e1" % (T, a["id"])
        h.fail[gk] = "retry_wait"
        h.scan()
        h.execute()  # ticket issued, valid stamp posted, gate completion failed definitely
        self.assertEqual(h._state(gk), "retry_wait")
        gen = h.task()["input_gen"]
        h.post("u", OTHER, "an unrelated note")  # unaddressed: restarts quiet, input_gen unchanged
        h.light()
        h.cycle()
        h.light()
        h.cycle()
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "blocked")
        self.assertEqual(h._state(gk), "retry_wait")
        self.assertEqual(h.task()["input_gen"], gen)
        h.settle()
        h.cycle()
        self.assertEqual(h.cards[a["gate_card"]]["task"]["status"], "done")
        self.assertEqual(len(h.eff("kb_complete")), 1)
        self.assertTrue(h.verdict().valid)

    def test_revocation_stamp_is_never_deferred_by_quiet(self):
        h = H(self)
        h.ticketed()
        a = h.approvals()[0]
        h.post("m", REQ, "addressed", mention=True)  # quiet open; revocation committed by observe_commit
        h.cycle()
        self.assertIn("CLICKUP-GATE v1 audit=revoked task=%s rev=1 epoch=1 reason=new_input" % T,
                      [c["body"] for c in h.cards[a["gate_card"]]["comments"]])

    def test_g_queued_stamp_or_issue_with_newer_input_gen_is_cancelled(self):
        h = H(self)
        fe, promo = h.developing()
        h.done(fe)
        h.settle()
        new = h.scan()  # ticket issued; stamp and gate completion queued, not yet sent
        stamp = [i for i in new if i.kind == "kb_comment"][0].idem_key
        h.describe()  # newer input_gen observed before the executor runs
        h.execute()
        rows = {r["idem_key"]: r["state"] for r in h.rows()}
        self.assertEqual(rows[stamp], "cancelled_stale")
        self.assertEqual([r["state"] for r in h.rows("kb_complete")], ["cancelled_stale"])
        self.assertEqual(h.cards[h.approvals()[0]["gate_card"]]["task"]["status"], "blocked")

    def test_h_revocation_stamp_fails_ticket_already_invalid(self):
        for state in ("failed", "held_ambiguous"):
            with self.subTest(state=state):
                h = H(self)
                h.ticketed()
                a = h.approvals()[0]
                h.fail["clickup-dp:%s:stamp:a%d:e1:revoked" % (T, a["id"])] = state
                h.post("m", REQ, "addressed", mention=True)
                h.cycle()
                v = h.verdict()
                self.assertEqual(str(v), "INVALID new_input")
                self.assertTrue(any("audit stamp" in x for x in h.alerts))

    def test_i_kill_after_observe_commit_before_engine(self):
        h = H(self)
        h.ticketed()
        h.describe()  # committed; engine/outbox never ran
        self.assertEqual(str(h.verdict()), "INVALID new_input")
        h.store.close()
        h.store = Store(h.path, h.clock)
        self.addCleanup(h.store.close)
        self.assertEqual(str(h.verdict()), "INVALID new_input")

    def test_j_stale_renewal_and_delayed_valid_stamp_after_revocation(self):
        h = H(self)
        h.ticketed()
        a = h.approvals()[0]
        h.post("m", REQ, "addressed", mention=True)
        self.assertFalse(h.store.renew_ticket(a["id"], 1, h.now))
        # a delayed epoch-1 valid stamp row reaching the executor now is stale
        key = "clickup-dp:%s:stamp:a%d:e1:valid" % (T, a["id"])
        h.store.conn.execute("UPDATE outbox SET state='pending' WHERE idem_key=?", (key,))
        h.execute()
        self.assertEqual({r["idem_key"]: r["state"] for r in h.rows()}[key], "cancelled_stale")
        self.assertEqual(str(h.verdict()), "INVALID new_input")
        card2 = None
        h.settle()
        h.cycle()
        card2 = h.request_cards()[-1]
        h.cards[card2]["task"]["status"] = "running"
        h.classify(card2)
        h.settle()
        h.cycle()
        self.assertEqual(h.approvals()[0]["ticket_epoch"], 2)

    def test_k_terminal_hold_and_material_each_invalid_without_writes(self):
        h = H(self)
        h.ticketed()
        h.fail = {"*": "failed"}
        h.store.observe_commit(T, [], {"terminal": 1, "phase": "stopped"})
        self.assertEqual(str(h.verdict()), "INVALID terminal")

        h = H(self)
        h.ticketed()
        with h.store.transaction() as c:  # the S4 hold subcommand's transaction
            c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id=?", (T,))
            revoke_live_tickets(c, T, "operator", h.now)
        self.assertEqual(str(h.verdict()), "INVALID operator")

        h = H(self)
        fe, promo = h.ticketed()
        rel_card = h.request_cards()[0]
        with h.store.transaction() as c:  # a relay to classify, so the classification transaction runs
            c.execute("INSERT INTO relays(relay_ref, task, card, gen, item_ids, state) VALUES "
                      "('r-x', ?, ?, ?, '[]', 'delivered')", (T, rel_card, h.task()["input_gen"]))
        h.cards[rel_card]["task"]["status"] = "running"
        n = len(h.effects)
        h.classify(rel_card, "material", ref="r-x")
        h.scan()  # engine commits the classification; nothing executed yet
        self.assertEqual(str(h.verdict()), "INVALID material")
        self.assertEqual(len(h.effects), n)


class TerminalTest(unittest.TestCase):
    def test_terminal_archives_parked_request_card_only(self):
        h = H(self)
        card = h.picked_up()
        h.park(card)
        h.store.observe_commit(T, [], {"terminal": 1, "phase": "deployed"})
        h.cycle()
        self.assertEqual(h.cards[card]["task"]["status"], "archived")
        self.assertEqual([k for k, _ in h.effects if k.startswith("cu_")], [])


class PreconditionsTest(unittest.TestCase):
    def test_preconditions(self):
        t = {"phase": "awaiting_approval", "scope_gen": 1, "input_gen": 2, "published_rev": 1, "terminal": 0}
        apps = {7: {"state": "ticketed"}}
        P = Preconditions
        ok = rules.preconditions_ok
        self.assertTrue(ok(t, apps, P(T, list_id="L", phases=("awaiting_approval",), input_gen=2), "cu_comment",
                           None, "L"))
        self.assertFalse(ok(t, apps, P(T, list_id="L"), "cu_comment", None, None))
        self.assertFalse(ok(t, apps, P(T, input_gen=3), "kb_comment"))
        self.assertFalse(ok(t, apps, P(T, approval_id=7, approval_states=("revoked",)), "kb_comment"))
        self.assertFalse(ok(dict(t, terminal=1), apps, P(T), "cu_reply"))
        self.assertTrue(ok(dict(t, terminal=1), apps, P(T), "kb_archive"))
        self.assertTrue(ok(dict(t, terminal=1), apps, P(T), "kb_comment"))  # protective (audit stamp)
        live = P(T, phases=rules.LIVE_PHASES)
        self.assertTrue(ok(t, apps, live, "kb_create"))
        self.assertFalse(ok(dict(t, terminal=1), apps, live, "kb_create"))
        self.assertFalse(ok(dict(t, phase="not_admitted"), apps, live, "kb_comment"))
        card = {"task": {"status": "scheduled"}, "runs": [{"id": 4, "outcome": "scheduled"}]}
        self.assertTrue(ok(t, apps, P(T, wait_run_id=4), "kb_unblock", card))
        self.assertFalse(ok(t, apps, P(T, wait_run_id=3), "kb_unblock", card))
        self.assertFalse(ok(None, apps, P(T), "kb_comment"))

    def test_send_barrier_defers_quiet_expired_rows_only(self):
        """QSR F2: §4.4 at send/retry time; protective (untagged) rows never wait."""
        t = {"quiet_until_ms": 1000, "full_started_ms": 2000, "full_cursor": None, "last_light_ms": 5000,
             "last_full_ms": 2500}
        go = rules.send_barrier_ok
        self.assertTrue(go(t, {"barrier_anchor_ms": 1500}, 6000, 5000))
        self.assertFalse(go(dict(t, quiet_until_ms=7000), {"barrier_anchor_ms": 0}, 6000, 5000))  # fresh quiet
        self.assertFalse(go(t, {"barrier_anchor_ms": 2100}, 6000, 5000))  # no F started after the anchor
        self.assertFalse(go(t, {"barrier_anchor_ms": 0}, 6000, 5500))  # no L in this scan
        self.assertFalse(go(None, {"barrier_anchor_ms": 0}, 6000, 5000))
        self.assertTrue(go(dict(t, quiet_until_ms=7000), {"text": "audit=revoked"}, 6000, 5000))


if __name__ == "__main__":
    unittest.main()
