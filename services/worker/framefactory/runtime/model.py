"""Immutable runtime records for declarative, recoverable workflow execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from framefactory.skills.canonical import FrozenMap, normalize_json


def utc_now() -> datetime:
    """Return an aware UTC timestamp (kept injectable everywhere else)."""

    return datetime.now(UTC)


def require_aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def freeze_snapshot(value: object) -> object:
    """Take a defensive, recursively immutable JSON snapshot.

    Step adapters may provide their own frozen ``InputSnapshot`` value object. Such
    objects are retained; ordinary mappings and sequences are copied into the same
    immutable JSON representation used by the Skill Engine.
    """

    if isinstance(value, FrozenMap):
        return value
    if value.__class__.__name__ == "InputSnapshot":
        return value
    return normalize_json(value, path="$.input_snapshot")


class ExecutionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


# Run and step records deliberately share one vocabulary.
RunState = ExecutionState
StepState = ExecutionState
RunStatus = ExecutionState
StepStatus = ExecutionState


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_CHANGES = "request_changes"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")

    def delay_after(self, attempt_count: int) -> timedelta:
        """Deterministic capped exponential delay after a failed attempt."""

        if attempt_count < 1:
            raise ValueError("attempt_count must be positive")
        seconds = min(
            self.max_delay_seconds,
            self.base_delay_seconds * self.multiplier ** (attempt_count - 1),
        )
        return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class Lease:
    owner: str
    token: str
    expires_at: datetime
    heartbeat_at: datetime

    def __post_init__(self) -> None:
        if not self.owner or not self.token:
            raise ValueError("lease owner and token must not be empty")
        require_aware(self.expires_at, field_name="expires_at")
        require_aware(self.heartbeat_at, field_name="heartbeat_at")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("lease expiry must be after its heartbeat")

    def expired(self, now: datetime) -> bool:
        return self.expires_at <= require_aware(now, field_name="now")


@dataclass(frozen=True, slots=True)
class StepError:
    code: str
    message: str
    retryable: bool = False
    details: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("error code must not be empty")
        normalized = normalize_json(self.details, path="$.error.details")
        if not isinstance(normalized, FrozenMap):
            raise TypeError("error details must be an object")
        object.__setattr__(self, "details", normalized)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    decision: ReviewDecision | None = None
    actor_id: str | None = None
    comment: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    metadata: FrozenMap = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        normalized = normalize_json(self.metadata, path="$.review.metadata")
        if not isinstance(normalized, FrozenMap):
            raise TypeError("review metadata must be an object")
        object.__setattr__(self, "metadata", normalized)


@dataclass(frozen=True, slots=True)
class RunStep:
    """Declarative step definition captured before a run starts."""

    key: str
    step_type: str
    input_snapshot: object
    dependencies: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    queue_name: str = "default"
    retry_policy: RetryPolicy = RetryPolicy()
    review_required: bool = False
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.key or not self.step_type:
            raise ValueError("step key and step_type must not be empty")
        if not self.queue_name:
            raise ValueError("queue_name must not be empty")
        if not -100 <= self.priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if self.key in self.dependencies:
            raise ValueError("a step cannot depend on itself")
        object.__setattr__(
            self, "dependencies", tuple(dict.fromkeys(self.dependencies))
        )
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(sorted(set(self.required_capabilities))),
        )
        object.__setattr__(self, "input_snapshot", freeze_snapshot(self.input_snapshot))


@dataclass(frozen=True, slots=True)
class StepRecord:
    workspace_id: str
    run_id: str
    step_id: str
    step_key: str
    step_type: str
    input_snapshot: object
    dependencies: tuple[str, ...] = ()
    status: ExecutionState = ExecutionState.QUEUED
    queue_name: str = "default"
    required_capabilities: tuple[str, ...] = ()
    priority: int = 0
    available_at: datetime = field(default_factory=utc_now)
    attempt_count: int = 0
    retry_policy: RetryPolicy = RetryPolicy()
    lease: Lease | None = None
    output_artifacts: tuple[object, ...] = ()
    output_summary: object | None = None
    error: StepError | None = None
    review_required: bool = False
    static_review_required: bool = False
    review: ReviewRecord | None = None
    cancellation_requested_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    revision: int = 0

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "run_id",
            "step_id",
            "step_key",
            "step_type",
            "queue_name",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if not -100 <= self.priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if (
            self.attempt_count < 0
            or self.attempt_count > self.retry_policy.max_attempts
        ):
            raise ValueError("attempt_count is outside retry policy")
        if self.revision < 0:
            raise ValueError("revision must not be negative")
        for name in ("available_at", "created_at", "updated_at"):
            require_aware(getattr(self, name), field_name=name)
        for name in ("cancellation_requested_at", "started_at", "completed_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, field_name=name)
        object.__setattr__(self, "input_snapshot", freeze_snapshot(self.input_snapshot))
        if self.step_key in self.dependencies:
            raise ValueError("a step cannot depend on itself")
        object.__setattr__(
            self, "dependencies", tuple(dict.fromkeys(self.dependencies))
        )
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(sorted(set(self.required_capabilities))),
        )
        object.__setattr__(self, "output_artifacts", tuple(self.output_artifacts))
        self.require_consistent()

    @property
    def max_attempts(self) -> int:
        return self.retry_policy.max_attempts

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def cancellation_requested(self) -> bool:
        return self.cancellation_requested_at is not None

    def require_consistent(self) -> StepRecord:
        if self.status is ExecutionState.RUNNING and self.lease is None:
            raise ValueError("running step must hold a lease")
        if self.status is not ExecutionState.RUNNING and self.lease is not None:
            raise ValueError("only a running step may hold a lease")
        if self.status.terminal != (self.completed_at is not None):
            raise ValueError("terminal status and completed_at must agree")
        if self.status is ExecutionState.AWAITING_REVIEW and self.review is None:
            raise ValueError("awaiting_review step must have a review request")
        return self


@dataclass(frozen=True, slots=True)
class RunRecord:
    workspace_id: str
    run_id: str
    input_snapshot: object
    status: ExecutionState = ExecutionState.QUEUED
    cancellation_requested_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.workspace_id or not self.run_id:
            raise ValueError("workspace_id and run_id must not be empty")
        object.__setattr__(self, "input_snapshot", freeze_snapshot(self.input_snapshot))
        if self.status.terminal != (self.completed_at is not None):
            raise ValueError("terminal status and completed_at must agree")

    @property
    def terminal(self) -> bool:
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class StepClaim:
    workspace_id: str
    run_id: str
    step_id: str
    worker_id: str
    lease_token: str
    attempt: int


class CancellationToken:
    """Cooperative cancellation token backed by local and persisted signals."""

    __slots__ = ("_cancelled", "_checker")

    def __init__(self, checker: Callable[[], bool] | None = None) -> None:
        self._cancelled = False
        self._checker = checker

    @property
    def cancelled(self) -> bool:
        return self._cancelled or bool(self._checker and self._checker())

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise StepCancelled("step cancellation was requested")


class RuntimeErrorBase(Exception):
    """Base class for expected runtime control-flow errors."""


class InvalidTransition(RuntimeErrorBase):
    pass


class LeaseLost(RuntimeErrorBase):
    pass


class ConcurrentUpdate(RuntimeErrorBase):
    pass


class StepCancelled(RuntimeErrorBase):
    pass


class RetryableStepError(RuntimeErrorBase):
    """Expected transient failure with stable machine-readable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        code: str = "step_retryable_error",
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        if not code:
            raise ValueError("error code must not be empty")
        self.retry_after_seconds = retry_after_seconds
        self.code = code
        self.details = details or {}


class PermanentStepError(RuntimeErrorBase):
    """Expected non-retryable failure with stable machine-readable diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "step_permanent_error",
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        if not code:
            raise ValueError("error code must not be empty")
        self.code = code
        self.details = details or {}


class CapabilityUnavailable(PermanentStepError):
    pass
