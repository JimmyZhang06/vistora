from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

import rfc8785

from .context import WorkspaceContext
from .contracts import ContractValidator
from .errors import ApiError, ConflictError, ValidationError
from .models import (
    AssetBatchReanalysis,
    AssetBatchReview,
    AssetBatchTags,
    AssetLibraryCreate,
    AssetMetadataPatch,
    AssetReanalysisRequest,
    AssetStatusTransition,
    AssetUploadComplete,
    AssetUploadCreate,
    BatchItemCreate,
    ChannelDefaultComposition,
    ChannelWrite,
    ForkSkillRequest,
    GenerationBatchCreate,
    GenerationBatchRetryFailed,
    RunCompositionInput,
    RunCreate,
    SkillIdentityInput,
    SkillTestExecutionCreate,
    SkillVersionCreate,
    SkillVersionPatch,
    StepReviewRequest,
    ValidationReport,
    VideoSettingsInput,
)
from .repository import ControlRepository, Resource
from .storage import PresignedRequest, StoredObject

CONTRACT_VERSION = "1.0.0"
HASH_DOMAIN_FIELDS = (
    "input_schema",
    "research_policy",
    "writing_policy",
    "visual_policy",
    "asset_policy",
    "qc_policy",
    "capability_requirements",
    "output_contract",
    "default_pipeline_version_id",
)
T = TypeVar("T")

_ASPECT_RESOLUTIONS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _channel_slug(name: str, channel_id: UUID) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:80].rstrip("-")
    return slug or f"channel-{channel_id.hex[:12]}"


def _channel_platform(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    return normalized or None


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _content_hash(value: dict[str, Any]) -> str:
    domain = {field: value[field] for field in HASH_DOMAIN_FIELDS}
    return hashlib.sha256(rfc8785.dumps(domain)).hexdigest()


def _reference_fingerprint(kind: str, resource_id: UUID) -> str:
    """Fingerprint an unresolved immutable resource reference in the stage-one control plane."""

    value = {"kind": kind, "id": str(resource_id), "resolver": "reference.v1"}
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


class ControlService:
    def __init__(
        self,
        repository: ControlRepository,
        validator: ContractValidator,
        *,
        worker_capabilities: tuple[str, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.validator = validator
        self.worker_capabilities = (
            frozenset(worker_capabilities) if worker_capabilities is not None else None
        )
        # The in-process action log covers deterministic transitions. A durable adapter replaces
        # this together with the in-memory repository without changing the HTTP contract.
        self._action_idempotency: dict[str, tuple[str, Any]] = {}
        self._action_lock = asyncio.Lock()

    def _pipeline_capability_gaps(self, pipeline: Resource) -> list[Resource]:
        if self.worker_capabilities is None:
            return []
        gaps: list[Resource] = []
        for node in pipeline.get("nodes", []):
            operation = str(node.get("operation", "")).strip()
            if operation and operation not in self.worker_capabilities:
                gaps.append(
                    {
                        "capability": operation,
                        "resource": "pipeline",
                        "message": f"当前执行器尚未配置 {operation} 的生产 Provider。",
                    }
                )
        return gaps

    async def estimate_run(
        self, context: WorkspaceContext, command: RunCreate
    ) -> Resource:
        composition = await self._resolve_run_composition(context, command)
        version = await self.repository.get_skill_version(
            context.workspace_id, composition.skill_version_id
        )
        pipeline = await self.repository.get_pipeline_version(
            context.workspace_id, composition.pipeline_version_id
        )
        production_settings = await self._resolve_production_settings(
            context, command.video_settings
        )
        gaps = self._pipeline_capability_gaps(pipeline)
        if version["state"] != "published":
            gaps.append(
                {
                    "capability": "skill.version",
                    "resource": "skill",
                    "message": "所选 Skill 版本尚未发布。",
                }
            )
        if pipeline["state"] != "published" or pipeline["status"] != "active":
            gaps.append(
                {
                    "capability": "pipeline.version",
                    "resource": "pipeline",
                    "message": "所选 Pipeline 版本当前不可执行。",
                }
            )
        return {
            "cost": {"amount": 0, "currency": "CNY"},
            "duration_seconds": int(production_settings["target_duration_seconds"]),
            "capability_gaps": gaps,
            "capabilities_known": self.worker_capabilities is not None,
        }

    async def _resolve_production_settings(
        self,
        context: WorkspaceContext,
        override: VideoSettingsInput | None,
    ) -> Resource:
        preferences = await self.repository.get_creation_preferences(
            context.workspace_id, context.user_id
        )
        requested = override or VideoSettingsInput()
        subtitles = requested.subtitles
        acquisition = requested.asset_acquisition
        aspect_ratio = requested.aspect_ratio or preferences["default_aspect_ratio"]
        width, height = _ASPECT_RESOLUTIONS[aspect_ratio]

        def source(value: Any) -> str:
            return "run_override" if value is not None else "account_default"

        return {
            "language": requested.language or preferences["default_language"],
            "aspect_ratio": aspect_ratio,
            "target_duration_seconds": (
                requested.target_duration_seconds
                or preferences["default_duration_seconds"]
            ),
            "visibility": requested.visibility or preferences["default_visibility"],
            "auto_quality_check": (
                requested.auto_quality_check
                if requested.auto_quality_check is not None
                else preferences["auto_quality_check"]
            ),
            "resolution": {"width": width, "height": height},
            "frame_rate": requested.frame_rate or 30,
            "layout": requested.layout or "full_frame",
            "media_fit": requested.media_fit or "cover",
            "subtitles": {
                "enabled": (
                    subtitles.enabled
                    if subtitles is not None and subtitles.enabled is not None
                    else True
                ),
                "position": (
                    subtitles.position
                    if subtitles is not None and subtitles.position is not None
                    else "bottom"
                ),
                "size": (
                    subtitles.size
                    if subtitles is not None and subtitles.size is not None
                    else "medium"
                ),
                "max_lines": (
                    subtitles.max_lines
                    if subtitles is not None and subtitles.max_lines is not None
                    else 2
                ),
            },
            "asset_acquisition": {
                "enabled": acquisition.enabled if acquisition is not None else False,
                "sources": (
                    list(acquisition.sources)
                    if acquisition is not None
                    else ["wikimedia", "youtube", "bilibili"]
                ),
                "max_assets": acquisition.max_assets if acquisition is not None else 3,
                "copyright_status": (
                    acquisition.copyright_status if acquisition is not None else "licensed"
                ),
                "rights_confirmed": (
                    acquisition.rights_confirmed if acquisition is not None else False
                ),
            },
            "sources": {
                "language": source(requested.language),
                "aspect_ratio": source(requested.aspect_ratio),
                "target_duration_seconds": source(requested.target_duration_seconds),
                "visibility": source(requested.visibility),
                "auto_quality_check": source(requested.auto_quality_check),
                "layout": source(requested.layout),
                "media_fit": source(requested.media_fit),
                "frame_rate": source(requested.frame_rate),
                "subtitles": "run_override" if subtitles is not None else "system_default",
                "asset_acquisition": (
                    "run_override" if acquisition is not None else "system_default"
                ),
            },
        }

    async def _idempotent_action(
        self,
        operation_key: str,
        request_fingerprint: str,
        action: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        async with self._action_lock:
            previous = self._action_idempotency.get(operation_key)
            if previous is not None:
                previous_fingerprint, response = previous
                if previous_fingerprint != request_fingerprint:
                    raise ConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                return deepcopy(response), False
            response = await action()
            self._action_idempotency[operation_key] = (
                request_fingerprint,
                deepcopy(response),
            )
            return response, True

    async def list_channels(
        self,
        context: WorkspaceContext,
        *,
        search: str | None = None,
        platform: str | None = None,
        status: str | None = None,
    ) -> list[Resource]:
        normalized_search = search.strip() if search is not None else None
        return await self.repository.list_channels(
            context.workspace_id,
            search=normalized_search or None,
            platform=_channel_platform(platform),
            status=status,
        )

    async def get_channel(
        self, context: WorkspaceContext, channel_id: UUID
    ) -> Resource:
        return await self.repository.get_channel(context.workspace_id, channel_id)

    async def create_channel(
        self, context: WorkspaceContext, command: ChannelWrite, idempotency_key: str
    ) -> tuple[Resource, bool]:
        await self._validate_channel_composition(context, command.default_composition)
        await self._validate_channel_connection(context, command)
        channel_id = uuid4()
        timestamp = _now()
        resource = self._channel_resource(
            context,
            command,
            channel_id=channel_id,
            slug=command.slug or _channel_slug(command.name, channel_id),
            revision=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.validator.validate("channel", resource)
        return await self.repository.create_channel_idempotently(
            resource,
            operation_key=f"{context.workspace_id}:create_channel:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )

    async def replace_channel(
        self,
        context: WorkspaceContext,
        channel_id: UUID,
        command: ChannelWrite,
        expected_revision: int,
    ) -> Resource:
        current = await self.repository.get_channel(context.workspace_id, channel_id)
        await self._validate_channel_composition(context, command.default_composition)
        await self._validate_channel_connection(context, command)
        resource = self._channel_resource(
            context,
            command,
            channel_id=channel_id,
            slug=command.slug or current["slug"],
            revision=expected_revision + 1,
            created_at=current["created_at"],
            updated_at=_now(),
        )
        self.validator.validate("channel", resource)
        return await self.repository.replace_channel(
            resource, expected_revision=expected_revision
        )

    async def archive_channel(
        self, context: WorkspaceContext, channel_id: UUID, expected_revision: int
    ) -> Resource:
        return await self.repository.archive_channel(
            context.workspace_id, channel_id, expected_revision=expected_revision
        )

    async def _validate_channel_composition(
        self, context: WorkspaceContext, composition: ChannelDefaultComposition
    ) -> None:
        version = await self.repository.get_skill_version(
            context.workspace_id, composition.skill_version_id
        )
        if version["state"] != "published":
            raise ConflictError(
                "SKILL_VERSION_NOT_PUBLISHED",
                "Channel defaults require a published Skill version",
            )
        pipeline = await self.repository.get_pipeline_version(
            context.workspace_id, composition.pipeline_version_id
        )
        if pipeline["state"] != "published" or pipeline["status"] != "active":
            raise ConflictError(
                "PIPELINE_VERSION_NOT_PUBLISHED",
                "Channel defaults require an active published Pipeline version",
            )
        for library_id in composition.asset_library_ids:
            await self.repository.get_asset_library(context.workspace_id, library_id)

    async def _validate_channel_connection(
        self, context: WorkspaceContext, command: ChannelWrite
    ) -> None:
        if command.platform_connection_id is None:
            return
        connection = await self.repository.get_platform_connection(
            context.workspace_id, command.platform_connection_id
        )
        assert command.platform is not None
        connection_platform = _channel_platform(str(connection["platform"]))
        channel_platform = _channel_platform(command.platform)
        if connection_platform != channel_platform:
            raise ConflictError(
                "PLATFORM_CONNECTION_MISMATCH",
                "The platform Connection does not belong to the Channel platform",
                connection_platform=connection_platform,
                channel_platform=channel_platform,
            )

    @staticmethod
    def _channel_resource(
        context: WorkspaceContext,
        command: ChannelWrite,
        *,
        channel_id: UUID,
        slug: str,
        revision: int,
        created_at: str,
        updated_at: str,
    ) -> Resource:
        return {
            "schema_version": CONTRACT_VERSION,
            "id": str(channel_id),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "name": command.name.strip(),
            "slug": slug,
            "description": command.description.strip(),
            "platform": _channel_platform(command.platform),
            "handle": command.handle.strip() if command.handle is not None else None,
            "brand_profile": deepcopy(command.brand_profile),
            "platform_connection_id": (
                str(command.platform_connection_id)
                if command.platform_connection_id is not None
                else None
            ),
            "status": command.status,
            "revision": revision,
            "default_composition": command.default_composition.model_dump(mode="json"),
            "created_by": str(context.user_id),
            "created_at": created_at,
            "updated_at": updated_at,
        }

    async def _resolve_run_composition(
        self, context: WorkspaceContext, command: RunCreate
    ) -> RunCompositionInput:
        if command.channel_id is not None:
            channel = await self.repository.get_channel(
                context.workspace_id, command.channel_id
            )
            if channel["status"] != "active":
                raise ConflictError(
                    "CHANNEL_NOT_ACTIVE",
                    "Runs can only be created for an active Channel",
                    channel_id=str(command.channel_id),
                    channel_status=channel["status"],
                )
            if command.composition is None:
                return RunCompositionInput(
                    **channel["default_composition"], capabilities=[]
                )
        assert command.composition is not None
        return command.composition

    async def list_asset_libraries(self, context: WorkspaceContext) -> list[Resource]:
        return await self.repository.list_asset_libraries(context.workspace_id)

    async def get_asset_library(
        self, context: WorkspaceContext, library_id: UUID
    ) -> Resource:
        return await self.repository.get_asset_library(context.workspace_id, library_id)

    async def get_asset(self, context: WorkspaceContext, asset_id: UUID) -> Resource:
        return await self.repository.get_asset(context.workspace_id, asset_id)

    async def create_asset_library(
        self,
        context: WorkspaceContext,
        command: AssetLibraryCreate,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        timestamp = _now()
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "name": command.name.strip(),
            "slug": command.slug,
            "description": command.description.strip(),
            "visibility": "private",
            "status": "active",
            "asset_count": 0,
            "ready_asset_count": 0,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        return await self._idempotent_action(
            f"asset-library:{context.workspace_id}:{idempotency_key}",
            _fingerprint(command.model_dump(mode="json")),
            lambda: self.repository.create_asset_library(resource),
        )

    async def create_asset_upload(
        self,
        context: WorkspaceContext,
        command: AssetUploadCreate,
        upload: PresignedRequest,
        *,
        bucket: str,
        idempotency_key: str,
        idempotency_payload: Any | None = None,
    ) -> tuple[Resource, bool]:
        await self.repository.get_asset_library(context.workspace_id, command.library_id)
        timestamp = _now()
        asset_id = uuid4()
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(asset_id),
            "workspace_id": str(context.workspace_id),
            "library_id": str(command.library_id),
            "kind": command.kind,
            "title": command.title.strip(),
            "description": command.description.strip(),
            "metadata": {
                "tags": list(dict.fromkeys(value.strip() for value in command.tags)),
                "upload_schema": "presigned.v1",
                **({"source": command.source} if command.source is not None else {}),
            },
            "copyright_status": command.copyright_status,
            "status": "processing",
            "analysis_status": "pending",
            "revision": 1,
            "deleted_at": None,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
            "file": {
                "id": str(uuid4()),
                "storage_provider": "s3",
                "bucket": bucket,
                "object_key": upload.object.key,
                "original_filename": command.filename,
                "media_type": command.content_type,
                "byte_size": command.byte_size,
                "content_hash": command.sha256,
                "scan_status": "pending",
                "width": None,
                "height": None,
                "duration_ms": None,
            },
            "upload": {
                "method": upload.method,
                "url": upload.url,
                "headers": dict(upload.headers),
                "expires_at": upload.expires_at.isoformat().replace("+00:00", "Z"),
            },
        }

        async def persist_upload() -> Resource:
            saved = await self.repository.create_asset_upload(resource)
            saved["upload"] = deepcopy(resource["upload"])
            return saved

        return await self._idempotent_action(
            f"asset-upload:{context.workspace_id}:{idempotency_key}",
            _fingerprint(
                command.model_dump(mode="json")
                if idempotency_payload is None
                else idempotency_payload
            ),
            persist_upload,
        )

    async def complete_asset_upload(
        self,
        context: WorkspaceContext,
        asset_id: UUID,
        command: AssetUploadComplete,
        stored: StoredObject,
    ) -> Resource:
        asset = await self.repository.get_asset(context.workspace_id, asset_id)
        file = asset["file"]
        expected = (
            file["object_key"],
            file["content_hash"],
            file["media_type"],
            int(file["byte_size"]),
        )
        actual = (
            command.object_key,
            command.sha256,
            command.content_type,
            stored.size,
        )
        if expected != actual:
            raise ConflictError(
                "ASSET_UPLOAD_MISMATCH",
                "Completed upload does not match the initiated media descriptor",
            )
        return await self.repository.complete_asset_upload(
            context.workspace_id, asset_id, byte_size=stored.size
        )

    async def list_assets(
        self,
        context: WorkspaceContext,
        library_id: UUID,
        *,
        statuses: tuple[str, ...] = (),
        kinds: tuple[str, ...] = (),
        copyright_statuses: tuple[str, ...] = (),
        analysis_statuses: tuple[str, ...] = (),
        tags: tuple[str, ...] = (),
        search: str | None = None,
    ) -> list[Resource]:
        return await self.repository.list_assets(
            context.workspace_id,
            library_id,
            statuses=statuses,
            kinds=kinds,
            copyright_statuses=copyright_statuses,
            analysis_statuses=analysis_statuses,
            tags=tags,
            search=search.strip() if search else None,
        )

    async def update_asset_metadata(
        self,
        context: WorkspaceContext,
        asset_id: UUID,
        command: AssetMetadataPatch,
        expected_revision: int,
    ) -> Resource:
        current = await self.repository.get_asset(context.workspace_id, asset_id)
        resource = deepcopy(current)
        changes = command.model_dump(exclude_unset=True)
        for field in ("title", "description", "copyright_status"):
            if field in changes:
                value = changes[field]
                resource[field] = value.strip() if isinstance(value, str) else value
        if "rights_evidence" in changes:
            resource.setdefault("metadata", {})["rights_evidence"] = changes[
                "rights_evidence"
            ]
            resource["metadata"]["rights_confirmed"] = changes.get(
                "rights_confirmed", False
            )
        if "tags" in changes:
            tags = list(dict.fromkeys(value.strip() for value in changes["tags"]))
            resource["tags"] = tags
            resource.setdefault("metadata", {})["tags"] = tags
        resource["revision"] = int(current.get("revision", 1)) + 1
        resource["updated_at"] = _now()
        return await self.repository.update_asset_metadata(
            resource,
            expected_revision=expected_revision,
            actor_id=context.user_id,
        )

    async def transition_asset_status(
        self,
        context: WorkspaceContext,
        asset_id: UUID,
        command: AssetStatusTransition,
        expected_revision: int,
    ) -> Resource:
        return await self.repository.transition_asset_status(
            context.workspace_id,
            asset_id,
            target_status=command.target_status,
            expected_revision=expected_revision,
            actor_id=context.user_id,
            reason=command.reason,
        )

    async def delete_asset(
        self, context: WorkspaceContext, asset_id: UUID, expected_revision: int
    ) -> Resource:
        return await self.repository.transition_asset_status(
            context.workspace_id,
            asset_id,
            target_status="deleted",
            expected_revision=expected_revision,
            actor_id=context.user_id,
            reason="soft_delete",
        )

    async def restore_asset(
        self, context: WorkspaceContext, asset_id: UUID, expected_revision: int
    ) -> Resource:
        current = await self.repository.get_asset(context.workspace_id, asset_id)
        if current["status"] != "deleted":
            raise ConflictError(
                "ASSET_NOT_DELETED", "Only a soft-deleted asset can be restored"
            )
        previous = str((current.get("metadata") or {}).get("status_before_delete", "disabled"))
        if previous == "deleted":
            previous = "disabled"
        return await self.repository.transition_asset_status(
            context.workspace_id,
            asset_id,
            target_status=previous,
            expected_revision=expected_revision,
            actor_id=context.user_id,
            reason="restore",
        )

    async def list_asset_related(
        self, context: WorkspaceContext, asset_id: UUID, relation: str
    ) -> list[Resource]:
        return await self.repository.list_asset_related(
            context.workspace_id, asset_id, relation
        )

    async def list_asset_import_jobs(
        self, context: WorkspaceContext, library_id: UUID
    ) -> list[Resource]:
        return await self.repository.list_asset_import_jobs(
            context.workspace_id, library_id
        )

    async def request_asset_reanalysis(
        self,
        context: WorkspaceContext,
        asset_id: UUID,
        command: AssetReanalysisRequest,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        payload = {
            "asset_id": str(asset_id),
            "expected_revision": expected_revision,
            **command.model_dump(mode="json"),
        }
        return await self.repository.create_asset_analysis_job(
            context.workspace_id,
            asset_id,
            expected_revision=expected_revision,
            actor_id=context.user_id,
            reason=command.reason,
            operation_key=(
                f"{context.workspace_id}:asset_reanalysis:{idempotency_key}"
            ),
            request_fingerprint=_fingerprint(payload),
        )

    async def batch_review_assets(
        self,
        context: WorkspaceContext,
        command: AssetBatchReview,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        async def apply() -> Resource:
            successes: list[Resource] = []
            failures: list[Resource] = []
            for item in command.items:
                try:
                    asset = await self.repository.transition_asset_status(
                        context.workspace_id,
                        item.asset_id,
                        target_status=command.target_status,
                        expected_revision=item.expected_revision,
                        actor_id=context.user_id,
                        reason=command.reason,
                    )
                    successes.append(
                        {"asset_id": asset["id"], "revision": asset["revision"]}
                    )
                except ApiError as exc:
                    failures.append(self._asset_batch_failure(item.asset_id, exc))
            return self._batch_result(successes, failures)

        return await self.repository.run_asset_batch_idempotently(
            context.workspace_id,
            operation_key=f"asset-batch-review:{context.workspace_id}:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
            request_path="/v1/assets/batch-review",
            action=apply,
        )

    async def batch_tag_assets(
        self,
        context: WorkspaceContext,
        command: AssetBatchTags,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        async def apply() -> Resource:
            successes: list[Resource] = []
            failures: list[Resource] = []
            additions = [value.strip() for value in command.add]
            removals = {value.strip().casefold() for value in command.remove}
            for item in command.items:
                try:
                    current = await self.repository.get_asset(
                        context.workspace_id, item.asset_id
                    )
                    if current["status"] == "deleted":
                        raise ConflictError(
                            "ASSET_DELETED", "Deleted assets cannot be tagged"
                        )
                    existing = [
                        value
                        for value in current.get("tags", [])
                        if str(value).casefold() not in removals
                    ]
                    tags = list(dict.fromkeys([*existing, *additions]))
                    updated = deepcopy(current)
                    updated["tags"] = tags
                    updated.setdefault("metadata", {})["tags"] = tags
                    updated["revision"] = int(current.get("revision", 1)) + 1
                    updated["updated_at"] = _now()
                    saved = await self.repository.update_asset_metadata(
                        updated,
                        expected_revision=item.expected_revision,
                        actor_id=context.user_id,
                    )
                    successes.append(
                        {"asset_id": saved["id"], "revision": saved["revision"]}
                    )
                except ApiError as exc:
                    failures.append(self._asset_batch_failure(item.asset_id, exc))
            return self._batch_result(successes, failures)

        return await self.repository.run_asset_batch_idempotently(
            context.workspace_id,
            operation_key=f"asset-batch-tags:{context.workspace_id}:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
            request_path="/v1/assets/batch-tags",
            action=apply,
        )

    async def batch_reanalyze_assets(
        self,
        context: WorkspaceContext,
        command: AssetBatchReanalysis,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        async def apply() -> Resource:
            successes: list[Resource] = []
            failures: list[Resource] = []
            for item in command.items:
                try:
                    job, _created = await self.repository.create_asset_analysis_job(
                        context.workspace_id,
                        item.asset_id,
                        expected_revision=item.expected_revision,
                        actor_id=context.user_id,
                        reason=command.reason,
                        operation_key=(
                            f"{context.workspace_id}:asset_reanalysis:"
                            f"{idempotency_key}:{item.asset_id}"
                        ),
                        request_fingerprint=_fingerprint(
                            {
                                "asset_id": str(item.asset_id),
                                "expected_revision": item.expected_revision,
                                "reason": command.reason,
                            }
                        ),
                    )
                    successes.append(
                        {"asset_id": str(item.asset_id), "job_id": job["id"]}
                    )
                except ApiError as exc:
                    failures.append(self._asset_batch_failure(item.asset_id, exc))
            return self._batch_result(successes, failures)

        return await self.repository.run_asset_batch_idempotently(
            context.workspace_id,
            operation_key=(
                f"asset-batch-reanalysis:{context.workspace_id}:{idempotency_key}"
            ),
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
            request_path="/v1/assets/batch-reanalyze",
            action=apply,
        )

    @staticmethod
    def _asset_batch_failure(asset_id: UUID, exc: ApiError) -> Resource:
        return {
            "asset_id": str(asset_id),
            "code": exc.code,
            "message": exc.message,
            **(
                {"current_revision": exc.details["current_revision"]}
                if "current_revision" in exc.details
                else {}
            ),
        }

    @staticmethod
    def _batch_result(successes: list[Resource], failures: list[Resource]) -> Resource:
        return {
            "succeeded_count": len(successes),
            "failed_count": len(failures),
            "succeeded": successes,
            "failures": failures,
        }

    async def list_skills(self, context: WorkspaceContext) -> list[Resource]:
        return await self.repository.list_skills(context.workspace_id)

    async def get_skill(self, context: WorkspaceContext, skill_id: UUID) -> Resource:
        return await self.repository.get_skill(context.workspace_id, skill_id)

    async def create_skill(
        self,
        context: WorkspaceContext,
        command: SkillIdentityInput,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        timestamp = _now()
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "publisher_type": "user",
            "publisher_name": command.publisher_name,
            "name": command.name,
            "slug": command.slug,
            "description": command.description,
            "visibility": command.visibility,
            "status": "draft",
            "current_version_id": None,
            "forked_from_skill_id": None,
            "revision": 1,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.validator.validate("skill", resource)
        return await self.repository.create_skill_idempotently(
            resource,
            operation_key=f"{context.workspace_id}:create_skill:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )

    async def replace_skill(
        self,
        context: WorkspaceContext,
        skill_id: UUID,
        command: SkillIdentityInput,
        expected_revision: int,
    ) -> Resource:
        current = await self.repository.get_skill(context.workspace_id, skill_id)
        if current["ownership_type"] == "system":
            raise ConflictError("SYSTEM_SKILL_IMMUTABLE", "System-owned skills cannot be replaced")
        resource = deepcopy(current)
        resource.update(
            publisher_name=command.publisher_name,
            name=command.name,
            slug=command.slug,
            description=command.description,
            visibility=command.visibility,
            revision=int(current.get("revision", 1)) + 1,
            updated_at=_now(),
        )
        self.validator.validate("skill", resource)
        return await self.repository.replace_skill(
            resource, expected_revision=expected_revision
        )

    async def delete_skill(self, context: WorkspaceContext, skill_id: UUID) -> None:
        await self.repository.delete_skill(context.workspace_id, skill_id)

    async def list_skill_versions(
        self, context: WorkspaceContext, skill_id: UUID | None = None
    ) -> list[Resource]:
        if skill_id is not None:
            await self.repository.get_skill(context.workspace_id, skill_id)
        return await self.repository.list_skill_versions(context.workspace_id, skill_id)

    async def get_skill_version(
        self, context: WorkspaceContext, version_id: UUID
    ) -> Resource:
        return await self.repository.get_skill_version(context.workspace_id, version_id)

    async def create_skill_version(
        self,
        context: WorkspaceContext,
        command: SkillVersionCreate,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        skill = await self.repository.get_skill(context.workspace_id, command.skill_id)
        if skill["ownership_type"] == "system":
            raise ConflictError(
                "SYSTEM_SKILL_IMMUTABLE", "Create a fork before editing a system-owned skill"
            )

        spec: dict[str, Any]
        if command.source_version_id is not None:
            source = await self.repository.get_skill_version(
                context.workspace_id, command.source_version_id
            )
            if source["skill_id"] != str(command.skill_id):
                raise ValidationError(
                    "source_version_id does not belong to skill_id", path="source_version_id"
                )
            spec = {field: deepcopy(source[field]) for field in HASH_DOMAIN_FIELDS}
        else:
            spec = {}
        supplied = command.model_dump(mode="json", exclude_unset=True)
        for field in HASH_DOMAIN_FIELDS:
            if field in supplied:
                spec[field] = supplied[field]
        if command.source_version_id is None:
            spec.setdefault("default_pipeline_version_id", None)

        timestamp = _now()
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "skill_id": str(command.skill_id),
            "version": command.version,
            "state": "draft",
            "execution_kind": "declarative",
            **spec,
            "content_hash": "",
            "revision": 1,
            "test_topics": command.test_topics,
            "release_notes": command.release_notes,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "published_at": None,
        }
        resource["content_hash"] = _content_hash(resource)
        self._validate_skill_version_semantics(resource)
        self.validator.validate("skill_version", resource)
        created, is_new = await self.repository.create_skill_version_idempotently(
            resource,
            operation_key=f"{context.workspace_id}:create_skill_version:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )
        if is_new:
            skill["status"] = "draft"
            skill["revision"] = int(skill.get("revision", 1)) + 1
            skill["updated_at"] = timestamp
            await self.repository.replace_skill(skill)
        return created, is_new

    async def patch_skill_version(
        self,
        context: WorkspaceContext,
        version_id: UUID,
        patch: SkillVersionPatch,
        expected_revision: int,
    ) -> Resource:
        version = await self.repository.get_skill_version(context.workspace_id, version_id)
        skill = await self.repository.get_skill(context.workspace_id, UUID(version["skill_id"]))
        if version["ownership_type"] == "system" or skill["ownership_type"] == "system":
            raise ConflictError("SYSTEM_SKILL_IMMUTABLE", "System-owned versions are immutable")
        if version["state"] not in {"draft", "ready", "rejected"}:
            raise ConflictError(
                "IMMUTABLE_SKILL_VERSION", "Published or validating versions cannot be edited"
            )
        updates = patch.model_dump(mode="json", exclude_unset=True)
        if not updates:
            raise ValidationError("At least one draft field must be supplied", path="$")
        version.update(updates)
        version["state"] = "draft"
        version["revision"] = int(version.get("revision", 1)) + 1
        version["content_hash"] = _content_hash(version)
        self._validate_skill_version_semantics(version)
        self.validator.validate("skill_version", version)
        stored = await self.repository.replace_skill_version(
            version, expected_revision=expected_revision
        )
        skill["status"] = "draft"
        skill["revision"] = int(skill.get("revision", 1)) + 1
        skill["updated_at"] = _now()
        await self.repository.replace_skill(skill)
        return stored

    async def delete_skill_version(
        self, context: WorkspaceContext, version_id: UUID, expected_revision: int
    ) -> None:
        version = await self.repository.get_skill_version(context.workspace_id, version_id)
        skill = await self.repository.get_skill(context.workspace_id, UUID(version["skill_id"]))
        if skill["ownership_type"] == "system":
            raise ConflictError("SYSTEM_SKILL_IMMUTABLE", "System-owned versions are immutable")
        await self.repository.delete_skill_version(
            context.workspace_id, version_id, expected_revision=expected_revision
        )
        if skill["current_version_id"] is None:
            await self.repository.delete_skill(context.workspace_id, UUID(skill["id"]))

    async def validate_skill_version(
        self,
        context: WorkspaceContext,
        version_id: UUID,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[ValidationReport, bool]:
        fingerprint = _fingerprint(
            {"version_id": version_id, "expected_revision": expected_revision}
        )

        async def action() -> ValidationReport:
            version = await self.repository.get_skill_version(context.workspace_id, version_id)
            skill = await self.repository.get_skill(
                context.workspace_id, UUID(version["skill_id"])
            )
            if version["ownership_type"] == "system" or skill["ownership_type"] == "system":
                raise ConflictError("SYSTEM_SKILL_IMMUTABLE", "System-owned versions are immutable")
            if version["state"] not in {"draft", "ready", "rejected"}:
                raise ConflictError(
                    "IMMUTABLE_SKILL_VERSION", "This SkillVersion cannot be validated"
                )
            writing = version["writing_policy"]["markdown_instructions"]
            test_topics = [topic for topic in version.get("test_topics", []) if topic.strip()]
            checks = [
                {
                    "id": "schema",
                    "label": "Input schema",
                    "passed": bool(version["input_schema"].get("properties")),
                    "severity": "error",
                    "message": "The input schema must declare at least one property.",
                    "path": "input_schema.properties",
                },
                {
                    "id": "instructions",
                    "label": "Writing instructions",
                    "passed": len(writing.strip()) >= 20,
                    "severity": "error",
                    "message": "Writing instructions must contain at least 20 characters.",
                    "path": "writing_policy.markdown_instructions",
                },
                {
                    "id": "dangerous-content",
                    "label": "Declarative-only content",
                    "passed": re.search(
                        r"(?:process\.env|<script|(?:[a-z]:\\|/etc/))", writing, re.I
                    )
                    is None,
                    "severity": "error",
                    "message": (
                        "Instructions cannot contain scripts, environment access, or local paths."
                    ),
                    "path": "writing_policy.markdown_instructions",
                },
                {
                    "id": "test-topics",
                    "label": "Test topics",
                    "passed": len(test_topics) >= 1,
                    "severity": "error",
                    "message": "At least one non-empty test topic is required.",
                    "path": "test_topics",
                },
                {
                    "id": "output-contract",
                    "label": "Output contract",
                    "passed": bool(version["output_contract"].get("artifacts")),
                    "severity": "error",
                    "message": "At least one output artifact must be declared.",
                    "path": "output_contract.artifacts",
                },
            ]
            ready = all(check["passed"] or check["severity"] != "error" for check in checks)
            version["state"] = "ready" if ready else "rejected"
            version["revision"] = int(version.get("revision", 1)) + 1
            await self.repository.replace_skill_version(
                version, expected_revision=expected_revision
            )
            skill["status"] = "draft"
            skill["revision"] = int(skill.get("revision", 1)) + 1
            skill["updated_at"] = _now()
            await self.repository.replace_skill(skill)
            return ValidationReport(
                skill_id=UUID(version["skill_id"]),
                version_id=version_id,
                ready=ready,
                revision=version["revision"],
                checks=checks,
                estimated_cost={"amount": 0.0, "currency": "CNY"},
            )

        return await self._idempotent_action(
            f"{context.workspace_id}:validate_skill_version:{idempotency_key}",
            fingerprint,
            action,
        )

    async def publish_skill_version(
        self,
        context: WorkspaceContext,
        version_id: UUID,
        expected_revision: int,
        release_notes: str | None,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        fingerprint = _fingerprint(
            {
                "version_id": version_id,
                "expected_revision": expected_revision,
                "release_notes": release_notes,
            }
        )

        async def action() -> Resource:
            version = await self.repository.get_skill_version(context.workspace_id, version_id)
            skill = await self.repository.get_skill(
                context.workspace_id, UUID(version["skill_id"])
            )
            if version["ownership_type"] == "system" or skill["ownership_type"] == "system":
                raise ConflictError("SYSTEM_SKILL_IMMUTABLE", "System-owned versions are immutable")
            if version["state"] != "ready":
                raise ConflictError(
                    "VALIDATION_REQUIRED", "The draft must pass validation before publication"
                )
            published_at = _now()
            version["state"] = "published"
            version["published_at"] = published_at
            version["revision"] = int(version.get("revision", 1)) + 1
            if release_notes is not None:
                version["release_notes"] = release_notes
            self.validator.validate("skill_version", version)
            stored = await self.repository.replace_skill_version(
                version, expected_revision=expected_revision
            )
            skill["current_version_id"] = version["id"]
            skill["status"] = "active"
            skill["revision"] = int(skill.get("revision", 1)) + 1
            skill["updated_at"] = published_at
            self.validator.validate("skill", skill)
            await self.repository.replace_skill(skill)
            return stored

        return await self._idempotent_action(
            f"{context.workspace_id}:publish_skill_version:{idempotency_key}",
            fingerprint,
            action,
        )

    async def fork_skill(
        self,
        context: WorkspaceContext,
        source_skill_id: UUID,
        command: ForkSkillRequest,
        idempotency_key: str,
    ) -> tuple[tuple[Resource, Resource], bool]:
        source_skill = await self.repository.get_skill(context.workspace_id, source_skill_id)
        source_version_id = command.source_version_id or (
            UUID(source_skill["current_version_id"])
            if source_skill["current_version_id"] is not None
            else None
        )
        if source_version_id is None:
            raise ConflictError(
                "FORK_SOURCE_VERSION_REQUIRED",
                "The source skill has no current version; source_version_id is required",
            )
        source_version = await self.repository.get_skill_version(
            context.workspace_id, source_version_id
        )
        if source_version["skill_id"] != str(source_skill_id):
            raise ValidationError(
                "source_version_id does not belong to the source skill",
                path="source_version_id",
            )
        if source_version["state"] not in {"ready", "published"}:
            raise ConflictError(
                "FORK_SOURCE_NOT_READY", "Only ready or published versions can be forked"
            )

        timestamp = _now()
        skill: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "publisher_type": "user",
            "publisher_name": command.publisher_name,
            "name": command.name,
            "slug": command.slug,
            "description": command.description or source_skill["description"],
            "visibility": command.visibility,
            "status": "draft",
            "current_version_id": None,
            "forked_from_skill_id": str(source_skill_id),
            "revision": 1,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        version = deepcopy(source_version)
        version.update(
            id=str(uuid4()),
            workspace_id=str(context.workspace_id),
            ownership_type="workspace",
            skill_id=skill["id"],
            version="0.1.0",
            state="draft",
            revision=1,
            created_by=str(context.user_id),
            created_at=timestamp,
            published_at=None,
        )
        version["content_hash"] = _content_hash(version)
        self.validator.validate("skill", skill)
        self.validator.validate("skill_version", version)
        return await self.repository.fork_skill_idempotently(
            skill,
            version,
            operation_key=f"{context.workspace_id}:fork_skill:{idempotency_key}",
            request_fingerprint=_fingerprint(
                {"source_skill_id": source_skill_id, **command.model_dump(mode="json")}
            ),
        )

    async def list_skill_test_executions(
        self, context: WorkspaceContext
    ) -> list[Resource]:
        return await self.repository.list_skill_test_executions(context.workspace_id)

    async def get_skill_test_execution(
        self, context: WorkspaceContext, execution_id: UUID
    ) -> Resource:
        return await self.repository.get_skill_test_execution(
            context.workspace_id, execution_id
        )

    async def create_skill_test_execution(
        self,
        context: WorkspaceContext,
        command: SkillTestExecutionCreate,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        await self.repository.get_skill(context.workspace_id, command.skill_id)
        left = await self.repository.get_skill_version(
            context.workspace_id, command.left_version_id
        )
        right = await self.repository.get_skill_version(
            context.workspace_id, command.right_version_id
        )
        for field, version in (("left_version_id", left), ("right_version_id", right)):
            if version["skill_id"] != str(command.skill_id):
                raise ValidationError(f"{field} does not belong to skill_id", path=field)
        timestamp = _now()
        result = self._comparison_result(command.topic, left, right, timestamp)
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "skill_id": str(command.skill_id),
            "left_version_id": str(command.left_version_id),
            "right_version_id": str(command.right_version_id),
            "topic": command.topic,
            "inputs": command.inputs,
            "status": "succeeded",
            "evaluator": "local_deterministic_v1",
            "result": result,
            "error": None,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "started_at": timestamp,
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
        self.validator.validate("skill_test_execution", resource)
        return await self.repository.create_skill_test_execution_idempotently(
            resource,
            operation_key=f"{context.workspace_id}:create_skill_test:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )

    async def list_runs(self, context: WorkspaceContext) -> list[Resource]:
        return await self.repository.list_runs(context.workspace_id)

    async def get_run(self, context: WorkspaceContext, run_id: UUID) -> Resource:
        return await self.repository.get_run(context.workspace_id, run_id)

    async def cancel_run(
        self,
        context: WorkspaceContext,
        run_id: UUID,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        return await self.repository.cancel_run_idempotently(
            context.workspace_id,
            run_id,
            operation_key=f"{context.workspace_id}:cancel_run:{idempotency_key}",
            request_fingerprint=_fingerprint({"run_id": str(run_id)}),
        )

    async def list_run_steps(
        self, context: WorkspaceContext, run_id: UUID
    ) -> list[Resource]:
        return await self.repository.list_run_steps(context.workspace_id, run_id)

    async def get_run_step(
        self, context: WorkspaceContext, step_id: str
    ) -> Resource:
        return await self.repository.get_run_step(context.workspace_id, step_id)

    async def retry_run_step(
        self,
        context: WorkspaceContext,
        step_id: str,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        return await self.repository.retry_run_step_idempotently(
            context.workspace_id,
            step_id,
            actor_id=context.user_id,
            operation_key=f"{context.workspace_id}:retry_step:{idempotency_key}",
            request_fingerprint=_fingerprint({"step_id": step_id}),
        )

    async def review_run_step(
        self,
        context: WorkspaceContext,
        step_id: str,
        command: StepReviewRequest,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        return await self.repository.review_run_step_idempotently(
            context.workspace_id,
            step_id,
            decision=command.decision,
            actor_id=context.user_id,
            comment=command.reason,
            issue_codes=command.issue_codes,
            expected_revision=command.expected_revision,
            operation_key=f"{context.workspace_id}:review_step:{idempotency_key}",
            request_fingerprint=_fingerprint(
                {
                    "step_id": step_id,
                    **command.model_dump(mode="json"),
                }
            ),
        )

    async def list_artifacts(
        self, context: WorkspaceContext, run_id: UUID
    ) -> list[Resource]:
        return await self.repository.list_artifacts(context.workspace_id, run_id)

    async def get_artifact(
        self, context: WorkspaceContext, artifact_id: UUID
    ) -> Resource:
        return await self.repository.get_artifact(context.workspace_id, artifact_id)

    async def list_events(
        self,
        context: WorkspaceContext,
        run_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 101,
    ) -> list[Resource]:
        return await self.repository.list_events(
            context.workspace_id,
            run_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    async def get_event(self, context: WorkspaceContext, event_id: UUID) -> Resource:
        return await self.repository.get_event(context.workspace_id, event_id)

    async def create_run(
        self,
        context: WorkspaceContext,
        command: RunCreate,
        idempotency_key: str,
    ) -> tuple[Resource, bool]:
        composition = await self._resolve_run_composition(context, command)
        version = await self.repository.get_skill_version(
            context.workspace_id, composition.skill_version_id
        )
        if version["state"] != "published":
            raise ConflictError(
                "SKILL_VERSION_NOT_PUBLISHED", "Runs require a published skill version"
            )
        pipeline = await self.repository.get_pipeline_version(
            context.workspace_id, composition.pipeline_version_id
        )
        if pipeline["state"] != "published" or pipeline["status"] != "active":
            raise ConflictError(
                "PIPELINE_VERSION_NOT_PUBLISHED",
                "Runs require an active published Pipeline version",
            )
        capability_gaps = self._pipeline_capability_gaps(pipeline)
        if capability_gaps:
            raise ConflictError(
                "RUN_CAPABILITY_UNAVAILABLE",
                "The selected Pipeline has no configured production provider",
                capability_gaps=capability_gaps,
            )
        production_settings = await self._resolve_production_settings(
            context, command.video_settings
        )
        if production_settings["asset_acquisition"]["enabled"]:
            if not composition.asset_library_ids:
                raise ValidationError(
                    "automatic asset acquisition requires an asset library",
                    path="composition.asset_library_ids",
                )
            await self.repository.get_asset_library(
                context.workspace_id, composition.asset_library_ids[0]
            )
        timestamp = _now()
        render_preset = (
            {
                "id": str(composition.render_preset_version_id),
                "content_hash": _reference_fingerprint(
                    "render_preset_version", composition.render_preset_version_id
                ),
            }
            if composition.render_preset_version_id is not None
            else None
        )
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "channel_id": str(command.channel_id) if command.channel_id else None,
            "status": "queued",
            "idempotency_key": idempotency_key,
            "input": command.input,
            "composition_snapshot": {
                "skill_version": {
                    "id": version["id"],
                    "content_hash": version["content_hash"],
                },
                "asset_library_ids": [str(value) for value in composition.asset_library_ids],
                "voice_profile_id": (
                    str(composition.voice_profile_id)
                    if composition.voice_profile_id is not None
                    else None
                ),
                "render_preset": render_preset,
                "pipeline_version": {
                    "id": pipeline["id"],
                    "content_hash": pipeline["content_hash"],
                },
                "capabilities": composition.capabilities,
                "production_settings": production_settings,
            },
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "started_at": None,
            "finished_at": None,
            "updated_at": timestamp,
        }
        self.validator.validate("run", resource)
        return await self.repository.create_run_idempotently(
            resource,
            operation_key=f"{context.workspace_id}:create_run:{idempotency_key}",
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )

    async def list_generation_batches(
        self, context: WorkspaceContext
    ) -> list[Resource]:
        return await self.repository.list_generation_batches(context.workspace_id)

    async def get_generation_batch(
        self, context: WorkspaceContext, batch_id: UUID
    ) -> Resource:
        return await self.repository.get_generation_batch(
            context.workspace_id, batch_id
        )

    async def list_generation_batch_items(
        self,
        context: WorkspaceContext,
        batch_id: UUID,
        *,
        status: str | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Resource], int]:
        return await self.repository.list_generation_batch_items(
            context.workspace_id,
            batch_id,
            status=status,
            search=search,
            offset=offset,
            limit=limit,
        )

    async def create_generation_batch(
        self,
        context: WorkspaceContext,
        command: GenerationBatchCreate,
        idempotency_key: str,
    ) -> tuple[Resource, list[Resource], bool]:
        composition = command.composition
        version = await self.repository.get_skill_version(
            context.workspace_id, composition.skill_version_id
        )
        if version["state"] != "published":
            raise ConflictError(
                "SKILL_VERSION_NOT_PUBLISHED",
                "Batch Runs require a published skill version",
            )
        pipeline = await self.repository.get_pipeline_version(
            context.workspace_id, composition.pipeline_version_id
        )
        if pipeline["state"] != "published" or pipeline["status"] != "active":
            raise ConflictError(
                "PIPELINE_VERSION_NOT_PUBLISHED",
                "Batch Runs require an active published Pipeline version",
            )
        capability_gaps = self._pipeline_capability_gaps(pipeline)
        if capability_gaps:
            raise ConflictError(
                "RUN_CAPABILITY_UNAVAILABLE",
                "The selected Pipeline has no configured production provider",
                capability_gaps=capability_gaps,
            )
        production_settings = await self._resolve_production_settings(
            context, command.video_settings
        )
        if production_settings["asset_acquisition"]["enabled"]:
            if not composition.asset_library_ids:
                raise ValidationError(
                    "automatic asset acquisition requires an asset library",
                    path="composition.asset_library_ids",
                )
            await self.repository.get_asset_library(
                context.workspace_id, composition.asset_library_ids[0]
            )
        timestamp = _now()
        batch_id = uuid4()
        render_preset = (
            {
                "id": str(composition.render_preset_version_id),
                "content_hash": _reference_fingerprint(
                    "render_preset_version", composition.render_preset_version_id
                ),
            }
            if composition.render_preset_version_id is not None
            else None
        )
        composition_snapshot = {
            "skill_version": {
                "id": version["id"],
                "content_hash": version["content_hash"],
            },
            "asset_library_ids": [
                str(value) for value in composition.asset_library_ids
            ],
            "voice_profile_id": (
                str(composition.voice_profile_id)
                if composition.voice_profile_id is not None
                else None
            ),
            "render_preset": render_preset,
            "pipeline_version": {
                "id": pipeline["id"],
                "content_hash": pipeline["content_hash"],
            },
            "capabilities": composition.capabilities,
            "production_settings": production_settings,
        }
        batch: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(batch_id),
            "workspace_id": str(context.workspace_id),
            "name": command.name.strip(),
            "composition_snapshot": composition_snapshot,
            "created_by": str(context.user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        runs: list[Resource] = []
        items: list[Resource] = []
        for ordinal, entry in enumerate(command.items):
            run_id = uuid4()
            run_input = {**entry.inputs, "topic": entry.topic.strip()}
            run: Resource = {
                "schema_version": CONTRACT_VERSION,
                "id": str(run_id),
                "workspace_id": str(context.workspace_id),
                "ownership_type": "workspace",
                "channel_id": str(command.channel_id) if command.channel_id else None,
                "status": "queued",
                "idempotency_key": f"batch:{batch_id}:{ordinal}",
                "input": run_input,
                "composition_snapshot": composition_snapshot,
                "created_by": str(context.user_id),
                "created_at": timestamp,
                "started_at": None,
                "finished_at": None,
                "updated_at": timestamp,
            }
            self.validator.validate("run", run)
            runs.append(run)
            items.append(
                {
                    "id": str(uuid4()),
                    "workspace_id": str(context.workspace_id),
                    "batch_id": str(batch_id),
                    "run_id": str(run_id),
                    "ordinal": ordinal,
                    "label": entry.topic.strip()[:300],
                    "input": run_input,
                    "created_at": timestamp,
                }
            )
        resource, created = await self.repository.create_generation_batch_idempotently(
            batch,
            runs,
            items,
            operation_key=(
                f"{context.workspace_id}:create_generation_batch:{idempotency_key}"
            ),
            request_fingerprint=_fingerprint(command.model_dump(mode="json")),
        )
        if not created:
            replay_items, _ = await self.repository.list_generation_batch_items(
                context.workspace_id,
                UUID(resource["id"]),
                status=None,
                search=None,
                offset=0,
                limit=5000,
            )
            runs = [
                await self.repository.get_run(
                    context.workspace_id, UUID(item["run_id"])
                )
                for item in replay_items
            ]
        return resource, runs, created

    async def cancel_generation_batch(
        self,
        context: WorkspaceContext,
        batch_id: UUID,
        idempotency_key: str,
    ) -> Resource:
        await self.repository.get_generation_batch(context.workspace_id, batch_id)
        items, _ = await self.repository.list_generation_batch_items(
            context.workspace_id,
            batch_id,
            status=None,
            search=None,
            offset=0,
            limit=5000,
        )
        semaphore = asyncio.Semaphore(32)

        async def cancel(item: Resource) -> None:
            async with semaphore:
                await self.cancel_run(
                    context,
                    UUID(item["run_id"]),
                    f"{idempotency_key}:{item['run_id']}",
                )

        await asyncio.gather(*(cancel(item) for item in items))
        return await self.repository.get_generation_batch(
            context.workspace_id, batch_id
        )

    async def retry_failed_generation_batch(
        self,
        context: WorkspaceContext,
        batch_id: UUID,
        command: GenerationBatchRetryFailed,
        idempotency_key: str,
    ) -> tuple[Resource, list[Resource], bool]:
        source = await self.repository.get_generation_batch(
            context.workspace_id, batch_id
        )
        failed, _ = await self.repository.list_generation_batch_items(
            context.workspace_id,
            batch_id,
            status="failed",
            search=None,
            offset=0,
            limit=5000,
        )
        if not failed:
            raise ConflictError(
                "BATCH_HAS_NO_FAILED_ITEMS",
                "The generation batch has no failed items to retry",
            )
        first_run = await self.repository.get_run(
            context.workspace_id, UUID(failed[0]["run_id"])
        )
        snapshot = source["composition_snapshot"]
        skill = snapshot["skill_version"]
        pipeline = snapshot["pipeline_version"]
        render = snapshot.get("render_preset")
        production = snapshot.get("production_settings", {})
        subtitle_settings = production.get("subtitles", {})
        retry_command = GenerationBatchCreate(
            name=(command.name or f"{source['name']} · retry")[:160],
            channel_id=first_run.get("channel_id"),
            items=[
                BatchItemCreate(
                    topic=str(item["input"]["topic"]),
                    inputs={
                        key: value
                        for key, value in item["input"].items()
                        if key != "topic"
                    },
                )
                for item in failed
            ],
            composition=RunCompositionInput(
                skill_version_id=UUID(skill["id"]),
                pipeline_version_id=UUID(pipeline["id"]),
                asset_library_ids=[
                    UUID(value) for value in snapshot.get("asset_library_ids", [])
                ],
                voice_profile_id=(
                    UUID(snapshot["voice_profile_id"])
                    if snapshot.get("voice_profile_id")
                    else None
                ),
                render_preset_version_id=(
                    UUID(render["id"]) if render is not None else None
                ),
                capabilities=list(snapshot.get("capabilities", [])),
            ),
            video_settings=VideoSettingsInput(
                language=production.get("language"),
                aspect_ratio=production.get("aspect_ratio"),
                target_duration_seconds=production.get("target_duration_seconds"),
                visibility=production.get("visibility"),
                auto_quality_check=production.get("auto_quality_check"),
                layout=production.get("layout"),
                media_fit=production.get("media_fit"),
                frame_rate=production.get("frame_rate"),
                subtitles=subtitle_settings or None,
            ),
        )
        return await self.create_generation_batch(
            context,
            retry_command,
            f"retry-failed:{batch_id}:{idempotency_key}",
        )

    @staticmethod
    def _comparison_result(
        topic: str, left: Resource, right: Resource, created_at: str
    ) -> Resource:
        def side(version: Resource, label: str) -> Resource:
            instructions = version["writing_policy"]["markdown_instructions"]
            return {
                "version_id": version["id"],
                "version": version["version"],
                "output": f"{topic}: {label} applies the stored declarative policy.",
                "cost": {"amount": 0.0, "currency": "CNY"},
                "duration_ms": 0,
                "checks": [
                    {
                        "id": "declarative-policy",
                        "label": "Declarative policy",
                        "passed": bool(instructions.strip()),
                        "score": 1.0 if instructions.strip() else 0.0,
                        "note": "Evaluated locally without model or network execution.",
                    }
                ],
            }

        left_instructions = left["writing_policy"]["markdown_instructions"]
        right_instructions = right["writing_policy"]["markdown_instructions"]
        differences = []
        if left_instructions != right_instructions:
            differences.append(
                {
                    "path": "writing_policy.markdown_instructions",
                    "change": "changed",
                    "left": left_instructions,
                    "right": right_instructions,
                }
            )
        return {
            "topic": topic,
            "left": side(left, "Left version"),
            "right": side(right, "Right version"),
            "differences": differences,
            "created_at": created_at,
        }

    @staticmethod
    def _validate_skill_version_semantics(resource: Resource) -> None:
        research = resource["research_policy"]
        if research["minimum_sources"] > research["maximum_sources"]:
            raise ValidationError(
                "minimum_sources must be less than or equal to maximum_sources",
                path="research_policy.minimum_sources",
            )
        shot = resource["visual_policy"]["shot_duration_seconds"]
        if not shot["minimum"] <= shot["target"] <= shot["maximum"]:
            raise ValidationError(
                "shot duration must satisfy minimum <= target <= maximum",
                path="visual_policy.shot_duration_seconds",
            )
        declared = set(resource["input_schema"]["properties"])
        required = set(resource["input_schema"]["required"])
        if not required <= declared:
            raise ValidationError(
                "input_schema.required contains fields not declared in properties",
                path="input_schema.required",
            )
