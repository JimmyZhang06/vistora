"""Strict, provider-independent validation for frozen edit decision lists."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from framefactory.steps import ArtifactRef
from framefactory.worker.retrieval.evidence import (
    hard_constraint_evidence_valid,
    rights_evidence_valid,
)


class EdlValidationError(ValueError):
    """Raised before render when a frozen edit contract is not executable."""


@dataclass(frozen=True, slots=True)
class ValidatedEdlShot:
    ordinal: int
    selection_id: str
    artifact_id: str
    media_kind: str
    timeline_start_seconds: float
    timeline_end_seconds: float
    duration_seconds: float
    source_start_seconds: float
    source_end_seconds: float


def validate_edl(
    material_selection: Mapping[str, Any],
    timeline: Mapping[str, Any],
    asset_refs: Mapping[str, ArtifactRef],
    *,
    audio_ref: ArtifactRef | None = None,
    narration_timing_ref: ArtifactRef | None = None,
    candidate_manifest_ref: ArtifactRef | None = None,
    candidate_manifest: Mapping[str, Any] | None = None,
    audio_duration_seconds: float | None = None,
    source_durations: Mapping[str, float] | None = None,
) -> tuple[ValidatedEdlShot, ...]:
    """Validate every source and timeline bound without inventing padding.

    Images are allowed to loop because they have no temporal source boundary.
    Video shots must fit both their selected safe window and, when supplied by
    the renderer, the duration probed from the actual materialized bytes.
    """

    if material_selection.get("operation") != "timeline.align":
        raise EdlValidationError("material_selection operation must be timeline.align")
    if timeline.get("operation") != "timeline.align":
        raise EdlValidationError("timeline operation must be timeline.align")
    if timeline.get("time_base") != "seconds":
        raise EdlValidationError("timeline time_base must be seconds")
    policy = timeline.get("edl_policy")
    if not isinstance(policy, Mapping) or dict(policy) != {
        "video_padding": "forbidden",
        "video_loop": "forbidden",
        "image_loop": "allowed",
        "speed": 1.0,
    }:
        raise EdlValidationError("timeline EDL policy is unsupported")

    frame_rate = timeline.get("output_frame_rate")
    if not isinstance(frame_rate, Mapping):
        raise EdlValidationError("timeline.output_frame_rate must be an object")
    frame_rate_num = _positive_integer(frame_rate.get("num"))
    frame_rate_den = _positive_integer(frame_rate.get("den"))
    if frame_rate_num is None or frame_rate_den is None:
        raise EdlValidationError("timeline output frame rate must be positive integers")
    frames_per_second = frame_rate_num / frame_rate_den
    coordinate_tolerance = 1e-6

    selections_raw = material_selection.get("selections")
    shots_raw = timeline.get("shots")
    if not isinstance(selections_raw, Sequence) or isinstance(
        selections_raw, (str, bytes)
    ):
        raise EdlValidationError("material_selection.selections must be an array")
    if not isinstance(shots_raw, Sequence) or isinstance(shots_raw, (str, bytes)):
        raise EdlValidationError("timeline.shots must be an array")
    if not selections_raw:
        raise EdlValidationError("material_selection contains no selections")
    if not shots_raw:
        raise EdlValidationError("timeline contains no shots")

    selections: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(selections_raw):
        if not isinstance(value, Mapping):
            raise EdlValidationError(f"selection {index} must be an object")
        selection_id = str(value.get("selection_id", "")).strip()
        artifact_id = str(value.get("artifact_id", "")).strip()
        media_kind = str(value.get("media_kind", "")).strip()
        beat_id = str(value.get("beat_id", "")).strip()
        beat_sequence = _positive_integer(value.get("beat_sequence"))
        candidate_rank = _positive_integer(value.get("candidate_rank"))
        if not selection_id or selection_id in selections:
            raise EdlValidationError("selection IDs must be non-empty and unique")
        if not artifact_id or artifact_id not in asset_refs:
            raise EdlValidationError(
                f"selection {selection_id} references an unavailable asset artifact"
            )
        if media_kind not in {"image", "video"}:
            raise EdlValidationError(
                f"selection {selection_id} has unsupported media_kind"
            )
        if not beat_id or beat_sequence is None or candidate_rank is None:
            raise EdlValidationError(
                f"selection {selection_id} has invalid Beat lineage"
            )
        artifact = asset_refs[artifact_id]
        expected_hash = str(value.get("content_hash", "")).strip()
        if not expected_hash or expected_hash != artifact.content_hash:
            raise EdlValidationError(
                f"selection {selection_id} content hash does not match its artifact"
            )
        if media_kind == "image" and not artifact.media_type.startswith("image/"):
            raise EdlValidationError(
                f"selection {selection_id} media kind conflicts with its artifact"
            )
        if media_kind == "video" and not artifact.media_type.startswith("video/"):
            raise EdlValidationError(
                f"selection {selection_id} media kind conflicts with its artifact"
            )
        if str(value.get("filename") or "") != artifact.filename:
            raise EdlValidationError(
                f"selection {selection_id} filename conflicts with its artifact"
            )
        if str(value.get("media_type") or "") != artifact.media_type:
            raise EdlValidationError(
                f"selection {selection_id} media type conflicts with its artifact"
            )
        if media_kind == "video":
            allowed_start = _number(value.get("source_start_seconds"))
            allowed_end = _number(value.get("source_end_seconds"))
            allowed_start_ms = _non_negative_integer(value.get("source_start_ms"))
            allowed_end_ms = _positive_integer(value.get("source_end_ms"))
            if allowed_start is None or allowed_end is None or allowed_end <= allowed_start:
                raise EdlValidationError(
                    f"video selection {selection_id} lacks a positive source window"
                )
            if (
                allowed_start_ms is None
                or allowed_end_ms is None
                or allowed_end_ms <= allowed_start_ms
                or abs(allowed_start - allowed_start_ms / 1000) > 0.001
                or abs(allowed_end - allowed_end_ms / 1000) > 0.001
            ):
                raise EdlValidationError(
                    f"video selection {selection_id} has inconsistent millisecond bounds"
                )
        selections[selection_id] = value

    expected_audio_hash = str(timeline.get("audio_content_hash", "")).strip()
    selection_audio_hash = str(
        material_selection.get("audio_content_hash", "")
    ).strip()
    if not expected_audio_hash or selection_audio_hash != expected_audio_hash:
        raise EdlValidationError("timeline and material selection audio hashes conflict")
    if audio_ref is not None and expected_audio_hash != audio_ref.content_hash:
        raise EdlValidationError("timeline audio hash does not match the input audio artifact")
    timing_hash = str(timeline.get("narration_timing_content_hash", "")).strip()
    selection_timing_hash = str(
        material_selection.get("narration_timing_content_hash", "")
    ).strip()
    if not timing_hash or selection_timing_hash != timing_hash:
        raise EdlValidationError("timeline and material selection timing hashes conflict")
    if narration_timing_ref is not None and timing_hash != narration_timing_ref.content_hash:
        raise EdlValidationError(
            "timeline does not belong to the input narration timing artifact"
        )
    manifest_hash = str(
        material_selection.get("candidate_manifest_content_hash", "")
    ).strip()
    if not manifest_hash:
        raise EdlValidationError("material selection lacks candidate manifest lineage")
    if (
        candidate_manifest_ref is not None
        and manifest_hash != candidate_manifest_ref.content_hash
    ):
        raise EdlValidationError(
            "material selection does not belong to the input candidate manifest"
        )
    if candidate_manifest is not None:
        _validate_candidate_manifest_lineage(
            material_selection,
            selections,
            candidate_manifest,
            asset_refs,
        )

    declared_duration = _positive_number(timeline.get("duration_seconds"))
    if declared_duration is None:
        raise EdlValidationError("timeline duration must be positive and finite")
    declared_frames = round(declared_duration * frames_per_second)
    if abs(declared_frames / frames_per_second - declared_duration) > coordinate_tolerance:
        raise EdlValidationError("timeline duration is not aligned to its output frame rate")
    selection_duration = _positive_number(material_selection.get("duration_seconds"))
    if (
        selection_duration is None
        or abs(selection_duration - declared_duration) > coordinate_tolerance
    ):
        raise EdlValidationError("material selection and timeline durations conflict")
    audio_contract_duration = _positive_number(timeline.get("audio_duration_seconds"))
    if (
        audio_contract_duration is None
        or abs(audio_contract_duration - declared_duration)
        > 0.5 / frames_per_second + coordinate_tolerance
    ):
        raise EdlValidationError("timeline duration conflicts with its audio timing contract")
    if audio_duration_seconds is not None:
        measured_audio = _positive_number(audio_duration_seconds)
        if measured_audio is None:
            raise EdlValidationError("measured audio duration must be positive and finite")
        if abs(measured_audio - declared_duration) > 1 / frames_per_second + 1e-6:
            raise EdlValidationError("timeline duration does not match measured narration audio")

    result: list[ValidatedEdlShot] = []
    cursor = 0.0
    cursor_frame = 0
    measured_sources_supplied = source_durations is not None
    source_durations = source_durations or {}
    for index, value in enumerate(shots_raw):
        if not isinstance(value, Mapping):
            raise EdlValidationError(f"timeline shot {index} must be an object")
        ordinal = _integer(value.get("ordinal"), index)
        if ordinal != index:
            raise EdlValidationError("timeline shot ordinals must be contiguous from zero")
        selection_id = str(value.get("selection_id", "")).strip()
        if selection_id not in selections:
            raise EdlValidationError(f"timeline shot {index} references an unknown selection")
        selection = selections[selection_id]
        artifact_id = str(selection["artifact_id"])
        media_kind = str(selection["media_kind"])
        if str(value.get("artifact_id", "")).strip() != artifact_id:
            raise EdlValidationError(
                f"timeline shot {index} artifact conflicts with its selection"
            )
        if str(value.get("media_kind") or "") != media_kind:
            raise EdlValidationError(
                f"timeline shot {index} media kind conflicts with its selection"
            )
        if (
            str(value.get("beat_id") or "")
            != str(selection.get("beat_id") or "")
            or _positive_integer(value.get("beat_sequence"))
            != _positive_integer(selection.get("beat_sequence"))
        ):
            raise EdlValidationError(
                f"timeline shot {index} Beat identity conflicts with its selection"
            )
        start = _number(value.get("timeline_start_seconds"))
        end = _number(value.get("timeline_end_seconds"))
        duration = _positive_number(value.get("duration_seconds"))
        source_start = _number(value.get("source_start_seconds"))
        source_end = _number(value.get("source_end_seconds"))
        start_frame = _non_negative_integer(value.get("timeline_start_frame"))
        end_frame = _positive_integer(value.get("timeline_end_frame"))
        source_start_ms = _non_negative_integer(value.get("source_start_ms"))
        source_end_ms = _positive_integer(value.get("source_end_ms"))
        if None in {start, end, duration, source_start, source_end}:
            raise EdlValidationError(f"timeline shot {index} has invalid numeric bounds")
        assert start is not None and end is not None and duration is not None
        assert source_start is not None and source_end is not None
        if start < 0 or source_start < 0 or end <= start or source_end <= source_start:
            raise EdlValidationError(f"timeline shot {index} has non-positive bounds")
        if (
            start_frame is None
            or end_frame is None
            or end_frame <= start_frame
            or start_frame != cursor_frame
        ):
            raise EdlValidationError("timeline frame coordinates must be contiguous")
        if (
            abs(start - start_frame / frames_per_second) > coordinate_tolerance
            or abs(end - end_frame / frames_per_second) > coordinate_tolerance
            or abs(duration - (end_frame - start_frame) / frames_per_second)
            > coordinate_tolerance
        ):
            raise EdlValidationError("timeline seconds conflict with integer frame coordinates")
        if (
            source_start_ms is None
            or source_end_ms is None
            or source_end_ms <= source_start_ms
            or abs(source_start - source_start_ms / 1000) > 0.001
            or abs(source_end - source_end_ms / 1000) > 0.001
        ):
            raise EdlValidationError("source seconds conflict with millisecond coordinates")
        if abs(start - cursor) > coordinate_tolerance:
            raise EdlValidationError("timeline must be continuous without gaps or overlaps")
        if abs((end - start) - duration) > coordinate_tolerance:
            raise EdlValidationError(f"timeline shot {index} duration is inconsistent")
        padding = _number(value.get("padding_seconds"))
        if padding is None or abs(padding) > coordinate_tolerance:
            raise EdlValidationError("video or image padding is forbidden by the EDL contract")
        speed = _number(value.get("speed"))
        if speed is None or abs(speed - 1.0) > 1e-6:
            raise EdlValidationError("EDL speed changes are not supported")
        if value.get("freeze") is True or value.get("hold_last_frame") is True:
            raise EdlValidationError("frozen-frame compensation is forbidden")
        if media_kind == "video" and value.get("loop") is not False:
            raise EdlValidationError("video shots must declare loop=false")

        if media_kind == "video":
            allowed_start_ms = _non_negative_integer(selection.get("source_start_ms"))
            allowed_end_ms = _positive_integer(selection.get("source_end_ms"))
            assert allowed_start_ms is not None and allowed_end_ms is not None
            assert source_start_ms is not None and source_end_ms is not None
            if source_start_ms < allowed_start_ms or source_end_ms > allowed_end_ms:
                raise EdlValidationError(
                    f"timeline shot {index} escapes its selected source window"
                )
            if (source_end_ms - source_start_ms) / 1000 + coordinate_tolerance < duration:
                raise EdlValidationError(
                    f"video shot {index} is shorter than its EDL duration"
                )
            physical_duration = _positive_number(source_durations.get(artifact_id))
            if measured_sources_supplied and physical_duration is None:
                raise EdlValidationError(
                    f"video shot {index} has no measured source duration"
                )
            if physical_duration is not None and source_end > physical_duration + 0.001001:
                raise EdlValidationError(
                    f"video shot {index} exceeds the materialized source duration"
                )
        elif value.get("loop") is not True:
            raise EdlValidationError("image shots must declare loop=true")

        result.append(
            ValidatedEdlShot(
                ordinal=index,
                selection_id=selection_id,
                artifact_id=artifact_id,
                media_kind=media_kind,
                timeline_start_seconds=start,
                timeline_end_seconds=end,
                duration_seconds=duration,
                source_start_seconds=source_start,
                source_end_seconds=source_end,
            )
        )
        cursor = end
        cursor_frame = end_frame

    if abs(cursor - declared_duration) > coordinate_tolerance:
        raise EdlValidationError("timeline shots do not cover the declared duration")
    if cursor_frame != declared_frames:
        raise EdlValidationError("timeline frames do not cover the declared duration")
    return tuple(result)


def _validate_candidate_manifest_lineage(
    material_selection: Mapping[str, Any],
    selections: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
    asset_refs: Mapping[str, ArtifactRef],
) -> None:
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
        manifest_profile_valid = local_catalog or procedural_draft or hybrid_draft
    elif operation == "media.generate":
        generation = manifest.get("generation")
        manifest_profile_valid = (
            bool(str(manifest.get("provider") or "").strip())
            and manifest.get("catalog_scope") == "run"
            and manifest.get("local_catalog_only") is False
            and isinstance(generation, Mapping)
            and generation.get("mode") == "generated_only"
            and generation.get("continuity_mode")
            in {"none", "prompt_pack", "reference_frame"}
            and bool(
                str(
                    generation.get("generated_material_manifest_artifact_id")
                    or ""
                ).strip()
            )
            and bool(
                str(
                    generation.get("generated_material_manifest_content_hash")
                    or ""
                ).strip()
            )
        )
    else:
        manifest_profile_valid = False
    if not manifest_profile_valid or manifest.get("rights_status") != "verified":
        raise EdlValidationError("candidate manifest is not a verified supported snapshot")
    raw_beats = manifest.get("beats")
    coverage = manifest.get("coverage")
    if (
        not isinstance(raw_beats, Sequence)
        or isinstance(raw_beats, (str, bytes))
        or not raw_beats
        or not isinstance(coverage, Mapping)
        or coverage.get("status") != "complete"
        or coverage.get("missing_beat_ids") != []
        or coverage.get("total_beats") != len(raw_beats)
        or coverage.get("covered_beats") != len(raw_beats)
    ):
        raise EdlValidationError("candidate manifest coverage is incomplete")
    beats: dict[str, Mapping[str, Any]] = {}
    beat_sequences: set[int] = set()
    for value in raw_beats:
        if not isinstance(value, Mapping):
            raise EdlValidationError("candidate manifest Beats must be objects")
        beat_id = str(value.get("id") or "").strip()
        sequence = _positive_integer(value.get("sequence"))
        if (
            not beat_id
            or sequence is None
            or beat_id in beats
            or sequence in beat_sequences
            or value.get("coverage_status") != "covered"
        ):
            raise EdlValidationError("candidate manifest Beat identity is ambiguous")
        beats[beat_id] = value
        beat_sequences.add(sequence)

    raw_materialized = manifest.get("materialized_assets")
    if not isinstance(raw_materialized, Sequence) or isinstance(
        raw_materialized, (str, bytes)
    ):
        raise EdlValidationError("candidate manifest materialized assets are invalid")
    materialized: dict[str, Mapping[str, Any]] = {}
    for value in raw_materialized:
        if not isinstance(value, Mapping):
            raise EdlValidationError("materialized asset lineage must contain objects")
        artifact_id = str(value.get("artifact_id") or "").strip()
        artifact = asset_refs.get(artifact_id)
        if not artifact_id or artifact is None or artifact_id in materialized:
            raise EdlValidationError("materialized asset lineage is ambiguous")
        for key, expected in (
            ("kind", artifact.kind),
            ("filename", artifact.filename),
            ("media_type", artifact.media_type),
            ("content_hash", artifact.content_hash),
            ("byte_size", artifact.byte_size),
        ):
            if key in value and value.get(key) != expected:
                raise EdlValidationError(
                    f"materialized asset {key} conflicts with its artifact"
                )
        materialized[artifact_id] = value

    for selection_id, selection in selections.items():
        beat_id = str(selection.get("beat_id") or "")
        beat = beats.get(beat_id)
        if (
            beat is None
            or _positive_integer(beat.get("sequence"))
            != _positive_integer(selection.get("beat_sequence"))
        ):
            raise EdlValidationError(
                f"selection {selection_id} does not belong to a manifest Beat"
            )
        raw_candidates = beat.get("candidates")
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, (str, bytes)
        ):
            raise EdlValidationError("candidate manifest Beat candidates are invalid")
        rank = _positive_integer(selection.get("candidate_rank"))
        matches = [
            candidate
            for candidate in raw_candidates
            if isinstance(candidate, Mapping)
            and _positive_integer(candidate.get("rank")) == rank
        ]
        if len(matches) != 1:
            raise EdlValidationError(
                f"selection {selection_id} candidate rank is ambiguous"
            )
        candidate = matches[0]
        artifact_id = str(selection.get("artifact_id") or "")
        artifact = asset_refs[artifact_id]
        rejection_codes = candidate.get("rejection_codes")
        hard = candidate.get("hard_constraints")
        rights = candidate.get("rights_evidence")
        if (
            candidate.get("eligible") is not True
            or candidate.get("materialized") is not True
            or str(candidate.get("artifact_id") or "") != artifact_id
            or not isinstance(rejection_codes, Sequence)
            or isinstance(rejection_codes, (str, bytes))
            or bool(rejection_codes)
            or not isinstance(hard, Mapping)
            or not hard_constraint_evidence_valid(beat, candidate, hard)
            or not isinstance(rights, Mapping)
            or not rights_evidence_valid(rights)
        ):
            raise EdlValidationError(
                f"selection {selection_id} candidate evidence is not eligible"
            )
        if (
            str(candidate.get("content_hash") or "") != artifact.content_hash
            or str(candidate.get("artifact_filename") or "") != artifact.filename
            or str(candidate.get("media_type") or "") != artifact.media_type
            or str(candidate.get("asset_kind") or "")
            != str(selection.get("media_kind") or "")
            or str(candidate.get("asset_id") or "")
            != str(selection.get("asset_id") or "")
        ):
            raise EdlValidationError(
                f"selection {selection_id} conflicts with its manifest candidate"
            )
        materialized_record = materialized.get(artifact_id)
        if materialized_record is None:
            raise EdlValidationError(
                f"selection {selection_id} lacks materialized asset lineage"
            )
        if selection.get("media_kind") == "video":
            window = candidate.get("source_window")
            if not isinstance(window, Mapping):
                raise EdlValidationError("video candidate source window is invalid")
            start_ms = _non_negative_number(window.get("start_ms"))
            end_ms = _positive_number(window.get("end_ms"))
            duration_ms = _positive_number(window.get("duration_ms"))
            if (
                start_ms is None
                or end_ms is None
                or duration_ms is None
                or end_ms <= start_ms
                or abs((end_ms - start_ms) - duration_ms) > 0.001
                or abs(start_ms - int(selection["source_start_ms"])) > 0.001
                or abs(end_ms - int(selection["source_end_ms"])) > 0.001
            ):
                raise EdlValidationError(
                    f"selection {selection_id} source window conflicts with its candidate"
                )


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


def _non_negative_number(value: object) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _required_number(value: Mapping[str, Any], key: str) -> float:
    number = _number(value.get(key))
    if number is None:
        raise EdlValidationError(f"selection {key} must be finite")
    return number


def _integer(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _positive_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _non_negative_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
