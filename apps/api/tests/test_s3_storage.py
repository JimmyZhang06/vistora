from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from framefactory_api.s3_storage import (
    S3ObjectStorage,
    S3StorageConfigurationError,
    S3StorageSettings,
)
from framefactory_api.storage import ObjectIntegrityError, ObjectLocator, ObjectNotFound


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self, presign_origin: str = "https://objects.example.test") -> None:
        self.bucket_exists = True
        self.objects: dict[str, dict[str, Any]] = {}
        self.presign_calls: list[tuple[str, dict[str, Any]]] = []
        self.presign_origin = presign_origin.rstrip("/")

    def generate_presigned_url(
        self, method: str, *, Params: dict[str, Any], ExpiresIn: int, HttpMethod: str
    ) -> str:
        self.presign_calls.append(
            (method, {"params": Params, "expires": ExpiresIn, "http_method": HttpMethod})
        )
        return f"{self.presign_origin}/{Params['Key']}?signed=1"

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        del Bucket
        if not self.bucket_exists:
            raise FakeS3Error("NoSuchBucket")
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.bucket_exists = True
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        del Bucket
        if Key not in self.objects:
            raise FakeS3Error("NoSuchKey")
        stored = self.objects[Key]
        return {
            "ContentLength": len(stored["body"]),
            "ContentType": stored["content_type"],
            "Metadata": stored["metadata"],
            "ETag": '"test-etag"',
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        }

    def get_object(self, *, Bucket: str, Key: str, IfMatch: str) -> dict[str, Any]:
        del Bucket
        assert IfMatch == '"test-etag"'
        return {"Body": BytesIO(self.objects[Key]["body"])}

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, Any],
    ) -> None:
        del bucket
        self.objects[key] = {
            "body": Path(filename).read_bytes(),
            "content_type": ExtraArgs["ContentType"],
            "metadata": ExtraArgs["Metadata"],
        }


@pytest.fixture
def fake_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture
def storage(fake_client: FakeS3Client) -> S3ObjectStorage:
    return S3ObjectStorage(
        S3StorageSettings(
            bucket="framefactory-test",
            endpoint_url="http://127.0.0.1:9000",
            access_key_id="minio",
            secret_access_key="not-a-real-secret",
            addressing_style="path",
        ),
        client=fake_client,
    )


def test_s3_credentials_support_secret_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    access_key = tmp_path / "access-key"
    secret_key = tmp_path / "secret-key"
    access_key.write_text("framefactory\n", encoding="utf-8")
    secret_key.write_text("not-a-real-secret\n", encoding="utf-8")
    monkeypatch.setenv("FRAMEFACTORY_S3_BUCKET", "framefactory-test")
    monkeypatch.setenv("FRAMEFACTORY_S3_ACCESS_KEY_ID_FILE", str(access_key))
    monkeypatch.setenv("FRAMEFACTORY_S3_SECRET_ACCESS_KEY_FILE", str(secret_key))

    settings = S3StorageSettings.from_environment()

    assert settings.access_key_id == "framefactory"
    assert settings.secret_access_key == "not-a-real-secret"


@pytest.mark.asyncio
async def test_upload_uses_random_workspace_key_and_signed_metadata(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    digest = hashlib.sha256(b"hello").hexdigest()

    first = await storage.initiate_upload(
        workspace_id, sha256=digest.upper(), content_type="Video/MP4"
    )
    second = await storage.initiate_upload(
        workspace_id, sha256=digest, content_type="video/mp4"
    )

    assert first.object.key.startswith(f"workspaces/{workspace_id}/objects/")
    assert first.object.key != second.object.key
    assert "hello" not in first.object.key
    assert first.headers == {
        "Content-Type": "video/mp4",
        "x-amz-meta-sha256": digest,
        "x-amz-meta-workspace-id": str(workspace_id),
    }
    params = fake_client.presign_calls[0][1]["params"]
    assert params["ContentType"] == "video/mp4"
    assert params["Metadata"] == {
        "sha256": digest,
        "workspace-id": str(workspace_id),
    }


@pytest.mark.asyncio
async def test_complete_upload_validates_metadata_and_actual_bytes(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    content = b"verified object bytes"
    digest = hashlib.sha256(content).hexdigest()
    upload = await storage.initiate_upload(
        workspace_id, sha256=digest, content_type="application/octet-stream"
    )
    fake_client.objects[upload.object.key] = {
        "body": content,
        "content_type": upload.object.content_type,
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }

    completed = await storage.complete_upload(workspace_id, upload.object)

    assert completed.size == len(content)
    assert completed.sha256 == digest
    assert completed.etag == "test-etag"


@pytest.mark.asyncio
async def test_upload_file_streams_remote_asset_with_integrity_metadata(
    storage: S3ObjectStorage, fake_client: FakeS3Client, tmp_path: Path
) -> None:
    workspace_id = uuid4()
    content = b"downloaded public video"
    source = tmp_path / "source.mp4"
    source.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    upload = await storage.initiate_upload(
        workspace_id, sha256=digest, content_type="video/mp4"
    )

    await storage.upload_file(workspace_id, upload.object, source)
    completed = await storage.complete_upload(workspace_id, upload.object)

    assert completed.size == len(content)
    assert fake_client.objects[upload.object.key]["metadata"] == {
        "sha256": digest,
        "workspace-id": str(workspace_id),
    }


@pytest.mark.asyncio
async def test_complete_upload_rejects_forged_hash_metadata(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    expected = hashlib.sha256(b"expected").hexdigest()
    upload = await storage.initiate_upload(
        workspace_id, sha256=expected, content_type="video/mp4"
    )
    fake_client.objects[upload.object.key] = {
        "body": b"different",
        "content_type": "video/mp4",
        "metadata": {"sha256": expected, "workspace-id": str(workspace_id)},
    }

    with pytest.raises(ObjectIntegrityError, match="SHA-256 mismatch"):
        await storage.complete_upload(workspace_id, upload.object)


@pytest.mark.asyncio
async def test_presign_download_rejects_cross_workspace_key(storage: S3ObjectStorage) -> None:
    owner = uuid4()
    digest = hashlib.sha256(b"object").hexdigest()
    locator = ObjectLocator(
        key=f"workspaces/{owner}/objects/{uuid4().hex}",
        sha256=digest,
        content_type="video/mp4",
    )

    with pytest.raises(ObjectNotFound, match="does not belong"):
        await storage.presign_download(uuid4(), locator)


@pytest.mark.asyncio
async def test_reconstructed_locator_is_canonicalized(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    content = b"canonical descriptor"
    digest = hashlib.sha256(content).hexdigest()
    upload = await storage.initiate_upload(
        workspace_id, sha256=digest, content_type="video/mp4"
    )
    fake_client.objects[upload.object.key] = {
        "body": content,
        "content_type": "video/mp4",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }
    reconstructed = ObjectLocator(
        key=upload.object.key,
        sha256=digest.upper(),
        content_type="Video/MP4",
    )

    download = await storage.presign_download(workspace_id, reconstructed)

    assert download.object.sha256 == digest
    assert download.object.content_type == "video/mp4"


@pytest.mark.asyncio
async def test_presign_download_accepts_worker_artifact_key(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    content = b"rendered-video"
    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"workspaces/{workspace_id}/runs/{run_id}/artifacts/"
        f"{artifact_id}/final-video.mp4"
    )
    fake_client.objects[key] = {
        "body": content,
        "content_type": "video/mp4",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }

    download = await storage.presign_download(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="video/mp4"),
    )

    assert download.url.endswith("final-video.mp4?signed=1")


@pytest.mark.asyncio
async def test_presign_download_accepts_browser_capture_artifact_key(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    content = b"browser-capture-png"
    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"browser-capture/workspaces/{workspace_id}/runs/{run_id}/artifacts/"
        f"{artifact_id}/capture.png"
    )
    fake_client.objects[key] = {
        "body": content,
        "content_type": "image/png",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }

    download = await storage.presign_download(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="image/png"),
    )

    assert download.url.endswith("capture.png?signed=1")


@pytest.mark.asyncio
async def test_public_https_client_signs_browser_download_and_upload(
    fake_client: FakeS3Client,
) -> None:
    workspace_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    content = b"browser-public-delivery"
    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"browser-capture/workspaces/{workspace_id}/runs/{run_id}/artifacts/"
        f"{artifact_id}/capture.png"
    )
    fake_client.objects[key] = {
        "body": content,
        "content_type": "image/png",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }
    public_client = FakeS3Client("https://media.example.test")
    storage = S3ObjectStorage(
        S3StorageSettings(
            bucket="framefactory-test",
            endpoint_url="http://s3:9000",
            public_endpoint_url="https://media.example.test",
            access_key_id="test-access",
            secret_access_key="test-secret",
        ),
        client=fake_client,
        presign_client=public_client,
    )

    download = await storage.presign_download(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="image/png"),
    )
    upload = await storage.initiate_upload(
        workspace_id, sha256="b" * 64, content_type="image/png"
    )

    assert download.url.startswith("https://media.example.test/")
    assert upload.url.startswith("https://media.example.test/")
    assert len(public_client.presign_calls) == 2
    assert fake_client.presign_calls == []


@pytest.mark.parametrize(
    "value",
    [
        "http://media.example.test",
        "https://user:secret@media.example.test",
        "https://media.example.test/storage",
        "https://s3:9000",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.8",
    ],
)
def test_public_presign_endpoint_requires_clean_https_origin(value: str) -> None:
    settings = S3StorageSettings(
        bucket="framefactory-test",
        public_endpoint_url=value,
        access_key_id="test-access",
        secret_access_key="test-secret",
    )

    with pytest.raises(S3StorageConfigurationError, match="public endpoint"):
        settings.validate()


def test_development_loopback_public_endpoint_is_explicit_and_still_validated() -> None:
    settings = S3StorageSettings(
        bucket="framefactory-test",
        public_endpoint_url="http://127.0.0.1:59000",
        access_key_id="test-access",
        secret_access_key="test-secret",
        allow_insecure_loopback_public_endpoint=True,
    )
    settings.validate()

    with pytest.raises(S3StorageConfigurationError, match="either both"):
        S3StorageSettings(
            bucket="framefactory-test",
            public_endpoint_url="http://127.0.0.1:59000",
            access_key_id="test-access",
            allow_insecure_loopback_public_endpoint=True,
        ).validate()


def test_loopback_public_endpoint_flag_is_development_only(monkeypatch) -> None:
    monkeypatch.setenv("FRAMEFACTORY_ENV", "production")
    monkeypatch.setenv("FRAMEFACTORY_S3_BUCKET", "framefactory-test")
    monkeypatch.setenv("FRAMEFACTORY_S3_PUBLIC_ENDPOINT_URL", "http://127.0.0.1:59000")
    monkeypatch.setenv(
        "FRAMEFACTORY_S3_ALLOW_INSECURE_LOOPBACK_PUBLIC_ENDPOINT", "true"
    )
    with pytest.raises(S3StorageConfigurationError, match="development-only"):
        S3StorageSettings.from_environment()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["browser-captures/", "x/browser-capture/"])
async def test_presign_download_rejects_similar_browser_capture_prefixes(
    storage: S3ObjectStorage, prefix: str
) -> None:
    workspace_id = uuid4()
    key = (
        f"{prefix}workspaces/{workspace_id}/runs/{uuid4()}/artifacts/"
        f"{uuid4()}/capture.png"
    )

    with pytest.raises(ObjectNotFound, match="does not belong"):
        await storage.presign_download(
            workspace_id,
            ObjectLocator(key=key, sha256="a" * 64, content_type="image/png"),
        )


@pytest.mark.asyncio
async def test_presign_download_rejects_cross_workspace_browser_capture_key(
    storage: S3ObjectStorage,
) -> None:
    workspace_id = uuid4()
    key = (
        f"browser-capture/workspaces/{uuid4()}/runs/{uuid4()}/artifacts/"
        f"{uuid4()}/capture.png"
    )

    with pytest.raises(ObjectNotFound, match="does not belong"):
        await storage.presign_download(
            workspace_id,
            ObjectLocator(key=key, sha256="a" * 64, content_type="image/png"),
        )


@pytest.mark.asyncio
async def test_presign_download_accepts_integrity_checked_asset_derivative(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    asset_id = uuid4()
    source_digest = "a" * 64
    content = b"representative-jpeg"
    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"workspaces/{workspace_id}/asset-analysis/{asset_id}/{source_digest}/"
        f"frames/000-{digest[:16]}.jpg"
    )
    fake_client.objects[key] = {
        "body": content,
        "content_type": "image/jpeg",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }

    download = await storage.presign_download(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="image/jpeg"),
    )

    assert download.url.endswith(f"frames/000-{digest[:16]}.jpg?signed=1")


@pytest.mark.asyncio
async def test_presign_download_accepts_integrity_checked_imported_asset_source(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    content = b"historical-source-video"
    digest = hashlib.sha256(content).hexdigest()
    key = f"workspaces/{workspace_id}/assets/{digest}/历史素材 01.mp4"
    fake_client.objects[key] = {
        "body": content,
        "content_type": "video/mp4",
        # Historical imports predate workspace-id object metadata. The strict
        # workspace and digest segments in the key form the migration bridge.
        "metadata": {"sha256": digest},
    }

    download = await storage.presign_download(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="video/mp4"),
    )

    assert "历史素材 01.mp4?signed=1" in download.url


@pytest.mark.asyncio
async def test_imported_asset_source_rejects_digest_path_mismatch(
    storage: S3ObjectStorage,
) -> None:
    workspace_id = uuid4()
    locator = ObjectLocator(
        key=f"workspaces/{workspace_id}/assets/{'b' * 64}/source.mp4",
        sha256="a" * 64,
        content_type="video/mp4",
    )

    with pytest.raises(ObjectNotFound, match="content descriptor"):
        await storage.presign_download(workspace_id, locator)


@pytest.mark.asyncio
async def test_presign_download_rejects_unrecognized_asset_analysis_path(
    storage: S3ObjectStorage,
) -> None:
    workspace_id = uuid4()
    locator = ObjectLocator(
        key=f"workspaces/{workspace_id}/asset-analysis/arbitrary/private.txt",
        sha256="a" * 64,
        content_type="text/plain",
    )

    with pytest.raises(ObjectNotFound, match="does not belong"):
        await storage.presign_download(workspace_id, locator)


@pytest.mark.asyncio
async def test_read_bytes_returns_integrity_checked_small_artifact(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    workspace_id = uuid4()
    run_id = uuid4()
    artifact_id = uuid4()
    content = b'{"title":"preview"}'
    digest = hashlib.sha256(content).hexdigest()
    key = (
        f"workspaces/{workspace_id}/runs/{run_id}/artifacts/"
        f"{artifact_id}/script.json"
    )
    fake_client.objects[key] = {
        "body": content,
        "content_type": "application/json",
        "metadata": {"sha256": digest, "workspace-id": str(workspace_id)},
    }

    result = await storage.read_bytes(
        workspace_id,
        ObjectLocator(key=key, sha256=digest, content_type="application/json"),
        maximum_bytes=1024,
    )

    assert result == content


@pytest.mark.asyncio
async def test_healthcheck_and_explicit_local_bucket_creation(
    storage: S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    fake_client.bucket_exists = False
    await storage.ensure_bucket(create_if_missing=True)

    health = await storage.healthcheck()

    assert health.status == "ok"
    assert health.bucket == "framefactory-test"
    assert health.endpoint == "http://127.0.0.1:9000"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (S3StorageSettings(bucket=""), "bucket"),
        (S3StorageSettings(bucket="Valid_Bucket"), "bucket"),
        (
            S3StorageSettings(bucket="valid-bucket", endpoint_url="ftp://localhost"),
            "endpoint URL",
        ),
        (
            S3StorageSettings(
                bucket="valid-bucket", endpoint_url="https://user:secret@objects.example.test"
            ),
            "embedded credentials",
        ),
        (
            S3StorageSettings(bucket="valid-bucket", access_key_id="orphan"),
            "must either both be set",
        ),
        (
            S3StorageSettings(bucket="valid-bucket", signed_url_ttl_seconds=10),
            "TTL",
        ),
    ],
)
def test_settings_reject_invalid_values(
    settings: S3StorageSettings, message: str
) -> None:
    with pytest.raises(S3StorageConfigurationError, match=message):
        settings.validate()


def test_settings_load_r2_and_path_style_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "FRAMEFACTORY_S3_BUCKET": "framefactory-assets",
        "FRAMEFACTORY_S3_REGION": "auto",
        "FRAMEFACTORY_S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
        "FRAMEFACTORY_S3_ACCESS_KEY_ID": "test-access",
        "FRAMEFACTORY_S3_SECRET_ACCESS_KEY": "test-secret",
        "FRAMEFACTORY_S3_ADDRESSING_STYLE": "path",
        "FRAMEFACTORY_S3_SIGNED_URL_TTL_SECONDS": "600",
        "FRAMEFACTORY_S3_VERIFY_TLS": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = S3StorageSettings.from_environment()

    assert settings.endpoint_url == values["FRAMEFACTORY_S3_ENDPOINT_URL"]
    assert settings.region == "auto"
    assert settings.addressing_style == "path"
    assert settings.signed_url_ttl_seconds == 600
