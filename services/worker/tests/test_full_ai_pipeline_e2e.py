from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.edl_media import (
    _AI_DISCLOSURE,
    FFmpegEdlRenderCapability,
)
from framefactory.worker.adapters.legacy_media import FFmpegQualityCapability
from framefactory.worker.config import LegacyMediaSettings
from framefactory.worker.generation import (
    PaidOperationAction,
    PaidOperationDecision,
    PaidOperationRecord,
    RunwayClient,
    RunwayHttpResponse,
    RunwayMediaGenerationCapability,
)
from framefactory.worker.generation.creative_writing import (
    GeneratedCreativeWritingCapability,
)
from framefactory.worker.generation.verifier import GeneratedVideoVerifier
from framefactory.worker.providers import ProviderArtifact
from framefactory.worker.timeline_edl import TimelineCapability

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="Full-AI no-cost E2E requires FFmpeg and FFprobe",
)

_WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
_RUN_ID = "22222222-2222-4222-8222-222222222222"
_TERMS_HASH = hashlib.sha256(b"runway-terms-fixture").hexdigest()
_PRICING_HASH = hashlib.sha256(b"runway-pricing-fixture").hexdigest()


class MemoryStorage:
    """Run-scoped object store that has no asset-library or retrieval port."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.artifacts: dict[str, ArtifactRef] = {}
        self.read_kinds: list[str] = []
        self.materialized_kinds: list[str] = []

    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef:
        artifact_id = str(uuid4())
        reference = ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"runs/{context.run_id}/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=hashlib.sha256(artifact.data).hexdigest(),
            filename=artifact.filename,
        )
        self.values[artifact_id] = artifact.data
        self.artifacts[artifact_id] = reference
        return reference

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        self.read_kinds.append(artifact.kind)
        return self.values[artifact.id]

    def read_json(self, artifact: ArtifactRef) -> Mapping[str, Any]:
        return json.loads(self.read_bytes(artifact))

    def materialize(self, artifact: ArtifactRef, destination: Path) -> None:
        self.materialized_kinds.append(artifact.kind)
        destination.write_bytes(self.values[artifact.id])

    def publish_copy(self, *_args: object, **_kwargs: object) -> ArtifactRef:
        raise AssertionError("generated-only E2E must not copy catalog assets")


class FrozenWriterClient:
    def __init__(self, script: Mapping[str, Any]) -> None:
        self.script = dict(script)
        self.operations: list[str] = []

    async def structured(self, **request: Any) -> Mapping[str, Any]:
        self.operations.append(str(request["operation"]))
        return self.script


class DynamicVisionAnalyzer:
    """Independent deterministic vision evidence over real extracted frames."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        contract = technical["verification_contract"]
        assert isinstance(contract, Mapping)
        must_match = tuple(str(item) for item in contract["must_match"])
        must_not_match = tuple(str(item) for item in contract["must_not_match"])
        frame_hashes = [hashlib.sha256(frame.read_bytes()).hexdigest() for frame in frames]
        assert len(frame_hashes) == len(set(frame_hashes)) == 3
        self.requests.append(
            {
                "beat_id": contract["beat_id"],
                "frame_hashes": frame_hashes,
            }
        )
        return {
            "description": "动态测试画面呈现未来城市与清晨光线。",
            "labels": ["未来城市", "清晨", "动态画面"],
            "confidence": 0.99,
            "visual_description_check": {
                "matched": True,
                "evidence": "首、中、尾三帧均为连续变化的可用动态画面。",
            },
            "constraint_checks": [
                *(
                    {
                        "kind": "must_match",
                        "term": term,
                        "matched": True,
                        "evidence": f"独立画面分析确认出现 {term}。",
                    }
                    for term in must_match
                ),
                *(
                    {
                        "kind": "must_not_match",
                        "term": term,
                        "matched": False,
                        "evidence": f"独立画面分析未发现 {term}。",
                    }
                    for term in must_not_match
                ),
            ],
            "has_watermark": False,
            "has_embedded_text": False,
            "unsafe": False,
            "safety": {
                "adult": False,
                "violence": False,
                "self_harm": False,
                "hate_or_extremism": False,
                "illegal_activity": False,
                "recognizable_real_person_or_public_figure": False,
                "brand_or_logo": False,
                "protected_character": False,
            },
            "quality": {"usable": True, "score": 0.99},
            "cut_safe": True,
            "cut_safe_evidence": "首尾均无黑帧、跳切或动作截断。",
        }


class FakeRunwayTransport:
    """Implements the HTTP contract without sockets or a paid Provider call."""

    def __init__(self, video_bytes: bytes) -> None:
        self.video_bytes = video_bytes
        self.requests: list[tuple[str, str]] = []
        self.task_ids: list[str] = []

    @property
    def post_count(self) -> int:
        return sum(method == "POST" for method, _url in self.requests)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> RunwayHttpResponse:
        del timeout_seconds, maximum_response_bytes
        self.requests.append((method, url))
        if method == "POST" and url.endswith("/v1/text_to_video"):
            assert headers["Authorization"] == "Bearer no-cost-fake-key"
            payload = json.loads(body or b"{}")
            assert payload["model"] == "gen4.5"
            assert payload["duration"] == 5
            assert payload["ratio"] == "1280:720"
            task_id = str(uuid4())
            self.task_ids.append(task_id)
            return _http_json(
                url,
                {"id": task_id, "estimatedCost": {"credits": 5}},
            )
        if method == "GET" and "/v1/tasks/" in url:
            task_id = url.rsplit("/", 1)[-1]
            assert task_id in self.task_ids
            output_url = f"https://cdn.example.test/{task_id}.mp4"
            return _http_json(
                url,
                {
                    "id": task_id,
                    "createdAt": "2026-08-24T00:00:00Z",
                    "status": "SUCCEEDED",
                    "output": [output_url],
                    "cost": {"credits": 5},
                },
            )
        if method == "GET" and url.startswith("https://cdn.example.test/"):
            assert "Authorization" not in headers
            return RunwayHttpResponse(
                status=200,
                headers={"content-type": "video/mp4"},
                body=self.video_bytes,
                final_url=url,
            )
        raise AssertionError(f"unexpected fake Runway request: {method} {url}")


class MemoryPaidOperationLedger:
    """Terminal result checkpoints survive capability retries in this E2E."""

    def __init__(self) -> None:
        self.by_key: dict[str, PaidOperationRecord] = {}
        self.keys_by_id: dict[str, str] = {}
        self.settle_calls = 0

    def reserve(self, **values: Any) -> PaidOperationDecision:
        operation_key = str(values["operation_key"])
        existing = self.by_key.get(operation_key)
        if existing is not None:
            assert existing.request_hash == values["request_hash"]
            action = (
                PaidOperationAction.TERMINAL
                if existing.status == "succeeded"
                else PaidOperationAction.POLL_EXISTING
            )
            return PaidOperationDecision(existing, action)
        record = PaidOperationRecord(
            id=str(uuid4()),
            status="reserved",
            request_hash=str(values["request_hash"]),
            authorized_amount_minor=int(values["authorized_amount_minor"]),
            incurred_amount_minor=0,
            provider_request_id=None,
        )
        self.by_key[operation_key] = record
        self.keys_by_id[record.id] = operation_key
        return PaidOperationDecision(record, PaidOperationAction.SUBMIT_NEW)

    def begin_submit(self, **values: Any) -> PaidOperationRecord:
        return self._update(values, status="submitting")

    def mark_submitted(self, **values: Any) -> PaidOperationRecord:
        return self._update(
            values,
            status="submitted",
            provider_request_id=str(values["provider_request_id"]),
        )

    def mark_succeeded(self, **values: Any) -> PaidOperationRecord:
        return self._update(
            values,
            status="succeeded",
            provider_request_id=str(values["provider_request_id"]),
            incurred_amount_minor=int(values["incurred_amount_minor"]),
            result=dict(values["result"]),
        )

    def settle_run(self, **values: Any) -> None:
        expected = int(values["expected_candidate_count"])
        succeeded = sum(record.status == "succeeded" for record in self.by_key.values())
        assert succeeded == expected
        self.settle_calls += 1

    def release_definite_rejection(self, **_values: Any) -> PaidOperationRecord:
        raise AssertionError("the no-cost Provider must not reject")

    def mark_submit_unknown(self, **_values: Any) -> PaidOperationRecord:
        raise AssertionError("the no-cost Provider outcome must remain known")

    def mark_failed(self, **_values: Any) -> PaidOperationRecord:
        raise AssertionError("the no-cost Provider task must not fail")

    def _update(self, values: Mapping[str, Any], **changes: Any) -> PaidOperationRecord:
        operation_id = str(values["operation_id"])
        operation_key = self.keys_by_id[operation_id]
        current = self.by_key[operation_key]
        assert current.request_hash == values["request_hash"]
        updated = replace(current, **changes)
        self.by_key[operation_key] = updated
        return updated


def _http_json(url: str, value: Mapping[str, Any]) -> RunwayHttpResponse:
    return RunwayHttpResponse(
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(value, separators=(",", ":")).encode(),
        final_url=url,
    )


def _snapshot(full_ai_run_id: str) -> dict[str, Any]:
    terms = {
        "ref": "fixture://runway/terms/2026-08-24",
        "content_hash": _TERMS_HASH,
        "captured_at": "2026-08-24T00:00:00Z",
    }
    pricing = {
        "ref": "fixture://runway/pricing/2026-08-24",
        "content_hash": _PRICING_HASH,
        "captured_at": "2026-08-24T00:00:00Z",
        "currency": "USD",
        "credit_unit_minor": 1,
        "cost_per_second_minor": 1,
    }
    generation = {
        "provider_name": "runway",
        "model_id": "gen4.5",
        "ratio": "1280:720",
        "variants_per_beat": 1,
        "target_duration_seconds": 15,
        "scene_count": 3,
        "candidate_count": 3,
        "billable_seconds": 15,
        "max_cost_minor": 15,
        "continuity_mode": "prompt_pack",
        "full_ai_run_id": full_ai_run_id,
        "terms_snapshot": terms,
        "pricing_snapshot": pricing,
        "output_rights_confirmed": True,
        "output_rights_license_basis": "operator-confirmed-provider-terms",
    }
    return {
        "topic": "云海上的未来城市",
        "brief": "一座漂浮在云海上的未来城市，在清晨慢慢苏醒。",
        "direction": "cinematic",
        "aspect_ratio": "16:9",
        "duration_seconds": 15,
        "variants_per_scene": 1,
        "continuity": True,
        "ai_disclosure": True,
        "visual_source_mode": "generated_only",
        "full_ai_run_id": full_ai_run_id,
        "scene_count": 3,
        "candidate_count": 3,
        "billable_seconds": 15,
        "max_cost_minor": 15,
        "_framefactory": {
            "composition_snapshot": {
                "asset_library_ids": [],
                "visual_source_mode": "generated_only",
                "full_ai_generation": generation,
                "production_settings": {
                    "target_duration_seconds": 15,
                    "resolution": {"width": 480, "height": 270},
                    "frame_rate": 15,
                    "layout": "full_frame",
                    "media_fit": "cover",
                    "background_color": "#101820",
                    "subtitles": {
                        "enabled": False,
                        "position": "bottom",
                        "size": "small",
                        "max_lines": 2,
                    },
                },
            }
        },
    }


def _script() -> dict[str, Any]:
    narrations = (
        "晨光穿过云海，未来城市缓缓点亮第一座高塔。",
        "空中列车驶过玻璃街区，花园与道路依次苏醒。",
        "年轻设计师推开窗户，迎接城市崭新的一天。",
    )
    beats = [
        {
            "id": f"beat-{index:03d}",
            "sequence": index,
            "narration": narration,
            "visual_description": f"未来城市清晨连续动态镜头 {index}",
            "must_match": ["未来城市"],
            "must_not_match": ["文字水印"],
        }
        for index, narration in enumerate(narrations, start=1)
    ]
    return {
        "title": "云上晨光",
        "narration": "".join(narrations),
        "scenes": [beat["visual_description"] for beat in beats],
        "beats": beats,
    }


def _context(
    snapshot: Mapping[str, Any],
    step_id: str,
    *artifacts: ArtifactRef,
) -> StepContext:
    return StepContext(
        workspace_id=_WORKSPACE_ID,
        run_id=_RUN_ID,
        step_id=step_id,
        input_snapshot=snapshot,
        input_artifacts=artifacts,
    )


def _publish_native_tts_fixture(
    storage: MemoryStorage,
    snapshot: Mapping[str, Any],
    script_ref: ArtifactRef,
    audio_bytes: bytes,
) -> tuple[ArtifactRef, ArtifactRef]:
    context = _context(snapshot, "audio.synthesize", script_ref)
    audio_ref = storage.publish(
        context,
        ProviderArtifact("audio", "narration.wav", "audio/wav", audio_bytes),
    )
    script = storage.read_json(script_ref)
    beats = script["beats"]
    timing_beats = [
        {
            "id": beat["id"],
            "sequence": beat["sequence"],
            "text": beat["narration"],
            "start_seconds": float((index - 1) * 5),
            "end_seconds": float(index * 5),
            "estimated": False,
            "alignment_source": "native_word_aggregation",
        }
        for index, beat in enumerate(beats, start=1)
    ]
    timing = {
        "schema_version": "1.0.0",
        "operation": "audio.synthesize",
        "provider": "no-cost-native-tts-fixture",
        "source": "native_word_boundary",
        "granularity": "word",
        "estimated": False,
        "word_timing_estimated": False,
        "duration_seconds": 15.0,
        "audio_content_hash": audio_ref.content_hash,
        "script_content_hash": script_ref.content_hash,
        "words": [],
        "segments": timing_beats,
        "beats": timing_beats,
    }
    timing_ref = storage.publish(
        context,
        ProviderArtifact(
            "narration_timing",
            "narration-timing.json",
            "application/json",
            json.dumps(timing, ensure_ascii=False, sort_keys=True).encode(),
        ),
    )
    return audio_ref, timing_ref


def _media_fixture_bytes(root: Path) -> tuple[bytes, bytes]:
    clip = root / "generated.mp4"
    audio = root / "narration.wav"
    subprocess.run(
        (
            FFMPEG or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=480x270:rate=15:duration=5",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(clip),
        ),
        capture_output=True,
        check=True,
        timeout=120,
    )
    subprocess.run(
        (
            FFMPEG or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=15",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ),
        capture_output=True,
        check=True,
        timeout=120,
    )
    return clip.read_bytes(), audio.read_bytes()


def _artifact(result: Any, kind: str) -> ArtifactRef:
    return next(artifact for artifact in result.artifacts if artifact.kind == kind)


def _probe_disclosure(video_bytes: bytes, root: Path) -> Mapping[str, Any]:
    output = root / "final.mp4"
    output.write_bytes(video_bytes)
    completed = subprocess.run(
        (
            FFPROBE or "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=comment,description",
            "-of",
            "json",
            str(output),
        ),
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def test_no_cost_full_ai_pipeline_reaches_disclosed_qc_pass_without_catalog_assets() -> None:
    with tempfile.TemporaryDirectory(prefix="vistora-full-ai-e2e-") as raw_directory:
        root = Path(raw_directory)
        generated_video, narration_audio = _media_fixture_bytes(root)
        storage = MemoryStorage()
        full_ai_run_id = str(uuid4())
        snapshot = _snapshot(full_ai_run_id)

        writer = FrozenWriterClient(_script())
        writing_result = asyncio.run(
            GeneratedCreativeWritingCapability(writer, storage).execute(
                _context(snapshot, "writing.compose.generated")
            )
        )
        script_ref = _artifact(writing_result, "script")
        audio_ref, timing_ref = _publish_native_tts_fixture(
            storage,
            snapshot,
            script_ref,
            narration_audio,
        )

        transport = FakeRunwayTransport(generated_video)
        ledger = MemoryPaidOperationLedger()
        vision = DynamicVisionAnalyzer()
        verifier = GeneratedVideoVerifier(
            vision,
            vision_provider="independent-fixture-vision",
            vision_model="fixture-vision-v1",
            ffprobe_command=FFPROBE or "ffprobe",
            ffmpeg_command=FFMPEG or "ffmpeg",
        )
        generator = RunwayMediaGenerationCapability(
            RunwayClient(
                base_url="https://api.dev.runwayml.com",
                api_key="no-cost-fake-key",
                timeout_seconds=10,
                transport=transport,
            ),
            storage,
            ledger,
            verifier,
            worker_id="no-cost-e2e-worker",
            poll_interval_seconds=0.001,
        )
        generation_context = _context(
            snapshot,
            "media.generate",
            script_ref,
            audio_ref,
            timing_ref,
        )
        first_generation = asyncio.run(generator.execute(generation_context))
        first_request_count = len(transport.requests)

        assert writer.operations == ["writing.compose.generated"]
        assert transport.post_count == 3
        assert ledger.settle_calls == 1
        assert len(vision.requests) == 3
        assert snapshot["_framefactory"]["composition_snapshot"]["asset_library_ids"] == []

        candidate_ref = _artifact(first_generation, "candidate_manifest")
        audit_ref = _artifact(first_generation, "generated_material_manifest")
        asset_refs = tuple(
            artifact for artifact in first_generation.artifacts if artifact.kind == "asset"
        )
        candidate = storage.read_json(candidate_ref)
        audit = storage.read_json(audit_ref)
        assert candidate["operation"] == "media.generate"
        assert candidate["catalog_scope"] == "run"
        assert candidate["local_catalog_only"] is False
        assert audit["generated_only"] is True
        assert {job["verification_status"] for job in audit["jobs"]} == {"accepted"}

        timeline_result = asyncio.run(
            TimelineCapability(storage).execute(
                _context(
                    snapshot,
                    "timeline.align",
                    script_ref,
                    audio_ref,
                    timing_ref,
                    audit_ref,
                    candidate_ref,
                    *asset_refs,
                )
            )
        )
        selection_ref = _artifact(timeline_result, "material_selection")
        timeline_ref = _artifact(timeline_result, "timeline")
        timeline = storage.read_json(timeline_ref)
        accepted_artifact_ids = {
            str(job["output_artifact_id"])
            for job in audit["jobs"]
            if job["verification_status"] == "accepted"
        }
        assert {shot["artifact_id"] for shot in timeline["shots"]} <= accepted_artifact_ids

        media = LegacyMediaSettings(
            ffmpeg_command=FFMPEG or "ffmpeg",
            ffprobe_command=FFPROBE or "ffprobe",
            width=480,
            height=270,
            frame_rate=15,
        )
        render_result = asyncio.run(
            FFmpegEdlRenderCapability(media, storage).execute(
                _context(
                    snapshot,
                    "render.edl",
                    audio_ref,
                    timing_ref,
                    candidate_ref,
                    selection_ref,
                    timeline_ref,
                    *asset_refs,
                )
            )
        )
        final_ref = _artifact(render_result, "video")
        qc_result = asyncio.run(
            FFmpegQualityCapability(media, storage).execute(
                _context(snapshot, "quality.evaluate", final_ref)
            )
        )
        disclosure_probe = _probe_disclosure(storage.values[final_ref.id], root)
        tags = disclosure_probe["format"]["tags"]

        assert render_result.output_summary["ai_generated"] is True
        assert render_result.output_summary["ai_disclosure_metadata"] == _AI_DISCLOSURE
        assert _AI_DISCLOSURE in {tags.get("comment"), tags.get("description")}
        assert qc_result.output_summary["verdict"] == "pass"
        assert qc_result.requires_review is False

        replay_generation = asyncio.run(generator.execute(generation_context))
        assert _artifact(replay_generation, "candidate_manifest")
        assert transport.post_count == 3
        assert len(transport.requests) == first_request_count
        assert ledger.settle_calls == 2
        assert len(vision.requests) == 3
        assert "media.retrieve" not in writer.operations
        assert set(storage.read_kinds) <= {
            "script",
            "audio",
            "narration_timing",
            "generated_material_manifest",
            "candidate_manifest",
            "material_selection",
            "timeline",
        }
        assert set(storage.materialized_kinds) <= {"asset", "audio", "video"}
        assert first_generation.output_summary["accepted_candidates"] == 3
