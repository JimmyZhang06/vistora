from __future__ import annotations

from uuid import uuid4

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository

IDEMPOTENCY = {"Idempotency-Key": "test-operation-0001"}
VERSION_FIELDS = (
    "input_schema",
    "research_policy",
    "writing_policy",
    "visual_policy",
    "asset_policy",
    "qc_policy",
    "capability_requirements",
    "output_contract",
    "default_pipeline_version_id",
)


def _create_skill(client: TestClient) -> dict:
    example = contract_example("skill.schema.json")
    payload = {
        field: example[field]
        for field in ("publisher_name", "name", "slug", "description", "visibility")
    }
    response = client.post(
        "/v1/skills", json=payload, headers=IDEMPOTENCY
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_version(client: TestClient, skill_id: str) -> dict:
    payload = contract_example("skill-version.schema.json")
    payload = {
        "skill_id": skill_id,
        "version": payload["version"],
        "test_topics": ["One representative topic"],
        **{field: payload[field] for field in VERSION_FIELDS},
    }
    response = client.post(
        "/v1/skill-versions",
        json=payload,
        headers={"Idempotency-Key": "test-version-0001"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_skill_create_replace_list_and_delete(client: TestClient) -> None:
    created = _create_skill(client)
    assert created["ownership_type"] == "workspace"
    assert created["publisher_type"] == "user"
    assert created["current_version_id"] is None

    listed = client.get("/v1/skills", params={"limit": 1}).json()
    assert listed["data"] == [created]
    assert listed["page"]["has_more"] is False

    replacement = {
        field: created[field]
        for field in ("publisher_name", "name", "slug", "description", "visibility")
    }
    replacement["name"] = "A renamed method"
    replacement["slug"] = "a-renamed-method"
    response = client.put(
        f"/v1/skills/{created['id']}", json=replacement, headers={"If-Match": '"1"'}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "A renamed method"

    assert client.delete(f"/v1/skills/{created['id']}").status_code == 204
    missing = client.get(f"/v1/skills/{created['id']}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "RESOURCE_NOT_FOUND"


def test_version_is_hashed_then_published_and_selected(client: TestClient) -> None:
    skill = _create_skill(client)
    version = _create_version(client, skill["id"])
    assert version["state"] == "draft"
    assert len(version["content_hash"]) == 64
    assert version["content_hash"] != "a" * 64

    validation = client.post(
        f"/v1/skill-versions/{version['id']}/validate",
        headers={"Idempotency-Key": "validate-version-0001", "If-Match": '"1"'},
    )
    assert validation.status_code == 200
    assert validation.json()["ready"] is True
    published = client.post(
        f"/v1/skill-versions/{version['id']}/publish",
        json={},
        headers={"Idempotency-Key": "publish-version-0001", "If-Match": '"2"'},
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"
    assert published.json()["published_at"] is not None

    selected_skill = client.get(f"/v1/skills/{skill['id']}").json()
    assert selected_skill["current_version_id"] == version["id"]
    assert selected_skill["status"] == "active"


def test_invalid_skill_version_semantics_are_rejected(client: TestClient) -> None:
    skill = _create_skill(client)
    payload = contract_example("skill-version.schema.json")
    payload = {
        "skill_id": skill["id"],
        "version": payload["version"],
        **{field: payload[field] for field in VERSION_FIELDS},
    }
    payload["research_policy"]["minimum_sources"] = 9
    payload["research_policy"]["maximum_sources"] = 2
    response = client.post(
        "/v1/skill-versions",
        json=payload,
        headers={"Idempotency-Key": "invalid-version-0001"},
    )
    assert response.status_code == 422
    assert response.json()["details"]["path"] == "research_policy.minimum_sources"


def test_official_catalog_resource_is_visible_and_forkable() -> None:
    official_skill = contract_example("skill.schema.json")
    official_skill.update(
        id=str(uuid4()),
        workspace_id=str(uuid4()),
        ownership_type="system",
        publisher_type="system",
        publisher_name="FrameFactory",
    )
    official_version = contract_example("skill-version.schema.json")
    official_version.update(
        id=str(uuid4()),
        workspace_id=official_skill["workspace_id"],
        ownership_type="system",
        skill_id=official_skill["id"],
        state="published",
    )
    official_skill["current_version_id"] = official_version["id"]
    repository = InMemoryControlRepository(
        skills=[official_skill], skill_versions=[official_version]
    )

    with TestClient(create_app(repository=repository)) as client:
        assert client.get("/v1/skills").json()["data"][0]["id"] == official_skill["id"]
        response = client.post(
            f"/v1/skills/{official_skill['id']}/fork",
            json={
                "name": "My official fork",
                "slug": "my-official-fork",
                "publisher_name": "My Studio",
            },
            headers={"Idempotency-Key": "fork-official-0001"},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["skill"]["forked_from_skill_id"] == official_skill["id"]
    assert body["skill"]["ownership_type"] == "workspace"
    assert body["version"]["state"] == "draft"

