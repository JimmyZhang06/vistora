from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from framefactory.ports import QueueMessage
from framefactory.runtime import (
    ExecutionState,
    Lease,
    PermanentStepError,
    RetryableStepError,
    RetryPolicy,
    ReviewDecision,
    RunStep,
    Scheduler,
    WorkerRuntime,
)
from framefactory.runtime.state_machine import start_step
from framefactory.steps import StepContext, StepRegistry, StepResult
from framefactory.testing import InMemoryQueue, InMemoryRunStore, ManualClock


@dataclass
class _ScriptedStep:
    step_type: str
    outcomes: list[object]
    calls: int = 0

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, StepResult)
        return outcome


@dataclass
class _CancellingStep:
    step_type: str
    scheduler: Scheduler
    calls: int = 0

    async def execute(self, context: StepContext) -> StepResult:
        self.calls += 1
        self.scheduler.cancel_run(context.workspace_id, context.run_id)
        await context.checkpoint()
        return StepResult()


@dataclass
class _ApprovalRecordingStep:
    step_type: str
    approved_dependency_step_ids: tuple[str, ...] = ()
    approved_dependency_reviews: object | None = None

    async def execute(self, context: StepContext) -> StepResult:
        self.approved_dependency_step_ids = context.approved_dependency_step_ids
        self.approved_dependency_reviews = context.approved_dependency_reviews
        return StepResult()


class _RejectingRecoveryQueue(InMemoryQueue):
    def enqueue(self, message, available_at):
        del message, available_at
        raise ValueError("stale idempotency fingerprint")


class SchedulerEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(datetime(2026, 8, 15, tzinfo=UTC))
        self.store = InMemoryRunStore()
        self.queue = InMemoryQueue()
        self.scheduler = Scheduler(store=self.store, queue=self.queue, clock=self.clock)

    def run_worker(self, worker: WorkerRuntime, queue_name: str = "run-steps") -> bool:
        return asyncio.run(
            worker.process_one(queue_name=queue_name, worker_id="worker-1")
        )

    def test_create_process_review_dependency_and_duplicate_delivery(self) -> None:
        draft = _ScriptedStep(
            "writing.compose",
            [StepResult(output_summary={"draft": "ready"}, requires_review=True)],
        )
        render = _ScriptedStep(
            "render.compose", [StepResult(output_summary={"video": "ok"})]
        )
        runtime = WorkerRuntime(
            scheduler=self.scheduler,
            steps=StepRegistry((draft, render)),
        )
        definitions = (
            RunStep(
                key="draft",
                step_type="writing.compose",
                input_snapshot={"topic": "recovery"},
                queue_name="run-steps",
            ),
            RunStep(
                key="render",
                step_type="render.compose",
                input_snapshot={"format": "vertical"},
                dependencies=("draft",),
                queue_name="run-steps",
            ),
        )

        created = self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-review",
            input_snapshot={"topic": "recovery"},
            steps=definitions,
        )
        # A repeated API request can republish work but must not execute it twice.
        repeated = self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-review",
            input_snapshot={"topic": "recovery"},
            steps=definitions,
        )
        self.assertEqual(created.run_id, repeated.run_id)
        self.assertEqual(2, self.queue.pending_count("run-steps"))

        self.assertTrue(self.run_worker(runtime))
        draft_record = self.store.get_step("workspace", "run-review:draft")
        self.assertEqual(ExecutionState.AWAITING_REVIEW, draft_record.status)
        self.assertTrue(draft_record.review_required)
        self.assertEqual(1, draft.calls)

        # The duplicate delivery is acknowledged from durable state, not rerun.
        self.assertTrue(self.run_worker(runtime))
        self.assertEqual(1, draft.calls)
        self.assertEqual(0, render.calls)

        self.scheduler.review(
            workspace_id="workspace",
            step_id="run-review:draft",
            decision=ReviewDecision.APPROVE,
            actor_id="reviewer",
        )
        self.assertTrue(self.run_worker(runtime))
        self.assertEqual(1, render.calls)
        self.assertEqual(
            ExecutionState.SUCCEEDED,
            self.store.get_run("workspace", "run-review").status,
        )
        event_types = [event["type"] for event in self.store.events]
        self.assertIn("run.graph_materialized", event_types)
        self.assertIn("step.started", event_types)
        self.assertIn("step.awaiting_review", event_types)
        self.assertIn("review.recorded", event_types)
        self.assertIn("step.succeeded", event_types)
        self.assertIn("run.succeeded", event_types)
        sequences = [event["sequence"] for event in self.store.events]
        self.assertEqual(list(range(1, len(sequences) + 1)), sequences)
        keys = [event["deduplication_key"] for event in self.store.events]
        self.assertEqual(len(keys), len(set(keys)))

    def test_cross_queue_injection_is_acked_without_claiming_durable_step(self) -> None:
        step = _ScriptedStep("writing.compose", [StepResult()])
        runtime = WorkerRuntime(
            scheduler=self.scheduler,
            steps=StepRegistry((step,)),
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="cross-queue",
            input_snapshot={},
            steps=(
                RunStep(
                    key="write",
                    step_type="writing.compose",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )
        self.queue.enqueue(
            QueueMessage(
                workspace_id="workspace",
                run_id="cross-queue",
                step_id="cross-queue:write",
                queue_name="browser-capture",
            ),
            self.clock.now(),
        )
        self.assertTrue(self.run_worker(runtime, "browser-capture"))
        record = self.store.get_step("workspace", "cross-queue:write")
        self.assertEqual(ExecutionState.QUEUED, record.status)
        self.assertEqual(0, step.calls)
        self.assertEqual(0, self.queue.pending_count("browser-capture"))

    def test_recovery_queue_filter_never_publishes_other_queue(self) -> None:
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="filtered-recovery",
            input_snapshot={},
            steps=(
                RunStep(
                    key="capture",
                    step_type="web.capture.validate",
                    input_snapshot={},
                    queue_name="browser-capture",
                ),
                RunStep(
                    key="write",
                    step_type="writing.compose",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )
        for queue_name in ("browser-capture", "run-steps"):
            delivery = self.queue.reserve(queue_name, "drain", self.clock.now())
            assert delivery is not None
            self.queue.ack(delivery)
        self.scheduler.recover(queue_name="browser-capture")
        self.assertEqual(1, self.queue.pending_count("browser-capture"))
        self.assertEqual(0, self.queue.pending_count("run-steps"))

    def test_approved_review_provenance_is_passed_to_direct_dependency(self) -> None:
        screenshot = _ScriptedStep(
            "web.capture.screenshot", [StepResult(requires_review=True)]
        )
        materialize = _ApprovalRecordingStep("web.materialize")
        runtime = WorkerRuntime(
            scheduler=self.scheduler,
            steps=StepRegistry((screenshot, materialize)),
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="approved-provenance",
            input_snapshot={},
            steps=(
                RunStep(
                    key="screenshot",
                    step_type="web.capture.screenshot",
                    input_snapshot={},
                    queue_name="browser-capture",
                ),
                RunStep(
                    key="materialize",
                    step_type="web.materialize",
                    input_snapshot={},
                    dependencies=("screenshot",),
                    queue_name="run-steps",
                ),
            ),
        )
        self.assertTrue(self.run_worker(runtime, "browser-capture"))
        self.scheduler.review(
            workspace_id="workspace",
            step_id="approved-provenance:screenshot",
            decision=ReviewDecision.APPROVE,
            actor_id="reviewer",
            metadata={"kind": "scope_selection", "selected_page_ids": ["page-01"]},
        )
        self.assertTrue(self.run_worker(runtime, "run-steps"))
        self.assertEqual(
            ("approved-provenance:screenshot",),
            materialize.approved_dependency_step_ids,
        )
        self.assertEqual(
            "scope_selection",
            materialize.approved_dependency_reviews["approved-provenance:screenshot"][
                "kind"
            ],
        )

    def test_retry_release_and_eventual_success(self) -> None:
        flaky = _ScriptedStep(
            "research.collect",
            [
                RetryableStepError("temporary upstream outage"),
                StepResult(output_summary={"sources": 3}),
            ],
        )
        runtime = WorkerRuntime(scheduler=self.scheduler, steps=StepRegistry((flaky,)))
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-retry",
            input_snapshot={},
            steps=(
                RunStep(
                    key="research",
                    step_type="research.collect",
                    input_snapshot={"topic": "leases"},
                    queue_name="run-steps",
                    retry_policy=RetryPolicy(base_delay_seconds=2),
                ),
            ),
        )

        self.assertTrue(self.run_worker(runtime))
        retrying = self.store.get_step("workspace", "run-retry:research")
        self.assertEqual(ExecutionState.RETRYING, retrying.status)
        self.assertFalse(self.run_worker(runtime))

        self.clock.advance(seconds=2)
        self.assertTrue(self.run_worker(runtime))
        self.assertEqual(2, flaky.calls)
        self.assertEqual(
            ExecutionState.SUCCEEDED,
            self.store.get_run("workspace", "run-retry").status,
        )

    def test_retryable_error_honors_provider_retry_floor(self) -> None:
        flaky = _ScriptedStep(
            "research.collect",
            [RetryableStepError("rate limited", retry_after_seconds=30)],
        )
        runtime = WorkerRuntime(scheduler=self.scheduler, steps=StepRegistry((flaky,)))
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-provider-backoff",
            input_snapshot={},
            steps=(
                RunStep(
                    key="research",
                    step_type="research.collect",
                    input_snapshot={"topic": "leases"},
                    queue_name="run-steps",
                    retry_policy=RetryPolicy(base_delay_seconds=2),
                ),
            ),
        )

        self.assertTrue(self.run_worker(runtime))
        retrying = self.store.get_step("workspace", "run-provider-backoff:research")
        self.assertEqual(ExecutionState.RETRYING, retrying.status)
        self.assertEqual(
            self.clock.now() + timedelta(seconds=30), retrying.available_at
        )

    def test_typed_permanent_error_code_and_details_are_persisted(self) -> None:
        operation = _ScriptedStep(
            "media.inventory",
            [
                PermanentStepError(
                    "snapshot has no coverage",
                    code="asset_coverage_insufficient",
                    details={
                        "catalog_snapshot_id": "snapshot-1",
                        "missing_concepts": ["launch"],
                    },
                )
            ],
        )
        runtime = WorkerRuntime(
            scheduler=self.scheduler, steps=StepRegistry((operation,))
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-inventory-error",
            input_snapshot={},
            steps=(
                RunStep(
                    key="inventory",
                    step_type="media.inventory",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )

        self.assertTrue(self.run_worker(runtime))
        failed = self.store.get_step("workspace", "run-inventory-error:inventory")
        self.assertEqual(ExecutionState.FAILED, failed.status)
        self.assertEqual("asset_coverage_insufficient", failed.error.code)
        self.assertEqual("snapshot-1", failed.error.details["catalog_snapshot_id"])

    def test_dynamic_review_requirement_clears_after_successful_revision(self) -> None:
        media = _ScriptedStep(
            "media.select",
            [
                StepResult(output_summary={"missing": 2}, requires_review=True),
                StepResult(output_summary={"selected": 2}),
            ],
        )
        runtime = WorkerRuntime(
            scheduler=self.scheduler,
            steps=StepRegistry((media,)),
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-dynamic-review",
            input_snapshot={"topic": "assets"},
            steps=(
                RunStep(
                    key="assets",
                    step_type="media.select",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )

        self.assertTrue(self.run_worker(runtime))
        self.scheduler.review(
            workspace_id="workspace",
            step_id="run-dynamic-review:assets",
            decision=ReviewDecision.REQUEST_CHANGES,
            actor_id="system:asset-analysis",
        )
        self.clock.advance(seconds=2)
        self.assertTrue(self.run_worker(runtime))

        step = self.store.get_step("workspace", "run-dynamic-review:assets")
        self.assertEqual(ExecutionState.SUCCEEDED, step.status)
        self.assertFalse(step.review_required)

    def test_expired_lease_recovery_requeues_without_reusing_claim(self) -> None:
        operation = _ScriptedStep(
            "media.select", [StepResult(output_summary={"assets": 2})]
        )
        runtime = WorkerRuntime(
            scheduler=self.scheduler, steps=StepRegistry((operation,))
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-recover",
            input_snapshot={},
            steps=(
                RunStep(
                    key="media",
                    step_type="media.select",
                    input_snapshot={},
                    queue_name="run-steps",
                    retry_policy=RetryPolicy(base_delay_seconds=1),
                ),
            ),
        )
        delivery = self.queue.reserve(
            "run-steps",
            "crashed-worker",
            self.clock.now(),
            lease_seconds=1,
        )
        self.assertIsNotNone(delivery)
        queued = self.store.get_step("workspace", "run-recover:media")
        claimed = start_step(
            queued,
            Lease(
                owner="crashed-worker",
                token=delivery.receipt,
                heartbeat_at=self.clock.now(),
                expires_at=self.clock.now() + timedelta(seconds=1),
            ),
            at=self.clock.now(),
        )
        self.store.save_step(claimed, expected_revision=queued.revision)

        self.clock.advance(seconds=1)
        self.scheduler.recover()
        recovered = self.store.get_step("workspace", "run-recover:media")
        self.assertEqual(ExecutionState.RETRYING, recovered.status)
        self.assertEqual("worker_lease_expired", recovered.error.code)

        self.clock.advance(seconds=1)
        # The expired original and recovery publication may both be delivered;
        # only the CAS winner is allowed to execute the operation.
        while self.run_worker(runtime):
            pass
        self.assertEqual(1, operation.calls)
        self.assertEqual(
            2, self.store.get_step("workspace", "run-recover:media").attempt_count
        )

    def test_recovery_isolates_a_stale_queue_idempotency_conflict(self) -> None:
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-stale-queue",
            input_snapshot={},
            steps=(
                RunStep(
                    key="media",
                    step_type="media.select",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )
        delivery = self.queue.reserve(
            "run-steps", "crashed-worker", self.clock.now(), lease_seconds=1
        )
        queued = self.store.get_step("workspace", "run-stale-queue:media")
        claimed = start_step(
            queued,
            Lease(
                owner="crashed-worker",
                token=delivery.receipt,
                heartbeat_at=self.clock.now(),
                expires_at=self.clock.now() + timedelta(seconds=1),
            ),
            at=self.clock.now(),
        )
        self.store.save_step(claimed, expected_revision=queued.revision)
        self.clock.advance(seconds=1)
        self.scheduler.queue = _RejectingRecoveryQueue()

        recovered = self.scheduler.recover()

        self.assertEqual(1, recovered)
        step = self.store.get_step("workspace", "run-stale-queue:media")
        self.assertEqual(ExecutionState.RETRYING, step.status)

    def test_cancel_marks_unstarted_graph_terminal_and_drains_stale_messages(
        self,
    ) -> None:
        operation = _ScriptedStep("quality.evaluate", [StepResult()])
        runtime = WorkerRuntime(
            scheduler=self.scheduler, steps=StepRegistry((operation,))
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-cancel",
            input_snapshot={},
            steps=(
                RunStep(
                    key="qc",
                    step_type="quality.evaluate",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )

        cancelled = self.scheduler.cancel_run("workspace", "run-cancel")
        self.assertEqual(ExecutionState.CANCELLED, cancelled.status)
        self.assertTrue(self.run_worker(runtime))
        self.assertEqual(0, operation.calls)
        self.assertEqual(0, self.queue.pending_count())

    def test_running_cancellation_is_observed_at_a_worker_checkpoint(self) -> None:
        operation = _CancellingStep("audio.synthesize", self.scheduler)
        runtime = WorkerRuntime(
            scheduler=self.scheduler, steps=StepRegistry((operation,))
        )
        self.scheduler.create_run(
            workspace_id="workspace",
            run_id="run-running-cancel",
            input_snapshot={},
            steps=(
                RunStep(
                    key="audio",
                    step_type="audio.synthesize",
                    input_snapshot={},
                    queue_name="run-steps",
                ),
            ),
        )

        self.assertTrue(self.run_worker(runtime))
        self.assertEqual(1, operation.calls)
        self.assertEqual(
            ExecutionState.CANCELLED,
            self.store.get_step("workspace", "run-running-cancel:audio").status,
        )
        self.assertEqual(
            ExecutionState.CANCELLED,
            self.store.get_run("workspace", "run-running-cancel").status,
        )


if __name__ == "__main__":
    unittest.main()
