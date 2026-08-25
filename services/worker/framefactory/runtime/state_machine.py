"""Pure state transitions and run-state aggregation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime

from .model import (
    ExecutionState,
    InvalidTransition,
    Lease,
    ReviewDecision,
    ReviewRecord,
    StepError,
    StepRecord,
    require_aware,
)

ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.QUEUED: frozenset(
        {ExecutionState.RUNNING, ExecutionState.CANCELLED}
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.AWAITING_REVIEW,
            ExecutionState.RETRYING,
            ExecutionState.SUCCEEDED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.AWAITING_REVIEW: frozenset(
        {
            ExecutionState.SUCCEEDED,
            ExecutionState.RETRYING,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.RETRYING: frozenset(
        {ExecutionState.RUNNING, ExecutionState.FAILED, ExecutionState.CANCELLED}
    ),
    ExecutionState.SUCCEEDED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
}


def require_transition(source: ExecutionState, target: ExecutionState) -> None:
    if target not in ALLOWED_TRANSITIONS[source]:
        raise InvalidTransition(
            f"cannot transition from {source.value} to {target.value}"
        )


def start_step(record: StepRecord, lease: Lease, *, at: datetime) -> StepRecord:
    require_transition(record.status, ExecutionState.RUNNING)
    at = require_aware(at, field_name="at")
    if record.available_at > at:
        raise InvalidTransition("step is not available yet")
    if record.cancellation_requested:
        raise InvalidTransition("step has a pending cancellation request")
    if record.attempt_count >= record.max_attempts:
        raise InvalidTransition("step has exhausted its attempts")
    return replace(
        record,
        status=ExecutionState.RUNNING,
        attempt_count=record.attempt_count + 1,
        lease=lease,
        error=None,
        started_at=record.started_at or at,
        completed_at=None,
        updated_at=at,
    )


def heartbeat_step(record: StepRecord, lease: Lease, *, at: datetime) -> StepRecord:
    if record.status is not ExecutionState.RUNNING or record.lease is None:
        raise InvalidTransition("only a running step can heartbeat")
    if lease.token != record.lease.token or lease.owner != record.lease.owner:
        raise InvalidTransition("heartbeat lease identity does not match")
    return replace(record, lease=lease, updated_at=require_aware(at, field_name="at"))


def finish_step(
    record: StepRecord,
    *,
    artifacts: Iterable[object] = (),
    output_summary: object | None = None,
    review_required: bool | None = None,
    at: datetime,
) -> StepRecord:
    needs_review = (
        record.review_required if review_required is None else review_required
    )
    target = (
        ExecutionState.AWAITING_REVIEW if needs_review else ExecutionState.SUCCEEDED
    )
    require_transition(record.status, target)
    at = require_aware(at, field_name="at")
    review = ReviewRecord(requested_at=at) if needs_review else record.review
    return replace(
        record,
        status=target,
        review_required=needs_review,
        lease=None,
        output_artifacts=tuple(artifacts),
        output_summary=output_summary,
        review=review,
        completed_at=at if target.terminal else None,
        updated_at=at,
    )


def fail_step(
    record: StepRecord, error: StepError, *, retry: bool, at: datetime
) -> StepRecord:
    at = require_aware(at, field_name="at")
    can_retry = retry and record.attempt_count < record.max_attempts
    target = ExecutionState.RETRYING if can_retry else ExecutionState.FAILED
    require_transition(record.status, target)
    available_at = (
        at + record.retry_policy.delay_after(record.attempt_count)
        if can_retry
        else record.available_at
    )
    return replace(
        record,
        status=target,
        lease=None,
        error=error,
        available_at=available_at,
        completed_at=at if target.terminal else None,
        updated_at=at,
    )


def request_cancellation(record: StepRecord, *, at: datetime) -> StepRecord:
    at = require_aware(at, field_name="at")
    if record.terminal:
        return record
    if record.status is ExecutionState.RUNNING:
        return replace(
            record,
            cancellation_requested_at=record.cancellation_requested_at or at,
            updated_at=at,
        )
    require_transition(record.status, ExecutionState.CANCELLED)
    return replace(
        record,
        status=ExecutionState.CANCELLED,
        lease=None,
        cancellation_requested_at=record.cancellation_requested_at or at,
        completed_at=at,
        updated_at=at,
    )


def cancel_running_step(record: StepRecord, *, at: datetime) -> StepRecord:
    require_transition(record.status, ExecutionState.CANCELLED)
    at = require_aware(at, field_name="at")
    return replace(
        record,
        status=ExecutionState.CANCELLED,
        lease=None,
        cancellation_requested_at=record.cancellation_requested_at or at,
        completed_at=at,
        updated_at=at,
    )


def review_step(
    record: StepRecord,
    decision: ReviewDecision,
    *,
    actor_id: str,
    comment: str | None,
    at: datetime,
    metadata: Mapping[str, object] | None = None,
) -> StepRecord:
    if record.status is not ExecutionState.AWAITING_REVIEW:
        raise InvalidTransition("only an awaiting_review step can be reviewed")
    at = require_aware(at, field_name="at")
    if decision is ReviewDecision.APPROVE:
        target = ExecutionState.SUCCEEDED
        error = record.error
    elif (
        decision is ReviewDecision.REQUEST_CHANGES
        and record.attempt_count < record.max_attempts
    ):
        target = ExecutionState.RETRYING
        error = StepError(
            "review_changes_requested", comment or "changes requested", retryable=True
        )
    else:
        target = ExecutionState.FAILED
        error = StepError(
            "review_rejected", comment or "review rejected", retryable=False
        )
    require_transition(record.status, target)
    review = ReviewRecord(
        decision=decision,
        actor_id=actor_id,
        comment=comment,
        requested_at=record.review.requested_at if record.review else None,
        decided_at=at,
        metadata=metadata or (record.review.metadata if record.review else {}),
    )
    available_at = (
        at + record.retry_policy.delay_after(record.attempt_count)
        if target is ExecutionState.RETRYING
        else record.available_at
    )
    return replace(
        record,
        status=target,
        review=review,
        error=error,
        available_at=available_at,
        completed_at=at if target.terminal else None,
        updated_at=at,
    )


def aggregate_run_state(steps: Iterable[StepRecord]) -> ExecutionState:
    """Derive a run state deterministically from all of its persisted steps."""

    states = [step.status for step in steps]
    if not states:
        return ExecutionState.QUEUED
    if all(state is ExecutionState.SUCCEEDED for state in states):
        return ExecutionState.SUCCEEDED
    if any(state is ExecutionState.FAILED for state in states):
        return ExecutionState.FAILED
    if all(state.terminal for state in states) and any(
        state is ExecutionState.CANCELLED for state in states
    ):
        return ExecutionState.CANCELLED
    for state in (
        ExecutionState.AWAITING_REVIEW,
        ExecutionState.RUNNING,
        ExecutionState.RETRYING,
        ExecutionState.QUEUED,
    ):
        if state in states:
            return state
    return ExecutionState.CANCELLED
