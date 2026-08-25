"""Exact-name capability and declarative step registries."""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from framefactory.skills.capabilities import normalize_capability

from .model import RunStep, StepContext, StepResult


class UnknownCapabilityError(LookupError):
    pass


class DuplicateRegistrationError(ValueError):
    pass


class CapabilityRegistry:
    """Identity-free implementations keyed only by contract capability name."""

    def __init__(self, capabilities: Mapping[str, object] | None = None) -> None:
        self._items: dict[str, object] = {}
        self._frozen = False
        for name, implementation in (capabilities or {}).items():
            self.register(name, implementation)

    def register(self, name: str, implementation: object) -> CapabilityRegistry:
        if self._frozen:
            raise RuntimeError("capability registry is frozen")
        key = normalize_capability(name, path="$.capability_registry")
        if key in self._items:
            raise DuplicateRegistrationError(f"capability already registered: {key}")
        if not callable(implementation) and not callable(getattr(implementation, "execute", None)):
            raise TypeError("capability implementation must be callable or expose execute(context)")
        self._items[key] = implementation
        return self

    def resolve(self, name: str) -> object:
        key = normalize_capability(name, path="$.capability_registry")
        try:
            return self._items[key]
        except KeyError as exc:
            raise UnknownCapabilityError(f"capability is not registered: {key}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def freeze(self) -> CapabilityRegistry:
        self._frozen = True
        return self


@dataclass(frozen=True, slots=True)
class DeclarativeRunStep:
    """A step declaration that dispatches through an injected capability registry."""

    step_type: str
    capability: str
    required_inputs: tuple[str, ...] = ()
    required_artifact_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_type", normalize_capability(self.step_type, path="$.step_type"))
        object.__setattr__(self, "capability", normalize_capability(self.capability, path="$.capability"))
        object.__setattr__(self, "required_inputs", tuple(sorted(set(self.required_inputs))))
        object.__setattr__(self, "required_artifact_kinds", tuple(sorted(set(self.required_artifact_kinds))))

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        missing_inputs = tuple(key for key in self.required_inputs if key not in context.input_snapshot)
        if missing_inputs:
            raise ValueError(f"missing immutable step inputs: {', '.join(missing_inputs)}")
        available_kinds = {artifact.kind for artifact in context.input_artifacts}
        missing_artifacts = tuple(kind for kind in self.required_artifact_kinds if kind not in available_kinds)
        if missing_artifacts:
            raise ValueError(f"missing input artifact kinds: {', '.join(missing_artifacts)}")
        if context.capabilities is None or not callable(getattr(context.capabilities, "resolve", None)):
            raise TypeError("step context requires a capability registry")
        implementation = context.capabilities.resolve(self.capability)
        execute = getattr(implementation, "execute", implementation)
        result = execute(context)
        if inspect.isawaitable(result):
            result = await result
        await context.checkpoint()
        if isinstance(result, StepResult):
            return result
        if isinstance(result, Mapping):
            return StepResult(output_summary=result)
        raise TypeError(f"capability {self.capability} returned {type(result).__name__}, expected StepResult")


class StepRegistry:
    def __init__(self, steps: Iterable[RunStep] = ()) -> None:
        self._items: dict[str, RunStep] = {}
        self._frozen = False
        for step in steps:
            self.register(step)

    def register(self, step: RunStep) -> StepRegistry:
        if self._frozen:
            raise RuntimeError("step registry is frozen")
        key = normalize_capability(step.step_type, path="$.step_registry")
        if key in self._items:
            raise DuplicateRegistrationError(f"step already registered: {key}")
        self._items[key] = step
        return self

    def resolve(self, step_type: str) -> RunStep:
        key = normalize_capability(step_type, path="$.step_registry")
        try:
            return self._items[key]
        except KeyError as exc:
            raise UnknownCapabilityError(f"step is not registered: {key}") from exc

    def step_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def freeze(self) -> StepRegistry:
        self._frozen = True
        return self


DEFAULT_STEPS = (
    DeclarativeRunStep("research.collect", "research.collect", required_inputs=("topic",)),
    DeclarativeRunStep("writing.compose", "writing.compose", required_artifact_kinds=("research",)),
    DeclarativeRunStep(
        "writing.compose.webpage",
        "writing.compose.webpage",
        required_inputs=("duration_seconds",),
        required_artifact_kinds=("image", "manifest", "research"),
    ),
    DeclarativeRunStep(
        "web.capture.validate",
        "web.capture.validate",
        required_inputs=(
            "target_url",
            "public_page_confirmed",
            "rights_confirmed",
            "webpage_video_run_id",
        ),
    ),
    DeclarativeRunStep(
        "web.capture.screenshot",
        "web.capture.screenshot",
        required_inputs=(
            "target_url",
            "aspect_ratio",
            "public_page_confirmed",
            "rights_confirmed",
            "webpage_video_run_id",
        ),
        required_artifact_kinds=("manifest",),
    ),
    DeclarativeRunStep(
        "web.materialize",
        "web.materialize",
        required_artifact_kinds=("image", "manifest", "script"),
    ),
    DeclarativeRunStep("audio.synthesize", "audio.synthesize", required_artifact_kinds=("script",)),
    DeclarativeRunStep("media.select", "media.select", required_artifact_kinds=("script",)),
    DeclarativeRunStep("media.inventory", "media.inventory"),
    DeclarativeRunStep("media.retrieve", "media.retrieve", required_artifact_kinds=("script",)),
    DeclarativeRunStep(
        "timeline.align",
        "timeline.align",
        required_artifact_kinds=(
            "script",
            "audio",
            "narration_timing",
            "candidate_manifest",
            "asset",
        ),
    ),
    DeclarativeRunStep(
        "render.compose", "render.compose", required_artifact_kinds=("script", "audio", "manifest")
    ),
    DeclarativeRunStep(
        "render.edl",
        "render.edl",
        required_artifact_kinds=(
            "audio",
            "narration_timing",
            "candidate_manifest",
            "material_selection",
            "timeline",
            "asset",
        ),
    ),
    DeclarativeRunStep("quality.evaluate", "quality.evaluate", required_artifact_kinds=("video",)),
)


def default_step_registry() -> StepRegistry:
    """Return the legacy and timing-driven operations used by production graphs."""

    return StepRegistry(DEFAULT_STEPS).freeze()


# Short name retained for runtime context factories and integrations.
default_registry = default_step_registry

