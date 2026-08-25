"""Low-priority durable coordinator for one-click topic library builds."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from framefactory.ports import DeliveryLeaseLostError
from framefactory.runtime import PermanentStepError, RetryableStepError

from .adapters.database_assets import AssetAcquirer, AssetAcquisitionResult
from .adapters.redis_queue import RedisQueue
from .asset_service import _PayloadLeaseHeartbeat

logger = logging.getLogger("framefactory.worker.library-build")

LIBRARY_BUILD_QUEUE = "library-build"
_TERMINAL_STATUSES = frozenset(
    {"completed", "completed_with_errors", "failed", "cancelled"}
)


@dataclass(frozen=True, slots=True)
class LibraryBuildJob:
    workspace_id: str
    job_id: str
    library_id: str
    status: str
    stage: str
    spec: Mapping[str, Any]
    progress: Mapping[str, Any]
    error: Mapping[str, Any] | None
    revision: int


@dataclass(frozen=True, slots=True)
class AssetBuildProgress:
    analyzed: int
    indexed: int
    failed: int
    waiting: int
    missing_asset_ids: tuple[str, ...] = ()
    unusable_asset_ids: tuple[str, ...] = ()


class LibraryBuildRepository(Protocol):
    def load(self, workspace_id: str, job_id: str) -> LibraryBuildJob | None: ...

    def mark_transfer(self, job: LibraryBuildJob) -> LibraryBuildJob | None: ...

    def save_acquisition(
        self,
        job: LibraryBuildJob,
        *,
        result: AssetAcquisitionResult,
        error: Mapping[str, Any] | None,
    ) -> LibraryBuildJob | None: ...

    def inspect_assets(
        self, workspace_id: str, asset_ids: tuple[str, ...]
    ) -> AssetBuildProgress: ...

    def save_analysis_progress(
        self, job: LibraryBuildJob, progress: AssetBuildProgress
    ) -> LibraryBuildJob | None: ...

    def finish(
        self, job: LibraryBuildJob, progress: AssetBuildProgress
    ) -> LibraryBuildJob | None: ...

    def fail(
        self, job: LibraryBuildJob, *, code: str, message: str, details: Mapping[str, Any]
    ) -> LibraryBuildJob | None: ...


class PostgresLibraryBuildRepository:
    """Small CAS repository; Redis ownership is never treated as durable state."""

    _COLUMNS = """workspace_id,id,library_id,status,stage,spec,progress,error,revision"""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def load(self, workspace_id: str, job_id: str) -> LibraryBuildJob | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT {self._COLUMNS} FROM library_build_jobs
                      WHERE workspace_id=%s AND id=%s""",
                (workspace_id, job_id),
            ).fetchone()
        return _job(row) if row is not None else None

    def mark_transfer(self, job: LibraryBuildJob) -> LibraryBuildJob | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""UPDATE library_build_jobs
                       SET status='running',stage='transfer',
                           started_at=COALESCE(started_at,now()),updated_at=now(),
                           revision=revision+1
                     WHERE workspace_id=%s AND id=%s AND revision=%s
                       AND status IN ('queued','running')
                       AND stage IN ('discover','transfer')
                     RETURNING {self._COLUMNS}""",
                (job.workspace_id, job.job_id, job.revision),
            ).fetchone()
        return _job(row) if row is not None else None

    def save_acquisition(
        self,
        job: LibraryBuildJob,
        *,
        result: AssetAcquisitionResult,
        error: Mapping[str, Any] | None,
    ) -> LibraryBuildJob | None:
        asset_ids = tuple(dict.fromkeys(result.asset_ids))[:6]
        progress = {
            "asset_ids": list(asset_ids),
            "discovered": max(result.imported_count, len(asset_ids)),
            "transferred": len(asset_ids),
            "analyzed": 0,
            "indexed": 0,
            "failed": 0,
        }
        with self._connect() as connection:
            row = connection.execute(
                f"""UPDATE library_build_jobs
                       SET status='running',stage='analyze',progress=%s::jsonb,
                           error=%s::jsonb,updated_at=now(),revision=revision+1
                     WHERE workspace_id=%s AND id=%s AND revision=%s
                       AND status='running' AND stage='transfer'
                     RETURNING {self._COLUMNS}""",
                (
                    json.dumps(progress, ensure_ascii=False),
                    json.dumps(dict(error), ensure_ascii=False) if error else None,
                    job.workspace_id,
                    job.job_id,
                    job.revision,
                ),
            ).fetchone()
        return _job(row) if row is not None else None

    def inspect_assets(
        self, workspace_id: str, asset_ids: tuple[str, ...]
    ) -> AssetBuildProgress:
        if not asset_ids:
            return AssetBuildProgress(0, 0, 0, 0)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id,status,analysis_status FROM assets
                     WHERE workspace_id=%s AND id=ANY(%s::uuid[])""",
                (workspace_id, list(asset_ids)),
            ).fetchall()
        states = {str(row["id"]): row for row in rows}
        missing = tuple(asset_id for asset_id in asset_ids if asset_id not in states)
        analyzed = 0
        indexed = 0
        failed = len(missing)
        waiting = 0
        unusable: list[str] = []
        for asset_id in asset_ids:
            row = states.get(asset_id)
            if row is None:
                continue
            analysis_status = str(row["analysis_status"])
            asset_status = str(row["status"])
            if analysis_status in {"pending", "running"}:
                waiting += 1
                continue
            if analysis_status == "completed":
                analyzed += 1
                if asset_status == "ready":
                    indexed += 1
                else:
                    failed += 1
                    unusable.append(asset_id)
                continue
            failed += 1
            unusable.append(asset_id)
        return AssetBuildProgress(
            analyzed=analyzed,
            indexed=indexed,
            failed=failed,
            waiting=waiting,
            missing_asset_ids=missing,
            unusable_asset_ids=tuple(unusable),
        )

    def save_analysis_progress(
        self, job: LibraryBuildJob, progress: AssetBuildProgress
    ) -> LibraryBuildJob | None:
        value = _progress_value(job, progress)
        with self._connect() as connection:
            row = connection.execute(
                f"""UPDATE library_build_jobs
                       SET progress=%s::jsonb,updated_at=now(),revision=revision+1
                     WHERE workspace_id=%s AND id=%s AND revision=%s
                       AND status='running' AND stage='analyze'
                     RETURNING {self._COLUMNS}""",
                (
                    json.dumps(value, ensure_ascii=False),
                    job.workspace_id,
                    job.job_id,
                    job.revision,
                ),
            ).fetchone()
        return _job(row) if row is not None else None

    def finish(
        self, job: LibraryBuildJob, progress: AssetBuildProgress
    ) -> LibraryBuildJob | None:
        value = _progress_value(job, progress)
        asset_ids = _asset_ids(job.progress)
        has_errors = (
            not asset_ids
            or progress.failed > 0
            or progress.indexed != len(asset_ids)
            or job.error is not None
        )
        status = "completed_with_errors" if has_errors else "completed"
        error = _completion_error(job, progress) if has_errors else None
        with self._connect() as connection:
            row = connection.execute(
                f"""UPDATE library_build_jobs
                       SET status=%s,stage='index',progress=%s::jsonb,error=%s::jsonb,
                           completed_at=now(),updated_at=now(),revision=revision+1
                     WHERE workspace_id=%s AND id=%s AND revision=%s
                       AND status='running' AND stage IN ('analyze','index')
                     RETURNING {self._COLUMNS}""",
                (
                    status,
                    json.dumps(value, ensure_ascii=False),
                    json.dumps(error, ensure_ascii=False) if error else None,
                    job.workspace_id,
                    job.job_id,
                    job.revision,
                ),
            ).fetchone()
        return _job(row) if row is not None else None

    def fail(
        self,
        job: LibraryBuildJob,
        *,
        code: str,
        message: str,
        details: Mapping[str, Any],
    ) -> LibraryBuildJob | None:
        error = {"code": code, "message": message, "details": dict(details)}
        with self._connect() as connection:
            row = connection.execute(
                f"""UPDATE library_build_jobs
                       SET status='failed',error=%s::jsonb,completed_at=now(),
                           updated_at=now(),revision=revision+1
                     WHERE workspace_id=%s AND id=%s AND revision=%s
                       AND status IN ('queued','running')
                     RETURNING {self._COLUMNS}""",
                (
                    json.dumps(error, ensure_ascii=False),
                    job.workspace_id,
                    job.job_id,
                    job.revision,
                ),
            ).fetchone()
        return _job(row) if row is not None else None

    def _connect(self) -> Any:
        return psycopg.connect(self.database_url, row_factory=dict_row)


class LibraryBuildService:
    """Consumes library builds only after normal Run work has been offered first."""

    def __init__(
        self,
        *,
        repository: LibraryBuildRepository,
        queue: RedisQueue,
        acquirer: AssetAcquirer | None,
        clock: Any,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self.repository = repository
        self.queue = queue
        self.acquirer = acquirer
        self.clock = clock
        self.worker_id = worker_id
        self.lease_seconds = max(lease_seconds, 60.0)

    async def process_once(self) -> bool:
        delivery = await asyncio.to_thread(
            self.queue.reserve_payload,
            LIBRARY_BUILD_QUEUE,
            self.worker_id,
            self.clock.now(),
            self.lease_seconds,
        )
        if delivery is None:
            return False
        heartbeat = _PayloadLeaseHeartbeat(
            self.queue, delivery, self.lease_seconds
        )
        heartbeat.start()
        action = "release"
        current: LibraryBuildJob | None = None
        try:
            workspace_id, job_id, library_id = _payload_ids(delivery.payload)
            current = await asyncio.to_thread(
                self.repository.load, workspace_id, job_id
            )
            if current is None or current.status in _TERMINAL_STATUSES:
                action = "ack"
            elif current.library_id != library_id:
                await asyncio.to_thread(
                    self.repository.fail,
                    current,
                    code="library_build_payload_mismatch",
                    message="library build queue payload does not match the durable job",
                    details={"payload_library_id": library_id},
                )
                action = "ack"
            elif current.stage in {"discover", "transfer"}:
                action = await self._acquire(current)
            elif current.stage in {"analyze", "index"}:
                action = await self._reconcile_analysis(current)
            else:
                raise PermanentStepError(
                    "library build contains an unsupported stage",
                    code="library_build_state_invalid",
                    details={"stage": current.stage},
                )
        except PermanentStepError as exc:
            logger.error(
                "library build failed permanently id=%s code=%s error=%s",
                delivery.message_id,
                exc.code,
                exc,
            )
            if current is not None and current.status not in _TERMINAL_STATUSES:
                saved = await asyncio.to_thread(
                    self.repository.fail,
                    current,
                    code=exc.code,
                    message=str(exc),
                    details=exc.details,
                )
                action = "ack" if saved is not None else "release"
            else:
                action = "ack"
        except RetryableStepError:
            logger.warning(
                "library build provider is temporarily unavailable id=%s",
                delivery.message_id,
                exc_info=True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.error(
                "invalid library build delivery id=%s error=%s",
                delivery.message_id,
                exc,
            )
            if current is not None and current.status not in _TERMINAL_STATUSES:
                saved = await asyncio.to_thread(
                    self.repository.fail,
                    current,
                    code="library_build_input_invalid",
                    message=str(exc) or exc.__class__.__name__,
                    details={},
                )
                action = "ack" if saved is not None else "release"
            else:
                action = "ack"
        except Exception:
            logger.exception("library build polling failed id=%s", delivery.message_id)
        finally:
            heartbeat.stop()
        try:
            if action == "ack":
                await asyncio.to_thread(self.queue.ack_payload, delivery)
            else:
                await asyncio.to_thread(
                    self.queue.release_payload,
                    delivery,
                    self.clock.now() + timedelta(seconds=30),
                )
        except DeliveryLeaseLostError:
            logger.warning(
                "library build queue lease was lost id=%s action=%s",
                delivery.message_id,
                action,
            )
        return True

    async def _acquire(self, job: LibraryBuildJob) -> str:
        if self.acquirer is None:
            raise PermanentStepError(
                "library build requires a configured control API asset acquirer",
                code="library_build_acquisition_unavailable",
                details={"job_id": job.job_id},
            )
        spec = _acquisition_spec(job.spec)
        transferring = await asyncio.to_thread(self.repository.mark_transfer, job)
        if transferring is None:
            return "release"
        try:
            result = await self.acquirer.acquire(
                workspace_id=transferring.workspace_id,
                run_id=None,
                step_id=None,
                library_id=transferring.library_id,
                queries=spec["queries"],
                sources=spec["sources"],
                max_assets=spec["max_assets"],
                copyright_status=spec["copyright_status"],
                idempotency_scope=transferring.job_id,
            )
        except PermanentStepError as exc:
            saved = await asyncio.to_thread(
                self.repository.fail,
                transferring,
                code=exc.code,
                message=str(exc),
                details=exc.details,
            )
            return "ack" if saved is not None else "release"
        saved = await asyncio.to_thread(
            self.repository.save_acquisition,
            transferring,
            result=result,
            error=_acquisition_error(result),
        )
        # Analysis is deliberately polled on a later claim. Holding this lease
        # would starve normal Run steps while the independent analysis queue works.
        return "release"

    async def _reconcile_analysis(self, job: LibraryBuildJob) -> str:
        asset_ids = _asset_ids(job.progress)
        progress = await asyncio.to_thread(
            self.repository.inspect_assets, job.workspace_id, asset_ids
        )
        if progress.waiting:
            await asyncio.to_thread(
                self.repository.save_analysis_progress, job, progress
            )
            return "release"
        saved = await asyncio.to_thread(self.repository.finish, job, progress)
        return "ack" if saved is not None else "release"


def _job(row: Mapping[str, Any]) -> LibraryBuildJob:
    return LibraryBuildJob(
        workspace_id=str(row["workspace_id"]),
        job_id=str(row["id"]),
        library_id=str(row["library_id"]),
        status=str(row["status"]),
        stage=str(row["stage"]),
        spec=_mapping(row.get("spec")),
        progress=_mapping(row.get("progress")),
        error=(
            _mapping(row.get("error")) if row.get("error") is not None else None
        ),
        revision=int(row["revision"]),
    )


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("library build JSON fields must be objects")
    return dict(value)


def _payload_ids(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    values = tuple(
        str(UUID(str(payload.get(key) or "").strip()))
        for key in ("workspace_id", "job_id", "library_id")
    )
    return values[0], values[1], values[2]


def _acquisition_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    topic = str(spec.get("topic") or "").strip()
    maximum = int(spec.get("max_assets", 6))
    if not 1 <= maximum <= 6:
        raise ValueError("library build max_assets must be between 1 and 6")
    raw_queries = spec.get("queries", [])
    if not isinstance(raw_queries, Sequence) or isinstance(raw_queries, (str, bytes)):
        raise TypeError("library build queries must be an array")
    queries = tuple(
        dict.fromkeys(str(value).strip() for value in raw_queries if str(value).strip())
    )
    if not queries and topic:
        queries = (topic,)
    if not queries or len(queries) > 12 or any(len(value) > 500 for value in queries):
        raise ValueError("library build requires 1-12 bounded queries")
    # The acquisition endpoint imports at most one candidate per query pass.
    # Repeat the deduplicated editorial query set round-robin so the default
    # one-topic request can actually fill its requested asset budget. Its
    # request-level seen URL set advances repeated queries to later candidates.
    expanded_queries = list(queries)
    while len(expanded_queries) < maximum:
        expanded_queries.append(queries[len(expanded_queries) % len(queries)])
    raw_sources = spec.get("sources", ["wikimedia"])
    if not isinstance(raw_sources, Sequence) or isinstance(raw_sources, (str, bytes)):
        raise TypeError("library build sources must be an array")
    sources = tuple(dict.fromkeys(str(value).strip() for value in raw_sources))
    if not sources or any(value not in {"youtube", "bilibili", "wikimedia"} for value in sources):
        raise ValueError("library build contains an unsupported source")
    copyright_status = str(spec.get("copyright_status") or "public_domain")
    if copyright_status not in {"licensed", "public_domain"}:
        raise ValueError("library build copyright status is invalid")
    if spec.get("rights_confirmed") is not True:
        raise ValueError("library build rights must be explicitly confirmed")
    if copyright_status == "public_domain" and sources != ("wikimedia",):
        raise ValueError("public-domain library builds are limited to Wikimedia")
    return {
        "queries": tuple(expanded_queries[:12]),
        "sources": sources,
        "max_assets": maximum,
        "copyright_status": copyright_status,
    }


def _asset_ids(progress: Mapping[str, Any]) -> tuple[str, ...]:
    raw = progress.get("asset_ids", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TypeError("library build progress asset_ids must be an array")
    values: list[str] = []
    for item in raw:
        value = str(UUID(str(item)))
        if value not in values:
            values.append(value)
    return tuple(values[:6])


def _progress_value(
    job: LibraryBuildJob, progress: AssetBuildProgress
) -> dict[str, Any]:
    asset_ids = _asset_ids(job.progress)
    return {
        "asset_ids": list(asset_ids),
        "discovered": max(int(job.progress.get("discovered", 0)), len(asset_ids)),
        "transferred": max(int(job.progress.get("transferred", 0)), len(asset_ids)),
        "analyzed": progress.analyzed,
        "indexed": progress.indexed,
        "failed": progress.failed,
    }


def _acquisition_error(result: AssetAcquisitionResult) -> dict[str, Any] | None:
    if not result.unresolved_queries and not result.provider_errors and result.asset_ids:
        return None
    return {
        "code": "library_build_acquisition_partial",
        "message": "Some requested library coverage could not be acquired",
        "details": {
            "unresolved_queries": list(result.unresolved_queries[:12]),
            "provider_errors": [dict(item) for item in result.provider_errors[:50]],
            "asset_ids": list(result.asset_ids[:6]),
        },
    }


def _completion_error(
    job: LibraryBuildJob, progress: AssetBuildProgress
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "missing_asset_ids": list(progress.missing_asset_ids),
        "unusable_asset_ids": list(progress.unusable_asset_ids),
    }
    if job.error is not None:
        details["acquisition"] = dict(job.error)
    return {
        "code": "library_build_completed_with_errors",
        "message": "The library build completed but some assets were unavailable or unusable",
        "details": details,
    }
