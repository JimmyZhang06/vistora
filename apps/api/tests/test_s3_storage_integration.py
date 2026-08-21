from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import httpx
import pytest

from framefactory_api.s3_storage import S3ObjectStorage, S3StorageSettings


def _minio_settings() -> S3StorageSettings:
    endpoint = os.getenv("FRAMEFACTORY_TEST_S3_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("set FRAMEFACTORY_TEST_S3_ENDPOINT_URL to run the MinIO integration test")
    settings = S3StorageSettings(
        bucket=os.getenv("FRAMEFACTORY_TEST_S3_BUCKET", "framefactory-integration"),
        region=os.getenv("FRAMEFACTORY_TEST_S3_REGION", "us-east-1"),
        endpoint_url=endpoint,
        access_key_id=os.getenv("FRAMEFACTORY_TEST_S3_ACCESS_KEY_ID", "minioadmin"),
        secret_access_key=os.getenv(
            "FRAMEFACTORY_TEST_S3_SECRET_ACCESS_KEY", "minioadmin"
        ),
        addressing_style="path",
        verify_tls=os.getenv("FRAMEFACTORY_TEST_S3_VERIFY_TLS", "true").lower() == "true",
    )
    settings.validate()
    return settings


@pytest.mark.asyncio
async def test_minio_signed_upload_verify_and_download() -> None:
    storage = S3ObjectStorage(_minio_settings())
    await storage.ensure_bucket(create_if_missing=True)
    workspace_id = uuid4()
    content = b"FrameFactory MinIO integration payload"
    digest = hashlib.sha256(content).hexdigest()

    upload = await storage.initiate_upload(
        workspace_id, sha256=digest, content_type="application/octet-stream"
    )
    response = httpx.put(upload.url, content=content, headers=upload.headers, timeout=15)
    response.raise_for_status()

    completed = await storage.complete_upload(workspace_id, upload.object)
    download = await storage.presign_download(workspace_id, upload.object)
    response = httpx.get(download.url, timeout=15)
    response.raise_for_status()

    assert completed.sha256 == digest
    assert completed.size == len(content)
    assert response.content == content
