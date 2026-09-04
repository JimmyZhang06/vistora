"""Timing-driven material selection and deterministic timeline artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact
from framefactory.worker.retrieval.beats import normalize_beats
from framefactory.worker.retrieval.evidence import (
    hard_constraint_evidence_valid,
    rights_evidence_valid,
)
from framefactory.worker.timeline import plan_edit_timeline

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
_MAX_SHOTS_PER_TIMING_UNIT = 500
_MAX_TIMING_UNITS = 10_000
_MAX_EDL_SHOTS = 100_000
_MAX_MATERIAL_SELECTIONS = _MAX_EDL_SHOTS
_MAX_EDL_TITLE_LENGTH = 1_000
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GENERATED_SAFETY_FLAGS = (
    "adult",
    "violence",
    "self_harm",
    "hate_or_extremism",
    "illegal_activity",
    "recognizable_real_person_or_public_figure",
    "brand_or_logo",
    "protected_character",
)


class TimelineCapability:
    """Freeze selected candidates and an EDL-like timeline before rendering."""

    def __init__(self, storage: ArtifactStorage) -> None:
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script_ref = _required_artifact(context, "script")
        audio_ref = _required_artifact(context, "audio")
        timing_ref = _required_artifact(context, "narration_timing")
        manifest_ref = _required_artifact(context, "candidate_manifest")
        asset_refs = {
            artifact.id: artifact
            for artifact in context.input_artifacts
            if artifact.kind == "asset"
        }
        if not asset_refs:
            raise PermanentStepError("timeline.align received no materialized asset artifacts")

        script = dict(self.storage.read_json(script_ref))
        timing = dict(self.storage.read_json(timing_ref))
        manifest = dict(self.storage.read_json(manifest_ref))
        generated_manifests = {
            artifact.id: artifact
            for artifact in context.input_artifacts
            if artifact.kind == "generated_material_manifest"
        }
        generated_audit_ref = _validate_manifest_gate(
            manifest,
            script_ref,
            generated_manifests,
        )
        units, total_duration = _timing_units(
            timing,
            audio_content_hash=audio_ref.content_hash,
            script_content_hash=script_ref.content_hash,
        )
        manifest_beats = _manifest_beats(manifest)
        if not manifest_beats:
            raise PermanentStepError("candidate manifest contains no Beat candidates")
        _validate_script_beat_lineage(script, manifest_beats)
        if generated_audit_ref is not None:
            generated_audit = self.storage.read_json(generated_audit_ref)
            if not isinstance(generated_audit, Mapping):
                raise PermanentStepError(
                    "generated material audit artifact must contain an object"
                )
            _validate_generated_audit_lineage(
                manifest,
                manifest_beats,
                generated_audit,
                script_ref=script_ref,
                audio_ref=audio_ref,
                timing_ref=timing_ref,
                asset_refs=asset_refs,
                input_snapshot=context.input_snapshot.to_dict(),
            )
        timing_bindings = _bind_timing_units(units, manifest_beats)
        materialized, materialized_by_artifact = _validated_materialized_assets(
            manifest,
            asset_refs,
        )
        frame_rate_num, frame_rate_den = _output_frame_rate(context.input_snapshot)
        timing_spans_estimated = timing.get("estimated") is True or any(
            bool(unit.get("estimated")) for unit in units
        )

        prepared_units: list[
            tuple[
                Mapping[str, Any],
                Mapping[str, Any],
                tuple[Mapping[str, Any], ...],
                int,
            ]
        ] = []
        planned_shots = 0
        for unit, beat in timing_bindings:
            await context.checkpoint()
            candidates = _materialized_candidates(
                beat,
                asset_refs,
                materialized,
                materialized_by_artifact,
                manifest_content_hash=manifest_ref.content_hash,
            )
            if not candidates:
                raise PermanentStepError(
                    f"timeline.align has no materialized candidate for Beat {beat['id']}"
                )
            eligible, shot_count = _prepare_timing_unit(unit, beat, candidates)
            planned_shots = _reserve_shot_budget(planned_shots, shot_count)
            prepared_units.append((unit, beat, eligible, shot_count))

        shots: list[dict[str, Any]] = []
        used_selections: dict[str, dict[str, Any]] = {}
        for unit, beat, eligible, shot_count in prepared_units:
            await context.checkpoint()
            unit_shots = _plan_prepared_timing_unit(
                unit,
                beat,
                eligible,
                shot_count,
            )
            for shot in unit_shots:
                shot["ordinal"] = len(shots)
                shots.append(shot)
                selection = next(
                    item
                    for item in eligible
                    if item["selection_id"] == shot["selection_id"]
                )
                if (
                    selection["selection_id"] not in used_selections
                    and len(used_selections) >= _MAX_MATERIAL_SELECTIONS
                ):
                    raise PermanentStepError(
                        "timeline.align exceeds the 100000 material selection limit"
                    )
                used_selections.setdefault(
                    selection["selection_id"],
                    _selection_payload(selection),
                )

        if not shots:
            raise PermanentStepError("timeline.align could not plan any renderable shots")
        if len(shots) != planned_shots:
            raise PermanentStepError("timeline.align shot budget does not match planned output")
        if len(used_selections) > len(shots):
            raise PermanentStepError(
                "timeline.align material selection lineage exceeds its shot lineage"
            )
        timeline_duration = _quantize_timeline(
            shots,
            used_selections,
            total_duration=total_duration,
            frame_rate_num=frame_rate_num,
            frame_rate_den=frame_rate_den,
        )

        rounded_duration = _rounded_positive(
            timeline_duration,
            digits=6,
            field="timeline duration",
        )
        rounded_audio_duration = _rounded_positive(
            total_duration,
            digits=6,
            field="audio duration",
        )
        material_selection = {
            "schema_version": "1.0.0",
            "operation": "timeline.align",
            "candidate_manifest_content_hash": manifest_ref.content_hash,
            "narration_timing_content_hash": timing_ref.content_hash,
            "audio_content_hash": audio_ref.content_hash,
            "duration_seconds": rounded_duration,
            "selections": list(used_selections.values()),
        }
        timeline = {
            "schema_version": "2.0.0",
            "operation": "timeline.align",
            "time_base": "seconds",
            "duration_seconds": rounded_duration,
            "audio_duration_seconds": rounded_audio_duration,
            "output_frame_rate": {"num": frame_rate_num, "den": frame_rate_den},
            "audio_content_hash": audio_ref.content_hash,
            "narration_timing_content_hash": timing_ref.content_hash,
            "timing_estimated": timing_spans_estimated,
            "timing_source": str(timing.get("source", "unknown")),
            "title": str(script.get("title", ""))[:_MAX_EDL_TITLE_LENGTH],
            "narration": str(script.get("narration", "")),
            "subtitle_cues": list(_subtitle_cues(timing)),
            "edl_policy": {
                "video_padding": "forbidden",
                "video_loop": "forbidden",
                "image_loop": "allowed",
                "speed": 1.0,
            },
            "shots": shots,
        }
        selection_artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "material_selection",
                "material-selection.json",
                "application/json",
                _json_bytes(material_selection),
            ),
        )
        timeline_artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "timeline",
                "edit-timeline.json",
                "application/json",
                _json_bytes(timeline),
            ),
        )
        uncertain_cuts = sum(shot["cut_evidence"] == "candidate" for shot in shots)
        rights_status = str(manifest.get("rights_status", "unknown"))
        return StepResult(
            artifacts=(selection_artifact, timeline_artifact),
            output_summary={
                "provider": "deterministic-timing-timeline",
                "duration_seconds": round(timeline_duration, 3),
                "beats": len({shot["beat_id"] for shot in shots}),
                "shots": len(shots),
                "selected_assets": len(used_selections),
                "timing_estimated": timing_spans_estimated,
                "video_padding_seconds": 0,
                "uncertain_cuts": uncertain_cuts,
                "rights_status": rights_status,
            },
            requires_review=(
                timing_spans_estimated
                or uncertain_cuts > 0
                or rights_status not in {"verified", "owned", "licensed", "public_domain"}
            ),
        )


def _timing_units(
    timing: Mapping[str, Any],
    *,
    audio_content_hash: str,
    script_content_hash: str,
) -> tuple[tuple[dict[str, Any], ...], float]:
    if timing.get("operation") != "audio.synthesize":
        raise PermanentStepError("narration timing operation must be audio.synthesize")
    if str(timing.get("audio_content_hash", "")) != audio_content_hash:
        raise PermanentStepError("narration timing does not belong to the input audio")
    if str(timing.get("script_content_hash", "")) != script_content_hash:
        raise PermanentStepError("narration timing does not belong to the input script")
    duration = _positive_number(timing.get("duration_seconds"))
    if duration is None:
        raise PermanentStepError("narration timing duration is invalid")
    raw = timing.get("beats") or timing.get("segments")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise PermanentStepError("narration timing contains no usable units")
    if len(raw) > _MAX_TIMING_UNITS:
        raise PermanentStepError(
            "narration timing exceeds the 10000 timing unit limit"
        )
    units: list[dict[str, Any]] = []
    cursor = 0.0
    identities: set[str] = set()
    sequences: set[int] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise PermanentStepError("narration timing units must be objects")
        start = _number(value.get("start_seconds"))
        end = _number(value.get("end_seconds"))
        text = str(value.get("text", "")).strip()
        identity = str(value.get("id") or "").strip()
        sequence = _strict_positive_int(value.get("sequence"))
        if (
            start is None
            or end is None
            or start < 0
            or not text
            or not identity
            or sequence is None
            or end <= start
        ):
            raise PermanentStepError("narration timing unit is invalid")
        if identity in identities or sequence in sequences:
            raise PermanentStepError("narration timing unit identity is ambiguous")
        if abs(start - cursor) > 0.02:
            raise PermanentStepError("narration timing must be continuous")
        units.append(
            {
                "id": identity,
                "sequence": sequence,
                "text": text,
                "start_seconds": start,
                "end_seconds": end,
                "estimated": value.get("estimated") is True,
                "alignment_source": str(value.get("alignment_source") or "unknown"),
            }
        )
        identities.add(identity)
        sequences.add(sequence)
        cursor = end
    if abs(cursor - duration) > 0.02:
        raise PermanentStepError("narration timing units do not cover the audio duration")
    return tuple(units), duration


def _manifest_beats(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = manifest.get("beats")
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes))
        or not raw
        or any(not isinstance(item, Mapping) for item in raw)
    ):
        return ()
    values = [dict(item) for item in raw]
    values.sort(key=lambda item: _positive_int(item.get("sequence"), 1_000_000))
    identities: set[str] = set()
    sequences: set[int] = set()
    for index, value in enumerate(values):
        value["id"] = str(value.get("id") or f"beat-{index + 1:03d}")
        value["sequence"] = _positive_int(value.get("sequence"), index + 1)
        if value["id"] in identities or value["sequence"] in sequences:
            raise PermanentStepError("candidate manifest Beat identity is ambiguous")
        if value.get("coverage_status") != "covered":
            raise PermanentStepError("candidate manifest contains an uncovered Beat")
        identities.add(value["id"])
        sequences.add(value["sequence"])
    return tuple(values)


def _validate_manifest_gate(
    manifest: Mapping[str, Any],
    script_ref: ArtifactRef,
    generated_manifests: Mapping[str, ArtifactRef],
) -> ArtifactRef | None:
    generated_audit_ref: ArtifactRef | None = None
    operation = manifest.get("operation")
    if operation == "media.retrieve":
        local_catalog = (
            manifest.get("provider") == "database-asset-library"
            and manifest.get("catalog_scope") == "local"
            and manifest.get("local_catalog_only") is True
        )
        fallback = manifest.get("editorial_fallback")
        procedural_draft = (
            manifest.get("provider") == "procedural-editorial-cards"
            and manifest.get("catalog_scope") == "run"
            and manifest.get("local_catalog_only") is False
            and isinstance(fallback, Mapping)
            and fallback.get("enabled") is True
            and fallback.get("mode") == "procedural_cards"
            and fallback.get("draft") is True
            and fallback.get("replacement_required") is True
        )
        hybrid_draft = (
            manifest.get("provider") == "hybrid-local-and-editorial"
            and manifest.get("catalog_scope") == "run"
            and manifest.get("local_catalog_only") is False
            and isinstance(manifest.get("retrieval_policy"), Mapping)
            and isinstance(manifest.get("acquisition"), Mapping)
            and isinstance(fallback, Mapping)
            and fallback.get("enabled") is True
            and fallback.get("mode") == "procedural_cards"
            and fallback.get("draft") is True
            and fallback.get("replacement_required") is True
        )
        if not local_catalog and not procedural_draft and not hybrid_draft:
            raise PermanentStepError("candidate manifest is not a local catalog snapshot")
    elif operation == "media.generate":
        generation = manifest.get("generation")
        if (
            not str(manifest.get("provider") or "").strip()
            or manifest.get("catalog_scope") != "run"
            or manifest.get("local_catalog_only") is not False
            or not isinstance(generation, Mapping)
            or generation.get("mode") != "generated_only"
            or generation.get("continuity_mode")
            not in {"none", "prompt_pack", "reference_frame"}
        ):
            raise PermanentStepError(
                "generated candidate manifest is not a verified Run-scoped snapshot"
            )
        audit_id = str(
            generation.get("generated_material_manifest_artifact_id") or ""
        ).strip()
        audit_hash = str(
            generation.get("generated_material_manifest_content_hash") or ""
        ).strip()
        audit = generated_manifests.get(audit_id)
        if audit is None or audit.content_hash != audit_hash:
            raise PermanentStepError(
                "generated candidate manifest lacks its paid-generation audit artifact"
            )
        generated_audit_ref = audit
    else:
        raise PermanentStepError(
            "candidate manifest operation must be media.retrieve or media.generate"
        )
    if str(manifest.get("source_script_artifact_id") or "") != script_ref.id:
        raise PermanentStepError("candidate manifest does not belong to the input script")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise PermanentStepError("candidate manifest lacks coverage evidence")
    missing = coverage.get("missing_beat_ids")
    missing = (
        missing
        if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes))
        else None
    )
    if coverage.get("status") != "complete" or missing is None or missing:
        raise PermanentStepError("candidate manifest coverage is incomplete")
    raw_beats = manifest.get("beats")
    beat_count = len(raw_beats) if isinstance(raw_beats, list) else -1
    if (
        _positive_int(coverage.get("total_beats"), -1) != beat_count
        or _positive_int(coverage.get("covered_beats"), -1) != beat_count
    ):
        raise PermanentStepError("candidate manifest coverage counts are inconsistent")
    if manifest.get("rights_status") != "verified":
        raise PermanentStepError("candidate manifest rights status is not verified")
    return generated_audit_ref


def _validate_generated_audit_lineage(
    manifest: Mapping[str, Any],
    beats: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
    *,
    script_ref: ArtifactRef,
    audio_ref: ArtifactRef,
    timing_ref: ArtifactRef,
    asset_refs: Mapping[str, ArtifactRef],
    input_snapshot: Mapping[str, Any],
) -> None:
    generation = _generated_audit_mapping(
        manifest.get("generation"),
        "candidate manifest generation",
    )
    frozen = _full_ai_generation_lineage(input_snapshot)
    provider = str(frozen.get("provider_name") or "").strip()
    model_id = str(frozen.get("model_id") or "").strip()
    continuity_mode = str(frozen.get("continuity_mode") or "").strip()
    if not provider or not model_id or continuity_mode not in {"none", "prompt_pack"}:
        _reject_generated_audit("frozen provider, model, or continuity mode is invalid")

    expected_header = {
        "operation": "media.generate",
        "generated_only": True,
        "provider": provider,
        "model_id": model_id,
        "source_script_artifact_id": script_ref.id,
        "source_audio_artifact_id": audio_ref.id,
        "source_narration_timing_artifact_id": timing_ref.id,
        "continuity_mode": continuity_mode,
    }
    for field, expected in expected_header.items():
        if audit.get(field) != expected:
            _reject_generated_audit(f"{field} conflicts with immutable Run lineage")
    if (
        manifest.get("provider") != provider
        or generation.get("mode") != "generated_only"
        or generation.get("continuity_mode") != continuity_mode
    ):
        _reject_generated_audit(
            "provider or continuity conflicts with the candidate manifest"
        )

    terms = _generated_audit_mapping(audit.get("terms_snapshot"), "terms snapshot")
    frozen_terms = _generated_audit_mapping(
        frozen.get("terms_snapshot"),
        "frozen terms snapshot",
    )
    for field in ("ref", "content_hash", "captured_at"):
        expected = str(frozen_terms.get(field) or "").strip()
        if not expected or terms.get(field) != expected:
            _reject_generated_audit(
                f"terms snapshot {field} conflicts with immutable Run lineage"
            )
    rights_license_basis = _validate_generated_rights_snapshot(
        audit,
        frozen,
        terms,
    )

    (
        variants_per_beat,
        candidate_count,
        max_cost_minor,
        credit_unit_minor,
        cost_per_second_minor,
        currency,
    ) = _validate_generated_audit_plan_and_pricing(
        audit,
        frozen,
        beat_count=len(beats),
    )
    jobs = _generated_audit_objects(audit.get("jobs"), "jobs")
    if len(jobs) != candidate_count:
        _reject_generated_audit("job count conflicts with the frozen candidate count")
    beat_sequences = {
        str(beat.get("id") or ""): _strict_positive_int(beat.get("sequence"))
        for beat in beats
    }
    if "" in beat_sequences or any(
        sequence is None for sequence in beat_sequences.values()
    ):
        _reject_generated_audit("candidate Beat identity is invalid")

    accepted: dict[str, Mapping[str, Any]] = {}
    rejected_hashes: set[str] = set()
    paid_operation_ids: set[str] = set()
    provider_task_ids: set[str] = set()
    operation_keys: set[str] = set()
    job_variants: set[tuple[str, int]] = set()
    estimated_credits = Decimal(0)
    incurred_credits = Decimal(0)
    for job in jobs:
        _validate_generated_job_identity(
            job,
            beat_sequences=beat_sequences,
            model_id=model_id,
            variants_per_beat=variants_per_beat,
            paid_operation_ids=paid_operation_ids,
            provider_task_ids=provider_task_ids,
            operation_keys=operation_keys,
            job_variants=job_variants,
        )
        job_estimated = _generated_non_negative_decimal(
            job.get("estimated_cost_credits"),
            "job estimated cost credits",
        )
        job_incurred = _generated_non_negative_decimal(
            job.get("incurred_cost_credits"),
            "job incurred cost credits",
        )
        expected_job_credits = Decimal(5 * cost_per_second_minor) / Decimal(
            credit_unit_minor
        )
        if job_estimated != expected_job_credits:
            _reject_generated_audit("job estimated cost conflicts with frozen pricing")
        estimated_credits += job_estimated
        incurred_credits += job_incurred
        output_hash = str(job.get("output_content_hash") or "").strip()
        output_filename = str(job.get("output_filename") or "").strip()
        output_media_type = str(job.get("output_media_type") or "").strip()
        output_byte_size = _strict_positive_int(job.get("output_byte_size"))
        if (
            _SHA256.fullmatch(output_hash) is None
            or not output_filename
            or not output_media_type
            or output_byte_size is None
        ):
            _reject_generated_audit("job output identity is incomplete")
        rejection_codes = _generated_audit_strings(
            job.get("rejection_codes"),
            "job rejection codes",
        )
        verification_status = job.get("verification_status")
        if verification_status == "accepted":
            _validate_generated_accepted_verification(job)
            artifact_id = str(job.get("output_artifact_id") or "").strip()
            if not artifact_id or rejection_codes:
                _reject_generated_audit(
                    "accepted job lacks an artifact or contains rejection codes"
                )
            if artifact_id in accepted:
                _reject_generated_audit("accepted job artifact identity is ambiguous")
            artifact = asset_refs.get(artifact_id)
            if artifact is None or artifact.kind != "asset":
                _reject_generated_audit(
                    "accepted job artifact is unavailable to timeline.align"
                )
            expected_artifact = {
                "output_content_hash": artifact.content_hash,
                "output_filename": artifact.filename,
                "output_media_type": artifact.media_type,
                "output_byte_size": artifact.byte_size,
            }
            if any(job.get(field) != expected for field, expected in expected_artifact.items()):
                _reject_generated_audit(
                    "accepted job output conflicts with its asset artifact"
                )
            accepted[artifact_id] = job
        elif verification_status == "rejected":
            if job.get("output_artifact_id") is not None or not rejection_codes:
                _reject_generated_audit(
                    "rejected job must have null artifact lineage and rejection evidence"
                )
            rejected_hashes.add(output_hash)
        else:
            _reject_generated_audit("job verification status is invalid")

    expected_variants = set(range(1, variants_per_beat + 1))
    for beat_id in beat_sequences:
        if {
            variant for job_beat_id, variant in job_variants if job_beat_id == beat_id
        } != expected_variants:
            _reject_generated_audit(
                "jobs do not contain exactly one frozen variant for every Beat"
            )
    _validate_generated_audit_totals(
        audit,
        job_count=len(jobs),
        accepted_count=len(accepted),
        estimated_credits=estimated_credits,
        incurred_credits=incurred_credits,
        credit_unit_minor=credit_unit_minor,
        billable_seconds=candidate_count * 5,
        cost_per_second_minor=cost_per_second_minor,
        max_cost_minor=max_cost_minor,
        currency=currency,
    )
    if not accepted:
        _reject_generated_audit("contains no accepted generated output")
    candidates = _generated_candidates_by_artifact(beats)
    materialized = _generated_materialized_by_artifact(manifest)
    accepted_ids = set(accepted)
    if set(candidates) != accepted_ids or set(materialized) != accepted_ids:
        _reject_generated_audit(
            "accepted jobs, candidates, and materialized assets are not bidirectional"
        )

    candidate_hashes: set[str] = set()
    for artifact_id, job in accepted.items():
        candidate, beat = candidates[artifact_id]
        materialized_asset = materialized[artifact_id]
        _validate_generated_candidate_lineage(
            candidate,
            beat,
            materialized_asset,
            job,
            terms=terms,
            rights_license_basis=rights_license_basis,
        )
        candidate_hashes.add(str(candidate.get("content_hash") or ""))
    if rejected_hashes & candidate_hashes:
        _reject_generated_audit("rejected output bytes entered downstream lineage")


def _validate_generated_job_identity(
    job: Mapping[str, Any],
    *,
    beat_sequences: Mapping[str, int | None],
    model_id: str,
    variants_per_beat: int,
    paid_operation_ids: set[str],
    provider_task_ids: set[str],
    operation_keys: set[str],
    job_variants: set[tuple[str, int]],
) -> None:
    beat_id = str(job.get("beat_id") or "").strip()
    variant = _strict_positive_int(job.get("variant"))
    if (
        beat_id not in beat_sequences
        or job.get("beat_sequence") != beat_sequences[beat_id]
        or variant is None
        or variant > variants_per_beat
        or job.get("status") != "succeeded"
        or job.get("model_id") != model_id
    ):
        _reject_generated_audit("job Beat, status, or model identity is invalid")
    job_variant = (beat_id, variant)
    if job_variant in job_variants:
        _reject_generated_audit("job Beat variant identity is ambiguous")
    job_variants.add(job_variant)
    for field, seen in (
        ("paid_operation_id", paid_operation_ids),
        ("provider_task_id", provider_task_ids),
        ("operation_key", operation_keys),
    ):
        identity = str(job.get(field) or "").strip()
        if not identity or identity in seen:
            _reject_generated_audit(f"job {field} is missing or ambiguous")
        seen.add(identity)


def _validate_generated_accepted_verification(job: Mapping[str, Any]) -> None:
    safety = _generated_audit_mapping(job.get("safety"), "accepted job safety")
    if safety != {
        "status": "passed",
        "provider_moderation": "not_rejected",
        "failure_code": None,
    }:
        _reject_generated_audit("accepted job provider safety did not pass")
    verification = _generated_audit_mapping(
        job.get("visual_verification"),
        "accepted job visual verification",
    )
    expected_flags = {
        "has_watermark": False,
        "has_embedded_text": False,
        "unsafe": False,
        **{field: False for field in _GENERATED_SAFETY_FLAGS},
        "quality_usable": True,
        "cut_safe": True,
        "semantic_complete": True,
    }
    if any(verification.get(field) is not expected for field, expected in expected_flags.items()):
        _reject_generated_audit("accepted job visual safety verdict is invalid")
    confidence = _number(verification.get("confidence"))
    minimum_confidence = _number(verification.get("minimum_confidence"))
    quality_score = _number(verification.get("quality_score"))
    minimum_quality_score = _number(verification.get("minimum_quality_score"))
    visual_evidence = str(
        verification.get("visual_description_evidence") or ""
    ).strip()
    cut_evidence = str(verification.get("cut_safe_evidence") or "").strip()
    if (
        verification.get("visual_description_matched") is not True
        or confidence is None
        or minimum_confidence is None
        or quality_score is None
        or minimum_quality_score is None
        or not 0 <= confidence <= 1
        or not 0 < minimum_confidence <= 1
        or confidence < minimum_confidence
        or not 0 <= quality_score <= 1
        or not 0 < minimum_quality_score <= 1
        or quality_score < minimum_quality_score
        or not 1 <= len(visual_evidence) <= 2_000
        or not 1 <= len(cut_evidence) <= 2_000
    ):
        _reject_generated_audit(
            "accepted job confidence, quality, or evidence is below its gate"
        )
    _generated_constraint_checks(
        verification.get("must_match"),
        "accepted job must-match checks",
        expected_match=True,
    )
    _generated_constraint_checks(
        verification.get("must_not_match"),
        "accepted job must-not-match checks",
        expected_match=False,
    )


def _validate_generated_audit_plan_and_pricing(
    audit: Mapping[str, Any],
    frozen: Mapping[str, Any],
    *,
    beat_count: int,
) -> tuple[int, int, int, int, int, str]:
    plan = _generated_audit_mapping(audit.get("plan"), "generation plan")
    integer_fields = (
        "target_duration_seconds",
        "scene_count",
        "variants_per_beat",
        "candidate_count",
        "billable_seconds",
        "max_cost_minor",
    )
    expected_integers: dict[str, int] = {}
    for field in integer_fields:
        expected = _strict_positive_int(frozen.get(field))
        if expected is None or _strict_positive_int(plan.get(field)) != expected:
            _reject_generated_audit(
                f"generation plan {field} conflicts with immutable Run lineage"
            )
        expected_integers[field] = expected
    ratio = str(frozen.get("ratio") or "").strip()
    if not ratio or plan.get("ratio") != ratio or plan.get("clip_duration_seconds") != 5:
        _reject_generated_audit("generation plan ratio or clip duration is invalid")
    scene_count = expected_integers["scene_count"]
    variants_per_beat = expected_integers["variants_per_beat"]
    candidate_count = expected_integers["candidate_count"]
    billable_seconds = expected_integers["billable_seconds"]
    if (
        scene_count != beat_count
        or candidate_count != scene_count * variants_per_beat
        or billable_seconds != candidate_count * 5
    ):
        _reject_generated_audit("generation plan cardinality is internally inconsistent")

    frozen_pricing = _generated_audit_mapping(
        frozen.get("pricing_snapshot"),
        "frozen pricing snapshot",
    )
    pricing = _generated_audit_mapping(
        audit.get("pricing_snapshot"),
        "pricing snapshot",
    )
    for field in ("ref", "content_hash", "captured_at", "currency"):
        expected = str(frozen_pricing.get(field) or "").strip()
        if not expected or pricing.get(field) != expected:
            _reject_generated_audit(
                f"pricing snapshot {field} conflicts with immutable Run lineage"
            )
    credit_unit_minor = _strict_positive_int(frozen_pricing.get("credit_unit_minor"))
    cost_per_second_minor = _strict_positive_int(
        frozen_pricing.get("cost_per_second_minor")
    )
    if (
        credit_unit_minor is None
        or cost_per_second_minor is None
        or _strict_positive_int(pricing.get("credit_unit_minor"))
        != credit_unit_minor
        or _strict_positive_int(pricing.get("cost_per_second_minor"))
        != cost_per_second_minor
    ):
        _reject_generated_audit("pricing units conflict with immutable Run lineage")
    return (
        variants_per_beat,
        candidate_count,
        expected_integers["max_cost_minor"],
        credit_unit_minor,
        cost_per_second_minor,
        str(pricing["currency"]),
    )


def _validate_generated_audit_totals(
    audit: Mapping[str, Any],
    *,
    job_count: int,
    accepted_count: int,
    estimated_credits: Decimal,
    incurred_credits: Decimal,
    credit_unit_minor: int,
    billable_seconds: int,
    cost_per_second_minor: int,
    max_cost_minor: int,
    currency: str,
) -> None:
    authorized_decimal = estimated_credits * credit_unit_minor
    incurred_decimal = incurred_credits * credit_unit_minor
    if (
        authorized_decimal != authorized_decimal.to_integral_value()
        or incurred_decimal != incurred_decimal.to_integral_value()
    ):
        _reject_generated_audit("job costs do not resolve to minor currency units")
    authorized_minor = int(authorized_decimal)
    incurred_minor = int(incurred_decimal)
    if (
        authorized_minor != billable_seconds * cost_per_second_minor
        or incurred_minor > authorized_minor
        or authorized_minor > max_cost_minor
        or incurred_minor > max_cost_minor
    ):
        _reject_generated_audit("job costs exceed or conflict with the frozen budget")

    total = _generated_audit_mapping(audit.get("total_cost"), "total cost")
    if (
        total.get("currency") != currency
        or _generated_non_negative_decimal(
            total.get("estimated_credits"),
            "total estimated credits",
        )
        != estimated_credits
        or _generated_non_negative_decimal(
            total.get("incurred_credits"),
            "total incurred credits",
        )
        != incurred_credits
        or _strict_non_negative_int(total.get("authorized_amount_minor"))
        != authorized_minor
        or _strict_non_negative_int(total.get("incurred_amount_minor"))
        != incurred_minor
    ):
        _reject_generated_audit("total cost does not equal the audited job sum")

    safety = _generated_audit_mapping(audit.get("safety"), "safety summary")
    rejected_count = job_count - accepted_count
    if (
        _strict_non_negative_int(safety.get("accepted_outputs")) != accepted_count
        or _strict_non_negative_int(safety.get("rejected_outputs")) != rejected_count
        or accepted_count + rejected_count != job_count
    ):
        _reject_generated_audit("safety counts do not equal audited job verdicts")


def _generated_candidates_by_artifact(
    beats: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
    result: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for beat in beats:
        candidates = _generated_audit_objects(
            beat.get("candidates"),
            "candidate manifest candidates",
            allow_empty=True,
        )
        for candidate in candidates:
            artifact_id = str(candidate.get("artifact_id") or "").strip()
            if not artifact_id or artifact_id in result:
                _reject_generated_audit("candidate artifact identity is missing or ambiguous")
            result[artifact_id] = (candidate, beat)
    return result


def _generated_materialized_by_artifact(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    values = _generated_audit_objects(
        manifest.get("materialized_assets"),
        "materialized asset lineage",
    )
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        artifact_id = str(value.get("artifact_id") or "").strip()
        if not artifact_id or artifact_id in result:
            _reject_generated_audit(
                "materialized artifact identity is missing or ambiguous"
            )
        result[artifact_id] = value
    return result


def _validate_generated_candidate_lineage(
    candidate: Mapping[str, Any],
    beat: Mapping[str, Any],
    materialized: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    terms: Mapping[str, Any],
    rights_license_basis: str,
) -> None:
    expected_candidate = {
        "artifact_id": job.get("output_artifact_id"),
        "content_hash": job.get("output_content_hash"),
        "artifact_filename": job.get("output_filename"),
        "media_type": job.get("output_media_type"),
        "byte_size": job.get("output_byte_size"),
    }
    if any(
        candidate.get(field) != expected
        for field, expected in expected_candidate.items()
    ):
        _reject_generated_audit("candidate output identity conflicts with its accepted job")
    if (
        beat.get("id") != job.get("beat_id")
        or beat.get("sequence") != job.get("beat_sequence")
    ):
        _reject_generated_audit("candidate Beat identity conflicts with its accepted job")
    verification = _generated_audit_mapping(
        job.get("visual_verification"),
        "accepted job visual verification",
    )
    hard_constraints = _generated_audit_mapping(
        candidate.get("hard_constraints"),
        "candidate hard-constraint evidence",
    )
    cut_evidence = _generated_audit_mapping(
        candidate.get("cut_evidence"),
        "candidate cut evidence",
    )
    if (
        hard_constraints.get("passed") is not True
        or hard_constraints.get("must_match") != verification.get("must_match")
        or hard_constraints.get("must_not_match") != verification.get("must_not_match")
        or cut_evidence.get("cut_safe") is not verification.get("cut_safe")
        or cut_evidence.get("semantic_complete")
        is not verification.get("semantic_complete")
    ):
        _reject_generated_audit(
            "candidate verification evidence conflicts with its accepted job"
        )

    expected_materialized = {
        "artifact_id": job.get("output_artifact_id"),
        "kind": "asset",
        "content_hash": job.get("output_content_hash"),
        "filename": job.get("output_filename"),
        "media_type": job.get("output_media_type"),
        "byte_size": job.get("output_byte_size"),
        "asset_id": candidate.get("asset_id"),
        "asset_file_id": candidate.get("asset_file_id"),
    }
    if any(
        materialized.get(field) != expected
        for field, expected in expected_materialized.items()
    ) or any(
        not str(candidate.get(field) or "").strip()
        for field in ("asset_id", "asset_file_id")
    ):
        _reject_generated_audit(
            "materialized asset identity conflicts with its accepted candidate"
        )
    _validate_generated_rights_lineage(
        candidate.get("rights_evidence"),
        terms,
        rights_license_basis=rights_license_basis,
    )


def _validate_generated_rights_lineage(
    value: object,
    terms: Mapping[str, Any],
    *,
    rights_license_basis: str,
) -> None:
    rights = _generated_audit_mapping(value, "candidate rights evidence")
    expected = {
        "copyright_status": "licensed",
        "status_allowed": True,
        "evidence_required": True,
        "evidence_present": True,
        "verified": True,
        "verification_basis": ["operator_confirmed_provider_terms_snapshot"],
        "rejection_codes": [],
    }
    if any(rights.get(field) != item for field, item in expected.items()):
        _reject_generated_audit("candidate rights verdict is inconsistent")
    sources = _generated_audit_objects(rights.get("sources"), "rights sources")
    if len(sources) != 1:
        _reject_generated_audit("candidate rights source must be unique")
    source = sources[0]
    metadata = _generated_audit_mapping(source.get("metadata"), "rights metadata")
    expected_source = {
        "evidence_type": "operator_confirmed_provider_terms",
        "license": rights_license_basis,
        "locator": terms.get("ref"),
        "verified_at": terms.get("captured_at"),
        "content_hash": terms.get("content_hash"),
    }
    if (
        any(source.get(field) != item for field, item in expected_source.items())
        or metadata
        != {
            "output_rights_confirmed": True,
            "output_rights_license_basis": rights_license_basis,
            "terms_content_hash": terms.get("content_hash"),
        }
    ):
        _reject_generated_audit("candidate rights source conflicts with the audit terms")


def _validate_generated_rights_snapshot(
    audit: Mapping[str, Any],
    frozen: Mapping[str, Any],
    terms: Mapping[str, Any],
) -> str:
    rights_snapshot = _generated_audit_mapping(
        audit.get("rights_snapshot"),
        "rights snapshot",
    )
    license_basis = str(frozen.get("output_rights_license_basis") or "").strip()
    if (
        frozen.get("output_rights_confirmed") is not True
        or not license_basis
        or rights_snapshot
        != {
            "output_rights_confirmed": True,
            "output_rights_license_basis": license_basis,
            "terms_content_hash": terms.get("content_hash"),
        }
    ):
        _reject_generated_audit(
            "rights snapshot conflicts with immutable operator attestation"
        )
    return license_basis


def _full_ai_generation_lineage(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    framefactory = _generated_audit_mapping(
        snapshot.get("_framefactory"),
        "immutable _framefactory snapshot",
    )
    composition = _generated_audit_mapping(
        framefactory.get("composition_snapshot"),
        "immutable composition snapshot",
    )
    if (
        snapshot.get("visual_source_mode") != "generated_only"
        or composition.get("visual_source_mode") != "generated_only"
    ):
        _reject_generated_audit("Run is not frozen as generated_only")
    full_ai = _generated_audit_mapping(
        composition.get("full_ai_generation"),
        "immutable Full-AI generation snapshot",
    )
    if full_ai.get("output_rights_confirmed") is not True:
        _reject_generated_audit("provider output rights were not frozen as confirmed")
    return full_ai


def _generated_audit_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject_generated_audit(f"{field} must be an object")
    return value


def _generated_audit_objects(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or (not value and not allow_empty)
        or any(not isinstance(item, Mapping) for item in value)
    ):
        _reject_generated_audit(f"{field} must contain objects")
    return tuple(value)


def _generated_audit_strings(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        _reject_generated_audit(f"{field} must contain non-empty strings")
    return tuple(value)


def _generated_constraint_checks(
    value: object,
    field: str,
    *,
    expected_match: bool,
) -> tuple[Mapping[str, Any], ...]:
    checks = _generated_audit_objects(value, field, allow_empty=True)
    for check in checks:
        if (
            not str(check.get("term") or "").strip()
            or check.get("matched") is not expected_match
        ):
            _reject_generated_audit(f"{field} contains a false or malformed verdict")
    return checks


def _generated_non_negative_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        _reject_generated_audit(f"{field} must be a non-negative finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _reject_generated_audit(f"{field} must be a non-negative finite number")
    if not number.is_finite() or number < 0:
        _reject_generated_audit(f"{field} must be a non-negative finite number")
    return number


def _reject_generated_audit(reason: str) -> None:
    raise PermanentStepError(f"generated material audit {reason}")


def _validate_script_beat_lineage(
    script: Mapping[str, Any],
    beats: Sequence[Mapping[str, Any]],
) -> None:
    expected = normalize_beats(script)
    if len(expected) != len(beats):
        raise PermanentStepError("candidate manifest does not cover every script Beat")
    for expected_beat, manifest_beat in zip(expected, beats, strict=True):
        must_match = _string_terms(manifest_beat.get("must_match"))
        must_not_match = _string_terms(manifest_beat.get("must_not_match"))
        if (
            str(manifest_beat.get("id") or "") != expected_beat.id
            or _strict_positive_int(manifest_beat.get("sequence"))
            != expected_beat.sequence
            or str(manifest_beat.get("narration") or "").strip()
            != expected_beat.narration
            or str(manifest_beat.get("visual_description") or "").strip()
            != expected_beat.visual_description
            or must_match != expected_beat.must_match
            or must_not_match != expected_beat.must_not_match
        ):
            raise PermanentStepError(
                "candidate manifest Beat lineage conflicts with the input script"
            )


def _bind_timing_units(
    units: Sequence[Mapping[str, Any]],
    beats: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
    if len(units) != len(beats):
        raise PermanentStepError("narration timing does not cover every candidate Beat")
    result: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for index, (unit, beat) in enumerate(zip(units, beats, strict=True), start=1):
        unit_id = str(unit.get("id") or "")
        beat_id = str(beat.get("id") or "")
        unit_sequence = _strict_positive_int(unit.get("sequence"))
        beat_sequence = _strict_positive_int(beat.get("sequence"))
        fallback_segment = unit_id.startswith(("sentence-", "timing-"))
        if (
            unit_sequence != beat_sequence
            or (unit_id != beat_id and not fallback_segment)
            or _identity_text(unit.get("text"))
            != _identity_text(beat.get("narration"))
        ):
            raise PermanentStepError(
                f"narration timing unit {index} conflicts with candidate Beat identity"
            )
        result.append((unit, beat))
    return tuple(result)


def _validated_materialized_assets(
    manifest: Mapping[str, Any],
    asset_refs: Mapping[str, ArtifactRef],
) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
    raw = manifest.get("materialized_assets")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise PermanentStepError("candidate manifest has no materialized asset lineage")
    lookup: dict[str, str] = {}
    by_artifact: dict[str, Mapping[str, Any]] = {}
    for value in raw:
        if not isinstance(value, Mapping):
            raise PermanentStepError("materialized asset lineage must contain objects")
        artifact_id = str(value.get("artifact_id") or "").strip()
        artifact = asset_refs.get(artifact_id)
        if not artifact_id or artifact is None or artifact_id in by_artifact:
            raise PermanentStepError("materialized asset lineage is ambiguous or unavailable")
        expected_values = {
            "kind": artifact.kind,
            "filename": artifact.filename,
            "media_type": artifact.media_type,
            "content_hash": artifact.content_hash,
            "byte_size": artifact.byte_size,
        }
        for key, expected in expected_values.items():
            if key in value and value.get(key) != expected:
                raise PermanentStepError(
                    f"materialized asset {key} conflicts with its artifact"
                )
        by_artifact[artifact_id] = value
        for key in ("asset_file_id", "asset_id"):
            identity = str(value.get(key) or "").strip()
            if not identity:
                raise PermanentStepError(
                    f"materialized asset lineage lacks {key}"
                )
            previous = lookup.setdefault(identity, artifact_id)
            if previous != artifact_id:
                raise PermanentStepError("materialized asset identity is ambiguous")
    return lookup, by_artifact


def _materialized_candidates(
    beat: Mapping[str, Any],
    asset_refs: Mapping[str, ArtifactRef],
    materialized: Mapping[str, str],
    materialized_by_artifact: Mapping[str, Mapping[str, Any]],
    *,
    manifest_content_hash: str,
) -> tuple[dict[str, Any], ...]:
    raw = beat.get("candidates")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    result: list[dict[str, Any]] = []
    ranks: set[int] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise PermanentStepError("candidate manifest candidates must be objects")
        rank = _strict_positive_int(value.get("rank"))
        if rank is None or rank in ranks:
            raise PermanentStepError("candidate manifest ranks must be positive and unique")
        ranks.add(rank)
        hard_constraints = value.get("hard_constraints")
        hard_constraints = (
            hard_constraints if isinstance(hard_constraints, Mapping) else {}
        )
        rights_evidence = value.get("rights_evidence")
        rights_evidence = (
            rights_evidence if isinstance(rights_evidence, Mapping) else {}
        )
        rejection_codes = value.get("rejection_codes")
        if (
            value.get("eligible") is not True
            or value.get("materialized") is not True
            or hard_constraints.get("passed") is not True
            or rights_evidence.get("status_allowed") is not True
            or rights_evidence.get("verified") is not True
            or not isinstance(rejection_codes, list)
            or bool(rejection_codes)
        ):
            continue
        _validate_hard_constraint_evidence(beat, value, hard_constraints)
        _validate_rights_evidence(rights_evidence)
        artifact_id = _candidate_artifact_id(
            value,
            materialized,
            materialized_by_artifact,
        )
        artifact = asset_refs.get(artifact_id)
        if artifact is None:
            continue
        candidate_hash = str(value.get("content_hash") or "").strip()
        if not candidate_hash or candidate_hash != artifact.content_hash:
            raise PermanentStepError(
                "candidate manifest content hash conflicts with its asset artifact"
            )
        media_kind = _media_kind(artifact)
        if str(value.get("asset_kind") or "") != media_kind:
            raise PermanentStepError(
                "candidate asset kind conflicts with its materialized artifact"
            )
        if str(value.get("media_type") or "") != artifact.media_type:
            raise PermanentStepError(
                "candidate media type conflicts with its materialized artifact"
            )
        if str(value.get("artifact_filename") or "") != artifact.filename:
            raise PermanentStepError(
                "candidate filename conflicts with its materialized artifact"
            )
        if _strict_non_negative_int(value.get("byte_size")) != artifact.byte_size:
            raise PermanentStepError(
                "candidate byte size conflicts with its materialized artifact"
            )
        for lineage_key in ("candidate_id", "asset_id", "asset_file_id", "analysis_id", "segment_id"):
            if not str(value.get(lineage_key) or "").strip():
                raise PermanentStepError(f"candidate lineage lacks {lineage_key}")
        bounds = _candidate_source_bounds(value, media_kind)
        if media_kind == "video" and bounds is None:
            continue
        source_start, source_end = bounds or (0.0, 0.0)
        if media_kind == "video":
            physical_duration_ms = _positive_number(value.get("source_duration_ms"))
            if (
                physical_duration_ms is None
                or source_end * 1000 > physical_duration_ms + 0.001
            ):
                raise PermanentStepError(
                    "candidate source window exceeds its catalog media duration"
                )
        beat_id = str(beat.get("id", ""))
        identity = "\0".join(
            (
                manifest_content_hash,
                beat_id,
                artifact_id,
                f"{source_start:.6f}",
                f"{source_end:.6f}",
                str(value.get("rank", index + 1)),
            )
        )
        selection_id = f"selection-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
        labels = value.get("labels") or value.get("keywords") or ()
        cut_evidence = value.get("cut_evidence")
        cut_evidence = cut_evidence if isinstance(cut_evidence, Mapping) else {}
        result.append(
            {
                **dict(value),
                "selection_id": selection_id,
                "beat_id": beat_id,
                "beat_sequence": _positive_int(beat.get("sequence"), 1),
                "artifact_id": artifact_id,
                "asset_id": str(value.get("asset_id") or "") or None,
                "content_hash": artifact.content_hash,
                "filename": artifact.filename,
                "media_type": artifact.media_type,
                "media_kind": media_kind,
                "source_start_seconds": source_start,
                "source_end_seconds": source_end,
                "start_ms": source_start * 1000,
                "end_ms": source_end * 1000,
                "selected_for_scene": str(
                    beat.get("visual_description") or beat.get("description") or ""
                ),
                "selected_for_narration": str(beat.get("narration") or ""),
                "description": str(value.get("description") or value.get("visual_description") or ""),
                "source_transcript": str(value.get("source_transcript") or value.get("transcript") or ""),
                "labels": list(labels)
                if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes))
                else [],
                "cut_safe": value.get("cut_safe") is True
                or cut_evidence.get("cut_safe") is True,
                "semantic_complete": value.get("semantic_complete") is True
                or cut_evidence.get("semantic_complete") is True,
                "candidate_rank": _positive_int(value.get("rank"), index + 1),
            }
        )
    result.sort(key=lambda item: (item["candidate_rank"], item["selection_id"]))
    return tuple(result)


def _candidate_artifact_id(
    candidate: Mapping[str, Any],
    materialized: Mapping[str, str],
    materialized_by_artifact: Mapping[str, Mapping[str, Any]],
) -> str:
    nested = candidate.get("materialized_asset")
    nested = nested if isinstance(nested, Mapping) else {}
    direct = str(
        candidate.get("artifact_id")
        or candidate.get("materialized_artifact_id")
        or nested.get("artifact_id")
        or ""
    ).strip()
    resolved: set[str] = set()
    for key in ("asset_file_id", "asset_id"):
        identity = str(candidate.get(key) or "").strip()
        if identity and identity in materialized:
            resolved.add(materialized[identity])
    if len(resolved) > 1:
        raise PermanentStepError("candidate materialized identity is ambiguous")
    resolved_id = next(iter(resolved), "")
    if direct and resolved_id and direct != resolved_id:
        raise PermanentStepError(
            "candidate artifact conflicts with materialized asset lineage"
        )
    artifact_id = direct or resolved_id
    record = materialized_by_artifact.get(artifact_id)
    if not artifact_id or record is None:
        raise PermanentStepError(
            "candidate is marked materialized without materialized asset lineage"
        )
    # Retrieval deduplicates bytes by content hash.  Several database file
    # lineages may therefore point directly at one published Artifact while
    # materialized_assets records the first physical source.  The caller still
    # verifies this candidate's hash, byte size and media metadata against the
    # Artifact before accepting it.
    return artifact_id


def _validate_hard_constraint_evidence(
    beat: Mapping[str, Any],
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    if not hard_constraint_evidence_valid(beat, candidate, evidence):
        raise PermanentStepError(
            "candidate hard-constraint evidence is internally inconsistent"
        )


def _validate_rights_evidence(evidence: Mapping[str, Any]) -> None:
    if not rights_evidence_valid(evidence):
        raise PermanentStepError("candidate rights evidence is internally inconsistent")


def _candidate_source_bounds(
    candidate: Mapping[str, Any], media_kind: str
) -> tuple[float, float] | None:
    if media_kind == "image":
        return 0.0, 0.0
    nested = candidate.get("clip")
    nested = nested if isinstance(nested, Mapping) else {}
    source_window = candidate.get("source_window")
    source_window = source_window if isinstance(source_window, Mapping) else {}
    start_ms_value = _number(source_window.get("start_ms"))
    end_ms_value = _number(source_window.get("end_ms"))
    duration_ms_value = _number(source_window.get("duration_ms"))
    if (
        start_ms_value is None
        or end_ms_value is None
        or duration_ms_value is None
        or start_ms_value < 0
        or end_ms_value <= start_ms_value
        or abs((end_ms_value - start_ms_value) - duration_ms_value) > 0.001
    ):
        return None
    start = _first_number(
        candidate.get("source_start_seconds"),
        candidate.get("start_seconds"),
        nested.get("start_seconds"),
        source_window.get("start_seconds"),
    )
    end = _first_number(
        candidate.get("source_end_seconds"),
        candidate.get("end_seconds"),
        nested.get("end_seconds"),
        source_window.get("end_seconds"),
    )
    if start is None:
        start_ms = _first_number(
            candidate.get("start_ms"),
            nested.get("start_ms"),
            start_ms_value,
        )
        start = start_ms / 1000 if start_ms is not None else 0.0
    if end is None:
        end_ms = _first_number(
            candidate.get("end_ms"),
            nested.get("end_ms"),
            end_ms_value,
        )
        end = end_ms / 1000 if end_ms is not None else None
    if end is None:
        duration = _positive_number(
            candidate.get("source_duration_seconds") or candidate.get("duration_seconds")
        )
        end = start + duration if duration is not None else None
    if end is None or start < 0 or end <= start:
        return None
    return start, end


def _plan_timing_unit(
    unit: Mapping[str, Any],
    beat: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    eligible, shot_count = _prepare_timing_unit(unit, beat, candidates)
    return _plan_prepared_timing_unit(unit, beat, eligible, shot_count)


def _prepare_timing_unit(
    unit: Mapping[str, Any],
    beat: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], ...], int]:
    """Resolve a unit's candidates and shot count without constructing shots."""

    start = _required_number(unit, "start_seconds")
    end = _required_number(unit, "end_seconds")
    duration = end - start
    if duration < 0.25:
        raise PermanentStepError("timing unit is too short for a safe visual edit")
    base_count = _normal_shot_count(duration)
    maximum_count = min(_MAX_SHOTS_PER_TIMING_UNIT, max(base_count, math.ceil(duration / 0.25)))
    eligible: list[Mapping[str, Any]] = []
    shot_count = base_count
    for count in range(base_count, maximum_count + 1):
        shot_duration = duration / count
        eligible = [
            candidate
            for candidate in candidates
            if candidate["media_kind"] == "image"
            or _available_video_seconds(candidate) + 0.001 >= shot_duration
        ]
        if eligible and shot_duration >= 0.25:
            shot_count = count
            break
    if not eligible:
        raise PermanentStepError(
            f"video candidates for Beat {beat['id']} are shorter than every safe EDL interval"
        )
    return tuple(eligible), shot_count


def _plan_prepared_timing_unit(
    unit: Mapping[str, Any],
    beat: Mapping[str, Any],
    eligible: Sequence[Mapping[str, Any]],
    shot_count: int,
) -> list[dict[str, Any]]:
    """Construct shots only after the Run-wide EDL budget has passed."""

    if not eligible or shot_count < 1 or shot_count > _MAX_SHOTS_PER_TIMING_UNIT:
        raise PermanentStepError("prepared timing unit has an invalid shot plan")
    start = _required_number(unit, "start_seconds")
    end = _required_number(unit, "end_seconds")
    duration = end - start
    shot_duration = duration / shot_count
    duration_epsilon = max(1e-9, shot_duration * 1e-7)
    planner = plan_edit_timeline(
        {
            # One timing unit is already the semantic boundary. Removing only
            # sentence delimiters keeps the existing semantic planner while
            # preventing it from silently subdividing the frozen partition.
            "narration": _single_planning_sentence(str(unit.get("text", ""))),
            "scenes": [
                str(beat.get("visual_description") or beat.get("description") or "")
            ],
        },
        eligible,
        duration,
        minimum_shot_seconds=max(0.25, shot_duration - duration_epsilon),
        target_shot_seconds=shot_duration,
        maximum_shot_seconds=shot_duration + duration_epsilon,
    )
    if len(planner) != shot_count:
        raise PermanentStepError("timeline planner did not honor the strict timing partition")
    usage: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for index, planned in enumerate(planner):
        candidate = eligible[int(planned["asset_index"])]
        exact_start = start + duration * index / shot_count
        exact_end = end if index == shot_count - 1 else start + duration * (index + 1) / shot_count
        exact_duration = exact_end - exact_start
        source_start, source_end = _strict_source_window(
            candidate,
            exact_duration,
            usage[str(candidate["selection_id"])],
        )
        usage[str(candidate["selection_id"])] += 1
        if candidate["media_kind"] == "video" and source_end - source_start + 0.001 < exact_duration:
            raise PermanentStepError("video candidate is shorter than its EDL shot")
        result.append(
            {
                "ordinal": index,
                "beat_id": str(beat["id"]),
                "beat_sequence": _positive_int(beat.get("sequence"), 1),
                "selection_id": str(candidate["selection_id"]),
                "artifact_id": str(candidate["artifact_id"]),
                "media_kind": str(candidate["media_kind"]),
                "timeline_start_seconds": round(exact_start, 6),
                "timeline_end_seconds": round(exact_end, 6),
                "duration_seconds": round(exact_duration, 6),
                "source_start_seconds": round(source_start, 6),
                "source_end_seconds": round(source_end, 6),
                "padding_seconds": 0.0,
                "speed": 1.0,
                "loop": candidate["media_kind"] == "image",
                "narration": str(unit.get("text", "")),
                "semantic_score": planned.get("semantic_score", 0.0),
                "story_path_score": planned.get("story_path_score", 0.0),
                "transition": planned.get("transition", "continuity_cut"),
                "cut_evidence": planned.get("cut_evidence", "candidate"),
            }
        )
    return result


def _strict_source_window(
    candidate: Mapping[str, Any], duration: float, reuse: int
) -> tuple[float, float]:
    if candidate["media_kind"] == "image":
        return 0.0, duration
    start = _required_number(candidate, "source_start_seconds")
    end = _required_number(candidate, "source_end_seconds")
    available_offset = end - start - duration
    if available_offset < -0.001:
        raise PermanentStepError("video source window cannot cover its EDL duration")
    fraction = ((reuse * 0.38196601125) % 1.0) if reuse else 0.0
    source_start = start + max(0.0, available_offset) * fraction
    return source_start, source_start + duration


def _selection_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    source_start_ms = round(float(candidate["source_start_seconds"]) * 1000)
    source_end_ms = round(float(candidate["source_end_seconds"]) * 1000)
    return {
        "selection_id": candidate["selection_id"],
        "beat_id": candidate["beat_id"],
        "beat_sequence": candidate["beat_sequence"],
        "candidate_rank": candidate["candidate_rank"],
        "asset_id": candidate.get("asset_id"),
        "artifact_id": candidate["artifact_id"],
        "content_hash": candidate["content_hash"],
        "filename": candidate["filename"],
        "media_type": candidate["media_type"],
        "media_kind": candidate["media_kind"],
        "source_start_seconds": round(float(candidate["source_start_seconds"]), 6),
        "source_end_seconds": round(float(candidate["source_end_seconds"]), 6),
        "source_start_ms": source_start_ms,
        "source_end_ms": source_end_ms,
        "cut_safe": candidate.get("cut_safe") is True,
        "semantic_complete": candidate.get("semantic_complete") is True,
    }


def _quantize_timeline(
    shots: list[dict[str, Any]],
    selections: Mapping[str, Mapping[str, Any]],
    *,
    total_duration: float,
    frame_rate_num: int,
    frame_rate_den: int,
) -> float:
    frames_per_second = frame_rate_num / frame_rate_den
    total_frames = max(1, round(total_duration * frames_per_second))
    if total_frames < len(shots):
        raise PermanentStepError("output frame rate cannot represent every planned shot")
    cursor_frame = 0
    for index, shot in enumerate(shots):
        remaining = len(shots) - index - 1
        if index == len(shots) - 1:
            end_frame = total_frames
        else:
            desired_end = round(float(shot["timeline_end_seconds"]) * frames_per_second)
            end_frame = max(cursor_frame + 1, desired_end)
            end_frame = min(end_frame, total_frames - remaining)
        start_frame = cursor_frame
        start_seconds = start_frame / frames_per_second
        end_seconds = end_frame / frames_per_second
        duration_seconds = end_seconds - start_seconds
        selection = selections[str(shot["selection_id"])]
        if shot["media_kind"] == "image":
            source_start_ms = 0
            source_end_ms = math.ceil(duration_seconds * 1000 - 1e-9)
        else:
            allowed_start_ms = int(selection["source_start_ms"])
            allowed_end_ms = int(selection["source_end_ms"])
            required_ms = math.ceil(duration_seconds * 1000 - 1e-9)
            if allowed_end_ms - allowed_start_ms < required_ms:
                raise PermanentStepError(
                    "video candidate is shorter than its frame-quantized EDL shot"
                )
            desired_start_ms = round(float(shot["source_start_seconds"]) * 1000)
            source_start_ms = min(
                max(allowed_start_ms, desired_start_ms),
                allowed_end_ms - required_ms,
            )
            source_end_ms = source_start_ms + required_ms
        shot.update(
            {
                "timeline_start_frame": start_frame,
                "timeline_end_frame": end_frame,
                "timeline_start_seconds": round(start_seconds, 9),
                "timeline_end_seconds": round(end_seconds, 9),
                "duration_seconds": round(duration_seconds, 9),
                "source_start_ms": source_start_ms,
                "source_end_ms": source_end_ms,
                "source_start_seconds": round(source_start_ms / 1000, 6),
                "source_end_seconds": round(source_end_ms / 1000, 6),
            }
        )
        cursor_frame = end_frame
    return total_frames / frames_per_second


def _output_frame_rate(snapshot: Mapping[str, Any]) -> tuple[int, int]:
    framefactory = snapshot.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    production = composition.get("production_settings", {})
    production = production if isinstance(production, Mapping) else {}
    raw = production.get("frame_rate", 30)
    if isinstance(raw, bool):
        return 30, 1
    try:
        frame_rate = int(raw)
    except (TypeError, ValueError):
        return 30, 1
    return (frame_rate, 1) if 1 <= frame_rate <= 120 else (30, 1)


def _subtitle_cues(timing: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = timing.get("segments")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(
        {
            "text": str(value.get("text", "")),
            "start_seconds": value.get("start_seconds"),
            "end_seconds": value.get("end_seconds"),
            "estimated": value.get("estimated") is True,
        }
        for value in raw
        if isinstance(value, Mapping) and str(value.get("text", "")).strip()
    )


def _media_kind(artifact: ArtifactRef) -> str:
    if artifact.media_type.startswith("image/") or any(
        artifact.filename.lower().endswith(suffix) for suffix in _IMAGE_SUFFIXES
    ):
        return "image"
    if artifact.media_type.startswith("video/"):
        return "video"
    raise PermanentStepError("candidate artifact is neither an image nor a video")


def _available_video_seconds(candidate: Mapping[str, Any]) -> float:
    if candidate.get("media_kind") != "video":
        return math.inf
    return max(
        0.0,
        _required_number(candidate, "source_end_seconds")
        - _required_number(candidate, "source_start_seconds"),
    )


def _normal_shot_count(duration: float) -> int:
    lower = max(1, math.ceil(duration / 8.0))
    upper = max(1, math.floor(duration / 2.2))
    return min(upper, max(lower, math.ceil(duration / 4.8)))


def _reserve_shot_budget(current: int, requested: int) -> int:
    if current < 0 or requested < 1:
        raise PermanentStepError("timeline.align received an invalid shot budget")
    total = current + requested
    if total > _MAX_EDL_SHOTS:
        raise PermanentStepError(
            "timeline.align exceeds the 100000 EDL shot limit"
        )
    return total


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    try:
        return next(artifact for artifact in context.input_artifacts if artifact.kind == kind)
    except StopIteration as exc:
        raise PermanentStepError(f"required {kind} artifact is unavailable") from exc


def _required_number(value: Mapping[str, Any], key: str) -> float:
    number = _number(value.get(key))
    if number is None:
        raise PermanentStepError(f"{key} must be finite")
    return number


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _rounded_positive(value: float, *, digits: int, field: str) -> float:
    rounded = round(value, digits)
    if not math.isfinite(rounded) or rounded <= 0:
        raise PermanentStepError(f"{field} rounds to a non-positive schema value")
    return rounded


def _first_number(*values: object) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _strict_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _strict_non_negative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _string_terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _identity_text(value: object) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", str(value)).casefold()


def _single_planning_sentence(value: str) -> str:
    return " ".join(part for part in value.translate(str.maketrans("", "", "。！？!?;；")).split() if part)


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
