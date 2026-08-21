"""Long-running worker lifecycle and API-run intake bridge."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from framefactory.ports import DeliveryLeaseLostError
from framefactory.runtime import ReviewDecision, Scheduler, WorkerRuntime

from .adapters import PostgresRunStore, RedisQueue
from .adapters.asset_analysis import (
    LocalAssetStageProcessor,
    OpenAICompatibleAudioTranscriber,
    OpenAICompatibleVisionAnalyzer,
    S3MediaObjectStore,
    SystemMalwareScanner,
)
from .adapters.postgres_asset_jobs import PostgresAssetJobRepository
from .adapters.s3_artifacts import S3ArtifactStorage
from .asset_service import AssetAnalysisService
from .capabilities import configured_capabilities, production_step_registry
from .clock import SystemClock
from .config import WorkerSettings

logger = logging.getLogger("framefactory.worker")


class WorkerService:
    def __init__(
        self,
        *,
        settings: WorkerSettings,
        store: PostgresRunStore,
        queue: RedisQueue,
        scheduler: Scheduler,
        runtime: WorkerRuntime,
        artifact_storage: S3ArtifactStorage | None = None,
        asset_analysis: AssetAnalysisService | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.queue = queue
        self.scheduler = scheduler
        self.runtime = runtime
        self.artifact_storage = artifact_storage
        self.asset_analysis = asset_analysis
        self._stop = asyncio.Event()

    def healthcheck(self) -> None:
        self.store.healthcheck()
        self.queue.healthcheck()
        if self.artifact_storage is not None:
            self.artifact_storage.healthcheck()

    def stop(self) -> None:
        self._stop.set()

    def cancel_run(self, workspace_id: str, run_id: str):
        """Public control boundary; persists cancellation for running checkpoints."""

        return self.scheduler.cancel_run(workspace_id, run_id)

    def review_step(
        self,
        *,
        workspace_id: str,
        step_id: str,
        decision: ReviewDecision,
        actor_id: str,
        comment: str | None = None,
    ):
        """Public control boundary; persists and resumes an audited review gate."""

        return self.scheduler.review(
            workspace_id=workspace_id,
            step_id=step_id,
            decision=decision,
            actor_id=actor_id,
            comment=comment,
        )

    def close(self) -> None:
        if self.asset_analysis is not None:
            self.asset_analysis.close()
        self.queue.close()
        self.store.close()

    async def process_once(self) -> bool:
        """Consume one API run request or one durable step."""

        if self.asset_analysis is not None and self.asset_analysis.process_once():
            return True
        if self._initialize_one():
            return True
        return await self.runtime.process_one(
            queue_name=self.settings.queue_name,
            worker_id=self.settings.worker_id,
        )

    async def run_forever(self) -> None:
        self.healthcheck()
        self._recover_and_initialize()
        next_recovery = self.scheduler.clock.now() + timedelta(
            seconds=self.settings.recovery_interval_seconds
        )
        logger.info(
            "worker ready id=%s intake=%s steps=%s",
            self.settings.worker_id,
            self.settings.intake_queue_name,
            self.settings.queue_name,
        )
        while not self._stop.is_set():
            now = self.scheduler.clock.now()
            if now >= next_recovery:
                try:
                    self._recover_and_initialize()
                    if self.asset_analysis is not None:
                        self.asset_analysis.resume_settled_runs()
                except Exception:
                    logger.exception("periodic durable recovery failed")
                next_recovery = now + timedelta(
                    seconds=self.settings.recovery_interval_seconds
                )
            try:
                handled = await self.process_once()
            except Exception:
                logger.exception("worker polling iteration failed")
                handled = False
            if not handled:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    def _initialize_one(self) -> bool:
        delivery = self.queue.reserve(
            self.settings.intake_queue_name,
            self.settings.worker_id,
            self.scheduler.clock.now(),
            lease_seconds=self.settings.lease_seconds,
        )
        if delivery is None:
            return False
        message = delivery.message
        try:
            pending = next(
                (
                    item
                    for item in self.store.load_pending_graphs(limit=100)
                    if item.workspace_id == message.workspace_id and item.run_id == message.run_id
                ),
                None,
            )
            if pending is not None:
                self.scheduler.create_run(
                    workspace_id=pending.workspace_id,
                    run_id=pending.run_id,
                    input_snapshot=pending.input_snapshot,
                    steps=pending.nodes,
                )
            # A missing run or an already initialized run makes this delivery stale/idempotent.
            self.queue.ack(delivery)
        except (KeyError, TypeError, ValueError, LookupError) as exc:
            logger.error(
                "run graph initialization failed workspace=%s run=%s error=%s",
                message.workspace_id,
                message.run_id,
                exc,
            )
            self.store.record_initialization_error(
                message.workspace_id, message.run_id, str(exc) or exc.__class__.__name__
            )
            try:
                self.queue.ack(delivery)
            except DeliveryLeaseLostError:
                pass
        except Exception:
            logger.exception(
                "transient run initialization failure workspace=%s run=%s",
                message.workspace_id,
                message.run_id,
            )
            try:
                self.queue.release(
                    delivery,
                    self.scheduler.clock.now()
                    + timedelta(seconds=self.settings.poll_interval_seconds),
                )
            except DeliveryLeaseLostError:
                pass
        return True

    def _recover_and_initialize(self) -> None:
        recovered = self.scheduler.recover()
        if recovered:
            logger.info("recovered %s durable worker records", recovered)
        # Redis intake is authoritative for normal operation. This sweep closes
        # the DB-commit/Redis-publish crash window without fabricating results.
        for pending in self.store.load_pending_graphs(limit=100):
            try:
                self.scheduler.create_run(
                    workspace_id=pending.workspace_id,
                    run_id=pending.run_id,
                    input_snapshot=pending.input_snapshot,
                    steps=pending.nodes,
                )
            except (KeyError, TypeError, ValueError, LookupError) as exc:
                self.store.record_initialization_error(
                    pending.workspace_id,
                    pending.run_id,
                    str(exc) or exc.__class__.__name__,
                )


def build_service(settings: WorkerSettings) -> WorkerService:
    store = PostgresRunStore.connect(
        settings.database_url, timeout_seconds=settings.connect_timeout_seconds
    )
    try:
        queue = RedisQueue.connect(
            settings.redis_url,
            namespace=settings.redis_namespace,
            timeout_seconds=settings.connect_timeout_seconds,
        )
    except Exception:
        store.close()
        raise
    try:
        clock = SystemClock()
        scheduler = Scheduler(store=store, queue=queue, clock=clock)
        artifact_storage = (
            S3ArtifactStorage(settings.object_storage, recorder=store)
            if settings.object_storage is not None
            else None
        )
        runtime = WorkerRuntime(
            scheduler=scheduler,
            steps=production_step_registry(),
            capabilities=configured_capabilities(
                settings,
                artifact_storage=artifact_storage,
            ),
            lease_seconds=settings.lease_seconds,
        )
        asset_analysis = None
        if settings.asset_analysis is not None:
            if artifact_storage is None:
                raise ValueError("asset analysis requires durable object storage")
            analysis_settings = settings.asset_analysis
            asset_analysis = AssetAnalysisService(
                settings=analysis_settings,
                repository=PostgresAssetJobRepository(settings.database_url),
                queue=queue,
                scheduler=scheduler,
                processor=LocalAssetStageProcessor(
                    S3MediaObjectStore(artifact_storage.client),
                    SystemMalwareScanner(),
                    OpenAICompatibleVisionAnalyzer(
                        base_url=analysis_settings.base_url,
                        api_key=analysis_settings.api_key,
                        model=analysis_settings.model,
                        timeout_seconds=analysis_settings.timeout_seconds,
                    ),
                    (
                        OpenAICompatibleAudioTranscriber(
                            base_url=analysis_settings.asr_base_url,
                            api_key=analysis_settings.asr_api_key,
                            model=analysis_settings.asr_model,
                            timeout_seconds=analysis_settings.asr_timeout_seconds,
                            response_format=analysis_settings.asr_response_format,
                            timestamp_mode=analysis_settings.asr_timestamp_mode,
                        )
                        if analysis_settings.asr_base_url
                        and analysis_settings.asr_model
                        else None
                    ),
                    ffprobe_command=(
                        settings.legacy_media.ffprobe_command
                        if settings.legacy_media is not None
                        else "ffprobe"
                    ),
                    ffmpeg_command=(
                        settings.legacy_media.ffmpeg_command
                        if settings.legacy_media is not None
                        else "ffmpeg"
                    ),
                ),
                worker_id=settings.worker_id,
                lease_seconds=settings.lease_seconds,
            )
        return WorkerService(
            settings=settings,
            store=store,
            queue=queue,
            scheduler=scheduler,
            runtime=runtime,
            artifact_storage=artifact_storage,
            asset_analysis=asset_analysis,
        )
    except Exception:
        queue.close()
        store.close()
        raise
