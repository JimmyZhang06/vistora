from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from framefactory_api.contracts import ContractValidator
from framefactory_api.errors import ConflictError
from framefactory_api.postgres_repository import (
    PostgreSQLControlRepository,
    _api_key_from_row,
    _asset_from_row,
    _channel_from_row,
    _datetime_value,
    _run_step_from_row,
    _skill_from_row,
    _version_from_row,
)
from framefactory_api.seed_catalog import load_official_catalog

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_: Any) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.fetchval_result: Any = 1
        self.fetchrow_result: Any = None
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Context:
        return _Context(self)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executions.append((query, args))
        return self.fetchval_result

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.executions.append((query, args))
        return self.fetchrow_result

    async def execute(self, query: str, *args: Any) -> str:
        self.executions.append((query, args))
        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.executions.append((query, args))
        return []


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> _Context:
        return _Context(self.connection)

    async def close(self) -> None:
        self.closed = True


def _repository(connection: _Connection | None = None) -> PostgreSQLControlRepository:
    return PostgreSQLControlRepository(
        _Pool(connection or _Connection()),
        default_user_id=USER_ID,
        default_workspace_id=WORKSPACE_ID,
        default_workspace_name="FrameFactory",
    )


def test_asset_segments_only_query_current_completed_analysis() -> None:
    connection = _Connection()
    repository = _repository(connection)
    repository.get_asset = AsyncMock(return_value={})

    asyncio.run(repository.list_asset_related(WORKSPACE_ID, USER_ID, "segments"))

    query, args = connection.executions[-1]
    assert "JOIN asset_analyses aa" in query
    assert "aa.status='completed'" in query
    assert args == (WORKSPACE_ID, USER_ID)


def test_skill_row_mapping_normalizes_database_types() -> None:
    now = datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC)
    resource = _skill_from_row(
        {
            "schema_version": "1.0.0",
            "id": UUID("44444444-4444-4444-8444-444444444444"),
            "workspace_id": WORKSPACE_ID,
            "ownership_type": "workspace",
            "publisher_type": "user",
            "publisher_name": "Studio",
            "name": "Explainer",
            "slug": "explainer",
            "description": "Description",
            "visibility": "private",
            "status": "draft",
            "current_version_id": None,
            "forked_from_skill_id": None,
            "revision": 2,
            "created_by": USER_ID,
            "created_at": now,
            "updated_at": now,
        }
    )

    assert resource["workspace_id"] == str(WORKSPACE_ID)
    assert resource["revision"] == 2
    assert resource["created_at"] == "2026-08-15T01:02:03Z"


def test_channel_row_mapping_preserves_ordered_default_composition() -> None:
    now = datetime(2026, 8, 20, 1, 2, 3, tzinfo=UTC)
    library_ids = [
        UUID("66666666-6666-4666-8666-666666666666"),
        UUID("77777777-7777-4777-8777-777777777777"),
    ]
    resource = _channel_from_row(
        {
            "schema_version": "1.0.0",
            "id": UUID("33333333-3333-4333-8333-333333333333"),
            "workspace_id": WORKSPACE_ID,
            "ownership_type": "workspace",
            "name": "Daily Ideas",
            "slug": "daily-ideas",
            "description": "Description",
            "platform": "youtube",
            "handle": "@dailyideas",
            "brand_profile": '{"tone":"clear"}',
            "platform_connection_id": None,
            "status": "active",
            "revision": 3,
            "skill_version_id": UUID("55555555-5555-4555-8555-555555555555"),
            "pipeline_version_id": UUID("88888888-8888-4888-8888-888888888888"),
            "asset_library_ids": library_ids,
            "voice_profile_id": None,
            "render_preset_version_id": None,
            "created_by": USER_ID,
            "created_at": now,
            "updated_at": now,
        }
    )

    assert resource["revision"] == 3
    assert resource["brand_profile"] == {"tone": "clear"}
    assert resource["default_composition"]["asset_library_ids"] == [
        str(value) for value in library_ids
    ]


def test_platform_connection_lookup_never_selects_secret_reference() -> None:
    connection = _Connection()
    connection_id = UUID("99999999-9999-4999-8999-999999999999")
    connection.fetchrow_result = {
        "id": connection_id,
        "workspace_id": WORKSPACE_ID,
        "platform": "youtube",
        "name": "Publisher",
        "status": "active",
    }
    resource = asyncio.run(
        _repository(connection).get_platform_connection(WORKSPACE_ID, connection_id)
    )

    query, _args = connection.executions[-1]
    assert "secret_ref" not in query
    assert "secret_ref" not in resource


def test_channel_list_filters_are_parameterized_in_postgres() -> None:
    connection = _Connection()
    repository = _repository(connection)

    result = asyncio.run(
        repository.list_channels(
            WORKSPACE_ID,
            search="alpha%' OR true --",
            platform="youtube",
            status="active",
        )
    )

    assert result == []
    query, args = connection.executions[-1]
    assert "$2::text" in query
    assert "$3::text" in query
    assert "$4::text" in query
    assert "alpha%' OR true --" not in query
    assert args == (WORKSPACE_ID, "alpha%' OR true --", "youtube", "active")


def test_channel_create_uses_durable_postgres_idempotency_ledger() -> None:
    connection = _Connection()
    repository = _repository(connection)
    resource = {
        "id": "33333333-3333-4333-8333-333333333333",
        "workspace_id": str(WORKSPACE_ID),
    }
    repository._insert_channel = AsyncMock(return_value=resource)  # type: ignore[method-assign]

    created, is_new = asyncio.run(
        repository.create_channel_idempotently(
            resource,
            operation_key="channel-create-key",
            request_fingerprint="a" * 64,
        )
    )

    assert is_new is True
    assert created == resource
    assert repository._insert_channel.await_count == 1
    sql = "\n".join(query for query, _args in connection.executions)
    assert "INSERT INTO idempotency_keys" in sql
    assert "UPDATE idempotency_keys SET" in sql
    assert any("/v1/channels" in args for _query, args in connection.executions)


def test_channel_create_replays_completed_postgres_response() -> None:
    connection = _Connection()
    connection.fetchval_result = None
    resource = {
        "id": "33333333-3333-4333-8333-333333333333",
        "workspace_id": str(WORKSPACE_ID),
    }
    connection.fetchrow_result = {
        "request_hash": "b" * 64,
        "status": "completed",
        "response_body": resource,
    }
    repository = _repository(connection)
    repository._insert_channel = AsyncMock(return_value={})  # type: ignore[method-assign]

    replayed, is_new = asyncio.run(
        repository.create_channel_idempotently(
            resource,
            operation_key="channel-create-key",
            request_fingerprint="b" * 64,
        )
    )

    assert is_new is False
    assert replayed == resource
    assert repository._insert_channel.await_count == 0


def test_version_row_mapping_decodes_json_without_pool_codec() -> None:
    now = "2026-08-15T01:02:03Z"
    row = {
        "schema_version": "1.0.0",
        "id": UUID("55555555-5555-4555-8555-555555555555"),
        "workspace_id": WORKSPACE_ID,
        "ownership_type": "workspace",
        "skill_id": UUID("44444444-4444-4444-8444-444444444444"),
        "version": "1.0.0",
        "state": "draft",
        "execution_kind": "declarative",
        "input_schema": json.dumps({"type": "object"}),
        "research_policy": "{}",
        "writing_policy": "{}",
        "visual_policy": "{}",
        "asset_policy": "{}",
        "qc_policy": "{}",
        "output_contract": "{}",
        "capability_requirements": "[]",
        "default_pipeline_version_id": None,
        "content_hash": "a" * 64,
        "revision": 1,
        "test_topics": '["topic"]',
        "release_notes": "notes",
        "created_by": USER_ID,
        "created_at": now,
        "published_at": None,
    }

    resource = _version_from_row(row)

    assert resource["input_schema"] == {"type": "object"}
    assert resource["capability_requirements"] == []
    assert resource["test_topics"] == ["topic"]


def test_worker_step_row_mapping_uses_public_compound_id() -> None:
    now = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    run_id = UUID("99999999-9999-4999-8999-999999999999")
    resource = _run_step_from_row(
        {
            "database_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            "workspace_id": WORKSPACE_ID,
            "run_id": run_id,
            "worker_step_id": f"{run_id}:quality",
            "step_key": "quality",
            "step_type": "quality.evaluate",
            "status": "awaiting_review",
            "queue_name": "run-steps",
            "required_capabilities": [],
            "input_snapshot": '{"topic":"test"}',
            "output_summary": None,
            "output_artifacts": "[]",
            "dependencies": ["render"],
            "attempt_count": 1,
            "max_attempts": 3,
            "error": None,
            "review_required": True,
            "review": json.dumps(
                {
                    "decision": None,
                    "actor_id": None,
                    "comment": None,
                    "requested_at": None,
                    "decided_at": None,
                }
            ),
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "available_at": now,
            "cancellation_requested_at": None,
            "created_at": now,
            "started_at": now,
            "completed_at": None,
            "updated_at": now,
            "retry_policy": {},
            "worker_revision": 4,
        }
    )

    assert resource["id"] == f"{run_id}:quality"
    assert resource["status"] == "awaiting_review"
    assert resource["review_required"] is True
    assert resource["revision"] == 4


def _managed_asset_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 8, 17, 1, 2, 3, tzinfo=UTC)
    row: dict[str, Any] = {
        "id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "workspace_id": WORKSPACE_ID,
        "library_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "kind": "video",
        "title": "Managed asset",
        "description": "Description",
        "metadata": {"tags": ["manual"]},
        "copyright_status": "licensed",
        "status": "quarantined",
        "analysis_status": "completed",
        "revision": 7,
        "deleted_at": None,
        "created_by": USER_ID,
        "created_at": now,
        "updated_at": now,
        "file_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        "storage_provider": "s3",
        "bucket": "framefactory",
        "object_key": f"workspaces/{WORKSPACE_ID}/objects/" + "a" * 32,
        "original_filename": "asset.mp4",
        "media_type": "video/mp4",
        "byte_size": 10,
        "content_hash": "a" * 64,
        "scan_status": "clean",
        "width": 1920,
        "height": 1080,
        "duration_ms": 1000,
        "tags": '["analysis", "manual"]',
    }
    row.update(overrides)
    return row


def test_asset_row_mapping_includes_management_revision_and_safety_gates() -> None:
    resource = _asset_from_row(_managed_asset_row())

    assert resource["analysis_status"] == "completed"
    assert resource["revision"] == 7
    assert resource["tags"] == ["analysis", "manual"]
    assert resource["file"]["scan_status"] == "clean"


def test_postgres_asset_filters_are_workspace_scoped_and_parameterized() -> None:
    connection = _Connection()
    repository = _repository(connection)

    assets = asyncio.run(
        repository.list_assets(
            WORKSPACE_ID,
            UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            statuses=("ready",),
            kinds=("video",),
            copyright_statuses=("licensed",),
            analysis_statuses=("completed",),
            tags=("manual",),
            search="history",
        )
    )

    assert assets == []
    query, args = connection.executions[-1]
    assert "a.workspace_id=$1 AND a.library_id=$2" in query
    assert "a.updated_at DESC, a.id DESC" in query
    assert "cardinality($7::text[])" in query
    assert args[0] == WORKSPACE_ID
    assert args[2] == ["ready"]


def test_postgres_asset_batch_uses_durable_idempotency_ledger() -> None:
    connection = _Connection()
    repository = _repository(connection)

    async def action() -> dict[str, Any]:
        return {
            "succeeded_count": 1,
            "failed_count": 0,
            "succeeded": [{"asset_id": str(UUID(int=1)), "revision": 2}],
            "failures": [],
        }

    result, created = asyncio.run(
        repository.run_asset_batch_idempotently(
            WORKSPACE_ID,
            operation_key=f"{WORKSPACE_ID}:batch:test",
            request_fingerprint="a" * 64,
            request_path="/v1/assets/batch-review",
            action=action,
        )
    )

    sql = "\n".join(query for query, _args in connection.executions)
    assert created is True
    assert result["succeeded_count"] == 1
    assert "INSERT INTO idempotency_keys" in sql
    assert "resource_type='asset_batch_result'" in sql


def test_review_gate_uses_worker_revision_cas_and_appends_audit_action() -> None:
    now = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    run_id = UUID("99999999-9999-4999-8999-999999999999")
    database_step_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    step_id = f"{run_id}:quality"
    current = {
        "database_id": database_step_id,
        "workspace_id": WORKSPACE_ID,
        "run_id": run_id,
        "worker_step_id": step_id,
        "step_key": "quality",
        "step_type": "quality.evaluate",
        "status": "awaiting_review",
        "queue_name": "run-steps",
        "required_capabilities": [],
        "input_snapshot": {"topic": "test"},
        "output_summary": None,
        "output_artifacts": [],
        "dependencies": ["render"],
        "attempt_count": 1,
        "max_attempts": 3,
        "error": None,
        "review_required": True,
        "review": {"requested_at": now.isoformat()},
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "available_at": now,
        "cancellation_requested_at": None,
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "updated_at": now,
        "retry_policy": {
            "base_delay_seconds": 1,
            "max_delay_seconds": 300,
            "multiplier": 2,
        },
        "worker_revision": 4,
    }
    saved = {
        **current,
        "status": "succeeded",
        "review": {
            "decision": "approve",
            "actor_id": str(USER_ID),
            "comment": "Approved",
            "requested_at": now.isoformat(),
            "decided_at": now.isoformat(),
        },
        "completed_at": now,
        "worker_revision": 5,
    }

    class ScriptedConnection(_Connection):
        def __init__(self) -> None:
            super().__init__()
            self.fetchrows = [current, saved]

        async def fetchrow(self, query: str, *args: Any) -> Any:
            self.executions.append((query, args))
            return self.fetchrows.pop(0)

        async def fetch(self, query: str, *args: Any) -> list[Any]:
            self.executions.append((query, args))
            return [{"status": "succeeded"}]

    connection = ScriptedConnection()
    repository = _repository(connection)
    repository._append_run_event = AsyncMock()  # type: ignore[method-assign]
    repository._synchronize_run_status = AsyncMock()  # type: ignore[method-assign]

    reviewed, created = asyncio.run(
        repository.review_run_step_idempotently(
            WORKSPACE_ID,
            step_id,
            decision="approve",
            actor_id=USER_ID,
            comment="Approved",
            issue_codes=[],
            expected_revision=int(current["worker_revision"]),
            operation_key="review:quality:one",
            request_fingerprint="a" * 64,
        )
    )

    sql = "\n".join(query for query, _args in connection.executions)
    review_insert = next(
        args for query, args in connection.executions if "INSERT INTO review_actions" in query
    )
    assert created is True
    assert reviewed["status"] == "succeeded"
    repository._append_run_event.assert_awaited_once()
    repository._synchronize_run_status.assert_awaited_once()
    assert "worker_revision = $8" in sql
    assert "worker_revision = worker_revision + 1" in sql
    assert "THEN static_review_required ELSE review_required END" in sql
    assert review_insert[2] == database_step_id
    assert review_insert[3] == "approved"
    assert review_insert[6] == USER_ID


def test_healthcheck_and_close_use_the_pool_lifecycle() -> None:
    connection = _Connection()
    pool = _Pool(connection)
    repository = PostgreSQLControlRepository(
        pool,
        default_user_id=USER_ID,
        default_workspace_id=WORKSPACE_ID,
        default_workspace_name="FrameFactory",
    )

    asyncio.run(repository.healthcheck())
    asyncio.run(repository.close())

    assert pool.closed is True
    assert "SELECT 1" in connection.executions[0][0]


def test_contract_timestamps_are_normalized_for_asyncpg() -> None:
    value = _datetime_value("2026-08-15T01:02:03Z")

    assert value == datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC)
    assert _datetime_value(None) is None


def test_idempotency_replays_completed_response() -> None:
    connection = _Connection()
    connection.fetchval_result = None
    response = {"id": "44444444-4444-4444-8444-444444444444", "name": "Saved"}
    connection.fetchrow_result = {
        "request_hash": "a" * 64,
        "status": "completed",
        "response_body": json.dumps(response),
    }
    repository = _repository(connection)
    create_called = False

    async def exercise() -> tuple[dict[str, str], bool]:
        nonlocal create_called

        async def create() -> dict[str, str]:
            nonlocal create_called
            create_called = True
            return response

        return await repository._idempotent(
            connection,
            workspace_id=WORKSPACE_ID,
            operation_key="create:one",
            request_fingerprint="a" * 64,
            request_path="/v1/skills",
            response_type="skill",
            create=create,
        )

    replay, created = asyncio.run(exercise())

    assert replay == response
    assert created is False
    assert create_called is False


def test_idempotency_rejects_same_key_with_different_body() -> None:
    connection = _Connection()
    connection.fetchval_result = None
    connection.fetchrow_result = {
        "request_hash": "b" * 64,
        "status": "completed",
        "response_body": "{}",
    }
    repository = _repository(connection)

    async def exercise() -> None:
        async def create() -> dict[str, str]:
            return {}

        await repository._idempotent(
            connection,
            workspace_id=WORKSPACE_ID,
            operation_key="create:one",
            request_fingerprint="a" * 64,
            request_path="/v1/skills",
            response_type="skill",
            create=create,
        )

    with pytest.raises(ConflictError) as exc_info:
        asyncio.run(exercise())

    assert exc_info.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_control_repository_migration_contains_contract_columns() -> None:
    migration = (
        Path(__file__).resolve().parents[3] / "db" / "migrations" / "0002_control_repository.sql"
    ).read_text(encoding="utf-8")

    for column in (
        "revision",
        "test_topics",
        "release_notes",
        "idempotency_key",
        "left_version_id",
        "right_version_id",
    ):
        assert column in migration


def test_api_key_row_mapping_never_includes_secret_hash() -> None:
    now = datetime(2026, 8, 16, 1, 2, 3, tzinfo=UTC)
    resource = _api_key_from_row(
        {
            "id": UUID("66666666-6666-4666-8666-666666666666"),
            "workspace_id": WORKSPACE_ID,
            "name": "Automation",
            "key_prefix": "ffk_example",
            "key_hash": "must-not-be-mapped",
            "scopes": ["skills:read"],
            "created_at": now,
            "expires_at": None,
            "last_used_at": None,
            "revoked_at": None,
        }
    )

    assert resource["key_prefix"] == "ffk_example"
    assert "key_hash" not in resource


def test_account_settings_migration_preserves_hashed_secrets() -> None:
    migration = (
        Path(__file__).resolve().parents[3] / "db" / "migrations" / "0003_account_settings.sql"
    ).read_text(encoding="utf-8")

    assert "creation_preferences" in migration
    assert "revision integer NOT NULL" in migration
    assert "token_hash" not in migration
    assert "api_key text" not in migration


def test_bootstrap_inserts_official_pipeline_before_skill_versions() -> None:
    connection = _Connection()
    skills, versions = load_official_catalog(ContractValidator())
    repository = PostgreSQLControlRepository(
        _Pool(connection),
        default_user_id=USER_ID,
        default_workspace_id=WORKSPACE_ID,
        default_workspace_name="FrameFactory",
        official_skills=skills,
        official_skill_versions=versions,
    )

    asyncio.run(repository.bootstrap())

    statements = [query for query, _ in connection.executions]
    pipeline_version_index = next(
        index for index, query in enumerate(statements) if "INSERT INTO pipeline_versions" in query
    )
    skill_version_index = next(
        index for index, query in enumerate(statements) if "INSERT INTO skill_versions" in query
    )
    assert pipeline_version_index < skill_version_index
    pipeline_args = connection.executions[pipeline_version_index][1]
    assert pipeline_args[0] == UUID("30b37ab7-9cf7-5d26-ac3e-6d66880b536c")
    assert pipeline_args[1] != WORKSPACE_ID


def test_bootstrap_rejects_conflicting_persisted_pipeline_content() -> None:
    connection = _Connection()
    connection.fetchval_result = False
    skills, versions = load_official_catalog(ContractValidator())
    repository = PostgreSQLControlRepository(
        _Pool(connection),
        default_user_id=USER_ID,
        default_workspace_id=WORKSPACE_ID,
        default_workspace_name="FrameFactory",
        official_skills=skills,
        official_skill_versions=versions,
    )

    with pytest.raises(RuntimeError, match="Official PipelineVersion"):
        asyncio.run(repository.bootstrap())


def test_official_pipeline_migration_allows_only_public_system_cross_workspace_refs() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "db"
        / "migrations"
        / "0004_official_pipeline.sql"
    ).read_text(encoding="utf-8")

    assert "runs_skill_version_id_fkey" in migration
    assert "runs_pipeline_version_id_fkey" in migration
    assert "s.ownership_type = 'system'" in migration
    assert "p.visibility = 'public_readonly'" in migration
    assert "w.kind = 'system'" in migration
