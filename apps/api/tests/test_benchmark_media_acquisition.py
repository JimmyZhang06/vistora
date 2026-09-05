from __future__ import annotations

import io
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from framefactory_api import benchmark_note_sources as sources
from framefactory_api.benchmark_accounts import BenchmarkAccountError
from framefactory_api.benchmark_media_reports import (
    build_benchmark_deep_note_report,
    timeline_from_worker_analysis,
)

PROFILE = "a" * 24
NOTE = "b" * 24


def stream(url: str = "http://video.xhscdn.com/video.mp4", **changes: object) -> dict:
    return {
        "url": url,
        "size": 1000,
        "height": 1080,
        "format": "mp4",
        "duration": 18_000,
        **changes,
    }


def media(**changes: object) -> dict:
    return {"id": NOTE, "author": PROFILE, "type": "video", "streams": [stream()], **changes}


@pytest.mark.parametrize("changes", [{"id": "c" * 24}, {"author": "c" * 24}, {"type": "image"}])
def test_selected_media_must_match_note_account_and_type(changes: dict) -> None:
    with pytest.raises(BenchmarkAccountError):
        sources._select_video_stream(media(**changes), PROFILE, NOTE)


@pytest.mark.parametrize(
    "url",
    [
        "https://xhscdn.com.attacker.example/video",
        "https://user@video.xhscdn.com/video",
        "https://video.xhscdn.com:443/video",
        "https://video.xhscdn.com:invalid/video",
        "https://127.0.0.1/video",
        "file:///tmp/video",
        "https://video.xhscdn.com/video#part",
    ],
)
def test_untrusted_media_urls_are_rejected(url: str) -> None:
    with pytest.raises(BenchmarkAccountError):
        sources._select_video_stream(media(streams=[stream(url)]), PROFILE, NOTE)


def test_best_stream_is_upgraded_to_tls_and_bounded() -> None:
    selected = sources._select_video_stream(
        media(
            streams=[
                stream(height=2160, size=4000),
                stream(),
                stream(duration=600_001),
            ]
        ),
        PROFILE,
        NOTE,
    )
    assert selected["url"].startswith("https://")
    assert selected["height"] == 1080
    assert not sources._is_trusted_media_url(urlsplit("https://video.xhscdn.com:invalid/v"))


def test_mutation_rejects_untrusted_browser_origin_and_bad_profile():
    from fastapi.testclient import TestClient

    from framefactory_api.main import create_app
    from framefactory_api.repository import InMemoryControlRepository

    with TestClient(create_app(repository=InMemoryControlRepository())) as client:
        denied = client.post(
            "/v1/benchmark-analysis/jobs/00000000-0000-0000-0000-000000000001/cancel",
            headers={"Origin": "https://untrusted.example"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "BENCHMARK_ORIGIN_REJECTED"
        assert denied.headers["Cache-Control"] == "private, no-store"
        invalid = client.post(
            "/v1/benchmark-analysis/jobs",
            json={
                "platform": "xiaohongshu",
                "profile_url": "https://attacker.invalid/",
                "note_id": NOTE,
            },
            headers={"Idempotency-Key": "invalid-input-test"},
        )
        assert invalid.status_code == 422


def test_dns_private_address_is_rejected_before_connection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sources.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 443))]
    )
    with pytest.raises(BenchmarkAccountError, match="not publicly routed"):
        sources._download_research_video(
            "https://video.xhscdn.com/v", tmp_path / "source.mp4", deadline=time.monotonic() + 1
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "mode", ["success", "redirect", "truncated", "existing_part", "invalid_bytes"]
)
def test_transfer_integrity_redirect_and_partial_cleanup(
    monkeypatch, tmp_path: Path, mode: str
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"0" * 32
    if mode == "invalid_bytes":
        payload = b"not-a-media-container"

    class Response(io.BytesIO):
        status = 302 if mode == "redirect" else 200

        def getheader(self, name, default=""):
            return {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(payload) + (10 if mode == "truncated" else 0)),
            }.get(name, default)

    class Connection:
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, target, headers):
            assert "Cookie" not in headers and "Authorization" not in headers

        def getresponse(self):
            return Response(payload)

        def close(self):
            pass

    monkeypatch.setattr(
        sources.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 443))]
    )
    monkeypatch.setattr(sources, "_PinnedMediaConnection", Connection)
    destination = tmp_path / "source.mp4"
    partial = destination.with_suffix(".part")
    if mode == "existing_part":
        partial.write_bytes(b"preserve-other-file")
    if mode == "success":
        evidence = sources._download_research_video(
            "https://video.xhscdn.com/v", destination, deadline=time.monotonic() + 1
        )
        assert evidence["byte_size"] == len(payload)
        assert len(evidence["sha256"]) == 64
        assert destination.read_bytes() == payload
    else:
        with pytest.raises(BenchmarkAccountError):
            sources._download_research_video(
                "https://video.xhscdn.com/v", destination, deadline=time.monotonic() + 1
            )
        assert not destination.exists()
    assert (
        partial.read_bytes() == b"preserve-other-file"
        if mode == "existing_part"
        else not partial.exists()
    )


def test_report_preserves_gaps_and_does_not_count_samples_as_shots() -> None:
    result = {
        "status": "complete",
        "sampling": {"intervals_are_exact_shots": False},
        "technical": {"duration_ms": 6000, "has_audio": True},
        "temporal": {
            "speech_status": "absent",
            "silences": [
                {"start_ms": 0, "end_ms": 2000},
                {"start_ms": 1000, "end_ms": 3000},
            ],
        },
        "segments": [{"start_ms": 0, "end_ms": 6000, "description": "sampled frame"}],
        "limitations": ["OCR samples may miss transient text"],
    }
    report = build_benchmark_deep_note_report(
        timeline_from_worker_analysis(title="test", source_label="test", analysis=result)
    )
    assert report.status == "ready"  # Successful OCR with no text is not unavailable OCR.
    assert "OCR samples may miss transient text" in report.limitations
    assert report.metrics[0].label == "证据采样节奏"
    assert report.metrics[3].value == "50.0%"  # union, not overlapping silence double-count
    assert report.findings[0].confidence == "low"
    result["status"] = "partial"
    assert (
        build_benchmark_deep_note_report(
            timeline_from_worker_analysis(title="test", source_label="test", analysis=result)
        ).status
        == "partial"
    )


def test_resampled_audio_tail_does_not_break_real_video_report():
    result = {
        "status": "complete",
        "technical": {"duration_ms": 269466, "has_audio": True},
        "temporal": {
            "speech_status": "unknown",
            "silences": [{"start_ms": 269000, "end_ms": 269468}],
        },
        "segments": [{"start_ms": 0, "end_ms": 269466, "description": "real timeline"}],
    }
    adapted = timeline_from_worker_analysis(title="test", source_label="test", analysis=result)
    assert adapted.silences == [(269000, 269466)]
    result["temporal"]["silences"][0]["end_ms"] = 300000
    with pytest.raises(ValueError, match="silence range"):
        timeline_from_worker_analysis(title="test", source_label="test", analysis=result)
