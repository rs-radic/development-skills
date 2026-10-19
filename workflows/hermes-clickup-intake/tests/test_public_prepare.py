"""Offline preparation tests; every mutable fixture is owned under tests/.tmp.

The preparer is run from a disposable package copy so its output can be outside
that package and the intended Hermes home. No Hermes CLI or credential is used.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PACKAGE = Path(__file__).resolve().parents[1]
SAFE_FLAGS = {
    "enabled": False,
    "mode": "shadow",
    "cutover_ms": None,
    "notify_mode": "off",
    "handoff_unblock": False,
    "auto_gate": False,
}
REPLACED_CONSTANTS = {
    "config.py": {"DEFAULT_RUNTIME_DIR", "APPROVERS"},
    "main.py": {"HERMES_EXE", "CREDENTIAL"},
    "render.py": {"PYTHON_EXE", "ENTRY_SCRIPT"},
    "kanban_port.py": {"DISCORD_GUILD_ID"},
    "engine.py": {"REQUEST_SKILLS"},
}


def constants(path):
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(path.read_bytes()).body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in set().union(*REPLACED_CONSTANTS.values())
    }


def algorithms(path, replaced=()):
    tree = ast.parse(path.read_bytes())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in replaced):
            node.value = ast.Constant(value=None)
    return ast.dump(tree, include_attributes=False)


class PublicPreparationTest(unittest.TestCase):
    def setUp(self):
        self.tmp_parent = PACKAGE / "tests" / ".tmp"
        self.tmp_parent.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="public-prepare-", dir=self.tmp_parent))
        self.addCleanup(self.cleanup)
        self.package = self.root / "package"
        self.package.mkdir()
        shutil.copytree(PACKAGE / "clickup_intake", self.package / "clickup_intake",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(PACKAGE / "templates", self.package / "templates")
        shutil.copyfile(PACKAGE / "clickup_dedicated_panel_intake.py",
                        self.package / "clickup_dedicated_panel_intake.py")
        self.tool = self.package / "tools" / "prepare.py"
        self.tool.parent.mkdir()
        if (PACKAGE / "tools" / "prepare.py").is_file():
            shutil.copyfile(PACKAGE / "tools" / "prepare.py", self.tool)
        self.home = self.root / "hermes-home"
        self.hermes = self.root / "fake-hermes.exe"
        self.python = self.root / "fake-python.exe"
        self.hermes.write_bytes(b"not an executable: preparation must never run me")
        self.python.write_bytes(b"not an executable: preparation must never run me")
        self.credential = self.root / "private" / "never-read.dpapi"
        self.output = self.root / "staged"
        self.settings = {
            "hermes_home": self.home.as_posix(),
            "hermes_exe": self.hermes.as_posix(),
            "python_exe": self.python.as_posix(),
            "credential_path": self.credential.as_posix(),
            "discord_guild_id": "701234567890123456",
            "request_skills": ["plan", "pragmatic-code"],
            "config": {
                "workspace_id": "71001", "space_id": "72001", "folder_id": "73001",
                "list_id": "74001", "bot_user_id": 75001,
                "approvers": {"76001": "Review Owner", "76002": "Technical Owner"},
                "board": "planning-test", "discord_chat": "771234567890123456",
            },
        }
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")

    def cleanup(self):
        shutil.rmtree(self.root)
        self.assertFalse(self.root.exists())
        try:
            self.tmp_parent.rmdir()  # Only remove the shared parent when empty.
        except OSError:
            pass

    def prepare(self, output=None):
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(self.tool), "--settings", str(settings_path),
             "--output", str(output or self.output)], cwd=self.package, env=self.env,
            capture_output=True, text=True, encoding="utf-8", timeout=30)

    def assert_refused(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("refused", result.stderr.lower())
        self.assertFalse(self.output.exists())
        self.assertFalse(self.home.exists())
        self.assertFalse(self.credential.exists())

    def test_rejects_unknown_settings_without_echoing_values(self):
        original = json.dumps(self.settings)
        for scope, key in (("top", "api_token"), ("top", "unexpected"), ("config", "api_token")):
            with self.subTest(scope=scope, key=key):
                self.settings = json.loads(original)
                target = self.settings if scope == "top" else self.settings["config"]
                target[key] = "SYNTHETIC_NOT_A_SECRET"
                result = self.prepare()
                try:
                    self.assert_refused(result)
                    self.assertNotIn("SYNTHETIC_NOT_A_SECRET", result.stdout + result.stderr)
                finally:
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def reject_variant(self, change):
        original = self.settings
        self.settings = json.loads(json.dumps(original))
        change(self.settings)
        try:
            self.assert_refused(self.prepare())
        finally:
            self.settings = original
            if self.output.exists():
                shutil.rmtree(self.output)

    def test_requires_every_deployment_setting_and_explicit_routing_id(self):
        for key in self.settings:
            with self.subTest(missing=key):
                self.reject_variant(lambda value, key=key: value.pop(key))
        for key in self.settings["config"]:
            with self.subTest(missing_config=key):
                self.reject_variant(lambda value, key=key: value["config"].pop(key))

    def test_rejects_invalid_ids_approvers_and_request_skills(self):
        variants = [
            ("discord_guild_id", True), ("discord_guild_id", "guild"),
            ("discord_guild_id", "0"), ("discord_guild_id", "１２３"),
            ("request_skills", []), ("request_skills", "plan"),
            ("request_skills", ["plan", 1]), ("request_skills", [""]),
            ("request_skills", ["plan", "plan"]), ("request_skills", ["two skills"]),
        ]
        for key, bad in variants:
            with self.subTest(key=key, bad=bad):
                self.reject_variant(lambda value, key=key, bad=bad: value.__setitem__(key, bad))
        variants = [
            ("workspace_id", "abc"), ("space_id", "0"), ("folder_id", "-1"),
            ("list_id", "0123"), ("list_id", 123), ("bot_user_id", -1),
            ("bot_user_id", True), ("bot_user_id", "123"),
            ("approvers", {}), ("approvers", {"1": ""}), ("approvers", {"1": "  "}),
            ("approvers", {"0": "Owner"}), ("approvers", {"１２３": "Owner"}),
            ("approvers", {"123": 7}), ("board", ""), ("discord_chat", "channel"),
            ("enabled", "true"), ("mode", "typo"), ("notify_mode", "typo"),
            ("intervals", {"active_light_seconds": 0}), ("quiet_seconds", 0),
        ]
        for key, bad in variants:
            with self.subTest(config_key=key, bad=bad):
                self.reject_variant(lambda value, key=key, bad=bad: value["config"].__setitem__(key, bad))

    def test_rejects_reserved_public_fixture_ids_in_every_identity_role(self):
        cases = [("top", "discord_guild_id", "222222222222222222"),
                 ("config", "workspace_id", "11111111111"),
                 ("config", "space_id", "22222222222"),
                 ("config", "folder_id", "3333333333333333"),
                 ("config", "list_id", "4444444444444444"),
                 ("config", "bot_user_id", 900000001),
                 ("config", "discord_chat", "111111111111111111")]
        for scope, key, bad in cases:
            with self.subTest(scope=scope, key=key):
                self.reject_variant(lambda value, scope=scope, key=key, bad=bad:
                                    (value if scope == "top" else value["config"]).__setitem__(key, bad))
        for key in ("900000101", "900000102", "900000103"):
            with self.subTest(approver=key):
                self.reject_variant(lambda value, key=key:
                                    value["config"].__setitem__("approvers", {key: "Fixture owner"}))

    def test_example_has_the_exact_supported_shape_but_is_not_ready_to_deploy(self):
        example_path = PACKAGE / "installation.example.json"
        self.assertTrue(example_path.is_file(), "public settings example is missing")
        example = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(set(example), set(self.settings))
        self.assertEqual(example["request_skills"], ["plan", "pragmatic-code"])
        self.assertEqual(example["hermes_home"], "C:/Hermes")
        self.assertEqual(example["config"]["plain_attr_allowlist"], ["block-id"])
        self.assertEqual({key: example["config"][key] for key in SAFE_FLAGS}, SAFE_FLAGS)
        # Supply only synthetic executable locations; the reserved example IDs still refuse preparation.
        for key in ("hermes_home", "hermes_exe", "python_exe", "credential_path"):
            example[key] = self.settings[key]
        self.settings = example
        result = self.prepare()
        self.assert_refused(result)
        self.assertIn("reserved", result.stderr.lower())

    def test_write_failure_removes_only_the_new_staging_directory(self):
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        # A real partial staging write followed by an injected disk failure. No mock
        # replaces the preparer or runtime; only the final manifest write fails.
        probe = '''import json, pathlib, runpy, sys
from unittest.mock import patch
module = runpy.run_path(sys.argv[1])
settings = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
output = pathlib.Path(sys.argv[3])
write = pathlib.Path.write_bytes
def fail_manifest(path, data):
    if path.name == "manifest.json":
        raise OSError("injected disk failure")
    return write(path, data)
with patch.object(pathlib.Path, "write_bytes", fail_manifest):
    try:
        module["prepare"](settings, output)
    except OSError:
        pass
    else:
        raise AssertionError("write failure was swallowed")
assert not output.exists(), "partial staging was left behind"
'''
        result = subprocess.run([sys.executable, "-c", probe, str(self.tool), str(settings_path), str(self.output)],
                                cwd=self.package, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.hermes.read_bytes(), b"not an executable: preparation must never run me")
        self.assertTrue(self.package.is_dir())
        self.assertFalse(self.home.exists())

    def test_missing_runtime_source_refuses_before_creating_stage(self):
        for relative in ("clickup_intake/main.py", "clickup_intake/ports.py", "templates/request_card.md",
                         "templates/blocker_triage.md", "templates/question_protocol.md"):
            with self.subTest(missing=relative):
                source = self.package / relative
                content = source.read_bytes()
                source.unlink()
                try:
                    self.assert_refused(self.prepare())
                finally:
                    source.write_bytes(content)
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def test_preparation_never_opens_credential_or_spawns_or_connects(self):
        settings_path = self.root / "settings.json"
        settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        probe = '''import json, os, pathlib, runpy, sys
settings = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
credential = os.path.normcase(settings["credential_path"])
def audit(event, args):
    if event == "subprocess.Popen" or event.startswith("socket."):
        raise AssertionError("runtime effect during offline preparation: " + event)
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.normcase(os.fsdecode(args[0])) == credential:
            raise AssertionError("credential was opened")
sys.addaudithook(audit)
module = runpy.run_path(sys.argv[1])
module["prepare"](settings, pathlib.Path(sys.argv[3]))
'''
        result = subprocess.run([sys.executable, "-c", probe, str(self.tool), str(settings_path), str(self.output)],
                                cwd=self.package, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output / "manifest.json").is_file())
        self.assertFalse(self.home.exists())
        self.assertFalse(self.credential.exists())

    def test_requires_absolute_command_safe_paths_and_existing_executable_files(self):
        for key in ("hermes_home", "hermes_exe", "python_exe", "credential_path"):
            for bad in ("relative/path", str(self.root / "has space"), str(self.root / "has`tick"),
                        str(self.root / "has\nnewline"), ""):
                with self.subTest(key=key, bad=bad):
                    self.reject_variant(lambda value, key=key, bad=bad: value.__setitem__(key, bad))
        for key in ("hermes_exe", "python_exe"):
            for bad in (self.root / "not-installed.exe", self.root):
                with self.subTest(key=key, executable=bad):
                    self.reject_variant(lambda value, key=key, bad=bad: value.__setitem__(key, str(bad)))

    def test_refuses_existing_output_without_overwriting_any_files(self):
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("untouched", encoding="utf-8")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refused", result.stderr.lower())
        self.assertEqual(list(self.output.iterdir()), [marker])
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
        result = self.prepare(output=marker)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")

    def test_refuses_output_inside_package_or_installed_home(self):
        self.home.mkdir()
        for parent in (self.package, self.home):
            with self.subTest(parent=parent):
                destination = parent / "forbidden-stage"
                result = self.prepare(output=destination)
                try:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("outside", result.stderr.lower())
                    self.assertFalse(destination.exists())
                finally:
                    if destination.exists():
                        shutil.rmtree(destination)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_refuses_reparse_ancestor_including_lexical_dotdot_paths(self):
        actual = self.root / "actual"
        actual.mkdir()
        marker = actual / "keep.txt"
        marker.write_bytes(b"untouched")
        link = self.root / "junction"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(actual)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.addCleanup(lambda: link.rmdir())
        else:
            link.symlink_to(actual, target_is_directory=True)
            self.addCleanup(lambda: link.unlink())
        for destination in (link / "new-stage", link / ".." / "new-stage"):
            with self.subTest(destination=destination):
                result = self.prepare(output=destination)
                try:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("reparse", result.stderr.lower())
                    self.assertFalse(destination.exists())
                finally:
                    if destination.exists():
                        shutil.rmtree(destination)
        self.assertEqual(list(actual.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"untouched")

    def test_forces_disabled_shadow_no_routing_and_no_arg_entry_has_no_effects(self):
        self.settings["config"].update(enabled=True, mode="live", cutover_ms=12345,
                                       notify_mode="notify", handoff_unblock=True, auto_gate=True,
                                       runtime_dir=(self.root / "unwanted-runtime").as_posix())
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = json.loads((self.output / "integrations/clickup-dedicated-panel/config.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual({key: cfg[key] for key in SAFE_FLAGS}, SAFE_FLAGS)
        self.assertEqual(cfg["runtime_dir"],
                         (self.home / "integrations/clickup-dedicated-panel").as_posix())
        before = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, str(self.output / "scripts/clickup_dedicated_panel_intake.py")],
                                cwd=self.output, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(before, {p.relative_to(self.output): p.read_bytes()
                                  for p in self.output.rglob("*") if p.is_file()})
        self.assertFalse(self.home.exists())
        self.assertFalse(self.credential.exists())
        self.assertFalse((self.root / "unwanted-runtime").exists())

    def test_prepares_only_offline_payload_with_matching_constants_config_and_manifest(self):
        self.settings["config"]["plain_attr_allowlist"] = ["block-id"]
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())
        self.assertFalse(self.credential.exists())
        runtime = (self.home / "integrations" / "clickup-dedicated-panel").as_posix()
        config_file = self.output / "integrations" / "clickup-dedicated-panel" / "config.json"
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        self.assertEqual({key: cfg[key] for key in SAFE_FLAGS}, SAFE_FLAGS)
        self.assertEqual(cfg["runtime_dir"], runtime)
        for key, value in self.settings["config"].items():
            self.assertEqual(cfg[key], value)
        staged = self.output / "scripts" / "clickup_intake"
        values = {"DEFAULT_RUNTIME_DIR": runtime,
                  "APPROVERS": {int(k): v for k, v in cfg["approvers"].items()},
                  "HERMES_EXE": self.settings["hermes_exe"],
                  "CREDENTIAL": self.settings["credential_path"],
                  "PYTHON_EXE": self.settings["python_exe"],
                  "ENTRY_SCRIPT": (self.home / "scripts" / "clickup_dedicated_panel_intake.py").as_posix(),
                  "DISCORD_GUILD_ID": self.settings["discord_guild_id"],
                  "REQUEST_SKILLS": tuple(self.settings["request_skills"])}
        for name, replaced in REPLACED_CONSTANTS.items():
            self.assertEqual(constants(staged / name), {key: values[key] for key in replaced})
        for source in (self.package / "clickup_intake").glob("*.py"):
            self.assertEqual(algorithms(staged / source.name, REPLACED_CONSTANTS.get(source.name, ())),
                             algorithms(source, REPLACED_CONSTANTS.get(source.name, ())))
        for source in (self.package / "templates").glob("*.md"):
            self.assertEqual((self.output / "scripts" / "templates" / source.name).read_bytes(),
                             source.read_bytes())
        self.assertEqual((self.output / "scripts" / "clickup_dedicated_panel_intake.py").read_bytes(),
                         (self.package / "clickup_dedicated_panel_intake.py").read_bytes())
        manifest = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        payload = sorted(p.relative_to(self.output).as_posix() for p in self.output.rglob("*")
                         if p.is_file() and p.name != "manifest.json")
        self.assertEqual(manifest["algorithm"], "sha256")
        self.assertEqual(manifest["file_count"], len(payload))
        self.assertEqual([entry["path"] for entry in manifest["files"]], payload)
        for entry in manifest["files"]:
            content = (self.output / entry["path"]).read_bytes()
            self.assertEqual(entry["size"], len(content))
            self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(set(payload), {
            "scripts/clickup_dedicated_panel_intake.py", "integrations/clickup-dedicated-panel/config.json",
            *("scripts/clickup_intake/" + p.name for p in (self.package / "clickup_intake").glob("*.py")),
            *("scripts/templates/" + p.name for p in (self.package / "templates").glob("*.md")),
        })


if __name__ == "__main__":
    unittest.main()
