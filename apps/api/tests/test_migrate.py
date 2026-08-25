from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from framefactory_api.migrate import (
    _BASELINE_COLUMNS,
    ADVISORY_LOCK_ID,
    Migration,
    MigrationError,
    MigrationResult,
    discover_migrations,
    migrate_connection,
    migration_body,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        *,
        ledger_existed: bool = False,
        ledger: list[dict[str, str]] | None = None,
        present: set[tuple[str, str]] | None = None,
    ) -> None:
        self.ledger_existed = ledger_existed
        self.ledger = ledger or []
        self.present = present or set()
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_count = 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if "INSERT INTO public.schema_migrations" in sql:
            self.ledger.append(
                {"version": args[0], "path": args[1], "checksum_sha256": args[2]}
            )
        return "OK"

    async def fetchval(self, _sql: str) -> bool:
        return self.ledger_existed

    async def fetch(self, sql: str) -> list[dict[str, str]]:
        if "table_name = 'schema_migrations'" in sql:
            return [
                {"column_name": name}
                for name in ("version", "path", "checksum_sha256", "applied_at")
            ]
        if "FROM public.schema_migrations" in sql:
            return list(self.ledger)
        if "FROM information_schema.columns" in sql:
            return [
                {"table_name": table, "column_name": column}
                for table, column in self.present
            ]
        raise AssertionError(f"unexpected fetch: {sql}")

    def transaction(self) -> _Transaction:
        self.transaction_count += 1
        return _Transaction()


def _migration(path: str, sql: str = "SELECT 42;") -> Migration:
    return Migration(
        version=Path(path).stem,
        path=path,
        checksum_sha256=hashlib.sha256(sql.encode()).hexdigest(),
        sql=sql,
    )


def test_discover_migrations_orders_control_plane_before_worker(tmp_path: Path) -> None:
    control = tmp_path / "db" / "migrations"
    worker = tmp_path / "services" / "worker" / "migrations"
    control.mkdir(parents=True)
    worker.mkdir(parents=True)
    (control / "0002_second.sql").write_text("SELECT 2;", encoding="utf-8")
    (control / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (worker / "0001_worker.sql").write_text("SELECT 3;", encoding="utf-8")

    migrations = discover_migrations(tmp_path)

    assert [migration.path for migration in migrations] == [
        "db/migrations/0001_first.sql",
        "db/migrations/0002_second.sql",
        "services/worker/migrations/0001_worker.sql",
    ]
    assert migrations[0].checksum_sha256 == hashlib.sha256(b"SELECT 1;").hexdigest()


def test_discover_migrations_requires_both_owned_directories(tmp_path: Path) -> None:
    (tmp_path / "db" / "migrations").mkdir(parents=True)

    with pytest.raises(MigrationError, match="does not exist"):
        discover_migrations(tmp_path)


def test_migration_body_removes_only_explicit_outer_transaction() -> None:
    assert migration_body("BEGIN;\nSELECT 1;\nCOMMIT;\n") == "SELECT 1;"
    assert migration_body("SELECT 1;") == "SELECT 1;"


def test_event_observability_migration_is_discovered_and_baseline_proven() -> None:
    root = Path(__file__).resolve().parents[3]
    migration = next(
        item
        for item in discover_migrations(root)
        if item.path == "db/migrations/0005_run_event_observability.sql"
    )

    assert "run_events_deduplication_idx" in migration.sql
    assert "ALTER COLUMN deduplication_key SET NOT NULL" in migration.sql
    assert _BASELINE_COLUMNS[migration.path] == (
        ("run_events", "deduplication_key"),
        ("run_events", "correlation_id"),
        ("run_events", "causation_id"),
    )


@pytest.mark.asyncio
async def test_fresh_database_applies_sql_and_ledger_in_same_transaction() -> None:
    connection = FakeConnection()
    migration = _migration("db/migrations/9000_test.sql", "BEGIN; SELECT 7; COMMIT;")

    results = await migrate_connection(connection, [migration])

    assert results[0].action == "applied"
    assert connection.transaction_count == 1
    assert connection.ledger[0]["path"] == migration.path
    executed_sql = [sql for sql, _args in connection.executed]
    assert "SELECT 7;" in executed_sql
    assert connection.executed[0][1] == (ADVISORY_LOCK_ID,)
    assert connection.executed[-1][1] == (ADVISORY_LOCK_ID,)


@pytest.mark.asyncio
async def test_legacy_database_baselines_only_with_complete_column_proof() -> None:
    migration = _migration("db/migrations/0003_account_settings.sql")
    connection = FakeConnection(present=set(_BASELINE_COLUMNS[migration.path]))

    results = await migrate_connection(connection, [migration])

    assert results[0].action == "baselined"
    assert migration.sql not in [sql for sql, _args in connection.executed]


@pytest.mark.asyncio
async def test_legacy_database_rejects_partial_migration_proof() -> None:
    migration = _migration("db/migrations/0003_account_settings.sql")
    connection = FakeConnection(present={_BASELINE_COLUMNS[migration.path][0]})

    with pytest.raises(MigrationError, match="partially applied"):
        await migrate_connection(connection, [migration])

    assert connection.executed[-1][1] == (ADVISORY_LOCK_ID,)


@pytest.mark.asyncio
async def test_checksum_drift_fails_before_any_migration_is_executed() -> None:
    migration = _migration("db/migrations/0001_initial.sql")
    connection = FakeConnection(
        ledger_existed=True,
        ledger=[
            {
                "version": migration.version,
                "path": migration.path,
                "checksum_sha256": "0" * 64,
            }
        ],
    )

    with pytest.raises(MigrationError, match="checksum drift"):
        await migrate_connection(connection, [migration])

    assert connection.transaction_count == 0


@pytest.mark.asyncio
async def test_known_webpage_migration_predecessor_is_accepted_exactly() -> None:
    root = Path(__file__).resolve().parents[3]
    migration = next(
        item
        for item in discover_migrations(root)
        if item.path == "db/migrations/0022_webpage_video_control_plane.sql"
    )
    connection = FakeConnection(
        ledger_existed=True,
        ledger=[
            {
                "version": migration.version,
                "path": migration.path,
                "checksum_sha256": (
                    "f124c732997e5d52327201a4828054ea2bc8c518e79f126a814a7730d4bef00a"
                ),
            }
        ],
    )

    results = await migrate_connection(connection, [migration])

    assert results == (MigrationResult(migration.path, "unchanged"),)
    assert connection.transaction_count == 0


@pytest.mark.asyncio
async def test_missing_applied_migration_file_fails_closed() -> None:
    connection = FakeConnection(
        ledger_existed=True,
        ledger=[
            {
                "version": "0000_removed",
                "path": "db/migrations/0000_removed.sql",
                "checksum_sha256": "0" * 64,
            }
        ],
    )

    with pytest.raises(MigrationError, match="missing from the release"):
        await migrate_connection(connection, [_migration("db/migrations/0001_initial.sql")])

    assert connection.transaction_count == 0
