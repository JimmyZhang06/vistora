"""Exercise the deployed control API over real HTTP.

This gate intentionally has no in-process/TestClient fallback. It exits 2 (BLOCKED)
when a production capability or required fixture is absent, and exits 1 when a
capability is present but violates a release invariant.
"""

from __future__ import annotations

import os
from typing import Any

from release_gate_common import (
    ApiClient,
    GateBlocked,
    GateFailure,
    changed_run_payload,
    gate_main,
    idempotency_key,
    load_json,
    page_items,
    require_env,
    wait_for_run_status,
)


def _object(response: Any, label: str) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise GateFailure(f"{label} response must be a JSON object")
    return value


def _assert_same_resource(first: dict[str, Any], replay: dict[str, Any], label: str) -> str:
    first_id = first.get("id")
    if not isinstance(first_id, str) or replay.get("id") != first_id:
        raise GateFailure(f"{label} idempotent replay created or returned a different resource")
    return first_id


def _events(client: ApiClient, run_id: str) -> list[dict[str, Any]]:
    response = client.expect("GET", f"/v1/events?run_id={run_id}&limit=100", {200})
    events = page_items(response.json())
    sequences = [event.get("sequence") for event in events]
    if not sequences or any(not isinstance(value, int) for value in sequences):
        raise GateFailure(f"run {run_id} has no durable, sequenced events")
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise GateFailure(f"run {run_id} event sequence is not ordered and unique: {sequences}")
    return events


def _event_types(events: list[dict[str, Any]]) -> set[str]:
    return {
        str(event.get("type") or event.get("event_type"))
        for event in events
        if event.get("type") or event.get("event_type")
    }


def _review_step_path(client: ApiClient, run_id: str) -> str:
    review_steps = page_items(
        client.expect(
            "GET", f"/v1/steps?run_id={run_id}&limit=100", {200}
        ).json()
    )
    awaiting_review = [
        step for step in review_steps if step.get("status") == "awaiting_review"
    ]
    if len(awaiting_review) != 1 or not isinstance(awaiting_review[0].get("id"), str):
        raise GateFailure(
            "review run must expose exactly one addressable awaiting_review step"
        )
    return f"/v1/steps/{awaiting_review[0]['id']}/review"


def run_gate() -> dict[str, Any]:
    client = ApiClient.from_environment()

    health = _object(client.expect("GET", "/healthz", {200}), "health")
    persistence = health.get("persistence")
    if not isinstance(persistence, str) or persistence in {"memory", "in_memory", "process_memory"}:
        raise GateBlocked(
            f"API reports non-durable persistence {persistence!r}; PostgreSQL adapter is required"
        )
    readiness = _object(client.expect("GET", "/readyz", {200}), "readiness")
    if readiness.get("status") not in {"ok", "ready"}:
        raise GateFailure(f"readiness status is not ready: {readiness!r}")

    run_payload = load_json(require_env("FF_RELEASE_RUN_PAYLOAD_FILE"))
    create_key = idempotency_key("create")
    first_response = client.expect(
        "POST", "/v1/runs", {200, 201}, payload=run_payload, idempotency=create_key
    )
    replay_response = client.expect(
        "POST", "/v1/runs", {first_response.status}, payload=run_payload, idempotency=create_key
    )
    run = _object(first_response, "create run")
    replay = _object(replay_response, "create run replay")
    run_id = _assert_same_resource(run, replay, "run creation")

    conflict = client.request(
        "POST",
        "/v1/runs",
        payload=changed_run_payload(run_payload),
        idempotency=create_key,
    )
    if conflict.status != 409:
        raise GateFailure(
            "reusing an idempotency key with a changed request must return HTTP 409; "
            f"received {conflict.status}"
        )
    fetched = _object(client.expect("GET", f"/v1/runs/{run_id}", {200}), "get run")
    if fetched.get("id") != run_id:
        raise GateFailure("GET run returned the wrong resource")

    cancel_key = idempotency_key("cancel")
    cancelled_once_response = client.expect(
        "POST", f"/v1/runs/{run_id}/cancel", {200, 202}, idempotency=cancel_key
    )
    cancelled_twice_response = client.expect(
        "POST",
        f"/v1/runs/{run_id}/cancel",
        {cancelled_once_response.status},
        idempotency=cancel_key,
    )
    cancelled_once = _object(cancelled_once_response, "cancel run")
    cancelled_twice = _object(cancelled_twice_response, "cancel run replay")
    _assert_same_resource(cancelled_once, cancelled_twice, "run cancellation")
    wait_for_run_status(
        client,
        run_id,
        {"cancelled"},
        timeout=float(os.environ.get("FF_RELEASE_CANCEL_TIMEOUT_SECONDS", "120")),
    )
    cancel_types = _event_types(_events(client, run_id))
    if "run.created" not in cancel_types or "run.cancelled" not in cancel_types:
        raise GateFailure(
            f"durable event stream lacks run.created/run.cancelled: {sorted(cancel_types)}"
        )

    review_run_payload = load_json(require_env("FF_RELEASE_REVIEW_RUN_PAYLOAD_FILE"))
    review_action_payload = load_json(require_env("FF_RELEASE_REVIEW_ACTION_PAYLOAD_FILE"))
    review_run = _object(
        client.expect(
            "POST",
            "/v1/runs",
            {200, 201},
            payload=review_run_payload,
            idempotency=idempotency_key("review-run"),
        ),
        "create review run",
    )
    review_run_id = review_run.get("id")
    if not isinstance(review_run_id, str):
        raise GateFailure("review run response has no string id")
    wait_for_run_status(
        client,
        review_run_id,
        {"awaiting_review"},
        timeout=float(os.environ.get("FF_RELEASE_REVIEW_TIMEOUT_SECONDS", "600")),
    )
    review_path = _review_step_path(client, review_run_id)
    review_key = idempotency_key("review")
    reviewed_once_response = client.expect(
        "POST", review_path, {200, 201, 202}, payload=review_action_payload, idempotency=review_key
    )
    reviewed_twice_response = client.expect(
        "POST",
        review_path,
        {reviewed_once_response.status},
        payload=review_action_payload,
        idempotency=review_key,
    )
    reviewed_once = _object(reviewed_once_response, "review action")
    reviewed_twice = _object(reviewed_twice_response, "review action replay")
    _assert_same_resource(reviewed_once, reviewed_twice, "review action")
    review_events = _event_types(_events(client, review_run_id))
    if not any("review" in event_type or "approved" in event_type for event_type in review_events):
        raise GateFailure(f"review action emitted no durable review event: {sorted(review_events)}")

    return {
        "api_url": client.base_url,
        "persistence": persistence,
        "readiness": readiness.get("status"),
        "idempotency": "verified",
        "cancelled_run_id": run_id,
        "reviewed_run_id": review_run_id,
    }


if __name__ == "__main__":
    gate_main(run_gate)
