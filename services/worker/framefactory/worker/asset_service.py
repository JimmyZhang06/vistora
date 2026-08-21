"""Queue bridge for durable downloaded-asset analysis and Run resumption."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import Any

from framefactory.ports import DeliveryLeaseLostError
from framefactory.runtime import ExecutionState, ReviewDecision, Scheduler

from .adapters.asset_analysis import LocalAssetStageProcessor
from .adapters.postgres_asset_jobs import PostgresAssetJobRepository
from .adapters.redis_queue import RedisQueue
from .asset_pipeline import AssetAnalysisRunner
from .config import AssetAnalysisSettings

logger = logging.getLogger("framefactory.worker.asset-analysis")


class _PayloadLeaseHeartbeat:
    """Keep Redis ownership alive while scanning, extracting and tagging media."""

    def __init__(self, queue: RedisQueue, delivery: Any, lease_seconds: float) -> None:
        self.queue = queue
        self.delivery = delivery
        self.lease_seconds = max(lease_seconds, 30.0)
        self.interval_seconds = max(1.0, min(self.lease_seconds / 3, 30.0))
        self._stop = Event()
        self._thread = Thread(target=self._run, name="asset-analysis-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.queue.renew_payload(
                    self.delivery,
                    datetime.now(UTC),
                    lease_seconds=self.lease_seconds,
                )
            except DeliveryLeaseLostError:
                logger.warning(
                    "asset analysis queue lease was lost id=%s",
                    self.delivery.message_id,
                )
                return
            except Exception:
                logger.exception(
                    "asset analysis queue heartbeat failed id=%s",
                    self.delivery.message_id,
                )


class AssetAnalysisService:
    def __init__(
        self,
        *,
        settings: AssetAnalysisSettings,
        repository: PostgresAssetJobRepository,
        queue: RedisQueue,
        scheduler: Scheduler,
        processor: LocalAssetStageProcessor,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.queue = queue
        self.scheduler = scheduler
        self.processor = processor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def close(self) -> None:
        self.processor.close()

    def process_once(self) -> bool:
        # The heartbeat keeps long FFmpeg/ASR stages owned while the process is
        # healthy. A two-hour base lease made a crashed worker strand automatic
        # acquisition after restart, so cap crash recovery to ten minutes and
        # rely on renewal for normal long-running analysis.
        analysis_lease_seconds = max(self.lease_seconds, 600.0)
        delivery = self.queue.reserve_payload(
            self.settings.queue_name,
            self.worker_id,
            self.scheduler.clock.now(),
            lease_seconds=analysis_lease_seconds,
        )
        if delivery is None:
            return False
        heartbeat = _PayloadLeaseHeartbeat(
            self.queue, delivery, analysis_lease_seconds
        )
        heartbeat.start()
        queue_action = "ack"
        try:
            payload = self._payload(delivery.payload)
            batch_id = self.repository.create_batch_for_asset(
                payload["workspace_id"],
                payload["asset_id"],
                external_job_id=payload["job_id"],
                run_id=payload.get("run_id"),
                step_id=payload.get("step_id"),
                acquisition_id=payload.get("acquisition_id"),
                acquisition_count=int(payload.get("acquisition_count") or 1),
                auto_ready=self.settings.auto_ready,
                rate_limit_per_minute=self.settings.rate_limit_per_minute,
            )
            runner = AssetAnalysisRunner(
                repository=self.repository,
                processor=self.processor,
                worker_id=self.worker_id,
                lease_seconds=max(self.lease_seconds, 300),
                rate_limit_per_minute=self.settings.rate_limit_per_minute,
            )
            progress = runner.run_batch(batch_id, maximum_items=1)
            if progress.pending:
                # A retryable stage failure is durable in PostgreSQL. Keep the
                # Redis command retryable too so the batch is reclaimed after
                # its available_at backoff instead of becoming stranded.
                queue_action = "release"
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("invalid asset analysis job id=%s error=%s", delivery.message_id, exc)
        except Exception:
            logger.exception("asset analysis job failed id=%s", delivery.message_id)
            queue_action = "release"
        finally:
            heartbeat.stop()
        try:
            if queue_action == "ack":
                self.queue.ack_payload(delivery)
            else:
                self.queue.release_payload(
                    delivery,
                    self.scheduler.clock.now() + timedelta(seconds=10),
                )
        except DeliveryLeaseLostError:
            # The durable analysis batch is idempotent. A redelivery can safely
            # reconcile the already-completed state after ownership was lost.
            logger.warning(
                "asset analysis result finished after queue lease loss id=%s action=%s",
                delivery.message_id,
                queue_action,
            )
        self.resume_settled_runs()
        return True

    def resume_settled_runs(self) -> int:
        resumed = 0
        for target in self.repository.pending_resume_targets(limit=25):
            try:
                step = self.scheduler.store.get_step(
                    target["workspace_id"], target["step_id"]
                )
                if step is None:
                    self.repository.mark_resume_dispatched(target["batch_ids"])
                    continue
                if step.status is ExecutionState.AWAITING_REVIEW:
                    self.scheduler.review(
                        workspace_id=target["workspace_id"],
                        step_id=target["step_id"],
                        decision=ReviewDecision.REQUEST_CHANGES,
                        actor_id="system:asset-analysis",
                        comment="自动补充素材已完成安全分析与打标，系统正在重新匹配素材",
                    )
                    self.repository.mark_resume_dispatched(target["batch_ids"])
                    resumed += 1
                    continue
                # A running step may be racing with this reconciliation pass;
                # retain the marker so the next periodic sweep can observe its
                # settled state. Other terminal/non-waiting states no longer
                # need this acquisition group.
                if step.status is not ExecutionState.RUNNING:
                    self.repository.mark_resume_dispatched(target["batch_ids"])
            except Exception:
                # One stale/CAS-conflicting Run must not prevent other settled
                # acquisition groups from being resumed during the same sweep.
                logger.exception(
                    "automatic asset resume failed workspace=%s run=%s step=%s",
                    target.get("workspace_id"),
                    target.get("run_id"),
                    target.get("step_id"),
                )
        return resumed

    @staticmethod
    def _payload(payload: dict[str, Any]) -> dict[str, str | None]:
        required = {
            key: str(payload.get(key) or "").strip()
            for key in ("workspace_id", "asset_id", "job_id")
        }
        if not all(required.values()):
            raise ValueError("asset analysis payload is missing identifiers")
        return {
            **required,
            "run_id": str(payload.get("run_id") or "").strip() or None,
            "step_id": str(payload.get("step_id") or "").strip() or None,
            "acquisition_id": str(payload.get("acquisition_id") or "").strip() or None,
            "acquisition_count": str(payload.get("acquisition_count") or "1").strip(),
        }
