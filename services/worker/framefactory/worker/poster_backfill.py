"""Safe, idempotent poster generation for assets imported before poster support."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import boto3
import psycopg
from botocore.config import Config
from psycopg.rows import dict_row

from .adapters.asset_analysis import S3MediaObjectStore, SystemMalwareScanner
from .asset_pipeline import AssetPipelineError, AssetWork
from .config import WorkerSettings

logger = logging.getLogger("framefactory.worker.poster-backfill")


@dataclass(frozen=True, slots=True)
class PosterCandidate:
    workspace_id: str
    asset_id: str
    file_id: str
    media_type: str
    bucket: str
    object_key: str
    original_filename: str
    content_hash: str
    scan_status: str


@dataclass(frozen=True, slots=True)
class PosterBackfillResult:
    selected: int
    completed: int
    failed: int
    malware_rejected: int


class PosterBackfill:
    """Generate one representative JPEG after verifying and scanning each source."""

    def __init__(
        self,
        database_url: str,
        store: S3MediaObjectStore,
        scanner: SystemMalwareScanner,
        *,
        ffmpeg_command: str = "ffmpeg",
    ) -> None:
        self.database_url = database_url
        self.store = store
        self.scanner = scanner
        self.ffmpeg_command = ffmpeg_command

    @classmethod
    def from_settings(cls, settings: WorkerSettings) -> PosterBackfill:
        storage = settings.object_storage
        if storage is None:
            raise ValueError("poster backfill requires FRAMEFACTORY_S3_BUCKET")
        client = boto3.client(
            "s3",
            endpoint_url=storage.endpoint_url,
            region_name=storage.region,
            aws_access_key_id=storage.access_key_id,
            aws_secret_access_key=storage.secret_access_key,
            config=Config(s3={"addressing_style": "path"}),
        )
        return cls(
            settings.database_url,
            S3MediaObjectStore(client),
            SystemMalwareScanner(os.getenv("FRAMEFACTORY_MALWARE_SCAN_COMMAND", "clamscan")),
            ffmpeg_command=os.getenv("FRAMEFACTORY_FFMPEG_COMMAND", "ffmpeg"),
        )

    def plan(self, *, limit: int) -> tuple[PosterCandidate, ...]:
        if limit < 1:
            raise ValueError("poster backfill limit must be positive")
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            rows = connection.execute(
                """SELECT a.workspace_id::text,a.id::text AS asset_id,
                          f.id::text AS file_id,f.media_type,f.bucket,f.object_key,
                          f.original_filename,f.content_hash,f.scan_status
                     FROM assets a
                     JOIN LATERAL (
                       SELECT * FROM asset_files candidate
                        WHERE candidate.workspace_id=a.workspace_id
                          AND candidate.asset_id=a.id
                          AND candidate.deleted_at IS NULL
                        ORDER BY candidate.created_at DESC LIMIT 1
                     ) f ON true
                    WHERE a.kind IN ('image','video')
                      AND NOT (a.metadata ? 'poster')
                    ORDER BY CASE f.scan_status WHEN 'clean' THEN 0 ELSE 1 END,
                             a.created_at,a.id
                    LIMIT %s""",
                (limit,),
            ).fetchall()
        return tuple(PosterCandidate(**dict(row)) for row in rows)

    def run(self, *, limit: int, workers: int = 2) -> PosterBackfillResult:
        if not 1 <= workers <= 4:
            raise ValueError("poster backfill workers must be between 1 and 4")
        candidates = self.plan(limit=limit)
        completed = 0
        failed = 0
        malware_rejected = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asset-poster") as pool:
            futures = {pool.submit(self._process, candidate): candidate for candidate in candidates}
            for index, future in enumerate(as_completed(futures), start=1):
                candidate = futures[future]
                try:
                    outcome = future.result()
                except Exception:
                    failed += 1
                    logger.exception("poster backfill failed asset_id=%s", candidate.asset_id)
                else:
                    if outcome == "completed":
                        completed += 1
                    elif outcome == "malware_rejected":
                        malware_rejected += 1
                    else:
                        failed += 1
                if index % 25 == 0 or index == len(candidates):
                    logger.info(
                        "poster backfill progress processed=%s total=%s completed=%s failed=%s rejected=%s",
                        index,
                        len(candidates),
                        completed,
                        failed,
                        malware_rejected,
                    )
        return PosterBackfillResult(len(candidates), completed, failed, malware_rejected)

    def _process(self, candidate: PosterCandidate) -> str:
        suffix = Path(candidate.original_filename).suffix.lower()[:12] or ".bin"
        with tempfile.TemporaryDirectory(prefix="framefactory-poster-") as directory:
            source = Path(directory) / f"source{suffix}"
            poster = Path(directory) / "poster.jpg"
            work = AssetWork(
                item_id=f"poster:{candidate.asset_id}",
                batch_id="poster-backfill",
                workspace_id=candidate.workspace_id,
                asset_id=candidate.asset_id,
                revision=1,
                attempts=1,
                max_attempts=1,
                completed_stages=(),
                checkpoints={},
                copyright_status="unknown",
                content_hash=candidate.content_hash,
                media_type=candidate.media_type,
                bucket=candidate.bucket,
                object_key=candidate.object_key,
                original_filename=candidate.original_filename,
            )
            self.store.download(work, source)
            if _sha256(source) != candidate.content_hash:
                raise AssetPipelineError(
                    "content_hash_mismatch",
                    "source bytes differ from the durable asset file record",
                    retryable=False,
                )
            if candidate.scan_status != "clean":
                try:
                    self.scanner.scan(source)
                except AssetPipelineError as exc:
                    if exc.code == "malware_detected":
                        self._mark_rejected(candidate)
                        return "malware_rejected"
                    raise
            _extract_poster(self.ffmpeg_command, source, poster, candidate.media_type)
            object_key = self.store.publish_frame(work, poster, 0)
            descriptor = {
                "object_key": object_key,
                "content_hash": _sha256(poster),
                "media_type": "image/jpeg",
                "byte_size": poster.stat().st_size,
                "generator": "poster-backfill-v1",
            }
            self._publish(candidate, descriptor)
        return "completed"

    def _publish(self, candidate: PosterCandidate, descriptor: dict[str, Any]) -> None:
        with psycopg.connect(self.database_url) as connection, connection.transaction():
            connection.execute(
                """UPDATE asset_files SET scan_status='clean'
                    WHERE workspace_id=%s AND id=%s AND asset_id=%s
                      AND content_hash=%s AND scan_status <> 'rejected'""",
                (
                    candidate.workspace_id,
                    candidate.file_id,
                    candidate.asset_id,
                    candidate.content_hash,
                ),
            )
            connection.execute(
                """UPDATE assets
                      SET metadata=jsonb_set(metadata,'{poster}',%s::jsonb,true)
                    WHERE workspace_id=%s AND id=%s
                      AND NOT (metadata ? 'poster')""",
                (json.dumps(descriptor), candidate.workspace_id, candidate.asset_id),
            )

    def _mark_rejected(self, candidate: PosterCandidate) -> None:
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """UPDATE asset_files SET scan_status='rejected'
                    WHERE workspace_id=%s AND id=%s AND asset_id=%s
                      AND content_hash=%s""",
                (
                    candidate.workspace_id,
                    candidate.file_id,
                    candidate.asset_id,
                    candidate.content_hash,
                ),
            )


def _extract_poster(command: str, source: Path, destination: Path, media_type: str) -> None:
    executable = shutil.which(command)
    if executable is None:
        raise AssetPipelineError("ffmpeg_unavailable", "ffmpeg is unavailable", retryable=True)
    filters = "scale='min(1280,iw)':-2"
    if media_type.startswith("video/"):
        filters = f"thumbnail=100,{filters}"
    result = subprocess.run(
        (
            executable,
            "-v",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            filters,
            "-q:v",
            "3",
            "-map_metadata",
            "-1",
            "-y",
            str(destination),
        ),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise AssetPipelineError(
            "poster_generation_failed",
            "ffmpeg could not create an asset poster",
            retryable=False,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def result_json(result: PosterBackfillResult) -> str:
    return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
