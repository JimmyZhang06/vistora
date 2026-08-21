from __future__ import annotations

from copy import deepcopy

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_USER_ID, DEVELOPMENT_WORKSPACE_ID


def _repository() -> tuple[InMemoryControlRepository, dict, dict]:
    version = contract_example("skill-version.schema.json")
    version["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    version["ownership_type"] = "workspace"
    version["state"] = "published"
    version["published_at"] = version["created_at"]
    pipeline = contract_example("pipeline.schema.json")
    pipeline["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    pipeline["ownership_type"] = "workspace"
    pipeline["state"] = "published"
    pipeline["status"] = "active"
    pipeline["published_at"] = pipeline["created_at"]
    return (
        InMemoryControlRepository(
            skill_versions=[deepcopy(version)], pipelines=[deepcopy(pipeline)]
        ),
        version,
        pipeline,
    )


def test_batch_creation_is_idempotent_and_items_are_server_paginated() -> None:
    repository, version, pipeline = _repository()
    payload = {
        "name": "August launch",
        "items": [
            {"topic": f"Video topic {index:03d}", "inputs": {"row": index}}
            for index in range(120)
        ],
        "composition": {
            "skill_version_id": version["id"],
            "pipeline_version_id": pipeline["id"],
            "asset_library_ids": [],
            "voice_profile_id": None,
            "render_preset_version_id": None,
            "capabilities": [],
        },
    }
    headers = {"Idempotency-Key": "batch-august-launch-0001"}

    with TestClient(create_app(repository=repository)) as client:
        created = client.post("/v1/generation-batches", json=payload, headers=headers)
        replay = client.post("/v1/generation-batches", json=payload, headers=headers)
        batches = client.get("/v1/generation-batches")
        first_page = client.get(
            f"/v1/generation-batches/{created.json()['id']}/items",
            params={"limit": 50},
        )
        second_page = client.get(
            f"/v1/generation-batches/{created.json()['id']}/items",
            params={"limit": 50, "cursor": first_page.json()["page"]["next_cursor"]},
        )
        searched = client.get(
            f"/v1/generation-batches/{created.json()['id']}/items",
            params={"search": "topic 077"},
        )
        failed_run_id = first_page.json()["data"][0]["run_id"]
        repository._runs[failed_run_id]["status"] = "failed"
        cancelled = client.post(
            f"/v1/generation-batches/{created.json()['id']}/cancel",
            headers={"Idempotency-Key": "cancel-august-launch-0001"},
        )
        cancelled_replay = client.post(
            f"/v1/generation-batches/{created.json()['id']}/cancel",
            headers={"Idempotency-Key": "cancel-august-launch-0001"},
        )
        retry = client.post(
            f"/v1/generation-batches/{created.json()['id']}/retry-failed",
            json={},
            headers={"Idempotency-Key": "retry-august-launch-0001"},
        )
        retry_replay = client.post(
            f"/v1/generation-batches/{created.json()['id']}/retry-failed",
            json={},
            headers={"Idempotency-Key": "retry-august-launch-0001"},
        )

    assert created.status_code == replay.status_code == 202
    assert created.json() == replay.json()
    assert created.json()["total_count"] == 120
    assert created.json()["status_counts"]["queued"] == 120
    assert batches.json()["data"][0]["id"] == created.json()["id"]
    assert first_page.json()["total_count"] == 120
    assert len(first_page.json()["data"]) == len(second_page.json()["data"]) == 50
    assert first_page.json()["data"][0]["ordinal"] == 0
    assert second_page.json()["data"][0]["ordinal"] == 50
    assert searched.json()["total_count"] == 1
    assert searched.json()["data"][0]["input"]["row"] == 77
    assert searched.json()["data"][0]["workspace_id"] == str(
        DEVELOPMENT_WORKSPACE_ID
    )
    assert created.json()["created_by"] == str(DEVELOPMENT_USER_ID)
    assert cancelled.status_code == cancelled_replay.status_code == 202
    assert cancelled.json()["status"] == "completed_with_errors"
    assert cancelled.json()["status_counts"]["cancelled"] == 119
    assert cancelled.json()["status_counts"]["failed"] == 1
    assert retry.status_code == retry_replay.status_code == 202
    assert retry.json() == retry_replay.json()
    assert retry.json()["total_count"] == 1
    assert retry.json()["status_counts"]["queued"] == 1


def test_batch_rejects_more_than_5000_items() -> None:
    repository, version, pipeline = _repository()
    payload = {
        "name": "Too large",
        "items": [{"topic": f"Topic {index}"} for index in range(5001)],
        "composition": {
            "skill_version_id": version["id"],
            "pipeline_version_id": pipeline["id"],
        },
    }
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/generation-batches",
            json=payload,
            headers={"Idempotency-Key": "batch-too-large-0001"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"


def test_thousand_item_batch_keeps_management_response_bounded() -> None:
    repository, version, pipeline = _repository()
    payload = {
        "name": "Thousand item capacity gate",
        "items": [{"topic": f"Capacity topic {index:04d}"} for index in range(1000)],
        "composition": {
            "skill_version_id": version["id"],
            "pipeline_version_id": pipeline["id"],
        },
    }
    with TestClient(create_app(repository=repository)) as client:
        created = client.post(
            "/v1/generation-batches",
            json=payload,
            headers={"Idempotency-Key": "batch-capacity-1000-0001"},
        )
        page = client.get(
            f"/v1/generation-batches/{created.json()['id']}/items",
            params={"limit": 50},
        )

    assert created.status_code == 202
    assert created.json()["total_count"] == 1000
    assert page.json()["total_count"] == 1000
    assert len(page.json()["data"]) == 50
    assert page.json()["page"]["has_more"] is True
