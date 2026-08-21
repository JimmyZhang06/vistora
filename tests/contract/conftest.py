"""Shared fixtures and small parsers for the Phase 1 contract suite."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = REPO_ROOT / "packages" / "contracts"
MIGRATIONS_ROOT = REPO_ROOT / "db" / "migrations"
SEEDS_ROOT = REPO_ROOT / "db" / "seeds"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def walk_json(value: Any) -> Iterator[Any]:
    """Yield every JSON value below *value*, including *value* itself."""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


@pytest.fixture(scope="session")
def schema_files() -> list[Path]:
    files = sorted(CONTRACTS_ROOT.glob("schemas/**/*.schema.json"))
    assert files, f"no versioned JSON Schemas found under {CONTRACTS_ROOT}"
    return files


@pytest.fixture(scope="session")
def schemas(schema_files: list[Path]) -> dict[Path, dict[str, Any]]:
    loaded = {path: load_json(path) for path in schema_files}
    assert all(isinstance(schema, dict) for schema in loaded.values())
    return loaded


@pytest.fixture(scope="session")
def schema_store(schemas: dict[Path, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        schema["$id"]: schema
        for schema in schemas.values()
        if isinstance(schema.get("$id"), str)
    }


@pytest.fixture(scope="session")
def migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_ROOT.glob("*.sql"))
    assert files, f"no PostgreSQL migrations found under {MIGRATIONS_ROOT}"
    return files


@pytest.fixture(scope="session")
def migration_sql(migration_files: list[Path]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in migration_files)


@pytest.fixture(scope="session")
def normalized_sql(migration_sql: str) -> str:
    without_comments = re.sub(r"--.*?$|/\*.*?\*/", " ", migration_sql, flags=re.M | re.S)
    return re.sub(r"\s+", " ", without_comments).lower()


@pytest.fixture(scope="session")
def created_tables(normalized_sql: str) -> set[str]:
    return set(
        re.findall(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?",
            normalized_sql,
        )
    )
