"""Fail-honest capability wiring until providers are explicitly configured."""

from __future__ import annotations

from framefactory.runtime import CapabilityUnavailable
from framefactory.steps import (
    CapabilityRegistry,
    DeclarativeRunStep,
    StepContext,
    StepRegistry,
)

from .adapters.database_assets import ControlApiAssetAcquirer, DatabaseAssetCapability
from .adapters.legacy_media import (
    EdgeSpeechCapability,
    FFmpegQualityCapability,
    FFmpegRenderCapability,
    LegacyAssetCapability,
)
from .adapters.openai_compatible import (
    HttpTransport,
    OpenAICompatibleClient,
    OpenAIQualityCapability,
    OpenAIResearchCapability,
    OpenAIWritingCapability,
)
from .config import WorkerSettings
from .providers import ArtifactStorage

PRODUCTION_OPERATIONS = (
    "research.collect",
    "research.verify",
    "writing.compose",
    "audio.synthesize",
    "media.select",
    "media.generate",
    "timeline.align",
    "render.compose",
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


def configured_capabilities(
    settings: WorkerSettings,
    *,
    artifact_storage: ArtifactStorage | None = None,
    transport: HttpTransport | None = None,
) -> CapabilityRegistry:
    """Resolve providers from deployment config, never from account or Skill IDs."""

    implementations: dict[str, object] = {
        name: UnsupportedCapability(name) for name in PRODUCTION_OPERATIONS
    }
    provider = settings.openai_compatible
    if provider is not None:
        if artifact_storage is None:
            raise ValueError("configured text providers require durable artifact storage")

        def client(model: str) -> OpenAICompatibleClient:
            return OpenAICompatibleClient(
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=model,
                timeout_seconds=provider.timeout_seconds,
                transport=transport,
            )

        implementations["research.collect"] = OpenAIResearchCapability(
            client(provider.research_model), artifact_storage
        )
        implementations["writing.compose"] = OpenAIWritingCapability(
            client(provider.writing_model), artifact_storage
        )
        implementations["quality.evaluate"] = OpenAIQualityCapability(
            client(provider.quality_model), artifact_storage
        )
    media = settings.legacy_media
    if media is not None:
        if artifact_storage is None:
            raise ValueError("configured media providers require durable artifact storage")
        implementations["audio.synthesize"] = EdgeSpeechCapability(media, artifact_storage)
        if settings.asset_library is None:
            implementations["media.select"] = LegacyAssetCapability(media, artifact_storage)
        implementations["render.compose"] = FFmpegRenderCapability(media, artifact_storage)
        implementations["quality.evaluate"] = FFmpegQualityCapability(media, artifact_storage)
    asset_library = settings.asset_library
    if asset_library is not None:
        if artifact_storage is None:
            raise ValueError("configured database assets require durable artifact storage")
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
    return CapabilityRegistry(implementations).freeze()


def production_step_registry() -> StepRegistry:
    return StepRegistry(
        DeclarativeRunStep(step_type=name, capability=name)
        for name in PRODUCTION_OPERATIONS
    ).freeze()
