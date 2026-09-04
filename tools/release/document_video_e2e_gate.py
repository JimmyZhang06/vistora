"""Prove the deployed PDF-to-video path with a real, rights-approved PDF.

The gate uses external HTTP, durable API resources, the deployed queue and
workers, object storage, and a real ffprobe binary. It has no TestClient or mock
fallback. Missing infrastructure or operator approval is BLOCKED (exit 2);
violated release invariants are FAILED (exit 1).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from release_gate_common import (
    ApiClient,
    GateBlocked,
    GateFailure,
    gate_main,
    idempotency_key,
    page_items,
    require_env,
)

MAX_PDF_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
EXPECTED_REVIEW_ORDER = ("storyboard", "quality")
EXPECTED_NODES = {
    "inspect",
    "extract",
    "write",
    "storyboard",
    "materialize",
    "augment",
    "tts",
    "timeline",
    "render",
    "quality",
}
REQUIRED_ARTIFACTS = {
    "final.mp4": "video/mp4",
    "poster.png": "image/png",
    "captions.srt": "application/x-subrip",
    "storyboard.json": "application/json",
    "quality-report.json": "application/json",
}


@dataclass(frozen=True, slots=True)
class FileEvidence:
    path: Path
    byte_size: int
    sha256: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        del req, fp, code, msg, headers, newurl


def inspect_pdf_fixture(value: str) -> FileEvidence:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise GateBlocked(f"PDF fixture does not exist: {path}")
    if path.suffix.lower() != ".pdf":
        raise GateBlocked(f"PDF fixture must use a .pdf filename: {path}")
    try:
        byte_size = path.stat().st_size
        digest = hashlib.sha256()
        prefix = b""
        with path.open("rb") as source:
            prefix = source.read(8)
            digest.update(prefix)
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise GateBlocked(f"cannot read PDF fixture {path}: {exc}") from exc
    if not 0 < byte_size <= MAX_PDF_BYTES:
        raise GateBlocked(
            f"PDF fixture must contain 1-{MAX_PDF_BYTES} bytes; received {byte_size}"
        )
    if not prefix.startswith(b"%PDF-"):
        raise GateBlocked("PDF fixture does not start with a PDF signature")
    return FileEvidence(path, byte_size, digest.hexdigest())


def _object(value: Any, label: str) -> dict[str, Any]:
    if hasattr(value, "json"):
        value = value.json()
    if not isinstance(value, dict):
        raise GateFailure(f"{label} must be a JSON object")
    return value


def _transfer_url(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise GateFailure(f"{label} URL is missing")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GateFailure(f"{label} URL must be absolute HTTP(S)")
    if parsed.username or parsed.password:
        raise GateFailure(f"{label} URL must not contain userinfo")
    if parsed.scheme != "https" and os.environ.get("FF_RELEASE_ALLOW_HTTP") != "1":
        raise GateBlocked(
            f"{label} URL must use HTTPS; FF_RELEASE_ALLOW_HTTP=1 is local-only"
        )
    return value


def _upload_pdf(upload: Mapping[str, Any], fixture: FileEvidence, *, timeout: float) -> None:
    if upload.get("method") != "PUT":
        raise GateFailure("document upload descriptor must use PUT")
    url = _transfer_url(upload.get("url"), "document upload")
    raw_headers = upload.get("headers")
    if not isinstance(raw_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_headers.items()
    ):
        raise GateFailure("document upload headers must be a string map")
    headers = dict(raw_headers)
    headers.setdefault("Content-Type", "application/pdf")
    headers.setdefault("Content-Length", str(fixture.byte_size))
    try:
        with fixture.path.open("rb") as source:
            request = urllib.request.Request(url, data=source, headers=headers, method="PUT")
            with urllib.request.urlopen(
                request, timeout=timeout, context=ssl.create_default_context()
            ) as response:
                status = response.status
                response.read(4096)
    except urllib.error.HTTPError as exc:
        summary = exc.read(500).decode("utf-8", errors="replace")
        raise GateFailure(f"document upload returned HTTP {exc.code}: {summary}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateBlocked(f"cannot reach document object storage: {exc}") from exc
    if status not in {200, 201, 204}:
        raise GateFailure(f"document upload returned unexpected HTTP {status}")


def _artifact_from_step(step: Mapping[str, Any], filename: str) -> dict[str, Any]:
    artifacts = step.get("output_artifacts")
    if not isinstance(artifacts, list):
        raise GateFailure(f"review step {step.get('id')} has no artifact evidence")
    matches = [
        item
        for item in artifacts
        if isinstance(item, dict) and item.get("filename") == filename
    ]
    if len(matches) != 1:
        raise GateFailure(
            f"review step {step.get('id')} must expose exactly one {filename} artifact"
        )
    artifact = matches[0]
    if not isinstance(artifact.get("id"), str) or not isinstance(
        artifact.get("content_hash"), str
    ):
        raise GateFailure(f"{filename} artifact is missing immutable identity/hash")
    return artifact


def _inline_json(client: ApiClient, artifact: Mapping[str, Any]) -> dict[str, Any]:
    artifact_id = artifact["id"]
    response = client.expect(
        "GET", f"/v1/artifacts/{artifact_id}/content?inline=true", {200}
    )
    actual_hash = hashlib.sha256(response.body).hexdigest()
    if actual_hash != artifact["content_hash"]:
        raise GateFailure(
            f"inline artifact {artifact_id} hash differs from durable metadata"
        )
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateFailure(f"inline artifact {artifact_id} is not UTF-8 JSON") from exc
    return _object(value, f"artifact {artifact_id}")


def validate_review_evidence(
    client: ApiClient, step: Mapping[str, Any], source_sha256: str
) -> dict[str, Any]:
    node = step.get("node_key")
    if node == "storyboard":
        artifact = _artifact_from_step(step, "storyboard.json")
        storyboard = _inline_json(client, artifact)
        scenes = storyboard.get("scenes")
        if storyboard.get("schema_version") != "1.0.0":
            raise GateFailure("storyboard has an unsupported schema version")
        if storyboard.get("source_sha256") != source_sha256:
            raise GateFailure("storyboard is not bound to the uploaded PDF hash")
        if not isinstance(scenes, list) or not 1 <= len(scenes) <= 30:
            raise GateFailure("storyboard must contain 1-30 grounded scenes")
        for scene in scenes:
            if not isinstance(scene, dict) or not isinstance(scene.get("page"), int):
                raise GateFailure("storyboard contains an invalid page reference")
            if scene["page"] < 1 or scene.get("source_sha256") != source_sha256:
                raise GateFailure("storyboard scene is not grounded in the uploaded PDF")
        return artifact
    if node == "quality":
        artifact = _artifact_from_step(step, "quality-report.json")
        report = _inline_json(client, artifact)
        if report.get("schema_version") != "1.0.0" or report.get("status") != "passed":
            raise GateFailure("quality review evidence does not report a pass")
        if report.get("failures") != [] or report.get("source_mapping_complete") is not True:
            raise GateFailure("quality review evidence has failures or incomplete lineage")
        return artifact
    raise GateFailure(f"unexpected review gate {node!r} in document pipeline")


def _review_until_terminal(
    client: ApiClient,
    run_id: str,
    source_sha256: str,
    *,
    timeout: float,
    poll_interval: float,
) -> tuple[dict[str, Any], list[str]]:
    if os.environ.get("FF_RELEASE_DOCUMENT_AUTO_APPROVE") != "1":
        raise GateBlocked(
            "FF_RELEASE_DOCUMENT_AUTO_APPROVE=1 is required to approve the controlled "
            "storyboard and quality fixtures"
        )
    deadline = time.monotonic() + timeout
    reviewed: list[str] = []
    last_status: Any = None
    while time.monotonic() < deadline:
        run = _object(
            client.expect("GET", f"/v1/runs/{run_id}", {200}), "document run"
        )
        last_status = run.get("status")
        if last_status == "succeeded":
            if tuple(reviewed) != EXPECTED_REVIEW_ORDER:
                raise GateFailure(
                    f"document run succeeded without both ordered reviews: {reviewed}"
                )
            return run, reviewed
        if last_status in {"failed", "cancelled"}:
            steps = page_items(
                client.expect("GET", f"/v1/steps?run_id={run_id}&limit=100", {200}).json()
            )
            failures = [
                {"node": step.get("node_key"), "error": step.get("error")}
                for step in steps
                if step.get("status") == "failed"
            ]
            raise GateFailure(
                f"document run reached {last_status}; failed steps: {failures}"
            )
        steps = page_items(
            client.expect("GET", f"/v1/steps?run_id={run_id}&limit=100", {200}).json()
        )
        awaiting = [step for step in steps if step.get("status") == "awaiting_review"]
        if len(awaiting) > 1:
            raise GateFailure("document run exposes multiple simultaneous review gates")
        if awaiting:
            step = awaiting[0]
            node = step.get("node_key")
            expected = EXPECTED_REVIEW_ORDER[len(reviewed)] if len(reviewed) < 2 else None
            if node != expected:
                raise GateFailure(
                    f"document review order changed: expected {expected!r}, received {node!r}"
                )
            evidence = validate_review_evidence(client, step, source_sha256)
            revision = step.get("revision")
            if not isinstance(revision, int):
                raise GateFailure(f"review step {step.get('id')} has no integer revision")
            action = {
                "decision": "approve",
                "reason": "Controlled document-video release acceptance fixture",
                "issue_codes": [],
                "expected_revision": revision,
            }
            key = idempotency_key(f"document-{node}-review")
            first = _object(
                client.expect(
                    "POST",
                    f"/v1/steps/{step['id']}/review",
                    {200, 201, 202},
                    payload=action,
                    idempotency=key,
                ),
                f"{node} review",
            )
            replay = _object(
                client.expect(
                    "POST",
                    f"/v1/steps/{step['id']}/review",
                    {200, 201, 202},
                    payload=action,
                    idempotency=key,
                ),
                f"{node} review replay",
            )
            if first != replay or first.get("status") != "succeeded":
                raise GateFailure(f"{node} review was not an idempotent approval")
            review = first.get("review")
            evidence_items = review.get("evidence") if isinstance(review, dict) else None
            if not isinstance(evidence_items, list) or not any(
                isinstance(item, dict)
                and item.get("id") == evidence["id"]
                and item.get("content_hash") == evidence["content_hash"]
                for item in evidence_items
            ):
                raise GateFailure(f"{node} review did not persist immutable evidence")
            reviewed.append(str(node))
            continue
        time.sleep(poll_interval)
    raise GateFailure(
        f"document run {run_id} did not finish within {timeout}s; "
        f"last status was {last_status!r}, reviews={reviewed}"
    )


def validate_artifact_inventory(value: Any) -> dict[str, dict[str, Any]]:
    artifacts = page_items(value)
    by_name: dict[str, dict[str, Any]] = {}
    for filename, media_type in REQUIRED_ARTIFACTS.items():
        matches = [item for item in artifacts if item.get("filename") == filename]
        if len(matches) != 1:
            raise GateFailure(f"final Run must expose exactly one {filename} artifact")
        artifact = matches[0]
        if artifact.get("media_type") != media_type:
            raise GateFailure(f"{filename} has unexpected media type {artifact.get('media_type')!r}")
        if artifact.get("status") not in {None, "available"}:
            raise GateFailure(f"{filename} is not available")
        if not isinstance(artifact.get("byte_size"), int) or artifact["byte_size"] <= 0:
            raise GateFailure(f"{filename} has no positive byte size")
        if not isinstance(artifact.get("content_hash"), str) or len(
            artifact["content_hash"]
        ) != 64:
            raise GateFailure(f"{filename} has no SHA-256 metadata")
        by_name[filename] = artifact
    return by_name


def _presigned_artifact_url(client: ApiClient, artifact_id: str) -> str:
    request = urllib.request.Request(
        f"{client.base_url}/v1/artifacts/{artifact_id}/content",
        headers=dict(client.headers),
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()
    )
    try:
        with opener.open(request, timeout=client.timeout) as response:
            status = response.status
            location = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = exc.headers.get("Location")
        if status != 307:
            summary = exc.read(500).decode("utf-8", errors="replace")
            if status in {404, 405, 501}:
                raise GateBlocked(
                    f"artifact download capability is unavailable (HTTP {status}): {summary}"
                ) from exc
            raise GateFailure(
                f"artifact download request returned HTTP {status}: {summary}"
            ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateBlocked(f"cannot request artifact download URL: {exc}") from exc
    if status != 307 or not location:
        raise GateFailure(f"artifact content endpoint must return HTTP 307; received {status}")
    return _transfer_url(urllib.parse.urljoin(client.base_url, location), "artifact download")


def _download_to(
    url: str,
    destination: Path,
    *,
    timeout: float,
    maximum_bytes: int,
) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        request = urllib.request.Request(url, headers={"Accept": "video/mp4"}, method="GET")
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl.create_default_context()
        ) as response, destination.open("wb") as output:
            if response.status != 200:
                raise GateFailure(f"presigned artifact download returned HTTP {response.status}")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise GateFailure("downloaded MP4 exceeds the configured release-gate limit")
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                digest.update(chunk)
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        summary = exc.read(500).decode("utf-8", errors="replace")
        raise GateFailure(f"presigned artifact download returned HTTP {exc.code}: {summary}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateBlocked(f"cannot download final MP4 from object storage: {exc}") from exc
    return total, digest.hexdigest(), bytes(prefix)


def validate_probe(value: Any, aspect_ratio: str) -> dict[str, Any]:
    probe = _object(value, "ffprobe output")
    streams = probe.get("streams")
    format_value = probe.get("format")
    if not isinstance(streams, list) or not isinstance(format_value, dict):
        raise GateFailure("ffprobe output is missing streams or format")
    video = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audio = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if len(video) != 1 or len(audio) != 1:
        raise GateFailure("final MP4 must contain exactly one video and one audio stream")
    expected_size = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1080, 1080)}[
        aspect_ratio
    ]
    if (video[0].get("width"), video[0].get("height")) != expected_size:
        raise GateFailure(
            "final MP4 dimensions differ from the requested aspect ratio: "
            f"{video[0].get('width')}x{video[0].get('height')}"
        )
    if video[0].get("codec_name") != "h264" or audio[0].get("codec_name") != "aac":
        raise GateFailure("final MP4 must use H.264 video and AAC audio")
    try:
        duration = float(format_value.get("duration"))
    except (TypeError, ValueError) as exc:
        raise GateFailure("final MP4 has no numeric duration") from exc
    if duration <= 1:
        raise GateFailure("final MP4 duration is not usable")
    return {
        "duration_seconds": round(duration, 3),
        "width": expected_size[0],
        "height": expected_size[1],
        "video_codec": video[0]["codec_name"],
        "audio_codec": audio[0]["codec_name"],
    }


def _probe_video(path: Path, aspect_ratio: str) -> dict[str, Any]:
    command_name = os.environ.get("FF_RELEASE_FFPROBE_COMMAND", "ffprobe").strip()
    command = shutil.which(command_name)
    if command is None:
        raise GateBlocked(f"ffprobe executable is unavailable: {command_name}")
    try:
        process = subprocess.run(
            [command, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateBlocked(f"ffprobe could not inspect the final MP4: {exc}") from exc
    if process.returncode != 0:
        raise GateFailure(f"ffprobe rejected the final MP4: {process.stderr[:500]}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise GateFailure("ffprobe did not return JSON") from exc
    return validate_probe(value, aspect_ratio)


def _event_evidence(client: ApiClient, run_id: str) -> dict[str, int]:
    events = page_items(
        client.expect("GET", f"/v1/events?run_id={run_id}&limit=100", {200}).json()
    )
    sequences = [event.get("sequence") for event in events]
    if not sequences or any(not isinstance(item, int) for item in sequences):
        raise GateFailure("document Run has no durable sequenced events")
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise GateFailure("document Run event sequence is not ordered and unique")
    types = [str(event.get("type") or event.get("event_type") or "") for event in events]
    required = {"run.created": 1, "review.recorded": 2, "artifact.created": 5}
    counts = {name: types.count(name) for name in required}
    for name, minimum in required.items():
        if counts[name] < minimum:
            raise GateFailure(
                f"document Run event stream has {counts[name]} {name} events; expected at least {minimum}"
            )
    return counts


def _verify_retention_and_purge(
    client: ApiClient,
    *,
    source_id: str,
    source_revision: int,
    run_id: str,
    artifact_ids: list[str],
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    retention = _object(
        client.expect(
            "PATCH",
            f"/v1/document-sources/{source_id}/retention",
            {200},
            payload={
                "retention_until": "2999-01-01T00:00:00Z",
                "reason": "release gate retention policy assertion",
            },
            extra_headers={"If-Match": f'"{source_revision}"'},
        ),
        "retained document source",
    )
    purge_payload = {
        "reason": "controlled release-gate erasure",
        "delete_derived": True,
        "confirmation": "DELETE",
    }
    blocked_retention = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/purge-requests",
            {409},
            payload=purge_payload,
            idempotency=idempotency_key("document-purge-retained"),
            extra_headers={"If-Match": f'"{retention["revision"]}"'},
        ),
        "retention rejection",
    )
    if blocked_retention.get("code") != "DOCUMENT_RETENTION_ACTIVE":
        raise GateFailure("future retention did not fail closed")
    cleared = _object(
        client.expect(
            "PATCH",
            f"/v1/document-sources/{source_id}/retention",
            {200},
            payload={
                "retention_until": None,
                "reason": "release gate completed retention assertion",
            },
            extra_headers={"If-Match": f'"{retention["revision"]}"'},
        ),
        "retention-cleared document source",
    )
    held = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/legal-hold",
            {200},
            payload={"active": True, "reason": "release gate legal-hold assertion"},
            extra_headers={"If-Match": f'"{cleared["revision"]}"'},
        ),
        "held document source",
    )
    blocked_hold = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/purge-requests",
            {409},
            payload=purge_payload,
            idempotency=idempotency_key("document-purge-held"),
            extra_headers={"If-Match": f'"{held["revision"]}"'},
        ),
        "legal-hold rejection",
    )
    if blocked_hold.get("code") != "DOCUMENT_LEGAL_HOLD_ACTIVE":
        raise GateFailure("active legal hold did not fail closed")
    released = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/legal-hold",
            {200},
            payload={"active": False, "reason": "release gate released legal hold"},
            extra_headers={"If-Match": f'"{held["revision"]}"'},
        ),
        "released document source",
    )
    purge_key = idempotency_key("document-purge")
    purge = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/purge-requests",
            {202},
            payload=purge_payload,
            idempotency=purge_key,
            extra_headers={"If-Match": f'"{released["revision"]}"'},
        ),
        "document purge request",
    )
    replay = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/purge-requests",
            {202},
            payload=purge_payload,
            idempotency=purge_key,
            extra_headers={"If-Match": f'"{released["revision"]}"'},
        ),
        "document purge replay",
    )
    request_id = purge.get("id")
    if not isinstance(request_id, str) or replay.get("id") != request_id:
        raise GateFailure("document purge request is not idempotent")
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = purge
    while time.monotonic() < deadline:
        last = _object(
            client.expect("GET", f"/v1/document-purge-requests/{request_id}", {200}),
            "document purge progress",
        )
        if last.get("status") == "succeeded":
            break
        if last.get("status") in {"blocked", "failed"}:
            raise GateFailure(f"document purge reached {last.get('status')}: {last.get('last_error')}")
        time.sleep(poll_interval)
    else:
        raise GateFailure(
            f"document purge {request_id} did not finish within {timeout}s; "
            f"last status was {last.get('status')!r}"
        )
    source = _object(
        client.expect("GET", f"/v1/document-sources/{source_id}", {200}),
        "purged document source",
    )
    if source.get("status") != "purged" or source.get("filename") != "purged.pdf":
        raise GateFailure("purged source did not become a sanitized tombstone")
    remaining = page_items(
        client.expect("GET", f"/v1/artifacts?run_id={run_id}&limit=100", {200}).json()
    )
    if remaining:
        raise GateFailure("purged document Run still exposes downloadable artifacts")
    for artifact_id in artifact_ids:
        client.expect("GET", f"/v1/artifacts/{artifact_id}", {404})
        client.expect("GET", f"/v1/artifacts/{artifact_id}/content", {404})
    steps = page_items(
        client.expect("GET", f"/v1/steps?run_id={run_id}&limit=100", {200}).json()
    )
    decisions = [
        step.get("review", {}).get("decision")
        for step in steps
        if isinstance(step.get("review"), dict) and step["review"].get("decision")
    ]
    if decisions.count("approve") < 2:
        raise GateFailure("purge removed required storyboard/quality review evidence")
    _event_evidence(client, run_id)
    return {
        "request_id": request_id,
        "status": last["status"],
        "attempt_count": last.get("attempt_count"),
        "source_status": source["status"],
        "artifact_tombstones_verified": len(artifact_ids),
        "review_decisions_retained": len(decisions),
    }


def _positive_float(name: str, default: str) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError as exc:
        raise GateBlocked(f"{name} must be numeric") from exc
    if value <= 0:
        raise GateBlocked(f"{name} must be positive")
    return value


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError as exc:
        raise GateBlocked(f"{name} must be an integer") from exc
    if value <= 0:
        raise GateBlocked(f"{name} must be positive")
    return value


def run_gate() -> dict[str, Any]:
    if os.environ.get("FF_RELEASE_DOCUMENT_RIGHTS_CONFIRMED") != "1":
        raise GateBlocked(
            "FF_RELEASE_DOCUMENT_RIGHTS_CONFIRMED=1 is required for a legally approved fixture"
        )
    fixture = inspect_pdf_fixture(require_env("FF_RELEASE_DOCUMENT_PDF"))
    client = ApiClient.from_environment()
    timeout = _positive_float("FF_RELEASE_DOCUMENT_TIMEOUT_SECONDS", "7200")
    poll_interval = _positive_float("FF_RELEASE_DOCUMENT_POLL_SECONDS", "2")
    transfer_timeout = _positive_float("FF_RELEASE_DOCUMENT_TRANSFER_TIMEOUT_SECONDS", "300")

    health = _object(client.expect("GET", "/healthz", {200}), "health")
    persistence = health.get("persistence")
    if not isinstance(persistence, str) or persistence in {"memory", "in_memory", "process_memory"}:
        raise GateBlocked("document acceptance requires the durable PostgreSQL adapter")
    readiness = _object(client.expect("GET", "/readyz", {200}), "readiness")
    if readiness.get("status") not in {"ok", "ready"}:
        raise GateFailure(f"release API is not ready: {readiness!r}")

    source_payload = {
        "filename": fixture.path.name,
        "content_type": "application/pdf",
        "byte_size": fixture.byte_size,
        "sha256": fixture.sha256,
        "rights_confirmed": True,
    }
    source = _object(
        client.expect(
            "POST",
            "/v1/document-sources",
            {201},
            payload=source_payload,
            idempotency=idempotency_key("document-source"),
        ),
        "document source",
    )
    source_id = source.get("id")
    object_key = source.get("object_key")
    if not isinstance(source_id, str) or not isinstance(object_key, str):
        raise GateFailure("document source response is missing upload identity")
    upload = source.get("upload")
    if not isinstance(upload, dict):
        raise GateFailure("new document source has no presigned upload descriptor")
    _upload_pdf(upload, fixture, timeout=transfer_timeout)
    completed = _object(
        client.expect(
            "POST",
            f"/v1/document-sources/{source_id}/complete",
            {200},
            payload={
                "object_key": object_key,
                "sha256": fixture.sha256,
                "content_type": "application/pdf",
            },
        ),
        "completed document source",
    )
    if completed.get("status") != "uploaded" or completed.get("content_hash") != fixture.sha256:
        raise GateFailure("completed document source does not preserve the uploaded PDF hash")
    if "object_key" in completed or "bucket" in completed or "upload" in completed:
        raise GateFailure("completed document source leaks a private storage locator")

    aspect_ratio = os.environ.get("FF_RELEASE_DOCUMENT_ASPECT_RATIO", "").strip() or "16:9"
    if aspect_ratio not in {"16:9", "9:16", "1:1"}:
        raise GateBlocked("FF_RELEASE_DOCUMENT_ASPECT_RATIO must be 16:9, 9:16, or 1:1")
    try:
        duration_seconds = int(
            os.environ.get("FF_RELEASE_DOCUMENT_DURATION_SECONDS", "").strip() or "30"
        )
    except ValueError as exc:
        raise GateBlocked("FF_RELEASE_DOCUMENT_DURATION_SECONDS must be an integer") from exc
    if not 30 <= duration_seconds <= 600:
        raise GateBlocked("FF_RELEASE_DOCUMENT_DURATION_SECONDS must be 30-600")
    run_payload = {
        "source_id": source_id,
        "topic": os.environ.get("FF_RELEASE_DOCUMENT_TOPIC", "").strip()
        or "发布验收：仅解释有页码证据的结论",
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "generated_background_enabled": os.environ.get(
            "FF_RELEASE_DOCUMENT_GENERATED_BACKGROUND", "1"
        )
        == "1",
    }
    run_key = idempotency_key("document-run")
    first = _object(
        client.expect(
            "POST",
            "/v1/document-video/runs",
            {201},
            payload=run_payload,
            idempotency=run_key,
        ),
        "document Run",
    )
    replay = _object(
        client.expect(
            "POST",
            "/v1/document-video/runs",
            {201},
            payload=run_payload,
            idempotency=run_key,
        ),
        "document Run replay",
    )
    run_id = first.get("id")
    if not isinstance(run_id, str) or replay.get("id") != run_id:
        raise GateFailure("document Run creation is not idempotent")
    snapshot = first.get("composition_snapshot")
    document = snapshot.get("document_source") if isinstance(snapshot, dict) else None
    if not isinstance(document, dict) or document.get("content_hash") != fixture.sha256:
        raise GateFailure("document Run snapshot is not bound to the immutable source hash")

    final_run, reviews = _review_until_terminal(
        client,
        run_id,
        fixture.sha256,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    final_steps = page_items(
        client.expect("GET", f"/v1/steps?run_id={run_id}&limit=100", {200}).json()
    )
    nodes = {step.get("node_key") for step in final_steps}
    if nodes != EXPECTED_NODES or any(step.get("status") != "succeeded" for step in final_steps):
        raise GateFailure(
            "successful document Run does not contain exactly ten succeeded production steps"
        )
    artifacts = validate_artifact_inventory(
        client.expect("GET", f"/v1/artifacts?run_id={run_id}&limit=100", {200}).json()
    )
    video = artifacts["final.mp4"]
    url = _presigned_artifact_url(client, str(video["id"]))
    maximum_video_bytes = _positive_int(
        "FF_RELEASE_DOCUMENT_MAX_VIDEO_BYTES", str(DEFAULT_MAX_VIDEO_BYTES)
    )
    with tempfile.TemporaryDirectory(prefix="vistora-document-release-") as directory:
        path = Path(directory) / "final.mp4"
        byte_size, sha256, prefix = _download_to(
            url,
            path,
            timeout=transfer_timeout,
            maximum_bytes=maximum_video_bytes,
        )
        if byte_size != video["byte_size"] or sha256 != video["content_hash"]:
            raise GateFailure("downloaded MP4 differs from durable size/hash metadata")
        if b"ftyp" not in prefix[:32]:
            raise GateFailure("downloaded artifact has no MP4 file-type box")
        probe = _probe_video(path, aspect_ratio)
    quality = _inline_json(client, artifacts["quality-report.json"])
    try:
        reviewed_duration = float(quality.get("duration_seconds"))
    except (TypeError, ValueError) as exc:
        raise GateFailure("reviewed quality evidence has no numeric duration") from exc
    if abs(reviewed_duration - probe["duration_seconds"]) > 0.2:
        raise GateFailure("downloaded MP4 duration differs from reviewed quality evidence")
    events = _event_evidence(client, run_id)
    purge = None
    if os.environ.get("FF_RELEASE_DOCUMENT_VERIFY_PURGE") == "1":
        revision = completed.get("revision")
        if not isinstance(revision, int):
            raise GateFailure("completed document source has no integer revision")
        purge = _verify_retention_and_purge(
            client,
            source_id=source_id,
            source_revision=revision,
            run_id=run_id,
            artifact_ids=[str(item["id"]) for item in artifacts.values()],
            timeout=timeout,
            poll_interval=poll_interval,
        )

    return {
        "api_url": client.base_url,
        "persistence": persistence,
        "source_id": source_id,
        "source_sha256": fixture.sha256,
        "run_id": run_id,
        "run_status": final_run.get("status"),
        "reviews": reviews,
        "video_artifact_id": video["id"],
        "video_sha256": sha256,
        "video_byte_size": byte_size,
        "probe": probe,
        "events": events,
        "purge": purge,
    }


if __name__ == "__main__":
    gate_main(run_gate)
