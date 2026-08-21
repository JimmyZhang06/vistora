"""Persistence boundaries for run state and immutable artifacts.

``RunStore`` is intentionally expressed as aggregate snapshot operations with
optimistic revisions.  PostgreSQL adapters can implement each save with an
``UPDATE ... WHERE revision = expected_revision`` transaction. ``ObjectStore``
maps directly to immutable S3-style keys and conditional puts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from framefactory.runtime.model import RunRecord, StepRecord


class RunStore(Protocol):
    """Durable run/step snapshots with compare-and-swap writes."""

    def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        ...

    def save_run(self, run: RunRecord, expected_revision: int | None) -> RunRecord:
        ...

    def get_step(self, workspace_id: str, step_id: str) -> StepRecord | None:
        ...

    def save_step(self, step: StepRecord, expected_revision: int | None) -> StepRecord:
        ...

    def list_steps(self, workspace_id: str, run_id: str) -> Sequence[StepRecord]:
        ...

    def list_recoverable(self, now: datetime) -> Sequence[StepRecord]:
        """Return due queued/retrying steps and running steps with expired leases."""

    def append_event(
        self,
        workspace_id: str,
        run_id: str,
        *,
        event_type: str,
        deduplication_key: str,
        actor_type: str = "worker",
        actor_id: str | None = None,
        step_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Append a sequenced event once for a stable deduplication key."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    data: bytes
    media_type: str
    content_hash: str
    byte_size: int


class ObjectStore(Protocol):
    """Immutable binary object storage suitable for an S3 adapter."""

    def put(self, key: str, data: bytes, media_type: str) -> StoredObject:
        """Conditionally create an object; identical repeated puts are idempotent."""

    def get(self, key: str) -> bytes:
        ...

    def head(self, key: str) -> StoredObject | None:
        ...

    def exists(self, key: str) -> bool:
        ...
