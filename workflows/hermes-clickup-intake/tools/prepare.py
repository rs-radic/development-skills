#!/usr/bin/env python
"""Prepare a disabled ClickUp intake deployment in an offline staging directory.

This is not an installer. It never runs Hermes, reads the credential, schedules
jobs, contacts an API, or copies anything into an installed Hermes home.
"""

import argparse
import ast
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
from clickup_intake.config import from_dict  # noqa: E402


# Public fixture IDs are reserved sentinels, not deployable routing identities.
# Replace every ID in installation.example.json with an operator-supplied value.
RESERVED_IDS = frozenset({
    "11111111111", "22222222222", "3333333333333333", "4444444444444444",
    "900000001", "900000101", "900000102", "900000103",
    "111111111111111111", "222222222222222222",
})

SAFE_FLAGS = {
    "enabled": False,
    "mode": "shadow",
    "cutover_ms": None,
    "notify_mode": "off",
    "handoff_unblock": False,
    "auto_gate": False,
}


def replace_literals(source, replacements):
    """Replace only the value span of one top-level literal assignment per name."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for name, value in replacements.items():
        nodes = [node for node in tree.body if isinstance(node, ast.Assign)
                 and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id == name]
        if len(nodes) != 1:
            raise ValueError("expected exactly one literal assignment for " + name)
        old = nodes[0].value
        ast.literal_eval(old)
        start = offsets[old.lineno - 1] + old.col_offset
        end = offsets[old.end_lineno - 1] + old.end_col_offset
        edits.append((start, end, repr(value).encode("utf-8")))
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    compile(source, "<prepared source>", "exec")
    return source


def validate_id(value, name, *, integer=False):
    if integer:
        valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
    else:
        valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is not None
    if not valid:
        raise ValueError(name + " must be a positive " + ("integer" if integer else "decimal string"))
    if str(value) in RESERVED_IDS:
        raise ValueError(name + " uses a reserved public fixture ID; replace all example IDs")


def validate_settings(settings):
    allowed = {"hermes_home", "hermes_exe", "python_exe", "credential_path",
               "discord_guild_id", "request_skills", "config"}
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError("unknown settings keys; API tokens are not accepted")
    if allowed - set(settings):
        raise ValueError("missing required settings: " + ", ".join(sorted(allowed - set(settings))))
    for name in ("hermes_home", "hermes_exe", "python_exe", "credential_path"):
        value = settings[name]
        if (not isinstance(value, str) or not value or not Path(value).is_absolute()
                or any(ch.isspace() or ch in "`\x00" for ch in value)):
            raise ValueError(name + " must be absolute and contain no whitespace, backticks or NUL")
        if name in ("hermes_exe", "python_exe") and not Path(value).is_file():
            raise ValueError(name + " must name an existing executable file (it is not executed)")
    data = settings["config"]
    from_dict(data)  # Validate the supplied values before applying the offline safety overrides.
    required = {"workspace_id", "space_id", "folder_id", "list_id", "bot_user_id",
                "approvers", "board", "discord_chat"}
    if required - set(data):
        raise ValueError("missing explicit config fields: " + ", ".join(sorted(required - set(data))))
    for name in ("workspace_id", "space_id", "folder_id", "list_id", "discord_chat"):
        validate_id(data[name], "config." + name)
    validate_id(data["bot_user_id"], "config.bot_user_id", integer=True)
    validate_id(settings["discord_guild_id"], "discord_guild_id")
    if not data["approvers"]:
        raise ValueError("config.approvers must not be empty")
    for user_id, name in data["approvers"].items():
        validate_id(user_id, "config.approvers key")
        if not name.strip():
            raise ValueError("config.approvers names must not be blank")
    skills = settings["request_skills"]
    if (not isinstance(skills, list) or not skills
            or not all(isinstance(skill, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", skill)
                       for skill in skills)
            or len(set(skills)) != len(skills)):
        raise ValueError("request_skills must be a non-empty list of distinct skill identifiers")


def build_payload(settings):
    validate_settings(settings)
    home = Path(settings["hermes_home"])
    runtime_dir = (home / "integrations" / "clickup-dedicated-panel").as_posix()
    cfg_data = dict(settings["config"], runtime_dir=runtime_dir, **SAFE_FLAGS)
    cfg = from_dict(cfg_data)
    replacements = {
        "config.py": {"DEFAULT_RUNTIME_DIR": runtime_dir, "APPROVERS": cfg.approvers},
        "main.py": {"HERMES_EXE": settings["hermes_exe"], "CREDENTIAL": settings["credential_path"]},
        "render.py": {"PYTHON_EXE": settings["python_exe"],
                      "ENTRY_SCRIPT": (home / "scripts" / "clickup_dedicated_panel_intake.py").as_posix()},
        "kanban_port.py": {"DISCORD_GUILD_ID": settings["discord_guild_id"]},
        "engine.py": {"REQUEST_SKILLS": tuple(settings["request_skills"])},
    }
    module_names = {"__init__", "clickup_api", "clickup_effects", "config", "contracts", "engine",
                    "kanban_port", "main", "observe", "parse", "ports", "preworker", "preworker_snapshot",
                    "render", "rules", "stalled", "store", "ticket"}
    template_names = {"approval_record", "blocker_triage", "diagnostic_card", "gate_card", "orchestrator_card",
                      "preworker_card", "question_protocol", "recovery_card", "request_card", "ticket_recheck_paragraph"}
    for directory, names, suffix in (("clickup_intake", module_names, ".py"),
                                     ("templates", template_names, ".md")):
        if any(not (PACKAGE_ROOT / directory / (name + suffix)).is_file() for name in names):
            raise ValueError("package is missing required " + directory + " files")
    payload = {"scripts/clickup_dedicated_panel_intake.py":
               (PACKAGE_ROOT / "clickup_dedicated_panel_intake.py").read_bytes()}
    for source in sorted((PACKAGE_ROOT / "clickup_intake").glob("*.py")):
        data = source.read_bytes()
        if source.name in replacements:
            data = replace_literals(data, replacements[source.name])
        payload["scripts/clickup_intake/" + source.name] = data
    for source in sorted((PACKAGE_ROOT / "templates").glob("*.md")):
        payload["scripts/templates/" + source.name] = source.read_bytes()
    payload["integrations/clickup-dedicated-panel/config.json"] = json_bytes(dataclasses.asdict(cfg))
    return payload


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def reject_reparse_ancestors(path):
    # Check the lexical path BEFORE resolve() so a junction/../ spelling cannot
    # disappear during normalization. lstat also detects dangling symlinks.
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
            raise ValueError("output must not use symlink/reparse ancestors")


def validate_output(output, home):
    raw = output.absolute()
    reject_reparse_ancestors(raw)
    if raw.exists():
        raise ValueError("output already exists; no files were overwritten")
    if not raw.parent.is_dir():
        raise ValueError("output parent must be an existing directory")
    resolved = raw.resolve()
    for protected in (PACKAGE_ROOT, Path(home).resolve()):
        if resolved == protected or protected in resolved.parents:
            raise ValueError("output must be outside the package and hermes_home")
    return resolved


def prepare(settings, output):
    payload = build_payload(settings)
    output = validate_output(output, settings["hermes_home"])
    manifest = {"schema_version": 1, "algorithm": "sha256", "file_count": len(payload), "files": [
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(payload.items())]}
    output.mkdir()  # Exclusive creation; cleanup below owns only this new directory.
    try:
        for name, data in payload.items():
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (output / "manifest.json").write_bytes(json_bytes(manifest))
    except BaseException:
        shutil.rmtree(output)
        raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, type=Path, help="Private JSON with paths and IDs; no token")
    parser.add_argument("--output", required=True, type=Path, help="New offline staging directory, not Hermes home")
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.settings.read_text(encoding="utf-8"))
        manifest = prepare(settings, args.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Preparation refused: " + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"status": "prepared-disabled", "output": str(args.output),
                      "file_count": manifest["file_count"], "manifest": "manifest.json"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
