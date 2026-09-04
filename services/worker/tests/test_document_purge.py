from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from framefactory.worker.document_purge import (
    DocumentPurgeLease,
    DocumentPurgeService,
    PurgeObject,
)


class MissingObject(Exception):
    response: ClassVar[dict[str, Any]] = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


@dataclass
class Repository:
    lease: DocumentPurgeLease | None
    completed: list[str] = field(default_factory=list)
    retried: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def claim(self, **_: Any) -> DocumentPurgeLease | None:
        value, self.lease = self.lease, None
        return value

    def complete(self, lease: DocumentPurgeLease) -> None:
        self.completed.append(lease.request_id)

    def retry(self, lease: DocumentPurgeLease, error: dict[str, str]) -> None:
        self.retried.append((lease.request_id, error))

    def close(self) -> None:
        pass

    def healthcheck(self) -> None:
        pass


class VersionedS3:
    def __init__(self) -> None:
        self.deleted: list[dict[str, Any]] = []

    def get_bucket_versioning(self, **_: Any) -> dict[str, str]:
        return {"Status": "Enabled"}

    def list_object_versions(self, **request: Any) -> dict[str, Any]:
        key = request["Prefix"]
        return {
            "IsTruncated": False,
            "Versions": [{"Key": key, "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": key, "VersionId": "m1"}],
        }

    def delete_objects(self, **request: Any) -> dict[str, Any]:
        self.deleted.append(request)
        return {}

    def head_object(self, **_: Any) -> None:
        raise MissingObject


class FailingS3(VersionedS3):
    def get_bucket_versioning(self, **_: Any) -> dict[str, str]:
        raise RuntimeError("storage temporarily unavailable")


class PartiallyFailingS3(VersionedS3):
    def delete_objects(self, **request: Any) -> dict[str, Any]:
        self.deleted.append(request)
        return {
            "Errors": [
                {"Key": request["Delete"]["Objects"][0]["Key"], "Code": "AccessDenied"}
            ]
        }


def _lease(
    *, source_key: str = "workspaces/ws/documents/source.pdf"
) -> DocumentPurgeLease:
    return DocumentPurgeLease(
        request_id="request-1",
        workspace_id="ws",
        source_id="source-1",
        lease_token="lease-1",
        attempt_count=1,
        source=PurgeObject("bucket", source_key, "source"),
        derived=(
            PurgeObject(
                "bucket",
                "workspaces/ws/runs/run-1/artifacts/artifact-1/video.mp4",
                "artifact",
            ),
        ),
    )


def test_document_purge_removes_every_version_before_committing_tombstones() -> None:
    repository = Repository(_lease())
    s3 = VersionedS3()
    service = DocumentPurgeService(
        repository=repository,
        s3_client=s3,
        bucket="bucket",
        worker_id="worker-1",
        lease_seconds=30,
    )

    assert service.process_once() is True
    assert repository.completed == ["request-1"]
    assert repository.retried == []
    assert len(s3.deleted) == 2
    assert all(
        {item["VersionId"] for item in request["Delete"]["Objects"]} == {"v1", "m1"}
        for request in s3.deleted
    )


def test_document_purge_retries_without_committing_metadata_on_storage_failure() -> (
    None
):
    repository = Repository(_lease())
    service = DocumentPurgeService(
        repository=repository,
        s3_client=FailingS3(),
        bucket="bucket",
        worker_id="worker-1",
        lease_seconds=30,
    )

    assert service.process_once() is True
    assert repository.completed == []
    assert repository.retried[0][0] == "request-1"
    assert repository.retried[0][1]["code"] == "DOCUMENT_PURGE_ATTEMPT_FAILED"


def test_document_purge_retries_when_s3_bulk_delete_reports_item_errors() -> None:
    repository = Repository(_lease())
    service = DocumentPurgeService(
        repository=repository,
        s3_client=PartiallyFailingS3(),
        bucket="bucket",
        worker_id="worker-1",
        lease_seconds=30,
    )

    assert service.process_once() is True
    assert repository.completed == []
    assert repository.retried[0][1]["code"] == "DOCUMENT_PURGE_ATTEMPT_FAILED"
    assert "AccessDenied" in repository.retried[0][1]["message"]


def test_document_purge_rejects_cross_tenant_object_keys() -> None:
    repository = Repository(_lease(source_key="workspaces/other/documents/source.pdf"))
    service = DocumentPurgeService(
        repository=repository,
        s3_client=VersionedS3(),
        bucket="bucket",
        worker_id="worker-1",
        lease_seconds=30,
    )

    assert service.process_once() is True
    assert repository.completed == []
    assert repository.retried[0][1]["code"] == "DOCUMENT_PURGE_ATTEMPT_FAILED"
