from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_USER_ID, DEVELOPMENT_WORKSPACE_ID


class CapturingQueue:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        self.jobs.append(command)
        return SimpleNamespace(id=f"queue-{len(self.jobs)}"), True


def _library() -> dict[str, Any]:
    resource = deepcopy(contract_example("asset-library.schema.json"))
    resource["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    resource["ownership_type"] = "workspace"
    resource["status"] = "active"
    resource["created_by"] = str(DEVELOPMENT_USER_ID)
    return resource


def _command(library_id: str) -> dict[str, Any]:
    return {
        "library_id": library_id,
        "topic": "Urban public transport",
        "queries": [],
        "sources": ["wikimedia"],
        "max_assets": 6,
        "copyright_status": "public_domain",
        "rights_confirmed": True,
    }


def test_library_build_job_is_persisted_enqueued_read_and_cancelled() -> None:
    library = _library()
    repository = InMemoryControlRepository(asset_libraries=[library])
    queue = CapturingQueue()

    with TestClient(create_app(repository=repository, job_queue=queue)) as client:
        created = client.post(
            "/v1/library-build-jobs",
            json=_command(library["id"]),
            headers={"Idempotency-Key": "library-build-transit-0001"},
        )
        fetched = client.get(f"/v1/library-build-jobs/{created.json()['id']}")
        cancelled = client.post(
            f"/v1/library-build-jobs/{created.json()['id']}/cancel",
            headers={"If-Match": created.headers["etag"]},
        )

    assert created.status_code == 202
    assert created.json()["status"] == "queued"
    assert created.json()["stage"] == "discover"
    assert created.json()["progress"] == {
        "asset_ids": [],
        "discovered": 0,
        "transferred": 0,
        "analyzed": 0,
        "indexed": 0,
        "failed": 0,
    }
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["revision"] == 2
    assert len(queue.jobs) == 1
    assert queue.jobs[0]["queue_name"] == "library-build"
    assert queue.jobs[0]["payload"] == {
        "workspace_id": str(DEVELOPMENT_WORKSPACE_ID),
        "job_id": created.json()["id"],
        "library_id": library["id"],
        "spec": created.json()["spec"],
    }
    assert queue.jobs[0]["max_attempts"] == 2000


def test_library_build_job_persists_a_typed_failure_when_queue_is_unavailable() -> None:
    library = _library()
    repository = InMemoryControlRepository(asset_libraries=[library])

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/library-build-jobs",
            json=_command(library["id"]),
            headers={"Idempotency-Key": "library-build-no-queue-0001"},
        )
        job_id = response.json()["details"]["job_id"]
        persisted = client.get(f"/v1/library-build-jobs/{job_id}")

    assert response.status_code == 503
    assert response.json()["code"] == "LIBRARY_BUILD_QUEUE_UNAVAILABLE"
    assert persisted.status_code == 200
    assert persisted.json()["status"] == "queued"


def test_public_domain_library_build_rejects_non_wikimedia_sources() -> None:
    library = _library()
    repository = InMemoryControlRepository(asset_libraries=[library])
    command = _command(library["id"])
    command["sources"] = ["youtube"]

    with TestClient(create_app(repository=repository, job_queue=CapturingQueue())) as client:
        response = client.post(
            "/v1/library-build-jobs",
            json=command,
            headers={"Idempotency-Key": "library-build-rights-0001"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"
    assert repository._library_build_jobs == {}
