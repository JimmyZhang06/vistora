from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "schemas" / "v1"


def contract_example(filename: str) -> dict[str, Any]:
    schema = json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    return deepcopy(schema["examples"][0])


@pytest.fixture
def repository() -> InMemoryControlRepository:
    pipeline = contract_example("pipeline.schema.json")
    pipeline["ownership_type"] = "system"
    pipeline["visibility"] = "public_readonly"
    pipeline["status"] = "active"
    pipeline["published_at"] = pipeline["created_at"]
    return InMemoryControlRepository(pipelines=[pipeline])


@pytest.fixture
def client(repository: InMemoryControlRepository) -> TestClient:
    with TestClient(create_app(repository=repository)) as test_client:
        yield test_client
