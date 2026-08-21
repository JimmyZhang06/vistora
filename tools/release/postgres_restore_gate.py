"""Dump a quiesced PostgreSQL database and restore it into an empty disposable DB."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from release_gate_common import GateBlocked, GateFailure, gate_main, require_env

SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
REQUIRED_TABLES = {"runs", "run_steps", "run_events", "artifacts", "review_actions"}


def _pg_environment(url: str) -> tuple[dict[str, str], str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise GateBlocked("database URLs must be absolute postgres:// or postgresql:// URLs")
    database = parsed.path.lstrip("/")
    if not database or "/" in database:
        raise GateBlocked("database URL must name exactly one database")
    environment = dict(os.environ)
    environment.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGDATABASE": urllib.parse.unquote(database),
            "PGUSER": urllib.parse.unquote(parsed.username or ""),
            "PGPASSWORD": urllib.parse.unquote(parsed.password or ""),
        }
    )
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("sslmode"):
        environment["PGSSLMODE"] = query["sslmode"][0]
    return environment, environment["PGDATABASE"]


def _run(command: list[str], environment: dict[str, str]) -> str:
    try:
        result = subprocess.run(
            command, env=environment, text=True, capture_output=True, timeout=900, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise GateBlocked(f"cannot execute {command[0]}: {exc}") from exc
    if result.returncode:
        raise GateFailure(f"{command[0]} failed ({result.returncode}): {result.stderr[-2000:].strip()}")
    return result.stdout


def _psql(environment: dict[str, str], sql: str) -> str:
    return _run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql], environment)


def _tables(environment: dict[str, str]) -> list[str]:
    output = _psql(
        environment,
        "SELECT tablename FROM pg_catalog.pg_tables "
        "WHERE schemaname = 'public' ORDER BY tablename",
    )
    tables = [line.strip() for line in output.splitlines() if line.strip()]
    if any(not SAFE_IDENTIFIER.fullmatch(table) for table in tables):
        raise GateFailure("database contains a public table name unsafe for release verification")
    return tables


def _counts(environment: dict[str, str], tables: list[str]) -> dict[str, int]:
    return {
        table: int(_psql(environment, f'SELECT count(*) FROM public."{table}"').strip())
        for table in tables
    }


def run_gate() -> dict[str, Any]:
    if os.environ.get("FF_RELEASE_DATABASE_QUIESCED") != "1":
        raise GateBlocked(
            "set FF_RELEASE_DATABASE_QUIESCED=1 only after writers are stopped; "
            "exact backup/restore comparison requires a quiesced source"
        )
    source_env, source_name = _pg_environment(require_env("FF_RELEASE_DATABASE_URL"))
    restore_env, restore_name = _pg_environment(require_env("FF_RELEASE_RESTORE_DATABASE_URL"))
    prefix = os.environ.get("FF_RELEASE_RESTORE_DATABASE_PREFIX", "framefactory_restore_")
    if source_name == restore_name and source_env["PGHOST"] == restore_env["PGHOST"]:
        raise GateBlocked("source and restore databases must be different")
    if not restore_name.startswith(prefix):
        raise GateBlocked(
            f"refusing restore database {restore_name!r}; its name must start with {prefix!r}"
        )
    if _tables(restore_env):
        raise GateBlocked("restore database is not empty; this gate never cleans or overwrites it")

    source_tables = _tables(source_env)
    missing = REQUIRED_TABLES.difference(source_tables)
    if missing:
        raise GateBlocked(f"source database lacks required phase-five tables: {sorted(missing)}")
    source_counts = _counts(source_env, source_tables)
    with tempfile.TemporaryDirectory(prefix="framefactory-release-backup-") as directory:
        dump_path = Path(directory) / "framefactory.dump"
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--serializable-deferrable",
                "--file",
                str(dump_path),
            ],
            source_env,
        )
        if not dump_path.is_file() or dump_path.stat().st_size == 0:
            raise GateFailure("pg_dump produced an empty backup")
        _run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                restore_name,
                str(dump_path),
            ],
            restore_env,
        )
        dump_bytes = dump_path.stat().st_size

    restore_tables = _tables(restore_env)
    if restore_tables != source_tables:
        raise GateFailure("restored public table set differs from the source")
    restore_counts = _counts(restore_env, restore_tables)
    if restore_counts != source_counts:
        differences = {
            table: {"source": source_counts[table], "restore": restore_counts[table]}
            for table in source_tables
            if source_counts[table] != restore_counts[table]
        }
        raise GateFailure(f"restored row counts differ from the source: {differences}")
    _psql(restore_env, "SET CONSTRAINTS ALL IMMEDIATE; SELECT 1")
    return {"tables": len(source_tables), "rows": sum(source_counts.values()), "dump_bytes": dump_bytes}


if __name__ == "__main__":
    gate_main(run_gate)
