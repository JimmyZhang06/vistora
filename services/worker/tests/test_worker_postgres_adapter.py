from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg
from framefactory.ports import RevisionConflictError
from framefactory.runtime import (
    ExecutionState,
    PermanentStepError,
    ReviewRecord,
    RunRecord,
    StepRecord,
)
from framefactory.steps import ArtifactRef
from framefactory.worker.adapters.postgres import PostgresRunStore


class Cursor:
    def __init__(self, *, one=None, many=()) -> None:
        self.one = one
        self.many = many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class FakeConnection:
    def __init__(self, cursors: list[Cursor]) -> None:
        self.cursors = cursors
        self.calls: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql: str, parameters=()) -> Cursor:
        self.calls.append((sql, parameters))
        return self.cursors.pop(0)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class IntegrityFailureConnection(FakeConnection):
    def __init__(self, error: psycopg.IntegrityError) -> None:
        super().__init__([])
        self.error = error

    def execute(self, sql: str, parameters=()) -> Cursor:
        self.calls.append((sql, parameters))
        raise self.error


class PostgresRunStoreTests(unittest.TestCase):
    def test_artifact_tenant_constraint_is_a_permanent_stable_error(self) -> None:
        artifact = ArtifactRef(
            id="44444444-4444-4444-8444-444444444444",
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="55555555-5555-4555-8555-555555555555",
            kind="manifest",
            media_type="application/json",
            object_key=(
                "browser-capture/workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/"
                "44444444-4444-4444-8444-444444444444/capture-validation.json"
            ),
            byte_size=2,
            content_hash="a" * 64,
            filename="capture-validation.json",
            created_at="2026-08-24T00:00:00Z",
        )
        connection = IntegrityFailureConnection(
            psycopg.errors.CheckViolation(
                'new row violates check constraint "artifacts_tenant_key"'
            )
        )

        with self.assertRaises(PermanentStepError) as raised:
            PostgresRunStore(connection).record_artifact(artifact, bucket="artifacts")

        self.assertEqual("artifact_tenant_key_violation", raised.exception.code)
        self.assertNotIn("new row", str(raised.exception))

    def test_capture_attempt_binds_exact_final_worker_revision_and_replay(self) -> None:
        workspace_id = "11111111-1111-4111-8111-111111111111"
        run_id = "22222222-2222-4222-8222-222222222222"
        webpage_run_id = "33333333-3333-4333-8333-333333333333"
        step_id = UUID("44444444-4444-4444-8444-444444444444")
        artifact_id = "55555555-5555-4555-8555-555555555555"
        captured_at = datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC)
        sha256 = "a" * 64
        metadata = {
            "redirect_chain": ["https://example.com/?redacted=1"],
            "engine": "chromium",
            "browser_version": "140.0.0",
            "playwright_version": "1.62.0",
            "websocket_attempts": 0,
            "blocked_non_idempotent_requests": 0,
        }
        stored = {
            "outcome": "captured",
            "capture_revision": 7,
            "requested_url": "https://example.com/?redacted=1",
            "final_url": "https://example.com/?redacted=1",
            "viewport_width": 1920,
            "viewport_height": 1080,
            "full_page": False,
            "artifact_id": artifact_id,
            "sha256": sha256,
            "media_type": "image/png",
            "metadata": metadata,
            "error": None,
            "captured_at": captured_at,
        }
        connection = FakeConnection(
            [
                Cursor(one={"id": webpage_run_id, "worker_revision": 7}),
                Cursor(),
                Cursor(one=stored),
            ]
        )
        artifact = ArtifactRef(
            id=artifact_id,
            workspace_id=workspace_id,
            run_id=run_id,
            step_id=str(step_id),
            kind="image",
            media_type="image/png",
            object_key="browser-capture/workspaces/w/runs/r/artifacts/a/webpage.png",
            byte_size=24,
            content_hash=sha256,
            filename="webpage.png",
        )
        PostgresRunStore(connection)._record_webpage_capture_attempt(
            workspace_id=workspace_id,
            run_id=run_id,
            step_id=step_id,
            capture_revision=7,
            evidence={
                "webpage_video_run_id": webpage_run_id,
                "attempt_number": 2,
                "requested_url": "https://example.com/?redacted=1",
                "final_url": "https://example.com/?redacted=1",
                "viewport_width": 1920,
                "viewport_height": 1080,
                "full_page": False,
                "captured_at": captured_at.isoformat(),
                "metadata": metadata,
            },
            artifact=artifact,
            error=None,
        )
        self.assertIn("rs.worker_revision", connection.calls[0][0])
        self.assertIn("ON CONFLICT", connection.calls[1][0])

    def test_capture_attempt_rejects_stale_revision_before_insert(self) -> None:
        connection = FakeConnection([Cursor(one={"id": uuid4(), "worker_revision": 6})])
        with self.assertRaisesRegex(RuntimeError, "revision"):
            PostgresRunStore(connection)._record_webpage_capture_attempt(
                workspace_id="11111111-1111-4111-8111-111111111111",
                run_id="22222222-2222-4222-8222-222222222222",
                step_id=UUID("44444444-4444-4444-8444-444444444444"),
                capture_revision=7,
                evidence={
                    "webpage_video_run_id": "33333333-3333-4333-8333-333333333333",
                    "attempt_number": 1,
                    "requested_url": "https://example.com/",
                    "final_url": None,
                    "viewport_width": 1920,
                    "viewport_height": 1080,
                    "full_page": False,
                    "captured_at": None,
                    "metadata": {},
                },
                artifact=None,
                error={"code": "capture_failed", "retryable": False},
            )
        self.assertEqual(1, len(connection.calls))

    def test_healthcheck_requires_worker_schema_migration(self) -> None:
        connection = FakeConnection([Cursor(one={"healthy": False})])
        with self.assertRaisesRegex(RuntimeError, "0001_runtime_state.sql"):
            PostgresRunStore(connection).healthcheck()
        self.assertIn("static_review_required", connection.calls[0][0])

    def test_run_update_uses_revision_compare_and_swap(self) -> None:
        now = datetime(2026, 8, 16, tzinfo=UTC)
        connection = FakeConnection(
            [Cursor(one={"status": "queued"}), Cursor(one=None)]
        )
        store = PostgresRunStore(connection)
        with self.assertRaises(RevisionConflictError):
            store.save_run(
                RunRecord(
                    workspace_id="11111111-1111-4111-8111-111111111111",
                    run_id="22222222-2222-4222-8222-222222222222",
                    input_snapshot={},
                    status=ExecutionState.QUEUED,
                    created_at=now,
                    updated_at=now,
                    revision=4,
                ),
                expected_revision=3,
            )
        sql, parameters = connection.calls[1]
        self.assertIn("worker_revision = worker_revision + 1", sql)
        self.assertIn("worker_revision = %s", sql)
        self.assertIn("COALESCE(runs.cancel_requested_at", sql)
        self.assertEqual(3, parameters[-1])

    def test_retrying_step_maps_to_public_running_run_state(self) -> None:
        self.assertEqual(
            "running", PostgresRunStore._database_run_status(ExecutionState.RETRYING)
        )

    def test_pending_scan_materializes_pipeline_nodes_and_allows_official_pipeline(
        self,
    ) -> None:
        now = datetime(2026, 8, 16, tzinfo=UTC)
        connection = FakeConnection(
            [
                Cursor(
                    many=(
                        {
                            "workspace_id": "11111111-1111-4111-8111-111111111111",
                            "id": "22222222-2222-4222-8222-222222222222",
                            "input_snapshot": {"topic": "durability"},
                            "composition_snapshot": {
                                "skill_version": {"id": "version"}
                            },
                            "skill_version_id": "33333333-3333-4333-8333-333333333333",
                            "skill_version": "1.0.0",
                            "research_policy": {"depth": 3},
                            "writing_policy": {"tone": "documentary"},
                            "graph": {
                                "nodes": [
                                    {
                                        "key": "research",
                                        "operation": "research.collect",
                                        "depends_on": [],
                                        "maximum_attempts": 4,
                                        "review_gate": False,
                                    }
                                ]
                            },
                            "created_at": now,
                        },
                    )
                )
            ]
        )
        pending = PostgresRunStore(connection).load_pending_graphs()
        self.assertEqual({"topic": "durability"}, pending[0].input_snapshot)
        self.assertEqual("research.collect", pending[0].nodes[0].step_type)
        self.assertEqual(4, pending[0].nodes[0].retry_policy.max_attempts)
        step_input = pending[0].nodes[0].input_snapshot
        self.assertEqual(
            3,
            step_input["_framefactory"]["skill_version"]["research_policy"]["depth"],
        )
        sql = connection.calls[0][0]
        self.assertIn("pv.id = r.pipeline_version_id", sql)
        self.assertNotIn("pv.workspace_id = r.workspace_id AND pv.id", sql)
        self.assertIn("pw.kind = 'system'", sql)
        self.assertIn("p.visibility = 'public_readonly'", sql)
        self.assertIn("sv.id = r.skill_version_id", sql)
        self.assertIn("sv.state = 'published'", sql)
        self.assertNotIn("NOT EXISTS", sql)

    def test_review_transition_appends_audit_action(self) -> None:
        now = datetime(2026, 8, 16, tzinfo=UTC)
        connection = FakeConnection([Cursor()])
        store = PostgresRunStore(connection)
        store._write_review_action(
            StepRecord(
                workspace_id="11111111-1111-4111-8111-111111111111",
                run_id="22222222-2222-4222-8222-222222222222",
                step_id="22222222-2222-4222-8222-222222222222:quality",
                step_key="quality",
                step_type="quality.evaluate",
                input_snapshot={},
                status=ExecutionState.AWAITING_REVIEW,
                review_required=True,
                review=ReviewRecord(requested_at=now),
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        sql, parameters = connection.calls[0]
        self.assertIn("INSERT INTO review_actions", sql)
        self.assertEqual("requested", parameters[3])

    def test_invalid_graph_is_failed_without_blocking_other_runs(self) -> None:
        connection = FakeConnection(
            [
                Cursor(
                    many=(
                        {
                            "workspace_id": "11111111-1111-4111-8111-111111111111",
                            "id": "22222222-2222-4222-8222-222222222222",
                            "input_snapshot": {},
                            "graph": {"nodes": []},
                            "materialized_steps": 0,
                        },
                    )
                ),
                Cursor(),
            ]
        )
        pending = PostgresRunStore(connection).load_pending_graphs()
        self.assertEqual((), pending)
        self.assertEqual(
            "pipeline_initialization_failed", connection.calls[1][1][0].obj["code"]
        )

    def test_artifact_record_is_idempotent_and_verifies_immutable_identity(
        self,
    ) -> None:
        artifact = ArtifactRef(
            id="44444444-4444-4444-8444-444444444444",
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="55555555-5555-4555-8555-555555555555",
            kind="research",
            media_type="application/json",
            object_key=(
                "workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/"
                "44444444-4444-4444-8444-444444444444/research.json"
            ),
            byte_size=2,
            content_hash="a" * 64,
            filename="research.json",
            created_at="2026-08-16T00:00:00Z",
        )
        connection = FakeConnection(
            [
                Cursor(),
                Cursor(
                    one={
                        "object_key": artifact.object_key,
                        "content_hash": artifact.content_hash,
                        "byte_size": artifact.byte_size,
                    }
                ),
                Cursor(),
                Cursor(
                    one={
                        "object_key": artifact.object_key,
                        "content_hash": artifact.content_hash,
                        "byte_size": artifact.byte_size,
                    }
                ),
            ]
        )
        store = PostgresRunStore(connection)
        store.record_artifact(artifact, bucket="artifacts")
        store.record_artifact(artifact, bucket="artifacts")
        self.assertIn(
            "ON CONFLICT (workspace_id, id) DO NOTHING", connection.calls[0][0]
        )
        self.assertEqual(connection.calls[0][1][:11], connection.calls[2][1][:11])
        self.assertEqual(connection.calls[0][1][11].obj, connection.calls[2][1][11].obj)
        self.assertEqual(connection.calls[0][1][12], connection.calls[2][1][12])
        self.assertNotIn("secret", str(connection.calls))

    def test_retry_records_same_content_under_distinct_filename_identity(self) -> None:
        first = ArtifactRef(
            id="44444444-4444-4444-8444-444444444444",
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="55555555-5555-4555-8555-555555555555",
            kind="asset",
            media_type="image/jpeg",
            object_key=(
                "workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/"
                "44444444-4444-4444-8444-444444444444/asset-001.jpg"
            ),
            byte_size=12,
            content_hash="a" * 64,
            filename="asset-001.jpg",
            created_at="2026-08-16T00:00:00Z",
        )
        reordered_retry = replace(
            first,
            id="66666666-6666-4666-8666-666666666666",
            object_key=(
                "workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/"
                "66666666-6666-4666-8666-666666666666/asset-002.jpg"
            ),
            filename="asset-002.jpg",
        )
        connection = FakeConnection(
            [
                Cursor(one={"id": first.id}),
                Cursor(
                    one={
                        "object_key": first.object_key,
                        "content_hash": first.content_hash,
                        "byte_size": first.byte_size,
                    }
                ),
                Cursor(one={"id": reordered_retry.id}),
                Cursor(
                    one={
                        "object_key": reordered_retry.object_key,
                        "content_hash": reordered_retry.content_hash,
                        "byte_size": reordered_retry.byte_size,
                    }
                ),
            ]
        )
        store = PostgresRunStore(connection)
        store.append_event = MagicMock()  # type: ignore[method-assign]

        store.record_artifact(first, bucket="artifacts")
        store.record_artifact(reordered_retry, bucket="artifacts")

        self.assertNotEqual(first.id, reordered_retry.id)
        self.assertEqual(first.content_hash, reordered_retry.content_hash)
        self.assertEqual(first.id, connection.calls[0][1][0])
        self.assertEqual(reordered_retry.id, connection.calls[2][1][0])
        self.assertEqual(first.object_key, connection.calls[0][1][8])
        self.assertEqual(reordered_retry.object_key, connection.calls[2][1][8])
        self.assertEqual(4, len(connection.calls))
        self.assertEqual([], connection.cursors)
        self.assertEqual(2, store.append_event.call_count)

    def test_artifact_insert_emits_one_event_with_database_step_identity(self) -> None:
        artifact = ArtifactRef(
            id="44444444-4444-4444-8444-444444444444",
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="22222222-2222-4222-8222-222222222222:research",
            kind="research",
            media_type="application/json",
            object_key=(
                "workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/"
                "44444444-4444-4444-8444-444444444444/research.json"
            ),
            byte_size=2,
            content_hash="a" * 64,
            filename="research.json",
            created_at="2026-08-16T00:00:00Z",
        )
        connection = FakeConnection(
            [
                Cursor(one={"id": artifact.id}),
                Cursor(
                    one={
                        "object_key": artifact.object_key,
                        "content_hash": artifact.content_hash,
                        "byte_size": artifact.byte_size,
                    }
                ),
            ]
        )
        store = PostgresRunStore(connection)
        store.append_event = MagicMock()  # type: ignore[method-assign]

        store.record_artifact(artifact, bucket="artifacts")

        self.assertEqual(
            PostgresRunStore._database_step_id(artifact.step_id),
            connection.calls[0][1][3],
        )
        store.append_event.assert_called_once()
        self.assertEqual(
            "artifact.created", store.append_event.call_args.kwargs["event_type"]
        )

    def test_append_event_replay_does_not_allocate_another_sequence(self) -> None:
        connection = FakeConnection(
            [
                Cursor(one=None),
                Cursor(one={"id": "run"}),
                Cursor(one=None),
                Cursor(one={"sequence": 1}),
                Cursor(),
                Cursor(one={"id": "event"}),
            ]
        )
        store = PostgresRunStore(connection)

        for _ in range(2):
            store.append_event(
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
                event_type="run.graph_materialized",
                deduplication_key="graph",
            )

        inserts = [
            sql for sql, _ in connection.calls if "INSERT INTO run_events" in sql
        ]
        self.assertEqual(1, len(inserts))


if __name__ == "__main__":
    unittest.main()
