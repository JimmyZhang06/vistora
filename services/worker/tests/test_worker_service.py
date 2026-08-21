from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from framefactory.runtime import CapabilityUnavailable
from framefactory.steps import StepContext
from framefactory.worker.capabilities import unavailable_capabilities
from framefactory.worker.config import WorkerSettings
from framefactory.worker.service import WorkerService


class Store:
    def __init__(self, pending) -> None:
        self.pending = pending
        self.errors: list[tuple[str, str, str]] = []

    def load_pending_graphs(self, *, limit: int):
        return self.pending

    def record_initialization_error(self, workspace: str, run: str, error: str) -> None:
        self.errors.append((workspace, run, error))


class Scheduler:
    def __init__(self) -> None:
        self.clock = SimpleNamespace(now=lambda: datetime.now(UTC))
        self.created: list[tuple[str, str]] = []
        self.recoveries = 0

    def recover(self) -> int:
        self.recoveries += 1
        return 0

    def create_run(self, *, workspace_id: str, run_id: str, input_snapshot, steps):
        self.created.append((workspace_id, run_id))


class WorkerServiceRecoveryTests(unittest.TestCase):
    def test_periodic_database_sweep_closes_api_redis_publish_gap(self) -> None:
        pending = SimpleNamespace(
            workspace_id="workspace",
            run_id="run",
            input_snapshot={"topic": "recovery"},
            nodes=(object(),),
        )
        store = Store((pending,))
        scheduler = Scheduler()
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
        )
        service = WorkerService(
            settings=settings,
            store=store,  # type: ignore[arg-type]
            queue=SimpleNamespace(),  # type: ignore[arg-type]
            scheduler=scheduler,  # type: ignore[arg-type]
            runtime=SimpleNamespace(),  # type: ignore[arg-type]
        )
        service._recover_and_initialize()
        self.assertEqual(1, scheduler.recoveries)
        self.assertEqual([("workspace", "run")], scheduler.created)

    def test_unconfigured_production_capability_fails_without_fake_artifact(self) -> None:
        capability = unavailable_capabilities().resolve("render.compose")
        with self.assertRaisesRegex(CapabilityUnavailable, "no configured production provider"):
            asyncio.run(
                capability.execute(  # type: ignore[union-attr]
                    StepContext(
                        workspace_id="workspace",
                        run_id="run",
                        step_id="run:render",
                        input_snapshot={},
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
