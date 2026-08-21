from __future__ import annotations

from copy import deepcopy
from typing import Any

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.queue import QueueConnectionError
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings

VERSION_FIELDS = (
    "input_schema", "research_policy", "writing_policy", "visual_policy", "asset_policy",
    "qc_policy", "capability_requirements", "output_contract", "default_pipeline_version_id",
)


def _published_version(client: TestClient) -> dict:
    skill_response = client.post(
        "/v1/skills",
        json={
            "publisher_name": "Run tests",
            "name": "Run Skill",
            "slug": "run-skill",
            "description": "A run fixture",
            "visibility": "private",
        },
        headers={"Idempotency-Key": "create-run-skill-0001"},
    )
    skill = skill_response.json()
    payload = contract_example("skill-version.schema.json")
    payload = {
        "skill_id": skill["id"],
        "version": "1.0.0",
        "test_topics": ["Run creation"],
        **{field: payload[field] for field in VERSION_FIELDS},
    }
    version_response = client.post(
        "/v1/skill-versions",
        json=payload,
        headers={"Idempotency-Key": "create-run-version-0001"},
    )
    version = version_response.json()
    validated = client.post(
        f"/v1/skill-versions/{version['id']}/validate",
        headers={"Idempotency-Key": "validate-run-version-0001", "If-Match": '"1"'},
    )
    assert validated.status_code == 200
    return client.post(
        f"/v1/skill-versions/{version['id']}/publish",
        json={},
        headers={"Idempotency-Key": "publish-run-version-0001", "If-Match": '"2"'},
    ).json()


def _run_payload(version: dict) -> dict:
    return {
        "input": {"topic": "How small teams verify sources"},
        "composition": {
            "skill_version_id": version["id"],
            "pipeline_version_id": "88888888-8888-4888-8888-888888888888",
            "asset_library_ids": [],
        },
    }


def test_run_creation_is_idempotent_and_queryable(client: TestClient) -> None:
    version = _published_version(client)
    payload = _run_payload(version)
    headers = {"Idempotency-Key": "create-run-idempotent-0001"}

    first = client.post("/v1/runs", json=payload, headers=headers)
    second = client.post("/v1/runs", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "queued"

    fetched = client.get(f"/v1/runs/{first.json()['id']}")
    assert fetched.json() == first.json()
    assert client.get("/v1/runs").json()["data"] == [first.json()]


def test_idempotency_key_rejects_a_different_run_payload(client: TestClient) -> None:
    version = _published_version(client)
    payload = _run_payload(version)
    changed = deepcopy(payload)
    changed["input"]["topic"] = "A different request"
    headers = {"Idempotency-Key": "create-run-conflict-0001"}

    assert client.post("/v1/runs", json=payload, headers=headers).status_code == 201
    conflict = client.post("/v1/runs", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_run_requires_published_skill_version(client: TestClient) -> None:
    version = _published_version(client)
    payload = _run_payload(version)
    payload["composition"]["skill_version_id"] = "55555555-5555-4555-8555-555555555555"
    response = client.post(
        "/v1/runs",
        json=payload,
        headers={"Idempotency-Key": "create-run-bad-hash-0001"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_run_estimate_reports_worker_capability_gaps_and_blocks_creation() -> None:
    repository = _repository_with_pipeline()
    settings = Settings(worker_capabilities=())
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)

        estimate = capability_client.post("/v1/runs/estimate", json=payload)
        created = capability_client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "blocked-run-capability-0001"},
        )

    assert estimate.status_code == 200
    assert estimate.json()["capabilities_known"] is True
    assert estimate.json()["capability_gaps"]
    assert {gap["capability"] for gap in estimate.json()["capability_gaps"]} >= {
        "research.collect",
        "writing.compose",
    }
    assert created.status_code == 409
    assert created.json()["code"] == "RUN_CAPABILITY_UNAVAILABLE"


class _CapturingQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.available = True

    async def healthcheck(self) -> None:
        return None

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        if not self.available:
            raise QueueConnectionError("test queue is unavailable")
        self.enqueued.append(command)
        return object(), True


def _repository_with_pipeline() -> InMemoryControlRepository:
    pipeline = contract_example("pipeline.schema.json")
    pipeline["ownership_type"] = "system"
    pipeline["visibility"] = "public_readonly"
    pipeline["status"] = "active"
    pipeline["published_at"] = pipeline["created_at"]
    return InMemoryControlRepository(pipelines=[pipeline])


def test_run_is_dispatched_to_the_worker_intake_queue() -> None:
    repository = _repository_with_pipeline()
    queue = _CapturingQueue()
    with TestClient(create_app(repository=repository, job_queue=queue)) as queued_client:
        version = _published_version(queued_client)
        response = queued_client.post(
            "/v1/runs",
            json=_run_payload(version),
            headers={"Idempotency-Key": "create-run-dispatch-0001"},
        )

    assert response.status_code == 201
    run = response.json()
    assert queue.enqueued == [
        {
            "queue_name": "runs",
            "payload": {
                "workspace_id": run["workspace_id"],
                "run_id": run["id"],
            },
            "deduplication_key": f"run:{run['workspace_id']}:{run['id']}",
            "max_attempts": 5,
        }
    ]


def test_idempotent_replay_retries_dispatch_after_queue_recovery() -> None:
    repository = _repository_with_pipeline()
    queue = _CapturingQueue()
    with TestClient(create_app(repository=repository, job_queue=queue)) as queued_client:
        version = _published_version(queued_client)
        headers = {"Idempotency-Key": "create-run-dispatch-retry-0001"}
        queue.available = False
        unavailable = queued_client.post(
            "/v1/runs", json=_run_payload(version), headers=headers
        )
        queue.available = True
        replay = queued_client.post(
            "/v1/runs", json=_run_payload(version), headers=headers
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "RUN_DISPATCH_UNAVAILABLE"
    assert replay.status_code == 201
    assert unavailable.json()["details"]["run_id"] == replay.json()["id"]
    assert queue.enqueued[0]["payload"]["run_id"] == replay.json()["id"]
