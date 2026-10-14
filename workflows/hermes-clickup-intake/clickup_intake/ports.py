"""Port interfaces (plan r5 §2.2). S1 implements ClickUpTransport, S3 implements KanbanPort."""

from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .contracts import KanbanResult, TransportResult


@runtime_checkable
class ClickUpTransport(Protocol):
    def request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None,
                body: Optional[Mapping[str, Any]] = None,
                multipart: Optional[Mapping[str, Any]] = None) -> TransportResult:
        """One HTTP call to api.clickup.com; ``body`` and ``multipart`` are mutually exclusive."""


@runtime_checkable
class KanbanPort(Protocol):
    """The installed ``hermes kanban --board <board>`` CLI; author/created-by is ``clickup-intake``."""

    def create(self, title: str, *, body_file: str, idempotency_key: str, assignee: Optional[str] = None,
               skills: Sequence[str] = (), initial_status: str = "blocked") -> KanbanResult: ...

    def comment(self, card_id: str, body: str) -> KanbanResult: ...

    def attach(self, card_id: str, path: str, name: str) -> KanbanResult: ...

    def attachments(self, card_id: str) -> KanbanResult: ...

    def show(self, card_id: str) -> KanbanResult: ...

    def unblock(self, card_id: str) -> KanbanResult: ...

    def complete(self, card_id: str, summary: str, metadata: Optional[Mapping[str, Any]] = None) -> KanbanResult: ...

    def archive(self, card_id: str) -> KanbanResult: ...

    def subscribe(self, card_id: str) -> KanbanResult: ...
