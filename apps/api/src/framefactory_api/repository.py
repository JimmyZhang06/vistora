from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from .account import AccountRepository
from .asset_policy import normalized_tags, validate_asset_transition
from .errors import ConflictError, NotFoundError, PreconditionFailedError

Resource = dict[str, Any]


def _catalog_snapshot_content_hash(library_ids: Sequence[str], items: Sequence[Resource]) -> str:
    """Hash only frozen catalog identity, using a stable order and encoding."""

    normalized_library_ids = sorted({str(value) for value in library_ids})
    normalized_items = sorted(
        (
            {
                "library_id": str(item["library_id"]),
                "asset_id": str(item["asset_id"]),
                "asset_file_id": str(item["asset_file_id"]),
                "analysis_id": str(item["analysis_id"]),
                "content_hash": str(item["content_hash"]),
            }
            for item in items
        ),
        key=lambda item: (
            item["library_id"],
            item["asset_id"],
            item["asset_file_id"],
            item["analysis_id"],
            item["content_hash"],
        ),
    )
    payload = {
        "schema_version": "1.0.0",
        "library_ids": normalized_library_ids,
        "items": normalized_items,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SkillRepository(Protocol):
    async def list_skills(self, workspace_id: UUID) -> list[Resource]: ...

    async def get_skill(self, workspace_id: UUID, skill_id: UUID) -> Resource: ...

    async def create_skill(self, resource: Resource) -> Resource: ...

    async def create_skill_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]: ...

    async def replace_skill(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource: ...

    async def delete_skill(self, workspace_id: UUID, skill_id: UUID) -> None: ...

    async def list_skill_versions(
        self, workspace_id: UUID, skill_id: UUID | None = None
    ) -> list[Resource]: ...

    async def get_skill_version(self, workspace_id: UUID, version_id: UUID) -> Resource: ...

    async def create_skill_version(self, resource: Resource) -> Resource: ...

    async def create_skill_version_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]: ...

    async def replace_skill_version(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource: ...

    async def delete_skill_version(
        self, workspace_id: UUID, version_id: UUID, *, expected_revision: int
    ) -> None: ...

    async def fork_skill_idempotently(
        self,
        skill: Resource,
        version: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[tuple[Resource, Resource], bool]: ...


class ChannelRepository(Protocol):
    async def get_platform_connection(
        self, workspace_id: UUID, connection_id: UUID
    ) -> Resource: ...

    async def list_channels(
        self,
        workspace_id: UUID,
        *,
        search: str | None = None,
        platform: str | None = None,
        status: str | None = None,
    ) -> list[Resource]: ...

    async def get_channel(self, workspace_id: UUID, channel_id: UUID) -> Resource: ...

    async def create_channel(self, resource: Resource) -> Resource: ...

    async def create_channel_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]: ...

    async def replace_channel(self, resource: Resource, *, expected_revision: int) -> Resource: ...

    async def archive_channel(
        self, workspace_id: UUID, channel_id: UUID, *, expected_revision: int
    ) -> Resource: ...


class RunRepository(Protocol):
    async def get_pipeline_version(self, workspace_id: UUID, version_id: UUID) -> Resource: ...

    async def list_runs(self, workspace_id: UUID) -> list[Resource]: ...

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> Resource: ...

    async def create_run_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def cancel_run_idempotently(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def list_run_steps(self, workspace_id: UUID, run_id: UUID) -> list[Resource]: ...

    async def get_run_step(self, workspace_id: UUID, step_id: str) -> Resource: ...

    async def retry_run_step_idempotently(
        self,
        workspace_id: UUID,
        step_id: str,
        *,
        actor_id: UUID,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

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
    ) -> tuple[Resource, bool]: ...

    async def list_artifacts(self, workspace_id: UUID, run_id: UUID) -> list[Resource]: ...

    async def get_artifact(self, workspace_id: UUID, artifact_id: UUID) -> Resource: ...

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 101,
    ) -> list[Resource]: ...

    async def get_event(self, workspace_id: UUID, event_id: UUID) -> Resource: ...


class FullAiRepository(Protocol):
    async def find_full_ai_run_by_idempotency_key(
        self, workspace_id: UUID, idempotency_key: str
    ) -> Resource | None: ...

    async def create_full_ai_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def get_full_ai_run(self, workspace_id: UUID, full_ai_run_id: UUID) -> Resource: ...


class WebpageVideoRepository(Protocol):
    async def create_webpage_video_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
        active_run_limit: int,
    ) -> tuple[Resource, bool]: ...

    async def count_active_webpage_video_runs(self, workspace_id: UUID) -> int: ...

    async def get_webpage_video_run(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource: ...

    async def append_webpage_capture_attempt(self, resource: Resource) -> tuple[Resource, bool]: ...

    async def list_webpage_capture_attempts(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> list[Resource]: ...

    async def get_webpage_pilot_feedback(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource | None: ...

    async def list_webpage_pilot_feedback(
        self, workspace_id: UUID, *, limit: int
    ) -> tuple[list[Resource], int]: ...

    async def upsert_webpage_pilot_feedback_idempotently(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...


class GenerationBatchRepository(Protocol):
    async def list_generation_batches(self, workspace_id: UUID) -> list[Resource]: ...

    async def get_generation_batch(self, workspace_id: UUID, batch_id: UUID) -> Resource: ...

    async def list_generation_batch_items(
        self,
        workspace_id: UUID,
        batch_id: UUID,
        *,
        status: str | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Resource], int]: ...

    async def create_generation_batch_idempotently(
        self,
        batch: Resource,
        runs: list[Resource],
        items: list[Resource],
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...


class CatalogRepository(Protocol):
    async def create_or_reuse_catalog_snapshot(
        self,
        workspace_id: UUID,
        library_ids: Sequence[UUID],
        *,
        created_by: UUID | None = None,
    ) -> Resource: ...

    async def get_catalog_snapshot(self, workspace_id: UUID, snapshot_id: UUID) -> Resource: ...

    async def create_library_build_job_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def get_library_build_job(self, workspace_id: UUID, job_id: UUID) -> Resource: ...

    async def cancel_library_build_job(
        self,
        workspace_id: UUID,
        job_id: UUID,
        *,
        expected_revision: int,
    ) -> Resource: ...


class AssetRepository(Protocol):
    async def create_document_source_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def get_document_source(self, workspace_id: UUID, source_id: UUID) -> Resource: ...

    async def complete_document_source(
        self, workspace_id: UUID, source_id: UUID, *, byte_size: int
    ) -> Resource: ...

    async def update_document_retention(
        self,
        workspace_id: UUID,
        source_id: UUID,
        *,
        retention_until: datetime | None,
        reason: str,
        actor_id: UUID,
        expected_revision: int,
    ) -> Resource: ...

    async def set_document_legal_hold(
        self,
        workspace_id: UUID,
        source_id: UUID,
        *,
        active: bool,
        reason: str,
        actor_id: UUID,
        expected_revision: int,
    ) -> Resource: ...

    async def request_document_purge_idempotently(
        self,
        resource: Resource,
        *,
        actor_id: UUID,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...

    async def get_document_purge_request(
        self, workspace_id: UUID, request_id: UUID
    ) -> Resource: ...

    async def list_asset_libraries(self, workspace_id: UUID) -> list[Resource]: ...

    async def get_asset_library(self, workspace_id: UUID, library_id: UUID) -> Resource: ...

    async def create_asset_library(self, resource: Resource) -> Resource: ...

    async def create_asset_upload(self, resource: Resource) -> Resource: ...

    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Resource: ...

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
    ) -> list[Resource]: ...

    async def update_asset_metadata(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        actor_id: UUID,
    ) -> Resource: ...

    async def transition_asset_status(
        self,
        workspace_id: UUID,
        asset_id: UUID,
        *,
        target_status: str,
        expected_revision: int,
        actor_id: UUID,
        reason: str,
    ) -> Resource: ...

    async def list_asset_related(
        self, workspace_id: UUID, asset_id: UUID, relation: str
    ) -> list[Resource]: ...

    async def list_asset_import_jobs(
        self, workspace_id: UUID, library_id: UUID
    ) -> list[Resource]: ...

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
    ) -> tuple[Resource, bool]: ...

    async def run_asset_batch_idempotently(
        self,
        workspace_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
        request_path: str,
        action: Callable[[], Awaitable[Resource]],
    ) -> tuple[Resource, bool]: ...

    async def complete_asset_upload(
        self, workspace_id: UUID, asset_id: UUID, *, byte_size: int
    ) -> Resource: ...


class SkillTestExecutionRepository(Protocol):
    async def get_skill_test_execution(
        self, workspace_id: UUID, execution_id: UUID
    ) -> Resource: ...

    async def list_skill_test_executions(self, workspace_id: UUID) -> list[Resource]: ...

    async def create_skill_test_execution_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...


class ControlRepository(
    SkillRepository,
    ChannelRepository,
    RunRepository,
    FullAiRepository,
    WebpageVideoRepository,
    GenerationBatchRepository,
    CatalogRepository,
    AssetRepository,
    SkillTestExecutionRepository,
    AccountRepository,
    Protocol,
):
    """Persistence port implemented in memory today and by PostgreSQL later."""


@dataclass(frozen=True, slots=True)
class _IdempotentRecord:
    request_fingerprint: str
    response: Any


class InMemoryControlRepository:
    """Process-local implementation for UI integration and API contract tests.

    It intentionally exposes the same repository port a transactional PostgreSQL adapter will
    implement. Data disappears on restart and must not be used as production persistence.
    """

    def __init__(
        self,
        *,
        channels: list[Resource] | None = None,
        platform_connections: list[Resource] | None = None,
        skills: list[Resource] | None = None,
        skill_versions: list[Resource] | None = None,
        pipelines: list[Resource] | None = None,
        runs: list[Resource] | None = None,
        run_steps: list[Resource] | None = None,
        artifacts: list[Resource] | None = None,
        events: list[Resource] | None = None,
        generation_batches: list[Resource] | None = None,
        generation_batch_items: list[Resource] | None = None,
        asset_libraries: list[Resource] | None = None,
        assets: list[Resource] | None = None,
        document_sources: list[Resource] | None = None,
        document_purge_requests: list[Resource] | None = None,
        catalog_snapshots: list[Resource] | None = None,
        library_build_jobs: list[Resource] | None = None,
    ) -> None:
        # Official catalog entries and user resources share one model and one repository path.
        self._channels: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (channels or [])
        }
        self._platform_connections: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (platform_connections or [])
        }
        self._skills: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (skills or [])
        }
        self._versions: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (skill_versions or [])
        }
        self._pipelines: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (pipelines or [])
        }
        self._runs: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (runs or [])
        }
        self._full_ai_runs: dict[str, Resource] = {}
        self._full_ai_paid_operations: dict[str, Resource] = {}
        self._webpage_video_runs: dict[str, Resource] = {}
        self._webpage_capture_attempts: dict[str, Resource] = {}
        self._webpage_pilot_feedback: dict[str, Resource] = {}
        self._run_steps: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (run_steps or [])
        }
        self._artifacts: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (artifacts or [])
        }
        self._events: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (events or [])
        }
        self._generation_batches: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (generation_batches or [])
        }
        self._generation_batch_items: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (generation_batch_items or [])
        }
        self._asset_libraries: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (asset_libraries or [])
        }
        self._assets: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (assets or [])
        }
        self._document_sources: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (document_sources or [])
        }
        self._document_purge_requests: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (document_purge_requests or [])
        }
        for source in self._document_sources.values():
            source.setdefault("retention_until", None)
            source.setdefault("legal_hold", False)
            source.setdefault("legal_hold_reason", None)
            source.setdefault("legal_hold_set_by", None)
            source.setdefault("legal_hold_set_at", None)
            source.setdefault("deletion_requested_at", None)
            source.setdefault("purged_at", None)
        for asset in self._assets.values():
            asset.setdefault("revision", 1)
            asset.setdefault("analysis_status", "pending")
            asset.setdefault("deleted_at", None)
            asset.setdefault("tags", normalized_tags(asset))
        self._catalog_snapshots: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (catalog_snapshots or [])
        }
        self._catalog_snapshots_by_hash: dict[tuple[str, str], str] = {
            (resource["workspace_id"], resource["content_hash"]): resource["id"]
            for resource in self._catalog_snapshots.values()
        }
        self._library_build_jobs: dict[str, Resource] = {
            resource["id"]: deepcopy(resource) for resource in (library_build_jobs or [])
        }
        self._asset_analysis_jobs: dict[str, Resource] = {}
        self._asset_audit_events: dict[str, Resource] = {}
        self._test_executions: dict[str, Resource] = {}
        self._profiles: dict[tuple[str, str], Resource] = {}
        self._creation_preferences: dict[tuple[str, str], Resource] = {}
        self._account_sessions: dict[str, Resource] = {}
        self._api_keys: dict[str, Resource] = {}
        self._idempotency: dict[str, _IdempotentRecord] = {}
        self._lock = asyncio.Lock()
        self._asset_batch_lock = asyncio.Lock()

    async def ensure_account(
        self,
        user_id: UUID,
        workspace_id: UUID,
        *,
        email: str,
        display_name: str,
    ) -> None:
        key = (str(workspace_id), str(user_id))
        now = self._now()
        async with self._lock:
            self._profiles.setdefault(
                key,
                {
                    "user_id": str(user_id),
                    "workspace_id": str(workspace_id),
                    "email": email,
                    "display_name": display_name,
                    "avatar_url": None,
                    "locale": "zh-CN",
                    "timezone": "Asia/Shanghai",
                    "revision": 1,
                    "updated_at": now,
                },
            )
            self._creation_preferences.setdefault(
                key,
                {
                    "user_id": str(user_id),
                    "workspace_id": str(workspace_id),
                    "default_language": "zh-CN",
                    "default_aspect_ratio": "16:9",
                    "default_duration_seconds": 180,
                    "default_visibility": "private",
                    "auto_quality_check": True,
                    "revision": 1,
                    "updated_at": now,
                },
            )

    async def get_account_profile(self, workspace_id: UUID, user_id: UUID) -> Resource:
        resource = self._profiles.get((str(workspace_id), str(user_id)))
        if resource is None:
            raise NotFoundError("account_profile", str(user_id))
        return deepcopy(resource)

    async def replace_account_profile(
        self, resource: Resource, *, expected_revision: int
    ) -> Resource:
        key = (resource["workspace_id"], resource["user_id"])
        async with self._lock:
            current = self._profiles.get(key)
            if current is None:
                raise NotFoundError("account_profile", resource["user_id"])
            self._check_revision(current, expected_revision)
            self._profiles[key] = deepcopy(resource)
            return deepcopy(resource)

    async def get_creation_preferences(self, workspace_id: UUID, user_id: UUID) -> Resource:
        resource = self._creation_preferences.get((str(workspace_id), str(user_id)))
        if resource is None:
            raise NotFoundError("creation_preferences", str(user_id))
        return deepcopy(resource)

    async def replace_creation_preferences(
        self, resource: Resource, *, expected_revision: int
    ) -> Resource:
        key = (resource["workspace_id"], resource["user_id"])
        async with self._lock:
            current = self._creation_preferences.get(key)
            if current is None:
                raise NotFoundError("creation_preferences", resource["user_id"])
            self._check_revision(current, expected_revision)
            self._creation_preferences[key] = deepcopy(resource)
            return deepcopy(resource)

    async def list_account_sessions(self, workspace_id: UUID, user_id: UUID) -> list[Resource]:
        return self._sorted_copy(
            self._public_session(session)
            for session in self._account_sessions.values()
            if session["workspace_id"] == str(workspace_id) and session["user_id"] == str(user_id)
        )

    async def revoke_account_session(
        self, workspace_id: UUID, user_id: UUID, session_id: UUID
    ) -> Resource:
        async with self._lock:
            session = self._account_sessions.get(str(session_id))
            if (
                session is None
                or session["workspace_id"] != str(workspace_id)
                or session["user_id"] != str(user_id)
            ):
                raise NotFoundError("session", str(session_id))
            if session["revoked_at"] is None:
                session["revoked_at"] = self._now()
            return deepcopy(session)

    async def list_api_keys(self, workspace_id: UUID, user_id: UUID) -> list[Resource]:
        del user_id
        return self._sorted_copy(
            self._public_api_key(key)
            for key in self._api_keys.values()
            if key["workspace_id"] == str(workspace_id)
        )

    async def create_api_key(self, resource: Resource) -> Resource:
        async with self._lock:
            if any(key["key_hash"] == resource["key_hash"] for key in self._api_keys.values()):
                raise ConflictError("API_KEY_COLLISION", "Could not allocate a unique API key")
            self._api_keys[resource["id"]] = deepcopy(resource)
            return self._public_api_key(resource)

    async def revoke_api_key(self, workspace_id: UUID, user_id: UUID, key_id: UUID) -> Resource:
        del user_id
        async with self._lock:
            key = self._api_keys.get(str(key_id))
            if key is None or key["workspace_id"] != str(workspace_id):
                raise NotFoundError("api_key", str(key_id))
            if key["revoked_at"] is None:
                key["revoked_at"] = self._now()
            return self._public_api_key(key)

    @staticmethod
    def _visible(resource: Resource, workspace_id: UUID) -> bool:
        return resource["ownership_type"] == "system" or resource["workspace_id"] == str(
            workspace_id
        )

    async def list_skills(self, workspace_id: UUID) -> list[Resource]:
        return self._sorted_copy(
            resource for resource in self._skills.values() if self._visible(resource, workspace_id)
        )

    async def get_skill(self, workspace_id: UUID, skill_id: UUID) -> Resource:
        resource = self._skills.get(str(skill_id))
        if resource is None or not self._visible(resource, workspace_id):
            raise NotFoundError("skill", str(skill_id))
        return deepcopy(resource)

    async def list_channels(
        self,
        workspace_id: UUID,
        *,
        search: str | None = None,
        platform: str | None = None,
        status: str | None = None,
    ) -> list[Resource]:
        needle = search.casefold() if search is not None else None
        return self._sorted_copy(
            channel
            for channel in self._channels.values()
            if channel["workspace_id"] == str(workspace_id)
            and (
                needle is None
                or needle
                in " ".join(
                    str(channel.get(field) or "")
                    for field in ("name", "slug", "description", "handle")
                ).casefold()
            )
            and (platform is None or channel.get("platform") == platform)
            and (status is None or channel.get("status") == status)
        )

    async def get_platform_connection(self, workspace_id: UUID, connection_id: UUID) -> Resource:
        connection = self._platform_connections.get(str(connection_id))
        if connection is None or connection["workspace_id"] != str(workspace_id):
            raise NotFoundError("platform_connection", str(connection_id))
        public = deepcopy(connection)
        public.pop("secret_ref", None)
        return public

    async def get_channel(self, workspace_id: UUID, channel_id: UUID) -> Resource:
        channel = self._channels.get(str(channel_id))
        if channel is None or channel["workspace_id"] != str(workspace_id):
            raise NotFoundError("channel", str(channel_id))
        return deepcopy(channel)

    async def create_channel(self, resource: Resource) -> Resource:
        async with self._lock:
            return self._create_channel_unlocked(resource)

    async def create_channel_idempotently(
        self, resource: Resource, *, operation_key: str, request_fingerprint: str
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            created = self._create_channel_unlocked(resource)
            self._record_idempotent(operation_key, request_fingerprint, created)
            return created, True

    def _create_channel_unlocked(self, resource: Resource) -> Resource:
        resource_id = resource["id"]
        if resource_id in self._channels:
            raise ConflictError("CHANNEL_ALREADY_EXISTS", "A Channel with this id already exists")
        if any(
            channel["workspace_id"] == resource["workspace_id"]
            and channel["slug"] == resource["slug"]
            for channel in self._channels.values()
        ):
            raise ConflictError(
                "CHANNEL_SLUG_ALREADY_EXISTS",
                "A Channel with this slug already exists in the workspace",
                slug=resource["slug"],
            )
        if (
            resource["platform"] is not None
            and resource["handle"] is not None
            and any(
                channel["workspace_id"] == resource["workspace_id"]
                and channel["platform"] == resource["platform"]
                and channel["handle"] == resource["handle"]
                for channel in self._channels.values()
            )
        ):
            raise ConflictError(
                "CHANNEL_HANDLE_ALREADY_EXISTS",
                "This platform handle already belongs to a Channel",
                platform=resource["platform"],
                handle=resource["handle"],
            )
        self._channels[resource_id] = deepcopy(resource)
        return deepcopy(resource)

    async def replace_channel(self, resource: Resource, *, expected_revision: int) -> Resource:
        async with self._lock:
            resource_id = resource["id"]
            current = self._channels.get(resource_id)
            if current is None or current["workspace_id"] != resource["workspace_id"]:
                raise NotFoundError("channel", resource_id)
            self._check_revision(current, expected_revision)
            if any(
                channel_id != resource_id
                and channel["workspace_id"] == resource["workspace_id"]
                and channel["slug"] == resource["slug"]
                for channel_id, channel in self._channels.items()
            ):
                raise ConflictError(
                    "CHANNEL_SLUG_ALREADY_EXISTS",
                    "A Channel with this slug already exists in the workspace",
                    slug=resource["slug"],
                )
            if (
                resource["platform"] is not None
                and resource["handle"] is not None
                and any(
                    channel_id != resource_id
                    and channel["workspace_id"] == resource["workspace_id"]
                    and channel["platform"] == resource["platform"]
                    and channel["handle"] == resource["handle"]
                    for channel_id, channel in self._channels.items()
                )
            ):
                raise ConflictError(
                    "CHANNEL_HANDLE_ALREADY_EXISTS",
                    "This platform handle already belongs to a Channel",
                    platform=resource["platform"],
                    handle=resource["handle"],
                )
            self._channels[resource_id] = deepcopy(resource)
            return deepcopy(resource)

    async def archive_channel(
        self, workspace_id: UUID, channel_id: UUID, *, expected_revision: int
    ) -> Resource:
        async with self._lock:
            current = self._channels.get(str(channel_id))
            if current is None or current["workspace_id"] != str(workspace_id):
                raise NotFoundError("channel", str(channel_id))
            self._check_revision(current, expected_revision)
            archived = deepcopy(current)
            archived["status"] = "archived"
            archived["revision"] = int(current["revision"]) + 1
            archived["updated_at"] = self._now()
            self._channels[str(channel_id)] = archived
            return deepcopy(archived)

    async def create_skill(self, resource: Resource) -> Resource:
        async with self._lock:
            resource_id = resource["id"]
            if resource_id in self._skills:
                raise ConflictError("SKILL_ALREADY_EXISTS", "A skill with this id already exists")
            if any(
                item["workspace_id"] == resource["workspace_id"]
                and item["slug"] == resource["slug"]
                for item in self._skills.values()
            ):
                raise ConflictError(
                    "SKILL_SLUG_ALREADY_EXISTS",
                    "A skill with this slug already exists in the workspace",
                    slug=resource["slug"],
                )
            self._skills[resource_id] = deepcopy(resource)
            return deepcopy(resource)

    async def create_skill_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            self._ensure_skill_can_be_created(resource)
            self._skills[resource["id"]] = deepcopy(resource)
            self._record_idempotent(operation_key, request_fingerprint, resource)
            return deepcopy(resource), True

    async def replace_skill(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource:
        async with self._lock:
            resource_id = resource["id"]
            if resource_id not in self._skills:
                raise NotFoundError("skill", resource_id)
            self._check_revision(self._skills[resource_id], expected_revision)
            if any(
                item_id != resource_id
                and item["workspace_id"] == resource["workspace_id"]
                and item["slug"] == resource["slug"]
                for item_id, item in self._skills.items()
            ):
                raise ConflictError(
                    "SKILL_SLUG_ALREADY_EXISTS",
                    "A skill with this slug already exists in the workspace",
                    slug=resource["slug"],
                )
            self._skills[resource_id] = deepcopy(resource)
            return deepcopy(resource)

    async def delete_skill(self, workspace_id: UUID, skill_id: UUID) -> None:
        async with self._lock:
            current = self._skills.get(str(skill_id))
            if current is None or not self._visible(current, workspace_id):
                raise NotFoundError("skill", str(skill_id))
            if current["ownership_type"] == "system":
                raise ConflictError(
                    "SYSTEM_SKILL_IMMUTABLE", "System-owned skills cannot be deleted"
                )
            if current["current_version_id"] is not None:
                raise ConflictError(
                    "PUBLISHED_SKILL_IMMUTABLE",
                    "A Skill with published history cannot be deleted",
                )
            version_ids = [
                version_id
                for version_id, version in self._versions.items()
                if version["skill_id"] == str(skill_id)
            ]
            if any(
                run["composition_snapshot"]["skill_version"]["id"] in version_ids
                for run in self._runs.values()
            ):
                raise ConflictError("SKILL_IN_USE", "A Run references this Skill")
            del self._skills[str(skill_id)]
            for version_id in version_ids:
                del self._versions[version_id]

    async def list_skill_versions(
        self, workspace_id: UUID, skill_id: UUID | None = None
    ) -> list[Resource]:
        return self._sorted_copy(
            resource
            for resource in self._versions.values()
            if self._visible(resource, workspace_id)
            and (skill_id is None or resource["skill_id"] == str(skill_id))
        )

    async def get_skill_version(self, workspace_id: UUID, version_id: UUID) -> Resource:
        resource = self._versions.get(str(version_id))
        if resource is None or not self._visible(resource, workspace_id):
            raise NotFoundError("skill_version", str(version_id))
        return deepcopy(resource)

    async def create_skill_version(self, resource: Resource) -> Resource:
        async with self._lock:
            resource_id = resource["id"]
            if resource_id in self._versions:
                raise ConflictError(
                    "SKILL_VERSION_ALREADY_EXISTS", "A skill version with this id already exists"
                )
            if any(
                item["skill_id"] == resource["skill_id"] and item["version"] == resource["version"]
                for item in self._versions.values()
            ):
                raise ConflictError(
                    "SKILL_VERSION_NUMBER_EXISTS",
                    "This semantic version already exists for the skill",
                    version=resource["version"],
                )
            self._versions[resource_id] = deepcopy(resource)
            return deepcopy(resource)

    async def create_skill_version_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            self._ensure_version_can_be_created(resource)
            self._versions[resource["id"]] = deepcopy(resource)
            self._record_idempotent(operation_key, request_fingerprint, resource)
            return deepcopy(resource), True

    async def replace_skill_version(
        self, resource: Resource, *, expected_revision: int | None = None
    ) -> Resource:
        async with self._lock:
            resource_id = resource["id"]
            if resource_id not in self._versions:
                raise NotFoundError("skill_version", resource_id)
            self._check_revision(self._versions[resource_id], expected_revision)
            self._versions[resource_id] = deepcopy(resource)
            return deepcopy(resource)

    async def delete_skill_version(
        self, workspace_id: UUID, version_id: UUID, *, expected_revision: int
    ) -> None:
        async with self._lock:
            resource = self._versions.get(str(version_id))
            if resource is None or not self._visible(resource, workspace_id):
                raise NotFoundError("skill_version", str(version_id))
            self._check_revision(resource, expected_revision)
            if resource["state"] != "draft":
                raise ConflictError(
                    "IMMUTABLE_SKILL_VERSION", "Only draft SkillVersions can be deleted"
                )
            if any(
                run["composition_snapshot"]["skill_version"]["id"] == str(version_id)
                for run in self._runs.values()
            ):
                raise ConflictError("SKILL_VERSION_IN_USE", "The draft is referenced by a Run")
            del self._versions[str(version_id)]

    async def fork_skill_idempotently(
        self,
        skill: Resource,
        version: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[tuple[Resource, Resource], bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                replay_skill, replay_version = replay
                return (replay_skill, replay_version), False
            self._ensure_skill_can_be_created(skill)
            self._ensure_version_can_be_created(version)
            self._skills[skill["id"]] = deepcopy(skill)
            self._versions[version["id"]] = deepcopy(version)
            response = (skill, version)
            self._record_idempotent(operation_key, request_fingerprint, response)
            return (deepcopy(skill), deepcopy(version)), True

    async def list_runs(self, workspace_id: UUID) -> list[Resource]:
        return self._sorted_copy(
            self._public_run(resource)
            for resource in self._runs.values()
            if self._visible(resource, workspace_id)
        )

    async def get_pipeline_version(self, workspace_id: UUID, version_id: UUID) -> Resource:
        resource = self._pipelines.get(str(version_id))
        if resource is None or not self._visible(resource, workspace_id):
            raise NotFoundError("pipeline_version", str(version_id))
        result = deepcopy(resource)
        result.setdefault("state", "published" if result.get("published_at") else "draft")
        return result

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> Resource:
        resource = self._runs.get(str(run_id))
        if resource is None or not self._visible(resource, workspace_id):
            raise NotFoundError("run", str(run_id))
        return self._public_run(resource)

    async def list_generation_batches(self, workspace_id: UUID) -> list[Resource]:
        values = [
            self._public_generation_batch(batch)
            for batch in self._generation_batches.values()
            if batch["workspace_id"] == str(workspace_id)
        ]
        return sorted(values, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    async def get_generation_batch(self, workspace_id: UUID, batch_id: UUID) -> Resource:
        batch = self._generation_batches.get(str(batch_id))
        if batch is None or batch["workspace_id"] != str(workspace_id):
            raise NotFoundError("generation_batch", str(batch_id))
        return self._public_generation_batch(batch)

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
        await self.get_generation_batch(workspace_id, batch_id)
        needle = (search or "").strip().casefold()
        values: list[Resource] = []
        for item in self._generation_batch_items.values():
            if item["batch_id"] != str(batch_id):
                continue
            run = self._runs[item["run_id"]]
            if status and run["status"] != status:
                continue
            if needle and needle not in f"{item['label']} {item['run_id']}".casefold():
                continue
            values.append(self._public_generation_batch_item(item, run))
        values.sort(key=lambda item: item["ordinal"])
        return values[offset : offset + limit], len(values)

    async def create_generation_batch_idempotently(
        self,
        batch: Resource,
        runs: list[Resource],
        items: list[Resource],
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            self._generation_batches[batch["id"]] = deepcopy(batch)
            for run, item in zip(runs, items, strict=True):
                persisted = deepcopy(run)
                persisted.setdefault("cancellation_requested_at", None)
                self._runs[run["id"]] = persisted
                self._generation_batch_items[item["id"]] = deepcopy(item)
                self._append_event(
                    persisted,
                    event_type="run.created",
                    deduplication_key="api:run-created",
                    actor_type="user",
                    actor_id=persisted.get("created_by"),
                    payload={"status": persisted["status"], "batch_id": batch["id"]},
                )
            response = self._public_generation_batch(batch)
            self._record_idempotent(operation_key, request_fingerprint, response)
            return response, True

    async def create_run_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False

            persisted = deepcopy(resource)
            persisted.setdefault("cancellation_requested_at", None)
            self._runs[resource["id"]] = persisted
            self._append_event(
                persisted,
                event_type="run.created",
                deduplication_key="api:run-created",
                actor_type="user",
                actor_id=persisted.get("created_by"),
                payload={"status": persisted["status"]},
            )
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint=request_fingerprint,
                response=deepcopy(persisted),
            )
            return deepcopy(persisted), True

    async def create_full_ai_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        """Atomically bind the dedicated Full-AI record to one scheduler Run."""

        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False

            persisted_scheduler = deepcopy(scheduler_run)
            persisted_scheduler.setdefault("cancellation_requested_at", None)
            self._runs[scheduler_run["id"]] = persisted_scheduler
            self._append_event(
                persisted_scheduler,
                event_type="run.created",
                deduplication_key="api:full-ai-run-created",
                actor_type="user",
                actor_id=persisted_scheduler.get("created_by"),
                payload={
                    "status": persisted_scheduler["status"],
                    "full_ai_run_id": resource["id"],
                    "visual_source_mode": "generated_only",
                },
            )
            persisted = deepcopy(resource)
            self._full_ai_runs[resource["id"]] = persisted
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint=request_fingerprint,
                response=deepcopy(persisted),
            )
            return deepcopy(persisted), True

    async def find_full_ai_run_by_idempotency_key(
        self, workspace_id: UUID, idempotency_key: str
    ) -> Resource | None:
        for resource in self._full_ai_runs.values():
            if (
                resource["workspace_id"] == str(workspace_id)
                and resource["idempotency_key"] == idempotency_key
            ):
                result = deepcopy(resource)
                scheduler = self._runs.get(resource["underlying_run_id"])
                if scheduler is not None:
                    result["status"] = self._full_ai_status(resource, scheduler)
                    result["updated_at"] = max(result["updated_at"], scheduler["updated_at"])
                return result
        return None

    async def get_full_ai_run(self, workspace_id: UUID, full_ai_run_id: UUID) -> Resource:
        resource = self._full_ai_runs.get(str(full_ai_run_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("full_ai_run", str(full_ai_run_id))
        public = self._public_full_ai_run(resource)
        scheduler = self._runs.get(resource["underlying_run_id"])
        if scheduler is not None:
            public["status"] = self._full_ai_status(resource, scheduler)
            public["updated_at"] = max(public["updated_at"], scheduler["updated_at"])
        return public

    async def create_webpage_video_run_idempotently(
        self,
        resource: Resource,
        scheduler_run: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
        active_run_limit: int,
    ) -> tuple[Resource, bool]:
        """Atomically bind the webpage control-plane record to one scheduler Run."""

        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False

            active_count = sum(
                1
                for wrapper in self._webpage_video_runs.values()
                if wrapper["workspace_id"] == resource["workspace_id"]
                and self._runs.get(wrapper["underlying_run_id"], {}).get("status")
                in {"queued", "running", "awaiting_review"}
            )
            if active_count >= active_run_limit:
                raise ConflictError(
                    "WEBPAGE_VIDEO_ACTIVE_RUN_LIMIT_REACHED",
                    "The workspace has reached its concurrent webpage-video Run limit",
                    active_runs=active_count,
                    max_active_runs=active_run_limit,
                )

            persisted_scheduler = deepcopy(scheduler_run)
            persisted_scheduler.setdefault("cancellation_requested_at", None)
            self._runs[scheduler_run["id"]] = persisted_scheduler
            self._append_event(
                persisted_scheduler,
                event_type="run.created",
                deduplication_key="api:webpage-video-run-created",
                actor_type="user",
                actor_id=persisted_scheduler.get("created_by"),
                payload={
                    "status": persisted_scheduler["status"],
                    "webpage_video_run_id": resource["id"],
                    "capture_mode": "viewport",
                },
            )
            persisted = deepcopy(resource)
            self._webpage_video_runs[resource["id"]] = persisted
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint=request_fingerprint,
                response=deepcopy(persisted),
            )
            return deepcopy(persisted), True

    async def count_active_webpage_video_runs(self, workspace_id: UUID) -> int:
        return sum(
            1
            for wrapper in self._webpage_video_runs.values()
            if wrapper["workspace_id"] == str(workspace_id)
            and self._runs.get(wrapper["underlying_run_id"], {}).get("status")
            in {"queued", "running", "awaiting_review"}
        )

    async def get_webpage_video_run(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource:
        resource = self._webpage_video_runs.get(str(webpage_video_run_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("webpage_video_run", str(webpage_video_run_id))
        result = deepcopy(resource)
        scheduler = self._runs.get(resource["underlying_run_id"])
        if scheduler is not None:
            result["scheduler_status"] = scheduler["status"]
            result["scheduler_updated_at"] = scheduler["updated_at"]
            result["cancellation_requested_at"] = scheduler.get("cancellation_requested_at")
        return result

    async def append_webpage_capture_attempt(self, resource: Resource) -> tuple[Resource, bool]:
        async with self._lock:
            run = self._webpage_video_runs.get(resource["webpage_video_run_id"])
            if (
                run is None
                or run["workspace_id"] != resource["workspace_id"]
                or run["underlying_run_id"] != resource["underlying_run_id"]
            ):
                raise NotFoundError("webpage_video_run", resource["webpage_video_run_id"])
            for existing in self._webpage_capture_attempts.values():
                same_attempt = (
                    existing["workspace_id"] == resource["workspace_id"]
                    and existing["webpage_video_run_id"] == resource["webpage_video_run_id"]
                    and int(existing["attempt_number"]) == int(resource["attempt_number"])
                )
                same_artifact = resource.get("artifact_id") is not None and existing.get(
                    "artifact_id"
                ) == resource.get("artifact_id")
                if not same_attempt and not same_artifact:
                    continue
                if existing != resource:
                    raise ConflictError(
                        "WEBPAGE_CAPTURE_ATTEMPT_CONFLICT",
                        "A capture attempt was replayed with different immutable evidence",
                        attempt_number=resource["attempt_number"],
                    )
                return deepcopy(existing), False
            persisted = deepcopy(resource)
            self._webpage_capture_attempts[resource["id"]] = persisted
            return deepcopy(persisted), True

    async def list_webpage_capture_attempts(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> list[Resource]:
        await self.get_webpage_video_run(workspace_id, webpage_video_run_id)
        attempts = [
            deepcopy(resource)
            for resource in self._webpage_capture_attempts.values()
            if resource["workspace_id"] == str(workspace_id)
            and resource["webpage_video_run_id"] == str(webpage_video_run_id)
        ]
        return sorted(
            attempts,
            key=lambda item: (int(item["attempt_number"]), item["created_at"]),
        )

    async def get_webpage_pilot_feedback(
        self, workspace_id: UUID, webpage_video_run_id: UUID
    ) -> Resource | None:
        await self.get_webpage_video_run(workspace_id, webpage_video_run_id)
        resource = self._webpage_pilot_feedback.get(str(webpage_video_run_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            return None
        return deepcopy(resource)

    async def list_webpage_pilot_feedback(
        self, workspace_id: UUID, *, limit: int
    ) -> tuple[list[Resource], int]:
        resources = [
            deepcopy(resource)
            for resource in self._webpage_pilot_feedback.values()
            if resource["workspace_id"] == str(workspace_id)
        ]
        resources.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return resources[:limit], len(resources)

    async def upsert_webpage_pilot_feedback_idempotently(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            run = self._webpage_video_runs.get(resource["webpage_video_run_id"])
            if run is None or run["workspace_id"] != resource["workspace_id"]:
                raise NotFoundError("webpage_video_run", resource["webpage_video_run_id"])
            current = self._webpage_pilot_feedback.get(resource["webpage_video_run_id"])
            if current is None:
                if expected_revision != 0:
                    raise PreconditionFailedError(0, expected_revision)
                persisted = deepcopy(resource)
                created = True
            else:
                self._check_revision(current, expected_revision)
                persisted = {
                    **deepcopy(resource),
                    "id": current["id"],
                    "created_at": current["created_at"],
                    "revision": int(current["revision"]) + 1,
                }
                created = False
            self._webpage_pilot_feedback[resource["webpage_video_run_id"]] = persisted
            self._record_idempotent(operation_key, request_fingerprint, persisted)
            return deepcopy(persisted), created

    async def cancel_run_idempotently(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            run = self._runs.get(str(run_id))
            if run is None or not self._visible(run, workspace_id):
                raise NotFoundError("run", str(run_id))
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                response = self._public_run(run)
            else:
                now = self._now()
                run["cancellation_requested_at"] = run.get("cancellation_requested_at") or now
                run["updated_at"] = now
                found_steps = False
                for step in self._run_steps.values():
                    if (
                        step["workspace_id"] != str(workspace_id)
                        or step["run_id"] != str(run_id)
                        or step["status"] in {"succeeded", "failed", "cancelled"}
                    ):
                        continue
                    found_steps = True
                    step["cancellation_requested_at"] = step.get("cancellation_requested_at") or now
                    step["updated_at"] = now
                    step["revision"] = int(step.get("revision", 0)) + 1
                    if step["status"] != "running":
                        step["status"] = "cancelled"
                        step["finished_at"] = now
                        step["lease_owner"] = None
                        step["lease_expires_at"] = None
                        step["heartbeat_at"] = None
                if not found_steps:
                    run["status"] = "cancelled"
                    run["finished_at"] = now
                self._synchronize_memory_run(run, now)
                self._append_event(
                    run,
                    event_type="run.cancellation_requested",
                    deduplication_key="api:cancellation-requested",
                    actor_type="user",
                    payload={"status": run["status"]},
                )
                if run["status"] == "cancelled":
                    self._append_event(
                        run,
                        event_type="run.cancelled",
                        deduplication_key="run-status:cancelled",
                        actor_type="system",
                        payload={"status": "cancelled"},
                    )
                response = self._public_run(run)
            self._record_idempotent(operation_key, request_fingerprint, response)
            return response, True

    async def list_run_steps(self, workspace_id: UUID, run_id: UUID) -> list[Resource]:
        run = self._runs.get(str(run_id))
        if run is None or not self._visible(run, workspace_id):
            raise NotFoundError("run", str(run_id))
        return self._sorted_copy(
            step
            for step in self._run_steps.values()
            if step["workspace_id"] == str(workspace_id) and step["run_id"] == str(run_id)
        )

    async def get_run_step(self, workspace_id: UUID, step_id: str) -> Resource:
        step = self._run_steps.get(step_id)
        if step is None or step["workspace_id"] != str(workspace_id):
            raise NotFoundError("run_step", step_id)
        return deepcopy(step)

    async def retry_run_step_idempotently(
        self,
        workspace_id: UUID,
        step_id: str,
        *,
        actor_id: UUID,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            step = self._run_steps.get(step_id)
            if step is None or step["workspace_id"] != str(workspace_id):
                raise NotFoundError("run_step", step_id)
            run = self._runs[step["run_id"]]
            if run.get("cancellation_requested_at") is not None:
                raise ConflictError(
                    "RUN_CANCELLATION_REQUESTED",
                    "A step cannot be retried after Run cancellation was requested",
                    step_id=step_id,
                )
            attempt = int(step.get("attempt", 0))
            if step["status"] != "failed" or attempt >= 20:
                raise ConflictError(
                    "STEP_NOT_RETRYABLE",
                    "Only a failed step below the manual retry safety limit can be retried",
                    step_id=step_id,
                    status=step["status"],
                )
            now = self._now()
            step["maximum_attempts"] = max(int(step.get("maximum_attempts", 3)), attempt + 1)
            step["status"] = "retrying"
            step["next_attempt_at"] = now
            step["finished_at"] = None
            step["cancellation_requested_at"] = None
            step["updated_at"] = now
            step["revision"] = int(step.get("revision", 0)) + 1
            reopened_keys = {str(step["node_key"])}
            while True:
                reopened_in_wave = False
                for downstream in self._run_steps.values():
                    if (
                        downstream["workspace_id"] != str(workspace_id)
                        or downstream["run_id"] != step["run_id"]
                        or downstream["status"] != "cancelled"
                        or not any(
                            dependency in reopened_keys
                            for dependency in downstream.get("dependencies", [])
                        )
                    ):
                        continue
                    downstream["status"] = "queued"
                    downstream["next_attempt_at"] = now
                    downstream["finished_at"] = None
                    downstream["cancellation_requested_at"] = None
                    downstream["error"] = None
                    downstream["review_required"] = bool(
                        downstream.get(
                            "static_review_required",
                            downstream.get("review_required", False),
                        )
                    )
                    downstream["updated_at"] = now
                    downstream["revision"] = int(downstream.get("revision", 0)) + 1
                    reopened_keys.add(str(downstream["node_key"]))
                    reopened_in_wave = True
                if not reopened_in_wave:
                    break
            self._synchronize_memory_run(run, now)
            self._append_event(
                run,
                event_type="step.retrying",
                deduplication_key=f"api-retry:{operation_key}",
                actor_type="user",
                actor_id=str(actor_id),
                step_id=step_id,
                payload={
                    "attempt": attempt,
                    "maximum_attempts": int(step["maximum_attempts"]),
                },
            )
            response = deepcopy(step)
            self._record_idempotent(operation_key, request_fingerprint, response)
            return response, True

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
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            step = self._run_steps.get(step_id)
            if step is None or step["workspace_id"] != str(workspace_id):
                raise NotFoundError("run_step", step_id)
            run = self._runs.get(step["run_id"])
            if run is None:  # pragma: no cover - protected by persistence constraints
                raise NotFoundError("run", step["run_id"])
            if run.get("cancellation_requested_at") is not None or run.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                raise ConflictError(
                    "RUN_NOT_ACCEPTING_REVIEW",
                    "A step cannot be reviewed after its Run stopped accepting work",
                    step_id=step_id,
                    status=run.get("status"),
                )
            if not step.get("review_required") or step["status"] != "awaiting_review":
                raise ConflictError(
                    "STEP_NOT_AWAITING_REVIEW",
                    "Only a review-gated step awaiting review can be reviewed",
                    step_id=step_id,
                    status=step["status"],
                )
            current_revision = int(step.get("revision", 0))
            if expected_revision is not None and expected_revision != current_revision:
                raise ConflictError(
                    "STEP_STATE_CHANGED",
                    "The step changed after its review evidence was loaded",
                    step_id=step_id,
                    expected_revision=expected_revision,
                    current_revision=current_revision,
                )
            now = self._now()
            attempt = int(step.get("attempt", 0))
            maximum_attempts = int(step.get("maximum_attempts", 3))
            if decision == "approve":
                status = "succeeded"
                error = step.get("error")
            elif decision == "revise" and attempt < maximum_attempts:
                status = "retrying"
                error = {
                    "code": "review_changes_requested",
                    "message": comment or "changes requested",
                    "retryable": True,
                    "details": {},
                }
            else:
                status = "failed"
                error = {
                    "code": "review_rejected",
                    "message": comment or "review rejected",
                    "retryable": False,
                    "details": {},
                }
            current_review = step.get("review") or {}
            step["review"] = {
                "decision": "request_changes" if decision == "revise" else decision,
                "actor_id": str(actor_id),
                "comment": comment,
                "issue_codes": list(issue_codes),
                "metadata": deepcopy(review_metadata) if review_metadata else None,
                "evidence": [
                    {
                        "id": item.get("id"),
                        "content_hash": item.get("content_hash"),
                    }
                    for item in step.get("output_artifacts", [])
                    if isinstance(item, dict)
                ],
                "reviewed_revision": current_revision,
                "requested_at": current_review.get("requested_at"),
                "decided_at": now,
            }
            step["status"] = status
            step["error"] = error
            if status == "retrying":
                retry_policy = step.get("retry_policy") or {}
                base = float(retry_policy.get("base_delay_seconds", 1))
                maximum = float(retry_policy.get("max_delay_seconds", 300))
                multiplier = float(retry_policy.get("multiplier", 2))
                retry_at = datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(
                    seconds=min(maximum, base * multiplier ** max(0, attempt - 1))
                )
                step["next_attempt_at"] = retry_at.isoformat().replace("+00:00", "Z")
            step["finished_at"] = now if status in {"succeeded", "failed"} else None
            step["updated_at"] = now
            step["revision"] = int(step.get("revision", 0)) + 1
            self._synchronize_memory_run(run, now)
            self._append_event(
                run,
                event_type="review.recorded",
                deduplication_key=f"api-review:{operation_key}",
                actor_type="user",
                actor_id=str(actor_id),
                step_id=step_id,
                payload={
                    "decision": step["review"]["decision"],
                    "status": status,
                    "issue_codes": list(issue_codes),
                    "reviewed_revision": current_revision,
                    "metadata": deepcopy(review_metadata) if review_metadata else None,
                },
            )
            response = deepcopy(step)
            self._record_idempotent(operation_key, request_fingerprint, response)
            return response, True

    async def create_or_reuse_catalog_snapshot(
        self,
        workspace_id: UUID,
        library_ids: Sequence[UUID],
        *,
        created_by: UUID | None = None,
    ) -> Resource:
        normalized_library_ids = tuple(sorted({str(value) for value in library_ids}))
        if not normalized_library_ids:
            raise ConflictError(
                "CATALOG_SNAPSHOT_LIBRARIES_REQUIRED",
                "A catalog snapshot requires at least one asset library",
            )
        for library_id in normalized_library_ids:
            library = await self.get_asset_library(workspace_id, UUID(library_id))
            if library.get("status") != "active":
                raise NotFoundError("asset_library", library_id)

        items: list[Resource] = []
        for asset in self._assets.values():
            if (
                asset.get("workspace_id") != str(workspace_id)
                or asset.get("library_id") not in normalized_library_ids
                or asset.get("kind") not in {"image", "video"}
                or asset.get("status") != "ready"
                or asset.get("analysis_status") != "completed"
                or asset.get("copyright_status") not in {"owned", "licensed", "public_domain"}
            ):
                continue
            file = asset.get("file") or {}
            if (
                file.get("scan_status") != "clean"
                or not file.get("id")
                or not file.get("content_hash")
            ):
                continue
            analyses = asset.get("analyses") or (
                [asset["analysis"]] if asset.get("analysis") else []
            )
            completed_analyses = [
                value
                for value in analyses
                if value.get("id") and value.get("status", "completed") == "completed"
            ]
            if completed_analyses:
                analysis = max(
                    completed_analyses,
                    key=lambda value: (
                        int(value.get("analysis_version", 0)),
                        str(value["id"]),
                    ),
                )
                analysis_id = str(analysis["id"])
            else:
                analysis_id = str(asset.get("analysis_id") or "")
            if not analysis_id:
                continue
            items.append(
                {
                    "library_id": str(asset["library_id"]),
                    "asset_id": str(asset["id"]),
                    "asset_file_id": str(file["id"]),
                    "analysis_id": analysis_id,
                    "content_hash": str(file["content_hash"]),
                }
            )
        items.sort(
            key=lambda item: (
                item["library_id"],
                item["asset_id"],
                item["asset_file_id"],
                item["analysis_id"],
            )
        )
        content_hash = _catalog_snapshot_content_hash(normalized_library_ids, items)
        async with self._lock:
            existing_id = self._catalog_snapshots_by_hash.get((str(workspace_id), content_hash))
            if existing_id is not None:
                return deepcopy(self._catalog_snapshots[existing_id])
            snapshot_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"framefactory-catalog-snapshot:{workspace_id}:{content_hash}",
                )
            )
            snapshot = {
                "schema_version": "1.0.0",
                "id": snapshot_id,
                "workspace_id": str(workspace_id),
                "library_ids": list(normalized_library_ids),
                "content_hash": content_hash,
                "item_count": len(items),
                "items": deepcopy(items),
                "created_by": str(created_by) if created_by is not None else None,
                "created_at": self._now(),
            }
            self._catalog_snapshots[snapshot_id] = deepcopy(snapshot)
            self._catalog_snapshots_by_hash[(str(workspace_id), content_hash)] = snapshot_id
            return deepcopy(snapshot)

    async def get_catalog_snapshot(self, workspace_id: UUID, snapshot_id: UUID) -> Resource:
        snapshot = self._catalog_snapshots.get(str(snapshot_id))
        if snapshot is None or snapshot["workspace_id"] != str(workspace_id):
            raise NotFoundError("catalog_snapshot", str(snapshot_id))
        return deepcopy(snapshot)

    async def create_library_build_job_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            library = await self.get_asset_library(
                UUID(resource["workspace_id"]), UUID(resource["library_id"])
            )
            if library.get("status") != "active":
                raise NotFoundError("asset_library", resource["library_id"])
            if resource["id"] in self._library_build_jobs:
                raise ConflictError(
                    "LIBRARY_BUILD_JOB_EXISTS",
                    "A library build job with this id already exists",
                    job_id=resource["id"],
                )
            now = self._now()
            persisted = deepcopy(resource)
            persisted.setdefault("schema_version", "1.0.0")
            persisted["status"] = "queued"
            persisted["stage"] = "discover"
            persisted.setdefault("spec", {})
            persisted.setdefault(
                "progress",
                {
                    "discovered": 0,
                    "transferred": 0,
                    "analyzed": 0,
                    "indexed": 0,
                    "failed": 0,
                    "asset_ids": [],
                },
            )
            persisted["error"] = None
            persisted["revision"] = 1
            persisted.setdefault("created_at", now)
            persisted["started_at"] = None
            persisted["completed_at"] = None
            persisted["updated_at"] = persisted["created_at"]
            persisted["idempotency_key"] = operation_key
            persisted["request_hash"] = request_fingerprint
            self._library_build_jobs[persisted["id"]] = deepcopy(persisted)
            public = self._public_library_build_job(persisted)
            self._record_idempotent(operation_key, request_fingerprint, public)
            return public, True

    async def get_library_build_job(self, workspace_id: UUID, job_id: UUID) -> Resource:
        resource = self._library_build_jobs.get(str(job_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("library_build_job", str(job_id))
        return self._public_library_build_job(resource)

    async def cancel_library_build_job(
        self,
        workspace_id: UUID,
        job_id: UUID,
        *,
        expected_revision: int,
    ) -> Resource:
        async with self._lock:
            resource = self._library_build_jobs.get(str(job_id))
            if resource is None or resource["workspace_id"] != str(workspace_id):
                raise NotFoundError("library_build_job", str(job_id))
            self._check_revision(resource, expected_revision)
            if resource["status"] in {
                "completed",
                "completed_with_errors",
                "failed",
                "cancelled",
            }:
                return self._public_library_build_job(resource)
            now = self._now()
            resource["status"] = "cancelled"
            resource["completed_at"] = now
            resource["updated_at"] = now
            resource["revision"] = int(resource.get("revision", 1)) + 1
            return self._public_library_build_job(resource)

    async def list_asset_libraries(self, workspace_id: UUID) -> list[Resource]:
        libraries: list[Resource] = []
        for resource in self._asset_libraries.values():
            if resource["workspace_id"] != str(workspace_id):
                continue
            item = deepcopy(resource)
            assets = [
                asset
                for asset in self._assets.values()
                if asset["workspace_id"] == str(workspace_id)
                and asset["library_id"] == resource["id"]
                and asset.get("status") != "deleted"
            ]
            item["asset_count"] = len(assets)
            item["ready_asset_count"] = sum(asset.get("status") == "ready" for asset in assets)
            libraries.append(item)
        return self._sorted_copy(libraries)

    async def get_asset_library(self, workspace_id: UUID, library_id: UUID) -> Resource:
        resource = self._asset_libraries.get(str(library_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("asset_library", str(library_id))
        result = deepcopy(resource)
        assets = [
            asset
            for asset in self._assets.values()
            if asset["workspace_id"] == str(workspace_id)
            and asset["library_id"] == str(library_id)
            and asset.get("status") != "deleted"
        ]
        result["asset_count"] = len(assets)
        result["ready_asset_count"] = sum(asset.get("status") == "ready" for asset in assets)
        return result

    async def create_asset_library(self, resource: Resource) -> Resource:
        async with self._lock:
            if any(
                item["workspace_id"] == resource["workspace_id"]
                and item["slug"] == resource["slug"]
                for item in self._asset_libraries.values()
            ):
                raise ConflictError(
                    "ASSET_LIBRARY_SLUG_EXISTS",
                    "An asset library with this slug already exists",
                    slug=resource["slug"],
                )
            self._asset_libraries[resource["id"]] = deepcopy(resource)
            return deepcopy(resource)

    async def create_document_source_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False
            persisted = deepcopy(resource)
            self._document_sources[persisted["id"]] = persisted
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint=request_fingerprint,
                response=deepcopy(persisted),
            )
            return deepcopy(persisted), True

    async def get_document_source(self, workspace_id: UUID, source_id: UUID) -> Resource:
        resource = self._document_sources.get(str(source_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("document_source", str(source_id))
        return deepcopy(resource)

    async def complete_document_source(
        self, workspace_id: UUID, source_id: UUID, *, byte_size: int
    ) -> Resource:
        async with self._lock:
            resource = self._document_sources.get(str(source_id))
            if resource is None or resource["workspace_id"] != str(workspace_id):
                raise NotFoundError("document_source", str(source_id))
            if int(resource["byte_size"]) != byte_size:
                raise ConflictError(
                    "DOCUMENT_SOURCE_SIZE_MISMATCH",
                    "Uploaded PDF size does not match the initiated source",
                )
            if resource["status"] == "uploaded":
                return deepcopy(resource)
            resource["status"] = "uploaded"
            resource["revision"] = int(resource["revision"]) + 1
            resource["uploaded_at"] = self._now()
            resource["updated_at"] = self._now()
            return deepcopy(resource)

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
        del actor_id, reason
        async with self._lock:
            resource = self._document_sources.get(str(source_id))
            if resource is None or resource["workspace_id"] != str(workspace_id):
                raise NotFoundError("document_source", str(source_id))
            self._check_revision(resource, expected_revision)
            if resource["status"] in {"deletion_pending", "purging", "purged"}:
                raise ConflictError(
                    "DOCUMENT_RETENTION_IMMUTABLE",
                    "Retention cannot change after deletion has started",
                )
            resource["retention_until"] = (
                retention_until.astimezone(UTC).isoformat().replace("+00:00", "Z")
                if retention_until is not None
                else None
            )
            resource["revision"] = int(resource["revision"]) + 1
            resource["updated_at"] = self._now()
            return deepcopy(resource)

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
        async with self._lock:
            resource = self._document_sources.get(str(source_id))
            if resource is None or resource["workspace_id"] != str(workspace_id):
                raise NotFoundError("document_source", str(source_id))
            self._check_revision(resource, expected_revision)
            if resource["status"] in {"purging", "purged"}:
                raise ConflictError(
                    "DOCUMENT_PURGE_ALREADY_STARTED",
                    "Legal hold cannot change after object deletion has started",
                )
            now = self._now()
            resource["legal_hold"] = active
            resource["legal_hold_reason"] = reason if active else None
            resource["legal_hold_set_by"] = str(actor_id) if active else None
            resource["legal_hold_set_at"] = now if active else None
            if active and resource["status"] == "deletion_pending":
                resource["status"] = "uploaded"
                for request in self._document_purge_requests.values():
                    if (
                        request["workspace_id"] == str(workspace_id)
                        and request["source_id"] == str(source_id)
                        and request["status"] in {"queued", "retrying"}
                    ):
                        request["status"] = "blocked"
                        request["last_error"] = {
                            "code": "DOCUMENT_LEGAL_HOLD_ACTIVE",
                            "message": "Deletion was blocked by a legal hold",
                        }
                        request["updated_at"] = now
            resource["revision"] = int(resource["revision"]) + 1
            resource["updated_at"] = now
            return deepcopy(resource)

    async def request_document_purge_idempotently(
        self,
        resource: Resource,
        *,
        actor_id: UUID,
        expected_revision: int,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        del actor_id
        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False
            source = self._document_sources.get(resource["source_id"])
            if source is None or source["workspace_id"] != resource["workspace_id"]:
                raise NotFoundError("document_source", resource["source_id"])
            self._check_revision(source, expected_revision)
            if source.get("legal_hold"):
                raise ConflictError(
                    "DOCUMENT_LEGAL_HOLD_ACTIVE",
                    "The document is protected by an active legal hold",
                )
            retention_until = source.get("retention_until")
            if retention_until is not None:
                retained_until = datetime.fromisoformat(str(retention_until).replace("Z", "+00:00"))
                if retained_until > datetime.now(UTC):
                    raise ConflictError(
                        "DOCUMENT_RETENTION_ACTIVE",
                        "The document has not reached the end of its retention period",
                        retention_until=retention_until,
                    )
            if source["status"] not in {"uploaded", "rejected"}:
                raise ConflictError(
                    "DOCUMENT_NOT_PURGEABLE",
                    "Only completed, non-purging document sources can be deleted",
                    status=source["status"],
                )
            active = next(
                (
                    run
                    for run in self._runs.values()
                    if run["workspace_id"] == resource["workspace_id"]
                    and str((run.get("input") or {}).get("document_source_id"))
                    == resource["source_id"]
                    and run.get("status") not in {"succeeded", "failed", "cancelled"}
                ),
                None,
            )
            if active is not None:
                raise ConflictError(
                    "DOCUMENT_RUN_ACTIVE",
                    "A document Run must finish or be cancelled before deletion",
                    run_id=active["id"],
                    status=active["status"],
                )
            persisted = deepcopy(resource)
            upload_expires_at = source.get("upload_expires_at")
            if upload_expires_at is not None:
                persisted["next_attempt_at"] = (
                    max(
                        datetime.fromisoformat(
                            str(persisted["next_attempt_at"]).replace("Z", "+00:00")
                        ),
                        datetime.fromisoformat(str(upload_expires_at).replace("Z", "+00:00")),
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            self._document_purge_requests[persisted["id"]] = persisted
            source["status"] = "deletion_pending"
            source["deletion_requested_at"] = persisted["created_at"]
            source["revision"] = int(source["revision"]) + 1
            source["updated_at"] = persisted["created_at"]
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint=request_fingerprint,
                response=deepcopy(persisted),
            )
            return deepcopy(persisted), True

    async def get_document_purge_request(self, workspace_id: UUID, request_id: UUID) -> Resource:
        resource = self._document_purge_requests.get(str(request_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("document_purge_request", str(request_id))
        return deepcopy(resource)

    async def create_asset_upload(self, resource: Resource) -> Resource:
        async with self._lock:
            await self.get_asset_library(
                UUID(resource["workspace_id"]), UUID(resource["library_id"])
            )
            duplicate = next(
                (
                    item
                    for item in self._assets.values()
                    if item["workspace_id"] == resource["workspace_id"]
                    and item["file"]["content_hash"] == resource["file"]["content_hash"]
                ),
                None,
            )
            if duplicate is not None:
                if duplicate.get("status") == "processing":
                    replacement = deepcopy(resource)
                    replacement["id"] = duplicate["id"]
                    replacement["file"]["id"] = duplicate["file"]["id"]
                    self._assets[duplicate["id"]] = replacement
                    return deepcopy(replacement)
                raise ConflictError(
                    "ASSET_CONTENT_EXISTS",
                    "This media file already exists in the workspace",
                    asset_id=duplicate["id"],
                )
            self._assets[resource["id"]] = deepcopy(resource)
            return deepcopy(resource)

    async def get_asset(self, workspace_id: UUID, asset_id: UUID) -> Resource:
        resource = self._assets.get(str(asset_id))
        if resource is None or resource["workspace_id"] != str(workspace_id):
            raise NotFoundError("asset", str(asset_id))
        result = deepcopy(resource)
        result.setdefault("revision", 1)
        result.setdefault("analysis_status", "pending")
        result.setdefault("deleted_at", None)
        result["tags"] = normalized_tags(result)
        return result

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
        await self.get_asset_library(workspace_id, library_id)
        needle = search.casefold().strip() if search else None
        selected: list[Resource] = []
        for value in self._assets.values():
            if value["workspace_id"] != str(workspace_id) or value["library_id"] != str(library_id):
                continue
            asset = await self.get_asset(workspace_id, UUID(value["id"]))
            if not statuses and asset["status"] == "deleted":
                continue
            if statuses and asset["status"] not in statuses:
                continue
            if kinds and asset["kind"] not in kinds:
                continue
            if copyright_statuses and asset["copyright_status"] not in copyright_statuses:
                continue
            if analysis_statuses and asset["analysis_status"] not in analysis_statuses:
                continue
            asset_tags = {tag.casefold() for tag in normalized_tags(asset)}
            if tags and not all(tag.casefold() in asset_tags for tag in tags):
                continue
            if needle:
                searchable = " ".join(
                    [
                        str(asset.get("title", "")),
                        str(asset.get("description", "")),
                        " ".join(normalized_tags(asset)),
                        str((asset.get("analysis") or {}).get("summary", "")),
                    ]
                ).casefold()
                if needle not in searchable:
                    continue
            selected.append(asset)
        return sorted(
            selected,
            key=lambda item: (str(item.get("updated_at", "")), item["id"]),
            reverse=True,
        )

    async def update_asset_metadata(
        self,
        resource: Resource,
        *,
        expected_revision: int,
        actor_id: UUID,
    ) -> Resource:
        async with self._lock:
            current = self._assets.get(resource["id"])
            if current is None or current["workspace_id"] != resource["workspace_id"]:
                raise NotFoundError("asset", resource["id"])
            self._check_revision(current, expected_revision)
            before = deepcopy(current)
            self._assets[resource["id"]] = deepcopy(resource)
            self._append_asset_audit(
                resource,
                actor_id=actor_id,
                action="asset.metadata_updated",
                before=before,
                after=resource,
            )
            return await self.get_asset(UUID(resource["workspace_id"]), UUID(resource["id"]))

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
        async with self._lock:
            current = self._assets.get(str(asset_id))
            if current is None or current["workspace_id"] != str(workspace_id):
                raise NotFoundError("asset", str(asset_id))
            self._check_revision(current, expected_revision)
            before = deepcopy(current)
            validate_asset_transition(current, target_status)
            if target_status == current["status"]:
                return await self.get_asset(workspace_id, asset_id)
            now = self._now()
            if target_status == "deleted":
                current.setdefault("metadata", {})["status_before_delete"] = current["status"]
                current["deleted_at"] = now
            elif current["status"] == "deleted":
                current["deleted_at"] = None
            current["status"] = target_status
            current["revision"] = int(current.get("revision", 1)) + 1
            current["updated_at"] = now
            self._append_asset_audit(
                current,
                actor_id=actor_id,
                action=("asset.deleted" if target_status == "deleted" else "asset.status_changed"),
                before=before,
                after=current,
                metadata={"reason": reason},
            )
            return await self.get_asset(workspace_id, asset_id)

    async def list_asset_related(
        self, workspace_id: UUID, asset_id: UUID, relation: str
    ) -> list[Resource]:
        asset = await self.get_asset(workspace_id, asset_id)
        if relation == "sources":
            source = (asset.get("metadata") or {}).get("source")
            return [] if source is None else [{"id": f"source:{asset_id}", **deepcopy(source)}]
        if relation == "rights_evidence":
            evidence = (asset.get("metadata") or {}).get("rights_evidence")
            return [] if evidence is None else [{"id": f"rights:{asset_id}", **deepcopy(evidence)}]
        if relation == "analyses":
            values = asset.get("analyses") or ([asset["analysis"]] if asset.get("analysis") else [])
            return deepcopy(values)
        if relation == "segments":
            return deepcopy(asset.get("segments", []))
        if relation == "usage_records":
            return deepcopy(asset.get("usage_records", []))
        if relation == "analysis_jobs":
            values = [
                deepcopy(job)
                for job in self._asset_analysis_jobs.values()
                if job["workspace_id"] == str(workspace_id) and job["asset_id"] == str(asset_id)
            ]
            return self._sorted_copy(values)
        if relation == "audit_events":
            values = [
                deepcopy(event)
                for event in self._asset_audit_events.values()
                if event["workspace_id"] == str(workspace_id) and event["asset_id"] == str(asset_id)
            ]
            return self._sorted_copy(values)
        raise ValueError(f"unsupported asset relation: {relation}")

    async def list_asset_import_jobs(self, workspace_id: UUID, library_id: UUID) -> list[Resource]:
        await self.get_asset_library(workspace_id, library_id)
        return []

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
        async with self._lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False
            current = self._assets.get(str(asset_id))
            if current is None or current["workspace_id"] != str(workspace_id):
                raise NotFoundError("asset", str(asset_id))
            self._check_revision(current, expected_revision)
            if current["status"] == "deleted":
                raise ConflictError(
                    "ASSET_DELETED", "Deleted assets cannot be reanalyzed", asset_id=str(asset_id)
                )
            now = self._now()
            job_id = str(uuid5(NAMESPACE_URL, operation_key))
            job: Resource = {
                "schema_version": "1.0.0",
                "id": job_id,
                "workspace_id": str(workspace_id),
                "asset_id": str(asset_id),
                "kind": "reanalysis",
                "status": "queued",
                "reason": reason,
                "requested_by": str(actor_id),
                "created_at": now,
                "updated_at": now,
            }
            self._asset_analysis_jobs[job_id] = deepcopy(job)
            before = deepcopy(current)
            current["analysis_status"] = "pending"
            current["status"] = "processing"
            current["revision"] = int(current.get("revision", 1)) + 1
            current["updated_at"] = now
            self._append_asset_audit(
                current,
                actor_id=actor_id,
                action="asset.reanalysis_requested",
                before=before,
                after=current,
                metadata={"job_id": job_id, "reason": reason},
            )
            self._idempotency[operation_key] = _IdempotentRecord(request_fingerprint, deepcopy(job))
            return job, True

    async def run_asset_batch_idempotently(
        self,
        workspace_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
        request_path: str,
        action: Callable[[], Awaitable[Resource]],
    ) -> tuple[Resource, bool]:
        del workspace_id, request_path
        async with self._asset_batch_lock:
            previous = self._idempotency.get(operation_key)
            if previous is not None:
                if previous.request_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(previous.response), False
            response = await action()
            self._idempotency[operation_key] = _IdempotentRecord(
                request_fingerprint, deepcopy(response)
            )
            return response, True

    async def complete_asset_upload(
        self, workspace_id: UUID, asset_id: UUID, *, byte_size: int
    ) -> Resource:
        async with self._lock:
            resource = self._assets.get(str(asset_id))
            if resource is None or resource["workspace_id"] != str(workspace_id):
                raise NotFoundError("asset", str(asset_id))
            if resource["file"]["byte_size"] != byte_size:
                raise ConflictError(
                    "ASSET_SIZE_MISMATCH",
                    "Uploaded media size does not match the declared size",
                )
            metadata = resource.setdefault("metadata", {})
            if metadata.get("upload_completed_at"):
                return deepcopy(resource)
            resource["status"] = "processing"
            resource["analysis_status"] = "pending"
            metadata["upload_completed_at"] = self._now()
            resource["revision"] = int(resource.get("revision", 1)) + 1
            resource["updated_at"] = self._now()
            return deepcopy(resource)

    def _append_asset_audit(
        self,
        asset: Resource,
        *,
        actor_id: UUID,
        action: str,
        before: Resource,
        after: Resource,
        metadata: Resource | None = None,
    ) -> None:
        event_id = str(uuid5(NAMESPACE_URL, f"{action}:{asset['id']}:{asset.get('revision', 1)}"))
        self._asset_audit_events[event_id] = {
            "id": event_id,
            "workspace_id": asset["workspace_id"],
            "asset_id": asset["id"],
            "actor_type": "user",
            "actor_id": str(actor_id),
            "action": action,
            "before": deepcopy(before),
            "after": deepcopy(after),
            "metadata": deepcopy(metadata or {}),
            "created_at": self._now(),
        }

    async def list_artifacts(self, workspace_id: UUID, run_id: UUID) -> list[Resource]:
        await self.get_run(workspace_id, run_id)
        return self._sorted_copy(
            value
            for value in self._artifacts.values()
            if value["workspace_id"] == str(workspace_id) and value["run_id"] == str(run_id)
        )

    async def get_artifact(self, workspace_id: UUID, artifact_id: UUID) -> Resource:
        value = self._artifacts.get(str(artifact_id))
        if value is None or value["workspace_id"] != str(workspace_id):
            raise NotFoundError("artifact", str(artifact_id))
        return deepcopy(value)

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 101,
    ) -> list[Resource]:
        await self.get_run(workspace_id, run_id)
        return [
            deepcopy(value)
            for value in sorted(self._events.values(), key=lambda item: item["sequence"])
            if value["workspace_id"] == str(workspace_id)
            and value["run_id"] == str(run_id)
            and int(value["sequence"]) > after_sequence
        ][:limit]

    async def get_event(self, workspace_id: UUID, event_id: UUID) -> Resource:
        value = self._events.get(str(event_id))
        if value is None or value["workspace_id"] != str(workspace_id):
            raise NotFoundError("event", str(event_id))
        return deepcopy(value)

    def _append_event(
        self,
        run: Resource,
        *,
        event_type: str,
        deduplication_key: str,
        actor_type: str,
        payload: dict[str, Any],
        actor_id: str | None = None,
        step_id: str | None = None,
    ) -> Resource:
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                f"framefactory-event:{run['workspace_id']}:{run['id']}:{deduplication_key}",
            )
        )
        existing = self._events.get(event_id)
        if existing is not None:
            return existing
        sequence = 1 + max(
            (
                int(value["sequence"])
                for value in self._events.values()
                if value["run_id"] == run["id"]
            ),
            default=0,
        )
        event = {
            "schema_version": "1.0.0",
            "id": event_id,
            "workspace_id": run["workspace_id"],
            "ownership_type": "workspace",
            "run_id": run["id"],
            "step_id": (
                str(uuid5(NAMESPACE_URL, f"framefactory-worker-step:{step_id}"))
                if step_id
                else None
            ),
            "sequence": sequence,
            "type": event_type,
            "occurred_at": self._now(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "correlation_id": run["id"],
            "causation_id": None,
            "payload": deepcopy(payload),
        }
        self._events[event_id] = event
        return event

    def _synchronize_memory_run(self, run: Resource, now: str) -> None:
        steps = [
            step
            for step in self._run_steps.values()
            if step["workspace_id"] == run["workspace_id"] and step["run_id"] == run["id"]
        ]
        if not steps:
            return
        states = [step["status"] for step in steps]
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
        else:
            status = "queued"
        run["status"] = status
        run["finished_at"] = now if status in {"succeeded", "failed", "cancelled"} else None
        run["updated_at"] = now

    @staticmethod
    def _public_run(resource: Resource) -> Resource:
        result = deepcopy(resource)
        result.setdefault("cancellation_requested_at", None)
        return result

    @staticmethod
    def _public_full_ai_run(resource: Resource) -> Resource:
        result = deepcopy(resource)
        result["project_run_id"] = result["underlying_run_id"]
        for internal in (
            "underlying_run_id",
            "idempotency_key",
            "request_hash",
            "estimate_fingerprint",
            "plan",
            "created_by",
        ):
            result.pop(internal, None)
        return result

    @staticmethod
    def _full_ai_status(resource: Resource, scheduler_run: Resource) -> str:
        if resource["billing"]["requires_reconciliation"]:
            return "reconciliation_required"
        scheduler_status = scheduler_run["status"]
        if scheduler_status in {"succeeded", "failed", "cancelled"}:
            return scheduler_status
        if scheduler_status == "awaiting_review":
            return "quality_check"
        if resource["status"] in {
            "planning",
            "generating",
            "assembling",
            "quality_check",
        }:
            return resource["status"]
        return "generating" if scheduler_status in {"running", "retrying"} else "queued"

    def _public_generation_batch(self, batch: Resource) -> Resource:
        counts = {
            state: 0
            for state in (
                "queued",
                "running",
                "awaiting_review",
                "succeeded",
                "failed",
                "cancelled",
            )
        }
        updated_at = batch["updated_at"]
        for item in self._generation_batch_items.values():
            if item["batch_id"] != batch["id"]:
                continue
            run = self._runs[item["run_id"]]
            counts[run["status"]] = counts.get(run["status"], 0) + 1
            updated_at = max(updated_at, run["updated_at"])
        total = sum(counts.values())
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
            **deepcopy(batch),
            "status": status,
            "total_count": total,
            "status_counts": counts,
            "updated_at": updated_at,
        }

    @staticmethod
    def _public_library_build_job(resource: Resource) -> Resource:
        public = deepcopy(resource)
        public.pop("idempotency_key", None)
        public.pop("request_hash", None)
        return public

    @staticmethod
    def _public_generation_batch_item(item: Resource, run: Resource) -> Resource:
        return {
            "schema_version": "1.0.0",
            "id": item["id"],
            "workspace_id": item["workspace_id"],
            "batch_id": item["batch_id"],
            "run_id": item["run_id"],
            "ordinal": item["ordinal"],
            "label": item["label"],
            "input": deepcopy(item["input"]),
            "status": run["status"],
            "cancellation_requested_at": run.get("cancellation_requested_at"),
            "created_at": item["created_at"],
            "updated_at": run["updated_at"],
        }

    async def list_skill_test_executions(self, workspace_id: UUID) -> list[Resource]:
        return self._sorted_copy(
            resource
            for resource in self._test_executions.values()
            if self._visible(resource, workspace_id)
        )

    async def get_skill_test_execution(self, workspace_id: UUID, execution_id: UUID) -> Resource:
        resource = self._test_executions.get(str(execution_id))
        if resource is None or not self._visible(resource, workspace_id):
            raise NotFoundError("skill_test_execution", str(execution_id))
        return deepcopy(resource)

    async def create_skill_test_execution_idempotently(
        self,
        resource: Resource,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]:
        async with self._lock:
            replay = self._idempotent_replay(operation_key, request_fingerprint)
            if replay is not None:
                return replay, False
            self._test_executions[resource["id"]] = deepcopy(resource)
            self._record_idempotent(operation_key, request_fingerprint, resource)
            return deepcopy(resource), True

    def _ensure_skill_can_be_created(self, resource: Resource) -> None:
        resource_id = resource["id"]
        if resource_id in self._skills:
            raise ConflictError("SKILL_ALREADY_EXISTS", "A skill with this id already exists")
        if any(
            item["workspace_id"] == resource["workspace_id"] and item["slug"] == resource["slug"]
            for item in self._skills.values()
        ):
            raise ConflictError(
                "SKILL_SLUG_ALREADY_EXISTS",
                "A skill with this slug already exists in the workspace",
                slug=resource["slug"],
            )

    def _ensure_version_can_be_created(self, resource: Resource) -> None:
        resource_id = resource["id"]
        if resource_id in self._versions:
            raise ConflictError(
                "SKILL_VERSION_ALREADY_EXISTS", "A skill version with this id already exists"
            )
        if any(
            item["skill_id"] == resource["skill_id"] and item["version"] == resource["version"]
            for item in self._versions.values()
        ):
            raise ConflictError(
                "SKILL_VERSION_NUMBER_EXISTS",
                "This semantic version already exists for the skill",
                version=resource["version"],
            )

    def _idempotent_replay(self, operation_key: str, request_fingerprint: str) -> Any | None:
        previous = self._idempotency.get(operation_key)
        if previous is None:
            return None
        if previous.request_fingerprint != request_fingerprint:
            raise ConflictError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used with a different request body",
            )
        return deepcopy(previous.response)

    def _record_idempotent(
        self, operation_key: str, request_fingerprint: str, response: Any
    ) -> None:
        self._idempotency[operation_key] = _IdempotentRecord(
            request_fingerprint=request_fingerprint,
            response=deepcopy(response),
        )

    @staticmethod
    def _check_revision(resource: Resource, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current_revision = int(resource.get("revision", 1))
        if current_revision != expected_revision:
            raise PreconditionFailedError(current_revision, expected_revision)

    @staticmethod
    def _sorted_copy(resources: Any) -> list[Resource]:
        return [
            deepcopy(resource)
            for resource in sorted(resources, key=lambda item: (item["created_at"], item["id"]))
        ]

    @staticmethod
    def _public_api_key(resource: Resource) -> Resource:
        public = deepcopy(resource)
        public.pop("key_hash", None)
        public.pop("created_by", None)
        return public

    @staticmethod
    def _public_session(resource: Resource) -> Resource:
        public = deepcopy(resource)
        public.pop("token_hash", None)
        public.pop("ip_hash", None)
        return public

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
