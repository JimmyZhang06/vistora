"""1k/5k asset pagination, bulk-operation, and queue-backpressure gate.

This gate runs only against a dedicated disposable workspace populated by a
repeatable seed manifest. It never seeds through direct production-table writes.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from asset_e2e_gate import require_disposable_context, walk_asset_pages
from release_gate_common import (
    ApiClient,
    GateBlocked,
    GateFailure,
    gate_main,
    idempotency_key,
    load_json,
)


@dataclass(frozen=True, slots=True)
class QueueObservation:
    status: int
    depth: int | None
    retry_after: str | None = None
    operation: str = "enqueue"


def assert_backpressure(
    observations: list[QueueObservation], *, high_watermark: int, low_watermark: int
) -> None:
    if not 0 <= low_watermark < high_watermark:
        raise GateFailure("queue low watermark must be below the high watermark")
    if not observations:
        raise GateFailure("queue backpressure probe produced no observations")
    if any(observation.status >= 500 for observation in observations):
        raise GateFailure("queue overload produced a server error")
    if any(
        observation.depth is not None and observation.depth > high_watermark
        for observation in observations
    ):
        raise GateFailure("queue depth exceeded its configured high watermark")
    enqueues = [observation for observation in observations if observation.operation == "enqueue"]
    rejected = [observation for observation in enqueues if observation.status == 429]
    if not rejected:
        raise GateFailure("queue did not apply backpressure at its high watermark")
    first_rejection = enqueues.index(rejected[0])
    accepted_before = enqueues[:first_rejection]
    if len(accepted_before) != high_watermark:
        raise GateFailure("queue applied backpressure before or after its exact high watermark")
    if any(
        observation.status not in {200, 201, 202} or observation.depth is None
        for observation in accepted_before
    ):
        raise GateFailure("queue did not report every accepted depth before the high watermark")
    if accepted_before[-1].depth != high_watermark:
        raise GateFailure("last accepted queue depth did not equal the high watermark")
    if any(observation.depth != high_watermark for observation in rejected):
        raise GateFailure("overload response did not preserve the high-watermark depth")
    if any(not observation.retry_after for observation in rejected):
        raise GateFailure("queue overload response omitted Retry-After")
    first_observation_rejection = observations.index(rejected[0])
    recovered_index = next(
        (
            index
            for index, observation in enumerate(
                observations[first_observation_rejection + 1 :], first_observation_rejection + 1
            )
            if observation.operation == "status"
            and observation.depth is not None
            and observation.depth <= low_watermark
        ),
        None,
    )
    if recovered_index is None:
        raise GateFailure("queue did not drain below its low watermark")
    if not any(
        observation.operation == "enqueue" and observation.status in {200, 201, 202}
        for observation in observations[recovered_index + 1 :]
    ):
        raise GateFailure("queue did not accept work after draining below its low watermark")


def _query(profile: Mapping[str, Any]) -> str:
    filters = profile.get("filters", {})
    if not isinstance(filters, dict):
        raise GateBlocked("load profile filters must be an object")
    return "/v1/assets" + (f"?{urlencode(filters, doseq=True)}" if filters else "")


def _ids_digest(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _assert_fixture_records(
    records: list[dict[str, Any]],
    *,
    workspace_id: str,
    fixture_id: str,
    filters: Mapping[str, Any],
) -> None:
    exact_fields = {"library_id", "kind", "status", "copyright_status"}
    for record in records:
        if str(record.get("workspace_id")) != workspace_id:
            raise GateFailure("load query returned a cross-workspace asset")
        metadata = record.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("release_fixture_id") != fixture_id:
            raise GateFailure("load query returned an asset outside the exact release fixture")
        for field in exact_fields.intersection(filters):
            if str(record.get(field)) != str(filters[field]):
                raise GateFailure(f"asset filter {field!r} was not enforced")
        search = filters.get("search")
        if search:
            searchable = " ".join(
                str(record.get(field, "")) for field in ("title", "description", "tags")
            ).casefold()
            if str(search).casefold() not in searchable:
                raise GateFailure("asset search filter was not enforced")


def _poll_bulk_job(client: ApiClient, job_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.expect("GET", f"/v1/asset-bulk-jobs/{job_id}", {200})
        value = response.json()
        if not isinstance(value, dict):
            raise GateFailure("bulk job response must be an object")
        last = value
        if value.get("status") == "completed":
            return value
        if value.get("status") in {"failed", "cancelled"}:
            raise GateFailure(f"bulk job {job_id} reached {value.get('status')}")
        time.sleep(1)
    raise GateFailure(f"bulk job {job_id} timed out; last={last}")


def run_gate() -> dict[str, Any]:
    if os.environ.get("FF_RELEASE_ASSET_TEST_WORKSPACE") != "1":
        raise GateBlocked("load gate requires FF_RELEASE_ASSET_TEST_WORKSPACE=1")
    manifest = load_json(os.environ.get("FF_RELEASE_ASSET_LOAD_MANIFEST", ""))
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise GateBlocked("load manifest must contain 1k/5k profiles")
    expected_sizes = {int(profile.get("expected_total", 0)) for profile in profiles}
    if not {1000, 5000}.issubset(expected_sizes):
        raise GateBlocked("load manifest must include exact 1000 and 5000 record profiles")
    client = ApiClient.from_environment()
    disposable = require_disposable_context(client)
    fixture_id = str(manifest.get("fixture_id") or "")
    if not fixture_id or manifest.get("workspace_id") != disposable["workspace_id"]:
        raise GateBlocked("load manifest workspace/fixture identity does not match API context")
    if manifest.get("ephemeral_scope") != disposable["scope"]:
        raise GateBlocked("load manifest does not match the ephemeral scope marker")
    results: list[dict[str, Any]] = []
    largest_ids: list[str] = []
    for profile in profiles:
        expected = int(profile["expected_total"])
        started = time.monotonic()
        records = walk_asset_pages(client, query=_query(profile), expected_total=expected)
        elapsed = time.monotonic() - started
        ids = [str(record["id"]) for record in records]
        filters = profile.get("filters", {})
        if not isinstance(filters, dict) or filters.get("release_fixture_id") != fixture_id:
            raise GateBlocked("every load profile must filter by the exact release_fixture_id")
        _assert_fixture_records(
            records,
            workspace_id=disposable["workspace_id"],
            fixture_id=fixture_id,
            filters=filters,
        )
        if _ids_digest(ids) != profile.get("expected_ids_sha256"):
            raise GateFailure("load profile IDs differ from the deterministic seed manifest")
        hard_limit = 5.0 if expected == 1000 else 20.0
        threshold = float(profile.get("max_elapsed_seconds", hard_limit))
        if threshold <= 0 or threshold > hard_limit:
            raise GateBlocked(f"load profile timeout must be within (0,{hard_limit}]")
        if elapsed > threshold:
            raise GateFailure(
                f"{expected}-asset pagination exceeded {threshold}s: {elapsed:.3f}s"
            )
        if expected == 5000:
            largest_ids = ids
        results.append(
            {
                "name": profile.get("name", str(expected)),
                "records": len(ids),
                "elapsed_seconds": round(elapsed, 3),
            }
        )

    queue = manifest.get("queue", {})
    high = 1000
    low = 750
    if queue.get("high_watermark") != high or queue.get("low_watermark") != low:
        raise GateBlocked("load manifest must use high=1000 and low=750")
    queue_status = client.expect("GET", "/v1/asset-analysis-queue", {200}).json()
    if not isinstance(queue_status, dict):
        raise GateFailure("analysis queue status must be an object")
    if (
        queue_status.get("namespace") != disposable["scope"]
        or queue_status.get("depth") != 0
        or queue_status.get("consumers_paused") is not True
        or queue_status.get("high_watermark") != high
        or queue_status.get("low_watermark") != low
    ):
        raise GateBlocked("analysis queue is not an empty, paused, isolated gate namespace")
    observations: list[QueueObservation] = []
    for asset_id in largest_ids:
        response = client.request(
            "POST",
            "/v1/asset-analysis-jobs",
            payload={"asset_id": asset_id, "analysis_version": 2},
            idempotency=idempotency_key("analysis-load"),
        )
        depth_value = response.headers.get("x-queue-depth")
        observations.append(
            QueueObservation(
                response.status,
                int(depth_value) if depth_value and depth_value.isdigit() else None,
                response.headers.get("retry-after"),
            )
        )
        if response.status not in {200, 201, 202, 429}:
            raise GateFailure(f"analysis enqueue returned HTTP {response.status}")
    if any(observation.status == 429 for observation in observations):
        client.expect(
            "POST",
            "/v1/asset-analysis-queue/resume",
            {200, 202, 204},
            idempotency=idempotency_key("queue-resume"),
        )
        deadline = time.monotonic() + float(
            os.environ.get("FF_RELEASE_ASSET_QUEUE_DRAIN_TIMEOUT_SECONDS", "600")
        )
        while time.monotonic() < deadline:
            queue_status = client.expect("GET", "/v1/asset-analysis-queue", {200}).json()
            if not isinstance(queue_status, dict) or not isinstance(queue_status.get("depth"), int):
                raise GateFailure("analysis queue status omitted integer depth")
            depth = int(queue_status["depth"])
            observations.append(QueueObservation(200, depth, operation="status"))
            if depth <= low:
                retry = client.request(
                    "POST",
                    "/v1/asset-analysis-jobs",
                    payload={"asset_id": largest_ids[-1], "analysis_version": 2},
                    idempotency=idempotency_key("analysis-recovery"),
                )
                retry_depth = retry.headers.get("x-queue-depth")
                observations.append(
                    QueueObservation(
                        retry.status,
                        int(retry_depth) if retry_depth and retry_depth.isdigit() else None,
                        retry.headers.get("retry-after"),
                    )
                )
                break
            time.sleep(1)
    assert_backpressure(observations, high_watermark=high, low_watermark=low)
    drain_deadline = time.monotonic() + float(
        os.environ.get("FF_RELEASE_ASSET_QUEUE_DRAIN_TIMEOUT_SECONDS", "600")
    )
    while time.monotonic() < drain_deadline:
        final_queue = client.expect("GET", "/v1/asset-analysis-queue", {200}).json()
        if not isinstance(final_queue, dict):
            raise GateFailure("analysis queue status must be an object")
        if final_queue.get("depth") == 0 and final_queue.get("inflight") == 0:
            break
        time.sleep(1)
    else:
        raise GateFailure("analysis queue did not fully drain before the bulk operation")

    bulk = client.expect(
        "POST",
        "/v1/assets/bulk-actions",
        {202},
        payload={
            "asset_ids": largest_ids,
            "action": "quarantine",
            "reason": "release-load-gate",
        },
        idempotency=idempotency_key("bulk-5k"),
    ).json()
    if not isinstance(bulk, dict) or not isinstance(bulk.get("job_id"), str):
        raise GateFailure("5k bulk action did not return an asynchronous job_id")
    completed = _poll_bulk_job(
        client,
        bulk["job_id"],
        float(os.environ.get("FF_RELEASE_ASSET_BULK_TIMEOUT_SECONDS", "600")),
    )
    if completed.get("processed_count") != 5000 or completed.get("failed_count") != 0:
        raise GateFailure(f"5k bulk action was incomplete: {completed}")
    if completed.get("audit_action_count") != 5000:
        raise GateFailure("5k bulk action did not persist one audit action per asset")
    verified = walk_asset_pages(
        client,
        query=_query(
            {
                "filters": {
                    "release_fixture_id": fixture_id,
                    "status": "quarantined",
                }
            }
        ),
        expected_total=5000,
    )
    verified_ids = [str(record["id"]) for record in verified]
    if _ids_digest(verified_ids) != _ids_digest(largest_ids):
        raise GateFailure("5k bulk action changed the wrong asset set")
    return {
        "profiles": results,
        "bulk": {"processed": completed.get("processed_count")},
        "backpressure": {
            "observations": len(observations),
            "rejections": sum(item.status == 429 for item in observations),
            "high_watermark": high,
            "low_watermark": low,
        },
    }


if __name__ == "__main__":
    gate_main(run_gate)
