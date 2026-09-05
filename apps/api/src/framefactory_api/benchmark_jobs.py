"""Durable, explicitly local benchmark jobs; no asset-library or queue side effects.

One API process owns this SQLite directory. The real media provider supervises a
cancellable child tree; legacy acquisition callbacks retain the sole lane until
they exit. Source, derived and transient files are measured during acquisition and
CPU/model execution; over-budget work is terminated before cleanup. Neither
provider logs nor transient source URLs are persisted or returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .benchmark_accounts import resolve_xiaohongshu_profile
from .benchmark_history import BenchmarkHistoryStore
from .benchmark_media_reports import (
    build_benchmark_deep_note_report,
    timeline_from_worker_analysis,
)
from .owned_process import WindowsProcessJob as _WindowsWorkerJob

MediaAcquirer = Callable[[str, str, Path], dict[str, Any]]
_ACTIVE = {"pending", "collecting", "analyzing"}
_RETRYABLE = {"failed", "cancelled", "interrupted"}
_ID = re.compile(r"^[0-9a-f]{24}$")
_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ENV_PREFIXES = (
    "FRAMEFACTORY_ASSET_VISION_",
    "FRAMEFACTORY_ASR_",
    "FRAMEFACTORY_BENCHMARK_",
)
_ARTIFACT_TYPES = {".jpg": "image/jpeg", ".wav": "audio/wav", ".mp3": "audio/mpeg"}
_MAX_JSON_BYTES = 8 * 1024 * 1024
_ATTEMPT_STORAGE_BYTES = 256 * 1024**2
_FREE_DISK_HEADROOM_BYTES = 32 * 1024**2
_PERSISTENCE_HEADROOM_BYTES = 32 * 1024**2
_STORAGE_POLL_SECONDS = 0.1
_SECRET_FIELDS = re.compile(r"(?:token|cookie|secret|password|api_key|authorization)", re.I)


class BenchmarkJobError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = status_code in {409, 429, 503, 507}


class BenchmarkJobService:
    """Workspace-scoped SQLite state plus one bounded local execution lane."""

    def __init__(
        self,
        root: Path,
        provider_env_file: Path | None,
        media_acquirer: MediaAcquirer,
        *,
        media_timeout_seconds: float = 120,
        worker_timeout_seconds: float = 1800,
        max_outstanding: int = 10,
        retention_days: int = 7,
        max_storage_bytes: int = 2 * 1024**3,
    ) -> None:
        self.root = root.resolve()
        self._env_file = provider_env_file
        self._acquire = media_acquirer
        # Older embedding callbacks retain their three-argument contract. The
        # real provider accepts this event and terminates its owned child tree.
        self._acquire_cancellable = "cancel_event" in inspect.signature(media_acquirer).parameters
        self._acquire_cancel: threading.Event | None = None
        self._media_timeout = max(0.1, min(media_timeout_seconds, 120))
        self._worker_timeout = max(0.1, min(worker_timeout_seconds, 1800))
        self._capacity = max(1, min(max_outstanding, 10))
        self._retention_days = max(1, min(retention_days, 7))
        self._storage_limit = max(1024, min(max_storage_bytes, 20 * 1024**3))
        self._attempt_storage_limit = min(_ATTEMPT_STORAGE_BYTES, self._storage_limit)
        self._persistence_headroom = min(_PERSISTENCE_HEADROOM_BYTES, self._storage_limit // 16)
        self._connection: sqlite3.Connection | None = None
        self._lock_file: BinaryIO | None = None
        self._runner: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._process: asyncio.subprocess.Process | None = None
        self._worker_job: _WindowsWorkerJob | None = None
        self._active_id: str | None = None
        self._attempt_directory: Path | None = None
        self._secrets: list[str] = []
        self._last_cleanup = 0.0

    async def start(self) -> None:
        if self._runner is not None:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_file = (self.root / "runner.lock").open("a+b")
        try:
            self._lock_file.seek(0, 2)
            if not self._lock_file.tell():
                self._lock_file.write(b"0")
                self._lock_file.flush()
            self._lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise BenchmarkJobError(
                "BENCHMARK_JOBS_ALREADY_RUNNING", "Local benchmark jobs already have an owner", 503
            ) from exc
        try:
            self._connection = sqlite3.connect(self.root / "jobs.sqlite3", timeout=5)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            if self._connection.execute("PRAGMA user_version").fetchone()[0] not in {0, 1, 2}:
                raise BenchmarkJobError(
                    "BENCHMARK_JOBS_SCHEMA_UNSUPPORTED",
                    "Unsupported local job database version",
                    503,
                )
            self._connection.executescript(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
                "request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "profile_user_id TEXT NOT NULL, note_id TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "payload TEXT NOT NULL, UNIQUE(workspace_id, request_key));"
                "CREATE INDEX IF NOT EXISTS jobs_latest ON jobs "
                "(workspace_id, profile_user_id, note_id, created_at DESC);"
                "CREATE TABLE IF NOT EXISTS job_requests ("
                "workspace_id TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE, "
                "PRIMARY KEY(workspace_id, request_key));"
                "INSERT OR IGNORE INTO job_requests SELECT workspace_id,request_key,fingerprint,"
                "job_id FROM jobs;"
                "PRAGMA user_version=2;"
            )
            self.history.migrate()
            # Archive existing completed reports before the legacy media TTL runs.
            with self._db:
                for row in self._db.execute(
                    "SELECT payload FROM jobs WHERE status IN ('ready','partial')"
                ):
                    self.history.save_video(json.loads(row[0]))
            for job in self._query("SELECT payload FROM jobs WHERE status IN (?,?,?)", *_ACTIVE):
                self._finish_error(
                    job,
                    "interrupted",
                    "BENCHMARK_JOB_INTERRUPTED",
                    "The previous local process stopped. Retry to resume analysis.",
                )
                previous_attempt = self._directory(job) / f"attempt-{int(job['attempt'])}"
                if previous_attempt.is_dir() and not previous_attempt.is_symlink():
                    self._attempt_directory = previous_attempt
                    self._cleanup_temporary_source(job)
                    self._attempt_directory = None
            self._cleanup_expired()
            self._closing = False
            self._runner = asyncio.create_task(self._run(), name="benchmark-local-jobs")
        except BaseException:
            self._release()
            raise

    async def close(self) -> None:
        self._closing = True
        if self._acquire_cancel:
            self._acquire_cancel.set()
        if self._active_id:
            job = self._find(self._active_id)
            if job and job["status"] in _ACTIVE:
                self._finish_error(
                    job,
                    "interrupted",
                    "BENCHMARK_JOB_INTERRUPTED",
                    "The local service stopped. Retry to resume analysis.",
                )
        await self._stop_process()
        self._wake.set()
        if self._runner:
            # The provider owns a total I/O deadline. Wait for its thread so a
            # new API process cannot acquire the same browser concurrently.
            await self._runner
            self._runner = None
        self._release()

    async def create(
        self,
        workspace_id: str,
        profile_url: str,
        note_id: str,
        idempotency_key: str,
        title: str = "",
    ) -> dict[str, Any]:
        self._ensure_running()
        self._validate_workspace(workspace_id)
        if not _ID.fullmatch(note_id) or not _KEY.fullmatch(idempotency_key):
            raise BenchmarkJobError(
                "BENCHMARK_JOB_INPUT_INVALID", "A valid note ID and idempotency key are required"
            )
        identity = resolve_xiaohongshu_profile(profile_url)
        request_key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        fingerprint = hashlib.sha256(f"{identity.profile_url}:{note_id}".encode()).hexdigest()
        existing = self._db.execute(
            "SELECT j.payload,r.fingerprint FROM job_requests r JOIN jobs j ON j.job_id=r.job_id "
            "WHERE r.workspace_id=? AND r.request_key=?",
            (workspace_id, request_key),
        ).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise BenchmarkJobError(
                    "BENCHMARK_IDEMPOTENCY_CONFLICT",
                    "This request key belongs to another note",
                    409,
                )
            return self._public(json.loads(existing["payload"]))
        # Repeated clicks with fresh keys cannot spend twice on an active note.
        active = self._query(
            "SELECT payload FROM jobs WHERE workspace_id=? AND profile_user_id=? AND note_id=? "
            "AND status IN (?,?,?) LIMIT 1",
            workspace_id,
            identity.external_user_id,
            note_id,
            *_ACTIVE,
        )
        if active:
            with self._db:
                self._db.execute(
                    "INSERT INTO job_requests VALUES (?,?,?,?)",
                    (workspace_id, request_key, fingerprint, active[0]["job_id"]),
                )
            return self._public(active[0])
        self._check_capacity()
        now = _now()
        job = {
            "schema_version": "1.0.0",
            "job_id": str(uuid4()),
            "workspace_id": workspace_id,
            "profile_user_id": identity.external_user_id,
            "note_id": note_id,
            "title": self._sanitize(title[:300]),
            "profile_url": identity.profile_url,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "attempt": 1,
            "progress": {"stage": "pending", "percent": 0, "message": "Waiting for local worker"},
            "source_evidence": None,
            "analysis": None,
            "report": None,
            "error": None,
            "artifacts": [],
        }
        with self._db:
            self._db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job["job_id"],
                    workspace_id,
                    request_key,
                    fingerprint,
                    identity.external_user_id,
                    note_id,
                    "pending",
                    now,
                    now,
                    json.dumps(job, ensure_ascii=False),
                ),
            )
            self._db.execute(
                "INSERT INTO job_requests VALUES (?,?,?,?)",
                (workspace_id, request_key, fingerprint, job["job_id"]),
            )
        self._wake.set()
        return self._public(job)

    async def get(self, workspace_id: str, job_id: str) -> dict[str, Any]:
        return self._public(self._owned(workspace_id, job_id))

    async def latest(
        self,
        workspace_id: str,
        profile_user_id: str,
        note_id: str | None = None,
    ) -> dict[str, Any] | None:
        self._ensure_running()
        self._validate_workspace(workspace_id)
        if not _ID.fullmatch(profile_user_id) or (note_id and not _ID.fullmatch(note_id)):
            raise BenchmarkJobError("BENCHMARK_JOB_INPUT_INVALID", "Invalid profile or note ID")
        query = "SELECT payload FROM jobs WHERE workspace_id=? AND profile_user_id=?"
        parameters: list[Any] = [workspace_id, profile_user_id]
        if note_id:
            query += " AND note_id=?"
            parameters.append(note_id)
        found = self._query(query + " ORDER BY created_at DESC LIMIT 1", *parameters)
        return self._public(found[0]) if found else None

    async def cancel(self, workspace_id: str, job_id: str) -> dict[str, Any]:
        job = self._owned(workspace_id, job_id)
        if job["status"] in _ACTIVE:
            self._finish_error(job, "cancelled", "BENCHMARK_JOB_CANCELLED", "Cancelled by the user")
            if self._active_id == job_id:
                if self._acquire_cancel:
                    self._acquire_cancel.set()
                await self._stop_process()
        return self._public(job)

    async def retry(self, workspace_id: str, job_id: str) -> dict[str, Any]:
        job = self._owned(workspace_id, job_id)
        if job["status"] not in _RETRYABLE:
            raise BenchmarkJobError(
                "BENCHMARK_JOB_NOT_RETRYABLE", "This job cannot be retried", 409
            )
        if job["attempt"] >= 3:
            raise BenchmarkJobError("BENCHMARK_JOB_ATTEMPTS_EXHAUSTED", "Three attempts used", 409)
        if self._active_id == job_id:
            raise BenchmarkJobError(
                "BENCHMARK_JOB_STILL_STOPPING",
                "The previous attempt is still releasing resources",
                409,
            )
        competing = self._query(
            "SELECT payload FROM jobs WHERE workspace_id=? AND profile_user_id=? AND note_id=? "
            "AND status IN (?,?,?) LIMIT 1",
            workspace_id,
            job["profile_user_id"],
            job["note_id"],
            *_ACTIVE,
        )
        if competing:
            raise BenchmarkJobError(
                "BENCHMARK_JOB_ALREADY_ACTIVE", "Another analysis of this note is active", 409
            )
        self._check_capacity()
        job.update(
            status="pending",
            attempt=job["attempt"] + 1,
            error=None,
            analysis=None,
            report=None,
            artifacts=[],
            source_evidence=None,
        )
        job["progress"] = {"stage": "pending", "percent": 0, "message": "Waiting to retry"}
        self._save(job)
        self._wake.set()
        return self._public(job)

    async def artifact(self, workspace_id: str, job_id: str, filename: str) -> Path:
        job = self._owned(workspace_id, job_id)
        if filename not in {item["filename"] for item in job["artifacts"]}:
            raise BenchmarkJobError("BENCHMARK_ARTIFACT_NOT_FOUND", "Artifact not found", 404)
        path = self._artifact_path(self._directory(job), filename)
        if path is None or not path.is_file():
            raise BenchmarkJobError("BENCHMARK_ARTIFACT_NOT_FOUND", "Artifact not found", 404)
        return path

    async def _run(self) -> None:
        while not self._closing:
            if time.monotonic() - self._last_cleanup > 3600:
                self._cleanup_expired()
            pending = self._query(
                "SELECT payload FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1"
            )
            if not pending:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=60)
                if time.monotonic() - self._last_cleanup > 3600:
                    self._cleanup_expired()
                continue
            job = pending[0]
            self._active_id = job["job_id"]
            try:
                await self._execute(job)
            except Exception:
                # Internal exceptions can contain provider credentials and URLs.
                latest = self._find(job["job_id"])
                if latest and latest["status"] in _ACTIVE:
                    self._finish_error(
                        latest,
                        "failed",
                        "BENCHMARK_JOB_FAILED",
                        "Analysis could not finish. Check provider availability and retry.",
                    )
            finally:
                await self._stop_process()
                self._cleanup_temporary_source(job)
                self._attempt_directory = None
                self._acquire_cancel = None
                self._active_id = None

    async def _execute(self, job: dict[str, Any]) -> None:
        try:
            self._check_storage_capacity()
        except BenchmarkJobError:
            self._finish_if_active(
                job, "BENCHMARK_JOB_STORAGE_FULL", "Local analysis storage is full"
            )
            return
        directory = self._directory(job)
        directory.mkdir(parents=True, exist_ok=True)
        # Every retry uses a fresh directory, so old output can never be mistaken
        # for a successful result of a newer worker attempt.
        attempt_dir = directory / f"attempt-{job['attempt']}"
        attempt_dir.mkdir(exist_ok=False)
        self._attempt_directory = attempt_dir
        source = attempt_dir / "source.mp4"
        self._progress(job, "collecting", 5, "Acquiring the selected note's media")
        self._acquire_cancel = threading.Event()
        acquisition = asyncio.create_task(
            asyncio.to_thread(
                self._acquire,
                job["profile_url"],
                job["note_id"],
                source,
                **({"cancel_event": self._acquire_cancel} if self._acquire_cancellable else {}),
            )
        )
        try:
            deadline = time.monotonic() + self._media_timeout
            while not acquisition.done():
                await asyncio.to_thread(self._check_running_storage, attempt_dir)
                if not self._is_active(job) or self._closing:
                    self._acquire_cancel.set()
                if time.monotonic() >= deadline:
                    raise TimeoutError
                await asyncio.wait({acquisition}, timeout=_STORAGE_POLL_SECONDS)
            evidence = acquisition.result()
            await asyncio.to_thread(self._check_running_storage, attempt_dir)
        except BenchmarkJobError as exc:
            self._acquire_cancel.set()
            with contextlib.suppress(Exception):
                await acquisition
            self._finish_if_active(job, exc.code, exc.message)
            return
        except TimeoutError:
            self._acquire_cancel.set()
            self._finish_if_active(
                job, "BENCHMARK_MEDIA_TIMEOUT", "Media acquisition exceeded its time limit"
            )
            # Do not overlap another acquisition with a timed-out Python thread.
            with contextlib.suppress(Exception):
                await acquisition
            return
        except Exception as exc:
            code = getattr(exc, "code", "")
            permitted = {
                "BENCHMARK_AUTHENTICATION_REQUIRED",
                "BENCHMARK_NOTE_UNAVAILABLE",
                "BENCHMARK_MEDIA_UNAVAILABLE",
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "BENCHMARK_MEDIA_TOO_LARGE",
                "BENCHMARK_SOURCE_UNAVAILABLE",
                "BENCHMARK_SOURCE_CHANGED",
                "BENCHMARK_NOTE_NOT_IN_SAMPLE",
                "BENCHMARK_NOTE_MISMATCH",
                "BENCHMARK_PROFILE_MISMATCH",
                "BENCHMARK_NOTE_IDENTITY_CONFLICT",
                "BENCHMARK_NOTE_IDENTITY_INVALID",
                "BENCHMARK_NOTE_IDENTITY_INCOMPLETE",
                "BENCHMARK_NOTE_INACCESSIBLE",
                "BENCHMARK_NOTE_FIELDS_INSUFFICIENT",
            }
            self._finish_if_active(
                job,
                code if code in permitted else "BENCHMARK_MEDIA_ACQUISITION_FAILED",
                "The source media is unavailable. Check login and provider connection, then retry.",
            )
            return
        if not self._is_active(job):
            return
        if (
            not source.is_file()
            or source.is_symlink()
            or not 0 < source.stat().st_size <= 250_000_000
        ):
            self._finish_if_active(job, "BENCHMARK_MEDIA_INVALID", "Media size or file is invalid")
            return
        if not isinstance(evidence, dict):
            raise ValueError("Provider evidence must be an object")
        if evidence.get("note_id") != job["note_id"]:
            raise ValueError("Provider returned another note")
        if evidence.get("profile_user_id") != job["profile_user_id"]:
            raise ValueError("Provider returned another profile")
        job["source_evidence"] = self._sanitize(evidence)
        job["title"] = self._sanitize(
            str(evidence.get("title") or job["title"] or job["note_id"])[:200]
        )
        self._progress(job, "analyzing", 15, "Analyzing frames, on-screen text and audio")
        environment = self._worker_environment()
        if os.name == "nt":
            self._worker_job = _WindowsWorkerJob()
        self._process = await asyncio.create_subprocess_exec(
            *self._worker_command(source, attempt_dir, job["title"]),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}),
        )
        if not self._is_active(job) or self._closing:
            await self._stop_process()
            return
        if self._worker_job:
            self._worker_job.attach(self._process.pid)
        # The Windows bootstrap waits on this pipe before importing Worker code.
        # Parent death before Job Object assignment gives EOF, so the bootstrap
        # exits without creating grandchildren or spending provider requests.
        if self._process.stdin:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self._process.stdin.write(b"1")
                await self._process.stdin.drain()
            self._process.stdin.close()
        deadline = time.monotonic() + self._worker_timeout
        while self._process.returncode is None:
            try:
                await asyncio.to_thread(self._check_running_storage, attempt_dir)
            except BenchmarkJobError as exc:
                await self._stop_process()
                self._finish_if_active(job, exc.code, exc.message)
                return
            if not self._is_active(job) or self._closing:
                await self._stop_process()
                return
            if time.monotonic() >= deadline:
                await self._stop_process()
                self._finish_if_active(job, "BENCHMARK_WORKER_TIMEOUT", "Analysis timed out")
                return
            try:
                await asyncio.wait_for(self._process.wait(), timeout=_STORAGE_POLL_SECONDS)
            except TimeoutError:
                self._read_progress(job, attempt_dir)
        if not self._is_active(job):
            return
        try:
            await asyncio.to_thread(self._check_running_storage, attempt_dir)
        except BenchmarkJobError as exc:
            self._finish_if_active(job, exc.code, exc.message)
            return
        if self._process.returncode != 0:
            progress = self._read_json(attempt_dir / "progress.json", limit=16_384)
            if (
                isinstance(progress, dict)
                and progress.get("message") == "media_storage_budget_exceeded"
            ):
                self._finish_if_active(
                    job, "BENCHMARK_JOB_STORAGE_FULL", "Analysis exceeded its local storage budget"
                )
                return
            self._finish_if_active(
                job,
                "BENCHMARK_WORKER_FAILED",
                "The analysis worker failed. Check provider configuration.",
            )
            return
        analysis = self._read_json(attempt_dir / "analysis.json")
        if not isinstance(analysis, dict):
            raise ValueError("Missing worker analysis")
        technical = analysis.get("technical") or {}
        if not 0 < int(technical.get("duration_ms") or 0) <= 600_000:
            raise ValueError("Media duration is outside the local job limit")
        # Frame keys are scoped to a specific immutable attempt before publishing.
        analysis = self._prefix_artifact_keys(analysis, attempt_dir.name)
        analysis = self._sanitize(analysis)
        timeline = timeline_from_worker_analysis(
            title=job["title"],
            source_label="Xiaohongshu authenticated media · local worker",
            analysis=analysis,
        )
        report = build_benchmark_deep_note_report(timeline).model_dump(mode="json")
        status = "ready" if analysis.get("status") == "complete" else "partial"
        if report["status"] == "partial":
            status = "partial"
        report["status"] = status
        job.update(analysis=analysis, report=report, status=status, error=None)
        job["artifacts"] = self._artifacts(directory, analysis)
        job["progress"] = {"stage": status, "percent": 100, "message": "Analysis complete"}
        self._save(job)

    def _worker_command(self, source: Path, directory: Path, title: str) -> list[str]:
        arguments = [
            "--source",
            str(source),
            "--output-dir",
            str(directory),
            "--title",
            title,
        ]
        if os.name == "nt":
            bootstrap = (
                "import runpy,sys\n"
                "if sys.stdin.buffer.read(1) != b'1': sys.exit(125)\n"
                "sys.argv[0] = 'framefactory.worker.benchmark_analysis'\n"
                "runpy.run_module('framefactory.worker.benchmark_analysis',run_name='__main__')\n"
            )
            return [sys.executable, "-c", bootstrap, *arguments]
        return [sys.executable, "-m", "framefactory.worker.benchmark_analysis", *arguments]

    def _worker_environment(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "HOME",
            "USERPROFILE",
            "VIRTUAL_ENV",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed or key.startswith(_ENV_PREFIXES)
        }
        if self._env_file:
            if not self._env_file.is_file() or self._env_file.stat().st_size > 65_536:
                raise BenchmarkJobError(
                    "BENCHMARK_PROVIDER_CONFIG_INVALID",
                    "Provider configuration is unavailable",
                    503,
                )
            for line in self._env_file.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if key.startswith(_ENV_PREFIXES) and re.fullmatch(r"[A-Z0-9_]+", key):
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                        value = value[1:-1]
                    environment[key] = value
        self._secrets = [
            value
            for key, value in environment.items()
            if _SECRET_FIELDS.search(key) and len(value) >= 6
        ]
        worker_root = Path(__file__).resolve().parents[4] / "services" / "worker"
        if worker_root.is_dir():
            environment["PYTHONPATH"] = str(worker_root)
        environment["PYTHONUNBUFFERED"] = "1"
        # Hard cap independent of a user-provided env file.
        environment["FRAMEFACTORY_BENCHMARK_MAX_DURATION_SECONDS"] = "600"
        environment["FRAMEFACTORY_BENCHMARK_MAX_ATTEMPT_BYTES"] = str(self._attempt_storage_limit)
        environment["FRAMEFACTORY_BENCHMARK_ALLOW_MODEL_DOWNLOAD"] = "false"
        if self._attempt_directory:
            # Provider libraries must place transient files under the watched
            # attempt, so cancellation/expiry cleanup also owns those files.
            temporary = self._attempt_directory / "tmp"
            temporary.mkdir(exist_ok=True)
            for key in ("TEMP", "TMP", "TMPDIR"):
                environment[key] = str(temporary)
        return environment

    async def _stop_process(self) -> None:
        process = self._process
        owned_windows_job = self._worker_job
        if owned_windows_job:
            owned_windows_job.close()
            self._worker_job = None
        if process is None or process.returncode is not None:
            return
        # Killing a process tree also stops ffmpeg children on cancellation.
        if os.name == "nt" and owned_windows_job is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=0x08000000,
            )
            await killer.wait()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    def _read_progress(self, job: dict[str, Any], directory: Path) -> None:
        if not self._is_active(job):
            return
        with contextlib.suppress(ValueError, OSError, json.JSONDecodeError):
            progress = self._read_json(directory / "progress.json", limit=16_384)
            if not isinstance(progress, dict):
                return
            percent = float(progress.get("percent", 0))
            if not math.isfinite(percent):
                return
            proposed = {
                "stage": self._sanitize(str(progress.get("stage") or "analyzing")[:80]),
                "percent": max(15, min(95, round(percent))),
                "message": self._sanitize(str(progress.get("message") or "Analyzing media")[:300]),
            }
            if proposed != job["progress"]:
                job["progress"] = proposed
                self._save(job)

    @staticmethod
    def _read_json(path: Path, limit: int = _MAX_JSON_BYTES) -> Any:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): item
                if key == "canonical_url" and _is_canonical_url(item)
                else self._sanitize(item)
                for key, item in value.items()
                if not _SECRET_FIELDS.search(str(key))
                and str(key) not in {"source_path", "output_dir", "media_url", "video_url"}
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[redacted]")
            return _URL.sub("[external URL removed]", value)
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def _prefix_artifact_keys(self, value: Any, prefix: str) -> Any:
        if isinstance(value, dict):
            return {
                key: [
                    f"{prefix}/{item}"
                    if isinstance(item, str) and self._artifact_path(self.root, item) is not None
                    else item
                    for item in items
                ]
                if key == "evidence_frame_keys" and isinstance(items, list)
                else self._prefix_artifact_item(key, items, prefix)
                for key, items in value.items()
            }
        if isinstance(value, list):
            return [self._prefix_artifact_keys(item, prefix) for item in value]
        return value

    def _prefix_artifact_item(self, key: str, item: Any, prefix: str) -> Any:
        return (
            f"{prefix}/{item}"
            if isinstance(item, str)
            and key in {"key", "representative_frame_key", "audio_key", "preview_key"}
            and self._artifact_path(self.root, item) is not None
            else self._prefix_artifact_keys(item, prefix)
        )

    def _artifacts(self, directory: Path, analysis: Any) -> list[dict[str, str]]:
        paths: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"key", "representative_frame_key", "audio_key", "preview_key"}:
                        path = (
                            self._artifact_path(directory, item) if isinstance(item, str) else None
                        )
                        if path is not None and path.is_file():
                            paths.add(item)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(analysis)
        return [
            {"filename": name, "media_type": _ARTIFACT_TYPES[Path(name).suffix.lower()]}
            for name in sorted(paths)
        ]

    @staticmethod
    def _artifact_path(directory: Path, filename: str) -> Path | None:
        relative = PurePosixPath(filename)
        if (
            not filename
            or "\\" in filename
            or ":" in filename
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.suffix.lower() not in _ARTIFACT_TYPES
        ):
            return None
        candidate = directory.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(directory.resolve()):
            return None
        return candidate

    def _owned(self, workspace_id: str, job_id: str) -> dict[str, Any]:
        self._ensure_running()
        self._validate_workspace(workspace_id)
        try:
            if str(UUID(job_id)) != job_id:
                raise ValueError
        except ValueError:
            raise BenchmarkJobError("BENCHMARK_JOB_NOT_FOUND", "Job not found", 404) from None
        job = self._find(job_id)
        if not job or job["workspace_id"] != workspace_id:
            raise BenchmarkJobError("BENCHMARK_JOB_NOT_FOUND", "Job not found", 404)
        return job

    @staticmethod
    def _validate_workspace(workspace_id: str) -> None:
        if not isinstance(workspace_id, str) or not workspace_id.strip() or len(workspace_id) > 200:
            raise BenchmarkJobError("BENCHMARK_JOB_INPUT_INVALID", "Invalid workspace")

    def _check_capacity(self) -> None:
        count = self._db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?)", tuple(_ACTIVE)
        ).fetchone()[0]
        if count >= self._capacity:
            raise BenchmarkJobError("BENCHMARK_JOB_QUEUE_FULL", "The local queue is full", 429)
        self._check_storage_capacity()

    def _check_storage_capacity(self) -> None:
        size, _attempt_size = self._storage_usage()
        reserve = self._attempt_storage_limit
        if (
            size + reserve + self._persistence_headroom > self._storage_limit
            or shutil.disk_usage(self.root).free < reserve + _FREE_DISK_HEADROOM_BYTES
        ):
            raise BenchmarkJobError(
                "BENCHMARK_JOB_STORAGE_FULL",
                "The local 2 GiB analysis budget or free disk space is exhausted. "
                "Wait for retention cleanup or change the configured storage budget.",
                507,
            )

    def _storage_usage(self, attempt: Path | None = None) -> tuple[int, int]:
        """Count source, derived media, transient files and SQLite without following links."""
        total = current = entries = 0

        def fail_unreadable(error: OSError) -> None:
            raise error

        try:
            for folder, directories, filenames in os.walk(
                self.root, followlinks=False, onerror=fail_unreadable
            ):
                parent = Path(folder)
                entries += len(directories) + len(filenames)
                if entries > 50_000:
                    raise OSError("Local analysis file count exceeded")
                directories[:] = [
                    name for name in directories
                    if not (parent / name).is_symlink()
                    and not getattr(parent / name, "is_junction", lambda: False)()
                ]
                for name in filenames:
                    path = parent / name
                    try:
                        if path.is_symlink():
                            continue
                        size = path.stat().st_size
                    except FileNotFoundError:
                        continue  # An atomic progress update or cleanup just completed.
                    total += size
                    if attempt is not None and path.is_relative_to(attempt):
                        current += size
        except OSError as exc:
            raise BenchmarkJobError(
                "BENCHMARK_JOB_STORAGE_FULL", "Local analysis storage could not be measured", 507
            ) from exc
        return total, current

    def _check_running_storage(self, attempt: Path) -> None:
        total, current = self._storage_usage(attempt)
        if (
            total + self._persistence_headroom > self._storage_limit
            or current > self._attempt_storage_limit
            or shutil.disk_usage(self.root).free < _FREE_DISK_HEADROOM_BYTES
        ):
            raise BenchmarkJobError(
                "BENCHMARK_JOB_STORAGE_FULL",
                "Analysis exceeded its storage budget or free disk headroom. "
                "Temporary files are being removed; free disk space before retrying.",
                507,
            )

    def _cleanup_temporary_source(self, job: dict[str, Any]) -> None:
        directory = self._attempt_directory
        expected = self._directory(job) / f"attempt-{int(job['attempt'])}"
        if (
            directory is None
            or directory != expected
            or directory.is_symlink()
            or not directory.resolve().is_relative_to(self.root)
        ):
            return
        current = self._find(job["job_id"])
        cleanup_failed = False
        # Incomplete output is not published. Remove the whole owned attempt
        # only after both acquisition and the worker tree have stopped writing.
        if current and current["status"] in _RETRYABLE:
            try:
                if directory.exists():
                    shutil.rmtree(directory)
            except OSError:
                cleanup_failed = True
        for filename in ("source.mp4", "source.part", "tmp"):
            path = directory / filename
            if not path.resolve().is_relative_to(directory.resolve()):
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            warning = "Temporary media cleanup failed; local retention cleanup will retry."
            if current and current.get("report"):
                current["report"]["limitations"].append(warning)
                current["report"]["status"] = "partial"
                current["status"] = "partial"
                current["progress"]["stage"] = "partial"
                self._save(current)
            elif current and current.get("error"):
                current["error"]["message"] += " " + warning
                self._save(current)

    def _ensure_running(self) -> None:
        if (
            self._connection is None
            or self._closing
            or (self._runner is not None and self._runner.done())
        ):
            raise BenchmarkJobError(
                "BENCHMARK_JOBS_UNAVAILABLE", "Local analysis is not running", 503
            )

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise BenchmarkJobError(
                "BENCHMARK_JOBS_UNAVAILABLE", "Local analysis is not running", 503
            )
        return self._connection

    def _find(self, job_id: str) -> dict[str, Any] | None:
        result = self._query("SELECT payload FROM jobs WHERE job_id=?", job_id)
        return result[0] if result else None

    def _query(self, sql: str, *parameters: Any) -> list[dict[str, Any]]:
        return [json.loads(row["payload"]) for row in self._db.execute(sql, parameters).fetchall()]

    def _save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _now()
        with self._db:
            self._db.execute(
                "UPDATE jobs SET status=?,updated_at=?,payload=? WHERE job_id=?",
                (
                    job["status"],
                    job["updated_at"],
                    json.dumps(job, ensure_ascii=False),
                    job["job_id"],
                ),
            )
            self.history.save_video(job)

    @property
    def history(self) -> BenchmarkHistoryStore:
        return BenchmarkHistoryStore(self._db)

    def save_account_history(self, workspace_id: str, report: Any) -> str:
        self._ensure_running()
        self._validate_workspace(workspace_id)
        self._check_storage_capacity()
        with self._db:
            return self.history.save_account(workspace_id, report)

    def _progress(self, job: dict[str, Any], status: str, percent: int, message: str) -> None:
        job["status"] = status
        job["progress"] = {"stage": status, "percent": percent, "message": message}
        self._save(job)

    def _is_active(self, job: dict[str, Any]) -> bool:
        stored = self._find(job["job_id"])
        return stored is not None and stored["status"] in _ACTIVE

    def _finish_if_active(self, job: dict[str, Any], code: str, message: str) -> None:
        if self._is_active(job):
            self._finish_error(job, "failed", code, message)

    def _finish_error(self, job: dict[str, Any], status: str, code: str, message: str) -> None:
        job.update(
            status=status,
            error={"code": code, "message": message, "retryable": job["attempt"] < 3},
        )
        job["progress"] = {
            "stage": status,
            "percent": job["progress"]["percent"],
            "message": message,
        }
        self._save(job)

    def _directory(self, job: dict[str, Any]) -> Path:
        job_id = str(UUID(job["job_id"]))
        path = self.root / job_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise BenchmarkJobError("BENCHMARK_JOB_STORAGE_INVALID", "Invalid job storage", 503)
        return path

    @staticmethod
    def _public(job: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in job.items() if key != "profile_url"}

    def _cleanup_expired(self) -> None:
        self._last_cleanup = time.monotonic()
        cutoff = (datetime.now(UTC) - timedelta(days=self._retention_days)).isoformat()
        for job in self._query(
            "SELECT payload FROM jobs WHERE updated_at<? AND status NOT IN (?,?,?)",
            cutoff,
            *_ACTIVE,
        ):
            directory = self._directory(job)
            if directory.is_dir():
                try:
                    shutil.rmtree(directory)
                except OSError:
                    # A Windows FileResponse may still hold an artifact open.
                    # Retry retention later without killing the execution lane.
                    continue
            with self._db:
                self._db.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))

    def _release(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None
        if self._lock_file:
            if os.name == "nt":
                import msvcrt

                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            self._lock_file.close()
            self._lock_file = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_canonical_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.xiaohongshu.com"
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"/(?:explore|user/profile)/[0-9a-f]{24}", parsed.path) is not None
    )
