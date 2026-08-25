"""Durable workflow scheduling and at-least-once worker execution.

The scheduler only publishes identifiers.  Every decision is made again from
the durable run store, which makes duplicate queue messages and process restarts
safe.  The worker claims a step with compare-and-swap before invoking user code
and persists the result before acknowledging the queue delivery.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta

from framefactory.ports import (
    Clock,
    DeliveryLeaseLostError,
    Queue,
    QueueDelivery,
    QueueMessage,
    RevisionConflictError,
    RunStore,
)
from framefactory.steps.model import ArtifactRef, InputSnapshot, StepContext, StepResult
from framefactory.steps.registry import CapabilityRegistry, StepRegistry

from .model import (
    CancellationToken,
    ExecutionState,
    Lease,
    LeaseLost,
    PermanentStepError,
    RetryableStepError,
    ReviewDecision,
    RunRecord,
    RunStep,
    StepCancelled,
    StepError,
    StepRecord,
)
from .state_machine import (
    aggregate_run_state,
    cancel_running_step,
    fail_step,
    finish_step,
    heartbeat_step,
    request_cancellation,
    review_step,
    start_step,
)

logger = logging.getLogger("framefactory.worker.scheduler")


class Scheduler:
    """Create workflow snapshots and coordinate durable state transitions."""

    def __init__(self, *, store: RunStore, queue: Queue, clock: Clock) -> None:
        self.store = store
        self.queue = queue
        self.clock = clock

    def create_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        input_snapshot: object,
        steps: Sequence[RunStep],
    ) -> RunRecord:
        """Idempotently persist a run graph and publish its root steps."""

        definitions = tuple(steps)
        self._validate_graph(definitions)
        now = self.clock.now()
        candidate = RunRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            input_snapshot=input_snapshot,
            created_at=now,
            updated_at=now,
        )
        run = self.store.get_run(workspace_id, run_id)
        if run is None:
            run = self.store.save_run(candidate, expected_revision=None)
        elif run.input_snapshot != candidate.input_snapshot:
            raise ValueError(f"run already exists with different input: {run_id}")

        existing_by_key = {
            record.step_key: record
            for record in self.store.list_steps(workspace_id, run_id)
        }
        expected_keys = {definition.key for definition in definitions}
        unexpected = set(existing_by_key) - expected_keys
        if unexpected:
            raise ValueError(
                f"run already exists with different steps: {sorted(unexpected)!r}"
            )

        for definition in definitions:
            candidate_step = self._step_record(workspace_id, run_id, definition, now)
            existing = existing_by_key.get(definition.key)
            if existing is None:
                self.store.save_step(candidate_step, expected_revision=None)
            elif not self._same_definition(existing, candidate_step):
                raise ValueError(
                    f"run step already exists with different definition: {definition.key}"
                )

        self.store.append_event(
            workspace_id,
            run_id,
            event_type="run.graph_materialized",
            deduplication_key="worker:graph-materialized",
            payload={"step_count": len(definitions)},
            occurred_at=now,
        )

        self.schedule_ready(workspace_id, run_id)
        return self._sync_run(workspace_id, run_id)

    def schedule_ready(
        self,
        workspace_id: str,
        run_id: str,
        *,
        queue_name: str | None = None,
    ) -> int:
        """Publish all due steps whose dependencies have succeeded.

        Publishing may happen more than once after a crash.  Queue messages carry
        a stable idempotency key and the worker's CAS claim is the final guard.
        """

        now = self.clock.now()
        published = 0
        changed = False
        # Propagate an upstream terminal failure through every dependent level.
        # Refreshing after each wave keeps every write CAS-safe.
        while True:
            steps = tuple(self.store.list_steps(workspace_id, run_id))
            by_key = {step.step_key: step for step in steps}
            blocked = [
                step
                for step in steps
                if step.status in {ExecutionState.QUEUED, ExecutionState.RETRYING}
                and (queue_name is None or step.queue_name == queue_name)
                and any(
                    by_key[key].status
                    in {ExecutionState.FAILED, ExecutionState.CANCELLED}
                    for key in step.dependencies
                )
            ]
            if not blocked:
                break
            saved_in_wave = False
            for snapshot in blocked:
                cancelled = request_cancellation(snapshot, at=now)
                try:
                    self.store.save_step(cancelled, expected_revision=snapshot.revision)
                    changed = True
                    saved_in_wave = True
                except RevisionConflictError:
                    pass
            if not saved_in_wave:
                break

        steps = tuple(self.store.list_steps(workspace_id, run_id))
        by_key = {step.step_key: step for step in steps}
        for snapshot in steps:
            if snapshot.status not in {ExecutionState.QUEUED, ExecutionState.RETRYING}:
                continue
            if queue_name is not None and snapshot.queue_name != queue_name:
                continue
            dependencies = [by_key[key] for key in snapshot.dependencies]
            if snapshot.available_at > now or not all(
                dependency.status is ExecutionState.SUCCEEDED
                for dependency in dependencies
            ):
                continue
            self.queue.enqueue(
                QueueMessage(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    step_id=snapshot.step_id,
                    queue_name=snapshot.queue_name,
                    idempotency_key=(
                        f"{workspace_id}:{snapshot.step_id}:{snapshot.attempt_count + 1}"
                    ),
                ),
                snapshot.available_at,
            )
            published += 1
        if changed:
            self._sync_run(workspace_id, run_id)
        return published

    def review(
        self,
        *,
        workspace_id: str,
        step_id: str,
        decision: ReviewDecision,
        actor_id: str,
        comment: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> StepRecord:
        record = self._require_step(workspace_id, step_id)
        updated = review_step(
            record,
            decision,
            actor_id=actor_id,
            comment=comment,
            at=self.clock.now(),
            metadata=metadata,
        )
        saved = self.store.save_step(updated, expected_revision=record.revision)
        if saved.status is ExecutionState.RETRYING:
            self._enqueue(saved)
        elif saved.status is ExecutionState.SUCCEEDED:
            self.schedule_ready(saved.workspace_id, saved.run_id)
        self._sync_run(saved.workspace_id, saved.run_id)
        return saved

    def cancel_run(self, workspace_id: str, run_id: str) -> RunRecord:
        now = self.clock.now()
        run = self._require_run(workspace_id, run_id)
        if run.terminal:
            return run
        requested = replace(
            run,
            cancellation_requested_at=run.cancellation_requested_at or now,
            updated_at=now,
        )
        self.store.save_run(requested, expected_revision=run.revision)
        for record in self.store.list_steps(workspace_id, run_id):
            updated = request_cancellation(record, at=now)
            if updated != record:
                try:
                    self.store.save_step(updated, expected_revision=record.revision)
                except RevisionConflictError:
                    # A running worker will observe the run-level cancellation too.
                    pass
        return self._sync_run(workspace_id, run_id)

    def recover(self, *, queue_name: str | None = None) -> int:
        """Recover due work and expired worker leases after a restart."""

        now = self.clock.now()
        touched_runs: set[tuple[str, str]] = set()
        recovered = 0
        for snapshot in self.store.list_recoverable(now):
            if queue_name is not None and snapshot.queue_name != queue_name:
                continue
            current = self.store.get_step(snapshot.workspace_id, snapshot.step_id)
            if current is None:
                continue
            if current.status is ExecutionState.RUNNING:
                if current.lease is None or not current.lease.expired(now):
                    continue
                error = StepError(
                    "worker_lease_expired",
                    "worker lease expired before the step completed",
                    retryable=True,
                )
                updated = fail_step(current, error, retry=True, at=now)
                try:
                    saved = self.store.save_step(
                        updated, expected_revision=current.revision
                    )
                except RevisionConflictError:
                    continue
                if saved.status is ExecutionState.RETRYING:
                    try:
                        self._enqueue(saved)
                    except (RuntimeError, ValueError) as exc:
                        logger.error(
                            "recovery enqueue isolated workspace=%s run=%s step=%s error=%s",
                            saved.workspace_id,
                            saved.run_id,
                            saved.step_id,
                            exc,
                        )
                recovered += 1
            touched_runs.add((current.workspace_id, current.run_id))
        for workspace_id, run_id in touched_runs:
            try:
                recovered += self.schedule_ready(
                    workspace_id,
                    run_id,
                    queue_name=queue_name,
                )
            except (RuntimeError, ValueError) as exc:
                logger.error(
                    "run recovery isolated workspace=%s run=%s error=%s",
                    workspace_id,
                    run_id,
                    exc,
                )
            self._sync_run(workspace_id, run_id)
        return recovered

    def _sync_run(self, workspace_id: str, run_id: str) -> RunRecord:
        for _ in range(3):
            run = self._require_run(workspace_id, run_id)
            steps = tuple(self.store.list_steps(workspace_id, run_id))
            status = aggregate_run_state(steps)
            now = self.clock.now()
            started = run.started_at
            if started is None and any(step.attempt_count > 0 for step in steps):
                started = min(
                    (step.started_at for step in steps if step.started_at is not None),
                    default=now,
                )
            completed = (
                now
                if status.terminal and run.completed_at is None
                else run.completed_at
            )
            if not status.terminal:
                completed = None
            updated = replace(
                run,
                status=status,
                started_at=started,
                completed_at=completed,
                updated_at=now,
            )
            if updated == run:
                return run
            try:
                return self.store.save_run(updated, expected_revision=run.revision)
            except RevisionConflictError:
                continue
        raise RevisionConflictError(
            f"could not synchronize run after repeated conflicts: {run_id}"
        )

    def _enqueue(self, step: StepRecord) -> None:
        self.queue.enqueue(
            QueueMessage(
                workspace_id=step.workspace_id,
                run_id=step.run_id,
                step_id=step.step_id,
                queue_name=step.queue_name,
                idempotency_key=f"{step.workspace_id}:{step.step_id}:{step.attempt_count + 1}",
            ),
            step.available_at,
        )

    @staticmethod
    def _validate_graph(steps: Sequence[RunStep]) -> None:
        by_key = {step.key: step for step in steps}
        if len(by_key) != len(steps):
            raise ValueError("run step keys must be unique")
        for step in steps:
            missing = set(step.dependencies) - set(by_key)
            if missing:
                raise ValueError(
                    f"step {step.key} has unknown dependencies: {sorted(missing)!r}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError(f"run graph contains a dependency cycle at {key}")
            if key in visited:
                return
            visiting.add(key)
            for dependency in by_key[key].dependencies:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in by_key:
            visit(key)

    @staticmethod
    def _step_record(
        workspace_id: str, run_id: str, step: RunStep, now: datetime
    ) -> StepRecord:
        return StepRecord(
            workspace_id=workspace_id,
            run_id=run_id,
            step_id=f"{run_id}:{step.key}",
            step_key=step.key,
            step_type=step.step_type,
            input_snapshot=step.input_snapshot,
            dependencies=step.dependencies,
            queue_name=step.queue_name,
            required_capabilities=step.required_capabilities,
            priority=step.priority,
            retry_policy=step.retry_policy,
            review_required=step.review_required,
            static_review_required=step.review_required,
            available_at=now,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _same_definition(left: StepRecord, right: StepRecord) -> bool:
        names = (
            "step_id",
            "step_key",
            "step_type",
            "input_snapshot",
            "dependencies",
            "queue_name",
            "required_capabilities",
            "priority",
            "retry_policy",
            "static_review_required",
        )
        return all(getattr(left, name) == getattr(right, name) for name in names)

    def _require_run(self, workspace_id: str, run_id: str) -> RunRecord:
        run = self.store.get_run(workspace_id, run_id)
        if run is None:
            raise LookupError(f"run not found: {workspace_id}/{run_id}")
        return run

    def _require_step(self, workspace_id: str, step_id: str) -> StepRecord:
        step = self.store.get_step(workspace_id, step_id)
        if step is None:
            raise LookupError(f"step not found: {workspace_id}/{step_id}")
        return step


class WorkerRuntime:
    """Reserve and execute one durable step at a time."""

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        steps: StepRegistry,
        capabilities: CapabilityRegistry | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.scheduler = scheduler
        self.steps = steps
        self.capabilities = capabilities
        self.lease_seconds = lease_seconds

    async def process_one(self, *, queue_name: str, worker_id: str) -> bool:
        now = self.scheduler.clock.now()
        delivery = self.scheduler.queue.reserve(
            queue_name,
            worker_id,
            now,
            lease_seconds=self.lease_seconds,
        )
        if delivery is None:
            return False
        await self._process_delivery(delivery, reserved_queue_name=queue_name)
        return True

    async def _process_delivery(
        self,
        delivery: QueueDelivery,
        *,
        reserved_queue_name: str,
    ) -> None:
        message = delivery.message
        record = self.scheduler.store.get_step(message.workspace_id, message.step_id)
        if record is None or record.run_id != message.run_id:
            self._safe_ack(delivery)
            return
        # Fence cross-queue injection before any durable state transition. A
        # message physically placed on browser-capture cannot run a normal step
        # merely by naming a valid step_id (or vice versa).
        if (
            message.queue_name != reserved_queue_name
            or record.queue_name != reserved_queue_name
        ):
            self._safe_ack(delivery)
            return
        if record.status not in {ExecutionState.QUEUED, ExecutionState.RETRYING}:
            self._safe_ack(delivery)
            return
        if record.available_at > self.scheduler.clock.now():
            try:
                self.scheduler.queue.release(delivery, record.available_at)
            except DeliveryLeaseLostError:
                pass
            return
        run = self.scheduler.store.get_run(record.workspace_id, record.run_id)
        if run is None:
            self._safe_ack(delivery)
            return
        if run.cancellation_requested_at is not None or record.cancellation_requested:
            cancelled = request_cancellation(record, at=self.scheduler.clock.now())
            try:
                self.scheduler.store.save_step(
                    cancelled, expected_revision=record.revision
                )
            except RevisionConflictError:
                pass
            self._safe_ack(delivery)
            self.scheduler._sync_run(record.workspace_id, record.run_id)
            return
        dependencies = {
            step.step_key: step
            for step in self.scheduler.store.list_steps(
                record.workspace_id, record.run_id
            )
        }
        if not all(
            dependencies.get(key) is not None
            and dependencies[key].status is ExecutionState.SUCCEEDED
            for key in record.dependencies
        ):
            self._safe_ack(delivery)
            return

        now = self.scheduler.clock.now()
        lease = Lease(
            owner=delivery.worker_id,
            token=delivery.receipt,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=self.lease_seconds),
        )
        try:
            running = start_step(record, lease, at=now)
            running = self.scheduler.store.save_step(
                running, expected_revision=record.revision
            )
            self.scheduler._sync_run(running.workspace_id, running.run_id)
        except RevisionConflictError:
            self._safe_ack(delivery)
            return

        holder = [running]

        def cancellation_requested() -> bool:
            current = self.scheduler.store.get_step(
                running.workspace_id, running.step_id
            )
            current_run = self.scheduler.store.get_run(
                running.workspace_id, running.run_id
            )
            return (
                current is None
                or current.cancellation_requested
                or current.status is not ExecutionState.RUNNING
                or current.lease is None
                or current.lease.token != delivery.receipt
                or (
                    current_run is not None
                    and current_run.cancellation_requested_at is not None
                )
            )

        token = CancellationToken(cancellation_requested)

        def heartbeat() -> None:
            current = self.scheduler.store.get_step(
                running.workspace_id, running.step_id
            )
            now = self.scheduler.clock.now()
            if (
                current is None
                or current.status is not ExecutionState.RUNNING
                or current.lease is None
                or current.lease.token != delivery.receipt
                or current.lease.expired(now)
            ):
                raise LeaseLost(f"lease lost for step: {running.step_id}")
            renewed = Lease(
                owner=current.lease.owner,
                token=current.lease.token,
                heartbeat_at=now,
                expires_at=now + timedelta(seconds=self.lease_seconds),
            )
            updated = heartbeat_step(current, renewed, at=now)
            try:
                holder[0] = self.scheduler.store.save_step(
                    updated,
                    expected_revision=current.revision,
                )
            except RevisionConflictError as exc:
                raise LeaseLost(
                    f"lease concurrently changed for step: {running.step_id}"
                ) from exc

        input_artifacts = self._dependency_artifacts(running)
        context = StepContext(
            workspace_id=running.workspace_id,
            run_id=running.run_id,
            step_id=running.step_id,
            input_snapshot=InputSnapshot.capture(running.input_snapshot),
            cancellation_token=token,
            heartbeat=heartbeat,
            capabilities=self.capabilities,
            input_artifacts=input_artifacts,
            attempt=running.attempt_count,
            maximum_attempts=running.max_attempts,
            review_feedback=(
                running.review.comment
                if running.review is not None and running.review.comment
                else None
            ),
            approved_dependency_step_ids=tuple(
                dependency.step_id
                for key in running.dependencies
                if (dependency := dependencies.get(key)) is not None
                and dependency.status is ExecutionState.SUCCEEDED
                and dependency.review is not None
                and dependency.review.decision is ReviewDecision.APPROVE
            ),
            approved_dependency_reviews={
                dependency.step_id: dependency.review.metadata
                for key in running.dependencies
                if (dependency := dependencies.get(key)) is not None
                and dependency.status is ExecutionState.SUCCEEDED
                and dependency.review is not None
                and dependency.review.decision is ReviewDecision.APPROVE
            },
        )

        async def maintain_lease() -> None:
            interval = max(0.1, self.lease_seconds / 3)
            while True:
                await asyncio.sleep(interval)
                now = self.scheduler.clock.now()
                try:
                    self.scheduler.queue.renew(
                        delivery,
                        now,
                        lease_seconds=self.lease_seconds,
                    )
                except DeliveryLeaseLostError as exc:
                    raise LeaseLost(
                        f"delivery lease lost for step: {running.step_id}"
                    ) from exc
                heartbeat()

        lease_maintainer = asyncio.create_task(maintain_lease())

        try:
            implementation = self.steps.resolve(running.step_type)
            result = implementation.execute(context)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, StepResult):
                raise TypeError(
                    f"step {running.step_type} returned {type(result).__name__}, "
                    "expected StepResult"
                )
            # A step implementation is expected to checkpoint during long work,
            # but the runtime always fences the final write as well.
            await context.checkpoint()
            if lease_maintainer.done():
                await lease_maintainer
            lease_maintainer.cancel()
            with suppress(asyncio.CancelledError):
                await lease_maintainer
            current = holder[0]
            completed = finish_step(
                current,
                artifacts=result.artifacts,
                output_summary=result.output_summary,
                review_required=(
                    current.static_review_required or result.requires_review
                ),
                at=self.scheduler.clock.now(),
            )
            saved = self.scheduler.store.save_step(
                completed, expected_revision=current.revision
            )
        except StepCancelled:
            self._cancel_claimed(running, delivery)
            return
        except LeaseLost:
            # The durable running claim is intentionally left for lease recovery.
            self.scheduler.recover(queue_name=reserved_queue_name)
            return
        except RevisionConflictError:
            current = self.scheduler.store.get_step(
                running.workspace_id, running.step_id
            )
            if current is not None and current.cancellation_requested:
                self._cancel_claimed(current, delivery)
            else:
                self._safe_ack(delivery)
            return
        except Exception as exc:  # noqa: BLE001 - step failures become durable state
            self._fail_claimed(running, delivery, exc)
            return
        finally:
            if not lease_maintainer.done():
                lease_maintainer.cancel()
            with suppress(asyncio.CancelledError, LeaseLost):
                await lease_maintainer

        self._safe_ack(delivery)
        if saved.status is ExecutionState.SUCCEEDED:
            self.scheduler.schedule_ready(
                saved.workspace_id,
                saved.run_id,
                queue_name=reserved_queue_name,
            )
        self.scheduler._sync_run(saved.workspace_id, saved.run_id)

    def _cancel_claimed(self, claimed: StepRecord, delivery: QueueDelivery) -> None:
        current = self.scheduler.store.get_step(claimed.workspace_id, claimed.step_id)
        if (
            current is not None
            and current.status is ExecutionState.RUNNING
            and current.lease is not None
            and current.lease.token == delivery.receipt
        ):
            cancelled = cancel_running_step(current, at=self.scheduler.clock.now())
            try:
                self.scheduler.store.save_step(
                    cancelled, expected_revision=current.revision
                )
            except RevisionConflictError:
                pass
        self._safe_ack(delivery)
        self.scheduler._sync_run(claimed.workspace_id, claimed.run_id)

    def _fail_claimed(
        self, claimed: StepRecord, delivery: QueueDelivery, exc: Exception
    ) -> None:
        current = self.scheduler.store.get_step(claimed.workspace_id, claimed.step_id)
        if (
            current is None
            or current.status is not ExecutionState.RUNNING
            or current.lease is None
            or current.lease.token != delivery.receipt
        ):
            self._safe_ack(delivery)
            return
        retryable = isinstance(exc, (RetryableStepError, LeaseLost)) or not isinstance(
            exc, (PermanentStepError, LookupError, ValueError, TypeError)
        )
        default_code = "step_retryable_error" if retryable else "step_permanent_error"
        error_code = getattr(exc, "code", default_code)
        error_details = getattr(exc, "details", {})
        if not isinstance(error_code, str) or not error_code:
            error_code = default_code
        if not isinstance(error_details, Mapping):
            error_details = {}
        error = StepError(
            code=error_code,
            message=str(exc) or exc.__class__.__name__,
            retryable=retryable,
            details=error_details,
        )
        failed = fail_step(
            current, error, retry=retryable, at=self.scheduler.clock.now()
        )
        retry_floor = (
            exc.retry_after_seconds if isinstance(exc, RetryableStepError) else None
        )
        if failed.status is ExecutionState.RETRYING and retry_floor is not None:
            failed = replace(
                failed,
                available_at=max(
                    failed.available_at,
                    self.scheduler.clock.now() + timedelta(seconds=retry_floor),
                ),
            )
        try:
            saved = self.scheduler.store.save_step(
                failed, expected_revision=current.revision
            )
        except RevisionConflictError:
            self._safe_ack(delivery)
            return
        if saved.status is ExecutionState.RETRYING:
            try:
                self.scheduler.queue.release(delivery, saved.available_at)
            except DeliveryLeaseLostError:
                self.scheduler._enqueue(saved)
        else:
            self._safe_ack(delivery)
            self.scheduler.schedule_ready(
                saved.workspace_id,
                saved.run_id,
                queue_name=delivery.message.queue_name,
            )
        self.scheduler._sync_run(saved.workspace_id, saved.run_id)

    def _dependency_artifacts(self, record: StepRecord) -> tuple[ArtifactRef, ...]:
        dependencies = set(record.dependencies)
        artifacts: list[ArtifactRef] = []
        for step in self.scheduler.store.list_steps(record.workspace_id, record.run_id):
            if step.step_key not in dependencies:
                continue
            artifacts.extend(
                artifact
                for artifact in step.output_artifacts
                if isinstance(artifact, ArtifactRef)
            )
        return tuple(artifacts)

    def _safe_ack(self, delivery: QueueDelivery) -> None:
        try:
            self.scheduler.queue.ack(delivery)
        except DeliveryLeaseLostError:
            # The broker will redeliver; persisted state makes that harmless.
            pass
