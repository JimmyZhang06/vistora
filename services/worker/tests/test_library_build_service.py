from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from framefactory.worker.adapters.database_assets import AssetAcquisitionResult
from framefactory.worker.library_build_service import (
    AssetBuildProgress,
    LibraryBuildJob,
    LibraryBuildService,
)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
JOB_ID = "22222222-2222-4222-8222-222222222222"
LIBRARY_ID = "33333333-3333-4333-8333-333333333333"
ASSET_ID = "44444444-4444-4444-8444-444444444444"


def job(*, status: str = "queued", stage: str = "discover", asset_ids=()):
    return LibraryBuildJob(
        workspace_id=WORKSPACE_ID,
        job_id=JOB_ID,
        library_id=LIBRARY_ID,
        status=status,
        stage=stage,
        spec={
            "topic": "Apollo 11",
            "queries": [],
            "sources": ["wikimedia"],
            "max_assets": 6,
            "copyright_status": "public_domain",
            "rights_confirmed": True,
        },
        progress={
            "asset_ids": list(asset_ids),
            "discovered": len(asset_ids),
            "transferred": len(asset_ids),
            "analyzed": 0,
            "indexed": 0,
            "failed": 0,
        },
        error=None,
        revision=1,
    )


class FakeQueue:
    def __init__(self) -> None:
        self.delivery = SimpleNamespace(
            message_id="queue-message",
            payload={
                "workspace_id": WORKSPACE_ID,
                "job_id": JOB_ID,
                "library_id": LIBRARY_ID,
                "spec": {},
            },
        )
        self.acked = 0
        self.released = 0

    def reserve_payload(self, *_args: Any):
        delivery, self.delivery = self.delivery, None
        return delivery

    def renew_payload(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def ack_payload(self, _delivery: Any) -> None:
        self.acked += 1

    def release_payload(self, _delivery: Any, _available_at: Any) -> None:
        self.released += 1


class FakeRepository:
    def __init__(
        self,
        current: LibraryBuildJob,
        *,
        asset_progress: AssetBuildProgress | None = None,
    ) -> None:
        self.current = current
        self.asset_progress = asset_progress or AssetBuildProgress(0, 0, 0, 0)
        self.saved_acquisitions = 0
        self.inspections = 0
        self.finished = 0
        self.failed = 0

    def load(self, workspace_id: str, job_id: str):
        self.assert_ids(workspace_id, job_id)
        return self.current

    def mark_transfer(self, current: LibraryBuildJob):
        self.current = replace(
            current, status="running", stage="transfer", revision=current.revision + 1
        )
        return self.current

    def save_acquisition(self, current: LibraryBuildJob, *, result, error):
        self.saved_acquisitions += 1
        self.current = replace(
            current,
            stage="analyze",
            progress={
                "asset_ids": list(result.asset_ids),
                "discovered": result.imported_count,
                "transferred": len(result.asset_ids),
                "analyzed": 0,
                "indexed": 0,
                "failed": 0,
            },
            error=error,
            revision=current.revision + 1,
        )
        return self.current

    def inspect_assets(self, workspace_id: str, asset_ids: tuple[str, ...]):
        self.assert_ids(workspace_id, JOB_ID)
        self.inspections += 1
        self.last_asset_ids = asset_ids
        return self.asset_progress

    def save_analysis_progress(self, current, progress):
        self.current = replace(current, revision=current.revision + 1)
        return self.current

    def finish(self, current, progress):
        self.finished += 1
        self.current = replace(
            current,
            status="completed" if not progress.failed else "completed_with_errors",
            stage="index",
            revision=current.revision + 1,
        )
        return self.current

    def fail(self, current, *, code, message, details):
        del code, message, details
        self.failed += 1
        self.current = replace(
            current, status="failed", revision=current.revision + 1
        )
        return self.current

    def assert_ids(self, workspace_id: str, job_id: str) -> None:
        if workspace_id != WORKSPACE_ID or job_id != JOB_ID:
            raise AssertionError((workspace_id, job_id))


class FakeAcquirer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def acquire(self, **request: Any) -> AssetAcquisitionResult:
        self.calls.append(request)
        return AssetAcquisitionResult(1, (), (), (ASSET_ID,))


class LibraryBuildServiceTests(unittest.TestCase):
    def service(self, repository, queue, acquirer):
        return LibraryBuildService(
            repository=repository,
            queue=queue,
            acquirer=acquirer,
            clock=SimpleNamespace(now=lambda: datetime.now(UTC)),
            worker_id="worker-test",
            lease_seconds=60,
        )

    def test_first_claim_acquires_once_persists_ids_and_releases(self) -> None:
        repository = FakeRepository(job())
        queue = FakeQueue()
        acquirer = FakeAcquirer()

        self.assertTrue(asyncio.run(self.service(repository, queue, acquirer).process_once()))

        self.assertEqual(1, len(acquirer.calls))
        self.assertEqual(("Apollo 11",) * 6, acquirer.calls[0]["queries"])
        self.assertIsNone(acquirer.calls[0]["run_id"])
        self.assertIsNone(acquirer.calls[0]["step_id"])
        self.assertEqual(JOB_ID, acquirer.calls[0]["idempotency_scope"])
        self.assertEqual((ASSET_ID,), tuple(repository.current.progress["asset_ids"]))
        self.assertEqual("analyze", repository.current.stage)
        self.assertEqual(1, queue.released)
        self.assertEqual(0, queue.acked)

    def test_analyze_claim_only_polls_frozen_asset_ids_and_finishes(self) -> None:
        repository = FakeRepository(
            job(status="running", stage="analyze", asset_ids=(ASSET_ID,)),
            asset_progress=AssetBuildProgress(1, 1, 0, 0),
        )
        queue = FakeQueue()
        acquirer = FakeAcquirer()

        self.assertTrue(asyncio.run(self.service(repository, queue, acquirer).process_once()))

        self.assertEqual([], acquirer.calls)
        self.assertEqual((ASSET_ID,), repository.last_asset_ids)
        self.assertEqual(1, repository.finished)
        self.assertEqual("completed", repository.current.status)
        self.assertEqual(1, queue.acked)

    def test_cancelled_job_is_acked_without_acquisition(self) -> None:
        repository = FakeRepository(job(status="cancelled"))
        queue = FakeQueue()
        acquirer = FakeAcquirer()

        self.assertTrue(asyncio.run(self.service(repository, queue, acquirer).process_once()))

        self.assertEqual([], acquirer.calls)
        self.assertEqual(1, queue.acked)

    def test_missing_control_api_fails_job_instead_of_fabricating_assets(self) -> None:
        repository = FakeRepository(job())
        queue = FakeQueue()

        self.assertTrue(asyncio.run(self.service(repository, queue, None).process_once()))

        self.assertEqual(1, repository.failed)
        self.assertEqual("failed", repository.current.status)
        self.assertEqual(1, queue.acked)


if __name__ == "__main__":
    unittest.main()
