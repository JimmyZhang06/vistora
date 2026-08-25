from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from framefactory_api.context import WorkspaceContext
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.service import ControlService
from framefactory_api.settings import Settings
from framefactory_api.storage import ObjectLocator, PresignedRequest

PIPELINE_VERSION_ID = "eaf69761-8571-5404-a50a-8ef5c0aa90d3"
CAPABILITIES = (
    "web.site.discover",
    "web.page.capture_batch",
    "web.region.analyze",
    "web.storyboard.plan",
    "writing.compose.webpage_story",
    "web.materialize.regions",
    "web.capture.validate",
    "web.capture.screenshot",
    "writing.compose.webpage",
    "web.materialize",
    "audio.synthesize",
    "render.compose",
    "quality.evaluate",
)
CREATE_BODY = {
    "target_url": "https://example.com/product?campaign=summer#details",
    "capture": {"mode": "viewport", "aspect_ratio": "16:9", "full_page": False},
    "video": {
        "topic": "介绍这个公开产品页面",
        "duration_seconds": 30,
        "subtitles_enabled": True,
        "voice_profile_id": None,
    },
    "rights": {"public_page_confirmed": True, "rights_confirmed": True},
}


class Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        self.enqueued.append(command)
        return SimpleNamespace(id="webpage-job"), True


class Storage:
    def __init__(self) -> None:
        self.requests: list[ObjectLocator] = []

    async def presign_download(
        self,
        workspace_id: UUID,
        object: ObjectLocator,
        *,
        expires_in: int | None = None,
    ) -> PresignedRequest:
        del workspace_id, expires_in
        self.requests.append(object)
        return PresignedRequest(
            method="GET",
            url=f"https://media.example.test/{object.key}?signed=1",
            headers={},
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            object=object,
        )


class ContextProvider:
    def __init__(self, permissions: frozenset[str]) -> None:
        settings = Settings()
        self.context = WorkspaceContext(
            user_id=settings.default_user_id,
            workspace_id=settings.default_workspace_id,
            workspace_name=settings.default_workspace_name,
            permissions=permissions,
        )

    def resolve(self, requested_workspace_id: UUID | None = None) -> WorkspaceContext:
        del requested_workspace_id
        return self.context


def repository() -> InMemoryControlRepository:
    root = Path(__file__).resolve().parents[3]
    seed_root = root / "packages/seeds/official-skills/v1"
    pipeline = json.loads(
        (seed_root / "pipelines/webpage-video-production/1.json").read_text(encoding="utf-8")
    )
    version = json.loads(
        (seed_root / "webpage-video-director/1.0.0.json").read_text(encoding="utf-8")
    )
    site_pipeline = json.loads(
        (seed_root / "pipelines/webpage-video-production/2.json").read_text(encoding="utf-8")
    )
    site_version = json.loads(
        (seed_root / "webpage-video-director/1.1.0.json").read_text(encoding="utf-8")
    )
    return InMemoryControlRepository(
        skill_versions=[version, site_version],
        pipelines=[pipeline, site_pipeline],
    )


def app(
    repo: InMemoryControlRepository,
    queue: Queue | None = None,
    storage: Storage | None = None,
    context_provider: ContextProvider | None = None,
):
    return create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=repo,
        job_queue=queue or Queue(),
        object_storage=storage or Storage(),
        context_provider=context_provider,
    )


def create_run(client: TestClient, key: str = "webpage-create-0001") -> dict[str, Any]:
    response = client.post(
        "/v1/webpage-video/runs",
        headers={"Idempotency-Key": key},
        json=CREATE_BODY,
    )
    assert response.status_code == 201, response.text
    return response.json()


def capture_step(run: dict[str, Any], *, attempt: int = 1) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    artifact_id = str(uuid4())
    return {
        "schema_version": "1.0.0",
        "id": f"{run['project_run_id']}:screenshot",
        "workspace_id": run["workspace_id"],
        "run_id": run["project_run_id"],
        "node_key": "screenshot",
        "operation": "web.capture.screenshot",
        "status": "awaiting_review",
        "queue_name": "run-steps",
        "required_capabilities": ["web.capture.screenshot"],
        "input_snapshot": {},
        "output_summary": {"final_url": "https://example.com/product"},
        "output_artifacts": [
            {
                "id": artifact_id,
                "kind": "image",
                "media_type": "image/png",
                "filename": "capture.png",
                "byte_size": 1234,
                "content_hash": "a" * 64,
            }
        ],
        "dependencies": ["validate"],
        "attempt": attempt,
        "maximum_attempts": 3,
        "error": None,
        "review_required": True,
        "review": {
            "decision": None,
            "actor_id": None,
            "comment": None,
            "issue_codes": [],
            "requested_at": now,
            "decided_at": None,
        },
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": None,
        "next_attempt_at": now,
        "cancellation_requested_at": None,
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "updated_at": now,
        "revision": 3,
    }


def add_capture_evidence(
    repo: InMemoryControlRepository, run: dict[str, Any], *, attempt: int = 1
) -> dict[str, Any]:
    step = capture_step(run, attempt=attempt)
    artifact = {
        "schema_version": "1.0.0",
        "id": step["output_artifacts"][0]["id"],
        "workspace_id": run["workspace_id"],
        "ownership_type": "workspace",
        "run_id": run["project_run_id"],
        "step_id": str(uuid4()),
        "kind": "image",
        "media_type": "image/png",
        "object_key": (
            f"browser-capture/workspaces/{run['workspace_id']}/runs/"
            f"{run['project_run_id']}/artifacts/{step['output_artifacts'][0]['id']}/capture.png"
        ),
        "byte_size": 1234,
        "content_hash": "a" * 64,
        "filename": "capture.png",
        "created_at": step["created_at"],
        "expires_at": None,
    }
    repo._run_steps[step["id"]] = deepcopy(step)
    repo._artifacts[artifact["id"]] = deepcopy(artifact)
    repo._runs[run["project_run_id"]]["status"] = "awaiting_review"
    repo._runs[run["project_run_id"]]["updated_at"] = step["updated_at"]
    asyncio.run(
        repo.append_webpage_capture_attempt(
            {
                "schema_version": "1.0.0",
                "id": str(uuid4()),
                "workspace_id": run["workspace_id"],
                "webpage_video_run_id": run["id"],
                "underlying_run_id": run["project_run_id"],
                "screenshot_step_id": str(uuid4()),
                "attempt_number": attempt,
                "capture_revision": 3,
                "outcome": "captured",
                "requested_url": CREATE_BODY["target_url"],
                "final_url": "https://example.com/product",
                "viewport_width": 1920,
                "viewport_height": 1080,
                "full_page": False,
                "artifact_id": artifact["id"],
                "sha256": "a" * 64,
                "media_type": "image/png",
                "metadata": {},
                "error": None,
                "captured_at": step["finished_at"],
                "created_at": step["created_at"],
            }
        )
    )
    return step


def quality_step(run: dict[str, Any]) -> dict[str, Any]:
    step = capture_step(run)
    step.update(
        {
            "id": f"{run['project_run_id']}:quality",
            "node_key": "quality",
            "operation": "quality.evaluate",
            "dependencies": ["render"],
            "output_artifacts": [],
            "revision": 7,
        }
    )
    return step


def test_options_create_flattened_input_idempotency_and_quota() -> None:
    repo = repository()
    queue = Queue()
    with TestClient(app(repo, queue=queue)) as client:
        options = client.get("/v1/webpage-video/options")
        first = create_run(client)
        replay = create_run(client)
        conflict = client.post(
            "/v1/webpage-video/runs",
            headers={"Idempotency-Key": "webpage-create-0001"},
            json={
                **CREATE_BODY,
                "video": {**CREATE_BODY["video"], "topic": "changed"},
            },
        )
        for index in range(2, 5):
            create_run(client, f"webpage-create-000{index}")
        blocked = client.get("/v1/webpage-video/options")
        over_limit = client.post(
            "/v1/webpage-video/runs",
            headers={"Idempotency-Key": "webpage-create-0005"},
            json=CREATE_BODY,
        )
        replay_at_limit = create_run(client)
        listed = client.get("/v1/runs")

    assert options.json()["status"] == "ready"
    assert options.json()["limits"]["aspect_ratios"] == ["16:9", "9:16", "1:1", "4:3"]
    assert options.json()["subtitles"] == {"supported": True, "default_enabled": True}
    assert first == replay == replay_at_limit
    assert conflict.status_code == 409
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["blockers"][-1]["code"] == "WEBPAGE_VIDEO_ACTIVE_RUN_LIMIT_REACHED"
    assert over_limit.status_code == 409
    listed_runs = listed.json()["data"]
    assert len(listed_runs) == 4
    assert {item["id"] for item in listed_runs} == {
        resource["underlying_run_id"] for resource in repo._webpage_video_runs.values()
    }
    assert all(
        item["input"]["webpage_video_run_id"] in repo._webpage_video_runs
        for item in listed_runs
    )
    scheduler = repo._runs[first["project_run_id"]]
    assert scheduler["input"] == {
        "webpage_video_run_id": first["id"],
        "target_url": "https://example.com/product?campaign=summer",
        "requested_url": CREATE_BODY["target_url"],
        "topic": "介绍这个公开产品页面",
        "aspect_ratio": "16:9",
        "duration_seconds": 30,
        "subtitles_enabled": True,
        "public_page_confirmed": True,
        "rights_confirmed": True,
    }
    assert scheduler["composition_snapshot"]["pipeline_version"]["id"] == PIPELINE_VERSION_ID
    assert scheduler["composition_snapshot"]["production_settings"]["resolution"] == {
        "width": 1920,
        "height": 1080,
    }
    assert len(queue.enqueued) == 6  # every safe replay repairs a potentially lost dispatch


def test_generic_create_rejects_webpage_operations_even_when_marked_optional() -> None:
    repo = repository()
    pipeline = repo._pipelines[PIPELINE_VERSION_ID]
    pipeline["nodes"][0]["required"] = False
    assert ControlService._pipeline_requires_webpage_video_endpoint(pipeline)
    with TestClient(app(repo)) as client:
        response = client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "generic-webpage-bypass-0001"},
            json={
                "input": {"topic": "must use dedicated endpoint"},
                "composition": {
                    "skill_version_id": "cbfb37f7-3f10-5678-97a4-448c72db2980",
                    "pipeline_version_id": PIPELINE_VERSION_ID,
                    "asset_library_ids": [],
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "WEBPAGE_VIDEO_ENDPOINT_REQUIRED"
    assert response.json()["details"]["required_endpoint"] == "/v1/webpage-video/runs"
    assert repo._runs == {}


def test_url_rights_voice_and_exact_payload_validation() -> None:
    with TestClient(app(repository())) as client:
        for target_url in (
            "http://example.com",
            "https://127.0.0.1/private",
            "https://user:secret@example.com",
            r"https://example.com\@127.0.0.1/",
        ):
            response = client.post(
                "/v1/webpage-video/runs",
                headers={"Idempotency-Key": f"invalid-{uuid4()}"},
                json={**CREATE_BODY, "target_url": target_url},
            )
            assert response.status_code == 422
        missing_voice = deepcopy(CREATE_BODY)
        del missing_voice["video"]["voice_profile_id"]
        unsupported_voice = deepcopy(CREATE_BODY)
        unsupported_voice["video"]["voice_profile_id"] = str(uuid4())
        rights = deepcopy(CREATE_BODY)
        rights["rights"]["rights_confirmed"] = False
        assert (
            client.post(
                "/v1/webpage-video/runs",
                headers={"Idempotency-Key": "missing-voice-0001"},
                json=missing_voice,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/webpage-video/runs",
                headers={"Idempotency-Key": "unsupported-voice-0001"},
                json=unsupported_voice,
            ).json()["code"]
            == "WEBPAGE_VIDEO_VOICE_PROFILE_UNAVAILABLE"
        )
        assert (
            client.post(
                "/v1/webpage-video/runs",
                headers={"Idempotency-Key": "rights-false-0001"},
                json=rights,
            ).status_code
            == 422
        )


def test_capture_signing_cas_generic_bypass_and_semantic_replay() -> None:
    repo = repository()
    storage = Storage()
    with TestClient(app(repo, storage=storage)) as client:
        run = create_run(client)
        step = add_capture_evidence(repo, run)
        polled = client.get(f"/v1/webpage-video/runs/{run['id']}")
        generic_bypass = client.post(
            f"/v1/steps/{step['id']}/review",
            headers={"Idempotency-Key": "generic-capture-review-0001"},
            json={"decision": "approve", "expected_revision": 3},
        )
        capture = client.get(f"/v1/webpage-video/runs/{run['id']}/capture")
        wrong_hash = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-review-wrong-hash"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": "b" * 64,
            },
        )
        wrong_revision = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-review-wrong-revision"},
            json={
                "decision": "approve",
                "expected_revision": 2,
                "expected_sha256": "a" * 64,
            },
        )
        review_body = {
            "decision": "approve",
            "expected_revision": 3,
            "expected_sha256": "a" * 64,
            "comment": "截图准确",
        }
        approved = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-review-approve-0001"},
            json=review_body,
        )
        fresh_key_replay = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-review-approve-0002"},
            json=review_body,
        )
        changed_comment = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-review-approve-0001"},
            json={**review_body, "comment": "different"},
        )

    assert polled.json()["capture"]["artifact"]["preview_url"] is None
    assert storage.requests == [storage.requests[0]]
    assert capture.json()["artifact"]["preview_url"].startswith("https://media.example.test/")
    assert capture.headers["cache-control"] == "private, no-store"
    assert generic_bypass.status_code == 422
    assert generic_bypass.json()["code"] == "WEBPAGE_VIDEO_ENDPOINT_REQUIRED"
    assert wrong_hash.status_code == 409
    assert wrong_revision.status_code == 409
    assert approved.status_code == fresh_key_replay.status_code == 202
    assert approved.json()["capture"]["status"] == "approved"
    assert fresh_key_replay.json()["capture"]["review"]["reviewed_revision"] == 3
    assert changed_comment.status_code == 409


def test_recapture_limit_cancel_review_race_and_quality_escape() -> None:
    repo = repository()
    with TestClient(app(repo)) as client:
        exhausted = create_run(client, "webpage-exhausted-0001")
        exhausted_step = add_capture_evidence(repo, exhausted, attempt=3)
        recapture = client.post(
            f"/v1/webpage-video/runs/{exhausted['id']}/capture/review",
            headers={"Idempotency-Key": "capture-recapture-exhausted"},
            json={
                "decision": "recapture",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
            },
        )
        cancelled = create_run(client, "webpage-cancelled-0001")
        add_capture_evidence(repo, cancelled)
        cancel = client.post(
            f"/v1/webpage-video/runs/{cancelled['id']}/cancel",
            headers={"Idempotency-Key": "webpage-cancel-0001"},
        )
        review_after_cancel = client.post(
            f"/v1/webpage-video/runs/{cancelled['id']}/capture/review",
            headers={"Idempotency-Key": "capture-after-cancel-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
            },
        )
        quality_run = create_run(client, "webpage-quality-0001")
        quality = quality_step(quality_run)
        repo._run_steps[quality["id"]] = quality
        repo._runs[quality_run["project_run_id"]]["status"] = "awaiting_review"
        quality_review = client.post(
            f"/v1/steps/{quality['id']}/review",
            headers={"Idempotency-Key": "quality-review-0001"},
            json={"decision": "approve", "expected_revision": 7},
        )

        recapture_run = create_run(client, "webpage-recapture-0001")
        add_capture_evidence(repo, recapture_run)
        recaptured = client.post(
            f"/v1/webpage-video/runs/{recapture_run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-recapture-0001"},
            json={
                "decision": "recapture",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
                "comment": "页面还没加载完整",
            },
        )

    assert recapture.status_code == 409
    assert recapture.json()["code"] == "WEBPAGE_CAPTURE_RECAPTURE_LIMIT_REACHED"
    assert repo._run_steps[exhausted_step["id"]]["status"] == "awaiting_review"
    assert cancel.status_code == 202
    assert review_after_cancel.status_code == 409
    assert quality_review.status_code == 202
    assert quality_review.json()["status"] == "succeeded"
    assert recaptured.status_code == 202
    assert recaptured.json()["capture"]["status"] == "changes_requested"
    assert recaptured.json()["capture"]["artifact"] is None


def test_missing_attempt_reject_and_sanitized_failure() -> None:
    repo = repository()
    with TestClient(app(repo)) as client:
        missing_attempt_run = create_run(client, "webpage-missing-attempt-0001")
        missing_step = capture_step(missing_attempt_run)
        repo._run_steps[missing_step["id"]] = missing_step
        repo._runs[missing_attempt_run["project_run_id"]]["status"] = "awaiting_review"
        missing_attempt = client.post(
            f"/v1/webpage-video/runs/{missing_attempt_run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-missing-attempt-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
            },
        )

        rejected_run = create_run(client, "webpage-reject-0001")
        add_capture_evidence(repo, rejected_run)
        rejected = client.post(
            f"/v1/webpage-video/runs/{rejected_run['id']}/capture/review",
            headers={"Idempotency-Key": "capture-reject-0001"},
            json={
                "decision": "reject",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
                "comment": "内容不适合使用",
            },
        )

        failed_run = create_run(client, "webpage-failed-0001")
        failed_step = capture_step(failed_run)
        failed_step.update(
            {
                "status": "failed",
                "error": {
                    "code": "capture.browser_failed",
                    "message": "secret https://user:password@example.com/private",
                    "retryable": True,
                    "details": {"provider_body": "token=secret"},
                },
            }
        )
        repo._run_steps[failed_step["id"]] = failed_step
        repo._runs[failed_run["project_run_id"]]["status"] = "failed"
        failed = client.get(f"/v1/webpage-video/runs/{failed_run['id']}")

    assert missing_attempt.status_code == 409
    assert missing_attempt.json()["code"] == "WEBPAGE_CAPTURE_ATTEMPT_NOT_RECORDED"
    assert rejected.status_code == 202
    assert rejected.json()["status"] == "failed"
    assert rejected.json()["capture"]["status"] == "rejected"
    assert not any(
        step.get("node_key") == "write"
        for step in repo._run_steps.values()
        if step["run_id"] == rejected_run["project_run_id"]
    )
    failure = failed.json()["failure"]
    assert failure == {
        "stage": "screenshot",
        "code": "capture.browser_failed",
        "message": "The target page screenshot could not be captured or approved.",
        "retryable": True,
    }
    assert "secret" not in str(failure)


def test_final_video_is_bound_to_current_render_and_only_signed_after_success() -> None:
    repo = repository()
    storage = Storage()
    with TestClient(app(repo, storage=storage)) as client:
        run = create_run(client, "webpage-final-video-0001")
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        video_id = str(uuid4())
        render = capture_step(run)
        render.update(
            {
                "id": f"{run['project_run_id']}:render",
                "node_key": "render",
                "operation": "render.compose",
                "status": "succeeded",
                "review_required": False,
                "review": None,
                "dependencies": ["write", "tts", "materialize"],
                "output_artifacts": [
                    {
                        "id": video_id,
                        "kind": "video",
                        "media_type": "video/mp4",
                        "filename": "final.mp4",
                        "byte_size": 4321,
                        "content_hash": "c" * 64,
                    }
                ],
                "updated_at": now,
            }
        )
        quality = quality_step(run)
        quality.update({"status": "succeeded", "review_required": False, "review": None})
        repo._run_steps[render["id"]] = render
        repo._run_steps[quality["id"]] = quality
        repo._artifacts[video_id] = {
            "schema_version": "1.0.0",
            "id": video_id,
            "workspace_id": run["workspace_id"],
            "ownership_type": "workspace",
            "run_id": run["project_run_id"],
            "step_id": str(uuid4()),
            "kind": "video",
            "media_type": "video/mp4",
            "object_key": (
                f"workspaces/{run['workspace_id']}/runs/{run['project_run_id']}/"
                f"artifacts/{video_id}/final.mp4"
            ),
            "byte_size": 4321,
            "content_hash": "c" * 64,
            "filename": "final.mp4",
            "created_at": now,
            "expires_at": None,
        }
        # An orphan video in the Run artifact list must never replace current render evidence.
        orphan_id = str(uuid4())
        repo._artifacts[orphan_id] = {
            **repo._artifacts[video_id],
            "id": orphan_id,
            "content_hash": "d" * 64,
            "object_key": f"workspaces/{run['workspace_id']}/orphan.mp4",
        }
        repo._runs[run["project_run_id"]]["status"] = "running"
        before_success = client.get(f"/v1/webpage-video/runs/{run['id']}")
        repo._runs[run["project_run_id"]]["status"] = "succeeded"
        succeeded = client.get(f"/v1/webpage-video/runs/{run['id']}")

    assert before_success.json()["final_video"] is None
    assert storage.requests == [storage.requests[0]]
    assert succeeded.json()["status"] == "succeeded"
    assert succeeded.json()["final_video"]["id"] == video_id
    assert succeeded.json()["final_video"]["sha256"] == "c" * 64
    assert succeeded.json()["final_video"]["preview_url"].startswith("https://media.example.test/")


def test_webpage_generic_read_and_quality_review_require_url_capture_scope() -> None:
    repo = repository()
    provider = ContextProvider(frozenset({"url_capture:write"}))
    with TestClient(app(repo, context_provider=provider)) as client:
        run = create_run(client, "webpage-scope-0001")
        quality = quality_step(run)
        repo._run_steps[quality["id"]] = quality
        repo._runs[run["project_run_id"]]["status"] = "awaiting_review"
        listed = client.get("/v1/runs")
        direct = client.get(f"/v1/runs/{run['project_run_id']}")
        steps = client.get("/v1/steps", params={"run_id": run["project_run_id"]})
        review = client.post(
            f"/v1/steps/{quality['id']}/review",
            headers={"Idempotency-Key": "quality-scope-review-0001"},
            json={"decision": "approve", "expected_revision": 7},
        )
        dedicated_review = client.post(
            f"/v1/webpage-video/runs/{run['id']}/capture/review",
            headers={"Idempotency-Key": "dedicated-scope-review-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": "a" * 64,
            },
        )

    assert listed.status_code == 200
    assert listed.json()["data"] == []
    assert (
        direct.status_code
        == steps.status_code
        == review.status_code
        == dedicated_review.status_code
        == 403
    )
    assert direct.json()["code"] == "URL_CAPTURE_PERMISSION_DENIED"


def test_migration_and_openapi_preserve_capture_audit_contract() -> None:
    root = Path(__file__).resolve().parents[3]
    migration = (root / "db/migrations/0022_webpage_video_control_plane.sql").read_text(
        encoding="utf-8"
    )
    convergence = (
        root / "db/migrations/0023_webpage_video_schema_convergence.sql"
    ).read_text(encoding="utf-8")
    openapi = (root / "packages/contracts/openapi/v1.yaml").read_text(encoding="utf-8")

    assert "webpage_capture_attempts_prevent_update" in migration
    assert "webpage_capture_attempts_prevent_delete" in migration
    assert "sha256 IS NOT NULL" in migration
    assert "media_type IS NOT NULL" in migration
    assert "CREATE INDEX IF NOT EXISTS webpage_video_runs_active_idx" in convergence
    assert "COMMENT ON COLUMN webpage_video_runs.status" in convergence
    assert "/v1/webpage-video/runs/{webpage_video_run_id}/capture/review:" in openapi
    assert "decision: { type: string, enum: [approve, recapture, reject] }" in openapi
    assert "url_capture:review" in openapi
