from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from framefactory.runtime import ExecutionState, ReviewDecision
from framefactory.worker.asset_pipeline import BatchProgress
from framefactory.worker.asset_service import AssetAnalysisService
from framefactory.worker.config import AssetAnalysisSettings


class FakeRepository:
    def __init__(self, targets: tuple[dict[str, object], ...]) -> None:
        self.targets = targets
        self.marked: list[tuple[str, ...]] = []

    def pending_resume_targets(self, *, limit: int = 25):
        return self.targets[:limit]

    def mark_resume_dispatched(self, batch_ids):
        self.marked.append(tuple(batch_ids))


class FakeStore:
    def __init__(self, status: ExecutionState) -> None:
        self.step = SimpleNamespace(status=status)

    def get_step(self, workspace_id: str, step_id: str):
        del workspace_id, step_id
        return self.step


class FakeScheduler:
    def __init__(self, status: ExecutionState, *, fail: bool = False) -> None:
        self.store = FakeStore(status)
        self.clock = SimpleNamespace(now=lambda: datetime.now(UTC))
        self.reviews: list[dict[str, object]] = []
        self.fail = fail

    def review(self, **kwargs):
        if self.fail:
            raise RuntimeError("simulated revision race")
        self.reviews.append(kwargs)
        self.store.step.status = ExecutionState.RETRYING
        return self.store.step


class FakeProcessor:
    def close(self) -> None:
        return None


class AssetAnalysisServiceTests(unittest.TestCase):
    def service(self, status: ExecutionState):
        repository = FakeRepository(({
            "workspace_id": "workspace",
            "run_id": "run",
            "step_id": "run:assets",
            "batch_ids": ["batch-1", "batch-2"],
        },))
        scheduler = FakeScheduler(status)
        service = AssetAnalysisService(
            settings=AssetAnalysisSettings(
                base_url="https://vision.example/v1", api_key="dummy", model="vision"
            ),
            repository=repository,  # type: ignore[arg-type]
            queue=SimpleNamespace(),  # type: ignore[arg-type]
            scheduler=scheduler,  # type: ignore[arg-type]
            processor=FakeProcessor(),  # type: ignore[arg-type]
            worker_id="worker",
            lease_seconds=30,
        )
        return service, repository, scheduler

    def test_settled_assets_retry_the_waiting_media_step_once(self) -> None:
        service, repository, scheduler = self.service(ExecutionState.AWAITING_REVIEW)

        self.assertEqual(1, service.resume_settled_runs())

        self.assertEqual(ReviewDecision.REQUEST_CHANGES, scheduler.reviews[0]["decision"])
        self.assertEqual([("batch-1", "batch-2")], repository.marked)

    def test_running_media_step_is_not_marked_dispatched_too_early(self) -> None:
        service, repository, scheduler = self.service(ExecutionState.RUNNING)

        self.assertEqual(0, service.resume_settled_runs())

        self.assertEqual([], scheduler.reviews)
        self.assertEqual([], repository.marked)

    def test_revision_race_keeps_resume_marker_for_periodic_reconciliation(self) -> None:
        service, repository, _scheduler = self.service(ExecutionState.AWAITING_REVIEW)
        service.scheduler = FakeScheduler(ExecutionState.AWAITING_REVIEW, fail=True)  # type: ignore[assignment]

        self.assertEqual(0, service.resume_settled_runs())

        self.assertEqual([], repository.marked)

    def test_retryable_pipeline_result_releases_queue_delivery(self) -> None:
        delivery = SimpleNamespace(
            message_id="delivery-1",
            payload={"workspace_id": "workspace", "asset_id": "asset", "job_id": "job"},
        )
        queue = SimpleNamespace(
            reserve_payload=lambda *_args, **_kwargs: delivery,
            renew_payload=lambda *_args, **_kwargs: None,
            ack_payload=lambda *_args, **_kwargs: self.fail("retry must not be acked"),
            release_payload=lambda item, available_at: setattr(queue, "released", (item, available_at)),
            released=None,
        )
        repository = SimpleNamespace(
            create_batch_for_asset=lambda *_args, **_kwargs: "batch-1",
            pending_resume_targets=lambda **_kwargs: (),
        )
        scheduler = FakeScheduler(ExecutionState.RUNNING)
        service = AssetAnalysisService(
            settings=AssetAnalysisSettings(
                base_url="https://vision.example/v1", api_key="dummy", model="vision"
            ),
            repository=repository,  # type: ignore[arg-type]
            queue=queue,  # type: ignore[arg-type]
            scheduler=scheduler,  # type: ignore[arg-type]
            processor=FakeProcessor(),  # type: ignore[arg-type]
            worker_id="worker",
            lease_seconds=30,
        )
        progress = BatchProgress(1, 1, 0, 0, 0, 0, 0)

        with patch(
            "framefactory.worker.asset_service.AssetAnalysisRunner.run_batch",
            return_value=progress,
        ):
            self.assertTrue(service.process_once())

        self.assertIsNotNone(queue.released)


if __name__ == "__main__":
    unittest.main()
