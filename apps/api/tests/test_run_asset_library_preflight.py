from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_USER_ID, DEVELOPMENT_WORKSPACE_ID


def _published_version() -> dict:
    version = contract_example("skill-version.schema.json")
    version["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    version["ownership_type"] = "workspace"
    version["state"] = "published"
    version["published_at"] = version["created_at"]
    return version


def _pipeline(*, retrieve_required: bool | None) -> dict:
    pipeline = contract_example("pipeline.schema.json")
    pipeline["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    pipeline["ownership_type"] = "workspace"
    pipeline["state"] = "published"
    pipeline["status"] = "active"
    pipeline["published_at"] = pipeline["created_at"]
    if retrieve_required is not None:
        media_node = next(
            node for node in pipeline["nodes"] if node["operation"] == "media.select"
        )
        media_node["operation"] = "media.retrieve"
        media_node["required"] = retrieve_required
    return pipeline


def _library(*, status: str = "active", workspace_id: UUID = DEVELOPMENT_WORKSPACE_ID) -> dict:
    library = contract_example("asset-library.schema.json")
    library["id"] = str(uuid4())
    library["workspace_id"] = str(workspace_id)
    library["ownership_type"] = "workspace"
    library["status"] = status
    library["created_by"] = str(DEVELOPMENT_USER_ID)
    return library


def _repository(
    *,
    retrieve_required: bool | None,
    libraries: list[dict] | None = None,
) -> tuple[InMemoryControlRepository, dict, dict]:
    version = _published_version()
    pipeline = _pipeline(retrieve_required=retrieve_required)
    return (
        InMemoryControlRepository(
            skill_versions=[deepcopy(version)],
            pipelines=[deepcopy(pipeline)],
            asset_libraries=deepcopy(libraries or []),
        ),
        version,
        pipeline,
    )


def _run_payload(version: dict, pipeline: dict, library_ids: list[str]) -> dict:
    return {
        "input": {"topic": "Run asset-library preflight"},
        "composition": {
            "skill_version_id": version["id"],
            "pipeline_version_id": pipeline["id"],
            "asset_library_ids": library_ids,
        },
    }


def test_required_media_retrieve_rejects_run_without_asset_library() -> None:
    repository, version, pipeline = _repository(retrieve_required=True)
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(version, pipeline, []),
            headers={"Idempotency-Key": "retrieve-without-library-0001"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"
    assert response.json()["details"]["path"] == "composition.asset_library_ids"
    assert repository._runs == {}


def test_required_media_retrieve_allows_explicit_no_asset_draft() -> None:
    repository, version, pipeline = _repository(retrieve_required=True)
    payload = _run_payload(version, pipeline, [])
    payload["video_settings"] = {"no_asset_draft": {"enabled": True}}
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "retrieve-editorial-draft-0001"},
        )

    assert response.status_code == 201
    settings = response.json()["composition_snapshot"]["production_settings"]
    assert settings["no_asset_draft"] == {
        "enabled": True,
        "mode": "procedural_cards",
        "draft": True,
        "replacement_required": True,
    }
    assert settings["sources"]["no_asset_draft"] == "run_override"


def test_required_media_retrieve_accepts_active_workspace_library() -> None:
    library = _library()
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[library]
    )
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(version, pipeline, [library["id"]]),
            headers={"Idempotency-Key": "retrieve-with-active-library-0001"},
        )

    assert response.status_code == 201
    assert response.json()["composition_snapshot"]["asset_library_ids"] == [
        library["id"]
    ]


def test_inventory_pipeline_freezes_catalog_snapshot_for_single_run() -> None:
    library = _library()
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[library]
    )
    pipeline["nodes"].append(
        {
            "key": "inventory",
            "operation": "media.inventory",
            "depends_on": [],
            "required": True,
            "maximum_attempts": 2,
            "timeout_seconds": 900,
            "review_gate": False,
        }
    )
    repository._pipelines[pipeline["id"]] = deepcopy(pipeline)
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(version, pipeline, [library["id"]]),
            headers={"Idempotency-Key": "inventory-catalog-snapshot-0001"},
        )

    assert response.status_code == 201
    snapshot_id = response.json()["composition_snapshot"]["catalog_snapshot_id"]
    assert snapshot_id in repository._catalog_snapshots


def test_inventory_run_without_materials_provisions_live_acquisition_library() -> None:
    repository, version, pipeline = _repository(retrieve_required=True)
    pipeline["nodes"].append(
        {
            "key": "inventory",
            "operation": "media.inventory",
            "depends_on": [],
            "required": True,
            "maximum_attempts": 2,
            "timeout_seconds": 900,
            "review_gate": False,
        }
    )
    repository._pipelines[pipeline["id"]] = deepcopy(pipeline)
    payload = _run_payload(version, pipeline, [])
    payload["video_settings"] = {
        "asset_acquisition": {
            "enabled": True,
            "sources": ["wikimedia"],
            "max_assets": 3,
            "copyright_status": "public_domain",
            "rights_confirmed": False,
        }
    }

    with TestClient(create_app(repository=repository)) as client:
        first = client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "inventory-auto-library-0001"},
        )
        replay = client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "inventory-auto-library-0001"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    snapshot = first.json()["composition_snapshot"]
    assert "catalog_snapshot_id" not in snapshot
    assert len(snapshot["asset_library_ids"]) == 1
    settings = snapshot["production_settings"]["asset_acquisition"]
    assert settings["library_id"] == snapshot["asset_library_ids"][0]
    assert settings["library_auto_provisioned"] is True
    libraries = list(repository._asset_libraries.values())
    assert len(libraries) == 1
    assert libraries[0]["slug"] == "auto-acquired-footage"
    assert libraries[0]["visibility"] == "private"


def test_auto_acquisition_recovers_when_the_original_auto_library_is_archived() -> None:
    archived = _library(status="archived")
    archived["slug"] = "auto-acquired-footage"
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[archived]
    )
    payload = _run_payload(version, pipeline, [])
    payload["video_settings"] = {
        "asset_acquisition": {
            "enabled": True,
            "sources": ["wikimedia"],
            "max_assets": 3,
            "copyright_status": "public_domain",
            "rights_confirmed": False,
        }
    }

    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": "archived-auto-library-0001"},
        )

    assert response.status_code == 201
    selected_id = response.json()["composition_snapshot"]["asset_library_ids"][0]
    selected = repository._asset_libraries[selected_id]
    assert selected["slug"] == "auto-acquired-footage-2"
    assert selected["status"] == "active"


def test_required_media_retrieve_rejects_foreign_workspace_library() -> None:
    foreign_library = _library(
        workspace_id=UUID("99999999-9999-4999-8999-999999999999")
    )
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[foreign_library]
    )
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(version, pipeline, [foreign_library["id"]]),
            headers={"Idempotency-Key": "retrieve-with-foreign-library-0001"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert response.json()["details"]["resource"] == "asset_library"
    assert repository._runs == {}


def test_required_media_retrieve_rejects_inactive_library() -> None:
    archived_library = _library(status="archived")
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[archived_library]
    )
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(version, pipeline, [archived_library["id"]]),
            headers={"Idempotency-Key": "retrieve-with-archived-library-0001"},
        )

    assert response.status_code == 409
    assert response.json()["code"] == "ASSET_LIBRARY_NOT_ACTIVE"
    assert response.json()["details"] == {
        "asset_library_id": archived_library["id"],
        "asset_library_status": "archived",
    }
    assert repository._runs == {}


def test_preflight_validates_every_selected_library() -> None:
    active_library = _library()
    archived_library = _library(status="archived")
    repository, version, pipeline = _repository(
        retrieve_required=True, libraries=[active_library, archived_library]
    )
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/runs",
            json=_run_payload(
                version, pipeline, [active_library["id"], archived_library["id"]]
            ),
            headers={"Idempotency-Key": "retrieve-with-mixed-libraries-0001"},
        )

    assert response.status_code == 409
    assert response.json()["details"]["asset_library_id"] == archived_library["id"]
    assert repository._runs == {}


def test_legacy_and_optional_retrieval_pipelines_remain_library_optional() -> None:
    for retrieve_required in (None, False):
        repository, version, pipeline = _repository(
            retrieve_required=retrieve_required
        )
        with TestClient(create_app(repository=repository)) as client:
            response = client.post(
                "/v1/runs",
                json=_run_payload(version, pipeline, []),
                headers={
                    "Idempotency-Key": f"library-optional-{retrieve_required}-0001"
                },
            )

        assert response.status_code == 201


def test_required_media_retrieve_rejects_batch_before_any_run_is_saved() -> None:
    repository, version, pipeline = _repository(retrieve_required=True)
    with TestClient(create_app(repository=repository)) as client:
        response = client.post(
            "/v1/generation-batches",
            json={
                "name": "Missing retrieval library",
                "items": [{"topic": "First"}, {"topic": "Second"}],
                "composition": _run_payload(version, pipeline, [])["composition"],
            },
            headers={"Idempotency-Key": "retrieve-batch-without-library-0001"},
        )

    assert response.status_code == 422
    assert repository._generation_batches == {}
    assert repository._runs == {}
