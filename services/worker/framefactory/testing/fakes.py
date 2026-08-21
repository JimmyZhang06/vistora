"""Deterministic in-memory implementations of worker infrastructure ports."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from framefactory.ports import (
    DeliveryLeaseLostError,
    ObjectConflictError,
    QueueDelivery,
    QueueMessage,
    RevisionConflictError,
    StoredObject,
)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class ManualClock:
    """A manually advanced UTC clock; no fake ever sleeps."""

    def __init__(self, current: datetime | None = None) -> None:
        self._current = current or datetime(2026, 1, 1, tzinfo=timezone.utc)
        _require_aware(self._current, "current")

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> datetime:
        _require_aware(value, "value")
        if value < self._current:
            raise ValueError("manual clock cannot move backwards")
        self._current = value
        return self._current

    def advance(self, *, seconds: float = 0, milliseconds: float = 0) -> datetime:
        if seconds < 0 or milliseconds < 0:
            raise ValueError("manual clock cannot move backwards")
        self._current += timedelta(seconds=seconds, milliseconds=milliseconds)
        return self._current


@dataclass(slots=True)
class _QueueEntry:
    sequence: int
    message: QueueMessage
    available_at: datetime
    delivery_count: int = 0
    active_receipt: str | None = None
    lease_expires_at: datetime | None = None
    worker_id: str | None = None


class InMemoryQueue:
    """A deterministic at-least-once queue with visibility leases."""

    def __init__(self) -> None:
        self._entries: list[_QueueEntry] = []
        self._next_sequence = 1
        self._next_message = 1
        self._next_receipt = 1

    def enqueue(self, message: QueueMessage, available_at: datetime) -> QueueMessage:
        _require_aware(available_at, "available_at")
        if not message.message_id:
            message = replace(message, message_id=f"message-{self._next_message:06d}")
            self._next_message += 1
        self._entries.append(_QueueEntry(self._next_sequence, copy.deepcopy(message), available_at))
        self._next_sequence += 1
        return message

    def reserve(
        self,
        queue_name: str,
        worker_id: str,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> QueueDelivery | None:
        _require_aware(now, "now")
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._expire_leases(now)
        candidates = [
            item
            for item in self._entries
            if item.message.queue_name == queue_name
            and item.active_receipt is None
            and item.available_at <= now
        ]
        if not candidates:
            return None
        item = min(candidates, key=lambda entry: (entry.available_at, entry.sequence))
        item.delivery_count += 1
        item.active_receipt = f"receipt-{self._next_receipt:06d}"
        self._next_receipt += 1
        item.worker_id = worker_id
        item.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return QueueDelivery(
            message=copy.deepcopy(item.message),
            receipt=item.active_receipt,
            worker_id=worker_id,
            delivery_count=item.delivery_count,
            lease_expires_at=item.lease_expires_at,
        )

    def ack(self, delivery: QueueDelivery) -> None:
        item = self._leased_entry(delivery)
        self._entries.remove(item)

    def renew(
        self,
        delivery: QueueDelivery,
        now: datetime,
        lease_seconds: float = 30.0,
    ) -> None:
        _require_aware(now, "now")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._expire_leases(now)
        item = self._leased_entry(delivery)
        item.lease_expires_at = now + timedelta(seconds=lease_seconds)

    def release(self, delivery: QueueDelivery, available_at: datetime) -> None:
        _require_aware(available_at, "available_at")
        item = self._leased_entry(delivery)
        item.available_at = available_at
        item.active_receipt = None
        item.lease_expires_at = None
        item.worker_id = None

    def redeliver(self, delivery: QueueDelivery, available_at: datetime) -> None:
        """Simulate a broker duplicate while the original delivery still exists."""

        self.enqueue(delivery.message, available_at)

    def pending_count(self, queue_name: str | None = None) -> int:
        return sum(
            1 for item in self._entries if queue_name is None or item.message.queue_name == queue_name
        )

    def _expire_leases(self, now: datetime) -> None:
        for item in self._entries:
            if item.active_receipt and item.lease_expires_at and item.lease_expires_at <= now:
                item.active_receipt = None
                item.lease_expires_at = None
                item.worker_id = None

    def _leased_entry(self, delivery: QueueDelivery) -> _QueueEntry:
        for item in self._entries:
            if item.active_receipt == delivery.receipt and item.worker_id == delivery.worker_id:
                return item
        raise DeliveryLeaseLostError(f"delivery lease is no longer active: {delivery.receipt}")


def _record_key(record: Any, resource_name: str) -> tuple[str, str]:
    workspace_id = getattr(record, "workspace_id", None)
    resource_id = getattr(record, resource_name, None)
    if not workspace_id or not resource_id:
        raise ValueError(f"record must define workspace_id and {resource_name}")
    return str(workspace_id), str(resource_id)


def _saved_copy(record: Any, revision: int) -> Any:
    if is_dataclass(record):
        # Runtime records are frozen and recursively immutable.  ``replace``
        # provides a detached aggregate without asking ``deepcopy`` to mutate
        # FrozenMap internals during reconstruction.
        return replace(record, revision=revision)
    cloned = copy.deepcopy(record)
    cloned.revision = revision
    return cloned


def _detached_copy(record: Any) -> Any:
    return replace(record) if is_dataclass(record) else copy.deepcopy(record)


def _status(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).lower()


class InMemoryRunStore:
    """Snapshot store with PostgreSQL-like optimistic concurrency."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], Any] = {}
        self._steps: dict[tuple[str, str], Any] = {}
        self.events: list[dict[str, Any]] = []
        self._event_keys: set[tuple[str, str, str]] = set()

    def get_run(self, workspace_id: str, run_id: str) -> Any | None:
        value = self._runs.get((workspace_id, run_id))
        return None if value is None else _detached_copy(value)

    def save_run(self, run: Any, expected_revision: int | None) -> Any:
        key = _record_key(run, "run_id")
        previous = self._runs.get(key)
        saved = self._cas_save(self._runs, key, run, expected_revision)
        if previous is not None and _status(previous.status) != _status(saved.status):
            status = _status(saved.status)
            event_type = (
                "run.started"
                if status == "running" and _status(previous.status) == "queued"
                else "run.retrying"
                if status == "running"
                else f"run.{status}"
            )
            self.append_event(
                key[0],
                key[1],
                event_type=event_type,
                deduplication_key=f"worker-run-status:{saved.revision}:{status}",
                payload={"previous_status": _status(previous.status), "status": status},
                occurred_at=saved.updated_at,
            )
        return _detached_copy(saved)

    def get_step(self, workspace_id: str, step_id: str) -> Any | None:
        value = self._steps.get((workspace_id, step_id))
        return None if value is None else _detached_copy(value)

    def save_step(self, step: Any, expected_revision: int | None) -> Any:
        key = _record_key(step, "step_id")
        saved = self._cas_save(self._steps, key, step, expected_revision)
        status = _status(saved.status)
        attempt = int(getattr(saved, "attempt_count", 0))
        event_type = "step.started" if status == "running" else f"step.{status}"
        self.append_event(
            saved.workspace_id,
            saved.run_id,
            event_type=event_type,
            deduplication_key=(
                f"worker-step:{saved.step_id}:attempt:{attempt}:status:{status}"
            ),
            step_id=saved.step_id,
            payload={"status": status, "attempt": attempt},
            occurred_at=getattr(saved, "updated_at", None),
        )
        review = getattr(saved, "review", None)
        if (
            review is not None
            and review.decision is not None
            and review.decided_at == getattr(saved, "updated_at", None)
        ):
            self.append_event(
                saved.workspace_id,
                saved.run_id,
                event_type="review.recorded",
                deduplication_key=(
                    f"worker-review:{saved.step_id}:{attempt}:{review.decision.value}"
                ),
                actor_type="user",
                actor_id=review.actor_id,
                step_id=saved.step_id,
                payload={"decision": review.decision.value, "status": status},
                occurred_at=getattr(saved, "updated_at", None),
            )
        return _detached_copy(saved)

    def list_steps(self, workspace_id: str, run_id: str) -> tuple[Any, ...]:
        records = [
            _detached_copy(step)
            for (stored_workspace, _), step in self._steps.items()
            if stored_workspace == workspace_id and str(step.run_id) == run_id
        ]
        return tuple(sorted(records, key=lambda item: str(item.step_id)))

    def list_recoverable(self, now: datetime) -> tuple[Any, ...]:
        _require_aware(now, "now")
        recoverable: list[Any] = []
        for step in self._steps.values():
            status = _status(step.status)
            available_at = getattr(step, "available_at", None)
            lease = getattr(step, "lease", None)
            lease_expires_at = getattr(lease, "expires_at", None) if lease is not None else None
            if status in {"queued", "retrying"} and (available_at is None or available_at <= now) or status == "running" and lease_expires_at is not None and lease_expires_at <= now:
                recoverable.append(_detached_copy(step))
        return tuple(sorted(recoverable, key=lambda item: (str(item.run_id), str(item.step_id))))

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
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        key = (workspace_id, run_id, deduplication_key)
        if key in self._event_keys:
            return
        self._event_keys.add(key)
        self.events.append(
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "step_id": step_id,
                "sequence": 1 + sum(
                    event["run_id"] == run_id for event in self.events
                ),
                "type": event_type,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "payload": payload or {},
                "occurred_at": occurred_at,
                "deduplication_key": deduplication_key,
            }
        )

    @staticmethod
    def _cas_save(
        records: dict[tuple[str, str], Any],
        key: tuple[str, str],
        record: Any,
        expected_revision: int | None,
    ) -> Any:
        current = records.get(key)
        if current is None:
            if expected_revision is not None:
                raise RevisionConflictError(
                    f"cannot create {key!r} with expected revision {expected_revision}"
                )
            next_revision = 1
        else:
            actual_revision = int(current.revision)
            if expected_revision != actual_revision:
                raise RevisionConflictError(
                    f"stale revision for {key!r}: expected {expected_revision}, actual {actual_revision}"
                )
            next_revision = actual_revision + 1
        saved = _saved_copy(record, next_revision)
        records[key] = saved
        return saved


class InMemoryObjectStore:
    """Immutable object fake with content-addressed idempotency semantics."""

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}

    def put(self, key: str, data: bytes, media_type: str) -> StoredObject:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("key must be a relative canonical object key")
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if not media_type:
            raise ValueError("media_type must not be empty")
        candidate = StoredObject(
            key=key,
            data=bytes(data),
            media_type=media_type,
            content_hash=hashlib.sha256(data).hexdigest(),
            byte_size=len(data),
        )
        existing = self._objects.get(key)
        if existing is not None:
            if existing == candidate:
                return copy.deepcopy(existing)
            raise ObjectConflictError(f"immutable object key already exists: {key}")
        self._objects[key] = candidate
        return copy.deepcopy(candidate)

    def get(self, key: str) -> bytes:
        return bytes(self._objects[key].data)

    def head(self, key: str) -> StoredObject | None:
        value = self._objects.get(key)
        return copy.deepcopy(value)

    def exists(self, key: str) -> bool:
        return key in self._objects
