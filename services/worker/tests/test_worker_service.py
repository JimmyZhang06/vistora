from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from framefactory.runtime import CapabilityUnavailable
from framefactory.steps import StepContext
from framefactory.worker.capabilities import unavailable_capabilities
from framefactory.worker.config import WorkerSettings
from framefactory.worker.service import WorkerService, build_service


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


class BlockingAssetAnalysis:
    def __init__(self, *, handled: bool) -> None:
        self.handled = handled
        self.started = threading.Event()
        self.release = threading.Event()

    def process_once(self) -> bool:
        self.started.set()
        if not self.release.wait(timeout=1):
            raise AssertionError("asset analysis blocked the asyncio event loop")
        return self.handled


class WorkerServiceRecoveryTests(unittest.TestCase):
    def test_build_service_always_wires_library_consumer_fail_closed(self) -> None:
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
        )
        store = SimpleNamespace(close=lambda: None)
        queue = SimpleNamespace(close=lambda: None)

        with (
            patch(
                "framefactory.worker.service.PostgresRunStore.connect",
                return_value=store,
            ),
            patch(
                "framefactory.worker.service.RedisQueue.connect", return_value=queue
            ),
        ):
            service = build_service(settings)

        self.assertIsNotNone(service.library_build)
        self.assertIsNone(service.library_build.acquirer)

    def test_blocking_asset_analysis_keeps_event_loop_responsive(self) -> None:
        analysis = BlockingAssetAnalysis(handled=True)
        runtime = SimpleNamespace(process_one=AsyncMock(return_value=False))
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
        )
        service = WorkerService(
            settings=settings,
            store=SimpleNamespace(),  # type: ignore[arg-type]
            queue=SimpleNamespace(),  # type: ignore[arg-type]
            scheduler=Scheduler(),  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            asset_analysis=analysis,  # type: ignore[arg-type]
        )

        async def exercise() -> bool:
            process = asyncio.create_task(service.process_once())
            while not analysis.started.is_set():
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(process.done())
            analysis.release.set()
            return await asyncio.wait_for(process, timeout=1)

        self.assertTrue(asyncio.run(exercise()))
        runtime.process_one.assert_not_awaited()

    def test_unhandled_asset_analysis_preserves_runtime_fallback(self) -> None:
        analysis = BlockingAssetAnalysis(handled=False)
        analysis.release.set()
        runtime = SimpleNamespace(process_one=AsyncMock(return_value=True))
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
        )
        service = WorkerService(
            settings=settings,
            store=SimpleNamespace(),  # type: ignore[arg-type]
            queue=SimpleNamespace(),  # type: ignore[arg-type]
            scheduler=Scheduler(),  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            asset_analysis=analysis,  # type: ignore[arg-type]
        )

        with patch.object(service, "_initialize_one", return_value=False):
            self.assertTrue(asyncio.run(service.process_once()))
        runtime.process_one.assert_awaited_once_with(
            queue_name=settings.queue_name,
            worker_id=settings.worker_id,
        )

    def test_library_build_is_polled_only_after_run_work_is_empty(self) -> None:
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
        )
        library_build = SimpleNamespace(process_once=AsyncMock(return_value=True))
        runtime = SimpleNamespace(process_one=AsyncMock(return_value=True))
        service = WorkerService(
            settings=settings,
            store=SimpleNamespace(),  # type: ignore[arg-type]
            queue=SimpleNamespace(),  # type: ignore[arg-type]
            scheduler=Scheduler(),  # type: ignore[arg-type]
            runtime=runtime,  # type: ignore[arg-type]
            library_build=library_build,  # type: ignore[arg-type]
        )

        with patch.object(service, "_initialize_one", return_value=False):
            self.assertTrue(asyncio.run(service.process_once()))
        library_build.process_once.assert_not_awaited()

        runtime.process_one = AsyncMock(return_value=False)
        with patch.object(service, "_initialize_one", return_value=False):
            self.assertTrue(asyncio.run(service.process_once()))
        library_build.process_once.assert_awaited_once()

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
