"""PostgreSQL CAS/checkpoint store for the isolated asset-analysis pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from framefactory.worker.asset_pipeline import (
    PIPELINE_VERSION,
    AssetPipelineError,
    AssetSource,
    AssetStage,
    AssetWork,
    BatchProgress,
    ReviewGate,
    analysis_tags,
)


class RevisionConflict(RuntimeError):
    pass


class PostgresAssetJobRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def plan(
        self,
        workspace_id: str,
        *,
        limit: int = 10,
        library_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        _canary_limit(limit)
        with self._connect() as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT a.id AS asset_id,a.title,a.copyright_status,
                              f.content_hash,f.media_type,f.byte_size
                         FROM assets a
                         JOIN LATERAL (
                           SELECT af.content_hash,af.media_type,af.byte_size
                             FROM asset_files af
                            WHERE af.workspace_id=a.workspace_id AND af.asset_id=a.id
                              AND af.deleted_at IS NULL
                            ORDER BY af.created_at LIMIT 1
                         ) f ON true
                        WHERE a.workspace_id=%s AND a.status='quarantined'
                          AND (%s::uuid IS NULL OR a.library_id=%s::uuid)
                          AND NOT EXISTS (
                            SELECT 1 FROM asset_analysis_items ai
                             WHERE ai.workspace_id=a.workspace_id AND ai.asset_id=a.id
                               AND ai.pipeline_version=%s AND ai.source_content_hash=f.content_hash
                               AND ai.status IN ('pending','running','retry_wait','awaiting_review')
                          )
                        ORDER BY a.created_at,a.id LIMIT %s""",
                    (workspace_id, library_id, library_id, PIPELINE_VERSION, limit),
                ).fetchall()
            )

    def create_batch(
        self,
        workspace_id: str,
        *,
        idempotency_key: str,
        limit: int = 10,
        rate_limit_per_minute: int = 30,
        library_id: str | None = None,
        max_attempts: int = 3,
    ) -> str:
        _canary_limit(limit)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not 1 <= rate_limit_per_minute <= 600:
            raise ValueError("rate_limit_per_minute must be between 1 and 600")
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        batch_id = uuid5(NAMESPACE_URL, f"asset-analysis-batch:{workspace_id}:{idempotency_key}")
        with self._connect() as connection, connection.transaction():
            existing = connection.execute(
                """SELECT id,configuration FROM asset_analysis_batches
                    WHERE workspace_id=%s AND idempotency_key=%s FOR UPDATE""",
                (workspace_id, idempotency_key),
            ).fetchone()
            configuration = {
                "library_id": library_id,
                "max_attempts": max_attempts,
                "auto_ready": False,
            }
            if existing is not None:
                if dict(existing["configuration"]) != configuration:
                    raise ValueError("batch idempotency key already exists with different input")
                return str(existing["id"])
            candidates = self.plan(workspace_id, limit=limit, library_id=library_id)
            connection.execute(
                """INSERT INTO asset_analysis_batches
                     (id,workspace_id,idempotency_key,status,dry_run,requested_limit,
                      rate_limit_per_minute,pipeline_version,configuration,total_count,pending_count)
                     VALUES (%s,%s,%s,'pending',false,%s,%s,%s,%s::jsonb,%s,%s)""",
                (
                    batch_id,
                    workspace_id,
                    idempotency_key,
                    limit,
                    rate_limit_per_minute,
                    PIPELINE_VERSION,
                    json.dumps(configuration),
                    len(candidates),
                    len(candidates),
                ),
            )
            for candidate in candidates:
                item_id = uuid5(NAMESPACE_URL, f"asset-analysis-item:{batch_id}:{candidate['asset_id']}")
                connection.execute(
                    """INSERT INTO asset_analysis_items
                         (id,workspace_id,batch_id,asset_id,source_content_hash,
                          pipeline_version,max_attempts)
                         VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        item_id,
                        workspace_id,
                        batch_id,
                        candidate["asset_id"],
                        candidate["content_hash"],
                        PIPELINE_VERSION,
                        max_attempts,
                    ),
                )
        return str(batch_id)

    def create_batch_for_asset(
        self,
        workspace_id: str,
        asset_id: str,
        *,
        external_job_id: str,
        run_id: str | None = None,
        step_id: str | None = None,
        acquisition_id: str | None = None,
        acquisition_count: int = 1,
        auto_ready: bool = False,
        rate_limit_per_minute: int = 30,
        max_attempts: int = 3,
    ) -> str:
        """Materialize one API analysis job into the recoverable analysis pipeline."""

        if not 1 <= acquisition_count <= 100:
            raise ValueError("acquisition_count must be between 1 and 100")
        idempotency_key = f"api-asset-analysis:{external_job_id}"
        batch_id = uuid5(
            NAMESPACE_URL, f"asset-analysis-batch:{workspace_id}:{idempotency_key}"
        )
        configuration = {
            "asset_id": asset_id,
            "external_job_id": external_job_id,
            "run_id": run_id,
            "step_id": step_id,
            "acquisition_id": acquisition_id,
            "acquisition_count": acquisition_count,
            "max_attempts": max_attempts,
            "auto_ready": auto_ready,
        }
        with self._connect() as connection, connection.transaction():
            existing = connection.execute(
                """SELECT id,configuration FROM asset_analysis_batches
                    WHERE workspace_id=%s AND idempotency_key=%s FOR UPDATE""",
                (workspace_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if dict(existing["configuration"]) != configuration:
                    raise ValueError(
                        "asset analysis idempotency key has different context"
                    )
                return str(existing["id"])
            candidate = connection.execute(
                """SELECT a.id AS asset_id,f.content_hash
                     FROM assets a
                     JOIN LATERAL (
                       SELECT af.content_hash FROM asset_files af
                        WHERE af.workspace_id=a.workspace_id AND af.asset_id=a.id
                          AND af.deleted_at IS NULL
                        ORDER BY af.created_at LIMIT 1
                     ) f ON true
                    WHERE a.workspace_id=%s AND a.id=%s
                      AND a.status IN ('processing','quarantined','awaiting_review')
                    FOR UPDATE OF a""",
                (workspace_id, asset_id),
            ).fetchone()
            if candidate is None:
                raise ValueError("asset is unavailable for analysis")
            item_id = uuid5(
                NAMESPACE_URL,
                f"asset-analysis-item:{batch_id}:{candidate['asset_id']}",
            )
            connection.execute(
                """UPDATE asset_analysis_items
                      SET status='cancelled',completed_at=now(),updated_at=now(),
                          failure_code='superseded_by_reanalysis',revision=revision+1
                    WHERE workspace_id=%s AND asset_id=%s AND pipeline_version=%s
                      AND source_content_hash=%s AND status='awaiting_review'""",
                (
                    workspace_id,
                    candidate["asset_id"],
                    PIPELINE_VERSION,
                    candidate["content_hash"],
                ),
            )
            connection.execute(
                """INSERT INTO asset_analysis_batches
                     (id,workspace_id,idempotency_key,status,dry_run,requested_limit,
                      rate_limit_per_minute,pipeline_version,configuration,total_count,
                      pending_count)
                     VALUES (%s,%s,%s,'pending',false,1,%s,%s,%s::jsonb,1,1)""",
                (
                    batch_id,
                    workspace_id,
                    idempotency_key,
                    rate_limit_per_minute,
                    PIPELINE_VERSION,
                    json.dumps(configuration),
                ),
            )
            connection.execute(
                """INSERT INTO asset_analysis_items
                     (id,workspace_id,batch_id,asset_id,source_content_hash,
                      pipeline_version,max_attempts)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    item_id,
                    workspace_id,
                    batch_id,
                    candidate["asset_id"],
                    candidate["content_hash"],
                    PIPELINE_VERSION,
                    max_attempts,
                ),
            )
            connection.execute(
                """UPDATE assets SET status='processing',analysis_status='running',
                       updated_at=now(),revision=revision+1
                     WHERE workspace_id=%s AND id=%s""",
                (workspace_id, asset_id),
            )
            connection.execute(
                """UPDATE asset_analysis_jobs SET status='running',started_at=COALESCE(started_at,now()),
                       updated_at=now(),queue_job_id=%s
                     WHERE workspace_id=%s AND id=%s""",
                (str(batch_id), workspace_id, external_job_id),
            )
        return str(batch_id)

    def batch_context(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT configuration FROM asset_analysis_batches WHERE id=%s",
                (batch_id,),
            ).fetchone()
        return dict(row["configuration"]) if row is not None else {}

    def pending_resume_targets(self, *, limit: int = 25) -> tuple[dict[str, Any], ...]:
        """Return settled auto-acquisition groups whose media step can be retried."""

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT workspace_id,configuration->>'run_id' AS run_id,
                          configuration->>'step_id' AS step_id,
                          array_agg(id::text ORDER BY created_at) AS batch_ids
                     FROM asset_analysis_batches b
                    WHERE configuration->>'run_id' IS NOT NULL
                      AND configuration->>'step_id' IS NOT NULL
                      AND configuration->>'resume_dispatched_at' IS NULL
                    GROUP BY workspace_id,configuration->>'run_id',configuration->>'step_id'
                   HAVING bool_and(status IN ('completed','completed_with_errors'))
                      AND count(*) >= max(
                        COALESCE((configuration->>'acquisition_count')::integer,1)
                      )
                    ORDER BY min(created_at) LIMIT %s""",
                (limit,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def mark_resume_dispatched(self, batch_ids: list[str] | tuple[str, ...]) -> None:
        if not batch_ids:
            return
        with self._connect() as connection:
            connection.execute(
                """UPDATE asset_analysis_batches
                      SET configuration=jsonb_set(configuration,'{resume_dispatched_at}',
                            to_jsonb(now()::text),true),updated_at=now(),revision=revision+1
                    WHERE id=ANY(%s::uuid[])""",
                (list(batch_ids),),
            )

    def is_dry_run(self, batch_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT dry_run FROM asset_analysis_batches WHERE id=%s",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"asset analysis batch does not exist: {batch_id}")
        return bool(row["dry_run"])

    def cancel_requested(self, batch_id: str) -> bool:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """SELECT status IN ('cancellation_requested','cancelled') AS cancelled
                     FROM asset_analysis_batches WHERE id=%s""",
                (batch_id,),
            ).fetchone()
        return row is None or bool(row["cancelled"])

    def request_cancel(self, batch_id: str) -> None:
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """UPDATE asset_analysis_batches
                      SET status='cancellation_requested',cancel_requested_at=now(),
                          updated_at=now(),revision=revision+1
                    WHERE id=%s AND status IN ('pending','running')""",
                (batch_id,),
            )
            connection.execute(
                """UPDATE asset_analysis_items SET status='cancelled',completed_at=now(),
                       updated_at=now(),revision=revision+1
                     WHERE batch_id=%s AND status IN ('pending','retry_wait')""",
                (batch_id,),
            )

    def retry_failed(self, batch_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE asset_analysis_items
                      SET status='retry_wait',failure_code=NULL,failure_reason=NULL,
                          retryable=false,available_at=now(),lease_owner=NULL,
                          lease_expires_at=NULL,updated_at=now(),revision=revision+1
                    WHERE batch_id=%s AND status='failed' AND attempts < max_attempts""",
                (batch_id,),
            )
            connection.execute(
                """UPDATE asset_analysis_batches SET status='running',completed_at=NULL,
                       updated_at=now(),revision=revision+1 WHERE id=%s""",
                (batch_id,),
            )
            return cursor.rowcount

    def claim(self, batch_id: str, worker_id: str, lease_seconds: float) -> AssetWork | None:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """SELECT i.id
                     FROM asset_analysis_items i
                     JOIN asset_analysis_batches b ON b.id=i.batch_id
                    WHERE i.batch_id=%s
                      AND b.status NOT IN ('cancellation_requested','cancelled')
                      AND ((i.status IN ('pending','retry_wait') AND i.available_at <= now())
                           OR (i.status='running' AND i.lease_expires_at < now()))
                    ORDER BY i.available_at,i.created_at,i.id
                    FOR UPDATE OF i SKIP LOCKED LIMIT 1""",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            claimed = connection.execute(
                """UPDATE asset_analysis_items
                      SET status='running',attempts=attempts+1,lease_owner=%s,
                          lease_expires_at=%s,started_at=COALESCE(started_at,now()),
                          failure_code=NULL,failure_reason=NULL,
                          updated_at=now(),revision=revision+1
                    WHERE id=%s RETURNING *""",
                (worker_id, lease_expires, row["id"]),
            ).fetchone()
            connection.execute(
                """UPDATE asset_analysis_batches SET status='running',
                       started_at=COALESCE(started_at,now()),updated_at=now(),revision=revision+1
                     WHERE id=%s AND status='pending'""",
                (batch_id,),
            )
            return self._work(connection, claimed)

    def renew_lease(self, item_id: str, worker_id: str, lease_seconds: float) -> bool:
        """Extend ownership without changing the CAS revision used by checkpoints."""
        lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE asset_analysis_items
                      SET lease_expires_at=%s,updated_at=now()
                    WHERE id=%s AND status='running' AND lease_owner=%s
                    RETURNING id""",
                (lease_expires, item_id, worker_id),
            ).fetchone()
        return row is not None

    def checkpoint(
        self,
        work: AssetWork,
        stage: AssetStage,
        value: dict[str, Any] | Any,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> AssetWork:
        if not isinstance(value, dict):
            value = dict(value)
        checkpoints = {**dict(work.checkpoints), stage.value: value}
        completed = tuple(dict.fromkeys((*work.completed_stages, stage.value)))
        lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE asset_analysis_items
                      SET checkpoints=%s::jsonb,completed_stages=%s,current_stage=%s,
                          lease_expires_at=%s,updated_at=now(),revision=revision+1
                    WHERE id=%s AND revision=%s AND status='running' AND lease_owner=%s
                    RETURNING revision""",
                (
                    json.dumps(checkpoints, ensure_ascii=False),
                    list(completed),
                    _next_stage(stage),
                    lease_expires,
                    work.item_id,
                    work.revision,
                    worker_id,
                ),
            ).fetchone()
        if row is None:
            raise RevisionConflict(f"asset analysis item lease/revision changed: {work.item_id}")
        return replace(
            work,
            revision=int(row["revision"]),
            completed_stages=completed,
            checkpoints=checkpoints,
        )

    def mark_cancelled(self, work: AssetWork, *, worker_id: str) -> None:
        self._terminal_update(work, "cancelled", worker_id=worker_id)

    def mark_failure(
        self,
        work: AssetWork,
        stage: AssetStage,
        error: AssetPipelineError,
        *,
        worker_id: str,
    ) -> None:
        retry = error.retryable and work.attempts < work.max_attempts
        status = "retry_wait" if retry else "failed"
        delay = min(3600, 5 * 2 ** max(0, work.attempts - 1))
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE asset_analysis_items
                      SET status=%s,current_stage=%s,failure_code=%s,failure_reason=%s,
                          retryable=%s,available_at=now()+(%s * interval '1 second'),
                          lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),
                          completed_at=CASE WHEN %s='failed' THEN now() ELSE NULL END,
                          revision=revision+1
                    WHERE id=%s AND revision=%s AND status='running' AND lease_owner=%s
                    RETURNING id""",
                (
                    status,
                    stage.value,
                    error.code[:100],
                    str(error)[:500],
                    error.retryable,
                    delay,
                    status,
                    work.item_id,
                    work.revision,
                    worker_id,
                ),
            ).fetchone()
            if stage is AssetStage.MALWARE_SCAN and status == "failed":
                scan_status = "rejected" if error.code == "malware_detected" else "failed"
                connection.execute(
                    """UPDATE asset_files SET scan_status=%s
                        WHERE workspace_id=%s AND asset_id=%s AND content_hash=%s""",
                    (scan_status, work.workspace_id, work.asset_id, work.content_hash),
                )
            if status == "failed":
                connection.execute(
                    """UPDATE assets SET status='quarantined',analysis_status='failed',
                           updated_at=now(),revision=revision+1
                         WHERE workspace_id=%s AND id=%s""",
                    (work.workspace_id, work.asset_id),
                )
                self._finish_external_job(
                    connection,
                    work.batch_id,
                    status="failed",
                    error={"code": error.code, "message": str(error)[:500]},
                )
        if row is None:
            raise RevisionConflict(f"asset analysis item lease/revision changed: {work.item_id}")

    def publish_analysis(
        self,
        work: AssetWork,
        analysis: dict[str, Any] | Any,
        gate: ReviewGate,
        *,
        worker_id: str,
    ) -> None:
        if not isinstance(analysis, dict):
            analysis = dict(analysis)
        result_hash = hashlib.sha256(
            json.dumps(analysis, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        analysis_id = uuid5(
            NAMESPACE_URL,
            f"asset-analysis:{work.workspace_id}:{work.asset_id}:{work.content_hash}:"
            f"{PIPELINE_VERSION}:{result_hash}",
        )
        with self._connect() as connection, connection.transaction():
            locked = connection.execute(
                """SELECT i.revision,a.status,a.copyright_status,f.scan_status
                     FROM asset_analysis_items i
                     JOIN assets a ON a.workspace_id=i.workspace_id AND a.id=i.asset_id
                     JOIN asset_files f ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                       AND f.content_hash=i.source_content_hash AND f.deleted_at IS NULL
                    WHERE i.id=%s FOR UPDATE OF i,a""",
                (work.item_id,),
            ).fetchone()
            if (
                locked is None
                or int(locked["revision"]) != work.revision
                or locked["status"]
                not in {"processing", "quarantined", "awaiting_review"}
            ):
                raise RevisionConflict(f"asset changed while analysis was running: {work.asset_id}")
            version = int(
                connection.execute(
                    """SELECT COALESCE(max(analysis_version),0)+1 AS version
                         FROM asset_analyses WHERE workspace_id=%s AND asset_id=%s""",
                    (work.workspace_id, work.asset_id),
                ).fetchone()["version"]
            )
            search_text = _analysis_search_text(analysis)
            connection.execute(
                """INSERT INTO asset_analyses
                     (id,workspace_id,asset_id,analysis_version,provider,model,status,
                      summary,language,people,organizations,locations,eras,scene_types,
                      actions,moods,visual_styles,keywords,has_embedded_text,has_watermark,
                      safety,quality,confidence,search_text,raw_result)
                     VALUES (%s,%s,%s,%s,'configured-vision-provider',%s,'completed',
                             %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                             %s::jsonb,%s,%s,%s::jsonb)
                     ON CONFLICT (workspace_id,id) DO UPDATE SET
                       analysis_version=EXCLUDED.analysis_version,status='completed',
                       raw_result=EXCLUDED.raw_result,created_at=now()""",
                (
                    analysis_id,
                    work.workspace_id,
                    work.asset_id,
                    version,
                    PIPELINE_VERSION,
                    analysis.get("summary", ""),
                    analysis.get("language"),
                    analysis.get("people", []),
                    analysis.get("organizations", []),
                    analysis.get("locations", []),
                    analysis.get("eras", []),
                    analysis.get("scene_types", []),
                    analysis.get("actions", []),
                    analysis.get("moods", []),
                    analysis.get("visual_styles", []),
                    analysis.get("keywords", []),
                    analysis.get("has_embedded_text", False),
                    analysis.get("has_watermark", False),
                    json.dumps(analysis.get("safety", {}), ensure_ascii=False),
                    json.dumps(analysis.get("quality", {}), ensure_ascii=False),
                    analysis.get("confidence"),
                    search_text,
                    json.dumps({**analysis, "review_gate": {"reasons": gate.reasons,
                                                            "eligible_for_auto_ready": gate.eligible_for_auto_ready}},
                               ensure_ascii=False),
                ),
            )
            connection.execute(
                """UPDATE asset_analyses SET status='superseded'
                    WHERE workspace_id=%s AND asset_id=%s AND status='completed' AND id<>%s""",
                (work.workspace_id, work.asset_id, analysis_id),
            )
            self._insert_segments(connection, work, analysis_id, analysis)
            self._insert_temporal_analysis(connection, work, analysis_id, analysis)
            self._insert_tags(connection, work, analysis_id, analysis)
            connection.execute(
                """UPDATE asset_files SET scan_status='clean',width=%s,height=%s,duration_ms=%s
                    WHERE workspace_id=%s AND asset_id=%s AND content_hash=%s""",
                (
                    analysis.get("technical", {}).get("width"),
                    analysis.get("technical", {}).get("height"),
                    analysis.get("technical", {}).get("duration_ms"),
                    work.workspace_id,
                    work.asset_id,
                    work.content_hash,
                ),
            )
            configuration_row = connection.execute(
                "SELECT configuration FROM asset_analysis_batches WHERE id=%s",
                (work.batch_id,),
            ).fetchone()
            configuration = (
                dict(configuration_row["configuration"]) if configuration_row else {}
            )
            auto_ready = bool(configuration.get("auto_ready")) and gate.eligible_for_auto_ready
            target_status = "ready" if auto_ready else "awaiting_review"
            preview = work.checkpoints.get(AssetStage.PREVIEW.value)
            keyframes = work.checkpoints.get(AssetStage.KEYFRAMES.value)
            metadata_patch: dict[str, Any] = {
                "asset_analysis": {
                    "pipeline_version": PIPELINE_VERSION,
                    "review_reasons": gate.reasons,
                    "eligible_for_auto_ready": gate.eligible_for_auto_ready,
                    "auto_ready": auto_ready,
                }
            }
            if isinstance(preview, dict):
                metadata_patch["preview"] = preview
            if isinstance(keyframes, dict) and isinstance(keyframes.get("poster"), dict):
                metadata_patch["poster"] = keyframes["poster"]
            connection.execute(
                """UPDATE assets SET status=%s,analysis_status='completed',
                       metadata=metadata || %s::jsonb,
                       updated_at=now() WHERE workspace_id=%s AND id=%s""",
                (
                    target_status,
                    json.dumps(metadata_patch),
                    work.workspace_id,
                    work.asset_id,
                ),
            )
            changed = connection.execute(
                """UPDATE asset_analysis_items SET status=%s,review_reasons=%s,
                       lease_owner=NULL,lease_expires_at=NULL,completed_at=now(),updated_at=now(),
                       revision=revision+1
                     WHERE id=%s AND revision=%s AND status='running' AND lease_owner=%s
                     RETURNING id""",
                (
                    target_status,
                    list(gate.reasons),
                    work.item_id,
                    work.revision,
                    worker_id,
                ),
            ).fetchone()
            if changed is None:
                raise RevisionConflict(f"asset analysis item lease/revision changed: {work.item_id}")
            self._finish_external_job(connection, work.batch_id, status="succeeded")

    @staticmethod
    def _finish_external_job(
        connection: Any,
        batch_id: str,
        *,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT configuration FROM asset_analysis_batches WHERE id=%s",
            (batch_id,),
        ).fetchone()
        configuration = dict(row["configuration"]) if row is not None else {}
        external_job_id = configuration.get("external_job_id")
        if not external_job_id:
            return
        connection.execute(
            """UPDATE asset_analysis_jobs SET status=%s,error=%s::jsonb,
                   completed_at=CASE WHEN %s IN ('succeeded','failed') THEN now() ELSE NULL END,
                   updated_at=now()
                 WHERE id=%s""",
            (status, json.dumps(error) if error else None, status, external_job_id),
        )

    def review(
        self,
        item_id: str,
        *,
        decision: str,
        actor_id: str | None,
        comment: str,
    ) -> None:
        if decision not in {"approve", "reject", "request_changes"}:
            raise ValueError("unsupported review decision")
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """SELECT i.*,a.copyright_status,f.scan_status,
                          EXISTS(SELECT 1 FROM asset_analyses aa WHERE aa.workspace_id=i.workspace_id
                            AND aa.asset_id=i.asset_id AND aa.status='completed') AS analyzed
                     FROM asset_analysis_items i
                     JOIN assets a ON a.workspace_id=i.workspace_id AND a.id=i.asset_id
                     JOIN asset_files f ON f.workspace_id=i.workspace_id AND f.asset_id=i.asset_id
                       AND f.content_hash=i.source_content_hash AND f.deleted_at IS NULL
                    WHERE i.id=%s FOR UPDATE OF i,a""",
                (item_id,),
            ).fetchone()
            if row is None or row["status"] != "awaiting_review":
                raise ValueError("asset item is not awaiting review")
            if decision == "approve":
                if row["copyright_status"] not in {"owned", "licensed", "public_domain"}:
                    raise ValueError("copyright must be resolved before approval")
                if row["scan_status"] != "clean" or not row["analyzed"]:
                    raise ValueError("scan and completed analysis are required before approval")
                item_status, asset_status = "ready", "ready"
            elif decision == "request_changes":
                item_status, asset_status = "retry_wait", "quarantined"
            else:
                item_status, asset_status = "cancelled", "quarantined"
            connection.execute(
                """INSERT INTO asset_review_actions
                     (id,workspace_id,item_id,asset_id,decision,actor_id,comment,previous_status)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (uuid4(), row["workspace_id"], row["id"], row["asset_id"], decision,
                 actor_id, comment[:1000], row["status"]),
            )
            if decision == "request_changes":
                retained = [
                    AssetStage.FILE_DETECTION.value,
                    AssetStage.MALWARE_SCAN.value,
                    AssetStage.FFPROBE.value,
                    AssetStage.FINGERPRINT.value,
                    AssetStage.PREVIEW.value,
                    AssetStage.KEYFRAMES.value,
                ]
                connection.execute(
                    """UPDATE asset_analysis_items SET status='retry_wait',available_at=now(),
                           current_stage='visual_analysis',completed_stages=%s,
                           checkpoints=checkpoints - ARRAY['visual_analysis','subtitle_extraction',
                             'audio_transcription','temporal_alignment','shot_segmentation',
                             'normalize_tags','rights_gate'],review_reasons='{}',completed_at=NULL,
                           updated_at=now(),revision=revision+1 WHERE id=%s""",
                    (retained, item_id),
                )
            else:
                connection.execute(
                    """UPDATE asset_analysis_items SET status=%s,available_at=now(),
                           completed_at=now(),updated_at=now(),revision=revision+1 WHERE id=%s""",
                    (item_status, item_id),
                )
            connection.execute(
                "UPDATE assets SET status=%s,updated_at=now() WHERE workspace_id=%s AND id=%s",
                (asset_status, row["workspace_id"], row["asset_id"]),
            )

    def refresh_batch(self, batch_id: str) -> BatchProgress:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(_PROGRESS_SQL, (batch_id,)).fetchone()
            progress = BatchProgress(**row)
            if progress.running or progress.pending:
                status = "running"
                completed_at = None
            elif progress.cancelled == progress.total:
                status = "cancelled"
                completed_at = datetime.now(UTC)
            elif progress.failed or progress.cancelled:
                status = "completed_with_errors"
                completed_at = datetime.now(UTC)
            else:
                status = "completed"
                completed_at = datetime.now(UTC)
            connection.execute(
                """UPDATE asset_analysis_batches SET status=%s,total_count=%s,pending_count=%s,
                       running_count=%s,awaiting_review_count=%s,ready_count=%s,failed_count=%s,
                       cancelled_count=%s,completed_at=%s,updated_at=now(),revision=revision+1
                     WHERE id=%s""",
                (status, progress.total, progress.pending, progress.running,
                 progress.awaiting_review, progress.ready, progress.failed,
                 progress.cancelled, completed_at, batch_id),
            )
        return progress

    def progress(self, batch_id: str) -> BatchProgress:
        with self._connect() as connection:
            row = connection.execute(_PROGRESS_SQL, (batch_id,)).fetchone()
        return BatchProgress(**row)

    def _terminal_update(self, work: AssetWork, status: str, *, worker_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """UPDATE asset_analysis_items SET status=%s,lease_owner=NULL,
                       lease_expires_at=NULL,completed_at=now(),updated_at=now(),revision=revision+1
                     WHERE id=%s AND revision=%s AND status='running' AND lease_owner=%s RETURNING id""",
                (status, work.item_id, work.revision, worker_id),
            ).fetchone()
        if row is None:
            raise RevisionConflict(f"asset analysis item lease/revision changed: {work.item_id}")

    def _work(self, connection: Any, item: dict[str, Any]) -> AssetWork:
        row = connection.execute(
            """SELECT a.copyright_status,f.content_hash,f.media_type,f.bucket,f.object_key,
                      COALESCE(f.original_filename,'source.bin') AS original_filename
                 FROM assets a JOIN asset_files f
                   ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                  AND f.content_hash=%s AND f.deleted_at IS NULL
                WHERE a.workspace_id=%s AND a.id=%s""",
            (item["source_content_hash"], item["workspace_id"], item["asset_id"]),
        ).fetchone()
        sources = connection.execute(
            """SELECT source_type,provider,locator,attribution,license FROM asset_sources
                WHERE workspace_id=%s AND asset_id=%s ORDER BY created_at,id""",
            (item["workspace_id"], item["asset_id"]),
        ).fetchall()
        return AssetWork(
            item_id=str(item["id"]), batch_id=str(item["batch_id"]),
            workspace_id=str(item["workspace_id"]), asset_id=str(item["asset_id"]),
            revision=int(item["revision"]), attempts=int(item["attempts"]),
            max_attempts=int(item["max_attempts"]), completed_stages=tuple(item["completed_stages"]),
            checkpoints=dict(item["checkpoints"]), copyright_status=str(row["copyright_status"]),
            content_hash=str(row["content_hash"]), media_type=str(row["media_type"]),
            bucket=str(row["bucket"]), object_key=str(row["object_key"]),
            original_filename=str(row["original_filename"]),
            sources=tuple(AssetSource(**source) for source in sources),
        )

    @staticmethod
    def _insert_segments(
        connection: Any, work: AssetWork, analysis_id: UUID, analysis: dict[str, Any]
    ) -> None:
        for segment in analysis.get("segments", []):
            segment_id = uuid5(NAMESPACE_URL, f"asset-segment:{analysis_id}:{segment['ordinal']}")
            search_text = " ".join(str(value) for value in (
                segment.get("description"), *segment.get("people", []), *segment.get("locations", []),
                *segment.get("keywords", []), segment.get("scene_type"), segment.get("action"),
                segment.get("era"), segment.get("mood"), segment.get("visual_style"),
                segment.get("shot_type"), segment.get("transcript"),
            ) if value)
            connection.execute(
                """INSERT INTO asset_segments
                     (id,workspace_id,asset_id,analysis_id,ordinal,start_ms,end_ms,description,
                      people,locations,keywords,scene_type,action,era,mood,visual_style,shot_type,
                      confidence,representative_frame_key,search_text,transcript,boundary_score,
                      boundary_reasons,cut_safe,semantic_complete)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                             %s,%s,%s,%s,%s)
                     ON CONFLICT (workspace_id,analysis_id,ordinal) DO NOTHING""",
                (segment_id, work.workspace_id, work.asset_id, analysis_id, segment["ordinal"],
                 segment["start_ms"], segment["end_ms"], segment["description"], segment["people"],
                 segment["locations"], segment["keywords"], segment.get("scene_type"),
                 segment.get("action"), segment.get("era"), segment.get("mood"),
                 segment.get("visual_style"), segment.get("shot_type"), segment.get("confidence"),
                 segment.get("representative_frame_key"), search_text, segment.get("transcript", ""),
                 segment.get("boundary_score"), segment.get("boundary_reasons", []),
                 segment.get("cut_safe", False), segment.get("semantic_complete", False)),
            )

    @staticmethod
    def _insert_temporal_analysis(
        connection: Any, work: AssetWork, analysis_id: UUID, analysis: dict[str, Any]
    ) -> None:
        temporal = analysis.get("temporal", {})
        if not isinstance(temporal, dict):
            return
        if temporal.get("status") != "available" and not temporal.get("full_text"):
            return
        source = temporal.get("source")
        if source not in {"embedded_subtitle", "asr", "aligned"}:
            return
        transcript_id = uuid5(NAMESPACE_URL, f"asset-transcript:{analysis_id}:{source}")
        cues = [item for item in temporal.get("cues", []) if isinstance(item, dict)]
        confidences = [
            float(item["confidence"])
            for item in cues
            if isinstance(item.get("confidence"), (int, float))
        ]
        confidence = sum(confidences) / len(confidences) if confidences else None
        connection.execute(
            """INSERT INTO asset_transcripts
                 (id,workspace_id,asset_id,analysis_id,source,language,provider,model,status,
                  full_text,confidence,word_count,raw_result)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                 ON CONFLICT (workspace_id,analysis_id,source) DO NOTHING""",
            (
                transcript_id,
                work.workspace_id,
                work.asset_id,
                analysis_id,
                source,
                temporal.get("language"),
                temporal.get("provider"),
                temporal.get("model"),
                temporal.get("status", "unavailable"),
                temporal.get("full_text", ""),
                confidence,
                len(temporal.get("words", [])),
                json.dumps(temporal, ensure_ascii=False),
            ),
        )
        for ordinal, cue in enumerate(cues):
            cue_id = uuid5(NAMESPACE_URL, f"asset-transcript-cue:{transcript_id}:{ordinal}")
            cue_source = cue.get("source")
            if cue_source not in {"embedded_subtitle", "asr", "aligned", "temporal"}:
                cue_source = "temporal"
            connection.execute(
                """INSERT INTO asset_transcript_cues
                     (id,workspace_id,asset_id,transcript_id,ordinal,start_ms,end_ms,text,
                      confidence,source)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (workspace_id,transcript_id,ordinal) DO NOTHING""",
                (
                    cue_id,
                    work.workspace_id,
                    work.asset_id,
                    transcript_id,
                    ordinal,
                    cue["start_ms"],
                    cue["end_ms"],
                    cue["text"],
                    cue.get("confidence"),
                    cue_source,
                ),
            )
        boundaries = [
            {
                "timestamp_ms": 0,
                "boundary_score": 1.0,
                "boundary_reasons": ["asset_start"],
                "cut_safe": True,
                "semantic_complete": True,
            },
            *[
                {
                    "timestamp_ms": segment["end_ms"],
                    "boundary_score": segment.get("boundary_score") or 0,
                    "boundary_reasons": segment.get("boundary_reasons", []),
                    "cut_safe": segment.get("cut_safe", False),
                    "semantic_complete": segment.get("semantic_complete", False),
                }
                for segment in analysis.get("segments", [])
            ],
        ]
        for boundary in boundaries:
            boundary_id = uuid5(
                NAMESPACE_URL,
                f"asset-shot-boundary:{analysis_id}:{boundary['timestamp_ms']}",
            )
            connection.execute(
                """INSERT INTO asset_shot_boundaries
                     (id,workspace_id,asset_id,analysis_id,timestamp_ms,score,reasons,
                      cut_safe,semantic_complete)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (workspace_id,analysis_id,timestamp_ms) DO NOTHING""",
                (
                    boundary_id,
                    work.workspace_id,
                    work.asset_id,
                    analysis_id,
                    boundary["timestamp_ms"],
                    boundary["boundary_score"],
                    boundary["boundary_reasons"],
                    boundary["cut_safe"],
                    boundary["semantic_complete"],
                ),
            )

    @staticmethod
    def _insert_tags(
        connection: Any, work: AssetWork, analysis_id: UUID, analysis: dict[str, Any]
    ) -> None:
        for name in analysis_tags(analysis):
            tag_id = uuid5(NAMESPACE_URL, f"asset-tag:{work.workspace_id}:{name.casefold()}")
            slug = "ai-" + hashlib.sha256(name.casefold().encode()).hexdigest()[:24]
            connection.execute(
                """INSERT INTO tags (id,workspace_id,name,slug) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,slug) DO UPDATE SET name=EXCLUDED.name""",
                (tag_id, work.workspace_id, name, slug),
            )
            connection.execute(
                """INSERT INTO asset_tags
                     (workspace_id,asset_id,tag_id,analysis_id,confidence,source)
                     VALUES (%s,%s,%s,%s,%s,'vision')
                     ON CONFLICT (workspace_id,asset_id,tag_id) DO UPDATE SET
                       analysis_id=EXCLUDED.analysis_id,confidence=EXCLUDED.confidence,
                       source=EXCLUDED.source,created_at=now()""",
                (work.workspace_id, work.asset_id, tag_id, analysis_id, analysis.get("confidence")),
            )

    def _connect(self) -> Any:
        return psycopg.connect(self.database_url, row_factory=dict_row)


def _next_stage(stage: AssetStage) -> str:
    stages = tuple(AssetStage)
    index = stages.index(stage)
    return stages[index + 1].value if index + 1 < len(stages) else "publish_analysis"


def _canary_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("asset jobs require an explicit canary limit between 1 and 100")


def _analysis_search_text(analysis: dict[str, Any]) -> str:
    values: list[str] = [str(analysis.get("summary", ""))]
    for name in ("people", "organizations", "locations", "eras", "scene_types", "actions",
                 "moods", "visual_styles", "keywords"):
        values.extend(str(value) for value in analysis.get(name, []))
    return " ".join(value for value in values if value).strip()


_PROGRESS_SQL = """SELECT count(*)::integer AS total,
       count(*) FILTER (WHERE status IN ('pending','retry_wait'))::integer AS pending,
       count(*) FILTER (WHERE status='running')::integer AS running,
       count(*) FILTER (WHERE status='awaiting_review')::integer AS awaiting_review,
       count(*) FILTER (WHERE status='ready')::integer AS ready,
       count(*) FILTER (WHERE status='failed')::integer AS failed,
       count(*) FILTER (WHERE status='cancelled')::integer AS cancelled
  FROM asset_analysis_items WHERE batch_id=%s"""
