"""Local report snapshots, independent of the short-lived media/job retention window."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from .benchmark_job_models import BenchmarkJobResponse
from .benchmark_reports import BenchmarkAccountReport
from .errors import ApiError

HistoryKind = Literal["account", "video"]


class BenchmarkHistorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: UUID
    kind: HistoryKind
    title: str
    profile_user_id: str
    note_id: str | None = None
    status: Literal["ready", "partial"]
    saved_at: datetime
    analyzed_at: datetime


class BenchmarkHistoryPage(BaseModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    items: list[BenchmarkHistorySummary]
    next_cursor: UUID | None = None


class BenchmarkHistoryDetail(BenchmarkHistorySummary):
    schema_version: Literal["1.0.0"] = "1.0.0"
    account_report: BenchmarkAccountReport | None = None
    video_job: BenchmarkJobResponse | None = None
    media_available: bool = False


class BenchmarkHistoryStore:
    """Shares the job connection; callers own transactions, including atomic completion."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection

    def migrate(self) -> None:
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS benchmark_history ("
            "record_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "source_key TEXT NOT NULL, title TEXT NOT NULL, profile_user_id TEXT NOT NULL, "
            "note_id TEXT, status TEXT NOT NULL, saved_at TEXT NOT NULL, "
            "analyzed_at TEXT NOT NULL, payload TEXT NOT NULL, "
            "UNIQUE(workspace_id,kind,source_key));"
            "CREATE INDEX IF NOT EXISTS history_workspace_date ON benchmark_history "
            "(workspace_id,saved_at DESC,record_id DESC);"
            "PRAGMA optimize;"
        )

    def save_account(self, workspace_id: str, report: BenchmarkAccountReport) -> str:
        payload = report.model_dump(mode="json", exclude={"history_record_id", "history_warning"})
        # Reopening the same cached capture is not a new analysis version.
        payload["snapshot"]["acquisition"]["from_cache"] = False
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self._insert(
            workspace_id, "account", key, report.snapshot.profile.nickname,
            report.snapshot.profile.user_id, None, "ready",
            report.generated_at.isoformat(), payload,
        )

    def save_video(self, job: dict[str, Any]) -> str | None:
        if job["status"] not in {"ready", "partial"} or not job.get("report"):
            return None
        # Public model rejects unknown fields and excludes the transient acquisition URL.
        payload = BenchmarkJobResponse.model_validate(
            {key: value for key, value in job.items() if key != "profile_url"}
        ).model_dump(mode="json")
        return self._insert(
            job["workspace_id"], "video", f"{job['job_id']}:{job['attempt']}",
            job.get("title") or job["note_id"], job["profile_user_id"], job["note_id"],
            job["status"], job["updated_at"], payload,
        )

    def _insert(
        self, workspace_id: str, kind: str, source_key: str, title: str,
        profile_id: str, note_id: str | None, status: str, analyzed_at: str,
        payload: dict[str, Any],
    ) -> str:
        existing = self.db.execute(
            "SELECT record_id FROM benchmark_history WHERE workspace_id=? AND kind=? "
            "AND source_key=?", (workspace_id, kind, source_key),
        ).fetchone()
        if existing and kind != "video":
            return str(existing[0])
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode()) > 32 * 1024**2:
            raise ApiError(507, "BENCHMARK_HISTORY_TOO_LARGE", "Report exceeds archive size limit")
        if existing:
            # Completion can be downgraded after temporary-media cleanup. Keep
            # the same attempt's archive in the caller's completion transaction
            # so history never hides a later partial status or recovery warning.
            self.db.execute(
                "UPDATE benchmark_history SET title=?,status=?,analyzed_at=?,payload=? "
                "WHERE workspace_id=? AND record_id=?",
                (title, status, analyzed_at, encoded, workspace_id, existing[0]),
            )
            return str(existing[0])
        record_id = str(uuid4())
        self.db.execute(
            "INSERT INTO benchmark_history VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, workspace_id, kind, source_key, title, profile_id, note_id,
             status, datetime.now(UTC).isoformat(), analyzed_at, encoded),
        )
        return record_id

    def list(
        self, workspace_id: str, *, kind: HistoryKind | None = None,
        query: str = "", cursor: str | None = None, limit: int = 20,
    ) -> dict[str, Any]:
        clauses = ["workspace_id=?"]
        parameters: list[Any] = [workspace_id]
        if kind:
            clauses.append("kind=?")
            parameters.append(kind)
        if query:
            clauses.append("instr(lower(title||' '||profile_user_id||' '||coalesce(note_id,'')), "
                           "lower(?))>0")
            parameters.append(query)
        if cursor:
            anchor = self.db.execute(
                "SELECT saved_at FROM benchmark_history WHERE workspace_id=? AND record_id=?",
                (workspace_id, cursor),
            ).fetchone()
            if not anchor:
                raise ApiError(
                    400, "BENCHMARK_HISTORY_CURSOR_INVALID", "Reload history to continue",
                )
            clauses.append("(saved_at,record_id)<(?,?)")
            parameters.extend([anchor[0], cursor])
        rows = self.db.execute(
            "SELECT record_id,kind,title,profile_user_id,note_id,status,saved_at,analyzed_at "
            "FROM benchmark_history WHERE " + " AND ".join(clauses)
            + " ORDER BY saved_at DESC,record_id DESC LIMIT ?",
            (*parameters, min(50, max(1, limit)) + 1),
        ).fetchall()
        page = rows[:limit]
        return {"items": [dict(row) for row in page],
                "next_cursor": page[-1]["record_id"] if len(rows) > limit else None}

    def get(self, workspace_id: str, record_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM benchmark_history WHERE workspace_id=? AND record_id=?",
            (workspace_id, record_id),
        ).fetchone()
        if not row:
            raise ApiError(404, "BENCHMARK_HISTORY_NOT_FOUND", "Saved report was not found")
        summary = {key: row[key] for key in BenchmarkHistorySummary.model_fields}
        payload = json.loads(row["payload"])
        if row["kind"] == "account":
            payload["history_record_id"] = record_id
            return {**summary, "account_report": payload}
        # Only current, still-retained artifact manifests may produce playable links.
        current = self.db.execute(
            "SELECT payload FROM jobs WHERE workspace_id=? AND job_id=?",
            (workspace_id, payload["job_id"]),
        ).fetchone()
        current_job = json.loads(current[0]) if current else None
        manifest = current_job.get("artifacts", []) if current_job else []
        payload["artifacts"] = [item for item in payload["artifacts"] if item in manifest]
        return {**summary, "video_job": payload, "media_available": bool(payload["artifacts"])}
