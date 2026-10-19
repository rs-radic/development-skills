"""Frozen shared contracts (plan r5 §2.2, §3, §6, §8.4, §10).

S1-S4 consume these unchanged. A change after S0 SPEC PASS is a contract change
that stops for an orchestrator decision.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class Phase(str, Enum):
    """Glue-owned task phase (§6)."""
    BASELINE = "baseline"
    NOT_ADMITTED = "not_admitted"
    NEW_QUIET = "new_quiet"
    PLANNING = "planning"
    AWAITING_BUSINESS = "awaiting_business"
    AWAITING_TECHNICAL = "awaiting_technical"
    IN_REVIEW = "in_review"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    APPROVAL_QUEUED = "approval_queued"
    DEVELOPMENT = "development"
    IMPLEMENTED = "implemented"
    PROMOTION_WINDOW = "promotion_window"
    PROMOTION_HELD = "promotion_held"
    READY_FOR_DEPLOYMENT = "ready_for_deployment"
    DEPLOYED = "deployed"
    STOPPED = "stopped"
    HOLD = "hold"


class Outcome(str, Enum):
    """Outcome class of one external call (§10.1)."""
    OK = "OK"
    DEFINITE_FAIL = "DEFINITE_FAIL"
    AMBIGUOUS = "AMBIGUOUS"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAIL = "AUTH_FAIL"


class RowState(str, Enum):
    """Outbox row state (§3)."""
    PENDING = "pending"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    AMBIGUOUS = "ambiguous"
    HELD_AMBIGUOUS = "held_ambiguous"
    DONE = "done"
    CANCELLED_STALE = "cancelled_stale"
    FAILED = "failed"


class ApprovalState(str, Enum):
    """Durable approval lifecycle state (§8.4, §8.5)."""
    HELD = "held"
    RECORDED = "recorded"
    STAGED = "staged"
    RELEASED = "released"
    STARTED = "started"
    IMPLEMENTED = "implemented"
    TICKETED = "ticketed"
    PROMOTED = "promoted"
    REVOKED = "revoked"
    STALE = "stale"


@dataclass(frozen=True)
class TicketVerdict:
    """VALID(epoch) or INVALID(reason); str() is the ticket-check output line (§8.5)."""
    valid: bool
    reason: Optional[str] = None
    epoch: Optional[int] = None

    @classmethod
    def VALID(cls, epoch):
        return cls(True, None, epoch)

    @classmethod
    def INVALID(cls, reason):
        return cls(False, reason, None)

    def __str__(self):
        return f"VALID epoch={self.epoch}" if self.valid else f"INVALID {self.reason}"


@dataclass(frozen=True)
class TicketSnapshot:
    """Approval and task rows for one task read from state.sqlite3, plus the configured lease."""
    task: Optional[Mapping[str, Any]]
    approvals: Sequence[Mapping[str, Any]]
    lease_seconds: int


@dataclass(frozen=True)
class Item:
    """One observed ClickUp comment, reply or attachment (§3 ``items``).

    ``hash`` is the current content hash; ``material`` is set by the observer for a
    material-candidate input (§5.2) and makes ``observe_commit`` stamp ``gen``.
    """
    kind: str  # comment | reply | attachment
    id: str
    parent_id: Optional[str]
    author_id: Optional[int]
    date_ms: Optional[int]
    hash: str
    mentions_bot: bool = False
    shape: Optional[str] = None
    shape_sig: Optional[str] = None
    reply_count: int = 0
    deleted: bool = False
    material: bool = False


@dataclass(frozen=True)
class Batch:
    """All items observed for one task up to the dispatch barrier (§5.1)."""
    task_id: str
    items: Sequence[Item] = ()
    gen: int = 0


@dataclass(frozen=True)
class Preconditions:
    """State an effect requires; rechecked before every send and retry (§10.1)."""
    task_id: str
    list_id: Optional[str] = None
    phases: Sequence[str] = ()
    scope_gen: Optional[int] = None
    input_gen: Optional[int] = None
    published_rev: Optional[int] = None
    approval_id: Optional[int] = None
    approval_states: Sequence[str] = ()
    wait_run_id: Optional[int] = None


@dataclass(frozen=True)
class BlockerPreconditions(Preconditions):
    """Preconditions of a question relay row (blocker plan r9 §4.3): the frozen S0 ``Preconditions`` keep their
    exact fields and legacy 9-key JSON; only blocker rows persist the two extra keys."""
    blocker_id: Optional[int] = None
    question_card: Optional[str] = None


def load_preconditions(d):
    """Rebuild persisted preconditions: ``BlockerPreconditions`` when a blocker key is present, else the legacy
    ``Preconditions``."""
    if "blocker_id" in d or "question_card" in d:
        return BlockerPreconditions(**d)
    return Preconditions(**d)


@dataclass(frozen=True)
class EffectIntent:
    """One planned external effect, persisted to the outbox before the call (§3, §10)."""
    idem_key: str
    kind: str
    target: str
    payload: Mapping[str, Any]
    preconditions: Preconditions
    depends_on: Sequence[str] = ()


@dataclass(frozen=True)
class TransportResult:
    """``ClickUpTransport.request`` result."""
    outcome: Outcome
    status: Optional[int] = None
    json: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class KanbanResult:
    """``KanbanPort`` call result."""
    outcome: Outcome
    json: Any = None
