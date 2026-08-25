"""Strict FFmpeg renderer for frozen material_selection + timeline contracts."""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.config import LegacyMediaSettings
from framefactory.worker.edl import EdlValidationError, validate_edl
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact

from .legacy_media import (
    _ass_text,
    _ass_time,
    _build_ass,
    _materialize_artifact,
    _media_filter,
    _probe_duration,
    _render_profile,
    _resolve_command,
    _run_command,
)

_AI_DISCLOSURE = "AI-generated content; mode=generated_only"


class FFmpegEdlRenderCapability:
    """Render a prevalidated EDL without replanning, freezing, or video loops."""

    def __init__(self, settings: LegacyMediaSettings, storage: ArtifactStorage) -> None:
        self.settings = settings
        self.storage = storage

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        profile = _render_profile(context.input_snapshot, self.settings)
        ffmpeg = _resolve_command(self.settings.ffmpeg_command, "FFmpeg")
        ffprobe = _resolve_command(self.settings.ffprobe_command, "FFprobe")
        audio_ref = _required_artifact(context, "audio")
        timing_ref = _required_artifact(context, "narration_timing")
        manifest_ref = _required_artifact(context, "candidate_manifest")
        selection_ref = _required_artifact(context, "material_selection")
        timeline_ref = _required_artifact(context, "timeline")
        candidate_manifest = dict(self.storage.read_json(manifest_ref))
        ai_disclosure = _full_ai_disclosure(context, candidate_manifest)
        material_selection = dict(self.storage.read_json(selection_ref))
        timeline = dict(self.storage.read_json(timeline_ref))
        asset_refs = {
            artifact.id: artifact
            for artifact in context.input_artifacts
            if artifact.kind == "asset"
        }
        try:
            structural_shots = validate_edl(
                material_selection,
                timeline,
                asset_refs,
                audio_ref=audio_ref,
                narration_timing_ref=timing_ref,
                candidate_manifest_ref=manifest_ref,
                candidate_manifest=candidate_manifest,
            )
        except EdlValidationError as exc:
            raise PermanentStepError(f"render.edl rejected its input: {exc}") from exc

        frame_rate = timeline.get("output_frame_rate")
        assert isinstance(frame_rate, Mapping)
        frame_rate_num = int(frame_rate["num"])
        frame_rate_den = int(frame_rate["den"])
        if frame_rate_den != 1 or frame_rate_num != profile.frame_rate:
            raise PermanentStepError(
                "render profile frame rate does not match the frozen timeline"
            )
        with tempfile.TemporaryDirectory(prefix="framefactory-edl-render-") as directory:
            work = Path(directory)
            audio_path = work / "narration.mp3"
            await _materialize_artifact(self.storage, audio_ref, audio_path)
            selected_artifact_ids = tuple(
                dict.fromkeys(shot.artifact_id for shot in structural_shots)
            )
            asset_paths: dict[str, Path] = {}
            source_durations: dict[str, float] = {}
            for index, artifact_id in enumerate(selected_artifact_ids, start=1):
                artifact = asset_refs[artifact_id]
                suffix = Path(artifact.filename).suffix.lower()
                if not suffix:
                    raise PermanentStepError("selected asset filename has no media suffix")
                path = work / f"source-{index:03d}{suffix}"
                await _materialize_artifact(self.storage, artifact, path)
                asset_paths[artifact_id] = path
                if artifact.media_type.startswith("video/"):
                    source_durations[artifact_id] = await _probe_duration(
                        ffprobe, path, context, cwd=work
                    )
            audio_duration = await _probe_duration(ffprobe, audio_path, context, cwd=work)
            try:
                shots = validate_edl(
                    material_selection,
                    timeline,
                    asset_refs,
                    audio_ref=audio_ref,
                    narration_timing_ref=timing_ref,
                    candidate_manifest_ref=manifest_ref,
                    candidate_manifest=candidate_manifest,
                    audio_duration_seconds=audio_duration,
                    source_durations=source_durations,
                )
            except EdlValidationError as exc:
                raise PermanentStepError(f"render.edl media preflight failed: {exc}") from exc

            segment_paths: list[Path] = []
            for shot in shots:
                await context.checkpoint()
                source = asset_paths[shot.artifact_id]
                segment = work / f"segment-{shot.ordinal + 1:04d}.mp4"
                command = _edl_segment_command(
                    ffmpeg,
                    source,
                    segment,
                    media_kind=shot.media_kind,
                    duration=shot.duration_seconds,
                    source_start_seconds=shot.source_start_seconds,
                    width=profile.width,
                    height=profile.height,
                    frame_rate=profile.frame_rate,
                    layout=profile.layout,
                    media_fit=profile.media_fit,
                    background_color=profile.background_color,
                )
                _assert_no_video_padding(command)
                await _run_command(command, context, cwd=work, timeout_seconds=900)
                segment_duration = await _probe_duration(
                    ffprobe,
                    segment,
                    context,
                    cwd=work,
                )
                _validate_rendered_duration(
                    segment_duration,
                    expected_duration=shot.duration_seconds,
                    frame_rate=profile.frame_rate,
                )
                segment_paths.append(segment)

            concat_file = work / "segments.txt"
            concat_file.write_text(
                "".join(f"file '{path.name}'\n" for path in segment_paths),
                encoding="utf-8",
            )
            visual = work / "visual.mp4"
            concat_command = (
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
            )
            _assert_no_video_padding(concat_command)
            await _run_command(
                concat_command,
                context,
                cwd=work,
                timeout_seconds=900,
            )

            duration = float(timeline["duration_seconds"])
            visual_duration = await _probe_duration(ffprobe, visual, context, cwd=work)
            _validate_rendered_duration(
                visual_duration,
                expected_duration=duration,
                frame_rate=profile.frame_rate,
            )
            captions = work / "captions.ass"
            captions.write_text(
                _build_timed_ass(
                    timeline,
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
            mux_command = (
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
                "-t",
                f"{duration:.9f}",
                "-movflags",
                "+faststart",
                *_ai_disclosure_metadata_args(ai_disclosure),
                output.name,
            )
            _assert_no_video_padding(mux_command)
            await _run_command(
                mux_command,
                context,
                cwd=work,
                timeout_seconds=3600,
            )
            rendered_duration = await _probe_duration(ffprobe, output, context, cwd=work)
            _validate_rendered_duration(
                rendered_duration,
                expected_duration=duration,
                frame_rate=profile.frame_rate,
            )
            if ai_disclosure is not None:
                await _verify_ai_disclosure(ffprobe, output, context, cwd=work)
            data = output.read_bytes()

        artifact = self.storage.publish(
            context,
            ProviderArtifact("video", "final.mp4", "video/mp4", data),
        )
        summary: dict[str, Any] = {
            "provider": "ffmpeg-edl-render",
            "duration_seconds": round(rendered_duration, 3),
            "width": profile.width,
            "height": profile.height,
            "frame_rate": profile.frame_rate,
            "shots": len(structural_shots),
            "assets": len(selected_artifact_ids),
            "edl_validated": True,
            "video_padding_seconds": 0,
        }
        if ai_disclosure is not None:
            summary["ai_generated"] = True
            summary["ai_disclosure_metadata"] = ai_disclosure
        return StepResult(
            artifacts=(artifact,),
            output_summary=summary,
        )


def _full_ai_disclosure(
    context: StepContext, candidate_manifest: Mapping[str, Any]
) -> str | None:
    if candidate_manifest.get("operation") != "media.generate":
        return None
    snapshot = context.input_snapshot.to_dict()
    if (
        snapshot.get("visual_source_mode") != "generated_only"
        or snapshot.get("ai_disclosure") is not True
    ):
        raise PermanentStepError(
            "render.edl refuses generated media without the immutable AI disclosure flag"
        )
    return _AI_DISCLOSURE


def _ai_disclosure_metadata_args(disclosure: str | None) -> tuple[str, ...]:
    if disclosure is None:
        return ()
    if disclosure != _AI_DISCLOSURE:
        raise ValueError("AI disclosure metadata value is not canonical")
    return (
        "-metadata",
        f"comment={disclosure}",
        "-metadata",
        f"description={disclosure}",
    )


async def _verify_ai_disclosure(
    ffprobe: str,
    output: Path,
    context: StepContext,
    *,
    cwd: Path,
) -> None:
    payload = await _run_command(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags=comment,description",
            "-of",
            "json",
            output.name,
        ),
        context,
        cwd=cwd,
        timeout_seconds=60,
    )
    try:
        value = json.loads(payload)
        tags = value["format"]["tags"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentStepError(
            "rendered Full-AI video lacks readable disclosure metadata"
        ) from exc
    if not isinstance(tags, Mapping) or not any(
        str(tags.get(name) or "") == _AI_DISCLOSURE
        for name in ("comment", "description")
    ):
        raise PermanentStepError(
            "rendered Full-AI video does not contain the required AI disclosure"
        )


def _edl_segment_command(
    ffmpeg: str,
    source: Path,
    output: Path,
    *,
    media_kind: str,
    duration: float,
    source_start_seconds: float,
    width: int,
    height: int,
    frame_rate: int,
    layout: str,
    media_fit: str,
    background_color: str,
) -> tuple[str, ...]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("EDL segment duration must be positive and finite")
    if media_kind not in {"image", "video"}:
        raise ValueError("EDL segment media_kind must be image or video")
    input_args = (
        ("-loop", "1", "-i", source.name)
        if media_kind == "image"
        else ("-ss", f"{source_start_seconds:.6f}", "-i", source.name)
    )
    video_filter = _media_filter(
        width,
        height,
        frame_rate,
        layout=layout,
        media_fit=media_fit,
        background_color=background_color,
    )
    video_filter += f",trim=duration={duration:.9f},setpts=PTS-STARTPTS"
    return (
        ffmpeg,
        "-y",
        "-v",
        "error",
        *input_args,
        "-t",
        f"{duration:.9f}",
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


def _assert_no_video_padding(command: Sequence[str]) -> None:
    rendered = " ".join(command).casefold()
    if "tpad" in rendered or "stop_mode=clone" in rendered:
        raise PermanentStepError("render.edl command attempted forbidden video padding")


def _validate_rendered_duration(
    rendered_duration: float,
    *,
    expected_duration: float,
    frame_rate: int,
) -> None:
    if frame_rate <= 0:
        raise ValueError("render frame rate must be positive")
    # One output frame is the M2 acceptance boundary. The epsilon absorbs only
    # floating-point comparison noise; it is not an extra container time budget.
    tolerance = 1 / frame_rate + 1e-6
    if (
        not math.isfinite(rendered_duration)
        or not math.isfinite(expected_duration)
        or abs(rendered_duration - expected_duration) > tolerance
    ):
        raise PermanentStepError("rendered video failed the frozen EDL duration gate")


def _build_timed_ass(
    timeline: Mapping[str, Any],
    *,
    width: int,
    height: int,
    layout: str,
    subtitle_enabled: bool,
    subtitle_position: str,
    subtitle_size: str,
    max_lines: int,
) -> str:
    duration = float(timeline["duration_seconds"])
    base = _build_ass(
        {"title": str(timeline.get("title", "")), "narration": ""},
        duration,
        width=width,
        height=height,
        layout=layout,
        subtitle_enabled=False,
        subtitle_position=subtitle_position,
        subtitle_size=subtitle_size,
        max_lines=max_lines,
    )
    if not subtitle_enabled:
        return base
    raw = timeline.get("subtitle_cues")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return base
    lines: list[str] = []
    for value in raw:
        if not isinstance(value, Mapping):
            continue
        text = str(value.get("text", "")).strip()
        start = _number(value.get("start_seconds"))
        end = _number(value.get("end_seconds"))
        if not text or start is None or end is None or end <= start:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{_ass_text(text)}"
        )
    return base + "\n".join(lines) + ("\n" if lines else "")


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    try:
        return next(artifact for artifact in context.input_artifacts if artifact.kind == kind)
    except StopIteration as exc:
        raise PermanentStepError(f"required {kind} artifact is unavailable") from exc


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
