"""Evidence honesty and real local-media coverage for the benchmark worker."""

from __future__ import annotations

import array
import itertools
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest
from framefactory.worker import benchmark_analysis as worker


def test_sampling_covers_whole_video_and_caps_model_budget() -> None:
    intervals = worker.sample_intervals(600_000, list(range(500, 600_000, 500)))
    assert len(intervals) == worker.MAX_FRAMES
    assert intervals[0][0] == 0 and intervals[-1][1] == 600_000
    assert all(first[1] == second[0] for first, second in itertools.pairwise(intervals))
    assert all(end > start for start, end in intervals)
    assert {0, 1000, 3000}.issubset({start for start, _end in intervals})
    assert worker.sample_intervals(500, []) == [(0, 500)]


def test_media_budget_includes_external_source_and_reserves_atomic_replacement(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"s" * 4096)
    output = tmp_path / "output"
    output.mkdir()
    target = output / "analysis.json"
    target.write_bytes(b"r" * 4096)
    budget = worker.MediaBudget(output, source, 8192)
    budget.reserve()
    with pytest.raises(worker.MediaStorageError, match="media_storage_budget_exceeded"):
        worker.atomic_json(target, {"status": "complete"}, budget=budget)
    assert target.read_bytes() == b"r" * 4096
    assert not list(output.glob(".*.tmp"))


def test_frame_budget_rejects_before_ffmpeg_or_provider_spend(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output"
    output.mkdir()
    def forbidden_command(*_args, **_kwargs):
        pytest.fail("Insufficient disk budget must not start ffmpeg")
    monkeypatch.setattr(worker, "_run", forbidden_command)
    budget = worker.MediaBudget(output, source, worker.MAX_FRAME_BYTES)
    with pytest.raises(worker.MediaStorageError, match="media_storage_budget_exceeded"):
        worker.extract_frames(source, output, "ffmpeg", [(0, 1000)], budget=budget)
    with pytest.raises(worker.AnalysisError, match="frame_sampling_budget_exceeded"):
        worker.extract_frames(source, output, "ffmpeg", [(0, 1000)] * 37)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Real FFmpeg required")
def test_real_audio_extraction_stops_at_600_seconds_and_stays_within_byte_cap(tmp_path):
    # A real overlong local source exercises FFmpeg's output deadline even when
    # container metadata would otherwise let decoding continue past the limit.
    source = tmp_path / "long-audio.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        second = b"\x00\x00" * 16000
        for _ in range(605):
            stream.writeframesraw(second)
    output = tmp_path / "output"
    output.mkdir()
    audio = worker.extract_audio(source, output, shutil.which("ffmpeg"))
    assert audio.stat().st_size <= worker.MAX_AUDIO_BYTES
    with wave.open(str(audio), "rb") as stream:
        assert stream.getnframes() == 600 * 16_000


def test_json_only_asr_never_invents_timestamps() -> None:
    value = worker.normalize_transcript({"text": "这是一段配音"}, 15_000, provider="sensevoice")
    assert value["text"] == "这是一段配音"
    assert value["segments"] == value["words"] == []
    assert value["timestamp_source"] is None
    assert value["timing_precision"] == "text_only"


def test_transcript_drops_nonfinite_outside_and_overlapping_provider_ranges() -> None:
    value = worker.normalize_transcript({"segments": [
        {"start": float("nan"), "end": 1, "text": "invalid"},
        {"start": -1, "end": 1, "text": "invalid"},
        {"start": 0.2, "end": 0.8, "text": "measured"},
        {"start": 0.3, "end": 0.7, "text": "overlap"},
        {"start": 1.5, "end": 3, "text": "clipped"},
        {"start": 5, "end": 6, "text": "outside"},
    ]}, 2000, provider="test")
    assert value["segments"] == [
        {"start_ms": 200, "end_ms": 800, "text": "measured"},
        {"start_ms": 1500, "end_ms": 2000, "text": "clipped"},
    ]


def test_empty_asr_alone_is_not_evidence_of_no_speech(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker, "local_transcribe", lambda *args: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(worker, "cloud_transcribe", lambda *args: worker.normalize_transcript({}, 1000, provider="cloud"))
    _transcript, temporal, cap = worker.speech_analysis(tmp_path / "audio.wav", 1000, {})
    assert temporal["speech_status"] == "unknown"
    assert cap["status"] != "complete"


def test_vad_negative_audio_skips_hallucination_prone_cloud_asr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker, "local_transcribe", lambda *args: (worker.normalize_transcript({}, 1000, provider="silero"), []))
    def unexpected_cloud(*args):
        pytest.fail("VAD-negative audio must not be sent for speculative text")
    monkeypatch.setattr(worker, "cloud_transcribe", unexpected_cloud)
    transcript, temporal, cap = worker.speech_analysis(tmp_path / "audio.wav", 1000, {})
    assert not transcript["text"]
    assert temporal["speech_status"] == "absent"
    assert cap["status"] == "complete"
    assert cap["cloud_request_count"] == 0


def test_provider_origin_rejects_cleartext_credentials_and_query() -> None:
    for address in ("http://example.org/v1", "https://user:secret@example.org/v1", "https://example.org/v1?key=secret", "https://example.org/#private"):
        with pytest.raises(worker.AnalysisError, match="credential_free_https"):
            worker._provider_url(address, "/chat/completions")
    assert worker._provider_url("https://example.org/v1/", "/chat/completions") == "https://example.org/v1/chat/completions"


def test_audio_energy_measures_signal_and_terminal_silence(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    samples = array.array("h", [12000, -12000] * 8000 + [0] * 16000)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(samples.tobytes())
    value = worker.audio_metrics(audio)
    assert value["silences"] == [{"start_ms": 1000, "end_ms": 2000}]
    assert value["low_energy_ratio"] == 0.5
    assert value["clipped_sample_ratio"] == 0
    assert value["peak_dbfs"] == pytest.approx(-8.73, abs=0.02)
    assert len(value["energy_windows"]) == 20


def test_segment_text_only_transcript_is_not_misplaced_on_timeline() -> None:
    frames = [{"id": 0, "key": "frames/0000.jpg", "start_ms": 0, "end_ms": 1000, "ocr": {"items": []}}]
    segments = worker.build_segments(frames, {"text": "untimed", "segments": []}, {})
    assert segments[0]["transcript"] == ""
    assert segments[0]["confidence"] is None
    assert "尚无视觉模型" in segments[0]["description"]


def test_pitch_measurement_is_explicit_periodicity_not_speaker_identity(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    audio = tmp_path / "tone.wav"
    samples = array.array("h", [round(16000 * math.sin(2 * math.pi * 200 * i / 16000)) for i in range(16000)])
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(samples.tobytes())
    result = worker.pitch_metrics(audio, [{"start_ms": 0, "end_ms": 1000}])
    assert result["median_hz"] == pytest.approx(200, abs=3)
    assert result["status"] == "complete"
    assert any("背景音乐" in item for item in result["limitations"])
    assert worker.pitch_metrics(audio, [])["status"] == "not_applicable"


def test_narrative_rejects_ungrounded_quotes_and_invalid_time_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [{"id": 0, "key": "frames/0000.jpg", "timestamp_ms": 500, "start_ms": 0, "end_ms": 1000,
               "vision": {"description": "red object"}}]
    sections = [
        {"start_ms": 0, "end_ms": 1000, "role": "opening", "observation": "red", "strategy_hypothesis": "contrast", "evidence_frame_keys": ["frames/0000.jpg"], "transcript_quotes": []},
        {"start_ms": 0, "end_ms": 1000, "evidence_frame_keys": ["frames/0000.jpg"], "transcript_quotes": ["fabricated"]},
        {"start_ms": 1000, "end_ms": 1500, "evidence_frame_keys": ["frames/0000.jpg"], "transcript_quotes": []},
    ]
    monkeypatch.setattr(worker, "_request", lambda *args: {"choices": [{"message": {"content": json.dumps({
        "sections": sections, "voiceover_findings": [{"claim": "invented speech", "evidence_kind": "transcript", "transcript_quote": "fiction"}],
    })}}]})
    result, cap = worker.analyze_narrative(frames, {"text": "", "segments": []}, {"speech_status": "absent"}, {}, 1500, {
        "FRAMEFACTORY_ASSET_VISION_BASE_URL": "https://example.org/v1", "FRAMEFACTORY_ASSET_VISION_API_KEY": "test-only",
        "FRAMEFACTORY_ASSET_VISION_MODEL": "test-only",
    })
    assert len(result["sections"]) == 1
    assert result["voiceover_findings"] == []
    assert cap["request_count"] == 1


def test_narrative_never_equates_vad_negative_with_silent_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [{"key": "frames/0000.jpg", "timestamp_ms": 500, "start_ms": 0, "end_ms": 1000, "vision": {"description": "flower"}}]
    monkeypatch.setattr(worker, "_request", lambda *args: {"choices": [{"message": {"content": json.dumps({
        "voiceover_findings": [{"claim": "静音处理", "evidence_kind": "audio_metrics", "transcript_quote": None}],
        "audio_visual_findings": [{"claim": "画面配合静音处理", "evidence_frame_keys": ["frames/0000.jpg"], "transcript_quote": None}],
    })}}]})
    result, _cap = worker.analyze_narrative(frames, {"text": "", "segments": []}, {"speech_status": "absent"}, {"rms_dbfs": -12}, 1000, {
        "FRAMEFACTORY_ASSET_VISION_BASE_URL": "https://example.org/v1", "FRAMEFACTORY_ASSET_VISION_API_KEY": "test-only", "FRAMEFACTORY_ASSET_VISION_MODEL": "test-only",
    })
    assert result["voiceover_findings"] == result["audio_visual_findings"] == []


def test_narrative_audio_visual_quote_must_overlap_frame_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [{"key": "frames/0000.jpg", "timestamp_ms": 500, "start_ms": 0, "end_ms": 1000, "vision": {"description": "flower"}}]
    monkeypatch.setattr(worker, "_request", lambda *args: {"choices": [{"message": {"content": json.dumps({
        "audio_visual_findings": [{"claim": "错绑旁白", "evidence_frame_keys": ["frames/0000.jpg"], "transcript_quote": "later"}],
    })}}]})
    result, _cap = worker.analyze_narrative(frames, {"text": "later", "segments": [{"start_ms": 2000, "end_ms": 2500, "text": "later"}]}, {"speech_status": "present"}, {}, 3000, {
        "FRAMEFACTORY_ASSET_VISION_BASE_URL": "https://example.org/v1", "FRAMEFACTORY_ASSET_VISION_API_KEY": "test-only", "FRAMEFACTORY_ASSET_VISION_MODEL": "test-only",
    })
    assert result["audio_visual_findings"] == []


def test_vision_rejects_unrelated_evidence_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "frames"
    folder.mkdir()
    (folder / "0000.jpg").write_bytes(b"test-only-transport-payload")
    frames = [{"id": 0, "key": "frames/0000.jpg", "timestamp_ms": 500}]
    monkeypatch.setattr(worker, "_request", lambda *args: {"choices": [{"message": {"content": json.dumps({
        "frames": [{"frame_id": 999, "description": "not supplied"}, {"frame_id": 0, "description": "red object"}],
        "insights": [{"claim": "unsupported", "evidence_frame_ids": [999]}, {"claim": "supported", "evidence_frame_ids": [0]}],
    })}}]})
    cap, insights = worker.analyze_vision(frames, tmp_path, {
        "FRAMEFACTORY_ASSET_VISION_BASE_URL": "https://example.org/v1",
        "FRAMEFACTORY_ASSET_VISION_API_KEY": "test-only-secret",
        "FRAMEFACTORY_ASSET_VISION_MODEL": "vision-test",
    }, lambda *args: None)
    assert cap["status"] == "complete"
    assert [item["claim"] for item in insights] == ["supported"]
    assert insights[0]["evidence_frame_keys"] == ["frames/0000.jpg"]
    assert "test-only-secret" not in json.dumps([cap, insights, frames])


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="real FFmpeg binaries required")
def test_real_ffmpeg_probe_scene_frames_and_honest_missing_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "two-scenes.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:r=12:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=12:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ], check=True, timeout=30)
    # Keep this local-media integration deterministic across optional OCR installations.
    monkeypatch.setattr(worker, "recognize_text", lambda *args: worker.capability("unavailable", "rapidocr", "test environment does not exercise OCR"))
    output = tmp_path / "job"
    result = worker.analyze(source, output, "two real scenes", environment={})
    assert result["technical"]["duration_ms"] == 2000
    assert result["technical"]["has_audio"] is False
    assert result["scene_cuts_ms"]
    assert result["status"] == "partial"
    assert result["capabilities"]["vision"]["status"] == "unavailable"
    assert result["capabilities"]["asr"]["status"] == "not_applicable"
    assert result["temporal"]["speech_status"] == "absent"
    assert all((output / item["key"]).read_bytes().startswith(b"\xff\xd8") for item in result["frames"])
    assert {0, 1000}.issubset({item["timestamp_ms"] for item in result["frames"]})
    assert json.loads((output / "analysis.json").read_text(encoding="utf-8"))["technical"] == result["technical"]
    assert json.loads((output / "progress.json").read_text(encoding="utf-8"))["percent"] == 100
    assert not list(output.glob("*.tmp"))
