import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.remote_assets import DownloadedAsset, RemoteSearchResult
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.storage import (
    ObjectLocator,
    ObjectStorageHealth,
    PresignedRequest,
    StoredObject,
)


class FakeAssetStorage:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(bucket="framefactory")
        self.pending: dict[str, ObjectLocator] = {}
        self.uploaded_sizes: dict[str, int] = {}
        self.upload_calls = 0

    async def healthcheck(self) -> ObjectStorageHealth:
        return ObjectStorageHealth("ok", "framefactory", "http://objects.test")

    async def initiate_upload(
        self,
        workspace_id: UUID,
        *,
        sha256: str,
        content_type: str,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        del expires_in
        locator = ObjectLocator(
            f"workspaces/{workspace_id}/objects/0123456789abcdef0123456789abcdef",
            sha256,
            content_type,
        )
        self.pending[locator.key] = locator
        return PresignedRequest(
            "PUT",
            "http://objects.test/upload",
            {"Content-Type": content_type},
            datetime.now(UTC) + timedelta(minutes=5),
            locator,
        )

    async def complete_upload(
        self, workspace_id: UUID, object: ObjectLocator
    ) -> StoredObject:
        assert object.key.startswith(f"workspaces/{workspace_id}/")
        assert self.pending[object.key] == object
        return StoredObject(
            object.key,
            object.sha256,
            object.content_type,
            self.uploaded_sizes.get(object.key, 12),
        )

    async def upload_file(
        self, workspace_id: UUID, object: ObjectLocator, path: Path
    ) -> None:
        assert object.key.startswith(f"workspaces/{workspace_id}/")
        assert self.pending[object.key] == object
        self.upload_calls += 1
        self.uploaded_sizes[object.key] = path.stat().st_size


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    async def healthcheck(self) -> None:
        return None

    async def enqueue(self, **kwargs: object) -> tuple[SimpleNamespace, bool]:
        self.jobs.append(dict(kwargs))
        return SimpleNamespace(id=f"queue-{len(self.jobs)}"), True


class FakeRemoteAssetGateway:
    async def search(
        self, platform: str, query: str, *, limit: int = 3
    ) -> tuple[RemoteSearchResult, ...]:
        del limit
        source_url = (
            f"https://www.youtube.com/watch?v={platform}-001"
            if platform == "youtube"
            else f"https://www.bilibili.com/video/BV{platform}001"
        )
        return (
            RemoteSearchResult(
                platform=platform,  # type: ignore[arg-type]
                source_url=source_url,
                external_id=f"{platform}-001",
                title=f"{query} public footage",
                author="Example creator",
                duration_seconds=12.5,
            ),
        )

    async def download(self, source_url: str, destination: Path) -> DownloadedAsset:
        path = destination / "authorized-video.mp4"
        path.write_bytes(b"remote-video")
        return DownloadedAsset(
            path=path,
            platform="youtube",
            source_url=source_url,
            canonical_url=source_url,
            external_id="video-001",
            title="Authorized public video",
            description="A licensed video",
            author="Example creator",
            license_name="licensed",
            filename=path.name,
            media_type="video/mp4",
            byte_size=path.stat().st_size,
            sha256="ca5c5afc85a2890c7eac092c8cf9915f60af275a01b65620648c434555c40672",
            duration_seconds=12.5,
        )


class FakeDiverseRemoteAssetGateway(FakeRemoteAssetGateway):
    async def search(
        self, platform: str, query: str, *, limit: int = 3
    ) -> tuple[RemoteSearchResult, ...]:
        del limit
        identity = hashlib.sha256(query.encode()).hexdigest()[:12]
        source_url = f"https://www.youtube.com/watch?v={identity}"
        return (
            RemoteSearchResult(
                platform="youtube",
                source_url=source_url,
                external_id=identity,
                title=f"{query} public footage",
                author="Example creator",
                duration_seconds=12.5,
            ),
        )

    async def download(self, source_url: str, destination: Path) -> DownloadedAsset:
        path = destination / "authorized-video.mp4"
        content = source_url.encode()
        path.write_bytes(content)
        return DownloadedAsset(
            path=path,
            platform="youtube",
            source_url=source_url,
            canonical_url=source_url,
            external_id=hashlib.sha256(content).hexdigest()[:12],
            title="Authorized public video",
            description="A licensed video",
            author="Example creator",
            license_name="licensed",
            filename=path.name,
            media_type="video/mp4",
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            duration_seconds=12.5,
        )


class FakeFallbackRemoteAssetGateway(FakeDiverseRemoteAssetGateway):
    async def search(
        self, platform: str, query: str, *, limit: int = 3
    ) -> tuple[RemoteSearchResult, ...]:
        del platform, query, limit
        return tuple(
            RemoteSearchResult(
                platform="youtube",
                source_url=f"https://www.youtube.com/watch?v=candidate-{index}",
                external_id=f"candidate-{index}",
                title=f"Candidate {index}",
                author="Example creator",
                duration_seconds=12.5,
            )
            for index in (1, 2)
        )


def test_asset_library_upload_and_completion_are_a_real_control_loop() -> None:
    repository = InMemoryControlRepository()
    storage = FakeAssetStorage()
    queue = FakeQueue()
    with TestClient(
        create_app(repository=repository, object_storage=storage, job_queue=queue)
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "library-001"},
            json={
                "name": "通用视频素材",
                "slug": "general-video",
                "description": "经过版权确认的通用画面",
            },
        )
        assert library.status_code == 201
        library_id = library.json()["id"]

        digest = "a" * 64
        initiated = client.post(
            "/v1/asset-uploads",
            headers={"Idempotency-Key": "asset-upload-001"},
            json={
                "library_id": library_id,
                "filename": "studio.mp4",
                "title": "AI 视频工作室",
                "description": "创作者在电脑前使用 AI 视频工具",
                "kind": "video",
                "content_type": "video/mp4",
                "byte_size": 12,
                "sha256": digest,
                "copyright_status": "owned",
                "tags": ["AI 视频", "自动剪辑", "创作工作室"],
            },
        )
        assert initiated.status_code == 201
        pending = initiated.json()
        assert pending["status"] == "processing"
        assert pending["upload"]["method"] == "PUT"

        completed = client.post(
            f"/v1/asset-uploads/{pending['id']}/complete",
            json={
                "object_key": pending["file"]["object_key"],
                "sha256": digest,
                "content_type": "video/mp4",
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "processing"
        assert completed.json()["analysis_status"] == "pending"
        assert completed.json()["file"]["scan_status"] == "pending"
        assert completed.json()["metadata"]["upload_completed_at"]
        assert len(queue.jobs) == 1
        assert queue.jobs[0]["queue_name"] == "asset-analysis"
        assert queue.jobs[0]["payload"]["asset_id"] == pending["id"]

        analysis_jobs = client.get(
            f"/v1/assets/{pending['id']}/analysis-jobs"
        ).json()["data"]
        assert analysis_jobs[0]["reason"] == "automatic_upload_analysis"
        assert analysis_jobs[0]["status"] == "queued"

        libraries = client.get("/v1/asset-libraries").json()["data"]
        assert libraries[0]["asset_count"] == 1
        assert libraries[0]["ready_asset_count"] == 0


def test_asset_upload_rejects_kind_mime_mismatch() -> None:
    with TestClient(
        create_app(
            repository=InMemoryControlRepository(),
            object_storage=FakeAssetStorage(),
        )
    ) as client:
        response = client.post(
            "/v1/asset-uploads",
            headers={"Idempotency-Key": "asset-upload-002"},
            json={
                "library_id": "10000000-0000-4000-8000-000000000001",
                "filename": "wrong.jpg",
                "title": "wrong",
                "kind": "video",
                "content_type": "image/jpeg",
                "byte_size": 1,
                "sha256": "b" * 64,
                "copyright_status": "owned",
            },
        )
        assert response.status_code == 422


def test_remote_video_is_downloaded_with_provenance_and_added_to_library() -> None:
    repository = InMemoryControlRepository()
    storage = FakeAssetStorage()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=storage,
            remote_asset_gateway=FakeRemoteAssetGateway(),
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "remote-library-001"},
            json={"name": "网络授权素材", "slug": "remote-licensed", "description": ""},
        ).json()
        imported = client.post(
            "/v1/asset-imports",
            headers={"Idempotency-Key": "remote-import-001"},
            json={
                "library_id": library["id"],
                "source_url": "https://www.youtube.com/watch?v=authorized",
                "copyright_status": "licensed",
                "tags": ["产业", "工厂"],
                "rights_confirmed": True,
            },
        )

        assert imported.status_code == 201
        asset = imported.json()
        assert asset["status"] == "processing"
        assert asset["metadata"]["source"]["platform"] == "youtube"
        assert asset["metadata"]["source"]["source_url"].startswith("https://www.youtube.com")
        assert asset["metadata"]["source"]["rights_confirmed"] is True
        assert asset["file"]["byte_size"] == len(b"remote-video")
        libraries = client.get("/v1/asset-libraries").json()["data"]
        assert libraries[0]["ready_asset_count"] == 0


def test_remote_video_requires_explicit_rights_confirmation() -> None:
    with TestClient(
        create_app(
            repository=InMemoryControlRepository(),
            object_storage=FakeAssetStorage(),
            remote_asset_gateway=FakeRemoteAssetGateway(),
        )
    ) as client:
        response = client.post(
            "/v1/asset-imports",
            headers={"Idempotency-Key": "remote-import-002"},
            json={
                "library_id": "10000000-0000-4000-8000-000000000001",
                "source_url": "https://www.bilibili.com/video/BV1example",
                "copyright_status": "licensed",
                "rights_confirmed": False,
            },
        )
        assert response.status_code == 422


def test_remote_import_idempotency_replays_the_scanned_asset_without_reupload() -> None:
    repository = InMemoryControlRepository()
    storage = FakeAssetStorage()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=storage,
            remote_asset_gateway=FakeRemoteAssetGateway(),
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "remote-replay-library"},
            json={"name": "可重放素材", "slug": "remote-replay", "description": ""},
        ).json()
        payload = {
            "library_id": library["id"],
            "source_url": "https://www.youtube.com/watch?v=authorized",
            "copyright_status": "licensed",
            "rights_confirmed": True,
        }
        headers = {"Idempotency-Key": "remote-import-replay"}

        first = client.post("/v1/asset-imports", headers=headers, json=payload)
        replay = client.post("/v1/asset-imports", headers=headers, json=payload)

        assert first.status_code == 201
        assert replay.status_code == 201
        assert replay.json()["id"] == first.json()["id"]
        assert replay.json()["status"] == "processing"
        assert storage.upload_calls == 1


def test_topic_acquisition_searches_and_imports_material_before_editing() -> None:
    repository = InMemoryControlRepository()
    storage = FakeAssetStorage()
    queue = FakeQueue()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=storage,
            remote_asset_gateway=FakeRemoteAssetGateway(),
            job_queue=queue,
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "auto-library-001"},
            json={"name": "自动补充素材", "slug": "auto-assets", "description": ""},
        ).json()

        response = client.post(
            "/v1/asset-acquisitions",
            headers={"Idempotency-Key": "auto-acquisition-001"},
            json={
                "library_id": library["id"],
                "queries": ["宋代城市生活"],
                "sources": ["youtube", "bilibili"],
                "max_assets": 2,
                "copyright_status": "licensed",
                "rights_confirmed": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["imported_count"] == 1
        assert len(response.json()["analysis_jobs"]) == 1
        assert queue.jobs[0]["queue_name"] == "asset-analysis"
        payload = queue.jobs[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["acquisition_count"] == 1
        assert payload["acquisition_id"] == "auto-acquisition-001"
        assert response.json()["unresolved_queries"] == []
        asset = response.json()["imported_assets"][0]
        assert asset["status"] == "processing"
        assert asset["description"] == "宋代城市生活"
        assert "auto-acquired" in asset["metadata"]["tags"]


def test_topic_acquisition_distributes_budget_across_story_queries() -> None:
    repository = InMemoryControlRepository()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=FakeAssetStorage(),
            remote_asset_gateway=FakeDiverseRemoteAssetGateway(),
            job_queue=FakeQueue(),
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "coverage-library-001"},
            json={"name": "赛事素材", "slug": "event-assets", "description": ""},
        ).json()

        response = client.post(
            "/v1/asset-acquisitions",
            headers={"Idempotency-Key": "coverage-acquisition-001"},
            json={
                "library_id": library["id"],
                "queries": ["2011 世界杯决赛", "2012 奥运会决赛"],
                "sources": ["youtube"],
                "max_assets": 2,
                "copyright_status": "licensed",
                "rights_confirmed": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["imported_count"] == 2
        assert {
            item["description"] for item in response.json()["imported_assets"]
        } == {"2011 世界杯决赛", "2012 奥运会决赛"}


def test_topic_acquisition_skips_workspace_duplicate_from_another_library() -> None:
    repository = InMemoryControlRepository()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=FakeAssetStorage(),
            remote_asset_gateway=FakeFallbackRemoteAssetGateway(),
            job_queue=FakeQueue(),
        )
    ) as client:
        first_library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "duplicate-source-library"},
            json={"name": "已有素材", "slug": "duplicate-source", "description": ""},
        ).json()
        target_library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "duplicate-target-library"},
            json={"name": "目标素材", "slug": "duplicate-target", "description": ""},
        ).json()
        assert first_library["id"] != target_library["id"]

        def acquire(library_id: str, key: str):
            return client.post(
                "/v1/asset-acquisitions",
                headers={"Idempotency-Key": key},
                json={
                    "library_id": library_id,
                    "queries": ["同一主题"],
                    "sources": ["youtube"],
                    "max_assets": 1,
                    "copyright_status": "licensed",
                    "rights_confirmed": True,
                },
            )

        first = acquire(first_library["id"], "duplicate-source-acquisition")
        first_asset_id = first.json()["imported_assets"][0]["id"]
        repository._assets[first_asset_id]["status"] = "ready"
        repository._assets[first_asset_id]["analysis_status"] = "completed"
        second = acquire(target_library["id"], "duplicate-target-acquisition")

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["imported_count"] == 1
        imported = second.json()["imported_assets"][0]
        assert imported["library_id"] == target_library["id"]
        assert imported["id"] != first.json()["imported_assets"][0]["id"]
        assert any(
            error["code"] == "REMOTE_CANDIDATE_ALREADY_IN_OTHER_LIBRARY"
            for error in second.json()["provider_errors"]
        )


def test_replayed_acquisition_skips_candidate_rejected_after_first_import() -> None:
    repository = InMemoryControlRepository()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=FakeAssetStorage(),
            remote_asset_gateway=FakeFallbackRemoteAssetGateway(),
            job_queue=FakeQueue(),
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "rejected-replay-library"},
            json={"name": "质量替补", "slug": "rejected-replay", "description": ""},
        ).json()
        payload = {
            "library_id": library["id"],
            "queries": ["极光夜空"],
            "sources": ["youtube"],
            "max_assets": 1,
            "copyright_status": "licensed",
            "rights_confirmed": True,
        }
        headers = {"Idempotency-Key": "rejected-replay-acquisition"}

        first = client.post("/v1/asset-acquisitions", headers=headers, json=payload)
        rejected_id = first.json()["imported_assets"][0]["id"]
        repository._assets[rejected_id]["status"] = "awaiting_review"
        repository._assets[rejected_id]["analysis_status"] = "completed"
        repository._assets[rejected_id]["revision"] += 1

        replay = client.post("/v1/asset-acquisitions", headers=headers, json=payload)

        assert replay.status_code == 201
        assert replay.json()["imported_count"] == 1
        replacement = replay.json()["imported_assets"][0]
        assert replacement["id"] != rejected_id
        assert repository._assets[rejected_id]["status"] == "deleted"


def test_acquisition_does_not_count_ready_candidate_already_in_library() -> None:
    repository = InMemoryControlRepository()
    with TestClient(
        create_app(
            repository=repository,
            object_storage=FakeAssetStorage(),
            remote_asset_gateway=FakeFallbackRemoteAssetGateway(),
            job_queue=FakeQueue(),
        )
    ) as client:
        library = client.post(
            "/v1/asset-libraries",
            headers={"Idempotency-Key": "available-candidate-library"},
            json={"name": "已有候选", "slug": "available-candidate", "description": ""},
        ).json()
        payload = {
            "library_id": library["id"],
            "queries": ["空间天气"],
            "sources": ["youtube"],
            "max_assets": 1,
            "copyright_status": "licensed",
            "rights_confirmed": True,
        }
        first = client.post(
            "/v1/asset-acquisitions",
            headers={"Idempotency-Key": "available-candidate-first"},
            json=payload,
        )
        existing_id = first.json()["imported_assets"][0]["id"]
        repository._assets[existing_id]["status"] = "ready"
        repository._assets[existing_id]["analysis_status"] = "completed"

        second = client.post(
            "/v1/asset-acquisitions",
            headers={"Idempotency-Key": "available-candidate-second"},
            json=payload,
        )

        assert second.status_code == 201
        assert second.json()["imported_count"] == 1
        assert second.json()["imported_assets"][0]["id"] != existing_id
        assert any(
            error["code"] == "REMOTE_CANDIDATE_ALREADY_AVAILABLE"
            for error in second.json()["provider_errors"]
        )
