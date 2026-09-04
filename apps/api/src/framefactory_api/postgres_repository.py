from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from .asset_policy import normalized_tags, validate_asset_transition
from .errors import ConflictError, NotFoundError, PreconditionFailedError
from .repository import Resource, _catalog_snapshot_content_hash

T = TypeVar("T")


class _Transaction(Protocol):
    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any: ...


class _Connection(Protocol):
    def transaction(self) -> _Transaction: ...

    async def execute(self, query: str, *args: Any) -> str: ...

    async def fetch(self, query: str, *args: Any) -> Sequence[Mapping[str, Any]]: ...

    async def fetchrow(self, query: str, *args: Any) -> Mapping[str, Any] | None: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...


class _Acquire(Protocol):
    async def __aenter__(self) -> _Connection: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any: ...


class _Pool(Protocol):
    def acquire(self) -> _Acquire: ...

    async def close(self) -> None: ...


SKILL_COLUMNS = """
id, workspace_id, ownership_type::text AS ownership_type,
publisher_type::text AS publisher_type, publisher_name, name, slug, description,
visibility::text AS visibility, status::text AS status, current_version_id,
forked_from_skill_id, revision, schema_version, created_by, created_at, updated_at
"""

CHANNEL_COLUMNS = """
c.id, c.workspace_id, c.ownership_type::text AS ownership_type, c.name, c.slug,
c.description, c.platform, c.handle, c.brand_profile, c.platform_connection_id,
c.status::text AS status, c.revision, c.schema_version, c.created_by, c.created_at,
c.updated_at, d.skill_version_id, d.pipeline_version_id, d.voice_profile_id,
d.render_preset_version_id,
COALESCE(
  ARRAY(
    SELECT l.asset_library_id
    FROM channel_default_asset_libraries l
    WHERE l.channel_id = c.id AND l.workspace_id = c.workspace_id
    ORDER BY l.ordinal
  ),
  ARRAY[]::uuid[]
) AS asset_library_ids
"""

VERSION_COLUMNS = """
id, workspace_id, ownership_type::text AS ownership_type, skill_id, version,
state::text AS state, execution_kind, input_schema, research_policy, writing_policy,
visual_policy, asset_policy, qc_policy, output_contract, capability_requirements,
default_pipeline_version_id, content_hash, revision, test_topics, release_notes,
schema_version, created_by, created_at, published_at
"""

RUN_COLUMNS = """
id, workspace_id, ownership_type::text AS ownership_type, channel_id, status::text AS status,
idempotency_key, input_snapshot, composition_snapshot, requested_by, created_at, started_at,
completed_at, updated_at, cancel_requested_at, schema_version
"""

FULL_AI_RUN_COLUMNS = """
far.id, far.workspace_id, far.underlying_run_id, far.status,
far.provider_name, far.model_id, far.spec, far.plan, far.quote, far.billing_status,
far.authorized_amount_minor, far.incurred_amount_minor, far.requires_reconciliation,
far.idempotency_key, far.request_hash, far.estimate_fingerprint, far.created_by,
far.created_at, far.updated_at, r.status::text AS scheduler_status,
r.updated_at AS scheduler_updated_at
"""

FULL_AI_RUN_RETURNING_COLUMNS = """
id, workspace_id, underlying_run_id, status, provider_name, model_id, spec, plan, quote,
billing_status, authorized_amount_minor, incurred_amount_minor, requires_reconciliation,
idempotency_key, request_hash, estimate_fingerprint, created_by, created_at, updated_at,
NULL::text AS scheduler_status, NULL::timestamptz AS scheduler_updated_at
"""

WEBPAGE_VIDEO_RUN_COLUMNS = """
wvr.id, wvr.workspace_id, wvr.underlying_run_id, wvr.status, wvr.requested_url,
wvr.normalized_url, wvr.spec, wvr.revision, wvr.idempotency_key, wvr.request_hash,
wvr.created_by, wvr.created_at, wvr.updated_at, r.status::text AS scheduler_status,
r.updated_at AS scheduler_updated_at, r.cancel_requested_at
"""

WEBPAGE_VIDEO_RUN_RETURNING_COLUMNS = """
id, workspace_id, underlying_run_id, status, requested_url, normalized_url, spec,
revision, idempotency_key, request_hash, created_by, created_at, updated_at,
NULL::text AS scheduler_status, NULL::timestamptz AS scheduler_updated_at,
NULL::timestamptz AS cancel_requested_at
"""

WEBPAGE_CAPTURE_ATTEMPT_COLUMNS = """
id, workspace_id, webpage_video_run_id, underlying_run_id, screenshot_step_id,
attempt_number, capture_revision, outcome, requested_url, final_url, viewport_width,
viewport_height, full_page, artifact_id, sha256, media_type, metadata, error,
captured_at, created_at
"""

WEBPAGE_PILOT_FEEDBACK_COLUMNS = """
id, workspace_id, webpage_video_run_id, customer_segment, baseline_minutes,
assisted_minutes, revision_count, outcome, satisfaction_score,
willingness_to_pay_hkd, notes, revision, created_by, created_at, updated_at
"""

TEST_EXECUTION_COLUMNS = """
id, workspace_id, ownership_type::text AS ownership_type, skill_id, left_version_id,
right_version_id, topic, inputs, status, evaluator, result, error, created_by, created_at,
started_at, finished_at, updated_at, schema_version
"""

RUN_STEP_COLUMNS = """
id AS database_id, workspace_id, run_id, worker_step_id, step_key, step_type,
status::text AS status, queue_name, required_capabilities, input_snapshot,
output_summary, error, priority, available_at, attempt_count, max_attempts,
lease_owner, lease_expires_at, heartbeat_at, started_at, completed_at, created_at,
updated_at, dependencies, output_artifacts, review_required, review,
cancellation_requested_at, retry_policy, worker_revision
"""

PROFILE_COLUMNS = """
u.id AS user_id, m.workspace_id, u.email, u.display_name, u.avatar_url,
u.locale, u.timezone, u.revision, u.updated_at
"""

PREFERENCE_COLUMNS = """
workspace_id, user_id, default_language, default_aspect_ratio,
default_duration_seconds, default_visibility::text AS default_visibility,
auto_quality_check, revision, updated_at
"""

SESSION_COLUMNS = """
id, workspace_id, user_id, user_agent, created_at, expires_at, last_seen_at, revoked_at
"""

API_KEY_COLUMNS = """
id, workspace_id, name, key_prefix, scopes, created_at, expires_at, last_used_at, revoked_at
"""

ARTIFACT_COLUMNS = """
id, workspace_id, run_id, step_id, kind, schema_version, media_type,
object_key, byte_size, content_hash, metadata, created_at
"""

EVENT_COLUMNS = """
id, workspace_id, run_id, step_id, sequence, event_type, schema_version,
payload, actor_type, actor_id, occurred_at, correlation_id, causation_id
"""

ASSET_LIBRARY_COLUMNS = """
l.id, l.workspace_id, l.name, l.slug, l.description,
l.visibility::text AS visibility, l.status::text AS status, l.created_by,
l.created_at, l.updated_at,
count(a.id) FILTER (WHERE a.status<>'deleted')::integer AS asset_count,
count(a.id) FILTER (WHERE a.status='ready')::integer AS ready_asset_count
"""

ASSET_COLUMNS = """
a.id, a.workspace_id, a.library_id, a.kind, a.title, a.description, a.metadata,
a.copyright_status, a.status, a.analysis_status, a.revision, a.deleted_at,
a.created_by, a.created_at, a.updated_at,
f.id AS file_id, f.storage_provider, f.bucket, f.object_key, f.original_filename,
f.media_type, f.byte_size, f.content_hash, f.scan_status, f.width, f.height,
f.duration_ms,
COALESCE((SELECT jsonb_agg(t.name ORDER BY t.name)
  FROM asset_tags at JOIN tags t
    ON t.workspace_id=at.workspace_id AND t.id=at.tag_id
  WHERE at.workspace_id=a.workspace_id AND at.asset_id=a.id), '[]'::jsonb) AS tags
"""

DOCUMENT_SOURCE_COLUMNS = """
id, workspace_id, filename, media_type, byte_size, content_hash, storage_provider,
bucket, object_key, rights_confirmed, status, validation, revision, created_by,
created_at, uploaded_at, upload_expires_at, retention_until, legal_hold,
legal_hold_reason, legal_hold_set_by, legal_hold_set_at, deletion_requested_at,
purged_at, updated_at
"""

DOCUMENT_PURGE_REQUEST_COLUMNS = """
id, workspace_id, source_id, status, reason, delete_derived, requested_by,
attempt_count, max_attempts, next_attempt_at, last_error, source_object_deleted_at,
derived_objects_deleted_at, created_at, started_at, completed_at, updated_at
"""

CATALOG_SNAPSHOT_COLUMNS = """
id, workspace_id, library_ids, content_hash, item_count, created_by, created_at
"""

LIBRARY_BUILD_JOB_COLUMNS = """
id, workspace_id, library_id, status, stage, spec, progress, error, idempotency_key,
request_hash, revision, created_by, created_at, started_at, completed_at, updated_at
"""


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _timestamp(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _asset_library_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "visibility": row["visibility"],
        "status": row["status"],
        "asset_count": int(row.get("asset_count", 0)),
        "ready_asset_count": int(row.get("ready_asset_count", 0)),
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _asset_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "library_id": str(row["library_id"]),
        "kind": row["kind"],
        "title": row["title"],
        "description": row["description"],
        "metadata": _json_value(row["metadata"]),
        "copyright_status": row["copyright_status"],
        "status": row["status"],
        "analysis_status": row.get("analysis_status", "pending"),
        "revision": int(row.get("revision", 1)),
        "deleted_at": _timestamp(row.get("deleted_at")),
        "tags": list(_json_value(row.get("tags", []))),
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
        "file": {
            "id": str(row["file_id"]),
            "storage_provider": row["storage_provider"],
            "bucket": row["bucket"],
            "object_key": row["object_key"],
            "original_filename": row["original_filename"],
            "media_type": row["media_type"],
            "byte_size": int(row["byte_size"]),
            "content_hash": row["content_hash"],
            "scan_status": row["scan_status"],
            "width": row["width"],
            "height": row["height"],
            "duration_ms": row["duration_ms"],
        },
    }


def _document_source_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "filename": row["filename"],
        "media_type": row["media_type"],
        "byte_size": int(row["byte_size"]),
        "content_hash": row["content_hash"],
        "storage_provider": row["storage_provider"],
        "bucket": row["bucket"],
        "object_key": row["object_key"],
        "rights_confirmed": bool(row["rights_confirmed"]),
        "status": row["status"],
        "validation": _json_value(row["validation"]),
        "revision": int(row["revision"]),
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "uploaded_at": _timestamp(row["uploaded_at"]),
        "upload_expires_at": _timestamp(row["upload_expires_at"]),
        "retention_until": _timestamp(row.get("retention_until")),
        "legal_hold": bool(row.get("legal_hold", False)),
        "legal_hold_reason": row.get("legal_hold_reason"),
        "legal_hold_set_by": _uuid(row.get("legal_hold_set_by")),
        "legal_hold_set_at": _timestamp(row.get("legal_hold_set_at")),
        "deletion_requested_at": _timestamp(row.get("deletion_requested_at")),
        "purged_at": _timestamp(row.get("purged_at")),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _document_purge_request_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "source_id": str(row["source_id"]),
        "status": row["status"],
        "reason": row["reason"],
        "delete_derived": bool(row["delete_derived"]),
        "requested_by": _uuid(row["requested_by"]),
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "next_attempt_at": _timestamp(row["next_attempt_at"]),
        "last_error": _json_value(row["last_error"]),
        "source_object_deleted_at": _timestamp(row["source_object_deleted_at"]),
        "derived_objects_deleted_at": _timestamp(row["derived_objects_deleted_at"]),
        "created_at": _timestamp(row["created_at"]),
        "started_at": _timestamp(row["started_at"]),
        "completed_at": _timestamp(row["completed_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _catalog_snapshot_from_rows(
    row: Mapping[str, Any], item_rows: Sequence[Mapping[str, Any]]
) -> Resource:
    items = [
        {
            "library_id": str(item["library_id"]),
            "asset_id": str(item["asset_id"]),
            "asset_file_id": str(item["asset_file_id"]),
            "analysis_id": str(item["analysis_id"]),
            "content_hash": item["content_hash"],
        }
        for item in item_rows
    ]
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "library_ids": [str(value) for value in row["library_ids"]],
        "content_hash": row["content_hash"],
        "item_count": int(row["item_count"]),
        "items": items,
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
    }


def _library_build_job_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "library_id": str(row["library_id"]),
        "status": row["status"],
        "stage": row["stage"],
        "spec": _json_value(row["spec"]),
        "progress": _json_value(row["progress"]),
        "error": _json_value(row["error"]),
        "revision": int(row["revision"]),
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "started_at": _timestamp(row["started_at"]),
        "completed_at": _timestamp(row["completed_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _public_database_row(row: Mapping[str, Any]) -> Resource:
    result: Resource = {}
    for key, value in row.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = _timestamp(value)
        elif isinstance(value, str) and key in {
            "metadata",
            "raw_result",
            "safety",
            "quality",
            "details",
            "error",
            "before",
            "after",
        }:
            result[key] = _json_value(value)
        elif isinstance(value, tuple):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def _datetime_value(value: Any) -> datetime | None:
    """Normalize contract timestamps for asyncpg's strict timestamptz codec."""

    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Expected an ISO timestamp or datetime, got {type(value).__name__}")


def _uuid(value: Any) -> str | None:
    return None if value is None else str(value)


def _skill_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"],
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": row["ownership_type"],
        "publisher_type": row["publisher_type"],
        "publisher_name": row["publisher_name"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "visibility": row["visibility"],
        "status": row["status"],
        "current_version_id": _uuid(row["current_version_id"]),
        "forked_from_skill_id": _uuid(row["forked_from_skill_id"]),
        "revision": row["revision"],
        "created_by": str(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _version_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"],
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": row["ownership_type"],
        "skill_id": str(row["skill_id"]),
        "version": row["version"],
        "state": row["state"],
        "execution_kind": row["execution_kind"],
        "input_schema": _json_value(row["input_schema"]),
        "research_policy": _json_value(row["research_policy"]),
        "writing_policy": _json_value(row["writing_policy"]),
        "visual_policy": _json_value(row["visual_policy"]),
        "asset_policy": _json_value(row["asset_policy"]),
        "qc_policy": _json_value(row["qc_policy"]),
        "capability_requirements": _json_value(row["capability_requirements"]),
        "output_contract": _json_value(row["output_contract"]),
        "default_pipeline_version_id": _uuid(row["default_pipeline_version_id"]),
        "content_hash": row["content_hash"],
        "revision": row["revision"],
        "test_topics": _json_value(row["test_topics"]),
        "release_notes": row["release_notes"],
        "created_by": str(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "published_at": _timestamp(row["published_at"]),
    }


def _run_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"],
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": row["ownership_type"],
        "channel_id": _uuid(row["channel_id"]),
        "status": row["status"],
        "cancellation_requested_at": _timestamp(row["cancel_requested_at"]),
        "idempotency_key": row["idempotency_key"],
        "input": _json_value(row["input_snapshot"]),
        "composition_snapshot": _json_value(row["composition_snapshot"]),
        "created_by": str(row["requested_by"]),
        "created_at": _timestamp(row["created_at"]),
        "started_at": _timestamp(row["started_at"]),
        "finished_at": _timestamp(row["completed_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _full_ai_run_from_row(row: Mapping[str, Any]) -> Resource:
    scheduler_status = row.get("scheduler_status")
    status = row["status"]
    if row["requires_reconciliation"]:
        status = "reconciliation_required"
    elif scheduler_status is not None:
        scheduler_status = str(scheduler_status)
        if scheduler_status in {"succeeded", "failed", "cancelled"}:
            status = scheduler_status
        elif scheduler_status == "awaiting_review":
            status = "quality_check"
        elif status not in {"planning", "generating", "assembling", "quality_check"}:
            status = "generating" if scheduler_status in {"running", "retrying"} else "queued"
    updated_at = row["updated_at"]
    scheduler_updated_at = row.get("scheduler_updated_at")
    if scheduler_updated_at is not None and scheduler_updated_at > updated_at:
        updated_at = scheduler_updated_at
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "project_run_id": str(row["underlying_run_id"]),
        "workspace_id": str(row["workspace_id"]),
        "underlying_run_id": str(row["underlying_run_id"]),
        "status": status,
        "mode": "generated_only",
        "provider": {
            "name": row["provider_name"],
            "model_id": row["model_id"],
        },
        "spec": _json_value(row["spec"]),
        "plan": _json_value(row["plan"]),
        "quote": _json_value(row["quote"]),
        "billing": {
            "status": row["billing_status"],
            "authorized_amount_minor": int(row["authorized_amount_minor"]),
            "incurred_amount_minor": int(row["incurred_amount_minor"]),
            "requires_reconciliation": bool(row["requires_reconciliation"]),
        },
        "idempotency_key": row["idempotency_key"],
        "request_hash": row["request_hash"],
        "estimate_fingerprint": row["estimate_fingerprint"],
        "created_by": str(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(updated_at),
    }


def _webpage_video_run_from_row(row: Mapping[str, Any]) -> Resource:
    updated_at = row["updated_at"]
    scheduler_updated_at = row.get("scheduler_updated_at")
    if scheduler_updated_at is not None and scheduler_updated_at > updated_at:
        updated_at = scheduler_updated_at
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "underlying_run_id": str(row["underlying_run_id"]),
        "status": row["status"],
        "requested_url": row["requested_url"],
        "normalized_url": row["normalized_url"],
        "spec": _json_value(row["spec"]),
        "revision": int(row["revision"]),
        "idempotency_key": row["idempotency_key"],
        "request_hash": row["request_hash"],
        "created_by": str(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(updated_at),
        "scheduler_status": row.get("scheduler_status"),
        "scheduler_updated_at": _timestamp(scheduler_updated_at),
        "cancellation_requested_at": _timestamp(row.get("cancel_requested_at")),
    }


def _webpage_capture_attempt_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "webpage_video_run_id": str(row["webpage_video_run_id"]),
        "underlying_run_id": str(row["underlying_run_id"]),
        "screenshot_step_id": str(row["screenshot_step_id"]),
        "attempt_number": int(row["attempt_number"]),
        "capture_revision": int(row["capture_revision"]),
        "outcome": row["outcome"],
        "requested_url": row["requested_url"],
        "final_url": row["final_url"],
        "viewport_width": int(row["viewport_width"]),
        "viewport_height": int(row["viewport_height"]),
        "full_page": bool(row["full_page"]),
        "artifact_id": _uuid(row["artifact_id"]),
        "sha256": row["sha256"],
        "media_type": row["media_type"],
        "metadata": _json_value(row["metadata"]) or {},
        "error": _json_value(row["error"]),
        "captured_at": _timestamp(row["captured_at"]),
        "created_at": _timestamp(row["created_at"]),
    }


def _webpage_pilot_feedback_from_row(row: Mapping[str, Any]) -> Resource:
    baseline = int(row["baseline_minutes"])
    assisted = int(row["assisted_minutes"])
    saved = baseline - assisted
    reduction = round((saved / baseline) * 100, 1) if baseline else 0.0
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "webpage_video_run_id": str(row["webpage_video_run_id"]),
        "customer_segment": row["customer_segment"],
        "baseline_minutes": baseline,
        "assisted_minutes": assisted,
        "saved_minutes": saved,
        "time_reduction_percent": reduction,
        "revision_count": int(row["revision_count"]),
        "outcome": row["outcome"],
        "satisfaction_score": row["satisfaction_score"],
        "willingness_to_pay_hkd": row["willingness_to_pay_hkd"],
        "notes": row["notes"],
        "revision": int(row["revision"]),
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _test_execution_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"],
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": row["ownership_type"],
        "skill_id": str(row["skill_id"]),
        "left_version_id": str(row["left_version_id"]),
        "right_version_id": str(row["right_version_id"]),
        "topic": row["topic"],
        "inputs": _json_value(row["inputs"]),
        "status": row["status"],
        "evaluator": row["evaluator"],
        "result": _json_value(row["result"]),
        "error": _json_value(row["error"]),
        "created_by": str(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "started_at": _timestamp(row["started_at"]),
        "finished_at": _timestamp(row["finished_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _generation_batch_from_row(row: Mapping[str, Any]) -> Resource:
    counts = {
        "queued": int(row.get("queued_count", 0)),
        "running": int(row.get("running_count", 0)),
        "awaiting_review": int(row.get("awaiting_review_count", 0)),
        "succeeded": int(row.get("succeeded_count", 0)),
        "failed": int(row.get("failed_count", 0)),
        "cancelled": int(row.get("cancelled_count", 0)),
    }
    total = int(row.get("total_count", sum(counts.values())))
    terminal = counts["succeeded"] + counts["failed"] + counts["cancelled"]
    if total and counts["succeeded"] == total:
        status = "succeeded"
    elif total and terminal == total:
        status = "completed_with_errors"
    elif counts["awaiting_review"]:
        status = "awaiting_review"
    elif counts["running"]:
        status = "running"
    else:
        status = "queued"
    return {
        "schema_version": "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "status": status,
        "total_count": total,
        "status_counts": counts,
        "composition_snapshot": _json_value(row["composition_snapshot"]),
        "catalog_snapshot_id": _uuid(row.get("catalog_snapshot_id")),
        "created_by": str(row["requested_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["aggregate_updated_at"]),
    }


_BATCH_AGGREGATE_COLUMNS = """
b.id, b.workspace_id, b.name, b.composition_snapshot, b.catalog_snapshot_id,
b.requested_by,
b.created_at,
GREATEST(b.updated_at, COALESCE(MAX(r.updated_at), b.updated_at)) AS aggregate_updated_at,
COUNT(i.id)::integer AS total_count,
COUNT(i.id) FILTER (WHERE r.status = 'queued')::integer AS queued_count,
COUNT(i.id) FILTER (WHERE r.status = 'running')::integer AS running_count,
COUNT(i.id) FILTER (WHERE r.status = 'awaiting_review')::integer AS awaiting_review_count,
COUNT(i.id) FILTER (WHERE r.status = 'succeeded')::integer AS succeeded_count,
COUNT(i.id) FILTER (WHERE r.status = 'failed')::integer AS failed_count,
COUNT(i.id) FILTER (WHERE r.status = 'cancelled')::integer AS cancelled_count
"""


def _run_step_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": "1.0.0",
        "id": row["worker_step_id"],
        "workspace_id": str(row["workspace_id"]),
        "run_id": str(row["run_id"]),
        "node_key": row["step_key"],
        "operation": row["step_type"],
        "status": row["status"],
        "queue_name": row["queue_name"],
        "required_capabilities": list(row["required_capabilities"] or ()),
        "input_snapshot": _json_value(row["input_snapshot"]),
        "output_summary": _json_value(row["output_summary"]),
        "output_artifacts": _json_value(row["output_artifacts"]) or [],
        "dependencies": list(row["dependencies"] or ()),
        "attempt": int(row["attempt_count"]),
        "maximum_attempts": int(row["max_attempts"]),
        "error": _json_value(row["error"]),
        "review_required": bool(row["review_required"]),
        "review": _json_value(row["review"]),
        "lease_owner": row["lease_owner"],
        "lease_expires_at": _timestamp(row["lease_expires_at"]),
        "heartbeat_at": _timestamp(row["heartbeat_at"]),
        "next_attempt_at": _timestamp(row["available_at"]),
        "cancellation_requested_at": _timestamp(row["cancellation_requested_at"]),
        "created_at": _timestamp(row["created_at"]),
        "started_at": _timestamp(row["started_at"]),
        "finished_at": _timestamp(row["completed_at"]),
        "updated_at": _timestamp(row["updated_at"]),
        "revision": int(row["worker_revision"]),
    }


def _artifact_from_row(row: Mapping[str, Any]) -> Resource:
    metadata = _json_value(row["metadata"]) or {}
    return {
        "schema_version": row["schema_version"] or "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": "workspace",
        "run_id": str(row["run_id"]),
        "step_id": _uuid(row["step_id"]),
        "kind": row["kind"],
        "media_type": row["media_type"],
        "object_key": row["object_key"],
        "byte_size": int(row["byte_size"] or 0),
        "content_hash": row["content_hash"],
        "filename": metadata.get("filename") or str(row["object_key"]).rsplit("/", 1)[-1],
        "created_at": _timestamp(row["created_at"]),
        "expires_at": None,
    }


def _event_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"],
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": "workspace",
        "run_id": str(row["run_id"]),
        "step_id": _uuid(row["step_id"]),
        "sequence": int(row["sequence"]),
        "type": row["event_type"],
        "occurred_at": _timestamp(row["occurred_at"]),
        "actor_type": row["actor_type"],
        "actor_id": row["actor_id"],
        "correlation_id": str(row["correlation_id"]),
        "causation_id": _uuid(row["causation_id"]),
        "payload": _json_value(row["payload"]) or {},
    }


def _profile_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "user_id": str(row["user_id"]),
        "workspace_id": str(row["workspace_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "avatar_url": row["avatar_url"],
        "locale": row["locale"],
        "timezone": row["timezone"],
        "revision": row["revision"],
        "updated_at": _timestamp(row["updated_at"]),
    }


def _preferences_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "user_id": str(row["user_id"]),
        "workspace_id": str(row["workspace_id"]),
        "default_language": row["default_language"],
        "default_aspect_ratio": row["default_aspect_ratio"],
        "default_duration_seconds": row["default_duration_seconds"],
        "default_visibility": row["default_visibility"],
        "auto_quality_check": row["auto_quality_check"],
        "revision": row["revision"],
        "updated_at": _timestamp(row["updated_at"]),
    }


def _channel_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "schema_version": row["schema_version"] or "1.0.0",
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "ownership_type": row["ownership_type"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "platform": row["platform"],
        "handle": row["handle"],
        "brand_profile": _json_value(row["brand_profile"]) or {},
        "platform_connection_id": _uuid(row["platform_connection_id"]),
        "status": row["status"],
        "revision": int(row["revision"]),
        "default_composition": {
            "skill_version_id": _uuid(row["skill_version_id"]),
            "pipeline_version_id": _uuid(row["pipeline_version_id"]),
            "asset_library_ids": [str(value) for value in row["asset_library_ids"]],
            "voice_profile_id": _uuid(row["voice_profile_id"]),
            "render_preset_version_id": _uuid(row["render_preset_version_id"]),
        },
        "created_by": _uuid(row["created_by"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
    }


def _session_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "user_id": str(row["user_id"]),
        "user_agent": row["user_agent"],
        "created_at": _timestamp(row["created_at"]),
        "expires_at": _timestamp(row["expires_at"]),
        "last_seen_at": _timestamp(row["last_seen_at"]),
        "revoked_at": _timestamp(row["revoked_at"]),
    }


def _api_key_from_row(row: Mapping[str, Any]) -> Resource:
    return {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "scopes": list(row["scopes"]),
        "created_at": _timestamp(row["created_at"]),
        "expires_at": _timestamp(row["expires_at"]),
        "last_used_at": _timestamp(row["last_used_at"]),
        "revoked_at": _timestamp(row["revoked_at"]),
    }


class PostgreSQLControlRepository:
    """Transactional asyncpg adapter for the control-plane repository port.

    The constructor accepts a pool-shaped object to keep the adapter unit-testable without a
    database. ``connect`` is the production factory and imports asyncpg lazily, so importing the
    API package does not make PostgreSQL a mandatory dependency for in-memory development.
    """

    def __init__(
        self,
        pool: _Pool,
        *,
        default_user_id: UUID,
        default_workspace_id: UUID,
        default_workspace_name: str,
        default_user_email: str = "owner@local.framefactory.invalid",
        default_user_display_name: str = "Vistora Owner",
        default_workspace_slug: str = "default-workspace",
        official_skills: Sequence[Resource] = (),
        official_skill_versions: Sequence[Resource] = (),
    ) -> None:
        self._pool = pool
        self._default_user_id = default_user_id
        self._default_workspace_id = default_workspace_id
        self._default_workspace_name = default_workspace_name
        self._default_user_email = default_user_email
        self._default_user_display_name = default_user_display_name
        self._default_workspace_slug = default_workspace_slug
        self._official_skills = tuple(official_skills)
        self._official_versions = tuple(official_skill_versions)
        self._official_pipelines = tuple(getattr(official_skill_versions, "pipelines", ()))

    @classmethod
    async def connect(cls, dsn: str, **kwargs: Any) -> PostgreSQLControlRepository:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - exercised only in misconfigured runtime
            raise RuntimeError(
                "PostgreSQL persistence requires the optional 'asyncpg' package"
            ) from exc

        repository_keys = {
            "default_user_id",
            "default_workspace_id",
            "default_workspace_name",
            "default_user_email",
            "default_user_display_name",
            "default_workspace_slug",
            "official_skills",
            "official_skill_versions",
        }
        repository_args = {key: kwargs.pop(key) for key in tuple(kwargs) if key in repository_keys}

        async def configure(connection: Any) -> None:
            await connection.set_type_codec(
                "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )
            await connection.set_type_codec(
                "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
            )

        pool = await asyncpg.create_pool(dsn, init=configure, **kwargs)
        return cls(pool, **repository_args)

    async def close(self) -> None:
        await self._pool.close()

    async def healthcheck(self) -> None:
        async with self._pool.acquire() as connection:
            if await connection.fetchval("SELECT 1") != 1:
                raise RuntimeError("PostgreSQL health check returned an unexpected result")

    async def bootstrap(self) -> None:
        """Idempotently provision the configured single workspace and official catalog."""
        async with self._transaction() as connection:
            await connection.execute(
                """
                INSERT INTO users (id, email, display_name, email_verified_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (id) DO NOTHING
                """,
                self._default_user_id,
                self._default_user_email,
                self._default_user_display_name,
            )
            await connection.execute(
                """
                INSERT INTO workspaces (id, kind, slug, name, owner_user_id)
                VALUES ($1, 'personal', $2, $3, $4)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
                """,
                self._default_workspace_id,
                self._default_workspace_slug,
                self._default_workspace_name,
                self._default_user_id,
            )
            await connection.execute(
                """
                INSERT INTO workspace_members (workspace_id, user_id, role, status, joined_at)
                VALUES ($1, $2, 'owner', 'active', now())
                ON CONFLICT (workspace_id, user_id) DO UPDATE SET
                  role = 'owner', status = 'active',
                  joined_at = COALESCE(workspace_members.joined_at, now())
                """,
                self._default_workspace_id,
                self._default_user_id,
            )
            await self._bootstrap_official_catalog(connection)

    async def ensure_account(
        self,
        user_id: UUID,
        workspace_id: UUID,
        *,
        email: str,
        display_name: str,
    ) -> None:
        """Create account settings after identity bootstrap, without resetting user edits."""
        del email, display_name
        async with self._transaction() as connection:
            member = await connection.fetchval(
                """SELECT EXISTS (
                  SELECT 1 FROM workspace_members
                  WHERE workspace_id = $1 AND user_id = $2 AND status = 'active'
                )""",
                workspace_id,
                user_id,
            )
            if not member:
                raise RuntimeError("configured account is not an active workspace member")
            await connection.execute(
                """INSERT INTO creation_preferences (workspace_id, user_id)
                VALUES ($1, $2) ON CONFLICT (workspace_id, user_id) DO NOTHING""",
                workspace_id,
                user_id,
            )

    async def get_account_profile(self, workspace_id: UUID, user_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {PROFILE_COLUMNS}
                FROM users u JOIN workspace_members m ON m.user_id = u.id
                WHERE u.id = $1 AND m.workspace_id = $2 AND m.status = 'active'""",
                user_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("account_profile", str(user_id))
        return _profile_from_row(row)

    async def replace_account_profile(
        self, resource: Resource, *, expected_revision: int
    ) -> Resource:
        user_id = UUID(resource["user_id"])
        workspace_id = UUID(resource["workspace_id"])
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                """SELECT u.revision FROM users u
                JOIN workspace_members m ON m.user_id = u.id
                WHERE u.id = $1 AND m.workspace_id = $2 AND m.status = 'active'
                FOR UPDATE OF u""",
                user_id,
                workspace_id,
            )
            if current is None:
                raise NotFoundError("account_profile", str(user_id))
            self._require_revision(current["revision"], expected_revision)
            row = await connection.fetchrow(
                """UPDATE users SET display_name = $2, avatar_url = $3,
                  locale = $4, timezone = $5, revision = revision + 1
                WHERE id = $1 RETURNING id AS user_id, $6::uuid AS workspace_id,
                  email, display_name, avatar_url, locale, timezone, revision, updated_at""",
                user_id,
                resource["display_name"],
                resource["avatar_url"],
                resource["locale"],
                resource["timezone"],
                workspace_id,
            )
        assert row is not None
        return _profile_from_row(row)

    async def get_creation_preferences(self, workspace_id: UUID, user_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {PREFERENCE_COLUMNS} FROM creation_preferences
                WHERE workspace_id = $1 AND user_id = $2""",
                workspace_id,
                user_id,
            )
        if row is None:
            raise NotFoundError("creation_preferences", str(user_id))
        return _preferences_from_row(row)

    async def replace_creation_preferences(
        self, resource: Resource, *, expected_revision: int
    ) -> Resource:
        workspace_id = UUID(resource["workspace_id"])
        user_id = UUID(resource["user_id"])
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                """SELECT revision FROM creation_preferences
                WHERE workspace_id = $1 AND user_id = $2 FOR UPDATE""",
                workspace_id,
                user_id,
            )
            if current is None:
                raise NotFoundError("creation_preferences", str(user_id))
            self._require_revision(current["revision"], expected_revision)
            row = await connection.fetchrow(
                f"""UPDATE creation_preferences SET
                  default_language = $3, default_aspect_ratio = $4,
                  default_duration_seconds = $5, default_visibility = $6,
                  auto_quality_check = $7, revision = revision + 1
                WHERE workspace_id = $1 AND user_id = $2
                RETURNING {PREFERENCE_COLUMNS}""",
                workspace_id,
                user_id,
                resource["default_language"],
                resource["default_aspect_ratio"],
                resource["default_duration_seconds"],
                resource["default_visibility"],
                resource["auto_quality_check"],
            )
        assert row is not None
        return _preferences_from_row(row)

    async def list_account_sessions(self, workspace_id: UUID, user_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {SESSION_COLUMNS} FROM sessions
                WHERE workspace_id = $1 AND user_id = $2
                ORDER BY created_at DESC, id""",
                workspace_id,
                user_id,
            )
        return [_session_from_row(row) for row in rows]

    async def revoke_account_session(
        self, workspace_id: UUID, user_id: UUID, session_id: UUID
    ) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""UPDATE sessions SET revoked_at = COALESCE(revoked_at, now())
                WHERE id = $1 AND workspace_id = $2 AND user_id = $3
                RETURNING {SESSION_COLUMNS}""",
                session_id,
                workspace_id,
                user_id,
            )
        if row is None:
            raise NotFoundError("session", str(session_id))
        return _session_from_row(row)

    async def list_api_keys(self, workspace_id: UUID, user_id: UUID) -> list[Resource]:
        del user_id
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {API_KEY_COLUMNS} FROM api_keys
                WHERE workspace_id = $1 ORDER BY created_at DESC, id""",
                workspace_id,
            )
        return [_api_key_from_row(row) for row in rows]

    async def create_api_key(self, resource: Resource) -> Resource:
        async with self._pool.acquire() as connection:
            try:
                row = await connection.fetchrow(
                    f"""INSERT INTO api_keys (
                      id, workspace_id, name, key_prefix, key_hash, scopes,
                      created_by, created_at, expires_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING {API_KEY_COLUMNS}""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    resource["name"],
                    resource["key_prefix"],
                    resource["key_hash"],
                    resource["scopes"],
                    UUID(resource["created_by"]),
                    _datetime_value(resource["created_at"]),
                    _datetime_value(resource["expires_at"]),
                )
            except Exception as exc:
                self._raise_database_error(exc, "api_key", resource)
        assert row is not None
        return _api_key_from_row(row)

    async def revoke_api_key(self, workspace_id: UUID, user_id: UUID, key_id: UUID) -> Resource:
        del user_id
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now())
                WHERE id = $1 AND workspace_id = $2 RETURNING {API_KEY_COLUMNS}""",
                key_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("api_key", str(key_id))
        return _api_key_from_row(row)

    async def list_skills(self, workspace_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {SKILL_COLUMNS} FROM skills
                WHERE workspace_id = $1 OR ownership_type = 'system'
                ORDER BY created_at, id""",
                workspace_id,
            )
        return [_skill_from_row(row) for row in rows]

    async def list_channels(
        self,
        workspace_id: UUID,
        *,
        search: str | None = None,
        platform: str | None = None,
        status: str | None = None,
    ) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {CHANNEL_COLUMNS}
                FROM channels c JOIN channel_defaults d
                  ON d.channel_id = c.id AND d.workspace_id = c.workspace_id
                WHERE c.workspace_id = $1
                  AND ($2::text IS NULL OR strpos(lower(concat_ws(' ',
                    c.name, c.slug, c.description, c.handle)), lower($2)) > 0)
                  AND ($3::text IS NULL OR lower(c.platform) = lower($3))
                  AND ($4::text IS NULL OR c.status::text = $4)
                ORDER BY c.created_at, c.id""",
                workspace_id,
                search,
                platform,
                status,
            )
        return [_channel_from_row(row) for row in rows]

    async def get_platform_connection(self, workspace_id: UUID, connection_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT id, workspace_id, platform, name, status::text AS status
                FROM platform_connections WHERE id = $1 AND workspace_id = $2""",
                connection_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("platform_connection", str(connection_id))
        return {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "platform": row["platform"],
            "name": row["name"],
            "status": row["status"],
        }

    async def get_channel(self, workspace_id: UUID, channel_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await self._select_channel(connection, workspace_id, channel_id)
        if row is None:
            raise NotFoundError("channel", str(channel_id))
        return _channel_from_row(row)

    async def create_channel(self, resource: Resource) -> Resource:
        async with self._transaction() as connection:
            return await self._insert_channel(connection, resource)

    async def create_channel_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/channels",
                response_type="channel",
                create=lambda: self._insert_channel(connection, resource),
            )

    async def _insert_channel(self, connection: _Connection, resource: Resource) -> Resource:
        composition = resource["default_composition"]
        try:
            await connection.execute(
                """INSERT INTO channels (
                  id, workspace_id, ownership_type, name, slug, description, platform,
                  handle, brand_profile, platform_connection_id, status, revision,
                  schema_version, created_by, created_at, updated_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                  $14, $15, $16
                )""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                resource["ownership_type"],
                resource["name"],
                resource["slug"],
                resource["description"],
                resource["platform"],
                resource["handle"],
                resource["brand_profile"],
                self._optional_uuid(resource["platform_connection_id"]),
                resource["status"],
                resource["revision"],
                resource["schema_version"],
                self._optional_uuid(resource["created_by"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["updated_at"]),
            )
            await connection.execute(
                """INSERT INTO channel_defaults (
                  channel_id, workspace_id, skill_version_id, voice_profile_id,
                  render_preset_version_id, pipeline_version_id, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                UUID(composition["skill_version_id"]),
                self._optional_uuid(composition["voice_profile_id"]),
                self._optional_uuid(composition["render_preset_version_id"]),
                UUID(composition["pipeline_version_id"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["updated_at"]),
            )
            await self._replace_channel_asset_libraries(connection, resource)
            row = await self._select_channel(
                connection, UUID(resource["workspace_id"]), UUID(resource["id"])
            )
        except Exception as exc:
            self._raise_database_error(exc, "channel", resource)
        assert row is not None
        return _channel_from_row(row)

    async def replace_channel(self, resource: Resource, *, expected_revision: int) -> Resource:
        composition = resource["default_composition"]
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                """SELECT revision FROM channels
                WHERE id = $1 AND workspace_id = $2 FOR UPDATE""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
            )
            if current is None:
                raise NotFoundError("channel", resource["id"])
            self._require_revision(int(current["revision"]), expected_revision)
            try:
                await connection.execute(
                    """UPDATE channels SET
                      name = $3, slug = $4, description = $5, platform = $6, handle = $7,
                      brand_profile = $8, platform_connection_id = $9, status = $10,
                      revision = $11, schema_version = $12, updated_at = $13
                    WHERE id = $1 AND workspace_id = $2""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    resource["name"],
                    resource["slug"],
                    resource["description"],
                    resource["platform"],
                    resource["handle"],
                    resource["brand_profile"],
                    self._optional_uuid(resource["platform_connection_id"]),
                    resource["status"],
                    int(current["revision"]) + 1,
                    resource["schema_version"],
                    _datetime_value(resource["updated_at"]),
                )
                await connection.execute(
                    """UPDATE channel_defaults SET
                      skill_version_id = $3, pipeline_version_id = $4,
                      voice_profile_id = $5, render_preset_version_id = $6,
                      updated_at = $7
                    WHERE channel_id = $1 AND workspace_id = $2""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    UUID(composition["skill_version_id"]),
                    UUID(composition["pipeline_version_id"]),
                    self._optional_uuid(composition["voice_profile_id"]),
                    self._optional_uuid(composition["render_preset_version_id"]),
                    _datetime_value(resource["updated_at"]),
                )
                await self._replace_channel_asset_libraries(connection, resource)
                row = await self._select_channel(
                    connection, UUID(resource["workspace_id"]), UUID(resource["id"])
                )
            except Exception as exc:
                self._raise_database_error(exc, "channel", resource)
            assert row is not None
            return _channel_from_row(row)

    async def archive_channel(
        self, workspace_id: UUID, channel_id: UUID, *, expected_revision: int
    ) -> Resource:
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                """SELECT revision FROM channels
                WHERE id = $1 AND workspace_id = $2 FOR UPDATE""",
                channel_id,
                workspace_id,
            )
            if current is None:
                raise NotFoundError("channel", str(channel_id))
            self._require_revision(int(current["revision"]), expected_revision)
            await connection.execute(
                """UPDATE channels SET status = 'archived', revision = revision + 1
                WHERE id = $1 AND workspace_id = $2""",
                channel_id,
                workspace_id,
            )
            row = await self._select_channel(connection, workspace_id, channel_id)
            assert row is not None
            return _channel_from_row(row)

    async def get_skill(self, workspace_id: UUID, skill_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {SKILL_COLUMNS} FROM skills
                WHERE id = $1 AND (workspace_id = $2 OR ownership_type = 'system')""",
                skill_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("skill", str(skill_id))
        return _skill_from_row(row)

    async def create_skill(self, resource: Resource) -> Resource:
        async with self._transaction() as connection:
            return await self._insert_skill(connection, resource)

    async def create_skill_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/skills",
                response_type="skill",
                create=lambda: self._insert_skill(connection, resource),
            )

    async def replace_skill(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource:
        async with self._transaction() as connection:
            row = await connection.fetchrow(
                """SELECT revision, ownership_type::text AS ownership_type
                FROM skills WHERE id = $1 FOR UPDATE""",
                UUID(resource["id"]),
            )
            if row is None:
                raise NotFoundError("skill", resource["id"])
            if row["ownership_type"] == "system":
                raise ConflictError(
                    "SYSTEM_SKILL_IMMUTABLE", "System-owned skills cannot be replaced"
                )
            self._require_revision(row["revision"], expected_revision)
            try:
                updated = await connection.fetchrow(
                    f"""UPDATE skills SET
                      publisher_name = $2, name = $3, slug = $4, description = $5,
                      visibility = $6, status = $7, current_version_id = $8,
                      forked_from_skill_id = $9, revision = $10, schema_version = $11,
                      updated_at = $12
                    WHERE id = $1 RETURNING {SKILL_COLUMNS}""",
                    UUID(resource["id"]),
                    resource["publisher_name"],
                    resource["name"],
                    resource["slug"],
                    resource["description"],
                    resource["visibility"],
                    resource["status"],
                    self._optional_uuid(resource["current_version_id"]),
                    self._optional_uuid(resource["forked_from_skill_id"]),
                    resource.get("revision", row["revision"] + 1),
                    resource["schema_version"],
                    _datetime_value(resource["updated_at"]),
                )
            except Exception as exc:
                self._raise_database_error(exc, "skill", resource)
            assert updated is not None
            return _skill_from_row(updated)

    async def delete_skill(self, workspace_id: UUID, skill_id: UUID) -> None:
        async with self._transaction() as connection:
            skill = await connection.fetchrow(
                """SELECT ownership_type::text AS ownership_type, current_version_id
                FROM skills WHERE id = $1 AND (workspace_id = $2 OR ownership_type = 'system')
                FOR UPDATE""",
                skill_id,
                workspace_id,
            )
            if skill is None:
                raise NotFoundError("skill", str(skill_id))
            if skill["ownership_type"] == "system":
                raise ConflictError(
                    "SYSTEM_SKILL_IMMUTABLE", "System-owned skills cannot be deleted"
                )
            if skill["current_version_id"] is not None:
                raise ConflictError(
                    "PUBLISHED_SKILL_IMMUTABLE", "A Skill with published history cannot be deleted"
                )
            in_use = await connection.fetchval(
                """SELECT EXISTS (
                  SELECT 1 FROM runs r JOIN skill_versions v ON v.id = r.skill_version_id
                  WHERE v.skill_id = $1
                )""",
                skill_id,
            )
            if in_use:
                raise ConflictError("SKILL_IN_USE", "A Run references this Skill")
            try:
                await connection.execute("DELETE FROM skill_versions WHERE skill_id = $1", skill_id)
                await connection.execute("DELETE FROM skills WHERE id = $1", skill_id)
            except Exception as exc:
                self._raise_database_error(exc, "skill", {"id": str(skill_id)})

    async def list_skill_versions(
        self, workspace_id: UUID, skill_id: UUID | None = None
    ) -> list[Resource]:
        args: tuple[Any, ...] = (workspace_id,)
        skill_filter = ""
        if skill_id is not None:
            skill_filter = " AND skill_id = $2"
            args = (workspace_id, skill_id)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {VERSION_COLUMNS} FROM skill_versions
                WHERE (workspace_id = $1 OR ownership_type = 'system'){skill_filter}
                ORDER BY created_at, id""",
                *args,
            )
        return [_version_from_row(row) for row in rows]

    async def get_skill_version(self, workspace_id: UUID, version_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {VERSION_COLUMNS} FROM skill_versions
                WHERE id = $1 AND (workspace_id = $2 OR ownership_type = 'system')""",
                version_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("skill_version", str(version_id))
        return _version_from_row(row)

    async def create_skill_version(self, resource: Resource) -> Resource:
        async with self._transaction() as connection:
            return await self._insert_version(connection, resource)

    async def create_skill_version_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/skill-versions",
                response_type="skill_version",
                create=lambda: self._insert_version(connection, resource),
            )

    async def replace_skill_version(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource:
        async with self._transaction() as connection:
            row = await connection.fetchrow(
                """SELECT revision, state::text AS state
                FROM skill_versions WHERE id = $1 FOR UPDATE""",
                UUID(resource["id"]),
            )
            if row is None:
                raise NotFoundError("skill_version", resource["id"])
            self._require_revision(row["revision"], expected_revision)
            try:
                updated = await connection.fetchrow(
                    f"""UPDATE skill_versions SET
                      state = $2, input_schema = $3, research_policy = $4, writing_policy = $5,
                      visual_policy = $6, asset_policy = $7, qc_policy = $8, output_contract = $9,
                      capability_requirements = $10, default_pipeline_version_id = $11,
                      content_hash = $12, revision = $13, test_topics = $14, release_notes = $15,
                      published_at = $16
                    WHERE id = $1 RETURNING {VERSION_COLUMNS}""",
                    UUID(resource["id"]),
                    resource["state"],
                    resource["input_schema"],
                    resource["research_policy"],
                    resource["writing_policy"],
                    resource["visual_policy"],
                    resource["asset_policy"],
                    resource["qc_policy"],
                    resource["output_contract"],
                    resource["capability_requirements"],
                    self._optional_uuid(resource["default_pipeline_version_id"]),
                    resource["content_hash"],
                    resource.get("revision", row["revision"] + 1),
                    resource.get("test_topics", []),
                    resource.get("release_notes", ""),
                    _datetime_value(resource["published_at"]),
                )
            except Exception as exc:
                self._raise_database_error(exc, "skill_version", resource)
            assert updated is not None
            return _version_from_row(updated)

    async def delete_skill_version(
        self, workspace_id: UUID, version_id: UUID, *, expected_revision: int
    ) -> None:
        async with self._transaction() as connection:
            row = await connection.fetchrow(
                """SELECT revision, state::text AS state FROM skill_versions
                WHERE id = $1 AND (workspace_id = $2 OR ownership_type = 'system') FOR UPDATE""",
                version_id,
                workspace_id,
            )
            if row is None:
                raise NotFoundError("skill_version", str(version_id))
            self._require_revision(row["revision"], expected_revision)
            if row["state"] != "draft":
                raise ConflictError(
                    "IMMUTABLE_SKILL_VERSION", "Only draft SkillVersions can be deleted"
                )
            in_use = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE skill_version_id = $1)", version_id
            )
            if in_use:
                raise ConflictError("SKILL_VERSION_IN_USE", "The draft is referenced by a Run")
            try:
                await connection.execute("DELETE FROM skill_versions WHERE id = $1", version_id)
            except Exception as exc:
                self._raise_database_error(exc, "skill_version", {"id": str(version_id)})

    async def fork_skill_idempotently(
        self,
        skill: Resource,
        version: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[tuple[Resource, Resource], bool]:
        async with self._transaction() as connection:

            async def create() -> dict[str, Resource]:
                created_skill = await self._insert_skill(connection, skill)
                created_version = await self._insert_version(connection, version)
                return {"skill": created_skill, "version": created_version}

            response, created = await self._idempotent(
                connection,
                workspace_id=UUID(skill["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/skills/{skill.get('forked_from_skill_id')}/fork",
                response_type="skill_fork",
                create=create,
            )
            return (response["skill"], response["version"]), created

    async def list_runs(self, workspace_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE workspace_id = $1 ORDER BY created_at, id",
                workspace_id,
            )
        return [_run_from_row(row) for row in rows]

    async def get_pipeline_version(self, workspace_id: UUID, version_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT pv.id, pv.workspace_id, pv.version, pv.state::text AS state,
                  pv.graph, pv.capability_requirements, pv.content_hash, pv.created_by,
                  pv.created_at, pv.published_at, p.name, p.slug, p.description,
                  p.visibility::text AS visibility, p.status::text AS status,
                  w.kind::text AS workspace_kind
                FROM pipeline_versions pv
                JOIN pipelines p ON p.id = pv.pipeline_id
                JOIN workspaces w ON w.id = pv.workspace_id
                WHERE pv.id = $1 AND (
                  pv.workspace_id = $2 OR (
                    w.kind = 'system' AND p.visibility = 'public_readonly'
                  )
                )""",
                version_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("pipeline_version", str(version_id))
        graph = _json_value(row["graph"])
        return {
            "id": str(row["id"]),
            "workspace_id": str(row["workspace_id"]),
            "ownership_type": ("system" if row["workspace_kind"] == "system" else "workspace"),
            "name": row["name"],
            "slug": row["slug"],
            "description": row["description"],
            "visibility": row["visibility"],
            "status": row["status"],
            "version": row["version"],
            "state": row["state"],
            "nodes": graph.get("nodes", []),
            "capability_requirements": _json_value(row["capability_requirements"]),
            "content_hash": row["content_hash"],
            "created_by": str(row["created_by"]),
            "created_at": _timestamp(row["created_at"]),
            "published_at": _timestamp(row["published_at"]),
        }

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE id = $1 AND workspace_id = $2",
                run_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("run", str(run_id))
        return _run_from_row(row)

    async def list_generation_batches(self, workspace_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {_BATCH_AGGREGATE_COLUMNS}
                FROM generation_batches b
                LEFT JOIN generation_batch_items i
                  ON i.workspace_id=b.workspace_id AND i.batch_id=b.id
                LEFT JOIN runs r
                  ON r.workspace_id=i.workspace_id AND r.id=i.run_id
                WHERE b.workspace_id=$1
                GROUP BY b.id
                ORDER BY b.created_at DESC, b.id DESC""",
                workspace_id,
            )
        return [_generation_batch_from_row(row) for row in rows]

    async def get_generation_batch(self, workspace_id: UUID, batch_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {_BATCH_AGGREGATE_COLUMNS}
                FROM generation_batches b
                LEFT JOIN generation_batch_items i
                  ON i.workspace_id=b.workspace_id AND i.batch_id=b.id
                LEFT JOIN runs r
                  ON r.workspace_id=i.workspace_id AND r.id=i.run_id
                WHERE b.workspace_id=$1 AND b.id=$2
                GROUP BY b.id""",
                workspace_id,
                batch_id,
            )
        if row is None:
            raise NotFoundError("generation_batch", str(batch_id))
        return _generation_batch_from_row(row)

    async def list_generation_batch_items(
        self,
        workspace_id: UUID,
        batch_id: UUID,
        *,
        status: str | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Resource], int]:
        needle = (search or "").strip() or None
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT true FROM generation_batches WHERE workspace_id=$1 AND id=$2",
                workspace_id,
                batch_id,
            )
            if not exists:
                raise NotFoundError("generation_batch", str(batch_id))
            total = await connection.fetchval(
                """SELECT COUNT(*) FROM generation_batch_items i
                JOIN runs r ON r.workspace_id=i.workspace_id AND r.id=i.run_id
                WHERE i.workspace_id=$1 AND i.batch_id=$2
                  AND ($3::text IS NULL OR r.status::text=$3)
                  AND ($4::text IS NULL OR i.label ILIKE '%' || $4 || '%'
                    OR i.run_id::text ILIKE '%' || $4 || '%')""",
                workspace_id,
                batch_id,
                status,
                needle,
            )
            rows = await connection.fetch(
                """SELECT i.id, i.workspace_id, i.batch_id, i.run_id, i.ordinal,
                  i.label, i.input_snapshot, i.created_at, r.status::text AS status,
                  r.cancel_requested_at, r.updated_at
                FROM generation_batch_items i
                JOIN runs r ON r.workspace_id=i.workspace_id AND r.id=i.run_id
                WHERE i.workspace_id=$1 AND i.batch_id=$2
                  AND ($3::text IS NULL OR r.status::text=$3)
                  AND ($4::text IS NULL OR i.label ILIKE '%' || $4 || '%'
                    OR i.run_id::text ILIKE '%' || $4 || '%')
                ORDER BY i.ordinal
                LIMIT $5 OFFSET $6""",
                workspace_id,
                batch_id,
                status,
                needle,
                limit,
                offset,
            )
        return (
            [
                {
                    "schema_version": "1.0.0",
                    "id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "batch_id": str(row["batch_id"]),
                    "run_id": str(row["run_id"]),
                    "ordinal": int(row["ordinal"]),
                    "label": row["label"],
                    "input": _json_value(row["input_snapshot"]),
                    "status": row["status"],
                    "cancellation_requested_at": _timestamp(row["cancel_requested_at"]),
                    "created_at": _timestamp(row["created_at"]),
                    "updated_at": _timestamp(row["updated_at"]),
                }
                for row in rows
            ],
            int(total or 0),
        )

    async def create_generation_batch_idempotently(
        self,
        batch: Resource,
        runs: list[Resource],
        items: list[Resource],
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def create() -> Resource:
                await connection.execute(
                    """INSERT INTO generation_batches (
                      id, workspace_id, name, composition_snapshot, catalog_snapshot_id,
                      requested_by, created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    UUID(batch["id"]),
                    UUID(batch["workspace_id"]),
                    batch["name"],
                    batch["composition_snapshot"],
                    self._optional_uuid(batch.get("catalog_snapshot_id")),
                    UUID(batch["created_by"]),
                    _datetime_value(batch["created_at"]),
                    _datetime_value(batch["updated_at"]),
                )
                for run, item in zip(runs, items, strict=True):
                    await self._insert_run(connection, run)
                    await connection.execute(
                        """INSERT INTO generation_batch_items (
                          id, workspace_id, batch_id, run_id, ordinal, label,
                          input_snapshot, created_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                        UUID(item["id"]),
                        UUID(item["workspace_id"]),
                        UUID(item["batch_id"]),
                        UUID(item["run_id"]),
                        int(item["ordinal"]),
                        item["label"],
                        item["input"],
                        _datetime_value(item["created_at"]),
                    )
                return {
                    **batch,
                    "status": "queued",
                    "total_count": len(items),
                    "status_counts": {
                        "queued": len(items),
                        "running": 0,
                        "awaiting_review": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "cancelled": 0,
                    },
                }

            return await self._idempotent(
                connection,
                workspace_id=UUID(batch["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/generation-batches",
                response_type="generation_batch",
                create=create,
                response_status=202,
            )

    async def create_run_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/runs",
                response_type="run",
                create=lambda: self._insert_run(connection, resource),
            )

    async def create_full_ai_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def create() -> Resource:
                await self._insert_run(connection, scheduler_run)
                row = await connection.fetchrow(
                    f"""INSERT INTO full_ai_runs (
                      id, workspace_id, underlying_run_id, status, provider_name,
                      model_id, spec, plan, quote, billing_status, authorized_amount_minor,
                      incurred_amount_minor, requires_reconciliation, idempotency_key,
                      request_hash, estimate_fingerprint, created_by, created_at, updated_at
                    ) VALUES (
                      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                      $14, $15, $16, $17, $18, $19
                    ) RETURNING {FULL_AI_RUN_RETURNING_COLUMNS}""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    UUID(resource["underlying_run_id"]),
                    resource["status"],
                    resource["provider"]["name"],
                    resource["provider"]["model_id"],
                    resource["spec"],
                    resource["plan"],
                    resource["quote"],
                    resource["billing"]["status"],
                    resource["billing"]["authorized_amount_minor"],
                    resource["billing"]["incurred_amount_minor"],
                    resource["billing"]["requires_reconciliation"],
                    resource["idempotency_key"],
                    resource["request_hash"],
                    resource["estimate_fingerprint"],
                    UUID(resource["created_by"]),
                    _datetime_value(resource["created_at"]),
                    _datetime_value(resource["updated_at"]),
                )
                if row is None:  # pragma: no cover - INSERT RETURNING contract
                    raise RuntimeError("Full-AI Run insert returned no row")
                return _full_ai_run_from_row(row)

            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/full-ai/runs",
                response_type="full_ai_run",
                create=create,
            )

    async def find_full_ai_run_by_idempotency_key(
        self, workspace_id: UUID, idempotency_key: str
    ) -> Resource | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {FULL_AI_RUN_COLUMNS}
                FROM full_ai_runs far
                JOIN runs r
                  ON r.workspace_id = far.workspace_id
                 AND r.id = far.underlying_run_id
                WHERE far.workspace_id = $1 AND far.idempotency_key = $2""",
                workspace_id,
                idempotency_key,
            )
        return None if row is None else _full_ai_run_from_row(row)

    async def get_full_ai_run(self, workspace_id: UUID, full_ai_run_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {FULL_AI_RUN_COLUMNS}
                FROM full_ai_runs far
                JOIN runs r
                  ON r.workspace_id = far.workspace_id
                 AND r.id = far.underlying_run_id
                WHERE far.workspace_id = $1 AND far.id = $2""",
                workspace_id,
                full_ai_run_id,
            )
        if row is None:
            raise NotFoundError("full_ai_run", str(full_ai_run_id))
        return _full_ai_run_from_row(row)

    async def create_webpage_video_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
        active_run_limit: int,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def create() -> Resource:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 90521))",
                    resource["workspace_id"],
                )
                active_count = int(
                    await connection.fetchval(
                        """SELECT count(*)
                        FROM webpage_video_runs wvr
                        JOIN runs r
                          ON r.workspace_id = wvr.workspace_id
                         AND r.id = wvr.underlying_run_id
                        WHERE wvr.workspace_id = $1
                          AND r.status IN ('queued', 'running', 'awaiting_review')""",
                        UUID(resource["workspace_id"]),
                    )
                    or 0
                )
                if active_count >= active_run_limit:
                    raise ConflictError(
                        "WEBPAGE_VIDEO_ACTIVE_RUN_LIMIT_REACHED",
                        ("The workspace has reached its concurrent webpage-video Run limit"),
                        active_runs=active_count,
                        max_active_runs=active_run_limit,
                    )
                await self._insert_run(connection, scheduler_run)
                try:
                    row = await connection.fetchrow(
                        f"""INSERT INTO webpage_video_runs (
                          id, workspace_id, underlying_run_id, status, requested_url,
                          normalized_url, spec, revision, idempotency_key, request_hash,
                          created_by, created_at, updated_at
                        ) VALUES (
                          $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
                        ) RETURNING {WEBPAGE_VIDEO_RUN_RETURNING_COLUMNS}""",
                        UUID(resource["id"]),
                        UUID(resource["workspace_id"]),
                        UUID(resource["underlying_run_id"]),
                        resource["status"],
                        resource["requested_url"],
                        resource["normalized_url"],
                        resource["spec"],
                        int(resource.get("revision", 1)),
                        resource["idempotency_key"],
                        resource["request_hash"],
                        UUID(resource["created_by"]),
                        _datetime_value(resource["created_at"]),
                        _datetime_value(resource["updated_at"]),
                    )
                except Exception as exc:
                    self._raise_database_error(exc, "webpage_video_run", resource)
                    raise  # pragma: no cover - _raise_database_error always raises
                if row is None:  # pragma: no cover - INSERT RETURNING contract
                    raise RuntimeError("Webpage-video Run insert returned no row")
                return _webpage_video_run_from_row(row)

            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/webpage-video/runs",
                response_type="webpage_video_run",
                create=create,
            )

    async def count_active_webpage_video_runs(self, workspace_id: UUID) -> int:
        async with self._pool.acquire() as connection:
            value = await connection.fetchval(
                """SELECT count(*)
                FROM webpage_video_runs wvr
                JOIN runs r
                  ON r.workspace_id = wvr.workspace_id
                 AND r.id = wvr.underlying_run_id
                WHERE wvr.workspace_id = $1
                  AND r.status IN ('queued', 'running', 'awaiting_review')""",
                workspace_id,
            )
        return int(value or 0)

    async def get_webpage_video_run(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {WEBPAGE_VIDEO_RUN_COLUMNS}
                FROM webpage_video_runs wvr
                JOIN runs r
                  ON r.workspace_id = wvr.workspace_id
                 AND r.id = wvr.underlying_run_id
                WHERE wvr.workspace_id = $1 AND wvr.id = $2""",
                workspace_id,
                webpage_video_run_id,
            )
        if row is None:
            raise NotFoundError("webpage_video_run", str(webpage_video_run_id))
        return _webpage_video_run_from_row(row)

    async def append_webpage_capture_attempt(self, resource: Resource) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            try:
                row = await connection.fetchrow(
                    f"""INSERT INTO webpage_capture_attempts (
                      id, workspace_id, webpage_video_run_id, underlying_run_id,
                      screenshot_step_id, attempt_number, capture_revision, outcome,
                      requested_url, final_url, viewport_width, viewport_height, full_page,
                      artifact_id, sha256, media_type, metadata, error, captured_at, created_at
                    ) VALUES (
                      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                      $14, $15, $16, $17, $18, $19, $20
                    ) ON CONFLICT DO NOTHING
                    RETURNING {WEBPAGE_CAPTURE_ATTEMPT_COLUMNS}""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    UUID(resource["webpage_video_run_id"]),
                    UUID(resource["underlying_run_id"]),
                    UUID(resource["screenshot_step_id"]),
                    int(resource["attempt_number"]),
                    int(resource["capture_revision"]),
                    resource["outcome"],
                    resource["requested_url"],
                    resource.get("final_url"),
                    int(resource["viewport_width"]),
                    int(resource["viewport_height"]),
                    bool(resource.get("full_page", False)),
                    self._optional_uuid(resource.get("artifact_id")),
                    resource.get("sha256"),
                    resource.get("media_type"),
                    resource.get("metadata", {}),
                    resource.get("error"),
                    _datetime_value(resource.get("captured_at")),
                    _datetime_value(resource["created_at"]),
                )
            except Exception as exc:
                self._raise_database_error(exc, "webpage_capture_attempt", resource)
                raise  # pragma: no cover - _raise_database_error always raises
            if row is not None:
                return _webpage_capture_attempt_from_row(row), True

            existing_row = await connection.fetchrow(
                f"""SELECT {WEBPAGE_CAPTURE_ATTEMPT_COLUMNS}
                FROM webpage_capture_attempts
                WHERE workspace_id = $1 AND webpage_video_run_id = $2
                  AND (
                    attempt_number = $3
                    OR ($4::uuid IS NOT NULL AND artifact_id = $4::uuid)
                  )
                ORDER BY attempt_number DESC
                LIMIT 1""",
                UUID(resource["workspace_id"]),
                UUID(resource["webpage_video_run_id"]),
                int(resource["attempt_number"]),
                self._optional_uuid(resource.get("artifact_id")),
            )
            if existing_row is None:  # pragma: no cover - unique conflict contract
                raise ConflictError(
                    "WEBPAGE_CAPTURE_ATTEMPT_CONFLICT",
                    "The immutable capture attempt conflicted with another record",
                )
            existing = _webpage_capture_attempt_from_row(existing_row)
            comparable_keys = (
                "workspace_id",
                "webpage_video_run_id",
                "underlying_run_id",
                "screenshot_step_id",
                "attempt_number",
                "capture_revision",
                "outcome",
                "requested_url",
                "final_url",
                "viewport_width",
                "viewport_height",
                "full_page",
                "artifact_id",
                "sha256",
                "media_type",
                "metadata",
                "error",
                "captured_at",
            )
            expected = {
                **resource,
                "workspace_id": str(resource["workspace_id"]),
                "webpage_video_run_id": str(resource["webpage_video_run_id"]),
                "underlying_run_id": str(resource["underlying_run_id"]),
                "screenshot_step_id": str(resource["screenshot_step_id"]),
                "artifact_id": (
                    str(resource["artifact_id"])
                    if resource.get("artifact_id") is not None
                    else None
                ),
                "full_page": bool(resource.get("full_page", False)),
                "metadata": resource.get("metadata", {}),
                "error": resource.get("error"),
                "captured_at": _timestamp(_datetime_value(resource.get("captured_at"))),
            }
            if any(existing.get(key) != expected.get(key) for key in comparable_keys):
                raise ConflictError(
                    "WEBPAGE_CAPTURE_ATTEMPT_CONFLICT",
                    "A capture attempt was replayed with different immutable evidence",
                    attempt_number=resource["attempt_number"],
                )
            return existing, False

    async def list_webpage_capture_attempts(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> list[Resource]:
        await self.get_webpage_video_run(workspace_id, webpage_video_run_id)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {WEBPAGE_CAPTURE_ATTEMPT_COLUMNS}
                FROM webpage_capture_attempts
                WHERE workspace_id = $1 AND webpage_video_run_id = $2
                ORDER BY attempt_number, created_at, id""",
                workspace_id,
                webpage_video_run_id,
            )
        return [_webpage_capture_attempt_from_row(row) for row in rows]

    async def get_webpage_pilot_feedback(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {WEBPAGE_PILOT_FEEDBACK_COLUMNS}
                FROM webpage_video_pilot_feedback
                WHERE workspace_id = $1 AND webpage_video_run_id = $2""",
                workspace_id,
                webpage_video_run_id,
            )
        return None if row is None else _webpage_pilot_feedback_from_row(row)

    async def list_webpage_pilot_feedback(
        self, workspace_id: UUID, *, limit: int
    ) -> tuple[list[Resource], int]:
        async with self._pool.acquire() as connection:
            total = await connection.fetchval(
                """SELECT count(*) FROM webpage_video_pilot_feedback
                WHERE workspace_id = $1""",
                workspace_id,
            )
            rows = await connection.fetch(
                f"""SELECT {WEBPAGE_PILOT_FEEDBACK_COLUMNS}
                FROM webpage_video_pilot_feedback
                WHERE workspace_id = $1
                ORDER BY updated_at DESC, id DESC
                LIMIT $2""",
                workspace_id,
                limit,
            )
        return [_webpage_pilot_feedback_from_row(row) for row in rows], int(total or 0)

    async def upsert_webpage_pilot_feedback_idempotently(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            existed = await connection.fetchval(
                """SELECT true FROM webpage_video_pilot_feedback
                WHERE workspace_id = $1 AND webpage_video_run_id = $2""",
                UUID(resource["workspace_id"]),
                UUID(resource["webpage_video_run_id"]),
            )
            if not existed and expected_revision != 0:
                raise PreconditionFailedError(0, expected_revision)

            async def save() -> Resource:
                row = await connection.fetchrow(
                    f"""INSERT INTO webpage_video_pilot_feedback (
                      id, workspace_id, webpage_video_run_id, customer_segment,
                      baseline_minutes, assisted_minutes, revision_count, outcome,
                      satisfaction_score, willingness_to_pay_hkd, notes, revision,
                      created_by, created_at, updated_at
                    ) VALUES (
                      $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 1, $12, $13, $14
                    )
                    ON CONFLICT (workspace_id, webpage_video_run_id) DO UPDATE SET
                      customer_segment = EXCLUDED.customer_segment,
                      baseline_minutes = EXCLUDED.baseline_minutes,
                      assisted_minutes = EXCLUDED.assisted_minutes,
                      revision_count = EXCLUDED.revision_count,
                      outcome = EXCLUDED.outcome,
                      satisfaction_score = EXCLUDED.satisfaction_score,
                      willingness_to_pay_hkd = EXCLUDED.willingness_to_pay_hkd,
                      notes = EXCLUDED.notes,
                      revision = webpage_video_pilot_feedback.revision + 1,
                      updated_at = EXCLUDED.updated_at
                    WHERE webpage_video_pilot_feedback.revision = $15
                    RETURNING {WEBPAGE_PILOT_FEEDBACK_COLUMNS}""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    UUID(resource["webpage_video_run_id"]),
                    resource["customer_segment"],
                    int(resource["baseline_minutes"]),
                    int(resource["assisted_minutes"]),
                    int(resource["revision_count"]),
                    resource["outcome"],
                    resource.get("satisfaction_score"),
                    resource.get("willingness_to_pay_hkd"),
                    resource.get("notes"),
                    UUID(resource["created_by"]),
                    _datetime_value(resource["created_at"]),
                    _datetime_value(resource["updated_at"]),
                    expected_revision,
                )
                if row is None:
                    current_revision = await connection.fetchval(
                        """SELECT revision FROM webpage_video_pilot_feedback
                        WHERE workspace_id = $1 AND webpage_video_run_id = $2""",
                        UUID(resource["workspace_id"]),
                        UUID(resource["webpage_video_run_id"]),
                    )
                    raise PreconditionFailedError(int(current_revision or 0), expected_revision)
                return _webpage_pilot_feedback_from_row(row)

            saved, created = await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=(
                    f"/v1/webpage-video/runs/{resource['webpage_video_run_id']}/pilot-feedback"
                ),
                response_type="webpage_video_pilot_feedback",
                create=save,
                response_status=201 if not existed else 200,
            )
            return saved, bool(created and not existed)

    async def cancel_run_idempotently(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def cancel() -> Resource:
                current = await connection.fetchrow(
                    f"""SELECT {RUN_COLUMNS} FROM runs
                    WHERE id = $1 AND workspace_id = $2 FOR UPDATE""",
                    run_id,
                    workspace_id,
                )
                if current is None:
                    raise NotFoundError("run", str(run_id))
                if current["status"] in {"succeeded", "failed", "cancelled"}:
                    return _run_from_row(current)
                now = datetime.now(UTC)
                await connection.execute(
                    """UPDATE runs SET
                      cancel_requested_at = COALESCE(cancel_requested_at, $3),
                      updated_at = $3, worker_revision = worker_revision + 1
                    WHERE id = $1 AND workspace_id = $2""",
                    run_id,
                    workspace_id,
                    now,
                )
                await connection.execute(
                    """UPDATE run_steps SET
                      status = CASE WHEN status = 'running' THEN status
                        ELSE 'cancelled'::step_status END,
                      cancellation_requested_at = COALESCE(cancellation_requested_at, $3),
                      completed_at = CASE WHEN status = 'running' THEN completed_at ELSE $3 END,
                      lease_owner = CASE WHEN status = 'running' THEN lease_owner ELSE NULL END,
                      lease_token = CASE WHEN status = 'running' THEN lease_token ELSE NULL END,
                      lease_expires_at = CASE
                        WHEN status = 'running' THEN lease_expires_at ELSE NULL END,
                      heartbeat_at = CASE WHEN status = 'running' THEN heartbeat_at ELSE NULL END,
                      updated_at = $3, worker_revision = worker_revision + 1
                    WHERE run_id = $1 AND workspace_id = $2
                      AND worker_step_id IS NOT NULL
                      AND status NOT IN ('succeeded', 'failed', 'cancelled')""",
                    run_id,
                    workspace_id,
                    now,
                )
                await connection.execute(
                    """UPDATE runs SET status = 'cancelled', completed_at = $3,
                      updated_at = $3, worker_revision = worker_revision + 1
                    WHERE id = $1 AND workspace_id = $2
                      AND NOT EXISTS (
                        SELECT 1 FROM run_steps
                        WHERE run_steps.run_id = runs.id
                          AND run_steps.workspace_id = runs.workspace_id
                          AND worker_step_id IS NOT NULL
                      )""",
                    run_id,
                    workspace_id,
                    now,
                )
                await self._synchronize_run_status(
                    connection, workspace_id=workspace_id, run_id=run_id, at=now
                )
                await self._append_run_event(
                    connection,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    event_type="run.cancellation_requested",
                    deduplication_key="api:cancellation-requested",
                    actor_type="user",
                    actor_id=None,
                    payload={"requested": True},
                    occurred_at=now,
                )
                saved = await connection.fetchrow(
                    f"SELECT {RUN_COLUMNS} FROM runs WHERE id = $1 AND workspace_id = $2",
                    run_id,
                    workspace_id,
                )
                if saved is None:  # pragma: no cover - protected by the row lock
                    raise RuntimeError("Run disappeared during cancellation")
                return _run_from_row(saved)

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/runs/{run_id}/cancel",
                response_type="run_cancellation",
                response_status=202,
                create=cancel,
            )

    async def list_run_steps(self, workspace_id: UUID, run_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE id = $1 AND workspace_id = $2)",
                run_id,
                workspace_id,
            )
            if not exists:
                raise NotFoundError("run", str(run_id))
            rows = await connection.fetch(
                f"""SELECT {RUN_STEP_COLUMNS} FROM run_steps
                WHERE workspace_id = $1 AND run_id = $2 AND worker_step_id IS NOT NULL
                ORDER BY created_at, id""",
                workspace_id,
                run_id,
            )
        return [_run_step_from_row(row) for row in rows]

    async def get_run_step(self, workspace_id: UUID, step_id: str) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {RUN_STEP_COLUMNS} FROM run_steps
                WHERE workspace_id = $1 AND worker_step_id = $2""",
                workspace_id,
                step_id,
            )
        if row is None:
            raise NotFoundError("run_step", step_id)
        return _run_step_from_row(row)

    async def retry_run_step_idempotently(
        self,
        workspace_id: UUID,
        step_id: str,
        *,
        actor_id: UUID,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def retry() -> Resource:
                current = await connection.fetchrow(
                    f"""SELECT {RUN_STEP_COLUMNS} FROM run_steps
                    WHERE workspace_id = $1 AND worker_step_id = $2 FOR UPDATE""",
                    workspace_id,
                    step_id,
                )
                if current is None:
                    raise NotFoundError("run_step", step_id)
                run = await connection.fetchrow(
                    """SELECT status::text AS status, cancel_requested_at FROM runs
                    WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                    workspace_id,
                    current["run_id"],
                )
                if run is None:  # pragma: no cover - protected by the foreign key
                    raise NotFoundError("run", str(current["run_id"]))
                if run["cancel_requested_at"] is not None:
                    raise ConflictError(
                        "RUN_CANCELLATION_REQUESTED",
                        "A step cannot be retried after Run cancellation was requested",
                        step_id=step_id,
                    )
                if current["status"] != "failed" or int(current["attempt_count"]) >= 20:
                    raise ConflictError(
                        "STEP_NOT_RETRYABLE",
                        "Only a failed step below the manual retry safety limit can be retried",
                        step_id=step_id,
                        status=current["status"],
                    )
                now = datetime.now(UTC)
                saved = await connection.fetchrow(
                    f"""UPDATE run_steps SET
                      status = 'retrying', available_at = $3, completed_at = NULL,
                      cancellation_requested_at = NULL, updated_at = $3,
                      max_attempts = GREATEST(max_attempts, attempt_count + 1),
                      review_required = static_review_required,
                      worker_revision = worker_revision + 1
                    WHERE workspace_id = $1 AND worker_step_id = $2
                      AND worker_revision = $4 AND status = 'failed'
                    RETURNING {RUN_STEP_COLUMNS}""",
                    workspace_id,
                    step_id,
                    now,
                    int(current["worker_revision"]),
                )
                if saved is None:
                    raise ConflictError(
                        "STEP_STATE_CHANGED",
                        "The step changed while retry was being requested",
                        step_id=step_id,
                    )
                await connection.execute(
                    """WITH RECURSIVE downstream(step_key) AS (
                      SELECT $4::text
                      UNION
                      SELECT child.step_key
                      FROM run_steps child
                      JOIN downstream parent ON parent.step_key = ANY(child.dependencies)
                      WHERE child.workspace_id = $1 AND child.run_id = $2
                        AND child.worker_step_id IS NOT NULL
                    )
                    UPDATE run_steps SET
                      status = 'queued', available_at = $3, completed_at = NULL,
                      cancellation_requested_at = NULL, error = NULL,
                      lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
                      review_required = static_review_required,
                      updated_at = $3, worker_revision = worker_revision + 1
                    WHERE workspace_id = $1 AND run_id = $2
                      AND step_key IN (SELECT step_key FROM downstream WHERE step_key <> $4)
                      AND status = 'cancelled'""",
                    workspace_id,
                    current["run_id"],
                    now,
                    current["step_key"],
                )
                await self._synchronize_run_status(
                    connection,
                    workspace_id=workspace_id,
                    run_id=current["run_id"],
                    at=now,
                )
                await self._append_run_event(
                    connection,
                    workspace_id=workspace_id,
                    run_id=current["run_id"],
                    step_id=current["database_id"],
                    event_type="step.retrying",
                    deduplication_key=f"api-retry:{operation_key}",
                    actor_type="user",
                    actor_id=str(actor_id),
                    payload={
                        "attempt": int(current["attempt_count"]),
                        "maximum_attempts": max(
                            int(current["max_attempts"]),
                            int(current["attempt_count"]) + 1,
                        ),
                    },
                    occurred_at=now,
                )
                return _run_step_from_row(saved)

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/steps/{step_id}/retry",
                response_type="run_step_retry",
                response_status=202,
                create=retry,
            )

    async def review_run_step_idempotently(
        self,
        workspace_id: UUID,
        step_id: str,
        *,
        decision: str,
        actor_id: UUID,
        comment: str | None,
        issue_codes: list[str],
        expected_revision: int | None,
        operation_key: str,
        request_fingerprint: str,
        review_metadata: Resource | None = None,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def review() -> Resource:
                current = await connection.fetchrow(
                    f"""SELECT {RUN_STEP_COLUMNS} FROM run_steps
                    WHERE workspace_id = $1 AND worker_step_id = $2 FOR UPDATE""",
                    workspace_id,
                    step_id,
                )
                if current is None:
                    raise NotFoundError("run_step", step_id)
                run = await connection.fetchrow(
                    """SELECT status::text AS status, cancel_requested_at FROM runs
                    WHERE workspace_id = $1 AND id = $2 FOR UPDATE""",
                    workspace_id,
                    current["run_id"],
                )
                if run is None:  # pragma: no cover - protected by the foreign key
                    raise NotFoundError("run", str(current["run_id"]))
                if run["cancel_requested_at"] is not None or run["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    raise ConflictError(
                        "RUN_NOT_ACCEPTING_REVIEW",
                        "A step cannot be reviewed after its Run stopped accepting work",
                        step_id=step_id,
                        status=run["status"],
                    )
                if not current["review_required"] or current["status"] != "awaiting_review":
                    raise ConflictError(
                        "STEP_NOT_AWAITING_REVIEW",
                        "Only a review-gated step awaiting review can be reviewed",
                        step_id=step_id,
                        status=current["status"],
                    )
                current_revision = int(current["worker_revision"])
                if expected_revision is not None and expected_revision != current_revision:
                    raise ConflictError(
                        "STEP_STATE_CHANGED",
                        "The step changed after its review evidence was loaded",
                        step_id=step_id,
                        expected_revision=expected_revision,
                        current_revision=current_revision,
                    )
                now = datetime.now(UTC)
                attempt = int(current["attempt_count"])
                maximum_attempts = int(current["max_attempts"])
                retry_policy = _json_value(current["retry_policy"]) or {}
                stored_decision = "request_changes" if decision == "revise" else decision
                if decision == "approve":
                    target = "succeeded"
                    error = _json_value(current["error"])
                elif decision == "revise" and attempt < maximum_attempts:
                    target = "retrying"
                    error = {
                        "code": "review_changes_requested",
                        "message": comment or "changes requested",
                        "retryable": True,
                        "details": {},
                    }
                else:
                    target = "failed"
                    error = {
                        "code": "review_rejected",
                        "message": comment or "review rejected",
                        "retryable": False,
                        "details": {},
                    }
                existing_review = _json_value(current["review"]) or {}
                review_value = {
                    "decision": stored_decision,
                    "actor_id": str(actor_id),
                    "comment": comment,
                    "issue_codes": list(issue_codes),
                    "metadata": dict(review_metadata) if review_metadata else None,
                    "evidence": [
                        {
                            "id": item.get("id"),
                            "content_hash": item.get("content_hash"),
                        }
                        for item in (_json_value(current["output_artifacts"]) or [])
                        if isinstance(item, dict)
                    ],
                    "reviewed_revision": current_revision,
                    "requested_at": existing_review.get("requested_at"),
                    "decided_at": now.isoformat(),
                }
                available_at = current["available_at"]
                if target == "retrying":
                    base = float(retry_policy.get("base_delay_seconds", 1))
                    maximum = float(retry_policy.get("max_delay_seconds", 300))
                    multiplier = float(retry_policy.get("multiplier", 2))
                    available_at = now + timedelta(
                        seconds=min(maximum, base * multiplier ** max(0, attempt - 1))
                    )
                saved = await connection.fetchrow(
                    f"""UPDATE run_steps SET
                      status = $3::step_status, review = $4, error = $5,
                      available_at = $6,
                      review_required = CASE WHEN $3::text = 'retrying'
                        THEN static_review_required ELSE review_required END,
                      completed_at = CASE
                        WHEN $3::text IN ('succeeded', 'failed') THEN $7::timestamptz
                        ELSE NULL END,
                      updated_at = $7, worker_revision = worker_revision + 1
                    WHERE workspace_id = $1 AND worker_step_id = $2
                      AND worker_revision = $8 AND status = 'awaiting_review'
                    RETURNING {RUN_STEP_COLUMNS}""",
                    workspace_id,
                    step_id,
                    target,
                    review_value,
                    error,
                    available_at,
                    now,
                    int(current["worker_revision"]),
                )
                if saved is None:
                    raise ConflictError(
                        "STEP_STATE_CHANGED",
                        "The step changed while the review decision was being recorded",
                        step_id=step_id,
                    )
                action = {
                    "approve": "approved",
                    "reject": "rejected",
                    "revise": "changes_requested",
                }[decision]
                await connection.execute(
                    """INSERT INTO review_actions (
                      workspace_id, run_id, step_id, action, comment, details, actor_user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    workspace_id,
                    current["run_id"],
                    current["database_id"],
                    action,
                    comment,
                    {
                        "actor_id": str(actor_id),
                        "issue_codes": list(issue_codes),
                        "reviewed_revision": current_revision,
                        "evidence": review_value["evidence"],
                        "metadata": review_value["metadata"],
                    },
                    actor_id,
                )
                await self._append_run_event(
                    connection,
                    workspace_id=workspace_id,
                    run_id=current["run_id"],
                    step_id=current["database_id"],
                    event_type="review.recorded",
                    deduplication_key=f"api-review:{operation_key}",
                    actor_type="user",
                    actor_id=str(actor_id),
                    payload={
                        "decision": stored_decision,
                        "status": target,
                        "issue_codes": list(issue_codes),
                        "reviewed_revision": current_revision,
                        "metadata": review_value["metadata"],
                    },
                    occurred_at=now,
                )
                await self._synchronize_run_status(
                    connection,
                    workspace_id=workspace_id,
                    run_id=current["run_id"],
                    at=now,
                )
                return _run_step_from_row(saved)

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/steps/{step_id}/review",
                response_type="run_step_review",
                response_status=202,
                create=review,
            )

    async def create_or_reuse_catalog_snapshot(
        self,
        workspace_id: UUID,
        library_ids: Sequence[UUID],
        *,
        created_by: UUID | None = None,
    ) -> Resource:
        normalized_library_ids = tuple(sorted(set(library_ids), key=str))
        if not normalized_library_ids:
            raise ConflictError(
                "CATALOG_SNAPSHOT_LIBRARIES_REQUIRED",
                "A catalog snapshot requires at least one asset library",
            )
        async with self._transaction() as connection:
            library_rows = await connection.fetch(
                """SELECT id FROM asset_libraries
                WHERE workspace_id=$1 AND id=ANY($2::uuid[]) AND status='active'
                ORDER BY id FOR SHARE""",
                workspace_id,
                list(normalized_library_ids),
            )
            visible_ids = {UUID(str(row["id"])) for row in library_rows}
            missing = next(
                (value for value in normalized_library_ids if value not in visible_ids),
                None,
            )
            if missing is not None:
                raise NotFoundError("asset_library", str(missing))

            item_rows = await connection.fetch(
                """SELECT a.library_id, a.id AS asset_id,
                  af.id AS asset_file_id, aa.id AS analysis_id, af.content_hash
                FROM assets a
                JOIN LATERAL (
                  SELECT f.id, f.content_hash
                  FROM asset_files f
                  WHERE f.workspace_id=a.workspace_id AND f.asset_id=a.id
                    AND f.deleted_at IS NULL AND f.scan_status='clean'
                  ORDER BY f.created_at, f.id LIMIT 1
                ) af ON true
                JOIN LATERAL (
                  SELECT candidate.id
                  FROM asset_analyses candidate
                  WHERE candidate.workspace_id=a.workspace_id
                    AND candidate.asset_id=a.id AND candidate.status='completed'
                  ORDER BY candidate.analysis_version DESC, candidate.id DESC LIMIT 1
                ) aa ON true
                WHERE a.workspace_id=$1 AND a.library_id=ANY($2::uuid[])
                  AND a.kind IN ('image','video')
                  AND a.status='ready' AND a.analysis_status='completed'
                  AND a.copyright_status IN ('owned','licensed','public_domain')
                ORDER BY a.library_id, a.id, af.id, aa.id""",
                workspace_id,
                list(normalized_library_ids),
            )
            items = [
                {
                    "library_id": str(row["library_id"]),
                    "asset_id": str(row["asset_id"]),
                    "asset_file_id": str(row["asset_file_id"]),
                    "analysis_id": str(row["analysis_id"]),
                    "content_hash": row["content_hash"],
                }
                for row in item_rows
            ]
            normalized_strings = tuple(str(value) for value in normalized_library_ids)
            content_hash = _catalog_snapshot_content_hash(normalized_strings, items)
            snapshot_id = uuid5(
                NAMESPACE_URL,
                f"framefactory-catalog-snapshot:{workspace_id}:{content_hash}",
            )
            snapshot_row = await connection.fetchrow(
                f"""INSERT INTO catalog_snapshots (
                  id, workspace_id, library_ids, content_hash, item_count,
                  created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,now())
                ON CONFLICT (workspace_id, content_hash) DO NOTHING
                RETURNING {CATALOG_SNAPSHOT_COLUMNS}""",
                snapshot_id,
                workspace_id,
                list(normalized_library_ids),
                content_hash,
                len(items),
                created_by,
            )
            if snapshot_row is not None:
                for ordinal, item in enumerate(items):
                    await connection.execute(
                        """INSERT INTO catalog_snapshot_items (
                          snapshot_id, workspace_id, ordinal, library_id, asset_id,
                          asset_file_id, analysis_id, content_hash
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                        snapshot_id,
                        workspace_id,
                        ordinal,
                        UUID(item["library_id"]),
                        UUID(item["asset_id"]),
                        UUID(item["asset_file_id"]),
                        UUID(item["analysis_id"]),
                        item["content_hash"],
                    )
            else:
                snapshot_row = await connection.fetchrow(
                    f"""SELECT {CATALOG_SNAPSHOT_COLUMNS}
                    FROM catalog_snapshots
                    WHERE workspace_id=$1 AND content_hash=$2""",
                    workspace_id,
                    content_hash,
                )
                if snapshot_row is None:  # pragma: no cover - unique-key contract
                    raise RuntimeError("Catalog snapshot disappeared during reuse")
                snapshot_id = UUID(str(snapshot_row["id"]))
            persisted_items = await connection.fetch(
                """SELECT library_id, asset_id, asset_file_id, analysis_id, content_hash
                FROM catalog_snapshot_items
                WHERE workspace_id=$1 AND snapshot_id=$2 ORDER BY ordinal""",
                workspace_id,
                snapshot_id,
            )
        return _catalog_snapshot_from_rows(snapshot_row, persisted_items)

    async def get_catalog_snapshot(self, workspace_id: UUID, snapshot_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {CATALOG_SNAPSHOT_COLUMNS} FROM catalog_snapshots
                WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                snapshot_id,
            )
            if row is None:
                raise NotFoundError("catalog_snapshot", str(snapshot_id))
            items = await connection.fetch(
                """SELECT library_id, asset_id, asset_file_id, analysis_id, content_hash
                FROM catalog_snapshot_items
                WHERE workspace_id=$1 AND snapshot_id=$2 ORDER BY ordinal""",
                workspace_id,
                snapshot_id,
            )
        return _catalog_snapshot_from_rows(row, items)

    async def create_library_build_job_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        workspace_id = UUID(resource["workspace_id"])
        async with self._transaction() as connection:
            library_exists = await connection.fetchval(
                """SELECT EXISTS (SELECT 1 FROM asset_libraries
                WHERE workspace_id=$1 AND id=$2 AND status='active')""",
                workspace_id,
                UUID(resource["library_id"]),
            )
            if not library_exists:
                raise NotFoundError("asset_library", resource["library_id"])
            existing = await connection.fetchrow(
                f"""SELECT {LIBRARY_BUILD_JOB_COLUMNS} FROM library_build_jobs
                WHERE workspace_id=$1 AND idempotency_key=$2 FOR UPDATE""",
                workspace_id,
                operation_key,
            )
            if existing is not None:
                if existing["request_hash"] != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return _library_build_job_from_row(existing), False

            now = _datetime_value(resource.get("created_at")) or datetime.now(UTC)
            progress = resource.get("progress") or {
                "discovered": 0,
                "transferred": 0,
                "analyzed": 0,
                "indexed": 0,
                "failed": 0,
                "asset_ids": [],
            }
            saved = await connection.fetchrow(
                f"""INSERT INTO library_build_jobs (
                  id, workspace_id, library_id, status, stage, spec, progress, error,
                  idempotency_key, request_hash, revision, created_by, created_at,
                  started_at, completed_at, updated_at
                ) VALUES (
                  $1,$2,$3,'queued','discover',$4,$5,NULL,$6,$7,1,$8,$9,NULL,NULL,$9
                ) ON CONFLICT DO NOTHING
                RETURNING {LIBRARY_BUILD_JOB_COLUMNS}""",
                UUID(resource["id"]),
                workspace_id,
                UUID(resource["library_id"]),
                resource["spec"],
                progress,
                operation_key,
                request_fingerprint,
                self._optional_uuid(resource.get("created_by")),
                now,
            )
            if saved is None:
                saved = await connection.fetchrow(
                    f"""SELECT {LIBRARY_BUILD_JOB_COLUMNS} FROM library_build_jobs
                    WHERE workspace_id=$1 AND idempotency_key=$2 FOR UPDATE""",
                    workspace_id,
                    operation_key,
                )
                if saved is None:
                    raise ConflictError(
                        "LIBRARY_BUILD_JOB_EXISTS",
                        "A library build job with this id already exists",
                        job_id=resource["id"],
                    )
                if saved["request_hash"] != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return _library_build_job_from_row(saved), False
        return _library_build_job_from_row(saved), True

    async def get_library_build_job(self, workspace_id: UUID, job_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {LIBRARY_BUILD_JOB_COLUMNS} FROM library_build_jobs
                WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                job_id,
            )
        if row is None:
            raise NotFoundError("library_build_job", str(job_id))
        return _library_build_job_from_row(row)

    async def cancel_library_build_job(
        self,
        workspace_id: UUID,
        job_id: UUID,
        *,
        expected_revision: int,
    ) -> Resource:
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                f"""SELECT {LIBRARY_BUILD_JOB_COLUMNS} FROM library_build_jobs
                WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                workspace_id,
                job_id,
            )
            if current is None:
                raise NotFoundError("library_build_job", str(job_id))
            self._require_revision(int(current["revision"]), expected_revision)
            if current["status"] in {
                "completed",
                "completed_with_errors",
                "failed",
                "cancelled",
            }:
                return _library_build_job_from_row(current)
            saved = await connection.fetchrow(
                f"""UPDATE library_build_jobs
                SET status='cancelled', completed_at=now(), updated_at=now(),
                    revision=revision+1
                WHERE workspace_id=$1 AND id=$2 AND revision=$3
                RETURNING {LIBRARY_BUILD_JOB_COLUMNS}""",
                workspace_id,
                job_id,
                expected_revision,
            )
            if saved is None:  # pragma: no cover - row is locked above
                raise PreconditionFailedError(int(current["revision"]) + 1, expected_revision)
        return _library_build_job_from_row(saved)

    async def list_asset_libraries(self, workspace_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {ASSET_LIBRARY_COLUMNS}
                FROM asset_libraries l
                LEFT JOIN assets a ON a.workspace_id=l.workspace_id AND a.library_id=l.id
                WHERE l.workspace_id=$1 AND l.status='active'
                GROUP BY l.id ORDER BY l.created_at, l.id""",
                workspace_id,
            )
        return [_asset_library_from_row(row) for row in rows]

    async def get_asset_library(self, workspace_id: UUID, library_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {ASSET_LIBRARY_COLUMNS}
                FROM asset_libraries l
                LEFT JOIN assets a ON a.workspace_id=l.workspace_id AND a.library_id=l.id
                WHERE l.workspace_id=$1 AND l.id=$2
                GROUP BY l.id""",
                workspace_id,
                library_id,
            )
        if row is None:
            raise NotFoundError("asset_library", str(library_id))
        return _asset_library_from_row(row)

    async def create_asset_library(self, resource: Resource) -> Resource:
        try:
            async with self._transaction() as connection:
                await connection.execute(
                    """INSERT INTO asset_libraries
                       (id,workspace_id,name,slug,description,visibility,status,created_by,
                        created_at,updated_at)
                       VALUES ($1,$2,$3,$4,$5,'private','active',$6,$7,$7)""",
                    UUID(resource["id"]),
                    UUID(resource["workspace_id"]),
                    resource["name"],
                    resource["slug"],
                    resource["description"],
                    UUID(resource["created_by"]),
                    _datetime_value(resource["created_at"]),
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ConflictError(
                    "ASSET_LIBRARY_SLUG_EXISTS",
                    "An asset library with this slug already exists",
                    slug=resource["slug"],
                ) from exc
            raise
        return await self.get_asset_library(UUID(resource["workspace_id"]), UUID(resource["id"]))

    async def create_document_source_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        workspace_id = UUID(resource["workspace_id"])

        async with self._transaction() as connection:

            async def create() -> Resource:
                row = await connection.fetchrow(
                    f"""INSERT INTO document_sources (
                      id, workspace_id, filename, media_type, byte_size, content_hash,
                      storage_provider, bucket, object_key, rights_confirmed, status,
                      validation, revision, created_by, created_at, upload_expires_at,
                      updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,'s3',$7,$8,true,'uploading',$9,1,$10,$11,$12,$11)
                    RETURNING {DOCUMENT_SOURCE_COLUMNS}""",
                    UUID(resource["id"]),
                    workspace_id,
                    resource["filename"],
                    resource["media_type"],
                    int(resource["byte_size"]),
                    resource["content_hash"],
                    resource["bucket"],
                    resource["object_key"],
                    resource["validation"],
                    UUID(resource["created_by"]),
                    _datetime_value(resource["created_at"]),
                    _datetime_value(resource["upload_expires_at"]),
                )
                if row is None:  # pragma: no cover - INSERT RETURNING contract
                    raise RuntimeError("document source insert returned no row")
                return {
                    **_document_source_from_row(row),
                    **({"upload": resource["upload"]} if resource.get("upload") else {}),
                }

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/document-sources",
                response_type="document_source",
                create=create,
            )

    async def get_document_source(self, workspace_id: UUID, source_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {DOCUMENT_SOURCE_COLUMNS} FROM document_sources
                WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                source_id,
            )
        if row is None:
            raise NotFoundError("document_source", str(source_id))
        return _document_source_from_row(row)

    async def complete_document_source(
        self, workspace_id: UUID, source_id: UUID, *, byte_size: int
    ) -> Resource:
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                f"""SELECT {DOCUMENT_SOURCE_COLUMNS} FROM document_sources
                WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                workspace_id,
                source_id,
            )
            if current is None:
                raise NotFoundError("document_source", str(source_id))
            if int(current["byte_size"]) != byte_size:
                raise ConflictError(
                    "DOCUMENT_SOURCE_SIZE_MISMATCH",
                    "Uploaded PDF size does not match the initiated source",
                )
            if current["status"] == "uploaded":
                return _document_source_from_row(current)
            row = await connection.fetchrow(
                f"""UPDATE document_sources SET status='uploaded', uploaded_at=now(),
                revision=revision+1, updated_at=now()
                WHERE workspace_id=$1 AND id=$2 RETURNING {DOCUMENT_SOURCE_COLUMNS}""",
                workspace_id,
                source_id,
            )
        if row is None:  # pragma: no cover - locked row contract
            raise RuntimeError("document source completion returned no row")
        return _document_source_from_row(row)

    async def update_document_retention(
        self,
        workspace_id: UUID,
        source_id: UUID,
        *,
        retention_until: datetime | None,
        reason: str,
        actor_id: UUID,
        expected_revision: int,
    ) -> Resource:
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                f"""SELECT {DOCUMENT_SOURCE_COLUMNS} FROM document_sources
                WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                workspace_id,
                source_id,
            )
            if current is None:
                raise NotFoundError("document_source", str(source_id))
            if int(current["revision"]) != expected_revision:
                raise PreconditionFailedError(int(current["revision"]), expected_revision)
            if current["status"] in {"deletion_pending", "purging", "purged"}:
                raise ConflictError(
                    "DOCUMENT_RETENTION_IMMUTABLE",
                    "Retention cannot change after deletion has started",
                )
            row = await connection.fetchrow(
                f"""UPDATE document_sources
                SET retention_until=$3, revision=revision+1, updated_at=now()
                WHERE workspace_id=$1 AND id=$2 AND revision=$4
                RETURNING {DOCUMENT_SOURCE_COLUMNS}""",
                workspace_id,
                source_id,
                retention_until,
                expected_revision,
            )
            if row is None:  # pragma: no cover - row is locked above
                raise PreconditionFailedError(int(current["revision"]) + 1, expected_revision)
            await connection.execute(
                """INSERT INTO audit_logs (
                  workspace_id, actor_type, actor_id, action, resource_type,
                  resource_id, before_data, after_data, metadata
                ) VALUES ($1,'user',$2,'document.retention.updated','document_source',$3,
                  jsonb_build_object('retention_until',$4::timestamptz),
                  jsonb_build_object('retention_until',$5::timestamptz),
                  jsonb_build_object('reason',$6))""",
                workspace_id,
                str(actor_id),
                source_id,
                current["retention_until"],
                retention_until,
                reason,
            )
        return _document_source_from_row(row)

    async def set_document_legal_hold(
        self,
        workspace_id: UUID,
        source_id: UUID,
        *,
        active: bool,
        reason: str,
        actor_id: UUID,
        expected_revision: int,
    ) -> Resource:
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                f"""SELECT {DOCUMENT_SOURCE_COLUMNS} FROM document_sources
                WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                workspace_id,
                source_id,
            )
            if current is None:
                raise NotFoundError("document_source", str(source_id))
            if int(current["revision"]) != expected_revision:
                raise PreconditionFailedError(int(current["revision"]), expected_revision)
            if current["status"] in {"purging", "purged"}:
                raise ConflictError(
                    "DOCUMENT_PURGE_ALREADY_STARTED",
                    "Legal hold cannot change after object deletion has started",
                )
            status_value = (
                "uploaded"
                if active and current["status"] == "deletion_pending"
                else current["status"]
            )
            row = await connection.fetchrow(
                f"""UPDATE document_sources SET
                  legal_hold=$3,
                  legal_hold_reason=CASE WHEN $3 THEN $4 ELSE NULL END,
                  legal_hold_set_by=CASE WHEN $3 THEN $5 ELSE NULL END,
                  legal_hold_set_at=CASE WHEN $3 THEN now() ELSE NULL END,
                  status=$6, revision=revision+1, updated_at=now()
                WHERE workspace_id=$1 AND id=$2 AND revision=$7
                RETURNING {DOCUMENT_SOURCE_COLUMNS}""",
                workspace_id,
                source_id,
                active,
                reason,
                actor_id,
                status_value,
                expected_revision,
            )
            if row is None:  # pragma: no cover - row is locked above
                raise PreconditionFailedError(int(current["revision"]) + 1, expected_revision)
            if active:
                await connection.execute(
                    """UPDATE document_purge_requests SET status='blocked',
                      last_error=jsonb_build_object(
                        'code','DOCUMENT_LEGAL_HOLD_ACTIVE',
                        'message','Deletion was blocked by a legal hold'),
                      updated_at=now()
                    WHERE workspace_id=$1 AND source_id=$2
                      AND status IN ('queued','retrying')""",
                    workspace_id,
                    source_id,
                )
            await connection.execute(
                """INSERT INTO audit_logs (
                  workspace_id, actor_type, actor_id, action, resource_type,
                  resource_id, before_data, after_data, metadata
                ) VALUES ($1,'user',$2,$3,'document_source',$4,
                  jsonb_build_object('legal_hold',$5::boolean),
                  jsonb_build_object('legal_hold',$6::boolean),
                  jsonb_build_object('reason',$7))""",
                workspace_id,
                str(actor_id),
                "document.legal_hold.applied" if active else "document.legal_hold.released",
                source_id,
                bool(current["legal_hold"]),
                active,
                reason,
            )
        return _document_source_from_row(row)

    async def request_document_purge_idempotently(
        self,
        resource: Resource,
        *,
        actor_id: UUID,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        workspace_id = UUID(resource["workspace_id"])
        source_id = UUID(resource["source_id"])
        async with self._transaction() as connection:

            async def create() -> Resource:
                source = await connection.fetchrow(
                    f"""SELECT {DOCUMENT_SOURCE_COLUMNS} FROM document_sources
                    WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
                    workspace_id,
                    source_id,
                )
                if source is None:
                    raise NotFoundError("document_source", str(source_id))
                if int(source["revision"]) != expected_revision:
                    raise PreconditionFailedError(int(source["revision"]), expected_revision)
                if bool(source["legal_hold"]):
                    raise ConflictError(
                        "DOCUMENT_LEGAL_HOLD_ACTIVE",
                        "The document is protected by an active legal hold",
                    )
                retention_until = source["retention_until"]
                if retention_until is not None and retention_until > datetime.now(UTC):
                    raise ConflictError(
                        "DOCUMENT_RETENTION_ACTIVE",
                        "The document has not reached the end of its retention period",
                        retention_until=_timestamp(retention_until),
                    )
                if source["status"] not in {"uploaded", "rejected"}:
                    raise ConflictError(
                        "DOCUMENT_NOT_PURGEABLE",
                        "Only completed, non-purging document sources can be deleted",
                        status=source["status"],
                    )
                active = await connection.fetchrow(
                    """SELECT id,status::text AS status FROM runs
                    WHERE workspace_id=$1
                      AND input_snapshot->>'document_source_id'=$2
                      AND status NOT IN ('succeeded','failed','cancelled')
                    ORDER BY created_at LIMIT 1""",
                    workspace_id,
                    str(source_id),
                )
                if active is not None:
                    raise ConflictError(
                        "DOCUMENT_RUN_ACTIVE",
                        "A document Run must finish or be cancelled before deletion",
                        run_id=str(active["id"]),
                        status=active["status"],
                    )
                row = await connection.fetchrow(
                    f"""INSERT INTO document_purge_requests (
                      id, workspace_id, source_id, status, reason, delete_derived,
                      idempotency_key, request_fingerprint, requested_by,
                      attempt_count, max_attempts, next_attempt_at, created_at, updated_at
                    ) VALUES ($1,$2,$3,'queued',$4,true,$5,$6,$7,0,$8,
                      GREATEST($9,$10),$9,$9)
                    RETURNING {DOCUMENT_PURGE_REQUEST_COLUMNS}""",
                    UUID(resource["id"]),
                    workspace_id,
                    source_id,
                    resource["reason"],
                    operation_key,
                    request_fingerprint,
                    actor_id,
                    int(resource["max_attempts"]),
                    _datetime_value(resource["created_at"]),
                    source["upload_expires_at"],
                )
                if row is None:  # pragma: no cover - INSERT RETURNING contract
                    raise RuntimeError("document purge request insert returned no row")
                await connection.execute(
                    """UPDATE document_sources SET status='deletion_pending',
                      deletion_requested_at=$3, revision=revision+1, updated_at=$3
                    WHERE workspace_id=$1 AND id=$2""",
                    workspace_id,
                    source_id,
                    _datetime_value(resource["created_at"]),
                )
                await connection.execute(
                    """INSERT INTO audit_logs (
                      workspace_id, actor_type, actor_id, action, resource_type,
                      resource_id, after_data, metadata
                    ) VALUES ($1,'user',$2,'document.purge.requested',
                      'document_source',$3,
                      jsonb_build_object('purge_request_id',$4::text,'status','queued'),
                      jsonb_build_object('reason',$5,'delete_derived',true))""",
                    workspace_id,
                    str(actor_id),
                    source_id,
                    UUID(resource["id"]),
                    resource["reason"],
                )
                return _document_purge_request_from_row(row)

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/document-sources/{source_id}/purge-requests",
                response_type="document_purge_request",
                create=create,
                response_status=202,
            )

    async def get_document_purge_request(self, workspace_id: UUID, request_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {DOCUMENT_PURGE_REQUEST_COLUMNS}
                FROM document_purge_requests WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                request_id,
            )
        if row is None:
            raise NotFoundError("document_purge_request", str(request_id))
        return _document_purge_request_from_row(row)

    async def create_asset_upload(self, resource: Resource) -> Resource:
        workspace_id = UUID(resource["workspace_id"])
        file = resource["file"]
        saved_asset_id = UUID(resource["id"])
        try:
            async with self._transaction() as connection:
                library_exists = await connection.fetchval(
                    """SELECT EXISTS (SELECT 1 FROM asset_libraries
                       WHERE workspace_id=$1 AND id=$2 AND status='active')""",
                    workspace_id,
                    UUID(resource["library_id"]),
                )
                if not library_exists:
                    raise NotFoundError("asset_library", resource["library_id"])
                existing = await connection.fetchrow(
                    """SELECT a.id AS asset_id,a.status,f.id AS file_id,
                              f.scan_status
                       FROM assets a JOIN asset_files f
                         ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                       WHERE a.workspace_id=$1 AND f.content_hash=$2
                         AND f.deleted_at IS NULL
                       FOR UPDATE OF a, f""",
                    workspace_id,
                    file["content_hash"],
                )
                if existing is not None:
                    if existing["status"] != "processing" or existing["scan_status"] != "pending":
                        raise ConflictError(
                            "ASSET_CONTENT_EXISTS",
                            "This media file already exists in the workspace",
                            asset_id=str(existing["asset_id"]),
                        )
                    saved_asset_id = existing["asset_id"]
                    await connection.execute(
                        """UPDATE assets SET library_id=$3,kind=$4,title=$5,
                                  description=$6,metadata=$7,copyright_status=$8,
                                  created_by=$9,updated_at=$10
                           WHERE workspace_id=$1 AND id=$2""",
                        workspace_id,
                        saved_asset_id,
                        UUID(resource["library_id"]),
                        resource["kind"],
                        resource["title"],
                        resource["description"],
                        resource["metadata"],
                        resource["copyright_status"],
                        UUID(resource["created_by"]),
                        _datetime_value(resource["created_at"]),
                    )
                    await connection.execute(
                        """UPDATE asset_files SET bucket=$3,object_key=$4,
                                  original_filename=$5,media_type=$6,byte_size=$7,
                                  scan_status='pending'
                           WHERE workspace_id=$1 AND id=$2""",
                        workspace_id,
                        existing["file_id"],
                        file["bucket"],
                        file["object_key"],
                        file["original_filename"],
                        file["media_type"],
                        file["byte_size"],
                    )
                else:
                    await connection.execute(
                        """INSERT INTO assets
                           (id,workspace_id,library_id,kind,title,description,metadata,
                            copyright_status,status,created_by,created_at,updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'processing',$9,$10,$10)""",
                        saved_asset_id,
                        workspace_id,
                        UUID(resource["library_id"]),
                        resource["kind"],
                        resource["title"],
                        resource["description"],
                        resource["metadata"],
                        resource["copyright_status"],
                        UUID(resource["created_by"]),
                        _datetime_value(resource["created_at"]),
                    )
                    await connection.execute(
                        """INSERT INTO asset_files
                           (id,workspace_id,asset_id,storage_provider,bucket,object_key,
                            original_filename,media_type,byte_size,content_hash,scan_status)
                           VALUES ($1,$2,$3,'s3',$4,$5,$6,$7,$8,$9,'pending')""",
                        UUID(file["id"]),
                        workspace_id,
                        saved_asset_id,
                        file["bucket"],
                        file["object_key"],
                        file["original_filename"],
                        file["media_type"],
                        file["byte_size"],
                        file["content_hash"],
                    )
                resource_metadata = _json_value(resource["metadata"]) or {}
                source = resource_metadata.get("source") or {}
                evidence = resource_metadata.get("rights_evidence") or {}
                evidence_locator = str(evidence.get("locator") or "").strip()
                parsed_evidence_locator = urlsplit(evidence_locator)
                trusted_public_domain_evidence = (
                    resource["copyright_status"] == "public_domain"
                    and evidence.get("evidence_type") == "verified_public_domain"
                    and evidence.get("provider") == "wikimedia"
                    and parsed_evidence_locator.scheme == "https"
                    and parsed_evidence_locator.hostname == "commons.wikimedia.org"
                    and bool(str(evidence.get("verified_at") or "").strip())
                    and bool(str(evidence.get("license") or "").strip())
                )
                if not trusted_public_domain_evidence:
                    evidence = {}
                locator = source.get("source_url")
                if evidence:
                    locator = evidence["locator"]
                source_id = uuid5(
                    NAMESPACE_URL,
                    f"framefactory-api-source:{workspace_id}:{saved_asset_id}:"
                    f"{locator or file['content_hash']}",
                )
                await connection.execute(
                    """INSERT INTO asset_sources
                       (id,workspace_id,asset_id,source_type,locator,provider,attribution,
                        license,evidence_type,verified_at,captured_at,metadata)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),$11)
                       ON CONFLICT (workspace_id,id) DO NOTHING""",
                    source_id,
                    workspace_id,
                    saved_asset_id,
                    "website" if locator else "upload",
                    locator,
                    evidence.get("provider") or source.get("platform") or "direct-upload",
                    evidence.get("attribution") or source.get("author"),
                    evidence.get("license")
                    or source.get("license")
                    or resource["copyright_status"],
                    evidence.get("evidence_type") or "copyright",
                    _datetime_value(evidence.get("verified_at")),
                    source,
                )
                await self._replace_asset_tags(
                    connection,
                    workspace_id,
                    saved_asset_id,
                    normalized_tags(resource),
                )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ConflictError(
                    "ASSET_CONTENT_EXISTS",
                    "This media file already exists in the workspace",
                    content_hash=file["content_hash"],
                ) from exc
            raise
        return await self.get_asset(workspace_id, saved_asset_id)

    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {ASSET_COLUMNS} FROM assets a JOIN asset_files f
                ON f.workspace_id=a.workspace_id AND f.asset_id=a.id AND f.deleted_at IS NULL
                WHERE a.workspace_id=$1 AND a.id=$2 ORDER BY f.created_at LIMIT 1""",
                workspace_id,
                asset_id,
            )
        if row is None:
            raise NotFoundError("asset", str(asset_id))
        return _asset_from_row(row)

    async def list_assets(
        self,
        workspace_id: UUID,
        library_id: UUID,
        *,
        statuses: tuple[str, ...],
        kinds: tuple[str, ...],
        copyright_statuses: tuple[str, ...],
        analysis_statuses: tuple[str, ...],
        tags: tuple[str, ...],
        search: str | None,
    ) -> list[Resource]:
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                """SELECT EXISTS (SELECT 1 FROM asset_libraries
                   WHERE workspace_id=$1 AND id=$2 AND status='active')""",
                workspace_id,
                library_id,
            )
            if not exists:
                raise NotFoundError("asset_library", str(library_id))
            rows = await connection.fetch(
                f"""SELECT {ASSET_COLUMNS}
                FROM assets a JOIN asset_files f
                  ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                 AND f.deleted_at IS NULL
                WHERE a.workspace_id=$1 AND a.library_id=$2
                  AND (cardinality($3::text[]) > 0 AND a.status=ANY($3::text[])
                       OR cardinality($3::text[]) = 0 AND a.status <> 'deleted')
                  AND (cardinality($4::text[]) = 0 OR a.kind=ANY($4::text[]))
                  AND (cardinality($5::text[]) = 0 OR a.copyright_status=ANY($5::text[]))
                  AND (cardinality($6::text[]) = 0 OR a.analysis_status=ANY($6::text[]))
                  AND (cardinality($7::text[]) = 0 OR NOT EXISTS (
                    SELECT 1 FROM unnest($7::text[]) wanted(tag)
                    WHERE NOT EXISTS (
                      SELECT 1 FROM asset_tags at JOIN tags t
                        ON t.workspace_id=at.workspace_id AND t.id=at.tag_id
                      WHERE at.workspace_id=a.workspace_id AND at.asset_id=a.id
                        AND lower(t.name)=lower(wanted.tag)
                    )
                  ))
                  AND ($8::text IS NULL OR (
                    to_tsvector('simple', a.title || ' ' || a.description)
                      @@ plainto_tsquery('simple', $8)
                    OR EXISTS (
                      SELECT 1 FROM asset_analyses aa
                      WHERE aa.workspace_id=a.workspace_id AND aa.asset_id=a.id
                        AND aa.status='completed' AND aa.search_text ILIKE '%' || $8 || '%'
                    )
                  ))
                ORDER BY a.updated_at DESC, a.id DESC""",
                workspace_id,
                library_id,
                list(statuses),
                list(kinds),
                list(copyright_statuses),
                list(analysis_statuses),
                list(tags),
                search,
            )
        return [_asset_from_row(row) for row in rows]

    async def update_asset_metadata(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        actor_id: UUID,
    ) -> Resource:
        workspace_id, asset_id = UUID(resource["workspace_id"]), UUID(resource["id"])
        async with self._transaction() as connection:
            current = await connection.fetchrow(
                f"""SELECT {ASSET_COLUMNS} FROM assets a JOIN asset_files f
                ON f.workspace_id=a.workspace_id AND f.asset_id=a.id AND f.deleted_at IS NULL
                WHERE a.workspace_id=$1 AND a.id=$2 FOR UPDATE OF a, f""",
                workspace_id,
                asset_id,
            )
            if current is None:
                raise NotFoundError("asset", str(asset_id))
            current_revision = int(current["revision"])
            if current_revision != expected_revision:
                raise PreconditionFailedError(current_revision, expected_revision)
            saved = await connection.fetchrow(
                """UPDATE assets SET title=$3,description=$4,metadata=$5,
                     copyright_status=$6,revision=revision+1,updated_at=now()
                   WHERE workspace_id=$1 AND id=$2 AND revision=$7
                   RETURNING revision,updated_at""",
                workspace_id,
                asset_id,
                resource["title"],
                resource["description"],
                resource["metadata"],
                resource["copyright_status"],
                expected_revision,
            )
            if saved is None:
                raise PreconditionFailedError(current_revision + 1, expected_revision)
            await self._replace_asset_tags(
                connection, workspace_id, asset_id, normalized_tags(resource)
            )
            after = deepcopy(resource)
            after["revision"] = int(saved["revision"])
            after["updated_at"] = _timestamp(saved["updated_at"])
            await self._append_asset_audit(
                connection,
                workspace_id=workspace_id,
                asset_id=asset_id,
                actor_id=actor_id,
                action="asset.metadata_updated",
                before=_asset_from_row(current),
                after=after,
            )
        return await self.get_asset(workspace_id, asset_id)

    async def transition_asset_status(
        self,
        workspace_id: UUID,
        asset_id: UUID,
        *,
        target_status: str,
        expected_revision: int,
        actor_id: UUID,
        reason: str,
    ) -> Resource:
        async with self._transaction() as connection:
            row = await connection.fetchrow(
                f"""SELECT {ASSET_COLUMNS} FROM assets a JOIN asset_files f
                ON f.workspace_id=a.workspace_id AND f.asset_id=a.id AND f.deleted_at IS NULL
                WHERE a.workspace_id=$1 AND a.id=$2 FOR UPDATE OF a, f""",
                workspace_id,
                asset_id,
            )
            if row is None:
                raise NotFoundError("asset", str(asset_id))
            current = _asset_from_row(row)
            if current["revision"] != expected_revision:
                raise PreconditionFailedError(current["revision"], expected_revision)
            validate_asset_transition(current, target_status)
            if current["status"] == target_status:
                return current
            metadata = deepcopy(current["metadata"])
            if target_status == "deleted":
                metadata["status_before_delete"] = current["status"]
            saved = await connection.fetchrow(
                """UPDATE assets SET status=$3,metadata=$4,
                     deleted_at=CASE WHEN $3='deleted' THEN now() ELSE NULL END,
                     revision=revision+1,updated_at=now()
                   WHERE workspace_id=$1 AND id=$2 AND revision=$5
                   RETURNING revision,updated_at,deleted_at""",
                workspace_id,
                asset_id,
                target_status,
                metadata,
                expected_revision,
            )
            if saved is None:
                raise PreconditionFailedError(current["revision"] + 1, expected_revision)
            after = deepcopy(current)
            after.update(
                status=target_status,
                metadata=metadata,
                revision=int(saved["revision"]),
                updated_at=_timestamp(saved["updated_at"]),
                deleted_at=_timestamp(saved["deleted_at"]),
            )
            await self._append_asset_audit(
                connection,
                workspace_id=workspace_id,
                asset_id=asset_id,
                actor_id=actor_id,
                action="asset.deleted" if target_status == "deleted" else "asset.status_changed",
                before=current,
                after=after,
                metadata={"reason": reason},
            )
        return await self.get_asset(workspace_id, asset_id)

    async def list_asset_related(
        self, workspace_id: UUID, asset_id: UUID, relation: str
    ) -> list[Resource]:
        await self.get_asset(workspace_id, asset_id)
        queries = {
            "segments": """SELECT s.id,s.asset_id,s.analysis_id,s.ordinal,s.start_ms,s.end_ms,
                s.description,s.people,s.locations,s.keywords,s.scene_type,s.action,s.era,s.mood,
                s.visual_style,s.shot_type,s.confidence,s.representative_frame_key,s.transcript,
                s.boundary_score,s.boundary_reasons,s.cut_safe,s.semantic_complete,s.created_at
                FROM asset_segments s JOIN asset_analyses aa
                  ON aa.workspace_id=s.workspace_id AND aa.id=s.analysis_id
                WHERE s.workspace_id=$1 AND s.asset_id=$2 AND aa.status='completed'
                ORDER BY s.start_ms,s.id""",
            "analyses": """SELECT id,asset_id,analysis_version,provider,model,status,
                summary,language,people,organizations,locations,eras,scene_types,actions,
                moods,visual_styles,keywords,has_embedded_text,has_watermark,safety,
                quality,confidence,raw_result,created_at FROM asset_analyses
                WHERE workspace_id=$1 AND asset_id=$2 ORDER BY analysis_version DESC,id DESC""",
            "sources": """SELECT id,asset_id,source_type,locator,provider,attribution,
                license,evidence_type,verified_at,verified_by,captured_at,metadata,created_at
                FROM asset_sources WHERE workspace_id=$1 AND asset_id=$2
                ORDER BY created_at DESC,id DESC""",
            "rights_evidence": """SELECT id,asset_id,source_type,locator,provider,
                attribution,license,evidence_type,verified_at,verified_by,captured_at,
                metadata,created_at FROM asset_sources
                WHERE workspace_id=$1 AND asset_id=$2
                  AND (license IS NOT NULL OR evidence_type <> 'provenance')
                ORDER BY created_at DESC,id DESC""",
            "usage_records": """SELECT id,asset_id,run_id,step_id,usage_type,details,
                occurred_at FROM asset_usage_records WHERE workspace_id=$1 AND asset_id=$2
                ORDER BY occurred_at DESC,id DESC""",
            "analysis_jobs": """SELECT j.id,j.asset_id,j.kind,j.status,j.reason,
                j.requested_by,j.queue_job_id,j.error,j.created_at,j.started_at,
                j.completed_at,j.updated_at,i.status AS pipeline_status,
                i.current_stage,i.completed_stages,i.attempts,i.max_attempts,
                i.failure_code,i.failure_reason
                FROM asset_analysis_jobs j
                LEFT JOIN asset_analysis_batches b
                  ON b.workspace_id=j.workspace_id AND b.id::text=j.queue_job_id
                LEFT JOIN asset_analysis_items i
                  ON i.workspace_id=b.workspace_id AND i.batch_id=b.id
                 AND i.asset_id=j.asset_id
                WHERE j.workspace_id=$1 AND j.asset_id=$2
                ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         j.created_at DESC,j.id DESC""",
            "audit_events": """SELECT id,resource_id AS asset_id,actor_type,actor_id,
                action,before_data AS before,after_data AS after,metadata,
                occurred_at AS created_at FROM audit_logs
                WHERE workspace_id=$1 AND resource_type='asset' AND resource_id=$2
                ORDER BY occurred_at DESC,id DESC""",
        }
        query = queries.get(relation)
        if query is None:
            raise ValueError(f"unsupported asset relation: {relation}")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, workspace_id, asset_id)
        return [_public_database_row(row) for row in rows]

    async def list_asset_import_jobs(self, workspace_id: UUID, library_id: UUID) -> list[Resource]:
        await self.get_asset_library(workspace_id, library_id)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT id,library_id,source_root,status,discovered_count,imported_count,
                   deduplicated_count,tagged_count,rejected_count,error,requested_by,
                   started_at,completed_at,created_at,updated_at
                   FROM asset_ingestion_jobs WHERE workspace_id=$1 AND library_id=$2
                   ORDER BY created_at DESC,id DESC""",
                workspace_id,
                library_id,
            )
        return [_public_database_row(row) for row in rows]

    async def create_asset_analysis_job(
        self,
        workspace_id: UUID,
        asset_id: UUID,
        *,
        expected_revision: int,
        actor_id: UUID,
        reason: str,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:

            async def create() -> Resource:
                row = await connection.fetchrow(
                    f"""SELECT {ASSET_COLUMNS} FROM assets a JOIN asset_files f
                    ON f.workspace_id=a.workspace_id AND f.asset_id=a.id
                       AND f.deleted_at IS NULL
                    WHERE a.workspace_id=$1 AND a.id=$2 FOR UPDATE OF a, f""",
                    workspace_id,
                    asset_id,
                )
                if row is None:
                    raise NotFoundError("asset", str(asset_id))
                current = _asset_from_row(row)
                if current["revision"] != expected_revision:
                    raise PreconditionFailedError(current["revision"], expected_revision)
                if current["status"] == "deleted":
                    raise ConflictError("ASSET_DELETED", "Deleted assets cannot be reanalyzed")
                job_id = uuid5(NAMESPACE_URL, operation_key)
                now = datetime.now(UTC)
                await connection.execute(
                    """INSERT INTO asset_analysis_jobs
                       (id,workspace_id,asset_id,kind,status,reason,requested_by,
                        created_at,updated_at)
                       VALUES ($1,$2,$3,'reanalysis','queued',$4,$5,$6,$6)""",
                    job_id,
                    workspace_id,
                    asset_id,
                    reason,
                    actor_id,
                    now,
                )
                await connection.execute(
                    """UPDATE assets SET status='processing',analysis_status='pending',
                         revision=revision+1,updated_at=$3
                       WHERE workspace_id=$1 AND id=$2 AND revision=$4""",
                    workspace_id,
                    asset_id,
                    now,
                    expected_revision,
                )
                job = {
                    "schema_version": "1.0.0",
                    "id": str(job_id),
                    "workspace_id": str(workspace_id),
                    "asset_id": str(asset_id),
                    "kind": "reanalysis",
                    "status": "queued",
                    "reason": reason,
                    "requested_by": str(actor_id),
                    "queue_job_id": None,
                    "error": None,
                    "created_at": _timestamp(now),
                    "started_at": None,
                    "completed_at": None,
                    "updated_at": _timestamp(now),
                }
                after = deepcopy(current)
                after.update(
                    status="processing",
                    analysis_status="pending",
                    revision=current["revision"] + 1,
                    updated_at=_timestamp(now),
                )
                await self._append_asset_audit(
                    connection,
                    workspace_id=workspace_id,
                    asset_id=asset_id,
                    actor_id=actor_id,
                    action="asset.reanalysis_requested",
                    before=current,
                    after=after,
                    metadata={"job_id": str(job_id), "reason": reason},
                )
                return job

            return await self._idempotent(
                connection,
                workspace_id=workspace_id,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path=f"/v1/assets/{asset_id}/reanalyze",
                response_type="asset_analysis_job",
                response_status=202,
                create=create,
            )

    async def run_asset_batch_idempotently(
        self,
        workspace_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
        request_path: str,
        action: Callable[[], Awaitable[Resource]],
    ) -> tuple[Resource, bool]:
        # The durable ledger serializes/replays a batch across API restarts. Per-item mutations
        # retain their own short transactions so one failure can be reported without rolling
        # back successful siblings.
        async with self._transaction() as connection:
            inserted = await connection.fetchval(
                """INSERT INTO idempotency_keys (
                  workspace_id,key,request_method,request_path,request_hash,status,
                  locked_until,expires_at
                ) VALUES ($1,$2,'POST',$3,$4,'processing',now()+interval '5 minutes',
                  now()+interval '24 hours')
                ON CONFLICT (workspace_id,key) DO NOTHING RETURNING true""",
                workspace_id,
                operation_key,
                request_path,
                request_fingerprint,
            )
            if not inserted:
                previous = await connection.fetchrow(
                    """SELECT request_hash,status,response_body FROM idempotency_keys
                       WHERE workspace_id=$1 AND key=$2 FOR UPDATE""",
                    workspace_id,
                    operation_key,
                )
                if previous is None:  # pragma: no cover - protected by the unique key
                    raise RuntimeError("Asset batch idempotency record disappeared")
                if previous["request_hash"] != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                if previous["status"] != "completed" or previous["response_body"] is None:
                    raise ConflictError(
                        "IDEMPOTENT_OPERATION_IN_PROGRESS",
                        "The idempotent asset batch has not completed",
                    )
                return _json_value(previous["response_body"]), False
        response = await action()
        async with self._transaction() as connection:
            await connection.execute(
                """UPDATE idempotency_keys SET status='completed',response_status=200,
                     response_body=$3,resource_type='asset_batch_result',locked_until=NULL
                   WHERE workspace_id=$1 AND key=$2 AND status='processing'""",
                workspace_id,
                operation_key,
                response,
            )
        return response, True

    async def complete_asset_upload(
        self, workspace_id: UUID, asset_id: UUID, *, byte_size: int
    ) -> Resource:
        async with self._transaction() as connection:
            row = await connection.fetchrow(
                f"""SELECT {ASSET_COLUMNS} FROM assets a JOIN asset_files f
                ON f.workspace_id=a.workspace_id AND f.asset_id=a.id AND f.deleted_at IS NULL
                WHERE a.workspace_id=$1 AND a.id=$2 FOR UPDATE OF a, f""",
                workspace_id,
                asset_id,
            )
            if row is None:
                raise NotFoundError("asset", str(asset_id))
            if int(row["byte_size"]) != byte_size:
                raise ConflictError(
                    "ASSET_SIZE_MISMATCH",
                    "Uploaded media size does not match the declared size",
                )
            metadata = _json_value(row["metadata"])
            if metadata.get("upload_completed_at"):
                return _asset_from_row(row)
            metadata["upload_completed_at"] = _timestamp(datetime.now(UTC))
            await connection.execute(
                """UPDATE assets SET status='processing',analysis_status='pending',
                     metadata=$3,revision=revision+1,updated_at=now()
                   WHERE workspace_id=$1 AND id=$2""",
                workspace_id,
                asset_id,
                metadata,
            )
        return await self.get_asset(workspace_id, asset_id)

    async def list_artifacts(self, workspace_id: UUID, run_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE workspace_id=$1 AND id=$2)",
                workspace_id,
                run_id,
            )
            if not exists:
                raise NotFoundError("run", str(run_id))
            rows = await connection.fetch(
                f"""SELECT {ARTIFACT_COLUMNS} FROM artifacts
                WHERE workspace_id=$1 AND run_id=$2 AND status='available'
                ORDER BY created_at, id""",
                workspace_id,
                run_id,
            )
        return [_artifact_from_row(row) for row in rows]

    async def get_artifact(self, workspace_id: UUID, artifact_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {ARTIFACT_COLUMNS} FROM artifacts
                WHERE workspace_id=$1 AND id=$2 AND status='available'""",
                workspace_id,
                artifact_id,
            )
        if row is None:
            raise NotFoundError("artifact", str(artifact_id))
        return _artifact_from_row(row)

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 101,
    ) -> list[Resource]:
        async with self._pool.acquire() as connection:
            exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM runs WHERE workspace_id=$1 AND id=$2)",
                workspace_id,
                run_id,
            )
            if not exists:
                raise NotFoundError("run", str(run_id))
            rows = await connection.fetch(
                f"""SELECT {EVENT_COLUMNS} FROM run_events
                WHERE workspace_id=$1 AND run_id=$2 AND sequence>$3
                ORDER BY sequence LIMIT $4""",
                workspace_id,
                run_id,
                after_sequence,
                limit,
            )
        return [_event_from_row(row) for row in rows]

    async def get_event(self, workspace_id: UUID, event_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT {EVENT_COLUMNS} FROM run_events WHERE workspace_id=$1 AND id=$2",
                workspace_id,
                event_id,
            )
        if row is None:
            raise NotFoundError("event", str(event_id))
        return _event_from_row(row)

    async def list_skill_test_executions(self, workspace_id: UUID) -> list[Resource]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT {TEST_EXECUTION_COLUMNS} FROM skill_evaluations
                WHERE workspace_id = $1 AND evaluation_kind = 'version_comparison'
                ORDER BY created_at, id""",
                workspace_id,
            )
        return [_test_execution_from_row(row) for row in rows]

    async def get_skill_test_execution(self, workspace_id: UUID, execution_id: UUID) -> Resource:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""SELECT {TEST_EXECUTION_COLUMNS} FROM skill_evaluations
                WHERE id = $1 AND workspace_id = $2 AND evaluation_kind = 'version_comparison'""",
                execution_id,
                workspace_id,
            )
        if row is None:
            raise NotFoundError("skill_test_execution", str(execution_id))
        return _test_execution_from_row(row)

    async def create_skill_test_execution_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._transaction() as connection:
            return await self._idempotent(
                connection,
                workspace_id=UUID(resource["workspace_id"]),
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                request_path="/v1/skill-test-executions",
                response_type="skill_test_execution",
                create=lambda: self._insert_test_execution(connection, resource),
            )

    async def _replace_asset_tags(
        self,
        connection: _Connection,
        workspace_id: UUID,
        asset_id: UUID,
        tag_names: list[str],
    ) -> None:
        desired = list(dict.fromkeys(value.strip() for value in tag_names if value.strip()))[:64]
        await connection.execute(
            """DELETE FROM asset_tags at USING tags t
               WHERE at.workspace_id=$1 AND at.asset_id=$2 AND at.source='manual'
                 AND t.workspace_id=at.workspace_id AND t.id=at.tag_id
                 AND NOT (t.name=ANY($3::text[]))""",
            workspace_id,
            asset_id,
            desired,
        )

        for tag_name in desired:
            tag_id = uuid5(
                NAMESPACE_URL,
                f"framefactory-manual-tag:{workspace_id}:{tag_name.casefold()}",
            )
            slug = "manual-" + tag_id.hex[:24]
            await connection.execute(
                """INSERT INTO tags (id,workspace_id,name,slug) VALUES ($1,$2,$3,$4)
                   ON CONFLICT (workspace_id,slug) DO UPDATE SET name=EXCLUDED.name""",
                tag_id,
                workspace_id,
                tag_name,
                slug,
            )
            await connection.execute(
                """INSERT INTO asset_tags
                   (workspace_id,asset_id,tag_id,analysis_id,confidence,source)
                   VALUES ($1,$2,$3,NULL,1,'manual')
                   ON CONFLICT (workspace_id,asset_id,tag_id) DO NOTHING""",
                workspace_id,
                asset_id,
                tag_id,
            )

    async def _select_channel(
        self, connection: _Connection, workspace_id: UUID, channel_id: UUID
    ) -> Mapping[str, Any] | None:
        return await connection.fetchrow(
            f"""SELECT {CHANNEL_COLUMNS}
            FROM channels c JOIN channel_defaults d
              ON d.channel_id = c.id AND d.workspace_id = c.workspace_id
            WHERE c.id = $1 AND c.workspace_id = $2""",
            channel_id,
            workspace_id,
        )

    async def _replace_channel_asset_libraries(
        self, connection: _Connection, resource: Resource
    ) -> None:
        channel_id = UUID(resource["id"])
        workspace_id = UUID(resource["workspace_id"])
        await connection.execute(
            """DELETE FROM channel_default_asset_libraries
            WHERE channel_id = $1 AND workspace_id = $2""",
            channel_id,
            workspace_id,
        )
        for ordinal, library_id in enumerate(resource["default_composition"]["asset_library_ids"]):
            await connection.execute(
                """INSERT INTO channel_default_asset_libraries (
                  channel_id, workspace_id, asset_library_id, ordinal
                ) VALUES ($1, $2, $3, $4)""",
                channel_id,
                workspace_id,
                UUID(library_id),
                ordinal,
            )

    async def _append_asset_audit(
        self,
        connection: _Connection,
        *,
        workspace_id: UUID,
        asset_id: UUID,
        actor_id: UUID,
        action: str,
        before: Resource,
        after: Resource,
        metadata: Resource | None = None,
    ) -> None:
        await connection.execute(
            """INSERT INTO audit_logs
               (workspace_id,actor_type,actor_id,action,resource_type,resource_id,
                before_data,after_data,metadata)
               VALUES ($1,'user',$2,$3,'asset',$4,$5,$6,$7)""",
            workspace_id,
            str(actor_id),
            action,
            asset_id,
            before,
            after,
            metadata or {},
        )

    @asynccontextmanager
    async def _transaction(self) -> Any:
        async with self._pool.acquire() as connection, connection.transaction():
            yield connection

    async def _insert_skill(self, connection: _Connection, resource: Resource) -> Resource:
        try:
            row = await connection.fetchrow(
                f"""INSERT INTO skills (
                  id, workspace_id, ownership_type, publisher_type, publisher_name, name, slug,
                  description, visibility, status, current_version_id, forked_from_skill_id,
                  revision, schema_version, created_by, created_at, updated_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
                ) RETURNING {SKILL_COLUMNS}""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                resource["ownership_type"],
                resource["publisher_type"],
                resource["publisher_name"],
                resource["name"],
                resource["slug"],
                resource["description"],
                resource["visibility"],
                resource["status"],
                self._optional_uuid(resource["current_version_id"]),
                self._optional_uuid(resource["forked_from_skill_id"]),
                resource.get("revision", 1),
                resource["schema_version"],
                UUID(resource["created_by"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["updated_at"]),
            )
        except Exception as exc:
            self._raise_database_error(exc, "skill", resource)
        assert row is not None
        return _skill_from_row(row)

    async def _insert_version(self, connection: _Connection, resource: Resource) -> Resource:
        try:
            row = await connection.fetchrow(
                f"""INSERT INTO skill_versions (
                  id, workspace_id, ownership_type, skill_id, version, schema_version, state,
                  execution_kind, input_schema, research_policy, writing_policy, visual_policy,
                  asset_policy, qc_policy, output_contract, capability_requirements,
                  default_pipeline_version_id, content_hash, revision, test_topics, release_notes,
                  created_by, created_at, published_at
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                  $16, $17, $18, $19, $20, $21, $22, $23, $24
                ) RETURNING {VERSION_COLUMNS}""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                resource["ownership_type"],
                UUID(resource["skill_id"]),
                resource["version"],
                resource["schema_version"],
                resource["state"],
                resource["execution_kind"],
                resource["input_schema"],
                resource["research_policy"],
                resource["writing_policy"],
                resource["visual_policy"],
                resource["asset_policy"],
                resource["qc_policy"],
                resource["output_contract"],
                resource["capability_requirements"],
                self._optional_uuid(resource["default_pipeline_version_id"]),
                resource["content_hash"],
                resource.get("revision", 1),
                resource.get("test_topics", []),
                resource.get("release_notes", ""),
                UUID(resource["created_by"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["published_at"]),
            )
        except Exception as exc:
            self._raise_database_error(exc, "skill_version", resource)
        assert row is not None
        return _version_from_row(row)

    async def _insert_run(self, connection: _Connection, resource: Resource) -> Resource:
        composition = resource["composition_snapshot"]
        render_preset = composition["render_preset"]
        asset_library_ids = composition["asset_library_ids"]
        try:
            row = await connection.fetchrow(
                f"""INSERT INTO runs (
                  id, workspace_id, ownership_type, channel_id, skill_version_id, asset_library_id,
                  voice_profile_id, render_preset_version_id, pipeline_version_id, status,
                  input_snapshot, composition_snapshot, capability_snapshot, requested_by,
                  created_at, started_at, completed_at, updated_at, schema_version, idempotency_key
                ) VALUES (
                  $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                  $16, $17, $18, $19, $20
                ) RETURNING {RUN_COLUMNS}""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                resource["ownership_type"],
                self._optional_uuid(resource["channel_id"]),
                UUID(composition["skill_version"]["id"]),
                self._optional_uuid(asset_library_ids[0] if asset_library_ids else None),
                self._optional_uuid(composition["voice_profile_id"]),
                self._optional_uuid(render_preset["id"] if render_preset else None),
                UUID(composition["pipeline_version"]["id"]),
                resource["status"],
                resource["input"],
                composition,
                composition["capabilities"],
                UUID(resource["created_by"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["started_at"]),
                _datetime_value(resource["finished_at"]),
                _datetime_value(resource["updated_at"]),
                resource["schema_version"],
                resource["idempotency_key"],
            )
        except Exception as exc:
            self._raise_database_error(exc, "run", resource)
        assert row is not None
        await self._append_run_event(
            connection,
            workspace_id=UUID(resource["workspace_id"]),
            run_id=UUID(resource["id"]),
            event_type="run.created",
            deduplication_key="api:run-created",
            actor_type="user",
            actor_id=resource["created_by"],
            payload={"status": resource["status"]},
            occurred_at=_datetime_value(resource["created_at"]),
        )
        return _run_from_row(row)

    async def _insert_test_execution(self, connection: _Connection, resource: Resource) -> Resource:
        input_snapshot = {
            "topic": resource["topic"],
            "inputs": resource["inputs"],
            "left_version_id": resource["left_version_id"],
            "right_version_id": resource["right_version_id"],
        }
        try:
            row = await connection.fetchrow(
                f"""INSERT INTO skill_evaluations (
                  id, workspace_id, skill_version_id, evaluation_kind, status, input_snapshot,
                  result, created_by, created_at, completed_at, schema_version, ownership_type,
                  skill_id, left_version_id, right_version_id, topic, inputs, evaluator, error,
                  started_at, finished_at, updated_at
                ) VALUES (
                  $1, $2, $3, 'version_comparison', $4, $5, $6, $7, $8, $9, $10, $11,
                  $12, $13, $14, $15, $16, $17, $18, $19, $20, $21
                ) RETURNING {TEST_EXECUTION_COLUMNS}""",
                UUID(resource["id"]),
                UUID(resource["workspace_id"]),
                UUID(resource["left_version_id"]),
                resource["status"],
                input_snapshot,
                resource["result"],
                UUID(resource["created_by"]),
                _datetime_value(resource["created_at"]),
                _datetime_value(resource["finished_at"]),
                resource["schema_version"],
                resource["ownership_type"],
                UUID(resource["skill_id"]),
                UUID(resource["left_version_id"]),
                UUID(resource["right_version_id"]),
                resource["topic"],
                resource["inputs"],
                resource["evaluator"],
                resource["error"],
                _datetime_value(resource["started_at"]),
                _datetime_value(resource["finished_at"]),
                _datetime_value(resource["updated_at"]),
            )
        except Exception as exc:
            self._raise_database_error(exc, "skill_test_execution", resource)
        assert row is not None
        return _test_execution_from_row(row)

    async def _synchronize_run_status(
        self,
        connection: _Connection,
        *,
        workspace_id: UUID,
        run_id: UUID,
        at: datetime,
    ) -> None:
        current_run = await connection.fetchrow(
            """SELECT status::text AS status, worker_revision FROM runs
            WHERE workspace_id=$1 AND id=$2 FOR UPDATE""",
            workspace_id,
            run_id,
        )
        if current_run is None:
            raise NotFoundError("run", str(run_id))
        rows = await connection.fetch(
            """SELECT status::text AS status FROM run_steps
            WHERE workspace_id = $1 AND run_id = $2 AND worker_step_id IS NOT NULL""",
            workspace_id,
            run_id,
        )
        if not rows:
            return
        states = [row["status"] for row in rows]
        if all(value == "succeeded" for value in states):
            status = "succeeded"
        elif "failed" in states:
            status = "failed"
        elif all(value in {"succeeded", "failed", "cancelled"} for value in states) and (
            "cancelled" in states
        ):
            status = "cancelled"
        elif "awaiting_review" in states:
            status = "awaiting_review"
        elif any(value in {"running", "retrying"} for value in states):
            status = "running"
        elif "queued" in states:
            status = "queued"
        else:  # pragma: no cover - database enum and worker state machine constrain this
            status = "cancelled"
        terminal = status in {"succeeded", "failed", "cancelled"}
        saved_revision = await connection.fetchval(
            """UPDATE runs SET status = $3::run_status,
              completed_at = CASE WHEN $4 THEN COALESCE(completed_at, $5) ELSE NULL END,
              updated_at = $5, worker_revision = worker_revision + 1
            WHERE workspace_id = $1 AND id = $2
            RETURNING worker_revision""",
            workspace_id,
            run_id,
            status,
            terminal,
            at,
        )
        if status != current_run["status"]:
            if status == "running":
                event_type = "run.started" if current_run["status"] == "queued" else "run.retrying"
            else:
                event_type = f"run.{status}"
            await self._append_run_event(
                connection,
                workspace_id=workspace_id,
                run_id=run_id,
                event_type=event_type,
                deduplication_key=f"api-run-status:{saved_revision}:{status}",
                actor_type="system",
                actor_id=None,
                payload={"previous_status": current_run["status"], "status": status},
                occurred_at=at,
            )

    async def _append_run_event(
        self,
        connection: _Connection,
        *,
        workspace_id: UUID,
        run_id: UUID,
        event_type: str,
        deduplication_key: str,
        actor_type: str,
        actor_id: str | None,
        payload: Mapping[str, Any],
        occurred_at: datetime | None = None,
        step_id: UUID | None = None,
        causation_id: UUID | None = None,
    ) -> Resource:
        """Append once while serializing sequence allocation on the Run row."""

        existing = await connection.fetchrow(
            f"""SELECT {EVENT_COLUMNS} FROM run_events
            WHERE workspace_id=$1 AND run_id=$2 AND deduplication_key=$3""",
            workspace_id,
            run_id,
            deduplication_key,
        )
        if existing is not None:
            return _event_from_row(existing)
        locked = await connection.fetchval(
            "SELECT id FROM runs WHERE workspace_id=$1 AND id=$2 FOR UPDATE",
            workspace_id,
            run_id,
        )
        if locked is None:
            raise NotFoundError("run", str(run_id))
        sequence = await connection.fetchval(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id=$1",
            run_id,
        )
        event_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-event:{workspace_id}:{run_id}:{deduplication_key}",
        )
        row = await connection.fetchrow(
            f"""INSERT INTO run_events (
              id, workspace_id, run_id, step_id, sequence, event_type, schema_version,
              payload, actor_type, actor_id, occurred_at, deduplication_key,
              correlation_id, causation_id
            ) VALUES ($1,$2,$3,$4,$5,$6,'1.0.0',$7,$8,$9,$10,$11,$3,$12)
            ON CONFLICT (workspace_id, run_id, deduplication_key) DO NOTHING
            RETURNING {EVENT_COLUMNS}""",
            event_id,
            workspace_id,
            run_id,
            step_id,
            int(sequence),
            event_type,
            dict(payload),
            actor_type,
            actor_id,
            occurred_at or datetime.now(UTC),
            deduplication_key,
            causation_id,
        )
        if row is None:
            row = await connection.fetchrow(
                f"""SELECT {EVENT_COLUMNS} FROM run_events
                WHERE workspace_id=$1 AND run_id=$2 AND deduplication_key=$3""",
                workspace_id,
                run_id,
                deduplication_key,
            )
        if row is None:  # pragma: no cover - database contract violation
            raise RuntimeError("Run event could not be appended or replayed")
        return _event_from_row(row)

    async def _idempotent(
        self,
        connection: _Connection,
        *,
        workspace_id: UUID,
        operation_key: str,
        request_fingerprint: str,
        request_path: str,
        response_type: str,
        create: Callable[[], Awaitable[T]],
        response_status: int = 201,
    ) -> tuple[T, bool]:
        inserted = await connection.fetchval(
            """INSERT INTO idempotency_keys (
              workspace_id, key, request_method, request_path, request_hash, status,
              locked_until, expires_at
            ) VALUES ($1, $2, 'POST', $3, $4, 'processing', now() + interval '1 minute',
              now() + interval '24 hours')
            ON CONFLICT (workspace_id, key) DO NOTHING
            RETURNING true""",
            workspace_id,
            operation_key,
            request_path,
            request_fingerprint,
        )
        if not inserted:
            previous = await connection.fetchrow(
                """SELECT request_hash, status, response_body FROM idempotency_keys
                WHERE workspace_id = $1 AND key = $2 FOR UPDATE""",
                workspace_id,
                operation_key,
            )
            if previous is None:  # pragma: no cover - protected by the unique-key transaction
                raise RuntimeError("Idempotency record disappeared during replay")
            if previous["request_hash"] != request_fingerprint:
                raise ConflictError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was already used with a different request body",
                )
            if previous["status"] != "completed" or previous["response_body"] is None:
                raise ConflictError(
                    "IDEMPOTENT_OPERATION_IN_PROGRESS",
                    "The idempotent operation has not completed",
                )
            return _json_value(previous["response_body"]), False

        response = await create()
        resource_id = self._response_resource_id(response)
        await connection.execute(
            """UPDATE idempotency_keys SET
              status = 'completed', response_status = $6, response_body = $3,
              resource_type = $4, resource_id = $5, locked_until = NULL
            WHERE workspace_id = $1 AND key = $2""",
            workspace_id,
            operation_key,
            response,
            response_type,
            resource_id,
            response_status,
        )
        return response, True

    async def _bootstrap_official_catalog(self, connection: _Connection) -> None:
        if (
            not self._official_skills
            and not self._official_versions
            and not self._official_pipelines
        ):
            return
        pipeline_versions = tuple(seed.version for seed in self._official_pipelines)
        actor_ids = {
            UUID(resource["created_by"])
            for resource in (
                *self._official_skills,
                *self._official_versions,
                *pipeline_versions,
            )
        }
        for actor_id in sorted(actor_ids, key=str):
            await connection.execute(
                """INSERT INTO users (id, email, display_name, email_verified_at)
                VALUES ($1, $2, 'Vistora System', now()) ON CONFLICT (id) DO NOTHING""",
                actor_id,
                f"system+{actor_id}@framefactory.invalid",
            )
        workspace_ids = {
            UUID(resource["workspace_id"])
            for resource in (
                *self._official_skills,
                *self._official_versions,
                *pipeline_versions,
            )
        }
        for workspace_id in sorted(workspace_ids, key=str):
            await connection.execute(
                """INSERT INTO workspaces (id, kind, slug, name, owner_user_id)
                VALUES ($1, 'system', $2, 'Vistora Official', NULL)
                ON CONFLICT (id) DO NOTHING""",
                workspace_id,
                f"official-{str(workspace_id).replace('-', '')[:16]}",
            )
        for pipeline in self._official_pipelines:
            await self._insert_official_pipeline(connection, pipeline.pipeline_id, pipeline.version)
        for skill in self._official_skills:
            seed = dict(skill)
            seed["current_version_id"] = None
            await self._insert_official_skill(connection, seed)
        for version in self._official_versions:
            await self._insert_official_version(connection, version)
        for skill in self._official_skills:
            await connection.execute(
                """UPDATE skills SET current_version_id = $2
                WHERE id = $1 AND ownership_type = 'system'""",
                UUID(skill["id"]),
                self._optional_uuid(skill["current_version_id"]),
            )

    async def _insert_official_pipeline(
        self,
        connection: _Connection,
        pipeline_id: str,
        version: Resource,
    ) -> None:
        await connection.execute(
            """INSERT INTO pipelines (
              id, workspace_id, name, slug, description, visibility, status,
              current_version_id, created_by, created_at, updated_at
            ) VALUES (
              $1, $2, $3, $4, $5, 'public_readonly', $6, NULL, $7, $8, $8
            ) ON CONFLICT (id) DO NOTHING""",
            UUID(pipeline_id),
            UUID(version["workspace_id"]),
            version["name"],
            version["slug"],
            version["description"],
            version["status"],
            UUID(version["created_by"]),
            _datetime_value(version["created_at"]),
        )
        await connection.execute(
            """INSERT INTO pipeline_versions (
              id, workspace_id, pipeline_id, version, schema_version, state, graph,
              capability_requirements, output_contract, content_hash, created_by,
              created_at, published_at
            ) VALUES (
              $1, $2, $3, $4, $5, 'published', $6, $7, '{}'::jsonb, $8, $9, $10, $11
            ) ON CONFLICT (id) DO NOTHING""",
            UUID(version["id"]),
            UUID(version["workspace_id"]),
            UUID(pipeline_id),
            version["version"],
            version["schema_version"],
            {"nodes": version["nodes"]},
            version["capability_requirements"],
            version["content_hash"],
            UUID(version["created_by"]),
            _datetime_value(version["created_at"]),
            _datetime_value(version["published_at"]),
        )
        pipeline_matches = await connection.fetchval(
            """SELECT EXISTS (
              SELECT 1 FROM pipeline_versions
              WHERE id = $1 AND workspace_id = $2 AND pipeline_id = $3
                AND content_hash = $4
                AND graph = $5::jsonb
                AND capability_requirements = $6::jsonb
            )""",
            UUID(version["id"]),
            UUID(version["workspace_id"]),
            UUID(pipeline_id),
            version["content_hash"],
            {"nodes": version["nodes"]},
            version["capability_requirements"],
        )
        if not pipeline_matches:
            raise RuntimeError(
                f"Official PipelineVersion {version['id']} conflicts with persisted content"
            )
        await connection.execute(
            """UPDATE pipelines SET current_version_id = $2,
              name = $4,
              description = $5,
              status = $6,
              updated_at = GREATEST(updated_at, $7)
            WHERE id = $1 AND workspace_id = $3 AND visibility = 'public_readonly'""",
            UUID(pipeline_id),
            UUID(version["id"]),
            UUID(version["workspace_id"]),
            version["name"],
            version["description"],
            version["status"],
            _datetime_value(version["published_at"]),
        )

    async def _insert_official_skill(self, connection: _Connection, resource: Resource) -> None:
        await connection.execute(
            """INSERT INTO skills (
              id, workspace_id, ownership_type, publisher_type, publisher_name, name, slug,
              description, visibility, status, current_version_id, forked_from_skill_id,
              revision, schema_version, created_by, created_at, updated_at
            ) VALUES (
              $1, $2, 'system', 'system', $3, $4, $5, $6, 'public_readonly', $7,
              NULL, $8, $9, $10, $11, $12, $13
            ) ON CONFLICT (id) DO NOTHING""",
            UUID(resource["id"]),
            UUID(resource["workspace_id"]),
            resource["publisher_name"],
            resource["name"],
            resource["slug"],
            resource["description"],
            resource["status"],
            self._optional_uuid(resource["forked_from_skill_id"]),
            resource.get("revision", 1),
            resource["schema_version"],
            UUID(resource["created_by"]),
            _datetime_value(resource["created_at"]),
            _datetime_value(resource["updated_at"]),
        )

    async def _insert_official_version(self, connection: _Connection, resource: Resource) -> None:
        await connection.execute(
            """INSERT INTO skill_versions (
              id, workspace_id, ownership_type, skill_id, version, schema_version, state,
              execution_kind, input_schema, research_policy, writing_policy, visual_policy,
              asset_policy, qc_policy, output_contract, capability_requirements,
              default_pipeline_version_id, content_hash, revision, test_topics, release_notes,
              created_by, created_at, published_at
            ) VALUES (
              $1, $2, 'system', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
              $14, $15, $16, $17, $18, $19, $20, $21, $22, $23
            ) ON CONFLICT (id) DO NOTHING""",
            UUID(resource["id"]),
            UUID(resource["workspace_id"]),
            UUID(resource["skill_id"]),
            resource["version"],
            resource["schema_version"],
            resource["state"],
            resource["execution_kind"],
            resource["input_schema"],
            resource["research_policy"],
            resource["writing_policy"],
            resource["visual_policy"],
            resource["asset_policy"],
            resource["qc_policy"],
            resource["output_contract"],
            resource["capability_requirements"],
            self._optional_uuid(resource["default_pipeline_version_id"]),
            resource["content_hash"],
            resource.get("revision", 1),
            resource.get("test_topics", []),
            resource.get("release_notes", ""),
            UUID(resource["created_by"]),
            _datetime_value(resource["created_at"]),
            _datetime_value(resource["published_at"]),
        )
        version_matches = await connection.fetchval(
            """SELECT EXISTS (
              SELECT 1 FROM skill_versions
              WHERE id = $1 AND workspace_id = $2 AND skill_id = $3
                AND content_hash = $4
                AND input_schema = $5::jsonb
                AND research_policy = $6::jsonb
                AND writing_policy = $7::jsonb
                AND visual_policy = $8::jsonb
                AND asset_policy = $9::jsonb
                AND qc_policy = $10::jsonb
                AND capability_requirements = $11::jsonb
                AND output_contract = $12::jsonb
                AND default_pipeline_version_id IS NOT DISTINCT FROM $13
            )""",
            UUID(resource["id"]),
            UUID(resource["workspace_id"]),
            UUID(resource["skill_id"]),
            resource["content_hash"],
            resource["input_schema"],
            resource["research_policy"],
            resource["writing_policy"],
            resource["visual_policy"],
            resource["asset_policy"],
            resource["qc_policy"],
            resource["capability_requirements"],
            resource["output_contract"],
            self._optional_uuid(resource["default_pipeline_version_id"]),
        )
        if not version_matches:
            raise RuntimeError(
                f"Official SkillVersion {resource['id']} conflicts with persisted content"
            )

    @staticmethod
    def _optional_uuid(value: str | UUID | None) -> UUID | None:
        return None if value is None else UUID(str(value))

    @staticmethod
    def _require_revision(current: int, expected: int | None) -> None:
        if expected is not None and current != expected:
            raise PreconditionFailedError(current, expected)

    @staticmethod
    def _response_resource_id(response: Any) -> UUID | None:
        if isinstance(response, Mapping):
            candidate = response.get("id")
            if candidate is None and isinstance(response.get("skill"), Mapping):
                candidate = response["skill"].get("id")
            if candidate is None:
                return None
            try:
                return UUID(str(candidate))
            except ValueError:
                # Worker step identifiers are stable compound strings (run UUID + node key),
                # while the idempotency ledger's optional resource_id column is UUID-only.
                return None
        return None

    @staticmethod
    def _raise_database_error(exc: Exception, resource_type: str, resource: Resource) -> None:
        sqlstate = getattr(exc, "sqlstate", None)
        constraint = getattr(exc, "constraint_name", "") or ""
        if sqlstate == "23505":
            if resource_type == "channel" and "slug" in constraint:
                raise ConflictError(
                    "CHANNEL_SLUG_ALREADY_EXISTS",
                    "A Channel with this slug already exists in the workspace",
                    slug=resource.get("slug"),
                ) from exc
            if resource_type == "channel" and "platform_handle" in constraint:
                raise ConflictError(
                    "CHANNEL_HANDLE_ALREADY_EXISTS",
                    "This platform handle already belongs to a Channel",
                    platform=resource.get("platform"),
                    handle=resource.get("handle"),
                ) from exc
            if resource_type == "skill" and "slug" in constraint:
                raise ConflictError(
                    "SKILL_SLUG_ALREADY_EXISTS",
                    "A skill with this slug already exists in the workspace",
                    slug=resource.get("slug"),
                ) from exc
            if resource_type == "skill_version" and constraint.endswith("skill_id_version_key"):
                raise ConflictError(
                    "SKILL_VERSION_NUMBER_EXISTS",
                    "This semantic version already exists for the skill",
                    version=resource.get("version"),
                ) from exc
            codes = {
                "skill": ("SKILL_ALREADY_EXISTS", "A skill with this id already exists"),
                "channel": ("CHANNEL_ALREADY_EXISTS", "A Channel with this id already exists"),
                "skill_version": (
                    "SKILL_VERSION_ALREADY_EXISTS",
                    "A skill version with this id already exists",
                ),
                "run": ("RUN_ALREADY_EXISTS", "A Run with this id already exists"),
                "webpage_video_run": (
                    "WEBPAGE_VIDEO_RUN_ALREADY_EXISTS",
                    "A webpage-video Run with this id already exists",
                ),
                "webpage_capture_attempt": (
                    "WEBPAGE_CAPTURE_ATTEMPT_CONFLICT",
                    "The immutable webpage capture attempt conflicts with persisted evidence",
                ),
                "skill_test_execution": (
                    "SKILL_TEST_ALREADY_EXISTS",
                    "A Skill test execution with this id already exists",
                ),
                "api_key": (
                    "API_KEY_COLLISION",
                    "Could not allocate a unique API key",
                ),
            }
            code, message = codes[resource_type]
            raise ConflictError(code, message) from exc
        if sqlstate in {"23503", "23514"}:
            raise ConflictError(
                "RESOURCE_REFERENCE_INVALID",
                "The resource references a missing or incompatible persisted resource",
                resource_type=resource_type,
            ) from exc
        if sqlstate == "55000" and resource_type == "skill_version":
            raise ConflictError(
                "IMMUTABLE_SKILL_VERSION", "Published SkillVersions cannot be changed"
            ) from exc
        raise exc
