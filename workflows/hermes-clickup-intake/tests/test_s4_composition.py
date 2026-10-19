"""S4 composition tests: T9, T18, T19 (plan r5 §4.1, §8, §10, §11, §13.1).

The real ``main`` entry runs against the S1 ``FakeClickUp`` transport, an in-memory Kanban board that keeps
``show --json`` shapes (runs, comments, attachments with stored files, parents, subscriptions) and a fake
clock. Workers and humans are simulated between scans. T9 injects a crash after every external write and
before every state commit (each distinct point once) and reruns the scan until it completes.
"""

import contextlib
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from clickup_intake import kanban_port as kp
from clickup_intake import main as glue_main
from clickup_intake import render
from clickup_intake.contracts import KanbanResult, Outcome
from clickup_intake.store import Store
from fakes.fake_clickup import CORRECT_STATUSES, LIST_ID, FakeClickUp
from fakes.fake_clock import FakeClock

TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
BOT, APPROVER_A, APPROVER_B, REQ = 900000001, 900000101, 900000102, 555001
CHAT = "111111111111111111"
PROMOTED = "f" * 40
PLAN_R1 = "## ClickUp summary\nDark mode for the panel settings page.\n\n## Plan\nAdd a theme toggle.\n"


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _read(path):
    with open(path, "rb") as f:
        return f.read()


# ------------------------------------------------------------------------------------------ crashes
class Crash(BaseException):
    """Simulated process death: not an Exception, so nothing in the glue swallows it."""


class Injector:
    """Crash once at the first occurrence of each distinct point while ``active``."""

    def __init__(self):
        self.active, self.seen, self.crashes = False, set(), 0

    def point(self, sig):
        if self.active and sig not in self.seen:
            self.seen.add(sig)
            self.crashes += 1
            raise Crash(sig)


class CrashingTransport:
    def __init__(self, inner, inj):
        self.inner, self.inj = inner, inj

    def request(self, method, path, params=None, body=None, multipart=None):
        r = self.inner.request(method, path, params=params, body=body, multipart=multipart)
        if method != "GET":
            self.inj.point(("cu", method, path, json.dumps(body, sort_keys=True), (multipart or {}).get("filename")))
        return r


def crash_store(inj):
    class CrashStore(Store):
        """Crash before COMMIT of every distinct write transaction (rolled back by ``Store.transaction``)."""

        def __init__(self, *a, **kw):
            self._stmts = []
            super().__init__(*a, **kw)
            self.conn.set_trace_callback(self._stmts.append)

        @contextlib.contextmanager
        def transaction(self):
            with Store.transaction(self) as c:
                del self._stmts[:]
                yield c
                writes = [s for s in self._stmts if not s.lstrip().upper().startswith("SELECT")]
                if writes:  # timestamps normalized: one crash per logical commit point, not per instant
                    inj.point(("commit", _sha(re.sub(r"\d{10,}", "#", "\n".join(writes)))))
    return CrashStore


# ------------------------------------------------------------------------------------------ board
class FakeBoard:
    """Kanban board with the ``show --json`` / ``attachments --json`` / ``notify-list --json`` shapes the glue reads."""

    def __init__(self, root, clock, inj=None):
        self.root, self.clock, self.inj = root, clock, inj or Injector()
        os.makedirs(root, exist_ok=True)
        self.cards, self.keys, self.calls = {}, {}, []
        self.discord_chat = CHAT
        self.on_spawn = None
        self._n, self._run = 0, 0

    # -- helpers
    def _now(self):
        return self.clock() // 1000

    def _new(self, title, body="", assignee=None, status="blocked", parents=(), skills=(), key=None):
        self._n += 1
        cid = "t_%08x" % (0xc0de0000 + self._n)
        self.cards[cid] = {"task": {"id": cid, "title": title, "body": body, "assignee": assignee, "status": status,
                                    "skills": list(skills), "created_by": "clickup-intake", "completed_at": None,
                                    "key": key},
                           "parents": list(parents), "comments": [], "runs": [], "events": [], "atts": [], "subs": []}
        return cid

    def _event(self, cid, kind, **payload):
        self.cards[cid]["events"].append({"kind": kind, "payload": payload, "created_at": self._now()})

    def _regate(self):
        for c in self.cards.values():
            if c["task"]["status"] == "todo" and all(
                    self.cards[p]["task"]["status"] in ("done", "archived") for p in c["parents"]):
                c["task"]["status"] = "ready"

    def _mut(self, name, cid, *sig):
        self.calls.append((name, cid))
        self.inj.point(("kb", name, cid) + sig)

    def mutations(self):
        return [c for c in self.calls if c[0] not in ("show", "attachments", "subscriptions")]

    # -- KanbanPort
    def create(self, title, *, body_file, idempotency_key, assignee=None, skills=(), initial_status="blocked"):
        if idempotency_key not in self.keys:
            with open(body_file, encoding="utf-8") as f:
                body = f.read()
            self.keys[idempotency_key] = self._new(title, body, assignee, initial_status, (), skills, idempotency_key)
        cid = self.keys[idempotency_key]
        self._mut("create", cid, idempotency_key)
        return KanbanResult(Outcome.OK, dict(self.cards[cid]["task"]))

    def comment(self, card, body):
        self.cards[card]["comments"].append({"author": "clickup-intake", "body": body.strip(),
                                             "created_at": self._now()})
        self._mut("comment", card, _sha(body))
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def attach(self, card, path, name):
        with open(path, "rb") as f:
            data = f.read()
        stored = os.path.join(self.root, "%s-%d-%s" % (card, len(self.cards[card]["atts"]), name))
        with open(stored, "wb") as f:
            f.write(data)
        self.cards[card]["atts"].append({"id": len(self.cards[card]["atts"]) + 1, "filename": name, "size": len(data),
                                         "uploaded_by": "clickup-intake", "stored_path": stored})
        self._mut("attach", card, name, _sha(data))
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def attachments(self, card):
        self.calls.append(("attachments", card))
        return KanbanResult(Outcome.OK, [dict(a) for a in self.cards[card]["atts"]])

    def show(self, card):
        self.calls.append(("show", card))
        c = self.cards.get(card)
        if c is None:
            return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "no such task"})
        out = {k: json.loads(json.dumps(c[k])) for k in ("task", "parents", "comments", "runs", "events")}
        out["children"] = [i for i, x in self.cards.items() if card in x["parents"]]
        return KanbanResult(Outcome.OK, out)

    def unblock(self, card):
        t = self.cards[card]["task"]
        if t["status"] not in ("blocked", "scheduled"):
            return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "not blocked"})
        t["status"] = "todo"
        self._event(card, "unblocked")
        self._regate()
        self._mut("unblock", card)
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def complete(self, card, summary, metadata=None):
        self.cards[card]["task"].update(status="done", completed_at=self._now())
        self._event(card, "completed", summary=summary)
        self._regate()
        self._mut("complete", card)
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def archive(self, card):
        self.cards[card]["task"]["status"] = "archived"
        self._regate()
        self._mut("archive", card)
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def subscribe(self, card):
        sub = {"platform": "discord", "chat_id": CHAT, "chat_type": "group", "notifier_profile": "default",
               "delivery_mode": "notify", "delivery_metadata": {"guild_id": kp.DISCORD_GUILD_ID}}
        if sub not in self.cards[card]["subs"]:
            self.cards[card]["subs"].append(sub)
        self._mut("subscribe", card)
        return KanbanResult(Outcome.OK, {"stdout": ""})

    def subscriptions(self, card):
        self.calls.append(("subscriptions", card))
        return KanbanResult(Outcome.OK, list(self.cards[card]["subs"]))

    # -- workers (not glue calls)
    def claim(self, card, profile):
        self._run += 1
        self.cards[card]["task"]["status"] = "running"
        self.cards[card]["runs"].append({"id": self._run, "profile": profile, "status": "running", "outcome": None,
                                         "metadata": None, "started_at": self._now()})
        return self._run

    def _end(self, card, outcome, metadata=None):
        self.cards[card]["runs"][-1].update(status="done", outcome=outcome, metadata=metadata)

    def worker_comment(self, card, author, body):
        self.cards[card]["comments"].append({"author": author, "body": body, "created_at": self._now()})

    def park(self, card, *outputs):
        for body in outputs:
            self.worker_comment(card, "planner", body)
        self._end(card, "scheduled")
        self.cards[card]["task"]["status"] = "scheduled"
        self._event(card, "scheduled", run_id=self.cards[card]["runs"][-1]["id"])

    def request_review(self, card, task, rev, text, accounts_gen, *outputs):
        for body in outputs:
            self.worker_comment(card, "planner", body)
        name = "clickup-plan-r%d.md" % rev
        stored = os.path.join(self.root, "%s-planner-%s" % (card, name))
        with open(stored, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.cards[card]["atts"].append({"id": len(self.cards[card]["atts"]) + 1, "filename": name,
                                         "size": len(text.encode()), "uploaded_by": "planner", "stored_path": stored})
        self._end(card, "review_requested", {"clickup_task": task, "plan_revision": rev, "plan_attachment": name,
                                             "plan_sha256": _sha(text), "accounts_gen": accounts_gen})
        self.cards[card]["task"]["status"] = "review"
        return _sha(text)

    def finish(self, card, profile, metadata=None):
        if self.cards[card]["task"]["status"] != "running":
            self.claim(card, profile)
        self._end(card, "completed", metadata)
        self.cards[card]["task"].update(status="done", completed_at=self._now())
        self._regate()

    def worker_create(self, title, parents=(), body="", assignee="implementor"):
        cid = self._new(title, body, assignee, "todo", parents)
        self._regate()
        return cid

    # -- readers
    def relays(self, card):
        return [c["body"] for c in self.cards[card]["comments"] if c["body"].startswith("CLICKUP-IN v1 ")]

    def by_key(self, prefix):
        return [cid for key, cid in self.keys.items() if key.startswith(prefix)]


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """Token-free attachment downloads (§8.2); records URLs and headers."""

    def __init__(self):
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(b"attachment bytes")


# ------------------------------------------------------------------------------------------ world
class World:
    def __init__(self, tc, crash=False, **cfg):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.tc = tc
        self.clock = FakeClock()
        self.inj = Injector()
        self.cu = FakeClickUp(self.clock)
        self.transport = CrashingTransport(self.cu, self.inj)
        self.board = FakeBoard(os.path.join(self.dir, "board"), self.clock, self.inj)
        self.opener = FakeOpener()
        self.root = os.path.join(self.dir, "hermes")
        self.runtime = os.path.join(self.root, "integrations", "clickup-dedicated-panel")
        self.cfg = dict({"runtime_dir": self.runtime, "enabled": True, "mode": "live",
                         "cutover_ms": self.clock() - 60_000}, **cfg)
        self.write_config()
        self.crash = crash
        self.outputs = []
        self.scans = 0
        if crash:
            p = mock.patch.object(glue_main, "Store", crash_store(self.inj))
            p.start()
            tc.addCleanup(p.stop)

    def write_config(self, **over):
        self.cfg.update(over)
        os.makedirs(self.runtime, exist_ok=True)
        with open(os.path.join(self.runtime, "config.json"), "w", encoding="utf-8") as f:
            json.dump(self.cfg, f)

    @property
    def state_path(self):
        return os.path.join(self.runtime, "state.sqlite3")

    def main(self, *argv):
        """One process run; crashes (T9) rerun the same command at the same instant until it completes."""
        while True:
            buf = io.StringIO()
            self.inj.active = self.crash
            try:
                rc = glue_main.main(list(argv), root=self.root, transport=self.transport, port=self.board,
                                    clock=self.clock, opener=self.opener, out=buf)
            except Crash:
                continue
            finally:
                self.inj.active = False
            lines = buf.getvalue().splitlines()
            self.outputs.extend(lines)
            return rc, lines

    def run(self, *extra):
        self.scans += 1
        return self.main("run", *extra)

    def q(self, sql, *args):
        conn = Store(self.state_path, self.clock).conn
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def task(self, tid):
        return self.q("SELECT * FROM tasks WHERE clickup_task_id=?", tid)[0]

    def bot_comments(self, tid):
        out = list(self.cu.comments.get(tid, []))
        for c in list(out):
            out += self.cu.replies.get(c["id"], [])
        return [c for c in out if c["user"]["id"] == BOT]

    def statuses_written(self):
        return [b["status"] for (m, p, _), b in zip(self.cu.calls, self.cu.bodies) if m == "PUT" and b]

    def step(self, until, limit=60, seconds=61, world=None):
        """Advance one minute per scan; ``world()`` simulates humans/workers; stop when ``until()``."""
        for _ in range(limit * (6 if self.crash else 1)):  # crashed reruns defer writes to the next confirming L
            self.clock.advance(seconds)
            self.run()
            if world:
                world()
            if until():
                return
        self.tc.fail("condition not reached; outputs=%r" % self.outputs[-10:])


# ------------------------------------------------------------------------------------------ planner
def relay_of(board, card):
    line = board.relays(card)[-1].splitlines()[0]
    m = re.fullmatch(r"CLICKUP-IN v1 relay=(\S+) gen=(\d+)", line)
    return m.group(1), int(m.group(2))


def out(kind, ref, text="", **extra):
    head = "CLICKUP-OUT v1 kind=%s relay=%s" % (kind, ref) + "".join(" %s=%s" % kv for kv in extra.items())
    return head + ("\n" + text if text else "")


def classify(ref, gen, result="answer_only"):
    return out("classification", ref, gen=gen, result=result)


class Lifecycle:
    """T19 offline lifecycle: pickup -> business -> technical -> PASS -> publish -> approve -> queued -> started
    -> implemented -> ticketed -> promoted / ready for deployment."""

    def __init__(self, w):
        self.w, self.b, self.cu = w, w.board, w.cu
        self.tid = self.cu.add_task(name="Dark mode", description="Please add dark mode.", creator=REQ)
        self.cu.add_attachment(self.tid, REQ, title="mock.png", size=16)
        self.planner_turns = 0
        self.plan_sha = None

    def req_cards(self):
        return sorted(self.b.by_key("clickup-dp:%s:req:" % self.tid), key=lambda c: self.b.cards[c]["task"]["key"])

    def phase(self):
        return self.w.task(self.tid)["phase"]

    def planner(self, script):
        """Ready request card -> claim, run ``script(card, ref, gen)`` (it parks or requests review)."""
        def act():
            for card in self.req_cards():
                if self.b.cards[card]["task"]["status"] == "ready":
                    self.b.claim(card, "planner")
                    ref, gen = relay_of(self.b, card)
                    self.planner_turns += 1
                    script(card, ref, gen)
        return act

    def pickup_to_business(self):
        def script(card, ref, gen):
            self.b.park(card, out("business_questions", ref, "Which users need dark mode?"), classify(ref, gen))
        self.w.step(lambda: self.phase() == "awaiting_business" and self.bot_q("Which users"),
                    world=self.planner(script))

    def say(self, user, text, **kw):
        """A human comment a few seconds after the latest observation."""
        self.w.clock.advance(5)
        return self.cu.add_comment(self.tid, user, text, **kw)

    def bot_q(self, text):
        return any(text in c["comment_text"] for c in self.w.bot_comments(self.tid))

    def business_to_technical(self):
        self.say(REQ, "All panel users.")
        self.say(REQ, "Business requirements closed")

        def script(card, ref, gen):
            self.b.park(card, out("technical_questions", ref, "Should it follow the OS theme?"), classify(ref, gen))
        self.w.step(lambda: self.phase() == "awaiting_technical" and self.bot_q("OS theme"),
                    world=self.planner(script))

    def technical_to_published(self):
        self.say(APPROVER_A, "Yes, follow the OS theme.")
        self.say(APPROVER_A, "Technical requirements closed")

        def script(card, ref, gen):
            self.b.worker_comment(card, "planner", classify(ref, gen))
            self.plan_sha = self.b.request_review(card, self.tid, 1, PLAN_R1, gen)
            self.b.claim(card, "plan-reviewer")
            self.b.finish(card, "plan-reviewer")
        self.w.step(lambda: self.phase() == "awaiting_approval" and self.bot_q("Plan r1, SHA-256"),
                    world=self.planner(script))

    def approve_to_released(self):
        self.say(APPROVER_A, "Approve plan r1 for development")
        self.w.step(lambda: self.orch() and self.b.cards[self.orch()]["task"]["status"] == "ready")

    def orch(self):
        ids = self.b.by_key("clickup-dp:%s:orch:" % self.tid)
        return ids[0] if ids else None

    def gate(self):
        ids = self.b.by_key("clickup-dp:%s:gate:" % self.tid)
        return ids[0] if ids else None

    def orchestrate(self):
        orch, gate = self.orch(), self.gate()
        self.b.claim(orch, "orchestrator")
        fe = self.b.worker_create("final E2E r1")
        promo = self.b.worker_create("dev promotion r1", parents=[gate], assignee="integrator",
                                     body="Promote.\n\n" + render.promotion_block(gate, self.tid, 1, self.plan_sha))
        cleanup = self.b.worker_create("cleanup r1", parents=[promo])
        self.b.finish(orch, "orchestrator", {"clickup_gates": {
            "plan_revision": 1, "plan_sha256": self.plan_sha, "final_e2e": fe, "promotion": promo,
            "cleanup": cleanup, "lineage": [fe, promo, cleanup]}})
        self.fe, self.promo = fe, promo
        self.w.step(lambda: self.phase() == "development" and self.bot_q("Development started for r1"))

    def e2e_done_to_ticketed(self):
        self.b.finish(self.fe, "e2e-tester")
        self.w.step(lambda: self.phase() == "promotion_window"
                    and self.b.cards[self.promo]["task"]["status"] == "ready", limit=40)

    def ticket_check(self):
        return self.w.main("ticket-check", "--gate", self.gate(), "--task", self.tid, "--rev", "1",
                           "--sha", self.plan_sha)

    def promote(self):
        self.b.finish(self.promo, "integrator", {"promoted_sha": PROMOTED})
        self.w.step(lambda: self.phase() == "ready_for_deployment" and "ready for deployment"
                    in self.w.statuses_written())

    def full(self):
        self.pickup_to_business()
        self.business_to_technical()
        self.technical_to_published()
        self.approve_to_released()
        self.orchestrate()
        self.e2e_done_to_ticketed()
        rc, lines = self.ticket_check()
        self.w.tc.assertEqual((rc, lines), (0, ["VALID epoch=1"]))
        self.promote()


def operator_lines(w):
    """Run output other than ``ticket-check`` verdicts: operator alerts and notices."""
    return [x for x in w.outputs if not x.startswith(("VALID ", "INVALID "))]


EXPECTED_STATUSES = ["business requirements", "technical requirements", "waiting for approval", "implementing",
                     "implemented", "ready for deployment"]


def assert_no_duplicates(tc, w, life):
    """No duplicate card/comment/reply/status/approval/handoff/stamp (T9)."""
    b = w.board
    tc.assertEqual(w.statuses_written(), EXPECTED_STATUSES)
    refs = [re.search(r"ref: cu-[0-9a-f]{12}", c["comment_text"]).group(0) for c in w.bot_comments(life.tid)]
    tc.assertEqual(len(refs), len(set(refs)), refs)
    texts = [c["comment_text"].rsplit("\n\nref:", 1)[0] for c in w.bot_comments(life.tid)]
    tc.assertEqual(len(texts), len(set(texts)), texts)
    uploads = [a["title"] for a in w.cu.tasks[life.tid]["attachments"] if a["user"]["id"] == BOT]
    tc.assertEqual(len(uploads), 1, uploads)
    tc.assertEqual(len(life.req_cards()), 1)
    tc.assertEqual(len(b.by_key("clickup-dp:%s:gate:" % life.tid)), 1)
    tc.assertEqual(len(b.by_key("clickup-dp:%s:orch:" % life.tid)), 1)
    for cid, c in b.cards.items():
        own = [x["body"] for x in c["comments"] if x["author"] == "clickup-intake"]
        tc.assertEqual(len(own), len(set(own)), (cid, own))
        atts = [(a["filename"], _sha(_read(a["stored_path"]))) for a in c["atts"]]
        tc.assertEqual(len(atts), len(set(atts)), (cid, atts))
        tc.assertLessEqual(len(c["subs"]), 1)
    stamps = [x["body"] for x in b.cards[life.gate()]["comments"] if x["body"].startswith("CLICKUP-GATE v1")]
    tc.assertEqual(len(stamps), 1, stamps)
    tc.assertEqual(sum(1 for c, k in b.calls if c == "unblock" and k == life.orch()), 1)
    req = life.req_cards()[0]
    tc.assertEqual(sum(1 for c, k in b.calls if c == "unblock" and k == req), len(b.relays(req)))
    tc.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[req]["events"]), 3)
    apps = w.q("SELECT * FROM approvals WHERE task=?", life.tid)
    tc.assertEqual([(a["state"], a["promoted_sha"]) for a in apps], [("promoted", PROMOTED)])
    tc.assertEqual(sum(1 for c, k in b.calls if c == "complete" and k == life.gate()), 1)


# ------------------------------------------------------------------------------------------ T19
class T19LifecycleTest(unittest.TestCase):
    def test_offline_lifecycle_pickup_to_ready_for_deployment(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        b, req = w.board, life.req_cards()[0]
        card = b.cards[req]
        self.assertEqual(card["task"]["assignee"], "planner")
        self.assertEqual(card["task"]["skills"], ["plan", "pragmatic-code"])
        self.assertIn("Trusted rules", card["task"]["body"])
        names = [a["filename"] for a in card["atts"]]
        self.assertTrue(any(re.fullmatch(r"clickup-context-g\d+\.md", n) for n in names), names)
        self.assertIn("clickup-ledger.md", names)
        self.assertTrue(any(n.endswith("-mock.png") for n in names))
        for req_ in w.opener.requests:  # attachment staging never carries the token
            self.assertNotIn("Authorization", req_.headers)
        self.assertEqual(len(card["subs"]), 1)
        self.assertTrue(kp.has_passive_subscription(card["subs"], CHAT))
        # Initial release of the blocked never-run request card used port.unblock (not the parked-wait guard).
        self.assertEqual(card["events"][0]["kind"], "unblocked")

        life.business_to_technical()
        life.technical_to_published()
        pub = [c for c in w.bot_comments(life.tid) if "Plan r1, SHA-256" in c["comment_text"]][0]
        self.assertIn("Dark mode for the panel settings page.", pub["comment_text"])
        self.assertIn("Independent review passed; this is NOT approval.", pub["comment_text"])
        upload = [a for a in w.cu.tasks[life.tid]["attachments"] if a["user"]["id"] == BOT][0]
        self.assertRegex(upload["title"], r"^plan-r1-%s-[0-9a-f]{6}\.md$" % life.plan_sha[:12])
        self.assertEqual(upload["size"], len(PLAN_R1.encode()))

        life.approve_to_released()
        self.assertTrue(any("queued for development; not started" in c["comment_text"]
                            for c in w.bot_comments(life.tid)))
        gate, orch = life.gate(), life.orch()
        self.assertEqual((b.cards[gate]["task"]["status"], b.cards[gate]["task"]["assignee"]), ("blocked", None))
        # §8.2 on every card: the generation-bound context snapshot and the ledger, one file each, read back.
        ctx = "clickup-context-g%d.md" % w.task(life.tid)["input_gen"]
        ledger = "clickup-ledger-g%d.md" % w.task(life.tid)["input_gen"]
        self.assertEqual(sorted(a["filename"] for a in b.cards[orch]["atts"]),
                         ["clickup-approval-r1.md", ctx, ledger, "clickup-plan-r1.md"])
        self.assertEqual(sorted(a["filename"] for a in b.cards[gate]["atts"]), [ctx, ledger])
        for card in (orch, gate):
            files = {a["filename"]: _read(a["stored_path"]).decode("utf-8") for a in b.cards[card]["atts"]}
            self.assertIn("Approve plan r1 for development", files[ctx])  # complete history, approval included
            self.assertIn("UNTRUSTED-CLICKUP-DATA", files[ctx])
            self.assertIn("| r1 | %s |" % life.plan_sha, files[ledger])
            self.assertIn("Approver One", files[ledger])
        with open([a for a in b.cards[orch]["atts"] if a["filename"] == "clickup-plan-r1.md"][0]["stored_path"],
                  "rb") as f:
            self.assertEqual(_sha(f.read()), life.plan_sha)
        self.assertIn(render.ticket_marker(gate, life.tid, 1, life.plan_sha), b.cards[orch]["task"]["body"])
        self.assertTrue(kp.has_passive_subscription(b.cards[orch]["subs"], CHAT))
        self.assertTrue(kp.has_passive_subscription(b.cards[gate]["subs"], CHAT))

        life.orchestrate()
        life.e2e_done_to_ticketed()
        self.assertEqual(b.cards[gate]["task"]["status"], "done")
        self.assertEqual([x["body"].splitlines()[0] for x in b.cards[gate]["comments"]],
                         ["CLICKUP-GATE v1 audit=valid task=%s rev=1 sha=%s epoch=1 input_gen=%d"
                          % (life.tid, life.plan_sha, w.task(life.tid)["input_gen"])])
        self.assertEqual(life.ticket_check(), (0, ["VALID epoch=1"]))
        life.promote()
        self.assertEqual(w.statuses_written(), EXPECTED_STATUSES)
        self.assertNotIn("deployed", w.statuses_written())
        assert_no_duplicates(self, w, life)
        # Each written status comes from the verified expected set; no ClickUp write ever left the API paths.
        self.assertTrue(all(re.fullmatch(r"/v2/(task|comment)/[^/]+(/(comment|reply|attachment))?", p)
                            for m, p, _ in w.cu.writes()))
        self.assertEqual(operator_lines(w), [])  # the happy path raises no operator alert

    def test_ticket_lapses_when_the_poller_stops(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        life.approve_to_released()
        life.orchestrate()
        life.e2e_done_to_ticketed()
        self.assertEqual(life.ticket_check(), (0, ["VALID epoch=1"]))
        w.clock.advance(1501)  # no scans: no renewals
        self.assertEqual(life.ticket_check(), (3, ["INVALID lease_expired"]))


# Synthetic completed-run receipt fields with no top-level promoted_sha (PROMOTED-SHA-F1).
RECEIPT_SHA = "ab12cd34ab12cd34ab12cd34ab12cd34ab12cd34"
RECEIPT_MD = {"candidate": {"sha": RECEIPT_SHA, "tree": "2222222222222222222222222222222222222222",
                            "base": "3333333333333333333333333333333333333333",
                            "branch": "feature/example-change"},
              "promoted": {"dev_before": "3333333333333333333333333333333333333333", "dev_after": RECEIPT_SHA,
                           "master": "4444444444444444444444444444444444444444 (unchanged)",
                           "push": "non-force fast-forward"}}


class PromotedShaCompositionTest(unittest.TestCase):
    """PROMOTED-SHA-F1 through the composed scan: future handoff wording and completed-promotion recovery."""

    def ticketed(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        life.approve_to_released()
        life.orchestrate()
        life.e2e_done_to_ticketed()
        return w, life

    def test_orchestrator_body_requires_canonical_promoted_sha_and_structured_receipt_reaches_ready(self):
        w, life = self.ticketed()
        self.assertIn("top-level completion metadata `promoted_sha` set to the full 40-hex SHA",
                      w.board.cards[life.orch()]["task"]["body"])
        w.board.finish(life.promo, "integrator", RECEIPT_MD)
        w.step(lambda: life.phase() == "ready_for_deployment" and "ready for deployment" in w.statuses_written())
        self.assertEqual(w.statuses_written(), EXPECTED_STATUSES)
        apps = w.q("SELECT * FROM approvals WHERE task=?", life.tid)
        self.assertEqual([(a["state"], a["promoted_sha"]) for a in apps], [("promoted", RECEIPT_SHA)])
        self.assertEqual(operator_lines(w), [])

    def test_conflicting_or_narrative_results_leave_the_status_and_alert(self):
        cases = {"canonical_conflict": dict(RECEIPT_MD, promoted_sha="d" * 40),
                 "short_hashes": {"candidate": {"sha": RECEIPT_SHA[:8]}, "promoted": {"dev_after": RECEIPT_SHA[:8]}},
                 "narrative_only": {"summary": "fast-forwarded dev to %s" % RECEIPT_SHA}}
        for how, md in cases.items():
            with self.subTest(how=how):
                w, life = self.ticketed()
                w.board.finish(life.promo, "integrator", md)
                w.step(lambda: any("without a valid promoted_sha" in x for x in operator_lines(w)))
                for _ in range(3):
                    w.clock.advance(61)
                    w.run()
                self.assertEqual(life.phase(), "promotion_window")
                self.assertNotIn("ready for deployment", w.statuses_written())
                self.assertEqual([a["promoted_sha"] for a in w.q("SELECT * FROM approvals WHERE task=?", life.tid)],
                                 [None])


class T19AdmissionAndShadowTest(unittest.TestCase):
    def test_done_type_at_first_sight_creates_nothing(self):
        w = World(self)
        w.cu.ignore_filters = True  # the fake serves it even though the filter should exclude it
        for status in ("implemented", "ready for deployment", "implementing", "deployed"):
            w.cu.add_task(name="late " + status, status=status, creator=REQ)
        for _ in range(8):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.board.cards, {})
        self.assertEqual(w.board.mutations(), [])
        self.assertEqual(w.cu.writes(), [])
        self.assertEqual(w.q("SELECT * FROM tasks"), [])

    def test_moved_to_implemented_during_initial_quiet_is_not_admitted(self):
        w = World(self)
        tid = w.cu.add_task(name="Quick one", creator=REQ)
        w.clock.advance(61)
        w.run()
        self.assertEqual(w.task(tid)["phase"], "new_quiet")
        w.cu.set_status(tid, "implemented")
        for _ in range(8):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.task(tid)["phase"], "not_admitted")
        self.assertEqual(w.board.cards, {})
        self.assertEqual(w.board.mutations(), [])
        self.assertEqual(w.cu.writes(), [])

    def test_shadow_mode_reads_and_decides_but_writes_nothing(self):
        w = World(self, mode="shadow")
        tid = w.cu.add_task(name="Shadowed", creator=REQ)
        lines = []
        for _ in range(8):
            w.clock.advance(61)
            lines += w.run()[1]
        self.assertEqual(w.cu.writes(), [])
        self.assertEqual(w.board.mutations(), [])
        self.assertTrue(lines and all(x.startswith("shadow: preflight OK") for x in lines))
        pending = w.q("SELECT kind, state FROM outbox WHERE task=?", tid)
        self.assertEqual(pending, [{"kind": "kb_create", "state": "pending"}])
        with open(os.path.join(w.runtime, "logs", "intake.jsonl"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn('"event": "would", "kind": "kb_create"', log)
        self.assertNotIn("Shadowed", log)  # ids, kinds and outcomes only
        # --mode shadow overrides a live config the same way.
        w.write_config(mode="live")
        w.clock.advance(61)
        w.run("--mode", "shadow")
        self.assertEqual(w.board.mutations(), [])

    def test_disabled_exits_silently(self):
        w = World(self, enabled=False)
        w.cu.add_task(name="x", creator=REQ)
        w.clock.advance(61)
        self.assertEqual(w.run(), (0, []))
        self.assertEqual(w.cu.calls, [])


MISMATCHES = {
    "old_spelling": [("implimenting", "custom") if n == "implementing" else ("implimented", "done")
                     if n == "implemented" else (n, t) for n, t in CORRECT_STATUSES],
    "retyped": [(n, "custom" if n == "implemented" else t) for n, t in CORRECT_STATUSES],
    "missing": list(CORRECT_STATUSES[:-1]),
    "extra": list(CORRECT_STATUSES) + [("on hold", "custom")],
    "case_or_space": [("Implementing ", t) if n == "implementing" else (n, t) for n, t in CORRECT_STATUSES],
}


class T19PreflightScanTest(unittest.TestCase):
    """Scan-level status-set preflight: one list GET, nothing else, one alert; recovery proceeds normally."""

    def seeded(self):
        w = World(self)
        s = Store(w.state_path, w.clock)
        now = w.clock()
        tasks = {}
        for phase in ("awaiting_business", "development", "promotion_window"):
            tid = w.cu.add_task(name=phase, creator=REQ)
            tasks[phase] = tid
            s.observe_commit(tid, [], {"phase": phase, "eligible": 1, "requester_id": REQ, "content_hash": "c",
                                       "last_full_ms": now, "last_light_ms": now, "full_started_ms": now,
                                       "last_status_seen": "reviewing feature"})
        gate = w.board._new("gate", status="done")
        tid = tasks["promotion_window"]
        with s.transaction() as c:
            c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, gate_card, ticket_epoch,"
                      " ticket_input_gen, ticket_renewed_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                      (tid, 1, 0, "a" * 64, "ticketed", gate, 1, 0, now))
            # A queued notice for a phase the task has since left: the recheck must cancel it, never send it.
            c.execute("INSERT INTO outbox(idem_key, task, kind, target, body, preconditions, depends_on)"
                      " VALUES (?,?,?,?,?,?,?)",
                      ("clickup-dp:q", tasks["development"], "cu_comment", tasks["development"],
                       json.dumps({"text": "queued", "task": tasks["development"]}),
                       json.dumps({"task_id": tasks["development"], "list_id": LIST_ID, "phases": ["planning"]}),
                       "[]"))
        s.close()
        return w, tasks, gate

    def test_each_mismatch_stops_the_scan_before_task_polling(self):
        for name, statuses in MISMATCHES.items():
            with self.subTest(name):
                w, tasks, gate = self.seeded()
                w.cu.statuses[LIST_ID] = [list(p) for p in statuses]
                renewed = w.q("SELECT ticket_renewed_ms FROM approvals")[0]["ticket_renewed_ms"]
                w.clock.advance(61)
                rc, lines = w.run()
                self.assertEqual(rc, 0)
                self.assertEqual(w.cu.calls, [("GET", "/v2/list/%s" % LIST_ID, {})])
                self.assertEqual(w.board.calls, [])
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("status-set preflight MISMATCH", lines[0])
                self.assertNotIn("Dark", lines[0])
                self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key='clickup-dp:q'"), [{"state": "pending"}])
                self.assertEqual(w.q("SELECT ticket_renewed_ms FROM approvals")[0]["ticket_renewed_ms"], renewed)
                # Second run: no repeat alert; shadow and admit stop the same way.
                w.clock.advance(61)
                self.assertEqual(w.run(), (0, []))
                w.clock.advance(61)
                self.assertEqual(w.run("--mode", "shadow"), (0, []))
                self.assertEqual(w.main("admit", "86other"), (1, []))
                self.assertEqual([c[1] for c in w.cu.calls], ["/v2/list/%s" % LIST_ID] * 4)
                self.assertEqual(w.board.calls, [])
                # Poller effectively stopped: past the lease the ticket is INVALID (fail closed).
                w.clock.advance(1500)
                rc, out = w.main("ticket-check", "--gate", gate, "--task", tasks["promotion_window"], "--rev", "1",
                                 "--sha", "a" * 64)
                self.assertEqual((rc, out), (3, ["INVALID lease_expired"]))

    def test_exact_set_in_any_order_recovers_and_rechecks_the_queued_row(self):
        w, tasks, _ = self.seeded()
        w.cu.statuses[LIST_ID] = [list(p) for p in MISMATCHES["old_spelling"]]
        w.clock.advance(61)
        w.run()
        w.cu.statuses[LIST_ID] = [list(p) for p in reversed(CORRECT_STATUSES)]
        w.clock.advance(61)
        rc, lines = w.run()
        self.assertEqual(lines[0], "ClickUp intake: status-set preflight matches again; polling resumed")
        self.assertFalse(any("MISMATCH" in x for x in lines))
        self.assertTrue(any("/v2/list/%s/task" % LIST_ID == c[1] for c in w.cu.calls))  # discovery ran
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key='clickup-dp:q'"), [{"state": "cancelled_stale"}])
        self.assertEqual(w.cu.writes(), [])


# ------------------------------------------------------------------------------------------ T18
class T18ReusableConversationTest(unittest.TestCase):
    def test_three_answer_only_exchanges_after_pass_reuse_one_new_request_card(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        b = w.board
        first = life.req_cards()[0]
        plan_store = [a for a in b.cards[first]["atts"] if a["filename"] == "clickup-plan-r1.md"][0]
        before = _read(plan_store["stored_path"])

        answers = []

        def script(card, ref, gen):
            n = len(answers) + 1
            answers.append(card)
            self.assertIn(life.req_cards()[-1], (card,))
            reply_to = w.q("SELECT id FROM items WHERE task=? AND kind='comment' AND cur_mentions_bot=1"
                           " ORDER BY date_ms DESC LIMIT 1", life.tid)[0]["id"]
            b.park(card, out("answer", ref, "Answer %d." % n, reply_to=reply_to), classify(ref, gen))

        for n in range(1, 4):
            life.say(APPROVER_B, " question %d?" % n, mention=BOT)
            w.step(lambda: len(answers) == n and any("Answer %d." % n in c["comment_text"]
                                                     for c in w.bot_comments(life.tid)),
                   world=life.planner(script))
            for _ in range(5):  # separated exchanges
                w.clock.advance(61)
                w.run()

        cards = life.req_cards()
        self.assertEqual(len(cards), 2)
        second = cards[1]
        self.assertEqual(answers, [second] * 3)
        self.assertEqual(len(b.relays(second)), 3)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[second]["events"]), 3)
        # Each relay carries a refreshed snapshot whose SHA-256 is the attached context file's.
        atts = {a["filename"]: a for a in b.cards[second]["atts"]}
        gens = []
        for body in b.relays(second):
            ctx_line = body.splitlines()[2]
            m = re.match(r"Refreshed snapshot: (clickup-context-g(\d+)\.md) \(attached, SHA-256 ([0-9a-f]{64})\)",
                         ctx_line)
            self.assertIsNotNone(m, ctx_line)
            with open(atts[m.group(1)]["stored_path"], "rb") as f:
                self.assertEqual(_sha(f.read()), m.group(3))
            gens.append(int(m.group(2)))
            self.assertIn("UNTRUSTED-CLICKUP-DATA", body)
        self.assertEqual(len(set(gens)), 3)
        # The plan bytes/hash are present on the new card and unchanged.
        with open(atts["clickup-plan-r1.md"]["stored_path"], "rb") as f:
            self.assertEqual(_sha(f.read()), life.plan_sha)
        self.assertEqual(_read(plan_store["stored_path"]), before)
        self.assertIn("clickup-plan-r1.md", b.cards[second]["task"]["body"])
        t = w.task(life.tid)
        self.assertEqual((t["phase"], t["published_rev"], t["published_sha256"]),
                         ("awaiting_approval", 1, life.plan_sha))
        self.assertEqual(t["classified_gen"], t["input_gen"])
        replies = [c for c in w.bot_comments(life.tid) if c["comment_text"].startswith("Answer ")]
        self.assertEqual(len(replies), 3)
        self.assertTrue(all(c in sum(w.cu.replies.values(), []) for c in replies))  # answers go to the thread
        self.assertEqual(w.statuses_written(), EXPECTED_STATUSES[:3])
        self.assertEqual(operator_lines(w), [])


# ------------------------------------------------------------------------------------------ §9 hold
def feedback_comments(board, card):
    """Outbound-filter feedback the integration posted to a request card (OUTBOUND-PROSE-F1)."""
    return [c["body"] for c in board.cards[card]["comments"]
            if c["author"] == "clickup-intake" and c["body"].startswith("CLICKUP-FILTER v1 ")]


def unblocks(board, card):
    return sum(1 for c, k in board.calls if c == "unblock" and k == card)


class OutboundFilterCompositionTest(unittest.TestCase):
    def test_filtered_planner_text_holds_the_task_and_posts_nothing(self):
        """A planner that keeps producing a filtered draft gets bounded feedback, then a visible hold."""
        w = World(self)
        life = Lifecycle(w)

        def script(card, ref, gen):
            w.board.park(card, out("business_questions", ref, "Use password: hunter2 for the test login?"),
                         classify(ref, gen))
        w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", world=life.planner(script))
        self.assertFalse(any("hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertEqual(w.statuses_written(), [])
        self.assertIn("ClickUp outbound filter hold: task %s (password); nothing was posted" % life.tid, w.outputs)
        self.assertFalse(any("hunter2" in line for line in w.outputs))
        # OUTBOUND-PROSE-F1: the first draft and each corrected draft were rejected; none was ever sent.
        self.assertEqual(w.q("SELECT state, outcome FROM outbox WHERE idem_key LIKE ? AND kind='cu_comment'",
                             "clickup-dp:%s:q:business:%%" % life.tid),
                         [{"state": "failed", "outcome": "FILTERED"}] * 3)
        card = life.req_cards()[0]
        fb = feedback_comments(w.board, card)
        self.assertEqual([x.splitlines()[0].rsplit(" ", 1)[1] for x in fb], ["attempt=1/2", "attempt=2/2"])
        self.assertFalse(any("hunter2" in x for x in fb))
        self.assertEqual(life.planner_turns, 3)
        self.assertTrue(any("no automatic correction left" in line for line in w.outputs), w.outputs)
        before = (len(w.board.mutations()), life.planner_turns)
        for _ in range(4):  # the hold is final until an operator acts: no further feedback or wake
            w.clock.advance(61)
            w.run()
        self.assertEqual((len(w.board.mutations()), life.planner_turns), before)


class OutboundFilterFeedbackTest(unittest.TestCase):
    """OUTBOUND-PROSE-F1 owner correction: a rejected planner draft goes back to the SAME request card with the
    triggered rule (never the matched text); its safe rewrite passes the same filter and is posted."""

    def rewrite(self, crash=False):
        w = World(self, crash=crash)
        life = Lifecycle(w)
        seen = []

        def script(card, ref, gen):
            seen.append(feedback_comments(w.board, card))
            if len(seen) == 1:
                w.board.park(card, out("business_questions", ref, "Use password: hunter2 for the test login?"),
                             classify(ref, gen))
            else:
                w.board.park(card, out("business_questions", ref, "Which test login should we use? Its password"
                                       " stays in the vault."))
        w.step(lambda: life.bot_q("Which test login") and w.statuses_written(), world=life.planner(script))
        return w, life, seen

    def check_rewrite(self, w, life, seen):
        b, card = w.board, life.req_cards()[0]
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], [])
        fb = seen[1]
        self.assertEqual(len(fb), 1)
        ref = relay_of(b, card)[0]
        head = fb[0].splitlines()[0]
        self.assertRegex(head, r"^CLICKUP-FILTER v1 output=[0-9a-f]{12} rule=password attempt=1/2$")
        self.assertIn("CLICKUP-OUT v1 kind=business_questions relay=%s" % ref, fb[0])
        self.assertNotIn("hunter2", fb[0])
        self.assertEqual(feedback_comments(b, card), fb)
        # the safe rewrite passed the same filter and resumed the pending delivery; the task never held
        self.assertTrue(life.bot_q("Which test login should we use?"))
        self.assertFalse(any("hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertEqual(w.task(life.tid)["phase"], "awaiting_business")
        self.assertEqual(w.statuses_written(), ["business requirements"])
        rows = w.q("SELECT state, outcome FROM outbox WHERE idem_key LIKE ? AND kind='cu_comment' ORDER BY rowid",
                   "clickup-dp:%s:q:business:%%" % life.tid)
        self.assertEqual(rows, [{"state": "failed", "outcome": "FILTERED"}, {"state": "done", "outcome": "OK"}])
        # initial release + one feedback wake; the operator sees one line, without the matched text
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 2)
        lines = operator_lines(w)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("outbound filter", lines[0])
        self.assertIn("asked for a corrected draft", lines[0])
        self.assertNotIn("hunter2", "\n".join(w.outputs))
        before = (len(b.mutations()), len(seen))
        for _ in range(4):  # restarts/duplicate observations send nothing twice
            w.clock.advance(61)
            w.run()
        self.assertEqual((len(b.mutations()), len(seen)), before)

    def test_rejected_draft_is_returned_to_the_planner_and_the_safe_rewrite_is_posted(self):
        self.check_rewrite(*self.rewrite())

    def test_crash_at_every_point_sends_one_feedback_and_one_wake(self):
        w, life, seen = self.rewrite(crash=True)
        self.assertGreater(w.inj.crashes, 20)
        self.check_rewrite(w, life, seen)

    def running_draft(self, w, life):
        """The relay for new input wakes the planner; it posts a filtered notice and has not parked yet."""
        life.pickup_to_business()
        card = life.req_cards()[0]
        life.say(APPROVER_B, " which login do we test with?", mention=BOT)

        def script(card_, ref, gen):
            w.board.worker_comment(card_, "planner", out("notice", ref, "The test login is admin, password: Hunter2!"))
            w.board.worker_comment(card_, "planner", classify(ref, gen))
        w.step(lambda: feedback_comments(w.board, card), world=life.planner(script))
        return card

    def test_feedback_waits_for_a_running_planner_and_wakes_it_once(self):
        w = World(self)
        life = Lifecycle(w)
        card = self.running_draft(w, life)
        b = w.board
        woken = unblocks(b, card)
        for _ in range(3):  # still running: the wake waits, nothing is cancelled or counted
            w.clock.advance(61)
            w.run()
        self.assertEqual(unblocks(b, card), woken)
        self.assertEqual(b.cards[card]["task"]["status"], "running")
        b.park(card)
        turns = []

        def script(card_, ref, gen):
            turns.append(feedback_comments(b, card_))
            b.park(card_, out("notice", ref, "The test login is in the vault entry for the QA account."))
        w.step(lambda: life.bot_q("vault entry for the QA account"), world=life.planner(script))
        self.assertEqual(unblocks(b, card), woken + 1)
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0]), 1)
        self.assertIn("rule=password attempt=1/2", turns[0][0].splitlines()[0])
        self.assertNotIn("Hunter2", turns[0][0])
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertFalse(any("Hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))

    def test_unreachable_planner_is_a_visible_hold(self):
        w = World(self)
        life = Lifecycle(w)
        card = self.running_draft(w, life)
        b = w.board
        woken = unblocks(b, card)
        b._end(card, "blocked")
        b.cards[card]["task"]["status"] = "blocked"
        w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", limit=5)
        self.assertEqual(unblocks(b, card), woken)
        self.assertTrue(any("request card %s is blocked" % card in line and "corrected draft" in line
                            for line in w.outputs), w.outputs)
        self.assertFalse(any("Hunter2" in line for line in w.outputs))

    # SPEC r14 finding 1: the correction must start with a CLICKUP-OUT v1 header, not the CLICKUP-FILTER line
    def test_feedback_names_the_clickup_out_header_the_correction_starts_with(self):
        w, life, seen = self.rewrite()
        ref = relay_of(w.board, life.req_cards()[0])[0]
        text = " ".join(seen[1][0].split())
        self.assertIn("Post the corrected text as a NEW comment whose first line is exactly \"CLICKUP-OUT v1"
                      " kind=business_questions relay=%s\" (the rejected output's header)" % ref, text)
        self.assertIn("The CLICKUP-FILTER line of this comment is not an output header", text)

    def test_feedback_without_the_rejected_comment_in_view_still_names_the_clickup_out_header(self):
        real = glue_main.Glue.filter_feedback

        def unseen(glue, row, reason):  # the rejected comment is missing from this scan's show
            glue.obs.cards.update({c: dict(s, comments=[]) for c, s in glue.obs.cards.items()})
            return real(glue, row, reason)
        with mock.patch.object(glue_main.Glue, "filter_feedback", unseen):
            w, life, seen = self.rewrite()
        text = " ".join(seen[1][0].split())
        self.assertIn("Post the corrected text as a NEW comment whose first line is a \"CLICKUP-OUT v1\" header with"
                      " the same kind, relay and reply_to as the rejected output", text)
        self.assertIn("The CLICKUP-FILTER line of this comment is not an output header", text)
        self.assertTrue(life.bot_q("Which test login should we use?"))

    # SPEC r14 finding 2: a woken planner that ends its run without a corrected draft is a visible hold
    def uncorrected(self, second, crash=False):
        """First draft filtered; the feedback-woken planner ends its run with ``second(board, card, fb)``."""
        w = World(self, crash=crash)
        life = Lifecycle(w)
        turns = []

        def script(card, ref, gen):
            turns.append(feedback_comments(w.board, card))
            if len(turns) == 1:
                w.board.park(card, out("business_questions", ref, "Use password: hunter2 for the test login?"),
                             classify(ref, gen))
            else:
                second(w.board, card, turns[-1])
        world = life.planner(script)
        w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", world=world)
        card = life.req_cards()[0]
        b = w.board
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(turns[1]), 1)
        lines = [x for x in operator_lines(w) if "without a corrected draft" in x]
        self.assertEqual(lines, ["ClickUp outbound filter: request card %s ended its run after the feedback on output"
                                 " %s of task %s without a corrected draft; nothing was posted; task on hold"
                                 % (card, turns[1][0].split()[2][len("output="):], life.tid)])
        self.assertFalse(any("hunter2" in x for x in w.outputs))
        self.assertFalse(any("hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertEqual(w.q("SELECT state FROM outbox WHERE kind='alert' AND idem_key LIKE '%:uncorrected'"),
                         [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 2)  # release + feedback
        before = (len(b.mutations()), life.planner_turns, len(w.outputs))
        for _ in range(4):  # one durable line; no further feedback, wake or mutation while it holds
            w.clock.advance(61)
            w.run()
            world()
        self.assertEqual((len(b.mutations()), life.planner_turns, len(w.outputs)), before)
        self.assertEqual(w.task(life.tid)["hold_reason"], "outbound_filter")

    def test_woken_planner_that_parks_without_a_correction_is_a_visible_hold(self):
        self.uncorrected(lambda b, card, fb: b.park(card))

    def test_woken_planner_that_starts_with_the_filter_line_is_a_visible_hold(self):
        self.uncorrected(lambda b, card, fb: b.park(
            card, fb[-1].splitlines()[0] + "\nWhich test login should we use? It stays in the vault."))

    def test_uncorrected_hold_is_restart_safe(self):
        self.uncorrected(lambda b, card, fb: b.park(card), crash=True)

    # OP-Q1: the final rejection, its hold and its durable alert commit together
    def test_bound_hold_survives_death_right_after_the_last_rejected_draft_is_recorded(self):
        w = World(self)
        life = Lifecycle(w)

        def script(card, ref, gen):
            w.board.park(card, out("business_questions", ref, "Use password: hunter2 for the test login?"),
                         classify(ref, gen))
        world = life.planner(script)
        crashed = post_commit_crash(
            self, "SELECT COUNT(*) FROM outbox WHERE kind='cu_comment' AND outcome='FILTERED'", 3)
        w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", world=world)
        self.assertEqual(crashed, [True])
        card = life.req_cards()[0]
        self.assertEqual(w.q("SELECT state, outcome FROM outbox WHERE idem_key LIKE ? AND kind='cu_comment'",
                             "clickup-dp:%s:q:business:%%" % life.tid), [{"state": "failed", "outcome": "FILTERED"}] * 3)
        self.assertEqual(len(feedback_comments(w.board, card)), 2)
        self.assertEqual(life.planner_turns, 3)
        lines = [x for x in operator_lines(w) if "no automatic correction left" in x]
        self.assertEqual(lines, ["ClickUp outbound filter: request card %s produced 3 rejected drafts for task %s"
                                 " (gen %d); no automatic correction left; task on hold"
                                 % (card, life.tid, w.task(life.tid)["input_gen"])])
        self.assertFalse(any("hunter2" in x for x in w.outputs))
        self.assertFalse(any("hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        before = (len(w.board.mutations()), life.planner_turns, len(w.outputs))
        for _ in range(4):  # the durable line printed once; no further feedback, wake or mutation while it holds
            w.clock.advance(61)
            w.run()
            world()
        self.assertEqual((len(w.board.mutations()), life.planner_turns, len(w.outputs)), before)
        self.assertEqual(w.task(life.tid)["hold_reason"], "outbound_filter")

    def test_unreachable_hold_survives_death_right_after_the_wake_is_failed(self):
        w = World(self)
        life = Lifecycle(w)
        card = self.running_draft(w, life)
        b = w.board
        woken = unblocks(b, card)
        b._end(card, "blocked")
        b.cards[card]["task"]["status"] = "blocked"
        crashed = post_commit_crash(self, "SELECT COUNT(*) FROM outbox WHERE outcome='UNREACHABLE'", 1)
        w.step(lambda: w.task(life.tid)["hold_reason"] == "outbound_filter", limit=5)
        self.assertEqual(crashed, [True])
        self.assertEqual(unblocks(b, card), woken)
        self.assertEqual([x for x in operator_lines(w) if "cannot be asked for a corrected draft" in x],
                         [glue_main._no_correction(card, "blocked", life.tid)])
        self.assertFalse(any("Hunter2" in line for line in w.outputs))

    # OP-Q2: the feedback wake is bound to the producing run when the feedback is created
    def test_feedback_seen_by_an_intervening_relay_run_is_not_woken_again(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        card = life.req_cards()[0]
        b = w.board
        real_wait = glue_main.Glue.feedback_wait
        seen, delayed = [], []

        def failing_show(glue, row, target):  # the feedback wake's own read fails until a relay run saw the feedback
            if not any(n for _, _, n in seen):
                delayed.append(glue.now)
                with mock.patch.object(glue.port, "show", return_value=KanbanResult(Outcome.DEFINITE_FAIL, {})):
                    return real_wait(glue, row, target)
            return real_wait(glue, row, target)

        def script(card_, ref, gen):
            seen.append((ref, gen, len(feedback_comments(b, card_))))
            if len(seen) == 1:
                b.park(card_, out("notice", ref, "The test login is admin, password: Hunter2!"), classify(ref, gen))
                life.say(APPROVER_B, " also, can it be dark blue?", mention=BOT)
            elif len(seen) == 2:  # the correction keeps the rejected output's header, as the feedback asks
                b.park(card_, out("notice", seen[0][0], "The test login is in the vault."), classify(ref, gen))
            else:
                b.park(card_, classify(ref, gen))
        life.say(APPROVER_B, " which login do we test with?", mention=BOT)
        world = life.planner(script)
        with mock.patch.object(glue_main.Glue, "feedback_wait", failing_show):
            for _ in range(30):
                w.clock.advance(61)
                w.run()
                world()
        self.assertTrue(delayed)
        self.assertEqual(len(seen), 2, seen)  # the relay run that saw the feedback corrected the draft; no extra run
        self.assertEqual(seen[1][2], 1)
        self.assertTrue(life.bot_q("The test login is in the vault."))
        self.assertEqual(len(feedback_comments(b, card)), 1)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 3)  # pickup, g2, g3 relays
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertFalse(any("Hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))

    # SPEC r16: a relay run that started before the (delayed) feedback comment was delivered never saw it
    def delayed_feedback(self, third):
        """First draft filtered while new input arrives; the feedback comment fails transiently until the relay
        run for that input (which never saw the feedback) has parked with only its answer to the new input; the
        next run ends with ``third(board, card, ref)``."""
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        card = life.req_cards()[0]
        b = w.board
        real_comment = b.comment
        seen, failed = [], []

        def comment(card_, body):
            if body.startswith("CLICKUP-FILTER v1") and len(seen) < 2:
                failed.append(w.clock())
                return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "transient"})
            return real_comment(card_, body)

        def script(card_, ref, gen):
            seen.append((ref, gen, len(feedback_comments(b, card_))))
            if len(seen) == 1:
                b.park(card_, out("notice", ref, "The test login is admin, password: Hunter2!"), classify(ref, gen))
                life.say(APPROVER_B, " also, can it be dark blue?", mention=BOT)
            elif len(seen) == 2:
                b.park(card_, out("notice", ref, "Dark blue works."), classify(ref, gen))
            else:  # ``ref`` is the rejected output's relay: the header the feedback asks for
                third(b, card_, seen[0][0])
        life.say(APPROVER_B, " which login do we test with?", mention=BOT)
        world = life.planner(script)
        with mock.patch.object(b, "comment", comment):
            w.step(lambda: len(seen) == 3 and (life.bot_q("The test login is in the vault.")
                                               or w.task(life.tid)["hold_reason"]), world=world)
            for _ in range(4):
                w.clock.advance(61)
                w.run()
                world()
        self.assertTrue(failed)
        # the relay run answered the new input without the feedback; the feedback wake gave it to the next run
        self.assertEqual([n for _, _, n in seen], [0, 0, 1], seen)
        self.assertTrue(life.bot_q("Dark blue works."))
        self.assertEqual(len(feedback_comments(b, card)), 1)
        self.assertFalse(any("Hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertFalse(any("Hunter2" in x for x in w.outputs))
        return w, life, card

    def test_relay_run_that_started_before_a_delayed_feedback_comment_does_not_count_as_its_correction(self):
        w, life, card = self.delayed_feedback(
            lambda b, card, ref: b.park(card, out("notice", ref, "The test login is in the vault.")))
        self.assertTrue(life.bot_q("The test login is in the vault."))
        self.assertIsNone(w.task(life.tid)["hold_reason"])

    def test_relay_answer_before_a_delayed_feedback_comment_is_not_taken_for_the_missing_correction(self):
        w, life, card = self.delayed_feedback(lambda b, card, ref: b.park(card))
        self.assertEqual(w.task(life.tid)["hold_reason"], "outbound_filter")
        self.assertEqual(len([x for x in operator_lines(w) if "without a corrected draft" in x]), 1)

    def test_feedback_waits_for_a_readable_request_card(self):
        real = glue_main.Glue.filter_feedback
        dropped = []

        def unreadable(glue, row, reason):  # the request card's show failed in the scan of the first rejection
            if not dropped:
                dropped.append(True)
                glue.obs.cards.clear()
            return real(glue, row, reason)
        with mock.patch.object(glue_main.Glue, "filter_feedback", unreadable):
            w, life, seen = self.rewrite()
        card = life.req_cards()[0]
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(seen[1]), 1)
        self.assertTrue(life.bot_q("Which test login should we use?"))
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in w.board.cards[card]["events"]), 2)
        self.assertEqual([x for x in operator_lines(w) if "not readable" in x],
                         ["ClickUp outbound filter: request card %s of task %s is not readable; its feedback is"
                          " deferred" % (card, life.tid)])

    # OP-Q3/OP-Q4: only an output with the rejected output's kind/relay/reply_to is its correction
    def late_read(self, correct):
        """Probe shape: the first draft is filtered while new input arrives; the feedback comment fails
        transiently, so the relay run for that input starts BEFORE the feedback is delivered. That run is still
        running when it lands, reads it and parks with its answer to the new input, plus (``correct``) the
        correction under the rejected output's exact header. A further run answers only the new input."""
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        card = life.req_cards()[0]
        b = w.board
        real_comment = b.comment
        seen, pending, failed = [], [], []

        def comment(card_, body):
            if body.startswith("CLICKUP-FILTER v1") and len(seen) < 2:
                failed.append(w.clock())
                return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "transient"})
            return real_comment(card_, body)

        def script(card_, ref, gen):
            seen.append((ref, gen, len(feedback_comments(b, card_))))
            if len(seen) == 1:
                b.park(card_, out("notice", ref, "The test login is admin, password: Hunter2!"), classify(ref, gen))
                life.say(APPROVER_B, " also, can it be dark blue?", mention=BOT)
            elif len(seen) == 2:
                pending.append((ref, gen))  # still running when the feedback is delivered
            else:
                b.park(card_, out("notice", ref, "Dark blue is still available."), classify(ref, gen))
        life.say(APPROVER_B, " which login do we test with?", mention=BOT)
        world = life.planner(script)
        with mock.patch.object(b, "comment", comment):
            for _ in range(40):
                w.clock.advance(61)
                w.run()
                if pending and feedback_comments(b, card):
                    ref, gen = pending.pop()
                    w.clock.advance(5)  # the running relay run reads the delivered feedback, then parks
                    outputs = [out("notice", ref, "Dark blue works."), classify(ref, gen)]
                    if correct:
                        outputs.append(out("notice", seen[0][0], "The test login is in the vault."))
                    b.park(card, *outputs)
                world()
        self.assertTrue(failed)
        self.assertEqual([n for _, _, n in seen[:2]], [0, 0], seen)
        self.assertEqual(len(feedback_comments(b, card)), 1)
        self.assertIn("relay=" + seen[0][0], feedback_comments(b, card)[0])
        self.assertTrue(life.bot_q("Dark blue works."))
        return w, life, card, seen, world

    def test_exact_header_correction_by_a_run_that_started_before_the_feedback_is_not_woken_or_held(self):
        w, life, card, seen, world = self.late_read(True)
        b = w.board
        self.assertEqual(len(seen), 2, seen)  # pickup, g2, g3 releases; no redundant feedback wake
        self.assertEqual(unblocks(b, card), 3)
        self.assertEqual(sum("The test login is in the vault." in c["comment_text"]
                             for c in w.bot_comments(life.tid)), 1)
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertEqual([x for x in operator_lines(w) if "without a corrected draft" in x], [])
        life.say(APPROVER_B, " can we continue with this request?", mention=BOT)  # later input still wakes the planner
        for _ in range(8):
            w.clock.advance(61)
            w.run()
            world()
        self.assertEqual(unblocks(b, card), 4)
        self.assertEqual(len(seen), 3, seen)
        self.assertIsNone(w.task(life.tid)["hold_reason"])
        self.assertFalse(any("Hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertFalse(any("Hunter2" in x for x in w.outputs))

    def test_output_for_another_relay_does_not_discharge_the_missing_correction(self):
        w, life, card, seen, world = self.late_read(False)
        self.assertEqual(len(seen), 3, seen)  # the feedback wake ran; it answered only the newer relay
        self.assertTrue(life.bot_q("Dark blue is still available."))
        self.assertFalse(any("The test login is in the vault." in c["comment_text"]
                             for c in w.bot_comments(life.tid)))
        self.assertEqual(w.task(life.tid)["hold_reason"], "outbound_filter")
        self.assertEqual(len([x for x in operator_lines(w) if "without a corrected draft" in x]), 1)
        self.assertFalse(any("Hunter2" in c["comment_text"] for c in w.bot_comments(life.tid)))
        self.assertFalse(any("Hunter2" in x for x in w.outputs))


def post_commit_crash(tc, count_sql, n):
    """Simulated death right after the first commit that makes ``count_sql`` return ``n`` (the scan reruns)."""
    crashed = []

    class PostCommitCrashStore(Store):
        @contextlib.contextmanager
        def transaction(self):
            with Store.transaction(self) as c:
                yield c
            if not crashed and self.conn.execute(count_sql).fetchone()[0] == n:
                crashed.append(True)
                raise Crash("death right after a commit")
    p = mock.patch.object(glue_main, "Store", PostCommitCrashStore)
    p.start()
    tc.addCleanup(p.stop)
    return crashed


# ------------------------------------------------------------------------------- QSR-612 regressions
def staging_progress(w):
    """Partial context snapshots currently staged (resumable F3 staging)."""
    root = os.path.join(w.runtime, "staging")
    return [d for d in (os.listdir(root) if os.path.isdir(root) else ())
            if os.path.exists(os.path.join(root, d, "snapshot.json"))]


class QsrSendBoundaryTest(unittest.TestCase):
    """F1/F2 at the executor: reply destinations and quiet-expired sends/retries (plan §4.4, §7.1, §8.3, §9, §10.1)."""

    def test_reply_destination_outside_this_task_is_never_written(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        foreign = w.cu.add_task(name="Unrelated task", list_id="not-the-approved-list")
        root = w.cu.add_comment(foreign, REQ, "Outside scope")
        card = life.req_cards()[0]
        ref, _ = relay_of(w.board, card)
        w.board.worker_comment(card, "planner", out("answer", ref, "Answer belonging to the approved task",
                                                    reply_to=root))
        w.clock.advance(61)
        w.run()
        # A reply row naming that root (queued before the engine check existed) is refused at the send boundary.
        s = Store(w.state_path, w.clock)
        with s.transaction() as c:
            c.execute("INSERT INTO outbox(idem_key, task, kind, target, body, preconditions, depends_on)"
                      " VALUES (?,?,?,?,?,?,?)",
                      ("clickup-dp:%s:out:legacy" % life.tid, life.tid, "cu_reply", life.tid,
                       json.dumps({"root": root, "text": "legacy answer", "task": life.tid, "ref": "cu-legacy"}),
                       json.dumps({"task_id": life.tid, "list_id": LIST_ID}), "[]"))
        s.close()
        for _ in range(6):  # covers at least one L of this task (write scope confirmed)
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.cu.count("POST", "/v2/comment/%s/reply" % root), 0)
        self.assertEqual([c for c in w.cu.replies[root] if c["user"]["id"] == BOT], [])
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key=?", "clickup-dp:%s:out:legacy" % life.tid),
                         [{"state": "cancelled_stale"}])
        lines = operator_lines(w)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("names a reply_to that is not an observed comment of task %s" % life.tid, lines[0])
        self.assertEqual(lines[1], "ClickUp reply outbox clickup-dp:%s:out:legacy targets a thread that is not"
                                   " an observed comment of task %s; not sent" % (life.tid, life.tid))
        self.assertNotIn(root, "\n".join(lines))

    def test_failed_gate_completion_waits_for_the_new_quiet_and_barrier(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        life.approve_to_released()
        life.orchestrate()
        real, failed = w.board.complete, []

        def fail_once(card, summary, metadata=None):
            if card == life.gate() and not failed:
                failed.append(card)
                return KanbanResult(Outcome.DEFINITE_FAIL)
            return real(card, summary, metadata)
        w.board.complete = fail_once
        w.board.finish(life.fe, "e2e-tester")
        w.step(lambda: bool(w.q("SELECT 1 AS x FROM outbox WHERE kind='kb_complete' AND state='retry_wait'")),
               limit=45)
        life.say(REQ, "A new unaddressed comment that restarts quiet")
        for _ in range(3):  # the retry is due, but the new quiet window is open: deferred, not attempted
            w.clock.advance(61)
            w.run()
            self.assertGreater(w.task(life.tid)["quiet_until_ms"], w.clock())
            self.assertEqual(w.board.cards[life.gate()]["task"]["status"], "blocked")
        row = w.q("SELECT state, attempts FROM outbox WHERE kind='kb_complete'")[0]
        self.assertEqual((row["state"], row["attempts"]), ("retry_wait", 1))
        w.step(lambda: w.board.cards[life.gate()]["task"]["status"] == "done", limit=15)
        t = w.task(life.tid)
        self.assertGreaterEqual(w.clock(), t["quiet_until_ms"])
        self.assertGreaterEqual(t["full_started_ms"], t["quiet_until_ms"])  # a barrier F after the new quiet
        self.assertEqual(sum(1 for c, k in w.board.calls if c == "complete" and k == life.gate()), 1)
        self.assertEqual(life.ticket_check(), (0, ["VALID epoch=1"]))

    def test_failed_publication_upload_waits_for_the_new_quiet_window(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.say(APPROVER_A, "Yes, follow the OS theme.")
        life.say(APPROVER_A, "Technical requirements closed")
        w.cu.inject("POST", r"/attachment$", Outcome.DEFINITE_FAIL)

        def script(card, ref, gen):
            w.board.worker_comment(card, "planner", classify(ref, gen))
            life.plan_sha = w.board.request_review(card, life.tid, 1, PLAN_R1, gen)
            w.board.claim(card, "plan-reviewer")
            w.board.finish(card, "plan-reviewer")
        w.step(lambda: bool(w.q("SELECT 1 AS x FROM outbox WHERE kind='clickup_attachment' AND state='retry_wait'")),
               world=life.planner(script))
        life.say(REQ, "An unrelated remark while the upload waits for its retry")
        for _ in range(2):
            w.clock.advance(61)
            w.run()
            self.assertGreater(w.task(life.tid)["quiet_until_ms"], w.clock())
        self.assertEqual(w.cu.count("POST", r"/attachment$"), 1)
        self.assertFalse(life.bot_q("Plan r1, SHA-256"))
        w.step(lambda: life.bot_q("Plan r1, SHA-256"))
        self.assertEqual(w.cu.count("POST", r"/attachment$"), 2)
        self.assertEqual(w.q("SELECT attempts FROM outbox WHERE kind='clickup_attachment'"), [{"attempts": 2}])


class QsrContextAndClassificationTest(unittest.TestCase):
    """F3 resumable generation/barrier-bound context staging; F4 missing-classification alert (§4.3-§4.5, §5.3, §8.2)."""

    def test_history_above_one_scan_budget_is_staged_across_scans_and_picked_up(self):
        w = World(self)
        tid = w.cu.add_task(name="Long but supported history", creator=REQ)
        for i in range(45):
            root = w.cu.add_comment(tid, REQ, "root %d" % i)
            w.cu.add_comment(tid, REQ, "reply %d" % i, reply_to=root)
        parked, late, per_scan = [], [], []

        def world():
            if staging_progress(w) and not late:  # staging spans scans; new activity restarts it
                late.append(w.cu.add_comment(tid, APPROVER_B, "late remark during staging"))
            for card in w.board.by_key("clickup-dp:%s:req:" % tid):
                if w.board.cards[card]["task"]["status"] == "ready":
                    w.board.claim(card, "planner")
                    ref, gen = relay_of(w.board, card)
                    w.board.park(card, out("business_questions", ref, "Who needs it?"), classify(ref, gen))
                    parked.append(card)
        for _ in range(60):
            before = len(w.cu.calls)
            w.clock.advance(61)
            w.run()
            per_scan.append(len(w.cu.calls) - before)
            world()
            if parked and any("Who needs it?" in c["comment_text"] for c in w.bot_comments(tid)):
                break
        else:
            self.fail("pickup never completed; per-scan requests %r" % per_scan)
        self.assertTrue(late)
        self.assertLessEqual(max(per_scan), 40, per_scan)
        card = w.board.cards[parked[0]]
        # the late remark re-stages the unsent first wake (S2 R2-F2): its one context file may carry -b<k>
        ctx = [a for a in card["atts"] if re.fullmatch(r"clickup-context-g\d+(-b\d+)?\.md", a["filename"])]
        self.assertEqual(len(ctx), 1)
        text = _read(ctx[0]["stored_path"]).decode("utf-8")
        for i in (0, 44):  # complete, untruncated history
            self.assertIn("root %d" % i, text)
            self.assertIn("reply %d" % i, text)
        self.assertIn("late remark during staging", text)  # restarted after the new activity, not stale
        self.assertEqual(sum(e["kind"] == "unblocked" for e in card["events"]), 1)
        self.assertEqual(staging_progress(w), [])
        self.assertEqual(operator_lines(w), [])

    def test_transient_orchestrator_context_attach_failure_with_large_history_still_releases(self):
        """SPEC r3: the orchestrator's context retry keeps the shared snapshot after the gate's row is done."""
        w = World(self)
        life = Lifecycle(w)
        for i in range(45):
            root = w.cu.add_comment(life.tid, REQ, "root %d" % i)
            w.cu.add_comment(life.tid, REQ, "reply %d" % i, reply_to=root)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        real, failed = w.board.attach, []

        def attach(card, path, name):
            if card == life.orch() and re.fullmatch(r"clickup-context-g\d+\.md", name) and not failed:
                failed.append(name)
                return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "transient"})
            return real(card, path, name)
        w.board.attach = attach
        life.approve_to_released()
        self.assertEqual(len(failed), 1)
        rows = w.q("SELECT idem_key, state, attempts FROM outbox WHERE kind='kb_attach' AND idem_key LIKE ?",
                   "%:attach:context:g%")
        self.assertEqual({(r["idem_key"].split(":")[2], r["state"], r["attempts"]) for r in rows},
                         {("gate", "done", 1), ("orch", "done", 2)})
        for card in (life.orch(), life.gate()):
            ctx = [a for a in w.board.cards[card]["atts"] if re.fullmatch(r"clickup-context-g\d+\.md", a["filename"])]
            self.assertEqual(len(ctx), 1, card)
            text = _read(ctx[0]["stored_path"]).decode("utf-8")
            for i in (0, 44):
                self.assertIn("root %d" % i, text)
                self.assertIn("reply %d" % i, text)
        self.assertEqual(staging_progress(w), [])
        self.assertEqual(operator_lines(w), [])

    def test_parked_without_classification_alerts_once_and_wakes_nothing(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        life.say(REQ, "Please explain one detail", mention=BOT)

        def script(card, ref, gen):
            w.board.park(card, out("answer", ref, "The requested explanation"))
        w.step(lambda: life.bot_q("The requested explanation"), world=life.planner(script))
        card = life.req_cards()[-1]
        unblocks = sum(1 for c, k in w.board.calls if c == "unblock" and k == card)
        for _ in range(5):
            w.clock.advance(61)
            w.run()
        t = w.task(life.tid)
        self.assertGreater(t["input_gen"], t["classified_gen"])
        lines = operator_lines(w)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^request card %s parked without classifying relay \S+ \(gen %d\); approval and"
                                   r" tickets stay suspended$" % (card, t["input_gen"]))
        self.assertEqual(sum(1 for c, k in w.board.calls if c == "unblock" and k == card), unblocks)
        self.assertEqual(w.board.cards[card]["task"]["status"], "scheduled")


# ------------------------------------------------------------------------------- QSR-619 regressions
def fail_first_unblock(board, which):
    """The first ``unblock`` of a card selected by ``which(card)`` fails definitely (nothing sent)."""
    real, failed = board.unblock, []

    def unblock(card):
        if which(card) and not failed:
            failed.append(card)
            return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "transient"})
        return real(card)
    board.unblock = unblock
    return failed


SNAPSHOT_LINE = r"Refreshed snapshot: (clickup-context-g\d+(?:-b\d+)?\.md) \(attached, SHA-256 ([0-9a-f]{64})\)"


class QsrFirstWakeAndReleaseTest(unittest.TestCase):
    """R2-F2 (§4.4, §5.1, §8.2): the first planner run gets the batch and context of its actual barrier.
    R2-F1 (§5.3, §8.4 step 3, §10.1): a cancelled-stale initial release is replanned after answer_only."""

    def test_unaddressed_input_during_first_wake_retry_reaches_the_first_planner_run(self):
        w = World(self)
        life = Lifecycle(w)
        failed = fail_first_unblock(w.board, lambda card: card in life.req_cards())
        w.step(lambda: bool(failed))
        card = life.req_cards()[0]
        b = w.board
        first = [a for a in b.cards[card]["atts"] if a["filename"].startswith("clickup-context-")]
        self.assertEqual(len(first), 1)  # the initial snapshot, relay comment and attachments succeeded
        first_bytes = _read(first[0]["stored_path"])
        self.assertNotIn(b"only enterprise customers", first_bytes)
        input_gen = w.task(life.tid)["input_gen"]
        late = life.say(REQ, "only enterprise customers need this")  # unaddressed: context, never a wake
        seen = []

        def script(card_, ref, gen):
            seen.append((ref, b.relays(card_)[-1]))
            b.park(card_, out("business_questions", ref, "Which plan tier?"), classify(ref, gen))
        w.step(lambda: bool(seen), world=life.planner(script))
        self.assertEqual(life.planner_turns, 1)
        ref, body = seen[0]
        self.assertRegex(ref, r"-b2-w0$")
        self.assertIn("only enterprise customers need this", body)  # the item is in the first batch
        m = re.search(SNAPSHOT_LINE, body)
        self.assertIsNotNone(m, body)
        named = [a for a in b.cards[card]["atts"] if a["filename"] == m.group(1)]
        self.assertEqual(len(named), 1, [a["filename"] for a in b.cards[card]["atts"]])
        data = _read(named[0]["stored_path"])
        self.assertEqual(_sha(data), m.group(2))  # the file the relay names is the one attached and verified
        self.assertIn(b"only enterprise customers need this", data)
        names = [a["filename"] for a in b.cards[card]["atts"]]
        self.assertEqual(len(names), len(set(names)), names)  # attachments are never overwritten
        self.assertEqual(_read(first[0]["stored_path"]), first_bytes)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 1)
        for _ in range(5):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.task(life.tid)["input_gen"], input_gen)  # the unaddressed comment raised no gen
        item = w.q("SELECT delivered_relay FROM items WHERE task=? AND id=?", life.tid, late)[0]
        self.assertEqual(item["delivered_relay"], ref)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 1)  # no extra wake
        self.assertEqual(operator_lines(w), [])

    def test_cancelled_initial_release_is_replanned_after_answer_only(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        b = w.board
        failed = fail_first_unblock(b, lambda card: card == life.orch())
        life.say(APPROVER_A, "Approve plan r1 for development")
        w.step(lambda: bool(failed))
        release = "%%:%s:orch:r1:%%:unblock" % life.tid
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key LIKE ?", release), [{"state": "retry_wait"}])
        question = life.say(APPROVER_B, " does this cover mobile?", mention=BOT)

        def script(card, ref, gen):
            b.park(card, out("answer", ref, "Yes, mobile is included.", reply_to=question), classify(ref, gen))
        w.step(lambda: b.cards[life.orch()]["task"]["status"] == "ready", world=life.planner(script))
        # the engine records the verified release at the task's next confirming pass
        w.step(lambda: w.q("SELECT state FROM approvals WHERE task=?", life.tid) == [{"state": "released"}], limit=10)
        self.assertEqual(failed, [life.orch()])
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key LIKE ?", release), [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[life.orch()]["events"]), 1)
        t = w.task(life.tid)
        self.assertEqual(t["classified_gen"], t["input_gen"])
        for card in (life.orch(), life.gate()):  # the current generation's context precedes the release
            self.assertIn("clickup-context-g%d.md" % t["input_gen"], [a["filename"] for a in b.cards[card]["atts"]])
        self.assertTrue(life.bot_q("Yes, mobile is included."))
        self.assertEqual(operator_lines(w), [])


# ------------------------------------------------------------------------------- QSR-624 regressions
class QsrLaterRelayAndAbandonTest(unittest.TestCase):
    """R3-F1 (§4.4, §5.1, §8.2, §8.3, T18): an unsent LATER relay wakes with the batch and context of its
    actual barrier, under its original wait run. R3-F2 (§10.2, §11): a scan never undoes ``outbox abandon``,
    and ``outbox resend`` of the held row still sends it exactly once."""

    def test_unaddressed_input_during_a_later_relay_retry_reaches_that_wake(self):
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        b = w.board
        card = life.req_cards()[0]
        wakes = sum(e["kind"] == "unblocked" for e in b.cards[card]["events"])
        failed = fail_first_unblock(b, lambda cid: cid == card)
        question = life.say(REQ, "Please clarify the licensed users", mention=BOT)
        w.step(lambda: bool(failed))
        old = re.search(r"-g(\d+)-w(\d+)$", relay_of(b, card)[0])
        self.assertIsNotNone(old)
        gen, wait_run = old.group(1), old.group(2)
        self.assertNotEqual(wait_run, "0")  # a later relay to the parked card, not the first wake
        input_gen = w.task(life.tid)["input_gen"]
        late = life.say(APPROVER_B, "only enterprise customers need this")  # unaddressed: context, never a wake
        seen = []

        def script(cid, ref, g):
            seen.append((ref, b.relays(cid)[-1]))
            b.park(cid, out("answer", ref, "The licensed users are included.", reply_to=question), classify(ref, g))
        w.step(lambda: bool(seen), world=life.planner(script))
        ref, body = seen[0]
        self.assertRegex(ref, r"-g%s-b2-w%s$" % (gen, wait_run))  # re-staged under the same wait run
        self.assertIn("only enterprise customers need this", body)
        m = re.search(SNAPSHOT_LINE, body)
        self.assertIsNotNone(m, body)
        named = [a for a in b.cards[card]["atts"] if a["filename"] == m.group(1)]
        self.assertEqual(len(named), 1, [a["filename"] for a in b.cards[card]["atts"]])
        data = _read(named[0]["stored_path"])
        self.assertEqual(_sha(data), m.group(2))
        self.assertIn(b"only enterprise customers need this", data)
        contexts = [a["filename"] for a in b.cards[card]["atts"] if a["filename"].startswith("clickup-context-")]
        self.assertEqual(len(contexts), len(set(contexts)), contexts)  # context files are never overwritten
        rows = {r["idem_key"]: r for r in w.q("SELECT idem_key, state, outcome, body, preconditions FROM outbox"
                                                 " WHERE kind='kb_unblock' AND idem_key LIKE ?", "%:relay:%")}
        wake = rows["clickup-dp:%s:relay:%s:unblock" % (life.tid, ref)]  # still fenced by the original wait run
        self.assertEqual((json.loads(wake["body"])["wait_run_id"], json.loads(wake["preconditions"])["wait_run_id"]),
                         (int(wait_run), int(wait_run)))
        stale = rows["clickup-dp:%s:relay:%s:unblock" % (life.tid, old.string)]
        self.assertEqual((stale["state"], stale["outcome"]), ("cancelled_stale", "SUPERSEDED"))  # never sent
        for _ in range(20):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.task(life.tid)["input_gen"], input_gen)  # the unaddressed comment raised no gen
        item = w.q("SELECT delivered_relay FROM items WHERE task=? AND id=?", life.tid, late)[0]
        self.assertEqual(item["delivered_relay"], ref)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), wakes + 1)
        self.assertEqual(b.cards[card]["task"]["status"], "scheduled")
        self.assertEqual(operator_lines(w), [])

    @staticmethod
    def _exhausted_release(w):
        """Approve r1 while every orchestrator unblock fails definitely, until the release row is ``failed``
        (held); then the board recovers. Returns (lifecycle, release key, failed attempts)."""
        life = Lifecycle(w)
        life.pickup_to_business()
        life.business_to_technical()
        life.technical_to_published()
        b = w.board
        real_unblock, attempts = b.unblock, []

        def fail_orch(cid):
            if cid == life.orch():
                attempts.append(cid)
                return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "definite offline fixture failure"})
            return real_unblock(cid)
        b.unblock = fail_orch
        life.say(APPROVER_A, "Approve plan r1 for development")
        release = "%%:%s:orch:r1:%%:unblock" % life.tid
        w.step(lambda: [r["state"] for r in w.q("SELECT state FROM outbox WHERE idem_key LIKE ?", release)]
               == ["failed"], seconds=1901)
        b.unblock = real_unblock
        return life, w.q("SELECT idem_key FROM outbox WHERE idem_key LIKE ?", release)[0]["idem_key"], attempts

    def test_abandoned_initial_release_is_never_restarted_by_later_scans(self):
        w = World(self)
        life, key, attempts = self._exhausted_release(w)
        b = w.board
        orch = life.orch()
        self.assertEqual(w.main("outbox", "abandon", key), (0, ["abandon done"]))
        alerts = len(operator_lines(w))
        for _ in range(10):  # confirming scans never undo the operator's abandon
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.q("SELECT state, outcome FROM outbox WHERE idem_key=?", key),
                         [{"state": "cancelled_stale", "outcome": "ABANDONED"}])
        self.assertEqual(b.cards[orch]["task"]["status"], "blocked")
        self.assertFalse(any(e["kind"] == "unblocked" for e in b.cards[orch]["events"]))
        self.assertEqual(w.q("SELECT state FROM approvals WHERE task=?", life.tid), [{"state": "staged"}])
        self.assertEqual(len(operator_lines(w)), alerts)
        self.assertEqual(len(attempts), 6)

    def test_resend_of_an_exhausted_initial_release_sends_it_once(self):
        w = World(self)
        life, key, attempts = self._exhausted_release(w)
        b = w.board
        orch = life.orch()
        self.assertEqual(w.main("outbox", "resend", key), (0, ["resend done"]))
        w.step(lambda: w.q("SELECT state FROM approvals WHERE task=?", life.tid) == [{"state": "released"}])
        self.assertEqual(b.cards[orch]["task"]["status"], "ready")
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key=?", key), [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[orch]["events"]), 1)
        self.assertEqual(len(attempts), 6)


# ------------------------------------------------------------------------------- QSR-629 regression
@unittest.skipUnless(os.name == "nt", "process identity (pid + creation time) is read via the Windows API")
class QsrInitialUnblockSurvivingChildTest(unittest.TestCase):
    """R4-F1 (§8.1, §10.2-10.3, T9/T17): initial-unblock recovery defers while the recorded child of an earlier
    attempt is alive, then reconciles from the card; it never sends an overlapping unblock or a second wake."""

    def _journaled_first_unblock(self, w):
        """The first request-card unblock fails once; then the poller 'dies' with a later attempt journaled
        as ``sending`` under a real live child pid/start time. Returns (card, key, child, real unblock, calls)."""
        Lifecycle(w)
        b = w.board
        real, attempted = b.unblock, []

        def fail(card):
            attempted.append(card)
            return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "transient"})
        b.unblock = fail
        w.step(lambda: bool(attempted))
        card = attempted[0]
        key = w.q("SELECT idem_key FROM outbox WHERE kind='kb_unblock' AND state='retry_wait'")[0]["idem_key"]
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait, 30)
        self.addCleanup(child.kill)
        st = kp.process_start_time(child.pid)
        self.assertTrue(kp.child_alive(child.pid, st))
        store = Store(w.state_path, w.clock)
        with store.transaction() as c:
            c.execute("UPDATE outbox SET state='sending', child_pid=?, child_start_time=? WHERE idem_key=?",
                      (child.pid, st, key))
        store.close()
        calls = []
        b.unblock = lambda cid: calls.append(cid) or real(cid)
        return card, key, child, real, calls

    def _scan(self, w):
        w.clock.advance(61)
        w.run()

    def _row(self, w, key):
        return w.q("SELECT state, attempts, child_pid FROM outbox WHERE idem_key=?", key)[0]

    def test_surviving_child_commits_and_planner_parks_before_the_retry_no_second_wake(self):
        w = World(self)
        card, key, child, real, calls = self._journaled_first_unblock(w)
        b = w.board
        journaled = self._row(w, key)
        self._scan(w)  # the earlier child is alive: nothing is sent and its journal is kept
        self.assertEqual(calls, [])
        self.assertEqual(self._row(w, key), journaled)
        self.assertEqual(b.cards[card]["task"]["status"], "blocked")
        # the surviving child's original unblock commits; the planner runs and parks its first wait
        real(card)
        b.claim(card, "planner")
        ref, gen = relay_of(b, card)
        b.park(card, out("business_questions", ref, "Which users?"), classify(ref, gen))
        self._scan(w)  # still alive: still deferred, even though the card is now a scheduled wait
        self.assertEqual(calls, [])
        self.assertEqual(self._row(w, key), journaled)
        child.kill()
        child.wait(30)
        for _ in range(5):  # after the child exits, recovery reconciles from the card instead of resending
            self._scan(w)
        self.assertEqual(calls, [])
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key=?", key), [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 1)
        self.assertEqual(len(b.cards[card]["runs"]), 1)  # exactly one planner run for this batch
        self.assertEqual(b.cards[card]["task"]["status"], "scheduled")

    def test_child_that_exits_without_committing_is_resent_exactly_once(self):
        w = World(self)
        card, key, child, _real, calls = self._journaled_first_unblock(w)
        b = w.board
        self._scan(w)
        self.assertEqual(calls, [])
        child.kill()
        child.wait(30)
        self._scan(w)
        self.assertEqual(calls, [card])
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key=?", key), [{"state": "done"}])
        self.assertEqual(b.cards[card]["task"]["status"], "ready")
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 1)


# ------------------------------------------------------------------------------- QSR-659 regression
class QsrPartialContextResendTest(unittest.TestCase):
    """R8-F1 (§8.2, §10.2, §11, T9/T17): the first relay's context file is attached, its ledger upload fails
    definitely six times (failed row, task on hold), ordinary scans run their staging sweep, then the operator
    acts. ``outbox resend`` must reuse the ORIGINAL rendered bundle: the attached context still verifies by
    exact name/size/SHA, only the missing files are sent, nothing is stored under a CLI-renamed ``name (n)``,
    and the card is woken exactly once. ``outbox abandon`` reclaims the retained bundle and wakes nothing."""

    def _exhausted(self, w):
        life = Lifecycle(w)
        b = w.board
        real, mode, stored = b.attach, {"fail": True}, []

        def attach(card, path, name):  # installed-CLI semantics: a repeated name is stored as ``name (n).ext``
            if mode["fail"] and name == "clickup-ledger.md":
                return KanbanResult(Outcome.DEFINITE_FAIL, {"error": "definite offline fixture failure"})
            existing, actual, n = {a["filename"] for a in b.cards[card]["atts"]}, name, 1
            while actual in existing:
                actual = "%s (%d)%s" % (os.path.splitext(name)[0], n, os.path.splitext(name)[1])
                n += 1
            stored.append(actual)
            return real(card, path, actual)
        b.attach = attach
        w.step(lambda: bool(w.task(life.tid)["hold_reason"]), seconds=1861, limit=15)
        rows = w.q("SELECT idem_key, state, attempts FROM outbox WHERE kind='kb_attach'")
        self.assertEqual([(r["state"], r["attempts"]) for r in rows], [("failed", 6)])
        key = rows[0]["idem_key"]
        card = life.req_cards()[0]
        self.assertEqual([a["filename"] for a in b.cards[card]["atts"]], ["clickup-context-g1.md"])
        original = _read(b.cards[card]["atts"][0]["stored_path"])
        m = re.search(SNAPSHOT_LINE, b.relays(card)[-1])
        self.assertEqual((m.group(1), m.group(2)), ("clickup-context-g1.md", _sha(original)))
        for _ in range(3):  # ordinary scans (each a fresh process run) settle the wake and sweep staging
            w.clock.advance(1861)
            w.run()
        bundle = os.path.join(w.runtime, "staging", _sha(key.rsplit(":", 1)[0] + ":unblock")[:16])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 0)
        mode["fail"] = False
        return life, key, card, original, stored, bundle

    def test_resend_after_partial_context_upload_reuses_the_original_bundle_and_wakes_once(self):
        w = World(self)
        life, key, card, original, stored, bundle = self._exhausted(w)
        b = w.board
        self.assertEqual(w.main("outbox", "resend", key), (0, ["resend done"]))
        seen = []

        def script(cid, ref, gen):
            seen.append(ref)
            b.park(cid, out("business_questions", ref, "Which users need dark mode?"), classify(ref, gen))
        w.step(lambda: bool(seen), world=life.planner(script), limit=15)
        for _ in range(5):
            w.clock.advance(61)
            w.run()
        names = [a["filename"] for a in b.cards[card]["atts"]]
        self.assertEqual(sorted(names), ["a1001-mock.png", "clickup-context-g1.md", "clickup-ledger.md"])
        self.assertEqual(stored.count("clickup-context-g1.md"), 1)  # the applied context was not sent again
        ctx = b.cards[card]["atts"][names.index("clickup-context-g1.md")]
        self.assertEqual(_read(ctx["stored_path"]), original)
        self.assertTrue(kp.attachment_matches(b.attachments(card).json, "clickup-context-g1.md", ctx["stored_path"],
                                              uploaded_by="clickup-intake"))
        m = re.search(SNAPSHOT_LINE, b.relays(card)[-1])
        self.assertEqual(m.group(2), _sha(original))  # the relay comment names the attached bytes
        self.assertEqual(len(b.relays(card)), 1)
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key=?", key), [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 1)
        self.assertEqual(life.planner_turns, 1)
        self.assertFalse(os.path.exists(bundle))  # retry staging reclaimed after the relay resolved
        self.assertEqual(operator_lines(w), ["Kanban kb_attach outbox %s failed after 6 attempts; task on hold" % key,
                                             "resend done"])

    def test_abandon_after_partial_context_upload_reclaims_the_bundle_and_wakes_nothing(self):
        w = World(self)
        life, key, card, original, stored, bundle = self._exhausted(w)
        b = w.board
        self.assertTrue(os.path.isfile(os.path.join(bundle, "clickup-context-g1.md")))  # kept while resendable
        self.assertEqual(w.main("outbox", "abandon", key), (0, ["abandon done"]))
        for _ in range(5):
            w.clock.advance(61)
            w.run()
        self.assertEqual(w.q("SELECT state, outcome FROM outbox WHERE idem_key=?", key),
                         [{"state": "cancelled_stale", "outcome": "ABANDONED"}])
        self.assertFalse(os.path.exists(bundle))
        self.assertEqual([a["filename"] for a in b.cards[card]["atts"]], ["clickup-context-g1.md"])
        self.assertEqual(_read(b.cards[card]["atts"][0]["stored_path"]), original)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.cards[card]["events"]), 0)


# ------------------------------------------------------------------------------------------ T9
class T9FaultInjectionTest(unittest.TestCase):
    def test_crash_after_every_external_call_and_before_every_commit_has_no_duplicates(self):
        w = World(self, crash=True)
        life = Lifecycle(w)
        life.full()
        self.assertGreater(w.inj.crashes, 100)
        kinds = {sig[0] for sig in w.inj.seen}
        self.assertEqual(kinds, {"cu", "kb", "commit"})
        assert_no_duplicates(self, w, life)
        self.assertEqual(operator_lines(w), [])  # no alert left behind by a crash between the two passes


# ------------------------------------------------------------------------------------- two-pass alerts
class TwoPassAlertTest(unittest.TestCase):
    def test_genuine_alerts_print_exactly_once(self):
        """The card-less first pass neither raises its own alerts nor swallows genuine ones."""
        w = World(self)
        life = Lifecycle(w)
        life.pickup_to_business()
        bold = [{"text": "Business requirements closed", "attributes": {"bold": True}}]
        life.say(REQ, "Business requirements closed", parts=bold)  # UNKNOWN shape: notice + drift alert
        life.business_to_technical()
        life.technical_to_published()
        life.approve_to_released()
        life.orchestrate()
        life.e2e_done_to_ticketed()
        life.say(APPROVER_B, " one more thing?", mention=BOT)  # new input in promotion_window revokes the ticket
        w.step(lambda: w.q("SELECT state FROM approvals")[0]["state"] == "revoked")
        for _ in range(3):
            w.clock.advance(61)
            w.run()
        lines = operator_lines(w)
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("ClickUp schema drift: new UNKNOWN comment shape"), lines)
        self.assertEqual(lines[1], "ticket revoked: task %s r1 epoch 1 reason new_input" % life.tid)

    def test_crash_after_the_first_pass_queued_a_false_alert_prints_nothing(self):
        w = World(self)
        life = Lifecycle(w)
        real, crashed = glue_main.engine.evaluate, []

        def evaluate(store, now, obs, cfg):
            new = real(store, now, obs, cfg)
            if not obs.cards and not crashed and any(i.kind == "alert" for i in new):
                crashed.append([i.idem_key for i in new if i.kind == "alert"])
                raise Crash()
            return new
        with mock.patch.object(glue_main.engine, "evaluate", evaluate):
            life.pickup_to_business()
            life.business_to_technical()
            life.technical_to_published()
            life.approve_to_released()
        self.assertEqual(crashed, [["clickup-dp:alert:source:a1"]])
        self.assertEqual(operator_lines(w), [])


# ------------------------------------------------------------- E2E-F2: repeated relays, real Kanban CLI
HERMES_EXE = os.environ.get("CLICKUP_INTAKE_TEST_HERMES_EXE", "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe")
CLI_HOME = os.path.join(TMP_ROOT, "s4-cli-home")
CLI_BOARD = "clickup-s4-relays"
CLI_BOARD_DB = os.path.join(CLI_HOME, "kanban", "boards", CLI_BOARD, "kanban.db")


def cli_env(**extra):
    """The test_cli_contract isolation: no inherited HERMES_* pins, temporary home, no lazy installs."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HERMES_HOME=CLI_HOME, HERMES_KANBAN_HOME=CLI_HOME, HERMES_DISABLE_LAZY_INSTALLS="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def cli(*args, env=None):
    p = subprocess.run([HERMES_EXE, "kanban", "--board", CLI_BOARD] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env or cli_env(), timeout=300)
    if p.returncode != 0:
        raise AssertionError("hermes kanban %s failed: %s %s" % (args[0], p.stdout, p.stderr))
    return p.stdout


class CliBoard(kp.KanbanCli):
    """The glue's real ``KanbanCli`` port on the isolated board, plus planner/reviewer actions through the same
    installed CLI (``claim``, worker comments, ``schedule``, ``attach`` + ``request-review``, ``complete``)."""

    def __init__(self):
        super().__init__(HERMES_EXE, CLI_BOARD, discord_chat=CHAT, env=cli_env())
        self.requests = []
        self.created = {}

    def create(self, title, *, idempotency_key, **kw):
        res = super().create(title, idempotency_key=idempotency_key, **kw)
        if res.outcome is Outcome.OK:
            self.created[idempotency_key] = res.json["id"]
        if res.outcome is Outcome.OK and ":req:" in idempotency_key and res.json["id"] not in self.requests:
            self.requests.append(res.json["id"])
        return res

    def relays(self, card):
        return [c["body"] for c in self.show(card).json["comments"] if c["body"].startswith("CLICKUP-IN v1 ")]

    def by_key(self, prefix):
        """Glue-created cards by idempotency-key prefix (the FakeBoard helper ``Lifecycle.orch/gate`` use)."""
        return [cid for key, cid in sorted(self.created.items()) if key.startswith(prefix)]

    def _worker_env(self, card):
        return cli_env(HERMES_KANBAN_TASK=card, HERMES_KANBAN_RUN_ID=str(kp.latest_run_id(self.show(card).json)),
                       HERMES_KANBAN_DB=CLI_BOARD_DB)

    def comment_as(self, card, author, body):
        cli("comment", card, "--author", author, "--", body)

    def park(self, card, *outputs):
        for body in outputs:
            self.comment_as(card, "planner", body)
        cli("schedule", card, "CLICKUP_WAIT", env=self._worker_env(card))

    def review_and_pass(self, card, task, rev, text, accounts_gen):
        name = "clickup-plan-r%d.md" % rev
        path = os.path.join(CLI_HOME, "planner-" + name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        env = self._worker_env(card)
        cli("attach", card, path, "--name", name, "--author", "planner", env=env)
        cli("request-review", card, "--summary", "plan r%d" % rev, "--reviewer", "plan-reviewer", "--metadata",
            json.dumps({"clickup_task": task, "plan_revision": rev, "plan_attachment": name,
                        "plan_sha256": _sha(text), "accounts_gen": accounts_gen}), env=env)
        cli("complete", card, "--summary", "PASS: plan r%d approved for owner review" % rev)
        return _sha(text)


class _IsolatedCliHome(unittest.TestCase):
    """A fresh isolated HERMES_HOME with the disposable board for one test class; removed, absence asserted."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(CLI_HOME):
            raise RuntimeError("stale temporary home %s; remove it first" % CLI_HOME)
        os.makedirs(CLI_HOME)
        try:
            p = subprocess.run([HERMES_EXE, "kanban", "boards", "create", CLI_BOARD], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=cli_env(), timeout=300)
            if p.returncode != 0 or os.path.normcase(os.path.abspath(CLI_BOARD_DB)) not in os.path.normcase(p.stdout):
                raise RuntimeError("board create did not land in the temporary home: %s %s" % (p.stdout, p.stderr))
        except BaseException:
            shutil.rmtree(CLI_HOME)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(CLI_HOME)
        if os.path.exists(CLI_HOME):
            raise AssertionError("temporary home still present: %s" % CLI_HOME)


class RepeatedRelayAttachmentNamesCliTest(_IsolatedCliHome):
    """E2E-F2 (source integration regression): the installed CLI stores a repeated attachment name as ``name (n).ext``, so
    each relay's ledger, staged human files and plan copy need names no other relay on the reusable request card
    uses; otherwise the exact-name readback never matches and the next wake never happens (§8.1-§8.3, §10.2)."""

    def test_two_relays_to_one_card_with_repeated_names_attach_read_back_and_wake_again(self):
        w = World(self, board=CLI_BOARD, notify_mode="off")
        w.board = b = CliBoard()
        life = Lifecycle(w)  # the task carries a human upload, so every relay stages that same file again
        turns = []

        def planner():
            for card in b.requests:
                if kp.card_status(b.show(card).json) != "ready":
                    continue
                cli("claim", card)
                ref, gen = relay_of(b, card)
                turns.append(card)
                if len(turns) == 1:  # request 1: straight to a real review PASS of r1
                    b.comment_as(card, "planner", classify(ref, gen))
                    life.plan_sha = b.review_and_pass(card, life.tid, 1, PLAN_R1, gen)
                else:
                    reply_to = w.q("SELECT id FROM items WHERE task=? AND kind='comment' AND cur_mentions_bot=1"
                                   " ORDER BY date_ms DESC LIMIT 1", life.tid)[0]["id"]
                    b.park(card, out("answer", ref, "Answer %d." % (len(turns) - 1), reply_to=reply_to),
                           classify(ref, gen))

        w.step(lambda: life.phase() == "awaiting_approval" and life.bot_q("Plan r1, SHA-256"),
               limit=12, seconds=181, world=planner)
        for n in (1, 2):  # request 2: relay 1 attaches the passed plan; relay 2 repeats plan, ledger and file
            life.say(APPROVER_B, " question %d?" % n, mention=BOT)
            w.step(lambda: len(turns) == n + 1, limit=12, seconds=181, world=planner)
        w.step(lambda: w.task(life.tid)["classified_gen"] == w.task(life.tid)["input_gen"], limit=5, seconds=181)
        first, second = b.requests
        self.assertEqual(turns, [first, second, second])  # the reusable card was woken again by the second relay
        shown = b.show(second).json
        self.assertEqual(sum(e["kind"] == "unblocked" for e in shown["events"]), 2)
        self.assertEqual(kp.card_status(shown), "scheduled")
        relays = b.relays(second)
        self.assertEqual(len(relays), 2)
        glue = [a for a in b.attachments(second).json if a["uploaded_by"] == "clickup-intake"]
        names = [a["filename"] for a in glue]
        self.assertEqual(len(names), 8, names)  # 2 relays x (context, ledger, plan copy, staged upload)
        self.assertEqual(len(set(names)), 8, names)
        self.assertEqual([n for n in names if re.search(r" \(\d+\)", n)], [], names)  # nothing renamed by Hermes
        self.assertEqual(sum(n.startswith("clickup-ledger") for n in names), 2, names)
        self.assertEqual(sum("-mock" in n for n in names), 2, names)
        stored = {a["filename"]: a["stored_path"] for a in glue}
        plans = [n for n in names if n.startswith("clickup-plan-r1")]
        self.assertEqual(len(plans), 2, names)
        self.assertTrue(all(_sha(_read(stored[n])) == life.plan_sha for n in plans))
        # the first relay keeps the names the card body references; each relay comment names its own context
        self.assertIn("clickup-plan-r1.md", shown["task"]["body"])
        self.assertTrue({"clickup-plan-r1.md", "clickup-ledger.md"} <= set(names), names)
        for body in relays:
            m = re.match(r"Refreshed snapshot: (\S+) \(attached, SHA-256 ([0-9a-f]{64})\)", body.splitlines()[2])
            ctx = _read(stored[m.group(1)])
            self.assertEqual(_sha(ctx), m.group(2))
            refs = re.findall(r"; staged as (\S+)", ctx.decode("utf-8"))  # the context names the attached copy
            self.assertEqual(len(refs), 1)
            self.assertIn(refs[0], stored)
        # every attach row went out once: no ambiguous readback, no resend, no hold, no alert
        rows = w.q("SELECT idem_key, state, attempts FROM outbox WHERE kind='kb_attach'")
        self.assertEqual(len(rows), 3)
        self.assertEqual([r for r in rows if (r["state"], r["attempts"]) != ("done", 1)], [])
        self.assertEqual(w.q("SELECT idem_key FROM outbox WHERE state IN ('ambiguous', 'held_ambiguous', 'failed')"),
                         [])
        t = w.task(life.tid)
        self.assertEqual((t["phase"], t["hold_reason"], t["classified_gen"]),
                         ("awaiting_approval", None, t["input_gen"]))
        self.assertEqual(operator_lines(w), [])


class ApprovalCardLedgerGenerationsCliTest(_IsolatedCliHome):
    """QSR643 R7-F1 (§8.2, §8.4 step 3, §10.2; T17-T19): a new input_gen while the approval is still unreleased
    refreshes the context AND ledger on the SAME orchestrator and gate cards. The installed CLI never overwrites a
    name, so each generation's ledger needs its own name or the exact readback never matches and the release is
    never sent. FakeBoard keeps duplicate names, so this runs the R2-F1 sequence on the real installed CLI."""

    def test_new_generation_after_a_failed_release_attaches_its_own_ledger_and_releases_once(self):
        w = World(self, board=CLI_BOARD, notify_mode="off")
        w.board = b = CliBoard()
        life = Lifecycle(w)
        turns, asked = [], []

        def planner():
            for card in b.requests:
                if kp.card_status(b.show(card).json) != "ready":
                    continue
                cli("claim", card)
                ref, gen = relay_of(b, card)
                turns.append(card)
                if len(turns) == 1:  # straight to a real review PASS of r1
                    b.comment_as(card, "planner", classify(ref, gen))
                    life.plan_sha = b.review_and_pass(card, life.tid, 1, PLAN_R1, gen)
                else:
                    b.park(card, out("answer", ref, "Yes, mobile is included.", reply_to=asked[0]), classify(ref, gen))

        w.step(lambda: life.phase() == "awaiting_approval" and life.bot_q("Plan r1, SHA-256"),
               limit=12, seconds=181, world=planner)
        failed = fail_first_unblock(b, lambda card: card == life.orch())
        life.say(APPROVER_A, "Approve plan r1 for development")
        w.step(lambda: bool(failed), limit=12, seconds=181)
        orch, gate = life.orch(), life.gate()
        release = "%%:%s:orch:r1:%%:unblock" % life.tid
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key LIKE ?", release), [{"state": "retry_wait"}])
        old = w.task(life.tid)["input_gen"]
        asked.append(life.say(APPROVER_B, " does this cover mobile?", mention=BOT))
        w.step(lambda: kp.card_status(b.show(orch).json) == "ready", limit=15, seconds=181, world=planner)
        w.step(lambda: w.q("SELECT state FROM approvals WHERE task=?", life.tid) == [{"state": "released"}],
               limit=10, seconds=181)
        t = w.task(life.tid)
        new = t["input_gen"]
        self.assertGreater(new, old)
        self.assertEqual((t["classified_gen"], t["hold_reason"]), (new, None))
        self.assertEqual(failed, [orch])
        self.assertEqual(w.q("SELECT state FROM outbox WHERE idem_key LIKE ?", release), [{"state": "done"}])
        self.assertEqual(sum(e["kind"] == "unblocked" for e in b.show(orch).json["events"]), 1)  # one release
        ledgers = ["clickup-ledger-g%d.md" % g for g in (old, new)]
        held = {}
        for card in (orch, gate):
            glue = [a for a in b.attachments(card).json if a["uploaded_by"] == "clickup-intake"]
            names = [a["filename"] for a in glue]
            self.assertEqual(len(names), len(set(names)), names)
            self.assertEqual([n for n in names if re.search(r" \(\d+\)", n)], [], names)  # nothing renamed
            self.assertTrue(set(ledgers + ["clickup-context-g%d.md" % g for g in (old, new)]) <= set(names), names)
            self.assertNotIn("clickup-ledger.md", names)
            for a in glue:
                self.assertEqual(a["size"], os.path.getsize(a["stored_path"]))
            held[card] = {a["filename"]: a["stored_path"] for a in glue}
        for name in ledgers:  # exact name/size/SHA readback: the gate holds byte-identical copies
            self.assertTrue(kp.attachment_matches(b.attachments(gate).json, name, held[orch][name]), name)
            self.assertIn("| r1 | %s |" % life.plan_sha, _read(held[orch][name]).decode("utf-8"))
        rows = w.q("SELECT idem_key, state, attempts FROM outbox WHERE kind='kb_attach' AND idem_key LIKE ?",
                   "%%:%s:%%:attach:ledger:%%" % life.tid)
        self.assertEqual(len([r for r in rows if ":orch:" in r["idem_key"] or ":gate:" in r["idem_key"]]), 4, rows)
        self.assertEqual([r for r in rows if r["state"] != "done"], [], rows)
        self.assertEqual(w.q("SELECT idem_key FROM outbox WHERE state IN ('ambiguous', 'held_ambiguous', 'failed')"),
                         [])
        self.assertTrue(life.bot_q("Yes, mobile is included."))
        self.assertEqual(operator_lines(w), [])


class SchedulerNoArgumentEntryTest(unittest.TestCase):
    """INSTALL-F1 (source installation regression): the Hermes ``--no-agent`` cron runs the deployed ``.py`` entry with
    no arguments (``sys.argv == [script]``). That must be the ``run`` scan, not an argparse usage error (rc 2).
    The entry is deployed in the §2.1 layout (``<root>/scripts/clickup_dedicated_panel_intake.py`` +
    ``clickup_intake/`` + ``templates/``, config under ``<root>/integrations/clickup-dedicated-panel/``)."""

    def setUp(self):
        os.makedirs(TMP_ROOT, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=TMP_ROOT)
        self.addCleanup(shutil.rmtree, self.dir, True)
        worktree = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        scripts = os.path.join(self.dir, "hermes", "scripts")
        shutil.copytree(os.path.join(worktree, "clickup_intake"), os.path.join(scripts, "clickup_intake"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(worktree, "templates"), os.path.join(scripts, "templates"))
        self.entry = os.path.join(scripts, "clickup_dedicated_panel_intake.py")
        shutil.copy2(os.path.join(worktree, "clickup_dedicated_panel_intake.py"), self.entry)
        self.runtime = os.path.join(self.dir, "hermes", "integrations", "clickup-dedicated-panel")
        os.makedirs(self.runtime)

    def config(self, **cfg):
        with open(os.path.join(self.runtime, "config.json"), "w", encoding="utf-8") as f:
            json.dump(dict({"runtime_dir": self.runtime}, **cfg), f)

    def scheduler_process(self, *args):
        """A real process with the scheduler's argv shape ``[python, script]``: ``sys.argv == [script]``."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, self.entry] + list(args), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=300)

    def test_no_argument_process_with_installed_disabled_config_exits_0_with_no_effects(self):
        self.config(enabled=False, mode="shadow", cutover_ms=None)
        p = self.scheduler_process()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        self.assertEqual(os.listdir(self.runtime), ["config.json"])  # no lock, state or log written
        # Explicit arguments still go to the parser unchanged.
        bad = self.scheduler_process("bogus")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("invalid choice: 'bogus'", bad.stderr)

    def test_no_argument_entry_runs_the_shadow_scan_and_writes_nothing(self):
        self.config(enabled=True, mode="shadow", cutover_ms=None)
        clock = FakeClock()
        cu = FakeClickUp(clock)
        board = FakeBoard(os.path.join(self.dir, "board"), clock)
        tokens = []
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [self.entry]), mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(glue_main.clickup_api, "load_token", lambda p: tokens.append(p) or "tok"), \
                mock.patch.object(glue_main.clickup_api, "HttpTransport", lambda token: cu), \
                mock.patch.object(glue_main.kp, "KanbanCli", lambda *a, **kw: board), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as done:
                runpy.run_path(self.entry, run_name="__main__")
        self.assertEqual(done.exception.code, 0)
        self.assertEqual(tokens, [glue_main.CREDENTIAL])  # the run path reached the ClickUp transport
        self.assertEqual(out.getvalue().splitlines(),
                         ["shadow: preflight OK; discovery pages=1 complete=True admitted=0; task passes=0; "
                          "would-effects=0"])
        self.assertTrue(cu.calls)
        self.assertEqual(cu.writes(), [])
        self.assertEqual(board.mutations(), [])


if __name__ == "__main__":
    unittest.main()
