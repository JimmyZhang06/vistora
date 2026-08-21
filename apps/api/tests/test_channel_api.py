from __future__ import annotations

from copy import deepcopy

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.repository import InMemoryControlRepository


def _published_defaults(repository: InMemoryControlRepository) -> dict[str, object]:
    version = contract_example("skill-version.schema.json")
    version["ownership_type"] = "system"
    version["state"] = "published"
    version["published_at"] = version["created_at"]
    repository._versions[version["id"]] = deepcopy(version)
    pipeline = next(iter(repository._pipelines.values()))
    return {
        "skill_version_id": version["id"],
        "pipeline_version_id": pipeline["id"],
        "asset_library_ids": [],
        "voice_profile_id": None,
        "render_preset_version_id": None,
    }


def _payload(repository: InMemoryControlRepository) -> dict[str, object]:
    return {
        "name": "Daily Ideas",
        "description": "Daily publishing defaults",
        "platform": "youtube",
        "handle": "@dailyideas",
        "brand_profile": {"tone": "clear", "primary_color": "#101828"},
        "platform_connection_id": None,
        "status": "active",
        "default_composition": _published_defaults(repository),
    }


def _idempotency(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def test_channel_crud_uses_etag_and_archives_on_delete(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    payload = _payload(repository)
    created = client.post(
        "/v1/channels", json=payload, headers=_idempotency("channel-crud-001")
    )
    assert created.status_code == 201
    assert created.headers["ETag"] == '"1"'
    channel = created.json()
    assert channel["slug"] == "daily-ideas"
    assert channel["default_composition"] == payload["default_composition"]
    assert "secret" not in channel

    listed = client.get("/v1/channels")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [channel["id"]]

    replacement = deepcopy(payload)
    replacement["status"] = "paused"
    updated = client.put(
        f"/v1/channels/{channel['id']}",
        json=replacement,
        headers={"If-Match": created.headers["ETag"]},
    )
    assert updated.status_code == 200
    assert updated.headers["ETag"] == '"2"'
    assert updated.json()["status"] == "paused"

    stale = client.put(
        f"/v1/channels/{channel['id']}",
        json=payload,
        headers={"If-Match": '"1"'},
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "REVISION_CONFLICT"

    archived = client.delete(
        f"/v1/channels/{channel['id']}", headers={"If-Match": '"2"'}
    )
    assert archived.status_code == 204
    fetched = client.get(f"/v1/channels/{channel['id']}")
    assert fetched.json()["status"] == "archived"
    assert fetched.headers["ETag"] == '"3"'


def test_channel_create_is_durably_idempotent_in_memory(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    payload = _payload(repository)
    headers = _idempotency("channel-create-replay-001")

    created = client.post("/v1/channels", json=payload, headers=headers)
    replayed = client.post("/v1/channels", json=payload, headers=headers)

    assert created.status_code == 201
    assert replayed.status_code == 201
    assert replayed.json() == created.json()
    assert replayed.headers["ETag"] == created.headers["ETag"]
    assert len(repository._channels) == 1

    changed = deepcopy(payload)
    changed["name"] = "Different request"
    rejected = client.post("/v1/channels", json=changed, headers=headers)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_channel_list_filters_before_paginating_and_normalizes_platform(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    for index, (name, platform, channel_status) in enumerate(
        (
            ("Alpha Daily", "YouTube", "active"),
            ("Alpha Weekly", "YOUTUBE", "active"),
            ("Alpha Paused", "youtube", "paused"),
            ("Beta Daily", "youtube", "active"),
        ),
        start=1,
    ):
        payload = _payload(repository)
        payload.update(
            {
                "name": name,
                "slug": f"channel-filter-{index}",
                "handle": f"@channel-filter-{index}",
                "platform": platform,
                "status": channel_status,
            }
        )
        response = client.post(
            "/v1/channels",
            json=payload,
            headers=_idempotency(f"channel-filter-create-{index}"),
        )
        assert response.status_code == 201
        assert response.json()["platform"] == "youtube"

    first_page = client.get(
        "/v1/channels",
        params={
            "search": "alpha",
            "platform": "YouTube",
            "status": "active",
            "limit": 1,
        },
    )
    assert first_page.status_code == 200
    first = first_page.json()
    assert len(first["data"]) == 1
    assert first["page"]["has_more"] is True

    second_page = client.get(
        "/v1/channels",
        params={
            "search": "alpha",
            "platform": "youtube",
            "status": "active",
            "limit": 1,
            "cursor": first["page"]["next_cursor"],
        },
    )
    assert second_page.status_code == 200
    second = second_page.json()
    assert len(second["data"]) == 1
    assert second["page"]["has_more"] is False
    assert {first["data"][0]["name"], second["data"][0]["name"]} == {
        "Alpha Daily",
        "Alpha Weekly",
    }


def test_channel_mutations_require_if_match_with_428(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    payload = _payload(repository)
    created = client.post(
        "/v1/channels",
        json=payload,
        headers=_idempotency("channel-precondition-create"),
    )
    channel_id = created.json()["id"]

    replaced = client.put(f"/v1/channels/{channel_id}", json=payload)
    archived = client.delete(f"/v1/channels/{channel_id}")

    for response in (replaced, archived):
        assert response.status_code == 428
        assert response.json()["code"] == "PRECONDITION_REQUIRED"


def test_run_resolves_active_channel_defaults_into_immutable_snapshot(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    created = client.post(
        "/v1/channels",
        json=_payload(repository),
        headers=_idempotency("channel-run-defaults-create"),
    )
    channel = created.json()

    run_response = client.post(
        "/v1/runs",
        json={"channel_id": channel["id"], "input": {"topic": "Channel defaults"}},
        headers={"Idempotency-Key": "channel-default-run-1"},
    )
    assert run_response.status_code == 201
    run = run_response.json()
    snapshot = run["composition_snapshot"]
    defaults = channel["default_composition"]
    assert run["channel_id"] == channel["id"]
    assert snapshot["skill_version"]["id"] == defaults["skill_version_id"]
    assert snapshot["pipeline_version"]["id"] == defaults["pipeline_version_id"]
    assert snapshot["asset_library_ids"] == defaults["asset_library_ids"]
    assert snapshot["voice_profile_id"] is None
    assert snapshot["render_preset"] is None

    changed = deepcopy(_payload(repository))
    changed["name"] = "Renamed Channel"
    replaced = client.put(
        f"/v1/channels/{channel['id']}",
        json=changed,
        headers={"If-Match": created.headers["ETag"]},
    )
    assert replaced.status_code == 200
    persisted = client.get(f"/v1/runs/{run['id']}").json()
    assert persisted["composition_snapshot"] == snapshot


def test_paused_channel_cannot_supply_run_defaults(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    payload = _payload(repository)
    payload["status"] = "paused"
    channel = client.post(
        "/v1/channels",
        json=payload,
        headers=_idempotency("channel-paused-create"),
    ).json()
    response = client.post(
        "/v1/runs",
        json={"channel_id": channel["id"], "input": {"topic": "Paused"}},
        headers={"Idempotency-Key": "paused-channel-run"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CHANNEL_NOT_ACTIVE"


def test_channel_rejects_embedded_platform_credentials(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    payload = _payload(repository)
    payload["brand_profile"] = {"oauth": {"access_token": "must-not-persist"}}
    response = client.post(
        "/v1/channels",
        json=payload,
        headers=_idempotency("channel-secret-rejected"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"


def test_channel_stores_only_a_valid_platform_connection_reference(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    connection_id = "99999999-9999-4999-8999-999999999999"
    workspace_id = client.get("/v1/context").json()["workspace_id"]
    repository._platform_connections[connection_id] = {
        "id": connection_id,
        "workspace_id": workspace_id,
        "platform": "youtube",
        "name": "Publisher",
        "status": "active",
        "secret_ref": "vault://publishing/youtube",
    }
    payload = _payload(repository)
    payload["platform_connection_id"] = connection_id

    payload["platform"] = "YouTube"
    response = client.post(
        "/v1/channels",
        json=payload,
        headers=_idempotency("channel-connection-create"),
    )

    assert response.status_code == 201
    assert response.json()["platform_connection_id"] == connection_id
    assert response.json()["platform"] == "youtube"
    assert "secret_ref" not in response.text

    mismatched = _payload(repository)
    mismatched["platform"] = "bilibili"
    mismatched["platform_connection_id"] = connection_id
    rejected = client.post(
        "/v1/channels",
        json=mismatched,
        headers=_idempotency("channel-connection-mismatch"),
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "PLATFORM_CONNECTION_MISMATCH"
