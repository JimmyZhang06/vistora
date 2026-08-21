from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from framefactory_api.context import WorkspaceContext
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_USER_ID, DEVELOPMENT_WORKSPACE_ID
from framefactory_api.storage import (
    ObjectLocator,
    ObjectStorageHealth,
    PresignedRequest,
)

LIBRARY_ID = UUID("10000000-0000-4000-8000-000000000001")


def _asset(
    *,
    title: str,
    status: str = "quarantined",
    copyright_status: str = "licensed",
    scan_status: str = "clean",
    analysis_status: str = "completed",
    tags: list[str] | None = None,
    workspace_id: UUID = DEVELOPMENT_WORKSPACE_ID,
) -> dict[str, object]:
    asset_id = uuid4()
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    digest = asset_id.hex * 2
    return {
        "schema_version": "1.0.0",
        "id": str(asset_id),
        "workspace_id": str(workspace_id),
        "library_id": str(LIBRARY_ID),
        "kind": "video",
        "title": title,
        "description": f"{title} description",
        "metadata": {"tags": tags or []},
        "tags": tags or [],
        "copyright_status": copyright_status,
        "status": status,
        "analysis_status": analysis_status,
        "revision": 1,
        "deleted_at": now if status == "deleted" else None,
        "created_by": str(DEVELOPMENT_USER_ID),
        "created_at": now,
        "updated_at": now,
        "file": {
            "id": str(uuid4()),
            "storage_provider": "s3",
            "bucket": "framefactory",
            "object_key": f"workspaces/{workspace_id}/objects/{asset_id.hex}",
            "original_filename": f"{asset_id}.mp4",
            "media_type": "video/mp4",
            "byte_size": 12,
            "content_hash": digest,
            "scan_status": scan_status,
            "width": 1920,
            "height": 1080,
            "duration_ms": 1000,
        },
    }


def _library(workspace_id: UUID = DEVELOPMENT_WORKSPACE_ID) -> dict[str, object]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": "1.0.0",
        "id": str(LIBRARY_ID),
        "workspace_id": str(workspace_id),
        "name": "Managed assets",
        "slug": "managed-assets",
        "description": "",
        "visibility": "private",
        "status": "active",
        "created_by": str(DEVELOPMENT_USER_ID),
        "created_at": now,
        "updated_at": now,
    }


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    async def healthcheck(self) -> None:
        return None

    async def enqueue(self, **kwargs: object) -> tuple[SimpleNamespace, bool]:
        self.jobs.append(dict(kwargs))
        return SimpleNamespace(id=f"queue-{len(self.jobs)}"), True


class FakeDownloadStorage:
    def __init__(self) -> None:
        self.locators: list[ObjectLocator] = []

    async def healthcheck(self) -> ObjectStorageHealth:
        return ObjectStorageHealth("ok", "framefactory", "http://objects.test")

    async def presign_download(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        assert object.key.startswith(f"workspaces/{workspace_id}/")
        self.locators.append(object)
        return PresignedRequest(
            "GET",
            "https://objects.test/signed",
            {},
            datetime.now(UTC) + timedelta(seconds=expires_in or 300),
            object,
        )


class ReadOnlyContextProvider:
    def resolve(self, requested_workspace_id: UUID | None = None) -> WorkspaceContext:
        del requested_workspace_id
        return WorkspaceContext(
            DEVELOPMENT_USER_ID,
            DEVELOPMENT_WORKSPACE_ID,
            "Read only",
            frozenset({"assets:read"}),
        )


def test_asset_list_uses_filter_bound_keyset_cursor_and_hides_object_keys() -> None:
    assets = [
        _asset(title="历史 城市", status="ready", tags=["城市", "宋代"]),
        _asset(title="工厂", status="disabled", tags=["工业"]),
        _asset(title="deleted", status="deleted", tags=["城市"]),
    ]
    repository = InMemoryControlRepository(asset_libraries=[_library()], assets=assets)
    with TestClient(create_app(repository=repository)) as client:
        first = client.get(
            f"/v1/asset-libraries/{LIBRARY_ID}/assets",
            params=[("tag", "城市"), ("q", "历史"), ("limit", "1")],
        )
        assert first.status_code == 200
        assert [item["title"] for item in first.json()["data"]] == ["历史 城市"]
        assert "object_key" not in first.json()["data"][0]["file"]

        default_page = client.get(f"/v1/asset-libraries/{LIBRARY_ID}/assets")
        assert {item["status"] for item in default_page.json()["data"]} == {
            "ready",
            "disabled",
        }

        invalid = client.get(
            f"/v1/asset-libraries/{LIBRARY_ID}/assets",
            params={"cursor": "invalid"},
        )
        assert invalid.status_code == 422


def test_detail_metadata_cas_and_workspace_scope() -> None:
    managed = _asset(title="Managed")
    foreign = _asset(
        title="Foreign",
        workspace_id=UUID("99999999-9999-4999-8999-999999999999"),
    )
    repository = InMemoryControlRepository(
        asset_libraries=[_library()], assets=[managed, foreign]
    )
    with TestClient(create_app(repository=repository)) as client:
        detail = client.get(f"/v1/assets/{managed['id']}")
        assert detail.status_code == 200
        assert detail.headers["etag"] == '"1"'
        assert "object_key" not in detail.json()["file"]

        update = client.patch(
            f"/v1/assets/{managed['id']}",
            headers={"If-Match": '"1"'},
            json={"title": "Updated title", "tags": ["人物", "历史"]},
        )
        assert update.status_code == 200
        assert update.json()["revision"] == 2
        assert update.json()["tags"] == ["人物", "历史"]
        stale = client.patch(
            f"/v1/assets/{managed['id']}",
            headers={"If-Match": '"1"'},
            json={"description": "stale"},
        )
        assert stale.status_code == 412
        assert client.get(f"/v1/assets/{foreign['id']}").status_code == 404


def test_ready_gate_checks_rights_scan_and_analysis_and_batch_reports_failures() -> None:
    unknown = _asset(title="Unknown", copyright_status="unknown")
    unscanned = _asset(title="Unscanned", scan_status="pending")
    unanalyzed = _asset(title="Unanalyzed", analysis_status="pending")
    eligible = _asset(title="Eligible")
    assets = [unknown, unscanned, unanalyzed, eligible]
    repository = InMemoryControlRepository(asset_libraries=[_library()], assets=assets)
    with TestClient(create_app(repository=repository)) as client:
        payload = {
            "items": [
                {"asset_id": item["id"], "expected_revision": 1}
                for item in assets
            ],
            "target_status": "ready",
            "reason": "review complete",
        }
        result = client.post(
            "/v1/assets/batch-review",
            headers={"Idempotency-Key": "batch-ready-001"},
            json=payload,
        )
        assert result.status_code == 200
        assert result.json()["succeeded_count"] == 1
        assert result.json()["failed_count"] == 3
        assert {item["code"] for item in result.json()["failures"]} == {
            "ASSET_NOT_READY"
        }
        assert client.get(f"/v1/assets/{eligible['id']}").json()["status"] == "ready"
        replay = client.post(
            "/v1/assets/batch-review",
            headers={"Idempotency-Key": "batch-ready-001"},
            json=payload,
        )
        assert replay.json() == result.json()
        conflict = client.post(
            "/v1/assets/batch-review",
            headers={"Idempotency-Key": "batch-ready-001"},
            json={**payload, "reason": "different request"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_soft_delete_restore_tags_audit_and_bounded_batch_validation() -> None:
    managed = _asset(title="Managed", status="disabled", tags=["old"])
    repository = InMemoryControlRepository(asset_libraries=[_library()], assets=[managed])
    with TestClient(create_app(repository=repository)) as client:
        tagged = client.post(
            "/v1/assets/batch-tags",
            headers={"Idempotency-Key": "batch-tags-001"},
            json={
                "items": [{"asset_id": managed["id"], "expected_revision": 1}],
                "add": ["new"],
                "remove": ["old"],
            },
        )
        assert tagged.json()["succeeded_count"] == 1
        deleted = client.delete(
            f"/v1/assets/{managed['id']}", headers={"If-Match": '"2"'}
        )
        assert deleted.json()["status"] == "deleted"
        restored = client.post(
            f"/v1/assets/{managed['id']}/restore", headers={"If-Match": '"3"'}
        )
        assert restored.json()["status"] == "disabled"
        events = client.get(f"/v1/assets/{managed['id']}/audit-events").json()["data"]
        assert {event["action"] for event in events} >= {
            "asset.metadata_updated",
            "asset.deleted",
            "asset.status_changed",
        }

        too_many = client.post(
            "/v1/assets/batch-tags",
            headers={"Idempotency-Key": "batch-tags-too-many"},
            json={
                "items": [
                    {"asset_id": str(uuid4()), "expected_revision": 1}
                    for _ in range(101)
                ],
                "add": ["x"],
            },
        )
        assert too_many.status_code == 422


def test_reanalysis_is_persisted_and_enqueued_without_http_computation() -> None:
    managed = _asset(title="Managed", status="ready")
    queue = FakeQueue()
    repository = InMemoryControlRepository(asset_libraries=[_library()], assets=[managed])
    with TestClient(create_app(repository=repository, job_queue=queue)) as client:
        requested = client.post(
            f"/v1/assets/{managed['id']}/reanalyze",
            headers={"If-Match": '"1"', "Idempotency-Key": "reanalyze-001"},
            json={"reason": "new model"},
        )
        assert requested.status_code == 202
        assert requested.json()["status"] == "queued"
        assert len(queue.jobs) == 1
        asset = client.get(f"/v1/assets/{managed['id']}").json()
        assert asset["status"] == "processing"
        assert asset["analysis_status"] == "pending"
        jobs = client.get(f"/v1/assets/{managed['id']}/analysis-jobs").json()["data"]
        assert [job["id"] for job in jobs] == [requested.json()["id"]]


def test_download_uses_persisted_workspace_object_and_state_permissions() -> None:
    ready = _asset(title="Ready", status="ready")
    preview_ready = _asset(title="Preview ready", status="ready")
    preview_ready["metadata"]["preview"] = {
        "object_key": (
            f"workspaces/{DEVELOPMENT_WORKSPACE_ID}/asset-analysis/"
            f"{preview_ready['id']}/preview.mp4"
        ),
        "content_hash": "b" * 64,
        "media_type": "video/mp4",
        "byte_size": 6,
    }
    preview_ready["metadata"]["poster"] = {
        "object_key": (
            f"workspaces/{DEVELOPMENT_WORKSPACE_ID}/asset-analysis/"
            f"{preview_ready['id']}/poster.jpg"
        ),
        "content_hash": "c" * 64,
        "media_type": "image/jpeg",
        "byte_size": 3,
    }
    quarantined = _asset(title="Quarantined", status="quarantined")
    pending = _asset(title="Pending scan", status="quarantined", scan_status="pending")
    storage = FakeDownloadStorage()
    repository = InMemoryControlRepository(
        asset_libraries=[_library()], assets=[ready, preview_ready, quarantined, pending]
    )
    with TestClient(create_app(repository=repository, object_storage=storage)) as client:
        signed = client.get(f"/v1/assets/{ready['id']}/downloads/original")
        assert signed.status_code == 200
        assert signed.json()["url"] == "https://objects.test/signed"
        assert storage.locators[0].key == ready["file"]["object_key"]
        fallback = client.get(f"/v1/assets/{ready['id']}/downloads/preview")
        assert fallback.status_code == 200
        assert fallback.json()["variant"] == "original"
        assert fallback.json()["requested_variant"] == "preview"
        assert fallback.json()["fallback"] is True
        assert fallback.json()["media_type"] == "video/mp4"
        preview = client.get(f"/v1/assets/{preview_ready['id']}/downloads/preview")
        assert preview.status_code == 200
        assert preview.json()["variant"] == "preview"
        assert preview.json()["fallback"] is False
        assert storage.locators[-1].key == preview_ready["metadata"]["preview"]["object_key"]
        poster = client.get(f"/v1/assets/{preview_ready['id']}/downloads/poster")
        assert poster.status_code == 200
        assert poster.json()["variant"] == "poster"
        assert poster.json()["fallback"] is False
        assert poster.json()["media_type"] == "image/jpeg"
        assert storage.locators[-1].key == preview_ready["metadata"]["poster"]["object_key"]
        review_preview = client.get(f"/v1/assets/{quarantined['id']}/downloads/preview")
        assert review_preview.status_code == 200
        assert review_preview.json()["variant"] == "original"
        assert review_preview.json()["fallback"] is True
        denied = client.get(f"/v1/assets/{quarantined['id']}/downloads/original")
        assert denied.status_code == 403
        pending_preview = client.get(f"/v1/assets/{pending['id']}/downloads/preview")
        assert pending_preview.status_code == 404

    with TestClient(
        create_app(
            repository=repository,
            object_storage=storage,
            context_provider=ReadOnlyContextProvider(),
        )
    ) as client:
        denied = client.get(f"/v1/assets/{ready['id']}/downloads/original")
        assert denied.status_code == 403
        assert denied.json()["code"] == "ASSET_PERMISSION_DENIED"
