"""ClickUp technical-blocker recovery (plan r3 §4-§8, §10.1): classification, membership, engine and executor.

Engine cases drive the real Store and engine through the S2 harness ``H`` (``test_s2_workflow``). Executor and
composition cases drive the real ``main`` through ``World``/``Lifecycle`` (``test_s4_composition``) on a board that
also records the native ``claimed`` events, typed blocks and ``wake`` subscriptions that recovery reads.
"""

import dataclasses
import hashlib
import inspect
import json
import os
import re
import sqlite3
import types
import unittest
from unittest import mock

from clickup_intake import engine, render
from clickup_intake import kanban_port as kp
from clickup_intake import main as glue_main
from clickup_intake.contracts import KanbanResult, Outcome
from clickup_intake.engine import Observation, evaluate
from test_s2_workflow import APPROVER_B, PROMOTED, APPROVER_A, REQ, SHA2, H, T
from test_s4_composition import TMP_ROOT, FakeBoard, Lifecycle, World, crash_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "2d53206cb907ae7d1f2e3e8ca0ce0e5170f217fd"  # source provenance; no private Git history is needed
PUBLIC_FIXTURE_BASE = "532c102bdaa5bf2f7238f37973ce9e0563ddf0a9"
BASELINE_ROOT = os.path.join(REPO, "tests", "fixtures", "recovery_base")
REASON = ("The approved r8 amendment needs existing-card archive and foreign-card attach operations that worker "
          "tools do not expose. No new owner approval is needed.")
E_S = 1_792_500_000
MIN = 60_000
_RUN = [900]


def tearDownModule():
    if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
        os.rmdir(TMP_ROOT)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fresh(card="t_w0000001", status="ready"):
    return {"task": {"id": card, "status": status, "title": "W", "assignee": "orchestrator"}, "parents": [],
            "children": [], "comments": [], "runs": [], "events": []}


class Worker:
    """Native lifecycle of one card as ``show --json`` exports it: runs, typed block events, comments."""

    def __init__(self, show, now_s):
        self.s, self.now = show, now_s
        for k in ("runs", "events", "comments", "parents"):
            show.setdefault(k, [])

    def ev(self, event, run=None, **payload):
        self.s["events"].append({"kind": event, "payload": payload, "created_at": self.now(), "run_id": run})

    def claim(self, profile="orchestrator"):
        _RUN[0] += 1
        rid = _RUN[0]
        self.s["runs"].append({"id": rid, "profile": profile, "status": "running", "outcome": None,
                               "started_at": self.now(), "ended_at": None, "worker_pid": 4242})
        self.s["task"]["status"] = "running"
        self.ev("claimed", rid, run_id=rid, lock="host:1")
        return rid

    def block(self, kind="capability", reason=REASON, loop=False, claimed=True):
        if not claimed:  # an operator block of a card without a live run synthesizes a never-claimed run
            _RUN[0] += 1
            self.s["runs"].append({"id": _RUN[0], "profile": None, "status": "running", "outcome": None,
                                   "started_at": self.now(), "ended_at": None, "worker_pid": None})
        run = self.s["runs"][-1]
        run.update(status="blocked", outcome="blocked", ended_at=self.now(), worker_pid=None, summary=reason)
        self.s["task"]["status"] = "triage" if loop else "blocked"
        payload = {"reason": reason, "kind": kind, "recurrences": 2 if loop else 1, "source_status": "ready"}
        if loop:
            payload["limit"] = 2
        self.ev("block_loop_detected" if loop else "blocked", run["id"], **payload)
        return run["id"]

    def unblock(self, status="ready"):
        self.s["task"]["status"] = status
        self.ev("unblocked")

    def triage_to(self, status="ready"):
        self.s["task"]["status"] = status
        self.ev("status", status=status, requested_status=status)

    def comment(self, body, author="default"):
        self.s["comments"].append({"author": author, "body": body, "created_at": self.now()})

    def ack(self, card, run, disposition, author="default"):
        self.comment("CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=%s\ndetails" % (card, run, disposition), author)


def worker_at(t=E_S, card="t_w0000001"):
    clock = [t]
    s = fresh(card)
    return s, Worker(s, lambda: clock[0]), clock


# ============================================================================ §4.2/§4.3 classifier
class ClassifierTest(unittest.TestCase):
    def test_typed_worker_stops_are_actionable_and_bound_to_the_run(self):
        for kind in ("capability", "transient", "needs_input"):
            with self.subTest(kind):
                s, w, _ = worker_at()
                r = w.claim()
                w.block(kind)
                inc = kp.recovery_incident(s)
                self.assertEqual({k: inc[k] for k in ("run_id", "event_kind", "event_created_s", "block_kind",
                                                       "class", "reason", "reason_sha256", "source_status")},
                                 {"run_id": r, "event_kind": "blocked", "event_created_s": E_S, "block_kind": kind,
                                  "class": kind, "reason": REASON, "reason_sha256": _sha(REASON),
                                  "source_status": "ready"})

    def test_triage_through_block_loop_detected_is_a_loop_incident(self):
        s, w, _ = worker_at()
        r = w.claim()
        w.block("capability", loop=True)
        inc = kp.recovery_incident(s)
        self.assertEqual((inc["run_id"], inc["event_kind"], inc["block_kind"], inc["class"]),
                         (r, "block_loop_detected", "capability", "loop"))

    def test_ticket_invalid_needs_input_is_left_to_the_approval_path(self):
        s, w, _ = worker_at()
        w.claim()
        w.block("needs_input", "ClickUp approval ticket INVALID superseded; the promotion cannot proceed")
        self.assertIsNone(kp.recovery_incident(s))

    def test_every_non_actionable_state_produces_nothing(self):
        def initial(w):
            w.s["task"]["status"] = "blocked"
            w.ev("blocked", None, reason="initial_status", status="blocked")

        def unclaimed(w):
            w.block(claimed=False)

        def untyped(w):
            w.claim()
            w.block(kind=None)

        def dependency(w):
            r = w.claim()
            w.s["runs"][-1].update(outcome="dependency", ended_at=w.now())
            w.s["task"]["status"] = "todo"
            w.ev("dependency_wait", r, kind="dependency", reason="CLICKUP-QUESTION")

        def scheduled(w):
            r = w.claim()
            w.s["runs"][-1].update(outcome="scheduled", ended_at=w.now())
            w.s["task"]["status"] = "scheduled"
            w.ev("scheduled", r)

        def gave_up(w):
            w.claim()
            w.block()
            w.unblock()
            r2 = w.claim()
            w.s["runs"][-1].update(outcome="gave_up", ended_at=w.now())
            w.s["task"]["status"] = "blocked"
            w.ev("gave_up", r2)

        cases = {"initial_status": initial, "unclaimed_synthesized_run": unclaimed, "untyped": untyped,
                 "dependency_wait": dependency, "scheduled": scheduled, "gave_up": gave_up,
                 "running": lambda w: w.claim(),
                 "review": lambda w: (w.claim(), w.s["task"].update(status="review")),
                 "done": lambda w: (w.claim(), w.block(), w.s["task"].update(status="done")),
                 "archived": lambda w: (w.claim(), w.block(), w.s["task"].update(status="archived")),
                 "unblocked": lambda w: (w.claim(), w.block(), w.unblock()),
                 "reclaimed": lambda w: (w.claim(), w.block(), w.unblock(), w.claim())}
        for name, act in cases.items():
            with self.subTest(name):
                s, w, _ = worker_at()
                act(w)
                self.assertIsNone(kp.recovery_incident(s))


class OutcomeAndAckTest(unittest.TestCase):
    def blocked(self, loop=False):
        s, w, clock = worker_at()
        r = w.claim()
        w.block(loop=loop)
        clock[0] += 30
        return s, w, r, clock

    def test_outcomes_from_native_records(self):
        cases = {"resumed": lambda w: (w.unblock(), w.claim()), "gated": lambda w: w.unblock("todo"),
                 "handed_off": lambda w: w.unblock("ready"), "review_handed_off": lambda w: w.unblock("review"),
                 "closed": lambda w: w.s["task"].update(status="done"),
                 "archived": lambda w: w.s["task"].update(status="archived")}
        expect = {"resumed": "resumed", "gated": "gated", "handed_off": "handed_off",
                  "review_handed_off": "handed_off", "closed": "closed", "archived": "closed"}
        for name, act in cases.items():
            with self.subTest(name):
                s, w, r, _ = self.blocked()
                act(w)
                state, at = kp.recovery_outcome(s, r, E_S)
                self.assertEqual(state, expect[name])
                if state in ("gated", "handed_off"):
                    self.assertEqual(at, E_S + 30)

    def test_loop_triage_status_events_and_an_unchanged_block(self):
        s, w, r, _ = self.blocked(loop=True)
        self.assertEqual(kp.recovery_outcome(s, r, E_S), (None, None))
        w.triage_to("ready")
        self.assertEqual(kp.recovery_outcome(s, r, E_S), ("handed_off", E_S + 30))
        s, w, r, _ = self.blocked(loop=True)
        w.triage_to("todo")
        self.assertEqual(kp.recovery_outcome(s, r, E_S), ("gated", E_S + 30))

    def test_an_unblock_before_the_event_does_not_count(self):
        s, w, clock = worker_at()
        w.claim()
        w.block()
        w.unblock()
        clock[0] += 10
        r2 = w.claim()
        w.block()
        self.assertEqual(kp.recovery_outcome(s, r2, E_S + 10), (None, None))

    def test_acks_are_exact_dated_and_not_the_intakes_own(self):
        s, w, r, clock = self.blocked()
        w.ack("t_w0000001", r, "working")
        self.assertEqual(kp.recovery_ack(s, "t_w0000001", r, E_S), {"final": None, "final_s": None,
                                                                    "working_s": E_S + 30})
        w.ack("t_w0000001", r, "owner", author="clickup-intake")  # the intake's own text is never an ACK
        w.ack("t_w0000001", r + 1, "owner")  # another run
        w.ack("t_other001", r, "owner")  # another card
        w.comment("note CLICKUP-RECOVERY-ACK v1 card=t_w0000001 run=%d disposition=owner" % r)  # not the first line
        self.assertIsNone(kp.recovery_ack(s, "t_w0000001", r, E_S)["final"])
        clock[0] += 5
        w.ack("t_w0000001", r, "cannot_act")
        self.assertEqual(kp.recovery_ack(s, "t_w0000001", r, E_S)["final"], "cannot_act")
        self.assertEqual(kp.recovery_ack(s, "t_w0000001", r, E_S)["final_s"], E_S + 35)

    def test_an_ack_dated_before_the_event_is_ignored(self):
        s, w, clock = worker_at()
        r = w.claim()
        w.ack("t_w0000001", r, "owner")
        clock[0] += 1
        w.block()
        self.assertIsNone(kp.recovery_ack(s, "t_w0000001", r, E_S + 1)["final"])


# ============================================================================ §6.1 admission verdict (pure)
class VerdictTest(unittest.TestCase):
    """10.1 #10: ``recovery_verdict`` truth table."""

    W = "t_w0000001"

    def setUp(self):
        self.s, self.w, self.clock = worker_at(card=self.W)
        self.r = self.w.claim()
        self.w.block()
        a = {"id": 6, "task": "86task1", "rev": 8, "accounts_gen": 4, "state": "released",
             "plan_sha256": "8" * 64, "gate_card": "t_g0000001", "orchestrator_card": self.W, "clickup_gates": None}
        self.a = a
        self.snap = {"enabled": True, "approvals": [a], "members": {self.W: a},
                     "task": {"clickup_task_id": "86task1", "phase": "development", "terminal": 0,
                              "hold_reason": None, "input_gen": 3, "classified_gen": 3, "scope_gen": 2},
                     "incident": {"id": 1, "task": "86task1", "card": self.W, "run_id": self.r,
                                  "event_kind": "blocked", "event_created_s": E_S, "block_kind": "capability",
                                  "class": "capability", "reason_sha256": _sha(REASON), "approval_id": 6,
                                  "approval_rev": 8, "plan_sha256": "8" * 64, "state": "open"}}

    def verdict(self, snap=None, show=None):
        return kp.recovery_verdict(self.snap if snap is None else snap, self.s if show is None else show,
                                   self.W, self.r)

    def mutated(self, **parts):
        snap = json.loads(json.dumps(self.snap))
        for name, change in parts.items():
            if callable(change):
                change(snap)
            else:
                snap[name] = change
        return snap

    def test_current_for_the_exact_open_incident(self):
        v = self.verdict()
        self.assertEqual((str(v), v.code), ("CURRENT incident=1 card=%s run=%d approval=6 rev=8 class=capability"
                                            " source_status=ready" % (self.W, self.r), 0))

    def test_a_loop_incident_is_current_in_triage(self):
        s, w, _ = worker_at(card=self.W)
        r = w.claim()
        w.block(loop=True)
        snap = self.mutated(incident=lambda d: d["incident"].update(run_id=r, event_kind="block_loop_detected",
                                                                     **{"class": "loop"}))
        self.assertEqual(kp.recovery_verdict(snap, s, self.W, r).code, 0)
        snap["incident"]["class"] = "capability"
        self.assertEqual(str(kp.recovery_verdict(snap, s, self.W, r)), "STALE status_triage")

    def test_stale_cases(self):
        def show_with(change):
            s = json.loads(json.dumps(self.s))
            change(s, Worker(s, lambda: E_S + 60))
            return s

        def newer_approval(d):
            d["approvals"].append(dict(d["approvals"][0], id=7, rev=9, state="recorded"))

        cases = {
            "recovery_disabled": self.mutated(enabled=False),
            "no_incident": self.mutated(incident=None),
            "incident_resumed": self.mutated(incident=lambda d: d["incident"].update(state="resumed")),
            "incident_escalated": self.mutated(incident=lambda d: d["incident"].update(state="escalated")),
            "incident_handed_off": self.mutated(incident=lambda d: d["incident"].update(state="handed_off")),
            "terminal": self.mutated(task=lambda d: d["task"].update(terminal=1)),
            "held": self.mutated(task=lambda d: d["task"].update(hold_reason="operator")),
            "phase": self.mutated(task=lambda d: d["task"].update(phase="awaiting_approval")),
            "approval_revoked": self.mutated(approvals=lambda d: d["approvals"][0].update(state="revoked")),
            "approval_stale": self.mutated(approvals=lambda d: d["approvals"][0].update(state="stale")),
            "not_current": self.mutated(task=lambda d: d["task"].update(scope_gen=5)),
            "superseded": self.mutated(approvals=newer_approval),
            "not_member": self.mutated(members={}),
        }
        for reason, snap in cases.items():
            with self.subTest(reason):
                v = self.verdict(snap)
                self.assertEqual((str(v), v.code), ("STALE " + reason, 3))
        shows = {
            "status_ready": show_with(lambda s, w: w.unblock()),
            "status_running": show_with(lambda s, w: (w.unblock(), w.claim())),
            "status_done": show_with(lambda s, w: s["task"].update(status="done")),
            "newer_run": show_with(lambda s, w: (w.unblock(), w.claim(), w.block())),
            "open_run": show_with(lambda s, w: s["runs"][-1].update(ended_at=None)),
            "worker_pid": show_with(lambda s, w: s["runs"][-1].update(worker_pid=99)),
            "event_changed": show_with(lambda s, w: s["events"][-1].update(created_at=E_S + 1)),
            "reason_changed": show_with(lambda s, w: s["events"][-1]["payload"].update(reason="other")),
        }
        expect = {"worker_pid": "open_run", "reason_changed": "event_changed"}
        for name, show in shows.items():
            with self.subTest(name):
                v = self.verdict(show=show)
                self.assertEqual((str(v), v.code), ("STALE " + expect.get(name, name), 3))

    def test_stale_when_the_stored_event_or_approval_binding_differs(self):
        """F3 (§4.5, §5.2): E's kind and block kind and A*'s rev and plan digest must equal the incident's
        stored binding. Deliberately inconsistent synthetic snapshots, one field each; inputs are never written."""
        def show_with(change):
            s = json.loads(json.dumps(self.s))
            change(s["events"][-1])
            return s

        def incident(**cols):
            return self.mutated(incident=lambda d: d["incident"].update(**cols))

        def approval(**cols):
            return self.mutated(approvals=lambda d: d["approvals"][0].update(**cols))

        cases = {
            "observed event kind": (self.snap, show_with(lambda e: e.update(kind="block_loop_detected")),
                                    "event_changed"),
            "observed block kind": (self.snap, show_with(lambda e: e["payload"].update(kind="transient")),
                                    "event_changed"),
            "stored event kind": (incident(event_kind="block_loop_detected"), self.s, "event_changed"),
            "stored block kind": (incident(block_kind="needs_input"), self.s, "event_changed"),
            "approval plan digest": (approval(plan_sha256="9" * 64), self.s, "approval_changed"),
            "approval rev": (approval(rev=9), self.s, "approval_changed"),
            "stored plan digest": (incident(plan_sha256="7" * 64), self.s, "approval_changed"),
            "stored approval rev": (incident(approval_rev=7), self.s, "approval_changed"),
        }
        for name, (snap, show, reason) in cases.items():
            with self.subTest(name):
                before = json.dumps([snap, show], sort_keys=True)
                v = self.verdict(snap, show)
                self.assertEqual((str(v), v.code), ("STALE " + reason, 3))
                self.assertEqual(json.dumps([snap, show], sort_keys=True), before)
        self.assertEqual(self.verdict().code, 0)  # the unchanged binding stays CURRENT

    def test_unavailable_state_or_show(self):
        self.assertEqual((str(self.verdict(snap=False)), self.verdict(snap=False).code),
                         ("UNAVAILABLE state_unavailable", 2))
        v = kp.recovery_verdict(self.snap, None, self.W, self.r)
        self.assertEqual((str(v), v.code), ("UNAVAILABLE show_unavailable", 2))


# ============================================================================ §6/§8 carrier body bounds
class CarrierBodyBoundTest(unittest.TestCase):
    """F1 (r3 §6, §8): the reason is clipped to 4,000 characters and the whole rendered body to 16 KB of
    UTF-8, on whole characters; the complete binding, full-reason digest and fixed procedure are always kept."""

    SENTINEL = "@@REASON-SENTINEL@@"
    LIMIT = 16 * 1024  # r3 §8, UTF-8 bytes of the whole body

    def params(self, reason):
        return {"task": "86task1", "card": "t_w0000001", "run_id": 901, "event_kind": "blocked",
                "event_created_s": E_S, "block_kind": "capability", "class": "capability", "reason": reason,
                "reason_sha256": _sha(reason), "approval_id": 6, "rev": 8, "plan_sha256": "8" * 64, "attempt": 3,
                "adopted": 1, "source_status": "ready"}

    def rendered(self, reason):
        """(body, the reason as rendered); the rest of the body is byte-identical to the reason-free rendering."""
        p = self.params(reason)
        head, tail = render.recovery_card_body(dict(p, reason=self.SENTINEL)).split(self.SENTINEL)
        self.assertEqual(head + tail, render.recovery_card_body(dict(p, reason="")))
        body = render.recovery_card_body(p)
        self.assertTrue(body.startswith(head) and body.endswith(tail))
        shown = body[len(head):len(body) - len(tail)]
        self.assertEqual(body, head + shown + tail)
        for fixed in (_sha(reason), "plan SHA-256 " + "8" * 64, "Wake attempt 3 of 3; adopted 1",
                      render.recovery_check_command("t_w0000001", 901), "## 5. Owner-only or cannot act",
                      "complete W on the worker's behalf."):
            self.assertIn(fixed, head + tail)
        self.assertEqual(shown, reason[:len(shown)])  # a prefix of whole characters
        self.assertNotIn("\ufffd", shown)
        self.assertLessEqual(len(body.encode("utf-8")), self.LIMIT)
        return body, shown

    def test_an_ascii_reason_is_clipped_to_4000_characters_within_the_byte_bound(self):
        body, shown = self.rendered("r" * 4100)
        self.assertEqual(shown, "r" * 4000)

    def test_a_four_byte_reason_is_clipped_to_the_byte_bound(self):
        reason = "\U0001f6a7" * 4000  # 16,000 bytes: alone it nearly fills the bound
        body, shown = self.rendered(reason)
        self.assertGreater(len(shown), 0)
        self.assertLess(len(shown), 4000)
        self.assertGreater(len(body.encode("utf-8")) + 4, self.LIMIT)  # maximal

    def test_a_mixed_width_reason_is_cut_between_characters(self):
        mixed = "a\u00e9\u4e2d\U0001f6a7" * 1001  # 1-, 2-, 3- and 4-byte characters, 10 bytes per cycle
        short = []
        for pad in range(10):  # ten byte offsets: the bound falls inside a multibyte character for several
            with self.subTest(pad):
                text = ("x" * pad + mixed)[:4003]
                body, shown = self.rendered(text)
                self.assertLess(len(shown), 4000)
                nxt = text[len(shown)]
                self.assertGreater(len(body.encode("utf-8")) + len(nxt.encode("utf-8")), self.LIMIT)
                short.append(len(body.encode("utf-8")) < self.LIMIT)
        self.assertTrue(any(short))  # at least one bound fell inside a multibyte character, which was dropped whole


# ============================================================================ §4.1 membership
def ns(h):
    return types.SimpleNamespace(c=h.store.conn)


def enable(h, at=None):
    h.store.ensure_relay_tables()
    h.store.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                         (engine.RECOVERY_KEY, json.dumps(h.now if at is None else at)))


def amended(tc):
    """Synthetic S2 amendment shape (§4.1): r1 started with ``clickup_gates``, then the r2
    amendment approved and released with ``clickup_gates`` NULL; its orchestrator row carries lineage=[r1]."""
    h = H(tc)
    h.developing()
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
    h.approve("a2", APPROVER_B, rev=2)
    a1, a2 = h.approvals()
    tc.assertEqual((a1["state"], a1["revoked_reason"], a2["state"], a2["clickup_gates"], h.task()["phase"]),
                   ("revoked", "superseded", "released", None, "development"))
    return h, a1, a2


class MembershipTest(unittest.TestCase):
    def test_reproduction_shape_is_the_live_orchestrator_plus_the_handed_lineage(self):
        h, a1, a2 = amended(self)
        o = json.loads(h.store.conn.execute("SELECT body FROM outbox WHERE idem_key=?",
                                            ("clickup-dp:%s:orch:r2:g%d" % (T, a2["accounts_gen"]),)).fetchone()[0])
        self.assertEqual([x["approval_id"] for x in o["params"]["lineage"]], [a1["id"]])
        g1 = json.loads(a1["clickup_gates"])
        members = engine._recovery_members(ns(h), T)
        self.assertEqual({c: a["id"] for c, a in members.items()},
                         {c: a2["id"] for c in [a2["orchestrator_card"], g1["final_e2e"], g1["promotion"],
                                                g1["cleanup"]] + g1["lineage"]})
        self.assertNotIn(a1["gate_card"], members)
        self.assertNotIn(a2["gate_card"], members)
        q = h.new_card(status="blocked")
        h.cards[q]["task"]["title"] = "CLICKUP-QUESTION v1 " + a2["orchestrator_card"]
        h.cards[a2["orchestrator_card"]]["parents"].append(q)
        self.assertNotIn(q, engine._recovery_members(ns(h), T))
        self.assert_rule1_is_live_watched(h)

    def assert_rule1_is_live_watched(self, h):
        members = engine._recovery_members(ns(h), T)
        live = {c: a["id"] for c, a in engine._watched(ns(h), T).items() if a["state"] in engine.WATCHED_STATES}
        self.assertEqual({c: members[c]["id"] for c in live}, live)

    def test_an_approval_revoked_for_new_input_contributes_nothing(self):
        h = H(self)
        h.ticketed()
        h.post("late", REQ, "one more thing", mention=True)
        self.assertEqual([(a["state"], a["revoked_reason"]) for a in h.approvals()], [("revoked", "new_input")])
        self.assertTrue(engine._watched(ns(h), T))
        self.assertEqual(engine._recovery_members(ns(h), T), {})

    def test_a_promoted_approvals_cards_are_members(self):
        h = H(self)
        fe, promo = h.ticketed()
        h.done(promo, sha=PROMOTED)
        h.settle()
        h.cycle()
        a = h.approvals()[-1]
        self.assertEqual(a["state"], "promoted")
        g = json.loads(a["clickup_gates"])
        self.assertEqual(set(engine._recovery_members(ns(h), T)),
                         {a["orchestrator_card"], g["final_e2e"], g["promotion"], g["cleanup"]})
        self.assert_rule1_is_live_watched(h)

    def stage(self, h, approvals, lineage_revs):
        """Approval rows plus a live approval whose orchestrator-create row the UNMODIFIED ``engine._stage`` builds."""
        c = h.store.conn
        for rev, gates, gate in approvals:
            c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, revoked_reason, gate_card,"
                      " orchestrator_card, clickup_gates) VALUES (?,?,?,?,?,?,?,?,?)",
                      (T, rev, 0, str(rev) * 64, "revoked", "superseded", gate, "t_o000000%d" % rev,
                       json.dumps(gates) if gates else None))
        rev = len(approvals) + 1
        aid = c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, comment_id, author_id,"
                        " author_name) VALUES (?,?,?,?,?,?,?,?)",
                        (T, rev, 0, str(rev) * 64, "recorded", "ap", APPROVER_A, "Approver One")).lastrowid
        c.execute("INSERT INTO outbox(idem_key, task, kind, state, result_ref) VALUES (?,?,?,?,?)",
                  ("clickup-dp:%s:gate:r%d:g0" % (T, rev), T, "kb_create", "done", "t_g000000%d" % rev))
        ctx = engine._Ctx(h.store, h.now, Observation(), h.cfg)
        engine._stage(ctx, T, ctx.approval(aid))
        ok = "clickup-dp:%s:orch:r%d:g0" % (T, rev)
        body = json.loads(c.execute("SELECT body FROM outbox WHERE idem_key=?", (ok,)).fetchone()[0])
        self.assertEqual([x["rev"] for x in body["params"]["lineage"]], lineage_revs)
        orch = "t_o000000%d" % rev
        c.execute("UPDATE outbox SET state='done', result_ref=? WHERE idem_key=?", (orch, ok))
        c.execute("UPDATE approvals SET state='released', gate_card=?, orchestrator_card=? WHERE id=?",
                  ("t_g000000%d" % rev, orch, aid))
        return aid, orch, body

    def test_a_superseded_approval_the_live_orchestrator_row_does_not_name_contributes_nothing(self):
        h = H(self)
        h.seed(phase="development")
        aid, orch, _ = self.stage(h, [(1, None, "t_g0000001")], [])
        self.assertEqual({c: a["id"] for c, a in engine._recovery_members(ns(h), T).items()}, {orch: aid})

    def test_two_predecessor_lineage_hands_over_only_its_last_element(self):
        """r3 control: P1 (old-only card X) and P2 (carries Y forward) are both in ``params.lineage``; only P2's
        snapshot is handed over by the orchestrator body, so only P2's cards (Y included) are members."""
        h = H(self)
        h.seed(phase="development")
        x, y = "t_00000fa1", "t_00000fb2"
        g1 = {"plan_revision": 1, "plan_sha256": "1" * 64, "final_e2e": "t_e0000001", "promotion": "t_p0000001",
              "cleanup": "t_c0000001", "lineage": ["t_e0000001", "t_p0000001", "t_c0000001", x, y]}
        g2 = {"plan_revision": 2, "plan_sha256": "2" * 64, "final_e2e": "t_e0000002", "promotion": "t_p0000002",
              "cleanup": "t_c0000002", "lineage": ["t_e0000002", "t_p0000002", "t_c0000002", y]}
        aid, orch, body = self.stage(h, [(1, g1, "t_g0000001"), (2, g2, "t_g0000002")], [1, 2])
        p2 = {"t_e0000002", "t_p0000002", "t_c0000002", y}
        members = engine._recovery_members(ns(h), T)
        self.assertEqual({c: a["id"] for c, a in members.items()}, {c: aid for c in p2 | {orch}})
        handed = engine._handed_lineage(body["params"])
        cards = lambda g: {g[k] for k in ("final_e2e", "promotion", "cleanup")} | set(g.get("lineage") or [])  # noqa
        self.assertEqual(cards(handed), p2)
        # ... which is exactly what the UNMODIFIED Glue.card_body hands to render.orchestrator_card_body
        g = glue_main.Glue(h.cfg, h.store, None, None, h.now)
        with mock.patch.object(glue_main.Glue, "plan_bytes", return_value=b"plan"), \
                mock.patch.object(render, "orchestrator_card_body", return_value="body") as ocb:
            g.card_body(T, body)
        self.assertEqual(cards(ocb.call_args[0][9]), cards(handed))

        enable(h)
        shown = []

        class Port:
            discord_chat = h.cfg.discord_chat

            def show(self, card):
                shown.append(card)
                return KanbanResult(Outcome.OK, h.cards.get(card) or fresh(card, "todo"))
        glue_main.Glue(h.cfg, h.store, None, Port(), h.now)._observe_recovery({}, set())
        self.assertEqual(set(shown), p2 | {orch})
        self.assertNotIn(x, shown)
        for card in (x, y):
            h.cards[card] = fresh(card)
            w = Worker(h.cards[card], lambda: h.now // 1000)
            w.claim()
            w.block()
        for _ in range(3):
            h.clock.advance(61)
            h.scan()
        self.assertEqual([r["card"] for r in rows(h, "recoveries")], [y])
        self.assertFalse([r for r in h.rows() if ":rcv:%s:" % x in r["idem_key"] or x in (r["target"] or "")])
        self.assertTrue([r for r in h.rows() if ":rcv:%s:" % y in r["idem_key"]])

    def test_another_approvals_gate_card_is_never_a_rule_1_member(self):
        """r3 §4.1 'never a member: the gate_card of any approval row', also when a live approval's
        ``clickup_gates`` lists it; ``_watched`` itself is unchanged and still returns it."""
        h = H(self)
        h.seed(phase="development")
        aid, orch, _ = self.stage(h, [(1, None, "t_g0000001")], [])
        g = {"plan_revision": 2, "plan_sha256": "2" * 64, "final_e2e": "t_e0000002", "promotion": "t_p0000002",
             "cleanup": "t_c0000002", "lineage": ["t_e0000002", "t_p0000002", "t_c0000002", "t_g0000001"]}
        h.store.conn.execute("UPDATE approvals SET state='started', clickup_gates=? WHERE id=?", (json.dumps(g), aid))
        self.assertIn("t_g0000001", engine._watched(ns(h), T))
        members = engine._recovery_members(ns(h), T)
        self.assertEqual({c: a["id"] for c, a in members.items()},
                         {c: aid for c in (orch, "t_e0000002", "t_p0000002", "t_c0000002")})

    def test_watched_is_byte_identical_to_the_baseline(self):
        # The immutable public fixture is the sanitized equivalent of source release BASE.
        with open(os.path.join(BASELINE_ROOT, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["source_revision"], BASE)
        self.assertEqual(manifest["public_fixture_base"], PUBLIC_FIXTURE_BASE)
        entry = next(e for e in manifest["files"] if e["path"] == "clickup_intake/engine.py")
        with open(os.path.join(BASELINE_ROOT, entry["path"]), "rb") as f:
            data = f.read()
        self.assertEqual(len(data), entry["size"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
        base = data.decode("utf-8").replace("\r\n", "\n")
        m = re.search(r"^def _watched\(.*?(?=^def )", base, re.S | re.M)
        self.assertEqual(inspect.getsource(engine._watched).rstrip(), m.group(0).rstrip())


# ============================================================================ engine (S2 harness)
def rows(h, table, where="1=1"):
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM %s WHERE %s ORDER BY rowid" % (table, where))]


class Rec:
    """Scenario A: approval r1 ``started`` in ``development``; W is its final_e2e card (rule 1)."""

    def __init__(self, tc, ticketed=False):
        self.tc = tc
        self.h = h = H(tc)
        self.fe, self.promo = h.ticketed() if ticketed else h.developing()
        self.a = h.approvals()[-1]
        self.cleanup = json.loads(self.a["clickup_gates"])["cleanup"]
        enable(h)
        h.clock.advance(5)
        self.W = self.cleanup if ticketed else self.fe
        self.w = self.worker(self.W)

    def worker(self, card):
        return Worker(self.h.cards[card], lambda: self.h.now // 1000)

    def scan(self, minutes=0):
        self.h.clock.advance(minutes * 60)
        return self.h.scan()

    def at(self, detect_ms, minutes):
        self.h.clock.now_ms = detect_ms + minutes * MIN
        return self.h.scan()

    def incidents(self):
        return rows(self.h, "recoveries")

    def rcv(self, attempt=None):
        out = [r for r in self.h.rows() if ":rcv:" in r["idem_key"] and r["kind"] != "alert"]
        return [r for r in out if attempt is None or (":%d:" % attempt) in r["idem_key"].rsplit(":", 2)[0] + ":"]

    def alerts(self):
        return [json.loads(r["body"])["text"] for r in self.h.rows("alert") if ":rcv:" in r["idem_key"]]

    def blocked(self, **kw):
        r = self.w.claim()
        self.h.clock.advance(1)
        self.w.block(**kw)
        self.scan(1)
        return r


class EngineDetectionTest(unittest.TestCase):
    def test_first_event_queues_one_bound_chain_and_nothing_on_w(self):
        rec = Rec(self)
        r = rec.blocked()
        [inc] = rec.incidents()
        self.assertEqual({k: inc[k] for k in ("task", "card", "run_id", "event_kind", "block_kind", "class",
                                              "reason_sha256", "approval_id", "approval_rev", "plan_sha256", "adopted",
                                              "state", "attempts")},
                         {"task": T, "card": rec.W, "run_id": r, "event_kind": "blocked", "block_kind": "capability",
                          "class": "capability", "reason_sha256": _sha(REASON), "approval_id": rec.a["id"],
                          "approval_rev": 1, "plan_sha256": rec.a["plan_sha256"], "adopted": 0, "state": "open",
                          "attempts": 1})
        k = "clickup-dp:%s:rcv:%s:%d:1" % (T, rec.W, r)
        self.assertEqual([(x["idem_key"], x["kind"]) for x in rec.rcv()],
                         [(k + ":create", "kb_create"), (k + ":subscribe", "kb_subscribe"), (k + ":wake", "kb_complete")])
        create = json.loads(rec.rcv()[0]["body"])
        self.assertEqual((create["title"], create["assignee"], create["skills"], create["initial_status"],
                          create["idempotency_key"], create["template"]),
                         ("CLICKUP-RECOVERY v1 %s run=%d attempt=1" % (rec.W, r), None, [], "blocked", k + ":create",
                          "recovery_card"))
        self.assertEqual(json.loads(rec.rcv()[1]["body"])["delivery_mode"], "wake")
        self.assertEqual(json.loads(rec.rcv()[1]["depends_on"]), [k + ":create"])
        self.assertEqual(json.loads(rec.rcv()[2]["depends_on"]), [k + ":subscribe"])
        pre = json.loads(rec.rcv()[0]["preconditions"])
        self.assertEqual(list(pre), ["task_id", "list_id", "phases", "scope_gen", "input_gen", "published_rev",
                                     "approval_id", "approval_states", "wait_run_id"])  # legacy 9-key shape
        self.assertEqual((pre["phases"], pre["approval_id"], pre["approval_states"]),
                         (list(engine.AMENDMENT_PHASES), rec.a["id"], list(engine.WATCHED_STATES)))
        self.assertNotIn(rec.W, [x["target"] for x in rec.h.rows()])  # no-bypass control A
        self.assertFalse([x for x in rec.h.rows() if x["kind"] in ("kb_unblock",) and ":rcv:" in x["idem_key"]])

    def test_pre_cutover_stop_is_not_replayed_and_disabled_recovery_does_nothing(self):
        rec = Rec(self)
        rec.h.store.conn.execute("DELETE FROM meta WHERE key=?", (engine.RECOVERY_KEY,))
        rec.blocked()
        self.assertEqual(rec.incidents(), [])
        enable(rec.h)
        for _ in range(3):
            rec.scan(1)
        self.assertEqual((rec.incidents(), rec.rcv()), ([], []))

    def test_initial_status_unclaimed_and_owner_approval_stops_produce_nothing(self):
        rec = Rec(self)
        w2 = rec.worker(rec.promo)
        w2.block(claimed=False)  # synthesized operator block
        rec.w.claim()
        rec.w.block("needs_input", "ClickUp approval ticket INVALID new_input")
        for _ in range(3):
            rec.scan(1)
        self.assertEqual((rec.incidents(), rec.rcv()), ([], []))
        gate = rec.a["gate_card"]
        self.assertEqual(rec.h.cards[gate]["task"]["status"], "blocked")  # the initial-status gate is never one

    def test_duplicate_polls_add_nothing(self):
        rec = Rec(self)
        rec.blocked()
        n = len(rec.h.rows())
        for _ in range(20):
            rec.scan(1)
        self.assertEqual(len(rec.h.rows()), n)
        self.assertEqual(len(rec.incidents()), 1)


class EngineOutcomeTest(unittest.TestCase):
    """10.1 #6."""

    def test_resumed_gated_and_closed(self):
        for name, act, state in (("resumed", lambda w: (w.unblock(), w.claim()), "resumed"),
                                 ("gated", lambda w: w.unblock("todo"), "gated"),
                                 ("closed", lambda w: w.s["task"].update(status="done"), "closed")):
            with self.subTest(name):
                rec = Rec(self)
                rec.blocked()
                act(rec.w)
                rec.scan(1)
                self.assertEqual([i["state"] for i in rec.incidents()], [state])
                self.assertEqual(rec.alerts(), [])

    def test_handed_off_alerts_once_when_no_worker_starts_within_180_minutes(self):
        rec = Rec(self)
        rec.blocked()
        rec.w.unblock("ready")
        rec.scan(1)
        self.assertEqual(rec.incidents()[0]["state"], "handed_off")
        rec.scan(178)
        self.assertEqual((rec.incidents()[0]["state"], rec.alerts()), ("handed_off", []))
        rec.scan(3)
        self.assertEqual(rec.incidents()[0]["state"], "escalated")
        [text] = rec.alerts()
        self.assertIn("handed off, no worker started", text)
        self.assertEqual(len(rec.rcv()), 3)  # no re-wake while handed off
        rec.scan(600)
        self.assertEqual(len(rec.alerts()), 1)

    def test_a_reblock_on_a_new_run_is_a_new_incident(self):
        rec = Rec(self)
        r1 = rec.blocked()
        rec.w.unblock()
        rec.h.clock.advance(1)
        r2 = rec.blocked()
        self.assertEqual([(i["run_id"], i["state"]) for i in rec.incidents()], [(r1, "resumed"), (r2, "open")])

    def test_a_loop_incident_resumes_through_a_triage_status_event_and_a_claim(self):
        rec = Rec(self)
        r = rec.blocked(loop=True)
        [inc] = rec.incidents()
        self.assertEqual((inc["run_id"], inc["class"], inc["event_kind"]), (r, "loop", "block_loop_detected"))
        rec.w.triage_to("ready")
        rec.scan(1)
        self.assertEqual(rec.incidents()[0]["state"], "handed_off")
        rec.w.claim()
        rec.scan(1)
        self.assertEqual(rec.incidents()[0]["state"], "resumed")

    def test_the_fourth_incident_escalates_without_a_wake(self):
        rec = Rec(self)
        for i in range(4):
            if i:
                rec.w.unblock()
                rec.h.clock.advance(1)
            rec.blocked()
        rec.scan(1)
        self.assertEqual([i["state"] for i in rec.incidents()], ["resumed"] * 3 + ["escalated"])
        self.assertEqual(len(rec.rcv()), 9)
        [text] = rec.alerts()
        self.assertIn("incident cap reached", text)
        self.assertLessEqual(len(text), 600)


class EngineScheduleTest(unittest.TestCase):
    """10.1 #7: attempts at +60/+180, escalation at +360, ACK handling."""

    def attempts(self, rec):
        return sorted({int(r["idem_key"].split(":")[-2]) for r in rec.rcv()})

    def test_retries_then_one_escalation_then_nothing(self):
        rec = Rec(self)
        rec.blocked()
        d = rec.incidents()[0]["detected_ms"]
        for minutes, want in ((59, [1]), (61, [1, 2]), (179, [1, 2]), (181, [1, 2, 3]), (359, [1, 2, 3])):
            rec.at(d, minutes)
            self.assertEqual(self.attempts(rec), want, minutes)
            self.assertEqual((rec.incidents()[0]["state"], rec.alerts()), ("open", []))
        rec.at(d, 361)
        self.assertEqual(rec.incidents()[0]["state"], "escalated")
        [text] = rec.alerts()
        self.assertTrue(text.startswith("ClickUp recovery needs owner attention: %s run %d (capability, ClickUp %s r1)"
                                        " not resumed after 3 AI wakes. Reason: " % (rec.W, rec.incidents()[0]["run_id"],
                                                                                      T)), text)
        n = len(rec.h.rows())
        rec.at(d, 400)
        rec.at(d, 3000)
        self.assertEqual(len(rec.h.rows()), n)

    def test_working_ack_defers_wakes_and_escalation_but_never_beyond_24_hours(self):
        rec = Rec(self)
        r = rec.blocked()
        d = rec.incidents()[0]["detected_ms"]
        rec.at(d, 30)
        rec.w.ack(rec.W, r, "working")
        rec.at(d, 31)
        rec.at(d, 61)
        self.assertEqual(self.attempts(rec), [1])
        rec.at(d, 209)
        self.assertEqual(self.attempts(rec), [1])
        rec.at(d, 211)
        self.assertEqual(self.attempts(rec), [1, 2])
        t = 211
        while t < 24 * 60 - 1:  # a working coordinator keeps acking
            t += 170
            rec.at(d, min(t, 24 * 60 - 1))
            rec.w.ack(rec.W, r, "working")
            self.assertEqual(rec.incidents()[0]["state"], "open", t)
        rec.at(d, 24 * 60 - 1)
        self.assertEqual(rec.incidents()[0]["state"], "open")
        rec.at(d, 24 * 60 + 1)
        self.assertEqual(rec.incidents()[0]["state"], "escalated")
        self.assertEqual(len(rec.alerts()), 1)
        self.assertLessEqual(max(self.attempts(rec)), 3)

    def test_repeated_working_acks_cannot_carry_an_attempt_past_the_24_hour_ceiling(self):
        """F2 (§7.2): valid working ACKs before every deadline keep attempt 2 deferred; the first current
        observation at exactly detect + 24 h escalates once and queues no overdue wake, then or later."""
        rec = Rec(self)
        r = rec.blocked()
        d = rec.incidents()[0]["detected_ms"]
        self.assertEqual(rec.incidents()[0]["abs_ms"], d + 24 * 60 * MIN)
        for minute in range(59, 24 * 60, 170):  # 59, 229, ..., 1419: each before the deferred deadline
            rec.at(d, minute)
            rec.w.ack(rec.W, r, "working")
            rec.at(d, minute + 1)
            inc = rec.incidents()[0]
            self.assertEqual((self.attempts(rec), inc["attempts"], inc["state"], rec.alerts()), ([1], 1, "open", []),
                             minute)
        wake_rows = len(rec.rcv())
        rec.at(d, 24 * 60)
        inc = rec.incidents()[0]
        self.assertEqual((inc["state"], inc["attempts"], self.attempts(rec), len(rec.rcv())),
                         ("escalated", 1, [1], wake_rows))
        [text] = rec.alerts()
        self.assertTrue(text.startswith("ClickUp recovery needs owner attention: %s run %d (capability, ClickUp %s r1)"
                                        " not resumed after 1 AI wakes. Reason: " % (rec.W, r, T)), text)
        self.assertLessEqual(len(text), 600)
        n = len(rec.h.rows())
        for minute in (24 * 60, 24 * 60 + 1, 3000):
            rec.at(d, minute)
        self.assertEqual((len(rec.h.rows()), len(rec.alerts())), (n, 1))

    def test_owner_and_cannot_act_acks_stop_wakes_with_one_alert(self):
        for disposition, why in (("owner", "coordinator: owner"), ("cannot_act", "coordinator: cannot act")):
            with self.subTest(disposition):
                rec = Rec(self)
                r = rec.blocked()
                rec.w.ack(rec.W, r, disposition)
                rec.scan(1)
                self.assertEqual(rec.incidents()[0]["state"], "escalated")
                [text] = rec.alerts()
                self.assertIn(why, text)
                rec.scan(200)
                self.assertEqual(self.attempts(rec), [1])
                self.assertEqual(len(rec.alerts()), 1)

    def test_intake_authored_or_earlier_acks_are_ignored(self):
        rec = Rec(self)
        r = rec.w.claim()
        rec.w.ack(rec.W, r, "owner")  # before the event
        rec.h.clock.advance(2)
        rec.w.block()
        rec.scan(1)
        rec.w.ack(rec.W, r, "owner", author="clickup-intake")
        rec.scan(1)
        self.assertEqual((rec.incidents()[0]["state"], rec.alerts()), ("open", []))


class EngineAuthorityTest(unittest.TestCase):
    """10.1 #8."""

    def test_a_held_task_pauses_and_resumes_its_schedule(self):
        rec = Rec(self)
        rec.blocked()
        d = rec.incidents()[0]["detected_ms"]
        rec.at(d, 59)
        rec.h.store.observe_commit(T, [], {"hold_reason": "operator"})
        rec.at(d, 62)
        self.assertEqual(len(rec.rcv()), 3)
        rec.h.store.observe_commit(T, [], {"hold_reason": None})
        rec.at(d, 63)
        self.assertEqual(len(rec.rcv()), 6)
        self.assertEqual(rec.incidents()[0]["state"], "open")

    def test_terminal_task_supersedes_without_effects(self):
        rec = Rec(self)
        rec.blocked()
        n = len(rec.h.rows())
        rec.h.store.observe_commit(T, [], {"terminal": 1})
        rec.scan(1)
        self.assertEqual(rec.incidents()[0]["state"], "superseded")
        self.assertEqual([r for r in rec.h.rows()[n:] if ":rcv:" in r["idem_key"]], [])

    def test_approval_revoked_by_new_input_supersedes(self):
        rec = Rec(self, ticketed=True)
        self.assertEqual(rec.a["state"], "ticketed")
        rec.blocked()
        self.assertEqual(rec.incidents()[0]["approval_id"], rec.a["id"])
        rec.h.post("late", REQ, "one more thing", mention=True)
        rec.scan(1)
        self.assertEqual(rec.incidents()[0]["state"], "superseded")
        self.assertEqual(rec.alerts(), [])

    def test_the_cards_empty_progress_pass_decides_nothing(self):
        rec = Rec(self)
        r = rec.w.claim()
        rec.w.block()
        rec.h.clock.advance(60)
        empty = Observation(scan_started_ms=rec.h.now - 2, confirmed=frozenset({T}))
        n = len(rec.h.rows())
        self.assertEqual(evaluate(rec.h.store, rec.h.now, empty, rec.h.cfg), [])
        self.assertEqual((rec.incidents(), len(rec.h.rows())), ([], n))
        rec.scan(1)
        d = rec.incidents()[0]["detected_ms"]
        rec.h.clock.now_ms = d + 400 * MIN
        n = len(rec.h.rows())
        evaluate(rec.h.store, rec.h.now, dataclass_replace(empty, scan_started_ms=rec.h.now - 2), rec.h.cfg)
        self.assertEqual((rec.incidents()[0]["state"], len(rec.h.rows())), ("open", n))
        self.assertEqual(rec.incidents()[0]["run_id"], r)


def dataclass_replace(obj, **kw):
    return dataclasses.replace(obj, **kw)


class DisableTest(unittest.TestCase):
    """10.1 #11: ``disable_recovery``."""

    def test_disable_cancels_unsent_rows_archives_unsent_carriers_and_disables_open_incidents(self):
        rec = Rec(self)
        runs = {}
        for card in (rec.fe, rec.promo, rec.cleanup):
            w = rec.worker(card)
            runs[card] = w.claim()
            w.block()
        rec.scan(1)
        self.assertEqual(len(rec.incidents()), 3)
        c = rec.h.store.conn
        key = lambda card, s: "clickup-dp:%s:rcv:%s:%d:1:%s" % (T, card, runs[card], s)  # noqa: E731
        for s in ("create", "subscribe", "wake"):
            c.execute("UPDATE outbox SET state='done' WHERE idem_key=?", (key(rec.fe, s),))
        c.execute("UPDATE outbox SET result_ref='t_cf000001' WHERE idem_key=?", (key(rec.fe, "create"),))
        c.execute("UPDATE outbox SET state='done', result_ref='t_cf000002' WHERE idem_key=?",
                  (key(rec.promo, "create"),))
        c.execute("UPDATE outbox SET state='retry_wait' WHERE idem_key=?", (key(rec.promo, "subscribe"),))
        engine.disable_recovery(rec.h.store, rec.h.now)
        self.assertIsNone(c.execute("SELECT 1 FROM meta WHERE key=?", (engine.RECOVERY_KEY,)).fetchone())
        self.assertEqual([i["state"] for i in rec.incidents()], ["disabled"] * 3)
        state = {r["idem_key"]: r["state"] for r in rec.h.rows()}
        self.assertEqual([state[key(rec.fe, s)] for s in ("create", "subscribe", "wake")], ["done"] * 3)
        self.assertEqual([state[key(rec.promo, s)] for s in ("create", "subscribe", "wake")],
                         ["done", "cancelled_stale", "cancelled_stale"])
        self.assertEqual([state[key(rec.cleanup, s)] for s in ("create", "subscribe", "wake")],
                         ["cancelled_stale"] * 3)
        archives = [(r["idem_key"], r["kind"], r["target"], r["state"]) for r in rec.h.rows()
                    if r["idem_key"].endswith(":archive")]
        self.assertEqual(archives, [(key(rec.promo, "archive"), "kb_archive", "t_cf000002", "pending")])
        self.assertEqual(json.loads(c.execute("SELECT preconditions FROM outbox WHERE idem_key=?",
                                              (key(rec.promo, "archive"),)).fetchone()[0])["phases"], [])
        n = len(rec.h.rows())
        rec.scan(70)
        self.assertEqual(len(rec.h.rows()), n)


# ============================================================================ composition (real main)
class RecoveryBoard(FakeBoard):
    """FakeBoard plus the native records recovery reads: ``claimed`` events, typed blocks, ``wake`` subscriptions."""

    on_subscribe = None

    def _ev(self, cid, event, run=None, **payload):
        self.cards[cid]["events"].append({"kind": event, "payload": payload, "created_at": self._now(),
                                          "run_id": run})

    def claim(self, card, profile):
        rid = super().claim(card, profile)
        self.cards[card]["runs"][-1].update(ended_at=None, worker_pid=4242)
        self._ev(card, "claimed", rid, run_id=rid)
        return rid

    def _end(self, card, outcome, metadata=None):
        super()._end(card, outcome, metadata)
        self.cards[card]["runs"][-1].update(ended_at=self._now(), worker_pid=None)

    def block(self, card, kind="capability", reason=REASON):
        self._end(card, "blocked")
        self.cards[card]["task"]["status"] = "blocked"
        self._ev(card, "blocked", self.cards[card]["runs"][-1]["id"], reason=reason, kind=kind, recurrences=1,
                 source_status="ready")

    def subscribe(self, card, mode="notify"):
        sub = {"platform": "discord", "chat_id": self.discord_chat, "chat_type": "group", "notifier_profile": "default",
               "delivery_mode": mode, "delivery_metadata": {"guild_id": kp.DISCORD_GUILD_ID}}
        if sub not in self.cards[card]["subs"]:
            self.cards[card]["subs"].append(sub)
        self._mut("subscribe", card, mode)
        if self.on_subscribe:
            cb, self.on_subscribe = self.on_subscribe, None
            cb()
        return KanbanResult(Outcome.OK, {"stdout": ""})


def developing_world(tc):
    w = World(tc)
    w.board = RecoveryBoard(os.path.join(w.dir, "board"), w.clock, w.inj)
    life = Lifecycle(w)
    life.pickup_to_business()
    life.business_to_technical()
    life.technical_to_published()
    life.approve_to_released()
    life.orchestrate()
    return w, life


def store_conn(w):
    conn = sqlite3.connect(w.state_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def shown(board, since):
    return [c for name, c in board.calls[since:] if name == "show"]


class CompositionTest(unittest.TestCase):
    def setUp(self):
        self.w, self.life = developing_world(self)
        self.b = self.w.board
        self.a = self.w.q("SELECT * FROM approvals")[-1]

    def enable_and_block(self):
        rc, out = self.w.main("recovery", "enable")
        self.assertEqual((rc, out), (0, ["recovery enabled; cutover %d" % self.w.clock()]))
        self.w.clock.advance(5)
        r = self.b.claim(self.life.fe, "e2e-tester")
        self.w.clock.advance(1)
        self.b.block(self.life.fe)
        self.e_s = self.w.clock() // 1000
        return r

    def rcv(self):
        return {r["idem_key"]: r for r in self.w.q("SELECT * FROM outbox WHERE idem_key LIKE '%:rcv:%'"
                                                   " AND kind != 'alert' ORDER BY rowid")}

    def carriers(self):
        return self.b.by_key("clickup-dp:%s:rcv:" % self.life.tid)

    def test_first_event_wakes_once_and_duplicate_polls_add_nothing(self):
        """10.1 #2 and #3."""
        self.w.clock.advance(61)
        mark = len(self.b.calls)
        self.w.run()
        baseline = set(shown(self.b, mark))
        r = self.enable_and_block()
        fe = self.life.fe
        self.w.step(self.carriers)  # the engine decides on the next scan that confirms the task (§4.1)
        [carrier] = self.carriers()
        k = "clickup-dp:%s:rcv:%s:%d:1" % (self.life.tid, fe, r)
        rows = self.rcv()
        self.assertEqual([(key, x["kind"], x["state"]) for key, x in rows.items()],
                         [(k + ":create", "kb_create", "done"), (k + ":subscribe", "kb_subscribe", "done"),
                          (k + ":wake", "kb_complete", "done")])
        params = json.loads(rows[k + ":create"]["body"])["params"]
        self.assertEqual(params, {"task": self.life.tid, "card": fe, "run_id": r, "event_kind": "blocked",
                                  "event_created_s": self.e_s, "block_kind": "capability", "class": "capability",
                                  "reason": REASON, "reason_sha256": _sha(REASON), "approval_id": self.a["id"],
                                  "rev": 1, "plan_sha256": self.a["plan_sha256"], "attempt": 1, "adopted": 0,
                                  "source_status": "ready"})
        c = self.b.cards[carrier]
        body = render.recovery_card_body(params)
        self.assertEqual(_sha(c["task"]["body"]), _sha(body))
        self.assertIn(render.recovery_check_command(fe, r), body)
        self.assertEqual((c["task"]["title"], c["task"]["assignee"], c["task"]["status"]),
                         ("CLICKUP-RECOVERY v1 %s run=%d attempt=1" % (fe, r), None, "done"))
        self.assertEqual([s["delivery_mode"] for s in c["subs"]], ["wake"])
        [done] = [e for e in c["events"] if e["kind"] == "completed"]
        summary = done["payload"]["summary"]
        self.assertEqual(summary, render.recovery_summary(params, carrier))
        self.assertEqual(summary.splitlines()[0], "CLICKUP-RECOVERY: act as operator for %s run %d (capability) —"
                                                  " read card %s body; approval r1." % (fe, r, carrier))
        self.assertLessEqual(len(summary), 400)
        # no-bypass control A: no effect on W, no unblock/edit/link of any card by recovery
        self.assertEqual([x for x in self.b.mutations()[mark:] if x[1] == fe], [])
        self.assertTrue(all(x["kind"] in ("kb_create", "kb_subscribe", "kb_complete") for x in rows.values()))
        self.assertTrue(all(x["target"] in (None, carrier) for x in rows.values()))
        # #3: twenty unchanged scans
        members = set(engine._recovery_members(types.SimpleNamespace(c=store_conn(self.w)), self.life.tid))
        n_rows, n_out = len(self.w.q("SELECT * FROM outbox")), len(self.w.outputs)
        for _ in range(20):
            mark = len(self.b.calls)
            self.w.clock.advance(61)
            self.w.run()
            seen = shown(self.b, mark)
            self.assertNotIn(carrier, seen)
            self.assertLessEqual(set(seen), baseline | members)
            self.assertEqual(seen.count(fe), 1)
        self.assertEqual((len(self.w.q("SELECT * FROM outbox")), self.w.outputs[n_out:]), (n_rows, []))

    def test_lost_acknowledgement_at_each_effect_sends_each_effect_once(self):
        """10.1 #4: a crash after create, subscribe and complete (each before its row update) and before every
        state commit; reruns produce one carrier, one wake-mode subscription and one ``completed`` event."""
        r = self.enable_and_block()
        p = mock.patch.object(glue_main, "Store", crash_store(self.w.inj))
        p.start()
        self.addCleanup(p.stop)
        self.w.crash = True
        k = "clickup-dp:%s:rcv:%s:%d:1" % (self.life.tid, self.life.fe, r)
        self.w.step(lambda: (self.rcv().get(k + ":wake") or {}).get("state") == "done")
        self.w.crash = False
        [carrier] = self.carriers()
        for sig in (("kb", "create", carrier, k + ":create"), ("kb", "subscribe", carrier, "wake"),
                    ("kb", "complete", carrier)):
            self.assertIn(sig, self.w.inj.seen)
        c = self.b.cards[carrier]
        self.assertEqual([s["delivery_mode"] for s in c["subs"]], ["wake"])
        self.assertEqual(sum(1 for x in self.b.calls if x == ("subscribe", carrier)), 1)
        self.assertEqual(sum(1 for x in self.b.calls if x == ("complete", carrier)), 1)
        self.assertEqual(sum(1 for e in c["events"] if e["kind"] == "completed"), 1)
        for _ in range(3):
            self.w.clock.advance(61)
            self.w.run()
        self.assertEqual(self.carriers(), [carrier])
        self.assertEqual(sum(1 for e in c["events"] if e["kind"] == "completed"), 1)

    def test_a_stale_wake_is_cancelled_and_its_carrier_archived(self):
        """10.1 #5: W moves between incident and send (the fresh ``show`` at the wake gate)."""
        for name, move, outcome in (("unblocked", lambda: self.b.unblock(self.life.fe), "handed_off"),
                                    ("reclaimed", lambda: (self.b.unblock(self.life.fe),
                                                           self.b.claim(self.life.fe, "e2e-tester")), "resumed")):
            with self.subTest(name):
                if name == "reclaimed":
                    self.setUp()
                r = self.enable_and_block()
                self.b.on_subscribe = move
                self.w.step(self.carriers)
                [carrier] = self.carriers()
                k = "clickup-dp:%s:rcv:%s:%d:1" % (self.life.tid, self.life.fe, r)
                rows = self.rcv()
                self.assertEqual((rows[k + ":wake"]["state"], rows[k + ":wake"]["outcome"]), ("cancelled_stale", "STALE"))
                self.assertEqual((rows[k + ":archive"]["kind"], rows[k + ":archive"]["target"],
                                  rows[k + ":archive"]["state"]), ("kb_archive", carrier, "done"))
                c = self.b.cards[carrier]
                self.assertEqual(c["task"]["status"], "archived")
                self.assertFalse([e for e in c["events"] if e["kind"] == "completed"])
                self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] != "open")
                self.assertEqual([i["state"] for i in self.w.q("SELECT state FROM recoveries")], [outcome])

    def test_a_final_ack_at_send_time_cancels_the_wake_and_escalates_once(self):
        """r3 §5.2 'no ACK owner/cannot_act': the ACK lands on W after the scan's observation, before the wake."""
        for disposition, why in (("owner", "coordinator: owner"), ("cannot_act", "coordinator: cannot act")):
            with self.subTest(disposition):
                if disposition == "cannot_act":
                    self.setUp()
                r = self.enable_and_block()
                fe = self.life.fe
                ack = "CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=%s\ndetails" % (fe, r, disposition)
                self.b.on_subscribe = lambda: self.b.worker_comment(fe, "default", ack)
                self.w.step(self.carriers)
                [carrier] = self.carriers()
                k = "clickup-dp:%s:rcv:%s:%d:1" % (self.life.tid, fe, r)
                rows = self.rcv()
                self.assertEqual((rows[k + ":wake"]["state"], rows[k + ":wake"]["outcome"]), ("cancelled_stale", "STALE"))
                self.assertEqual((rows[k + ":archive"]["kind"], rows[k + ":archive"]["target"],
                                  rows[k + ":archive"]["state"]), ("kb_archive", carrier, "done"))
                c = self.b.cards[carrier]
                self.assertEqual(c["task"]["status"], "archived")
                self.assertFalse([e for e in c["events"] if e["kind"] == "completed"])
                self.w.step(lambda: self.w.q("SELECT state FROM recoveries")[0]["state"] != "open")
                self.assertEqual([i["state"] for i in self.w.q("SELECT state FROM recoveries")], ["escalated"])
                for _ in range(3):
                    self.w.clock.advance(61)
                    self.w.run()
                alerts = [json.loads(a["body"])["text"] for a in
                          self.w.q("SELECT body FROM outbox WHERE kind='alert' AND idem_key LIKE '%:rcv:%'")]
                self.assertEqual(len(alerts), 1)
                self.assertIn(why, alerts[0])
                self.assertEqual(self.carriers(), [carrier])

    def test_adopt_accepts_only_an_exact_current_pre_cutover_incident(self):
        """10.1 #9."""
        fe, life = self.life.fe, self.life
        r = self.b.claim(fe, "e2e-tester")
        self.w.clock.advance(1)
        self.b.block(fe)
        self.w.clock.advance(60)
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r)),
                         (1, ["adopt refused: recovery not enabled"]))
        self.w.main("recovery", "enable")
        for _ in range(6):  # past a confirming scan
            self.w.clock.advance(61)
            self.w.run()
        self.assertEqual(self.w.q("SELECT * FROM recoveries"), [])  # pre-cutover: no replay
        # refusals change nothing
        gate = self.a["gate_card"]
        cleanup = json.loads(self.a["clickup_gates"])["cleanup"]
        promo_id = json.loads(self.a["clickup_gates"])["promotion"]
        self.b.cards[cleanup]["task"]["status"] = "running"
        self.b.cards[cleanup]["runs"].append({"id": 7001, "status": "running", "outcome": None, "ended_at": None})
        self.b.block(cleanup)  # a never-claimed run
        r2 = self.b.claim(promo_id, "integrator")
        self.b.block(promo_id, "needs_input", "ClickUp approval ticket INVALID not_ticketed")
        for argv, why in (((fe, r + 1), "run %d is not the latest blocked run of %s (latest %d)" % (r + 1, fe, r)),
                          ((gate, 1), "no actionable typed worker stop on %s" % gate),
                          ((cleanup, 7001), "no actionable typed worker stop on %s" % cleanup),
                          ((promo_id, r2), "no actionable typed worker stop on %s" % promo_id),
                          (("t_00000bad", 1), "card t_00000bad is unreadable")):
            with self.subTest(argv):
                self.assertEqual(self.w.main("recovery", "adopt", "--card", argv[0], "--run", str(argv[1])),
                                 (1, ["adopt refused: " + why]))
        self.assertEqual(self.w.q("SELECT * FROM recoveries"), [])
        rc, out = self.w.main("recovery", "adopt", "--card", fe, "--run", str(r))
        self.assertEqual((rc, out), (0, ["adopted incident 1 card=%s run=%d" % (fe, r)]))
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r)),
                         (0, ["adopted incident 1 card=%s run=%d (existing)" % (fe, r)]))
        [inc] = self.w.q("SELECT * FROM recoveries")
        self.assertEqual((inc["adopted"], inc["state"], inc["attempts"]), (1, "open", 0))
        self.w.step(self.carriers)
        self.assertEqual(self.w.q("SELECT attempts FROM recoveries")[0]["attempts"], 1)
        [carrier] = self.carriers()
        self.assertEqual(self.b.cards[carrier]["task"]["status"], "done")
        # a resolved incident and a missing live approval are refused too
        self.b.unblock(fe)
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r)),
                         (0, ["adopted incident 1 card=%s run=%d (existing)" % (fe, r)]))
        r3 = self.b.claim(fe, "e2e-tester")
        self.b.block(fe)
        self.w.main("hold", life.tid)
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r3)),
                         (1, ["adopt refused: task %s is held" % life.tid]))
        self.w.main("release", life.tid)
        conn = store_conn(self.w)
        conn.execute("UPDATE approvals SET state='revoked', revoked_reason='operator'")
        conn.close()
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r3)),
                         (1, ["adopt refused: %s is not a recovery member of a live approval" % fe]))
        self.b.unblock(fe)
        self.assertEqual(self.w.main("recovery", "adopt", "--card", fe, "--run", str(r3)),
                         (1, ["adopt refused: no actionable typed worker stop on %s" % fe]))


if __name__ == "__main__":
    unittest.main()
