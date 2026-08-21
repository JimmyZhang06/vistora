"""Local media stages and explicitly configured analysis-provider boundary."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import mimetypes
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Any, Protocol

from framefactory.worker.asset_pipeline import (
    AssetPipelineError,
    AssetStage,
    AssetWork,
)


class MalwareScanner(Protocol):
    def scan(self, path: Path) -> Mapping[str, Any]: ...


class VisionAnalyzer(Protocol):
    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class AudioTranscriber(Protocol):
    def transcribe(self, audio: Path) -> Mapping[str, Any]: ...


class OpenAICompatibleAudioTranscriber:
    """Word-timestamp ASR adapter for OpenAI-compatible transcription APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 600.0,
        response_format: str = "verbose_json",
        timestamp_mode: str = "word",
    ) -> None:
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.response_format = response_format
        self.timestamp_mode = timestamp_mode

    def transcribe(self, audio: Path) -> Mapping[str, Any]:
        audio_bytes = audio.read_bytes()
        boundary = "framefactory-" + hashlib.sha256(audio_bytes[:65536]).hexdigest()[:24]
        fields = {"model": self.model, "response_format": self.response_format}
        if self.timestamp_mode != "none":
            fields["timestamp_granularities[]"] = self.timestamp_mode
        body = _multipart_body(
            boundary,
            fields=fields,
            file_field="file",
            filename=audio.name,
            media_type="audio/mpeg",
            data=audio_bytes,
        )
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(16_777_217)
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
            raise AssetPipelineError(
                "asr_provider_rejected",
                f"speech transcription provider rejected the request (HTTP {exc.code})",
                retryable=retryable,
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise AssetPipelineError(
                "asr_provider_unavailable",
                "speech transcription provider is temporarily unavailable",
                retryable=True,
            ) from exc
        if len(payload) > 16_777_216:
            raise AssetPipelineError(
                "asr_response_too_large",
                "speech transcription response exceeded the safe limit",
                retryable=False,
            )
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetPipelineError(
                "asr_response_invalid",
                "speech transcription provider returned invalid JSON",
                retryable=False,
            ) from exc
        if not isinstance(value, Mapping) or value.get("error"):
            raise AssetPipelineError(
                "asr_response_invalid",
                "speech transcription provider returned an invalid result",
                retryable=False,
            )
        return _normalize_transcription(value, provider="openai-compatible", model=self.model)


class MediaObjectStore(Protocol):
    def download(self, work: AssetWork, destination: Path) -> None: ...

    def publish_preview(self, work: AssetWork, preview: Path) -> Mapping[str, Any]: ...

    def publish_frame(self, work: AssetWork, frame: Path, ordinal: int) -> str: ...


class ClamAVScanner:
    """Fail-closed ClamAV command adapter; signatures are managed externally."""

    def __init__(self, command: str = "clamscan") -> None:
        self.command = shutil.which(command)

    def scan(self, path: Path) -> Mapping[str, Any]:
        if self.command is None:
            raise AssetPipelineError(
                "malware_scanner_unavailable",
                "configured malware scanner is unavailable",
                retryable=True,
            )
        result = subprocess.run(
            (self.command, "--no-summary", str(path)),
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return {"status": "clean", "engine": "clamav"}
        if result.returncode == 1:
            raise AssetPipelineError(
                "malware_detected",
                "malware scanner rejected the source object",
                retryable=False,
            )
        raise AssetPipelineError(
            "malware_scan_failed",
            "malware scanner could not complete",
            retryable=True,
        )


class SystemMalwareScanner:
    """Use ClamAV when present, otherwise Windows Defender; never skip scanning."""

    def __init__(self, clamav_command: str = "clamscan") -> None:
        self._clamav = shutil.which(clamav_command)
        defender = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
        self._defender = defender if defender.is_file() else None

    def scan(self, path: Path) -> Mapping[str, Any]:
        if self._clamav:
            return ClamAVScanner(self._clamav).scan(path)
        if self._defender:
            result = subprocess.run(
                (str(self._defender), "-Scan", "-ScanType", "3", "-File", str(path)),
                capture_output=True,
                check=False,
                timeout=300,
            )
            if result.returncode == 0:
                return {"status": "clean", "engine": "windows-defender"}
            if result.returncode == 2:
                raise AssetPipelineError(
                    "malware_detected",
                    "malware scanner rejected the source object",
                    retryable=False,
                )
            raise AssetPipelineError(
                "malware_scan_failed",
                "malware scanner could not complete",
                retryable=True,
            )
        raise AssetPipelineError(
            "malware_scanner_unavailable",
            "no supported malware scanner is available",
            retryable=True,
        )


class OpenAICompatibleVisionAnalyzer:
    """Analyze representative frames through a configured multimodal endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": _VISION_PROMPT}]
        for frame in frames[:8]:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
        }
        command = shutil.which("curl")
        if command is None:
            raise AssetPipelineError(
                "vision_transport_unavailable",
                "configured vision provider transport is unavailable",
                retryable=True,
            )
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as stream:
            json.dump(body, stream, ensure_ascii=False)
            request_path = Path(stream.name)
        try:
            last_error: Exception | None = None
            for _attempt in range(3):
                try:
                    result = subprocess.run(
                        (
                            command,
                            "-sS",
                            "--ssl-no-revoke",
                            "--noproxy",
                            "*",
                            "--fail-with-body",
                            "-X",
                            "POST",
                            self.url,
                            "-H",
                            f"Authorization: Bearer {self.api_key}",
                            "-H",
                            "Content-Type: application/json",
                            "--data-binary",
                            f"@{request_path}",
                        ),
                        capture_output=True,
                        check=False,
                        timeout=self.timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise AssetPipelineError(
                        "vision_provider_timeout", "vision provider timed out", retryable=True
                    ) from exc
                if result.returncode != 0:
                    raise AssetPipelineError(
                        "vision_provider_failed",
                        "vision provider request failed",
                        retryable=True,
                    )
                try:
                    envelope = json.loads(result.stdout)
                    if isinstance(envelope, Mapping) and envelope.get("error"):
                        raise AssetPipelineError(
                            "vision_provider_rejected",
                            "vision provider rejected the analysis request",
                            retryable=True,
                        )
                    raw = envelope["choices"][0]["message"]["content"]
                    value = _extract_json_object(raw)
                    return {**dict(value), "technical_context": dict(technical)}
                except AssetPipelineError:
                    raise
                except (
                    IndexError,
                    KeyError,
                    TypeError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as exc:
                    last_error = exc
            raise AssetPipelineError(
                "invalid_visual_result",
                "vision provider repeatedly returned invalid JSON",
                retryable=True,
            ) from last_error
        finally:
            request_path.unlink(missing_ok=True)


class CommandVisionAnalyzer:
    """Authorized provider hook using JSON over stdio, with no downloader."""

    def __init__(self, command: str, *, timeout_seconds: float = 300.0) -> None:
        arguments = tuple(shlex.split(command, posix=False))
        if not arguments:
            raise ValueError("vision analyzer command must not be empty")
        executable = shutil.which(arguments[0])
        self.arguments = ((executable or arguments[0]), *arguments[1:])
        self.available = executable is not None
        self.timeout_seconds = timeout_seconds

    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not self.available:
            raise AssetPipelineError(
                "vision_provider_unavailable",
                "configured vision provider command is unavailable",
                retryable=True,
            )
        request = json.dumps(
            {"schema_version": "1.0", "frames": [str(path) for path in frames], "technical": technical},
            ensure_ascii=False,
        )
        result = subprocess.run(
            self.arguments,
            input=request,
            capture_output=True,
            check=False,
            text=True,
            timeout=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise AssetPipelineError(
                "vision_provider_failed",
                "vision provider command failed",
                retryable=True,
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssetPipelineError(
                "invalid_visual_result",
                "vision provider returned invalid JSON",
                retryable=False,
            ) from exc
        if not isinstance(value, Mapping):
            raise AssetPipelineError(
                "invalid_visual_result",
                "vision provider result must be an object",
                retryable=False,
            )
        return value


class S3MediaObjectStore:
    def __init__(self, client: Any) -> None:
        self.client = client

    def download(self, work: AssetWork, destination: Path) -> None:
        try:
            response = self.client.get_object(Bucket=work.bucket, Key=work.object_key)
            body = response["Body"]
            with destination.open("wb") as stream:
                while block := body.read(1024 * 1024):
                    stream.write(block)
        except Exception as exc:
            raise AssetPipelineError(
                "source_object_unavailable",
                "source object could not be read",
                retryable=True,
            ) from exc

    def publish_preview(self, work: AssetWork, preview: Path) -> Mapping[str, Any]:
        digest = _sha256(preview)
        media_type = "video/mp4" if preview.suffix.lower() == ".mp4" else "image/jpeg"
        key = (
            f"workspaces/{work.workspace_id}/asset-analysis/{work.asset_id}/"
            f"{work.content_hash}/preview/{digest[:16]}{preview.suffix.lower()}"
        )
        try:
            with preview.open("rb") as stream:
                self.client.put_object(
                    Bucket=work.bucket,
                    Key=key,
                    Body=stream,
                    ContentType=media_type,
                    Metadata={"sha256": digest, "workspace-id": work.workspace_id},
                    IfNoneMatch="*",
                )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if status != 412:
                raise AssetPipelineError(
                    "preview_publish_failed",
                    "asset preview could not be persisted",
                    retryable=True,
                ) from exc
            head = self.client.head_object(Bucket=work.bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") != digest:
                raise AssetPipelineError(
                    "preview_object_conflict",
                    "immutable preview key contains different bytes",
                    retryable=False,
                ) from exc
        return {
            "object_key": key,
            "content_hash": digest,
            "media_type": media_type,
            "byte_size": preview.stat().st_size,
        }

    def publish_frame(self, work: AssetWork, frame: Path, ordinal: int) -> str:
        digest = _sha256(frame)
        key = (
            f"workspaces/{work.workspace_id}/asset-analysis/{work.asset_id}/"
            f"{work.content_hash}/frames/{ordinal:03d}-{digest[:16]}.jpg"
        )
        try:
            with frame.open("rb") as stream:
                self.client.put_object(
                    Bucket=work.bucket,
                    Key=key,
                    Body=stream,
                    ContentType="image/jpeg",
                    Metadata={"sha256": digest, "workspace-id": work.workspace_id},
                    IfNoneMatch="*",
                )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 412:
                raise AssetPipelineError(
                    "frame_publish_failed",
                    "representative frame could not be persisted",
                    retryable=True,
                ) from exc
            head = self.client.head_object(Bucket=work.bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") != digest:
                raise AssetPipelineError(
                    "frame_object_conflict",
                    "immutable frame key contains different bytes",
                    retryable=False,
                ) from exc
        return key


class LocalAssetStageProcessor:
    """stdlib/FFmpeg implementation; scanner and vision are mandatory ports."""

    def __init__(
        self,
        store: MediaObjectStore,
        scanner: MalwareScanner,
        vision: VisionAnalyzer,
        transcriber: AudioTranscriber | None = None,
        *,
        ffprobe_command: str = "ffprobe",
        ffmpeg_command: str = "ffmpeg",
    ) -> None:
        self.store = store
        self.scanner = scanner
        self.vision = vision
        self.transcriber = transcriber
        self.ffprobe = ffprobe_command
        self.ffmpeg = ffmpeg_command
        self._directories: dict[str, tempfile.TemporaryDirectory[str]] = {}

    def close(self) -> None:
        for directory in self._directories.values():
            directory.cleanup()
        self._directories.clear()

    def run(
        self,
        stage: AssetStage,
        work: AssetWork,
        checkpoints: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]:
        if cancelled():
            raise AssetPipelineError("batch_cancelled", "batch cancellation requested", retryable=False)
        source = self._source(work)
        if stage is AssetStage.FILE_DETECTION:
            return self._detect(source, work.media_type)
        if stage is AssetStage.MALWARE_SCAN:
            return self.scanner.scan(source)
        if stage is AssetStage.FFPROBE:
            return self._probe(source)
        if stage is AssetStage.FINGERPRINT:
            digest = _sha256(source, cancelled=cancelled)
            if digest != work.content_hash:
                raise AssetPipelineError(
                    "content_hash_mismatch",
                    "source bytes differ from the durable asset file record",
                    retryable=False,
                )
            return {"sha256": digest, "algorithm": "sha256"}
        if stage is AssetStage.PREVIEW:
            technical = _mapping(checkpoints.get(AssetStage.FFPROBE.value))
            return self._preview(source, work, technical, cancelled)
        if stage is AssetStage.KEYFRAMES:
            technical = _mapping(checkpoints.get(AssetStage.FFPROBE.value))
            return self._keyframes(source, work, technical, cancelled)
        if stage is AssetStage.VISUAL_ANALYSIS:
            frames = self._frame_paths(work, checkpoints)
            return self._analyze_frames(
                frames, _mapping(checkpoints.get(AssetStage.FFPROBE.value))
            )
        if stage is AssetStage.SUBTITLE_EXTRACTION:
            technical = _mapping(checkpoints.get(AssetStage.FFPROBE.value))
            return self._subtitle_extraction(source, technical)
        if stage is AssetStage.AUDIO_TRANSCRIPTION:
            technical = _mapping(checkpoints.get(AssetStage.FFPROBE.value))
            return self._audio_transcription(source, technical, cancelled)
        if stage is AssetStage.TEMPORAL_ALIGNMENT:
            return align_temporal_sources(
                _mapping(checkpoints.get(AssetStage.SUBTITLE_EXTRACTION.value)),
                _mapping(checkpoints.get(AssetStage.AUDIO_TRANSCRIPTION.value)),
            )
        if stage is AssetStage.SHOT_SEGMENTATION:
            technical = _mapping(checkpoints.get("ffprobe"))
            visual = _mapping(checkpoints.get("visual_analysis"))
            return {"segments": self._segments(source, work, technical, visual)}
        if stage is AssetStage.NORMALIZE_TAGS:
            return {"schema_version": "1.0", "normalizer": "worker"}
        if stage is AssetStage.RIGHTS_GATE:
            return {
                "copyright_status": work.copyright_status,
                "source_licenses": [source.license for source in work.sources if source.license],
            }
        raise AssetPipelineError("unknown_stage", f"unsupported stage: {stage}", retryable=False)

    def _source(self, work: AssetWork) -> Path:
        directory = self._directories.get(work.item_id)
        if directory is None:
            directory = tempfile.TemporaryDirectory(prefix="framefactory-asset-job-")
            self._directories[work.item_id] = directory
        suffix = Path(work.original_filename).suffix.lower()[:12]
        path = Path(directory.name) / f"source{suffix}"
        if not path.exists():
            self.store.download(work, path)
        return path

    @staticmethod
    def _detect(path: Path, declared_media_type: str) -> Mapping[str, Any]:
        with path.open("rb") as stream:
            header = stream.read(64)
        detected = _signature_media_type(header)
        declared = declared_media_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_type(path.name)[0]
        if detected is None or not detected.startswith(("image/", "video/")):
            raise AssetPipelineError("unsupported_media", "file signature is not supported", retryable=False)
        if not _media_types_compatible(detected, declared):
            raise AssetPipelineError(
                "media_type_mismatch",
                "declared media type conflicts with file signature",
                retryable=False,
            )
        return {"media_type": detected, "extension_guess": guessed, "byte_size": path.stat().st_size}

    def _probe(self, path: Path) -> Mapping[str, Any]:
        if shutil.which(self.ffprobe) is None:
            raise AssetPipelineError("ffprobe_unavailable", "ffprobe is unavailable", retryable=True)
        result = subprocess.run(
            (self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)),
            capture_output=True,
            check=False,
            timeout=180,
        )
        if result.returncode != 0:
            raise AssetPipelineError("ffprobe_rejected", "ffprobe rejected the media", retryable=False)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AssetPipelineError("ffprobe_invalid", "ffprobe returned invalid JSON", retryable=False) from exc
        streams = value.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        if video is None:
            raise AssetPipelineError("video_stream_missing", "media has no visual stream", retryable=False)
        duration = value.get("format", {}).get("duration") or video.get("duration")
        duration_ms = round(float(duration) * 1000) if duration else None
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        subtitles = [
            {
                "index": item.get("index"),
                "codec": item.get("codec_name"),
                "language": _mapping(item.get("tags")).get("language"),
            }
            for item in streams
            if item.get("codec_type") == "subtitle"
        ]
        return {
            "format_name": value.get("format", {}).get("format_name"),
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "duration_ms": duration_ms,
            "frame_rate": video.get("avg_frame_rate"),
            "has_audio": audio is not None,
            "audio_codec": audio.get("codec_name") if audio else None,
            "subtitle_streams": subtitles,
        }

    def _preview(
        self,
        source: Path,
        work: AssetWork,
        technical: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]:
        if shutil.which(self.ffmpeg) is None:
            raise AssetPipelineError("ffmpeg_unavailable", "ffmpeg is unavailable", retryable=True)
        if cancelled():
            raise AssetPipelineError(
                "batch_cancelled", "batch cancellation requested", retryable=False
            )
        is_image = work.media_type.startswith("image/")
        preview = source.parent / ("preview.jpg" if is_image else "preview.mp4")
        if is_image:
            command = (
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(1600,iw)':-2",
                "-q:v",
                "3",
                "-map_metadata",
                "-1",
                "-y",
                str(preview),
            )
        else:
            command = (
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                "scale='min(960,iw)':-2",
                "-r",
                "30",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-maxrate",
                "1600k",
                "-bufsize",
                "3200k",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "-1",
                "-y",
                str(preview),
            )
        result = subprocess.run(command, capture_output=True, check=False, timeout=1800)
        if result.returncode != 0 or not preview.is_file() or preview.stat().st_size == 0:
            raise AssetPipelineError(
                "preview_transcode_failed",
                "ffmpeg could not create a browser-compatible preview",
                retryable=False,
            )
        if cancelled():
            raise AssetPipelineError(
                "batch_cancelled", "batch cancellation requested", retryable=False
            )
        descriptor = dict(self.store.publish_preview(work, preview))
        descriptor.update(
            {
                "width": min(960, int(technical.get("width") or 960)),
                "duration_ms": technical.get("duration_ms"),
            }
        )
        return descriptor

    def _keyframes(
        self,
        source: Path,
        work: AssetWork,
        technical: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]:
        if shutil.which(self.ffmpeg) is None:
            raise AssetPipelineError("ffmpeg_unavailable", "ffmpeg is unavailable", retryable=True)
        duration = max(1000, int(technical.get("duration_ms") or 1000))
        is_image = work.media_type.startswith("image/")
        count = 1 if is_image else _representative_frame_count(duration)
        shot_boundaries: list[int] = []
        if not is_image and duration >= 120_000:
            shot_boundaries = self._scene_boundaries(source, duration)
        timestamps = _representative_timestamps(duration, count, shot_boundaries)
        frames: list[dict[str, Any]] = []
        poster: dict[str, Any] | None = None
        directory = source.parent
        poster_ordinal = len(timestamps) // 2
        for ordinal, timestamp_ms in enumerate(timestamps):
            if cancelled():
                raise AssetPipelineError("batch_cancelled", "batch cancellation requested", retryable=False)
            frame = directory / f"frame-{ordinal:03d}.jpg"
            # A still image is a one-frame input. Seeking it before decoding can
            # make ffmpeg consume the only frame without producing an output
            # (observed with ffmpeg 8 on Windows), even when the timestamp is 0.
            # Video inputs still seek before decoding to keep extraction fast.
            seek_args = () if is_image else ("-ss", f"{timestamp_ms / 1000:.3f}")
            result = subprocess.run(
                (self.ffmpeg, "-v", "error", *seek_args, "-i", str(source),
                 "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", "-y", str(frame)),
                capture_output=True,
                check=False,
                timeout=180,
            )
            if result.returncode != 0 or not frame.is_file():
                raise AssetPipelineError("keyframe_failed", "ffmpeg could not extract a frame", retryable=False)
            key = self.store.publish_frame(work, frame, ordinal)
            frames.append({"ordinal": ordinal, "timestamp_ms": timestamp_ms, "object_key": key})
            if ordinal == poster_ordinal:
                poster = {
                    "object_key": key,
                    "content_hash": _sha256(frame),
                    "media_type": "image/jpeg",
                    "byte_size": frame.stat().st_size,
                    "timestamp_ms": timestamp_ms,
                }
        return {
            "frames": frames,
            "poster": poster,
            "shot_boundaries": shot_boundaries,
            "sampling_strategy": "single_image" if is_image else "scene_aware_dynamic",
        }

    def _frame_paths(self, work: AssetWork, checkpoints: Mapping[str, Any]) -> tuple[Path, ...]:
        source = self._source(work)
        raw = _mapping(checkpoints.get("keyframes")).get("frames", [])
        paths = tuple(source.parent / f"frame-{int(item['ordinal']):03d}.jpg" for item in raw)
        if not paths or not all(path.is_file() for path in paths):
            raise AssetPipelineError("keyframes_missing", "local keyframes are unavailable", retryable=True)
        return paths

    def _analyze_frames(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        batches = [frames[index : index + 8] for index in range(0, len(frames), 8)]
        results = [dict(self.vision.analyze(batch, technical)) for batch in batches]
        if len(results) == 1:
            return results[0]
        merged = dict(results[0])
        merged["segments"] = [
            item
            for result in results
            for item in result.get("segments", [])
            if isinstance(item, Mapping)
        ]
        for key in ("people", "locations", "scene_types", "actions", "moods", "keywords"):
            values = [
                result.get(key, [])
                for result in results
                if isinstance(result.get(key, []), Sequence)
                and not isinstance(result.get(key, []), (str, bytes))
            ]
            merged[key] = list(
                dict.fromkeys(
                    str(item)
                    for group in values
                    for item in group
                    if str(item).strip()
                )
            )[:128]
        confidences = [
            float(result["confidence"])
            for result in results
            if isinstance(result.get("confidence"), (int, float))
            and not isinstance(result.get("confidence"), bool)
        ]
        if confidences:
            merged["confidence"] = round(sum(confidences) / len(confidences), 3)
        merged["analysis_batches"] = len(results)
        merged["representative_frames"] = len(frames)
        return merged

    def _subtitle_extraction(
        self,
        source: Path,
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        streams = technical.get("subtitle_streams", [])
        if not isinstance(streams, Sequence) or not streams:
            return {"status": "unavailable", "reason": "subtitle_stream_missing", "cues": []}
        stream = next((item for item in streams if isinstance(item, Mapping)), None)
        if stream is None or not isinstance(stream.get("index"), int):
            return {"status": "unavailable", "reason": "subtitle_stream_invalid", "cues": []}
        subtitle = source.parent / "embedded-subtitles.vtt"
        result = subprocess.run(
            (
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                f"0:{stream['index']}",
                "-f",
                "webvtt",
                "-y",
                str(subtitle),
            ),
            capture_output=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0 or not subtitle.is_file():
            return {
                "status": "unavailable",
                "reason": "subtitle_codec_unsupported",
                "codec": stream.get("codec"),
                "cues": [],
            }
        cues = parse_webvtt(subtitle.read_text(encoding="utf-8-sig", errors="replace"))
        return {
            "status": "available" if cues else "unavailable",
            "reason": None if cues else "subtitle_empty",
            "source": "embedded_subtitle",
            "language": stream.get("language"),
            "codec": stream.get("codec"),
            "cues": cues,
        }

    def _audio_transcription(
        self,
        source: Path,
        technical: Mapping[str, Any],
        cancelled: Callable[[], bool],
    ) -> Mapping[str, Any]:
        if technical.get("has_audio") is not True:
            return {
                "status": "unavailable",
                "reason": "audio_stream_missing",
                "segments": [],
                "words": [],
                "silences": [],
            }
        silences = self._silences(source)
        if self.transcriber is None:
            return {
                "status": "unavailable",
                "reason": "asr_provider_unconfigured",
                "segments": [],
                "words": [],
                "silences": silences,
            }
        if cancelled():
            raise AssetPipelineError("batch_cancelled", "batch cancellation requested", retryable=False)
        for stale in source.parent.glob("speech-*.mp3"):
            stale.unlink(missing_ok=True)
        audio_pattern = source.parent / "speech-%04d.mp3"
        result = subprocess.run(
            (
                self.ffmpeg,
                "-v",
                "error",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "48k",
                "-f",
                "segment",
                "-segment_time",
                "300",
                "-reset_timestamps",
                "1",
                "-map_metadata",
                "-1",
                "-y",
                str(audio_pattern),
            ),
            capture_output=True,
            check=False,
            timeout=1800,
        )
        chunks = tuple(
            path
            for path in sorted(source.parent.glob("speech-*.mp3"))
            if path.stat().st_size > 0
        )
        if result.returncode != 0 or not chunks:
            raise AssetPipelineError(
                "audio_extraction_failed",
                "ffmpeg could not extract speech audio",
                retryable=False,
            )
        duration_ms = int(technical.get("duration_ms") or 0)
        transcriptions: list[tuple[int, int, Mapping[str, Any]]] = []
        for index, chunk in enumerate(chunks):
            if cancelled():
                raise AssetPipelineError(
                    "batch_cancelled", "batch cancellation requested", retryable=False
                )
            start_ms = index * 300_000
            end_ms = min(duration_ms or start_ms + 300_000, start_ms + 300_000)
            transcriptions.append(
                (start_ms, max(start_ms + 1, end_ms), self.transcriber.transcribe(chunk))
            )
        value = _merge_transcription_chunks(transcriptions)
        value["silences"] = silences
        return value

    def _silences(self, source: Path) -> list[dict[str, int]]:
        result = subprocess.run(
            (
                self.ffmpeg,
                "-hide_banner",
                "-i",
                str(source),
                "-af",
                "silencedetect=noise=-35dB:d=0.25",
                "-f",
                "null",
                "-",
            ),
            capture_output=True,
            check=False,
            text=True,
            timeout=900,
        )
        if result.returncode != 0:
            raise AssetPipelineError(
                "silence_detection_failed",
                "ffmpeg silence detection failed",
                retryable=False,
            )
        starts = [float(value) for value in re.findall(r"silence_start: ([0-9.]+)", result.stderr)]
        ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", result.stderr)]
        return [
            {"start_ms": round(start * 1000), "end_ms": round(end * 1000)}
            for start, end in zip(starts, ends, strict=False)
            if end > start
        ]

    def _segments(
        self,
        source: Path,
        work: AssetWork,
        technical: Mapping[str, Any],
        visual: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        duration = max(1, int(technical.get("duration_ms") or 1))
        shot_boundaries = (
            [0, duration]
            if work.media_type.startswith("image/")
            else list(
                _mapping(work.checkpoints.get(AssetStage.KEYFRAMES.value)).get(
                    "shot_boundaries", []
                )
            )
            or self._scene_boundaries(source, duration)
        )
        temporal = _mapping(work.checkpoints.get(AssetStage.TEMPORAL_ALIGNMENT.value))
        boundaries = semantic_cut_boundaries(
            duration,
            shot_boundaries=shot_boundaries,
            cues=temporal.get("cues", []),
            words=temporal.get("words", []),
            silences=temporal.get("silences", []),
        )
        descriptions = visual.get("segments", [])
        frames = _mapping(work.checkpoints.get("keyframes")).get("frames", [])
        result: list[dict[str, Any]] = []
        for ordinal, (start_info, end_info) in enumerate(itertools.pairwise(boundaries)):
            start, end = int(start_info["timestamp_ms"]), int(end_info["timestamp_ms"])
            visual_index = min(
                len(descriptions) - 1,
                max(0, round(((start + end) / 2) / duration * max(0, len(descriptions) - 1))),
            )
            detail = descriptions[visual_index] if descriptions and isinstance(descriptions[visual_index], Mapping) else {}
            frame = min(frames, key=lambda item: abs(int(item["timestamp_ms"]) - start), default={})
            transcript = _transcript_for_range(temporal.get("cues", []), start, end)
            result.append({
                "start_ms": start,
                "end_ms": end,
                "description": detail.get("description") or visual.get("summary") or "unclassified shot",
                "people": detail.get("people", visual.get("people", [])),
                "locations": detail.get("locations", visual.get("locations", [])),
                "keywords": detail.get("keywords", visual.get("keywords", [])),
                "scene_type": detail.get("scene_type"),
                "action": detail.get("action"),
                "era": detail.get("era"),
                "mood": detail.get("mood"),
                "visual_style": detail.get("visual_style"),
                "shot_type": detail.get("shot_type"),
                "confidence": detail.get("confidence", visual.get("confidence")),
                "representative_frame_key": frame.get("object_key"),
                "transcript": transcript,
                "boundary_score": end_info["score"],
                "boundary_reasons": end_info["reasons"],
                "cut_safe": end_info["cut_safe"],
                "semantic_complete": end_info["semantic_complete"],
            })
        return result

    def _scene_boundaries(self, source: Path, duration_ms: int) -> list[int]:
        result = subprocess.run(
            (self.ffmpeg, "-hide_banner", "-i", str(source), "-vf", "select='gt(scene,0.35)',showinfo",
             "-f", "null", "-"),
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise AssetPipelineError("shot_detection_failed", "ffmpeg shot detection failed", retryable=False)
        points = [round(float(value) * 1000) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)]
        return list(dict.fromkeys((0, *(point for point in points if 0 < point < duration_ms), duration_ms)))


def parse_webvtt(value: str) -> list[dict[str, Any]]:
    """Parse text WebVTT into normalized, bounded cues without trusting markup."""

    cues: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\s*\r?\n", value.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        start = _vtt_timestamp_ms(timing[0].strip())
        end = _vtt_timestamp_ms(timing[1].strip().split()[0])
        text = " ".join(lines[timing_index + 1 :])
        text = re.sub(r"<[^>]+>", "", unescape(text)).strip()
        if start is None or end is None or end <= start or not text:
            continue
        cues.append(
            {
                "ordinal": len(cues),
                "start_ms": start,
                "end_ms": end,
                "text": text[:4000],
                "confidence": 1.0,
                "source": "embedded_subtitle",
            }
        )
    return cues[:20_000]


def align_temporal_sources(
    subtitles: Mapping[str, Any],
    transcription: Mapping[str, Any],
) -> dict[str, Any]:
    subtitle_cues = _valid_cues(subtitles.get("cues"), "embedded_subtitle")
    asr_cues = _valid_cues(transcription.get("segments"), "asr")
    words = _valid_words(transcription.get("words"))
    silences = _valid_ranges(transcription.get("silences"))
    transcription_text = str(transcription.get("text") or "").strip()
    if subtitle_cues:
        cues = subtitle_cues
        source = "aligned" if asr_cues else "embedded_subtitle"
        if asr_cues:
            for cue in cues:
                overlap = " ".join(
                    item["text"]
                    for item in asr_cues
                    if item["end_ms"] > cue["start_ms"]
                    and item["start_ms"] < cue["end_ms"]
                )
                cue["confidence"] = round(
                    SequenceMatcher(
                        None,
                        _normalized_text(cue["text"]),
                        _normalized_text(overlap),
                    ).ratio(),
                    3,
                )
    else:
        cues = asr_cues
        source = "asr" if asr_cues or transcription_text else "none"
    return {
        "status": "available" if cues else "unavailable",
        "source": source,
        "language": subtitles.get("language") or transcription.get("language"),
        "provider": transcription.get("provider"),
        "model": transcription.get("model"),
        "full_text": (
            " ".join(cue["text"] for cue in cues)
            or transcription_text
        )[:200_000],
        "cues": cues,
        "words": words,
        "silences": silences,
        "subtitle_status": subtitles.get("status", "unavailable"),
        "asr_status": transcription.get("status", "unavailable"),
        "timing_precision": (
            "word"
            if words
            else "cue"
            if subtitle_cues
            else transcription.get("timing_precision", "none")
        ),
    }


def _representative_frame_count(duration_ms: int) -> int:
    if duration_ms <= 80_000:
        return min(8, max(1, round(duration_ms / 10_000)))
    # Long-form sources need broader visual coverage without unbounded provider cost.
    return min(32, max(8, (duration_ms + 179_999) // 180_000))


def _representative_timestamps(
    duration_ms: int,
    count: int,
    shot_boundaries: Sequence[int],
) -> list[int]:
    if count <= 1:
        return [0]
    interior = sorted({int(item) for item in shot_boundaries if 0 < item < duration_ms})
    result: list[int] = []
    for ordinal in range(count):
        target = round(duration_ms * (ordinal + 0.5) / count)
        if interior:
            boundary = min(interior, key=lambda item: abs(item - target))
            timestamp = min(duration_ms - 1, boundary + 250)
            if timestamp in result:
                timestamp = min(duration_ms - 1, target)
        else:
            timestamp = min(duration_ms - 1, target)
        result.append(max(0, timestamp))
    return result


def semantic_cut_boundaries(
    duration_ms: int,
    *,
    shot_boundaries: Sequence[int],
    cues: Any,
    words: Any,
    silences: Any,
    minimum_segment_ms: int = 900,
    maximum_segment_ms: int = 15_000,
) -> list[dict[str, Any]]:
    """Score cut candidates from vision, language and silence evidence."""

    if duration_ms <= 0:
        return []
    candidates: list[dict[str, Any]] = []

    def add(timestamp: int, score: float, reason: str, *, semantic: bool = False) -> None:
        if 0 < timestamp < duration_ms:
            candidates.append(
                {
                    "timestamp_ms": timestamp,
                    "score": score,
                    "reasons": [reason],
                    "semantic_complete": semantic,
                }
            )

    for point in shot_boundaries:
        add(int(point), 0.58, "shot_boundary")
    for cue in _valid_cues(cues, "temporal"):
        semantic = bool(cue.get("semantic_complete", True))
        add(
            int(cue["end_ms"]),
            0.62 if semantic else 0.2,
            "sentence_end" if semantic else "estimated_transcript_boundary",
            semantic=semantic,
        )
    for silence in _valid_ranges(silences):
        length = silence["end_ms"] - silence["start_ms"]
        add(
            round((silence["start_ms"] + silence["end_ms"]) / 2),
            min(0.62, 0.38 + length / 5000),
            "silence",
        )

    clustered: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda candidate: candidate["timestamp_ms"]):
        if clustered and item["timestamp_ms"] - clustered[-1]["timestamp_ms"] <= 450:
            current = clustered[-1]
            total = current["score"] + item["score"]
            current["timestamp_ms"] = round(
                (current["timestamp_ms"] * current["score"] + item["timestamp_ms"] * item["score"])
                / total
            )
            current["score"] = min(1.0, total)
            current["reasons"] = list(dict.fromkeys((*current["reasons"], *item["reasons"])))
            current["semantic_complete"] = (
                current["semantic_complete"] or item["semantic_complete"]
            )
        else:
            clustered.append(dict(item))

    word_ranges = _valid_words(words)
    for item in clustered:
        timestamp = item["timestamp_ms"]
        inside_word = any(
            word["start_ms"] + 60 < timestamp < word["end_ms"] - 60
            for word in word_ranges
        )
        has_audio_or_language_evidence = bool(
            item["semantic_complete"] or "silence" in item["reasons"]
        )
        item["cut_safe"] = has_audio_or_language_evidence and not inside_word
        if inside_word:
            item["score"] = max(0.0, item["score"] - 0.8)

    selected = [
        {
            "timestamp_ms": 0,
            "score": 1.0,
            "reasons": ["asset_start"],
            "cut_safe": True,
            "semantic_complete": True,
        }
    ]
    while selected[-1]["timestamp_ms"] < duration_ms:
        start = selected[-1]["timestamp_ms"]
        target = min(start + maximum_segment_ms, start + 6000)
        window = [
            item
            for item in clustered
            if start + minimum_segment_ms <= item["timestamp_ms"] <= start + maximum_segment_ms
            and item["cut_safe"]
            and item["score"] >= 0.55
        ]
        if window:
            semantic_window = [item for item in window if item["semantic_complete"]]
            next_item = (
                min(semantic_window, key=lambda item: item["timestamp_ms"])
                if semantic_window
                else max(
                    window,
                    key=lambda item: (
                        item["score"]
                        - abs(item["timestamp_ms"] - target)
                        / maximum_segment_ms
                        * 0.25,
                        item["score"],
                    ),
                )
            )
        elif duration_ms - start <= maximum_segment_ms:
            break
        else:
            guard_target = min(duration_ms, start + maximum_segment_ms)
            safe_guard = _nearest_safe_word_gap(
                guard_target,
                lower=start + minimum_segment_ms,
                upper=guard_target,
                words=word_ranges,
            )
            fallback = [
                item
                for item in clustered
                if start + minimum_segment_ms <= item["timestamp_ms"] <= start + maximum_segment_ms
            ]
            next_item = max(
                fallback,
                key=lambda item: (
                    item["score"]
                    - abs(item["timestamp_ms"] - target) / maximum_segment_ms * 0.2,
                    item["timestamp_ms"],
                ),
                default={
                    "timestamp_ms": safe_guard,
                    "score": 0.25,
                    "reasons": [
                        "maximum_duration_word_gap"
                        if safe_guard != guard_target
                        else "maximum_duration_guard"
                    ],
                    "cut_safe": bool(word_ranges)
                    and not _inside_word(safe_guard, word_ranges),
                    "semantic_complete": False,
                },
            )
        if next_item["timestamp_ms"] <= start:
            break
        selected.append(dict(next_item))
    if selected[-1]["timestamp_ms"] != duration_ms:
        selected.append(
            {
                "timestamp_ms": duration_ms,
                "score": 1.0,
                "reasons": ["asset_end"],
                "cut_safe": True,
                "semantic_complete": True,
            }
        )
    return selected


def _nearest_safe_word_gap(
    target: int,
    *,
    lower: int,
    upper: int,
    words: Sequence[Mapping[str, Any]],
) -> int:
    if not words or not _inside_word(target, words):
        return target
    candidates = {
        int(word["end_ms"])
        for word in words
        if lower <= int(word["end_ms"]) <= upper
        and not _inside_word(int(word["end_ms"]), words)
    }
    return min(candidates, key=lambda value: (abs(target - value), -value), default=target)


def _merge_transcription_chunks(
    chunks: Sequence[tuple[int, int, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Merge bounded provider calls back onto the source-media clock."""

    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    texts: list[str] = []
    provider = ""
    model = ""
    language: Any = None
    provider_timing = False
    for start_ms, end_ms, value in chunks:
        provider = provider or str(value.get("provider") or "")
        model = model or str(value.get("model") or "")
        language = language or value.get("language")
        text = str(value.get("text") or "").strip()
        if text:
            texts.append(text)
        chunk_segments = _valid_cues(value.get("segments"), "asr")
        chunk_words = _valid_words(value.get("words"))
        if chunk_segments or chunk_words:
            provider_timing = True
        for cue in chunk_segments:
            segments.append(
                {
                    **cue,
                    "ordinal": len(segments),
                    "start_ms": start_ms + int(cue["start_ms"]),
                    "end_ms": min(end_ms, start_ms + int(cue["end_ms"])),
                }
            )
        for word in chunk_words:
            words.append(
                {
                    **word,
                    "ordinal": len(words),
                    "start_ms": start_ms + int(word["start_ms"]),
                    "end_ms": min(end_ms, start_ms + int(word["end_ms"])),
                }
            )
        if not chunk_segments and text:
            for cue in _estimated_text_cues(text, start_ms, end_ms):
                segments.append({**cue, "ordinal": len(segments)})
    return {
        "status": "available" if segments else "unavailable",
        "reason": None if provider_timing else "coarse_chunk_timing",
        "provider": provider,
        "model": model,
        "language": language,
        "text": " ".join(texts)[:200_000],
        "segments": segments[:20_000],
        "words": words[:100_000],
        "timing_precision": (
            "word" if words else "segment" if provider_timing else "chunk"
        ),
        "chunk_count": len(chunks),
    }


def _estimated_text_cues(text: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """Localize text-only ASR without presenting estimates as semantic cut proof."""

    cleaned = " ".join(text.split()).strip()
    if not cleaned or end_ms <= start_ms:
        return []
    sentences = [
        value.strip()
        for value in re.split(r"(?<=[。！？.!?])\s*", cleaned)
        if value.strip()
    ]
    if len(sentences) == 1 and len(sentences[0]) > 120:
        value = sentences[0]
        sentences = [value[index : index + 80] for index in range(0, len(value), 80)]
    weights = [max(1, len(_normalized_text(value))) for value in sentences]
    total_weight = sum(weights)
    duration = end_ms - start_ms
    cursor = start_ms
    result: list[dict[str, Any]] = []
    consumed = 0
    for index, (sentence, weight) in enumerate(zip(sentences, weights, strict=True)):
        consumed += weight
        cue_end = (
            end_ms
            if index == len(sentences) - 1
            else start_ms + round(duration * consumed / total_weight)
        )
        cue_end = max(cursor + 1, min(end_ms, cue_end))
        result.append(
            {
                "ordinal": index,
                "start_ms": cursor,
                "end_ms": cue_end,
                "text": sentence[:4000],
                "confidence": None,
                "source": "asr_estimated",
                "semantic_complete": False,
            }
        )
        cursor = cue_end
    return result


def _normalize_transcription(
    value: Mapping[str, Any],
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    words = _valid_words(value.get("words"), seconds=True)
    segments = _valid_cues(value.get("segments"), "asr", seconds=True)
    if not segments and words:
        segments = [
            {
                "ordinal": 0,
                "start_ms": words[0]["start_ms"],
                "end_ms": words[-1]["end_ms"],
                "text": "".join(word["text"] for word in words),
                "confidence": None,
                "source": "asr",
            }
        ]
    return {
        "status": "available" if segments else "unavailable",
        "reason": None if segments else "word_timestamps_missing",
        "provider": provider,
        "model": model,
        "language": value.get("language"),
        "text": str(value.get("text") or "")[:200_000],
        "segments": segments,
        "words": words,
        "timing_precision": "word" if words else "segment" if segments else "text_only",
    }


def _valid_cues(value: Any, source: str, *, seconds: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:20_000]:
        if not isinstance(item, Mapping):
            continue
        multiplier = 1000 if seconds else 1
        try:
            start = round(float(item.get("start_ms", item.get("start"))) * multiplier)
            end = round(float(item.get("end_ms", item.get("end"))) * multiplier)
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if start < 0 or end <= start or not text:
            continue
        confidence = item.get("confidence", item.get("avg_logprob"))
        result.append(
            {
                "ordinal": len(result),
                "start_ms": start,
                "end_ms": end,
                "text": text[:4000],
                "confidence": _bounded_score(confidence),
                "source": str(item.get("source") or source),
                "semantic_complete": bool(item.get("semantic_complete", True)),
            }
        )
    return result


def _valid_words(value: Any, *, seconds: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:100_000]:
        if not isinstance(item, Mapping):
            continue
        multiplier = 1000 if seconds else 1
        try:
            start = round(float(item.get("start_ms", item.get("start"))) * multiplier)
            end = round(float(item.get("end_ms", item.get("end"))) * multiplier)
        except (TypeError, ValueError):
            continue
        text = str(item.get("word", item.get("text", ""))).strip()
        if start < 0 or end <= start or not text:
            continue
        result.append(
            {
                "ordinal": len(result),
                "start_ms": start,
                "end_ms": end,
                "text": text[:200],
                "confidence": _bounded_score(item.get("confidence", item.get("probability"))),
            }
        )
    return result


def _valid_ranges(value: Any) -> list[dict[str, int]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, int]] = []
    for item in value[:20_000]:
        if not isinstance(item, Mapping):
            continue
        try:
            start, end = int(item.get("start_ms")), int(item.get("end_ms"))
        except (TypeError, ValueError):
            continue
        if start >= 0 and end > start:
            result.append({"start_ms": start, "end_ms": end})
    return result


def _transcript_for_range(cues: Any, start_ms: int, end_ms: int) -> str:
    return " ".join(
        cue["text"]
        for cue in _valid_cues(cues, "temporal")
        if cue["end_ms"] > start_ms and cue["start_ms"] < end_ms
    )[:4000]


def _inside_word(timestamp_ms: int, words: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        int(word["start_ms"]) + 60 < timestamp_ms < int(word["end_ms"]) - 60
        for word in words
    )


def _normalized_text(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", value.casefold())


def _bounded_score(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 1:
        return None
    return round(number, 3)


def _vtt_timestamp_ms(value: str) -> int | None:
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})", value)
    if match is None:
        return None
    hours, minutes, seconds, millis = match.groups(default="0")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def _multipart_body(
    boundary: str,
    *,
    fields: Mapping[str, str],
    file_field: str,
    filename: str,
    media_type: str,
    data: bytes,
) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _signature_media_type(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        major = header[8:12]
        return "video/quicktime" if major == b"qt  " else "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/x-matroska" if b"matroska" in header.lower() else "video/webm"
    return None


def _media_types_compatible(detected: str, declared: str) -> bool:
    if not declared or declared == "application/octet-stream" or detected == declared:
        return True
    aliases = (
        {"video/mp4", "video/quicktime"},
        # WebM and Matroska share the EBML signature. The DocType may appear
        # beyond the bounded signature window, so ffprobe remains the source
        # of truth for the exact container after this security gate.
        {"video/webm", "video/matroska", "video/x-matroska"},
    )
    return any({detected, declared} <= group for group in aliases)


def _sha256(path: Path, *, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            if cancelled and cancelled():
                raise AssetPipelineError("batch_cancelled", "batch cancellation requested", retryable=False)
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _extract_json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        value = "".join(
            str(item.get("text") or "") for item in value if isinstance(item, Mapping)
        )
    if not isinstance(value, str):
        raise TypeError("vision response content must be text or an object")
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("JSON object not found", value, 0)
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, Mapping):
        raise TypeError("vision response JSON must be an object")
    return parsed


_VISION_PROMPT = """你是视频素材分析器。综合所有代表帧，只输出一个 JSON 对象，不要 Markdown。
字段必须包含：summary、language、people、organizations、locations、eras、scene_types、
actions、moods、visual_styles、keywords、has_embedded_text、embedded_text_type、
has_watermark、safety、quality、
confidence、segments。数组字段使用简洁中文标签；confidence 与 quality.score 为 0 到 1；
safety 至少包含 adult、violence；quality 包含 usable、score。
segments 为镜头语义列表，每项包含 description、people、locations、keywords、scene_type、
action、era、mood、visual_style、shot_type、confidence。不要猜测无法从画面确认的人名；
存在台标、账号角标或平台水印时 has_watermark=true；画面有任何文字时
has_embedded_text=true，并将 embedded_text_type 严格设为 none、scoreboard、
small_caption、subtitle、large_text 或 mixed 之一。体育比赛正常比分牌用 scoreboard；
对白字幕用 subtitle；遮挡主体的大字用 large_text。"""
