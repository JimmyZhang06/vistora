from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from .environment import environment_value
from .storage import (
    ObjectIntegrityError,
    ObjectLocator,
    ObjectNotFound,
    ObjectStorageHealth,
    ObjectStorageUnavailable,
    PresignedRequest,
    StoredObject,
)

AddressingStyle = Literal["auto", "path", "virtual"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_OBJECT_KEY_RE = re.compile(
    r"^workspaces/(?P<workspace>[0-9a-f-]{36})/objects/(?P<object>[0-9a-f]{32})$"
)
_ARTIFACT_KEY_RE = re.compile(
    r"^workspaces/(?P<workspace>[0-9a-f-]{36})/runs/[0-9a-f-]{36}/"
    r"artifacts/[0-9a-f-]{36}/[^/]+$"
)
_ASSET_DERIVED_KEY_RE = re.compile(
    r"^workspaces/(?P<workspace>[0-9a-f-]{36})/asset-analysis/"
    r"[0-9a-f-]{36}/[0-9a-f]{64}/"
    r"(?:preview/[0-9a-f]{16}\.(?:jpg|mp4)|frames/[0-9]{3}-[0-9a-f]{16}\.jpg)$"
)
_ASSET_SOURCE_KEY_RE = re.compile(
    r"^workspaces/(?P<workspace>[0-9a-f-]{36})/assets/(?P<source_hash>[0-9a-f]{64})/"
    r"(?P<filename>(?!\.\.?$)[^/\x00-\x1f]{1,255})$"
)
_NOT_FOUND_CODES = {"404", "NoSuchBucket", "NoSuchKey", "NotFound"}


class S3Client(Protocol):
    def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str: ...

    def head_bucket(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def create_bucket(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def head_object(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def get_object(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def upload_file(self, *args: Any, **kwargs: Any) -> None: ...


class S3StorageConfigurationError(ValueError):
    """Invalid S3/R2 configuration detected before any network call is made."""


@dataclass(frozen=True, slots=True)
class S3StorageSettings:
    bucket: str
    region: str = "us-east-1"
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    addressing_style: AddressingStyle = "auto"
    signed_url_ttl_seconds: int = 900
    verify_tls: bool = True

    @classmethod
    def from_environment(cls, *, prefix: str = "FRAMEFACTORY_S3_") -> S3StorageSettings:
        bucket = os.getenv(f"{prefix}BUCKET", "")
        raw_ttl = os.getenv(f"{prefix}SIGNED_URL_TTL_SECONDS", "900")
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise S3StorageConfigurationError(
                f"{prefix}SIGNED_URL_TTL_SECONDS must be an integer"
            ) from exc

        settings = cls(
            bucket=bucket,
            region=os.getenv(f"{prefix}REGION", "us-east-1"),
            endpoint_url=os.getenv(f"{prefix}ENDPOINT_URL") or None,
            access_key_id=environment_value(f"{prefix}ACCESS_KEY_ID"),
            secret_access_key=environment_value(f"{prefix}SECRET_ACCESS_KEY"),
            session_token=environment_value(f"{prefix}SESSION_TOKEN"),
            addressing_style=cast(
                AddressingStyle, os.getenv(f"{prefix}ADDRESSING_STYLE", "auto").lower()
            ),
            signed_url_ttl_seconds=ttl,
            verify_tls=_parse_bool(os.getenv(f"{prefix}VERIFY_TLS", "true"), prefix=prefix),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not _valid_bucket_name(self.bucket):
            raise S3StorageConfigurationError(
                "S3 bucket must be a DNS-compatible name containing 3-63 lowercase characters"
            )
        if not self.region or any(character.isspace() for character in self.region):
            raise S3StorageConfigurationError("S3 region must be a non-empty value without spaces")
        if self.endpoint_url is not None:
            endpoint = urlparse(self.endpoint_url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
                raise S3StorageConfigurationError(
                    "S3 endpoint URL must be an absolute http:// or https:// URL"
                )
            if endpoint.query or endpoint.fragment:
                raise S3StorageConfigurationError(
                    "S3 endpoint URL cannot contain a query string or fragment"
                )
            if endpoint.username is not None or endpoint.password is not None:
                raise S3StorageConfigurationError(
                    "S3 endpoint URL cannot contain embedded credentials"
                )
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise S3StorageConfigurationError(
                "S3 access key ID and secret access key must either both be set or both be absent"
            )
        if self.session_token and not self.access_key_id:
            raise S3StorageConfigurationError("S3 session token requires explicit credentials")
        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise S3StorageConfigurationError(
                "S3 addressing style must be one of: auto, path, virtual"
            )
        if not 60 <= self.signed_url_ttl_seconds <= 604_800:
            raise S3StorageConfigurationError(
                "S3 signed URL TTL must be between 60 and 604800 seconds"
            )


class S3ObjectStorage:
    """Async application adapter for AWS S3, Cloudflare R2, and MinIO.

    The object descriptor should be persisted with the owning application record. Upload metadata
    is part of the presigned PUT request, and ``complete_upload`` additionally hashes the stored
    bytes before the object is accepted as durable.
    """

    def __init__(self, settings: S3StorageSettings, *, client: S3Client | None = None) -> None:
        settings.validate()
        self.settings = settings
        self._client = client or _build_s3_client(settings)

    async def initiate_upload(
        self,
        workspace_id: UUID,
        *,
        sha256: str,
        content_type: str,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        digest = _validate_sha256(sha256)
        mime = _validate_content_type(content_type)
        ttl = self._validate_ttl(expires_in)
        key = f"workspaces/{workspace_id}/objects/{uuid4().hex}"
        metadata = {"sha256": digest, "workspace-id": str(workspace_id)}
        params = {
            "Bucket": self.settings.bucket,
            "Key": key,
            "ContentType": mime,
            "Metadata": metadata,
        }
        try:
            url = self._client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=ttl, HttpMethod="PUT"
            )
        except Exception as exc:
            raise self._unavailable("could not create a signed upload URL", exc) from exc

        locator = ObjectLocator(key=key, sha256=digest, content_type=mime)
        return PresignedRequest(
            method="PUT",
            url=url,
            headers={
                "Content-Type": mime,
                "x-amz-meta-sha256": digest,
                "x-amz-meta-workspace-id": str(workspace_id),
            },
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            object=locator,
        )

    async def complete_upload(
        self, workspace_id: UUID, object: ObjectLocator
    ) -> StoredObject:
        object = self._validate_locator(workspace_id, object)
        head = await self._head_and_validate(workspace_id, object)
        etag = head.get("ETag")
        params: dict[str, Any] = {"Bucket": self.settings.bucket, "Key": object.key}
        if etag:
            params["IfMatch"] = etag
        try:
            response = await asyncio.to_thread(self._client.get_object, **params)
            actual_digest = await asyncio.to_thread(_hash_response_body, response)
        except ObjectIntegrityError:
            raise
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFound(f"object {object.key!r} no longer exists") from exc
            raise self._unavailable(
                f"could not verify uploaded object {object.key!r}", exc
            ) from exc
        if actual_digest != object.sha256:
            raise ObjectIntegrityError(
                f"object {object.key!r} SHA-256 mismatch: expected {object.sha256}, "
                f"received {actual_digest}"
            )
        return _stored_object(object, head)

    async def upload_file(
        self, workspace_id: UUID, object: ObjectLocator, path: Path
    ) -> None:
        object = self._validate_locator(workspace_id, object)
        if not path.is_file() or path.is_symlink():
            raise ObjectNotFound("local upload source does not exist")
        extra_args = {
            "ContentType": object.content_type,
            "Metadata": {"sha256": object.sha256, "workspace-id": str(workspace_id)},
        }
        try:
            await asyncio.to_thread(
                self._client.upload_file,
                str(path),
                self.settings.bucket,
                object.key,
                ExtraArgs=extra_args,
            )
        except Exception as exc:
            raise self._unavailable("could not upload a local media file", exc) from exc

    async def presign_download(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        object = self._validate_locator(workspace_id, object)
        await self._head_and_validate(workspace_id, object)
        ttl = self._validate_ttl(expires_in)
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.settings.bucket, "Key": object.key},
                ExpiresIn=ttl,
                HttpMethod="GET",
            )
        except Exception as exc:
            raise self._unavailable("could not create a signed download URL", exc) from exc
        return PresignedRequest(
            method="GET",
            url=url,
            headers={},
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            object=object,
        )

    async def read_bytes(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        maximum_bytes: int,
    ) -> bytes:
        if isinstance(maximum_bytes, bool) or maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be a positive integer")
        object = self._validate_locator(workspace_id, object)
        head = await self._head_and_validate(workspace_id, object)
        if int(head.get("ContentLength", 0)) > maximum_bytes:
            raise ObjectIntegrityError("object exceeds the inline preview size limit")
        params: dict[str, Any] = {"Bucket": self.settings.bucket, "Key": object.key}
        if head.get("ETag"):
            params["IfMatch"] = head["ETag"]
        try:
            response = await asyncio.to_thread(self._client.get_object, **params)
            data = await asyncio.to_thread(
                _read_response_body,
                response,
                maximum_bytes,
            )
        except ObjectIntegrityError:
            raise
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFound(f"object {object.key!r} no longer exists") from exc
            raise self._unavailable(f"could not read object {object.key!r}", exc) from exc
        if hashlib.sha256(data).hexdigest() != object.sha256:
            raise ObjectIntegrityError(
                f"object {object.key!r} SHA-256 mismatch while reading inline content"
            )
        return data

    async def healthcheck(self) -> ObjectStorageHealth:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.settings.bucket)
        except Exception as exc:
            code = _error_code(exc) or type(exc).__name__
            raise ObjectStorageUnavailable(
                f"S3 health check failed for bucket {self.settings.bucket!r} "
                f"at {self.settings.endpoint_url or 'the AWS default endpoint'} ({code})"
            ) from exc
        return ObjectStorageHealth(
            status="ok", bucket=self.settings.bucket, endpoint=self.settings.endpoint_url
        )

    async def ensure_bucket(self, *, create_if_missing: bool = False) -> None:
        """Check the bucket, optionally creating it for local MinIO/test environments."""

        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.settings.bucket)
            return
        except Exception as exc:
            if not create_if_missing or _error_code(exc) not in _NOT_FOUND_CODES:
                raise self._unavailable(
                    f"bucket {self.settings.bucket!r} is unavailable", exc
                ) from exc

        kwargs: dict[str, Any] = {"Bucket": self.settings.bucket}
        if self.settings.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": self.settings.region
            }
        try:
            await asyncio.to_thread(self._client.create_bucket, **kwargs)
        except Exception as exc:
            raise self._unavailable(
                f"could not create bucket {self.settings.bucket!r}", exc
            ) from exc

    async def _head_and_validate(
        self, workspace_id: UUID, object: ObjectLocator
    ) -> dict[str, Any]:
        try:
            head = await asyncio.to_thread(
                self._client.head_object, Bucket=self.settings.bucket, Key=object.key
            )
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise ObjectNotFound(f"object {object.key!r} does not exist") from exc
            raise self._unavailable(f"could not inspect object {object.key!r}", exc) from exc

        metadata = {str(key).lower(): str(value) for key, value in head.get("Metadata", {}).items()}
        actual_digest = metadata.get("sha256")
        actual_workspace = metadata.get("workspace-id")
        actual_content_type = head.get("ContentType")
        if actual_digest != object.sha256:
            raise ObjectIntegrityError(
                f"object {object.key!r} SHA-256 metadata does not match its descriptor"
            )
        legacy_source = _ASSET_SOURCE_KEY_RE.fullmatch(object.key)
        workspace_metadata_is_compatible = actual_workspace == str(workspace_id) or (
            legacy_source is not None and actual_workspace is None
        )
        if not workspace_metadata_is_compatible:
            raise ObjectIntegrityError(
                f"object {object.key!r} workspace metadata does not match its owner"
            )
        if actual_content_type != object.content_type:
            raise ObjectIntegrityError(
                f"object {object.key!r} MIME metadata does not match its descriptor"
            )
        return head

    def _validate_locator(
        self, workspace_id: UUID, object: ObjectLocator
    ) -> ObjectLocator:
        digest = _validate_sha256(object.sha256)
        content_type = _validate_content_type(object.content_type)
        match = (
            _OBJECT_KEY_RE.fullmatch(object.key)
            or _ARTIFACT_KEY_RE.fullmatch(object.key)
            or _ASSET_DERIVED_KEY_RE.fullmatch(object.key)
            or _ASSET_SOURCE_KEY_RE.fullmatch(object.key)
        )
        if match is None or match.group("workspace") != str(workspace_id):
            raise ObjectNotFound("object does not belong to the requested workspace")
        source_match = _ASSET_SOURCE_KEY_RE.fullmatch(object.key)
        if source_match is not None and source_match.group("source_hash") != digest:
            raise ObjectNotFound("asset source key does not match its content descriptor")
        return ObjectLocator(
            key=object.key,
            sha256=digest,
            content_type=content_type,
        )

    def _validate_ttl(self, expires_in: int | None) -> int:
        ttl = self.settings.signed_url_ttl_seconds if expires_in is None else expires_in
        if isinstance(ttl, bool) or not 60 <= ttl <= 604_800:
            raise ValueError("signed URL expiry must be between 60 and 604800 seconds")
        return ttl

    def _unavailable(self, action: str, exc: Exception) -> ObjectStorageUnavailable:
        code = _error_code(exc) or type(exc).__name__
        endpoint = self.settings.endpoint_url or "the AWS default endpoint"
        return ObjectStorageUnavailable(f"S3 {action} at {endpoint} ({code})")


def _build_s3_client(settings: S3StorageSettings) -> S3Client:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise S3StorageConfigurationError(
            "boto3 is required for the S3 object storage adapter; install the API runtime extras"
        ) from exc

    kwargs: dict[str, Any] = {
        "region_name": settings.region,
        "endpoint_url": settings.endpoint_url,
        "verify": settings.verify_tls,
        "config": Config(
            signature_version="s3v4", s3={"addressing_style": settings.addressing_style}
        ),
    }
    if settings.access_key_id:
        kwargs.update(
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            aws_session_token=settings.session_token,
        )
    return cast(S3Client, boto3.client("s3", **kwargs))


def _parse_bool(value: str, *, prefix: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise S3StorageConfigurationError(f"{prefix}VERIFY_TLS must be true or false")


def _valid_bucket_name(value: str) -> bool:
    if not _BUCKET_RE.fullmatch(value) or ".." in value or ".-" in value or "-." in value:
        return False
    parts = value.split(".")
    return not (len(parts) == 4 and all(part.isdigit() and int(part) <= 255 for part in parts))


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
    return normalized


def _validate_content_type(value: str) -> str:
    normalized = value.strip().lower()
    if not _MIME_RE.fullmatch(normalized):
        raise ValueError("content_type must be a concrete MIME type such as video/mp4")
    return normalized


def _hash_response_body(response: dict[str, Any]) -> str:
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise ObjectIntegrityError("S3 get_object response did not contain a readable body")
    digest = hashlib.sha256()
    try:
        while chunk := body.read(1024 * 1024):
            if not isinstance(chunk, bytes):
                raise ObjectIntegrityError("S3 object body returned non-byte content")
            digest.update(chunk)
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()
    return digest.hexdigest()


def _read_response_body(response: dict[str, Any], maximum_bytes: int) -> bytes:
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise ObjectIntegrityError("S3 get_object response did not contain a readable body")
    try:
        data = body.read(maximum_bytes + 1)
        if not isinstance(data, bytes):
            raise ObjectIntegrityError("S3 object body returned non-byte content")
        if len(data) > maximum_bytes:
            raise ObjectIntegrityError("object exceeds the inline preview size limit")
        return data
    finally:
        close = getattr(body, "close", None)
        if close is not None:
            close()


def _clean_etag(value: Any) -> str | None:
    return str(value).strip('"') if value else None


def _stored_object(object: ObjectLocator, head: dict[str, Any]) -> StoredObject:
    return StoredObject(
        key=object.key,
        sha256=object.sha256,
        content_type=object.content_type,
        size=int(head.get("ContentLength", 0)),
        etag=_clean_etag(head.get("ETag")),
        last_modified=head.get("LastModified"),
    )


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if isinstance(error, dict) and error.get("Code") is not None:
        return str(error["Code"])
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict) and metadata.get("HTTPStatusCode") is not None:
        return str(metadata["HTTPStatusCode"])
    return None
