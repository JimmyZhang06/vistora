"""API integration of local durable jobs with an explicit fixture subprocess."""

from __future__ import annotations

import time
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from test_benchmark_jobs import NOTE, PROFILE, PROFILE_ID, ChildFixtureService, acquire

from framefactory_api.context import WorkspaceContext
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings

BODY = {"platform": "xiaohongshu", "profile_url": PROFILE, "note_id": NOTE}
HEADERS = {"Idempotency-Key": "benchmark-route-test-1"}


class MutableContextProvider:
    def __init__(self):
        settings = Settings()
        self.context = WorkspaceContext(
            user_id=settings.default_user_id,
            workspace_id=settings.default_workspace_id,
            workspace_name="Route test",
            permissions=frozenset({"assets:read", "assets:write"}),
        )

    def resolve(self, requested_workspace_id=None):
        return self.context


def make_app(service=None, context=None):
    return create_app(
        settings=Settings(),
        repository=InMemoryControlRepository(),
        benchmark_job_service=service,
        context_provider=context,
    )


def wait_for(client, job_id, statuses):
    for _ in range(150):
        response = client.get(f"/v1/benchmark-analysis/jobs/{job_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in statuses:
            return response.json()
        time.sleep(0.02)
    raise AssertionError("Fixture job did not reach expected API state")


def test_disabled_jobs_return_explicit_unavailable_and_no_store():
    with TestClient(make_app()) as client:
        response = client.post("/v1/benchmark-analysis/jobs", headers=HEADERS, json=BODY)
        assert response.status_code == 503
        assert response.json()["code"] == "BENCHMARK_ANALYSIS_UNAVAILABLE"
        assert response.headers["Cache-Control"] == "private, no-store"


def test_job_routes_create_poll_cancel_retry_and_persist(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire, worker_mode="hang")
    context = MutableContextProvider()
    with TestClient(make_app(service, context)) as client:
        empty = client.get("/v1/benchmark-analysis/latest", params={"profile_user_id": PROFILE_ID})
        assert empty.status_code == 404
        assert empty.json()["code"] == "BENCHMARK_ANALYSIS_NOT_FOUND"
        created = client.post("/v1/benchmark-analysis/jobs", headers=HEADERS, json=BODY)
        assert created.status_code == 202, created.text
        assert created.headers["Cache-Control"] == "private, no-store"
        job_id = created.json()["job_id"]
        repeated = client.post("/v1/benchmark-analysis/jobs", headers=HEADERS, json=BODY)
        assert repeated.json()["job_id"] == job_id
        wait_for(client, job_id, {"analyzing"})
        cancelled = client.post(f"/v1/benchmark-analysis/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        service.worker_mode = "complete"
        for _ in range(100):
            retry = client.post(f"/v1/benchmark-analysis/jobs/{job_id}/retry")
            if retry.status_code == 200:
                break
            assert retry.json()["code"] == "BENCHMARK_JOB_STILL_STOPPING"
            time.sleep(0.02)
        assert retry.status_code == 200, retry.text
        ready = wait_for(client, job_id, {"ready", "partial"})
        assert ready["attempt"] == 2
        latest = client.get(
            "/v1/benchmark-analysis/latest",
            params={
                "profile_user_id": PROFILE_ID,
                "note_id": NOTE,
            },
        )
        assert latest.json()["job_id"] == job_id
        filename = ready["artifacts"][0]["filename"]
        artifact = client.get(f"/v1/benchmark-analysis/jobs/{job_id}/artifacts/{filename}")
        assert artifact.status_code == 200
        assert artifact.content == b"fixture"
        assert artifact.headers["Cache-Control"] == "private, no-store"
        assert artifact.headers["X-Content-Type-Options"] == "nosniff"
        for path in ["attempt-2/source.mp4", "..%2Fjobs.sqlite3", "C:%2Fsecret.jpg"]:
            denied = client.get(f"/v1/benchmark-analysis/jobs/{job_id}/artifacts/{path}")
            assert denied.status_code == 404
        original = context.context
        context.context = replace(original, workspace_id=uuid4())
        foreign = client.get(f"/v1/benchmark-analysis/jobs/{job_id}")
        assert foreign.status_code == 404
        foreign_artifact = client.get(f"/v1/benchmark-analysis/jobs/{job_id}/artifacts/{filename}")
        assert foreign_artifact.status_code == 404
        context.context = original
    reopened = ChildFixtureService(tmp_path, None, acquire)
    with TestClient(make_app(reopened, context)) as client:
        assert client.get(f"/v1/benchmark-analysis/jobs/{job_id}").json()["report"] is not None


def test_readonly_context_can_poll_but_cannot_create_cancel_or_retry(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    context = MutableContextProvider()
    with TestClient(make_app(service, context)) as client:
        created = client.post("/v1/benchmark-analysis/jobs", headers=HEADERS, json=BODY)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        context.context = replace(context.context, permissions=frozenset({"assets:read"}))
        assert client.get(f"/v1/benchmark-analysis/jobs/{job_id}").status_code == 200
        requests = [
            client.post("/v1/benchmark-analysis/jobs", headers=HEADERS, json=BODY),
            client.post(f"/v1/benchmark-analysis/jobs/{job_id}/cancel"),
            client.post(f"/v1/benchmark-analysis/jobs/{job_id}/retry"),
        ]
        assert [response.status_code for response in requests] == [403, 403, 403]
        context.context = replace(context.context, permissions=frozenset())
        assert client.get(f"/v1/benchmark-analysis/jobs/{job_id}").status_code == 403


def test_job_creation_requires_idempotency_and_valid_note_contract(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    with TestClient(make_app(service)) as client:
        assert client.post("/v1/benchmark-analysis/jobs", json=BODY).status_code == 422
        invalid = client.post(
            "/v1/benchmark-analysis/jobs", headers=HEADERS, json={**BODY, "note_id": "../secret"}
        )
        assert invalid.status_code == 422
        wrong_platform = client.post(
            "/v1/benchmark-analysis/jobs",
            headers=HEADERS,
            json={**BODY, "platform": "unrecognized"},
        )
        assert wrong_platform.status_code == 422
