from __future__ import annotations

import json
from pathlib import Path
from typing import Self
from unittest.mock import patch

from framefactory.worker.adapters.asset_analysis import (
    OpenAICompatibleAudioTranscriber,
    _merge_transcription_chunks,
)


class FakeResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.payload = json.dumps(value).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_text_only_asr_does_not_claim_timing_precision(tmp_path: Path) -> None:
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"test-audio")
    transcriber = OpenAICompatibleAudioTranscriber(
        base_url="https://speech.example/v1",
        api_key="dummy-asr-key",
        model="text-only-model",
        response_format="json",
        timestamp_mode="none",
    )

    with patch(
        "framefactory.worker.adapters.asset_analysis.urllib.request.urlopen",
        return_value=FakeResponse({"text": "完整转写，但没有时间戳。"}),
    ) as send:
        result = transcriber.transcribe(audio)

    request = send.call_args.args[0]
    assert b'timestamp_granularities[]' not in request.data
    assert result["timing_precision"] == "text_only"
    assert result["status"] == "unavailable"
    assert result["text"] == "完整转写，但没有时间戳。"


def test_word_timestamp_asr_is_eligible_for_safe_cut_alignment(tmp_path: Path) -> None:
    audio = tmp_path / "speech.mp3"
    audio.write_bytes(b"test-audio")
    transcriber = OpenAICompatibleAudioTranscriber(
        base_url="https://speech.example/v1",
        api_key="dummy-asr-key",
        model="word-clock-model",
    )

    with patch(
        "framefactory.worker.adapters.asset_analysis.urllib.request.urlopen",
        return_value=FakeResponse(
            {
                "text": "说完再切",
                "language": "zh",
                "words": [
                    {"start": 0.0, "end": 0.4, "word": "说完"},
                    {"start": 0.5, "end": 0.9, "word": "再切"},
                ],
            }
        ),
    ) as send:
        result = transcriber.transcribe(audio)

    request = send.call_args.args[0]
    assert b'timestamp_granularities[]' in request.data
    assert result["timing_precision"] == "word"
    assert result["status"] == "available"
    assert len(result["words"]) == 2


def test_chunked_text_only_asr_preserves_source_clock_without_claiming_word_timing() -> None:
    result = _merge_transcription_chunks(
        (
            (0, 300_000, {"provider": "speech", "model": "sense", "text": "第一段"}),
            (300_000, 420_000, {"provider": "speech", "model": "sense", "text": "第二段"}),
        )
    )

    assert result["status"] == "available"
    assert result["timing_precision"] == "chunk"
    assert result["reason"] == "coarse_chunk_timing"
    assert result["chunk_count"] == 2
    assert result["segments"][0]["start_ms"] == 0
    assert result["segments"][-1]["end_ms"] == 420_000
    assert all(item["semantic_complete"] is False for item in result["segments"])
    assert all(item["source"] == "asr_estimated" for item in result["segments"])


def test_chunked_word_timestamps_are_shifted_onto_the_original_media_clock() -> None:
    result = _merge_transcription_chunks(
        (
            (
                300_000,
                600_000,
                {
                    "provider": "speech",
                    "model": "timestamped",
                    "text": "继续",
                    "words": [
                        {"start_ms": 500, "end_ms": 900, "text": "继续"}
                    ],
                },
            ),
        )
    )

    assert result["timing_precision"] == "word"
    assert result["words"][0]["start_ms"] == 300_500
    assert result["words"][0]["end_ms"] == 300_900
