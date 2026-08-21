from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from .environment import environment_value

DEVELOPMENT_USER_ID = uuid5(NAMESPACE_URL, "framefactory.local/installation/default-user")
DEVELOPMENT_WORKSPACE_ID = uuid5(
    NAMESPACE_URL, "framefactory.local/installation/default-workspace"
)
DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

RepositoryBackend = Literal["memory", "postgresql"]


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "Vistora Control API"
    environment: str = "development"
    default_user_id: UUID = DEVELOPMENT_USER_ID
    default_workspace_id: UUID = DEVELOPMENT_WORKSPACE_ID
    default_workspace_name: str = "My Vistora"
    default_user_email: str = "owner@local.framefactory.invalid"
    default_user_display_name: str = "Vistora Owner"
    contract_schema_dir: Path | None = None
    official_seed_manifest: Path | None = None
    repository_backend: RepositoryBackend = "memory"
    database_url: str | None = None
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10
    redis_enabled: bool = False
    object_storage_enabled: bool = False
    s3_create_bucket: bool = False
    worker_capabilities: tuple[str, ...] | None = None
    cors_allow_origins: tuple[str, ...] = DEVELOPMENT_CORS_ORIGINS
    cors_allow_origin_regex: str | None = r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$"

    def __post_init__(self) -> None:
        normalized_environment = self.environment.strip().lower()
        if self.repository_backend not in {"memory", "postgresql"}:
            raise ValueError(
                "FRAMEFACTORY_REPOSITORY_BACKEND must be 'memory' or 'postgresql'"
            )
        if (
            normalized_environment in {"production", "prod"}
            and self.repository_backend != "postgresql"
        ):
            raise ValueError("production requires the PostgreSQL repository backend")
        if normalized_environment in {"production", "prod"} and (
            self.default_user_id == DEVELOPMENT_USER_ID
            or self.default_workspace_id == DEVELOPMENT_WORKSPACE_ID
        ):
            raise ValueError(
                "production requires explicit default user and workspace identifiers"
            )
        if self.repository_backend == "postgresql" and not self.database_url:
            raise ValueError(
                "FRAMEFACTORY_DATABASE_URL is required for the PostgreSQL repository backend"
            )
        if self.postgres_pool_min_size < 1:
            raise ValueError("PostgreSQL minimum pool size must be at least 1")
        if self.postgres_pool_max_size < self.postgres_pool_min_size:
            raise ValueError(
                "PostgreSQL maximum pool size must be greater than or equal to the minimum"
            )

    @classmethod
    def from_environment(cls) -> Settings:
        schema_dir = os.getenv("FRAMEFACTORY_CONTRACT_SCHEMA_DIR")
        seed_manifest = os.getenv("FRAMEFACTORY_OFFICIAL_SEED_MANIFEST")
        environment = os.getenv("FRAMEFACTORY_ENV", "development").strip().lower()
        default_repository = (
            "postgresql" if environment in {"production", "prod"} else "memory"
        )
        repository_backend = os.getenv(
            "FRAMEFACTORY_REPOSITORY_BACKEND", default_repository
        ).strip().lower()
        configured_origins = os.getenv("FRAMEFACTORY_CORS_ALLOW_ORIGINS")
        configured_worker_capabilities = os.getenv("FRAMEFACTORY_WORKER_CAPABILITIES")
        return cls(
            environment=environment,
            default_user_id=UUID(
                os.getenv("FRAMEFACTORY_DEFAULT_USER_ID", str(DEVELOPMENT_USER_ID))
            ),
            default_workspace_id=UUID(
                os.getenv("FRAMEFACTORY_DEFAULT_WORKSPACE_ID", str(DEVELOPMENT_WORKSPACE_ID))
            ),
            default_workspace_name=os.getenv(
                "FRAMEFACTORY_DEFAULT_WORKSPACE_NAME", "My Vistora"
            ),
            default_user_email=os.getenv(
                "FRAMEFACTORY_DEFAULT_USER_EMAIL", "owner@local.framefactory.invalid"
            ),
            default_user_display_name=os.getenv(
                "FRAMEFACTORY_DEFAULT_USER_DISPLAY_NAME", "Vistora Owner"
            ),
            contract_schema_dir=Path(schema_dir).resolve() if schema_dir else None,
            official_seed_manifest=(
                Path(seed_manifest).resolve() if seed_manifest else None
            ),
            repository_backend=cast(RepositoryBackend, repository_backend),
            database_url=environment_value("FRAMEFACTORY_DATABASE_URL"),
            postgres_pool_min_size=_environment_int(
                "FRAMEFACTORY_POSTGRES_POOL_MIN_SIZE", 1
            ),
            postgres_pool_max_size=_environment_int(
                "FRAMEFACTORY_POSTGRES_POOL_MAX_SIZE", 10
            ),
            redis_enabled=_environment_bool(
                "FRAMEFACTORY_REDIS_ENABLED",
                default=bool(environment_value("FRAMEFACTORY_REDIS_URL")),
            ),
            object_storage_enabled=_environment_bool(
                "FRAMEFACTORY_OBJECT_STORAGE_ENABLED",
                default=bool(os.getenv("FRAMEFACTORY_S3_BUCKET")),
            ),
            s3_create_bucket=_environment_bool(
                "FRAMEFACTORY_S3_CREATE_BUCKET", default=False
            ),
            worker_capabilities=(
                tuple(
                    sorted(
                        {
                            capability.strip()
                            for capability in configured_worker_capabilities.split(",")
                            if capability.strip()
                        }
                    )
                )
                if configured_worker_capabilities is not None
                else (() if environment in {"production", "prod"} else None)
            ),
            cors_allow_origins=(
                tuple(origin.strip() for origin in configured_origins.split(",") if origin.strip())
                if configured_origins is not None
                else (DEVELOPMENT_CORS_ORIGINS if environment == "development" else ())
            ),
            cors_allow_origin_regex=(
                os.getenv("FRAMEFACTORY_CORS_ALLOW_ORIGIN_REGEX")
                or (
                    r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$"
                    if environment == "development"
                    else None
                )
            ),
        )


def _environment_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _environment_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
