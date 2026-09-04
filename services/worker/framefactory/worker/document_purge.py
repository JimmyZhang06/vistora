"""Crash-recoverable PostgreSQL/S3 erasure for document-video content.

The control plane freezes a source before this worker sees it.  This service
deletes every object version first and only then commits metadata tombstones;
partial attempts are safe to retry because deleting an absent S3 object is a
successful no-op and the PostgreSQL lease fences stale workers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PurgeObject:
    bucket: str
    key: str
    kind: str


@dataclass(frozen=True, slots=True)
class DocumentPurgeLease:
    request_id: str
    workspace_id: str
    source_id: str
    lease_token: str
    attempt_count: int
    source: PurgeObject
    derived: tuple[PurgeObject, ...]


class PostgresDocumentPurgeRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @classmethod
    def connect(
        cls, database_url: str, *, timeout_seconds: float = 5.0
    ) -> PostgresDocumentPurgeRepository:
        return cls(
            psycopg.connect(
                database_url,
                connect_timeout=max(1, round(timeout_seconds)),
                row_factory=dict_row,
                autocommit=True,
            )
        )

    def close(self) -> None:
        self._connection.close()

    def healthcheck(self) -> None:
        row = self._connection.execute(
            """SELECT to_regclass('public.document_purge_requests') IS NOT NULL
                      AND EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='document_sources'
                          AND column_name='purged_at') AS healthy"""
        ).fetchone()
        if row is None or not bool(row["healthy"]):
            raise RuntimeError(
                "document purge schema is unavailable; apply "
                "db/migrations/0027_document_retention_and_purge.sql"
            )

    def claim(
        self, *, worker_id: str, lease_seconds: float
    ) -> DocumentPurgeLease | None:
        token = uuid4()
        with self._connection.transaction():
            self._connection.execute(
                """WITH recovered AS (
                     UPDATE document_purge_requests p SET
                       status=CASE WHEN attempt_count >= max_attempts THEN 'failed'
                                   ELSE 'retrying' END,
                       completed_at=CASE WHEN attempt_count >= max_attempts THEN now()
                                         ELSE NULL END,
                       next_attempt_at=now(), lease_owner=NULL, lease_token=NULL,
                       lease_expires_at=NULL,
                       last_error=jsonb_build_object(
                         'code','PURGE_LEASE_EXPIRED',
                         'message','The previous purge worker lease expired'),
                       updated_at=now()
                     WHERE status='purging' AND lease_expires_at <= now()
                     RETURNING workspace_id,source_id
                   )
                   UPDATE document_sources s SET status='deletion_pending',
                     revision=revision+1,updated_at=now()
                   FROM recovered r
                   WHERE s.workspace_id=r.workspace_id AND s.id=r.source_id
                     AND s.status='purging'"""
            )
            row = self._connection.execute(
                """SELECT p.id,p.workspace_id,p.source_id,p.attempt_count,
                          s.bucket AS source_bucket,s.object_key AS source_key
                   FROM document_purge_requests p
                   JOIN document_sources s
                     ON s.workspace_id=p.workspace_id AND s.id=p.source_id
                   WHERE p.status IN ('queued','retrying')
                     AND p.next_attempt_at <= now()
                     AND p.attempt_count < p.max_attempts
                     AND s.status='deletion_pending'
                     AND s.upload_expires_at <= now()
                     AND NOT s.legal_hold
                     AND (s.retention_until IS NULL OR s.retention_until <= now())
                     AND NOT EXISTS (
                       SELECT 1 FROM runs r
                       WHERE r.workspace_id=p.workspace_id
                         AND r.input_snapshot->>'document_source_id'=p.source_id::text
                         AND r.status NOT IN ('succeeded','failed','cancelled'))
                   ORDER BY p.next_attempt_at,p.created_at,p.id
                   FOR UPDATE OF p,s SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            request_id = str(row["id"])
            workspace_id = str(row["workspace_id"])
            source_id = str(row["source_id"])
            saved = self._connection.execute(
                """UPDATE document_purge_requests SET status='purging',
                     attempt_count=attempt_count+1, lease_owner=%s, lease_token=%s,
                     lease_expires_at=now()+(%s * interval '1 second'),
                     started_at=COALESCE(started_at,now()), updated_at=now()
                   WHERE id=%s AND status IN ('queued','retrying')
                   RETURNING attempt_count""",
                (worker_id, token, lease_seconds, row["id"]),
            ).fetchone()
            if saved is None:  # pragma: no cover - row lock contract
                return None
            self._connection.execute(
                """UPDATE document_sources SET status='purging',
                     revision=revision+1,updated_at=now()
                   WHERE workspace_id=%s AND id=%s AND status='deletion_pending'""",
                (row["workspace_id"], row["source_id"]),
            )
            artifacts = self._connection.execute(
                """SELECT a.bucket,a.object_key
                   FROM artifacts a JOIN runs r
                     ON r.workspace_id=a.workspace_id AND r.id=a.run_id
                   WHERE a.workspace_id=%s
                     AND r.input_snapshot->>'document_source_id'=%s
                     AND a.status <> 'deleted' AND a.deleted_at IS NULL
                   ORDER BY a.created_at,a.id""",
                (row["workspace_id"], source_id),
            ).fetchall()
        return DocumentPurgeLease(
            request_id=request_id,
            workspace_id=workspace_id,
            source_id=source_id,
            lease_token=str(token),
            attempt_count=int(saved["attempt_count"]),
            source=PurgeObject(
                str(row["source_bucket"]), str(row["source_key"]), "source"
            ),
            derived=tuple(
                PurgeObject(str(item["bucket"]), str(item["object_key"]), "artifact")
                for item in artifacts
            ),
        )

    def complete(self, lease: DocumentPurgeLease) -> None:
        with self._connection.transaction():
            request = self._connection.execute(
                """SELECT p.status,p.lease_token,s.legal_hold,s.status AS source_status
                   FROM document_purge_requests p JOIN document_sources s
                     ON s.workspace_id=p.workspace_id AND s.id=p.source_id
                   WHERE p.id=%s AND p.workspace_id=%s FOR UPDATE OF p,s""",
                (lease.request_id, lease.workspace_id),
            ).fetchone()
            if (
                request is None
                or request["status"] != "purging"
                or str(request["lease_token"]) != lease.lease_token
            ):
                raise RuntimeError("document purge lease was lost before completion")
            if bool(request["legal_hold"]) or request["source_status"] != "purging":
                raise RuntimeError(
                    "document purge policy changed after the lease was claimed"
                )

            # Lock every related Run before checking the object manifest.  The
            # artifacts foreign key then prevents a late publisher from adding
            # an object between this check and the tombstone commit.
            self._connection.execute(
                """SELECT id FROM runs
                   WHERE workspace_id=%s
                     AND input_snapshot->>'document_source_id'=%s
                   FOR UPDATE""",
                (lease.workspace_id, lease.source_id),
            ).fetchall()
            current_objects = self._connection.execute(
                """SELECT a.bucket,a.object_key
                   FROM artifacts a JOIN runs r
                     ON r.workspace_id=a.workspace_id AND r.id=a.run_id
                   WHERE a.workspace_id=%s
                     AND r.input_snapshot->>'document_source_id'=%s
                     AND a.status <> 'deleted' AND a.deleted_at IS NULL
                   FOR UPDATE OF a""",
                (lease.workspace_id, lease.source_id),
            ).fetchall()
            expected_objects = {(item.bucket, item.key) for item in lease.derived}
            actual_objects = {
                (str(item["bucket"]), str(item["object_key"]))
                for item in current_objects
            }
            if actual_objects != expected_objects:
                raise RuntimeError(
                    "document artifact manifest changed during purge; retrying safely"
                )

            # Preserve Run/review/audit lineage, but replace every live locator
            # and user-supplied filename with a deterministic tombstone.
            self._connection.execute(
                """UPDATE run_steps s SET output_artifacts=(
                     SELECT COALESCE(jsonb_agg(
                       CASE WHEN EXISTS (
                         SELECT 1 FROM artifacts a
                         WHERE a.workspace_id=s.workspace_id AND a.run_id=s.run_id
                           AND a.id::text=element.value->>'id')
                       THEN element.value || jsonb_build_object(
                         'object_key',format(
                           'workspaces/%%s/runs/%%s/artifacts/%%s/purged',
                           s.workspace_id,s.run_id,element.value->>'id'),
                         'filename','purged')
                       ELSE element.value END ORDER BY element.ordinality), '[]'::jsonb)
                     FROM jsonb_array_elements(COALESCE(s.output_artifacts,'[]'::jsonb))
                          WITH ORDINALITY AS element(value,ordinality))
                   WHERE s.workspace_id=%s AND EXISTS (
                     SELECT 1 FROM runs r WHERE r.workspace_id=s.workspace_id
                       AND r.id=s.run_id
                       AND r.input_snapshot->>'document_source_id'=%s)""",
                (lease.workspace_id, lease.source_id),
            )
            self._connection.execute(
                """UPDATE artifacts a SET status='deleted',deleted_at=now(),
                     object_key=format(
                       'workspaces/%%s/runs/%%s/artifacts/%%s/purged',
                       a.workspace_id,a.run_id,a.id),
                     metadata=jsonb_build_object('purged',true)
                   FROM runs r
                   WHERE r.workspace_id=a.workspace_id AND r.id=a.run_id
                     AND a.workspace_id=%s
                     AND r.input_snapshot->>'document_source_id'=%s
                     AND a.status <> 'deleted'""",
                (lease.workspace_id, lease.source_id),
            )
            self._connection.execute(
                """UPDATE runs SET composition_snapshot=jsonb_set(
                     composition_snapshot,'{document_source}',
                     (composition_snapshot->'document_source') || jsonb_build_object(
                       'filename','purged.pdf','bucket','purged',
                       'object_key',concat(
                         'workspaces/',workspace_id,'/deleted/documents/',%s::text)),true)
                   WHERE workspace_id=%s
                     AND input_snapshot->>'document_source_id'=%s""",
                (
                    lease.source_id,
                    lease.workspace_id,
                    lease.source_id,
                ),
            )
            self._connection.execute(
                """UPDATE document_sources SET status='purged',filename='purged.pdf',
                     bucket='purged',
                     object_key=concat('workspaces/',workspace_id,'/deleted/documents/',id),
                     validation=jsonb_build_object('state','purged'),purged_at=now(),
                     revision=revision+1,updated_at=now()
                   WHERE workspace_id=%s AND id=%s AND status='purging'""",
                (lease.workspace_id, lease.source_id),
            )
            self._connection.execute(
                """UPDATE document_purge_requests SET status='succeeded',
                     source_object_deleted_at=now(),derived_objects_deleted_at=now(),
                     completed_at=now(),lease_owner=NULL,lease_token=NULL,
                     lease_expires_at=NULL,last_error=NULL,updated_at=now()
                   WHERE id=%s AND workspace_id=%s AND lease_token=%s""",
                (lease.request_id, lease.workspace_id, lease.lease_token),
            )
            self._connection.execute(
                """INSERT INTO audit_logs (
                     workspace_id,actor_type,actor_id,action,resource_type,
                     resource_id,after_data,metadata)
                   VALUES (%s,'worker',%s,'document.purge.completed',
                     'document_source',%s,
                     jsonb_build_object('purge_request_id',%s::text,'status','purged'),
                     jsonb_build_object('derived_object_count',%s))""",
                (
                    lease.workspace_id,
                    "document-purge-worker",
                    lease.source_id,
                    lease.request_id,
                    len(lease.derived),
                ),
            )

    def retry(self, lease: DocumentPurgeLease, error: dict[str, str]) -> None:
        with self._connection.transaction():
            row = self._connection.execute(
                """UPDATE document_purge_requests SET
                     status=CASE WHEN attempt_count >= max_attempts THEN 'failed'
                                 ELSE 'retrying' END,
                     next_attempt_at=now()+(LEAST(900,POWER(2,attempt_count)) * interval '1 second'),
                     completed_at=CASE WHEN attempt_count >= max_attempts THEN now()
                                       ELSE NULL END,
                     lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                     last_error=%s,updated_at=now()
                   WHERE id=%s AND workspace_id=%s AND status='purging'
                     AND lease_token=%s RETURNING status""",
                (Jsonb(error), lease.request_id, lease.workspace_id, lease.lease_token),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "document purge lease was lost while scheduling a retry"
                )
            self._connection.execute(
                """UPDATE document_sources SET status='deletion_pending',
                     revision=revision+1,updated_at=now()
                   WHERE workspace_id=%s AND id=%s AND status='purging'""",
                (lease.workspace_id, lease.source_id),
            )


class DocumentPurgeService:
    def __init__(
        self,
        *,
        repository: PostgresDocumentPurgeRepository,
        s3_client: Any,
        bucket: str,
        worker_id: str,
        lease_seconds: float,
    ) -> None:
        self.repository = repository
        self.s3_client = s3_client
        self.bucket = bucket
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def close(self) -> None:
        self.repository.close()

    def healthcheck(self) -> None:
        self.repository.healthcheck()

    def process_once(self) -> bool:
        lease = self.repository.claim(
            worker_id=self.worker_id, lease_seconds=self.lease_seconds
        )
        if lease is None:
            return False
        try:
            for item in (lease.source, *lease.derived):
                self._validate_object(lease, item)
                self._delete_all_versions(item)
            self.repository.complete(lease)
        except Exception as exc:
            logger.exception(
                "document purge attempt failed request=%s", lease.request_id
            )
            self.repository.retry(
                lease,
                {
                    "code": "DOCUMENT_PURGE_ATTEMPT_FAILED",
                    "message": str(exc)[:1000] or exc.__class__.__name__,
                },
            )
        return True

    def _validate_object(self, lease: DocumentPurgeLease, item: PurgeObject) -> None:
        workspace_prefix = f"workspaces/{lease.workspace_id}/"
        if item.bucket != self.bucket or not item.key.startswith(workspace_prefix):
            raise ValueError(
                "purge object violates the configured tenant storage boundary"
            )
        if "/../" in f"/{item.key}/" or "\x00" in item.key:
            raise ValueError("purge object key contains an unsafe path segment")

    def _delete_all_versions(self, item: PurgeObject) -> None:
        versioning = self.s3_client.get_bucket_versioning(Bucket=item.bucket).get(
            "Status"
        )
        if versioning in {"Enabled", "Suspended"}:
            key_marker: str | None = None
            version_marker: str | None = None
            while True:
                request: dict[str, Any] = {"Bucket": item.bucket, "Prefix": item.key}
                if key_marker is not None:
                    request["KeyMarker"] = key_marker
                if version_marker is not None:
                    request["VersionIdMarker"] = version_marker
                page = self.s3_client.list_object_versions(**request)
                versions = [
                    {"Key": value["Key"], "VersionId": value["VersionId"]}
                    for group in (
                        page.get("Versions", []),
                        page.get("DeleteMarkers", []),
                    )
                    for value in group
                    if value.get("Key") == item.key
                ]
                if versions:
                    response = (
                        self.s3_client.delete_objects(
                            Bucket=item.bucket,
                            Delete={"Objects": versions, "Quiet": True},
                        )
                        or {}
                    )
                    errors = response.get("Errors", [])
                    if errors:
                        codes = sorted(
                            {str(error.get("Code", "Unknown")) for error in errors}
                        )
                        raise RuntimeError(
                            "S3 rejected one or more version deletions: "
                            + ", ".join(codes)
                        )
                if not page.get("IsTruncated"):
                    break
                key_marker = page.get("NextKeyMarker")
                version_marker = page.get("NextVersionIdMarker")
        else:
            self.s3_client.delete_object(Bucket=item.bucket, Key=item.key)
        try:
            self.s3_client.head_object(Bucket=item.bucket, Key=item.key)
        except Exception as exc:
            response = getattr(exc, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return
            raise
        raise RuntimeError("S3 object remained readable after the purge request")
