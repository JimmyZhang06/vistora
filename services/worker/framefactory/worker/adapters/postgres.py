"""PostgreSQL CAS adapter for the durable runtime snapshots.

The API owns ``runs`` creation.  The worker owns runtime transitions and creates
``run_steps`` from the immutable pipeline graph.  Migration 0001 adds only the
metadata the runtime state machine needs; existing API columns remain the public
read model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from framefactory.ports import RevisionConflictError
from framefactory.runtime import (
    ExecutionState,
    Lease,
    RetryPolicy,
    ReviewDecision,
    ReviewRecord,
    RunRecord,
    RunStep,
    StepError,
    StepRecord,
)
from framefactory.skills.canonical import thaw_json
from framefactory.steps import ArtifactRef


@dataclass(frozen=True, slots=True)
class PendingRunGraph:
    workspace_id: str
    run_id: str
    input_snapshot: object
    nodes: tuple[RunStep, ...]


class PostgresRunStore:
    """Synchronous port implementation backed by psycopg's transaction/CAS API."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @classmethod
    def connect(cls, database_url: str, *, timeout_seconds: float = 5.0) -> PostgresRunStore:
        connection = psycopg.connect(
            database_url,
            connect_timeout=max(1, round(timeout_seconds)),
            row_factory=dict_row,
            autocommit=True,
        )
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def healthcheck(self) -> None:
        row = self._connection.execute(
            """SELECT count(*) = 14 AS healthy
                 FROM information_schema.columns
                WHERE (table_schema, table_name, column_name) IN (
                  ('public','runs','worker_revision'),
                  ('public','runs','worker_error'),
                  ('public','run_steps','worker_step_id'),
                  ('public','run_steps','dependencies'),
                  ('public','run_steps','retry_policy'),
                  ('public','run_steps','output_artifacts'),
                  ('public','run_steps','review_required'),
                  ('public','run_steps','static_review_required'),
                  ('public','run_steps','review'),
                  ('public','run_steps','cancellation_requested_at'),
                  ('public','run_steps','worker_revision')
                  ,('public','run_events','deduplication_key')
                  ,('public','run_events','correlation_id')
                  ,('public','run_events','causation_id')
                )"""
        ).fetchone()
        if not row or not bool(row["healthy"]):
            raise RuntimeError(
                "PostgreSQL worker schema is unavailable; apply "
                "services/worker/migrations/0001_runtime_state.sql and the "
                "current db/migrations set"
            )

    def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            """SELECT workspace_id, id, input_snapshot, status, cancel_requested_at,
                      started_at, completed_at, created_at, updated_at, worker_revision
                 FROM runs WHERE workspace_id = %s AND id = %s""",
            (workspace_id, run_id),
        ).fetchone()
        return None if row is None else self._run(row)

    def save_run(self, run: RunRecord, expected_revision: int | None) -> RunRecord:
        if expected_revision is None:
            # Run creation requires API-owned composition and identity fields.
            if self.get_run(run.workspace_id, run.run_id) is None:
                raise RuntimeError("worker cannot create a run; create it through the control API")
            raise RevisionConflictError(f"run already exists: {run.workspace_id}/{run.run_id}")
        with self._transaction():
            current = self._connection.execute(
                """SELECT status::text AS status FROM runs
                WHERE workspace_id=%s AND id=%s AND worker_revision=%s FOR UPDATE""",
                (run.workspace_id, run.run_id, expected_revision),
            ).fetchone()
            if current is None:
                raise RevisionConflictError(
                    f"stale run revision: {run.workspace_id}/{run.run_id}/{expected_revision}"
                )
            row = self._connection.execute(
                """UPDATE runs
                  SET status = %s,
                      cancel_requested_at = COALESCE(runs.cancel_requested_at, %s),
                      started_at = %s,
                      completed_at = %s, updated_at = %s,
                      worker_revision = worker_revision + 1
                WHERE workspace_id = %s AND id = %s AND worker_revision = %s
            RETURNING workspace_id, id, input_snapshot, status, cancel_requested_at,
                      started_at, completed_at, created_at, updated_at, worker_revision""",
                (
                self._database_run_status(run.status),
                run.cancellation_requested_at,
                run.started_at,
                run.completed_at,
                run.updated_at,
                run.workspace_id,
                run.run_id,
                expected_revision,
                ),
            ).fetchone()
            if row is not None and current["status"] != row["status"]:
                status = str(row["status"])
                event_type = (
                    "run.started"
                    if status == "running" and current["status"] == "queued"
                    else "run.retrying"
                    if status == "running"
                    else f"run.{status}"
                )
                self.append_event(
                    run.workspace_id,
                    run.run_id,
                    event_type=event_type,
                    deduplication_key=f"worker-run-status:{row['worker_revision']}:{status}",
                    payload={"previous_status": current["status"], "status": status},
                    occurred_at=run.updated_at,
                )
        if row is None:
            raise RevisionConflictError(
                f"stale run revision: {run.workspace_id}/{run.run_id}/{expected_revision}"
            )
        return self._run(row)

    def get_step(self, workspace_id: str, step_id: str) -> StepRecord | None:
        row = self._connection.execute(
            f"{self._STEP_SELECT} WHERE workspace_id = %s AND worker_step_id = %s",
            (workspace_id, step_id),
        ).fetchone()
        return None if row is None else self._step(row)

    def save_step(self, step: StepRecord, expected_revision: int | None) -> StepRecord:
        values = self._step_values(step)
        if expected_revision is None:
            with self._transaction():
                row = self._connection.execute(
                    """INSERT INTO run_steps (
                       id, workspace_id, run_id, worker_step_id, step_key, step_type,
                       status, queue_name, required_capabilities, input_snapshot,
                       output_summary, error, priority, available_at, attempt_count,
                       max_attempts, lease_owner, lease_token, lease_expires_at,
                       heartbeat_at, started_at, completed_at, created_at, updated_at,
                       dependencies, retry_policy, output_artifacts, review_required,
                       static_review_required, review, cancellation_requested_at,
                       worker_revision
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, 1
                   ) ON CONFLICT (workspace_id, worker_step_id) DO NOTHING
                   RETURNING worker_step_id""",
                    (self._database_step_id(step.step_id), *values),
                ).fetchone()
                if row is not None:
                    self._append_step_event(step, revision=1)
            if row is None:
                raise RevisionConflictError(
                    f"step already exists: {step.workspace_id}/{step.step_id}"
                )
        else:
            with self._transaction():
                row = self._connection.execute(
                    """UPDATE run_steps SET
                       run_id=%s, step_key=%s, step_type=%s, status=%s, queue_name=%s,
                       required_capabilities=%s, input_snapshot=%s, output_summary=%s,
                       error=%s, priority=%s, available_at=%s, attempt_count=%s,
                       max_attempts=%s, lease_owner=%s, lease_token=%s,
                       lease_expires_at=%s, heartbeat_at=%s, started_at=%s,
                       completed_at=%s, created_at=%s, updated_at=%s, dependencies=%s,
                       retry_policy=%s, output_artifacts=%s, review_required=%s,
                       review=%s, cancellation_requested_at=%s,
                       worker_revision=worker_revision+1
                 WHERE workspace_id=%s AND worker_step_id=%s AND worker_revision=%s
                     RETURNING worker_step_id""",
                    (
                        values[1],
                        *values[3:27],
                        *values[28:],
                        step.workspace_id,
                        step.step_id,
                        expected_revision,
                    ),
                ).fetchone()
                if row is None:
                    raise RevisionConflictError(
                        f"stale step revision: {step.workspace_id}/{step.step_id}/{expected_revision}"
                    )
                self._write_review_action(step)
                self._append_step_event(step, revision=expected_revision + 1)
        saved = self.get_step(step.workspace_id, step.step_id)
        if saved is None:  # pragma: no cover - database contract violation
            raise RuntimeError("PostgreSQL did not return the saved worker step")
        return saved

    def list_steps(self, workspace_id: str, run_id: str) -> Sequence[StepRecord]:
        rows = self._connection.execute(
            f"{self._STEP_SELECT} WHERE workspace_id = %s AND run_id = %s ORDER BY created_at, id",
            (workspace_id, run_id),
        ).fetchall()
        return tuple(self._step(row) for row in rows)

    def list_recoverable(self, now: datetime) -> Sequence[StepRecord]:
        rows = self._connection.execute(
            f"""{self._STEP_SELECT}
                 WHERE (status IN ('queued', 'retrying') AND available_at <= %s)
                    OR (status = 'running' AND lease_expires_at <= %s)
                 ORDER BY available_at, created_at""",
            (now, now),
        ).fetchall()
        return tuple(self._step(row) for row in rows)

    def load_pending_graphs(self, *, limit: int = 25) -> tuple[PendingRunGraph, ...]:
        """Read API-created runs that have not yet had their graph materialized."""

        rows = self._connection.execute(
            """SELECT r.workspace_id, r.id, r.input_snapshot, r.composition_snapshot,
                      pv.graph, sv.id AS skill_version_id, sv.version AS skill_version,
                      sv.input_schema, sv.research_policy, sv.writing_policy,
                      sv.visual_policy, sv.asset_policy, sv.qc_policy,
                      sv.output_contract, sv.capability_requirements,
                      (SELECT count(*) FROM run_steps rs
                        WHERE rs.workspace_id = r.workspace_id
                          AND rs.run_id = r.id
                          AND rs.worker_step_id IS NOT NULL) AS materialized_steps
                 FROM runs r
                 JOIN pipeline_versions pv
                   ON pv.id = r.pipeline_version_id
                 JOIN pipelines p
                   ON p.workspace_id = pv.workspace_id AND p.id = pv.pipeline_id
                 JOIN workspaces pw ON pw.id = pv.workspace_id
                 JOIN skill_versions sv
                   ON sv.id = r.skill_version_id
                  AND (sv.workspace_id = r.workspace_id OR sv.ownership_type = 'system')
                  AND sv.state = 'published'
                WHERE r.status = 'queued'
                  AND (
                    pv.workspace_id = r.workspace_id
                    OR (
                      pw.kind = 'system'
                      AND p.visibility = 'public_readonly'
                      AND pv.state = 'published'
                    )
                  )
                ORDER BY materialized_steps ASC, r.priority DESC, r.created_at
                LIMIT %s""",
            (limit,),
        ).fetchall()
        pending: list[PendingRunGraph] = []
        for row in rows:
            try:
                graph = self._json(row["graph"]) or {}
                nodes = graph if isinstance(graph, list) else graph.get("nodes", [])
                if not isinstance(nodes, list) or not nodes:
                    raise ValueError(f"pipeline graph for run {row['id']} has no nodes")
                if int(row.get("materialized_steps", 0)) >= len(nodes):
                    continue
                run_input = self._json(row["input_snapshot"])
                if not isinstance(run_input, Mapping):
                    raise TypeError(f"run {row['id']} input snapshot is not an object")
                snapshot = {
                    **run_input,
                    "_framefactory": {
                        "composition_snapshot": self._json(row.get("composition_snapshot")) or {},
                        "skill_version": {
                            "id": str(row.get("skill_version_id", "")),
                            "version": row.get("skill_version"),
                            "input_schema": self._json(row.get("input_schema")) or {},
                            "research_policy": self._json(row.get("research_policy")) or {},
                            "writing_policy": self._json(row.get("writing_policy")) or {},
                            "visual_policy": self._json(row.get("visual_policy")) or {},
                            "asset_policy": self._json(row.get("asset_policy")) or {},
                            "qc_policy": self._json(row.get("qc_policy")) or {},
                            "output_contract": self._json(row.get("output_contract")) or {},
                            "capability_requirements": (
                                self._json(row.get("capability_requirements")) or []
                            ),
                        },
                    },
                }
                definitions = tuple(
                    RunStep(
                        key=str(node["key"]),
                        step_type=str(node["operation"]),
                        input_snapshot=snapshot,
                        dependencies=tuple(node.get("depends_on", ())),
                        queue_name="run-steps",
                        retry_policy=RetryPolicy(
                            max_attempts=int(node.get("maximum_attempts", 3))
                        ),
                        review_required=bool(node.get("review_gate", False)),
                    )
                    for node in nodes
                )
                pending.append(
                    PendingRunGraph(
                        workspace_id=str(row["workspace_id"]),
                        run_id=str(row["id"]),
                        # The API-owned Run keeps the user's original input.
                        # Provider policy/composition metadata belongs to the
                        # immutable per-step snapshot assembled above.  Passing
                        # the augmented snapshot as the Run input breaks the
                        # scheduler's idempotency check for every API-created
                        # Run before its graph can be materialized.
                        input_snapshot=run_input,
                        nodes=definitions,
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                self.record_initialization_error(
                    str(row["workspace_id"]),
                    str(row["id"]),
                    str(exc) or exc.__class__.__name__,
                )
        return tuple(pending)

    def record_artifact(self, artifact: ArtifactRef, *, bucket: str) -> None:
        """Idempotently persist an already-uploaded immutable object reference."""

        with self._transaction():
            inserted = self._connection.execute(
                """INSERT INTO artifacts (
                   id, workspace_id, run_id, step_id, kind, status, schema_version,
                   media_type, storage_provider, bucket, object_key, content_hash,
                   byte_size, metadata, created_at
               ) VALUES (
                   %s, %s, %s, %s, %s, 'available', %s,
                   %s, 's3', %s, %s, %s, %s, %s, %s
               ) ON CONFLICT (workspace_id, id) DO NOTHING RETURNING id""",
                (
                artifact.id,
                artifact.workspace_id,
                artifact.run_id,
                self._database_step_id(artifact.step_id),
                artifact.kind,
                artifact.schema_version,
                artifact.media_type,
                bucket,
                artifact.object_key,
                artifact.content_hash,
                artifact.byte_size,
                Jsonb({"filename": artifact.filename}),
                self._datetime(artifact.created_at),
                ),
            ).fetchone()
            row = self._connection.execute(
                """SELECT object_key, content_hash, byte_size FROM artifacts
               WHERE workspace_id=%s AND id=%s""",
                (artifact.workspace_id, artifact.id),
            ).fetchone()
            if inserted is not None:
                self.append_event(
                    artifact.workspace_id,
                    artifact.run_id,
                    event_type="artifact.created",
                    deduplication_key=f"artifact:{artifact.id}",
                    step_id=artifact.step_id,
                    payload={
                        "artifact_id": artifact.id,
                        "kind": artifact.kind,
                        "content_hash": artifact.content_hash,
                    },
                    occurred_at=self._datetime(artifact.created_at),
                )
        if (
            row is None
            or row["object_key"] != artifact.object_key
            or row["content_hash"] != artifact.content_hash
            or int(row["byte_size"]) != artifact.byte_size
        ):
            raise RuntimeError("durable artifact record conflicts with uploaded object")

    def record_initialization_error(self, workspace_id: str, run_id: str, error: str) -> None:
        with self._transaction():
            row = self._connection.execute(
                """UPDATE runs SET status='failed', completed_at=now(), updated_at=now(),
                               worker_error=%s, worker_revision=worker_revision+1
                WHERE workspace_id=%s AND id=%s AND status='queued'
                RETURNING worker_revision, updated_at""",
                (Jsonb({"code": "pipeline_initialization_failed", "message": error}), workspace_id, run_id),
            ).fetchone()
            if row is not None:
                self.append_event(
                    workspace_id,
                    run_id,
                    event_type="run.failed",
                    deduplication_key=f"worker-run-status:{row['worker_revision']}:failed",
                    payload={"status": "failed", "code": "pipeline_initialization_failed"},
                    occurred_at=row["updated_at"],
                )

    def append_event(
        self,
        workspace_id: str,
        run_id: str,
        *,
        event_type: str,
        deduplication_key: str,
        actor_type: str = "worker",
        actor_id: str | None = None,
        step_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Append once with sequence allocation serialized by the parent Run row."""

        database_step_id = self._database_step_id(step_id) if step_id else None
        event_id = uuid5(
            NAMESPACE_URL,
            f"framefactory-event:{workspace_id}:{run_id}:{deduplication_key}",
        )
        with self._transaction():
            existing = self._connection.execute(
                """SELECT id FROM run_events
                WHERE workspace_id=%s AND run_id=%s AND deduplication_key=%s""",
                (workspace_id, run_id, deduplication_key),
            ).fetchone()
            if existing is not None:
                return
            locked = self._connection.execute(
                """SELECT id FROM runs
                WHERE workspace_id=%s AND id=%s FOR UPDATE""",
                (workspace_id, run_id),
            ).fetchone()
            if locked is None:
                raise LookupError(f"run not found: {workspace_id}/{run_id}")
            # Recheck after the lock because another writer may have committed
            # the same deterministic event while this transaction was waiting.
            existing = self._connection.execute(
                """SELECT id FROM run_events
                WHERE workspace_id=%s AND run_id=%s AND deduplication_key=%s""",
                (workspace_id, run_id, deduplication_key),
            ).fetchone()
            if existing is not None:
                return
            sequence_row = self._connection.execute(
                """SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence
                FROM run_events WHERE run_id=%s""",
                (run_id,),
            ).fetchone()
            sequence = int(sequence_row["sequence"])
            self._connection.execute(
                """INSERT INTO run_events (
                  id, workspace_id, run_id, step_id, sequence, event_type,
                  schema_version, payload, actor_type, actor_id, occurred_at,
                  deduplication_key, correlation_id, causation_id
                ) VALUES (%s,%s,%s,%s,%s,%s,'1.0.0',%s,%s,%s,%s,%s,%s,NULL)
                ON CONFLICT (workspace_id, run_id, deduplication_key) DO NOTHING""",
                (
                    event_id,
                    workspace_id,
                    run_id,
                    database_step_id,
                    sequence,
                    event_type,
                    Jsonb(dict(payload or {})),
                    actor_type,
                    actor_id,
                    occurred_at or datetime.now(UTC),
                    deduplication_key,
                    run_id,
                ),
            )

    def _append_step_event(self, step: StepRecord, *, revision: int) -> None:
        event_type = {
            ExecutionState.QUEUED: "step.queued",
            ExecutionState.RUNNING: "step.started",
            ExecutionState.RETRYING: "step.retrying",
            ExecutionState.AWAITING_REVIEW: "step.awaiting_review",
            ExecutionState.SUCCEEDED: "step.succeeded",
            ExecutionState.FAILED: "step.failed",
            ExecutionState.CANCELLED: "step.cancelled",
        }[step.status]
        payload: dict[str, Any] = {
            "status": step.status.value,
            "attempt": step.attempt_count,
            "revision": revision,
        }
        if step.error is not None:
            payload["error_code"] = step.error.code
        self.append_event(
            step.workspace_id,
            step.run_id,
            event_type=event_type,
            deduplication_key=(
                f"worker-step:{step.step_id}:attempt:{step.attempt_count}:status:{step.status.value}"
            ),
            step_id=step.step_id,
            payload=payload,
            occurred_at=step.updated_at,
        )
        if (
            step.review is not None
            and step.review.decision is not None
            and step.review.decided_at == step.updated_at
        ):
            self.append_event(
                step.workspace_id,
                step.run_id,
                event_type="review.recorded",
                deduplication_key=(
                    f"worker-review:{step.step_id}:{step.attempt_count}:"
                    f"{step.review.decision.value}"
                ),
                actor_type="user",
                actor_id=step.review.actor_id,
                step_id=step.step_id,
                payload={
                    "decision": step.review.decision.value,
                    "status": step.status.value,
                },
                occurred_at=step.updated_at,
            )

    def _transaction(self):
        transaction = getattr(self._connection, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()

    def _write_review_action(self, step: StepRecord) -> None:
        if step.review is None:
            return
        if (
            step.review.decision is None
            and step.status is ExecutionState.AWAITING_REVIEW
            and step.review.requested_at == step.updated_at
        ):
            action = "requested"
        elif (
            step.review.decision is ReviewDecision.APPROVE
            and step.review.decided_at == step.updated_at
        ):
            action = "approved"
        elif (
            step.review.decision is ReviewDecision.REJECT
            and step.review.decided_at == step.updated_at
        ):
            action = "rejected"
        elif (
            step.review.decision is ReviewDecision.REQUEST_CHANGES
            and step.review.decided_at == step.updated_at
        ):
            action = "changes_requested"
        else:
            return
        actor_uuid = None
        if step.review.actor_id:
            try:
                actor_uuid = UUID(step.review.actor_id)
            except ValueError:
                actor_uuid = None
        self._connection.execute(
            """INSERT INTO review_actions (
                   workspace_id, run_id, step_id, action, comment, details, actor_user_id
               ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                step.workspace_id,
                step.run_id,
                self._database_step_id(step.step_id),
                action,
                step.review.comment,
                Jsonb({"actor_id": step.review.actor_id}),
                actor_uuid,
            ),
        )

    _STEP_SELECT = """SELECT workspace_id, run_id, worker_step_id, step_key, step_type,
        status, queue_name, required_capabilities, input_snapshot, output_summary,
        error, priority, available_at, attempt_count, max_attempts, lease_owner,
        lease_token, lease_expires_at, heartbeat_at, started_at, completed_at,
        created_at, updated_at, dependencies, retry_policy, output_artifacts,
        review_required, static_review_required, review,
        cancellation_requested_at, worker_revision
        FROM run_steps"""

    @staticmethod
    def _database_step_id(step_id: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"framefactory-worker-step:{step_id}")

    @staticmethod
    def _database_run_status(status: ExecutionState) -> str:
        # The public run contract intentionally has no retrying state. A retrying
        # step means the aggregate run is still running.
        return "running" if status is ExecutionState.RETRYING else status.value

    @classmethod
    def _run(cls, row: Mapping[str, Any]) -> RunRecord:
        return RunRecord(
            workspace_id=str(row["workspace_id"]),
            run_id=str(row["id"]),
            input_snapshot=cls._json(row["input_snapshot"]),
            status=ExecutionState(row["status"]),
            cancellation_requested_at=row["cancel_requested_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=int(row["worker_revision"]),
        )

    @classmethod
    def _step(cls, row: Mapping[str, Any]) -> StepRecord:
        lease = None
        if row["lease_owner"] is not None:
            lease = Lease(
                owner=row["lease_owner"],
                token=str(row["lease_token"]),
                expires_at=row["lease_expires_at"],
                heartbeat_at=row["heartbeat_at"],
            )
        error_value = cls._json(row["error"])
        error = None
        if error_value:
            error = StepError(
                code=error_value["code"],
                message=error_value["message"],
                retryable=bool(error_value.get("retryable", False)),
                details=error_value.get("details", {}),
            )
        review_value = cls._json(row["review"])
        review = None
        if review_value:
            decision = review_value.get("decision")
            review = ReviewRecord(
                decision=ReviewDecision(decision) if decision else None,
                actor_id=review_value.get("actor_id"),
                comment=review_value.get("comment"),
                requested_at=cls._datetime(review_value.get("requested_at")),
                decided_at=cls._datetime(review_value.get("decided_at")),
            )
        retry = cls._json(row["retry_policy"]) or {}
        artifacts = tuple(
            ArtifactRef.from_mapping(value)
            for value in (cls._json(row["output_artifacts"]) or [])
        )
        return StepRecord(
            workspace_id=str(row["workspace_id"]),
            run_id=str(row["run_id"]),
            step_id=row["worker_step_id"],
            step_key=row["step_key"],
            step_type=row["step_type"],
            input_snapshot=cls._json(row["input_snapshot"]),
            dependencies=tuple(row["dependencies"] or ()),
            status=ExecutionState(row["status"]),
            queue_name=row["queue_name"],
            required_capabilities=tuple(row["required_capabilities"] or ()),
            priority=int(row["priority"]),
            available_at=row["available_at"],
            attempt_count=int(row["attempt_count"]),
            retry_policy=RetryPolicy(
                max_attempts=int(row["max_attempts"]),
                base_delay_seconds=float(retry.get("base_delay_seconds", 1)),
                max_delay_seconds=float(retry.get("max_delay_seconds", 300)),
                multiplier=float(retry.get("multiplier", 2)),
            ),
            lease=lease,
            output_artifacts=artifacts,
            output_summary=cls._json(row["output_summary"]),
            error=error,
            review_required=bool(row["review_required"]),
            static_review_required=bool(row["static_review_required"]),
            review=review,
            cancellation_requested_at=row["cancellation_requested_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=int(row["worker_revision"]),
        )

    @staticmethod
    def _datetime(value: str | datetime | None) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _json(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    @staticmethod
    def _step_values(step: StepRecord) -> tuple[Any, ...]:
        lease = step.lease
        error = None
        if step.error:
            error = {
                "code": step.error.code,
                "message": step.error.message,
                "retryable": step.error.retryable,
                "details": thaw_json(step.error.details),
            }
        review = None
        if step.review:
            review = {
                "decision": step.review.decision.value if step.review.decision else None,
                "actor_id": step.review.actor_id,
                "comment": step.review.comment,
                "requested_at": (
                    step.review.requested_at.isoformat() if step.review.requested_at else None
                ),
                "decided_at": (
                    step.review.decided_at.isoformat() if step.review.decided_at else None
                ),
            }
        snapshot = (
            step.input_snapshot.to_dict()
            if callable(getattr(step.input_snapshot, "to_dict", None))
            else thaw_json(step.input_snapshot)
        )
        return (
            step.workspace_id,
            step.run_id,
            step.step_id,
            step.step_key,
            step.step_type,
            step.status.value,
            step.queue_name,
            list(step.required_capabilities),
            Jsonb(snapshot),
            Jsonb(thaw_json(step.output_summary)) if step.output_summary is not None else None,
            Jsonb(error) if error else None,
            step.priority,
            step.available_at,
            step.attempt_count,
            step.retry_policy.max_attempts,
            lease.owner if lease else None,
            lease.token if lease else None,
            lease.expires_at if lease else None,
            lease.heartbeat_at if lease else None,
            step.started_at,
            step.completed_at,
            step.created_at,
            step.updated_at,
            list(step.dependencies),
            Jsonb(
                {
                    "base_delay_seconds": step.retry_policy.base_delay_seconds,
                    "max_delay_seconds": step.retry_policy.max_delay_seconds,
                    "multiplier": step.retry_policy.multiplier,
                }
            ),
            Jsonb(
                [
                    artifact.to_dict()
                    if callable(getattr(artifact, "to_dict", None))
                    else artifact
                    for artifact in step.output_artifacts
                ]
            ),
            step.review_required,
            step.static_review_required,
            Jsonb(review) if review else None,
            step.cancellation_requested_at,
        )
