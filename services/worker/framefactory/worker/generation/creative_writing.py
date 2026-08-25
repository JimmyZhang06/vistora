"""Generated-only creative writing capability.

This capability deliberately does not consume research or the stock-footage
writing policy.  It turns the immutable Full-AI brief into an exact, paid-plan
bounded Beat set before any video-generation request can be submitted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from framefactory.runtime import PermanentStepError
from framefactory.steps import StepContext, StepResult

from ..providers import ArtifactStorage, ProviderArtifact
from ..retrieval import normalize_beats

_DIRECTIONS = frozenset({"cinematic", "graphic", "illustrated"})
_ASPECT_RATIOS = frozenset({"9:16", "16:9"})
_DURATIONS = frozenset({15, 30, 45, 60})


class StructuredTextClient(Protocol):
    async def structured(
        self,
        *,
        operation: str,
        system: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _FrozenPlan:
    brief: str
    direction: str
    aspect_ratio: str
    duration_seconds: int
    scene_count: int
    variants_per_scene: int
    candidate_count: int
    billable_seconds: int
    continuity: bool


class GeneratedCreativeWritingCapability:
    """Write exactly the scene count authorized by the Full-AI quote."""

    def __init__(self, client: StructuredTextClient, storage: ArtifactStorage) -> None:
        self.client = client
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        snapshot = context.input_snapshot.to_dict()
        plan = _frozen_plan(snapshot)
        schema = _script_schema(plan.scene_count)
        payload = {
            "brief": plan.brief,
            "direction": plan.direction,
            "aspect_ratio": plan.aspect_ratio,
            "duration_seconds": plan.duration_seconds,
            "scene_count": plan.scene_count,
            "clip_seconds": 5,
            "continuity_mode": "prompt_pack" if plan.continuity else "none",
            "human_review_feedback": context.review_feedback,
        }
        result = dict(
            await self.client.structured(
                operation="writing.compose.generated",
                system=_creative_system_prompt(plan),
                payload=payload,
                schema=schema,
            )
        )
        problems = _script_plan_problems(result, plan)
        if problems:
            result = dict(
                await self.client.structured(
                    operation="writing.compose.generated",
                    system=(
                        _creative_system_prompt(plan)
                        + " The prior draft violated these immutable plan constraints: "
                        + ", ".join(problems)
                        + ". Rewrite the complete script; do not add or remove scenes."
                    ),
                    payload={**payload, "draft": result},
                    schema=schema,
                )
            )
            problems = _script_plan_problems(result, plan)
        if problems:
            raise PermanentStepError(
                "FULL_AI_PLAN_DRIFT: creative script does not match the frozen paid plan: "
                + ", ".join(problems)
            )

        canonical = _canonical_script(result, expected_beats=plan.scene_count)
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "script",
                "script.json",
                "application/json",
                json.dumps(
                    canonical,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ),
        )
        await context.checkpoint()
        return StepResult(
            artifacts=(artifact,),
            output_summary={
                "provider_protocol": "openai-compatible",
                "creative_mode": "generated_only",
                "scenes": plan.scene_count,
                "beats": plan.scene_count,
                "target_duration_seconds": plan.duration_seconds,
                "scene_plan_fit": True,
                "continuity_mode": "prompt_pack" if plan.continuity else "none",
            },
        )


def _frozen_plan(snapshot: Mapping[str, Any]) -> _FrozenPlan:
    if snapshot.get("visual_source_mode") != "generated_only":
        raise PermanentStepError(
            "writing.compose.generated requires visual_source_mode=generated_only"
        )
    brief = str(snapshot.get("brief") or "").strip()
    if not brief or len(brief) > 1_600:
        raise PermanentStepError("Full-AI brief must contain 1 to 1600 characters")
    direction = str(snapshot.get("direction") or "")
    aspect_ratio = str(snapshot.get("aspect_ratio") or "")
    duration = _integer(snapshot.get("duration_seconds"), field="duration_seconds")
    scene_count = _integer(snapshot.get("scene_count"), field="scene_count")
    variants = _integer(snapshot.get("variants_per_scene"), field="variants_per_scene")
    candidate_count = _integer(snapshot.get("candidate_count"), field="candidate_count")
    billable_seconds = _integer(snapshot.get("billable_seconds"), field="billable_seconds")
    if direction not in _DIRECTIONS:
        raise PermanentStepError("Full-AI direction is unsupported")
    if aspect_ratio not in _ASPECT_RATIOS:
        raise PermanentStepError("Full-AI aspect ratio is unsupported")
    if duration not in _DURATIONS:
        raise PermanentStepError("Full-AI duration is unsupported")
    if scene_count != duration // 5:
        raise PermanentStepError("FULL_AI_PLAN_DRIFT: scene count does not match duration")
    if not 1 <= variants <= 3:
        raise PermanentStepError("Full-AI variants_per_scene is unsupported")
    if candidate_count != scene_count * variants:
        raise PermanentStepError("FULL_AI_PLAN_DRIFT: candidate count is inconsistent")
    if billable_seconds != candidate_count * 5:
        raise PermanentStepError("FULL_AI_PLAN_DRIFT: billable seconds are inconsistent")
    if snapshot.get("ai_disclosure") is not True:
        raise PermanentStepError("Full-AI disclosure is required")
    continuity = snapshot.get("continuity")
    if not isinstance(continuity, bool):
        raise PermanentStepError("Full-AI continuity must be boolean")
    try:
        UUID(str(snapshot.get("full_ai_run_id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PermanentStepError("Full-AI run identity is invalid") from exc
    return _FrozenPlan(
        brief=brief,
        direction=direction,
        aspect_ratio=aspect_ratio,
        duration_seconds=duration,
        scene_count=scene_count,
        variants_per_scene=variants,
        candidate_count=candidate_count,
        billable_seconds=billable_seconds,
        continuity=continuity,
    )


def _creative_system_prompt(plan: _FrozenPlan) -> str:
    continuity = (
        "Repeat a concise subject, wardrobe, palette, environment and cinematography bible "
        "inside every visual description, while acknowledging that prompt continuity is not a "
        "reference-frame identity lock."
        if plan.continuity
        else "Each scene may vary freely while preserving the story's causal order."
    )
    lower, upper = _narration_character_bounds(plan.duration_seconds)
    beat_lower, beat_upper = _beat_narration_character_bounds()
    return (
        "Create an original production script from the user's creative brief. This is a fictional "
        "generated-video workflow, not factual reporting or stock-footage search. You may elaborate "
        "settings, actions, atmosphere and camera language when consistent with the brief, but do "
        "not imitate a living artist, public figure, protected character or brand. Return exactly "
        f"{plan.scene_count} chronological scenes and exactly {plan.scene_count} Beats. Each Beat "
        "must cover one independently generated five-second visual, have a unique stable id and a "
        "sequence from one without gaps. Beat narrations must form an exact ordered partition of the "
        f"full narration. The full Chinese narration must contain {lower} to {upper} non-whitespace "
        f"characters for a {plan.duration_seconds}-second voice track, and every Beat narration must "
        f"contain {beat_lower} to {beat_upper} non-whitespace characters so its real speech timing can "
        f"fit one five-second generated clip. {continuity}"
    )


def _script_schema(scene_count: int) -> dict[str, Any]:
    beat = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "sequence",
            "narration",
            "visual_description",
            "must_match",
            "must_not_match",
        ],
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 160},
            "sequence": {"type": "integer", "minimum": 1, "maximum": scene_count},
            "narration": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "visual_description": {"type": "string", "minLength": 1, "maxLength": 900},
            "must_match": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "must_not_match": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "narration", "scenes", "beats"],
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "narration": {"type": "string", "minLength": 1, "maxLength": 5_000},
            "scenes": {
                "type": "array",
                "minItems": scene_count,
                "maxItems": scene_count,
                "items": {"type": "string", "minLength": 1, "maxLength": 900},
            },
            "beats": {
                "type": "array",
                "minItems": scene_count,
                "maxItems": scene_count,
                "items": beat,
            },
        },
    }


def _script_plan_problems(script: Mapping[str, Any], plan: _FrozenPlan) -> tuple[str, ...]:
    problems: list[str] = []
    scenes = script.get("scenes")
    beats = script.get("beats")
    if not isinstance(scenes, list) or len(scenes) != plan.scene_count:
        problems.append(f"scenes_must_equal_{plan.scene_count}")
    if not isinstance(beats, list) or len(beats) != plan.scene_count:
        problems.append(f"beats_must_equal_{plan.scene_count}")
    narration = str(script.get("narration") or "")
    lower, upper = _narration_character_bounds(plan.duration_seconds)
    visible = len("".join(narration.split()))
    if not lower <= visible <= upper:
        problems.append(f"narration_characters_must_be_{lower}_to_{upper}")
    if isinstance(beats, list):
        mapped_beats = [item for item in beats if isinstance(item, Mapping)]
        if len(mapped_beats) != len(beats):
            problems.append("every_beat_must_be_an_object")
            return tuple(problems)
        sequences = [item.get("sequence") for item in mapped_beats]
        if sequences != list(range(1, plan.scene_count + 1)):
            problems.append("beat_sequence_must_be_contiguous")
        beat_ids = [str(item.get("id") or "").strip() for item in mapped_beats]
        if (
            any(not beat_id or len(beat_id) > 160 for beat_id in beat_ids)
            or len(set(beat_ids)) != len(beat_ids)
        ):
            problems.append("beat_ids_must_be_unique_and_bounded")
        beat_narrations = [str(item.get("narration") or "").strip() for item in mapped_beats]
        if "".join(beat_narrations) != narration.strip():
            problems.append("beat_narration_must_partition_full_narration")
        # Each generated candidate is exactly five seconds in P1.  Keeping the
        # narration budget reasonably balanced per Beat prevents the EDL from
        # having to reuse overlapping trims of the same five-second clip when
        # one Beat consumes a disproportionate share of the voice track.
        per_beat_lower, per_beat_upper = _beat_narration_character_bounds()
        if any(
            not per_beat_lower <= len("".join(text.split())) <= per_beat_upper
            for text in beat_narrations
        ):
            problems.append(
                f"each_beat_narration_must_be_{per_beat_lower}_to_{per_beat_upper}_characters"
            )
    return tuple(problems)


def _canonical_script(script: Mapping[str, Any], *, expected_beats: int) -> dict[str, Any]:
    try:
        beats = normalize_beats(script)
    except (TypeError, ValueError, PermanentStepError) as exc:
        raise PermanentStepError(
            "FULL_AI_PLAN_DRIFT: generated script cannot be normalized"
        ) from exc
    if len(beats) != expected_beats:
        raise PermanentStepError(
            "FULL_AI_PLAN_DRIFT: normalized Beat count differs from frozen scene count"
        )
    narration = str(script.get("narration") or "").strip()
    if "".join(beat.narration for beat in beats) != narration:
        raise PermanentStepError(
            "FULL_AI_PLAN_DRIFT: Beat narration is not an exact ordered partition"
        )
    canonical_beats = [
        {
            "id": beat.id,
            "sequence": beat.sequence,
            "narration": beat.narration,
            "visual_description": beat.visual_description,
            "must_match": list(beat.must_match),
            "must_not_match": list(beat.must_not_match),
        }
        for beat in beats
    ]
    return {
        "title": str(script.get("title") or "").strip(),
        "narration": narration,
        "scenes": [beat["visual_description"] for beat in canonical_beats],
        "beats": canonical_beats,
    }


def _narration_character_bounds(target_duration: int) -> tuple[int, int]:
    return max(20, round(target_duration * 3.5)), round(target_duration * 6.5)


def _beat_narration_character_bounds() -> tuple[int, int]:
    # The provider clip budget is frozen at five seconds.  This deliberately
    # leaves headroom for punctuation and TTS prosody while rejecting extreme
    # partitions before any paid video request is submitted.
    return 12, 30


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentStepError(f"Full-AI {field} must be an integer")
    return value
