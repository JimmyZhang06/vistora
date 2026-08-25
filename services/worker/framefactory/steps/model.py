"""Immutable values exchanged between the worker runtime and registered steps."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from framefactory.skills.canonical import (
    FrozenMap,
    normalize_json,
    sha256_content_hash,
    thaw_json,
)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A metadata-only reference to an immutable object-store artifact.

    The fields intentionally mirror ``packages/contracts``.  Steps exchange these
    references and never infer tenant/account paths or open an object by an old
    account id.
    """

    id: str
    workspace_id: str
    run_id: str
    step_id: str | None
    kind: str
    media_type: str
    object_key: str
    byte_size: int
    content_hash: str
    filename: str
    created_at: str | None = None
    expires_at: str | None = None
    schema_version: str = "1.0.0"
    ownership_type: str = "workspace"

    def __post_init__(self) -> None:
        if self.byte_size < 0:
            raise ValueError("artifact byte_size must not be negative")
        if not self.object_key:
            raise ValueError("artifact object_key must not be empty")
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise ValueError("artifact filename must be a basename")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ArtifactRef:
        return cls(
            **{
                name: value.get(name)
                for name in cls.__dataclass_fields__
                if name in value
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "workspace_id": self.workspace_id,
            "ownership_type": self.ownership_type,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "kind": self.kind,
            "media_type": self.media_type,
            "object_key": self.object_key,
            "byte_size": self.byte_size,
            "content_hash": self.content_hash,
            "filename": self.filename,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class InputSnapshot(Mapping[str, Any]):
    """Recursively immutable, content-addressed input captured at run creation."""

    data: FrozenMap
    content_hash: str

    @classmethod
    def capture(cls, value: Mapping[str, Any] | InputSnapshot) -> InputSnapshot:
        if isinstance(value, cls):
            return value
        normalized = normalize_json(value, path="$.input_snapshot")
        if not isinstance(normalized, FrozenMap):
            raise TypeError("input snapshot must be an object")
        return cls(data=normalized, content_hash=sha256_content_hash(normalized))

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self.data)


@runtime_checkable
class CancellationToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class NullCancellationToken:
    @property
    def cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


Heartbeat = Callable[[], object | Awaitable[object]]


@runtime_checkable
class ArtifactPublisher(Protocol):
    def publish(
        self,
        path: object,
        *,
        workspace_id: str,
        run_id: str,
        step_id: str,
        kind: str,
        media_type: str,
    ) -> ArtifactRef | Awaitable[ArtifactRef]: ...


@dataclass(frozen=True, slots=True)
class StepContext:
    """All step dependencies, passed explicitly and safe to retain across retries."""

    workspace_id: str
    run_id: str
    step_id: str
    input_snapshot: InputSnapshot | Mapping[str, Any]
    cancellation_token: CancellationToken = field(default_factory=NullCancellationToken)
    heartbeat: Heartbeat = field(default=lambda: None)
    capabilities: object | None = None
    input_artifacts: tuple[ArtifactRef, ...] = ()
    attempt: int = 1
    maximum_attempts: int = 3
    review_feedback: str | None = None
    artifact_publisher: ArtifactPublisher | None = None
    approved_dependency_step_ids: tuple[str, ...] = ()
    approved_dependency_reviews: Mapping[str, Any] = field(default_factory=FrozenMap)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input_snapshot", InputSnapshot.capture(self.input_snapshot)
        )
        object.__setattr__(self, "input_artifacts", tuple(self.input_artifacts))
        object.__setattr__(
            self,
            "approved_dependency_step_ids",
            tuple(sorted(set(self.approved_dependency_step_ids))),
        )
        normalized_reviews = normalize_json(
            self.approved_dependency_reviews,
            path="$.approved_dependency_reviews",
        )
        if not isinstance(normalized_reviews, FrozenMap):
            raise TypeError("approved dependency reviews must be an object")
        object.__setattr__(self, "approved_dependency_reviews", normalized_reviews)
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        if self.maximum_attempts < self.attempt:
            raise ValueError("maximum_attempts must be at least the current attempt")

    async def checkpoint(self) -> None:
        """Cooperative cancellation and lease heartbeat boundary."""

        self.cancellation_token.raise_if_cancelled()
        result = self.heartbeat()
        if inspect.isawaitable(result):
            await result
        self.cancellation_token.raise_if_cancelled()


@dataclass(frozen=True, slots=True)
class StepResult:
    artifacts: tuple[ArtifactRef, ...] = ()
    output_summary: FrozenMap | Mapping[str, Any] = field(default_factory=FrozenMap)
    requires_review: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        normalized = normalize_json(self.output_summary, path="$.output_summary")
        if not isinstance(normalized, FrozenMap):
            raise TypeError("output_summary must be an object")
        object.__setattr__(self, "output_summary", normalized)

    def summary_dict(self) -> dict[str, Any]:
        return thaw_json(self.output_summary)


@runtime_checkable
class RunStep(Protocol):
    step_type: str

    def execute(self, context: StepContext) -> StepResult | Awaitable[StepResult]: ...
