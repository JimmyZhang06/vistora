from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

QueueStatus = Literal[
    "queued",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "cancelled",
]


class QueueError(RuntimeError):
    """Base error raised by a durable queue adapter."""


class QueueConnectionError(QueueError):
    """Redis is unavailable or rejected a queue command."""


class JobNotFoundError(QueueError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"Queue job does not exist: {job_id}")
        self.job_id = job_id


class LeaseLostError(QueueError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"Queue lease is no longer owned by this worker: {job_id}")
        self.job_id = job_id


class IdempotencyConflictError(QueueError):
    def __init__(self, deduplication_key: str) -> None:
        super().__init__(
            "Queue deduplication key was already used with a different job: "
            f"{deduplication_key}"
        )
        self.deduplication_key = deduplication_key


@dataclass(frozen=True, slots=True)
class QueueJob:
    id: str
    queue_name: str
    payload: dict[str, Any]
    status: QueueStatus
    priority: int
    attempt_count: int
    max_attempts: int
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancellation_requested: bool = False


class JobQueue(Protocol):
    """Durable job queue boundary shared by API producers and workers."""

    async def enqueue(
        self,
        *,
        queue_name: str,
        payload: dict[str, Any],
        deduplication_key: str,
        priority: int = 0,
        max_attempts: int = 3,
        delay_seconds: float = 0,
    ) -> tuple[QueueJob, bool]: ...

    async def claim(
        self,
        *,
        queue_name: str,
        worker_id: str,
        lease_seconds: float,
    ) -> QueueJob | None: ...

    async def renew(
        self, *, job_id: str, lease_token: str, lease_seconds: float
    ) -> QueueJob: ...

    async def acknowledge(
        self,
        *,
        job_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
    ) -> QueueJob: ...

    async def retry(
        self,
        *,
        job_id: str,
        lease_token: str,
        error: dict[str, Any],
        delay_seconds: float | None = None,
    ) -> QueueJob: ...

    async def cancel(self, job_id: str) -> QueueJob: ...

    async def get(self, job_id: str) -> QueueJob: ...

    async def healthcheck(self) -> None: ...
