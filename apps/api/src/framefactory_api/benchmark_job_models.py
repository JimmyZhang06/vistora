from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .benchmark_media_reports import BenchmarkDeepNoteReport


class BenchmarkJobModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkJobProgress(BenchmarkJobModel):
    stage: str
    percent: float = Field(ge=0, le=100)
    message: str


class BenchmarkJobFailure(BenchmarkJobModel):
    code: str
    message: str
    retryable: bool


class BenchmarkJobArtifact(BenchmarkJobModel):
    filename: str
    media_type: str


class BenchmarkJobResponse(BenchmarkJobModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    job_id: UUID
    workspace_id: str
    profile_user_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    note_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    title: str
    status: Literal[
        "pending",
        "collecting",
        "analyzing",
        "ready",
        "partial",
        "failed",
        "cancelled",
        "interrupted",
    ]
    progress: BenchmarkJobProgress
    created_at: datetime
    updated_at: datetime
    attempt: int = Field(ge=1)
    source_evidence: dict[str, Any] | None
    analysis: dict[str, Any] | None
    report: BenchmarkDeepNoteReport | None
    error: BenchmarkJobFailure | None
    artifacts: list[BenchmarkJobArtifact]
