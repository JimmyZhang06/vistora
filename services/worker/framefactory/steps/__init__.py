"""Declarative, recoverable FrameFactory run steps."""

from .legacy import (
    LegacyAssetAdapter,
    LegacyCallableAdapter,
    LegacyOutput,
    legacy_capability_registry,
)
from .model import (
    ArtifactPublisher,
    ArtifactRef,
    CancellationToken,
    InputSnapshot,
    NullCancellationToken,
    RunStep,
    StepContext,
    StepResult,
)
from .registry import (
    CapabilityRegistry,
    DeclarativeRunStep,
    DuplicateRegistrationError,
    StepRegistry,
    UnknownCapabilityError,
    default_registry,
    default_step_registry,
)

__all__ = [
    "ArtifactPublisher",
    "ArtifactRef",
    "CancellationToken",
    "CapabilityRegistry",
    "DeclarativeRunStep",
    "DuplicateRegistrationError",
    "InputSnapshot",
    "LegacyAssetAdapter",
    "LegacyCallableAdapter",
    "LegacyOutput",
    "NullCancellationToken",
    "RunStep",
    "StepContext",
    "StepRegistry",
    "StepResult",
    "UnknownCapabilityError",
    "default_registry",
    "default_step_registry",
    "legacy_capability_registry",
]
