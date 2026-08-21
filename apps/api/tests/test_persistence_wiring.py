from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import framefactory_api.main as main_module
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings


class ManagedRepository(InMemoryControlRepository):
    def __init__(self) -> None:
        super().__init__()
        self.healthchecks = 0
        self.closes = 0

    async def healthcheck(self) -> None:
        self.healthchecks += 1

    async def close(self) -> None:
        self.closes += 1


class ManagedDependency:
    def __init__(self) -> None:
        self.healthchecks = 0
        self.closes = 0

    async def healthcheck(self) -> None:
        self.healthchecks += 1

    async def close(self) -> None:
        self.closes += 1


def test_production_rejects_memory_repository() -> None:
    with pytest.raises(ValueError, match="production requires"):
        Settings(environment="production", repository_backend="memory")


def test_postgresql_repository_requires_database_url() -> None:
    with pytest.raises(ValueError, match="FRAMEFACTORY_DATABASE_URL"):
        Settings(repository_backend="postgresql")


def test_production_requires_installation_identity() -> None:
    with pytest.raises(ValueError, match="explicit default user"):
        Settings(
            environment="production",
            repository_backend="postgresql",
            database_url="postgresql://example.invalid/framefactory",
        )


def test_secret_file_settings_are_supported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    database_secret = tmp_path / "database-url"
    redis_secret = tmp_path / "redis-url"
    database_secret.write_text("postgresql://db/framefactory\n", encoding="utf-8")
    redis_secret.write_text("redis://queue/0\n", encoding="utf-8")
    monkeypatch.setenv("FRAMEFACTORY_DATABASE_URL_FILE", str(database_secret))
    monkeypatch.setenv("FRAMEFACTORY_REDIS_URL_FILE", str(redis_secret))
    monkeypatch.setenv("FRAMEFACTORY_REPOSITORY_BACKEND", "postgresql")

    settings = Settings.from_environment()

    assert settings.database_url == "postgresql://db/framefactory"
    assert settings.redis_enabled is True


def test_lifespan_healthchecks_and_closes_owned_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = ManagedRepository()
    queue = ManagedDependency()
    storage = ManagedDependency()

    async def create_repository(*_args: Any, **_kwargs: Any) -> ManagedRepository:
        return repository

    def create_queue() -> ManagedDependency:
        return queue

    async def create_storage(_settings: Settings) -> ManagedDependency:
        return storage

    monkeypatch.setattr(main_module, "_create_repository", create_repository)
    monkeypatch.setattr(main_module, "_create_redis_queue", create_queue)
    monkeypatch.setattr(main_module, "_create_object_storage", create_storage)

    settings = Settings(
        repository_backend="postgresql",
        database_url="postgresql://example.invalid/framefactory",
        redis_enabled=True,
        object_storage_enabled=True,
    )
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["persistence"] == "postgresql"
        assert repository.healthchecks == 2
        assert queue.healthchecks == 2
        assert storage.healthchecks == 2

    assert repository.closes == 1
    assert queue.closes == 1
    assert storage.closes == 1
