from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.migrate import run_migrations
from framefactory_api.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "FRAMEFACTORY_TEST_POSTGRES_URL"
MUTATION_OPT_IN_ENV = "FRAMEFACTORY_TEST_POSTGRES_ALLOW_MUTATION"


def _disposable_database_url() -> str:
    database_url = os.getenv(DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(f"set {DATABASE_URL_ENV} to run the real PostgreSQL Channel E2E test")
    if os.getenv(MUTATION_OPT_IN_ENV) != "1":
        pytest.skip(
            f"set {MUTATION_OPT_IN_ENV}=1 only for a dedicated disposable PostgreSQL database"
        )

    parsed = urlparse(database_url)
    database_name = unquote(parsed.path.lstrip("/")).lower()
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        pytest.fail(f"{DATABASE_URL_ENV} must be an absolute PostgreSQL URL")
    if not database_name or not any(marker in database_name for marker in ("test", "e2e")):
        pytest.fail(
            f"{DATABASE_URL_ENV} database name must contain 'test' or 'e2e'; "
            "the integration test writes durable rows"
        )
    return database_url


@pytest.fixture(scope="module")
def postgres_client() -> Iterator[TestClient]:
    database_url = _disposable_database_url()
    asyncio.run(run_migrations(database_url, REPOSITORY_ROOT))
    settings = Settings(
        repository_backend="postgresql",
        database_url=database_url,
        default_user_id=UUID("77777777-7777-4777-8777-777777777777"),
        default_workspace_id=UUID("88888888-8888-4888-8888-888888888888"),
        default_workspace_name="Channel PostgreSQL E2E",
        default_user_email="channel-postgres-e2e@local.invalid",
        default_user_display_name="Channel PostgreSQL E2E",
    )
    with TestClient(create_app(settings=settings)) as client:
        yield client


def test_official_channel_defaults_and_run_snapshot_use_real_postgres(
    postgres_client: TestClient,
) -> None:
    context = postgres_client.get("/v1/context")
    assert context.status_code == 200
    workspace_id = context.json()["workspace_id"]

    versions = postgres_client.get("/v1/skill-versions?limit=100")
    assert versions.status_code == 200
    official = next(
        version
        for version in versions.json()["data"]
        if version["ownership_type"] == "system"
        and version["state"] == "published"
        and version["default_pipeline_version_id"] is not None
    )
    assert official["workspace_id"] != workspace_id

    nonce = uuid4().hex[:12]
    payload = {
        "name": f"PostgreSQL Channel {nonce}",
        "slug": f"postgres-channel-{nonce}",
        "description": "Exercises public system defaults across the real database boundary.",
        "platform": "youtube",
        "handle": f"@pg-{nonce}",
        "brand_profile": {"tone": "clear"},
        "platform_connection_id": None,
        "status": "active",
        "default_composition": {
            "skill_version_id": official["id"],
            "pipeline_version_id": official["default_pipeline_version_id"],
            "asset_library_ids": [],
            "voice_profile_id": None,
            "render_preset_version_id": None,
        },
    }
    created_response = postgres_client.post(
        "/v1/channels",
        json=payload,
        headers={"Idempotency-Key": f"postgres-channel-create-{nonce}"},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    assert created["workspace_id"] == workspace_id
    assert created["default_composition"] == payload["default_composition"]
    assert created_response.headers["etag"] == '"1"'

    run_response = postgres_client.post(
        "/v1/runs",
        json={"channel_id": created["id"], "input": {"topic": "PostgreSQL defaults"}},
        headers={"Idempotency-Key": f"postgres-channel-e2e-{nonce}"},
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    assert run["channel_id"] == created["id"]
    assert run["composition_snapshot"]["skill_version"]["id"] == official["id"]
    assert (
        run["composition_snapshot"]["pipeline_version"]["id"]
        == official["default_pipeline_version_id"]
    )

    archived_response = postgres_client.delete(
        f"/v1/channels/{created['id']}",
        headers={"If-Match": created_response.headers["etag"]},
    )
    assert archived_response.status_code == 204, archived_response.text
    archived = postgres_client.get(f"/v1/channels/{created['id']}")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert archived.headers["etag"] == '"2"'


def test_official_skill_fork_keeps_public_system_pipeline_in_real_postgres(
    postgres_client: TestClient,
) -> None:
    context_response = postgres_client.get("/v1/context")
    assert context_response.status_code == 200
    workspace_id = context_response.json()["workspace_id"]

    versions_response = postgres_client.get("/v1/skill-versions?limit=100")
    assert versions_response.status_code == 200
    skills_response = postgres_client.get("/v1/skills?limit=100")
    assert skills_response.status_code == 200
    official_skill = next(
        skill
        for skill in skills_response.json()["data"]
        if skill["ownership_type"] == "system"
        and skill["slug"] == "general-topic-explainer"
    )
    official = next(
        version
        for version in versions_response.json()["data"]
        if version["ownership_type"] == "system"
        and version["skill_id"] == official_skill["id"]
        and version["state"] == "published"
        and version["default_pipeline_version_id"] is not None
    )

    nonce = uuid4().hex[:12]
    fork_payload = {
        "source_version_id": official["id"],
        "name": f"PostgreSQL official fork {nonce}",
        "slug": f"postgres-official-fork-{nonce}",
        "description": "Exercises an official Pipeline reference across workspaces.",
        "visibility": "private",
    }
    fork_headers = {"Idempotency-Key": f"postgres-official-fork-{nonce}"}
    fork_response = postgres_client.post(
        f"/v1/skills/{official['skill_id']}/fork",
        json=fork_payload,
        headers=fork_headers,
    )
    assert fork_response.status_code == 201, fork_response.text
    forked = fork_response.json()
    assert forked["skill"]["workspace_id"] == workspace_id
    assert forked["skill"]["forked_from_skill_id"] == official["skill_id"]
    assert forked["version"]["workspace_id"] == workspace_id
    assert forked["version"]["state"] == "draft"
    assert (
        forked["version"]["default_pipeline_version_id"]
        == official["default_pipeline_version_id"]
    )

    replay_response = postgres_client.post(
        f"/v1/skills/{official['skill_id']}/fork",
        json=fork_payload,
        headers=fork_headers,
    )
    assert replay_response.status_code == 201, replay_response.text
    assert replay_response.json() == forked

    topic_response = postgres_client.patch(
        f"/v1/skill-versions/{forked['version']['id']}",
        json={"test_topics": ["PostgreSQL follow-up draft"]},
        headers={
            "Idempotency-Key": f"postgres-official-topic-{nonce}",
            "If-Match": '"1"',
        },
    )
    assert topic_response.status_code == 200, topic_response.text

    validation_response = postgres_client.post(
        f"/v1/skill-versions/{forked['version']['id']}/validate",
        headers={
            "Idempotency-Key": f"postgres-official-validate-{nonce}",
            "If-Match": '"2"',
        },
    )
    assert validation_response.status_code == 200, validation_response.text
    assert validation_response.json()["ready"] is True

    publish_response = postgres_client.post(
        f"/v1/skill-versions/{forked['version']['id']}/publish",
        json={},
        headers={
            "Idempotency-Key": f"postgres-official-publish-{nonce}",
            "If-Match": '"3"',
        },
    )
    assert publish_response.status_code == 200, publish_response.text
    published = publish_response.json()
    assert published["state"] == "published"

    follow_up_response = postgres_client.post(
        "/v1/skill-versions",
        json={
            "skill_id": forked["skill"]["id"],
            "version": "0.1.1",
            "source_version_id": published["id"],
        },
        headers={"Idempotency-Key": f"postgres-follow-up-draft-{nonce}"},
    )
    assert follow_up_response.status_code == 201, follow_up_response.text
    follow_up = follow_up_response.json()
    assert follow_up["state"] == "draft"
    assert follow_up["content_hash"] == published["content_hash"]
