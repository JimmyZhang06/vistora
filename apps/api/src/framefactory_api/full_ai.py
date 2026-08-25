from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from .context import WorkspaceContext
from .errors import ApiError, ConflictError
from .models import StrictModel
from .queue import JobQueue, QueueError
from .repository import Resource

CONTRACT_VERSION = "1.0.0"
FULL_AI_MODE = "generated_only"
FULL_AI_PIPELINE_SLUG = "full-ai-production"
FULL_AI_PIPELINE_VERSION = 2
FULL_AI_PIPELINE_VERSION_ID = UUID("6d6bad5a-e758-5d6c-9c39-e7c7713e5a4c")
FULL_AI_SKILL_VERSION_ID = UUID("0979a0d0-4e92-589b-9559-5655b4fbaee0")
QUOTE_WINDOW_SECONDS = 10 * 60

Direction = Literal["cinematic", "graphic", "illustrated"]
AspectRatio = Literal["9:16", "16:9"]
DurationSeconds = Literal[15, 30, 45, 60]
VariantsPerScene = Literal[1, 2, 3]


class FullAiBlocker(StrictModel):
    code: str
    message: str
    retryable: bool


class FullAiPipelineOption(StrictModel):
    slug: Literal["full-ai-production"] = FULL_AI_PIPELINE_SLUG
    version: Literal[2] = FULL_AI_PIPELINE_VERSION
    visual_source_mode: Literal["generated_only"] = FULL_AI_MODE


class FullAiProviderOption(StrictModel):
    name: str | None
    model_id: str | None
    status: Literal["ready", "unconfigured", "incomplete"]
    supports_reconciliation: Literal[False] = False
    submit_unknown_policy: Literal["manual_only"] = "manual_only"
    continuity_modes: list[str] = Field(default_factory=lambda: ["prompt_pack", "none"])


class FullAiLimits(StrictModel):
    brief_max_length: Literal[1600] = 1600
    duration_seconds: list[int] = Field(default_factory=lambda: [15, 30, 45, 60])
    aspect_ratios: list[str] = Field(default_factory=lambda: ["9:16", "16:9"])
    directions: list[str] = Field(
        default_factory=lambda: ["cinematic", "graphic", "illustrated"]
    )
    variants_per_scene: list[int] = Field(default_factory=lambda: [1, 2, 3])
    clip_seconds: Literal[5] = 5


class FullAiOptionsResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    mode: Literal["generated_only"] = FULL_AI_MODE
    status: Literal["ready", "blocked"]
    pipeline: FullAiPipelineOption = Field(default_factory=FullAiPipelineOption)
    provider: FullAiProviderOption
    limits: FullAiLimits = Field(default_factory=FullAiLimits)
    blockers: list[FullAiBlocker]


class FullAiVideoSpec(StrictModel):
    brief: str = Field(min_length=1, max_length=1600)
    direction: Direction
    aspect_ratio: AspectRatio
    duration_seconds: DurationSeconds
    variants_per_scene: VariantsPerScene
    continuity: bool
    ai_disclosure: bool

    @field_validator("brief")
    @classmethod
    def normalize_brief(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("brief must contain visible text")
        return normalized


class FullAiEstimateRequest(FullAiVideoSpec):
    pass


class FullAiPlan(StrictModel):
    scene_count: int
    clip_seconds: Literal[5] = 5
    candidate_count: int
    billable_seconds: int


class FullAiQuote(StrictModel):
    currency: Literal["USD"] = "USD"
    amount_minor: int = Field(ge=0)
    expires_at: datetime


class FullAiEstimateResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    status: Literal["ready", "blocked"]
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan: FullAiPlan
    quote: FullAiQuote | None
    blockers: list[FullAiBlocker]


class FullAiRunCreate(FullAiVideoSpec):
    estimate_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_cost_minor: int = Field(ge=0, le=100_000_000)
    currency: Literal["USD"] = "USD"

    def spec(self) -> FullAiVideoSpec:
        return FullAiVideoSpec.model_validate(
            self.model_dump(
                exclude={"estimate_fingerprint", "max_cost_minor", "currency"}
            )
        )


class FullAiSelectedProvider(StrictModel):
    name: str
    model_id: str


class FullAiBilling(StrictModel):
    status: Literal[
        "not_started",
        "submitting",
        "submitted",
        "submit_unknown",
        "settled",
        "failed",
    ]
    authorized_amount_minor: int = Field(ge=0)
    incurred_amount_minor: int = Field(ge=0)
    requires_reconciliation: bool


class FullAiRunResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    id: UUID
    project_run_id: UUID
    workspace_id: UUID
    status: Literal[
        "queued",
        "planning",
        "generating",
        "assembling",
        "quality_check",
        "succeeded",
        "failed",
        "cancelled",
        "reconciliation_required",
    ]
    mode: Literal["generated_only"] = FULL_AI_MODE
    provider: FullAiSelectedProvider
    spec: FullAiVideoSpec
    quote: FullAiQuote
    billing: FullAiBilling
    created_at: datetime
    updated_at: datetime


class FullAiRepository(Protocol):
    async def get_pipeline_version(
        self, workspace_id: UUID, version_id: UUID
    ) -> Resource: ...

    async def get_skill_version(
        self, workspace_id: UUID, version_id: UUID
    ) -> Resource: ...

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

    async def get_full_ai_run(
        self, workspace_id: UUID, full_ai_run_id: UUID
    ) -> Resource: ...


@dataclass(frozen=True, slots=True)
class FullAiRuntimeConfiguration:
    provider_name: str | None
    model_id: str | None
    cost_per_second_minor: int | None
    credit_unit_minor: int | None
    worker_capabilities: tuple[str, ...] | None
    queue_available: bool
    object_storage_available: bool
    terms_reference: str | None = None
    terms_content_hash: str | None = None
    terms_captured_at: str | None = None
    pricing_reference: str | None = None
    pricing_content_hash: str | None = None
    pricing_captured_at: str | None = None
    output_rights_confirmed: bool = False
    output_rights_license_basis: str | None = None

    def blockers(self) -> tuple[FullAiBlocker, ...]:
        if not self.provider_name and not self.model_id:
            return (
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_NOT_CONFIGURED",
                    message="The Full-AI generation provider is not configured",
                    retryable=False,
                ),
            )

        blockers: list[FullAiBlocker] = []
        if not self.provider_name:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_NAME_NOT_CONFIGURED",
                    message="The Full-AI provider name is missing",
                    retryable=False,
                )
            )
        if not self.model_id:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_MODEL_NOT_CONFIGURED",
                    message="The Full-AI provider model is missing",
                    retryable=False,
                )
            )
        elif self.model_id != "gen4.5":
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_MODEL_UNSUPPORTED",
                    message="P1 supports only the Runway gen4.5 model",
                    retryable=False,
                )
            )
        if self.provider_name and self.provider_name.casefold() != "runway":
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_UNSUPPORTED",
                    message="P1 supports only the Runway provider",
                    retryable=False,
                )
            )
        if self.cost_per_second_minor is None or self.cost_per_second_minor <= 0:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PRICING_NOT_CONFIGURED",
                    message="A durable price rate is required before paid generation",
                    retryable=False,
                )
            )
        if self.credit_unit_minor != 1:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PRICING_UNIT_NOT_CONFIGURED",
                    message=(
                        "Runway credit pricing must be frozen as one USD cent per credit"
                    ),
                    retryable=False,
                )
            )
        if not _valid_external_snapshot(
            self.terms_reference,
            self.terms_content_hash,
            self.terms_captured_at,
        ):
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_TERMS_NOT_FROZEN",
                    message="Provider terms must be captured and content-addressed",
                    retryable=False,
                )
            )
        if not self.output_rights_confirmed:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_OUTPUT_RIGHTS_UNCONFIRMED",
                    message=(
                        "An operator must confirm the Provider output rights before generation"
                    ),
                    retryable=False,
                )
            )
        elif not (self.output_rights_license_basis or "").strip():
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_OUTPUT_RIGHTS_BASIS_MISSING",
                    message="The operator rights attestation requires a license basis",
                    retryable=False,
                )
            )
        if not _valid_external_snapshot(
            self.pricing_reference,
            self.pricing_content_hash,
            self.pricing_captured_at,
        ):
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PROVIDER_PRICING_NOT_FROZEN",
                    message="Provider pricing must be captured and content-addressed",
                    retryable=False,
                )
            )
        capabilities = frozenset(self.worker_capabilities or ())
        required = {
            "model.text_generation",
            "model.video_generation",
            "model.generated_video_verification",
            "writing.compose.generated",
            "audio.synthesize",
            "media.generate",
            "timeline.align",
            "render.edl",
            "render.subtitle_sentence",
            "quality.evaluate",
        }
        missing = sorted(required - capabilities)
        if missing:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_GENERATION_CAPABILITY_UNAVAILABLE",
                    message=f"The worker is missing capabilities: {', '.join(missing)}",
                    retryable=True,
                )
            )
        if not self.queue_available:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_QUEUE_UNAVAILABLE",
                    message="The durable Run queue is unavailable",
                    retryable=True,
                )
            )
        if not self.object_storage_available:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_OBJECT_STORAGE_UNAVAILABLE",
                    message="Object storage is required for generated media",
                    retryable=True,
                )
            )
        return tuple(blockers)

    @property
    def provider_status(self) -> Literal["ready", "unconfigured", "incomplete"]:
        if not self.provider_name and not self.model_id:
            return "unconfigured"
        return "ready" if not self.blockers() else "incomplete"


class FullAiControlService:
    def __init__(
        self,
        repository: FullAiRepository,
        configuration: FullAiRuntimeConfiguration,
        *,
        queue: JobQueue | None,
        validate_scheduler_run: Callable[[Resource], None],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.configuration = configuration
        self.queue = queue
        self.validate_scheduler_run = validate_scheduler_run
        self._now = now or (lambda: datetime.now(UTC))

    async def options(self, context: WorkspaceContext) -> FullAiOptionsResponse:
        blockers = list(self.configuration.blockers())
        catalog_blockers = await self._catalog_blockers(context)
        blockers.extend(catalog_blockers)
        return FullAiOptionsResponse(
            status="blocked" if blockers else "ready",
            provider=FullAiProviderOption(
                name=self.configuration.provider_name,
                model_id=self.configuration.model_id,
                status=self.configuration.provider_status,
            ),
            blockers=blockers,
        )

    async def estimate(
        self, context: WorkspaceContext, command: FullAiEstimateRequest
    ) -> FullAiEstimateResponse:
        blockers = list(self.configuration.blockers())
        blockers.extend(await self._catalog_blockers(context))
        if not command.ai_disclosure:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_DISCLOSURE_REQUIRED",
                    message="AI-generated content disclosure must remain enabled",
                    retryable=False,
                )
            )
        plan = _plan(command)
        if blockers:
            return FullAiEstimateResponse(
                status="blocked",
                request_fingerprint=_fingerprint(command.model_dump(mode="json")),
                plan=plan,
                quote=None,
                blockers=blockers,
            )
        return self._ready_estimate(command, plan)

    async def create_run(
        self,
        context: WorkspaceContext,
        command: FullAiRunCreate,
        idempotency_key: str,
    ) -> tuple[FullAiRunResponse, bool]:
        request_hash = _fingerprint(command.model_dump(mode="json"))
        previous = await self.repository.find_full_ai_run_by_idempotency_key(
            context.workspace_id, idempotency_key
        )
        if previous is not None:
            if previous.get("request_hash") != request_hash:
                raise ConflictError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency-Key was already used with a different request body",
                )
            await self._ensure_enqueued(previous)
            return (
                FullAiRunResponse.model_validate(_public_full_ai_resource(previous)),
                False,
            )

        spec = command.spec()
        estimate = await self.estimate(
            context, FullAiEstimateRequest.model_validate(spec.model_dump())
        )
        if estimate.status != "ready" or estimate.quote is None:
            raise ApiError(
                503,
                "FULL_AI_PROVIDER_UNAVAILABLE",
                "Full-AI generation is blocked until every production dependency is ready",
                details={
                    "blockers": [item.model_dump(mode="json") for item in estimate.blockers]
                },
            )
        if command.estimate_fingerprint != estimate.request_fingerprint:
            raise ConflictError(
                "FULL_AI_ESTIMATE_STALE",
                "The Full-AI estimate expired or no longer matches this request",
            )
        if command.max_cost_minor < estimate.quote.amount_minor:
            raise ConflictError(
                "FULL_AI_BUDGET_EXCEEDED",
                "The current quote exceeds the authorized Full-AI budget",
                quoted_amount_minor=estimate.quote.amount_minor,
                max_cost_minor=command.max_cost_minor,
                currency=command.currency,
            )

        timestamp = self._now().astimezone(UTC)
        full_ai_run_id = uuid4()
        scheduler_run_id = uuid4()
        pipeline = await self.repository.get_pipeline_version(
            context.workspace_id, FULL_AI_PIPELINE_VERSION_ID
        )
        skill_version = await self.repository.get_skill_version(
            context.workspace_id, FULL_AI_SKILL_VERSION_ID
        )
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(full_ai_run_id),
            "workspace_id": str(context.workspace_id),
            "underlying_run_id": str(scheduler_run_id),
            "status": "queued",
            "mode": FULL_AI_MODE,
            "provider": {
                "name": self.configuration.provider_name,
                "model_id": self.configuration.model_id,
            },
            "spec": spec.model_dump(mode="json"),
            "plan": estimate.plan.model_dump(mode="json"),
            "quote": estimate.quote.model_dump(mode="json"),
            "billing": {
                "status": "not_started",
                "authorized_amount_minor": command.max_cost_minor,
                "incurred_amount_minor": 0,
                "requires_reconciliation": False,
            },
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "estimate_fingerprint": command.estimate_fingerprint,
            "created_by": str(context.user_id),
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "updated_at": timestamp.isoformat().replace("+00:00", "Z"),
        }
        scheduler_run: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(scheduler_run_id),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "channel_id": None,
            "status": "queued",
            "idempotency_key": f"full-ai:{_fingerprint(idempotency_key)[:40]}",
            "input": {
                "topic": spec.brief,
                "brief": spec.brief,
                "direction": spec.direction,
                "aspect_ratio": spec.aspect_ratio,
                "duration_seconds": spec.duration_seconds,
                "variants_per_scene": spec.variants_per_scene,
                "continuity": spec.continuity,
                "ai_disclosure": spec.ai_disclosure,
                "visual_source_mode": FULL_AI_MODE,
                "full_ai_run_id": str(full_ai_run_id),
                "scene_count": estimate.plan.scene_count,
                "candidate_count": estimate.plan.candidate_count,
                "billable_seconds": estimate.plan.billable_seconds,
                "max_cost_minor": command.max_cost_minor,
            },
            "composition_snapshot": {
                "skill_version": {
                    "id": skill_version["id"],
                    "content_hash": skill_version["content_hash"],
                },
                # Kept as an empty compatibility field for existing Run consumers.
                # The dedicated endpoint accepts no library identifier and never fills it.
                "asset_library_ids": [],
                "voice_profile_id": None,
                "render_preset": None,
                "pipeline_version": {
                    "id": pipeline["id"],
                    "content_hash": pipeline["content_hash"],
                },
                "visual_source_mode": FULL_AI_MODE,
                "full_ai_generation": {
                    "provider_name": self.configuration.provider_name,
                    "model_id": self.configuration.model_id,
                    "ratio": (
                        "720:1280" if spec.aspect_ratio == "9:16" else "1280:720"
                    ),
                    "variants_per_beat": spec.variants_per_scene,
                    "target_duration_seconds": spec.duration_seconds,
                    "scene_count": estimate.plan.scene_count,
                    "candidate_count": estimate.plan.candidate_count,
                    "billable_seconds": estimate.plan.billable_seconds,
                    "max_cost_minor": command.max_cost_minor,
                    "continuity_mode": (
                        "prompt_pack" if spec.continuity else "none"
                    ),
                    "full_ai_run_id": str(full_ai_run_id),
                    "terms_snapshot": {
                        "ref": self.configuration.terms_reference,
                        "content_hash": self.configuration.terms_content_hash,
                        "captured_at": self.configuration.terms_captured_at,
                    },
                    "pricing_snapshot": {
                        "ref": self.configuration.pricing_reference,
                        "content_hash": self.configuration.pricing_content_hash,
                        "captured_at": self.configuration.pricing_captured_at,
                        "currency": "USD",
                        "credit_unit_minor": self.configuration.credit_unit_minor,
                        "cost_per_second_minor": self.configuration.cost_per_second_minor,
                    },
                    "output_rights_confirmed": (
                        self.configuration.output_rights_confirmed
                    ),
                    "output_rights_license_basis": (
                        self.configuration.output_rights_license_basis
                    ),
                },
                "production_settings": _production_settings(spec),
                "capabilities": list(pipeline.get("capability_requirements", [])),
            },
            "created_by": str(context.user_id),
            "created_at": resource["created_at"],
            "started_at": None,
            "finished_at": None,
            "updated_at": resource["updated_at"],
        }
        self.validate_scheduler_run(scheduler_run)
        saved, created = await self.repository.create_full_ai_run_idempotently(
            resource,
            scheduler_run,
            operation_key=f"{context.workspace_id}:create_full_ai_run:{idempotency_key}",
            request_fingerprint=resource["request_hash"],
        )
        await self._ensure_enqueued(saved)
        return FullAiRunResponse.model_validate(_public_full_ai_resource(saved)), created

    async def _ensure_enqueued(self, saved: Resource) -> None:
        """Idempotently close the DB-commit to durable-dispatch recovery window."""

        if saved.get("status") != "queued":
            return
        if self.queue is None:  # guarded by the readiness check; defensive fail-closed
            raise ApiError(
                503,
                "FULL_AI_DISPATCH_UNAVAILABLE",
                "The Full-AI Run was saved but the durable queue is unavailable",
                details={"full_ai_run_id": saved["id"]},
            )
        try:
            await self.queue.enqueue(
                queue_name="runs",
                payload={
                    "workspace_id": saved["workspace_id"],
                    "run_id": saved["underlying_run_id"],
                    "full_ai_run_id": saved["id"],
                },
                deduplication_key=(
                    f"run:{saved['workspace_id']}:{saved['underlying_run_id']}"
                ),
                # Provider submission retry is controlled by the paid-operation ledger,
                # never by an unbounded transport retry.
                max_attempts=1,
            )
        except QueueError as exc:
            raise ApiError(
                503,
                "FULL_AI_DISPATCH_UNAVAILABLE",
                "The Full-AI Run was saved but could not be dispatched",
                details={"full_ai_run_id": saved["id"]},
            ) from exc

    async def get_run(
        self, context: WorkspaceContext, full_ai_run_id: UUID
    ) -> FullAiRunResponse:
        resource = await self.repository.get_full_ai_run(
            context.workspace_id, full_ai_run_id
        )
        return FullAiRunResponse.model_validate(_public_full_ai_resource(resource))

    async def _catalog_blockers(
        self, context: WorkspaceContext
    ) -> list[FullAiBlocker]:
        blockers: list[FullAiBlocker] = []
        try:
            pipeline = await self.repository.get_pipeline_version(
                context.workspace_id, FULL_AI_PIPELINE_VERSION_ID
            )
            if pipeline.get("state") != "published" or pipeline.get("status") != "active":
                raise LookupError
            operations = {
                str(node.get("operation"))
                for node in pipeline.get("nodes", [])
                if isinstance(node, dict) and node.get("required", True)
            }
            capabilities = frozenset(self.configuration.worker_capabilities or ())
            missing_operations = sorted(operations - capabilities)
            if missing_operations:
                blockers.append(
                    FullAiBlocker(
                        code="FULL_AI_PIPELINE_CAPABILITY_UNAVAILABLE",
                        message=(
                            "The worker cannot execute Full-AI operations: "
                            f"{', '.join(missing_operations)}"
                        ),
                        retryable=True,
                    )
                )
            if "media.retrieve" in operations:
                raise LookupError
        except Exception:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_PIPELINE_UNAVAILABLE",
                    message="The dedicated Full-AI PipelineVersion is unavailable",
                    retryable=False,
                )
            )
        try:
            version = await self.repository.get_skill_version(
                context.workspace_id, FULL_AI_SKILL_VERSION_ID
            )
            if version.get("state") != "published":
                raise LookupError
            if version.get("visual_policy", {}).get("generated_media_allowed") is not True:
                raise LookupError
            if version.get("asset_policy", {}).get("library_binding") != "none":
                raise LookupError
        except Exception:
            blockers.append(
                FullAiBlocker(
                    code="FULL_AI_SKILL_UNAVAILABLE",
                    message="The dedicated Full-AI SkillVersion is unavailable",
                    retryable=False,
                )
            )
        return blockers

    def _ready_estimate(
        self, command: FullAiVideoSpec, plan: FullAiPlan
    ) -> FullAiEstimateResponse:
        rate = self.configuration.cost_per_second_minor
        assert rate is not None
        now = self._now().astimezone(UTC)
        bucket_start = int(now.timestamp()) // QUOTE_WINDOW_SECONDS * QUOTE_WINDOW_SECONDS
        expires_at = datetime.fromtimestamp(
            bucket_start + QUOTE_WINDOW_SECONDS, tz=UTC
        )
        amount_minor = plan.billable_seconds * rate
        quote_domain = {
            "spec": command.model_dump(mode="json"),
            "provider": self.configuration.provider_name,
            "model_id": self.configuration.model_id,
            "rate_minor": rate,
            "credit_unit_minor": self.configuration.credit_unit_minor,
            "amount_minor": amount_minor,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "terms_snapshot": {
                "ref": self.configuration.terms_reference,
                "content_hash": self.configuration.terms_content_hash,
                "captured_at": self.configuration.terms_captured_at,
            },
            "pricing_snapshot": {
                "ref": self.configuration.pricing_reference,
                "content_hash": self.configuration.pricing_content_hash,
                "captured_at": self.configuration.pricing_captured_at,
            },
            "output_rights": {
                "confirmed": self.configuration.output_rights_confirmed,
                "license_basis": self.configuration.output_rights_license_basis,
            },
        }
        return FullAiEstimateResponse(
            status="ready",
            request_fingerprint=_fingerprint(quote_domain),
            plan=plan,
            quote=FullAiQuote(
                amount_minor=amount_minor,
                expires_at=expires_at,
            ),
            blockers=[],
        )


def _plan(spec: FullAiVideoSpec) -> FullAiPlan:
    scene_count = math.ceil(spec.duration_seconds / 5)
    candidate_count = scene_count * spec.variants_per_scene
    return FullAiPlan(
        scene_count=scene_count,
        candidate_count=candidate_count,
        billable_seconds=candidate_count * 5,
    )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _production_settings(spec: FullAiVideoSpec) -> Resource:
    width, height = (
        (720, 1280) if spec.aspect_ratio == "9:16" else (1280, 720)
    )
    return {
        "language": "zh-CN",
        "aspect_ratio": spec.aspect_ratio,
        "target_duration_seconds": spec.duration_seconds,
        "visibility": "private",
        "auto_quality_check": True,
        "resolution": {"width": width, "height": height},
        "frame_rate": 30,
        "layout": "full_frame",
        "media_fit": "cover",
        "subtitles": {
            "enabled": True,
            "position": "bottom",
            "size": "medium",
            "max_lines": 2,
        },
        "asset_acquisition": {
            "enabled": False,
            "sources": ["wikimedia", "youtube", "bilibili"],
            "max_assets": 1,
            "copyright_status": "licensed",
            "rights_confirmed": False,
        },
        "sources": {
            "language": "system_default",
            "aspect_ratio": "run_override",
            "target_duration_seconds": "run_override",
            "visibility": "system_default",
            "auto_quality_check": "system_default",
            "layout": "system_default",
            "media_fit": "system_default",
            "frame_rate": "system_default",
            "subtitles": "system_default",
            "asset_acquisition": "system_default",
        },
    }


def _valid_external_snapshot(
    reference: str | None,
    content_hash: str | None,
    captured_at: str | None,
) -> bool:
    if not reference or not content_hash or not captured_at:
        return False
    if re.fullmatch(r"[a-f0-9]{64}", content_hash) is None:
        return False
    parsed = urlsplit(reference)
    if parsed.scheme == "https":
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return False
    elif parsed.scheme != "urn":
        return False
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if captured.tzinfo is None:
        return False
    return captured.astimezone(UTC) <= datetime.now(UTC)


def _public_full_ai_resource(resource: Resource) -> Resource:
    public = {
        key: value
        for key, value in resource.items()
        if key
        not in {
            "underlying_run_id",
            "idempotency_key",
            "request_hash",
            "estimate_fingerprint",
            "plan",
            "created_by",
        }
    }
    if "underlying_run_id" in resource:
        public["project_run_id"] = resource["underlying_run_id"]
    return public
