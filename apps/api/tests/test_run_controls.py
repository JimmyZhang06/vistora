from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_USER_ID, DEVELOPMENT_WORKSPACE_ID
from framefactory_api.storage import ObjectLocator, PresignedRequest


class DownloadStorage:
    def __init__(self) -> None:
        self.requests: list[tuple[UUID, ObjectLocator]] = []
        self.inline_content = b'{"title":"visible preview"}'

    async def presign_download(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        del expires_in
        self.requests.append((workspace_id, object))
        return PresignedRequest(
            method="GET",
            url="https://objects.example.test/private-video?signed=1",
            headers={},
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            object=object,
        )

    async def read_bytes(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        maximum_bytes: int,
    ) -> bytes:
        self.requests.append((workspace_id, object))
        assert len(self.inline_content) <= maximum_bytes
        return self.inline_content


def _run(status: str = "queued") -> dict:
    resource = contract_example("run.schema.json")
    resource["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    resource["created_by"] = str(DEVELOPMENT_USER_ID)
    resource["status"] = status
    return resource


def _step(run: dict, key: str, *, status: str, review_required: bool = False) -> dict:
    timestamp = run["created_at"]
    return {
        "schema_version": "1.0.0",
        "id": f"{run['id']}:{key}",
        "workspace_id": run["workspace_id"],
        "run_id": run["id"],
        "node_key": key,
        "operation": f"quality.{key}",
        "status": status,
        "queue_name": "run-steps",
        "required_capabilities": [],
        "input_snapshot": run["input"],
        "output_summary": None,
        "output_artifacts": [],
        "dependencies": [],
        "attempt": 1,
        "maximum_attempts": 3,
        "error": None,
        "review_required": review_required,
        "review": (
            {
                "decision": None,
                "actor_id": None,
                "comment": None,
                "requested_at": timestamp,
                "decided_at": None,
            }
            if review_required
            else None
        ),
        "lease_owner": "worker-1" if status == "running" else None,
        "lease_expires_at": timestamp if status == "running" else None,
        "heartbeat_at": timestamp if status == "running" else None,
        "next_attempt_at": timestamp,
        "cancellation_requested_at": None,
        "created_at": timestamp,
        "started_at": timestamp if status in {"running", "awaiting_review"} else None,
        "finished_at": None,
        "updated_at": timestamp,
        "revision": 2,
    }


def test_review_gate_is_listed_and_approval_is_idempotent() -> None:
    run = _run("awaiting_review")
    step = _step(run, "quality", status="awaiting_review", review_required=True)
    repository = InMemoryControlRepository(runs=[run], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        listed = client.get("/v1/steps", params={"run_id": run["id"]})
        first = client.post(
            f"/v1/steps/{step['id']}/review",
            json={
                "decision": "approve",
                "reason": "Ready to publish",
                "issue_codes": [],
                "expected_revision": 2,
            },
            headers={"Idempotency-Key": "approve-quality-0001"},
        )
        replay = client.post(
            f"/v1/steps/{step['id']}/review",
            json={
                "decision": "approve",
                "reason": "Ready to publish",
                "issue_codes": [],
                "expected_revision": 2,
            },
            headers={"Idempotency-Key": "approve-quality-0001"},
        )
        fetched_run = client.get(f"/v1/runs/{run['id']}")

    assert listed.status_code == 200
    assert listed.json()["data"] == [step]
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["status"] == "succeeded"
    assert first.json()["review"]["decision"] == "approve"
    assert first.json()["review"]["reviewed_revision"] == 2
    assert first.json()["review"]["issue_codes"] == []
    assert fetched_run.json()["status"] == "succeeded"


def test_editorial_draft_cannot_be_approved_before_material_replacement() -> None:
    run = _run("awaiting_review")
    retrieve = _step(run, "retrieve", status="succeeded")
    retrieve["operation"] = "media.retrieve"
    retrieve["output_summary"] = {
        "visual_source_mode": "editorial_fallback",
        "draft": True,
        "replacement_required": True,
    }
    quality = _step(run, "quality", status="awaiting_review", review_required=True)
    repository = InMemoryControlRepository(runs=[run], run_steps=[retrieve, quality])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/steps/{quality['id']}/review",
            json={"decision": "approve", "expected_revision": 2},
            headers={"Idempotency-Key": "approve-editorial-draft-0001"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "EDITORIAL_DRAFT_REPLACEMENT_REQUIRED"
    assert response.json()["details"] == {
        "run_id": run["id"],
        "replacement_path": f"/create?replace={run['id']}",
    }
@pytest.mark.parametrize(
    ("decision", "attempt", "expected_status", "stored_decision", "error_code"),
    [
        ("reject", 1, "failed", "reject", "review_rejected"),
        ("revise", 1, "retrying", "request_changes", "review_changes_requested"),
        ("revise", 3, "failed", "request_changes", "review_rejected"),
    ],
)
def test_review_decisions_match_worker_state_machine(
    decision: str,
    attempt: int,
    expected_status: str,
    stored_decision: str,
    error_code: str,
) -> None:
    run = _run("awaiting_review")
    step = _step(run, "quality", status="awaiting_review", review_required=True)
    step["attempt"] = attempt
    repository = InMemoryControlRepository(runs=[run], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/steps/{step['id']}/review",
            json={"decision": decision, "reason": "Human decision"},
            headers={"Idempotency-Key": f"review-{decision}-{attempt}-0001"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == expected_status
    assert response.json()["review"]["decision"] == stored_decision
    assert response.json()["error"]["code"] == error_code


def test_review_rejects_non_reviewable_or_changed_step() -> None:
    run = _run()
    step = _step(run, "research", status="queued")
    repository = InMemoryControlRepository(runs=[run], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/steps/{step['id']}/review",
            json={"decision": "approve"},
            headers={"Idempotency-Key": "approve-non-review-0001"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "STEP_NOT_AWAITING_REVIEW"


def test_review_rejects_stale_evidence_revision() -> None:
    run = _run("awaiting_review")
    step = _step(run, "quality", status="awaiting_review", review_required=True)
    repository = InMemoryControlRepository(runs=[run], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/steps/{step['id']}/review",
            json={
                "decision": "revise",
                "reason": "The visual does not match the narration",
                "issue_codes": ["visual.mismatch"],
                "expected_revision": 1,
            },
            headers={"Idempotency-Key": "review-stale-evidence-0001"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "STEP_STATE_CHANGED"


def test_cancellation_is_idempotent_and_propagates_without_faking_running_step() -> None:
    run = _run("running")
    queued = _step(run, "writing", status="queued")
    running = _step(run, "render", status="running")
    repository = InMemoryControlRepository(runs=[run], run_steps=[queued, running])
    headers = {"Idempotency-Key": "cancel-active-run-0001"}

    with TestClient(create_app(repository=repository)) as client:
        first = client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        replay = client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        listed = client.get("/v1/steps", params={"run_id": run["id"]})

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["status"] == "running"
    by_key = {item["node_key"]: item for item in listed.json()["data"]}
    assert by_key["writing"]["status"] == "cancelled"
    assert by_key["render"]["status"] == "running"
    assert by_key["render"]["cancellation_requested_at"] is not None


def test_step_lookup_is_workspace_scoped_and_unknown_run_is_not_an_empty_page() -> None:
    run = _run()
    step = _step(run, "quality", status="awaiting_review", review_required=True)
    repository = InMemoryControlRepository(runs=[deepcopy(run)], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        missing_run = client.get(
            "/v1/steps", params={"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        )
        missing_step = client.get(
            "/v1/steps/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:quality"
        )

    assert missing_run.status_code == missing_step.status_code == 404


def test_manual_retry_is_idempotent_and_emits_a_sequenced_event() -> None:
    run = _run("failed")
    step = _step(run, "render", status="failed")
    step["error"] = {"code": "provider_error", "message": "failed", "retryable": True}
    step["finished_at"] = run["created_at"]
    downstream = _step(run, "quality", status="cancelled")
    downstream["dependencies"] = ["render"]
    downstream["cancellation_requested_at"] = run["created_at"]
    downstream["finished_at"] = run["created_at"]
    repository = InMemoryControlRepository(runs=[run], run_steps=[step, downstream])
    headers = {"Idempotency-Key": "retry-render-0001"}

    with TestClient(create_app(repository=repository)) as client:
        first = client.post(f"/v1/steps/{step['id']}/retry", headers=headers)
        replay = client.post(f"/v1/steps/{step['id']}/retry", headers=headers)
        events = client.get("/v1/events", params={"run_id": run["id"]})
        listed = client.get("/v1/steps", params={"run_id": run["id"]})

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["status"] == "retrying"
    by_key = {item["node_key"]: item for item in listed.json()["data"]}
    assert by_key["quality"]["status"] == "queued"
    assert by_key["quality"]["cancellation_requested_at"] is None
    assert [event["type"] for event in events.json()["data"]] == ["step.retrying"]
    assert [event["sequence"] for event in events.json()["data"]] == [1]


def test_manual_retry_extends_an_exhausted_automatic_attempt_budget() -> None:
    run = _run("failed")
    step = _step(run, "writing", status="failed")
    step["attempt"] = step["maximum_attempts"] = 3
    step["error"] = {
        "code": "step_permanent_error",
        "message": "provider constraint changed after deployment",
        "retryable": False,
    }
    repository = InMemoryControlRepository(runs=[run], run_steps=[step])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            f"/v1/steps/{step['id']}/retry",
            headers={"Idempotency-Key": "retry-exhausted-writing-0001"},
        )

    assert response.status_code == 202
    assert response.json()["status"] == "retrying"
    assert response.json()["attempt"] == 3
    assert response.json()["maximum_attempts"] == 4


def test_artifact_and_event_queries_are_run_and_workspace_scoped() -> None:
    run = _run()
    artifact = contract_example("artifact.schema.json")
    artifact["workspace_id"] = run["workspace_id"]
    artifact["run_id"] = run["id"]
    artifact["object_key"] = (
        f"workspaces/{run['workspace_id']}/runs/{run['id']}/artifacts/"
        f"{artifact['id']}/{artifact['filename']}"
    )
    event = contract_example("event.schema.json")
    event["workspace_id"] = run["workspace_id"]
    event["run_id"] = run["id"]
    event["correlation_id"] = run["id"]
    repository = InMemoryControlRepository(
        runs=[run], artifacts=[artifact], events=[event]
    )

    with TestClient(create_app(repository=repository)) as client:
        artifacts = client.get("/v1/artifacts", params={"run_id": run["id"]})
        fetched_artifact = client.get(f"/v1/artifacts/{artifact['id']}")
        events = client.get(
            "/v1/events", params={"run_id": run["id"], "after_sequence": 0}
        )
        fetched_event = client.get(f"/v1/events/{event['id']}")
        missing_run = client.get(
            "/v1/artifacts",
            params={"run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        )

    assert artifacts.json()["data"] == [artifact]
    assert fetched_artifact.json() == artifact
    assert events.json()["data"] == [event]
    assert fetched_event.json() == event
    assert missing_run.status_code == 404


def test_artifact_content_redirect_is_scoped_and_uses_durable_descriptor() -> None:
    run = _run("succeeded")
    artifact = contract_example("artifact.schema.json")
    artifact["workspace_id"] = run["workspace_id"]
    artifact["run_id"] = run["id"]
    artifact["object_key"] = (
        f"workspaces/{run['workspace_id']}/runs/{run['id']}/artifacts/"
        f"{artifact['id']}/{artifact['filename']}"
    )
    repository = InMemoryControlRepository(runs=[run], artifacts=[artifact])
    storage = DownloadStorage()

    with TestClient(create_app(repository=repository, object_storage=storage)) as client:
        response = client.get(
            f"/v1/artifacts/{artifact['id']}/content", follow_redirects=False
        )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "https://objects.example.test/private-video?signed=1"
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert storage.requests == [
        (
            DEVELOPMENT_WORKSPACE_ID,
            ObjectLocator(
                key=artifact["object_key"],
                sha256=artifact["content_hash"],
                content_type=artifact["media_type"],
            ),
        )
    ]


def test_json_artifact_content_can_be_read_inline_for_browser_preview() -> None:
    run = _run("succeeded")
    artifact = contract_example("artifact.schema.json")
    storage = DownloadStorage()
    artifact.update(
        {
            "workspace_id": run["workspace_id"],
            "run_id": run["id"],
            "filename": "script.json",
            "kind": "script",
            "media_type": "application/json",
            "byte_size": len(storage.inline_content),
            "content_hash": hashlib.sha256(storage.inline_content).hexdigest(),
        }
    )
    artifact["object_key"] = (
        f"workspaces/{run['workspace_id']}/runs/{run['id']}/artifacts/"
        f"{artifact['id']}/{artifact['filename']}"
    )
    repository = InMemoryControlRepository(runs=[run], artifacts=[artifact])

    with TestClient(create_app(repository=repository, object_storage=storage)) as client:
        response = client.get(
            f"/v1/artifacts/{artifact['id']}/content", params={"inline": "true"}
        )

    assert response.status_code == 200
    assert response.json() == {"title": "visible preview"}
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-sha256"] == artifact["content_hash"]
