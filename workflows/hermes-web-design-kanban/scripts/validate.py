"""Check this documentation package offline, without runtime imports or writes."""
import argparse
import json
from pathlib import Path
import re

FILES = {
    "README.md", "workflow.json", "skills/concept-first-web-design/SKILL.md",
    "templates/concept.md", "templates/review-and-e2e.md", "templates/publication.md",
    "templates/cleanup.md", "scripts/validate.py",
}
OPTIONAL_SIBLING = "../hermes-approval-gated-kanban/profiles.json"

# Fixed public contract; changing it requires reviewing the workflow, not a live vote.
STAGES = ["concept", "security/spec review", "technical approval", "completed"]
GATES = [
    ["completed_concept_revision", "concept_approval"],
    ["spec_pass", "quality_security_pass", "shared_dev_deployed", "shared_dev_e2e_pass"],
    ["release_approval", "task_delta_published", "existing_ci_deployed", "production_verified", "cleanup_verified"],
    [],
]
ROLES = {"orchestrator", "implementor", "spec-reviewer", "quality-security-reviewer", "integrator", "e2e-tester"}
APPROVAL = {
    "sets": ["concept", "release"],
    "independently_configured": True,
    "eligible_votes_required": 1,
    "authenticated_platform_author_required": True,
    "explicit_action_required": True,
    "exact_revision_required": True,
    "freshness_required": True,
    "bot_or_forwarded_approval_allowed": False,
    "status_change_authorizes_release": False,
    "concept_grants_release": False,
}
LIFECYCLE = {
    "same_task": True,
    "stable_preview": True,
    "revision_request": "tagged_list_member",
    "extra_human_plan_gate": False,
    "native_same_card_spec": True,
    "corrections": "same_task_and_existing_cards",
    "review_order": ["spec", "quality_security", "shared_dev_deployment", "shared_dev_e2e", "release_approval"],
    "release_scope": "task_delta_and_accepted_dependencies",
    "cancellation_signal": "archive",
    "unexpected_tracker_state": "non_destructive_hold",
    "archive_rolls_back_production": False,
    "completion_requires": ["production_verified", "cleanup_verified"],
}


def same(left, right):
    """JSON equality with booleans distinct from numbers."""
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def check_manifest(data):
    errors = []
    keys = {"schema_version", "kind", "visible_stages", "roles", "approval_policy", "lifecycle"}
    if not isinstance(data, dict) or set(data) != keys:
        return ["manifest_shape"]
    if type(data["schema_version"]) is not int or data["schema_version"] != 1 or data["kind"] != "documentation-template":
        errors.append("manifest_shape")
    stages = data["visible_stages"]
    if not isinstance(stages, list) or any(not isinstance(s, dict) or set(s) != {"name", "exit_evidence"} for s in stages):
        errors.append("visible_stages")
    else:
        if [s["name"] for s in stages] != STAGES:
            errors.append("visible_stages")
        if not same([s["exit_evidence"] for s in stages], GATES):
            errors.append("stage_gates")
    roles = data["roles"]
    if not isinstance(roles, dict) or set(roles) != ROLES or any(not isinstance(v, str) or not v.strip() for v in roles.values()):
        errors.append("roles")
    if not same(data["approval_policy"], APPROVAL):
        errors.append("approval_policy")
    if not same(data["lifecycle"], LIFECYCLE):
        errors.append("lifecycle")
    return errors


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def check_package(root):
    """Read only the declared public files, never unknown extras or links."""
    errors = []
    texts = {}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != FILES:
        errors.append("package_files")
    for name in sorted(FILES):
        path = root / name
        if any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents):
            errors.append("package_files")
            continue
        if not path.resolve().is_relative_to(root):
            errors.append("package_files")
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > 100_000:
                errors.append("file_format")
                continue
            texts[name] = raw.decode("utf-8")
        except (OSError, UnicodeError):
            errors.append("file_format")
    # Heuristics catch obvious publication accidents, not all possible secrets.
    patterns = [
        r"https?://[^\s<>\"]+", r"\b[A-Za-z]:[\\/]", r"\b\d{9,}\b",
        r"/(?:home|Users|etc|var|tmp|opt)/", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",
        r"(?i)\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*[\"']?[^\s<\"']{6,}",
    ]
    for name, text in texts.items():
        if any(re.search(pattern, text) for pattern in patterns):
            errors.append("public_data")
        if name.endswith(".md"):
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                target = link.split("#", 1)[0]
                if not target:
                    continue
                if name == "README.md" and target == OPTIONAL_SIBLING:
                    continue  # Optional historical context; not part of a standalone package.
                resolved = (root / name).parent.joinpath(target).resolve()
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    errors.append("local_links")
    skill = texts.get("skills/concept-first-web-design/SKILL.md", "").replace("\r\n", "\n")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.+)", skill, re.DOTALL)
    if not match:
        errors.append("skill_frontmatter")
    else:
        rows = [line.partition(":") for line in match[1].splitlines()]
        metadata = {key: value.strip() for key, sep, value in rows if sep}
        description = metadata.get("description", "")
        if (len(metadata) != len(rows) or set(metadata) != {"name", "description", "version"}
                or metadata.get("name") != "concept-first-web-design"
                or not description.startswith("Use when ") or not description.endswith(".")
                or len(description) > 60 or not re.fullmatch(r"\d+\.\d+\.\d+", metadata.get("version", ""))):
            errors.append("skill_frontmatter")
    try:
        compile(texts.get("scripts/validate.py", ""), "scripts/validate.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        errors.append("python_syntax")
    return texts, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        texts, errors = check_package(args.directory.resolve())
    except OSError:
        texts, errors = {}, ["package_files"]
    try:
        data = json.loads(texts.get("workflow.json", ""), object_pairs_hook=unique_object)
        errors.extend(check_manifest(data))
    except (ValueError, TypeError):
        errors.append("manifest_format")
    print(json.dumps({"ok": not errors, "errors": sorted(set(errors))}))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
