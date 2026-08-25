from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest
from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.generation.creative_writing import (
    GeneratedCreativeWritingCapability,
)


class FakeClient:
    def __init__(self, *responses: Mapping[str, Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def structured(self, **request: Any) -> Mapping[str, Any]:
        self.requests.append(request)
        return self.responses.pop(0)


class MemoryStorage:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    def publish(self, context: StepContext, artifact: Any) -> ArtifactRef:
        identity = str(uuid4())
        self.payloads[identity] = artifact.data
        return ArtifactRef(
            id=identity,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"runs/{context.run_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash="a" * 64,
            filename=artifact.filename,
        )


def snapshot(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "topic": "云海上的未来城市",
        "brief": "一座漂浮在云海上的未来城市，在清晨慢慢苏醒。",
        "direction": "cinematic",
        "aspect_ratio": "9:16",
        "duration_seconds": 15,
        "variants_per_scene": 2,
        "continuity": True,
        "ai_disclosure": True,
        "visual_source_mode": "generated_only",
        "full_ai_run_id": str(uuid4()),
        "scene_count": 3,
        "candidate_count": 6,
        "billable_seconds": 30,
        "max_cost_minor": 360,
    }
    value.update(overrides)
    return value


def script(*, scenes: int = 3) -> dict[str, Any]:
    narration_parts = [
        "晨光穿过云层，漂浮城市从安静中醒来。",
        "空中列车掠过高塔，街道的灯光依次熄灭。",
        "人们推开窗户，让新一天的风进入房间。",
    ][:scenes]
    beats = [
        {
            "id": f"beat-{index:03d}",
            "sequence": index,
            "narration": narration,
            "visual_description": f"未来城市清晨连续镜头 {index}",
            "must_match": ["未来城市"],
            "must_not_match": ["文字水印"],
        }
        for index, narration in enumerate(narration_parts, start=1)
    ]
    return {
        "title": "云上晨光",
        "narration": "".join(narration_parts),
        "scenes": [beat["visual_description"] for beat in beats],
        "beats": beats,
    }


def context(value: Mapping[str, Any] | None = None) -> StepContext:
    return StepContext(
        workspace_id=str(uuid4()),
        run_id=str(uuid4()),
        step_id="write",
        input_snapshot=dict(value or snapshot()),
    )


def test_generated_writer_needs_no_research_and_publishes_exact_plan() -> None:
    client = FakeClient(script())
    storage = MemoryStorage()

    result = asyncio.run(
        GeneratedCreativeWritingCapability(client, storage).execute(context())
    )

    assert result.requires_review is False
    assert result.output_summary["scene_plan_fit"] is True
    assert len(client.requests) == 1
    assert client.requests[0]["operation"] == "writing.compose.generated"
    assert client.requests[0]["schema"]["properties"]["beats"]["minItems"] == 3
    beat_schema = client.requests[0]["schema"]["properties"]["beats"]["items"]
    assert beat_schema["properties"]["must_match"]["maxItems"] == 12
    assert beat_schema["properties"]["must_not_match"]["maxItems"] == 12
    payload = json.loads(storage.payloads[result.artifacts[0].id])
    assert len(payload["beats"]) == 3
    assert "".join(item["narration"] for item in payload["beats"]) == payload["narration"]


def test_generated_writer_repairs_scene_count_before_publishing() -> None:
    client = FakeClient(script(scenes=2), script())
    storage = MemoryStorage()

    result = asyncio.run(
        GeneratedCreativeWritingCapability(client, storage).execute(context())
    )

    assert len(client.requests) == 2
    assert result.output_summary["beats"] == 3


def test_generated_writer_repairs_unbalanced_or_non_partitioned_beats() -> None:
    unbalanced = script()
    unbalanced["beats"][0]["narration"] = unbalanced["narration"]
    unbalanced["beats"][1]["narration"] = "短"
    unbalanced["beats"][2]["narration"] = "句"
    client = FakeClient(unbalanced, script())
    storage = MemoryStorage()

    result = asyncio.run(
        GeneratedCreativeWritingCapability(client, storage).execute(context())
    )

    assert len(client.requests) == 2
    assert "beat_narration_must_partition_full_narration" in client.requests[1]["system"]
    assert "each_beat_narration_must_be_12_to_30_characters" in client.requests[1]["system"]
    payload = json.loads(storage.payloads[result.artifacts[0].id])
    assert "".join(item["narration"] for item in payload["beats"]) == payload["narration"]


def test_generated_writer_rejects_persistent_unbalanced_beats_before_paid_generation() -> None:
    unbalanced = script()
    parts = ["这是一段明显过长并且会独占绝大多数旁白时长的镜头说明" * 3, "短", "句"]
    unbalanced["narration"] = "".join(parts)
    for beat, narration in zip(unbalanced["beats"], parts, strict=True):
        beat["narration"] = narration
    client = FakeClient(unbalanced, unbalanced)
    storage = MemoryStorage()

    with pytest.raises(PermanentStepError, match="FULL_AI_PLAN_DRIFT"):
        asyncio.run(
            GeneratedCreativeWritingCapability(client, storage).execute(context())
        )

    assert storage.payloads == {}


def test_generated_writer_fails_before_generation_when_plan_still_drifts() -> None:
    client = FakeClient(script(scenes=2), script(scenes=2))
    storage = MemoryStorage()

    with pytest.raises(PermanentStepError, match="FULL_AI_PLAN_DRIFT"):
        asyncio.run(
            GeneratedCreativeWritingCapability(client, storage).execute(context())
        )

    assert storage.payloads == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"visual_source_mode": "catalog"},
        {"candidate_count": 7},
        {"billable_seconds": 35},
        {"ai_disclosure": False},
        {"aspect_ratio": "1:1"},
    ],
)
def test_generated_writer_rejects_non_generated_or_tampered_snapshots(
    overrides: Mapping[str, Any],
) -> None:
    client = FakeClient(script())

    with pytest.raises(PermanentStepError):
        asyncio.run(
            GeneratedCreativeWritingCapability(client, MemoryStorage()).execute(
                context(snapshot(**overrides))
            )
        )

    assert client.requests == []
