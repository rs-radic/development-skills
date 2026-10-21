"""ClickUp technical-blocker recovery against the real installed ``hermes kanban`` CLI (plan r3 §10.2 #12-#14).

Same isolation as ``test_cli_contract``: ``HERMES_HOME=<worktree>/tests/.tmp/home``, a disposable board, every
inherited ``HERMES_*`` variable dropped, no gateway, no LLM, no dispatch spawn; the entry script is deployed in the
§2.1 layout under the temporary home for the real ``recovery-check`` subcommand. The home is deleted at the end and
its absence asserted. Control B is the only place in this candidate that sets the delegated-child marker, and only
in that control's child environment.
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unittest

import test_cli_contract as cc
from clickup_intake import kanban_port as kp
from clickup_intake import render
from clickup_intake.config import load_config
from clickup_intake.contracts import EffectIntent, Outcome, Preconditions, RowState
from clickup_intake.store import Store

BOARD = "clickup-recovery-cli"
BOARD_DB = os.path.join(cc.HOME, "kanban", "boards", BOARD, "kanban.db")
RUNTIME = os.path.join(cc.HOME, "integrations", "clickup-dedicated-panel")
REPO = cc.WORKTREE
SOURCE_BASE = "2d53206cb907ae7d1f2e3e8ca0ce0e5170f217fd"  # source test provenance only
PUBLIC_BASE = "532c102bdaa5bf2f7238f37973ce9e0563ddf0a9"  # public pre-recovery release, including older tests
REASON = "The approved amendment needs an existing-card archive that worker tools do not expose."
PLAN_SHA = "8" * 64
CHILD_MARKER = "HERMES_DELEGATED_" + "CHILD_CONTEXT"  # spelled once below, in control B's child environment


def cli(*args, env=None):
    return cc.cli(*args, board=BOARD, env=env)


def worker_env(card, run_id):
    return cc.isolated_env(HERMES_KANBAN_TASK=card, HERMES_KANBAN_RUN_ID=str(run_id), HERMES_KANBAN_DB=BOARD_DB)


def eff(kind, target, payload=None):
    return EffectIntent(idem_key="%s:%s" % (kind, target), kind=kind, target=target, payload=payload or {},
                        preconditions=Preconditions(task_id="cli"))


def db_events(card):
    conn = sqlite3.connect("file:%s?mode=ro" % BOARD_DB.replace("\\", "/"), uri=True)
    try:
        return conn.execute("SELECT id, kind FROM task_events WHERE task_id=? ORDER BY id", (card,)).fetchall()
    finally:
        conn.close()


class RecoveryCliTest(unittest.TestCase):
    port = None

    @classmethod
    def setUpClass(cls):
        if os.path.exists(cc.HOME):
            raise RuntimeError("stale temporary home %s; remove it first" % cc.HOME)
        os.makedirs(cc.HOME)
        try:
            rc, out, err = cc.cli("boards", "create", BOARD, board=None)
            if rc != 0 or os.path.normcase(os.path.abspath(BOARD_DB)) not in os.path.normcase(out):
                raise RuntimeError("board create did not land in the temporary home: %s %s %s" % (rc, out, err))
            scripts = os.path.dirname(cc.ENTRY)
            shutil.copytree(os.path.join(REPO, "clickup_intake"), os.path.join(scripts, "clickup_intake"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(os.path.join(REPO, "templates"), os.path.join(scripts, "templates"))
            # As in cc's harness, only the disposable deployed copy gets the selected real CLI path.
            module = os.path.join(scripts, "clickup_intake", "main.py")
            with open(module, encoding="utf-8") as f:
                source = f.read()
            source = source.replace('HERMES_EXE = "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe"',
                                    "HERMES_EXE = " + repr(cc.HERMES_EXE))
            with open(module, "w", encoding="utf-8", newline="\n") as f:
                f.write(source)
            shutil.copy2(os.path.join(REPO, "clickup_dedicated_panel_intake.py"), cc.ENTRY)
            cc.write(os.path.join(RUNTIME, "config.json"), json.dumps({"runtime_dir": RUNTIME, "board": BOARD}))
        except BaseException:
            shutil.rmtree(cc.HOME)
            raise
        cls.port = kp.KanbanCli(cc.HERMES_EXE, BOARD, discord_chat=cc.CHAT, env=cc.isolated_env())
        cls.cfg = load_config(os.path.join(RUNTIME, "config.json"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cc.HOME)
        if os.path.exists(cc.HOME):
            raise AssertionError("temporary home still present: %s" % cc.HOME)
        if os.path.isdir(cc.TMP_ROOT) and not os.listdir(cc.TMP_ROOT):
            os.rmdir(cc.TMP_ROOT)

    def show(self, card):
        res = self.port.show(card)
        self.assertEqual(res.outcome, Outcome.OK, res.json)
        return res.json

    def stopped_card(self, title):
        """A real watched card W: created, claimed by a worker run and stopped by that run with ``capability``."""
        rc, out, err = cli("create", "--assignee", "orchestrator", "--json", "--", title)
        self.assertEqual(rc, 0, err)
        w = json.loads(out)["id"]
        self.assertEqual(cli("claim", w)[0], 0)
        run = kp.latest_run_id(self.show(w))
        rc, _, err = cli("block", "--kind", "capability", w, REASON, env=worker_env(w, run))
        self.assertEqual(rc, 0, err)
        return w, run

    # ------------------------------------------------------------------ #12 carrier contract
    def test_carrier_contract(self):
        import gateway.kanban_watchers_notifier as notifier  # installed module, read-only, for _WAKE_KINDS only
        w, run = self.stopped_card("W carrier contract")  # a real W: the summary names only existing cards
        params = {"task": "86b7rcv01", "card": w, "run_id": run, "event_kind": "blocked",
                  "event_created_s": 1_792_500_000, "block_kind": "capability", "class": "capability", "reason": REASON,
                  "reason_sha256": hashlib.sha256(REASON.encode()).hexdigest(), "approval_id": 6, "rev": 8,
                  "plan_sha256": PLAN_SHA, "attempt": 1, "adopted": 1, "source_status": "ready"}
        body = render.recovery_card_body(params)
        self.assertIn(render.recovery_check_command(w, run), body)
        for rule in ("CLICKUP-RECOVERY stale wake %s run %d" % (w, run), "STALE", "UNAVAILABLE",
                     "CLICKUP-RECOVERY-RECEIPT v1 card=%s run=%d" % (w, run),
                     "CLICKUP-RECOVERY-ACK v1 card=%s run=%d disposition=" % (w, run),
                     "parents=[\"<W>\"]", "Assembly hold"):
            self.assertIn(rule, body)
        self.assertLessEqual(len(body.encode("utf-8")), 16 * 1024)
        path = cc.write(os.path.join(cc.HOME, "staging", "carrier.md"), body)
        key = "clickup-dp:86b7rcv01:rcv:%s:%d:1:create" % (w, run)
        res = self.port.create("CLICKUP-RECOVERY v1 %s run=%d attempt=1" % (w, run), body_file=path,
                               idempotency_key=key)
        self.assertEqual(res.outcome, Outcome.OK, res.json)
        carrier = res.json["id"]
        self.assertEqual((res.json["status"], res.json["assignee"]), ("blocked", None))
        rc, out, err = cli("dispatch", "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(carrier, out)  # an unassigned blocked carrier is never dispatched

        self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.SUBSCRIBE, carrier, {"mode": "wake"})).state,
                         RowState.DONE)
        [sub] = self.port.subscriptions(carrier).json
        self.assertEqual({k: sub[k] for k in ("platform", "chat_id", "chat_type", "notifier_profile", "delivery_mode",
                                              "delivery_metadata")},
                         {"platform": "discord", "chat_id": cc.CHAT, "chat_type": "group", "notifier_profile": "default",
                          "delivery_mode": "wake", "delivery_metadata": {"guild_id": kp.DISCORD_GUILD_ID}})
        self.assertTrue(kp.has_subscription([sub], cc.CHAT, "wake"))
        self.assertFalse(kp.has_passive_subscription([sub], cc.CHAT))
        cursor = sub["last_event_id"]
        self.assertEqual(cursor, max(i for i, _ in db_events(carrier)))  # a new subscription starts caught up
        summary = render.recovery_summary(params, carrier)
        self.assertLessEqual(len(summary), 400)
        self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.COMPLETE, carrier, {"summary": summary})).state,
                         RowState.DONE)
        # a lost acknowledgement of complete is read back as done and never resent
        again = kp.run_kanban_effect(self.port, eff(kp.COMPLETE, carrier, {"summary": summary}),
                                     prior_child=(1, "gone"), alive=lambda pid, st: False)
        self.assertEqual(again.state, RowState.DONE)
        above = [kind for i, kind in db_events(carrier) if i > cursor]
        self.assertEqual(above, ["completed"])
        self.assertIn("completed", notifier._WAKE_KINDS)
        self.assertEqual(self.show(carrier)["task"]["status"], "done")

        # a stale attempt's carrier (never completed) is archived; archived is not a wake kind
        res = self.port.create("CLICKUP-RECOVERY v1 %s run=%d attempt=2" % (w, run), body_file=path,
                               idempotency_key=key.replace(":1:create", ":2:create"))
        stale = res.json["id"]
        self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.ARCHIVE, stale)).state, RowState.DONE)
        self.assertEqual(self.show(stale)["task"]["status"], "archived")
        self.assertNotIn("archived", notifier._WAKE_KINDS)

    # ------------------------------------------------------------------ #13 delayed/duplicate wake
    def seed(self, task, w, run, approval_state="released"):
        shown = self.show(w)
        inc = kp.recovery_incident(shown)
        self.assertIsNotNone(inc, shown)
        store = Store(self.cfg.state_path, lambda: 1_792_600_000_000)
        try:
            store.ensure_relay_tables()
            with store.transaction() as c:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('recovery_cutover_ms', '1792000000000')")
                c.execute("INSERT INTO tasks(clickup_task_id, phase, input_gen, classified_gen, scope_gen, content_hash)"
                          " VALUES (?, 'development', 4, 4, 4, 'c0')", (task,))
                aid = c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, gate_card,"
                                " orchestrator_card) VALUES (?, 8, 4, ?, ?, 't_00000ga7', ?)",
                                (task, PLAN_SHA, approval_state, w)).lastrowid
                iid = c.execute("INSERT INTO recoveries(task, card, run_id, event_kind, event_created_s, block_kind,"
                                " class, reason_sha256, approval_id, approval_rev, plan_sha256, adopted, state, attempts)"
                                " VALUES (?,?,?,?,?,?,?,?,?,8,?,1,'open',1)",
                                (task, w, run, inc["event_kind"], inc["event_created_s"], inc["block_kind"],
                                 inc["class"], inc["reason_sha256"], aid, PLAN_SHA)).lastrowid
        finally:
            store.close()
        return aid, iid

    def recovery_check(self, card, run):
        p = subprocess.run([sys.executable, cc.ENTRY, "recovery-check", "--card", card, "--run", str(run)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=cc.isolated_env(),
                           timeout=300)
        self.assertEqual(p.stderr, "")
        return p.returncode, p.stdout.strip()

    def frozen(self, cards):
        def view(card):
            s = self.show(card)
            return json.dumps({k: s.get(k) for k in ("task", "runs", "events", "comments", "parents", "children")},
                              sort_keys=True)
        with open(self.cfg.state_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        wal = self.cfg.state_path + "-wal"  # a read-only open may leave an empty -wal; it never holds frames
        return [view(c) for c in cards], digest, os.path.getsize(wal) if os.path.exists(wal) else 0

    def test_delayed_or_duplicate_wakes_are_stale_and_the_check_mutates_nothing(self):
        fixtures = {}
        for name in ("reclaimed", "superseded", "held"):
            w, run = self.stopped_card("W %s" % name)
            fixtures[name] = (w, run) + self.seed("86b7rcv-" + name, w, run)
        cards = [f[0] for f in fixtures.values()]
        for name, (w, run, aid, iid) in fixtures.items():
            src = kp.recovery_incident(self.show(w))["source_status"]
            before = self.frozen(cards)
            self.assertEqual(self.recovery_check(w, run),
                             (0, "CURRENT incident=%d card=%s run=%d approval=%d rev=8 class=capability"
                                 " source_status=%s" % (iid, w, run, aid, src)))
            self.assertEqual(self.frozen(cards), before)
        w, run = fixtures["reclaimed"][:2]
        self.assertEqual(cli("unblock", w)[0], 0)
        self.assertEqual(cli("claim", w)[0], 0)
        store = Store(self.cfg.state_path, lambda: 1_792_600_000_000)
        try:
            with store.transaction() as c:
                c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state) VALUES"
                          " ('86b7rcv-superseded', 9, 5, ?, 'recorded')", ("9" * 64,))
                c.execute("UPDATE tasks SET hold_reason='operator' WHERE clickup_task_id='86b7rcv-held'")
        finally:
            store.close()
        for name, reason in (("reclaimed", "status_running"), ("superseded", "superseded"), ("held", "held")):
            with self.subTest(name):
                w, run = fixtures[name][:2]
                before = self.frozen(cards)
                self.assertEqual(self.recovery_check(w, run), (3, "STALE " + reason))
                self.assertEqual(self.frozen(cards), before)
        self.assertEqual(self.recovery_check("t_00000bad", 1), (2, "UNAVAILABLE show_unavailable"))

    # ------------------------------------------------------------------ #14 refused-operation control B
    def test_control_b_a_fenced_child_cannot_mutate_and_production_never_bypasses_the_fence(self):
        w, _ = self.stopped_card("W control B")
        before = self.show(w)
        child = cc.isolated_env(HERMES_DELEGATED_CHILD_CONTEXT=cc.HOME)
        rc, out, err = cli("archive", w, env=child)
        self.assertNotEqual(rc, 0)
        self.assertIn("delegate_task child contexts cannot mutate Kanban tasks via the CLI", err + out)
        self.assertEqual(self.show(w), before)

        forbidden = [re.escape(CHILD_MARKER), "scrub_" + "kanban_env", "_set_status_" + "direct", r"task_events",
                     r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+tasks\b"]
        added = self.added_lines(["clickup_intake", "templates", "clickup_dedicated_panel_intake.py"])
        self.assertTrue(added)
        for path, line in added:
            for pattern in forbidden:
                self.assertIsNone(re.search(pattern, line, re.I), (path, line))
        marked = [(path, line.strip()) for path, line in self.added_lines(["clickup_intake", "templates", "tests",
                                                                          "clickup_dedicated_panel_intake.py"])
                  if CHILD_MARKER in line]
        self.assertEqual(marked, [("tests/test_recovery_cli.py",
                                   "child = cc.isolated_env(%s=cc.HOME)" % CHILD_MARKER)])

    def added_lines(self, roots):
        """Candidate additions against the public pre-recovery base, tracked or not; no private history."""
        out = []
        available = subprocess.run(["git", "-C", REPO, "cat-file", "-e", PUBLIC_BASE + "^{commit}"],
                                   capture_output=True)
        if available.returncode:
            raise RuntimeError("recovery CLI safety guard requires a full public Git clone containing " + PUBLIC_BASE
                               + "; source archives or shallow clones missing this base are unsupported")
        prefix = subprocess.run(["git", "-C", REPO, "rev-parse", "--show-prefix"], capture_output=True,
                                text=True, check=True).stdout.strip()
        tracked = subprocess.run(["git", "-C", REPO, "ls-tree", "-r", "--name-only", PUBLIC_BASE],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        current = subprocess.run(["git", "-C", REPO, "ls-files", "--cached", "--others", "--exclude-standard"],
                                 capture_output=True, text=True, check=True).stdout.splitlines()
        for path in sorted(set(tracked) | set(current)):
            if not any(path == r or path.startswith(r + "/") for r in roots):
                continue
            old = subprocess.run(["git", "-C", REPO, "show", "%s:%s%s" % (PUBLIC_BASE, prefix, path)],
                                 capture_output=True, check=True).stdout.decode("utf-8").splitlines() \
                if path in tracked else []
            full = os.path.join(REPO, path)
            new = []
            if os.path.exists(full):
                with open(full, encoding="utf-8") as f:
                    new = f.read().splitlines()
            out += [(path, line[1:]) for line in difflib.unified_diff(old, new, lineterm="", n=0)
                    if line.startswith("+") and not line.startswith("+++")]
        return out


if __name__ == "__main__":
    unittest.main()
