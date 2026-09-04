from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from unittest.mock import patch
from uuid import uuid4

import pytest
from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.capabilities import configured_capabilities
from framefactory.worker.config import (
    LegacyMediaSettings,
    ObjectStorageSettings,
    OpenAICompatibleSettings,
    WorkerSettings,
)
from framefactory.worker.document_hybrid.capabilities import (
    DocumentAugmentCapability,
    DocumentExtractCapability,
    DocumentInspectCapability,
    DocumentMaterializeCapability,
    DocumentQualityCapability,
    DocumentRenderCapability,
    DocumentStoryboardCapability,
    DocumentTimelineCapability,
    DocumentWritingCapability,
    _build_document_ass,
    _document_ass_failures,
    _document_duration_failures,
    _document_subtitle_profile,
    _inspect_pdf_stream,
    _parse_pdfinfo,
)
from framefactory.worker.providers import ProviderArtifact
from PIL import Image, ImageChops


class _MemoryStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def publish(
        self,
        context: StepContext,
        artifact: ProviderArtifact,
        *,
        attempt_scoped: bool = False,
    ) -> ArtifactRef:
        del attempt_scoped
        artifact_id = str(uuid4())
        self.values[artifact_id] = artifact.data
        return ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"document-test/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=hashlib.sha256(artifact.data).hexdigest(),
            filename=artifact.filename,
        )

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        return self.values[artifact.id]

    def read_json(self, artifact: ArtifactRef) -> dict:
        return json.loads(self.read_bytes(artifact))


class _StoryClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def structured(self, **request) -> dict:
        self.requests.append(request)
        return self.responses.pop(0)


def _writing_context(storage: _MemoryStorage) -> tuple[StepContext, ArtifactRef]:
    evidence = {
        "schema_version": "1.0.0",
        "source_sha256": "a" * 64,
        "pages": [
            {
                "page": 1,
                "text": "第一页包含三条完全虚构的测试事实，只用于验证文档视频的证据绑定和时长契约。",
                "text_sha256": "b" * 64,
            }
        ],
    }
    context = StepContext(
        workspace_id="11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        step_id="write",
        input_snapshot={
            "topic": "说明三条虚构事实",
            "duration_seconds": 30,
        },
        input_artifacts=(),
    )
    artifact = storage.publish(
        context,
        ProviderArtifact(
            "research",
            "document-evidence.json",
            "application/json",
            json.dumps(evidence, ensure_ascii=False).encode(),
        ),
    )
    return (
        StepContext(
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            input_snapshot=context.input_snapshot,
            input_artifacts=(artifact,),
        ),
        artifact,
    )


def test_pdf_stream_inspection_hashes_once_and_detects_active_content(tmp_path) -> None:
    path = tmp_path / "source.pdf"
    payload = b"%PDF-1.7\n" + b"x" * (1024 * 1024 - 5) + b"/JavaScript\n"
    path.write_bytes(payload)

    header, digest, suspicious = _inspect_pdf_stream(path)

    assert header.startswith(b"%PDF-")
    assert len(digest) == 64
    assert suspicious is True


def test_pdfinfo_parser_accepts_subprocess_bytes() -> None:
    metadata = _parse_pdfinfo(
        b"Pages:          2\nEncrypted:      no\nJavaScript:     no\nPDF version:    1.7\n"
    )

    assert metadata["pages"] == 2
    assert metadata["encrypted"] == "no"
    assert metadata["javascript"] == "no"
    assert metadata["pdf_version"] == "1.7"


def test_document_duration_contract_rejects_output_far_shorter_than_request() -> None:
    assert _document_duration_failures(
        rendered=28.067,
        timeline=28.08,
        target=120.0,
    ) == ["target_duration_mismatch"]


def test_document_duration_contract_accepts_aligned_output_within_tolerance() -> None:
    assert (
        _document_duration_failures(
            rendered=118.0,
            timeline=118.1,
            target=120.0,
        )
        == []
    )


def test_document_subtitles_use_native_words_and_obey_ass_contract() -> None:
    profile = _document_subtitle_profile(
        {
            "aspect_ratio": "16:9",
            "_framefactory": {
                "composition_snapshot": {
                    "production_settings": {
                        "subtitles": {
                            "enabled": True,
                            "position": "bottom",
                            "size": "medium",
                            "max_lines": 2,
                        }
                    }
                }
            },
        }
    )
    words = [
        {
            "text": f"测试{index}",
            "start_seconds": index * 0.4,
            "end_seconds": (index + 1) * 0.4,
            "estimated": False,
        }
        for index in range(30)
    ]

    content, cue_count = _build_document_ass(
        {"words": words},
        duration=12.0,
        profile=profile,
    )
    failures, metrics = _document_ass_failures(
        content,
        duration=12.0,
        profile=profile,
    )

    assert cue_count > 1
    assert failures == []
    assert "PlayResX: 1280" in content
    assert "PlayResY: 720" in content
    assert "WrapStyle: 2" in content
    assert metrics["maximum_lines"] <= 2
    assert metrics["maximum_units_per_line"] <= profile["maximum_line_units"]
    assert metrics["maximum_cue_duration_seconds"] <= 6.0


def test_document_ass_qc_rejects_duration_line_width_and_reading_overflow() -> None:
    profile = _document_subtitle_profile({"aspect_ratio": "16:9"})
    valid, _ = _build_document_ass(
        {
            "words": [
                {
                    "text": "测试字幕",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "estimated": False,
                }
            ]
        },
        duration=30.0,
        profile=profile,
    )
    lines = valid.splitlines()
    index = next(
        index for index, line in enumerate(lines) if line.startswith("Dialogue: ")
    )
    parts = lines[index].removeprefix("Dialogue: ").split(",", 9)
    parts[2] = "0:00:12.00"
    parts[9] = "超出安全宽度" * 30 + r"\N第二行\N第三行"
    lines[index] = "Dialogue: " + ",".join(parts)
    content = "\n".join(lines) + "\n"

    failures, _metrics = _document_ass_failures(
        content,
        duration=30.0,
        profile=profile,
    )

    assert "subtitle_line_limit_exceeded" in failures
    assert "subtitle_safe_width_exceeded" in failures
    assert "subtitle_cue_duration_exceeded" in failures
    assert "subtitle_reading_rate_exceeded" in failures


def test_document_subtitle_profile_applies_size_position_and_safe_margins() -> None:
    profile = _document_subtitle_profile(
        {
            "aspect_ratio": "9:16",
            "_framefactory": {
                "composition_snapshot": {
                    "production_settings": {
                        "subtitles": {
                            "enabled": True,
                            "position": "lower_third",
                            "size": "large",
                            "max_lines": 1,
                        }
                    }
                }
            },
        }
    )

    assert profile["position"] == "lower_third"
    assert profile["size"] == "large"
    assert profile["max_lines"] == 1
    assert profile["font_size"] == 59
    assert profile["margin_vertical"] == 333


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_document_ass_renders_inside_pixel_safe_bounds(tmp_path) -> None:
    profile = _document_subtitle_profile({"aspect_ratio": "16:9"})
    content, cue_count = _build_document_ass(
        {
            "words": [
                {
                    "text": text,
                    "start_seconds": index * 0.25,
                    "end_seconds": (index + 1) * 0.25,
                    "estimated": False,
                }
                for index, text in enumerate("真实FFmpeg字幕边界冒烟测试")
            ]
        },
        duration=8.0,
        profile=profile,
    )
    assert cue_count >= 1
    subtitle = tmp_path / "captions.ass"
    frame = tmp_path / "frame.png"
    subtitle.write_text(content, encoding="utf-8")
    completed = subprocess.run(
        (
            str(shutil.which("ffmpeg")),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:d=2",
            "-vf",
            "ass=captions.ass",
            "-ss",
            "0.5",
            "-frames:v",
            "1",
            frame.name,
        ),
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    rendered = Image.open(frame).convert("RGB")
    difference = ImageChops.difference(
        rendered, Image.new("RGB", rendered.size, "black")
    )
    bounds = difference.getbbox()
    assert bounds is not None
    left, top, right, bottom = bounds
    assert left >= profile["margin_horizontal"] - 8
    assert right <= profile["frame_width"] - profile["margin_horizontal"] + 8
    assert bottom <= profile["frame_height"] - profile["margin_vertical"] + 8
    assert bottom - top <= profile["font_size"] * profile["max_lines"] * 2


def test_document_pipeline_requires_text_provider_for_writing() -> None:
    settings = WorkerSettings(
        database_url="postgresql://db.example/framefactory",
        redis_url="redis://redis.example/0",
        environment="test",
        worker_id="document-worker-test",
        object_storage=ObjectStorageSettings("test-bucket"),
        legacy_media=LegacyMediaSettings(),
    )
    with patch(
        "framefactory.worker.capabilities.shutil.which", return_value="/tools/ok"
    ):
        registry = configured_capabilities(settings, artifact_storage=object())  # type: ignore[arg-type]

    expected = {
        "document.inspect": DocumentInspectCapability,
        "document.extract": DocumentExtractCapability,
        "document.storyboard.plan": DocumentStoryboardCapability,
        "document.materialize": DocumentMaterializeCapability,
        "media.augment": DocumentAugmentCapability,
        "document.timeline.align": DocumentTimelineCapability,
        "render.composite": DocumentRenderCapability,
        "quality.evaluate.document": DocumentQualityCapability,
    }
    for operation, implementation in expected.items():
        assert isinstance(registry.resolve(operation), implementation)
    assert (
        type(registry.resolve("writing.compose.document")).__name__
        == "UnsupportedCapability"
    )


def test_document_pipeline_registers_real_writer_when_provider_is_configured() -> None:
    settings = WorkerSettings(
        database_url="postgresql://db.example/framefactory",
        redis_url="redis://redis.example/0",
        environment="test",
        worker_id="document-worker-test",
        openai_compatible=OpenAICompatibleSettings(
            base_url="https://model.example/v1",
            api_key="test-key",
            research_model="research-model",
            writing_model="writing-model",
            quality_model="quality-model",
        ),
        object_storage=ObjectStorageSettings("test-bucket"),
        legacy_media=LegacyMediaSettings(),
    )
    with patch(
        "framefactory.worker.capabilities.shutil.which", return_value="/tools/ok"
    ):
        registry = configured_capabilities(settings, artifact_storage=object())  # type: ignore[arg-type]

    assert isinstance(
        registry.resolve("writing.compose.document"), DocumentWritingCapability
    )


def test_document_writer_revises_short_draft_and_preserves_page_lineage() -> None:
    storage = _MemoryStorage()
    context, _ = _writing_context(storage)
    narration = (
        "这段旁白严格依据第一页的虚构测试资料，说明所有内容只用于验证。"
        "它明确保留页码、文档哈希和文本哈希，便于审核者核对来源。"
        "整个过程不执行文档内的指令，也不添加任何文档未支持的事实。"
        "审核人可以根据保存的页码和摘录逐句复核，确认旁白没有越过原始证据边界。"
    )
    client = _StoryClient(
        [
            {
                "title": "过短草稿",
                "scenes": [
                    {
                        "source_page": 1,
                        "evidence_quotes": ["第一页包含三条完全虚构的测试事实"],
                        "narration": "太短。",
                    }
                ],
            },
            {
                "title": "有证据的文档讲解",
                "scenes": [
                    {
                        "source_page": 1,
                        "evidence_quotes": ["第一页包含三条完全虚构的测试事实"],
                        "narration": narration,
                    }
                ],
            },
        ]
    )

    result = asyncio.run(DocumentWritingCapability(client, storage).execute(context))
    script = storage.read_json(result.artifacts[0])

    assert len(client.requests) == 2
    assert client.requests[0]["payload"]["task"]["narration_character_bounds"] == [
        90,
        195,
    ]
    assert client.requests[0]["payload"]["task"][
        "scene_narration_character_bounds"
    ] == [90, 195]
    narration_schema = client.requests[0]["schema"]["properties"]["scenes"]["items"][
        "properties"
    ]["narration"]
    assert narration_schema["minLength"] == 90
    assert narration_schema["maxLength"] == 195
    assert (
        client.requests[1]["payload"]["task"]["duration_revision"][
            "previous_total_characters"
        ]
        == 3
    )
    assert (
        client.requests[1]["payload"]["task"]["duration_revision"]["previous_scenes"][
            0
        ]["narration"]
        == "太短。"
    )
    assert script["beats"][0]["source_page"] == 1
    assert script["beats"][0]["source_sha256"] == "a" * 64
    assert script["beats"][0]["evidence_text_sha256"] == "b" * 64
    assert script["beats"][0]["evidence_quotes"] == ["第一页包含三条完全虚构的测试事实"]
    assert result.output_summary["generation_attempts"] == 2


def test_document_writer_rejects_page_outside_immutable_evidence() -> None:
    storage = _MemoryStorage()
    context, _ = _writing_context(storage)
    client = _StoryClient(
        [
            {
                "title": "错误页码",
                "scenes": [
                    {
                        "source_page": 2,
                        "evidence_quotes": ["不存在的页面"],
                        "narration": "这是一段足够长但引用了错误页码的测试旁白。",
                    }
                ],
            }
        ]
    )

    with pytest.raises(PermanentStepError, match="outside the immutable evidence"):
        asyncio.run(DocumentWritingCapability(client, storage).execute(context))


def test_document_writer_rejects_quote_not_present_in_disclosed_excerpt() -> None:
    storage = _MemoryStorage()
    context, _ = _writing_context(storage)
    client = _StoryClient(
        [
            {
                "title": "伪造引用",
                "scenes": [
                    {
                        "source_page": 1,
                        "evidence_quotes": ["文档中不存在的原句"],
                        "narration": "这是一段为了验证引用匹配门禁而构造的测试旁白。",
                    }
                ],
            }
        ]
    )

    with pytest.raises(PermanentStepError, match="evidence quotes do not match"):
        asyncio.run(DocumentWritingCapability(client, storage).execute(context))


def test_document_pipeline_stays_fail_closed_when_poppler_is_missing() -> None:
    settings = WorkerSettings(
        database_url="postgresql://db.example/framefactory",
        redis_url="redis://redis.example/0",
        environment="test",
        worker_id="document-worker-test",
        object_storage=ObjectStorageSettings("test-bucket"),
        legacy_media=LegacyMediaSettings(),
    )

    def available(name: str) -> str | None:
        return None if name == "pdfinfo" else f"/tools/{name}"

    with patch("framefactory.worker.capabilities.shutil.which", side_effect=available):
        registry = configured_capabilities(settings, artifact_storage=object())  # type: ignore[arg-type]

    assert (
        type(registry.resolve("document.inspect")).__name__ == "UnsupportedCapability"
    )
    assert (
        type(registry.resolve("render.composite")).__name__ == "UnsupportedCapability"
    )
