"""Migration adapters for the proven v1 Edge TTS, asset catalog and FFmpeg stack."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import re
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from framefactory.runtime import (
    CapabilityUnavailable,
    PermanentStepError,
    RetryableStepError,
)
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.config import LegacyMediaSettings
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact
from framefactory.worker.retrieval.beats import normalize_beats
from framefactory.worker.timeline import plan_edit_timeline

_IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".webp"}
# FFmpeg can decode Matroska directly. Render steps always transcode selected
# sources into normalized H.264/AAC MP4 segments before concatenation, so MKV
# inputs do not leak container/codec differences into the final render.
_VIDEO_SUFFIXES = {".m4v", ".mkv", ".mov", ".mp4", ".webm"}
_SENTENCE_BREAK = re.compile(r"(?<=[。！？!?；;])\s*")
_MAX_TIMING_SEGMENTS = 10_000
_MAX_TIMING_WORDS = 10_000
logger = logging.getLogger("framefactory.worker.media")


@dataclass(frozen=True, slots=True)
class _RenderProfile:
    width: int
    height: int
    frame_rate: int
    layout: str
    media_fit: str
    subtitle_enabled: bool
    subtitle_position: str
    subtitle_size: str
    subtitle_max_lines: int
    background_color: str = "101218"


class EdgeSpeechCapability:
    def __init__(
        self,
        settings: LegacyMediaSettings,
        storage: ArtifactStorage,
        *,
        synthesizer: Callable[
            [str, Path, str, str],
            Awaitable[Sequence[Mapping[str, Any]] | None],
        ]
        | None = None,
        duration_probe: Callable[[Path, StepContext], Awaitable[float]] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self._synthesizer = synthesizer or _edge_synthesize
        self._duration_probe = duration_probe

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script_artifact = _required_artifact(context, "script")
        script = self.storage.read_json(script_artifact)
        narration = str(script.get("narration", "")).strip()
        if not narration:
            raise PermanentStepError("script narration is empty; TTS was not attempted")
        if len(narration) > 100_000:
            raise PermanentStepError("script narration exceeds the 100000 character TTS limit")
        with tempfile.TemporaryDirectory(prefix="framefactory-tts-") as directory:
            output = Path(directory) / "narration.mp3"
            target_duration = _target_duration_seconds(context.input_snapshot)
            rate = self.settings.tts_rate
            duration, word_boundaries = await self._synthesize_and_probe(
                narration, output, rate, context
            )
            attempts = 1
            while (
                target_duration is not None
                and not _duration_fits(duration, target_duration, 0.15)
                and attempts < 4
            ):
                adjusted_rate = _adjust_tts_rate(rate, duration, target_duration)
                if adjusted_rate == rate:
                    break
                rate = adjusted_rate
                duration, word_boundaries = await self._synthesize_and_probe(
                    narration, output, rate, context
                )
                attempts += 1
            data = output.read_bytes()
        duration_fit = target_duration is None or _duration_fits(
            duration, target_duration, 0.20
        )
        audio_content_hash = hashlib.sha256(data).hexdigest()
        timing = _narration_timing_payload(
            script,
            duration=duration,
            word_boundaries=word_boundaries,
            audio_content_hash=audio_content_hash,
            script_content_hash=script_artifact.content_hash,
        )
        webpage_video = _is_webpage_video_snapshot(context.input_snapshot)
        if webpage_video:
            if not duration_fit:
                raise PermanentStepError(
                    "webpage video narration could not meet the requested duration after "
                    "bounded speech-rate adjustment",
                    code="webpage_video_tts_duration_mismatch",
                )
            if bool(timing["word_timing_estimated"]) or not timing["words"]:
                raise PermanentStepError(
                    "webpage video narration requires native WordBoundary timing",
                    code="webpage_video_tts_native_timing_required",
                )
        artifact = self.storage.publish(
            context,
            ProviderArtifact("audio", "narration.mp3", "audio/mpeg", data),
        )
        if artifact.content_hash != audio_content_hash:
            raise PermanentStepError("published narration audio hash does not match its bytes")
        timing_artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "narration_timing",
                "narration-timing.json",
                "application/json",
                json.dumps(
                    timing,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            ),
        )
        return StepResult(
            artifacts=(artifact, timing_artifact),
            output_summary={
                "provider": "edge-tts",
                "voice": self.settings.tts_voice,
                "rate": rate,
                "characters": len(narration),
                "duration_seconds": round(duration, 3),
                "target_duration_seconds": target_duration,
                "duration_fit": duration_fit,
                "synthesis_attempts": attempts,
                "timing_estimated": timing["estimated"],
                "timing_granularity": timing["granularity"],
                "word_boundaries": len(timing["words"]),
            },
            # A sentence/beat boundary may be proportionally mapped onto native
            # WordBoundary events when provider tokens do not exactly reproduce
            # the authored punctuation.  That semantic grouping remains marked
            # estimated in the artifact, but webpage rendering is safe to
            # continue because captions consume the validated native word cues.
            requires_review=(
                not duration_fit
                or (bool(timing["estimated"]) and not webpage_video)
            ),
        )

    async def _synthesize_and_probe(
        self,
        narration: str,
        output: Path,
        rate: str,
        context: StepContext,
    ) -> tuple[float, Sequence[Mapping[str, Any]] | None]:
        word_boundaries: Sequence[Mapping[str, Any]] | None = None
        for provider_attempt in range(3):
            output.unlink(missing_ok=True)
            try:
                word_boundaries = await self._synthesizer(
                    narration,
                    output,
                    self.settings.tts_voice,
                    rate,
                )
                break
            except CapabilityUnavailable:
                raise
            except Exception as exc:
                logger.warning(
                    "Edge TTS synthesis attempt failed attempt=%s error_type=%s",
                    provider_attempt + 1,
                    type(exc).__name__,
                    exc_info=True,
                )
                if provider_attempt == 2:
                    raise RetryableStepError(
                        "configured Edge TTS provider is temporarily unavailable"
                    ) from exc
                await context.checkpoint()
                await asyncio.sleep(2**provider_attempt)
        if not output.is_file() or output.stat().st_size < 1024:
            raise RetryableStepError("configured Edge TTS provider returned invalid audio")
        if self._duration_probe is not None:
            return await self._duration_probe(output, context), word_boundaries
        ffprobe = _resolve_command(self.settings.ffprobe_command, "FFprobe")
        return (
            await _probe_duration(ffprobe, output, context, cwd=output.parent),
            word_boundaries,
        )


class FFmpegQualityCapability:
    """Inspect the actual rendered bytes and only escalate concrete AV failures."""

    def __init__(self, settings: LegacyMediaSettings, storage: ArtifactStorage) -> None:
        self.settings = settings
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        video = _required_artifact(context, "video")
        ffprobe = _resolve_command(self.settings.ffprobe_command, "FFprobe")
        ffmpeg = _resolve_command(self.settings.ffmpeg_command, "FFmpeg")
        with tempfile.TemporaryDirectory(prefix="framefactory-qc-") as directory:
            root = Path(directory)
            source = root / "final.mp4"
            await _materialize_artifact(self.storage, video, source)
            probe_output = await _run_command(
                (
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=index,codec_type,codec_name,width,height",
                    "-of",
                    "json",
                    source.name,
                ),
                context,
                cwd=root,
                timeout_seconds=60,
            )
            try:
                probe = json.loads(probe_output)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PermanentStepError("rendered video probe returned invalid JSON") from exc
            analysis_log = await _run_av_detection(ffmpeg, source, context, cwd=root)

        streams = probe.get("streams", []) if isinstance(probe, Mapping) else []
        video_streams = [
            item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"
        ]
        audio_streams = [
            item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"
        ]
        duration = _number((probe.get("format") or {}).get("duration"))
        profile = _render_profile(context.input_snapshot, self.settings)
        target_duration = _target_duration_seconds(context.input_snapshot)
        black_seconds = _detected_duration(analysis_log, "black_start", "black_end")
        silent_seconds = _detected_duration(analysis_log, "silence_start", "silence_end")
        risks: list[str] = []
        if not video_streams:
            risks.append("video_stream_missing")
        if not audio_streams:
            risks.append("audio_stream_missing")
        if duration <= 0:
            risks.append("duration_invalid")
        if target_duration is not None and not _duration_fits(duration, target_duration, 0.25):
            risks.append("duration_out_of_range")
        if video_streams:
            width = int(video_streams[0].get("width") or 0)
            height = int(video_streams[0].get("height") or 0)
            if (width, height) != (profile.width, profile.height):
                risks.append("resolution_mismatch")
        if duration > 0 and black_seconds > max(0.75, duration * 0.03):
            risks.append("excessive_black_frames")
        if duration > 0 and silent_seconds > max(3.0, duration * 0.4):
            risks.append("excessive_audio_silence")
        report = {
            "schema_version": "1.0.0",
            "scope": "decoded_audio_video",
            "verdict": "pass" if not risks else "needs_review",
            "risks": risks,
            "duration_seconds": round(duration, 3),
            "target_duration_seconds": target_duration,
            "black_seconds": round(black_seconds, 3),
            "silent_seconds": round(silent_seconds, 3),
            "video_streams": len(video_streams),
            "audio_streams": len(audio_streams),
        }
        artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "qc_report",
                "quality.json",
                "application/json",
                json.dumps(report, ensure_ascii=False, sort_keys=True).encode(),
            ),
        )
        return StepResult(
            artifacts=(artifact,),
            output_summary=report,
            requires_review=bool(risks),
        )


class LegacyAssetCapability:
    def __init__(self, settings: LegacyMediaSettings, storage: ArtifactStorage) -> None:
        self.settings = settings
        self.storage = storage
        self._catalog = _load_catalog(settings)

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script = dict(self.storage.read_json(_required_artifact(context, "script")))
        selected = _select_assets(script, self._catalog, self.settings.maximum_assets)
        if not selected:
            raise PermanentStepError("configured legacy catalogs contain no usable local media")

        references: list[ArtifactRef] = []
        manifest_items: list[dict[str, Any]] = []
        unverified_rights = False
        for index, item in enumerate(selected, start=1):
            await context.checkpoint()
            source = item["_path"]
            suffix = source.suffix.lower()
            filename = f"asset-{index:03d}{suffix}"
            media_type = mimetypes.guess_type(filename)[0] or (
                "image/jpeg" if suffix in _IMAGE_SUFFIXES else "video/mp4"
            )
            artifact = self.storage.publish(
                context,
                ProviderArtifact("asset", filename, media_type, source.read_bytes()),
            )
            references.append(artifact)
            rights_verified = _rights_verified(item)
            unverified_rights = unverified_rights or not rights_verified
            manifest_items.append(
                {
                    "filename": filename,
                    "artifact_id": artifact.id,
                    "media_type": media_type,
                    "duration_seconds": _positive_number(item.get("dur")),
                    "labels": _labels(item),
                    "rights_verified": rights_verified,
                }
            )

        manifest = {
            "schema_version": "1.0.0",
            "provider": "legacy-catalog-migration-bridge",
            "script": script,
            "assets": manifest_items,
            "rights_status": "review_required" if unverified_rights else "verified",
        }
        manifest_ref = self.storage.publish(
            context,
            ProviderArtifact(
                "manifest",
                "asset-manifest.json",
                "application/json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            ),
        )
        return StepResult(
            artifacts=(*references, manifest_ref),
            output_summary={
                "provider": "legacy-catalog-migration-bridge",
                "selected_assets": len(references),
                "rights_status": manifest["rights_status"],
            },
            requires_review=unverified_rights,
        )


class FFmpegRenderCapability:
    def __init__(self, settings: LegacyMediaSettings, storage: ArtifactStorage) -> None:
        self.settings = settings
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        profile = _render_profile(context.input_snapshot, self.settings)
        ffmpeg = _resolve_command(self.settings.ffmpeg_command, "FFmpeg")
        ffprobe = _resolve_command(self.settings.ffprobe_command, "FFprobe")
        audio_ref = _required_artifact(context, "audio")
        manifest_ref = _required_artifact(context, "manifest")
        manifest = dict(self.storage.read_json(manifest_ref))
        timing_refs = [
            artifact
            for artifact in context.input_artifacts
            if artifact.kind == "narration_timing"
        ]
        webpage_video = _is_webpage_video_snapshot(context.input_snapshot)
        if webpage_video and len(timing_refs) != 1:
            raise PermanentStepError(
                "webpage render requires exactly one native narration timing artifact"
            )
        if len(timing_refs) > 1:
            raise PermanentStepError("render received multiple narration timing artifacts")
        narration_timing: Mapping[str, Any] | None = None
        if timing_refs:
            candidate = self.storage.read_json(timing_refs[0])
            if not isinstance(candidate, Mapping):
                raise PermanentStepError("narration timing artifact must be an object")
            narration_timing = candidate
            if candidate.get("audio_content_hash") != audio_ref.content_hash:
                raise PermanentStepError("narration timing targets a different audio artifact")
            script_ref = _required_artifact(context, "script")
            if candidate.get("script_content_hash") != script_ref.content_hash:
                raise PermanentStepError("narration timing targets a different script artifact")
            if webpage_video and (
                bool(candidate.get("word_timing_estimated", True))
                or not candidate.get("words")
            ):
                raise PermanentStepError(
                    "webpage render requires native WordBoundary narration timing"
                )
        entries = manifest.get("assets")
        if not isinstance(entries, list) or not entries:
            raise PermanentStepError("asset manifest contains no renderable media")
        by_id = {artifact.id: artifact for artifact in context.input_artifacts if artifact.kind == "asset"}

        with tempfile.TemporaryDirectory(prefix="framefactory-render-") as directory:
            work = Path(directory)
            audio_path = work / "narration.mp3"
            audio_path.write_bytes(self.storage.read_bytes(audio_ref))
            asset_paths: list[tuple[Path, Mapping[str, Any]]] = []
            materialized_assets: dict[str, Path] = {}
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, Mapping):
                    raise PermanentStepError("asset manifest entry must be an object")
                artifact = by_id.get(str(entry.get("artifact_id", "")))
                if artifact is None:
                    raise PermanentStepError("asset manifest references an unavailable artifact")
                suffix = Path(artifact.filename).suffix.lower()
                if suffix not in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES:
                    raise PermanentStepError("asset artifact has an unsupported media extension")
                path = materialized_assets.get(artifact.id)
                if path is None:
                    path = work / f"source-{len(materialized_assets) + 1:03d}{suffix}"
                    await _materialize_artifact(self.storage, artifact, path)
                    materialized_assets[artifact.id] = path
                asset_paths.append((path, entry))

            duration = await _probe_duration(ffprobe, audio_path, context, cwd=work)
            if duration <= 0.2:
                raise PermanentStepError("narration audio duration is too short to render")
            timeline = plan_edit_timeline(
                manifest.get("script", {}) if isinstance(manifest.get("script"), Mapping) else {},
                entries,
                duration,
            )
            if not timeline:
                raise PermanentStepError("render timeline could not be planned")
            segment_paths: list[Path] = []
            for index, shot in enumerate(timeline, start=1):
                await context.checkpoint()
                asset_index = int(shot["asset_index"])
                source, entry = asset_paths[asset_index]
                segment = work / f"segment-{index:03d}.mp4"
                command = _segment_command(
                    ffmpeg,
                    source,
                    segment,
                    float(shot["duration_seconds"]),
                    width=profile.width,
                    height=profile.height,
                    frame_rate=profile.frame_rate,
                    layout=profile.layout,
                    media_fit=profile.media_fit,
                    background_color=profile.background_color,
                    source_start_seconds=float(shot["source_start_seconds"]),
                    source_end_seconds=float(shot["source_end_seconds"]),
                    image_motion=str(shot.get("motion") or entry.get("motion") or "static"),
                    motion_focus=(
                        shot.get("motion_focus")
                        if isinstance(shot.get("motion_focus"), Mapping)
                        else entry.get("motion_focus")
                    ),
                )
                await _run_command(command, context, cwd=work, timeout_seconds=900)
                segment_paths.append(segment)

            concat_file = work / "segments.txt"
            concat_file.write_text(
                "".join(f"file '{path.name}'\n" for path in segment_paths),
                encoding="utf-8",
            )
            visual = work / "visual.mp4"
            await _run_command(
                (
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    concat_file.name,
                    "-c",
                    "copy",
                    visual.name,
                ),
                context,
                cwd=work,
                timeout_seconds=900,
            )
            captions = work / "captions.ass"
            captions.write_text(
                _build_ass(
                    manifest.get("script", {}),
                    duration,
                    timing=narration_timing,
                    width=profile.width,
                    height=profile.height,
                    layout=profile.layout,
                    subtitle_enabled=profile.subtitle_enabled,
                    subtitle_position=profile.subtitle_position,
                    subtitle_size=profile.subtitle_size,
                    max_lines=profile.subtitle_max_lines,
                ),
                encoding="utf-8-sig",
            )
            output = work / "final.mp4"
            await _run_command(
                (
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    visual.name,
                    "-i",
                    audio_path.name,
                    "-vf",
                    "ass=captions.ass",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "21",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    output.name,
                ),
                context,
                cwd=work,
                timeout_seconds=3600,
            )
            rendered_duration = await _probe_duration(ffprobe, output, context, cwd=work)
            if abs(rendered_duration - duration) > max(0.5, 1 / profile.frame_rate * 3):
                raise PermanentStepError("rendered video failed the audio/video duration gate")
            data = output.read_bytes()

        artifact = self.storage.publish(
            context,
            ProviderArtifact("video", "final.mp4", "video/mp4", data),
        )
        timeline_artifact = self.storage.publish(
            context,
            ProviderArtifact(
                "timeline",
                "edit-timeline.json",
                "application/json",
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "duration_seconds": round(duration, 3),
                        "shots": timeline,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
            ),
        )
        return StepResult(
            artifacts=(artifact, timeline_artifact),
            output_summary={
                "provider": "ffmpeg-legacy-render",
                "duration_seconds": round(rendered_duration, 3),
                "width": profile.width,
                "height": profile.height,
                "frame_rate": profile.frame_rate,
                "layout": profile.layout,
                "media_fit": profile.media_fit,
                "assets": len(asset_paths),
                "shots": len(timeline),
                "candidate_cuts": sum(
                    shot["cut_evidence"] == "candidate" for shot in timeline
                ),
                "subtitle_timing": (
                    "narration_timing" if narration_timing is not None else "duration_weighted"
                ),
            },
        )


async def _edge_synthesize(
    text: str, output: Path, voice: str, rate: str
) -> tuple[Mapping[str, Any], ...]:
    try:
        from edge_tts import Communicate
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise CapabilityUnavailable("Edge TTS dependency is not installed") from exc

    boundaries: list[Mapping[str, Any]] = []
    with output.open("wb") as stream:
        # edge-tts 7.2.x defaults to SentenceBoundary.  Request word metadata
        # explicitly; otherwise the filter below would truthfully return no
        # native word track and every production synthesis would be escalated
        # as estimated timing.
        async for event in Communicate(
            text,
            voice,
            rate=rate,
            boundary="WordBoundary",
        ).stream():
            event_type = str(event.get("type", ""))
            if event_type == "audio":
                data = event.get("data")
                if isinstance(data, bytes):
                    stream.write(data)
            elif event_type == "WordBoundary":
                boundaries.append(
                    {
                        "text": str(event.get("text", "")),
                        "offset": event.get("offset"),
                        "duration": event.get("duration"),
                    }
                )
    return tuple(boundaries)


def _narration_timing_payload(
    script: Mapping[str, Any],
    *,
    duration: float,
    word_boundaries: Sequence[Mapping[str, Any]] | None,
    audio_content_hash: str,
    script_content_hash: str,
) -> dict[str, Any]:
    """Build one truthful timing artifact for both production and fallback TTS.

    Edge ``WordBoundary`` events use 100-nanosecond ticks.  Test doubles and
    alternate adapters may instead return explicit second fields.  If neither
    representation yields a valid word track, sentence intervals are derived
    deterministically from the measured audio duration and marked estimated.
    """

    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("narration timing duration must be positive and finite")
    rounded_duration = round(duration, 6)
    if rounded_duration <= 0:
        raise PermanentStepError(
            "narration timing duration rounds to a non-positive schema value"
        )
    narration = str(script.get("narration", "")).strip()
    words = _normalize_word_boundaries(word_boundaries, duration)
    if len(words) > _MAX_TIMING_WORDS:
        raise PermanentStepError(
            "native narration timing exceeds the 10000 word interval limit"
        )
    word_timing_estimated = not words
    sentences = [item.strip() for item in _SENTENCE_BREAK.split(narration) if item.strip()]
    if not sentences:
        sentences = [narration]
    if len(sentences) > _MAX_TIMING_SEGMENTS:
        raise PermanentStepError(
            "script narration exceeds the 10000 segment narration timing limit"
        )
    segments = _aligned_timing_units(
        tuple({"text": sentence} for sentence in sentences),
        duration=duration,
        words=words,
        word_timing_estimated=word_timing_estimated,
        unit_prefix="sentence",
    )
    raw_beats = script.get("beats")
    has_authored_beats = (
        isinstance(raw_beats, Sequence)
        and not isinstance(raw_beats, (str, bytes))
        and any(isinstance(item, Mapping) for item in raw_beats)
    )
    beats = normalize_beats(script) if has_authored_beats else ()
    beat_units = tuple(
        {
            "id": beat.id,
            "sequence": beat.sequence,
            "text": beat.narration,
        }
        for beat in beats
    )
    aligned_beats = _aligned_timing_units(
        beat_units,
        duration=duration,
        words=words,
        word_timing_estimated=word_timing_estimated,
        unit_prefix="beat",
    )
    span_estimated = any(
        bool(item.get("estimated")) for item in (*segments, *aligned_beats)
    )
    return {
        "schema_version": "1.0.0",
        "operation": "audio.synthesize",
        "provider": "edge-tts",
        "source": (
            "duration_weighted_sentence_estimate"
            if word_timing_estimated
            else "edge_word_boundary"
        ),
        "granularity": "sentence" if word_timing_estimated else "word",
        "estimated": word_timing_estimated or span_estimated,
        "word_timing_estimated": word_timing_estimated,
        "duration_seconds": rounded_duration,
        "audio_content_hash": audio_content_hash,
        "script_content_hash": script_content_hash,
        "words": list(words),
        "segments": list(segments),
        "beats": list(aligned_beats),
    }


def _normalize_word_boundaries(
    values: Sequence[Mapping[str, Any]] | None,
    duration: float,
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for raw in values or ():
        if not isinstance(raw, Mapping):
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        start = _finite_number(raw.get("start_seconds"))
        end = _finite_number(raw.get("end_seconds"))
        if start is None:
            offset = _finite_number(raw.get("offset"))
            start = offset / 10_000_000 if offset is not None else None
        if end is None:
            raw_duration = _finite_number(raw.get("duration"))
            end = (
                start + raw_duration / 10_000_000
                if start is not None and raw_duration is not None
                else None
            )
        if start is None or end is None or start < 0 or end <= start:
            continue
        clipped_start = min(duration, start)
        clipped_end = min(duration, end)
        if clipped_end <= clipped_start:
            continue
        rounded_start = round(clipped_start, 6)
        rounded_end = round(clipped_end, 6)
        if rounded_end <= rounded_start or rounded_end <= 0:
            continue
        normalized.append(
            {
                "ordinal": len(normalized),
                "text": text,
                "start_seconds": rounded_start,
                "end_seconds": rounded_end,
                "estimated": False,
                "alignment_source": "native_word_boundary",
            }
        )
    normalized.sort(key=lambda item: (item["start_seconds"], item["ordinal"]))
    for ordinal, item in enumerate(normalized):
        item["ordinal"] = ordinal
    return tuple(normalized)


def _aligned_timing_units(
    units: Sequence[Mapping[str, Any]],
    *,
    duration: float,
    words: Sequence[Mapping[str, Any]],
    word_timing_estimated: bool,
    unit_prefix: str,
) -> tuple[dict[str, Any], ...]:
    values = [dict(unit) for unit in units if str(unit.get("text", "")).strip()]
    if not values:
        return ()
    native_cuts = _exact_word_aggregation_cuts(values, words, duration)
    if native_cuts is not None:
        cuts = native_cuts
        alignment_estimated = False
        alignment_source = "native_word_aggregation"
    else:
        weights = [_timing_weight(str(unit["text"])) for unit in values]
        total_weight = sum(weights) or float(len(values))
        cumulative = 0.0
        cuts = [0.0]
        internal_cut_count = len(values) - 1
        native_candidates = sorted(
            {
                float(word["start_seconds"])
                for word in words[1:]
                if 0 < float(word["start_seconds"]) < duration
            }
        )
        use_native_candidates = (
            len(words) >= len(values)
            and len(native_candidates) >= internal_cut_count
        )
        for cut_index, weight in enumerate(weights[:-1]):
            cumulative += weight
            target = duration * cumulative / total_weight
            if use_native_candidates:
                remaining_after = internal_cut_count - cut_index - 1
                eligible = [value for value in native_candidates if value > cuts[-1]]
                selectable = eligible[: len(eligible) - remaining_after]
                if selectable:
                    target = min(
                        selectable,
                        key=lambda value: (abs(value - target), value),
                    )
            cuts.append(max(cuts[-1], min(duration, target)))
        cuts.append(duration)
        alignment_estimated = True
        alignment_source = (
            "duration_weighted_sentence_estimate"
            if word_timing_estimated
            else "word_boundary_proportional_estimate"
        )
    result: list[dict[str, Any]] = []
    for index, unit in enumerate(values):
        start = cuts[index]
        end = cuts[index + 1]
        if end <= start:
            raise PermanentStepError(
                "narration timing unit has a non-positive interval"
            )
        rounded_start = round(start, 6)
        rounded_end = round(end, 6)
        if rounded_end <= rounded_start or rounded_end <= 0:
            raise PermanentStepError(
                "narration timing unit rounds to a non-positive interval"
            )
        result.append(
            {
                "id": str(unit.get("id") or f"{unit_prefix}-{index + 1:03d}"),
                "sequence": _positive_int(unit.get("sequence"), index + 1),
                "text": str(unit["text"]),
                "start_seconds": rounded_start,
                "end_seconds": rounded_end,
                "estimated": alignment_estimated,
                "alignment_source": alignment_source,
            }
        )
    if result:
        result[0]["start_seconds"] = 0.0
        result[-1]["end_seconds"] = round(duration, 6)
    return tuple(result)


def _exact_word_aggregation_cuts(
    units: Sequence[Mapping[str, Any]],
    words: Sequence[Mapping[str, Any]],
    duration: float,
) -> list[float] | None:
    """Return native cuts only when unit text is an exact ordered token grouping."""

    if not words or len(units) > len(words):
        return None
    unit_tokens = [_timing_token_text(str(unit.get("text", ""))) for unit in units]
    word_tokens = [_timing_token_text(str(word.get("text", ""))) for word in words]
    if not all(unit_tokens) or not all(word_tokens):
        return None
    if "".join(unit_tokens) != "".join(word_tokens):
        return None
    cumulative_words: dict[int, int] = {}
    total = 0
    for index, token in enumerate(word_tokens, start=1):
        total += len(token)
        cumulative_words[total] = index
    cuts = [0.0]
    cumulative_units = 0
    for token in unit_tokens[:-1]:
        cumulative_units += len(token)
        word_index = cumulative_words.get(cumulative_units)
        if word_index is None or word_index >= len(words):
            return None
        cuts.append(float(words[word_index]["start_seconds"]))
    cuts.append(duration)
    if any(end <= start for start, end in pairwise(cuts)):
        return None
    return cuts


def _timing_token_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value).casefold()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timing_weight(value: str) -> float:
    visible = len(re.sub(r"\s+", "", value))
    pauses = 0.7 * len(re.findall(r"[，、,:：]", value))
    stops = 1.8 * len(re.findall(r"[。！？!?；;]", value))
    return max(1.0, visible + pauses + stops)


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _target_duration_seconds(snapshot: object) -> float | None:
    root = snapshot if isinstance(snapshot, Mapping) else {}
    if _is_webpage_video_snapshot(root):
        value = root.get("duration_seconds")
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if 1 <= number <= 86_400 else None
    framefactory = root.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    production = composition.get("production_settings", {})
    production = production if isinstance(production, Mapping) else {}
    value = production.get("target_duration_seconds")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 86_400 else None


def _is_webpage_video_snapshot(snapshot: object) -> bool:
    root = snapshot if isinstance(snapshot, Mapping) else {}
    value = root.get("webpage_video_run_id")
    return isinstance(value, str) and bool(value.strip())


def _duration_fits(actual: float, target: float, tolerance: float) -> bool:
    return abs(actual - target) / target <= tolerance


def _adjust_tts_rate(current: str, actual: float, target: float) -> str:
    match = re.fullmatch(r"([+-]?)(\d{1,3})%", current.strip())
    if match is None or actual <= 0 or target <= 0:
        return current
    signed = int(match.group(2)) * (-1 if match.group(1) == "-" else 1)
    speed = max(0.1, 1 + signed / 100)
    adjusted = min(1.8, max(0.5, speed * actual / target))
    percentage = round((adjusted - 1) * 100)
    return f"{percentage:+d}%"


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    try:
        return next(item for item in context.input_artifacts if item.kind == kind)
    except StopIteration as exc:
        raise PermanentStepError(f"required {kind} artifact is unavailable") from exc


def _load_catalog(settings: LegacyMediaSettings) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for catalog_path in settings.catalog_paths:
        try:
            raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("legacy asset catalog is not valid UTF-8 JSON") from exc
        entries = raw.get("clips") if isinstance(raw, Mapping) else raw
        if not isinstance(entries, list):
            raise TypeError("legacy asset catalog must be a list or an object containing clips")
        for entry in entries:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("file"), str):
                continue
            path = (catalog_path.parent / entry["file"]).resolve()
            if not path.is_relative_to(settings.asset_root) or path in seen or not path.is_file():
                continue
            if path.suffix.lower() not in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES:
                continue
            if str(entry.get("has_text", "无")) not in {"", "无", "角落水印"}:
                continue
            seen.add(path)
            values.append({**dict(entry), "_path": path})
    return tuple(values)


def _select_assets(
    script: Mapping[str, Any], catalog: tuple[dict[str, Any], ...], maximum: int
) -> tuple[dict[str, Any], ...]:
    scenes = script.get("scenes")
    scene_values = [str(value) for value in scenes] if isinstance(scenes, list) else []
    query = " ".join(
        [str(script.get("title", "")), str(script.get("narration", "")), *scene_values]
    ).lower()
    target = min(len(catalog), maximum, max(3, len(scene_values), 10))

    def rank(item: Mapping[str, Any]) -> tuple[int, str]:
        labels = _labels(item)
        score = sum(3 if label.lower() in query else 0 for label in labels if len(label) >= 2)
        digest = hashlib.sha256(
            (query + "\0" + str(item.get("file", ""))).encode("utf-8")
        ).hexdigest()
        return score, digest

    ordered = sorted(catalog, key=rank, reverse=True)
    return tuple(ordered[:target])


def _labels(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("lib", "person", "scene", "action", "cat", "style", "mood"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    tags = item.get("tags")
    if isinstance(tags, list):
        values.extend(str(value).strip() for value in tags if str(value).strip())
    return values


def _rights_verified(item: Mapping[str, Any]) -> bool:
    value = item.get("license") or item.get("rights")
    if isinstance(value, str):
        return value.lower() in {"licensed", "owned", "public_domain", "cc0"}
    if isinstance(value, Mapping):
        return value.get("verified") is True
    return False


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(number, 3) if number > 0 else None


def _resolve_command(value: str, label: str) -> str:
    path = Path(value)
    resolved = str(path.resolve()) if path.is_file() else shutil.which(value)
    if not resolved:
        raise CapabilityUnavailable(f"{label} executable is not installed or configured")
    return resolved


async def _run_command(
    command: tuple[str, ...],
    context: StepContext,
    *,
    cwd: Path,
    timeout_seconds: float,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise CapabilityUnavailable("configured media executable could not be started") from exc
    communication = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + timeout_seconds
    try:
        while not communication.done():
            if time.monotonic() >= deadline:
                process.kill()
                await communication
                raise RetryableStepError("configured media process exceeded its timeout")
            await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
            await context.checkpoint()
        stdout, _stderr = await communication
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        communication.cancel()
        raise
    if process.returncode != 0:
        raise PermanentStepError(
            f"configured media process failed with exit code {process.returncode}"
        )
    return stdout


async def _run_av_detection(
    ffmpeg: str,
    source: Path,
    context: StepContext,
    *,
    cwd: Path,
) -> str:
    command = (
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        source.name,
        "-vf",
        "blackdetect=d=0.75:pix_th=0.10",
        "-af",
        "silencedetect=n=-50dB:d=3",
        "-f",
        "null",
        "-",
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise CapabilityUnavailable("configured FFmpeg quality probe could not start") from exc
    communication = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + 300
    while not communication.done():
        if time.monotonic() >= deadline:
            process.kill()
            await communication
            raise RetryableStepError("configured FFmpeg quality probe timed out")
        await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
        await context.checkpoint()
    _stdout, stderr = await communication
    if process.returncode != 0:
        raise PermanentStepError("rendered video could not be decoded for quality inspection")
    return stderr.decode("utf-8", errors="replace")


def _detected_duration(log: str, start_name: str, end_name: str) -> float:
    starts = [float(value) for value in re.findall(rf"{start_name}:\s*([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(rf"{end_name}:\s*([0-9.]+)", log)]
    return sum(max(0.0, end - start) for start, end in zip(starts, ends, strict=False))


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


async def _materialize_artifact(
    storage: ArtifactStorage, artifact: ArtifactRef, destination: Path
) -> None:
    materialize = getattr(storage, "materialize", None)
    if callable(materialize):
        await asyncio.to_thread(materialize, artifact, destination)
        return
    data = await asyncio.to_thread(storage.read_bytes, artifact)
    await asyncio.to_thread(destination.write_bytes, data)


async def _probe_duration(
    ffprobe: str, path: Path, context: StepContext, *, cwd: Path
) -> float:
    output = await _run_command(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path.name,
        ),
        context,
        cwd=cwd,
        timeout_seconds=60,
    )
    try:
        return float(output.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise PermanentStepError("media duration probe returned an invalid value") from exc


def _segment_command(
    ffmpeg: str,
    source: Path,
    output: Path,
    duration: float,
    *,
    width: int,
    height: int,
    frame_rate: int,
    layout: str = "full_frame",
    media_fit: str = "cover",
    background_color: str = "101218",
    source_start_seconds: float = 0.0,
    source_end_seconds: float | None = None,
    image_motion: str = "static",
    motion_focus: object = None,
) -> tuple[str, ...]:
    is_image = source.suffix.lower() in _IMAGE_SUFFIXES
    input_args: tuple[str, ...]
    source_duration = duration
    padding = 0.0
    if is_image:
        input_args = ("-loop", "1")
    else:
        source_duration = max(
            0.001,
            min(
                duration,
                (source_end_seconds or source_start_seconds + duration)
                - source_start_seconds,
            ),
        )
        padding = max(0.0, duration - source_duration)
        # Limit FFmpeg's input to the virtual clip produced during ingestion.
        # Repeating the complete source would cross the selected segment's end.
        input_args = ("-t", f"{source_duration:.6f}")
    video_filter = _media_filter(
        width,
        height,
        frame_rate,
        layout=layout,
        media_fit=media_fit,
        background_color=background_color,
    )
    if is_image and image_motion == "zoom_in":
        video_filter += _slow_zoom_filter(
            width,
            height,
            frame_rate,
            duration,
            motion_focus,
        )
    if padding > 0:
        video_filter += f",tpad=stop_mode=clone:stop_duration={padding:.6f}"
    video_filter += f",trim=duration={duration:.6f},setpts=PTS-STARTPTS"
    return (
        ffmpeg,
        "-y",
        "-v",
        "error",
        *input_args,
        "-ss",
        f"{source_start_seconds:.6f}",
        "-i",
        source.name,
        "-t",
        f"{duration:.6f}",
        "-vf",
        video_filter,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        output.name,
    )


def _media_filter(
    width: int,
    height: int,
    frame_rate: int,
    *,
    layout: str,
    media_fit: str,
    background_color: str,
) -> str:
    if layout == "editorial":
        viewport_y = round(height * 0.24)
        viewport_height = max(240, round(height * 0.43))
    else:
        viewport_y = 0
        viewport_height = height
    if media_fit == "blurred_contain":
        return (
            "split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=28[bgfill];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgfit];"
            "[bgfill][fgfit]overlay=(W-w)/2:(H-h)/2,"
            f"fps={frame_rate},format=yuv420p"
        )
    ratio = "increase" if media_fit == "cover" else "decrease"
    fitted = f"scale={width}:{viewport_height}:force_original_aspect_ratio={ratio}"
    if media_fit == "cover":
        fitted += f",crop={width}:{viewport_height}"
    else:
        fitted += (
            f",pad={width}:{viewport_height}:(ow-iw)/2:(oh-ih)/2:"
            f"color=0x{background_color}"
        )
    if layout == "editorial":
        fitted += (
            f",pad={width}:{height}:0:{viewport_y}:color=0x{background_color}"
        )
    return f"{fitted},fps={frame_rate},format=yuv420p"


def _render_profile(snapshot: object, fallback: LegacyMediaSettings) -> _RenderProfile:
    root = snapshot if isinstance(snapshot, Mapping) else {}
    framefactory = root.get("_framefactory", {})
    framefactory = framefactory if isinstance(framefactory, Mapping) else {}
    composition = framefactory.get("composition_snapshot", {})
    composition = composition if isinstance(composition, Mapping) else {}
    production = composition.get("production_settings", {})
    production = production if isinstance(production, Mapping) else {}
    resolution = production.get("resolution", {})
    resolution = resolution if isinstance(resolution, Mapping) else {}
    subtitles = production.get("subtitles", {})
    subtitles = subtitles if isinstance(subtitles, Mapping) else {}
    # Browser regions frequently have landscape or content-height dimensions.
    # Letterboxing those assets inside a portrait webpage video leaves most of
    # the frame black and makes the detail motion ineffective.  Webpage runs
    # therefore use a dedicated full-bleed fit; other pipelines continue to
    # honour their immutable production setting.
    webpage_media_fit = "blurred_contain" if _is_webpage_video_snapshot(root) else None

    def integer(value: object, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
            return value
        return default

    def choice(value: object, default: str, allowed: set[str]) -> str:
        return value if isinstance(value, str) and value in allowed else default

    return _RenderProfile(
        width=integer(resolution.get("width"), fallback.width, 240, 7680),
        height=integer(resolution.get("height"), fallback.height, 240, 7680),
        frame_rate=integer(production.get("frame_rate"), fallback.frame_rate, 1, 120),
        layout=choice(production.get("layout"), "full_frame", {"full_frame", "editorial"}),
        media_fit=webpage_media_fit
        or choice(
            production.get("media_fit"),
            "cover",
            {"cover", "contain", "blurred_contain"},
        ),
        subtitle_enabled=(
            subtitles.get("enabled")
            if isinstance(subtitles.get("enabled"), bool)
            else True
        ),
        subtitle_position=choice(
            subtitles.get("position"), "bottom", {"bottom", "lower_third"}
        ),
        subtitle_size=choice(
            subtitles.get("size"), "medium", {"small", "medium", "large"}
        ),
        subtitle_max_lines=integer(subtitles.get("max_lines"), 2, 1, 3),
    )


def _text_units(value: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(character) in {"W", "F", "A"} else 0.56
        for character in value
    )


def _wrap_lines(value: str, maximum_units: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in value.strip():
        if character == "\n":
            if current:
                lines.append(current.rstrip())
                current = ""
            continue
        candidate = current + character
        if current and _text_units(candidate) > maximum_units:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return lines or [" "]


def _caption_cues(narration: str, maximum_units: int, max_lines: int) -> list[str]:
    sentences = [value.strip() for value in _SENTENCE_BREAK.split(narration) if value.strip()]
    if not sentences:
        sentences = [narration or " "]
    cues: list[str] = []
    for sentence in sentences:
        lines = _wrap_lines(sentence, maximum_units)
        for index in range(0, len(lines), max_lines):
            cues.append("\n".join(lines[index : index + max_lines]))
    return cues


def _build_ass(
    script: object,
    duration: float,
    *,
    timing: object | None = None,
    width: int,
    height: int,
    layout: str = "full_frame",
    subtitle_enabled: bool = True,
    subtitle_position: str = "bottom",
    subtitle_size: str = "medium",
    max_lines: int = 2,
) -> str:
    mapping = script if isinstance(script, Mapping) else {}
    narration = str(mapping.get("narration", "")).strip()
    size_scale = {"small": 0.031, "medium": 0.038, "large": 0.046}.get(
        subtitle_size, 0.038
    )
    font_size = max(28, round(height * size_scale))
    margin_horizontal = max(40, round(width * 0.055))
    safe_width = width - 2 * margin_horizontal
    maximum_units = max(8, int(safe_width / max(1, font_size)))
    chunks = _caption_cues(narration, maximum_units, max_lines)
    total_characters = max(1, sum(len(value.replace("\n", "")) for value in chunks))
    timed_chunks = _timed_caption_cues(timing, maximum_units, max_lines, duration)
    margin_vertical = max(
        40,
        round(height * (0.26 if subtitle_position == "lower_third" else 0.14)),
    )
    title = str(mapping.get("title", "")).strip()
    title_size = max(36, round(height * 0.062))
    title_lines = "\n".join(
        _wrap_lines(title, max(8, int(width * 0.76 / title_size)))[:2]
    )
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,4,1,2,{margin_horizontal},{margin_horizontal},{margin_vertical},1
Style: AiLabel,Noto Sans CJK SC,{max(18, font_size // 2)},&H00CCCCCC,&H00CCCCCC,&H00000000,&H50000000,0,0,0,0,100,100,0,0,1,2,0,7,24,24,24,1
Style: Title,Noto Sans CJK SC,{title_size},&H00F1E8D2,&H00F1E8D2,&H00101218,&H00101218,-1,0,0,0,100,100,2,0,1,2,0,8,{margin_horizontal},{margin_horizontal},{max(32, round(height * 0.065))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},AiLabel,,0,0,0,,内容由AI生成"]
    if layout == "editorial" and title_lines:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(duration)},Title,,0,0,0,,{_ass_text(title_lines)}"
        )
    if not subtitle_enabled:
        return header + "\n".join(lines) + "\n"
    if timed_chunks:
        for start, end, chunk in timed_chunks:
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{_ass_text(chunk)}"
            )
        return header + "\n".join(lines) + "\n"
    cursor = 0.0
    for index, chunk in enumerate(chunks):
        part = duration * len(chunk.replace("\n", "")) / total_characters
        end = duration if index == len(chunks) - 1 else cursor + part
        lines.append(
            f"Dialogue: 0,{_ass_time(cursor)},{_ass_time(end)},Default,,0,0,0,,{_ass_text(chunk)}"
        )
        cursor = end
    return header + "\n".join(lines) + "\n"


def _timed_caption_cues(
    timing: object,
    maximum_units: int,
    max_lines: int,
    duration: float,
) -> tuple[tuple[float, float, str], ...]:
    if not isinstance(timing, Mapping):
        return ()
    raw_words = timing.get("words")
    if not isinstance(raw_words, list) or not raw_words:
        raise PermanentStepError("narration timing contains no word cues")
    words: list[tuple[str, float, float]] = []
    prior_start = -1.0
    for raw in raw_words:
        if not isinstance(raw, Mapping) or bool(raw.get("estimated")):
            raise PermanentStepError("narration timing word cue is estimated or malformed")
        text = str(raw.get("text") or "").strip()
        start = _finite_number(raw.get("start_seconds"))
        end = _finite_number(raw.get("end_seconds"))
        if (
            not text
            or start is None
            or end is None
            or start < 0
            or end <= start
            or start < prior_start
            or end > duration + 0.25
        ):
            raise PermanentStepError("narration timing word cue is outside audio duration")
        prior_start = start
        words.append((text, start, min(duration, end)))

    groups: list[list[tuple[str, float, float]]] = []
    current: list[tuple[str, float, float]] = []
    for word in words:
        candidate = [*current, word]
        text = _join_timing_words(candidate)
        if current and len(_wrap_lines(text, maximum_units)) > max_lines:
            groups.append(current)
            current = [word]
        else:
            current = candidate
    if current:
        groups.append(current)
    return tuple(
        (
            group[0][1],
            group[-1][2],
            "\n".join(_wrap_lines(_join_timing_words(group), maximum_units)[:max_lines]),
        )
        for group in groups
    )


def _slow_zoom_filter(
    width: int,
    height: int,
    frame_rate: int,
    duration: float,
    focus: object,
) -> str:
    value = focus if isinstance(focus, Mapping) else {}
    focus_x = min(
        1.0, max(0.0, _number(value.get("x", 0.5)) if value else 0.5)
    )
    focus_y = min(
        1.0, max(0.0, _number(value.get("y", 0.5)) if value else 0.5)
    )
    last_frame = max(1, round(duration * frame_rate) - 1)
    zoom = (
        "1+0.10*(0.5-0.5*cos(PI*"
        f"min(on,{last_frame})/{last_frame}))"
    )
    x = f"max(0,min(iw-iw/zoom,{focus_x:.6f}*iw-iw/(2*zoom)))"
    y = f"max(0,min(ih-ih/zoom,{focus_y:.6f}*ih-ih/(2*zoom)))"
    return (
        f",zoompan=z='{zoom}':x='{x}':y='{y}':d=1:"
        f"s={width}x{height}:fps={frame_rate},format=yuv420p"
    )


def _join_timing_words(words: Sequence[tuple[str, float, float]]) -> str:
    result = ""
    for text, _start, _end in words:
        separator = (
            " "
            if result
            and result[-1:].isascii()
            and result[-1:].isalnum()
            and text[:1].isascii()
            and text[:1].isalnum()
            else ""
        )
        result += separator + text
    return result


def _ass_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    remaining = seconds % 60
    return f"{hours}:{minutes:02d}:{remaining:05.2f}"


def _ass_text(value: str) -> str:
    cleaned = value.replace("\\", "").replace("{", "（").replace("}", "）")
    return cleaned.replace("\n", "\\N")
