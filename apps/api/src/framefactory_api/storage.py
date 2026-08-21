from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID


class ObjectStorageError(RuntimeError):
    """Base error raised by an object storage adapter."""


class ObjectStorageUnavailable(ObjectStorageError):
    """The configured object storage service could not complete an operation."""


class ObjectNotFound(ObjectStorageError):
    """An object does not exist, or is outside the requested workspace."""


class ObjectIntegrityError(ObjectStorageError):
    """Stored content or metadata does not match its durable object descriptor."""


@dataclass(frozen=True, slots=True)
class ObjectLocator:
    """The durable object identity that application persistence should retain."""

    key: str
    sha256: str
    content_type: str


@dataclass(frozen=True, slots=True)
class PresignedRequest:
    """A time-limited request whose headers are part of the S3 signature contract."""

    method: str
    url: str
    headers: Mapping[str, str]
    expires_at: datetime
    object: ObjectLocator


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    sha256: str
    content_type: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None


@dataclass(frozen=True, slots=True)
class ObjectStorageHealth:
    status: str
    bucket: str
    endpoint: str | None


class ObjectStorage(Protocol):
    async def initiate_upload(
        self,
        workspace_id: UUID,
        *,
        sha256: str,
        content_type: str,
        expires_in: int | None = None,
    ) -> PresignedRequest: ...

    async def complete_upload(
        self, workspace_id: UUID, object: ObjectLocator
    ) -> StoredObject: ...

    async def upload_file(
        self, workspace_id: UUID, object: ObjectLocator, path: Path
    ) -> None: ...

    async def presign_download(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        expires_in: int | None = None,
    ) -> PresignedRequest: ...

    async def read_bytes(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        maximum_bytes: int,
    ) -> bytes: ...

    async def healthcheck(self) -> ObjectStorageHealth: ...
