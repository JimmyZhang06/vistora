"""Recoverable, fail-closed asset analysis orchestration.

The pipeline is deliberately independent from Run execution: each asset is an
isolated leased item, so one malformed file cannot abort a canary batch.  The
repository owns CAS/checkpoint durability; processors own only deterministic
stage work and never decide that an asset is publishable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from threading import Event, Thread
from typing import Any, Protocol

PIPELINE_VERSION = "asset-analysis-v3"
logger = logging.getLogger("framefactory.worker.asset-pipeline")


class AssetStage(StrEnum):
    FILE_DETECTION = "file_detection"
    MALWARE_SCAN = "malware_scan"
    FFPROBE = "ffprobe"
    FINGERPRINT = "fingerprint"
    PREVIEW = "preview"
    KEYFRAMES = "keyframes"
    VISUAL_ANALYSIS = "visual_analysis"
    SUBTITLE_EXTRACTION = "subtitle_extraction"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    TEMPORAL_ALIGNMENT = "temporal_alignment"
    SHOT_SEGMENTATION = "shot_segmentation"
    NORMALIZE_TAGS = "normalize_tags"
    RIGHTS_GATE = "rights_gate"


STAGES = tuple(AssetStage)


@dataclass(frozen=True, slots=True)
class AssetSource:
    source_type: str
    provider: str | None = None
    locator: str | None = None
    attribution: str | None = None
    license: str | None = None


@dataclass(frozen=True, slots=True)
class AssetWork:
    item_id: str
    batch_id: str
    workspace_id: str
    asset_id: str
    revision: int
    attempts: int
    max_attempts: int
    completed_stages: tuple[str, ...]
    checkpoints: Mapping[str, Any]
    copyright_status: str
    content_hash: str
    media_type: str
    bucket: str
    object_key: str
    original_filename: str
    sources: tuple[AssetSource, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewGate:
    reasons: tuple[str, ...]
    eligible_for_auto_ready: bool


@dataclass(frozen=True, slots=True)
class BatchProgress:
    total: int
    pending: int
    running: int
    awaiting_review: int
    ready: int
    failed: int
    cancelled: int


class AssetPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AssetJobRepository(Protocol):
    def is_dry_run(self, batch_id: str) -> bool: ...

    def cancel_requested(self, batch_id: str) -> bool: ...

    def claim(self, batch_id: str, worker_id: str, lease_seconds: float) -> AssetWork | None: ...

    def renew_lease(self, item_id: str, worker_id: str, lease_seconds: float) -> bool: ...

    def checkpoint(
        self,
        work: AssetWork,
        stage: AssetStage,
        value: Mapping[str, Any],
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> AssetWork: ...

    def mark_cancelled(self, work: AssetWork, *, worker_id: str) -> None: ...

    def mark_failure(
        self,
        work: AssetWork,
        stage: AssetStage,
        error: AssetPipelineError,
        *,
        worker_id: str,
    ) -> None: ...

    def publish_analysis(
        self,
        work: AssetWork,
        analysis: Mapping[str, Any],
        gate: ReviewGate,
        *,
        worker_id: str,
    ) -> None: ...

    def refresh_batch(self, batch_id: str) -> BatchProgress: ...


class AssetStageProcessor(Protocol):
    def run(
        self,
        stage: AssetStage,
        work: AssetWork,
        checkpoints: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class AssetAnalysisRunner:
    repository: AssetJobRepository
    processor: AssetStageProcessor
    worker_id: str
    lease_seconds: float = 300.0
    rate_limit_per_minute: int = 30
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _next_start: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if not isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not 1 <= self.rate_limit_per_minute <= 600:
            raise ValueError("rate_limit_per_minute must be between 1 and 600")

    def run_batch(self, batch_id: str, *, maximum_items: int | None = None) -> BatchProgress:
        if self.repository.is_dry_run(batch_id):
            raise ValueError("dry-run batches cannot execute or mutate assets")
        processed = 0
        while maximum_items is None or processed < maximum_items:
            if self.repository.cancel_requested(batch_id):
                break
            self._rate_limit()
            work = self.repository.claim(batch_id, self.worker_id, self.lease_seconds)
            if work is None:
                break
            self._run_item(work)
            processed += 1
        return self.repository.refresh_batch(batch_id)

    def _run_item(self, work: AssetWork) -> None:
        current = work
        completed = set(work.completed_stages)
        stage = AssetStage.FILE_DETECTION
        heartbeat = _ItemLeaseHeartbeat(
            self.repository,
            work.item_id,
            self.worker_id,
            self.lease_seconds,
        )
        heartbeat.start()
        try:
            for stage in STAGES:
                if stage.value in completed:
                    continue
                if self.repository.cancel_requested(work.batch_id):
                    self.repository.mark_cancelled(current, worker_id=self.worker_id)
                    return
                result = self.processor.run(
                    stage,
                    current,
                    current.checkpoints,
                    lambda: self.repository.cancel_requested(work.batch_id),
                )
                current = self.repository.checkpoint(
                    current,
                    stage,
                    result,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            analysis = normalized_analysis(current.checkpoints)
            gate = review_gate(current, analysis)
            self.repository.publish_analysis(
                current,
                analysis,
                gate,
                worker_id=self.worker_id,
            )
        except AssetPipelineError as exc:
            if exc.code == "batch_cancelled":
                self.repository.mark_cancelled(current, worker_id=self.worker_id)
            else:
                self.repository.mark_failure(current, stage, exc, worker_id=self.worker_id)
        except Exception as exc:  # noqa: BLE001 - isolate one asset behind a stable public reason
            error = AssetPipelineError(
                "unexpected_stage_failure",
                f"{type(exc).__name__}: {str(exc)[:400]}",
                retryable=True,
            )
            self.repository.mark_failure(current, stage, error, worker_id=self.worker_id)
        finally:
            heartbeat.stop()

    def _rate_limit(self) -> None:
        now = self.monotonic()
        if self._next_start > now:
            self.sleep(self._next_start - now)
            now = self.monotonic()
        self._next_start = now + 60.0 / self.rate_limit_per_minute


class _ItemLeaseHeartbeat:
    """Keep database ownership alive while a blocking media tool is running."""

    def __init__(
        self,
        repository: AssetJobRepository,
        item_id: str,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self.repository = repository
        self.item_id = item_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = max(0.05, min(lease_seconds / 3, 30.0))
        self._stop = Event()
        self._thread = Thread(target=self._run, name="asset-item-lease-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.repository.renew_lease(
                    self.item_id,
                    self.worker_id,
                    self.lease_seconds,
                )
            except Exception:
                logger.exception("asset item lease heartbeat failed id=%s", self.item_id)
                continue
            if not renewed:
                logger.warning("asset item lease was lost id=%s", self.item_id)
                return


_ARRAY_FIELDS = (
    "people",
    "organizations",
    "locations",
    "eras",
    "scene_types",
    "actions",
    "moods",
    "visual_styles",
    "keywords",
)


def normalized_analysis(checkpoints: Mapping[str, Any]) -> dict[str, Any]:
    raw = checkpoints.get(AssetStage.VISUAL_ANALYSIS.value, {})
    if not isinstance(raw, Mapping):
        raise AssetPipelineError("invalid_visual_result", "visual result is not an object", retryable=False)
    segmented = checkpoints.get(AssetStage.SHOT_SEGMENTATION.value, {})
    segments = segmented.get("segments", []) if isinstance(segmented, Mapping) else []
    probe = checkpoints.get(AssetStage.FFPROBE.value, {})
    temporal = checkpoints.get(AssetStage.TEMPORAL_ALIGNMENT.value, {})
    duration_ms = _integer(probe.get("duration_ms")) if isinstance(probe, Mapping) else None
    result: dict[str, Any] = {
        "summary": _text(raw.get("summary"), 500),
        "language": _text(raw.get("language"), 32) or "unknown",
        **{name: _texts(raw.get(name), 64 if name == "keywords" else 32) for name in _ARRAY_FIELDS},
        "has_embedded_text": raw.get("has_embedded_text") is True,
        "embedded_text_type": _optional(raw.get("embedded_text_type"), 32) or "unknown",
        "has_watermark": raw.get("has_watermark") is True,
        "safety": _object(raw.get("safety")),
        "quality": _object(raw.get("quality")),
        "confidence": _confidence(raw.get("confidence")),
        "segments": _segments(segments, duration_ms=duration_ms),
        "temporal": _object(temporal),
        "technical": dict(probe) if isinstance(probe, Mapping) else {},
        "fingerprint": _object(checkpoints.get(AssetStage.FINGERPRINT.value)),
    }
    if not result["segments"] and duration_ms and result["summary"]:
        result["segments"] = [{
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": duration_ms,
            "description": result["summary"],
            "people": result["people"],
            "locations": result["locations"],
            "keywords": result["keywords"],
            "scene_type": _first(result["scene_types"]),
            "action": _first(result["actions"]),
            "era": _first(result["eras"]),
            "mood": _first(result["moods"]),
            "visual_style": _first(result["visual_styles"]),
            "shot_type": None,
            "confidence": result["confidence"],
            "transcript": _text(_object(temporal).get("full_text"), 4000),
            "boundary_score": 1.0,
            "boundary_reasons": ["asset_bounds"],
            "cut_safe": True,
            "semantic_complete": bool(_object(temporal).get("full_text")),
        }]
    return result


def review_gate(work: AssetWork, analysis: Mapping[str, Any]) -> ReviewGate:
    reasons: list[str] = []
    allowed_rights = work.copyright_status in {"owned", "licensed", "public_domain"}
    licensed_source = any(source.license and source.license.strip() for source in work.sources)
    if not work.sources:
        reasons.append("source_provenance_missing")
    if work.copyright_status == "unknown":
        reasons.append("copyright_unknown")
    elif work.copyright_status == "restricted":
        reasons.append("copyright_restricted")
    elif work.copyright_status in {"licensed", "public_domain"} and not licensed_source:
        reasons.append("license_evidence_missing")
    confidence = analysis.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0.7:
        reasons.append("low_confidence")
    if analysis.get("has_watermark") is True:
        reasons.append("watermark_detected")
    if analysis.get("has_embedded_text") is True and analysis.get(
        "embedded_text_type"
    ) not in {"scoreboard", "small_caption", "比分牌", "小型角标"}:
        reasons.append("embedded_text_detected")
    safety = analysis.get("safety", {})
    if isinstance(safety, Mapping) and any(value is True for value in safety.values()):
        reasons.append("content_safety_risk")
    quality = analysis.get("quality", {})
    if isinstance(quality, Mapping):
        score = _confidence(quality.get("score"))
        if quality.get("usable") is False or score is None or score < 0.7:
            reasons.append("quality_review_required")
    technical = analysis.get("technical", {})
    temporal = analysis.get("temporal", {})
    language = str(analysis.get("language") or "").strip().casefold()
    people = analysis.get("people", ())
    speech_expected = language not in {"", "none", "无", "music", "instrumental"} or bool(
        people
    )
    if (
        isinstance(technical, Mapping)
        and technical.get("has_audio") is True
        and speech_expected
        and (
            not isinstance(temporal, Mapping)
            or temporal.get("status") != "available"
        )
    ):
        reasons.append("speech_timeline_missing")
    return ReviewGate(tuple(dict.fromkeys(reasons)), allowed_rights and not reasons)


def analysis_tags(analysis: Mapping[str, Any]) -> tuple[str, ...]:
    labels = {
        "people": "人物",
        "locations": "地点",
        "eras": "时代",
        "scene_types": "场景",
        "actions": "动作",
        "moods": "情绪",
        "visual_styles": "视觉风格",
        "keywords": "关键词",
    }
    values: list[str] = []
    for field_name, label in labels.items():
        for value in analysis.get(field_name, []):
            values.append(f"{label}:{value}")
    for segment in analysis.get("segments", []):
        if isinstance(segment, Mapping) and segment.get("shot_type"):
            values.append(f"景别:{segment['shot_type']}")
    values.append(f"嵌字:{'有' if analysis.get('has_embedded_text') else '无'}")
    if analysis.get("has_embedded_text"):
        values.append(f"嵌字类型:{analysis.get('embedded_text_type', 'unknown')}")
    values.append(f"水印:{'有' if analysis.get('has_watermark') else '无'}")
    safety_risks = []
    for key, value in _object(analysis.get("safety")).items():
        if value is True:
            safety_risks.append(str(key))
    values.extend(f"安全:{key}" for key in safety_risks)
    if not safety_risks:
        values.append("安全:通过")
    quality = _object(analysis.get("quality"))
    values.append(f"质量:{'可用' if quality.get('usable') is True else '需审核'}")
    temporal = _object(analysis.get("temporal"))
    values.append(f"时间轴:{temporal.get('source', '无') if temporal.get('status') == 'available' else '无'}")
    return tuple(dict.fromkeys(values))[:256]


def _segments(raw: Any, *, duration_ms: int | None) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    maximum_end = duration_ms or 86_400_000
    result: list[dict[str, Any]] = []
    previous_end = 0
    # Long-form media routinely needs more than 256 segments (only about
    # 64 minutes even at four segments/minute). Keep a high defensive ceiling
    # while allowing films and multi-hour source recordings to cover their
    # complete duration.
    for item in raw[:20_000]:
        if not isinstance(item, Mapping):
            continue
        start = max(previous_end, _integer(item.get("start_ms")) or 0)
        end = min(maximum_end, _integer(item.get("end_ms")) or 0)
        description = _text(item.get("description"), 500)
        if end <= start or not description:
            continue
        result.append({
            "ordinal": len(result),
            "start_ms": start,
            "end_ms": end,
            "description": description,
            "people": _texts(item.get("people"), 16),
            "locations": _texts(item.get("locations"), 16),
            "keywords": _texts(item.get("keywords"), 32),
            "scene_type": _optional(item.get("scene_type")),
            "action": _optional(item.get("action")),
            "era": _optional(item.get("era")),
            "mood": _optional(item.get("mood")),
            "visual_style": _optional(item.get("visual_style")),
            "shot_type": _optional(item.get("shot_type")),
            "confidence": _confidence(item.get("confidence")),
            "representative_frame_key": _optional(item.get("representative_frame_key"), 500),
            "transcript": _text(item.get("transcript"), 4000),
            "boundary_score": _confidence(item.get("boundary_score")),
            "boundary_reasons": _texts(item.get("boundary_reasons"), 16),
            "cut_safe": item.get("cut_safe") is True,
            "semantic_complete": item.get("semantic_complete") is True,
        })
        previous_end = end
    return result


def _text(value: Any, maximum: int = 100) -> str:
    return str(value).strip()[:maximum] if value is not None else ""


def _optional(value: Any, maximum: int = 100) -> str | None:
    value = _text(value, maximum)
    return value or None


def _texts(value: Any, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(dict.fromkeys(_text(item) for item in value if _text(item)))[:maximum]


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return round(max(0.0, min(1.0, number)), 3)


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _first(values: Sequence[str]) -> str | None:
    return values[0] if values else None
