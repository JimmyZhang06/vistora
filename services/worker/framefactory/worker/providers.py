"""Provider plugin boundaries; implementations are selected only by configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from framefactory.steps import ArtifactRef, StepContext


@dataclass(frozen=True, slots=True)
class ProviderArtifact:
    kind: str
    filename: str
    media_type: str
    data: bytes


class ArtifactStorage(Protocol):
    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef: ...

    def read_bytes(self, artifact: ArtifactRef) -> bytes: ...

    def materialize(self, artifact: ArtifactRef, destination: Path) -> None: ...

    def publish_copy(
        self,
        context: StepContext,
        *,
        kind: str,
        filename: str,
        media_type: str,
        source_bucket: str,
        source_key: str,
        content_hash: str,
        byte_size: int,
    ) -> ArtifactRef: ...

    def read_json(self, artifact: ArtifactRef) -> Mapping[str, Any]: ...


class SpeechProvider(Protocol):
    """Plugin boundary for TTS providers; no vendor identity belongs in the workflow."""

    async def synthesize(self, context: StepContext, script: Mapping[str, Any]) -> ProviderArtifact: ...


class AssetProvider(Protocol):
    """Plugin boundary for licensed acquisition or generation providers."""

    async def select(
        self, context: StepContext, script: Mapping[str, Any]
    ) -> tuple[ProviderArtifact, ...]: ...


class RenderProvider(Protocol):
    """Plugin boundary for deterministic render engines or remote render services."""

    async def render(
        self, context: StepContext, inputs: tuple[ArtifactRef, ...]
    ) -> ProviderArtifact: ...
