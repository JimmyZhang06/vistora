from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from framefactory.ports import (
    DeliveryLeaseLostError,
    ObjectConflictError,
    QueueMessage,
    RevisionConflictError,
)
from framefactory.testing import (
    InMemoryObjectStore,
    InMemoryQueue,
    InMemoryRunStore,
    ManualClock,
)


@dataclass(frozen=True)
class _Lease:
    expires_at: datetime


@dataclass(frozen=True)
class _Step:
    workspace_id: str
    run_id: str
    step_id: str
    status: str = "queued"
    revision: int = 0
    available_at: datetime | None = None
    lease: _Lease | None = None


class PortFakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(datetime(2026, 8, 15, tzinfo=timezone.utc))

    def test_queue_redelivers_after_lease_expiry_and_rejects_stale_ack(self) -> None:
        queue = InMemoryQueue()
        message = queue.enqueue(
            QueueMessage("workspace", "run", "step"),
            self.clock.now(),
        )

        first = queue.reserve("run-steps", "worker-a", self.clock.now(), lease_seconds=10)
        self.assertIsNotNone(first)
        self.assertEqual(message.message_id, first.message.message_id)  # type: ignore[union-attr]
        self.clock.advance(seconds=10)
        second = queue.reserve("run-steps", "worker-b", self.clock.now())

        self.assertIsNotNone(second)
        self.assertEqual(2, second.delivery_count)  # type: ignore[union-attr]
        with self.assertRaises(DeliveryLeaseLostError):
            queue.ack(first)  # type: ignore[arg-type]
        queue.ack(second)  # type: ignore[arg-type]
        self.assertEqual(0, queue.pending_count())

    def test_queue_can_emit_a_true_duplicate_delivery(self) -> None:
        queue = InMemoryQueue()
        queue.enqueue(QueueMessage("workspace", "run", "step"), self.clock.now())
        first = queue.reserve("run-steps", "worker-a", self.clock.now())
        self.assertIsNotNone(first)
        queue.redeliver(first, self.clock.now())  # type: ignore[arg-type]

        duplicate = queue.reserve("run-steps", "worker-b", self.clock.now())

        self.assertIsNotNone(duplicate)
        self.assertEqual(first.message.message_id, duplicate.message.message_id)  # type: ignore[union-attr]

    def test_run_store_uses_cas_and_returns_detached_snapshots(self) -> None:
        store = InMemoryRunStore()
        created = store.save_step(_Step("workspace", "run", "step"), expected_revision=None)
        loaded = store.get_step("workspace", "step")

        self.assertEqual(1, created.revision)
        self.assertIsNot(created, loaded)
        updated = store.save_step(created, expected_revision=1)
        self.assertEqual(2, updated.revision)
        with self.assertRaises(RevisionConflictError):
            store.save_step(created, expected_revision=1)

    def test_recovery_query_includes_due_work_and_expired_leases_only(self) -> None:
        store = InMemoryRunStore()
        now = self.clock.now()
        records = (
            _Step("workspace", "run", "due", available_at=now),
            _Step("workspace", "run", "future", status="retrying", available_at=now + timedelta(seconds=1)),
            _Step("workspace", "run", "crashed", status="running", lease=_Lease(now)),
            _Step("workspace", "run", "healthy", status="running", lease=_Lease(now + timedelta(seconds=1))),
            _Step("workspace", "run", "review", status="awaiting_review"),
        )
        for record in records:
            store.save_step(record, expected_revision=None)

        recoverable = store.list_recoverable(now)

        self.assertEqual({"due", "crashed"}, {record.step_id for record in recoverable})

    def test_object_put_is_idempotent_but_never_overwrites(self) -> None:
        store = InMemoryObjectStore()
        first = store.put("workspaces/w/runs/r/artifacts/a/file.json", b"{}", "application/json")
        duplicate = store.put("workspaces/w/runs/r/artifacts/a/file.json", b"{}", "application/json")

        self.assertEqual(first, duplicate)
        self.assertEqual(b"{}", store.get(first.key))
        with self.assertRaises(ObjectConflictError):
            store.put(first.key, b"different", "application/json")


if __name__ == "__main__":
    unittest.main()
