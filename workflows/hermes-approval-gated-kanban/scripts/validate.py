"""Validate this public Hermes Kanban workflow folder without modifying Hermes state."""
from __future__ import annotations

import json
import math
import re
import sys
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "planner", "plan-reviewer", "orchestrator", "implementor", "spec-reviewer",
    "quality-security-reviewer", "integrator", "e2e-tester",
}
GENERIC_SKILLS = {"approval-gated-kanban-development", "plan-review-gate"}
BUNDLED_SKILLS = {"sdlc-review"}
REQUIRED = {
    "README.md", "assets/hermes-approval-gated-kanban-flow.png", "skill-routing.md", "role-instructions.md", "profiles.json", "templates/planner-card.md",
    "templates/orchestrator-card.md", "examples/partner-network-api/README.md",
    "skills/approval-gated-kanban-development/SKILL.md",
    "skills/plan-review-gate/SKILL.md", "scripts/validate.py",
}
PRIVATE_MARKERS = (
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"(?i)[A-Z]:[/\\]Users[/\\][^/\\]+"),
    re.compile(rb"(?i)(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}"),
)


def require(condition: bool, message: object) -> None:
    if not condition:
        raise ValueError(message)


def check_public_url(url: object, label: str) -> None:
    require(isinstance(url, str), ("invalid public URL", label))
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError(("invalid public URL", label)) from None
    require(not parsed.username and not parsed.password, ("credential-bearing URL", label))
    # API endpoint fragments have no server-side meaning and may carry OAuth secrets.
    require(not parsed.fragment, ("URL endpoint fragment not allowed", label))
    sensitive_key = re.compile(r"(?:^|[_-])(?:api[_-]?key|key|access[_-]?token|refresh[_-]?token|token|password|secret|credential|authorization|auth|signature|sig|session|code)(?:$|[_-])", re.I)
    require(not any(sensitive_key.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)),
            ("credential-bearing URL query", label))


def check_flow_png(payload: bytes) -> None:
    """Decode the supplied, metadata-free 8-bit RGB PNG using only the standard library."""
    require(payload.startswith(b"\x89PNG\r\n\x1a\n"), "invalid flow PNG signature")
    chunks: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(payload):
        require(offset + 12 <= len(payload), "truncated flow PNG chunk")
        length = int.from_bytes(payload[offset:offset + 4], "big")
        end = offset + 12 + length
        require(end <= len(payload), "truncated flow PNG data")
        kind = payload[offset + 4:offset + 8]
        data = payload[offset + 8:offset + 8 + length]
        checksum = int.from_bytes(payload[end - 4:end], "big")
        require(zlib.crc32(kind + data) == checksum, "flow PNG chunk CRC mismatch")
        chunks.append((kind, data))
        offset = end
    kinds = [kind for kind, _ in chunks]
    require(len(kinds) >= 3 and kinds[0] == b"IHDR" and kinds[-1] == b"IEND"
            and all(kind == b"IDAT" for kind in kinds[1:-1]), "unexpected flow PNG chunks")
    require(chunks[0][1] == b"\x00\x00\x06\x00\x00\x00\x04\x00\x08\x02\x00\x00\x00"
            and chunks[-1][1] == b"", "unexpected flow PNG format")
    decoder = zlib.decompressobj()
    row_size = 1 + 1536 * 3
    expected_size = 1024 * row_size
    try:
        pixels = decoder.decompress(b"".join(data for _, data in chunks[1:-1]), expected_size + 1)
    except zlib.error:
        raise ValueError("flow PNG image data invalid") from None
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail
            and len(pixels) == expected_size, "flow PNG image data invalid")
    require(all(pixels[row * row_size] in range(5) for row in range(1024)),
            "flow PNG scanline filter invalid")


def check() -> None:
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file()}
    require(actual == REQUIRED, ("unexpected or missing files", sorted(actual ^ REQUIRED)))
    image_path = "assets/hermes-approval-gated-kanban-flow.png"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    embedded = re.search(r"!\[([^\]\n]+)\]\(assets/hermes-approval-gated-kanban-flow\.png\)", readme)
    require(embedded is not None and bool(embedded.group(1).strip()), "flow image and alt text missing from README")
    check_flow_png((ROOT / image_path).read_bytes())
    data = json.loads((ROOT / "profiles.json").read_text(encoding="utf-8"))
    require(data["schema_version"] == 4, "schema version")
    require(data["system"] == "Hermes Agent", "wrong system")
    require(set(data["profiles"]) == EXPECTED, "unexpected profiles")
    instructions = (ROOT / "role-instructions.md").read_text(encoding="utf-8")
    require(all(len(re.findall(rf"(?m)^## {re.escape(role)}$", instructions)) == 1 for role in EXPECTED),
            "missing or duplicate role instruction sections")
    require("## Mandatory Kanban worktree and branch cleanup\n" in instructions,
            "common cleanup instructions missing")
    delegation = data["delegation_snapshot"]
    require(isinstance(delegation, dict) and set(delegation) == {"observed_at_utc", "scope", "profiles"}, "delegation snapshot shape")
    require(isinstance(delegation["observed_at_utc"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", delegation["observed_at_utc"]), "delegation observation date")
    date.fromisoformat(delegation["observed_at_utc"])
    require(isinstance(delegation["scope"], str) and bool(delegation["scope"].strip()), "delegation scope")
    require(isinstance(delegation["profiles"], dict) and set(delegation["profiles"]) == EXPECTED | {"default"}, "delegation profile roster")
    limit_keys = {"oneshot_max_children", "max_concurrent_children", "max_spawn_depth"}
    for label, row in delegation["profiles"].items():
        require(isinstance(row, dict) and set(row) == {"config_explicit", "limits_effective", "options_effective"}, ("delegation row shape", label))
        configured = row["config_explicit"]
        effective = row["limits_effective"]
        numeric_keys = limit_keys | {"max_iterations", "child_timeout_seconds"}
        bool_keys = {"inherit_mcp_toolsets", "orchestrator_enabled", "subagent_auto_approve"}
        string_keys = {"model", "provider", "reasoning_effort", "base_url", "api_mode"}
        require(isinstance(configured, dict) and set(configured) <= numeric_keys | bool_keys | string_keys, ("unsafe delegation config", label))
        require(isinstance(effective, dict) and set(effective) == limit_keys, ("delegation effective limits", label))
        require(all(type(value) is int and value >= (0 if key == "oneshot_max_children" else 1)
                    for key, value in effective.items()), ("invalid delegation limit", label))
        for key, value in configured.items():
            if key == "child_timeout_seconds":
                require(type(value) in (int, float) and math.isfinite(value), ("invalid delegation timeout", label))
            elif key in numeric_keys:
                require(type(value) is int and value >= (0 if key == "oneshot_max_children" else 1), ("invalid delegation number", label))
            elif key in bool_keys:
                require(type(value) is bool, ("invalid delegation boolean", label))
            else:
                require(isinstance(value, str), ("invalid delegation string", label))
                if key == "base_url":
                    check_public_url(value, label)
        require(all(configured[key] == effective[key] for key in limit_keys & configured.keys()), ("delegation explicit/effective mismatch", label))
        options = row["options_effective"]
        require(isinstance(options, dict) and set(options) == bool_keys | {"child_timeout_seconds"}, ("delegation effective options", label))
        require(all(type(options[key]) is bool for key in bool_keys), ("delegation effective booleans", label))
        timeout = options["child_timeout_seconds"]
        require(timeout is None or (type(timeout) in (int, float) and math.isfinite(timeout) and timeout >= 30), ("delegation effective timeout", label))
        # Installed snapshot defaults; compare explicit booleans without coercion.
        bool_defaults = {"inherit_mcp_toolsets": True, "orchestrator_enabled": True, "subagent_auto_approve": False}
        require(all(options[key] == configured.get(key, bool_defaults[key]) for key in bool_keys),
                ("delegation explicit/effective boolean mismatch", label))
        raw_timeout = configured.get("child_timeout_seconds", 0)
        expected_timeout = None if raw_timeout <= 0 else max(30.0, float(raw_timeout))
        require(timeout == expected_timeout, ("delegation explicit/effective timeout mismatch", label))
    settings = data["global_settings_observed"]
    explicit = data["global_settings_explicit"]
    global_allowed = {"kanban.dispatch_in_gateway", "kanban.dispatch_interval_seconds", "kanban.failure_limit",
                      "kanban.worker_log_rotate_bytes", "kanban.worker_log_backup_count", "kanban.default_assignee",
                      "kanban.auto_decompose", "kanban.auto_decompose_per_tick", "kanban.dispatch_stale_timeout_seconds",
                      "kanban.orchestrator_profile", "gateway.multiplex_profiles"}
    require(isinstance(explicit, dict) and set(explicit) <= global_allowed, "unsafe explicit global settings")
    global_bools = {"kanban.dispatch_in_gateway", "kanban.auto_decompose", "gateway.multiplex_profiles"}
    global_strings = {"kanban.default_assignee", "kanban.orchestrator_profile"}
    for key, value in explicit.items():
        if key in global_bools:
            require(type(value) is bool, "invalid global boolean")
        elif key in global_strings:
            require(isinstance(value, str) and (not value or re.fullmatch(r"[a-z][a-z0-9_-]*", value)), "invalid global profile name")
        else:
            require(type(value) is int and value >= 0, "invalid global number")
    require(isinstance(explicit, dict) and all(k in settings and settings[k] == v for k, v in explicit.items()), "global explicit/effective mismatch")
    require("kanban.dispatch_profiles" not in explicit, "dispatch_profiles must be omitted, not null")
    require(settings["kanban.auto_decompose"] is False, "auto-decompose snapshot changed")
    require(settings["kanban.review_dispatch"] is True, "review dispatch unavailable")
    require(settings["kanban.dispatch_in_gateway"] is True, "dispatcher unavailable")
    require("kanban.dispatch_profiles" not in settings, "dispatch_profiles must be omitted, not null")
    require(data["settings_intentionally_absent"] == ["kanban.dispatch_profiles"], "missing omission guidance")
    def check_runtime(label: str, model_cfg: object, compression: object, fallbacks: object, *, primary: tuple[str, str]) -> None:
        require(isinstance(model_cfg, dict) and set(model_cfg) <= {"default", "provider", "context_length", "base_url", "api_mode"}, ("unsafe model config", label))
        require((model_cfg.get("default"), model_cfg.get("provider")) == primary, ("model route mismatch", label))
        if "base_url" in model_cfg:
            check_public_url(model_cfg["base_url"], label)
        ctx = model_cfg.get("context_length")
        require(type(ctx) is int and ctx > 0, ("context window missing", label))
        require(isinstance(compression, dict), ("compression missing", label))
        cap = compression.get("threshold_tokens")
        require(type(cap) is int and cap > 0, ("compression cap missing", label))
        if "threshold" in compression:
            ratio = compression["threshold"]
            require(type(ratio) in (int, float) and 0 < ratio <= 1, ("compression ratio", label))
        allowed_compression = {"enabled", "threshold", "threshold_tokens", "target_ratio", "protect_last_n", "hygiene_hard_message_limit", "protect_first_n", "abort_on_summary_failure", "codex_gpt55_autoraise", "codex_responses_native", "progress_notices"}
        require(set(compression) <= allowed_compression and all(type(v) in (int, float, bool) for v in compression.values()), ("unsafe compression config", label))
        require(isinstance(fallbacks, list), ("fallback routes missing", label))
        for entry in fallbacks:
            require(isinstance(entry, dict) and {"provider", "model"} <= set(entry) <= {"provider", "model", "api_mode", "base_url"},
                    ("unsafe fallback entry", label))
            require(all(isinstance(v, str) and v.strip() for v in entry.values()), ("empty fallback", label))
            if "base_url" in entry:
                check_public_url(entry["base_url"], label)
            require(re.fullmatch(r"[a-z][a-z0-9_-]*", entry.get("api_mode", "chat")) is not None, ("invalid fallback api_mode", label))
            require((entry["model"], entry["provider"]) != primary, ("fallback repeats primary", label))
        require(len({(e["provider"], e["model"]) for e in fallbacks}) == len(fallbacks), ("duplicate fallback", label))
    default = data["default_profile_observed"]
    require(default["reasoning_effort"] in {"medium", "high", "xhigh"}, "default reasoning")
    check_runtime("default", default["model_config_explicit"], default["compression_config_explicit"], default["fallback_providers"], primary=(default["model_config_explicit"]["default"], default["model_config_explicit"]["provider"]))
    efforts = {"none", "minimal", "low", "medium", "high", "xhigh"}
    for label, snapshot in {"default": default, **data["profiles"]}.items():
        context = snapshot["context_config_explicit"]
        require(isinstance(context, dict) and set(context) <= {"engine"}, ("unsafe context config", label))
        require(not context or context["engine"] == "compressor", ("unexpected context engine", label))
        agent = snapshot["agent_config_explicit"]
        require(isinstance(agent, dict) and set(agent) <= {"reasoning_effort", "reasoning_overrides", "service_tier", "max_turns", "max_tokens", "temperature", "top_p"}, ("unsafe agent config", label))
        require(agent.get("reasoning_effort") == snapshot["reasoning_effort"], ("agent reasoning mismatch", label))
        overrides = agent.get("reasoning_overrides", {})
        require(isinstance(overrides, dict) and all(isinstance(k, str) and k.strip() and v in efforts for k, v in overrides.items()),
                ("invalid reasoning overrides", label))
        require(isinstance(agent.get("service_tier", "normal"), str) and re.fullmatch(r"[a-z][a-z0-9_-]*", agent.get("service_tier", "normal")),
                ("invalid service tier", label))
        # Exact-key per-model resolution only; aliases/provider-qualified keys need fresh installed-resolver review.
        def expected_effort(model: str) -> str:
            return overrides.get(model, snapshot["reasoning_effort"])
        require(snapshot["reasoning_effective"] == expected_effort(snapshot["model_config_explicit"]["default"]), ("primary reasoning mismatch", label))
        fallback_efforts = snapshot["fallback_reasoning_effective"]
        require(isinstance(fallback_efforts, list) and len(fallback_efforts) == len(snapshot["fallback_providers"])
                and all(effort == expected_effort(entry["model"]) for effort, entry in zip(fallback_efforts, snapshot["fallback_providers"])),
                ("fallback reasoning mismatch", label))
        for key in set(agent) - {"reasoning_effort", "reasoning_overrides", "service_tier"}:
            require(type(agent[key]) in (int, float) and math.isfinite(agent[key]), ("invalid agent number", label))
        runtime = snapshot["compression_runtime_observed"]
        require(isinstance(runtime, dict) and set(runtime) == {"context_length", "threshold", "threshold_tokens"}, ("compression runtime shape", label))
        require(type(runtime["context_length"]) is int and runtime["context_length"] == snapshot["model_config_explicit"]["context_length"], ("runtime context mismatch", label))
        require(type(runtime["threshold"]) in (int, float) and 0 < runtime["threshold"] <= 1, ("runtime compression ratio", label))
        require(type(runtime["threshold_tokens"]) is int and 0 < runtime["threshold_tokens"] <= snapshot["compression_config_explicit"]["threshold_tokens"], ("runtime compression cap", label))
        # Frozen installed-build evidence, not a model catalog or future provider default.
        # Only these captured routes, the pinned 900k window and an unreserved output
        # budget are supported. Smaller/floor-bound windows need fresh source review.
        model_cfg = snapshot["model_config_explicit"]
        compression = snapshot["compression_config_explicit"]
        primary = (model_cfg["default"], model_cfg["provider"])
        require(primary in {("gpt-6.1-sol", "openai-codex"), ("claude-opus-5-5", "anthropic")},
                ("unsupported compression snapshot route", label))
        expected_ratio = compression.get("threshold", 0.50)
        if primary == ("gpt-6.1-sol", "openai-codex"):
            autoraise = compression.get("codex_gpt55_autoraise", True)
            require(type(autoraise) is bool, ("invalid compression autoraise", label))
            if autoraise:
                expected_ratio = 0.85
        require(type(expected_ratio) in (int, float) and math.isfinite(expected_ratio)
                and 0 < expected_ratio < 1, ("unsupported compression snapshot budget", label))
        ratio_trigger = int(runtime["context_length"] * expected_ratio)
        require(runtime["context_length"] == 900000 and "max_tokens" not in agent
                and 64000 <= ratio_trigger < runtime["context_length"],
                ("unsupported compression snapshot budget", label))
        require(runtime["threshold"] == expected_ratio, ("runtime compression ratio mismatch", label))
        require(runtime["threshold_tokens"] == min(ratio_trigger, compression["threshold_tokens"]),
                ("runtime compression trigger mismatch", label))
    for name, route in data["profiles"].items():
        require(bool(route["model"] and route["provider"]), ("missing model", name))
        check_runtime(name, route["model_config_explicit"], route["compression_config_explicit"], route["fallback_providers"], primary=(route["model"], route["provider"]))
        require(bool(route["fallback_providers"]), ("worker fallback missing", name))
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
