"""Destructive-only-in-a-dedicated-workspace asset lifecycle release gate.

The fixture is JSON with ``image``, ``video`` and ``corrupt`` entries. Each entry
contains ``path``, ``filename``, ``kind`` and ``content_type``. Paths are resolved
relative to the fixture. ``run_payload`` is an optional JSON object in which
``${ASSET_LIBRARY_ID}`` and ``${ASSET_IDS}`` are replaced before Run creation.

The gate refuses to run unless FF_RELEASE_ASSET_TEST_WORKSPACE=1. It never reads
or mutates a user's existing asset; every resource uses a unique gate nonce.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from release_gate_common import (
    ApiClient,
    GateBlocked,
    GateFailure,
    gate_main,
    idempotency_key,
    load_json,
    page_items,
    require_env,
    wait_for_run_status,
)

EPHEMERAL_SCOPE_RE = re.compile(r"^ff-assets-[a-z0-9-]{8,64}$")


def _object(value: Any, label: str) -> dict[str, Any]:
    result = value.json() if hasattr(value, "json") else value
    if not isinstance(result, dict):
        raise GateFailure(f"{label} must be a JSON object")
    return result


def _fixture_bytes(fixture_path: Path, value: Mapping[str, Any]) -> bytes:
    relative = value.get("path")
    if not isinstance(relative, str) or not relative:
        raise GateBlocked("each media fixture must declare a relative path")
    path = (fixture_path.parent / relative).resolve()
    if not path.is_relative_to(fixture_path.parent.resolve()) or not path.is_file():
        raise GateBlocked(f"media fixture is absent or escapes its directory: {relative}")
    return path.read_bytes()


def require_disposable_context(client: ApiClient) -> dict[str, Any]:
    expected_workspace = require_env("FF_RELEASE_WORKSPACE_ID")
    scope = require_env("FF_RELEASE_ASSET_EPHEMERAL_SCOPE")
    if not EPHEMERAL_SCOPE_RE.fullmatch(scope):
        raise GateBlocked("ephemeral scope must match ff-assets-[a-z0-9-]{8,64}")
    context = _object(client.expect("GET", "/v1/context", {200}), "current context")
    if str(context.get("workspace_id")) != expected_workspace:
        raise GateBlocked(
            "API resolved a different workspace than FF_RELEASE_WORKSPACE_ID; refusing writes"
        )
    if scope not in str(context.get("workspace_name", "")).casefold():
        raise GateBlocked("resolved workspace name does not contain the ephemeral scope marker")
    health = _object(client.expect("GET", "/healthz", {200}), "health")
    if health.get("persistence") != "postgresql":
        raise GateBlocked("asset live gates require disposable PostgreSQL persistence")
    return {"workspace_id": expected_workspace, "scope": scope}


def _put_signed(upload: Mapping[str, Any], data: bytes) -> None:
    url = upload.get("url")
    method = upload.get("method")
    headers = upload.get("headers")
    if not isinstance(url, str) or method != "PUT" or not isinstance(headers, dict):
        raise GateFailure("signed upload descriptor is incomplete")
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status not in {200, 201, 204}:
                raise GateFailure(f"signed PUT returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise GateFailure(f"signed PUT returned HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateBlocked(f"cannot reach signed object upload URL: {exc}") from exc


def _create_upload(
    client: ApiClient,
    library_id: str,
    fixture: Mapping[str, Any],
    data: bytes,
    *,
    copyright_status: str,
    nonce: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        "library_id": library_id,
        "filename": str(fixture["filename"]),
        "title": f"release-gate-{nonce}-{fixture['kind']}",
        "description": "ephemeral release verification fixture",
        "kind": str(fixture["kind"]),
        "content_type": str(fixture["content_type"]),
        "byte_size": len(data),
        "sha256": digest,
        "copyright_status": copyright_status,
        "tags": ["release-gate", nonce],
    }
    initiated = _object(
        client.expect(
            "POST",
            "/v1/asset-uploads",
            {200, 201, 202},
            payload=payload,
            idempotency=idempotency_key(f"asset-{fixture['kind']}"),
        ),
        "create asset upload",
    )
    upload = initiated.get("upload")
    if not isinstance(upload, dict):
        raise GateFailure("create asset upload omitted its signed upload descriptor")
    _put_signed(upload, data)
    file_value = initiated.get("file")
    if not isinstance(file_value, dict):
        raise GateFailure("create asset upload omitted its durable file descriptor")
    completed = _object(
        client.expect(
            "POST",
            f"/v1/asset-uploads/{initiated['id']}/complete",
            {200, 202},
            payload={
                "object_key": file_value["object_key"],
                "sha256": digest,
                "content_type": payload["content_type"],
            },
            idempotency=idempotency_key("complete"),
        ),
        "complete asset upload",
    )
    if completed.get("status") == "ready":
        raise GateFailure("upload completion bypassed scan, analysis, and human review")
    return initiated, completed


def _wait_asset(client: ApiClient, asset_id: str, accepted: set[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _object(client.expect("GET", f"/v1/assets/{asset_id}", {200}), "get asset")
        if last.get("status") in accepted:
            return last
        time.sleep(1)
    raise GateFailure(
        f"asset {asset_id} did not reach {sorted(accepted)}; last={last and last.get('status')}"
    )


def _approve_concurrently(client: ApiClient, asset: Mapping[str, Any]) -> dict[str, Any]:
    asset_id = str(asset["id"])
    revision = asset.get("review_revision")
    if not isinstance(revision, int):
        raise GateFailure("reviewable asset omitted integer review_revision")
    payload = {
        "action": "approved",
        "expected_revision": revision,
        "comment": "release gate concurrent approval",
    }

    def submit(label: str) -> Any:
        return client.request(
            "POST",
            f"/v1/assets/{asset_id}/review",
            payload=payload,
            idempotency=f"release-asset-review-{label}-{uuid4()}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, ("a", "b")))
    successes = [response for response in responses if response.status in {200, 201, 202}]
    conflicts = [response for response in responses if response.status == 409]
    if len(successes) != 1 or len(conflicts) != 1:
        raise GateFailure(
            "concurrent review CAS must produce exactly one success and one HTTP 409; "
            f"received {[response.status for response in responses]}"
        )
    return _object(successes[0], "review response")


def walk_asset_pages(
    client: ApiClient,
    *,
    query: str,
    expected_total: int,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    items: list[dict[str, Any]] = []
    while True:
        separator = "&" if "?" in query else "?"
        path = f"{query}{separator}limit={page_size}"
        if cursor:
            path += f"&{urlencode({'cursor': cursor})}"
        page = _object(client.expect("GET", path, {200}), "asset page")
        page_values = page_items(page)
        if len(page_values) > page_size:
            raise GateFailure("asset page exceeded its requested limit")
        items.extend(page_values)
        metadata = page.get("page")
        if not isinstance(metadata, dict):
            raise GateFailure("asset page omitted cursor metadata")
        if page.get("total_count") != expected_total:
            raise GateFailure("asset pagination total_count changed during traversal")
        cursor = metadata.get("next_cursor")
        if not metadata.get("has_more"):
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise GateFailure("asset pagination cursor is missing or loops")
        seen_cursors.add(cursor)
    ids = [item.get("id") for item in items]
    if len(items) != expected_total or len(set(ids)) != expected_total:
        raise GateFailure("asset pagination skipped or duplicated records")
    return items


def _replace(value: Any, library_id: str, asset_ids: list[str]) -> Any:
    if value == "${ASSET_LIBRARY_ID}":
        return library_id
    if value == "${ASSET_IDS}":
        return asset_ids
    if isinstance(value, dict):
        return {key: _replace(item, library_id, asset_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, library_id, asset_ids) for item in value]
    return value


def run_gate() -> dict[str, Any]:
    if os.environ.get("FF_RELEASE_ASSET_TEST_WORKSPACE") != "1":
        raise GateBlocked(
            "set FF_RELEASE_ASSET_TEST_WORKSPACE=1 only for a dedicated disposable workspace"
        )
    fixture_path = Path(require_env("FF_RELEASE_ASSET_FIXTURE_FILE")).resolve()
    fixture = load_json(str(fixture_path))
    for name in ("image", "video", "corrupt"):
        if not isinstance(fixture.get(name), dict):
            raise GateBlocked(f"asset fixture must define object {name!r}")
    client = ApiClient.from_environment()
    disposable = require_disposable_context(client)
    nonce = uuid4().hex[:12]
    library = _object(
        client.expect(
            "POST",
            "/v1/asset-libraries",
            {200, 201},
            payload={
                "name": f"release-gate-{nonce}",
                "slug": f"release-gate-{nonce}",
                "description": "disposable release verification library",
            },
            idempotency=idempotency_key("library"),
        ),
        "create asset library",
    )
    library_id = str(library["id"])

    accepted_assets: list[dict[str, Any]] = []
    media_bytes: dict[str, bytes] = {}
    unsafe_asset_ids: list[str] = []
    for name in ("image", "video"):
        media = fixture[name]
        data = _fixture_bytes(fixture_path, media)
        media_bytes[name] = data
        initiated, _ = _create_upload(
            client, library_id, media, data, copyright_status="owned", nonce=nonce
        )
        reviewable = _wait_asset(
            client,
            str(initiated["id"]),
            {"awaiting_review", "quarantined"},
            float(os.environ.get("FF_RELEASE_ASSET_ANALYSIS_TIMEOUT_SECONDS", "600")),
        )
        if reviewable.get("status") != "awaiting_review":
            raise GateFailure(f"valid {name} fixture was quarantined before human review")
        approved = _approve_concurrently(client, reviewable) if name == "image" else _object(
            client.expect(
                "POST",
                f"/v1/assets/{initiated['id']}/review",
                {200, 201, 202},
                payload={
                    "action": "approved",
                    "expected_revision": reviewable.get("review_revision"),
                    "comment": "release gate approval",
                },
                idempotency=idempotency_key("review"),
            ),
            "review asset",
        )
        if approved.get("status") != "ready":
            approved = _wait_asset(client, str(initiated["id"]), {"ready"}, 60)
        file_value = approved.get("file")
        analysis = approved.get("analysis")
        review = approved.get("review")
        if not isinstance(file_value, dict) or file_value.get("scan_status") != "clean":
            raise GateFailure("approved asset is ready without a clean scan")
        if not isinstance(analysis, dict) or analysis.get("status") != "completed":
            raise GateFailure("approved asset is ready without completed analysis")
        if not isinstance(review, dict) or review.get("action") != "approved":
            raise GateFailure("approved asset is ready without a durable approval")
        if not isinstance(analysis.get("id"), str) or not isinstance(
            analysis.get("analysis_version"), int
        ):
            raise GateFailure("completed analysis omitted immutable identity/version")
        if not isinstance(review.get("id"), str) or not isinstance(
            review.get("review_revision"), int
        ):
            raise GateFailure("approval omitted immutable identity/revision")
        accepted_assets.append(approved)

    corrupt = fixture["corrupt"]
    corrupt_data = _fixture_bytes(fixture_path, corrupt)
    corrupt_asset, _ = _create_upload(
        client, library_id, corrupt, corrupt_data, copyright_status="owned", nonce=nonce
    )
    unsafe_asset_ids.append(str(corrupt_asset["id"]))
    _wait_asset(client, str(corrupt_asset["id"]), {"quarantined"}, 600)

    unknown_payload = deepcopy(fixture["image"])
    unknown_data = _fixture_bytes(fixture_path, unknown_payload) + b"\nunknown-rights-fixture"
    digest = hashlib.sha256(unknown_data).hexdigest()
    unknown_response = client.request(
        "POST",
        "/v1/asset-uploads",
        payload={
            "library_id": library_id,
            "filename": f"unknown-{unknown_payload['filename']}",
            "title": f"release-gate-{nonce}-unknown",
            "kind": unknown_payload["kind"],
            "content_type": unknown_payload["content_type"],
            "byte_size": len(unknown_data),
            "sha256": digest,
            "copyright_status": "unknown",
        },
        idempotency=idempotency_key("unknown"),
    )
    if unknown_response.status not in {200, 201, 202, 422}:
        raise GateFailure(f"unknown-rights upload returned unsafe HTTP {unknown_response.status}")
    if unknown_response.status != 422:
        unknown = _object(unknown_response, "unknown-rights upload")
        unsafe_asset_ids.append(str(unknown["id"]))
        if unknown.get("status") == "ready":
            raise GateFailure("unknown-rights asset became ready")
        upload = unknown.get("upload")
        file_value = unknown.get("file")
        if not isinstance(upload, dict) or not isinstance(file_value, dict):
            raise GateFailure("accepted unknown-rights upload omitted its signed descriptor")
        _put_signed(upload, unknown_data)
        completed_unknown = _object(
            client.expect(
                "POST",
                f"/v1/asset-uploads/{unknown['id']}/complete",
                {200, 202},
                payload={
                    "object_key": file_value["object_key"],
                    "sha256": digest,
                    "content_type": unknown_payload["content_type"],
                },
                idempotency=idempotency_key("complete-unknown"),
            ),
            "complete unknown-rights upload",
        )
        if completed_unknown.get("status") == "ready":
            raise GateFailure("unknown-rights asset became ready after upload completion")
        _wait_asset(client, str(unknown["id"]), {"quarantined"}, 600)

    original_image = media_bytes["image"]
    original_image_digest = hashlib.sha256(original_image).hexdigest()
    duplicate = client.request(
        "POST",
        "/v1/asset-uploads",
        payload={
            "library_id": library_id,
            "filename": fixture["image"]["filename"],
            "title": "duplicate content",
            "kind": fixture["image"]["kind"],
            "content_type": fixture["image"]["content_type"],
            "byte_size": len(original_image),
            "sha256": original_image_digest,
            "copyright_status": "owned",
        },
        idempotency=idempotency_key("duplicate"),
    )
    if duplicate.status not in {200, 201, 409}:
        raise GateFailure(f"duplicate upload returned HTTP {duplicate.status}")
    if (
        duplicate.status in {200, 201}
        and _object(duplicate, "duplicate upload").get("id") != accepted_assets[0].get("id")
    ):
        raise GateFailure("duplicate content created a second asset identity")

    oversized = client.request(
        "POST",
        "/v1/asset-uploads",
        payload={
            "library_id": library_id,
            "filename": "oversized.mp4",
            "title": "oversized fixture",
            "kind": "video",
            "content_type": "video/mp4",
            "byte_size": 524_288_001,
            "sha256": "f" * 64,
            "copyright_status": "owned",
        },
        idempotency=idempotency_key("oversized"),
    )
    if oversized.status not in {413, 422}:
        raise GateFailure(f"oversized declaration was not rejected: HTTP {oversized.status}")

    other_workspace = require_env("FF_RELEASE_ASSET_OTHER_WORKSPACE_ID")
    if other_workspace == require_env("FF_RELEASE_WORKSPACE_ID"):
        raise GateBlocked("cross-workspace gate requires two different workspace IDs")
    other = ApiClient(client.base_url, timeout=client.timeout)
    other.headers.update(client.headers)
    other.headers["X-Workspace-Id"] = other_workspace
    leaked = other.request("GET", f"/v1/assets/{accepted_assets[0]['id']}")
    if leaked.status not in {403, 404}:
        raise GateFailure(f"cross-workspace asset read returned HTTP {leaked.status}")

    run_payload = fixture.get("run_payload")
    if not isinstance(run_payload, dict):
        raise GateBlocked("asset fixture must include run_payload for Worker/Run snapshot verification")
    run = _object(
        client.expect(
            "POST",
            "/v1/runs",
            {200, 201},
            payload=_replace(
                deepcopy(run_payload),
                library_id,
                [str(asset["id"]) for asset in accepted_assets],
            ),
            idempotency=idempotency_key("asset-run"),
        ),
        "create asset-backed run",
    )
    run_id = str(run["id"])
    final_run = wait_for_run_status(client, run_id, {"succeeded"}, timeout=900)
    snapshot = final_run.get("asset_snapshot")
    if not isinstance(snapshot, dict):
        raise GateFailure("Run omitted its immutable asset_snapshot")
    snapshot_assets = snapshot.get("assets")
    if not isinstance(snapshot_assets, list) or not snapshot_assets:
        raise GateFailure("Run asset_snapshot omitted selected asset records")
    selected_ids = {
        str(item.get("asset_id")) for item in snapshot_assets if isinstance(item, dict)
    }
    if any(asset_id in selected_ids for asset_id in unsafe_asset_ids):
        raise GateFailure("Run snapshot references quarantined or unknown-rights material")
    approved_ids = {str(asset["id"]) for asset in accepted_assets}
    if not selected_ids.issubset(approved_ids):
        raise GateFailure("Run snapshot references an asset outside the approved gate set")
    required_snapshot_fields = {
        "asset_id",
        "library_id",
        "content_hash",
        "analysis_id",
        "analysis_version",
        "review_id",
        "review_revision",
        "copyright_status",
    }
    if any(
        not isinstance(item, dict) or not required_snapshot_fields.issubset(item)
        for item in snapshot_assets
    ):
        raise GateFailure("Run asset snapshot lacks immutable provenance/review fields")
    expected_snapshots = {
        str(asset["id"]): {
            "asset_id": str(asset["id"]),
            "library_id": str(asset["library_id"]),
            "content_hash": str(asset["file"]["content_hash"]),
            "analysis_id": str(asset["analysis"]["id"]),
            "analysis_version": asset["analysis"]["analysis_version"],
            "review_id": str(asset["review"]["id"]),
            "review_revision": asset["review"]["review_revision"],
            "copyright_status": asset["copyright_status"],
        }
        for asset in accepted_assets
    }
    for item in snapshot_assets:
        expected = expected_snapshots[str(item["asset_id"])]
        actual = {field: item.get(field) for field in required_snapshot_fields}
        if actual != expected:
            raise GateFailure(
                f"Run snapshot provenance differs from approved asset {item['asset_id']}"
            )

    artifacts = page_items(
        client.expect("GET", f"/v1/artifacts?run_id={run_id}&limit=100", {200}).json()
    )
    if not artifacts:
        raise GateFailure("asset-backed Run produced no traceable artifacts")
    for asset in accepted_assets:
        client.expect("DELETE", f"/v1/assets/{asset['id']}", {200, 202, 204})
    after_delete = _object(client.expect("GET", f"/v1/runs/{run_id}", {200}), "historical Run")
    if after_delete.get("asset_snapshot") != snapshot:
        raise GateFailure("soft deletion changed the historical Run asset snapshot")
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        if isinstance(artifact_id, str):
            client.expect("GET", f"/v1/artifacts/{artifact_id}/content", {200, 302, 307})

    return {
        "library_id": library_id,
        "approved_asset_ids": [asset["id"] for asset in accepted_assets],
        "unsafe_asset_ids": unsafe_asset_ids,
        "run_id": run_id,
        "artifact_count": len(artifacts),
        "cross_workspace": "isolated",
        "concurrent_review": "cas-verified",
        "history_after_soft_delete": "readable",
        "ephemeral_scope": disposable["scope"],
    }


if __name__ == "__main__":
    gate_main(run_gate)
