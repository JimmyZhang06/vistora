"""Fail-closed verification for run-scoped generated video candidates.

The generation provider is never treated as visual evidence for its own output.
This module probes and fully decodes the downloaded bytes, extracts independent
representative frames, and asks a separately configured vision analyzer to
prove every Beat constraint before a candidate can become eligible.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, Protocol

from framefactory.steps import StepContext
from framefactory.worker.retrieval.models import RetrievalBeat

_RATIOS = {
    "9:16": 9 / 16,
    "720:1280": 9 / 16,
    "16:9": 16 / 9,
    "1280:720": 16 / 9,
}
_MAX_TOOL_OUTPUT_BYTES = 1_048_576
_MIN_DIMENSION = 256
_REQUIRED_SAFETY_KEYS = frozenset(
    {
        "adult",
        "violence",
        "self_harm",
        "hate_or_extremism",
        "illegal_activity",
        "recognizable_real_person_or_public_figure",
        "brand_or_logo",
        "protected_character",
    }
)


class GeneratedVideoVerificationError(RuntimeError):
    """Verification could not establish enough trustworthy evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.evidence = dict(evidence or {})


class VisionAnalyzer(Protocol):
    """Independent visual model boundary used only for generated-media QC."""

    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]: ...


ConstraintKind = Literal["visual_description", "must_match", "must_not_match"]


@dataclass(frozen=True, slots=True)
class GeneratedConstraintCheck:
    kind: ConstraintKind
    term: str
    matched: bool
    evidence: str

    @property
    def passed(self) -> bool:
        return not self.matched if self.kind == "must_not_match" else self.matched

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "term": self.term,
            "matched": self.matched,
            "passed": self.passed,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class GeneratedVideoVerification:
    duration_seconds: float
    width: int
    height: int
    codec: str
    frame_rate: float
    description: str
    labels: tuple[str, ...]
    confidence: float
    constraint_checks: tuple[GeneratedConstraintCheck, ...]
    has_watermark: bool
    has_embedded_text: bool
    unsafe: bool
    safety: Mapping[str, bool]
    cut_safe: bool
    semantic_complete: bool
    provider: str
    model: str
    frame_hashes: tuple[str, ...]
    rejection_codes: tuple[str, ...]
    evidence: Mapping[str, Any]

    @property
    def eligible(self) -> bool:
        return not self.rejection_codes

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "frame_rate": self.frame_rate,
            "description": self.description,
            "labels": list(self.labels),
            "confidence": self.confidence,
            "constraint_checks": [item.to_dict() for item in self.constraint_checks],
            "has_watermark": self.has_watermark,
            "has_embedded_text": self.has_embedded_text,
            "unsafe": self.unsafe,
            "safety": dict(self.safety),
            "cut_safe": self.cut_safe,
            "semantic_complete": self.semantic_complete,
            "provider": self.provider,
            "model": self.model,
            "frame_hashes": list(self.frame_hashes),
            "rejection_codes": list(self.rejection_codes),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class _TechnicalEvidence:
    duration_seconds: float
    width: int
    height: int
    codec: str
    frame_rate: float
    format_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "frame_rate": self.frame_rate,
            "format_name": self.format_name,
        }


@dataclass(frozen=True, slots=True)
class _BeatEvidenceRequest:
    beat_id: str
    visual_description: str
    must_match: tuple[str, ...]
    must_not_match: tuple[str, ...]


class GeneratedVideoVerifier:
    """Verify one paid generated candidate without trusting provider metadata."""

    def __init__(
        self,
        vision: VisionAnalyzer | None,
        *,
        vision_provider: str,
        vision_model: str,
        ffprobe_command: str = "ffprobe",
        ffmpeg_command: str = "ffmpeg",
        minimum_confidence: float = 0.75,
        minimum_quality_score: float = 0.65,
        command_timeout_seconds: float = 180.0,
        command_runner: CommandRunner | None = None,
        command_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        if not 0 < minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between zero and one")
        if not 0 < minimum_quality_score <= 1:
            raise ValueError("minimum_quality_score must be between zero and one")
        if not math.isfinite(command_timeout_seconds) or command_timeout_seconds <= 0:
            raise ValueError("command timeout must be positive and finite")
        self.vision = vision
        self.vision_provider = vision_provider.strip()
        self.vision_model = vision_model.strip()
        self.ffprobe_command = ffprobe_command.strip()
        self.ffmpeg_command = ffmpeg_command.strip()
        self.minimum_confidence = minimum_confidence
        self.minimum_quality_score = minimum_quality_score
        self.command_timeout_seconds = command_timeout_seconds
        self._run_command = command_runner or subprocess.run
        self._resolve_command = command_resolver or shutil.which

    async def verify(
        self,
        video_bytes: bytes,
        beat: RetrievalBeat | Mapping[str, Any],
        expected_duration_seconds: float,
        ratio: str,
        context: StepContext,
    ) -> GeneratedVideoVerification:
        if self.vision is None or not self.vision_provider or not self.vision_model:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VISION_UNCONFIGURED",
                "generated video cannot be verified without an independent vision provider",
                retryable=False,
            )
        if not isinstance(video_bytes, bytes) or not video_bytes:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VIDEO_EMPTY",
                "generated video bytes are empty",
                retryable=False,
            )
        if isinstance(expected_duration_seconds, bool) or not math.isfinite(
            expected_duration_seconds
        ) or expected_duration_seconds <= 0:
            raise ValueError("expected_duration_seconds must be positive and finite")
        if ratio not in _RATIOS:
            raise ValueError("generated video ratio is unsupported")
        request = _beat_request(beat)
        ffprobe = self._tool(self.ffprobe_command, label="FFprobe")
        ffmpeg = self._tool(self.ffmpeg_command, label="FFmpeg")
        await context.checkpoint()

        with tempfile.TemporaryDirectory(prefix="framefactory-generated-verify-") as raw_dir:
            directory = Path(raw_dir)
            source = directory / "candidate.mp4"
            source.write_bytes(video_bytes)
            video_hash = hashlib.sha256(video_bytes).hexdigest()
            technical = await asyncio.to_thread(self._probe, ffprobe, source)
            self._validate_technical(
                technical,
                expected_duration_seconds=expected_duration_seconds,
                ratio=ratio,
                video_hash=video_hash,
            )
            await context.checkpoint()
            await asyncio.to_thread(self._decode_complete, ffmpeg, source, video_hash)
            await context.checkpoint()
            timestamps = _frame_timestamps(technical.duration_seconds, technical.frame_rate)
            frames = await asyncio.to_thread(
                self._extract_frames,
                ffmpeg,
                source,
                directory,
                timestamps,
            )
            frame_hashes = tuple(_sha256(path) for path in frames)
            if len(frames) < 3 or len(set(frame_hashes)) != len(frame_hashes):
                raise GeneratedVideoVerificationError(
                    "FULL_AI_VISUAL_EVIDENCE_INSUFFICIENT",
                    "representative frames are missing or not visually independent",
                    retryable=False,
                    evidence={
                        "video_sha256": video_hash,
                        "frame_timestamps_seconds": list(timestamps),
                        "frame_hashes": list(frame_hashes),
                    },
                )
            await context.checkpoint()
            vision_request = {
                **technical.to_dict(),
                "video_sha256": video_hash,
                "frame_timestamps_seconds": list(timestamps),
                "frame_hashes": list(frame_hashes),
                "verification_contract": {
                    "beat_id": request.beat_id,
                    "visual_description": request.visual_description,
                    "must_match": list(request.must_match),
                    "must_not_match": list(request.must_not_match),
                    "required_fields": [
                        "description",
                        "labels",
                        "confidence",
                        "visual_description_check",
                        "constraint_checks",
                        "has_watermark",
                        "has_embedded_text",
                        "unsafe",
                        "safety",
                        "quality",
                        "cut_safe",
                        "cut_safe_evidence",
                    ],
                },
            }
            analysis = await asyncio.to_thread(
                self.vision.analyze,
                frames,
                vision_request,
            )
            if inspect.isawaitable(analysis):
                analysis = await analysis
            await context.checkpoint()
            return self._verification(
                analysis,
                request=request,
                technical=technical,
                video_hash=video_hash,
                timestamps=timestamps,
                frame_hashes=frame_hashes,
            )

    def _tool(self, command: str, *, label: str) -> str:
        resolved = self._resolve_command(command) if command else None
        if not resolved:
            raise GeneratedVideoVerificationError(
                f"FULL_AI_{label.upper()}_UNAVAILABLE",
                f"{label} is unavailable for generated-video verification",
                retryable=True,
            )
        return resolved

    def _command(self, command: Sequence[str], *, code: str) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self._run_command(
                command,
                capture_output=True,
                check=False,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GeneratedVideoVerificationError(
                code,
                "generated-video verification tool could not complete",
                retryable=True,
            ) from exc
        if len(result.stdout) > _MAX_TOOL_OUTPUT_BYTES or len(result.stderr) > _MAX_TOOL_OUTPUT_BYTES:
            raise GeneratedVideoVerificationError(
                code,
                "generated-video verification tool output exceeded the safe limit",
                retryable=False,
            )
        return result

    def _probe(self, ffprobe: str, source: Path) -> _TechnicalEvidence:
        result = self._command(
            (
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate,duration:format=format_name,duration",
                "-of",
                "json",
                str(source),
            ),
            code="FULL_AI_FFPROBE_FAILED",
        )
        if result.returncode != 0:
            raise GeneratedVideoVerificationError(
                "FULL_AI_FFPROBE_REJECTED",
                "FFprobe rejected the generated video",
                retryable=False,
            )
        try:
            value = json.loads(result.stdout)
            streams = value["streams"]
            stream = streams[0]
            raw_duration = value.get("format", {}).get("duration") or stream.get("duration")
            duration = float(raw_duration)
            width = _positive_integer(stream.get("width"), field="width")
            height = _positive_integer(stream.get("height"), field="height")
            frame_rate = _frame_rate(stream.get("avg_frame_rate"))
            codec = str(stream.get("codec_name") or "").strip().casefold()
            format_name = str(value.get("format", {}).get("format_name") or "").strip()
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GeneratedVideoVerificationError(
                "FULL_AI_FFPROBE_INVALID",
                "FFprobe returned incomplete generated-video evidence",
                retryable=False,
            ) from exc
        if not math.isfinite(duration) or duration <= 0 or not codec or not format_name:
            raise GeneratedVideoVerificationError(
                "FULL_AI_FFPROBE_INVALID",
                "FFprobe returned invalid generated-video metadata",
                retryable=False,
            )
        return _TechnicalEvidence(
            duration_seconds=round(duration, 6),
            width=width,
            height=height,
            codec=codec,
            frame_rate=frame_rate,
            format_name=format_name,
        )

    def _validate_technical(
        self,
        technical: _TechnicalEvidence,
        *,
        expected_duration_seconds: float,
        ratio: str,
        video_hash: str,
    ) -> None:
        evidence = {**technical.to_dict(), "expected_ratio": ratio, "video_sha256": video_hash}
        if technical.width < _MIN_DIMENSION or technical.height < _MIN_DIMENSION:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VIDEO_RESOLUTION_UNUSABLE",
                "generated video resolution is too small for editing",
                retryable=False,
                evidence=evidence,
            )
        actual_ratio = technical.width / technical.height
        ratio_tolerance = max(0.002, 1 / max(technical.width, technical.height))
        if abs(actual_ratio - _RATIOS[ratio]) > ratio_tolerance:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VIDEO_RATIO_MISMATCH",
                "generated video dimensions do not match the requested ratio",
                retryable=False,
                evidence=evidence,
            )
        frame_tolerance = 1 / technical.frame_rate
        if abs(technical.duration_seconds - expected_duration_seconds) > frame_tolerance + 0.001:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VIDEO_DURATION_MISMATCH",
                "generated video duration differs by more than one output frame",
                retryable=False,
                evidence={
                    **evidence,
                    "expected_duration_seconds": expected_duration_seconds,
                    "duration_tolerance_seconds": round(frame_tolerance, 6),
                },
            )

    def _decode_complete(self, ffmpeg: str, source: Path, video_hash: str) -> None:
        result = self._command(
            (
                ffmpeg,
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-f",
                "null",
                "-",
            ),
            code="FULL_AI_VIDEO_DECODE_FAILED",
        )
        if result.returncode != 0:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VIDEO_DECODE_FAILED",
                "FFmpeg could not completely decode the generated video",
                retryable=False,
                evidence={"video_sha256": video_hash},
            )

    def _extract_frames(
        self,
        ffmpeg: str,
        source: Path,
        directory: Path,
        timestamps: tuple[float, float, float],
    ) -> tuple[Path, ...]:
        frames: list[Path] = []
        for ordinal, timestamp in enumerate(timestamps):
            output = directory / f"frame-{ordinal:02d}.jpg"
            result = self._command(
                (
                    ffmpeg,
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp:.6f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1280,iw)':-2",
                    "-q:v",
                    "2",
                    "-map_metadata",
                    "-1",
                    "-y",
                    str(output),
                ),
                code="FULL_AI_FRAME_EXTRACTION_FAILED",
            )
            if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                raise GeneratedVideoVerificationError(
                    "FULL_AI_FRAME_EXTRACTION_FAILED",
                    "FFmpeg could not extract complete representative-frame evidence",
                    retryable=False,
                    evidence={"frame_ordinal": ordinal, "timestamp_seconds": timestamp},
                )
            frames.append(output)
        return tuple(frames)

    def _verification(
        self,
        analysis: Mapping[str, Any],
        *,
        request: _BeatEvidenceRequest,
        technical: _TechnicalEvidence,
        video_hash: str,
        timestamps: tuple[float, float, float],
        frame_hashes: tuple[str, ...],
    ) -> GeneratedVideoVerification:
        if not isinstance(analysis, Mapping) or not analysis:
            raise self._invalid_vision("vision provider returned no auditable evidence")
        description = str(analysis.get("description") or "").strip()[:4_000]
        labels = _labels(analysis.get("labels"))
        confidence = _score(analysis.get("confidence"), field="confidence")
        has_watermark = _boolean(analysis, "has_watermark")
        has_embedded_text = _boolean(analysis, "has_embedded_text")
        explicit_unsafe = _boolean(analysis, "unsafe")
        safety = _safety(analysis.get("safety"))
        quality = _mapping(analysis.get("quality"), field="quality")
        quality_usable = _boolean(quality, "usable")
        quality_score = _score(quality.get("score"), field="quality.score")
        raw_cut_safe = _boolean(analysis, "cut_safe")
        cut_safe_evidence = str(analysis.get("cut_safe_evidence") or "").strip()[:2_000]
        if not description or not labels or not cut_safe_evidence:
            raise self._invalid_vision(
                "vision evidence lacks description, labels, or cut-safety evidence"
            )

        checks = _constraint_checks(analysis, request)
        unsafe = explicit_unsafe or any(safety.values())
        rejection_codes: list[str] = []
        visual_check = checks[0]
        if not visual_check.passed:
            rejection_codes.append("visual_description_mismatch")
        if any(not item.passed for item in checks if item.kind == "must_match"):
            rejection_codes.append("must_match_failed")
        if any(not item.passed for item in checks if item.kind == "must_not_match"):
            rejection_codes.append("must_not_match_detected")
        if has_watermark:
            rejection_codes.append("watermark_detected")
        if has_embedded_text:
            rejection_codes.append("embedded_text_detected")
        if unsafe:
            rejection_codes.append("unsafe_content")
        if not quality_usable or quality_score < self.minimum_quality_score:
            rejection_codes.append("visual_quality_unusable")
        if confidence < self.minimum_confidence:
            rejection_codes.append("low_visual_confidence")
        cut_safe = raw_cut_safe and quality_usable
        if not cut_safe:
            rejection_codes.append("cut_unsafe")
        semantic_complete = (
            confidence >= self.minimum_confidence
            and bool(description)
            and bool(labels)
            and all(item.passed for item in checks)
        )
        unique_rejections = tuple(dict.fromkeys(rejection_codes))
        return GeneratedVideoVerification(
            duration_seconds=technical.duration_seconds,
            width=technical.width,
            height=technical.height,
            codec=technical.codec,
            frame_rate=technical.frame_rate,
            description=description,
            labels=labels,
            confidence=confidence,
            constraint_checks=checks,
            has_watermark=has_watermark,
            has_embedded_text=has_embedded_text,
            unsafe=unsafe,
            safety=safety,
            cut_safe=cut_safe,
            semantic_complete=semantic_complete,
            provider=self.vision_provider,
            model=self.vision_model,
            frame_hashes=frame_hashes,
            rejection_codes=unique_rejections,
            evidence={
                "schema_version": "1.0.0",
                "video_sha256": video_hash,
                "frame_timestamps_seconds": list(timestamps),
                "frame_hashes": list(frame_hashes),
                "technical": technical.to_dict(),
                "visual_description_check": visual_check.to_dict(),
                "quality": {"usable": quality_usable, "score": quality_score},
                "cut_safe_evidence": cut_safe_evidence,
                "minimum_confidence": self.minimum_confidence,
                "minimum_quality_score": self.minimum_quality_score,
            },
        )

    def _invalid_vision(self, message: str) -> GeneratedVideoVerificationError:
        return GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            message,
            retryable=False,
            evidence={"provider": self.vision_provider, "model": self.vision_model},
        )


def _beat_request(beat: RetrievalBeat | Mapping[str, Any]) -> _BeatEvidenceRequest:
    if isinstance(beat, RetrievalBeat):
        identity = beat.id.strip()
        visual_description = beat.visual_description.strip()
        must_match = beat.must_match
        must_not_match = beat.must_not_match
    elif isinstance(beat, Mapping):
        identity = str(beat.get("id") or "").strip()
        visual_description = str(beat.get("visual_description") or "").strip()
        must_match = _terms(beat.get("must_match"))
        must_not_match = _terms(beat.get("must_not_match"))
    else:
        raise TypeError("beat must be a RetrievalBeat or mapping")
    if not identity or not visual_description:
        raise GeneratedVideoVerificationError(
            "FULL_AI_BEAT_INVALID",
            "generated-video verification requires a stable Beat and visual description",
            retryable=False,
        )
    return _BeatEvidenceRequest(
        beat_id=identity,
        visual_description=visual_description,
        must_match=tuple(dict.fromkeys(item.strip() for item in must_match if item.strip())),
        must_not_match=tuple(dict.fromkeys(item.strip() for item in must_not_match if item.strip())),
    )


def _constraint_checks(
    analysis: Mapping[str, Any],
    request: _BeatEvidenceRequest,
) -> tuple[GeneratedConstraintCheck, ...]:
    visual = _mapping(
        analysis.get("visual_description_check"),
        field="visual_description_check",
    )
    visual_check = GeneratedConstraintCheck(
        kind="visual_description",
        term=request.visual_description,
        matched=_boolean(visual, "matched"),
        evidence=_evidence_text(visual.get("evidence"), field="visual_description_check"),
    )
    raw_checks = analysis.get("constraint_checks")
    if not isinstance(raw_checks, Sequence) or isinstance(raw_checks, (str, bytes)):
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            "vision constraint_checks must be a list",
            retryable=False,
        )
    parsed: dict[tuple[str, str], GeneratedConstraintCheck] = {}
    for raw in raw_checks:
        if not isinstance(raw, Mapping):
            raise GeneratedVideoVerificationError(
                "FULL_AI_VISION_EVIDENCE_INVALID",
                "vision constraint check must be an object",
                retryable=False,
            )
        kind = str(raw.get("kind") or "")
        term = str(raw.get("term") or "").strip()
        if kind not in {"must_match", "must_not_match"} or not term:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VISION_EVIDENCE_INVALID",
                "vision constraint check kind or term is invalid",
                retryable=False,
            )
        key = (kind, term)
        if key in parsed:
            raise GeneratedVideoVerificationError(
                "FULL_AI_VISION_EVIDENCE_INVALID",
                "vision constraint checks contain duplicates",
                retryable=False,
            )
        parsed[key] = GeneratedConstraintCheck(
            kind=kind,  # type: ignore[arg-type]
            term=term,
            matched=_boolean(raw, "matched"),
            evidence=_evidence_text(raw.get("evidence"), field=f"{kind}:{term}"),
        )
    expected = tuple(("must_match", term) for term in request.must_match) + tuple(
        ("must_not_match", term) for term in request.must_not_match
    )
    if set(parsed) != set(expected):
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            "vision did not provide exactly one check for every Beat constraint",
            retryable=False,
            evidence={
                "expected": [list(item) for item in expected],
                "received": [list(item) for item in parsed],
            },
        )
    return (visual_check, *(parsed[item] for item in expected))


def _frame_timestamps(duration: float, frame_rate: float) -> tuple[float, float, float]:
    frame = 1 / frame_rate
    first = min(max(frame / 2, 0.001), duration / 4)
    middle = duration / 2
    last = max(middle + frame, duration - max(frame, 0.05))
    last = min(duration - 0.001, last)
    values = tuple(round(item, 6) for item in (first, middle, last))
    if len(set(values)) != 3 or not 0 <= values[0] < values[1] < values[2] < duration:
        raise GeneratedVideoVerificationError(
            "FULL_AI_FRAME_TIMESTAMPS_INVALID",
            "video duration cannot support first, middle, and final frame evidence",
            retryable=False,
        )
    return values


def _frame_rate(value: Any) -> float:
    try:
        rate = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("frame rate is invalid") from exc
    if not math.isfinite(rate) or not 1 <= rate <= 240:
        raise ValueError("frame rate is outside the supported range")
    return round(rate, 6)


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a positive integer")
    result = int(value)
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _boolean(value: Mapping[str, Any], field: str) -> bool:
    result = value.get(field)
    if not isinstance(result, bool):
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence field {field} must be boolean",
            retryable=False,
        )
    return result


def _score(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence field {field} must be a score",
            retryable=False,
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence field {field} must be a score",
            retryable=False,
        ) from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence field {field} must be between zero and one",
            retryable=False,
        )
    return round(result, 4)


def _safety(value: Any) -> dict[str, bool]:
    safety = _mapping(value, field="safety")
    if set(safety) != _REQUIRED_SAFETY_KEYS:
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            "vision safety evidence must contain exactly the eight generated-media policy flags",
            retryable=False,
        )
    result: dict[str, bool] = {}
    for raw_key, raw_value in safety.items():
        key = str(raw_key).strip()
        if not key or not isinstance(raw_value, bool):
            raise GeneratedVideoVerificationError(
                "FULL_AI_VISION_EVIDENCE_INVALID",
                "vision safety flags must be named booleans",
                retryable=False,
            )
        result[key] = raw_value
    return result


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence field {field} must be an object",
            retryable=False,
        )
    return value


def _labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        dict.fromkeys(str(item).strip()[:160] for item in value if str(item).strip())
    )[:128]


def _terms(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        dict.fromkeys(str(item).strip()[:240] for item in value if str(item).strip())
    )[:12]


def _evidence_text(value: Any, *, field: str) -> str:
    evidence = str(value or "").strip()[:2_000]
    if not evidence:
        raise GeneratedVideoVerificationError(
            "FULL_AI_VISION_EVIDENCE_INVALID",
            f"vision evidence for {field} must not be empty",
            retryable=False,
        )
    return evidence


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
