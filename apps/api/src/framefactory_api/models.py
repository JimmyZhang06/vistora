from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextResponse(StrictModel):
    mode: str = "single_workspace"
    user_id: UUID
    workspace_id: UUID
    workspace_name: str
    workspace_header_required: bool = False
    multi_tenant_features_enabled: bool = False


class HealthResponse(StrictModel):
    status: str
    service: str
    persistence: str


class SkillIdentityInput(StrictModel):
    workspace_id: UUID | None = None
    publisher_name: str = Field(default="My Vistora", min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    description: str = Field(default="", max_length=4000)
    visibility: Literal["private", "workspace"] = "private"


class SkillVersionCreate(StrictModel):
    workspace_id: UUID | None = None
    skill_id: UUID
    version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    source_version_id: UUID | None = None
    input_schema: dict[str, Any] | None = None
    research_policy: dict[str, Any] | None = None
    writing_policy: dict[str, Any] | None = None
    visual_policy: dict[str, Any] | None = None
    asset_policy: dict[str, Any] | None = None
    qc_policy: dict[str, Any] | None = None
    capability_requirements: list[dict[str, Any]] | None = None
    output_contract: dict[str, Any] | None = None
    default_pipeline_version_id: UUID | None = None
    test_topics: list[str] = Field(default_factory=list, max_length=100)
    release_notes: str = Field(default="", max_length=10000)

    @model_validator(mode="after")
    def require_source_or_complete_spec(self) -> SkillVersionCreate:
        spec = (
            self.input_schema,
            self.research_policy,
            self.writing_policy,
            self.visual_policy,
            self.asset_policy,
            self.qc_policy,
            self.capability_requirements,
            self.output_contract,
        )
        supplied = sum(value is not None for value in spec)
        if self.source_version_id is None and supplied != len(spec):
            raise ValueError("all declarative policy fields are required without source_version_id")
        if self.source_version_id is not None and supplied not in {0, len(spec)}:
            raise ValueError(
                "provide either no policy fields or every policy field with source_version_id"
            )
        return self


class SkillVersionPatch(StrictModel):
    input_schema: dict[str, Any] | None = None
    research_policy: dict[str, Any] | None = None
    writing_policy: dict[str, Any] | None = None
    visual_policy: dict[str, Any] | None = None
    asset_policy: dict[str, Any] | None = None
    qc_policy: dict[str, Any] | None = None
    capability_requirements: list[dict[str, Any]] | None = None
    output_contract: dict[str, Any] | None = None
    default_pipeline_version_id: UUID | None = None
    test_topics: list[str] | None = Field(default=None, max_length=100)
    release_notes: str | None = Field(default=None, max_length=10000)


class PublishSkillVersionRequest(StrictModel):
    release_notes: str | None = Field(default=None, max_length=10000)


class Money(StrictModel):
    amount: float = Field(ge=0)
    currency: Literal["CNY", "USD"]


class ValidationCheck(StrictModel):
    id: str
    label: str
    passed: bool
    severity: Literal["error", "warning", "info"]
    message: str
    path: str | None = None


class ValidationReport(StrictModel):
    skill_id: UUID
    version_id: UUID
    ready: bool
    revision: int = Field(ge=1)
    checks: list[ValidationCheck]
    estimated_cost: Money


class SkillTestExecutionCreate(StrictModel):
    workspace_id: UUID | None = None
    skill_id: UUID
    left_version_id: UUID
    right_version_id: UUID
    topic: str = Field(min_length=1, max_length=1000)
    inputs: dict[str, Any] = Field(default_factory=dict)


class RunCompositionInput(StrictModel):
    skill_version_id: UUID
    pipeline_version_id: UUID
    asset_library_ids: list[UUID] = Field(default_factory=list, max_length=100)
    voice_profile_id: UUID | None = None
    render_preset_version_id: UUID | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def require_unique_asset_libraries(self) -> RunCompositionInput:
        if len(set(self.asset_library_ids)) != len(self.asset_library_ids):
            raise ValueError("asset_library_ids must be unique")
        return self


class ChannelDefaultComposition(StrictModel):
    skill_version_id: UUID
    pipeline_version_id: UUID
    asset_library_ids: list[UUID] = Field(max_length=100)
    voice_profile_id: UUID | None
    render_preset_version_id: UUID | None

    @model_validator(mode="after")
    def require_unique_asset_libraries(self) -> ChannelDefaultComposition:
        if len(set(self.asset_library_ids)) != len(self.asset_library_ids):
            raise ValueError("asset_library_ids must be unique")
        return self


class ChannelWrite(StrictModel):
    workspace_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=80,
    )
    description: str = Field(default="", max_length=4000)
    platform: str | None = Field(default=None, min_length=1, max_length=80)
    handle: str | None = Field(default=None, min_length=1, max_length=300)
    brand_profile: dict[str, Any] = Field(default_factory=dict)
    platform_connection_id: UUID | None = None
    status: Literal["active", "paused", "archived"] = "active"
    default_composition: ChannelDefaultComposition

    @model_validator(mode="after")
    def validate_platform_shape_and_secret_boundary(self) -> ChannelWrite:
        has_platform_binding = self.handle is not None or self.platform_connection_id is not None
        if has_platform_binding and self.platform is None:
            raise ValueError("platform is required with handle or platform_connection_id")

        forbidden_fragments = ("secret", "token", "password", "credential", "api_key", "apikey")

        def visit(value: Any, path: str) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    normalized = str(key).lower().replace("-", "_")
                    if any(fragment in normalized for fragment in forbidden_fragments):
                        raise ValueError(
                            "brand_profile must not contain platform credentials "
                            f"({path}.{key})"
                        )
                    visit(nested, f"{path}.{key}")
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    visit(nested, f"{path}[{index}]")

        visit(self.brand_profile, "brand_profile")
        return self


class SubtitleSettingsInput(StrictModel):
    enabled: bool | None = None
    position: Literal["bottom", "lower_third"] | None = None
    size: Literal["small", "medium", "large"] | None = None
    max_lines: int | None = Field(default=None, ge=1, le=3)


class AssetAcquisitionSettingsInput(StrictModel):
    enabled: bool = False
    sources: list[Literal["youtube", "bilibili", "wikimedia"]] = Field(
        default_factory=lambda: ["wikimedia", "youtube", "bilibili"],
        min_length=1,
        max_length=3,
    )
    max_assets: int = Field(default=3, ge=1, le=6)
    copyright_status: Literal["licensed", "public_domain"] = "licensed"
    rights_confirmed: bool = False

    @model_validator(mode="after")
    def validate_automatic_rights(self) -> AssetAcquisitionSettingsInput:
        if self.enabled and not self.rights_confirmed:
            raise ValueError("automatic asset acquisition requires rights confirmation")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("automatic asset acquisition sources must be unique")
        return self


class VideoSettingsInput(StrictModel):
    """Per-Run overrides; omitted values are resolved from durable defaults."""

    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3"] | None = None
    target_duration_seconds: int | None = Field(default=None, ge=15, le=3600)
    visibility: Literal["private", "workspace"] | None = None
    auto_quality_check: bool | None = None
    layout: Literal["full_frame", "editorial"] | None = None
    media_fit: Literal["cover", "contain"] | None = None
    frame_rate: Literal[24, 25, 30, 50, 60] | None = None
    subtitles: SubtitleSettingsInput | None = None
    asset_acquisition: AssetAcquisitionSettingsInput | None = None


class RunCreate(StrictModel):
    workspace_id: UUID | None = None
    channel_id: UUID | None = None
    input: dict[str, Any]
    composition: RunCompositionInput | None = None
    video_settings: VideoSettingsInput | None = None

    @model_validator(mode="after")
    def require_composition_or_channel(self) -> RunCreate:
        if self.composition is None and self.channel_id is None:
            raise ValueError("composition is required when channel_id is omitted")
        return self


class BatchItemCreate(StrictModel):
    topic: str = Field(min_length=1, max_length=1000)
    inputs: dict[str, Any] = Field(default_factory=dict)


class GenerationBatchCreate(StrictModel):
    workspace_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    channel_id: UUID | None = None
    items: list[BatchItemCreate] = Field(min_length=1, max_length=5000)
    composition: RunCompositionInput
    video_settings: VideoSettingsInput | None = None
    research_mode: Literal["off", "when_missing", "required"] = "when_missing"


class GenerationBatchRetryFailed(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)


class AssetLibraryCreate(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    description: str = Field(default="", max_length=2000)


class AssetUploadCreate(StrictModel):
    library_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    kind: Literal["image", "video"]
    content_type: str = Field(pattern=r"^(?:image|video)/[A-Za-z0-9.+-]+$", max_length=120)
    # User-owned local uploads may be long-form masters. Remote acquisition keeps
    # its separate 500 MiB safety ceiling in RemoteAssetGateway.
    byte_size: int = Field(gt=0, le=5_368_709_120)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    copyright_status: Literal["owned", "licensed", "public_domain"]
    tags: list[str] = Field(default_factory=list, max_length=64)
    source: dict[str, str | float | bool | None] | None = None

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_media_kind(self) -> AssetUploadCreate:
        if not self.content_type.startswith(f"{self.kind}/"):
            raise ValueError("content_type must match the declared media kind")
        if "/" in self.filename or "\\" in self.filename or "\x00" in self.filename:
            raise ValueError("filename must not contain a path")
        cleaned_tags = [value.strip() for value in self.tags]
        if any(not value or len(value) > 100 for value in cleaned_tags):
            raise ValueError("tags must contain 1-100 visible characters")
        return self


class RemoteAssetImportCreate(StrictModel):
    library_id: UUID
    source_url: str = Field(min_length=12, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    copyright_status: Literal["owned", "licensed", "public_domain"]
    tags: list[str] = Field(default_factory=list, max_length=64)
    rights_confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_tags(self) -> RemoteAssetImportCreate:
        cleaned_tags = [value.strip() for value in self.tags]
        if any(not value or len(value) > 100 for value in cleaned_tags):
            raise ValueError("tags must contain 1-100 visible characters")
        return self


class AssetAcquisitionCreate(StrictModel):
    library_id: UUID
    queries: list[str] = Field(min_length=1, max_length=12)
    sources: list[Literal["youtube", "bilibili", "wikimedia"]] = Field(
        min_length=1, max_length=3
    )
    max_assets: int = Field(default=3, ge=1, le=6)
    copyright_status: Literal["licensed", "public_domain"] = "licensed"
    rights_confirmed: Literal[True]
    run_id: UUID | None = None
    step_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_acquisition(self) -> AssetAcquisitionCreate:
        cleaned = [value.strip() for value in self.queries]
        if any(not value or len(value) > 500 for value in cleaned):
            raise ValueError("queries must contain 1-500 visible characters")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be unique")
        return self


class LibraryBuildJobCreate(StrictModel):
    library_id: UUID
    topic: str = Field(min_length=1, max_length=1000)
    queries: list[str] = Field(default_factory=list, max_length=12)
    sources: list[Literal["youtube", "bilibili", "wikimedia"]] = Field(
        default_factory=lambda: ["wikimedia"],
        min_length=1,
        max_length=3,
    )
    max_assets: int = Field(default=6, ge=1, le=6)
    copyright_status: Literal["licensed", "public_domain"] = "public_domain"
    rights_confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_build_spec(self) -> LibraryBuildJobCreate:
        if not self.topic.strip():
            raise ValueError("topic must contain visible characters")
        cleaned_queries = [value.strip() for value in self.queries]
        if any(not value or len(value) > 500 for value in cleaned_queries):
            raise ValueError("queries must contain 1-500 visible characters")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be unique")
        if self.copyright_status == "public_domain" and any(
            source != "wikimedia" for source in self.sources
        ):
            raise ValueError(
                "public_domain library builds only support the wikimedia source"
            )
        return self


class AssetUploadComplete(StrictModel):
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str = Field(pattern=r"^(?:image|video)/[A-Za-z0-9.+-]+$", max_length=120)

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class AssetMetadataPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    copyright_status: Literal[
        "unknown", "owned", "licensed", "public_domain", "restricted"
    ] | None = None
    rights_confirmed: bool | None = None
    rights_evidence: dict[str, str | float | bool | None] | None = None
    tags: list[str] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def require_evidence_for_asserted_rights(self) -> AssetMetadataPatch:
        if not self.model_fields_set:
            raise ValueError("at least one mutable metadata field is required")
        if self.copyright_status in {"owned", "licensed", "public_domain"}:
            if self.rights_confirmed is not True:
                raise ValueError("rights_confirmed must be true for asserted copyright rights")
            if not self.rights_evidence:
                raise ValueError("rights_evidence is required for asserted copyright rights")
        if self.tags is not None:
            cleaned_tags = [value.strip() for value in self.tags]
            if any(not value or len(value) > 100 for value in cleaned_tags):
                raise ValueError("tags must contain 1-100 visible characters")
        return self


class AssetStatusTransition(StrictModel):
    target_status: Literal["processing", "quarantined", "ready", "disabled"]
    reason: str = Field(min_length=1, max_length=2000)


class AssetReanalysisRequest(StrictModel):
    reason: str = Field(default="manual_reanalysis", min_length=1, max_length=500)


class AssetBatchItem(StrictModel):
    asset_id: UUID
    expected_revision: int = Field(ge=1)


class AssetBatchReview(StrictModel):
    items: list[AssetBatchItem] = Field(min_length=1, max_length=100)
    target_status: Literal["processing", "quarantined", "ready", "disabled"]
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def asset_ids_are_unique(self) -> AssetBatchReview:
        if len({item.asset_id for item in self.items}) != len(self.items):
            raise ValueError("asset_id values must be unique")
        return self


class AssetBatchTags(StrictModel):
    items: list[AssetBatchItem] = Field(min_length=1, max_length=100)
    add: list[str] = Field(default_factory=list, max_length=64)
    remove: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_batch_tags(self) -> AssetBatchTags:
        if len({item.asset_id for item in self.items}) != len(self.items):
            raise ValueError("asset_id values must be unique")
        additions = [value.strip() for value in self.add]
        removals = [value.strip() for value in self.remove]
        if not additions and not removals:
            raise ValueError("at least one tag must be added or removed")
        if any(not value or len(value) > 100 for value in additions + removals):
            raise ValueError("tags must contain 1-100 visible characters")
        if set(additions) & set(removals):
            raise ValueError("the same tag cannot be added and removed")
        return self


class AssetBatchReanalysis(StrictModel):
    items: list[AssetBatchItem] = Field(min_length=1, max_length=100)
    reason: str = Field(default="batch_reanalysis", min_length=1, max_length=500)

    @model_validator(mode="after")
    def asset_ids_are_unique(self) -> AssetBatchReanalysis:
        if len({item.asset_id for item in self.items}) != len(self.items):
            raise ValueError("asset_id values must be unique")
        return self


class StepReviewRequest(StrictModel):
    decision: Literal["approve", "reject", "revise"]
    reason: str | None = Field(default=None, max_length=4000)
    issue_codes: list[
        Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$", max_length=80)]
    ] = Field(default_factory=list, max_length=20)
    expected_revision: int | None = Field(default=None, ge=0)


class ForkSkillRequest(StrictModel):
    source_version_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    publisher_name: str = Field(default="My Vistora", min_length=1, max_length=160)
    visibility: str = Field(default="private", pattern=r"^(private|workspace)$")


class ForkSkillResponse(StrictModel):
    skill: dict[str, Any]
    version: dict[str, Any]


class PageResponse(StrictModel):
    limit: int
    has_more: bool
    next_cursor: str | None


class ResourcePage(StrictModel):
    data: list[dict[str, Any]]
    page: PageResponse


class BatchItemPage(ResourcePage):
    total_count: int = Field(ge=0)
