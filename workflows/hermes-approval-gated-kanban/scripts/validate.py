"""Validate this public Hermes Kanban workflow folder without modifying Hermes state."""
from __future__ import annotations

import json
import re
import sys
import zlib
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
    "README.md", "assets/hermes-approval-gated-kanban-flow.png", "skill-routing.md", "profiles.json", "templates/planner-card.md",
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
    require(data["schema_version"] == 2, "schema version")
    require(data["system"] == "Hermes Agent", "wrong system")
    require(set(data["profiles"]) == EXPECTED, "unexpected profiles")
    settings = data["global_settings_observed"]
    explicit = data["global_settings_explicit"]
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
            require(isinstance(model_cfg["base_url"], str), ("invalid model URL", label))
            parsed = urlsplit(model_cfg["base_url"])
            require(not parsed.username and not parsed.password, ("credential-bearing model URL", label))
            sensitive_key = re.compile(r"(?:^|[_-])(?:api[_-]?key|key|access[_-]?token|refresh[_-]?token|token|password|secret|credential|authorization|auth|signature|sig|session|code)(?:$|[_-])", re.I)
            require(not any(sensitive_key.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)), ("credential-bearing model URL query", label))
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
            require(isinstance(entry, dict) and set(entry) == {"provider", "model"}, ("unsafe fallback entry", label))
            require(all(isinstance(v, str) and v.strip() for v in entry.values()), ("empty fallback", label))
            require((entry["model"], entry["provider"]) != primary, ("fallback repeats primary", label))
        require(len({(e["provider"], e["model"]) for e in fallbacks}) == len(fallbacks), ("duplicate fallback", label))
    default = data["default_profile_observed"]
    require(default["reasoning_effort"] in {"medium", "high", "xhigh"}, "default reasoning")
    check_runtime("default", default["model_config_explicit"], default["compression_config_explicit"], default["fallback_providers"], primary=(default["model_config_explicit"]["default"], default["model_config_explicit"]["provider"]))
    require(not default["fallback_providers"], "default fallback unexpectedly configured")
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
