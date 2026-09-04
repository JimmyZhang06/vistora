from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

RELEASE_TOOLS = Path(__file__).resolve().parents[1] / "release"
sys.path.insert(0, str(RELEASE_TOOLS))

from document_video_e2e_gate import (
    GateBlocked,
    GateFailure,
    _verify_retention_and_purge,
    inspect_pdf_fixture,
    validate_artifact_inventory,
    validate_probe,
    validate_review_evidence,
)


@dataclass
class _Response:
    body: bytes
    status: int = 200

    def json(self) -> Any:
        return json.loads(self.body)


class _InlineClient:
    def __init__(self, value: dict[str, Any]) -> None:
        self.body = json.dumps(value, separators=(",", ":")).encode()
        self.path: str | None = None

    def expect(self, method: str, path: str, statuses: set[int]) -> _Response:
        assert method == "GET"
        assert statuses == {200}
        self.path = path
        return _Response(self.body)


class _PurgeClient:
    def __init__(self) -> None:
        self.purge_posts = 0

    def expect(
        self,
        method: str,
        path: str,
        statuses: set[int],
        **options: Any,
    ) -> _Response:
        payload = options.get("payload") or {}
        if method == "PATCH" and path.endswith("/retention"):
            revision = 3 if payload["retention_until"] else 4
            return _Response(json.dumps({"revision": revision}).encode())
        if method == "POST" and path.endswith("/legal-hold"):
            revision = 5 if payload["active"] else 6
            return _Response(json.dumps({"revision": revision}).encode())
        if method == "POST" and path.endswith("/purge-requests"):
            self.purge_posts += 1
            if self.purge_posts == 1:
                return _Response(b'{"code":"DOCUMENT_RETENTION_ACTIVE"}', 409)
            if self.purge_posts == 2:
                return _Response(b'{"code":"DOCUMENT_LEGAL_HOLD_ACTIVE"}', 409)
            return _Response(b'{"id":"purge-1","status":"queued"}', 202)
        if method == "GET" and path == "/v1/document-purge-requests/purge-1":
            return _Response(
                b'{"id":"purge-1","status":"succeeded","attempt_count":1}'
            )
        if method == "GET" and path == "/v1/document-sources/source-1":
            return _Response(b'{"status":"purged","filename":"purged.pdf"}')
        if method == "GET" and path == "/v1/artifacts?run_id=run-1&limit=100":
            return _Response(b'{"data":[]}')
        if method == "GET" and path.startswith("/v1/artifacts/"):
            assert statuses == {404}
            return _Response(b'{"code":"RESOURCE_NOT_FOUND"}', 404)
        if method == "GET" and path == "/v1/steps?run_id=run-1&limit=100":
            return _Response(
                b'{"data":[{"review":{"decision":"approve"}},'
                b'{"review":{"decision":"approve"}}]}'
            )
        if method == "GET" and path == "/v1/events?run_id=run-1&limit=100":
            events = [
                {"sequence": 1, "event_type": "run.created"},
                {"sequence": 2, "event_type": "review.recorded"},
                {"sequence": 3, "event_type": "review.recorded"},
                *[
                    {"sequence": value, "event_type": "artifact.created"}
                    for value in range(4, 9)
                ],
            ]
            return _Response(json.dumps({"data": events}).encode())
        raise AssertionError(f"unexpected request: {method} {path} {statuses}")


def _artifact(filename: str, body: bytes, media_type: str = "application/json") -> dict[str, Any]:
    return {
        "id": f"artifact-{filename}",
        "filename": filename,
        "media_type": media_type,
        "byte_size": len(body),
        "content_hash": hashlib.sha256(body).hexdigest(),
        "status": "available",
    }


def test_pdf_fixture_is_hashed_without_trusting_its_extension(tmp_path: Path) -> None:
    path = tmp_path / "evidence.pdf"
    content = b"%PDF-1.7\nrelease evidence\n%%EOF\n"
    path.write_bytes(content)

    evidence = inspect_pdf_fixture(str(path))

    assert evidence.byte_size == len(content)
    assert evidence.sha256 == hashlib.sha256(content).hexdigest()


def test_pdf_fixture_rejects_disguised_non_pdf(tmp_path: Path) -> None:
    path = tmp_path / "evidence.pdf"
    path.write_bytes(b"not a PDF")

    with pytest.raises(GateBlocked, match="signature"):
        inspect_pdf_fixture(str(path))


def test_storyboard_review_requires_every_scene_to_match_source_hash() -> None:
    source_hash = "a" * 64
    storyboard = {
        "schema_version": "1.0.0",
        "source_sha256": source_hash,
        "scenes": [{"page": 2, "source_sha256": source_hash}],
    }
    client = _InlineClient(storyboard)
    artifact = _artifact("storyboard.json", client.body)

    reviewed = validate_review_evidence(
        client,
        {"id": "run:storyboard", "node_key": "storyboard", "output_artifacts": [artifact]},
        source_hash,
    )

    assert reviewed == artifact
    assert client.path == f"/v1/artifacts/{artifact['id']}/content?inline=true"


def test_storyboard_review_rejects_cross_source_scene() -> None:
    storyboard = {
        "schema_version": "1.0.0",
        "source_sha256": "a" * 64,
        "scenes": [{"page": 1, "source_sha256": "b" * 64}],
    }
    client = _InlineClient(storyboard)

    with pytest.raises(GateFailure, match="not grounded"):
        validate_review_evidence(
            client,
            {
                "id": "run:storyboard",
                "node_key": "storyboard",
                "output_artifacts": [_artifact("storyboard.json", client.body)],
            },
            "a" * 64,
        )


def test_artifact_inventory_requires_final_delivery_set() -> None:
    media_types = {
        "final.mp4": "video/mp4",
        "poster.png": "image/png",
        "captions.srt": "application/x-subrip",
        "storyboard.json": "application/json",
        "quality-report.json": "application/json",
    }
    artifacts = [
        _artifact(name, name.encode(), media_type) for name, media_type in media_types.items()
    ]

    inventory = validate_artifact_inventory({"data": artifacts})

    assert set(inventory) == set(media_types)


def test_artifact_inventory_rejects_missing_poster() -> None:
    with pytest.raises(GateFailure, match="poster.png"):
        validate_artifact_inventory(
            {
                "data": [
                    _artifact("final.mp4", b"video", "video/mp4"),
                    _artifact("captions.srt", b"captions", "application/x-subrip"),
                    _artifact("storyboard.json", b"{}"),
                    _artifact("quality-report.json", b"{}"),
                ]
            }
        )


def test_probe_requires_expected_codecs_audio_and_dimensions() -> None:
    result = validate_probe(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "30.125"},
        },
        "16:9",
    )

    assert result["duration_seconds"] == 30.125


def test_probe_rejects_silent_delivery() -> None:
    with pytest.raises(GateFailure, match="video and one audio"):
        validate_probe(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 720,
                        "height": 1280,
                    }
                ],
                "format": {"duration": "30"},
            },
            "9:16",
        )


def test_release_gate_can_destructively_verify_retention_hold_and_purge() -> None:
    client = _PurgeClient()

    result = _verify_retention_and_purge(
        client,  # type: ignore[arg-type]
        source_id="source-1",
        source_revision=2,
        run_id="run-1",
        artifact_ids=["artifact-1", "artifact-2"],
        timeout=1,
        poll_interval=0.001,
    )

    assert result == {
        "request_id": "purge-1",
        "status": "succeeded",
        "attempt_count": 1,
        "source_status": "purged",
        "artifact_tombstones_verified": 2,
        "review_decisions_retained": 2,
    }
