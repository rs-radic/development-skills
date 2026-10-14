"""§13.2 Hermes CLI contract test (S3), plan r5.

Real installed ``hermes kanban`` CLI against an isolated temporary ``HERMES_HOME=<worktree>/tests/.tmp/home``
and a disposable board: no gateway, no LLM, no dispatch spawn. Every inherited ``HERMES_*`` variable (the
dispatcher's board pins and the delegated-child marker included) is dropped from the child environment.
``HERMES_DISABLE_LAZY_INSTALLS=1`` stops the launcher from provisioning a ~500 MB dependency store inside
the temporary home; the CLI code that runs is the installed one.

The ticket-check cases call the S0 ``ticket_verdict`` through the thin harness ``ticket_check`` below
(read-only state snapshot + real ``show --json`` of the gate); S4 repeats each of them through the real
``ticket-check`` subcommand of the entry script deployed in the §2.1 layout under the temporary home
(``<home>/scripts/clickup_dedicated_panel_intake.py`` + ``clickup_intake/`` + ``templates/``), which reads the
config of its own installation (``<home>/integrations/clickup-dedicated-panel/config.json``).
The temporary home is deleted at the end and its absence asserted.
"""

import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest

from clickup_intake import kanban_port as kp
from clickup_intake import render
from clickup_intake.config import from_dict, load_config
from clickup_intake.contracts import EffectIntent, Item, Outcome, Preconditions, RowState
from clickup_intake.store import Store, load_ticket_snapshot
from clickup_intake.ticket import ticket_verdict

HERMES_EXE = os.environ.get("CLICKUP_INTAKE_TEST_HERMES_EXE", "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe")
TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
HOME = os.path.join(TMP_ROOT, "home")
BOARD = "clickup-cli-contract"
BOARD_DB = os.path.join(HOME, "kanban", "boards", BOARD, "kanban.db")
CHAT = "111111111111111111"
SHA = "d" * 64
WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(HOME, "scripts", "clickup_dedicated_panel_intake.py")


def isolated_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HERMES_HOME=HOME, HERMES_KANBAN_HOME=HOME, HERMES_DISABLE_LAZY_INSTALLS="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def cli(*args, board=BOARD, env=None):
    """Raw CLI call for verbs the glue never uses (worker/reviewer/operator simulation)."""
    argv = [HERMES_EXE, "kanban"] + (["--board", board] if board else []) + list(args)
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env or isolated_env(), timeout=300)
    return p.returncode, p.stdout, p.stderr


def worker_env(card, run_id):
    return isolated_env(HERMES_KANBAN_TASK=card, HERMES_KANBAN_RUN_ID=str(run_id), HERMES_KANBAN_DB=BOARD_DB)


def ticket_check(port, cfg, gate, task, rev, sha, now_ms):
    """Thin §13.2 harness: read-only local snapshot at the configured path + real gate ``show`` -> S0 verdict."""
    snapshot = load_ticket_snapshot(cfg.state_path, task, cfg.ticket_lease_seconds)
    shown = port.show(gate)
    gate_json = shown.json if shown.outcome is Outcome.OK else ""
    return ticket_verdict(snapshot, gate_json, task, rev, sha, gate, now_ms), gate_json


def now_ms():
    return int(time.time() * 1000)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def eff(kind, target, payload=None, wait_run_id=None):
    return EffectIntent(idem_key=f"{kind}:{target}", kind=kind, target=target, payload=payload or {},
                        preconditions=Preconditions(task_id="cli", wait_run_id=wait_run_id))


class HermesCliContractTest(unittest.TestCase):
    port = None

    @classmethod
    def setUpClass(cls):
        if os.path.exists(HOME):
            raise RuntimeError(f"stale temporary home {HOME}; remove it first")
        os.makedirs(HOME)
        try:
            rc, out, err = cli("boards", "create", BOARD, board=None)
            if rc != 0 or os.path.normcase(os.path.abspath(BOARD_DB)) not in os.path.normcase(out):
                raise RuntimeError(f"board create did not land in the temporary home: {rc} {out} {err}")
        except BaseException:
            shutil.rmtree(HOME)
            raise
        cls.port = kp.KanbanCli(HERMES_EXE, BOARD, discord_chat=CHAT, env=isolated_env())
        scripts = os.path.dirname(ENTRY)  # §2.1 deployed layout for the real ticket-check subcommand (S4)
        shutil.copytree(os.path.join(WORKTREE, "clickup_intake"), os.path.join(scripts, "clickup_intake"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(WORKTREE, "templates"), os.path.join(scripts, "templates"))
        # The packaged source uses an inert example path; only this disposable test copy gets the real CLI.
        module = os.path.join(scripts, "clickup_intake", "main.py")
        with open(module, encoding="utf-8") as f:
            source = f.read()
        source = source.replace('HERMES_EXE = "C:/Hermes/hermes-agent/venv/Scripts/hermes.exe"',
                                "HERMES_EXE = " + repr(HERMES_EXE))
        with open(module, "w", encoding="utf-8", newline="\n") as f:
            f.write(source)
        shutil.copy2(os.path.join(WORKTREE, "clickup_dedicated_panel_intake.py"), ENTRY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(HOME)
        if os.path.exists(HOME):
            raise AssertionError(f"temporary home still present: {HOME}")
        if os.path.isdir(TMP_ROOT) and not os.listdir(TMP_ROOT):
            os.rmdir(TMP_ROOT)

    def create(self, title, key, body="card body", **kw):
        res = self.port.create(title, body_file=write(os.path.join(HOME, "staging", key.replace(":", "_") + ".md"),
                                                      body), idempotency_key=key, **kw)
        self.assertEqual(res.outcome, Outcome.OK, res.json)
        return res.json

    def show(self, card):
        res = self.port.show(card)
        self.assertEqual(res.outcome, Outcome.OK, res.json)
        return res.json

    # ------------------------------------------------------------------ create / dispatch
    def test_create_with_same_idempotency_key_returns_same_id(self):
        first = self.create("ClickUp 86b7cli01: approval gate r1", "clickup-dp:86b7cli01:gate:r1:g1",
                            initial_status="blocked")
        again = self.create("ClickUp 86b7cli01: approval gate r1 (retry)", "clickup-dp:86b7cli01:gate:r1:g1",
                            initial_status="blocked")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual((first["status"], first["assignee"], first["created_by"]), ("blocked", None, "clickup-intake"))

    def test_unassigned_blocked_gate_is_not_dispatchable(self):
        gate = self.create("ClickUp 86b7cli02: approval gate r1", "clickup-dp:86b7cli02:gate:r1:g1")["id"]
        ready = self.create("ClickUp 86b7cli02: request 1", "clickup-dp:86b7cli02:req:1", assignee="planner",
                            initial_status="running")
        self.assertEqual(ready["status"], "ready")
        rc, out, err = cli("dispatch", "--dry-run", "--json")
        self.assertEqual(rc, 0, err)
        plan = json.loads(out)
        listed = json.dumps(plan)
        self.assertIn(ready["id"], listed)  # the dry run does see an assigned ready card
        self.assertNotIn(gate, listed)
        # A dry run may report the assigned card as a prospective dispatch (installed kanban_db_dispatch: it
        # appends to ``spawned`` and returns before any claim/spawn), but it never actually dispatches.
        after = self.show(ready["id"])
        self.assertEqual((after["task"]["status"], after["runs"]), ("ready", []))
        self.assertEqual(self.show(gate)["task"]["status"], "blocked")

    # ------------------------------------------------------------------ attach / comment / subscribe readback
    def test_attach_comment_author_and_passive_subscription_readback(self):
        card = self.create("ClickUp 86b7cli03: request 1", "clickup-dp:86b7cli03:req:1", assignee="planner",
                           skills=("plan", "pragmatic-code"),
                           initial_status="blocked")["id"]
        ctx = write(os.path.join(HOME, "staging", "clickup-context-g1.md"),
                    render.context_file({"task_id": "86b7cli03", "gen": 1, "name": "n", "description": "d"}))
        res = kp.run_kanban_effect(self.port, eff(kp.ATTACH, card, {"path": ctx, "name": "clickup-context-g1.md"}))
        self.assertEqual(res.state, RowState.DONE)
        atts = self.port.attachments(card).json
        self.assertEqual([(a["filename"], a["uploaded_by"], a["size"]) for a in atts],
                         [("clickup-context-g1.md", "clickup-intake", os.path.getsize(ctx))])
        with open(atts[0]["stored_path"], "rb") as a, open(ctx, "rb") as b:
            self.assertEqual(hashlib.sha256(a.read()).hexdigest(), hashlib.sha256(b.read()).hexdigest())

        body = "CLICKUP-IN v1 relay=rel-1 gen=1\n-leading dash line\nsecond line  "
        res = kp.run_kanban_effect(self.port, eff(kp.COMMENT, card, {"body": body}))
        self.assertEqual(res.state, RowState.DONE)
        # A crashed attempt replayed after its child is gone: readback finds it, nothing is re-posted.
        again = kp.run_kanban_effect(self.port, eff(kp.COMMENT, card, {"body": body}), prior_child=(1, "gone"),
                                     alive=lambda pid, st: False)
        self.assertEqual(again.state, RowState.DONE)
        shown = self.show(card)
        self.assertEqual([(c["author"], c["body"]) for c in shown["comments"]],
                         [("clickup-intake", body.strip())])
        self.assertEqual(shown["task"]["skills"],
                         ["plan", "pragmatic-code"])

        res = kp.run_kanban_effect(self.port, eff(kp.SUBSCRIBE, card))
        self.assertEqual(res.state, RowState.DONE)
        subs = self.port.subscriptions(card).json
        self.assertEqual(len(subs), 1)
        self.assertEqual({k: subs[0][k] for k in ("platform", "chat_id", "chat_type", "notifier_profile",
                                                  "delivery_mode", "delivery_metadata")},
                         {"platform": "discord", "chat_id": CHAT, "chat_type": "group", "notifier_profile": "default",
                          "delivery_mode": "notify", "delivery_metadata": {"guild_id": "222222222222222222"}})

    # ------------------------------------------------------------------ schedule / unblock reuse
    def test_worker_schedule_and_glue_unblock_cycle_three_times_without_triage(self):
        card = self.create("ClickUp 86b7cli04: request 1", "clickup-dp:86b7cli04:req:1", assignee="planner",
                           initial_status="running")["id"]
        parked = []
        for cycle in range(1, 4):
            self.assertEqual(cli("claim", card)[0], 0)
            run_id = kp.latest_run_id(self.show(card))
            rc, _, err = cli("schedule", card, "CLICKUP_WAIT", env=worker_env(card, run_id))
            self.assertEqual(rc, 0, err)
            relay = f"CLICKUP-IN v1 relay=rel-{cycle} gen={cycle}"
            self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.COMMENT, card, {"body": relay + "\nbatch"})).state,
                             RowState.DONE)
            wake = eff(kp.UNBLOCK, card, {"relay_line": relay}, wait_run_id=run_id)
            res = kp.run_kanban_effect(self.port, wake)
            self.assertEqual(res.state, RowState.DONE)
            self.assertIn(kp.card_status(res.json), ("ready", "todo"))
            # Replayed after a crash before the local commit: the guard sees the unblock landed.
            self.assertEqual(kp.run_kanban_effect(self.port, wake, prior_child=(1, "gone"),
                                                  alive=lambda pid, st: False).state, RowState.DONE)
            parked.append(run_id)
        shown = self.show(card)
        self.assertEqual(shown["task"]["status"], "ready")
        self.assertEqual(len(set(parked)), 3)
        self.assertEqual(parked, sorted(parked))
        self.assertEqual([r["id"] for r in shown["runs"] if r["outcome"] == "scheduled"], parked)
        self.assertEqual([e["run_id"] for e in shown["events"] if e["kind"] == "scheduled"], parked)
        self.assertEqual(sum(e["kind"] == "unblocked" for e in shown["events"]), 3)
        self.assertNotIn("triage", json.dumps([e["kind"] for e in shown["events"]]))
        # A queued wake for an old wait is never re-sent once newer runs exist.
        self.assertEqual(kp.wake_decision(shown, parked[0], "CLICKUP-IN v1 relay=rel-1 gen=1"), "done")

    # ------------------------------------------------------------------ PASS evidence
    def test_request_review_and_complete_produce_parseable_pass_evidence(self):
        card = self.create("ClickUp 86b7cli05: request 1", "clickup-dp:86b7cli05:req:1", assignee="planner",
                           initial_status="running")["id"]
        self.assertEqual(cli("claim", card)[0], 0)
        run_id = kp.latest_run_id(self.show(card))
        plan = write(os.path.join(HOME, "staging", "clickup-plan-r1.md"), "## ClickUp summary\nAdds X.\n")
        with open(plan, "rb") as f:
            plan_sha = hashlib.sha256(f.read()).hexdigest()
        meta = {"clickup_task": "86b7cli05", "plan_revision": 1, "plan_attachment": "clickup-plan-r1.md",
                "plan_sha256": plan_sha, "accounts_gen": 2}
        wenv = worker_env(card, run_id)
        self.assertEqual(cli("attach", card, plan, "--name", "clickup-plan-r1.md", "--author", "planner", env=wenv)[0], 0)
        rc, _, err = cli("request-review", card, "--summary", "plan r1", "--reviewer", "plan-reviewer",
                         "--metadata", json.dumps(meta), env=wenv)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(kp.pass_evidence(self.show(card), self.port.attachments(card).json))  # review != PASS
        rc, _, err = cli("complete", card, "--summary", "PASS: plan r1 approved for owner review")
        self.assertEqual(rc, 0, err)
        shown = self.show(card)
        self.assertEqual(shown["runs"][-1]["profile"], "plan-reviewer")
        self.assertEqual(kp.pass_evidence(shown, self.port.attachments(card).json), meta)

    # ------------------------------------------------------------------ gate parenting
    def test_gate_completion_and_archive_release_children(self):
        task, rev = "86b7cli06", 1
        gate = self.create(render.gate_title(task, rev, "Dark mode"), render.gate_key(task, rev, 1))["id"]
        promo_body = write(os.path.join(HOME, "staging", "promo.md"),
                           "Promote r1.\n\n" + render.promotion_block(gate, task, rev, SHA) + "\n")
        rc, out, err = cli("create", "--assignee", "integrator", "--parent", gate, "--body-file", promo_body,
                           "--json", "--", "promotion r1")
        self.assertEqual(rc, 0, err)
        promo = json.loads(out)
        self.assertEqual(promo["status"], "todo")
        self.assertTrue(kp.promotion_guarded(self.show(promo["id"]), gate, render.ticket_marker(gate, task, rev, SHA)))
        res = kp.run_kanban_effect(self.port, eff(kp.COMPLETE, gate, {"summary": "ClickUp approval r1 current at "
                                                                                 "input_gen 3"}))
        self.assertEqual(res.state, RowState.DONE)
        self.assertEqual(self.show(promo["id"])["task"]["status"], "ready")

        gate2 = self.create(render.gate_title(task, 2, ""), render.gate_key(task, 2, 1))["id"]
        rc, out, err = cli("create", "--assignee", "integrator", "--parent", gate2, "--json", "--", "promotion r2")
        self.assertEqual(rc, 0, err)
        promo2 = json.loads(out)["id"]
        self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.ARCHIVE, gate2)).state, RowState.DONE)
        self.assertEqual(self.show(promo2)["task"]["status"], "ready")

    # ------------------------------------------------------------------ ticket-check (thin harness over S0)
    def seed(self, store, task, gate, renewed_ms, gen=4):
        with store.transaction() as c:
            c.execute("INSERT INTO tasks(clickup_task_id, phase, input_gen, classified_gen, scope_gen, content_hash)"
                      " VALUES (?,?,?,?,?,?)", (task, "promotion_window", gen, gen, 3, "c0"))
            c.execute("INSERT INTO approvals(task, rev, accounts_gen, plan_sha256, state, gate_card, ticket_epoch,"
                      " ticket_input_gen, ticket_renewed_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                      (task, 2, 3, SHA, "ticketed", gate, 1, gen, renewed_ms))

    def state_bytes(self, cfg):
        """SHA-256 of the state file, plus the WAL size. A read-only open may leave an empty ``-wal``
        (SQLite WAL bookkeeping); it must never hold frames."""
        with open(cfg.state_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        wal = cfg.state_path + "-wal"
        return digest, os.path.getsize(wal) if os.path.exists(wal) else 0

    def subcommand(self, gate, task, rev, sha, env=None):
        """The real deployed ``ticket-check`` subcommand (S4): (exit code, output)."""
        p = subprocess.run([sys.executable, ENTRY, "ticket-check", "--gate", gate, "--task", task, "--rev", str(rev),
                            "--sha", sha], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env or isolated_env(), timeout=300)
        self.assertEqual(p.stderr, "")
        return p.returncode, p.stdout.strip()

    def check(self, cfg, gate, task, rev, sha, expect, env=None, port=None, config=None):
        """Harness verdict and the real subcommand agree: ``expect`` with exit 0 (VALID) or 3 (INVALID)."""
        verdict, gate_json = ticket_check(port or self.port, cfg, gate, task, rev, sha, now_ms())
        self.assertEqual(str(verdict), expect)
        if config is not None:
            write(self.cfg_path, json.dumps(config))
        try:
            self.assertEqual(self.subcommand(gate, task, rev, sha, env), (0 if verdict.valid else 3, expect))
        finally:
            if config is not None:
                write(self.cfg_path, json.dumps(self.base_config))
        return gate_json

    def test_ticket_check_against_real_gate_show(self):
        runtime = os.path.join(HOME, "integrations", "clickup-dedicated-panel")
        self.base_config = {"runtime_dir": runtime, "board": BOARD}
        cfg_path = self.cfg_path = write(os.path.join(runtime, "config.json"), json.dumps(self.base_config))
        cfg = load_config(cfg_path)
        self.assertEqual(os.path.normcase(cfg.state_path), os.path.normcase(os.path.join(runtime, "state.sqlite3")))
        gate = self.create("ClickUp 86b7cli07: approval gate r2", "clickup-dp:86b7cli07:gate:r2:g3")["id"]
        self.assertEqual(kp.run_kanban_effect(self.port, eff(kp.COMPLETE, gate, {"summary": "ClickUp approval r2 "
                                                                                          "current at input_gen 4"})).state,
                         RowState.DONE)
        blocked_gate = self.create("ClickUp 86b7cli08: approval gate r2", "clickup-dp:86b7cli08:gate:r2:g3")["id"]
        store = Store(cfg.state_path, now_ms)
        try:
            t0 = now_ms()
            for task, g in (("86b7cli07", gate), ("86b7cli08", blocked_gate), ("86b7cli09", gate)):
                self.seed(store, task, g, t0)
        finally:
            store.close()

        # Fresh ticketed row + completed gate -> VALID; also under worker fencing, and nothing is written.
        before = self.state_bytes(cfg)
        self.check(cfg, gate, "86b7cli07", 2, SHA, "VALID epoch=1")
        fenced_env = isolated_env(HERMES_KANBAN_TASK=blocked_gate, HERMES_DELEGATED_CHILD_CONTEXT=HOME,
                                  HERMES_KANBAN_DB=BOARD_DB)
        fenced = kp.KanbanCli(HERMES_EXE, BOARD, discord_chat=CHAT, env=fenced_env)
        self.check(cfg, gate, "86b7cli07", 2, SHA, "VALID epoch=1", env=fenced_env, port=fenced)
        refused = fenced.comment(gate, "fenced write attempt")
        self.assertEqual(refused.outcome, Outcome.DEFINITE_FAIL)
        self.assertIn("cannot mutate", refused.json["error"])
        self.assertEqual(self.state_bytes(cfg), before)

        # observe_commit raising input_gen -> INVALID at once, with no gate comment posted at all.
        store = Store(cfg.state_path, now_ms)
        try:
            item = Item(kind="comment", id="c-new", parent_id=None, author_id=555, date_ms=now_ms(), hash="h1",
                        mentions_bot=True, shape="PLAIN", shape_sig="s", material=True)
            self.assertEqual(store.observe_commit("86b7cli07", [item], {}).input_gen, 5)
        finally:
            store.close()
        gate_json = self.check(cfg, gate, "86b7cli07", 2, SHA, "INVALID new_input")
        self.assertEqual(gate_json["comments"], [])

        # Audit stamps are ignored in both directions, whoever posts them.
        stamp_ok = render.gate_stamp_valid("86b7cli07", 2, SHA, 1, 4)
        self.assertEqual(cli("comment", gate, "--author", "someone", "--", stamp_ok)[0], 0)
        self.assertEqual(cli("comment", gate, "--author", "clickup-intake", "--",
                             render.gate_stamp_revoked("86b7cli09", 2, 1, "operator"))[0], 0)
        gate_json = self.check(cfg, gate, "86b7cli07", 2, SHA, "INVALID new_input")
        self.assertEqual(len(gate_json["comments"]), 2)
        self.check(cfg, gate, "86b7cli09", 2, SHA, "VALID epoch=1")

        # Gate still blocked -> INVALID.
        self.check(cfg, blocked_gate, "86b7cli08", 2, SHA, "INVALID gate_not_done")

        # Lease set to 1 s, after the wait -> INVALID.
        short = from_dict({"runtime_dir": runtime, "board": BOARD, "ticket_lease_seconds": 1})
        store = Store(short.state_path, now_ms)
        try:
            self.seed(store, "86b7cli10", gate, now_ms())
        finally:
            store.close()
        time.sleep(1.2)
        self.check(short, gate, "86b7cli10", 2, SHA, "INVALID lease_expired",
                   config=dict(self.base_config, ticket_lease_seconds=1))

        # Missing state file -> INVALID, and the read-only opener does not create it.
        missing = from_dict({"runtime_dir": os.path.join(HOME, "integrations", "absent"), "board": BOARD})
        self.check(missing, gate, "86b7cli09", 2, SHA, "INVALID state_unavailable",
                   config=dict(self.base_config, runtime_dir=missing.runtime_dir))
        self.assertFalse(os.path.exists(missing.runtime_dir))

        # Another connection holds an exclusive lock beyond the 5 s busy timeout -> INVALID.
        holder = sqlite3.connect(cfg.state_path, isolation_level=None)
        try:
            holder.execute("PRAGMA locking_mode=EXCLUSIVE")
            holder.execute("BEGIN EXCLUSIVE")
            holder.execute("UPDATE meta SET value=value WHERE key='schema_version'")
            started = time.time()
            verdict, _ = ticket_check(self.port, cfg, gate, "86b7cli09", 2, SHA, now_ms())
            self.assertEqual(str(verdict), "INVALID state_unavailable")
            self.assertGreaterEqual(time.time() - started, 5)
            started = time.time()
            self.assertEqual(self.subcommand(gate, "86b7cli09", 2, SHA), (3, "INVALID state_unavailable"))
            self.assertGreaterEqual(time.time() - started, 5)
            holder.execute("ROLLBACK")
        finally:
            holder.close()
        self.check(cfg, gate, "86b7cli09", 2, SHA, "VALID epoch=1")
        # The subcommand only reads: the state file bytes (and WAL size) are unchanged by it.
        after = self.state_bytes(cfg)
        self.assertEqual(self.subcommand(gate, "86b7cli09", 2, SHA), (0, "VALID epoch=1"))
        self.assertEqual(self.state_bytes(cfg), after)

    # ------------------------------------------------------------------ disposable board removal
    def test_boards_rm_delete_removes_the_board_directory(self):
        slug = "clickup-cli-rm"
        board_dir = os.path.join(HOME, "kanban", "boards", slug)
        self.assertEqual(cli("boards", "create", slug, board=None)[0], 0)
        self.assertTrue(os.path.isdir(board_dir))
        rc, _, err = cli("boards", "rm", slug, "--delete", board=None)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(board_dir))
        self.assertEqual(glob.glob(os.path.join(HOME, "kanban", "boards", "_archived", slug + "-*")), [])


if __name__ == "__main__":
    unittest.main()
