"""Real SQLite/HTTP history compatibility; collection and workers are explicit fixtures."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_benchmark_discovery_recovery import UID, URL, page
from test_benchmark_job_routes import MutableContextProvider
from test_benchmark_jobs import NOTE, PROFILE, ChildFixtureService, acquire, terminal

from framefactory_api.benchmark_accounts import BenchmarkAccountGateway
from framefactory_api.benchmark_history import BenchmarkHistoryDetail
from framefactory_api.benchmark_jobs import BenchmarkJobService
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings

BODY = {"platform": "xiaohongshu", "profile_url": URL}


def history_app(service=None, context=None, gateway=None):
    return create_app(
        settings=Settings(),
        repository=InMemoryControlRepository(),
        benchmark_account_gateway=gateway or BenchmarkAccountGateway(fetcher=lambda *_: page()),
        benchmark_job_service=service,
        context_provider=context,
    )


def test_account_refresh_saves_deduplicated_history_and_reopens_with_discovery_fields(
    tmp_path, monkeypatch,
):
    service = BenchmarkJobService(tmp_path, None, acquire)
    context = MutableContextProvider()
    gateway = BenchmarkAccountGateway(fetcher=lambda *_: page())
    requested_refresh = []
    collect = gateway.collect

    async def record_refresh(platform, profile_url, *, refresh_note_identity=False):
        requested_refresh.append(refresh_note_identity)
        return await collect(platform, profile_url, refresh_note_identity=refresh_note_identity)

    monkeypatch.setattr(gateway, "collect", record_refresh)
    with TestClient(history_app(service, context, gateway)) as client:
        first = client.post("/v1/benchmark-accounts/report", json=BODY)
        assert first.status_code == 200, first.text
        record_id = first.json()["history_record_id"]
        assert record_id
        assert first.json()["history_warning"] is None
        repeat = client.post(
            "/v1/benchmark-accounts/report", json={**BODY, "refresh_note_identity": True},
        )
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["history_record_id"] == record_id
        assert requested_refresh == [False, True]
        listed = client.get("/v1/benchmark-history", params={"kind": "account", "q": UID})
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["items"]) == 1
        assert listed.json()["items"][0]["record_id"] == record_id
        assert listed.headers["Cache-Control"] == "private, no-store"
        assert client.get("/v1/benchmark-history", params={"kind": "video"}).json()["items"] == []

    reopened = BenchmarkJobService(tmp_path, None, acquire)
    with TestClient(history_app(reopened, context)) as client:
        detail = client.get(f"/v1/benchmark-history/{record_id}")
        assert detail.status_code == 200, detail.text
        assert detail.headers["Cache-Control"] == "private, no-store"
        report = detail.json()["account_report"]
        assert report["history_record_id"] == record_id
        assert report["snapshot"]["acquisition"]["discovery_version"] == "1"
        assert report["snapshot"]["acquisition"]["note_identity_status"] == "complete"
        assert report["snapshot"]["notes"][0]["identity_status"] == "verified"


def test_history_workspace_scope_permissions_and_invalid_cursor(tmp_path):
    context = MutableContextProvider()
    with TestClient(history_app(BenchmarkJobService(tmp_path, None, acquire), context)) as client:
        saved = client.post("/v1/benchmark-accounts/report", json=BODY)
        record_id = saved.json()["history_record_id"]
        original = context.context
        context.context = replace(original, workspace_id=uuid4())
        assert client.get("/v1/benchmark-history").json()["items"] == []
        assert client.get(f"/v1/benchmark-history/{record_id}").status_code == 404
        cursor = client.get("/v1/benchmark-history", params={"cursor": record_id})
        assert cursor.status_code == 400
        assert cursor.json()["code"] == "BENCHMARK_HISTORY_CURSOR_INVALID"
        context.context = replace(original, permissions=frozenset({"assets:read"}))
        assert client.get(f"/v1/benchmark-history/{record_id}").status_code == 200
        assert client.post("/v1/benchmark-accounts/report", json=BODY).status_code == 403
        context.context = replace(original, permissions=frozenset())
        assert client.get("/v1/benchmark-history").status_code == 403
        assert client.get(f"/v1/benchmark-history/{record_id}").status_code == 403


def test_sqlite_archive_failure_returns_completed_report_with_recovery_warning(
    tmp_path, monkeypatch,
):
    service = BenchmarkJobService(tmp_path, None, acquire)

    def failed_save(*_):
        raise sqlite3.OperationalError("read-only database /private/local/path")

    monkeypatch.setattr(service, "save_account_history", failed_save)
    with TestClient(history_app(service)) as client:
        response = client.post("/v1/benchmark-accounts/report", json=BODY)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["history_record_id"] is None
        assert "保存失败" in payload["history_warning"]
        assert payload["note_reports"]
        assert "/private/local/path" not in response.text


def test_disabled_history_keeps_account_report_available_and_reports_storage_unavailable():
    with TestClient(history_app()) as client:
        report = client.post("/v1/benchmark-accounts/report", json=BODY)
        assert report.status_code == 200, report.text
        assert report.json()["history_record_id"] is None
        assert "历史存储未启用" in report.json()["history_warning"]
        unavailable = client.get("/v1/benchmark-history")
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "BENCHMARK_ANALYSIS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_legacy_video_backfills_before_media_retention_and_remains_readable(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    await service.start()
    try:
        job = await service.create(
            workspace_id="history-test", profile_url=PROFILE, note_id=NOTE,
            idempotency_key="history-retention-test",
        )
        complete = await terminal(service, "history-test", job["job_id"])
        assert complete["status"] == "ready"
        saved = service.history.list("history-test", kind="video")["items"]
        assert len(saved) == 1
        before = BenchmarkHistoryDetail.model_validate(
            service.history.get("history-test", saved[0]["record_id"]),
        )
        assert before.media_available is True
        assert before.video_job.artifacts
        # Simulate a pre-history database with an expired completed job. Startup
        # must archive it before deleting its retained media and live job row.
        expired = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        with service._db:
            service._db.execute("DELETE FROM benchmark_history")
            service._db.execute("UPDATE jobs SET updated_at=?", (expired,))
    finally:
        await service.close()

    reopened = ChildFixtureService(tmp_path, None, acquire)
    await reopened.start()
    try:
        assert reopened._find(job["job_id"]) is None
        records = reopened.history.list("history-test", kind="video")["items"]
        assert len(records) == 1
        detail = BenchmarkHistoryDetail.model_validate(
            reopened.history.get("history-test", records[0]["record_id"]),
        )
        assert detail.video_job.report is not None
        assert detail.media_available is False
        assert detail.video_job.artifacts == []
        serialized = json.dumps(detail.model_dump(mode="json"))
        assert "must-not-persist" not in serialized
        assert "cdn.invalid" not in serialized
        assert "profile_url" not in detail.video_job.model_dump()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_cleanup_failure_updates_same_attempt_archive_with_partial_warning(
    tmp_path, monkeypatch,
):
    service = ChildFixtureService(tmp_path, None, acquire)
    ready_record_ids = []
    cleanup = service._cleanup_temporary_source
    unlink = Path.unlink

    def failed_source_unlink(path, *args, **kwargs):
        if path.name == "source.mp4" and path.is_relative_to(tmp_path):
            raise PermissionError("Explicit fixture for a locked temporary media file")
        return unlink(path, *args, **kwargs)

    def observe_completion_then_cleanup(job):
        records = service.history.list("history-test")["items"]
        if records and records[0]["status"] == "ready":
            ready_record_ids.append(records[0]["record_id"])
        cleanup(job)

    monkeypatch.setattr(Path, "unlink", failed_source_unlink)
    monkeypatch.setattr(service, "_cleanup_temporary_source", observe_completion_then_cleanup)
    await service.start()
    try:
        job = await service.create(
            workspace_id="history-test", profile_url=PROFILE, note_id=NOTE,
            idempotency_key="history-cleanup-test",
        )
        complete = await terminal(service, "history-test", job["job_id"])
        assert complete["status"] == "partial"
        records = service.history.list("history-test")["items"]
        assert len(records) == 1
        assert ready_record_ids == [records[0]["record_id"]]
        assert records[0]["status"] == "partial"
        archived = service.history.get("history-test", records[0]["record_id"])
        assert archived["video_job"]["report"]["status"] == "partial"
        limitations = archived["video_job"]["report"]["limitations"]
        assert any("cleanup failed" in item for item in limitations)
    finally:
        await service.close()
