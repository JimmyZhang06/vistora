"""Truthful no-material fallback for an editable, non-final video draft."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact

from .models import RetrievalBeat

_CARD_VARIANTS = 8


def editorial_fallback_enabled(snapshot: Mapping[str, Any]) -> bool:
    framefactory = snapshot.get("_framefactory")
    if not isinstance(framefactory, Mapping):
        return False
    composition = framefactory.get("composition_snapshot")
    if not isinstance(composition, Mapping):
        return False
    production = composition.get("production_settings")
    if not isinstance(production, Mapping):
        return False
    fallback = production.get("no_asset_draft")
    return isinstance(fallback, Mapping) and fallback.get("enabled") is True


def editorial_inventory(
    *, topic: str, concepts: Sequence[str], reason: str
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "operation": "media.inventory",
        "provider": "procedural-editorial-cards",
        "topic": topic,
        "coverage": {
            "status": "editorial_fallback",
            "total_concepts": len(concepts),
            "covered_concepts": 0,
            "missing_concepts": list(concepts),
        },
        "eligible_candidates": 0,
        "unique_assets": 0,
        "unique_segments": 0,
        "editorial_fallback": {
            "enabled": True,
            "mode": "procedural_cards",
            "draft": True,
            "replacement_required": True,
            "reason": reason,
        },
        "concepts": [
            {
                "concept": concept,
                "status": "planned_placeholder",
                "eligible_candidates": 0,
                "representatives": [],
            }
            for concept in concepts
        ],
    }


def build_editorial_fallback(
    context: StepContext,
    storage: ArtifactStorage,
    *,
    script_artifact: ArtifactRef,
    beats: Sequence[RetrievalBeat],
    reason: str,
) -> StepResult:
    """Publish deterministic BMP cards and a normal candidate-manifest lineage.

    The cards are deliberately described as temporary editorial material.  They
    are real image artifacts so the existing EDL and FFmpeg renderer remain the
    only downstream production path.
    """

    artifacts, plan_ref, manifest = build_editorial_fallback_payload(
        context,
        storage,
        script_artifact=script_artifact,
        beats=beats,
        reason=reason,
    )
    manifest_ref = storage.publish(
        context,
        ProviderArtifact(
            "candidate_manifest",
            "candidate-manifest.json",
            "application/json",
            _json_bytes(manifest),
        ),
    )
    return StepResult(
        artifacts=(*artifacts, plan_ref, manifest_ref),
        output_summary={
            "operation": "media.retrieve",
            "provider": "procedural-editorial-cards",
            "visual_source_mode": "editorial_fallback",
            "draft": True,
            "replacement_required": True,
            "fallback_reason": reason,
            "total_beats": len(beats),
            "covered_beats": len(beats),
            "missing_beat_ids": [],
            "materialized_assets": len(artifacts),
            "candidate_manifest_artifact_id": manifest_ref.id,
            "video_plan_artifact_id": plan_ref.id,
            "rights_status": "verified",
        },
    )


def build_editorial_fallback_payload(
    context: StepContext,
    storage: ArtifactStorage,
    *,
    script_artifact: ArtifactRef,
    beats: Sequence[RetrievalBeat],
    reason: str,
) -> tuple[tuple[ArtifactRef, ...], ArtifactRef, dict[str, Any]]:
    """Build reusable fallback rows without publishing a competing manifest."""

    artifacts: list[ArtifactRef] = []
    artifact_by_variant: dict[int, ArtifactRef] = {}
    materialized_artifact_ids: set[str] = set()
    beat_rows: list[dict[str, Any]] = []
    materialized: list[dict[str, Any]] = []
    plan_rows: list[dict[str, Any]] = []
    for beat in beats:
        variant = (beat.sequence - 1) % _CARD_VARIANTS + 1
        asset = artifact_by_variant.get(variant)
        if asset is None:
            data = _card_bmp(f"editorial-palette-{variant}", variant)
            asset = storage.publish(
                context,
                ProviderArtifact(
                    "asset",
                    f"editorial-card-{variant:03d}.bmp",
                    "image/bmp",
                    data,
                ),
            )
            artifact_by_variant[variant] = asset
            artifacts.append(asset)
        identity = hashlib.sha256(
            f"{beat.id}\0{asset.content_hash}".encode()
        ).hexdigest()[:20]
        asset_id = str(uuid5(NAMESPACE_URL, f"vistora:editorial:asset:{identity}"))
        asset_file_id = str(uuid5(NAMESPACE_URL, f"vistora:editorial:file:{identity}"))
        analysis_id = str(uuid5(NAMESPACE_URL, f"vistora:editorial:analysis:{identity}"))
        segment_id = str(uuid5(NAMESPACE_URL, f"vistora:editorial:segment:{identity}"))
        must_match = [
            {"term": term, "matched": True} for term in beat.must_match
        ]
        must_not_match = [
            {"term": term, "matched": False} for term in beat.must_not_match
        ]
        constraint_text = " ".join(beat.must_match).strip()
        candidate = {
            "candidate_id": f"candidate-{identity}",
            "rank": 1,
            "eligible": True,
            "materialized": True,
            "artifact_id": asset.id,
            "artifact_filename": asset.filename,
            "asset_id": asset_id,
            "asset_file_id": asset_file_id,
            "analysis_id": analysis_id,
            "segment_id": segment_id,
            "title": f"Editorial draft card {beat.sequence}",
            "description": constraint_text or "temporary editorial draft visual",
            "labels": list(beat.must_match),
            "source_transcript": "",
            "asset_kind": "image",
            "media_type": asset.media_type,
            "content_hash": asset.content_hash,
            "byte_size": asset.byte_size,
            "source_duration_ms": None,
            "source_window": {"start_ms": 0, "end_ms": 0, "duration_ms": 0},
            "score": 0.0,
            "score_breakdown": {
                "lexical_similarity": 0.0,
                "label_hits": len(beat.must_match),
                "label_score": 0.0,
                "cut_safe_bonus": 0.0,
                "semantic_complete_bonus": 0.0,
                "total": 0.0,
            },
            "hard_constraints": {
                "passed": True,
                "must_match": must_match,
                "must_not_match": must_not_match,
            },
            "rejection_codes": [],
            "cut_evidence": {"cut_safe": True, "semantic_complete": False},
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
        beat_rows.append(
            {
                "id": beat.id,
                "sequence": beat.sequence,
                "narration": beat.narration,
                "visual_description": beat.visual_description,
                "query": beat.query(),
                "must_match": list(beat.must_match),
                "must_not_match": list(beat.must_not_match),
                "coverage_status": "covered",
                "candidates": [candidate],
            }
        )
        if asset.id not in materialized_artifact_ids:
            materialized_artifact_ids.add(asset.id)
            materialized.append(
                {
                    "artifact_id": asset.id,
                    "kind": asset.kind,
                    "filename": asset.filename,
                    "media_type": asset.media_type,
                    "content_hash": asset.content_hash,
                    "byte_size": asset.byte_size,
                    "asset_id": asset_id,
                    "asset_file_id": asset_file_id,
                }
            )
        plan_rows.append(
            {
                "beat_id": beat.id,
                "sequence": beat.sequence,
                "narration": beat.narration,
                "visual_intent": beat.visual_description,
                "temporary_visual": asset.filename,
                "replacement_status": "required_before_publish",
            }
        )

    plan = {
        "schema_version": "1.0.0",
        "operation": "video.plan",
        "mode": "no_asset_editorial_draft",
        "draft": True,
        "source_script_artifact_id": script_artifact.id,
        "replacement_required": True,
        "success_definition": (
            "playable narrated MP4 with replaceable visual slots; not publish-ready"
        ),
        "beats": plan_rows,
    }
    plan_ref = storage.publish(
        context,
        ProviderArtifact(
            "video_plan",
            "video-plan.json",
            "application/json",
            _json_bytes(plan),
        ),
    )
    manifest = {
        "schema_version": "1.0.0",
        "operation": "media.retrieve",
        "provider": "procedural-editorial-cards",
        "catalog_scope": "run",
        "local_catalog_only": False,
        "source_script_artifact_id": script_artifact.id,
        "coverage": {
            "status": "complete",
            "total_beats": len(beat_rows),
            "covered_beats": len(beat_rows),
            "missing_beat_ids": [],
        },
        "rights_status": "verified",
        "editorial_fallback": {
            "enabled": True,
            "mode": "procedural_cards",
            "draft": True,
            "replacement_required": True,
            "reason": reason,
            "video_plan_artifact_id": plan_ref.id,
            "video_plan_content_hash": plan_ref.content_hash,
        },
        "beats": beat_rows,
        "materialized_assets": materialized,
    }
    return tuple(artifacts), plan_ref, manifest


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _card_bmp(identity: str, sequence: int, *, width: int = 96, height: int = 54) -> bytes:
    """Create a small deterministic 24-bit BMP without an imaging dependency."""

    digest = hashlib.sha256(f"{identity}:{sequence}".encode()).digest()
    base = tuple(48 + value % 128 for value in digest[:3])
    accent = tuple(96 + value % 144 for value in digest[3:6])
    row_size = (width * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            band = (x * 3 // width + y * 2 // height + sequence) % 5
            color = accent if band == 0 or (width // 7 < x < width // 7 + 3) else base
            shade = 12 if (x + y) % 17 == 0 else 0
            red, green, blue = (min(255, channel + shade) for channel in color)
            row.extend((blue, green, red))
        row.extend(b"\0" * (row_size - width * 3))
        pixels.extend(row)
    offset = 14 + 40
    size = offset + len(pixels)
    return b"BM" + struct.pack("<IHHI", size, 0, 0, offset) + struct.pack(
        "<IIIHHIIIIII",
        40,
        width,
        height,
        1,
        24,
        0,
        len(pixels),
        2835,
        2835,
        0,
        0,
    ) + bytes(pixels)
