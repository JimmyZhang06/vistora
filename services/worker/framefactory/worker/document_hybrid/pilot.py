from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAXIMUM_PDF_BYTES = 200 * 1024 * 1024
MAXIMUM_PDF_PAGES = 100
MAXIMUM_STORYBOARD_BYTES = 2 * 1024 * 1024


class PipelineError(RuntimeError):
    """Raised when the local pilot cannot safely complete."""


@dataclass(frozen=True, slots=True)
class Tools:
    pdfinfo: Path
    pdftoppm: Path
    ffmpeg: Path
    ffprobe: Path
    powershell: Path


@dataclass(frozen=True, slots=True)
class Scene:
    scene_id: str
    page: int
    narration: str
    crop: tuple[float, float, float, float]
    layout: str
    background_policy: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run(
    command: Sequence[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"command timed out after {timeout_seconds}s: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "")[-4000:]
        raise PipelineError(f"command failed ({exc.returncode}): {command[0]}\n{tail}") from exc


def _resolve_tool(explicit: str | None, *names: str) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise PipelineError(f"configured executable does not exist: {path}")
        return path
    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    raise PipelineError(f"required executable is unavailable: {', '.join(names)}")


def _resolve_tools(arguments: argparse.Namespace) -> Tools:
    return Tools(
        pdfinfo=_resolve_tool(arguments.pdfinfo, "pdfinfo.exe", "pdfinfo"),
        pdftoppm=_resolve_tool(arguments.pdftoppm, "pdftoppm.exe", "pdftoppm"),
        ffmpeg=_resolve_tool(arguments.ffmpeg, "ffmpeg.exe", "ffmpeg"),
        ffprobe=_resolve_tool(arguments.ffprobe, "ffprobe.exe", "ffprobe"),
        powershell=_resolve_tool(arguments.powershell, "pwsh.exe", "pwsh", "powershell.exe", "powershell"),
    )


def _pdf_metadata(pdf: Path, tool: Path) -> dict[str, Any]:
    completed = _run([str(tool), str(pdf)], timeout_seconds=30)
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip().lower().replace(" ", "_")] = value.strip()
    try:
        pages = int(values["pages"])
    except (KeyError, ValueError) as exc:
        raise PipelineError("pdfinfo did not return a valid page count") from exc
    return {
        "pages": pages,
        "encrypted": values.get("encrypted", "unknown"),
        "javascript": values.get("javascript", "unknown"),
        "page_size": values.get("page_size", "unknown"),
        "pdf_version": values.get("pdf_version", "unknown"),
    }


def _load_storyboard(path: Path, *, page_count: int) -> tuple[str, list[Scene]]:
    if path.stat().st_size > MAXIMUM_STORYBOARD_BYTES:
        raise PipelineError("storyboard exceeds the 2 MiB safety limit")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != "1.0.0":
        raise PipelineError("storyboard must be a schema_version 1.0.0 object")
    title = raw.get("title")
    scenes = raw.get("scenes")
    if not isinstance(title, str) or not title.strip():
        raise PipelineError("storyboard title is required")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 30:
        raise PipelineError("storyboard must contain between 1 and 30 scenes")
    parsed: list[Scene] = []
    seen: set[str] = set()
    for index, item in enumerate(scenes, 1):
        if not isinstance(item, Mapping):
            raise PipelineError(f"scene {index} must be an object")
        scene_id = item.get("id")
        page = item.get("page")
        narration = item.get("narration")
        crop = item.get("crop", [0.04, 0.04, 0.92, 0.88])
        layout = item.get("layout", "evidence_card")
        background_policy = item.get("background_policy", "procedural")
        if not isinstance(scene_id, str) or not re.fullmatch(r"scene-[0-9]{3}", scene_id):
            raise PipelineError(f"scene {index} has an invalid id")
        if scene_id in seen:
            raise PipelineError(f"duplicate scene id: {scene_id}")
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= page_count:
            raise PipelineError(f"{scene_id} references page outside 1..{page_count}")
        if not isinstance(narration, str) or not narration.strip() or len(narration) > 500:
            raise PipelineError(f"{scene_id} narration must contain 1..500 characters")
        if layout not in {"hero", "evidence_card", "data_focus", "split_explain"}:
            raise PipelineError(f"{scene_id} has unsupported layout: {layout}")
        if background_policy not in {"procedural", "agnes_preferred"}:
            raise PipelineError(f"{scene_id} has unsupported background policy")
        if (
            not isinstance(crop, list)
            or len(crop) != 4
            or any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in crop)
        ):
            raise PipelineError(f"{scene_id} crop must be four normalized numbers")
        x, y, width, height = (float(value) for value in crop)
        if min(x, y, width, height) < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            raise PipelineError(f"{scene_id} crop must stay inside the source page")
        seen.add(scene_id)
        parsed.append(
            Scene(
                scene_id=scene_id,
                page=page,
                narration=narration.strip(),
                crop=(x, y, width, height),
                layout=layout,
                background_policy=background_policy,
            )
        )
    return title.strip(), parsed


def _render_page(pdf: Path, output: Path, page: int, tool: Path) -> None:
    prefix = output.with_suffix("")
    _run(
        [
            str(tool),
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            "-png",
            "-r",
            "170",
            str(pdf),
            str(prefix),
        ],
        timeout_seconds=120,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError(f"page renderer did not create {output.name}")


_SAPI_SCRIPT = r'''param(
  [Parameter(Mandatory=$true)][string]$TextPath,
  [Parameter(Mandatory=$true)][string]$OutputPath,
  [Parameter(Mandatory=$true)][string]$VoiceName
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = [IO.File]::ReadAllText($TextPath, [Text.Encoding]::UTF8)
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SelectVoice($VoiceName)
$speaker.Rate = 0
$speaker.Volume = 100
$speaker.SetOutputToWaveFile($OutputPath)
$speaker.Speak($text)
$speaker.Dispose()
'''


def _synthesize_sapi(
    narration: str,
    *,
    voice: str,
    output: Path,
    work: Path,
    powershell: Path,
) -> None:
    text_path = work / "narration.txt"
    script_path = work / "synthesize.ps1"
    text_path.write_text(narration, encoding="utf-8")
    script_path.write_text(_SAPI_SCRIPT, encoding="utf-8")
    _run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            "-TextPath",
            str(text_path),
            "-OutputPath",
            str(output),
            "-VoiceName",
            voice,
        ],
        timeout_seconds=120,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError("Windows SAPI did not produce narration audio")


def _probe_json(path: Path, ffprobe: Path) -> dict[str, Any]:
    completed = _run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=60,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise PipelineError("ffprobe returned an invalid object")
    return value


def _duration_seconds(path: Path, ffprobe: Path) -> float:
    probe = _probe_json(path, ffprobe)
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"ffprobe did not return duration for {path.name}") from exc
    if duration <= 0:
        raise PipelineError(f"invalid duration for {path.name}")
    return duration


def _ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{fraction:03d}"


def _escape_ass(value: str) -> str:
    return value.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _subtitle_chunks(value: str, maximum: int = 22) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in value.strip():
        current += character
        if len(current) >= maximum or character in "。！？；：，":
            if current.strip():
                chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    return chunks or [value.strip()]


def _write_scene_ass(path: Path, *, narration: str, duration: float, page: int) -> None:
    chunks = _subtitle_chunks(narration)
    usable = max(0.5, duration - 0.15)
    slice_seconds = usable / len(chunks)
    source_text = _escape_ass(f"来源文件 · 第 {page} 页")
    dialogues = [
        f"Dialogue: 0,0:00:00.00,{_ass_timestamp(duration)},Source,,0,0,0,,{source_text}"
    ]
    for index, chunk in enumerate(chunks):
        start = index * slice_seconds
        end = duration if index == len(chunks) - 1 else (index + 1) * slice_seconds
        dialogues.append(
            f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},Subtitle,,0,0,0,,"
            f"{_escape_ass(chunk)}"
        )
    content = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,Microsoft YaHei,31,&H00FFFFFF,&H000000FF,&HCC071427,&H99071427,0,0,0,0,100,100,0,0,1,2.2,0,2,80,80,32,1
Style: Source,Microsoft YaHei,18,&H00E8F1FF,&H000000FF,&H80071427,&H60071427,0,0,0,0,100,100,0,0,1,1.5,0,7,40,40,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(dialogues) + "\n"
    path.write_text(content, encoding="utf-8-sig")


def _layout_dimensions(layout: str) -> tuple[int, int, int]:
    if layout == "data_focus":
        return 1170, 560, -12
    if layout == "hero":
        return 890, 540, -6
    if layout == "split_explain":
        return 1060, 535, -10
    return 1040, 535, -10


def _render_scene(
    *,
    scene: Scene,
    page_png: Path,
    audio: Path,
    duration: float,
    output: Path,
    work: Path,
    ffmpeg: Path,
) -> None:
    ass_path = work / "scene.ass"
    _write_scene_ass(ass_path, narration=scene.narration, duration=duration, page=scene.page)
    x, y, width, height = scene.crop
    panel_width, panel_height, y_offset = _layout_dimensions(scene.layout)
    crop = f"crop=iw*{width:.6f}:ih*{height:.6f}:iw*{x:.6f}:ih*{y:.6f}"
    filter_complex = (
        "[0:v]format=rgba[base];"
        "[1:v]format=rgba,colorchannelmixer=aa=0.24,gblur=sigma=64[glow];"
        "[base][glow]overlay=x='W-w-40+45*sin(t*0.32)':"
        "y='-120+55*cos(t*0.27)':eval=frame:shortest=1[bg];"
        f"[2:v]{crop},"
        f"scale={panel_width}:{panel_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=iw+24:ih+24:12:12:color=0xF7FAFF,setsar=1[doc];"
        f"[bg][doc]overlay=x='(W-w)/2':y='(H-h)/2{y_offset:+d}':shortest=1[comp];"
        "[comp]subtitles=scene.ass[v];"
        "[3:a]apad=pad_dur=0.35[a]"
    )
    _run(
        [
            str(ffmpeg),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x071427:s=1280x720:r=30:d={duration:.3f}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x2E6BFF:s=520x520:r=30:d={duration:.3f}",
            "-loop",
            "1",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(page_png),
            "-i",
            str(audio),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(output),
        ],
        timeout_seconds=600,
        cwd=work,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise PipelineError(f"renderer did not produce {output.name}")


def _write_srt(path: Path, scenes: Sequence[tuple[Scene, float]]) -> None:
    lines: list[str] = []
    cursor = 0.0
    cue = 1
    for scene, duration in scenes:
        chunks = _subtitle_chunks(scene.narration)
        slice_seconds = duration / len(chunks)
        for index, chunk in enumerate(chunks):
            start = cursor + index * slice_seconds
            end = cursor + (duration if index == len(chunks) - 1 else (index + 1) * slice_seconds)
            lines.extend([str(cue), f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}", chunk, ""])
            cue += 1
        cursor += duration
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def _concat_scenes(scene_paths: Sequence[Path], *, output: Path, work: Path, ffmpeg: Path) -> None:
    concat = work / "concat.txt"
    concat.write_text(
        "\n".join(f"file '{path.name}'" for path in scene_paths) + "\n",
        encoding="utf-8",
    )
    _run(
        [
            str(ffmpeg),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout_seconds=300,
        cwd=work,
    )


def _render_preview(video: Path, output: Path, ffmpeg: Path) -> None:
    _run(
        [
            str(ffmpeg),
            "-y",
            "-ss",
            "2",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale=960:-2",
            str(output),
        ],
        timeout_seconds=60,
    )


def _artifact(path: Path, media_type: str) -> dict[str, Any]:
    return {
        "filename": path.name,
        "media_type": media_type,
        "byte_size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def execute_pilot(
    *,
    input_pdf: Path,
    storyboard_path: Path,
    pipeline_path: Path,
    output_dir: Path,
    tools: Tools,
    voice: str,
) -> dict[str, Any]:
    input_pdf = input_pdf.resolve()
    storyboard_path = storyboard_path.resolve()
    pipeline_path = pipeline_path.resolve()
    output_dir = output_dir.resolve()
    if not input_pdf.is_file() or input_pdf.suffix.lower() != ".pdf":
        raise PipelineError("input must be an existing PDF file")
    if input_pdf.stat().st_size > MAXIMUM_PDF_BYTES:
        raise PipelineError("input PDF exceeds the 200 MiB safety limit")
    if not storyboard_path.is_file() or not pipeline_path.is_file():
        raise PipelineError("storyboard and pipeline files must exist")
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    if pipeline.get("slug") != "document-hybrid-production":
        raise PipelineError("pipeline slug must be document-hybrid-production")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PipelineError("output directory must be empty to prevent accidental overwrite")
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = _pdf_metadata(input_pdf, tools.pdfinfo)
    if not 1 <= int(metadata["pages"]) <= MAXIMUM_PDF_PAGES:
        raise PipelineError("input PDF must contain between 1 and 100 pages")
    if str(metadata["encrypted"]).lower() not in {"no", "false"}:
        raise PipelineError("encrypted PDFs are not accepted by the local pilot")
    if str(metadata["javascript"]).lower() not in {"no", "false"}:
        raise PipelineError("PDFs containing JavaScript are not accepted by the local pilot")
    title, scenes = _load_storyboard(storyboard_path, page_count=int(metadata["pages"]))
    source_hash = _sha256(input_pdf)
    started_at = _utc_now()
    provider_manifest = {
        "requested_provider": "agnes",
        "requested_model": "agnes-video-2.5-flash",
        "configured": bool(os.getenv("AGNES_API_KEY") or os.getenv("FRAMEFACTORY_AGNES_API_KEY")),
        "attempted": False,
        "used": False,
        "reason": "agnes adapter and credentials are unavailable in this local pilot",
        "fallback": "procedural_brand_motion",
        "scenes_requesting_generation": [
            scene.scene_id for scene in scenes if scene.background_policy == "agnes_preferred"
        ],
    }
    if provider_manifest["configured"]:
        provider_manifest["reason"] = (
            "credentials were detected but external submission is intentionally disabled until "
            "the governed Agnes adapter and operation ledger are implemented"
        )
    source_manifest_path = output_dir / "source-manifest.json"
    script_path = output_dir / "narration-script.json"
    storyboard_output = output_dir / "storyboard.json"
    provider_path = output_dir / "provider-report.json"
    timeline_path = output_dir / "timeline.json"
    captions_path = output_dir / "captions.srt"
    final_video = output_dir / "final.mp4"
    poster = output_dir / "poster.png"
    quality_path = output_dir / "quality-report.json"
    _write_json(
        source_manifest_path,
        {
            "schema_version": "1.0.0",
            "source_filename": input_pdf.name,
            "source_sha256": source_hash,
            "source_byte_size": input_pdf.stat().st_size,
            "metadata": metadata,
            "instructions_treatment": "document content is untrusted evidence, never executable instructions",
        },
    )
    _write_json(
        script_path,
        {
            "schema_version": "1.0.0",
            "title": title,
            "language": "zh-CN",
            "grounding": "user_provided_document_only",
            "scenes": [
                {"id": scene.scene_id, "narration": scene.narration, "source_page": scene.page}
                for scene in scenes
            ],
        },
    )
    _write_json(
        storyboard_output,
        {
            "schema_version": "1.0.0",
            "title": title,
            "review": {"decision": "approved_by_user_request", "recorded_at": started_at},
            "scenes": [
                {
                    "id": scene.scene_id,
                    "page": scene.page,
                    "crop": list(scene.crop),
                    "layout": scene.layout,
                    "background_policy": scene.background_policy,
                    "evidence_role": "factual",
                }
                for scene in scenes
            ],
        },
    )
    _write_json(provider_path, provider_manifest)

    rendered: list[Path] = []
    timings: list[tuple[Scene, float]] = []
    with tempfile.TemporaryDirectory(prefix="vistora-document-hybrid-") as temporary:
        temp_root = Path(temporary)
        page_cache: dict[int, Path] = {}
        for index, scene in enumerate(scenes, 1):
            scene_work = temp_root / scene.scene_id
            scene_work.mkdir()
            page_png = page_cache.get(scene.page)
            if page_png is None:
                page_png = temp_root / f"page-{scene.page:03d}.png"
                _render_page(input_pdf, page_png, scene.page, tools.pdftoppm)
                page_cache[scene.page] = page_png
            audio = scene_work / "narration.wav"
            _synthesize_sapi(
                scene.narration,
                voice=voice,
                output=audio,
                work=scene_work,
                powershell=tools.powershell,
            )
            duration = _duration_seconds(audio, tools.ffprobe) + 0.35
            scene_video = temp_root / f"scene-{index:03d}.mp4"
            _render_scene(
                scene=scene,
                page_png=page_png,
                audio=audio,
                duration=duration,
                output=scene_video,
                work=scene_work,
                ffmpeg=tools.ffmpeg,
            )
            rendered.append(scene_video)
            timings.append((scene, _duration_seconds(scene_video, tools.ffprobe)))
        _concat_scenes(rendered, output=final_video, work=temp_root, ffmpeg=tools.ffmpeg)

    _write_srt(captions_path, timings)
    cursor = 0.0
    timeline_scenes: list[dict[str, Any]] = []
    for scene, duration in timings:
        timeline_scenes.append(
            {
                "id": scene.scene_id,
                "start_seconds": round(cursor, 3),
                "end_seconds": round(cursor + duration, 3),
                "duration_seconds": round(duration, 3),
                "layers": [
                    {"role": "background", "source_kind": "procedural_brand_motion"},
                    {
                        "role": "evidence",
                        "source_kind": "document_region",
                        "source_page": scene.page,
                        "crop": list(scene.crop),
                        "source_sha256": source_hash,
                    },
                    {"role": "subtitle", "source_kind": "narration"},
                ],
            }
        )
        cursor += duration
    _write_json(
        timeline_path,
        {
            "schema_version": "1.0.0",
            "operation": "document.timeline.align",
            "duration_seconds": round(cursor, 3),
            "canvas": {"width": 1280, "height": 720, "frame_rate": 30},
            "scenes": timeline_scenes,
        },
    )
    probe = _probe_json(final_video, tools.ffprobe)
    streams = probe.get("streams", [])
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    failures: list[str] = []
    warnings: list[str] = []
    if not isinstance(video_stream, Mapping):
        failures.append("missing_video_stream")
    elif (video_stream.get("width"), video_stream.get("height")) != (1280, 720):
        failures.append("unexpected_video_dimensions")
    if not isinstance(audio_stream, Mapping):
        failures.append("missing_audio_stream")
    actual_duration = _duration_seconds(final_video, tools.ffprobe)
    if abs(actual_duration - cursor) > 1.0:
        failures.append("timeline_duration_mismatch")
    if not provider_manifest["used"]:
        warnings.append("agnes_unavailable_procedural_fallback_used")
    warnings.append("automated_document_legibility_scoring_not_implemented")
    _render_preview(final_video, poster, tools.ffmpeg)
    quality = {
        "schema_version": "1.0.0",
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "warnings": warnings,
        "duration_seconds": round(actual_duration, 3),
        "expected_duration_seconds": round(cursor, 3),
        "video": {
            "codec": video_stream.get("codec_name") if isinstance(video_stream, Mapping) else None,
            "width": video_stream.get("width") if isinstance(video_stream, Mapping) else None,
            "height": video_stream.get("height") if isinstance(video_stream, Mapping) else None,
        },
        "audio": {
            "codec": audio_stream.get("codec_name") if isinstance(audio_stream, Mapping) else None,
            "sample_rate": audio_stream.get("sample_rate") if isinstance(audio_stream, Mapping) else None,
        },
        "source_sha256": source_hash,
        "provider_fallback": not provider_manifest["used"],
    }
    _write_json(quality_path, quality)
    if failures:
        raise PipelineError(f"quality checks failed: {', '.join(failures)}")

    artifacts = [
        _artifact(source_manifest_path, "application/json"),
        _artifact(script_path, "application/json"),
        _artifact(storyboard_output, "application/json"),
        _artifact(provider_path, "application/json"),
        _artifact(timeline_path, "application/json"),
        _artifact(captions_path, "application/x-subrip"),
        _artifact(final_video, "video/mp4"),
        _artifact(poster, "image/png"),
        _artifact(quality_path, "application/json"),
    ]
    run_manifest = {
        "schema_version": "1.0.0",
        "execution_kind": "local_pilot",
        "pipeline": {
            "id": pipeline.get("id"),
            "slug": pipeline.get("slug"),
            "version": pipeline.get("version"),
            "content_hash": pipeline.get("content_hash"),
        },
        "status": "succeeded",
        "degraded": not provider_manifest["used"],
        "started_at": started_at,
        "completed_at": _utc_now(),
        "source_sha256": source_hash,
        "artifacts": artifacts,
    }
    run_path = output_dir / "run.json"
    _write_json(run_path, run_manifest)
    run_manifest["run_manifest"] = _artifact(run_path, "application/json")
    return run_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="framefactory-document-hybrid-pilot")
    parser.add_argument("--input-pdf", required=True)
    parser.add_argument("--storyboard", required=True)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--voice", default="Microsoft Huihui Desktop")
    parser.add_argument("--pdfinfo")
    parser.add_argument("--pdftoppm")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--powershell")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = execute_pilot(
            input_pdf=Path(arguments.input_pdf),
            storyboard_path=Path(arguments.storyboard),
            pipeline_path=Path(arguments.pipeline),
            output_dir=Path(arguments.output_dir),
            tools=_resolve_tools(arguments),
            voice=arguments.voice,
        )
    except (OSError, ValueError, json.JSONDecodeError, PipelineError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
