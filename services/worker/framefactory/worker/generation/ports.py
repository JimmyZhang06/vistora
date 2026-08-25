"""Ports shared by generated-media orchestration and durable adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from framefactory.steps import StepContext
from framefactory.worker.retrieval.models import RetrievalBeat


class PaidOperationAction(StrEnum):
    SUBMIT_NEW = "submit_new"
    BUSY = "busy"
    POLL_EXISTING = "poll_existing"
    RECONCILE_EXISTING = "reconcile_existing"
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class PaidOperationRecord:
    id: str
    status: str
    request_hash: str
    authorized_amount_minor: int
    incurred_amount_minor: int
    provider_request_id: str | None
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PaidOperationDecision:
    record: PaidOperationRecord
    action: PaidOperationAction


class PaidOperationLedger(Protocol):
    def reserve(
        self,
        *,
        workspace_id: str,
        underlying_run_id: str,
        full_ai_run_id: str,
        scene_key: str,
        variant_index: int,
        operation_key: str,
        request_hash: str,
        provider_name: str,
        model_id: str,
        authorized_amount_minor: int,
        now: datetime,
    ) -> PaidOperationDecision: ...

    def begin_submit(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_owner: str,
        lease_token: str,
        lease_seconds: float,
        now: datetime,
    ) -> PaidOperationRecord: ...

    def release_definite_rejection(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_token: str,
        error: Mapping[str, Any],
        now: datetime,
    ) -> PaidOperationRecord: ...

    def mark_submitted(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_token: str,
        provider_request_id: str,
        now: datetime,
    ) -> PaidOperationRecord: ...

    def mark_submit_unknown(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_token: str | None,
        provider_request_id: str | None,
        error: Mapping[str, Any],
        now: datetime,
    ) -> PaidOperationRecord: ...

    def mark_succeeded(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        provider_request_id: str,
        incurred_amount_minor: int,
        result: Mapping[str, Any],
        now: datetime,
    ) -> PaidOperationRecord: ...

    def mark_failed(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        provider_request_id: str | None,
        incurred_amount_minor: int,
        cancelled: bool,
        error: Mapping[str, Any],
        now: datetime,
    ) -> PaidOperationRecord: ...

    def settle_run(
        self,
        *,
        workspace_id: str,
        full_ai_run_id: str,
        expected_candidate_count: int,
        now: datetime,
    ) -> None: ...


class GeneratedVideoVerifier(Protocol):
    async def verify(
        self,
        video_bytes: bytes,
        *,
        beat: RetrievalBeat,
        expected_duration_seconds: int,
        ratio: str,
        context: StepContext,
    ) -> Any: ...


def constraint_checks(
    terms: Sequence[str], haystack: str
) -> tuple[dict[str, Any], ...]:
    """Canonical case-insensitive checks used by verifier adapters and producers."""

    text = haystack.casefold()
    return tuple(
        {"term": term, "matched": term.casefold() in text}
        for term in terms
        if term
    )
