from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings
from framefactory_api.webpage_video import _run_failure

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


class Queue:
    async def enqueue(self, **command):
        return command, True


class Storage:
    async def presign_download(self, workspace_id, object, *, expires_in=None):
        from framefactory_api.storage import PresignedRequest

        del workspace_id, expires_in
        return PresignedRequest(
            method="GET",
            url=f"https://media.example.test/{object.key}",
            headers={},
            expires_at=datetime.now(UTC),
            object=object,
        )


def _repository() -> InMemoryControlRepository:
    root = Path(__file__).resolve().parents[3]
    seed = root / "packages/seeds/official-skills/v1"
    v1_pipeline = json.loads(
        (seed / "pipelines/webpage-video-production/1.json").read_text(encoding="utf-8")
    )
    v1_skill = json.loads(
        (seed / "webpage-video-director/1.0.0.json").read_text(encoding="utf-8")
    )
    v2_pipeline = json.loads(
        (seed / "pipelines/webpage-video-production/2.json").read_text(encoding="utf-8")
    )
    v2_skill = json.loads(
        (seed / "webpage-video-director/1.1.0.json").read_text(encoding="utf-8")
    )
    return InMemoryControlRepository(
        skill_versions=[v1_skill, v2_skill], pipelines=[v1_pipeline, v2_pipeline]
    )


def _create_body() -> dict:
    return {
        "target_url": "https://example.com/",
        "capture": {"mode": "viewport", "aspect_ratio": "16:9", "full_page": False},
        "video": {
            "topic": "介绍多页面产品能力",
            "duration_seconds": 30,
            "subtitles_enabled": True,
            "voice_profile_id": None,
        },
        "rights": {"public_page_confirmed": True, "rights_confirmed": True},
        "crawl": {
            "max_pages": 8,
            "max_depth": 1,
            "same_origin_only": True,
            "include_sitemap": True,
        },
    }


def _artifact(repo: InMemoryControlRepository, run: dict, *, media_type: str, name: str):
    artifact_id = str(uuid4())
    sha256 = ("d" if media_type == "application/json" else "e") * 64
    public = {
        "id": artifact_id,
        "kind": "manifest" if media_type == "application/json" else "image",
        "media_type": media_type,
        "filename": name,
        "byte_size": 123,
        "content_hash": sha256,
    }
    repo._artifacts[artifact_id] = {
        **public,
        "schema_version": "1.0.0",
        "workspace_id": run["workspace_id"],
        "ownership_type": "workspace",
        "run_id": run["project_run_id"],
        "step_id": str(uuid4()),
        "object_key": (
            f"workspaces/{run['workspace_id']}/runs/{run['project_run_id']}/"
            f"artifacts/{artifact_id}/{name}"
        ),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "expires_at": None,
    }
    return public


def _review_step(run: dict, *, kind: str, artifact: dict, content: dict) -> dict:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    node_key, operation = {
        "scope": ("discover", "web.site.discover"),
        "storyboard": ("storyboard", "web.storyboard.plan"),
    }[kind]
    return {
        "schema_version": "1.0.0",
        "id": f"{run['project_run_id']}:{node_key}",
        "workspace_id": run["workspace_id"],
        "run_id": run["project_run_id"],
        "node_key": node_key,
        "operation": operation,
        "status": "awaiting_review",
        "queue_name": "run-steps",
        "required_capabilities": [operation],
        "input_snapshot": {},
        "output_summary": {
            "manifest_artifact_id": artifact["id"],
            "manifest_hash": artifact["content_hash"],
            "manifest": content,
        },
        "output_artifacts": [artifact],
        "dependencies": [],
        "attempt": 1,
        "maximum_attempts": 3,
        "error": None,
        "review_required": True,
        "review": {"requested_at": now},
        "retry_policy": {},
        "created_at": now,
        "started_at": now,
        "finished_at": now,
        "updated_at": now,
        "revision": 3,
    }


def test_browser_failures_have_actionable_public_messages() -> None:
    failure = _run_failure(
        "failed",
        [
            {
                "node_key": "discover",
                "status": "failed",
                "error": {
                    "code": "browser_page_not_stable",
                    "retryable": True,
                },
            }
        ],
    )

    assert failure is not None
    assert failure.code == "browser_page_not_stable"
    assert failure.retryable is True
    assert "visible content did not become stable" in failure.message


def test_v2_crawl_limits_are_fail_closed() -> None:
    repo = _repository()
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=repo,
        job_queue=Queue(),
        object_storage=Storage(),
    )
    with TestClient(app) as client:
        for crawl in (
            {"max_pages": 13},
            {"max_depth": 3},
            {"same_origin_only": False},
        ):
            body = _create_body()
            body["crawl"].update(crawl)
            response = client.post(
                "/v1/webpage-video/runs",
                headers={"Idempotency-Key": f"invalid-crawl-{uuid4()}"},
                json=body,
            )
            assert response.status_code == 422


def test_v2_site_aggregation_and_two_hash_bound_review_gates() -> None:
    repo = _repository()
    app = create_app(
        settings=Settings(worker_capabilities=CAPABILITIES),
        repository=repo,
        job_queue=Queue(),
        object_storage=Storage(),
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/webpage-video/runs",
            headers={"Idempotency-Key": "v2-create-site-0001"},
            json=_create_body(),
        )
        assert created.status_code == 201, created.text
        run = created.json()
        scheduler_input = repo._runs[run["project_run_id"]]["input"]
        assert scheduler_input["max_pages"] == 8
        assert scheduler_input["same_origin_only"] is True
        assert "crawl" not in scheduler_input

        page_id = "page-01"
        pages = [
            {
                "id": page_id,
                "url": "https://example.com/",
                "canonical_url": "https://example.com/",
                "title": "Example",
                "page_type": "home",
                "depth": 0,
                "score": 0.9,
                "selected": True,
                "selection_reason": "root",
                "regions": [],
            }
        ]
        scope_artifact = _artifact(
            repo, run, media_type="application/json", name="site-manifest.json"
        )
        scope = _review_step(
            run,
            kind="scope",
            artifact=scope_artifact,
            content={
                "root_url": "https://example.com/",
                "discovered_count": 1,
                "selected_count": 1,
                "pages": pages,
            },
        )
        scope["output_summary"]["site_manifest_artifact_id"] = scope[
            "output_summary"
        ].pop("manifest_artifact_id")
        scope["output_summary"]["site_manifest_sha256"] = scope[
            "output_summary"
        ].pop("manifest_hash")
        repo._run_steps[scope["id"]] = scope
        repo._runs[run["project_run_id"]]["status"] = "awaiting_review"

        site = client.get(f"/v1/webpage-video/runs/{run['id']}/site")
        assert site.status_code == 200, site.text
        assert site.json()["scope"]["content"]["selected_count"] == 1
        assert site.json()["storyboard"] is None
        assert site.json()["scope"]["artifact"]["preview_url"].startswith(
            "https://media.example.test/"
        )
        domain_run = client.get(f"/v1/webpage-video/runs/{run['id']}")
        assert domain_run.status_code == 200, domain_run.text
        assert domain_run.json()["status"] == "awaiting_scope_review"
        invalid_selection = client.post(
            f"/v1/webpage-video/runs/{run['id']}/scope/review",
            headers={"Idempotency-Key": "v2-scope-invalid-selection-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": scope_artifact["content_hash"],
                "selected_page_ids": ["page-99"],
            },
        )
        assert invalid_selection.status_code == 409
        stale = client.post(
            f"/v1/webpage-video/runs/{run['id']}/scope/review",
            headers={"Idempotency-Key": "v2-scope-stale-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": "0" * 64,
                "selected_page_ids": [page_id],
            },
        )
        assert stale.status_code == 409
        approved = client.post(
            f"/v1/webpage-video/runs/{run['id']}/scope/review",
            headers={"Idempotency-Key": "v2-scope-approve-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": scope_artifact["content_hash"],
                "selected_page_ids": [page_id],
            },
        )
        assert approved.status_code == 202, approved.text
        assert approved.json()["scope"]["status"] == "approved"
        assert approved.json()["scope"]["review"]["metadata"][
            "selected_page_ids"
        ] == [page_id]
        replay = client.post(
            f"/v1/webpage-video/runs/{run['id']}/scope/review",
            headers={"Idempotency-Key": "v2-scope-approve-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": scope_artifact["content_hash"],
                "selected_page_ids": [page_id],
            },
        )
        assert replay.status_code == 202, replay.text

        image = _artifact(repo, run, media_type="image/png", name="hero.png")
        public_image = {
            key: value for key, value in image.items() if key != "content_hash"
        }
        public_image["sha256"] = image["content_hash"]
        region_id = "page-01:region-01"
        pages[0]["regions"] = [
            {
                "id": region_id,
                "page_id": page_id,
                "kind": "hero",
                "label": "very long untrusted page label " * 20,
                "reason": "DOM-derived key region",
                "score": 0.9,
                "bounding_box": {"x": 0, "y": 0, "width": 640, "height": 360},
                "artifact": public_image,
            }
        ]
        storyboard_artifact = _artifact(
            repo, run, media_type="application/json", name="storyboard-manifest.json"
        )
        shot_id = "shot-01"
        storyboard = _review_step(
            run,
            kind="storyboard",
            artifact=storyboard_artifact,
            content={
                "pages": pages,
                "shots": [
                    {
                        "id": shot_id,
                        "ordinal": 1,
                        "page_id": page_id,
                        "region_id": region_id,
                        "duration_seconds": 5,
                        "motion": "zoom_in",
                        "narration_cue": "首页",
                        "artifact": public_image,
                    }
                ],
            },
        )
        storyboard["output_summary"]["storyboard_manifest_artifact_id"] = storyboard[
            "output_summary"
        ].pop("manifest_artifact_id")
        storyboard["output_summary"]["storyboard_manifest_sha256"] = storyboard[
            "output_summary"
        ].pop("manifest_hash")
        repo._run_steps[storyboard["id"]] = storyboard
        repo._runs[run["project_run_id"]]["status"] = "awaiting_review"
        approved_storyboard = client.post(
            f"/v1/webpage-video/runs/{run['id']}/storyboard/review",
            headers={"Idempotency-Key": "v2-storyboard-approve-0001"},
            json={
                "decision": "approve",
                "expected_revision": 3,
                "expected_sha256": storyboard_artifact["content_hash"],
                "shots": [{"id": shot_id, "enabled": True, "order": 1}],
            },
        )
        assert approved_storyboard.status_code == 202, approved_storyboard.text
        assert approved_storyboard.json()["storyboard"]["status"] == "approved"
        assert len(
            approved_storyboard.json()["storyboard"]["content"]["pages"][0][
                "regions"
            ][0]["label"]
        ) == 240
        assert approved_storyboard.json()["storyboard"]["review"]["metadata"] == {
            "schema_version": "2.0.0",
            "kind": "storyboard_selection",
            "manifest_sha256": storyboard_artifact["content_hash"],
            "shots": [{"id": shot_id, "enabled": True, "order": 1}],
        }
