"""Real SQLite and child-process lifecycle tests with an explicit synthetic worker.

These tests validate orchestration only, not Xiaohongshu or model-provider E2E.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from framefactory_api.benchmark_accounts import BenchmarkAccountError
from framefactory_api.benchmark_jobs import BenchmarkJobError, BenchmarkJobService

PROFILE_ID = "5a8cf39111be10466d285d6b"
PROFILE = f"https://www.xiaohongshu.com/user/profile/{PROFILE_ID}"
NOTE = "6a94178b0000000025026802"
OTHER_NOTE = "6a94178b0000000025026803"


def acquire(_profile: str, note_id: str, destination: Path) -> dict:
    destination.write_bytes(b"synthetic orchestration fixture, not video")
    return {
        "profile_user_id": PROFILE_ID,
        "note_id": note_id,
        "title": "Fixture video",
        "canonical_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "media": {"kind": "video"},
        "cookie": "must-not-persist",
        "media_url": "https://cdn.invalid/?token=secret",
    }


class ChildFixtureService(BenchmarkJobService):
    def __init__(self, *args, worker_mode="complete", **kwargs):
        super().__init__(*args, **kwargs)
        self.worker_mode = worker_mode

    def _worker_command(self, source: Path, directory: Path, title: str) -> list[str]:
        if self.worker_mode == "hang":
            return [sys.executable, "-c", "import time; time.sleep(30)"]
        if self.worker_mode == "fail":
            return [sys.executable, "-c", "raise RuntimeError('https://secret.invalid/?token=xyz')"]
        analysis = {
            "status": self.worker_mode,
            "technical": {"duration_ms": 2000, "width": 640, "height": 480, "has_audio": True},
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 2000,
                    "description": "Explicit fixture frame",
                    "transcript": "Fixture narration",
                    "ocr_text": ["Fixture screen text"],
                    "representative_frame_key": "frames/0001.jpg",
                }
            ],
            "frames": [{"timestamp_ms": 0, "key": "frames/0001.jpg"}],
            "temporal": {"speech_status": "present"},
            "capabilities": {"vision": {"status": "complete", "provider": "test_fixture"}},
            "limitations": ["Synthetic orchestration fixture"],
        }
        script = (
            "import json,pathlib,sys; "
            "directory=pathlib.Path(sys.argv[1]); "
            "(directory/'frames').mkdir(); "
            "(directory/'frames'/'0001.jpg').write_bytes(b'fixture'); "
            "(directory/'analysis.json').write_text(sys.argv[2],encoding='utf-8')"
        )
        return [sys.executable, "-c", script, str(directory), json.dumps(analysis)]


async def terminal(service, workspace, job_id):
    for _ in range(150):
        job = await service.get(workspace, job_id)
        if job["status"] not in {"pending", "collecting", "analyzing"}:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("Fixture job did not reach terminal state")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_code",
    [
        "BENCHMARK_SOURCE_CHANGED",
        "BENCHMARK_NOTE_NOT_IN_SAMPLE",
        "BENCHMARK_NOTE_MISMATCH",
        "BENCHMARK_PROFILE_MISMATCH",
        "BENCHMARK_NOTE_IDENTITY_CONFLICT",
        "BENCHMARK_NOTE_IDENTITY_INVALID",
        "BENCHMARK_NOTE_IDENTITY_INCOMPLETE",
        "BENCHMARK_NOTE_INACCESSIBLE",
        "BENCHMARK_NOTE_FIELDS_INSUFFICIENT",
        "UNEXPECTED_PROVIDER_CODE",
        None,
    ],
)
async def test_acquisition_diagnostics_persist_without_provider_exception_details(
    tmp_path,
    provider_code,
):
    private_detail = "https://fixture.invalid/note?xsec_token=never-persist-provider-detail"

    def failed_acquisition(_profile, _note, _destination):
        if provider_code is None:
            raise RuntimeError(private_detail)
        raise BenchmarkAccountError(provider_code, private_detail, retryable=True)

    expected_code = (
        provider_code
        if provider_code not in {None, "UNEXPECTED_PROVIDER_CODE"}
        else "BENCHMARK_MEDIA_ACQUISITION_FAILED"
    )
    service = ChildFixtureService(tmp_path, None, failed_acquisition)
    await service.start()
    try:
        initial = await service.create("workspace-a", PROFILE, NOTE, "source-diagnostic-request")
        failed = await terminal(service, "workspace-a", initial["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == expected_code
        assert service._process is None  # Invalid discovery must never launch paid analysis.
        assert "never-persist-provider-detail" not in json.dumps(failed)
        assert "fixture.invalid" not in json.dumps(failed)
    finally:
        await service.close()

    reopened = ChildFixtureService(tmp_path, None, failed_acquisition)
    await reopened.start()
    try:
        persisted = await reopened.get("workspace-a", initial["job_id"])
        assert persisted["status"] == "failed"
        assert persisted["error"]["code"] == expected_code
        assert "never-persist-provider-detail" not in json.dumps(persisted)
        assert not list(tmp_path.glob("*/attempt-*/source.mp4"))
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_real_sqlite_child_report_persists_and_artifacts_are_workspace_scoped(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    await service.start()
    try:
        initial = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        alias = await service.create("workspace-a", PROFILE, NOTE, "request-key-alias")
        assert alias["job_id"] == initial["job_id"]
        ready = await terminal(service, "workspace-a", initial["job_id"])
        assert ready["status"] == "ready"
        assert not (tmp_path / initial["job_id"] / "attempt-1" / "source.mp4").exists()
        assert ready["report"]["timeline"][0]["transcript"] == "Fixture narration"
        assert ready["source_evidence"]["canonical_url"].endswith(NOTE)
        serialized = json.dumps(ready)
        assert "must-not-persist" not in serialized
        assert "cdn.invalid" not in serialized
        assert ready["artifacts"] == [
            {
                "filename": "attempt-1/frames/0001.jpg",
                "media_type": "image/jpeg",
            }
        ]
        frame = await service.artifact(
            "workspace-a", initial["job_id"], "attempt-1/frames/0001.jpg"
        )
        assert frame.read_bytes() == b"fixture"
        for workspace, filename in [
            ("workspace-b", "attempt-1/frames/0001.jpg"),
            ("workspace-a", "../jobs.sqlite3"),
            ("workspace-a", "attempt-1/source.mp4"),
        ]:
            with pytest.raises(BenchmarkJobError) as error:
                await service.artifact(workspace, initial["job_id"], filename)
            assert error.value.status_code == 404
    finally:
        await service.close()
    reopened = ChildFixtureService(tmp_path, None, acquire)
    await reopened.start()
    try:
        assert (await reopened.get("workspace-a", initial["job_id"]))["status"] == "ready"
        assert (await reopened.latest("workspace-a", PROFILE_ID, NOTE))["job_id"] == initial[
            "job_id"
        ]
        assert await reopened.latest("workspace-b", PROFILE_ID, NOTE) is None
        repeat = await reopened.create("workspace-a", PROFILE, NOTE, "request-key-1")
        assert repeat["job_id"] == initial["job_id"]
        replay_alias = await reopened.create("workspace-a", PROFILE, NOTE, "request-key-alias")
        assert replay_alias["job_id"] == initial["job_id"]
        with pytest.raises(BenchmarkJobError, match="another note"):
            await reopened.create("workspace-a", PROFILE, OTHER_NOTE, "request-key-1")
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_single_owner_lock_and_bounded_queue(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire, max_outstanding=1)
    second = ChildFixtureService(tmp_path, None, acquire)
    await service.start()
    try:
        with pytest.raises(BenchmarkJobError, match="already have an owner"):
            await second.start()
        first = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        duplicate = await service.create("workspace-a", PROFILE, NOTE, "request-key-2")
        assert first["job_id"] == duplicate["job_id"]
        with pytest.raises(BenchmarkJobError) as error:
            await service.create("workspace-a", PROFILE, OTHER_NOTE, "request-key-3")
        assert error.value.status_code == 429
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancel_kills_worker_and_retry_has_immutable_attempt_directory(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire, worker_mode="hang")
    await service.start()
    try:
        initial = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        for _ in range(100):
            if service._process is not None:
                break
            await asyncio.sleep(0.01)
        process = service._process
        assert process is not None
        cancelled = await service.cancel("workspace-a", initial["job_id"])
        assert cancelled["status"] == "cancelled"
        assert process.returncode is not None
        for _ in range(100):
            if service._active_id is None:
                break
            await asyncio.sleep(0.01)
        assert not (tmp_path / initial["job_id"] / "attempt-1" / "source.mp4").exists()
        service.worker_mode = "complete"
        retry = await service.retry("workspace-a", initial["job_id"])
        assert retry["attempt"] == 2
        ready = await terminal(service, "workspace-a", initial["job_id"])
        assert ready["status"] == "ready"
        assert ready["artifacts"][0]["filename"].startswith("attempt-2/")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_timeout_is_redacted_and_does_not_leave_running_process(tmp_path):
    service = ChildFixtureService(
        tmp_path, None, acquire, worker_mode="hang", worker_timeout_seconds=0.1
    )
    await service.start()
    try:
        initial = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        failed = await terminal(service, "workspace-a", initial["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "BENCHMARK_WORKER_TIMEOUT"
        assert service._process is not None and service._process.returncode is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_acquisition_retains_slot_until_callback_exits(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocking_acquire(profile, note_id, destination):
        calls.append(note_id)
        started.set()
        release.wait(3)
        return acquire(profile, note_id, destination)

    service = ChildFixtureService(tmp_path, None, blocking_acquire)
    await service.start()
    try:
        first = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        await service.cancel("workspace-a", first["job_id"])
        second = await service.create("workspace-a", PROFILE, OTHER_NOTE, "request-key-2")
        await asyncio.sleep(0.05)
        assert calls == [NOTE]
        assert (await service.get("workspace-a", second["job_id"]))["status"] == "pending"
        with pytest.raises(BenchmarkJobError, match="still releasing"):
            await service.retry("workspace-a", first["job_id"])
        release.set()
        assert (await terminal(service, "workspace-a", second["job_id"]))["status"] == "ready"
        assert (await service.get("workspace-a", first["job_id"]))["status"] == "cancelled"
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_restart_marks_unfinished_job_recoverable(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    await service.start()
    initial = await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
    # Closing before the background task is scheduled leaves the durable pending
    # row, reproducing an API restart before worker pickup.
    await service.close()
    reopened = ChildFixtureService(tmp_path, None, acquire)
    await reopened.start()
    try:
        recovered = await reopened.get("workspace-a", initial["job_id"])
        assert recovered["status"] == "interrupted"
        assert recovered["error"]["retryable"] is True
        await reopened.retry("workspace-a", initial["job_id"])
        assert (await terminal(reopened, "workspace-a", initial["job_id"]))["status"] == "ready"
    finally:
        await reopened.close()


def test_provider_environment_is_allowlisted_and_output_redacted(tmp_path, monkeypatch):
    configuration = tmp_path / "provider.env"
    configuration.write_text(
        "FRAMEFACTORY_ASSET_VISION_API_KEY=only-worker-secret\n"
        "FRAMEFACTORY_ASR_MODEL=actual-provider\n"
        "FRAMEFACTORY_BENCHMARK_MAX_DURATION_SECONDS=99999\n"
        "DATABASE_URL=do-not-forward\n"
        "PYTHONPATH=untrusted-python-path\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OTHER_API_KEY", "unrelated-secret")
    service = ChildFixtureService(tmp_path / "jobs", configuration, acquire)
    environment = service._worker_environment()
    assert environment["FRAMEFACTORY_ASSET_VISION_API_KEY"] == "only-worker-secret"
    assert environment["FRAMEFACTORY_BENCHMARK_MAX_DURATION_SECONDS"] == "600"
    assert "OTHER_API_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert environment.get("PYTHONPATH") != "untrusted-python-path"
    cleaned = service._sanitize(
        {
            "message": "failed only-worker-secret https://cdn.invalid/?xsec_token=secret",
            "api_key": "hidden",
            "token": "hidden",
        }
    )
    assert "only-worker-secret" not in json.dumps(cleaned)
    assert "cdn.invalid" not in json.dumps(cleaned)
    assert "api_key" not in cleaned


@pytest.mark.parametrize(
    "filename",
    [
        "../secret.jpg",
        "C:/secret.jpg",
        "frames\\secret.jpg",
        "/absolute.jpg",
        "source.mp4",
        "file:secret.jpg",
    ],
)
def test_artifact_path_rejects_escape_and_source_media(tmp_path, filename):
    assert BenchmarkJobService._artifact_path(tmp_path, filename) is None


@pytest.mark.asyncio
async def test_storage_budget_rejects_before_any_acquisition_without_deleting_files(tmp_path):
    untouched = tmp_path / "caller-note.txt"
    untouched.write_text("preserve unrelated storage content", encoding="utf-8")
    service = ChildFixtureService(tmp_path, None, acquire, max_storage_bytes=1024)
    await service.start()
    try:
        with pytest.raises(BenchmarkJobError) as error:
            await service.create("workspace-a", PROFILE, NOTE, "request-key-1")
        assert error.value.status_code == 507
        assert error.value.code == "BENCHMARK_JOB_STORAGE_FULL"
        assert untouched.read_text(encoding="utf-8") == "preserve unrelated storage content"
        assert list(tmp_path.glob("*/source.mp4")) == []
    finally:
        await service.close()


def test_evidence_frame_keys_and_audio_preview_use_same_attempt_scope(tmp_path):
    service = ChildFixtureService(tmp_path, None, acquire)
    result = service._prefix_artifact_keys(
        {
            "audio_analysis": {"audio_key": "audio.wav"},
            "creative_insights": [{"evidence_frame_keys": ["frames/0001.jpg"]}],
        },
        "attempt-2",
    )
    assert result["audio_analysis"]["audio_key"] == "attempt-2/audio.wav"
    assert result["creative_insights"][0]["evidence_frame_keys"] == ["attempt-2/frames/0001.jpg"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object crash recovery")
def test_api_hard_crash_kills_actual_worker_and_grandchild_then_recovers_sqlite(tmp_path):
    """Hard-kill an independent API stub, not a mocked process or graceful close."""
    import ctypes
    from ctypes import wintypes

    child_code = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "if sys.stdin.buffer.read(1) != b'1': sys.exit(125)\n"
        "grandchild=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
        "creationflags=0x08000000)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'worker':os.getpid(),"
        "'grandchild':grandchild.pid}),encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    parent_code = """
import asyncio,json,pathlib,sys
from framefactory_api.benchmark_jobs import BenchmarkJobService
root=pathlib.Path(sys.argv[1])
child_code=sys.argv[2]
class Service(BenchmarkJobService):
    def _worker_command(self, source, directory, title):
        return [sys.executable,'-c',child_code,str(root/'tree.json')]
def acquire(profile,note,destination):
    destination.write_bytes(b'explicit lifecycle fixture')
    return {'profile_user_id':'5a8cf39111be10466d285d6b','note_id':note,'title':'Crash fixture'}
async def run():
    service=Service(root/'jobs',None,acquire)
    await service.start()
    job=await service.create('workspace-a',
        'https://www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b',
        '6a94178b0000000025026802','hard-crash-request')
    (root/'job.json').write_text(json.dumps({'job_id':job['job_id']}),encoding='utf-8')
    while True:
        await asyncio.sleep(0.1)
asyncio.run(run())
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, str(tmp_path), child_code],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000,
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / "tree.json").exists() and time.monotonic() < deadline:
            assert parent.poll() is None, "API lifecycle stub exited before worker launch"
            time.sleep(0.02)
        tree = json.loads((tmp_path / "tree.json").read_text(encoding="utf-8"))
        for pid in (tree["worker"], tree["grandchild"]):
            handle = kernel.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | TERMINATE
            assert handle
            handles.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 258  # still running
        parent.kill()  # Terminate only the API stub; its kernel-owned Job closes.
        parent.wait(timeout=5)
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 5000) == 0, "Orphaned worker descendant"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
    job_id = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))["job_id"]

    async def recover():
        service = ChildFixtureService(tmp_path / "jobs", None, acquire)
        await service.start()
        try:
            recovered = await service.get("workspace-a", job_id)
            assert recovered["status"] == "interrupted"
            assert recovered["error"]["retryable"] is True
            assert not (tmp_path / "jobs" / job_id / "attempt-1" / "source.mp4").exists()
            await service.retry("workspace-a", job_id)
            assert (await terminal(service, "workspace-a", job_id))["status"] == "ready"
        finally:
            await service.close()

    asyncio.run(recover())


@pytest.mark.skipif(os.name != "nt", reason="Windows worker ready-pipe handshake")
def test_windows_bootstrap_exits_on_eof_before_importing_worker(tmp_path):
    service = BenchmarkJobService(tmp_path, None, acquire)
    command = service._worker_command(tmp_path / "missing.mp4", tmp_path, "fixture")
    result = subprocess.run(
        command, input=b"", capture_output=True, timeout=5, creationflags=0x08000000
    )
    assert result.returncode == 125
    assert not (tmp_path / "analysis.json").exists()
