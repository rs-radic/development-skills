"""Runtime configuration ``config.json`` (plan r5 §2.1). Unknown or invalid values are refused."""

import dataclasses
import json
import os
from dataclasses import dataclass, field

DEFAULT_RUNTIME_DIR = "C:/Hermes/integrations/clickup-dedicated-panel"

# Exactly the eight corrected (name, type) pairs; no alias for the old spellings.
EXPECTED_STATUSES = (
    ("reviewing feature", "open"),
    ("business requirements", "unstarted"),
    ("technical requirements", "unstarted"),
    ("waiting for approval", "unstarted"),
    ("implementing", "custom"),
    ("implemented", "done"),
    ("ready for deployment", "done"),
    ("deployed", "closed"),
)
INTAKE_STATUSES = ("reviewing feature", "business requirements", "technical requirements", "waiting for approval")
APPROVERS = {900000101: "Approver One", 900000102: "Approver Two", 900000103: "Approver Three"}
INTERVALS = {"active_light_seconds": 300, "active_full_seconds": 900, "promotion_full_seconds": 300,
             "late_light_seconds": 900, "late_full_seconds": 3600}
BUDGETS = {"scan_requests": 40, "rate_limit_reserve": 20, "per_task_requests": 15, "discovery_max_pages": 10,
           "comment_pages_cap": 40, "threaded_roots_cap": 200}


@dataclass(frozen=True)
class Config:
    workspace_id: str = "11111111111"
    space_id: str = "22222222222"
    folder_id: str = "3333333333333333"
    list_id: str = "4444444444444444"
    bot_user_id: int = 900000001
    approvers: dict = field(default_factory=lambda: dict(APPROVERS))
    expected_statuses: tuple = EXPECTED_STATUSES
    intake_statuses: tuple = INTAKE_STATUSES
    quiet_seconds: int = 180
    intervals: dict = field(default_factory=lambda: dict(INTERVALS))
    budgets: dict = field(default_factory=lambda: dict(BUDGETS))
    ticket_renew_seconds: int = 600
    ticket_lease_seconds: int = 1500
    plain_attr_allowlist: tuple = ()
    cutover_ms: object = None
    mode: str = "shadow"
    enabled: bool = False
    board: str = "example-project"
    discord_chat: str = "111111111111111111"
    notify_mode: str = "notify"
    handoff_unblock: bool = True
    auto_gate: bool = True
    runtime_dir: str = DEFAULT_RUNTIME_DIR

    @property
    def state_path(self):
        return os.path.join(self.runtime_dir, "state.sqlite3")


_FIELDS = {f.name for f in dataclasses.fields(Config)}


def _fail(msg):
    raise ValueError(f"invalid config: {msg}")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _positive_ints(name, given, defaults, minimum):
    if not isinstance(given, dict) or set(given) - set(defaults):
        _fail(f"{name} keys must be within {sorted(defaults)}")
    merged = {**defaults, **given}
    if not all(_is_int(v) and v >= minimum for v in merged.values()):
        _fail(f"{name} values must be integers >= {minimum}")
    return merged


def from_dict(data):
    """Validate a parsed ``config.json`` object and merge it over the plan defaults."""
    if not isinstance(data, dict):
        _fail("top level must be an object")
    unknown = set(data) - _FIELDS
    if unknown:
        _fail(f"unknown keys {sorted(unknown)}")
    d = dict(data)

    for key in ("workspace_id", "space_id", "folder_id", "list_id", "board", "discord_chat", "runtime_dir"):
        if key in d and (not isinstance(d[key], str) or not d[key]):
            _fail(f"{key} must be a non-empty string")
    for key in ("bot_user_id",):
        if key in d and not _is_int(d[key]):
            _fail(f"{key} must be an integer")
    for key in ("quiet_seconds", "ticket_renew_seconds", "ticket_lease_seconds"):
        if key in d and not (_is_int(d[key]) and d[key] > 0):
            _fail(f"{key} must be a positive integer")
    for key in ("enabled", "handoff_unblock", "auto_gate"):
        if key in d and not isinstance(d[key], bool):
            _fail(f"{key} must be true or false")
    if d.get("mode", "shadow") not in ("shadow", "live"):
        _fail("mode must be shadow or live")
    if d.get("notify_mode", "notify") not in ("notify", "off"):
        _fail("notify_mode must be notify or off")
    if "cutover_ms" in d and d["cutover_ms"] is not None and not _is_int(d["cutover_ms"]):
        _fail("cutover_ms must be an integer or null")

    if "approvers" in d:
        a = d["approvers"]
        if not isinstance(a, dict) or not all(isinstance(k, str) and k.isdigit() and isinstance(v, str)
                                              for k, v in a.items()):
            _fail("approvers must map numeric user ids to names")
        d["approvers"] = {int(k): v for k, v in a.items()}
    if "expected_statuses" in d:
        pairs = d["expected_statuses"]
        if not isinstance(pairs, list) or not all(
                isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) and x for x in p) for p in pairs):
            _fail("expected_statuses must be a list of [name, type] pairs")
        pairs = tuple(tuple(p) for p in pairs)
        if len({n for n, _ in pairs}) != len(pairs):
            _fail("expected_statuses names must be unique")
        d["expected_statuses"] = pairs
    if "intake_statuses" in d:
        if not isinstance(d["intake_statuses"], list) or not all(isinstance(s, str) for s in d["intake_statuses"]):
            _fail("intake_statuses must be a list of names")
        d["intake_statuses"] = tuple(d["intake_statuses"])
    if "plain_attr_allowlist" in d:
        if not isinstance(d["plain_attr_allowlist"], list) or not all(
                isinstance(s, str) for s in d["plain_attr_allowlist"]):
            _fail("plain_attr_allowlist must be a list of attribute keys")
        d["plain_attr_allowlist"] = tuple(d["plain_attr_allowlist"])
    if "intervals" in d:
        d["intervals"] = _positive_ints("intervals", d["intervals"], INTERVALS, 1)
    if "budgets" in d:
        d["budgets"] = _positive_ints("budgets", d["budgets"], BUDGETS, 1)

    cfg = Config(**d)
    names = {n: t for n, t in cfg.expected_statuses}
    for s in cfg.intake_statuses:
        if names.get(s) not in ("open", "unstarted"):
            _fail(f"intake status {s!r} must be an open/unstarted expected status")
    return cfg


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as e:
            _fail(f"not JSON ({e.__class__.__name__})")
    return from_dict(data)
