from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from .errors import ApiError
from .models import StrictModel

AccountResource = dict[str, Any]


class AccountRepository(Protocol):
    async def ensure_account(
        self,
        user_id: UUID,
        workspace_id: UUID,
        *,
        email: str,
        display_name: str,
    ) -> None: ...

    async def get_account_profile(
        self, workspace_id: UUID, user_id: UUID
    ) -> AccountResource: ...

    async def replace_account_profile(
        self, resource: AccountResource, *, expected_revision: int
    ) -> AccountResource: ...

    async def get_creation_preferences(
        self, workspace_id: UUID, user_id: UUID
    ) -> AccountResource: ...

    async def replace_creation_preferences(
        self, resource: AccountResource, *, expected_revision: int
    ) -> AccountResource: ...

    async def list_account_sessions(
        self, workspace_id: UUID, user_id: UUID
    ) -> list[AccountResource]: ...

    async def revoke_account_session(
        self, workspace_id: UUID, user_id: UUID, session_id: UUID
    ) -> AccountResource: ...

    async def list_api_keys(
        self, workspace_id: UUID, user_id: UUID
    ) -> list[AccountResource]: ...

    async def create_api_key(self, resource: AccountResource) -> AccountResource: ...

    async def revoke_api_key(
        self, workspace_id: UUID, user_id: UUID, key_id: UUID
    ) -> AccountResource: ...


class ProfileResponse(StrictModel):
    user_id: UUID
    workspace_id: UUID
    email: str
    display_name: str
    avatar_url: str | None
    locale: str
    timezone: str
    revision: int = Field(ge=1)
    updated_at: datetime


class ProfileUpdate(StrictModel):
    display_name: str = Field(min_length=1, max_length=160)
    avatar_url: str | None = Field(default=None, max_length=2048)
    locale: str = Field(default="zh-CN", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)

    @field_validator("display_name")
    @classmethod
    def display_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("avatar_url must use HTTPS (localhost HTTP is allowed)")
        return value


class CreationPreferencesResponse(StrictModel):
    user_id: UUID
    workspace_id: UUID
    default_language: str
    default_aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3"]
    default_duration_seconds: int = Field(ge=15, le=3600)
    default_visibility: Literal["private", "workspace"]
    auto_quality_check: bool
    revision: int = Field(ge=1)
    updated_at: datetime


class CreationPreferencesUpdate(StrictModel):
    default_language: str = Field(
        default="zh-CN", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
    )
    default_aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3"] = "16:9"
    default_duration_seconds: int = Field(default=180, ge=15, le=3600)
    default_visibility: Literal["private", "workspace"] = "private"
    auto_quality_check: bool = True


class SessionResponse(StrictModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    user_agent: str | None
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None


ApiKeyScope = Literal[
    "account:read",
    "skills:read",
    "skills:write",
    "runs:read",
    "runs:write",
    "url_capture:read",
    "url_capture:write",
    "url_capture:review",
]


class ApiKeyCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[ApiKeyScope] = Field(default_factory=lambda: ["skills:read"], max_length=16)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[ApiKeyScope]) -> list[ApiKeyScope]:
        if not value:
            raise ValueError("at least one scope is required")
        if len(value) != len(set(value)):
            raise ValueError("scopes must be unique")
        return value


class ApiKeyResponse(StrictModel):
    id: UUID
    workspace_id: UUID
    name: str
    key_prefix: str
    scopes: list[ApiKeyScope]
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedResponse(StrictModel):
    key: ApiKeyResponse
    api_key: str = Field(description="Returned once. Store it now; it cannot be recovered.")


class AccountCapabilitiesResponse(StrictModel):
    profile: Literal["available"] = "available"
    creation_preferences: Literal["available"] = "available"
    sessions: Literal["not_recorded"] = "not_recorded"
    api_keys: Literal["management_only"] = "management_only"
    two_factor_authentication: Literal["unavailable"] = "unavailable"


class AccountService:
    def __init__(self, repository: AccountRepository) -> None:
        self.repository = repository

    async def update_profile(
        self,
        workspace_id: UUID,
        user_id: UUID,
        command: ProfileUpdate,
        expected_revision: int,
    ) -> AccountResource:
        current = await self.repository.get_account_profile(workspace_id, user_id)
        updated = {
            **current,
            **command.model_dump(),
            "revision": int(current["revision"]) + 1,
            "updated_at": _now(),
        }
        return await self.repository.replace_account_profile(
            updated, expected_revision=expected_revision
        )

    async def update_creation_preferences(
        self,
        workspace_id: UUID,
        user_id: UUID,
        command: CreationPreferencesUpdate,
        expected_revision: int,
    ) -> AccountResource:
        current = await self.repository.get_creation_preferences(workspace_id, user_id)
        updated = {
            **current,
            **command.model_dump(),
            "revision": int(current["revision"]) + 1,
            "updated_at": _now(),
        }
        return await self.repository.replace_creation_preferences(
            updated, expected_revision=expected_revision
        )

    async def create_api_key(
        self,
        workspace_id: UUID,
        user_id: UUID,
        command: ApiKeyCreate,
    ) -> tuple[AccountResource, str]:
        now = datetime.now(UTC)
        if command.expires_at is not None:
            expires_at = command.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                raise ApiError(
                    422,
                    "API_KEY_EXPIRY_INVALID",
                    "API key expiry must be in the future",
                )
        else:
            expires_at = None

        secret = "ffk_" + secrets.token_urlsafe(36)
        resource: AccountResource = {
            "id": str(uuid4()),
            "workspace_id": str(workspace_id),
            "name": command.name,
            "key_prefix": secret[:12],
            "key_hash": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            "scopes": command.scopes,
            "created_by": str(user_id),
            "created_at": now,
            "expires_at": expires_at,
            "last_used_at": None,
            "revoked_at": None,
        }
        return await self.repository.create_api_key(resource), secret


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
