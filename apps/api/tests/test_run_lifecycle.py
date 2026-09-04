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


def test_offline_research_sources_exempt_only_research_collect() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    operations = {
        str(node["operation"])
        for node in pipeline["nodes"]
        if node["operation"] != "research.collect"
    }
    settings = Settings(worker_capabilities=tuple(sorted(operations)))
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "off",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.org/source-b",
                ],
            }
        )
        estimate = capability_client.post("/v1/runs/estimate", json=payload)
        created = capability_client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "offline-research-run-0001"},
        )

    assert estimate.status_code == 200
    assert estimate.json()["capability_gaps"] == []
    assert created.status_code == 201
    assert created.json()["input"]["research_mode"] == "off"
    assert created.json()["input"]["source_urls"] == payload["input"]["source_urls"]


def test_offline_research_requires_enough_distinct_https_sources() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    operations = {
        str(node["operation"])
        for node in pipeline["nodes"]
        if node["operation"] != "research.collect"
    }
    settings = Settings(worker_capabilities=tuple(sorted(operations)))
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "off",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.com/source-a#duplicate",
                    "http://example.org/not-https",
                ],
            }
        )
        estimate = capability_client.post("/v1/runs/estimate", json=payload)
        created = capability_client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "offline-research-run-insufficient-0001"},
        )

    assert {gap["capability"] for gap in estimate.json()["capability_gaps"]} == {
        "research.collect",
        "research.web_acquisition",
    }
    assert created.status_code == 422
    assert created.json()["code"] == "RUN_RESEARCH_SOURCES_INSUFFICIENT"
    assert created.json()["details"] == {
        "path": "input.source_urls",
        "minimum_sources": 2,
        "provided": 1,
    }
    assert repository._runs == {}


def test_offline_research_does_not_exempt_other_missing_capabilities() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    operations = {
        str(node["operation"])
        for node in pipeline["nodes"]
        if node["operation"] not in {"research.collect", "writing.compose"}
    }
    settings = Settings(worker_capabilities=tuple(sorted(operations)))
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "off",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.org/source-b",
                ],
            }
        )
        estimate = capability_client.post("/v1/runs/estimate", json=payload)
        created = capability_client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "offline-research-other-gap-0001"},
        )

    assert {gap["capability"] for gap in estimate.json()["capability_gaps"]} == {
        "writing.compose"
    }
    assert created.status_code == 409
    assert created.json()["code"] == "RUN_CAPABILITY_UNAVAILABLE"
    assert {
        gap["capability"] for gap in created.json()["details"]["capability_gaps"]
    } == {"writing.compose"}
    assert repository._runs == {}


def test_research_exemption_is_exact_and_requires_off_mode() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    pipeline["nodes"][0]["operation"] = "research.verify"
    operations = {
        str(node["operation"])
        for node in pipeline["nodes"]
        if node["operation"] != "research.verify"
    }
    settings = Settings(worker_capabilities=tuple(sorted(operations)))
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "off",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.org/source-b",
                ],
            }
        )
        exact_gap = capability_client.post("/v1/runs/estimate", json=payload)

    assert {gap["capability"] for gap in exact_gap.json()["capability_gaps"]} == {
        "research.verify",
        "research.web_acquisition",
    }

    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    operations = {
        str(node["operation"])
        for node in pipeline["nodes"]
        if node["operation"] != "research.collect"
    }
    settings = Settings(worker_capabilities=tuple(sorted(operations)))
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "when_missing",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.org/source-b",
                ],
            }
        )
        mode_gap = capability_client.post("/v1/runs/estimate", json=payload)

    assert {gap["capability"] for gap in mode_gap.json()["capability_gaps"]} == {
        "research.collect",
        "research.web_acquisition",
    }


def test_required_abstract_capability_requires_a_pipeline_provider_operation() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    pipeline["capability_requirements"] = []
    pipeline["nodes"] = [
        {
            **node,
            "depends_on": [
                dependency
                for dependency in node["depends_on"]
                if dependency != "research"
            ],
        }
        for node in pipeline["nodes"]
        if node["operation"] != "research.collect"
    ]
    operations = tuple(sorted(str(node["operation"]) for node in pipeline["nodes"]))
    settings = Settings(worker_capabilities=operations)
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        repository._versions[version["id"]]["capability_requirements"] = [
            {
                "name": "research.source_grounding",
                "level": "required",
                "minimum_version": "1.0.0",
            }
        ]
        payload = _run_payload(version)
        response = capability_client.post("/v1/runs/estimate", json=payload)
        created = capability_client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "abstract-capability-provider-0001"},
        )

    assert {gap["capability"] for gap in response.json()["capability_gaps"]} == {
        "research.source_grounding"
    }
    assert created.status_code == 409
    assert created.json()["code"] == "RUN_CAPABILITY_UNAVAILABLE"
    assert {
        gap["capability"] for gap in created.json()["details"]["capability_gaps"]
    } == {"research.source_grounding"}


def test_optional_abstract_capability_does_not_block_run_preflight() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    pipeline["capability_requirements"] = []
    operations = tuple(sorted(str(node["operation"]) for node in pipeline["nodes"]))
    settings = Settings(worker_capabilities=operations)
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        repository._versions[version["id"]]["capability_requirements"] = [
            {
                "name": "model.video_generation",
                "level": "optional",
                "minimum_version": "1.0.0",
            }
        ]
        response = capability_client.post("/v1/runs/estimate", json=_run_payload(version))

    assert response.json()["capability_gaps"] == []


def test_pipeline_capability_requirement_is_enforced() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    pipeline["capability_requirements"] = ["provider.specialized_service"]
    operations = tuple(sorted(str(node["operation"]) for node in pipeline["nodes"]))
    settings = Settings(worker_capabilities=operations)
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        repository._versions[version["id"]]["capability_requirements"] = []
        response = capability_client.post("/v1/runs/estimate", json=_run_payload(version))

    assert response.json()["capability_gaps"] == [
        {
            "capability": "provider.specialized_service",
            "resource": "pipeline",
            "message": (
                "所选 Pipeline 没有可满足 provider.specialized_service 的必需执行 operation。"
            ),
        }
    ]


def test_capability_minimum_version_is_enforced() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    pipeline["capability_requirements"] = []
    operations = tuple(sorted(str(node["operation"]) for node in pipeline["nodes"]))
    settings = Settings(worker_capabilities=operations)
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        repository._versions[version["id"]]["capability_requirements"] = [
            {
                "name": "model.text_generation",
                "level": "required",
                "minimum_version": "2.0.0",
            }
        ]
        response = capability_client.post("/v1/runs/estimate", json=_run_payload(version))

    assert {gap["capability"] for gap in response.json()["capability_gaps"]} == {
        "model.text_generation"
    }
    assert "2.0.0" in response.json()["capability_gaps"][0]["message"]


def test_offline_mode_waives_only_web_acquisition_requirement() -> None:
    repository = _repository_with_pipeline()
    pipeline = next(iter(repository._pipelines.values()))
    operations = tuple(
        sorted(
            str(node["operation"])
            for node in pipeline["nodes"]
            if node["operation"] != "research.collect"
        )
    )
    settings = Settings(worker_capabilities=operations)
    with TestClient(create_app(settings=settings, repository=repository)) as capability_client:
        version = _published_version(capability_client)
        repository._versions[version["id"]]["capability_requirements"] = [
            {
                "name": "research.web_acquisition",
                "level": "required",
                "minimum_version": "1.0.0",
            },
            {
                "name": "research.source_grounding",
                "level": "required",
                "minimum_version": "1.0.0",
            },
            {
                "name": "model.video_generation",
                "level": "required",
                "minimum_version": "1.0.0",
            },
        ]
        payload = _run_payload(version)
        payload["input"].update(
            {
                "research_mode": "off",
                "source_urls": [
                    "https://example.com/source-a",
                    "https://example.org/source-b",
                ],
            }
        )
        response = capability_client.post("/v1/runs/estimate", json=payload)

    assert {gap["capability"] for gap in response.json()["capability_gaps"]} == {
        "model.video_generation"
    }


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
