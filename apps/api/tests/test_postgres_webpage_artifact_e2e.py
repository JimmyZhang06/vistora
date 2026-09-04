from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from framefactory_api.main import create_app
from framefactory_api.migrate import run_migrations
from framefactory_api.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATABASE_URL_ENV = "FRAMEFACTORY_TEST_POSTGRES_URL"
MUTATION_OPT_IN_ENV = "FRAMEFACTORY_TEST_POSTGRES_ALLOW_MUTATION"
CAPABILITIES = (
    "web.capture.validate",
    "web.capture.screenshot",
    "web.site.discover",
    "web.page.capture_batch",
    "web.region.analyze",
    "web.storyboard.plan",
    "writing.compose.webpage",
    "writing.compose.webpage_story",
    "web.materialize",
    "web.materialize.regions",
    "audio.synthesize",
    "render.compose",
    "quality.evaluate",
)


def _disposable_database_url() -> str:
    database_url = os.getenv(DATABASE_URL_ENV, "").strip()
    if not database_url:
        pytest.skip(f"set {DATABASE_URL_ENV} to run the real PostgreSQL artifact E2E test")
    if os.getenv(MUTATION_OPT_IN_ENV) != "1":
        pytest.skip(
            f"set {MUTATION_OPT_IN_ENV}=1 only for a dedicated disposable PostgreSQL database"
        )
    parsed = urlparse(database_url)
    database_name = unquote(parsed.path.lstrip("/")).lower()
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        pytest.fail(f"{DATABASE_URL_ENV} must be an absolute PostgreSQL URL")
    if not database_name or not any(marker in database_name for marker in ("test", "e2e")):
        pytest.fail(f"{DATABASE_URL_ENV} database name must contain 'test' or 'e2e'")
    return database_url


class Queue:
    async def enqueue(self, **_: object) -> tuple[object, bool]:
        return SimpleNamespace(id="webpage-artifact-e2e"), True


class Storage:
    async def presign_download(self, *_: object, **__: object) -> object:
        raise AssertionError("artifact E2E does not request a signed download")


@pytest.fixture(scope="module")
def postgres_webpage_client() -> Iterator[tuple[TestClient, str]]:
    database_url = _disposable_database_url()
    asyncio.run(run_migrations(database_url, REPOSITORY_ROOT))
    settings = Settings(
        repository_backend="postgresql",
        database_url=database_url,
        worker_capabilities=CAPABILITIES,
        default_user_id=UUID("77777777-7777-4777-8777-777777777777"),
        default_workspace_id=UUID("88888888-8888-4888-8888-888888888888"),
        default_workspace_name="Webpage Artifact PostgreSQL E2E",
        default_user_email="webpage-artifact-postgres-e2e@local.invalid",
        default_user_display_name="Webpage Artifact PostgreSQL E2E",
    )
    with TestClient(
        create_app(settings=settings, job_queue=Queue(), object_storage=Storage())
    ) as client:
        yield client, database_url


def test_browser_capture_artifact_key_preserves_real_postgres_tenant_boundary(
    postgres_webpage_client: tuple[TestClient, str],
) -> None:
    client, database_url = postgres_webpage_client
    created = client.post(
        "/v1/webpage-video/runs",
        headers={"Idempotency-Key": f"artifact-e2e-{uuid4()}"},
        json={
            "target_url": "https://example.com/",
            "capture": {"mode": "viewport", "aspect_ratio": "16:9", "full_page": False},
            "video": {
                "topic": "PostgreSQL browser-capture artifact contract",
                "duration_seconds": 15,
                "subtitles_enabled": True,
                "voice_profile_id": None,
            },
            "rights": {"public_page_confirmed": True, "rights_confirmed": True},
        },
    )
    assert created.status_code == 201, created.text
    run = created.json()
    workspace_id = run["workspace_id"]
    run_id = run["project_run_id"]

    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as connection:
        step = connection.execute(
            """INSERT INTO run_steps (
                 workspace_id, run_id, step_key, step_type, queue_name, input_snapshot
               ) VALUES (%s, %s, 'validate', 'web.capture.validate',
                         'browser-capture', '{}'::jsonb)
               ON CONFLICT (run_id, step_key) DO UPDATE SET step_key=EXCLUDED.step_key
               RETURNING id""",
            (workspace_id, run_id),
        ).fetchone()
        assert step is not None

        def insert_artifact(object_key: str) -> None:
            connection.execute(
                """INSERT INTO artifacts (
                     id, workspace_id, run_id, step_id, kind, status, schema_version,
                     media_type, storage_provider, bucket, object_key, content_hash,
                     byte_size, metadata
                   ) VALUES (
                     %s, %s, %s, %s, 'manifest', 'available', '1.0.0',
                     'application/json', 's3', 'artifact-e2e', %s, %s, 2, '{}'::jsonb
                   )""",
                (uuid4(), workspace_id, run_id, step["id"], object_key, "a" * 64),
            )

        suffix = f"workspaces/{workspace_id}/runs/{run_id}/artifacts/{uuid4()}/proof.json"
        insert_artifact(suffix)
        insert_artifact(f"browser-capture/{suffix}")

        invalid_keys = (
            f"capture/{suffix}",
            f"browser-capture/workspaces/{uuid4()}/runs/{run_id}/artifacts/{uuid4()}/proof.json",
            f"browser-capture/{suffix}/../escape.json",
        )
        for object_key in invalid_keys:
            with pytest.raises(psycopg.errors.CheckViolation) as raised:
                insert_artifact(object_key)
            assert raised.value.diag.constraint_name == "artifacts_tenant_key"
