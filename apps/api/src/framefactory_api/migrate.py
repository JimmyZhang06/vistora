from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .environment import environment_value

ADVISORY_LOCK_ID = 0x4652414D45464143  # "FRAMEFAC", within PostgreSQL's signed bigint range.
MIGRATION_DIRECTORIES = ("db/migrations", "services/worker/migrations")

# A deployed pre-release build applied the webpage control-plane migration
# before its status comment was added and before the active-run index was moved
# into a convergence migration.  Accept exactly that one immutable predecessor
# only while the packaged successor checksum also matches.  Migration 0023
# makes fresh and upgraded databases converge on the same schema.
_COMPATIBLE_CHECKSUM_TRANSITIONS = {
    "db/migrations/0022_webpage_video_control_plane.sql": (
        "f124c732997e5d52327201a4828054ea2bc8c518e79f126a814a7730d4bef00a",
        "43f75b1cc2125e02f29b571df0c72837d7ee752cd600f95301e15972d3d503bd",
    )
}


class MigrationError(RuntimeError):
    """Raised when migrations cannot be applied without risking schema corruption."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    path: str
    checksum_sha256: str
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationResult:
    path: str
    action: Literal["applied", "baselined", "unchanged"]


# A legacy database can be baselined only when the objects that distinguish an
# old migration are all present.  The initial migration is transactional, so
# proving every table exists is sufficient to distinguish a completed run from
# an interrupted one.  Additive migrations also prove their introduced columns.
_INITIAL_TABLES = (
    "users",
    "workspaces",
    "workspace_members",
    "sessions",
    "pipelines",
    "pipeline_versions",
    "skills",
    "skill_versions",
    "skill_evaluations",
    "asset_libraries",
    "assets",
    "asset_files",
    "asset_sources",
    "tags",
    "asset_tags",
    "voice_profiles",
    "render_presets",
    "render_preset_versions",
    "channels",
    "channel_defaults",
    "runs",
    "run_steps",
    "run_events",
    "artifacts",
    "review_actions",
    "api_keys",
    "idempotency_keys",
    "webhooks",
    "webhook_deliveries",
    "audit_logs",
    "usage_records",
    "outbox_events",
)

_COMPOSITE_ROOT_COLUMNS = {
    "workspace_members": "workspace_id",
    "channel_defaults": "channel_id",
    "asset_tags": "asset_id",
}

_BASELINE_COLUMNS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "db/migrations/0001_initial.sql": tuple(
        (table, _COMPOSITE_ROOT_COLUMNS.get(table, "id")) for table in _INITIAL_TABLES
    ),
    "db/migrations/0002_control_repository.sql": (
        ("skills", "schema_version"),
        ("skills", "revision"),
        ("skill_versions", "revision"),
        ("skill_versions", "test_topics"),
        ("skill_versions", "release_notes"),
        ("runs", "schema_version"),
        ("runs", "ownership_type"),
        ("runs", "idempotency_key"),
        ("skill_evaluations", "schema_version"),
        ("skill_evaluations", "skill_id"),
        ("skill_evaluations", "left_version_id"),
        ("skill_evaluations", "right_version_id"),
        ("skill_evaluations", "updated_at"),
    ),
    "db/migrations/0003_account_settings.sql": (
        ("users", "avatar_url"),
        ("users", "locale"),
        ("users", "timezone"),
        ("users", "revision"),
        ("creation_preferences", "workspace_id"),
        ("creation_preferences", "user_id"),
        ("creation_preferences", "revision"),
    ),
    "services/worker/migrations/0001_runtime_state.sql": (
        ("runs", "worker_revision"),
        ("runs", "worker_error"),
        ("run_steps", "worker_step_id"),
        ("run_steps", "dependencies"),
        ("run_steps", "retry_policy"),
        ("run_steps", "output_artifacts"),
        ("run_steps", "review_required"),
        ("run_steps", "review"),
        ("run_steps", "cancellation_requested_at"),
        ("run_steps", "worker_revision"),
    ),
    "db/migrations/0005_run_event_observability.sql": (
        ("run_events", "deduplication_key"),
        ("run_events", "correlation_id"),
        ("run_events", "causation_id"),
    ),
    "db/migrations/0009_asset_content_management.sql": (
        ("assets", "revision"),
        ("assets", "analysis_status"),
        ("assets", "deleted_at"),
        ("asset_sources", "evidence_type"),
        ("asset_analysis_jobs", "id"),
        ("asset_usage_records", "id"),
    ),
    "db/migrations/0026_document_video_control_plane.sql": (
        ("document_sources", "id"),
        ("document_sources", "workspace_id"),
        ("document_sources", "object_key"),
        ("document_sources", "validation"),
        ("document_sources", "revision"),
    ),
    "db/migrations/0027_document_retention_and_purge.sql": (
        ("document_sources", "upload_expires_at"),
        ("document_sources", "retention_until"),
        ("document_sources", "legal_hold"),
        ("document_sources", "purged_at"),
        ("document_purge_requests", "id"),
        ("document_purge_requests", "lease_token"),
        ("document_purge_requests", "last_error"),
    ),
}

_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
  version text NOT NULL,
  path text NOT NULL,
  checksum_sha256 char(64) NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version, path),
  UNIQUE (path),
  CONSTRAINT schema_migrations_checksum_format
    CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
)
"""


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def discover_migrations(project_root: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for relative_directory in MIGRATION_DIRECTORIES:
        directory = project_root / relative_directory
        if not directory.is_dir():
            raise MigrationError(f"migration directory does not exist: {directory}")
        for sql_path in sorted(directory.glob("*.sql"), key=lambda path: path.name):
            sql = sql_path.read_text(encoding="utf-8")
            relative_path = sql_path.relative_to(project_root).as_posix()
            migrations.append(
                Migration(
                    version=sql_path.stem,
                    path=relative_path,
                    checksum_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                    sql=sql,
                )
            )
    if not migrations:
        raise MigrationError("no SQL migrations were discovered")
    return tuple(migrations)


def migration_body(sql: str) -> str:
    """Remove one explicit outer transaction so SQL and ledger commit atomically."""
    match = re.fullmatch(
        r"\s*BEGIN\s*;(?P<body>.*)COMMIT\s*;\s*",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return match.group("body").strip() if match else sql.strip()


async def _present_columns(connection: Any) -> set[tuple[str, str]]:
    rows = await connection.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        """
    )
    return {(row["table_name"], row["column_name"]) for row in rows}


def _baseline_decision(
    migration: Migration, present: set[tuple[str, str]]
) -> Literal["baseline", "apply"]:
    proof = _BASELINE_COLUMNS.get(migration.path)
    if not proof:
        # Migrations without a table/column signature must be written so that
        # replay on a legacy database is safe.  SQL errors are never swallowed.
        return "apply"
    found = sum(item in present for item in proof)
    if found == len(proof):
        return "baseline"
    if found == 0:
        return "apply"
    missing = ", ".join(
        f"{table}.{column}" for table, column in proof if (table, column) not in present
    )
    raise MigrationError(
        f"cannot safely baseline partially applied migration {migration.path}; "
        f"missing proof: {missing}"
    )


async def migrate_connection(
    connection: Any, migrations: Sequence[Migration]
) -> tuple[MigrationResult, ...]:
    await connection.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_ID)
    try:
        ledger_existed = bool(
            await connection.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        )
        await connection.execute(_LEDGER_SQL)
        ledger_columns = await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'schema_migrations'
            """
        )
        required = {"version", "path", "checksum_sha256", "applied_at"}
        available = {row["column_name"] for row in ledger_columns}
        if not required <= available:
            raise MigrationError("schema_migrations exists with an incompatible schema")

        ledger_rows = await connection.fetch(
            "SELECT version, path, checksum_sha256 FROM public.schema_migrations"
        )
        ledger = {row["path"]: row for row in ledger_rows}
        known_paths = {migration.path for migration in migrations}
        missing_files = sorted(set(ledger) - known_paths)
        if missing_files:
            raise MigrationError(
                "applied migration files are missing from the release: " + ", ".join(missing_files)
            )

        # Validate the complete known history before making any change.
        for migration in migrations:
            recorded = ledger.get(migration.path)
            if recorded is None:
                continue
            if recorded["version"] != migration.version:
                raise MigrationError(
                    f"migration version changed for {migration.path}: "
                    f"database={recorded['version']} file={migration.version}"
                )
            recorded_checksum = str(recorded["checksum_sha256"]).strip()
            if recorded_checksum != migration.checksum_sha256:
                compatible_transition = _COMPATIBLE_CHECKSUM_TRANSITIONS.get(migration.path)
                if compatible_transition != (
                    recorded_checksum,
                    migration.checksum_sha256,
                ):
                    raise MigrationError(
                        f"migration checksum drift detected for {migration.path}: "
                        f"database={recorded_checksum} file={migration.checksum_sha256}"
                    )

        present = await _present_columns(connection) if not ledger_existed else set()
        results: list[MigrationResult] = []
        for migration in migrations:
            if migration.path in ledger:
                results.append(MigrationResult(migration.path, "unchanged"))
                continue

            if not ledger_existed and _baseline_decision(migration, present) == "baseline":
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO public.schema_migrations (version, path, checksum_sha256)
                        VALUES ($1, $2, $3)
                        """,
                        migration.version,
                        migration.path,
                        migration.checksum_sha256,
                    )
                results.append(MigrationResult(migration.path, "baselined"))
                continue

            async with connection.transaction():
                await connection.execute(migration_body(migration.sql))
                await connection.execute(
                    """
                    INSERT INTO public.schema_migrations (version, path, checksum_sha256)
                    VALUES ($1, $2, $3)
                    """,
                    migration.version,
                    migration.path,
                    migration.checksum_sha256,
                )
            results.append(MigrationResult(migration.path, "applied"))
            present = await _present_columns(connection) if not ledger_existed else present
        return tuple(results)
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)


async def run_migrations(database_url: str, project_root: Path) -> tuple[MigrationResult, ...]:
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - packaging/runtime guard
        raise MigrationError("database migrations require asyncpg") from exc

    migrations = discover_migrations(project_root)
    connection = await asyncpg.connect(database_url)
    try:
        return await migrate_connection(connection, migrations)
    finally:
        await connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply FrameFactory PostgreSQL migrations")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=default_project_root(),
        help="directory containing db/migrations and services/worker/migrations",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = environment_value("FRAMEFACTORY_DATABASE_URL")
    if not database_url:
        raise MigrationError("FRAMEFACTORY_DATABASE_URL or its _FILE variant is required")
    results = asyncio.run(run_migrations(database_url, args.project_root.resolve()))
    for result in results:
        print(f"{result.action}: {result.path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
