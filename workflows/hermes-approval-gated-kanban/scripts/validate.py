"""Validate this public Hermes Kanban workflow folder without modifying Hermes state."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "planner", "plan-reviewer", "orchestrator", "implementor", "spec-reviewer",
    "quality-security-reviewer", "integrator", "e2e-tester",
}
GENERIC_SKILLS = {"approval-gated-kanban-development", "plan-review-gate"}
BUNDLED_SKILLS = {"sdlc-review"}
REQUIRED = {
    "README.md", "skill-routing.md", "profiles.json", "templates/planner-card.md",
    "templates/orchestrator-card.md", "examples/partner-network-api/README.md",
    "skills/approval-gated-kanban-development/SKILL.md",
    "skills/plan-review-gate/SKILL.md", "scripts/validate.py",
}
PRIVATE_MARKERS = (
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)[A-Z]:[/\\]Users[/\\][^/\\]+"),
    re.compile(rb"(?i)(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}\.\d{1,3}\.\d{1,3}"),
)


def require(condition: bool, message: object) -> None:
    if not condition:
        raise ValueError(message)


def check() -> None:
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()}
    require(actual == REQUIRED, ("unexpected or missing files", sorted(actual ^ REQUIRED)))
    data = json.loads((ROOT / "profiles.json").read_text(encoding="utf-8"))
    require(data["system"] == "Hermes Agent", "wrong system")
    require(set(data["profiles"]) == EXPECTED, "unexpected profiles")
    settings = data["global_settings_observed"]
    require(settings["kanban.auto_decompose"] is False, "auto-decompose snapshot changed")
    require(settings["kanban.review_dispatch"] is True, "review dispatch unavailable")
    require(settings["kanban.dispatch_in_gateway"] is True, "dispatcher unavailable")
    require("kanban.dispatch_profiles" not in settings, "dispatch_profiles must be omitted, not null")
    require(data["settings_intentionally_absent"] == ["kanban.dispatch_profiles"], "missing omission guidance")
    for name, route in data["profiles"].items():
        require(bool(route["model"] and route["provider"]), ("missing model", name))
        require(route["reasoning_effort"] in {"medium", "high", "xhigh"}, ("reasoning", name))
        require(bool(route["role"].strip()), ("empty role", name))
        require(set(route["skills"]) <= GENERIC_SKILLS | BUNDLED_SKILLS, ("unknown skill", name))
        require(bool(route["skills"]), ("no skills", name))
    require("plan-review-gate" in data["profiles"]["planner"]["skills"], "planner gate missing")
    require("plan-review-gate" in data["profiles"]["plan-reviewer"]["skills"], "reviewer gate missing")
    require("sdlc-review" in data["profiles"]["spec-reviewer"]["skills"], "native review skill missing")
    for skill in GENERIC_SKILLS:
        content = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
        require(content.startswith("---\n"), ("invalid skill frontmatter", skill))
        match = re.match(r"\A---\n(?P<fm>.*?)\n---\n(?P<body>[\s\S]+)", content, re.S)
        require(match is not None, ("missing skill body", skill))
        fm = match.group("fm")
        require(bool(re.search(rf"(?m)^name: {re.escape(skill)}$", fm)), ("skill name", skill))
        require(bool(re.search(r"(?m)^description: Use when .+\.$", fm)), ("skill description", skill))
        require(bool(match.group("body").strip()), ("empty skill", skill))
    for rel in actual:
        payload = (ROOT / rel).read_bytes()
        if rel.endswith((".md", ".json", ".py")):
            require(b"\r\n" not in payload, ("CRLF", rel))
        for pattern in PRIVATE_MARKERS:
            require(not pattern.search(payload), ("potential private data", rel, pattern.pattern))
    print(f"PASS: {len(EXPECTED)} profiles, {len(GENERIC_SKILLS)} skills, {len(actual)} files; no private-data markers")


if __name__ == "__main__":
    try:
        check()
    except (ValueError, KeyError, UnicodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
