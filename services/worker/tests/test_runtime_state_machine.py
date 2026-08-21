from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from framefactory.runtime import (
    ExecutionState,
    InvalidTransition,
    Lease,
    RetryPolicy,
    ReviewDecision,
    StepError,
    StepRecord,
)
from framefactory.runtime.state_machine import (
    aggregate_run_state,
    cancel_running_step,
    fail_step,
    finish_step,
    request_cancellation,
    review_step,
    start_step,
)


class RuntimeStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def step(self, **overrides: object) -> StepRecord:
        values: dict[str, object] = {
            "workspace_id": "workspace",
            "run_id": "run",
            "step_id": "step",
            "step_key": "research",
            "step_type": "research.collect",
            "input_snapshot": {"topic": "leases"},
            "available_at": self.now,
            "created_at": self.now,
            "updated_at": self.now,
            "retry_policy": RetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=5),
        }
        values.update(overrides)
        return StepRecord(**values)  # type: ignore[arg-type]

    def lease(self, token: str = "lease-1", at: datetime | None = None) -> Lease:
        heartbeat_at = at or self.now
        return Lease("worker", token, heartbeat_at + timedelta(seconds=30), heartbeat_at)

    def test_retry_uses_capped_exponential_backoff_then_succeeds(self) -> None:
        first = start_step(self.step(), self.lease(), at=self.now)
        retrying = fail_step(
            first,
            StepError("TRANSIENT", "try again", retryable=True),
            retry=True,
            at=self.now,
        )

        self.assertEqual(ExecutionState.RETRYING, retrying.status)
        self.assertEqual(self.now + timedelta(seconds=2), retrying.available_at)
        with self.assertRaises(InvalidTransition):
            start_step(retrying, self.lease("too-early"), at=self.now)

        second = start_step(retrying, self.lease("lease-2", retrying.available_at), at=retrying.available_at)
        completed = finish_step(second, output_summary={"sources": 3}, at=retrying.available_at)

        self.assertEqual(2, completed.attempt_count)
        self.assertEqual(ExecutionState.SUCCEEDED, completed.status)
        self.assertEqual(retrying.available_at, completed.completed_at)

    def test_maximum_attempts_turns_retryable_error_into_terminal_failure(self) -> None:
        record = self.step(retry_policy=RetryPolicy(max_attempts=1))
        running = start_step(record, self.lease(), at=self.now)

        failed = fail_step(
            running,
            StepError("UPSTREAM_TIMEOUT", "timeout", retryable=True),
            retry=True,
            at=self.now,
        )

        self.assertEqual(ExecutionState.FAILED, failed.status)
        self.assertEqual(self.now, failed.completed_at)

    def test_running_cancellation_is_cooperative_and_queued_cancellation_is_immediate(self) -> None:
        queued_cancelled = request_cancellation(self.step(), at=self.now)
        running = start_step(self.step(step_id="running"), self.lease(), at=self.now)
        requested = request_cancellation(running, at=self.now)

        self.assertEqual(ExecutionState.CANCELLED, queued_cancelled.status)
        self.assertEqual(ExecutionState.RUNNING, requested.status)
        self.assertTrue(requested.cancellation_requested)
        cancelled = cancel_running_step(requested, at=self.now + timedelta(seconds=1))
        self.assertEqual(ExecutionState.CANCELLED, cancelled.status)
        self.assertIsNone(cancelled.lease)

    def test_review_can_approve_reject_or_request_a_retry(self) -> None:
        running = start_step(self.step(review_required=True), self.lease(), at=self.now)
        awaiting = finish_step(running, at=self.now)

        self.assertEqual(ExecutionState.AWAITING_REVIEW, awaiting.status)
        approved = review_step(
            awaiting,
            ReviewDecision.APPROVE,
            actor_id="reviewer",
            comment="looks good",
            at=self.now,
        )
        self.assertEqual(ExecutionState.SUCCEEDED, approved.status)

        rejected = review_step(
            awaiting,
            ReviewDecision.REJECT,
            actor_id="reviewer",
            comment="unsafe source",
            at=self.now,
        )
        self.assertEqual(ExecutionState.FAILED, rejected.status)
        self.assertFalse(rejected.error.retryable)  # type: ignore[union-attr]

        changes = review_step(
            awaiting,
            ReviewDecision.REQUEST_CHANGES,
            actor_id="reviewer",
            comment="add citation",
            at=self.now,
        )
        self.assertEqual(ExecutionState.RETRYING, changes.status)
        self.assertGreater(changes.available_at, self.now)

    def test_invalid_transition_cannot_reopen_terminal_step(self) -> None:
        running = start_step(self.step(), self.lease(), at=self.now)
        succeeded = finish_step(running, at=self.now)

        with self.assertRaises(InvalidTransition):
            start_step(succeeded, self.lease("new"), at=self.now)

    def test_failed_branch_does_not_mutate_successful_sibling(self) -> None:
        succeeded = finish_step(start_step(self.step(step_id="one"), self.lease(), at=self.now), at=self.now)
        failed = fail_step(
            start_step(self.step(step_id="two"), self.lease("two"), at=self.now),
            StepError("BAD_INPUT", "isolated", retryable=False),
            retry=False,
            at=self.now,
        )

        self.assertEqual(ExecutionState.FAILED, aggregate_run_state((succeeded, failed)))
        self.assertEqual(ExecutionState.SUCCEEDED, succeeded.status)
        self.assertIsNone(succeeded.error)


if __name__ == "__main__":
    unittest.main()
