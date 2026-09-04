from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from .context import WorkspaceContext
from .errors import ApiError, ConflictError
from .models import StrictModel
from .queue import JobQueue, QueueError
from .repository import Resource

CONTRACT_VERSION = "1.0.0"
WEBPAGE_VIDEO_PIPELINE_SLUG = "webpage-video-production"
WEBPAGE_VIDEO_PIPELINE_VERSION = 1
WEBPAGE_VIDEO_PIPELINE_VERSION_ID = UUID("eaf69761-8571-5404-a50a-8ef5c0aa90d3")
WEBPAGE_VIDEO_SKILL_VERSION_ID = UUID("cbfb37f7-3f10-5678-97a4-448c72db2980")
WEBPAGE_VIDEO_SITE_PIPELINE_VERSION_ID = UUID("2110e922-329d-565f-ba7a-3616c9d5070b")
WEBPAGE_VIDEO_SITE_SKILL_VERSION_ID = UUID("fd8b8458-2fb6-51e2-b8c0-64bb5f05b707")
SCREENSHOT_STEP_KEY = "screenshot"
SCREENSHOT_OPERATION = "web.capture.screenshot"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_VIEWPORTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
}
_EXPECTED_GRAPH = (
    ("validate", "web.capture.validate", (), False),
    ("screenshot", SCREENSHOT_OPERATION, ("validate",), True),
    ("write", "writing.compose.webpage", ("screenshot",), False),
    ("materialize", "web.materialize", ("screenshot", "write"), False),
    ("tts", "audio.synthesize", ("write",), False),
    ("render", "render.compose", ("write", "tts", "materialize"), False),
    ("quality", "quality.evaluate", ("render",), False),
)
_EXPECTED_SITE_GRAPH = (
    ("discover", "web.site.discover", (), True),
    ("capture_pages", "web.page.capture_batch", ("discover",), False),
    ("analyze_regions", "web.region.analyze", ("capture_pages",), False),
    (
        "storyboard",
        "web.storyboard.plan",
        ("capture_pages", "analyze_regions"),
        True,
    ),
    ("write", "writing.compose.webpage_story", ("capture_pages", "storyboard"), False),
    (
        "materialize",
        "web.materialize.regions",
        ("capture_pages", "storyboard", "write"),
        False,
    ),
    ("tts", "audio.synthesize", ("write",), False),
    ("render", "render.compose", ("write", "materialize", "tts"), False),
    ("quality", "quality.evaluate", ("render",), False),
)
_REQUIRED_CAPABILITIES = frozenset(item[1] for item in _EXPECTED_SITE_GRAPH)
MAX_ACTIVE_RUNS_PER_WORKSPACE = 4

AspectRatio = Literal["16:9", "9:16", "1:1", "4:3"]
DurationSeconds = Literal[15, 30, 45, 60]
ReviewDecision = Literal["approve", "recapture", "reject"]
SiteReviewDecision = Literal["approve", "request_changes", "reject"]
SiteManifestKind = Literal["scope", "storyboard"]
PilotOutcome = Literal["evaluating", "adopted", "rejected"]
StableSiteId = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
]
RunStatus = Literal[
    "queued",
    "validating",
    "capturing",
    "discovering",
    "awaiting_scope_review",
    "capturing_pages",
    "analyzing_regions",
    "awaiting_storyboard_review",
    "awaiting_capture_review",
    "composing",
    "rendering",
    "quality_check",
    "quality_review_required",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
CaptureStatus = Literal[
    "pending",
    "capturing",
    "awaiting_review",
    "approved",
    "changes_requested",
    "rejected",
    "failed",
    "cancelled",
]


class WebpageVideoBlocker(StrictModel):
    code: str
    message: str
    retryable: bool


class WebpageVideoPipelineOption(StrictModel):
    slug: Literal["webpage-video-production"] = WEBPAGE_VIDEO_PIPELINE_SLUG
    version: Literal[2] = 2


class WebpageViewportOption(StrictModel):
    aspect_ratio: AspectRatio
    width: int
    height: int


class WebpageVideoLimits(StrictModel):
    capture_modes: list[str] = Field(default_factory=lambda: ["viewport"])
    full_page: list[bool] = Field(default_factory=lambda: [False])
    viewports: list[WebpageViewportOption] = Field(
        default_factory=lambda: [
            WebpageViewportOption(aspect_ratio="16:9", width=1920, height=1080),
            WebpageViewportOption(aspect_ratio="9:16", width=1080, height=1920),
            WebpageViewportOption(aspect_ratio="1:1", width=1080, height=1080),
            WebpageViewportOption(aspect_ratio="4:3", width=1440, height=1080),
        ]
    )
    aspect_ratios: list[str] = Field(default_factory=lambda: ["16:9", "9:16", "1:1", "4:3"])
    duration_seconds: list[int] = Field(default_factory=lambda: [15, 30, 45, 60])
    topic_max_length: Literal[1600] = 1600
    url_max_length: Literal[2048] = 2048
    voice_profile_id_optional: Literal[True] = True
    max_active_runs_per_workspace: Literal[4] = MAX_ACTIVE_RUNS_PER_WORKSPACE
    crawl_max_pages_default: Literal[8] = 8
    crawl_max_pages_limit: Literal[12] = 12
    crawl_max_depth_default: Literal[1] = 1
    crawl_max_depth_limit: Literal[2] = 2


class WebpageSubtitleOptions(StrictModel):
    supported: Literal[True] = True
    default_enabled: Literal[True] = True


class WebpageVideoOptionsResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    status: Literal["ready", "blocked"]
    pipeline: WebpageVideoPipelineOption = Field(default_factory=WebpageVideoPipelineOption)
    limits: WebpageVideoLimits = Field(default_factory=WebpageVideoLimits)
    voice_profiles: list[Resource] = Field(default_factory=list)
    subtitles: WebpageSubtitleOptions = Field(default_factory=WebpageSubtitleOptions)
    blockers: list[WebpageVideoBlocker]


class WebpageCaptureSpec(StrictModel):
    mode: Literal["viewport"] = "viewport"
    aspect_ratio: AspectRatio
    full_page: Literal[False] = False


class WebpageVideoSpec(StrictModel):
    topic: str = Field(min_length=1, max_length=1600)
    duration_seconds: DurationSeconds
    subtitles_enabled: bool
    voice_profile_id: UUID | None

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("topic must contain visible text")
        return normalized


class WebpageRightsSpec(StrictModel):
    public_page_confirmed: Literal[True]
    rights_confirmed: Literal[True]


class WebpageCrawlSpec(StrictModel):
    """Bounded, fail-closed crawl policy for webpage-video v2."""

    max_pages: int = Field(default=8, ge=1, le=12)
    max_depth: int = Field(default=1, ge=0, le=2)
    same_origin_only: Literal[True] = True
    include_sitemap: bool = True


class WebpageVideoRunCreate(StrictModel):
    target_url: str = Field(min_length=9, max_length=2048)
    capture: WebpageCaptureSpec
    video: WebpageVideoSpec
    rights: WebpageRightsSpec
    crawl: WebpageCrawlSpec | None = None

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        requested = value.strip()
        _normalize_public_https_url(requested)
        return requested


class WebpageBoundingBox(StrictModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class WebpageRegionResponse(StrictModel):
    id: StableSiteId
    page_id: StableSiteId
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=240)
    reason: str = Field(default="", max_length=1000)
    score: float = Field(ge=0, le=1)
    bounding_box: WebpageBoundingBox
    artifact: WebpageArtifactResponse | None = None


class WebpagePageResponse(StrictModel):
    id: StableSiteId
    url: str = Field(min_length=9, max_length=2048)
    canonical_url: str | None = Field(default=None, max_length=2048)
    title: str = Field(default="", max_length=500)
    page_type: str = Field(default="page", min_length=1, max_length=80)
    depth: int = Field(ge=0, le=2)
    score: float = Field(ge=0, le=1)
    selected: bool
    selection_reason: str = Field(default="", max_length=1000)
    screenshot: WebpageArtifactResponse | None = None
    regions: list[WebpageRegionResponse] = Field(default_factory=list, max_length=12)

    @field_validator("url", "canonical_url")
    @classmethod
    def validate_page_url(cls, value: str | None) -> str | None:
        if value is not None:
            _normalize_public_https_url(value)
        return value


class WebpageShotResponse(StrictModel):
    id: StableSiteId
    ordinal: int = Field(ge=1, le=64)
    page_id: StableSiteId
    region_id: StableSiteId | None = None
    duration_seconds: float = Field(gt=0, le=60)
    motion: Literal["static", "zoom_in", "zoom_out", "pan"] = "static"
    transition: Literal["cut", "fade_black"] = "cut"
    narration_cue: str = Field(default="", max_length=1000)
    artifact: WebpageArtifactResponse


class WebpageScopeContent(StrictModel):
    root_url: str
    discovered_count: int = Field(ge=0)
    selected_count: int = Field(ge=0, le=12)
    pages: list[WebpagePageResponse] = Field(max_length=12)

    @field_validator("root_url")
    @classmethod
    def validate_root_url(cls, value: str) -> str:
        return _normalize_public_https_url(value)

    @model_validator(mode="after")
    def selected_count_matches_pages(self) -> WebpageScopeContent:
        if self.selected_count != sum(page.selected for page in self.pages):
            raise ValueError("selected_count must match selected pages")
        if self.discovered_count < len(self.pages):
            raise ValueError("discovered_count cannot be smaller than pages")
        return self


class WebpageStoryboardContent(StrictModel):
    pages: list[WebpagePageResponse] = Field(max_length=12)
    shots: list[WebpageShotResponse] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def references_are_local(self) -> WebpageStoryboardContent:
        page_ids = {page.id for page in self.pages}
        region_ids = {region.id for page in self.pages for region in page.regions}
        if any(shot.page_id not in page_ids for shot in self.shots):
            raise ValueError("every shot must reference a page in this storyboard")
        if any(
            shot.region_id is not None and shot.region_id not in region_ids
            for shot in self.shots
        ):
            raise ValueError("every shot region must belong to this storyboard")
        if [shot.ordinal for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("shot ordinals must be contiguous and ordered")
        return self


class WebpageSiteManifestReviewResponse(StrictModel):
    decision: SiteReviewDecision
    comment: str | None
    reviewed_revision: int = Field(ge=1)
    reviewed_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    metadata: dict[str, Any] | None = None
    decided_at: datetime


class WebpageSiteManifestResponse(StrictModel):
    kind: SiteManifestKind
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["awaiting_review", "approved", "changes_requested", "rejected"]
    artifact: WebpageArtifactResponse
    content: WebpageScopeContent | WebpageStoryboardContent | None
    review: WebpageSiteManifestReviewResponse | None
    created_at: datetime


class WebpageSiteResponse(StrictModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    webpage_video_run_id: UUID
    crawl: WebpageCrawlSpec
    scope: WebpageSiteManifestResponse | None
    storyboard: WebpageSiteManifestResponse | None


class WebpageSiteReviewRequest(StrictModel):
    decision: SiteReviewDecision
    expected_revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("expected_sha256")
    @classmethod
    def normalize_manifest_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("comment")
    @classmethod
    def normalize_manifest_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WebpageScopeReviewRequest(WebpageSiteReviewRequest):
    selected_page_ids: list[StableSiteId] = Field(min_length=1, max_length=12)

    @field_validator("selected_page_ids")
    @classmethod
    def unique_selected_pages(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("selected_page_ids must not contain duplicates")
        return value


class WebpageStoryboardShotSelection(StrictModel):
    id: StableSiteId
    enabled: bool
    order: int = Field(ge=1, le=64)
    motion: Literal["static", "zoom_in", "zoom_out", "pan"] | None = None
    transition: Literal["cut", "fade_black"] | None = None


class WebpageStoryboardReviewRequest(WebpageSiteReviewRequest):
    shots: list[WebpageStoryboardShotSelection] = Field(min_length=1, max_length=64)

    @field_validator("shots")
    @classmethod
    def validate_shot_selection(
        cls, value: list[WebpageStoryboardShotSelection]
    ) -> list[WebpageStoryboardShotSelection]:
        ids = [shot.id for shot in value]
        orders = [shot.order for shot in value]
        if len(ids) != len(set(ids)):
            raise ValueError("shots must not contain duplicate ids")
        if len(orders) != len(set(orders)):
            raise ValueError("shots must not contain duplicate order values")
        if sorted(orders) != list(range(1, len(value) + 1)):
            raise ValueError("shot order values must be contiguous from 1")
        if not any(shot.enabled for shot in value):
            raise ValueError("at least one storyboard shot must be enabled")
        return value


class WebpageCaptureReviewRequest(StrictModel):
    decision: ReviewDecision
    expected_revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("expected_sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WebpageArtifactResponse(StrictModel):
    id: UUID
    kind: str
    media_type: str
    filename: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    preview_url: str | None = None
    download_url: str | None = None
    url_expires_at: datetime | None = None


class WebpageReviewResponse(StrictModel):
    decision: Literal["approve", "request_changes", "reject"]
    comment: str | None
    issue_codes: list[str]
    reviewed_revision: int = Field(ge=1)
    decided_at: datetime | None


class WebpageCaptureResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    webpage_video_run_id: UUID
    status: CaptureStatus
    revision: int = Field(ge=0)
    attempt_number: int = Field(ge=0)
    attempt_recorded: bool
    requested_url: str
    target_url: str
    final_url: str | None
    viewport: WebpageViewportOption
    artifact: WebpageArtifactResponse | None
    review: WebpageReviewResponse | None
    captured_at: datetime | None
    updated_at: datetime


class WebpageVideoFailure(StrictModel):
    stage: str
    code: str
    message: str
    retryable: bool


class WebpagePilotFeedbackSave(StrictModel):
    customer_segment: str = Field(min_length=1, max_length=120)
    baseline_minutes: int = Field(ge=1, le=10_080)
    assisted_minutes: int = Field(ge=1, le=10_080)
    revision_count: int = Field(ge=0, le=100)
    outcome: PilotOutcome
    satisfaction_score: int | None = Field(default=None, ge=1, le=5)
    willingness_to_pay_hkd: int | None = Field(default=None, ge=0, le=1_000_000)
    notes: str | None = Field(default=None, max_length=2000)
    expected_revision: int = Field(ge=0)

    @field_validator("customer_segment")
    @classmethod
    def normalize_customer_segment(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("customer_segment must not be blank")
        return normalized

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WebpagePilotFeedbackResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    id: UUID
    webpage_video_run_id: UUID
    customer_segment: str
    baseline_minutes: int = Field(ge=1)
    assisted_minutes: int = Field(ge=1)
    saved_minutes: int
    time_reduction_percent: float
    revision_count: int = Field(ge=0)
    outcome: PilotOutcome
    satisfaction_score: int | None = Field(default=None, ge=1, le=5)
    willingness_to_pay_hkd: int | None = Field(default=None, ge=0)
    notes: str | None
    revision: int = Field(ge=1)
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime


class WebpagePilotSummaryItem(StrictModel):
    webpage_video_run_id: UUID
    customer_segment: str
    baseline_minutes: int = Field(ge=1)
    assisted_minutes: int = Field(ge=1)
    saved_minutes: int
    time_reduction_percent: float
    revision_count: int = Field(ge=0)
    outcome: PilotOutcome
    satisfaction_score: int | None = Field(default=None, ge=1, le=5)
    willingness_to_pay_hkd: int | None = Field(default=None, ge=0)
    updated_at: datetime


class WebpagePilotSegmentSummary(StrictModel):
    customer_segment: str
    pilot_count: int = Field(ge=1)
    adopted_count: int = Field(ge=0)
    saved_minutes: int
    average_time_reduction_percent: float


class WebpagePilotSummaryResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    generated_at: datetime
    total_records: int = Field(ge=0)
    included_records: int = Field(ge=0)
    truncated: bool
    recommended_minimum_pilots: int = 3
    pilot_target_met: bool
    adopted_count: int = Field(ge=0)
    evaluating_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    baseline_minutes_total: int = Field(ge=0)
    assisted_minutes_total: int = Field(ge=0)
    saved_minutes_total: int
    time_reduction_percent: float | None
    average_satisfaction_score: float | None
    satisfaction_response_count: int = Field(ge=0)
    average_willingness_to_pay_hkd: float | None
    willingness_to_pay_response_count: int = Field(ge=0)
    segments: list[WebpagePilotSegmentSummary]
    items: list[WebpagePilotSummaryItem]


class WebpageVideoRunResponse(StrictModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    id: UUID
    project_run_id: UUID
    workspace_id: UUID
    status: RunStatus
    revision: int = Field(ge=0)
    requested_url: str
    target_url: str
    final_url: str | None
    spec: WebpageVideoRunCreate
    capture: WebpageCaptureResponse
    final_video: WebpageArtifactResponse | None
    pilot_feedback: WebpagePilotFeedbackResponse | None = None
    failure: WebpageVideoFailure | None
    created_at: datetime
    updated_at: datetime


class WebpageVideoRepository(Protocol):
    async def get_pipeline_version(self, workspace_id: UUID, version_id: UUID) -> Resource: ...

    async def get_skill_version(self, workspace_id: UUID, version_id: UUID) -> Resource: ...

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

    async def list_run_steps(self, workspace_id: UUID, run_id: UUID) -> list[Resource]: ...

    async def list_artifacts(self, workspace_id: UUID, run_id: UUID) -> list[Resource]: ...

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

    async def cancel_run_idempotently(
        self,
        workspace_id: UUID,
        run_id: UUID,
        *,
        operation_key: str,
        request_fingerprint: str,
    ) -> tuple[Resource, bool]: ...


@dataclass(frozen=True, slots=True)
class WebpageVideoRuntimeConfiguration:
    worker_capabilities: tuple[str, ...] | None
    queue_available: bool
    object_storage_available: bool
    public_delivery_available: bool = True

    def blockers(self) -> tuple[WebpageVideoBlocker, ...]:
        blockers: list[WebpageVideoBlocker] = []
        capabilities = frozenset(self.worker_capabilities or ())
        missing = sorted(_REQUIRED_CAPABILITIES - capabilities)
        if missing:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_CAPABILITY_UNAVAILABLE",
                    message=f"The worker is missing capabilities: {', '.join(missing)}",
                    retryable=True,
                )
            )
        if not self.queue_available:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_QUEUE_UNAVAILABLE",
                    message="The durable Run queue is unavailable",
                    retryable=True,
                )
            )
        if not self.object_storage_available:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_OBJECT_STORAGE_UNAVAILABLE",
                    message="Object storage is required for immutable screenshot evidence",
                    retryable=True,
                )
            )
        elif not self.public_delivery_available:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_PUBLIC_STORAGE_ENDPOINT_UNAVAILABLE",
                    message=("Object storage has no browser-reachable HTTPS signing endpoint"),
                    retryable=False,
                )
            )
        return tuple(blockers)


class WebpageVideoControlService:
    def __init__(
        self,
        repository: WebpageVideoRepository,
        configuration: WebpageVideoRuntimeConfiguration,
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

    async def options(self, context: WorkspaceContext) -> WebpageVideoOptionsResponse:
        blockers = list(self.configuration.blockers())
        blockers.extend(await self._catalog_blockers(context))
        active_runs = await self.repository.count_active_webpage_video_runs(context.workspace_id)
        if active_runs >= MAX_ACTIVE_RUNS_PER_WORKSPACE:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_ACTIVE_RUN_LIMIT_REACHED",
                    message=("The workspace has reached its concurrent webpage-video Run limit"),
                    retryable=True,
                )
            )
        return WebpageVideoOptionsResponse(
            status="blocked" if blockers else "ready",
            blockers=blockers,
        )

    async def create_run(
        self,
        context: WorkspaceContext,
        command: WebpageVideoRunCreate,
        idempotency_key: str,
    ) -> tuple[WebpageVideoRunResponse, bool]:
        options = await self.options(context)
        dependency_blockers = [
            blocker
            for blocker in options.blockers
            if blocker.code != "WEBPAGE_VIDEO_ACTIVE_RUN_LIMIT_REACHED"
        ]
        if dependency_blockers:
            raise ApiError(
                503,
                "WEBPAGE_VIDEO_UNAVAILABLE",
                "Webpage-video creation is blocked until every dependency is ready",
                details={
                    "blockers": [item.model_dump(mode="json") for item in dependency_blockers]
                },
            )

        if command.video.voice_profile_id is not None:
            raise ApiError(
                422,
                "WEBPAGE_VIDEO_VOICE_PROFILE_UNAVAILABLE",
                "This MVP currently supports only the system-default voice",
                details={"voice_profile_id": str(command.video.voice_profile_id)},
            )

        timestamp = self._now().astimezone(UTC)
        webpage_video_run_id = uuid4()
        scheduler_run_id = uuid4()
        normalized_url = _normalize_public_https_url(command.target_url)
        site_mode = command.crawl is not None
        pipeline_version_id = (
            WEBPAGE_VIDEO_SITE_PIPELINE_VERSION_ID
            if site_mode
            else WEBPAGE_VIDEO_PIPELINE_VERSION_ID
        )
        skill_version_id = (
            WEBPAGE_VIDEO_SITE_SKILL_VERSION_ID
            if site_mode
            else WEBPAGE_VIDEO_SKILL_VERSION_ID
        )
        try:
            pipeline = await self.repository.get_pipeline_version(
                context.workspace_id, pipeline_version_id
            )
            skill_version = await self.repository.get_skill_version(
                context.workspace_id, skill_version_id
            )
        except Exception as exc:
            if not site_mode:
                raise
            raise ApiError(
                503,
                "WEBPAGE_VIDEO_SITE_PIPELINE_UNAVAILABLE",
                "The multi-page webpage-video Pipeline is unavailable",
                details={"pipeline_version_id": str(pipeline_version_id)},
            ) from exc
        spec = command.model_dump(mode="json")
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(webpage_video_run_id),
            "workspace_id": str(context.workspace_id),
            "underlying_run_id": str(scheduler_run_id),
            "status": "queued",
            "requested_url": command.target_url,
            "normalized_url": normalized_url,
            "spec": spec,
            "revision": 1,
            "idempotency_key": idempotency_key,
            "request_hash": _fingerprint(spec),
            "created_by": str(context.user_id),
            "created_at": _timestamp(timestamp),
            "updated_at": _timestamp(timestamp),
        }
        voice_profile_id = command.video.voice_profile_id
        scheduler_input: Resource = {
            "webpage_video_run_id": str(webpage_video_run_id),
            "target_url": normalized_url,
            "requested_url": command.target_url,
            "topic": command.video.topic,
            "aspect_ratio": command.capture.aspect_ratio,
            "duration_seconds": command.video.duration_seconds,
            "subtitles_enabled": command.video.subtitles_enabled,
            "public_page_confirmed": command.rights.public_page_confirmed,
            "rights_confirmed": command.rights.rights_confirmed,
        }
        if command.crawl is not None:
            scheduler_input.update(command.crawl.model_dump(mode="json"))
        if voice_profile_id is not None:
            scheduler_input["voice_profile_id"] = str(voice_profile_id)
        scheduler_run: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(scheduler_run_id),
            "workspace_id": str(context.workspace_id),
            "ownership_type": "workspace",
            "channel_id": None,
            "status": "queued",
            "idempotency_key": f"webpage-video:{_fingerprint(idempotency_key)[:40]}",
            "input": scheduler_input,
            "composition_snapshot": {
                "skill_version": {
                    "id": skill_version["id"],
                    "content_hash": skill_version["content_hash"],
                },
                "asset_library_ids": [],
                "voice_profile_id": str(voice_profile_id) if voice_profile_id else None,
                "render_preset": None,
                "pipeline_version": {
                    "id": pipeline["id"],
                    "content_hash": pipeline["content_hash"],
                },
                "production_settings": _production_settings(command),
                "capabilities": list(pipeline.get("capability_requirements", [])),
            },
            "created_by": str(context.user_id),
            "created_at": resource["created_at"],
            "started_at": None,
            "finished_at": None,
            "updated_at": resource["updated_at"],
        }
        self.validate_scheduler_run(scheduler_run)
        saved, created = await self.repository.create_webpage_video_run_idempotently(
            resource,
            scheduler_run,
            operation_key=(f"{context.workspace_id}:create_webpage_video_run:{idempotency_key}"),
            request_fingerprint=resource["request_hash"],
            active_run_limit=MAX_ACTIVE_RUNS_PER_WORKSPACE,
        )
        if self.queue is None:  # readiness already guards this; defensive fail-closed
            raise ApiError(
                503,
                "WEBPAGE_VIDEO_DISPATCH_UNAVAILABLE",
                "The webpage-video Run was saved but the durable queue is unavailable",
                details={"webpage_video_run_id": saved["id"]},
            )
        try:
            await self.queue.enqueue(
                queue_name="runs",
                payload={
                    "workspace_id": saved["workspace_id"],
                    "run_id": saved["underlying_run_id"],
                    "webpage_video_run_id": saved["id"],
                },
                deduplication_key=(f"run:{saved['workspace_id']}:{saved['underlying_run_id']}"),
                max_attempts=5,
            )
        except QueueError as exc:
            raise ApiError(
                503,
                "WEBPAGE_VIDEO_DISPATCH_UNAVAILABLE",
                "The webpage-video Run was saved but could not be dispatched",
                details={"webpage_video_run_id": saved["id"]},
            ) from exc
        return await self._run_response(context, saved), created

    async def get_run(
        self, context: WorkspaceContext, webpage_video_run_id: UUID
    ) -> WebpageVideoRunResponse:
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id, webpage_video_run_id
        )
        return await self._run_response(context, resource)

    async def get_capture(
        self, context: WorkspaceContext, webpage_video_run_id: UUID
    ) -> WebpageCaptureResponse:
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id, webpage_video_run_id
        )
        steps, attempts = await self._capture_inputs(context, resource)
        return self._capture_response(resource, steps, attempts)

    async def save_pilot_feedback(
        self,
        context: WorkspaceContext,
        webpage_video_run_id: UUID,
        command: WebpagePilotFeedbackSave,
        idempotency_key: str,
    ) -> tuple[WebpagePilotFeedbackResponse, bool]:
        run = await self.get_run(context, webpage_video_run_id)
        if run.status != "succeeded":
            raise ConflictError(
                "WEBPAGE_PILOT_FEEDBACK_RUN_INCOMPLETE",
                "Pilot outcome evidence can only be saved after a successful Run",
                status=run.status,
            )
        timestamp = self._now()
        resource: Resource = {
            "schema_version": CONTRACT_VERSION,
            "id": str(uuid4()),
            "workspace_id": str(context.workspace_id),
            "webpage_video_run_id": str(webpage_video_run_id),
            **command.model_dump(exclude={"expected_revision"}),
            "revision": 1,
            "created_by": str(context.user_id),
            "created_at": _timestamp(timestamp),
            "updated_at": _timestamp(timestamp),
        }
        saved, created = (
            await self.repository.upsert_webpage_pilot_feedback_idempotently(
                resource,
                expected_revision=command.expected_revision,
                operation_key=(
                    f"{context.workspace_id}:save_webpage_pilot_feedback:"
                    f"{idempotency_key}"
                ),
                request_fingerprint=_fingerprint(
                    {
                        "webpage_video_run_id": str(webpage_video_run_id),
                        **command.model_dump(mode="json"),
                    }
                ),
            )
        )
        return WebpagePilotFeedbackResponse.model_validate(
            _public_pilot_feedback(saved)
        ), created

    async def pilot_summary(
        self, context: WorkspaceContext, *, limit: int = 200
    ) -> WebpagePilotSummaryResponse:
        feedback, total = await self.repository.list_webpage_pilot_feedback(
            context.workspace_id, limit=limit
        )
        public = [_public_pilot_feedback(item) for item in feedback]
        baseline_total = sum(int(item["baseline_minutes"]) for item in public)
        assisted_total = sum(int(item["assisted_minutes"]) for item in public)
        saved_total = baseline_total - assisted_total
        satisfaction = [
            int(item["satisfaction_score"])
            for item in public
            if item.get("satisfaction_score") is not None
        ]
        willingness = [
            int(item["willingness_to_pay_hkd"])
            for item in public
            if item.get("willingness_to_pay_hkd") is not None
        ]
        segment_values: dict[str, list[Resource]] = {}
        for item in public:
            segment_values.setdefault(str(item["customer_segment"]), []).append(item)
        segments = [
            WebpagePilotSegmentSummary(
                customer_segment=name,
                pilot_count=len(items),
                adopted_count=sum(item["outcome"] == "adopted" for item in items),
                saved_minutes=sum(int(item["saved_minutes"]) for item in items),
                average_time_reduction_percent=round(
                    sum(float(item["time_reduction_percent"]) for item in items)
                    / len(items),
                    1,
                ),
            )
            for name, items in sorted(
                segment_values.items(), key=lambda pair: (-len(pair[1]), pair[0])
            )
        ]
        return WebpagePilotSummaryResponse(
            generated_at=self._now(),
            total_records=total,
            included_records=len(public),
            truncated=total > len(public),
            pilot_target_met=total >= 3,
            adopted_count=sum(item["outcome"] == "adopted" for item in public),
            evaluating_count=sum(item["outcome"] == "evaluating" for item in public),
            rejected_count=sum(item["outcome"] == "rejected" for item in public),
            baseline_minutes_total=baseline_total,
            assisted_minutes_total=assisted_total,
            saved_minutes_total=saved_total,
            time_reduction_percent=(
                round(saved_total / baseline_total * 100, 1) if baseline_total else None
            ),
            average_satisfaction_score=(
                round(sum(satisfaction) / len(satisfaction), 1)
                if satisfaction
                else None
            ),
            satisfaction_response_count=len(satisfaction),
            average_willingness_to_pay_hkd=(
                round(sum(willingness) / len(willingness), 1) if willingness else None
            ),
            willingness_to_pay_response_count=len(willingness),
            segments=segments,
            items=[
                WebpagePilotSummaryItem(
                    webpage_video_run_id=item["webpage_video_run_id"],
                    customer_segment=item["customer_segment"],
                    baseline_minutes=item["baseline_minutes"],
                    assisted_minutes=item["assisted_minutes"],
                    saved_minutes=item["saved_minutes"],
                    time_reduction_percent=item["time_reduction_percent"],
                    revision_count=item["revision_count"],
                    outcome=item["outcome"],
                    satisfaction_score=item.get("satisfaction_score"),
                    willingness_to_pay_hkd=item.get("willingness_to_pay_hkd"),
                    updated_at=item["updated_at"],
                )
                for item in public
            ],
        )

    async def get_site(
        self, context: WorkspaceContext, webpage_video_run_id: UUID
    ) -> WebpageSiteResponse:
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id, webpage_video_run_id
        )
        crawl = resource.get("spec", {}).get("crawl")
        if not isinstance(crawl, Mapping):
            raise ConflictError(
                "WEBPAGE_SITE_MODE_NOT_ENABLED",
                "This Run uses the v1 single-page capture workflow",
                webpage_video_run_id=str(webpage_video_run_id),
            )
        steps = await self.repository.list_run_steps(
            context.workspace_id, UUID(resource["underlying_run_id"])
        )
        artifacts = await self.repository.list_artifacts(
            context.workspace_id, UUID(resource["underlying_run_id"])
        )
        return _site_response_from_steps(resource, steps, artifacts)

    async def review_site_manifest(
        self,
        context: WorkspaceContext,
        webpage_video_run_id: UUID,
        kind: SiteManifestKind,
        command: WebpageScopeReviewRequest | WebpageStoryboardReviewRequest,
        idempotency_key: str,
    ) -> WebpageSiteResponse:
        site = await self.get_site(context, webpage_video_run_id)
        current = site.scope if kind == "scope" else site.storyboard
        if current is None:
            raise ConflictError(
                "WEBPAGE_SITE_MANIFEST_NOT_READY",
                f"The {kind} manifest has not been materialized",
                kind=kind,
            )
        review_metadata = _site_review_metadata(kind, current, command)
        if (
            current.review is not None
            and current.review.reviewed_revision == command.expected_revision
            and current.review.reviewed_sha256 == command.expected_sha256
            and current.review.decision == command.decision
            and current.review.comment == command.comment
            and current.review.metadata == review_metadata
        ):
            return site
        if (
            current.revision != command.expected_revision
            or current.sha256 != command.expected_sha256
        ):
            raise ConflictError(
                "WEBPAGE_SITE_MANIFEST_CHANGED",
                f"The {kind} manifest changed after the review was loaded",
                expected_revision=command.expected_revision,
                current_revision=current.revision,
                expected_sha256=command.expected_sha256,
                current_sha256=current.sha256,
            )
        if current.review is not None:
            raise ConflictError(
                "WEBPAGE_SITE_MANIFEST_ALREADY_REVIEWED",
                f"The {kind} manifest already has an immutable review decision",
                decision=current.review.decision,
            )
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id,
            webpage_video_run_id,
        )
        steps = await self.repository.list_run_steps(
            context.workspace_id, UUID(resource["underlying_run_id"])
        )
        step = _site_review_step(steps, kind)
        if step is None:
            raise ConflictError(
                "WEBPAGE_SITE_MANIFEST_NOT_READY",
                f"The {kind} review step has not been materialized",
                kind=kind,
            )
        await self.repository.review_run_step_idempotently(
            context.workspace_id,
            str(step["id"]),
            decision=(
                "revise" if command.decision == "request_changes" else command.decision
            ),
            actor_id=context.user_id,
            comment=command.comment,
            issue_codes=(
                []
                if command.decision == "approve"
                else [f"webpage.{kind}.{command.decision}"]
            ),
            expected_revision=command.expected_revision,
            operation_key=(
                f"{context.workspace_id}:review_webpage_{kind}:{idempotency_key}"
            ),
            request_fingerprint=_fingerprint(
                {
                    "webpage_video_run_id": str(webpage_video_run_id),
                    "kind": kind,
                    **command.model_dump(mode="json"),
                }
            ),
            review_metadata=review_metadata,
        )
        return await self.get_site(context, webpage_video_run_id)

    async def review_capture(
        self,
        context: WorkspaceContext,
        webpage_video_run_id: UUID,
        command: WebpageCaptureReviewRequest,
        idempotency_key: str,
    ) -> WebpageVideoRunResponse:
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id, webpage_video_run_id
        )
        steps, attempts = await self._capture_inputs(context, resource)
        screenshot = _screenshot_step(steps)
        if screenshot is None:
            raise ConflictError(
                "WEBPAGE_CAPTURE_NOT_READY",
                "The screenshot step has not been materialized",
                webpage_video_run_id=str(webpage_video_run_id),
            )
        artifact = _capture_artifact(screenshot)
        if artifact is None:
            raise ConflictError(
                "WEBPAGE_CAPTURE_EVIDENCE_INVALID",
                "The screenshot step does not expose exactly one immutable image artifact",
                step_id=screenshot["id"],
            )
        actual_sha256 = str(artifact["content_hash"])
        if actual_sha256 != command.expected_sha256:
            raise ConflictError(
                "WEBPAGE_CAPTURE_SHA_MISMATCH",
                "The screenshot bytes changed after the review evidence was loaded",
                expected_sha256=command.expected_sha256,
                current_sha256=actual_sha256,
            )
        matching_attempt = _matching_attempt(
            attempts,
            screenshot,
            artifact,
            capture_revision=command.expected_revision,
        )
        if matching_attempt is None:
            raise ConflictError(
                "WEBPAGE_CAPTURE_ATTEMPT_NOT_RECORDED",
                "The immutable capture attempt was not durably recorded",
                step_id=screenshot["id"],
                expected_revision=command.expected_revision,
                expected_sha256=command.expected_sha256,
            )

        current_revision = int(screenshot.get("revision", 0))
        replay = _recorded_review_matches(screenshot, command)
        if command.expected_revision != current_revision and not replay:
            raise ConflictError(
                "WEBPAGE_CAPTURE_STATE_CHANGED",
                "The capture step changed after its review evidence was loaded",
                expected_revision=command.expected_revision,
                current_revision=current_revision,
            )
        if screenshot.get("status") != "awaiting_review" and not replay:
            raise ConflictError(
                "WEBPAGE_CAPTURE_NOT_AWAITING_REVIEW",
                "Only the screenshot step awaiting review can be reviewed",
                status=screenshot.get("status"),
            )
        if replay:
            return await self.get_run(context, webpage_video_run_id)

        scheduler_status = str(resource.get("scheduler_status") or resource.get("status"))
        if resource.get("cancellation_requested_at") is not None or scheduler_status in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ConflictError(
                "WEBPAGE_VIDEO_RUN_NOT_ACTIVE",
                "A capture cannot be reviewed after its Run stopped accepting work",
                scheduler_status=scheduler_status,
                cancellation_requested=(resource.get("cancellation_requested_at") is not None),
            )
        if command.decision == "recapture" and int(screenshot.get("attempt", 0)) >= int(
            screenshot.get("maximum_attempts", 3)
        ):
            raise ConflictError(
                "WEBPAGE_CAPTURE_RECAPTURE_LIMIT_REACHED",
                "The screenshot step has exhausted its configured capture attempts",
                attempt=int(screenshot.get("attempt", 0)),
                maximum_attempts=int(screenshot.get("maximum_attempts", 3)),
            )

        issue_codes = {
            "approve": [],
            "recapture": ["capture.changes_requested"],
            "reject": ["capture.rejected"],
        }[command.decision]
        await self.repository.review_run_step_idempotently(
            context.workspace_id,
            str(screenshot["id"]),
            decision=("revise" if command.decision == "recapture" else command.decision),
            actor_id=context.user_id,
            comment=command.comment,
            issue_codes=issue_codes,
            expected_revision=command.expected_revision,
            operation_key=(f"{context.workspace_id}:review_webpage_capture:{idempotency_key}"),
            request_fingerprint=_fingerprint(
                {
                    "webpage_video_run_id": str(webpage_video_run_id),
                    **command.model_dump(mode="json"),
                }
            ),
        )
        return await self.get_run(context, webpage_video_run_id)

    async def cancel_run(
        self,
        context: WorkspaceContext,
        webpage_video_run_id: UUID,
        idempotency_key: str,
    ) -> WebpageVideoRunResponse:
        resource = await self.repository.get_webpage_video_run(
            context.workspace_id, webpage_video_run_id
        )
        await self.repository.cancel_run_idempotently(
            context.workspace_id,
            UUID(resource["underlying_run_id"]),
            operation_key=(f"{context.workspace_id}:cancel_webpage_video:{idempotency_key}"),
            request_fingerprint=_fingerprint({"webpage_video_run_id": str(webpage_video_run_id)}),
        )
        return await self.get_run(context, webpage_video_run_id)

    async def _capture_inputs(
        self, context: WorkspaceContext, resource: Resource
    ) -> tuple[list[Resource], list[Resource]]:
        run_id = UUID(resource["underlying_run_id"])
        steps = await self.repository.list_run_steps(context.workspace_id, run_id)
        attempts = await self.repository.list_webpage_capture_attempts(
            context.workspace_id, UUID(resource["id"])
        )
        return steps, attempts

    async def _run_response(
        self, context: WorkspaceContext, resource: Resource
    ) -> WebpageVideoRunResponse:
        steps, attempts = await self._capture_inputs(context, resource)
        capture = self._capture_response(resource, steps, attempts)
        status = _run_status(resource, steps)
        final_video = _final_video(steps) if status == "succeeded" else None
        pilot_feedback = await self.repository.get_webpage_pilot_feedback(
            context.workspace_id, UUID(resource["id"])
        )
        updated_at = max(
            [
                _parse_timestamp(resource["updated_at"]),
                *(_parse_timestamp(step["updated_at"]) for step in steps if step.get("updated_at")),
            ]
        )
        public = {
            "schema_version": CONTRACT_VERSION,
            "id": resource["id"],
            "project_run_id": resource["underlying_run_id"],
            "workspace_id": resource["workspace_id"],
            "status": status,
            "revision": capture.revision,
            "requested_url": resource["requested_url"],
            "target_url": resource["normalized_url"],
            "final_url": capture.final_url,
            "spec": resource["spec"],
            "capture": capture,
            "final_video": final_video,
            "pilot_feedback": (
                _public_pilot_feedback(pilot_feedback)
                if pilot_feedback is not None
                else None
            ),
            "failure": _run_failure(status, steps),
            "created_at": resource["created_at"],
            "updated_at": updated_at,
        }
        return WebpageVideoRunResponse.model_validate(public)

    def _capture_response(
        self,
        resource: Resource,
        steps: list[Resource],
        attempts: list[Resource],
    ) -> WebpageCaptureResponse:
        screenshot = _screenshot_step(steps)
        aspect_ratio = resource["spec"]["capture"]["aspect_ratio"]
        width, height = _VIEWPORTS[aspect_ratio]
        now = _parse_timestamp(resource["updated_at"])
        if screenshot is None:
            return WebpageCaptureResponse(
                webpage_video_run_id=UUID(resource["id"]),
                status="pending",
                revision=0,
                attempt_number=0,
                attempt_recorded=False,
                requested_url=resource["requested_url"],
                target_url=resource["normalized_url"],
                final_url=None,
                viewport=WebpageViewportOption(
                    aspect_ratio=aspect_ratio, width=width, height=height
                ),
                artifact=None,
                review=None,
                captured_at=None,
                updated_at=now,
            )

        status = _capture_status(screenshot)
        show_evidence = status in {"awaiting_review", "approved", "rejected"}
        artifact = _capture_artifact(screenshot) if show_evidence else None
        matching_attempt = (
            _matching_attempt(attempts, screenshot, artifact) if artifact is not None else None
        )
        final_url = (
            str(matching_attempt["final_url"])
            if matching_attempt and matching_attempt.get("final_url")
            else _summary_url(screenshot.get("output_summary"), "final_url")
            if show_evidence
            else None
        )
        review = _public_review(screenshot.get("review"))
        return WebpageCaptureResponse(
            webpage_video_run_id=UUID(resource["id"]),
            status=status,
            revision=int(screenshot.get("revision", 0)),
            attempt_number=int(screenshot.get("attempt", 0)),
            attempt_recorded=matching_attempt is not None,
            requested_url=resource["requested_url"],
            target_url=resource["normalized_url"],
            final_url=final_url,
            viewport=WebpageViewportOption(aspect_ratio=aspect_ratio, width=width, height=height),
            artifact=_public_artifact(artifact) if artifact is not None else None,
            review=review,
            captured_at=(
                matching_attempt.get("captured_at")
                if matching_attempt is not None
                else screenshot.get("finished_at")
                if show_evidence
                else None
            ),
            updated_at=_parse_timestamp(screenshot["updated_at"]),
        )

    async def _catalog_blockers(self, context: WorkspaceContext) -> list[WebpageVideoBlocker]:
        blockers: list[WebpageVideoBlocker] = []
        try:
            pipeline = await self.repository.get_pipeline_version(
                context.workspace_id, WEBPAGE_VIDEO_SITE_PIPELINE_VERSION_ID
            )
            if pipeline.get("state") != "published" or pipeline.get("status") != "active":
                raise LookupError
            actual_graph = tuple(
                (
                    str(node.get("key")),
                    str(node.get("operation")),
                    tuple(node.get("depends_on", ())),
                    bool(node.get("review_gate", False)),
                    bool(node.get("required", True)),
                )
                for node in pipeline.get("nodes", [])
                if isinstance(node, Mapping)
            )
            expected_graph = tuple((*node, True) for node in _EXPECTED_SITE_GRAPH)
            if actual_graph != expected_graph:
                raise LookupError
        except Exception:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_PIPELINE_UNAVAILABLE",
                    message="The dedicated webpage-video PipelineVersion is unavailable",
                    retryable=False,
                )
            )
        try:
            version = await self.repository.get_skill_version(
                context.workspace_id, WEBPAGE_VIDEO_SITE_SKILL_VERSION_ID
            )
            if version.get("state") != "published":
                raise LookupError
            if str(version.get("default_pipeline_version_id")) != str(
                WEBPAGE_VIDEO_SITE_PIPELINE_VERSION_ID
            ):
                raise LookupError
        except Exception:
            blockers.append(
                WebpageVideoBlocker(
                    code="WEBPAGE_VIDEO_SKILL_UNAVAILABLE",
                    message="The dedicated webpage-video SkillVersion is unavailable",
                    retryable=False,
                )
            )
        return blockers


def _run_status(resource: Resource, steps: list[Resource]) -> RunStatus:
    scheduler_status = str(resource.get("scheduler_status") or resource.get("status"))
    if scheduler_status in {"succeeded", "failed", "cancelled"}:
        return scheduler_status  # type: ignore[return-value]
    if resource.get("cancellation_requested_at") is not None:
        return "cancelling"
    by_key = {str(step.get("node_key")): step for step in steps}
    site_mode = isinstance(resource.get("spec", {}).get("crawl"), Mapping)
    if site_mode:
        discover = by_key.get("discover")
        capture_pages = by_key.get("capture_pages")
        analyze_regions = by_key.get("analyze_regions")
        storyboard = by_key.get("storyboard")
        # Every downstream node is inserted as ``queued`` when the Run is
        # created, including nodes behind review gates.  Resolve the active
        # gate from upstream to downstream so an inert queued storyboard does
        # not mask a discover step that is actually awaiting scope review.
        if discover and discover.get("status") == "awaiting_review":
            return "awaiting_scope_review"
        if discover and discover.get("status") in {"queued", "running", "retrying"}:
            return "discovering"
        if capture_pages and capture_pages.get("status") in {
            "queued",
            "running",
            "retrying",
        }:
            return "capturing_pages"
        if analyze_regions and analyze_regions.get("status") in {
            "queued",
            "running",
            "retrying",
        }:
            return "analyzing_regions"
        if storyboard and storyboard.get("status") == "awaiting_review":
            return "awaiting_storyboard_review"
        if storyboard and storyboard.get("status") in {"queued", "running", "retrying"}:
            return "analyzing_regions"
    screenshot = by_key.get(SCREENSHOT_STEP_KEY)
    quality = by_key.get("quality")
    render = by_key.get("render")
    validate = by_key.get("validate")
    if screenshot and screenshot.get("status") == "awaiting_review":
        return "awaiting_capture_review"
    if quality and quality.get("status") == "awaiting_review":
        return "quality_review_required"
    if any(step.get("status") == "awaiting_review" for step in steps):
        return "failed"
    if quality and quality.get("status") in {"running", "retrying"}:
        return "quality_check"
    if render and render.get("status") in {"running", "retrying"}:
        return "rendering"
    if site_mode and by_key.get("storyboard", {}).get("status") == "succeeded":
        active_composition = any(
            by_key.get(key, {}).get("status") in {"queued", "running", "retrying"}
            for key in ("write", "materialize", "tts")
        )
        return "composing" if active_composition else "rendering"
    if screenshot and screenshot.get("status") == "succeeded":
        if render and render.get("status") == "succeeded":
            return "quality_check"
        active_composition = any(
            by_key.get(key, {}).get("status") in {"queued", "running", "retrying"}
            for key in ("write", "materialize", "tts")
        )
        return "composing" if active_composition else "rendering"
    if screenshot and screenshot.get("status") in {"running", "retrying"}:
        return "capturing"
    if validate and validate.get("status") in {"running", "retrying", "succeeded"}:
        return "capturing" if validate.get("status") == "succeeded" else "validating"
    return "queued"


def _run_failure(status: RunStatus, steps: list[Resource]) -> WebpageVideoFailure | None:
    if status != "failed":
        return None
    failed = next((step for step in reversed(steps) if step.get("status") == "failed"), None)
    if failed is None:
        unexpected_review = next(
            (
                step
                for step in reversed(steps)
                if step.get("status") == "awaiting_review"
                and step.get("node_key")
                not in {SCREENSHOT_STEP_KEY, "discover", "storyboard", "quality"}
            ),
            None,
        )
        if unexpected_review is not None:
            stage = str(unexpected_review.get("node_key") or "pipeline")
            supported_stages = {
                *(node[0] for node in _EXPECTED_GRAPH),
                "discover",
                "capture_pages",
                "analyze_regions",
                "storyboard",
            }
            safe_stage = stage if stage in supported_stages else "pipeline"
            return WebpageVideoFailure(
                stage=safe_stage,
                code="WEBPAGE_VIDEO_UNEXPECTED_REVIEW_GATE",
                message=(
                    "The Run reached an unsupported review gate and cannot continue "
                    "through the webpage-video workflow."
                ),
                retryable=False,
            )
        return WebpageVideoFailure(
            stage="pipeline",
            code="WEBPAGE_VIDEO_RUN_FAILED",
            message="The webpage-video Run failed before completing its current stage.",
            retryable=False,
        )
    stage = str(failed.get("node_key") or "pipeline")
    supported_stages = {
        *(node[0] for node in _EXPECTED_GRAPH),
        "discover",
        "capture_pages",
        "analyze_regions",
        "storyboard",
    }
    safe_stage = stage if stage in supported_stages else "pipeline"
    error = failed.get("error") if isinstance(failed.get("error"), Mapping) else {}
    raw_code = str(error.get("code") or "WEBPAGE_VIDEO_STAGE_FAILED")
    code = (
        raw_code
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", raw_code)
        else "WEBPAGE_VIDEO_STAGE_FAILED"
    )
    messages = {
        "validate": "The target page did not pass capture validation.",
        "screenshot": "The target page screenshot could not be captured or approved.",
        "discover": "The target site could not be discovered or its page scope was rejected.",
        "capture_pages": "One or more selected pages could not be captured.",
        "analyze_regions": "The captured pages could not be analyzed for key regions.",
        "storyboard": "The multi-page storyboard could not be composed or approved.",
        "write": "The webpage narration could not be composed.",
        "materialize": "The approved screenshot could not be prepared for rendering.",
        "tts": "The narration audio could not be synthesized.",
        "render": "The video could not be rendered.",
        "quality": "The rendered video did not pass quality control.",
        "pipeline": "The webpage-video Run failed before completing its current stage.",
    }
    actionable_messages = {
        "browser_capture_policy_blocked": (
            "The page's main navigation or capture resource budget was blocked by the "
            "public-web safety policy."
        ),
        "browser_no_document": "The site did not return a browser-renderable document.",
        "browser_authentication_required": (
            "The site requires authentication or is blocking automated capture."
        ),
        "browser_origin_rejected": "The site rejected the public capture request.",
        "browser_page_load_incomplete": (
            "The page did not finish its document load before the capture timeout."
        ),
        "browser_page_not_stable": (
            "The page loaded, but its visible content did not become stable before capture."
        ),
        "browser_navigation_failed": (
            "The browser could not reach a renderable page; the task can be retried."
        ),
        "browser_screenshot_failed": (
            "The page became available, but the browser could not produce the screenshot; "
            "the task can be retried."
        ),
        "browser_invalid_capture": "The browser returned an invalid screenshot payload.",
    }
    return WebpageVideoFailure(
        stage=safe_stage,
        code=code,
        message=actionable_messages.get(code, messages[safe_stage]),
        retryable=bool(error.get("retryable", False)),
    )


def _capture_status(step: Resource) -> CaptureStatus:
    status = str(step.get("status"))
    review = step.get("review") if isinstance(step.get("review"), Mapping) else {}
    decision = review.get("decision")
    if status == "awaiting_review":
        return "awaiting_review"
    if status == "succeeded" and decision == "approve":
        return "approved"
    if status == "failed" and decision == "reject":
        return "rejected"
    if status in {"queued", "retrying"} and decision == "request_changes":
        return "changes_requested"
    if status in {"queued", "running", "retrying"}:
        return "capturing"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _screenshot_step(steps: list[Resource]) -> Resource | None:
    matches = [
        step
        for step in steps
        if step.get("node_key") == SCREENSHOT_STEP_KEY
        and step.get("operation") == SCREENSHOT_OPERATION
    ]
    return matches[0] if len(matches) == 1 else None


def _capture_artifact(step: Resource) -> Resource | None:
    artifacts = step.get("output_artifacts")
    if not isinstance(artifacts, list):
        return None
    candidates = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, Mapping)
        and str(artifact.get("kind")) in {"image", "asset", "screenshot"}
        and str(artifact.get("media_type")) in {"image/png", "image/jpeg", "image/webp"}
        and _SHA256_RE.fullmatch(str(artifact.get("content_hash", ""))) is not None
        and artifact.get("id") is not None
    ]
    return dict(candidates[0]) if len(candidates) == 1 else None


def _matching_attempt(
    attempts: list[Resource],
    step: Resource,
    artifact: Mapping[str, Any] | None,
    *,
    capture_revision: int | None = None,
) -> Resource | None:
    if artifact is None:
        return None
    review = step.get("review") if isinstance(step.get("review"), Mapping) else {}
    evidence_revision = (
        capture_revision
        if capture_revision is not None
        else int(review.get("reviewed_revision", step.get("revision", 0)))
    )
    matches = [
        attempt
        for attempt in attempts
        if attempt.get("outcome") == "captured"
        and int(attempt.get("capture_revision", -1)) == evidence_revision
        and int(attempt.get("attempt_number", -1)) == int(step.get("attempt", 0))
        and str(attempt.get("artifact_id")) == str(artifact.get("id"))
        and attempt.get("sha256") == artifact.get("content_hash")
    ]
    return matches[0] if len(matches) == 1 else None


def _recorded_review_matches(step: Resource, command: WebpageCaptureReviewRequest) -> bool:
    review = step.get("review")
    if not isinstance(review, Mapping):
        return False
    expected_decision = "request_changes" if command.decision == "recapture" else command.decision
    expected_issue_codes = {
        "approve": [],
        "recapture": ["capture.changes_requested"],
        "reject": ["capture.rejected"],
    }[command.decision]
    evidence = review.get("evidence")
    return (
        review.get("decision") == expected_decision
        and review.get("comment") == command.comment
        and list(review.get("issue_codes") or []) == expected_issue_codes
        and int(review.get("reviewed_revision", -1)) == command.expected_revision
        and isinstance(evidence, list)
        and any(
            isinstance(item, Mapping) and item.get("content_hash") == command.expected_sha256
            for item in evidence
        )
    )


def _public_artifact(artifact: Mapping[str, Any]) -> Resource:
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "media_type": artifact["media_type"],
        "filename": artifact.get("filename") or "capture.png",
        "byte_size": int(artifact.get("byte_size") or 0),
        "sha256": artifact["content_hash"],
    }


def _site_review_step(
    steps: list[Resource], kind: SiteManifestKind
) -> Resource | None:
    expected = {
        "scope": ("discover", "web.site.discover"),
        "storyboard": ("storyboard", "web.storyboard.plan"),
    }[kind]
    matches = [
        step
        for step in steps
        if step.get("node_key") == expected[0] and step.get("operation") == expected[1]
    ]
    return matches[0] if len(matches) == 1 else None


def _site_review_metadata(
    kind: SiteManifestKind,
    manifest: WebpageSiteManifestResponse,
    command: WebpageScopeReviewRequest | WebpageStoryboardReviewRequest,
) -> Resource:
    if kind == "scope":
        if not isinstance(command, WebpageScopeReviewRequest) or not isinstance(
            manifest.content, WebpageScopeContent
        ):
            raise ConflictError(
                "WEBPAGE_SCOPE_SELECTION_UNAVAILABLE",
                "The scope manifest does not expose a reviewable page list",
            )
        candidate_ids = {page.id for page in manifest.content.pages}
        unexpected = [
            str(page_id)
            for page_id in command.selected_page_ids
            if page_id not in candidate_ids
        ]
        if unexpected:
            raise ConflictError(
                "WEBPAGE_SCOPE_SELECTION_INVALID",
                "selected_page_ids must be a subset of the current scope manifest",
                unexpected_page_ids=unexpected,
            )
        return {
            "schema_version": "2.0.0",
            "kind": "scope_selection",
            "manifest_sha256": manifest.sha256,
            "selected_page_ids": [str(page_id) for page_id in command.selected_page_ids],
        }

    if not isinstance(command, WebpageStoryboardReviewRequest) or not isinstance(
        manifest.content, WebpageStoryboardContent
    ):
        raise ConflictError(
            "WEBPAGE_STORYBOARD_SELECTION_UNAVAILABLE",
            "The storyboard manifest does not expose a reviewable shot list",
        )
    expected_ids = {shot.id for shot in manifest.content.shots}
    submitted_ids = {shot.id for shot in command.shots}
    if submitted_ids != expected_ids:
        raise ConflictError(
            "WEBPAGE_STORYBOARD_SELECTION_INVALID",
            "shots must contain every current storyboard shot exactly once",
            missing_shot_ids=sorted(str(value) for value in expected_ids - submitted_ids),
            unexpected_shot_ids=sorted(str(value) for value in submitted_ids - expected_ids),
        )
    ordered = sorted(command.shots, key=lambda shot: shot.order)
    return {
        "schema_version": "2.0.0",
        "kind": "storyboard_selection",
        "manifest_sha256": manifest.sha256,
        "shots": [
            shot.model_dump(mode="json", exclude_none=True) for shot in ordered
        ],
    }


def _site_manifest_artifact(
    step: Resource, run_artifacts: list[Resource], kind: SiteManifestKind
) -> Resource | None:
    step_artifacts = step.get("output_artifacts")
    step_artifacts = step_artifacts if isinstance(step_artifacts, list) else []
    summary = step.get("output_summary")
    summary = summary if isinstance(summary, Mapping) else {}
    expected_id = (
        summary.get("manifest_artifact_id")
        or summary.get(f"{kind}_manifest_artifact_id")
        or summary.get("site_manifest_artifact_id")
        or summary.get("storyboard_manifest_artifact_id")
    )
    expected_hash = (
        summary.get("manifest_hash")
        or summary.get("manifest_sha256")
        or summary.get(f"{kind}_manifest_sha256")
        or summary.get("site_manifest_sha256")
        or summary.get("storyboard_manifest_sha256")
        or summary.get("sha256")
    )
    # A queued review step has neither outputs nor an expected artifact
    # identity.  Do not let an unrelated JSON artifact from an earlier step
    # (for example the scope manifest) masquerade as this step's manifest.
    if not step_artifacts and expected_id is None and expected_hash is None:
        return None
    available = [*step_artifacts, *run_artifacts]
    candidates = [
        dict(artifact)
        for artifact in available
        if isinstance(artifact, Mapping)
        and artifact.get("id") is not None
        and str(artifact.get("media_type")) == "application/json"
        and _SHA256_RE.fullmatch(str(artifact.get("content_hash", ""))) is not None
        and (expected_id is None or str(artifact.get("id")) == str(expected_id))
        and (expected_hash is None or artifact.get("content_hash") == expected_hash)
    ]
    unique = {str(candidate["id"]): candidate for candidate in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _site_manifest_review(
    step: Resource, artifact: Mapping[str, Any]
) -> WebpageSiteManifestReviewResponse | None:
    review = step.get("review")
    if not isinstance(review, Mapping):
        return None
    decision = review.get("decision")
    if decision not in {"approve", "request_changes", "reject"}:
        return None
    evidence = review.get("evidence")
    if isinstance(evidence, list) and not any(
        isinstance(item, Mapping)
        and item.get("content_hash") == artifact.get("content_hash")
        for item in evidence
    ):
        return None
    decided_at = review.get("decided_at")
    if decided_at is None:
        return None
    return WebpageSiteManifestReviewResponse(
        decision=decision,
        comment=review.get("comment"),
        reviewed_revision=int(review.get("reviewed_revision", 0)),
        reviewed_sha256=str(artifact["content_hash"]),
        metadata=(
            dict(review["metadata"])
            if isinstance(review.get("metadata"), Mapping)
            else None
        ),
        decided_at=decided_at,
    )


def _site_manifest_content(
    resource: Resource,
    step: Resource,
    kind: SiteManifestKind,
) -> WebpageScopeContent | WebpageStoryboardContent | None:
    summary = step.get("output_summary")
    if not isinstance(summary, Mapping):
        return None
    embedded = summary.get("manifest")
    if isinstance(embedded, Mapping):
        candidate = dict(embedded)
    elif kind == "scope" and isinstance(summary.get("pages"), list):
        pages = summary["pages"]
        candidate = {
            "root_url": summary.get("root_url") or resource["normalized_url"],
            "discovered_count": summary.get("discovered_count", len(pages)),
            "selected_count": summary.get(
                "selected_count",
                sum(
                    bool(page.get("selected"))
                    for page in pages
                    if isinstance(page, Mapping)
                ),
            ),
            "pages": pages,
        }
    elif kind == "storyboard" and isinstance(summary.get("shots"), list):
        candidate = {
            "pages": summary.get("pages", []),
            "shots": summary["shots"],
        }
    else:
        return None
    candidate = _bounded_site_manifest_summary(candidate)
    try:
        model = WebpageScopeContent if kind == "scope" else WebpageStoryboardContent
        parsed = model.model_validate(candidate)
        root = urlsplit(str(resource["normalized_url"]))
        root_origin = (root.scheme, root.hostname, root.port)
        if any(
            (
                urlsplit(page.url).scheme,
                urlsplit(page.url).hostname,
                urlsplit(page.url).port,
            )
            != root_origin
            for page in parsed.pages
        ):
            raise ValueError("site manifest contains a cross-origin page")
        if isinstance(parsed, WebpageScopeContent):
            max_pages = int(resource["spec"]["crawl"]["max_pages"])
            if parsed.selected_count > max_pages:
                raise ValueError("site manifest exceeds the approved crawl page limit")
        return parsed
    except Exception:
        # The immutable artifact remains reviewable even if a legacy Worker did
        # not include the optional inline summary used by the Web UI.
        return None


def _bounded_site_manifest_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """Normalize untrusted inline display text to the public UI contract.

    Review decisions remain bound to the immutable artifact hash.  This helper
    only prevents an otherwise valid manifest from becoming undisplayable when
    page-authored labels exceed the intentionally small public response limits.
    """

    bounded = deepcopy(candidate)
    pages = bounded.get("pages")
    if not isinstance(pages, list):
        return bounded
    for page in pages:
        if not isinstance(page, dict):
            continue
        page["title"] = str(page.get("title") or "")[:500]
        page["selection_reason"] = str(page.get("selection_reason") or "")[:1000]
        regions = page.get("regions")
        if not isinstance(regions, list):
            continue
        for region in regions:
            if not isinstance(region, dict):
                continue
            label = re.sub(r"\s+", " ", str(region.get("label") or "")).strip()
            region["label"] = (label[:240] or str(region.get("kind") or "section")[:240])
            region["reason"] = str(region.get("reason") or "")[:1000]
    return bounded


def _site_manifest_response(
    resource: Resource,
    steps: list[Resource],
    artifacts: list[Resource],
    kind: SiteManifestKind,
) -> WebpageSiteManifestResponse | None:
    step = _site_review_step(steps, kind)
    if step is None:
        return None
    artifact = _site_manifest_artifact(step, artifacts, kind)
    if artifact is None:
        return None
    review = _site_manifest_review(step, artifact)
    decision = review.decision if review is not None else None
    status = {
        "approve": "approved",
        "request_changes": "changes_requested",
        "reject": "rejected",
    }.get(decision, "awaiting_review")
    return WebpageSiteManifestResponse(
        kind=kind,
        revision=int(step.get("revision", 0)),
        sha256=str(artifact["content_hash"]),
        status=status,
        artifact=WebpageArtifactResponse.model_validate(_public_artifact(artifact)),
        content=_site_manifest_content(resource, step, kind),
        review=review,
        created_at=(
            step.get("finished_at") or step.get("updated_at") or resource["updated_at"]
        ),
    )


def _site_response_from_steps(
    resource: Resource, steps: list[Resource], artifacts: list[Resource]
) -> WebpageSiteResponse:
    return WebpageSiteResponse(
        webpage_video_run_id=UUID(resource["id"]),
        crawl=WebpageCrawlSpec.model_validate(resource["spec"]["crawl"]),
        scope=_site_manifest_response(resource, steps, artifacts, "scope"),
        storyboard=_site_manifest_response(resource, steps, artifacts, "storyboard"),
    )


def _final_video(steps: list[Resource]) -> Resource | None:
    render_steps = [
        step
        for step in steps
        if step.get("node_key") == "render"
        and step.get("operation") == "render.compose"
        and step.get("status") == "succeeded"
    ]
    quality_steps = [
        step
        for step in steps
        if step.get("node_key") == "quality"
        and step.get("operation") == "quality.evaluate"
        and step.get("status") == "succeeded"
        and "render" in step.get("dependencies", [])
    ]
    if len(render_steps) != 1 or len(quality_steps) != 1:
        return None
    artifacts = render_steps[0].get("output_artifacts")
    if not isinstance(artifacts, list):
        return None
    candidates = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, Mapping)
        and artifact.get("kind") == "video"
        and artifact.get("media_type") == "video/mp4"
        and _SHA256_RE.fullmatch(str(artifact.get("content_hash", ""))) is not None
        and artifact.get("id") is not None
    ]
    if len(candidates) != 1:
        return None
    return _public_artifact(candidates[0])


def _public_pilot_feedback(resource: Resource) -> Resource:
    baseline = int(resource["baseline_minutes"])
    assisted = int(resource["assisted_minutes"])
    saved = baseline - assisted
    return {
        "schema_version": CONTRACT_VERSION,
        "id": resource["id"],
        "webpage_video_run_id": resource["webpage_video_run_id"],
        "customer_segment": resource["customer_segment"],
        "baseline_minutes": baseline,
        "assisted_minutes": assisted,
        "saved_minutes": saved,
        "time_reduction_percent": round((saved / baseline) * 100, 1),
        "revision_count": resource["revision_count"],
        "outcome": resource["outcome"],
        "satisfaction_score": resource.get("satisfaction_score"),
        "willingness_to_pay_hkd": resource.get("willingness_to_pay_hkd"),
        "notes": resource.get("notes"),
        "revision": resource["revision"],
        "created_by": resource.get("created_by"),
        "created_at": resource["created_at"],
        "updated_at": resource["updated_at"],
    }


def _public_review(value: Any) -> WebpageReviewResponse | None:
    if not isinstance(value, Mapping) or value.get("decision") not in {
        "approve",
        "request_changes",
        "reject",
    }:
        return None
    return WebpageReviewResponse(
        decision=value["decision"],
        comment=value.get("comment"),
        issue_codes=list(value.get("issue_codes") or []),
        reviewed_revision=int(value.get("reviewed_revision", 0)),
        decided_at=value.get("decided_at"),
    )


def _summary_url(value: Any, key: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    candidate = value.get(key)
    return str(candidate) if candidate else None


def _production_settings(command: WebpageVideoRunCreate) -> Resource:
    width, height = _VIEWPORTS[command.capture.aspect_ratio]
    return {
        "language": "zh-CN",
        "aspect_ratio": command.capture.aspect_ratio,
        "target_duration_seconds": command.video.duration_seconds,
        "visibility": "private",
        "auto_quality_check": True,
        "resolution": {"width": width, "height": height},
        "frame_rate": 30,
        "layout": "full_frame",
        "media_fit": "contain",
        "subtitles": {
            "enabled": command.video.subtitles_enabled,
            "position": "bottom",
            "size": "medium",
            "max_lines": 2,
        },
        "asset_acquisition": {
            "enabled": False,
            "sources": ["wikimedia"],
            "max_assets": 1,
            "copyright_status": "licensed",
            "rights_confirmed": command.rights.rights_confirmed,
        },
        "no_asset_draft": {
            "enabled": False,
            "mode": "procedural_cards",
            "draft": True,
            "replacement_required": True,
        },
        "sources": {
            "language": "system_default",
            "aspect_ratio": "run_override",
            "target_duration_seconds": "run_override",
            "visibility": "system_default",
            "auto_quality_check": "system_default",
            "layout": "system_default",
            "media_fit": "run_override",
            "frame_rate": "system_default",
            "subtitles": "run_override",
            "asset_acquisition": "system_default",
            "no_asset_draft": "system_default",
        },
    }


def _normalize_public_https_url(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("target_url contains control characters")
    if "\\" in value:
        raise ValueError("target_url cannot contain backslashes")
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https":
        raise ValueError("target_url must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("target_url must be an unauthenticated public URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("target_url contains an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError("target_url may use only the standard HTTPS port")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname or hostname.endswith(
            (".localhost", ".local", ".internal", ".home", ".lan")
        ):
            raise ValueError("target_url hostname is not a public DNS name") from None
        if re.fullmatch(r"[0-9a-fx.]+", hostname, flags=re.IGNORECASE):
            raise ValueError("target_url uses an ambiguous numeric hostname") from None
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("target_url hostname is invalid") from exc
    else:
        if not address.is_global:
            raise ValueError("target_url IP address must be globally routable")
        if address.version == 6:
            hostname = f"[{hostname}]"
    path = parsed.path or "/"
    normalized = urlunsplit(("https", hostname, path, parsed.query, ""))
    if len(normalized) > 2048:
        raise ValueError("normalized target_url exceeds 2048 characters")
    return normalized


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
