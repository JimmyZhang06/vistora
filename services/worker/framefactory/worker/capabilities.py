"""Fail-honest capability wiring until providers are explicitly configured."""

from __future__ import annotations

import shutil

from framefactory.runtime import CapabilityUnavailable
from framefactory.steps import (
    CapabilityRegistry,
    DeclarativeRunStep,
    StepContext,
    StepRegistry,
)

from .adapters.database_assets import ControlApiAssetAcquirer, DatabaseAssetCapability
from .adapters.edl_media import FFmpegEdlRenderCapability
from .adapters.legacy_media import (
    EdgeSpeechCapability,
    FFmpegQualityCapability,
    FFmpegRenderCapability,
    LegacyAssetCapability,
)
from .adapters.openai_compatible import (
    HttpsJsonResearchSearchGateway,
    HttpTransport,
    OpenAICompatibleClient,
    OpenAIQualityCapability,
    OpenAIResearchCapability,
    OpenAIWritingCapability,
    ResearchSearchGateway,
)
from .config import WorkerSettings
from .generation import (
    GeneratedCreativeWritingCapability,
    RunwayClient,
    RunwayMediaGenerationCapability,
    RunwayTransport,
)
from .generation.ports import (
    GeneratedVideoVerifier as GeneratedVideoVerifierPort,
)
from .generation.ports import (
    PaidOperationLedger,
)
from .generation.verifier import GeneratedVideoVerifier
from .generation.vision import OpenAICompatibleGeneratedVisionAnalyzer
from .providers import ArtifactStorage
from .retrieval import DatabaseInventoryCapability, DatabaseRetrievalCapability
from .timeline_edl import TimelineCapability
from .web_capture import (
    PlaywrightChromiumCaptureAdapter,
    PublicHttpsPolicy,
    PublicHttpsUrlValidator,
    WebCaptureScreenshotCapability,
    WebCaptureValidateCapability,
    WebMaterializeCapability,
    WebPageCaptureBatchCapability,
    WebpageStoryWritingCapability,
    WebpageWritingCapability,
    WebRegionAnalyzeCapability,
    WebRegionsMaterializeCapability,
    WebSiteDiscoverCapability,
    WebStoryboardPlanCapability,
)

PRODUCTION_OPERATIONS = (
    "research.collect",
    "research.verify",
    "writing.compose",
    "writing.compose.generated",
    "writing.compose.webpage",
    "writing.compose.webpage_story",
    "audio.synthesize",
    "media.select",
    "media.inventory",
    "media.retrieve",
    "media.generate",
    "web.capture.validate",
    "web.capture.screenshot",
    "web.site.discover",
    "web.page.capture_batch",
    "web.region.analyze",
    "web.storyboard.plan",
    "web.materialize",
    "web.materialize.regions",
    "timeline.align",
    "render.compose",
    "render.edl",
    "quality.evaluate",
    "review.gate",
    "delivery.package",
)


class UnsupportedCapability:
    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, context: StepContext) -> None:
        await context.checkpoint()
        raise CapabilityUnavailable(
            f"capability '{self.name}' has no configured production provider; "
            "the step was not executed and no artifact was created"
        )


def unavailable_capabilities() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for name in PRODUCTION_OPERATIONS:
        registry.register(name, UnsupportedCapability(name))
    return registry.freeze()


def assert_declared_capabilities_configured(
    registry: CapabilityRegistry,
    settings: WorkerSettings,
) -> None:
    """Fail startup when an API-advertised operation has no real worker.

    The browser process proves only its two isolated operations; the ordinary
    worker proves every other advertised operation.  This makes the shared API
    capability list a deployment assertion instead of silently accepting an
    ``UnsupportedCapability`` placeholder.
    """

    capture_operations = {
        "web.capture.validate",
        "web.capture.screenshot",
        "web.site.discover",
        "web.page.capture_batch",
    }
    executable_operations = set(PRODUCTION_OPERATIONS)
    required = (
        tuple(
            name
            for name in settings.declared_capabilities
            if name in executable_operations and name in capture_operations
        )
        if settings.browser_capture_only
        else tuple(
            name
            for name in settings.declared_capabilities
            if name in executable_operations and name not in capture_operations
        )
    )
    missing: list[str] = []
    for name in required:
        try:
            implementation = registry.resolve(name)
        except LookupError:
            missing.append(name)
            continue
        if isinstance(implementation, UnsupportedCapability):
            missing.append(name)
    if missing:
        raise ValueError(
            "advertised worker capabilities lack configured providers: "
            + ", ".join(sorted(missing))
        )


def configured_capabilities(
    settings: WorkerSettings,
    *,
    artifact_storage: ArtifactStorage | None = None,
    transport: HttpTransport | None = None,
    research_search: ResearchSearchGateway | None = None,
    runway_transport: RunwayTransport | None = None,
    paid_operation_ledger: PaidOperationLedger | None = None,
    generated_video_verifier: GeneratedVideoVerifierPort | None = None,
) -> CapabilityRegistry:
    """Resolve providers from deployment config, never from account or Skill IDs."""

    implementations: dict[str, object] = {
        name: UnsupportedCapability(name) for name in PRODUCTION_OPERATIONS
    }
    if settings.browser_capture_only:
        capture = settings.web_capture
        if capture is None or artifact_storage is None:
            raise ValueError(
                "browser-capture-only worker requires capture config and artifact storage"
            )
        validator = PublicHttpsUrlValidator(
            PublicHttpsPolicy(allow_non_standard_ports=capture.allow_non_standard_ports)
        )
        capture_adapter = PlaywrightChromiumCaptureAdapter(capture, validator)
        implementations["web.capture.validate"] = WebCaptureValidateCapability(
            validator, artifact_storage
        )
        implementations["web.capture.screenshot"] = WebCaptureScreenshotCapability(
            capture_adapter,
            artifact_storage,
            maximum_body_characters=capture.maximum_body_characters,
        )
        implementations["web.site.discover"] = WebSiteDiscoverCapability(
            capture_adapter,
            validator,
            artifact_storage,
            maximum_body_characters=capture.maximum_body_characters,
        )
        implementations["web.page.capture_batch"] = WebPageCaptureBatchCapability(
            capture_adapter,
            validator,
            artifact_storage,
            maximum_body_characters=capture.maximum_body_characters,
        )
        return CapabilityRegistry(implementations).freeze()
    provider = settings.openai_compatible
    resolved_research_search = research_search
    if resolved_research_search is None and settings.research_search is not None:
        search = settings.research_search
        resolved_research_search = HttpsJsonResearchSearchGateway(
            url=search.url,
            bearer_token=search.bearer_token,
            timeout_seconds=search.timeout_seconds,
            maximum_response_bytes=search.maximum_response_bytes,
            transport=transport,
        )
    if provider is not None:
        if artifact_storage is None:
            raise ValueError(
                "configured text providers require durable artifact storage"
            )

        def client(model: str) -> OpenAICompatibleClient:
            return OpenAICompatibleClient(
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=model,
                timeout_seconds=provider.timeout_seconds,
                transport=transport,
            )

        implementations["research.collect"] = OpenAIResearchCapability(
            client(provider.research_model),
            artifact_storage,
            search_gateway=resolved_research_search,
        )
        implementations["writing.compose"] = OpenAIWritingCapability(
            client(provider.writing_model), artifact_storage
        )
        implementations["writing.compose.webpage"] = WebpageWritingCapability(
            client(provider.writing_model), artifact_storage
        )
        implementations["writing.compose.webpage_story"] = (
            WebpageStoryWritingCapability(
                client(provider.writing_model), artifact_storage
            )
        )
        implementations["writing.compose.generated"] = (
            GeneratedCreativeWritingCapability(
                client(provider.writing_model), artifact_storage
            )
        )
        implementations["quality.evaluate"] = OpenAIQualityCapability(
            client(provider.quality_model), artifact_storage
        )
    if artifact_storage is not None:
        implementations["timeline.align"] = TimelineCapability(artifact_storage)
        implementations["web.materialize"] = WebMaterializeCapability(artifact_storage)
        implementations["web.region.analyze"] = WebRegionAnalyzeCapability(
            artifact_storage
        )
        implementations["web.storyboard.plan"] = WebStoryboardPlanCapability(
            artifact_storage
        )
        implementations["web.materialize.regions"] = WebRegionsMaterializeCapability(
            artifact_storage
        )
    media = settings.legacy_media
    if media is not None:
        if artifact_storage is None:
            raise ValueError(
                "configured media providers require durable artifact storage"
            )
        implementations["audio.synthesize"] = EdgeSpeechCapability(
            media, artifact_storage
        )
        if settings.asset_library is None:
            implementations["media.select"] = LegacyAssetCapability(
                media, artifact_storage
            )
        implementations["render.compose"] = FFmpegRenderCapability(
            media, artifact_storage
        )
        implementations["render.edl"] = FFmpegEdlRenderCapability(
            media, artifact_storage
        )
        implementations["quality.evaluate"] = FFmpegQualityCapability(
            media, artifact_storage
        )
    asset_library = settings.asset_library
    if asset_library is not None:
        if artifact_storage is None:
            raise ValueError(
                "configured database assets require durable artifact storage"
            )
        implementations["media.select"] = DatabaseAssetCapability(
            asset_library,
            artifact_storage,
            acquire_assets=(
                ControlApiAssetAcquirer(
                    asset_library.control_api_url,
                    timeout_seconds=asset_library.acquisition_timeout_seconds,
                )
                if asset_library.control_api_url
                else None
            ),
        )
        implementations["media.retrieve"] = DatabaseRetrievalCapability(
            asset_library,
            artifact_storage,
            acquire_assets=(
                ControlApiAssetAcquirer(
                    asset_library.control_api_url,
                    timeout_seconds=asset_library.acquisition_timeout_seconds,
                )
                if asset_library.control_api_url
                else None
            ),
        )
        implementations["media.inventory"] = DatabaseInventoryCapability(
            asset_library,
            artifact_storage,
        )
    runway = settings.runway
    vision = settings.full_ai_vision
    media = settings.legacy_media
    ffprobe_command = media.ffprobe_command if media is not None else "ffprobe"
    ffmpeg_command = media.ffmpeg_command if media is not None else "ffmpeg"
    verification_tools_ready = generated_video_verifier is not None or (
        shutil.which(ffprobe_command) is not None
        and shutil.which(ffmpeg_command) is not None
    )
    if (
        runway is not None
        and vision is not None
        and artifact_storage is not None
        and paid_operation_ledger is not None
        and verification_tools_ready
    ):
        verifier = generated_video_verifier or GeneratedVideoVerifier(
            OpenAICompatibleGeneratedVisionAnalyzer(
                base_url=vision.base_url,
                api_key=vision.api_key,
                model=vision.model,
                timeout_seconds=vision.timeout_seconds,
            ),
            vision_provider="openai-compatible-full-ai-vision",
            vision_model=vision.model,
            ffprobe_command=ffprobe_command,
            ffmpeg_command=ffmpeg_command,
            command_timeout_seconds=vision.timeout_seconds,
        )
        implementations["media.generate"] = RunwayMediaGenerationCapability(
            RunwayClient(
                base_url=runway.base_url,
                api_key=runway.api_key,
                timeout_seconds=runway.timeout_seconds,
                transport=runway_transport,
            ),
            artifact_storage,
            paid_operation_ledger,
            verifier,
            worker_id=settings.worker_id,
        )
    return CapabilityRegistry(implementations).freeze()


def production_step_registry() -> StepRegistry:
    required_inputs = {
        "web.capture.validate": (
            "target_url",
            "public_page_confirmed",
            "rights_confirmed",
            "webpage_video_run_id",
        ),
        "web.capture.screenshot": (
            "target_url",
            "aspect_ratio",
            "public_page_confirmed",
            "rights_confirmed",
            "webpage_video_run_id",
        ),
        "web.site.discover": (
            "target_url",
            "aspect_ratio",
            "public_page_confirmed",
            "rights_confirmed",
            "webpage_video_run_id",
        ),
        "web.page.capture_batch": (
            "aspect_ratio",
            "public_page_confirmed",
            "rights_confirmed",
        ),
        "writing.compose.webpage": ("duration_seconds",),
        "writing.compose.webpage_story": ("duration_seconds",),
    }
    required_artifacts = {
        "audio.synthesize": ("script",),
        "media.generate": ("script", "audio", "narration_timing"),
        "media.retrieve": ("script",),
        "timeline.align": (
            "script",
            "audio",
            "narration_timing",
            "candidate_manifest",
            "asset",
        ),
        "render.edl": (
            "audio",
            "narration_timing",
            "candidate_manifest",
            "material_selection",
            "timeline",
            "asset",
        ),
        "render.compose": ("script", "audio", "narration_timing", "manifest"),
        "web.capture.screenshot": ("manifest",),
        "web.page.capture_batch": ("manifest",),
        "web.region.analyze": ("image", "manifest"),
        "web.storyboard.plan": ("manifest",),
        "writing.compose.webpage": ("image", "manifest", "research"),
        "writing.compose.webpage_story": ("manifest", "research"),
        "web.materialize": ("image", "manifest", "script"),
        "web.materialize.regions": ("image", "manifest", "script"),
    }
    return StepRegistry(
        DeclarativeRunStep(
            step_type=name,
            capability=name,
            required_inputs=required_inputs.get(name, ()),
            required_artifact_kinds=required_artifacts.get(name, ()),
        )
        for name in PRODUCTION_OPERATIONS
    ).freeze()
