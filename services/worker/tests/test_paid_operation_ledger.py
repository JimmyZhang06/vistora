from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from framefactory.worker.generation.ledger import (
    PaidOperationConflictError,
    PaidOperationStateError,
    PostgresPaidOperationLedger,
)
from framefactory.worker.generation.ports import PaidOperationAction

NOW = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SCHEDULER_RUN_ID = "22222222-2222-4222-8222-222222222222"
FULL_AI_RUN_ID = "33333333-3333-4333-8333-333333333333"
OPERATION_ID = "44444444-4444-4444-8444-444444444444"
REQUEST_HASH = "a" * 64


class _Cursor:
    def __init__(self, *, one: Any = None, many: list[dict[str, Any]] | None = None):
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(
        self,
        cursors: list[_Cursor],
        *,
        transition_parent: dict[str, Any] | None = None,
        transition_request_hash: str = REQUEST_HASH,
    ) -> None:
        self.cursors = cursors
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.transition_parent = transition_parent or {
            "status": "generating",
            "requires_reconciliation": False,
        }
        self.transition_request_hash = transition_request_hash

    def execute(self, sql: str, parameters=()) -> _Cursor:
        self.calls.append((sql, tuple(parameters)))
        if "SELECT full_ai_run_id, request_hash" in sql:
            return _Cursor(
                one={
                    "full_ai_run_id": FULL_AI_RUN_ID,
                    "request_hash": self.transition_request_hash,
                }
            )
        if "SELECT status, requires_reconciliation" in sql:
            return _Cursor(one=self.transition_parent)
        return self.cursors.pop(0)

    def transaction(self):
        return nullcontext()

    def close(self) -> None:
        return None


def _parent(**overrides: Any) -> dict[str, Any]:
    return {
        "provider_name": "runway",
        "model_id": "gen4.5",
        "authorized_amount_minor": 120,
        "incurred_amount_minor": 0,
        "requires_reconciliation": False,
        "billing_status": "not_started",
        "status": "generating",
        "candidate_count": 2,
        **overrides,
    }


def _operation(**overrides: Any) -> dict[str, Any]:
    return {
        "id": OPERATION_ID,
        "workspace_id": WORKSPACE_ID,
        "full_ai_run_id": FULL_AI_RUN_ID,
        "operation_key": "media.generate:scene-1:variant:0",
        "scene_key": "scene-1",
        "variant_index": 0,
        "request_hash": REQUEST_HASH,
        "provider_name": "runway",
        "model_id": "gen4.5",
        "provider_idempotency_key": "audit-sha256:example",
        "status": "reserved",
        "provider_request_id": None,
        "authorized_amount_minor": 60,
        "incurred_amount_minor": 0,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "reconciliation_attempts": 0,
        "last_error": None,
        "result": None,
        **overrides,
    }


def _result(
    *, accepted: bool = True, with_artifact: bool = True
) -> dict[str, Any]:
    content_hash = "c" * 64
    filename = "generated-scene-1-v1.mp4"
    media_type = "video/mp4"
    byte_size = 4096
    artifact = (
        {
            "schema_version": "1.0.0",
            "id": "66666666-6666-4666-8666-666666666666",
            "workspace_id": WORKSPACE_ID,
            "ownership_type": "workspace",
            "run_id": SCHEDULER_RUN_ID,
            "step_id": f"{SCHEDULER_RUN_ID}:generate",
            "kind": "asset",
            "media_type": media_type,
            "object_key": f"workspaces/{WORKSPACE_ID}/runs/{SCHEDULER_RUN_ID}/asset.mp4",
            "byte_size": byte_size,
            "content_hash": content_hash,
            "filename": filename,
            "created_at": NOW.isoformat(),
            "expires_at": None,
        }
        if accepted and with_artifact
        else None
    )
    return {
        "schema_version": "1.0.0",
        "verification_status": "accepted" if accepted else "rejected",
        "output_content_hash": content_hash,
        "output_byte_size": byte_size,
        "output_media_type": media_type,
        "output_filename": filename,
        "accepted_artifact": artifact,
        "verification_evidence": {
            "accepted": accepted,
            "rejection_codes": [] if accepted else ["semantic_incomplete"],
            "verifier_provider": "openai-compatible",
            "verifier_model": "vision-model",
            "frame_hashes": ["d" * 64],
            "semantic_complete": accepted,
        },
    }


def _reserve(ledger: PostgresPaidOperationLedger, **overrides: Any):
    arguments = {
        "workspace_id": WORKSPACE_ID,
        "underlying_run_id": SCHEDULER_RUN_ID,
        "full_ai_run_id": FULL_AI_RUN_ID,
        "scene_key": "scene-1",
        "variant_index": 0,
        "operation_key": "media.generate:scene-1:variant:0",
        "request_hash": REQUEST_HASH,
        "provider_name": "runway",
        "model_id": "gen4.5",
        "authorized_amount_minor": 60,
        "now": NOW,
        **overrides,
    }
    return ledger.reserve(**arguments)


def _call_containing(
    connection: _Connection, fragment: str
) -> tuple[str, tuple[Any, ...]]:
    return next(call for call in connection.calls if fragment in call[0])


def test_reserve_locks_parent_checks_aggregate_budget_and_inserts_once() -> None:
    inserted = _operation()
    connection = _Connection(
        [
            _Cursor(one=_parent()),
            _Cursor(many=[]),
            _Cursor(one={"operation_count": 0, "authorized_total": 0}),
            _Cursor(one=inserted),
        ]
    )

    decision = _reserve(PostgresPaidOperationLedger(connection))

    assert decision.action is PaidOperationAction.SUBMIT_NEW
    assert decision.record.id == OPERATION_ID
    assert "FROM full_ai_runs" in connection.calls[0][0]
    assert "underlying_run_id = %s" in connection.calls[0][0]
    assert "FOR UPDATE" in connection.calls[0][0]
    assert "sum(authorized_amount_minor)" in connection.calls[2][0]
    assert "INSERT INTO full_ai_paid_operations" in connection.calls[3][0]
    provider_audit_key = connection.calls[3][1][9]
    assert provider_audit_key.startswith("audit-sha256:")


def test_reserve_replays_same_operation_and_rejects_a_different_hash() -> None:
    replay_connection = _Connection(
        [_Cursor(one=_parent()), _Cursor(many=[_operation()])]
    )
    replay = _reserve(PostgresPaidOperationLedger(replay_connection))
    assert replay.action is PaidOperationAction.SUBMIT_NEW
    assert len(replay_connection.calls) == 2

    conflict_connection = _Connection(
        [_Cursor(one=_parent()), _Cursor(many=[_operation()])]
    )
    with pytest.raises(PaidOperationConflictError) as caught:
        _reserve(
            PostgresPaidOperationLedger(conflict_connection),
            request_hash="b" * 64,
        )
    assert caught.value.code == "FULL_AI_OPERATION_REQUEST_CONFLICT"
    assert len(conflict_connection.calls) == 2


def test_reserve_rejects_candidate_or_money_beyond_the_frozen_plan() -> None:
    connection = _Connection(
        [
            _Cursor(one=_parent()),
            _Cursor(many=[]),
            _Cursor(one={"operation_count": 1, "authorized_total": 90}),
        ]
    )
    with pytest.raises(PaidOperationStateError) as caught:
        _reserve(PostgresPaidOperationLedger(connection))
    assert caught.value.code == "FULL_AI_BUDGET_EXCEEDED"
    assert all("INSERT INTO full_ai_paid_operations" not in sql for sql, _ in connection.calls)


def test_active_submit_lease_waits_but_expired_lease_becomes_unknown() -> None:
    active = _operation(
        status="submitting",
        lease_owner="worker-a",
        lease_token="55555555-5555-4555-8555-555555555555",
        lease_expires_at=NOW + timedelta(seconds=30),
    )
    active_connection = _Connection(
        [_Cursor(one=_parent()), _Cursor(many=[active])]
    )
    active_decision = _reserve(PostgresPaidOperationLedger(active_connection))
    assert active_decision.action is PaidOperationAction.BUSY
    assert len(active_connection.calls) == 2

    expired = {**active, "lease_expires_at": NOW - timedelta(seconds=1)}
    submit_unknown = _operation(
        status="submit_unknown",
        reconciliation_attempts=1,
        last_error={"automatic_resubmission_forbidden": True},
    )
    expired_connection = _Connection(
        [
            _Cursor(one=_parent()),
            _Cursor(many=[expired]),
            _Cursor(one=submit_unknown),
        ]
    )
    expired_decision = _reserve(PostgresPaidOperationLedger(expired_connection))
    assert expired_decision.action is PaidOperationAction.MANUAL_RECONCILIATION_REQUIRED
    expire_sql, expire_parameters = expired_connection.calls[2]
    assert "status = 'submit_unknown'" in expire_sql
    assert "lease_expires_at <= %s" in expire_sql
    assert expire_parameters[0].obj == {
        "code": "FULL_AI_SUBMIT_LEASE_EXPIRED",
        "submit_outcome": "unknown",
        "automatic_resubmission_forbidden": True,
    }


def test_submit_unknown_is_never_reopened_and_known_task_can_only_reconcile() -> None:
    unknown_connection = _Connection(
        [
            _Cursor(one=_parent(requires_reconciliation=True)),
            _Cursor(many=[_operation(status="submit_unknown")]),
        ]
    )
    unknown = _reserve(PostgresPaidOperationLedger(unknown_connection))
    assert unknown.action is PaidOperationAction.MANUAL_RECONCILIATION_REQUIRED
    assert len(unknown_connection.calls) == 2

    known_connection = _Connection(
        [
            _Cursor(one=_parent(requires_reconciliation=True)),
            _Cursor(
                many=[
                    _operation(
                        status="submit_unknown", provider_request_id="task-123"
                    )
                ]
            ),
        ]
    )
    known = _reserve(PostgresPaidOperationLedger(known_connection))
    assert known.action is PaidOperationAction.RECONCILE_EXISTING
    assert known.record.provider_request_id == "task-123"


def test_submit_transition_and_definite_rejection_release_are_lease_cas() -> None:
    lease_token = "55555555-5555-4555-8555-555555555555"
    submitting = _operation(
        status="submitting",
        lease_owner="worker-a",
        lease_token=lease_token,
        lease_expires_at=NOW + timedelta(seconds=120),
    )
    begin_connection = _Connection(
        [_Cursor(one=_operation()), _Cursor(one=submitting)]
    )
    began = PostgresPaidOperationLedger(begin_connection).begin_submit(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        lease_owner="worker-a",
        lease_token=lease_token,
        lease_seconds=120,
        now=NOW,
    )
    assert began.status == "submitting"
    assert "status = 'reserved'" in _call_containing(
        begin_connection, "UPDATE full_ai_paid_operations"
    )[0]

    released = _operation(
        last_error={"code": "RUNWAY_429", "definite_rejection": True}
    )
    release_connection = _Connection(
        [_Cursor(one=submitting), _Cursor(one=released)]
    )
    result = PostgresPaidOperationLedger(
        release_connection
    ).release_definite_rejection(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        lease_token=lease_token,
        error={"code": "RUNWAY_429"},
        now=NOW,
    )
    assert result.status == "reserved"
    release_sql, release_parameters = _call_containing(
        release_connection, "UPDATE full_ai_paid_operations"
    )
    assert release_parameters[0].obj["definite_rejection"] is True
    assert "lease_token = %s" in release_sql


def test_begin_submit_locks_parent_first_and_stops_after_any_unknown_charge() -> None:
    connection = _Connection(
        [],
        transition_parent={
            "status": "reconciliation_required",
            "requires_reconciliation": True,
        },
    )
    with pytest.raises(PaidOperationStateError) as caught:
        PostgresPaidOperationLedger(connection).begin_submit(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            lease_owner="worker-a",
            lease_token="55555555-5555-4555-8555-555555555555",
            lease_seconds=120,
            now=NOW,
        )
    assert caught.value.code == "FULL_AI_RECONCILIATION_REQUIRED"
    assert "SELECT full_ai_run_id, request_hash" in connection.calls[0][0]
    assert "FROM full_ai_runs" in connection.calls[1][0]
    assert "FOR UPDATE" in connection.calls[1][0]
    assert all("UPDATE full_ai_paid_operations" not in sql for sql, _ in connection.calls)


def test_submitted_unknown_and_success_transitions_preserve_task_and_cost() -> None:
    lease_token = "55555555-5555-4555-8555-555555555555"
    submitting = _operation(
        status="submitting",
        lease_owner="worker-a",
        lease_token=lease_token,
        lease_expires_at=NOW + timedelta(seconds=120),
    )
    submitted = _operation(status="submitted", provider_request_id="task-123")
    submitted_connection = _Connection(
        [_Cursor(one=submitting), _Cursor(one=submitted)]
    )
    result = PostgresPaidOperationLedger(submitted_connection).mark_submitted(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        lease_token=lease_token,
        provider_request_id="task-123",
        now=NOW,
    )
    assert result.provider_request_id == "task-123"

    unknown = _operation(status="submit_unknown", provider_request_id="task-123")
    unknown_connection = _Connection(
        [_Cursor(one=submitted), _Cursor(one=unknown)]
    )
    PostgresPaidOperationLedger(unknown_connection).mark_submit_unknown(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        lease_token=None,
        provider_request_id="task-123",
        error={"code": "QUERY_UNKNOWN"},
        now=NOW,
    )
    _, unknown_parameters = _call_containing(
        unknown_connection, "UPDATE full_ai_paid_operations"
    )
    assert unknown_parameters[1].obj[
        "automatic_resubmission_forbidden"
    ]

    checkpoint = _result()
    succeeded = _operation(
        status="succeeded",
        provider_request_id="task-123",
        incurred_amount_minor=55,
        result=checkpoint,
    )
    success_connection = _Connection(
        [_Cursor(one=unknown), _Cursor(one=succeeded)]
    )
    success = PostgresPaidOperationLedger(success_connection).mark_succeeded(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        provider_request_id="task-123",
        incurred_amount_minor=55,
        result=checkpoint,
        now=NOW,
    )
    assert success.incurred_amount_minor == 55
    assert success.result == checkpoint
    success_sql, success_parameters = _call_containing(
        success_connection, "UPDATE full_ai_paid_operations"
    )
    assert "result = %s" in success_sql
    assert success_parameters[2].obj == checkpoint

    discovered = _operation(status="submit_unknown", provider_request_id=None)
    discovered_success = _operation(
        status="succeeded",
        provider_request_id="task-discovered-manually",
        incurred_amount_minor=50,
        result=checkpoint,
    )
    discovered_connection = _Connection(
        [_Cursor(one=discovered), _Cursor(one=discovered_success)]
    )
    reconciled = PostgresPaidOperationLedger(discovered_connection).mark_succeeded(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        provider_request_id="task-discovered-manually",
        incurred_amount_minor=50,
        result=checkpoint,
        now=NOW,
    )
    assert reconciled.provider_request_id == "task-discovered-manually"
    assert "COALESCE(provider_request_id, %s)" in _call_containing(
        discovered_connection, "UPDATE full_ai_paid_operations"
    )[0]

    regression_connection = _Connection([_Cursor(one=succeeded)])
    with pytest.raises(PaidOperationConflictError) as caught:
        PostgresPaidOperationLedger(regression_connection).mark_succeeded(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            provider_request_id="task-123",
            incurred_amount_minor=54,
            result=checkpoint,
            now=NOW,
        )
    assert caught.value.code == "FULL_AI_COST_REGRESSION"


def test_terminal_success_replays_only_the_same_immutable_result_checkpoint() -> None:
    checkpoint = _result()
    succeeded = _operation(
        status="succeeded",
        provider_request_id="task-123",
        incurred_amount_minor=55,
        result=checkpoint,
    )
    reserve_connection = _Connection(
        [_Cursor(one=_parent()), _Cursor(many=[succeeded])]
    )
    decision = _reserve(PostgresPaidOperationLedger(reserve_connection))
    assert decision.action is PaidOperationAction.TERMINAL
    assert decision.record.result == checkpoint

    replay_connection = _Connection([_Cursor(one=succeeded)])
    replay = PostgresPaidOperationLedger(replay_connection).mark_succeeded(
        workspace_id=WORKSPACE_ID,
        operation_id=OPERATION_ID,
        request_hash=REQUEST_HASH,
        provider_request_id="task-123",
        incurred_amount_minor=55,
        result=checkpoint,
        now=NOW,
    )
    assert replay.result == checkpoint
    assert all("UPDATE full_ai_paid_operations" not in sql for sql, _ in replay_connection.calls)

    changed = _result()
    changed["verification_evidence"]["description"] = "different evidence"
    conflict_connection = _Connection([_Cursor(one=succeeded)])
    with pytest.raises(PaidOperationConflictError) as caught:
        PostgresPaidOperationLedger(conflict_connection).mark_succeeded(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            provider_request_id="task-123",
            incurred_amount_minor=55,
            result=changed,
            now=NOW,
        )
    assert caught.value.code == "FULL_AI_RESULT_CONFLICT"


def test_result_checkpoint_rejects_output_artifact_or_verification_tampering() -> None:
    mismatched_artifact = _result()
    mismatched_artifact["accepted_artifact"]["content_hash"] = "e" * 64
    with pytest.raises(ValueError, match="differs from the generated output"):
        PostgresPaidOperationLedger(_Connection([])).mark_succeeded(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            provider_request_id="task-123",
            incurred_amount_minor=55,
            result=mismatched_artifact,
            now=NOW,
        )

    mismatched_evidence = _result()
    mismatched_evidence["verification_evidence"]["accepted"] = False
    with pytest.raises(ValueError, match="conflicts with verification_status"):
        PostgresPaidOperationLedger(_Connection([])).mark_succeeded(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            provider_request_id="task-123",
            incurred_amount_minor=55,
            result=mismatched_evidence,
            now=NOW,
        )

    oversized_evidence = _result()
    oversized_evidence["verification_evidence"]["raw"] = "x" * (1024 * 1024)
    with pytest.raises(ValueError, match="one MiB evidence limit"):
        PostgresPaidOperationLedger(_Connection([])).mark_succeeded(
            workspace_id=WORKSPACE_ID,
            operation_id=OPERATION_ID,
            request_hash=REQUEST_HASH,
            provider_request_id="task-123",
            incurred_amount_minor=55,
            result=oversized_evidence,
            now=NOW,
        )


def test_settlement_requires_every_frozen_candidate_and_never_exceeds_budget() -> None:
    connection = _Connection(
        [
            _Cursor(one={"authorized_amount_minor": 120, "candidate_count": 2}),
            _Cursor(
                one={
                    "operation_count": 2,
                    "incurred_total": 110,
                    "all_succeeded": True,
                    "has_submit_unknown": False,
                }
            ),
            _Cursor(one={"id": FULL_AI_RUN_ID}),
        ]
    )
    PostgresPaidOperationLedger(connection).settle_run(
        workspace_id=WORKSPACE_ID,
        full_ai_run_id=FULL_AI_RUN_ID,
        expected_candidate_count=2,
        now=NOW,
    )
    settle_sql, settle_parameters = connection.calls[2]
    assert "billing_status = 'settled'" in settle_sql
    assert "authorized_amount_minor >= %s" in settle_sql
    assert settle_parameters[0] == 110

    unknown_connection = _Connection(
        [
            _Cursor(one={"authorized_amount_minor": 120, "candidate_count": 2}),
            _Cursor(
                one={
                    "operation_count": 2,
                    "incurred_total": 60,
                    "all_succeeded": False,
                    "has_submit_unknown": True,
                }
            ),
        ]
    )
    with pytest.raises(PaidOperationStateError) as caught:
        PostgresPaidOperationLedger(unknown_connection).settle_run(
            workspace_id=WORKSPACE_ID,
            full_ai_run_id=FULL_AI_RUN_ID,
            expected_candidate_count=2,
            now=NOW,
        )
    assert caught.value.code == "FULL_AI_RECONCILIATION_REQUIRED"
