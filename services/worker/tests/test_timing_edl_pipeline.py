from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.database_assets import ControlApiAssetAcquirer
from framefactory.worker.adapters.edl_media import (
    FFmpegEdlRenderCapability,
    _assert_no_video_padding,
    _edl_segment_command,
    _validate_rendered_duration,
)
from framefactory.worker.adapters.legacy_media import (
    EdgeSpeechCapability,
    FFmpegQualityCapability,
    FFmpegRenderCapability,
    _narration_timing_payload,
)
from framefactory.worker.capabilities import (
    configured_capabilities,
    production_step_registry,
)
from framefactory.worker.config import (
    AssetLibrarySettings,
    LegacyMediaSettings,
    ObjectStorageSettings,
    WorkerSettings,
)
from framefactory.worker.edl import EdlValidationError, validate_edl
from framefactory.worker.providers import ProviderArtifact
from framefactory.worker.retrieval import DatabaseRetrievalCapability, normalize_beats
from framefactory.worker.retrieval.editorial_fallback import build_editorial_fallback
from framefactory.worker.retrieval.evidence import (
    hard_constraint_evidence_valid,
    rights_evidence_valid,
)
from framefactory.worker.timeline_edl import (
    TimelineCapability,
    _plan_timing_unit,
    _reserve_shot_budget,
)


class MemoryStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.client = object()

    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef:
        artifact_id = str(uuid4())
        self.values[artifact_id] = artifact.data
        return ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"test/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=hashlib.sha256(artifact.data).hexdigest(),
            filename=artifact.filename,
        )

    def publish_copy(self, *_args: object, **_kwargs: object) -> ArtifactRef:
        raise AssertionError("the registry wiring test must not materialize an asset")

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        return self.values[artifact.id]

    def read_json(self, artifact: ArtifactRef) -> dict[str, Any]:
        return json.loads(self.read_bytes(artifact))

    def materialize(self, artifact: ArtifactRef, destination: Path) -> None:
        destination.write_bytes(self.read_bytes(artifact))


def _context(
    *artifacts: ArtifactRef,
    input_snapshot: dict[str, Any] | None = None,
    step_id: str = "run:test",
) -> StepContext:
    return StepContext(
        workspace_id="11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        step_id=step_id,
        input_snapshot=input_snapshot or {},
        input_artifacts=artifacts,
    )


def _publish_json(
    storage: MemoryStorage,
    context: StepContext,
    kind: str,
    payload: dict[str, Any],
) -> ArtifactRef:
    return storage.publish(
        context,
        ProviderArtifact(
            kind,
            f"{kind.replace('_', '-')}.json",
            "application/json",
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
    )


def _candidate(
    asset: ArtifactRef,
    *,
    rank: int,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    return {
        "candidate_id": f"candidate-{rank:020x}",
        "rank": rank,
        "eligible": True,
        "materialized": True,
        "artifact_id": asset.id,
        "artifact_filename": asset.filename,
        "asset_id": str(uuid4()),
        "asset_file_id": str(uuid4()),
        "analysis_id": str(uuid4()),
        "segment_id": str(uuid4()),
        "description": "matching visual",
        "labels": ["matching", "visual"],
        "source_transcript": "",
        "asset_kind": "video",
        "media_type": asset.media_type,
        "content_hash": asset.content_hash,
        "byte_size": asset.byte_size,
        "source_duration_ms": end_ms + 10_000,
        "source_window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": end_ms - start_ms,
        },
        "hard_constraints": {
            "passed": True,
            "must_match": [],
            "must_not_match": [],
        },
        "rejection_codes": [],
        "cut_evidence": {"cut_safe": True, "semantic_complete": True},
        "rights_evidence": {
            "copyright_status": "owned",
            "status_allowed": True,
            "evidence_required": False,
            "evidence_present": False,
            "verified": True,
            "verification_basis": ["copyright_status_owned"],
            "rejection_codes": [],
            "sources": [],
        },
    }


def _materialized_entry(
    asset: ArtifactRef,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact_id": asset.id,
        "kind": asset.kind,
        "filename": asset.filename,
        "media_type": asset.media_type,
        "content_hash": asset.content_hash,
        "byte_size": asset.byte_size,
        "asset_id": candidate["asset_id"],
        "asset_file_id": candidate["asset_file_id"],
    }


def _pipeline_inputs(
    *, timing_estimated: bool = False, source_window_ms: int = 5_000
) -> tuple[
    MemoryStorage,
    StepContext,
    ArtifactRef,
    ArtifactRef,
    ArtifactRef,
    ArtifactRef,
    tuple[ArtifactRef, ArtifactRef],
    dict[str, Any],
]:
    storage = MemoryStorage()
    seed = _context()
    script_payload = {
        "title": "Timing contract",
        "narration": "First narrated beat. Second narrated beat.",
        "beats": [
            {
                "id": "beat-one",
                "sequence": 1,
                "narration": "First narrated beat.",
                "visual_description": "first matching visual",
            },
            {
                "id": "beat-two",
                "sequence": 2,
                "narration": "Second narrated beat.",
                "visual_description": "second matching visual",
            },
        ],
    }
    script = _publish_json(storage, seed, "script", script_payload)
    audio = storage.publish(
        seed,
        ProviderArtifact("audio", "narration.mp3", "audio/mpeg", b"ID3" + b"a" * 2048),
    )
    timing_payload = {
        "schema_version": "1.0.0",
        "operation": "audio.synthesize",
        "provider": "edge-tts",
        "source": "edge_word_boundary",
        "granularity": "word",
        "estimated": timing_estimated,
        "word_timing_estimated": False,
        "duration_seconds": 4.0,
        "audio_content_hash": audio.content_hash,
        "script_content_hash": script.content_hash,
        "words": [
            {
                "ordinal": 0,
                "text": "First",
                "start_seconds": 0.0,
                "end_seconds": 0.5,
                "estimated": False,
                "alignment_source": "native_word_boundary",
            }
        ],
        "segments": [
            {
                "id": "sentence-001",
                "sequence": 1,
                "text": script_payload["narration"],
                "start_seconds": 0.0,
                "end_seconds": 4.0,
                "estimated": timing_estimated,
                "alignment_source": (
                    "word_boundary_proportional_estimate"
                    if timing_estimated
                    else "native_word_aggregation"
                ),
            }
        ],
        "beats": [
            {
                "id": "beat-one",
                "sequence": 1,
                "text": "First narrated beat.",
                "start_seconds": 0.0,
                "end_seconds": 2.0,
                "estimated": timing_estimated,
                "alignment_source": (
                    "word_boundary_proportional_estimate"
                    if timing_estimated
                    else "native_word_aggregation"
                ),
            },
            {
                "id": "beat-two",
                "sequence": 2,
                "text": "Second narrated beat.",
                "start_seconds": 2.0,
                "end_seconds": 4.0,
                "estimated": timing_estimated,
                "alignment_source": (
                    "word_boundary_proportional_estimate"
                    if timing_estimated
                    else "native_word_aggregation"
                ),
            },
        ],
    }
    timing = _publish_json(storage, seed, "narration_timing", timing_payload)
    assets = (
        storage.publish(
            seed,
            ProviderArtifact("asset", "first.mp4", "video/mp4", b"video-one"),
        ),
        storage.publish(
            seed,
            ProviderArtifact("asset", "second.mp4", "video/mp4", b"video-two"),
        ),
    )
    candidates = (
        _candidate(assets[0], rank=1, start_ms=1_000, end_ms=1_000 + source_window_ms),
        _candidate(assets[1], rank=1, start_ms=2_000, end_ms=2_000 + source_window_ms),
    )
    manifest_payload = {
        "schema_version": "1.0.0",
        "operation": "media.retrieve",
        "provider": "database-asset-library",
        "catalog_scope": "local",
        "local_catalog_only": True,
        "source_script_artifact_id": script.id,
        "coverage": {
            "status": "complete",
            "total_beats": 2,
            "covered_beats": 2,
            "missing_beat_ids": [],
        },
        "rights_status": "verified",
        "beats": [
            {
                **script_payload["beats"][0],
                "coverage_status": "covered",
                "candidates": [candidates[0]],
            },
            {
                **script_payload["beats"][1],
                "coverage_status": "covered",
                "candidates": [candidates[1]],
            },
        ],
        "materialized_assets": [
            _materialized_entry(asset, candidate)
            for asset, candidate in zip(assets, candidates, strict=True)
        ],
    }
    manifest = _publish_json(storage, seed, "candidate_manifest", manifest_payload)
    snapshot = {
        "_framefactory": {
            "composition_snapshot": {"production_settings": {"frame_rate": 25}}
        }
    }
    return (
        storage,
        _context(script, audio, timing, manifest, *assets, input_snapshot=snapshot),
        script,
        audio,
        timing,
        manifest,
        assets,
        manifest_payload,
    )


def _generated_pipeline_inputs(
    *,
    audit_mutator: Callable[[dict[str, Any]], None] | None = None,
    manifest_mutator: Callable[[dict[str, Any]], None] | None = None,
    snapshot_mutator: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[MemoryStorage, StepContext]:
    (
        storage,
        base_context,
        script,
        audio,
        timing,
        _retrieval_manifest,
        assets,
        retrieval_payload,
    ) = _pipeline_inputs()
    terms = {
        "ref": "https://runwayml.com/terms-and-conditions/",
        "content_hash": "a" * 64,
        "captured_at": "2026-08-23T00:00:00Z",
    }
    pricing = {
        "ref": "https://runwayml.com/pricing/",
        "content_hash": "b" * 64,
        "captured_at": "2026-08-23T00:00:00Z",
        "currency": "USD",
        "credit_unit_minor": 1,
        "cost_per_second_minor": 12,
    }
    rights = {
        "copyright_status": "licensed",
        "status_allowed": True,
        "evidence_required": True,
        "evidence_present": True,
        "verified": True,
        "verification_basis": ["operator_confirmed_provider_terms_snapshot"],
        "rejection_codes": [],
        "sources": [
            {
                "evidence_type": "operator_confirmed_provider_terms",
                "license": "operator-confirmed-provider-terms",
                "locator": terms["ref"],
                "verified_at": terms["captured_at"],
                "content_hash": terms["content_hash"],
                "metadata": {
                    "output_rights_confirmed": True,
                    "output_rights_license_basis": (
                        "operator-confirmed-provider-terms"
                    ),
                    "terms_content_hash": terms["content_hash"],
                },
            }
        ],
    }
    manifest_payload = copy.deepcopy(retrieval_payload)
    manifest_payload.update(
        {
            "operation": "media.generate",
            "provider": "runway",
            "catalog_scope": "run",
            "local_catalog_only": False,
        }
    )
    candidates = [
        beat["candidates"][0] for beat in manifest_payload["beats"]
    ]
    for candidate in candidates:
        candidate["source_duration_ms"] = 5_000
        candidate["source_window"] = {
            "start_ms": 0,
            "end_ms": 5_000,
            "duration_ms": 5_000,
        }
        candidate["rights_evidence"] = copy.deepcopy(rights)

    jobs: list[dict[str, Any]] = []
    for index, (beat, candidate, asset) in enumerate(
        zip(manifest_payload["beats"], candidates, assets, strict=True),
        start=1,
    ):
        jobs.append(
            {
                "beat_id": beat["id"],
                "beat_sequence": beat["sequence"],
                "variant": 1,
                "paid_operation_id": f"paid-operation-{index}",
                "operation_key": f"media.generate:beat:{index}:variant:0",
                "request_hash": f"request-{index}",
                "provider_task_id": f"provider-task-{index}",
                "status": "succeeded",
                "model_id": "gen4.5",
                "output_artifact_id": asset.id,
                "output_content_hash": asset.content_hash,
                "output_filename": asset.filename,
                "output_media_type": asset.media_type,
                "output_byte_size": asset.byte_size,
                "estimated_cost_credits": 60,
                "incurred_cost_credits": 60,
                "verification_status": "accepted",
                "rejection_codes": [],
                "safety": {
                    "status": "passed",
                    "provider_moderation": "not_rejected",
                    "failure_code": None,
                },
                "visual_verification": {
                    "confidence": 0.95,
                    "minimum_confidence": 0.75,
                    "quality_score": 0.9,
                    "minimum_quality_score": 0.65,
                    "visual_description_matched": True,
                    "visual_description_evidence": "The generated scene matches the Beat.",
                    "must_match": [],
                    "must_not_match": [],
                    "has_watermark": False,
                    "has_embedded_text": False,
                    "unsafe": False,
                    "adult": False,
                    "violence": False,
                    "self_harm": False,
                    "hate_or_extremism": False,
                    "illegal_activity": False,
                    "recognizable_real_person_or_public_figure": False,
                    "brand_or_logo": False,
                    "protected_character": False,
                    "quality_usable": True,
                    "cut_safe": True,
                    "cut_safe_evidence": "The clip boundaries are visually safe.",
                    "semantic_complete": True,
                },
            }
        )
    for index, beat in enumerate(manifest_payload["beats"], start=1):
        rejected_bytes = f"rejected-generated-video-{index}".encode()
        jobs.append(
            {
                "beat_id": beat["id"],
                "beat_sequence": beat["sequence"],
                "variant": 2,
                "paid_operation_id": f"paid-operation-rejected-{index}",
                "operation_key": f"media.generate:beat:{index}:variant:1",
                "request_hash": f"request-rejected-{index}",
                "provider_task_id": f"provider-task-rejected-{index}",
                "status": "succeeded",
                "model_id": "gen4.5",
                "output_artifact_id": None,
                "output_content_hash": hashlib.sha256(rejected_bytes).hexdigest(),
                "output_filename": f"rejected-{index}.mp4",
                "output_media_type": "video/mp4",
                "output_byte_size": len(rejected_bytes),
                "estimated_cost_credits": 60,
                "incurred_cost_credits": 60,
                "verification_status": "rejected",
                "rejection_codes": ["watermark_detected"],
            }
        )
    audit_payload = {
        "schema_version": "1.0.0",
        "operation": "media.generate",
        "generated_only": True,
        "provider": "runway",
        "model_id": "gen4.5",
        "source_script_artifact_id": script.id,
        "source_audio_artifact_id": audio.id,
        "source_narration_timing_artifact_id": timing.id,
        "plan": {
            "target_duration_seconds": 30,
            "scene_count": 2,
            "variants_per_beat": 2,
            "candidate_count": 4,
            "clip_duration_seconds": 5,
            "billable_seconds": 20,
            "max_cost_minor": 240,
            "ratio": "1280:720",
        },
        "continuity_mode": "prompt_pack",
        "terms_snapshot": copy.deepcopy(terms),
        "rights_snapshot": {
            "output_rights_confirmed": True,
            "output_rights_license_basis": "operator-confirmed-provider-terms",
            "terms_content_hash": terms["content_hash"],
        },
        "pricing_snapshot": copy.deepcopy(pricing),
        "safety": {
            "provider_moderation": "required",
            "accepted_outputs": 2,
            "rejected_outputs": 2,
        },
        "total_cost": {
            "estimated_credits": 240,
            "incurred_credits": 240,
            "incurred_amount_minor": 240,
            "authorized_amount_minor": 240,
            "currency": "USD",
        },
        "jobs": jobs,
    }
    if audit_mutator is not None:
        audit_mutator(audit_payload)
    audit = _publish_json(
        storage,
        _context(),
        "generated_material_manifest",
        audit_payload,
    )
    manifest_payload["generation"] = {
        "mode": "generated_only",
        "continuity_mode": "prompt_pack",
        "generated_material_manifest_artifact_id": audit.id,
        "generated_material_manifest_content_hash": audit.content_hash,
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest_payload)
    manifest = _publish_json(
        storage,
        _context(),
        "candidate_manifest",
        manifest_payload,
    )
    snapshot = base_context.input_snapshot.to_dict()
    snapshot.update(
        {
            "visual_source_mode": "generated_only",
            "ai_disclosure": True,
        }
    )
    composition = snapshot["_framefactory"]["composition_snapshot"]
    composition.update(
        {
            "visual_source_mode": "generated_only",
            "full_ai_generation": {
                "provider_name": "runway",
                "model_id": "gen4.5",
                "ratio": "1280:720",
                "variants_per_beat": 2,
                "target_duration_seconds": 30,
                "scene_count": 2,
                "candidate_count": 4,
                "billable_seconds": 20,
                "max_cost_minor": 240,
                "continuity_mode": "prompt_pack",
                "terms_snapshot": copy.deepcopy(terms),
                "pricing_snapshot": copy.deepcopy(pricing),
                "output_rights_confirmed": True,
                "output_rights_license_basis": "operator-confirmed-provider-terms",
            },
        }
    )
    if snapshot_mutator is not None:
        snapshot_mutator(snapshot)
    return storage, _context(
        script,
        audio,
        timing,
        audit,
        manifest,
        *assets,
        input_snapshot=snapshot,
    )


class EvidenceVerificationTests(unittest.TestCase):
    def test_hard_constraints_are_recomputed_from_candidate_text(self) -> None:
        beat = {"must_match": ["blue sky"], "must_not_match": ["watermark"]}
        candidate = {
            "title": "mountain trail",
            "description": "walking outdoors",
            "source_transcript": "",
            "labels": [],
        }
        forged = {
            "passed": True,
            "must_match": [{"term": "blue sky", "matched": True}],
            "must_not_match": [{"term": "watermark", "matched": False}],
        }

        self.assertFalse(hard_constraint_evidence_valid(beat, candidate, forged))

    def test_rights_verification_requires_real_verified_license_evidence(self) -> None:
        forged = {
            "copyright_status": "licensed",
            "status_allowed": True,
            "evidence_required": True,
            "evidence_present": True,
            "verified": True,
            "verification_basis": ["verified_license_evidence"],
            "rejection_codes": [],
            "sources": [{"license": "commercial license", "provider": "unknown"}],
        }

        self.assertFalse(rights_evidence_valid(forged))


class NarrationTimingTests(unittest.IsolatedAsyncioTestCase):
    def test_sentence_count_over_schema_limit_fails_closed(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "10000 segment"):
            _narration_timing_payload(
                {"narration": "甲。" * 10_001, "scenes": []},
                duration=100.0,
                word_boundaries=None,
                audio_content_hash="a" * 64,
                script_content_hash="b" * 64,
            )

    def test_sentence_count_at_schema_limit_is_preserved(self) -> None:
        payload = _narration_timing_payload(
            {"narration": "甲。" * 10_000, "scenes": []},
            duration=100.0,
            word_boundaries=None,
            audio_content_hash="a" * 64,
            script_content_hash="b" * 64,
        )

        self.assertEqual(10_000, len(payload["segments"]))

    def test_word_interval_limit_preserves_boundary_and_fails_above_it(self) -> None:
        def boundaries(count: int) -> tuple[dict[str, Any], ...]:
            return tuple(
                {
                    "text": "x",
                    "start_seconds": float(index),
                    "end_seconds": index + 0.5,
                }
                for index in range(count)
            )

        payload = _narration_timing_payload(
            {"narration": "x", "scenes": []},
            duration=10_000.0,
            word_boundaries=boundaries(10_000),
            audio_content_hash="a" * 64,
            script_content_hash="b" * 64,
        )
        self.assertEqual(10_000, len(payload["words"]))

        with self.assertRaisesRegex(PermanentStepError, "10000 word"):
            _narration_timing_payload(
                {"narration": "x", "scenes": []},
                duration=10_001.0,
                word_boundaries=boundaries(10_001),
                audio_content_hash="a" * 64,
                script_content_hash="b" * 64,
            )

    def test_sub_microsecond_duration_fails_before_schema_rounding(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "duration rounds"):
            _narration_timing_payload(
                {"narration": "甲。", "scenes": []},
                duration=0.0000001,
                word_boundaries=None,
                audio_content_hash="a" * 64,
                script_content_hash="b" * 64,
            )

    def test_native_word_that_rounds_to_zero_interval_is_discarded(self) -> None:
        payload = _narration_timing_payload(
            {"narration": "word", "scenes": []},
            duration=1.0,
            word_boundaries=(
                {
                    "text": "word",
                    "start_seconds": 0.0,
                    "end_seconds": 0.0000001,
                },
            ),
            audio_content_hash="a" * 64,
            script_content_hash="b" * 64,
        )

        self.assertEqual([], payload["words"])
        self.assertTrue(payload["word_timing_estimated"])
        self.assertGreater(payload["segments"][0]["end_seconds"], 0)

    def test_timing_unit_that_collapses_after_rounding_fails_closed(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "non-positive interval"):
            _narration_timing_payload(
                {"narration": "甲。乙。", "scenes": []},
                duration=0.000001,
                word_boundaries=None,
                audio_content_hash="a" * 64,
                script_content_hash="b" * 64,
            )

    def test_sentence_alignment_reserves_distinct_native_word_boundaries(self) -> None:
        payload = _narration_timing_payload(
            {"narration": "甲。乙。丙。丁。", "scenes": []},
            duration=10.0,
            word_boundaries=(
                {"text": "one", "start_seconds": 0.0, "end_seconds": 0.2},
                {"text": "two", "start_seconds": 4.0, "end_seconds": 4.2},
                {"text": "three", "start_seconds": 8.0, "end_seconds": 8.2},
                {"text": "four", "start_seconds": 9.0, "end_seconds": 9.2},
            ),
            audio_content_hash="a" * 64,
            script_content_hash="b" * 64,
        )

        self.assertEqual([0.0, 4.0, 8.0, 9.0], [
            segment["start_seconds"] for segment in payload["segments"]
        ])
        self.assertTrue(all(
            segment["end_seconds"] > segment["start_seconds"]
            for segment in payload["segments"]
        ))

    async def test_missing_boundaries_publish_deterministic_estimated_sentence_timing(
        self,
    ) -> None:
        storage = MemoryStorage()
        seed = _context()
        script = _publish_json(
            storage,
            seed,
            "script",
            {"narration": "第一句。第二句话。", "scenes": []},
        )

        async def synthesize(
            _text: str, output: Path, _voice: str, _rate: str
        ) -> None:
            output.write_bytes(b"ID3" + b"a" * 2048)

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 4.0

        capability = EdgeSpeechCapability(
            LegacyMediaSettings(),
            storage,
            synthesizer=synthesize,
            duration_probe=duration_probe,
        )
        first = await capability.execute(_context(script))
        second = await capability.execute(_context(script))
        first_timing = storage.read_json(first.artifacts[1])
        second_timing = storage.read_json(second.artifacts[1])

        self.assertEqual(["audio", "narration_timing"], [item.kind for item in first.artifacts])
        self.assertEqual(first_timing, second_timing)
        self.assertEqual(first.artifacts[1].content_hash, second.artifacts[1].content_hash)
        self.assertTrue(first.requires_review)
        self.assertTrue(first_timing["estimated"])
        self.assertTrue(first_timing["word_timing_estimated"])
        self.assertEqual([], first_timing["words"])
        self.assertEqual(2, len(first_timing["segments"]))
        self.assertTrue(all(item["estimated"] for item in first_timing["segments"]))
        self.assertEqual(0.0, first_timing["segments"][0]["start_seconds"])
        self.assertEqual(4.0, first_timing["segments"][-1]["end_seconds"])

    async def test_exact_native_word_aggregation_is_not_mislabeled_estimated(
        self,
    ) -> None:
        storage = MemoryStorage()
        seed = _context()
        script = _publish_json(
            storage,
            seed,
            "script",
            {"narration": "Hello world.", "scenes": []},
        )

        async def synthesize(
            _text: str, output: Path, _voice: str, _rate: str
        ) -> tuple[dict[str, Any], ...]:
            output.write_bytes(b"ID3" + b"a" * 2048)
            return (
                {"text": "Hello", "start_seconds": 0.0, "end_seconds": 0.7},
                {"text": "world", "start_seconds": 0.8, "end_seconds": 1.5},
            )

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 1.5

        result = await EdgeSpeechCapability(
            LegacyMediaSettings(),
            storage,
            synthesizer=synthesize,
            duration_probe=duration_probe,
        ).execute(_context(script))
        timing = storage.read_json(result.artifacts[1])

        self.assertFalse(result.requires_review)
        self.assertFalse(timing["estimated"])
        self.assertFalse(timing["word_timing_estimated"])
        self.assertTrue(all(not item["estimated"] for item in timing["words"]))
        self.assertEqual("native_word_aggregation", timing["segments"][0]["alignment_source"])

    async def test_repaired_beat_partition_uses_exact_native_word_boundaries(self) -> None:
        storage = MemoryStorage()
        seed = _context()
        narration = "Alpha beta. Gamma delta."
        script = _publish_json(
            storage,
            seed,
            "script",
            {
                "narration": narration,
                "beats": [
                    {
                        "id": "first",
                        "sequence": 1,
                        "narration": narration,
                        "visual_description": "first visual",
                    },
                    {
                        "id": "second",
                        "sequence": 2,
                        "narration": narration,
                        "visual_description": "second visual",
                    },
                ],
            },
        )

        async def synthesize(
            _text: str, output: Path, _voice: str, _rate: str
        ) -> tuple[dict[str, Any], ...]:
            output.write_bytes(b"ID3" + b"a" * 2048)
            return (
                {"text": "Alpha", "start_seconds": 0.0, "end_seconds": 0.5},
                {"text": "beta", "start_seconds": 0.6, "end_seconds": 1.0},
                {"text": "Gamma", "start_seconds": 1.1, "end_seconds": 1.6},
                {"text": "delta", "start_seconds": 1.7, "end_seconds": 2.2},
            )

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 2.2

        result = await EdgeSpeechCapability(
            LegacyMediaSettings(),
            storage,
            synthesizer=synthesize,
            duration_probe=duration_probe,
        ).execute(_context(script))
        timing = storage.read_json(result.artifacts[1])

        self.assertFalse(result.requires_review)
        self.assertFalse(timing["estimated"])
        self.assertEqual(
            ["Alpha beta.", "Gamma delta."],
            [beat["text"] for beat in timing["beats"]],
        )
        self.assertTrue(all(not beat["estimated"] for beat in timing["beats"]))
        self.assertTrue(
            all(
                beat["alignment_source"] == "native_word_aggregation"
                for beat in timing["beats"]
            )
        )

    async def test_native_word_mismatch_remains_estimated_and_requires_review(self) -> None:
        storage = MemoryStorage()
        seed = _context()
        script = _publish_json(
            storage,
            seed,
            "script",
            {
                "narration": "Alpha beta. Gamma delta.",
                "beats": [
                    {
                        "id": "first",
                        "sequence": 1,
                        "narration": "Alpha beta.",
                        "visual_description": "first visual",
                    },
                    {
                        "id": "second",
                        "sequence": 2,
                        "narration": "Gamma delta.",
                        "visual_description": "second visual",
                    },
                ],
            },
        )

        async def synthesize(
            _text: str, output: Path, _voice: str, _rate: str
        ) -> tuple[dict[str, Any], ...]:
            output.write_bytes(b"ID3" + b"a" * 2048)
            return (
                {"text": "Alpha", "start_seconds": 0.0, "end_seconds": 0.5},
                {"text": "wrong", "start_seconds": 0.6, "end_seconds": 1.0},
                {"text": "Gamma", "start_seconds": 1.1, "end_seconds": 1.6},
                {"text": "delta", "start_seconds": 1.7, "end_seconds": 2.2},
            )

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 2.2

        result = await EdgeSpeechCapability(
            LegacyMediaSettings(),
            storage,
            synthesizer=synthesize,
            duration_probe=duration_probe,
        ).execute(_context(script))
        timing = storage.read_json(result.artifacts[1])

        self.assertTrue(result.requires_review)
        self.assertTrue(timing["estimated"])
        self.assertTrue(all(beat["estimated"] for beat in timing["beats"]))
        self.assertTrue(
            all(
                beat["alignment_source"] == "word_boundary_proportional_estimate"
                for beat in timing["beats"]
            )
        )

    async def test_edge_stream_explicitly_requests_native_word_boundaries(self) -> None:
        storage = MemoryStorage()
        seed = _context()
        script = _publish_json(
            storage,
            seed,
            "script",
            {"narration": "Hello world.", "scenes": []},
        )
        constructor: dict[str, Any] = {}

        class FakeCommunicate:
            def __init__(
                self,
                text: str,
                voice: str,
                *,
                rate: str,
                boundary: str,
            ) -> None:
                constructor.update(
                    text=text,
                    voice=voice,
                    rate=rate,
                    boundary=boundary,
                )

            async def stream(self):  # type: ignore[no-untyped-def]
                yield {"type": "audio", "data": b"ID3" + b"a" * 2048}
                yield {
                    "type": "WordBoundary",
                    "text": "Hello",
                    "offset": 0,
                    "duration": 7_000_000,
                }
                yield {
                    "type": "WordBoundary",
                    "text": "world",
                    "offset": 8_000_000,
                    "duration": 7_000_000,
                }

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 1.5

        with patch.dict(
            sys.modules,
            {"edge_tts": SimpleNamespace(Communicate=FakeCommunicate)},
        ):
            result = await EdgeSpeechCapability(
                LegacyMediaSettings(),
                storage,
                duration_probe=duration_probe,
            ).execute(_context(script))
        timing = storage.read_json(result.artifacts[1])

        self.assertEqual("WordBoundary", constructor["boundary"])
        self.assertEqual("Hello world.", constructor["text"])
        self.assertFalse(result.requires_review)
        self.assertFalse(timing["estimated"])
        self.assertEqual([0.0, 0.8], [word["start_seconds"] for word in timing["words"]])

    async def test_timing_and_retrieval_share_normalized_beat_identity(self) -> None:
        storage = MemoryStorage()
        seed = _context()
        script_payload = {
            "narration": "Earlier beat. Later beat.",
            "beats": [
                {
                    "id": "duplicate-id",
                    "sequence": 20,
                    "narration": "Later beat.",
                    "visual_description": "later visual",
                },
                {
                    "id": "duplicate-id",
                    "sequence": 5,
                    "narration": "Earlier beat.",
                    "visual_description": "earlier visual",
                },
            ],
        }
        script = _publish_json(storage, seed, "script", script_payload)

        async def synthesize(
            _text: str, output: Path, _voice: str, _rate: str
        ) -> None:
            output.write_bytes(b"ID3" + b"a" * 2048)

        async def duration_probe(_path: Path, _context: StepContext) -> float:
            return 4.0

        result = await EdgeSpeechCapability(
            LegacyMediaSettings(),
            storage,
            synthesizer=synthesize,
            duration_probe=duration_probe,
        ).execute(_context(script))
        timing = storage.read_json(result.artifacts[1])
        expected = normalize_beats(script_payload)

        self.assertEqual(
            [(beat.id, beat.sequence, beat.narration) for beat in expected],
            [
                (beat["id"], beat["sequence"], beat["text"])
                for beat in timing["beats"]
            ],
        )
        self.assertEqual([1, 2], [beat["sequence"] for beat in timing["beats"]])
        self.assertNotEqual(timing["beats"][0]["id"], timing["beats"][1]["id"])


class TimelineContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_editorial_fallback_uses_the_normal_timeline_contract(self) -> None:
        storage, _base, script, audio, timing, _manifest, _assets, _payload = (
            _pipeline_inputs()
        )
        script_payload = storage.read_json(script)
        retrieval_context = _context(
            script,
            input_snapshot={
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {
                            "no_asset_draft": {"enabled": True}
                        }
                    }
                }
            },
            step_id="retrieve",
        )
        fallback = build_editorial_fallback(
            retrieval_context,
            storage,
            script_artifact=script,
            beats=normalize_beats(script_payload),
            reason="no_selected_asset_library",
        )
        manifest = next(
            artifact
            for artifact in fallback.artifacts
            if artifact.kind == "candidate_manifest"
        )
        assets = tuple(
            artifact for artifact in fallback.artifacts if artifact.kind == "asset"
        )
        timeline_context = _context(
            script,
            audio,
            timing,
            manifest,
            *assets,
            input_snapshot={
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"frame_rate": 25}
                    }
                }
            },
            step_id="timeline",
        )

        result = await TimelineCapability(storage).execute(timeline_context)

        self.assertFalse(result.requires_review)
        self.assertEqual(2, result.output_summary["selected_assets"])
        self.assertEqual(
            ["material_selection", "timeline"],
            [artifact.kind for artifact in result.artifacts],
        )

    def test_strict_partition_is_stable_for_fractional_shot_lengths(self) -> None:
        shots = _plan_timing_unit(
            {
                "text": "A fractional-duration narration unit.",
                "start_seconds": 0.0,
                "end_seconds": 9.7,
            },
            {
                "id": "beat-fractional",
                "sequence": 1,
                "visual_description": "matching still",
            },
            [
                {
                    "selection_id": "selection-fractional",
                    "artifact_id": str(uuid4()),
                    "media_kind": "image",
                    "selected_for_scene": "matching still",
                    "description": "matching still",
                    "labels": ["matching"],
                    "cut_safe": True,
                    "semantic_complete": True,
                }
            ],
        )

        self.assertEqual(3, len(shots))
        self.assertAlmostEqual(9.7, sum(shot["duration_seconds"] for shot in shots), 5)

    def test_one_beat_can_preserve_two_final_candidate_lineages(self) -> None:
        candidates = [
            {
                "selection_id": f"selection-{suffix}",
                "artifact_id": artifact_id,
                "media_kind": "image",
                "selected_for_scene": "matching still",
                "description": "matching still",
                "labels": ["matching"],
                "cut_safe": True,
                "semantic_complete": True,
            }
            for suffix, artifact_id in (
                ("a" * 20, "11111111-1111-4111-8111-111111111111"),
                ("b" * 20, "22222222-2222-4222-8222-222222222222"),
            )
        ]

        shots = _plan_timing_unit(
            {
                "text": "One Beat with two final visual shots.",
                "start_seconds": 0.0,
                "end_seconds": 5.0,
            },
            {
                "id": "beat-two-candidates",
                "sequence": 1,
                "visual_description": "matching still",
            },
            candidates,
        )

        self.assertEqual(2, len(shots))
        self.assertEqual(
            {candidate["selection_id"] for candidate in candidates},
            {shot["selection_id"] for shot in shots},
        )
        self.assertEqual(
            {
                candidate["selection_id"]: candidate["artifact_id"]
                for candidate in candidates
            },
            {shot["selection_id"]: shot["artifact_id"] for shot in shots},
        )

    def test_ten_thousand_beats_share_one_global_edl_shot_budget(self) -> None:
        total = 0
        for _ in range(10_000):
            total = _reserve_shot_budget(total, 10)
        self.assertEqual(100_000, total)

        with self.assertRaisesRegex(PermanentStepError, "100000 EDL shot"):
            _reserve_shot_budget(total, 1)

    def test_sub_microsecond_timing_fails_before_zero_rounding(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "too short"):
            _plan_timing_unit(
                {
                    "text": "Too short.",
                    "start_seconds": 0.0,
                    "end_seconds": 0.0000001,
                },
                {"id": "beat-too-short", "sequence": 1},
                [
                    {
                        "selection_id": f"selection-{'c' * 20}",
                        "artifact_id": "33333333-3333-4333-8333-333333333333",
                        "media_kind": "image",
                    }
                ],
            )

    async def test_timeline_consumes_nested_retrieval_evidence_and_stable_coordinates(
        self,
    ) -> None:
        storage, context, _script, audio, timing, _manifest, assets, _payload = (
            _pipeline_inputs()
        )
        result = await TimelineCapability(storage).execute(context)
        selection = storage.read_json(result.artifacts[0])
        timeline = storage.read_json(result.artifacts[1])

        self.assertFalse(result.requires_review)
        self.assertEqual(["material_selection", "timeline"], [item.kind for item in result.artifacts])
        self.assertEqual({"num": 25, "den": 1}, timeline["output_frame_rate"])
        self.assertEqual(100, timeline["shots"][-1]["timeline_end_frame"])
        self.assertTrue(all(shot["padding_seconds"] == 0 for shot in timeline["shots"]))
        self.assertTrue(all(shot["loop"] is False for shot in timeline["shots"]))
        self.assertTrue(
            all(isinstance(shot["source_start_ms"], int) for shot in timeline["shots"])
        )
        self.assertTrue(
            all(shot["cut_evidence"] == "semantic_safe" for shot in timeline["shots"])
        )
        validated = validate_edl(
            selection,
            timeline,
            {asset.id: asset for asset in assets},
            audio_ref=audio,
        )
        self.assertEqual(len(timeline["shots"]), len(validated))
        self.assertEqual(timing.content_hash, timeline["narration_timing_content_hash"])

    async def test_timeline_truncates_title_to_edl_schema_limit(self) -> None:
        storage, context, script, audio, timing, _manifest, assets, payload = (
            _pipeline_inputs()
        )
        script_payload = storage.read_json(script)
        script_payload["title"] = "题" * 1_001
        bounded_script = _publish_json(storage, _context(), "script", script_payload)

        timing_payload = storage.read_json(timing)
        timing_payload["script_content_hash"] = bounded_script.content_hash
        bounded_timing = _publish_json(
            storage,
            _context(),
            "narration_timing",
            timing_payload,
        )
        manifest_payload = copy.deepcopy(payload)
        manifest_payload["source_script_artifact_id"] = bounded_script.id
        bounded_manifest = _publish_json(
            storage,
            _context(),
            "candidate_manifest",
            manifest_payload,
        )

        result = await TimelineCapability(storage).execute(
            _context(
                bounded_script,
                audio,
                bounded_timing,
                bounded_manifest,
                *assets,
                input_snapshot=context.input_snapshot.to_dict(),
            )
        )
        timeline = storage.read_json(result.artifacts[1])

        self.assertEqual("题" * 1_000, timeline["title"])

    async def test_consumed_estimated_beat_requires_review_even_with_native_words(
        self,
    ) -> None:
        storage, context, *_rest = _pipeline_inputs(timing_estimated=True)
        result = await TimelineCapability(storage).execute(context)
        timeline = storage.read_json(result.artifacts[1])

        self.assertTrue(result.requires_review)
        self.assertTrue(timeline["timing_estimated"])

    async def test_incomplete_manifest_is_a_hard_gate(self) -> None:
        storage, context, script, audio, timing, _manifest, assets, payload = (
            _pipeline_inputs()
        )
        payload["coverage"]["status"] = "requires_review"
        payload["coverage"]["missing_beat_ids"] = ["beat-two"]
        manifest = _publish_json(storage, _context(), "candidate_manifest", payload)
        rejected = _context(
            script,
            audio,
            timing,
            manifest,
            *assets,
            input_snapshot=context.input_snapshot.to_dict(),
        )

        with self.assertRaisesRegex(PermanentStepError, "coverage is incomplete"):
            await TimelineCapability(storage).execute(rejected)

    async def test_candidate_evidence_is_fail_closed(self) -> None:
        mutations = {
            "eligible": lambda item: item.__setitem__("eligible", False),
            "materialized": lambda item: item.__setitem__("materialized", False),
            "constraints": lambda item: item["hard_constraints"].__setitem__(
                "passed", False
            ),
            "rights": lambda item: item["rights_evidence"].__setitem__(
                "status_allowed", False
            ),
            "rights verification": lambda item: item["rights_evidence"].__setitem__(
                "verified", False
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(evidence=label):
                storage, context, script, audio, timing, _manifest, assets, payload = (
                    _pipeline_inputs()
                )
                changed = copy.deepcopy(payload)
                mutate(changed["beats"][0]["candidates"][0])
                manifest = _publish_json(
                    storage, _context(), "candidate_manifest", changed
                )
                rejected = _context(
                    script,
                    audio,
                    timing,
                    manifest,
                    *assets,
                    input_snapshot=context.input_snapshot.to_dict(),
                )
                with self.assertRaisesRegex(
                    PermanentStepError, "no materialized candidate"
                ):
                    await TimelineCapability(storage).execute(rejected)

    async def test_candidate_lineage_and_catalog_duration_are_fail_closed(self) -> None:
        mutations = {
            "content hash": lambda item: item.__setitem__("content_hash", "0" * 64),
            "source duration": lambda item: item.__setitem__(
                "source_duration_ms", item["source_window"]["end_ms"] - 1
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(evidence=label):
                storage, context, script, audio, timing, _manifest, assets, payload = (
                    _pipeline_inputs()
                )
                changed = copy.deepcopy(payload)
                mutate(changed["beats"][0]["candidates"][0])
                manifest = _publish_json(
                    storage, _context(), "candidate_manifest", changed
                )
                rejected = _context(
                    script,
                    audio,
                    timing,
                    manifest,
                    *assets,
                    input_snapshot=context.input_snapshot.to_dict(),
                )
                with self.assertRaises(PermanentStepError):
                    await TimelineCapability(storage).execute(rejected)

    async def test_video_shorter_than_every_safe_interval_is_rejected(self) -> None:
        storage, context, *_rest = _pipeline_inputs(source_window_ms=100)
        with self.assertRaisesRegex(PermanentStepError, "shorter than every safe EDL"):
            await TimelineCapability(storage).execute(context)

    async def test_timing_must_cover_each_manifest_beat_once(self) -> None:
        storage, context, script, audio, timing, manifest, assets, _payload = (
            _pipeline_inputs()
        )
        changed = storage.read_json(timing)
        changed["beats"] = changed["beats"][:1]
        changed["beats"][0]["end_seconds"] = changed["duration_seconds"]
        incomplete_timing = _publish_json(
            storage,
            _context(),
            "narration_timing",
            changed,
        )
        rejected = _context(
            script,
            audio,
            incomplete_timing,
            manifest,
            *assets,
            input_snapshot=context.input_snapshot.to_dict(),
        )

        with self.assertRaisesRegex(PermanentStepError, "every candidate Beat"):
            await TimelineCapability(storage).execute(rejected)

    async def test_consumed_rights_and_materialized_lineage_are_not_boolean_trust(self) -> None:
        mutations = {
            "rights": lambda payload: payload["beats"][0]["candidates"][0][
                "rights_evidence"
            ].__setitem__("copyright_status", "unknown"),
            "materialized": lambda payload: payload["materialized_assets"][0].__setitem__(
                "content_hash", "0" * 64
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(lineage=label):
                storage, context, script, audio, timing, _manifest, assets, payload = (
                    _pipeline_inputs()
                )
                changed = copy.deepcopy(payload)
                mutate(changed)
                manifest = _publish_json(
                    storage,
                    _context(),
                    "candidate_manifest",
                    changed,
                )
                rejected = _context(
                    script,
                    audio,
                    timing,
                    manifest,
                    *assets,
                    input_snapshot=context.input_snapshot.to_dict(),
                )
                with self.assertRaises(PermanentStepError):
                    await TimelineCapability(storage).execute(rejected)

    async def test_content_hash_dedup_allows_distinct_database_lineage_aliases(self) -> None:
        storage, context, script, audio, timing, _manifest, assets, payload = (
            _pipeline_inputs()
        )
        changed = copy.deepcopy(payload)
        first = changed["beats"][0]["candidates"][0]
        alias = changed["beats"][1]["candidates"][0]
        alias.update(
            {
                "artifact_id": assets[0].id,
                "artifact_filename": assets[0].filename,
                "content_hash": assets[0].content_hash,
                "byte_size": assets[0].byte_size,
                "media_type": assets[0].media_type,
            }
        )
        changed["materialized_assets"] = [_materialized_entry(assets[0], first)]
        manifest = _publish_json(
            storage,
            _context(),
            "candidate_manifest",
            changed,
        )
        result = await TimelineCapability(storage).execute(
            _context(
                script,
                audio,
                timing,
                manifest,
                *assets,
                input_snapshot=context.input_snapshot.to_dict(),
            )
        )
        selection = storage.read_json(result.artifacts[0])

        self.assertEqual(
            {assets[0].id},
            {item["artifact_id"] for item in selection["selections"]},
        )
        self.assertEqual(
            {"beat-one", "beat-two"},
            {item["beat_id"] for item in selection["selections"]},
        )


class GeneratedTimelineAuditTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_audit_rejected(
        self,
        *,
        audit_mutator: Callable[[dict[str, Any]], None] | None = None,
        manifest_mutator: Callable[[dict[str, Any]], None] | None = None,
        snapshot_mutator: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        storage, context = _generated_pipeline_inputs(
            audit_mutator=audit_mutator,
            manifest_mutator=manifest_mutator,
            snapshot_mutator=snapshot_mutator,
        )
        with self.assertRaisesRegex(PermanentStepError, "generated material audit"):
            await TimelineCapability(storage).execute(context)

    async def test_generated_timeline_consumes_only_audit_accepted_outputs(
        self,
    ) -> None:
        storage, context = _generated_pipeline_inputs()

        result = await TimelineCapability(storage).execute(context)
        selection = storage.read_json(result.artifacts[0])
        asset_ids = {
            artifact.id for artifact in context.input_artifacts if artifact.kind == "asset"
        }

        self.assertEqual(
            ["material_selection", "timeline"],
            [artifact.kind for artifact in result.artifacts],
        )
        self.assertEqual(
            asset_ids,
            {item["artifact_id"] for item in selection["selections"]},
        )

    async def test_generated_audit_header_lineage_is_fail_closed(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "operation": lambda value: value.__setitem__("operation", "media.retrieve"),
            "generated-only": lambda value: value.__setitem__("generated_only", False),
            "provider": lambda value: value.__setitem__("provider", "another-provider"),
            "model": lambda value: value.__setitem__("model_id", "another-model"),
            "script": lambda value: value.__setitem__(
                "source_script_artifact_id", str(uuid4())
            ),
            "audio": lambda value: value.__setitem__(
                "source_audio_artifact_id", str(uuid4())
            ),
            "timing": lambda value: value.__setitem__(
                "source_narration_timing_artifact_id", str(uuid4())
            ),
            "continuity": lambda value: value.__setitem__("continuity_mode", "none"),
        }
        for label, mutate in mutations.items():
            with self.subTest(lineage=label):
                await self._assert_audit_rejected(audit_mutator=mutate)

    async def test_paid_plan_and_variant_grid_are_complete(self) -> None:
        plan_mutations = {
            "target_duration_seconds": 31,
            "scene_count": 3,
            "variants_per_beat": 3,
            "candidate_count": 5,
            "billable_seconds": 25,
            "max_cost_minor": 241,
            "ratio": "720:1280",
        }
        for field, replacement in plan_mutations.items():
            with self.subTest(plan=field):
                await self._assert_audit_rejected(
                    audit_mutator=lambda value, field=field, replacement=replacement: value[
                        "plan"
                    ].__setitem__(field, replacement)
                )

        await self._assert_audit_rejected(
            audit_mutator=lambda value: value["jobs"].pop()
        )
        await self._assert_audit_rejected(
            audit_mutator=lambda value: value["jobs"][3].__setitem__("variant", 1)
        )

        def reduce_audit_budget(value: dict[str, Any]) -> None:
            value["plan"]["max_cost_minor"] = 239

        def reduce_frozen_budget(value: dict[str, Any]) -> None:
            value["_framefactory"]["composition_snapshot"]["full_ai_generation"][
                "max_cost_minor"
            ] = 239

        await self._assert_audit_rejected(
            audit_mutator=reduce_audit_budget,
            snapshot_mutator=reduce_frozen_budget,
        )

    async def test_pricing_snapshot_is_exactly_frozen(self) -> None:
        mutations = {
            "ref": "https://example.test/pricing",
            "content_hash": "0" * 64,
            "captured_at": "2026-08-24T00:00:00Z",
            "currency": "CNY",
            "credit_unit_minor": 2,
            "cost_per_second_minor": 13,
        }
        for field, replacement in mutations.items():
            with self.subTest(pricing=field):
                await self._assert_audit_rejected(
                    audit_mutator=lambda value, field=field, replacement=replacement: value[
                        "pricing_snapshot"
                    ].__setitem__(field, replacement)
                )

    async def test_cost_and_safety_totals_equal_the_job_sum(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "total currency": lambda value: value["total_cost"].__setitem__(
                "currency", "CNY"
            ),
            "authorized amount": lambda value: value["total_cost"].__setitem__(
                "authorized_amount_minor", 239
            ),
            "incurred amount": lambda value: value["total_cost"].__setitem__(
                "incurred_amount_minor", 239
            ),
            "estimated credits": lambda value: value["total_cost"].__setitem__(
                "estimated_credits", 239
            ),
            "incurred credits": lambda value: value["total_cost"].__setitem__(
                "incurred_credits", 239
            ),
            "job estimated cost": lambda value: value["jobs"][0].__setitem__(
                "estimated_cost_credits", 59
            ),
            "job incurred cost": lambda value: value["jobs"][0].__setitem__(
                "incurred_cost_credits", 61
            ),
            "accepted safety count": lambda value: value["safety"].__setitem__(
                "accepted_outputs", 1
            ),
            "rejected safety count": lambda value: value["safety"].__setitem__(
                "rejected_outputs", 1
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(total=label):
                await self._assert_audit_rejected(audit_mutator=mutate)

    async def test_accepted_job_verification_is_recomputed(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "provider moderation": lambda value: value["jobs"][0][
                "safety"
            ].__setitem__("provider_moderation", "rejected"),
            "unsafe": lambda value: value["jobs"][0]["visual_verification"].__setitem__(
                "unsafe", True
            ),
            "watermark": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("has_watermark", True),
            "protected character": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("protected_character", True),
            "quality": lambda value: value["jobs"][0]["visual_verification"].__setitem__(
                "quality_usable", False
            ),
            "visual mismatch": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("visual_description_matched", False),
            "low confidence": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("confidence", 0.5),
            "non-finite confidence": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("confidence", float("inf")),
            "low quality score": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("quality_score", 0.5),
            "missing visual evidence": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("visual_description_evidence", ""),
            "oversized cut evidence": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("cut_safe_evidence", "x" * 2_001),
            "false must-match": lambda value: value["jobs"][0][
                "visual_verification"
            ].__setitem__("must_match", [{"term": "required", "matched": False}]),
            "candidate constraint drift": lambda value: value["beats"][0][
                "candidates"
            ][0]["hard_constraints"].__setitem__(
                "must_match", [{"term": "forged", "matched": True}]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(verification=label):
                if label == "candidate constraint drift":
                    await self._assert_audit_rejected(manifest_mutator=mutate)
                else:
                    await self._assert_audit_rejected(audit_mutator=mutate)

    async def test_accepted_job_output_identity_is_cross_checked(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "Beat": lambda value: value["jobs"][0].__setitem__(
                "beat_id", "another-beat"
            ),
            "sequence": lambda value: value["jobs"][0].__setitem__(
                "beat_sequence", 2
            ),
            "artifact": lambda value: value["jobs"][0].__setitem__(
                "output_artifact_id", str(uuid4())
            ),
            "hash": lambda value: value["jobs"][0].__setitem__(
                "output_content_hash", "0" * 64
            ),
            "filename": lambda value: value["jobs"][0].__setitem__(
                "output_filename", "forged.mp4"
            ),
            "media type": lambda value: value["jobs"][0].__setitem__(
                "output_media_type", "video/webm"
            ),
            "byte size": lambda value: value["jobs"][0].__setitem__(
                "output_byte_size", value["jobs"][0]["output_byte_size"] + 1
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(output_identity=label):
                await self._assert_audit_rejected(audit_mutator=mutate)

    async def test_candidate_and_materialized_identity_match_accepted_job(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "candidate artifact": lambda value: value["beats"][0]["candidates"][
                0
            ].__setitem__("artifact_id", str(uuid4())),
            "candidate hash": lambda value: value["beats"][0]["candidates"][
                0
            ].__setitem__("content_hash", "0" * 64),
            "candidate filename": lambda value: value["beats"][0]["candidates"][
                0
            ].__setitem__("artifact_filename", "forged.mp4"),
            "candidate media type": lambda value: value["beats"][0]["candidates"][
                0
            ].__setitem__("media_type", "video/webm"),
            "candidate byte size": lambda value: value["beats"][0]["candidates"][
                0
            ].__setitem__("byte_size", 999),
            "materialized filename": lambda value: value["materialized_assets"][
                0
            ].__setitem__("filename", "forged.mp4"),
            "materialized asset": lambda value: value["materialized_assets"][
                0
            ].__setitem__("asset_id", str(uuid4())),
        }
        for label, mutate in mutations.items():
            with self.subTest(output_identity=label):
                await self._assert_audit_rejected(manifest_mutator=mutate)

    async def test_accepted_and_downstream_sets_are_bidirectional(self) -> None:
        def audit_mutation(value: dict[str, Any]) -> None:
            value["jobs"].pop(0)
        manifest_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "missing candidate": lambda value: value["beats"][0][
                "candidates"
            ].clear(),
            "missing materialized asset": lambda value: value[
                "materialized_assets"
            ].pop(0),
        }

        await self._assert_audit_rejected(audit_mutator=audit_mutation)
        for label, mutate in manifest_mutations.items():
            with self.subTest(direction=label):
                await self._assert_audit_rejected(manifest_mutator=mutate)

    async def test_rejected_output_cannot_enter_downstream_lineage(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "artifact lineage": lambda value: value["jobs"][2].__setitem__(
                "output_artifact_id", str(uuid4())
            ),
            "accepted byte lineage": lambda value: value["jobs"][2].__setitem__(
                "output_content_hash", value["jobs"][0]["output_content_hash"]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(leak=label):
                await self._assert_audit_rejected(audit_mutator=mutate)

    async def test_continuity_and_rights_terms_are_cross_checked(self) -> None:
        audit_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "terms hash": lambda value: value["terms_snapshot"].__setitem__(
                "content_hash", "0" * 64
            ),
            "rights confirmation": lambda value: value[
                "rights_snapshot"
            ].__setitem__("output_rights_confirmed", False),
            "rights basis": lambda value: value["rights_snapshot"].__setitem__(
                "output_rights_license_basis", "forged-basis"
            ),
            "rights terms hash": lambda value: value["rights_snapshot"].__setitem__(
                "terms_content_hash", "0" * 64
            ),
        }
        manifest_mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "provider": lambda value: value.__setitem__(
                "provider", "another-provider"
            ),
            "continuity": lambda value: value["generation"].__setitem__(
                "continuity_mode", "none"
            ),
            "rights locator": lambda value: value["beats"][0]["candidates"][0][
                "rights_evidence"
            ]["sources"][0].__setitem__("locator", "https://example.test/terms"),
            "rights hash": lambda value: value["beats"][0]["candidates"][0][
                "rights_evidence"
            ]["sources"][0].__setitem__("content_hash", "0" * 64),
            "rights capture": lambda value: value["beats"][0]["candidates"][0][
                "rights_evidence"
            ]["sources"][0].__setitem__("verified_at", "2026-08-24T00:00:00Z"),
            "rights metadata basis": lambda value: value["beats"][0][
                "candidates"
            ][0]["rights_evidence"]["sources"][0]["metadata"].__setitem__(
                "output_rights_license_basis", "forged-basis"
            ),
            "rights metadata terms": lambda value: value["beats"][0][
                "candidates"
            ][0]["rights_evidence"]["sources"][0]["metadata"].__setitem__(
                "terms_content_hash", "0" * 64
            ),
        }

        for label, mutate in audit_mutations.items():
            with self.subTest(audit_rights=label):
                await self._assert_audit_rejected(audit_mutator=mutate)
        for label, mutate in manifest_mutations.items():
            with self.subTest(evidence=label):
                await self._assert_audit_rejected(manifest_mutator=mutate)
        await self._assert_audit_rejected(
            snapshot_mutator=lambda value: value["_framefactory"][
                "composition_snapshot"
            ]["full_ai_generation"].__setitem__(
                "output_rights_license_basis", "another-basis"
            )
        )


class EdlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        (
            self.storage,
            context,
            _script,
            self.audio,
            self.timing,
            self.manifest,
            self.assets,
            _payload,
        ) = _pipeline_inputs()
        result = await TimelineCapability(self.storage).execute(context)
        self.selection = self.storage.read_json(result.artifacts[0])
        self.timeline = self.storage.read_json(result.artifacts[1])
        self.asset_refs = {asset.id: asset for asset in self.assets}

    def test_validator_rejects_video_shorter_than_edl(self) -> None:
        timeline = copy.deepcopy(self.timeline)
        shot = timeline["shots"][0]
        shot["source_end_ms"] = shot["source_start_ms"] + 1_999
        shot["source_end_seconds"] = shot["source_end_ms"] / 1000
        with self.assertRaisesRegex(EdlValidationError, "shorter than its EDL"):
            validate_edl(
                self.selection,
                timeline,
                self.asset_refs,
                audio_ref=self.audio,
            )

    def test_validator_cross_checks_shot_beat_and_media_identity(self) -> None:
        mutations = {
            "Beat": lambda shot: shot.__setitem__("beat_id", "another-beat"),
            "media": lambda shot: shot.__setitem__("media_kind", "image"),
        }
        for label, mutate in mutations.items():
            with self.subTest(identity=label):
                timeline = copy.deepcopy(self.timeline)
                mutate(timeline["shots"][0])
                with self.assertRaisesRegex(EdlValidationError, "conflicts"):
                    validate_edl(
                        self.selection,
                        timeline,
                        self.asset_refs,
                        audio_ref=self.audio,
                    )

    def test_validator_rechecks_manifest_rights_and_candidate_lineage(self) -> None:
        manifest = self.storage.read_json(self.manifest)
        validate_edl(
            self.selection,
            self.timeline,
            self.asset_refs,
            audio_ref=self.audio,
            narration_timing_ref=self.timing,
            candidate_manifest_ref=self.manifest,
            candidate_manifest=manifest,
        )
        changed = copy.deepcopy(manifest)
        changed["beats"][0]["candidates"][0]["rights_evidence"][
            "copyright_status"
        ] = "unknown"
        with self.assertRaisesRegex(EdlValidationError, "evidence is not eligible"):
            validate_edl(
                self.selection,
                self.timeline,
                self.asset_refs,
                audio_ref=self.audio,
                narration_timing_ref=self.timing,
                candidate_manifest_ref=self.manifest,
                candidate_manifest=changed,
            )

    def test_validator_cross_checks_frames_and_measured_source_duration(self) -> None:
        frame_conflict = copy.deepcopy(self.timeline)
        frame_conflict["shots"][0]["timeline_start_seconds"] = 0.01
        with self.assertRaisesRegex(EdlValidationError, "integer frame coordinates"):
            validate_edl(
                self.selection,
                frame_conflict,
                self.asset_refs,
                audio_ref=self.audio,
            )

        first = self.timeline["shots"][0]
        measured = {
            asset.id: (
                first["source_end_seconds"] - 0.25
                if asset.id == first["artifact_id"]
                else 20.0
            )
            for asset in self.assets
        }
        with self.assertRaisesRegex(EdlValidationError, "materialized source duration"):
            validate_edl(
                self.selection,
                self.timeline,
                self.asset_refs,
                audio_ref=self.audio,
                source_durations=measured,
            )

    def test_validator_rejects_stale_manifest_and_timing_lineage(self) -> None:
        stale_manifest = copy.deepcopy(self.selection)
        stale_manifest["candidate_manifest_content_hash"] = "0" * 64
        with self.assertRaisesRegex(EdlValidationError, "candidate manifest"):
            validate_edl(
                stale_manifest,
                self.timeline,
                self.asset_refs,
                audio_ref=self.audio,
                narration_timing_ref=self.timing,
                candidate_manifest_ref=self.manifest,
            )

        stale_timing_selection = copy.deepcopy(self.selection)
        stale_timing_timeline = copy.deepcopy(self.timeline)
        stale_timing_selection["narration_timing_content_hash"] = "0" * 64
        stale_timing_timeline["narration_timing_content_hash"] = "0" * 64
        with self.assertRaisesRegex(EdlValidationError, "narration timing"):
            validate_edl(
                stale_timing_selection,
                stale_timing_timeline,
                self.asset_refs,
                audio_ref=self.audio,
                narration_timing_ref=self.timing,
                candidate_manifest_ref=self.manifest,
            )

    def test_segment_commands_never_pad_video_and_only_loop_images(self) -> None:
        common = {
            "duration": 2.0,
            "source_start_seconds": 1.0,
            "width": 1080,
            "height": 1920,
            "frame_rate": 30,
            "layout": "portrait",
            "media_fit": "cover",
            "background_color": "101218",
        }
        video = _edl_segment_command(
            "ffmpeg",
            Path("source.mp4"),
            Path("output.mp4"),
            media_kind="video",
            **common,
        )
        image = _edl_segment_command(
            "ffmpeg",
            Path("source.png"),
            Path("output.mp4"),
            media_kind="image",
            **common,
        )

        self.assertNotIn("-loop", video)
        self.assertEqual(("-loop", "1"), image[4:6])
        _assert_no_video_padding(video)
        _assert_no_video_padding(image)
        with self.assertRaisesRegex(PermanentStepError, "forbidden video padding"):
            _assert_no_video_padding(("ffmpeg", "-vf", "tpad=stop_mode=clone"))

    def test_render_duration_gate_rejects_drift_beyond_one_output_frame(self) -> None:
        _validate_rendered_duration(
            2.0 + 1 / 30 + 0.0000005,
            expected_duration=2.0,
            frame_rate=30,
        )
        with self.assertRaisesRegex(PermanentStepError, "duration gate"):
            _validate_rendered_duration(
                2.0 + 1 / 30 + 0.000002,
                expected_duration=2.0,
                frame_rate=30,
            )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    async def test_ffmpeg_edl_renderer_executes_the_frozen_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "narration.wav"
            video_path = root / "source.mp4"
            commands = (
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    str(audio_path),
                ),
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=teal:s=320x180:r=25",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-y",
                    str(video_path),
                ),
            )
            for command in commands:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(
                    0,
                    completed.returncode,
                    completed.stderr.decode(errors="replace"),
                )

            storage = MemoryStorage()
            seed = _context()
            script_payload = {
                "title": "Frozen EDL",
                "narration": "One exact beat.",
                "beats": [
                    {
                        "id": "beat-one",
                        "sequence": 1,
                        "narration": "One exact beat.",
                        "visual_description": "teal frame",
                    }
                ],
            }
            script = _publish_json(storage, seed, "script", script_payload)
            audio = storage.publish(
                seed,
                ProviderArtifact(
                    "audio", "narration.wav", "audio/wav", audio_path.read_bytes()
                ),
            )
            timing = _publish_json(
                storage,
                seed,
                "narration_timing",
                {
                    "operation": "audio.synthesize",
                    "duration_seconds": 2.0,
                    "audio_content_hash": audio.content_hash,
                    "script_content_hash": script.content_hash,
                    "source": "native_test_boundary",
                    "segments": [
                        {
                            "id": "sentence-001",
                            "sequence": 1,
                            "text": "One exact beat.",
                            "start_seconds": 0.0,
                            "end_seconds": 2.0,
                            "estimated": False,
                            "alignment_source": "native_word_aggregation",
                        }
                    ],
                    "beats": [
                        {
                            "id": "beat-one",
                            "sequence": 1,
                            "text": "One exact beat.",
                            "start_seconds": 0.0,
                            "end_seconds": 2.0,
                            "estimated": False,
                            "alignment_source": "native_word_aggregation",
                        }
                    ],
                },
            )
            asset = storage.publish(
                seed,
                ProviderArtifact(
                    "asset", "source.mp4", "video/mp4", video_path.read_bytes()
                ),
            )
            candidate = _candidate(
                asset,
                rank=1,
                start_ms=0,
                end_ms=2_500,
            )
            candidate["source_duration_ms"] = 3_000
            manifest = _publish_json(
                storage,
                seed,
                "candidate_manifest",
                {
                    "operation": "media.retrieve",
                    "provider": "database-asset-library",
                    "catalog_scope": "local",
                    "local_catalog_only": True,
                    "source_script_artifact_id": script.id,
                    "coverage": {
                        "status": "complete",
                        "total_beats": 1,
                        "covered_beats": 1,
                        "missing_beat_ids": [],
                    },
                    "rights_status": "verified",
                    "beats": [
                        {
                            **script_payload["beats"][0],
                            "coverage_status": "covered",
                            "candidates": [candidate],
                        }
                    ],
                    "materialized_assets": [_materialized_entry(asset, candidate)],
                },
            )
            snapshot = {
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {
                            "resolution": {"width": 320, "height": 180},
                            "frame_rate": 25,
                            "subtitles": {"enabled": True},
                        }
                    }
                }
            }
            aligned = await TimelineCapability(storage).execute(
                _context(
                    script,
                    audio,
                    timing,
                    manifest,
                    asset,
                    input_snapshot=snapshot,
                )
            )
            rendered = await FFmpegEdlRenderCapability(
                LegacyMediaSettings(width=320, height=180, frame_rate=25),
                storage,
            ).execute(
                _context(
                    audio,
                    timing,
                    manifest,
                    *aligned.artifacts,
                    asset,
                    input_snapshot=snapshot,
                )
            )

        self.assertEqual(["video"], [artifact.kind for artifact in rendered.artifacts])
        self.assertGreater(len(storage.read_bytes(rendered.artifacts[0])), 10_000)
        self.assertTrue(rendered.summary_dict()["edl_validated"])
        self.assertEqual(0, rendered.summary_dict()["video_padding_seconds"])

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    async def test_ffmpeg_renders_editorial_fallback_as_a_labeled_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "narration.wav"
            completed = await asyncio.to_thread(
                subprocess.run,
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-t",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    str(audio_path),
                ),
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))
            storage = MemoryStorage()
            seed = _context()
            script_payload = {
                "title": "Editorial fallback",
                "narration": "One exact beat.",
                "beats": [
                    {
                        "id": "beat-one",
                        "sequence": 1,
                        "narration": "One exact beat.",
                        "visual_description": "replace with matching footage",
                        "must_match": [],
                        "must_not_match": [],
                    }
                ],
            }
            script = _publish_json(storage, seed, "script", script_payload)
            audio = storage.publish(
                seed,
                ProviderArtifact("audio", "narration.wav", "audio/wav", audio_path.read_bytes()),
            )
            timing = _publish_json(
                storage,
                seed,
                "narration_timing",
                {
                    "operation": "audio.synthesize",
                    "duration_seconds": 2.0,
                    "audio_content_hash": audio.content_hash,
                    "script_content_hash": script.content_hash,
                    "source": "native_test_boundary",
                    "beats": [
                        {
                            "id": "beat-one",
                            "sequence": 1,
                            "text": "One exact beat.",
                            "start_seconds": 0.0,
                            "end_seconds": 2.0,
                            "estimated": False,
                        }
                    ],
                },
            )
            fallback = build_editorial_fallback(
                _context(script, step_id="retrieve"),
                storage,
                script_artifact=script,
                beats=normalize_beats(script_payload),
                reason="no_selected_asset_library",
            )
            manifest = next(
                artifact for artifact in fallback.artifacts if artifact.kind == "candidate_manifest"
            )
            assets = tuple(artifact for artifact in fallback.artifacts if artifact.kind == "asset")
            snapshot = {
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {
                            "resolution": {"width": 320, "height": 180},
                            "frame_rate": 25,
                            "subtitles": {"enabled": True},
                            "no_asset_draft": {"enabled": True},
                        }
                    }
                }
            }
            aligned = await TimelineCapability(storage).execute(
                _context(script, audio, timing, manifest, *assets, input_snapshot=snapshot)
            )
            rendered = await FFmpegEdlRenderCapability(
                LegacyMediaSettings(width=320, height=180, frame_rate=25), storage
            ).execute(
                _context(
                    audio,
                    timing,
                    manifest,
                    *aligned.artifacts,
                    *assets,
                    input_snapshot=snapshot,
                )
            )
            quality = await FFmpegQualityCapability(
                LegacyMediaSettings(width=320, height=180, frame_rate=25), storage
            ).execute(
                _context(rendered.artifacts[0], input_snapshot=snapshot, step_id="quality")
            )

        self.assertGreater(len(storage.read_bytes(rendered.artifacts[0])), 10_000)
        self.assertEqual("editorial-draft.mp4", rendered.artifacts[0].filename)
        self.assertEqual("editorial_fallback", rendered.summary_dict()["visual_source_mode"])
        self.assertTrue(rendered.summary_dict()["replacement_required"])
        self.assertTrue(quality.requires_review)
        self.assertIn(
            "editorial_draft_requires_material_replacement",
            quality.summary_dict()["risks"],
        )


class CapabilityBindingTests(unittest.TestCase):
    def test_new_steps_declare_only_their_direct_artifact_contracts(self) -> None:
        registry = production_step_registry()
        self.assertEqual(
            ("audio", "narration_timing", "script"),
            registry.resolve("media.generate").required_artifact_kinds,
        )
        self.assertEqual(
            ("script",), registry.resolve("media.retrieve").required_artifact_kinds
        )
        self.assertEqual(
            ("asset", "audio", "candidate_manifest", "narration_timing", "script"),
            registry.resolve("timeline.align").required_artifact_kinds,
        )
        self.assertEqual(
            (
                "asset",
                "audio",
                "candidate_manifest",
                "material_selection",
                "narration_timing",
                "timeline",
            ),
            registry.resolve("render.edl").required_artifact_kinds,
        )

    def test_configured_bindings_preserve_legacy_render_compose(self) -> None:
        storage = MemoryStorage()
        settings = WorkerSettings(
            database_url="postgresql://db.example/framefactory",
            redis_url="redis://redis.example/0",
            environment="test",
            worker_id="worker-test",
            object_storage=ObjectStorageSettings("test-bucket"),
            legacy_media=LegacyMediaSettings(),
            asset_library=AssetLibrarySettings(
                "postgresql://db.example/framefactory",
                control_api_url="http://127.0.0.1:8200",
            ),
        )
        registry = configured_capabilities(settings, artifact_storage=storage)

        self.assertIsInstance(
            registry.resolve("media.retrieve"), DatabaseRetrievalCapability
        )
        self.assertIsInstance(
            registry.resolve("media.retrieve")._acquire,
            ControlApiAssetAcquirer,
        )
        self.assertIsInstance(registry.resolve("timeline.align"), TimelineCapability)
        self.assertIsInstance(registry.resolve("render.edl"), FFmpegEdlRenderCapability)
        self.assertIsInstance(registry.resolve("render.compose"), FFmpegRenderCapability)


if __name__ == "__main__":
    unittest.main()
