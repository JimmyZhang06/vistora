"""Allowlisted compatibility boundary for the pre-worker ``src`` pipeline.

No declarative step imports ``src`` directly.  Deployments may register these
adapters while migrating; new implementations can replace them capability by
capability without changing the pipeline or branching on an account identity.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import ArtifactRef, StepContext, StepResult
from .registry import CapabilityRegistry

_ALLOWED_TARGETS = {
    "agent.steps_script:_facts_draft",
    "agent.steps_script:run",
    "agent.steps_tts:run",
    "agent.steps_briefs:run",
    "agent.steps_match:run",
    "agent.steps_assemble:run",
    "agent.steps_qc:run",
}


@dataclass(frozen=True, slots=True)
class LegacyOutput:
    relative_path: str
    kind: str
    media_type: str

    def __post_init__(self) -> None:
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("legacy output must stay below run_dir")


@contextmanager
def _legacy_import_path(root: Path):
    value = str(root.resolve())
    added = value not in sys.path
    if added:
        sys.path.insert(0, value)
    try:
        yield
    finally:
        if added:
            try:
                sys.path.remove(value)
            except ValueError:
                pass


def _load_target(target: str, legacy_src_root: Path):
    if target not in _ALLOWED_TARGETS:
        raise ValueError(f"legacy target is not allowlisted: {target}")
    module_name, function_name = target.split(":", 1)
    with _legacy_import_path(legacy_src_root):
        module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f"legacy target is not callable: {target}")
    return function


def _legacy_kwargs(context: StepContext, allowed_inputs: tuple[str, ...]) -> dict[str, Any]:
    snapshot = context.input_snapshot.to_dict()
    kwargs = {key: snapshot[key] for key in allowed_inputs if key in snapshot}
    for key, value in tuple(kwargs.items()):
        if value is not None and (key.endswith(("_path", "_dir")) or key == "final"):
            kwargs[key] = Path(value)
    return kwargs


async def _publish_outputs(
    context: StepContext,
    outputs: tuple[LegacyOutput, ...],
) -> tuple[ArtifactRef, ...]:
    if not outputs:
        return ()
    run_dir_value = context.input_snapshot.get("run_dir")
    if run_dir_value is None:
        raise ValueError("legacy file outputs require run_dir in the immutable input snapshot")
    run_dir = Path(run_dir_value).resolve()
    existing = [(output, (run_dir / output.relative_path).resolve()) for output in outputs]
    existing = [(output, path) for output, path in existing if path.is_file() and run_dir in path.parents]
    if not existing:
        return ()
    if context.artifact_publisher is None:
        raise RuntimeError("legacy output exists but no artifact_publisher was provided")
    artifacts: list[ArtifactRef] = []
    for output, path in existing:
        published = context.artifact_publisher.publish(
            path,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=output.kind,
            media_type=output.media_type,
        )
        if inspect.isawaitable(published):
            published = await published
        artifacts.append(published)
    return tuple(artifacts)


class LegacyCallableAdapter:
    def __init__(
        self,
        target: str,
        *,
        allowed_inputs: tuple[str, ...],
        outputs: tuple[LegacyOutput, ...] = (),
        legacy_src_root: Path | None = None,
    ) -> None:
        if target not in _ALLOWED_TARGETS:
            raise ValueError(f"legacy target is not allowlisted: {target}")
        self.target = target
        self.allowed_inputs = tuple(allowed_inputs)
        self.outputs = tuple(outputs)
        self.legacy_src_root = legacy_src_root or Path(__file__).resolve().parents[4] / "src"

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        function = _load_target(self.target, self.legacy_src_root)
        result = function(**_legacy_kwargs(context, self.allowed_inputs))
        if inspect.isawaitable(result):
            result = await result
        artifacts = await _publish_outputs(context, self.outputs)
        summary: dict[str, Any] = {"adapter": "legacy", "target": self.target}
        if isinstance(result, Mapping):
            summary["result"] = dict(result)
        elif isinstance(result, (str, int, float, bool)) or result is None:
            summary["result"] = result
        else:
            summary["result_type"] = type(result).__name__
        await context.checkpoint()
        return StepResult(artifacts=artifacts, output_summary=summary)


class LegacyAssetAdapter:
    """Keep the old brief+match pair behind one declarative media.select step."""

    def __init__(self, legacy_src_root: Path | None = None) -> None:
        self.legacy_src_root = legacy_src_root or Path(__file__).resolve().parents[4] / "src"

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        kwargs = _legacy_kwargs(context, ("run_dir", "index_path", "dlog", "prod"))
        brief = _load_target("agent.steps_briefs:run", self.legacy_src_root)(**kwargs)
        if inspect.isawaitable(brief):
            await brief
        match_kwargs = dict(kwargs)
        match_kwargs["auto"] = context.input_snapshot.get("auto", True)
        match = _load_target("agent.steps_match:run", self.legacy_src_root)(**match_kwargs)
        if inspect.isawaitable(match):
            match = await match
        artifacts = await _publish_outputs(
            context,
            (
                LegacyOutput("briefs.json", "manifest", "application/json"),
                LegacyOutput("edl.json", "manifest", "application/json"),
            ),
        )
        await context.checkpoint()
        return StepResult(
            artifacts=artifacts,
            output_summary={"adapter": "legacy", "target": "briefs+match", "matched": bool(match)},
        )


def legacy_capability_registry(legacy_src_root: Path | None = None) -> CapabilityRegistry:
    """Build opt-in legacy implementations; no old account id is accepted or read."""

    root = legacy_src_root
    registry = CapabilityRegistry()
    registry.register(
        "research.collect",
        LegacyCallableAdapter(
            "agent.steps_script:_facts_draft",
            allowed_inputs=("person", "angle", "prod"),
            legacy_src_root=root,
        ),
    )
    registry.register(
        "writing.compose",
        LegacyCallableAdapter(
            "agent.steps_script:run",
            allowed_inputs=(
                "run_dir", "person", "duration_s", "genre", "angle", "dlog",
                "exemplar_path", "prod", "review_draft_on_fail",
            ),
            outputs=(LegacyOutput("script.json", "script", "application/json"),),
            legacy_src_root=root,
        ),
    )
    registry.register(
        "audio.synthesize",
        LegacyCallableAdapter(
            "agent.steps_tts:run",
            allowed_inputs=(
                "run_dir", "provider", "voice", "speech_rate", "minimax_speed", "workers", "lang", "edge_rate",
            ),
            outputs=(
                LegacyOutput("manifest.json", "manifest", "application/json"),
            ),
            legacy_src_root=root,
        ),
    )
    registry.register("media.select", LegacyAssetAdapter(root))
    registry.register(
        "render.compose",
        LegacyCallableAdapter(
            "agent.steps_assemble:run",
            allowed_inputs=("run_dir", "lib_dir", "out_name", "ai_label"),
            legacy_src_root=root,
        ),
    )
    registry.register(
        "quality.evaluate",
        LegacyCallableAdapter(
            "agent.steps_qc:run",
            allowed_inputs=("run_dir", "lib_dir", "final", "out_name", "genre_desc", "dlog", "max_rounds"),
            outputs=(LegacyOutput("qc_report.json", "qc_report", "application/json"),),
            legacy_src_root=root,
        ),
    )
    return registry.freeze()

