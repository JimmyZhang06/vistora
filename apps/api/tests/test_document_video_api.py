from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from framefactory_api.context import WorkspaceContext
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings
from framefactory_api.storage import ObjectLocator, PresignedRequest, StoredObject

CAPABILITIES = (
    "document.inspect",
    "document.extract",
    "writing.compose.document",
    "document.storyboard.plan",
    "document.materialize",
    "media.augment",
    "audio.synthesize",
    "document.timeline.align",
    "render.composite",
    "quality.evaluate.document",
)


class Storage:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(bucket="framefactory")
        self.pending: dict[str, ObjectLocator] = {}

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
            f"workspaces/{workspace_id}/documents/source.pdf", sha256, content_type
        )
        self.pending[locator.key] = locator
        return PresignedRequest(
            "PUT",
            "https://objects.test/upload",
            {"Content-Type": content_type},
            datetime.now(UTC) + timedelta(minutes=5),
            locator,
        )

    async def complete_upload(self, workspace_id: UUID, object: ObjectLocator) -> StoredObject:
        assert object.key.startswith(f"workspaces/{workspace_id}/")
        assert self.pending[object.key] == object
        return StoredObject(object.key, object.sha256, object.content_type, 128)


class Queue:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        self.jobs.append(command)
        return SimpleNamespace(id="document-job"), True


class ContextProvider:
    def __init__(self, context: WorkspaceContext) -> None:
        self.context = context

    def resolve(self, requested_workspace_id: UUID | None = None) -> WorkspaceContext:
        del requested_workspace_id
        return self.context


def _repository() -> InMemoryControlRepository:
    root = Path(__file__).resolve().parents[3]
    seeds = root / "packages/seeds/official-skills/v1"
    skill = json.loads((seeds / "document-video-director/1.2.0.json").read_text(encoding="utf-8"))
    pipeline = json.loads(
        (seeds / "pipelines/document-hybrid-production/2.json").read_text(encoding="utf-8")
    )
    return InMemoryControlRepository(skill_versions=[skill], pipelines=[pipeline])


def _uploaded_source(client: TestClient, *, key: str, digest: str = "d" * 64) -> dict[str, Any]:
    created = client.post(
        "/v1/document-sources",
        headers={"Idempotency-Key": key},
        json={
            "filename": "private-evidence.pdf",
            "content_type": "application/pdf",
            "byte_size": 128,
            "sha256": digest,
            "rights_confirmed": True,
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    completed = client.post(
        f"/v1/document-sources/{source['id']}/complete",
        json={
            "object_key": source["object_key"],
            "sha256": digest,
            "content_type": "application/pdf",
        },
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def test_pdf_upload_creates_a_durable_document_run_without_exposing_storage() -> None:
    storage = Storage()
    queue = Queue()
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=_repository(),
        object_storage=storage,
        job_queue=queue,
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/document-sources",
            headers={"Idempotency-Key": "document-source-0001"},
            json={
                "filename": "evidence.pdf",
                "content_type": "application/pdf",
                "byte_size": 128,
                "sha256": "a" * 64,
                "rights_confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        source = created.json()
        assert source["upload"]["method"] == "PUT"
        assert source["object_key"].startswith("workspaces/")

        completed = client.post(
            f"/v1/document-sources/{source['id']}/complete",
            json={
                "object_key": source["object_key"],
                "sha256": "a" * 64,
                "content_type": "application/pdf",
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "uploaded"
        assert "object_key" not in completed.json()

        run_response = client.post(
            "/v1/document-video/runs",
            headers={"Idempotency-Key": "document-run-0001"},
            json={
                "source_id": source["id"],
                "topic": "只解释有页码证据的结论",
                "duration_seconds": 60,
                "aspect_ratio": "16:9",
                "generated_background_enabled": True,
            },
        )
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()
        assert run["status"] == "queued"
        assert run["composition_snapshot"]["skill_version"]["id"] == (
            "346045e2-779f-580f-9feb-839f70943827"
        )
        assert run["composition_snapshot"]["pipeline_version"]["id"] == (
            "0979fced-2c88-514a-a13a-1f7863798e62"
        )
        assert run["composition_snapshot"]["document_source"]["content_hash"] == "a" * 64
        assert queue.jobs[-1]["queue_name"] == "runs"

        fetched = client.get(f"/v1/document-sources/{source['id']}")
        assert fetched.status_code == 200
        assert "bucket" not in fetched.json()
        assert "object_key" not in fetched.json()


def test_document_source_idempotency_rejects_a_changed_descriptor() -> None:
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=_repository(),
        object_storage=Storage(),
        job_queue=Queue(),
    )
    body = {
        "filename": "evidence.pdf",
        "content_type": "application/pdf",
        "byte_size": 128,
        "sha256": "b" * 64,
        "rights_confirmed": True,
    }
    with TestClient(app) as client:
        first = client.post(
            "/v1/document-sources",
            headers={"Idempotency-Key": "same-document-source"},
            json=body,
        )
        assert first.status_code == 201
        changed = client.post(
            "/v1/document-sources",
            headers={"Idempotency-Key": "same-document-source"},
            json={**body, "filename": "different.pdf"},
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_document_source_is_not_visible_to_another_workspace() -> None:
    settings = Settings(worker_capabilities=CAPABILITIES)
    repo = _repository()
    storage = Storage()
    owner_app = create_app(
        settings=settings,
        repository=repo,
        object_storage=storage,
        job_queue=Queue(),
    )
    with TestClient(owner_app) as client:
        created = client.post(
            "/v1/document-sources",
            headers={"Idempotency-Key": "tenant-source-0001"},
            json={
                "filename": "tenant.pdf",
                "content_type": "application/pdf",
                "byte_size": 128,
                "sha256": "c" * 64,
                "rights_confirmed": True,
            },
        )
        assert created.status_code == 201
        source_id = created.json()["id"]

    other_context = WorkspaceContext(
        user_id=settings.default_user_id,
        workspace_id=UUID("33333333-3333-4333-8333-333333333333"),
        workspace_name="other",
    )
    other_app = create_app(
        settings=settings,
        repository=repo,
        object_storage=storage,
        job_queue=Queue(),
        context_provider=ContextProvider(other_context),
    )
    with TestClient(other_app) as client:
        hidden = client.get(f"/v1/document-sources/{source_id}")
        assert hidden.status_code == 404


def test_document_retention_and_legal_hold_fail_closed() -> None:
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=_repository(),
        object_storage=Storage(),
        job_queue=Queue(),
    )
    with TestClient(app) as client:
        source = _uploaded_source(client, key="retained-document")
        retained = client.patch(
            f"/v1/document-sources/{source['id']}/retention",
            headers={"If-Match": f'"{source["revision"]}"'},
            json={
                "retention_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "reason": "contractual retention",
            },
        )
        assert retained.status_code == 200, retained.text
        blocked = client.post(
            f"/v1/document-sources/{source['id']}/purge-requests",
            headers={
                "If-Match": f'"{retained.json()["revision"]}"',
                "Idempotency-Key": "purge-retained-document",
            },
            json={
                "reason": "user requested deletion",
                "delete_derived": True,
                "confirmation": "DELETE",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "DOCUMENT_RETENTION_ACTIVE"

        cleared = client.patch(
            f"/v1/document-sources/{source['id']}/retention",
            headers={"If-Match": f'"{retained.json()["revision"]}"'},
            json={"retention_until": None, "reason": "retention obligation ended"},
        )
        hold = client.post(
            f"/v1/document-sources/{source['id']}/legal-hold",
            headers={"If-Match": f'"{cleared.json()["revision"]}"'},
            json={"active": True, "reason": "litigation hold 2026-09"},
        )
        assert hold.status_code == 200, hold.text
        held = client.post(
            f"/v1/document-sources/{source['id']}/purge-requests",
            headers={
                "If-Match": f'"{hold.json()["revision"]}"',
                "Idempotency-Key": "purge-held-document",
            },
            json={
                "reason": "user requested deletion",
                "delete_derived": True,
                "confirmation": "DELETE",
            },
        )
        assert held.status_code == 409
        assert held.json()["code"] == "DOCUMENT_LEGAL_HOLD_ACTIVE"


def test_document_purge_request_is_revision_fenced_and_idempotent() -> None:
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=_repository(),
        object_storage=Storage(),
        job_queue=Queue(),
    )
    body = {
        "reason": "privacy deletion request",
        "delete_derived": True,
        "confirmation": "DELETE",
    }
    with TestClient(app) as client:
        source = _uploaded_source(client, key="purgeable-document", digest="e" * 64)
        stale = client.post(
            f"/v1/document-sources/{source['id']}/purge-requests",
            headers={"If-Match": '"1"', "Idempotency-Key": "purge-stale"},
            json=body,
        )
        assert stale.status_code == 412

        first = client.post(
            f"/v1/document-sources/{source['id']}/purge-requests",
            headers={
                "If-Match": f'"{source["revision"]}"',
                "Idempotency-Key": "purge-once",
            },
            json=body,
        )
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "queued"
        assert datetime.fromisoformat(
            first.json()["next_attempt_at"].replace("Z", "+00:00")
        ) >= datetime.fromisoformat(source["upload_expires_at"].replace("Z", "+00:00"))
        replay = client.post(
            f"/v1/document-sources/{source['id']}/purge-requests",
            headers={
                "If-Match": f'"{source["revision"]}"',
                "Idempotency-Key": "purge-once",
            },
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == first.json()["id"]
        request = client.get(first.headers["Location"])
        assert request.status_code == 200
        assert request.json()["attempt_count"] == 0
        frozen = client.get(f"/v1/document-sources/{source['id']}").json()
        assert frozen["status"] == "deletion_pending"
