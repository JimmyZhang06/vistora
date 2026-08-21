from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from framefactory_api.repository import InMemoryControlRepository


def test_profile_and_creation_preferences_use_etag_concurrency(
    client: TestClient,
) -> None:
    profile = client.get("/v1/account/profile")
    assert profile.status_code == 200
    assert profile.headers["etag"] == '"1"'

    updated = client.put(
        "/v1/account/profile",
        headers={"If-Match": profile.headers["etag"]},
        json={
            "display_name": "小帧",
            "avatar_url": "https://cdn.example.com/avatar.png",
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "小帧"
    assert updated.json()["revision"] == 2
    assert updated.headers["etag"] == '"2"'

    stale = client.put(
        "/v1/account/profile",
        headers={"If-Match": profile.headers["etag"]},
        json={
            "display_name": "stale",
            "avatar_url": None,
            "locale": "zh-CN",
            "timezone": "Asia/Shanghai",
        },
    )
    assert stale.status_code == 412
    assert stale.json()["code"] == "REVISION_CONFLICT"

    preferences = client.get("/v1/account/creation-preferences")
    assert preferences.status_code == 200
    changed = client.put(
        "/v1/account/creation-preferences",
        headers={"If-Match": preferences.headers["etag"]},
        json={
            "default_language": "en-US",
            "default_aspect_ratio": "9:16",
            "default_duration_seconds": 90,
            "default_visibility": "workspace",
            "auto_quality_check": False,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["default_aspect_ratio"] == "9:16"
    assert changed.json()["revision"] == 2


def test_api_key_plaintext_is_returned_once_and_only_hash_is_stored(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    created = client.post(
        "/v1/account/api-keys",
        json={"name": "Render CLI", "scopes": ["skills:read", "runs:write"]},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["api_key"].startswith("ffk_")
    key_id = body["key"]["id"]
    assert "key_hash" not in body["key"]

    stored = repository._api_keys[key_id]
    assert stored["key_hash"] != body["api_key"]
    assert body["api_key"] not in str(stored)
    assert len(stored["key_hash"]) == 64

    listed = client.get("/v1/account/api-keys")
    assert listed.status_code == 200
    assert listed.json() == [body["key"]]
    assert "api_key" not in listed.text
    assert "key_hash" not in listed.text

    revoked = client.delete(f"/v1/account/api-keys/{key_id}")
    assert revoked.status_code == 204
    after_revoke = client.get("/v1/account/api-keys").json()
    assert after_revoke[0]["revoked_at"] is not None


def test_api_key_rejects_past_expiry_and_duplicate_scopes(client: TestClient) -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    response = client.post(
        "/v1/account/api-keys",
        json={"name": "Expired", "scopes": ["skills:read"], "expires_at": past.isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "API_KEY_EXPIRY_INVALID"

    duplicate = client.post(
        "/v1/account/api-keys",
        json={"name": "Duplicate", "scopes": ["skills:read", "skills:read"]},
    )
    assert duplicate.status_code == 422


def test_sessions_can_be_listed_and_revoked_without_exposing_hashes(
    client: TestClient, repository: InMemoryControlRepository
) -> None:
    context = client.get("/v1/context").json()
    session_id = str(uuid4())
    now = datetime.now(UTC)
    repository._account_sessions[session_id] = {
        "id": session_id,
        "workspace_id": context["workspace_id"],
        "user_id": context["user_id"],
        "token_hash": "must-never-leak",
        "user_agent": "FrameFactory test client",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "last_seen_at": now.isoformat(),
        "revoked_at": None,
    }

    listed = client.get("/v1/account/sessions")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == session_id
    assert "token_hash" not in listed.text
    assert "must-never-leak" not in listed.text

    revoked = client.delete(f"/v1/account/sessions/{session_id}")
    assert revoked.status_code == 204
    assert client.get("/v1/account/sessions").json()[0]["revoked_at"] is not None


def test_two_factor_is_explicitly_unavailable_and_documented(client: TestClient) -> None:
    capabilities = client.get("/v1/account/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "profile": "available",
        "creation_preferences": "available",
        "sessions": "not_recorded",
        "api_keys": "management_only",
        "two_factor_authentication": "unavailable",
    }

    unavailable = client.post("/v1/account/two-factor")
    assert unavailable.status_code == 501
    assert unavailable.json()["code"] == "CAPABILITY_UNAVAILABLE"

    openapi = client.get("/openapi.json").json()
    assert "/v1/account/profile" in openapi["paths"]
    assert "/v1/account/api-keys" in openapi["paths"]
