"""At-least-once queue boundary.

A production adapter can map this protocol to a Redis stream or another
visibility-timeout queue.  Queue messages only contain resource identifiers;
the immutable input snapshot remains in :class:`RunStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class QueueMessage:
    workspace_id: str
    run_id: str
    step_id: str
    queue_name: str = "run-steps"
    message_id: str = ""
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class QueueDelivery:
    message: QueueMessage
    receipt: str
    worker_id: str
    delivery_count: int
    lease_expires_at: datetime


class Queue(Protocol):
    """An at-least-once queue with leased deliveries."""

    def enqueue(self, message: QueueMessage, available_at: datetime) -> QueueMessage:
        """Publish a message, returning it with any generated identifier."""

    def reserve(
        self,
        queue_name: str,
        worker_id: str,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> QueueDelivery | None:
        """Lease the oldest available message, or return ``None``."""

    def ack(self, delivery: QueueDelivery) -> None:
        """Remove a successfully handled delivery."""

    def renew(
        self,
        delivery: QueueDelivery,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> None:
        """Extend an active delivery lease while work is still progressing."""

    def release(self, delivery: QueueDelivery, available_at: datetime) -> None:
        """Return a delivery to the queue, optionally after a delay."""
