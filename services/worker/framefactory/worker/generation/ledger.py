"""PostgreSQL ledger for exactly-once paid Full-AI Provider submissions.

Runway does not expose a client idempotency key or a lookup endpoint for an
unknown submission.  This adapter therefore treats an expired submit lease as
an unknown paid outcome, never as permission to POST again.  The audit key is
local-only and must not be sent to the Provider.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .ports import PaidOperationAction, PaidOperationDecision, PaidOperationRecord

_OPERATION_SELECT = """
  SELECT id, workspace_id, full_ai_run_id, operation_key, scene_key,
         variant_index, request_hash, provider_name, model_id,
         provider_idempotency_key, status, provider_request_id,
         authorized_amount_minor, incurred_amount_minor, lease_owner,
         lease_token, lease_expires_at, reconciliation_attempts, last_error,
         result
    FROM full_ai_paid_operations
"""
_RESULT_MAX_BYTES = 1024 * 1024


class PaidOperationLedgerError(RuntimeError):
    """Base class carrying a stable machine-readable ledger error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class PaidOperationConflictError(PaidOperationLedgerError):
    """The stable paid-operation identity was reused with different inputs."""


class PaidOperationStateError(PaidOperationLedgerError):
    """A requested transition is unsafe for the persisted operation state."""


class PostgresPaidOperationLedger:
    """Strict synchronous adapter over migration 0020's paid-operation table."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @classmethod
    def connect(
        cls, database_url: str, *, timeout_seconds: float = 5.0
    ) -> PostgresPaidOperationLedger:
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
            """SELECT count(*) = 2 AS healthy
                 FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('full_ai_runs', 'full_ai_paid_operations')"""
        ).fetchone()
        if not row or not bool(row["healthy"]):
            raise RuntimeError(
                "Full-AI paid-operation schema is unavailable; apply db migration 0020"
            )

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
    ) -> PaidOperationDecision:
        _require_aware(now)
        _require_hash(request_hash)
        if not operation_key.strip() or not scene_key.strip():
            raise ValueError("paid operation and scene keys must not be empty")
        if variant_index not in {0, 1, 2}:
            raise ValueError("variant_index must be between zero and two")
        if authorized_amount_minor < 0:
            raise ValueError("authorized_amount_minor must not be negative")

        with self._transaction():
            parent = self._connection.execute(
                """SELECT provider_name, model_id, authorized_amount_minor,
                          incurred_amount_minor, requires_reconciliation,
                          billing_status, status,
                          (plan->>'candidate_count')::integer AS candidate_count
                     FROM full_ai_runs
                    WHERE workspace_id = %s AND id = %s
                      AND underlying_run_id = %s
                    FOR UPDATE""",
                (workspace_id, full_ai_run_id, underlying_run_id),
            ).fetchone()
            if parent is None:
                raise PaidOperationStateError(
                    "FULL_AI_RUN_NOT_FOUND",
                    "the paid operation is not bound to this scheduler Run",
                )
            if (
                str(parent["provider_name"]) != provider_name
                or str(parent["model_id"]) != model_id
            ):
                raise PaidOperationConflictError(
                    "FULL_AI_PROVIDER_SNAPSHOT_CONFLICT",
                    "Provider identity differs from the frozen Full-AI Run",
                )

            rows = self._connection.execute(
                f"""{_OPERATION_SELECT}
                     WHERE workspace_id = %s AND full_ai_run_id = %s
                       AND (operation_key = %s
                         OR (scene_key = %s AND variant_index = %s))
                     FOR UPDATE""",
                (
                    workspace_id,
                    full_ai_run_id,
                    operation_key,
                    scene_key,
                    variant_index,
                ),
            ).fetchall()
            if len(rows) > 1:
                raise PaidOperationConflictError(
                    "FULL_AI_OPERATION_IDENTITY_CONFLICT",
                    "operation and scene identity resolve to different paid records",
                )
            if rows:
                row = rows[0]
                self._validate_identity(
                    row,
                    operation_key=operation_key,
                    scene_key=scene_key,
                    variant_index=variant_index,
                    request_hash=request_hash,
                    provider_name=provider_name,
                    model_id=model_id,
                    authorized_amount_minor=authorized_amount_minor,
                )
                decision = self._decision_for_existing(row, now=now)
                if decision.action is PaidOperationAction.SUBMIT_NEW and (
                    bool(parent["requires_reconciliation"])
                    or str(parent["status"])
                    in {"reconciliation_required", "succeeded", "failed", "cancelled"}
                ):
                    raise PaidOperationStateError(
                        "FULL_AI_RECONCILIATION_REQUIRED",
                        "this Run cannot start another paid Provider submission",
                    )
                return decision

            if bool(parent["requires_reconciliation"]) or str(parent["status"]) == (
                "reconciliation_required"
            ):
                raise PaidOperationStateError(
                    "FULL_AI_RECONCILIATION_REQUIRED",
                    "another paid submission on this Run requires manual reconciliation",
                )
            if str(parent["status"]) in {"succeeded", "failed", "cancelled"}:
                raise PaidOperationStateError(
                    "FULL_AI_RUN_TERMINAL",
                    "new paid operations cannot be reserved for a terminal Run",
                )

            totals = self._connection.execute(
                """SELECT count(*)::integer AS operation_count,
                          COALESCE(sum(authorized_amount_minor), 0)::bigint
                            AS authorized_total
                     FROM full_ai_paid_operations
                    WHERE workspace_id = %s AND full_ai_run_id = %s""",
                (workspace_id, full_ai_run_id),
            ).fetchone()
            operation_count = int(totals["operation_count"] if totals else 0)
            authorized_total = int(totals["authorized_total"] if totals else 0)
            if operation_count >= int(parent["candidate_count"]):
                raise PaidOperationStateError(
                    "FULL_AI_PLAN_DRIFT",
                    "paid candidate count exceeds the frozen plan",
                )
            if (
                authorized_total + authorized_amount_minor
                > int(parent["authorized_amount_minor"])
            ):
                raise PaidOperationStateError(
                    "FULL_AI_BUDGET_EXCEEDED",
                    "paid operation authorization exceeds the frozen Run budget",
                )

            operation_id = str(uuid4())
            audit_key = _provider_audit_key(
                workspace_id, full_ai_run_id, operation_key, request_hash
            )
            row = self._connection.execute(
                f"""INSERT INTO full_ai_paid_operations (
                       id, workspace_id, full_ai_run_id, operation_key, scene_key,
                       variant_index, request_hash, provider_name, model_id,
                       provider_idempotency_key, status, authorized_amount_minor,
                       incurred_amount_minor, created_at, updated_at
                     ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       'reserved', %s, 0, %s, %s
                     )
                     RETURNING {_returning_columns()}""",
                (
                    operation_id,
                    workspace_id,
                    full_ai_run_id,
                    operation_key,
                    scene_key,
                    variant_index,
                    request_hash,
                    provider_name,
                    model_id,
                    audit_key,
                    authorized_amount_minor,
                    now,
                    now,
                ),
            ).fetchone()
            if row is None:  # pragma: no cover - PostgreSQL contract violation
                raise RuntimeError("PostgreSQL did not return the reserved paid operation")
            return PaidOperationDecision(
                record=_record(row), action=PaidOperationAction.SUBMIT_NEW
            )

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
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        if not lease_owner.strip() or not lease_token.strip():
            raise ValueError("submit lease owner and token must not be empty")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("submit lease duration must be positive and finite")
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._transaction():
            current = self._locked_operation(
                workspace_id,
                operation_id,
                request_hash,
                require_submission_allowed=True,
            )
            if current["status"] == "submitting":
                if (
                    str(current["lease_owner"]) == lease_owner
                    and str(current["lease_token"]) == lease_token
                ):
                    return _record(current)
                raise PaidOperationStateError(
                    "FULL_AI_PAID_OPERATION_BUSY",
                    "the paid operation already has a submit lease",
                )
            self._require_status(current, {"reserved"}, "begin submit")
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = 'submitting', lease_owner = %s, lease_token = %s,
                           lease_expires_at = %s, last_error = NULL, updated_at = %s
                     WHERE workspace_id = %s AND id = %s
                       AND request_hash = %s AND status = 'reserved'
                     RETURNING {_returning_columns()}""",
                (
                    lease_owner,
                    lease_token,
                    expires_at,
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                ),
            ).fetchone()
            return _record(_updated(row, "begin submit"))

    def release_definite_rejection(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_token: str,
        error: Mapping[str, Any],
        now: datetime,
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        payload = dict(error)
        payload["definite_rejection"] = True
        with self._transaction():
            current = self._locked_operation(workspace_id, operation_id, request_hash)
            if current["status"] == "reserved" and _definite_rejection(
                current.get("last_error")
            ):
                return _record(current)
            self._require_status(current, {"submitting"}, "release submit lease")
            self._require_lease(current, lease_token)
            if current["provider_request_id"] is not None or int(
                current["incurred_amount_minor"]
            ):
                raise PaidOperationStateError(
                    "FULL_AI_DEFINITE_REJECTION_INVALID",
                    "a known Provider task or incurred cost cannot be released for resubmission",
                )
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = 'reserved', lease_owner = NULL, lease_token = NULL,
                           lease_expires_at = NULL, last_error = %s, updated_at = %s
                     WHERE workspace_id = %s AND id = %s AND request_hash = %s
                       AND status = 'submitting' AND lease_token = %s
                     RETURNING {_returning_columns()}""",
                (
                    Jsonb(payload),
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                    lease_token,
                ),
            ).fetchone()
            return _record(_updated(row, "release submit lease"))

    def mark_submitted(
        self,
        *,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        lease_token: str,
        provider_request_id: str,
        now: datetime,
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        if not provider_request_id.strip():
            raise ValueError("provider_request_id must not be empty")
        with self._transaction():
            current = self._locked_operation(workspace_id, operation_id, request_hash)
            if current["status"] == "submitted":
                self._require_provider_request(current, provider_request_id)
                return _record(current)
            self._require_status(current, {"submitting"}, "mark submitted")
            self._require_lease(current, lease_token)
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = 'submitted', provider_request_id = %s,
                           lease_owner = NULL, lease_token = NULL,
                           lease_expires_at = NULL, updated_at = %s
                     WHERE workspace_id = %s AND id = %s AND request_hash = %s
                       AND status = 'submitting' AND lease_token = %s
                     RETURNING {_returning_columns()}""",
                (
                    provider_request_id,
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                    lease_token,
                ),
            ).fetchone()
            return _record(_updated(row, "mark submitted"))

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
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        payload = dict(error)
        payload["automatic_resubmission_forbidden"] = True
        with self._transaction():
            current = self._locked_operation(workspace_id, operation_id, request_hash)
            if current["status"] == "submit_unknown":
                self._require_compatible_provider_request(current, provider_request_id)
                return _record(current)
            self._require_status(
                current, {"submitting", "submitted"}, "mark submit unknown"
            )
            if current["status"] == "submitting":
                if lease_token is None:
                    raise PaidOperationStateError(
                        "FULL_AI_SUBMIT_LEASE_REQUIRED",
                        "an active submitting operation requires its lease token",
                    )
                self._require_lease(current, lease_token)
            elif lease_token is not None:
                raise PaidOperationStateError(
                    "FULL_AI_SUBMIT_LEASE_INVALID",
                    "a submitted operation no longer has a submit lease",
                )
            self._require_compatible_provider_request(current, provider_request_id)
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = 'submit_unknown',
                           provider_request_id = COALESCE(provider_request_id, %s),
                           lease_owner = NULL, lease_token = NULL,
                           lease_expires_at = NULL,
                           reconciliation_attempts = reconciliation_attempts + 1,
                           last_error = %s, updated_at = %s
                     WHERE workspace_id = %s AND id = %s AND request_hash = %s
                       AND status IN ('submitting', 'submitted')
                     RETURNING {_returning_columns()}""",
                (
                    provider_request_id,
                    Jsonb(payload),
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                ),
            ).fetchone()
            return _record(_updated(row, "mark submit unknown"))

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
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        checkpoint = _validated_result(result)
        with self._transaction():
            current = self._locked_operation(workspace_id, operation_id, request_hash)
            self._validate_cost(current, incurred_amount_minor)
            self._require_compatible_provider_request(current, provider_request_id)
            if current["status"] == "succeeded":
                if int(current["incurred_amount_minor"]) != incurred_amount_minor:
                    self._cost_conflict()
                if current.get("result") != checkpoint:
                    raise PaidOperationConflictError(
                        "FULL_AI_RESULT_CONFLICT",
                        "an idempotent success supplied a different result checkpoint",
                    )
                return _record(current)
            self._require_status(
                current, {"submitted", "submit_unknown"}, "mark succeeded"
            )
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = 'succeeded',
                           provider_request_id = COALESCE(provider_request_id, %s),
                           incurred_amount_minor = %s,
                           result = %s,
                           reconciled_at = CASE WHEN status = 'submit_unknown'
                             THEN %s ELSE reconciled_at END,
                           updated_at = %s
                     WHERE workspace_id = %s AND id = %s AND request_hash = %s
                       AND status IN ('submitted', 'submit_unknown')
                     RETURNING {_returning_columns()}""",
                (
                    provider_request_id,
                    incurred_amount_minor,
                    Jsonb(checkpoint),
                    now,
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                ),
            ).fetchone()
            return _record(_updated(row, "mark succeeded"))

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
    ) -> PaidOperationRecord:
        _require_aware(now)
        _require_hash(request_hash)
        destination = "cancelled" if cancelled else "failed"
        with self._transaction():
            current = self._locked_operation(workspace_id, operation_id, request_hash)
            self._validate_cost(current, incurred_amount_minor)
            self._require_compatible_provider_request(current, provider_request_id)
            if current["status"] == destination:
                if int(current["incurred_amount_minor"]) != incurred_amount_minor:
                    self._cost_conflict()
                return _record(current)
            self._require_status(
                current,
                {"reserved", "submitting", "submitted", "submit_unknown"},
                f"mark {destination}",
            )
            row = self._connection.execute(
                f"""UPDATE full_ai_paid_operations
                       SET status = %s,
                           provider_request_id = COALESCE(provider_request_id, %s),
                           incurred_amount_minor = %s,
                           lease_owner = NULL, lease_token = NULL,
                           lease_expires_at = NULL,
                           reconciled_at = CASE WHEN status = 'submit_unknown'
                             THEN %s ELSE reconciled_at END,
                           last_error = %s, updated_at = %s
                     WHERE workspace_id = %s AND id = %s AND request_hash = %s
                       AND status IN (
                         'reserved', 'submitting', 'submitted', 'submit_unknown'
                       )
                     RETURNING {_returning_columns()}""",
                (
                    destination,
                    provider_request_id,
                    incurred_amount_minor,
                    now,
                    Jsonb(dict(error)),
                    now,
                    workspace_id,
                    operation_id,
                    request_hash,
                ),
            ).fetchone()
            return _record(_updated(row, f"mark {destination}"))

    def settle_run(
        self,
        *,
        workspace_id: str,
        full_ai_run_id: str,
        expected_candidate_count: int,
        now: datetime,
    ) -> None:
        _require_aware(now)
        if expected_candidate_count < 1:
            raise ValueError("expected_candidate_count must be positive")
        with self._transaction():
            parent = self._connection.execute(
                """SELECT authorized_amount_minor,
                          (plan->>'candidate_count')::integer AS candidate_count
                     FROM full_ai_runs
                    WHERE workspace_id = %s AND id = %s
                    FOR UPDATE""",
                (workspace_id, full_ai_run_id),
            ).fetchone()
            if parent is None:
                raise PaidOperationStateError(
                    "FULL_AI_RUN_NOT_FOUND", "the Full-AI Run does not exist"
                )
            if int(parent["candidate_count"]) != expected_candidate_count:
                raise PaidOperationStateError(
                    "FULL_AI_PLAN_DRIFT",
                    "settlement candidate count differs from the frozen Run plan",
                )
            aggregate = self._connection.execute(
                """SELECT count(*)::integer AS operation_count,
                          COALESCE(sum(incurred_amount_minor), 0)::bigint
                            AS incurred_total,
                          COALESCE(bool_and(status = 'succeeded'), false)
                            AS all_succeeded,
                          COALESCE(bool_or(status = 'submit_unknown'), false)
                            AS has_submit_unknown
                     FROM full_ai_paid_operations
                    WHERE workspace_id = %s AND full_ai_run_id = %s""",
                (workspace_id, full_ai_run_id),
            ).fetchone()
            if bool(aggregate["has_submit_unknown"]):
                raise PaidOperationStateError(
                    "FULL_AI_RECONCILIATION_REQUIRED",
                    "unknown paid submissions must be reconciled before settlement",
                )
            if (
                int(aggregate["operation_count"]) != expected_candidate_count
                or not bool(aggregate["all_succeeded"])
            ):
                raise PaidOperationStateError(
                    "FULL_AI_PAID_OPERATIONS_INCOMPLETE",
                    "all frozen paid candidates must succeed before settlement",
                )
            incurred_total = int(aggregate["incurred_total"])
            if incurred_total > int(parent["authorized_amount_minor"]):
                raise PaidOperationStateError(
                    "FULL_AI_BUDGET_EXCEEDED",
                    "settled Provider cost exceeds the frozen Run budget",
                )
            row = self._connection.execute(
                """UPDATE full_ai_runs
                       SET billing_status = 'settled',
                           incurred_amount_minor = %s,
                           requires_reconciliation = false,
                           updated_at = %s
                     WHERE workspace_id = %s AND id = %s
                       AND authorized_amount_minor >= %s
                 RETURNING id""",
                (
                    incurred_total,
                    now,
                    workspace_id,
                    full_ai_run_id,
                    incurred_total,
                ),
            ).fetchone()
            if row is None:  # pragma: no cover - locked parent changed impossibly
                raise PaidOperationStateError(
                    "FULL_AI_BUDGET_EXCEEDED", "Run settlement exceeded authorization"
                )

    def _decision_for_existing(
        self, row: Mapping[str, Any], *, now: datetime
    ) -> PaidOperationDecision:
        status = str(row["status"])
        if status == "reserved":
            action = PaidOperationAction.SUBMIT_NEW
        elif status == "submitting":
            expires_at = row["lease_expires_at"]
            if expires_at is None:
                raise PaidOperationStateError(
                    "FULL_AI_LEDGER_CORRUPT",
                    "a submitting paid operation has no lease expiry",
                )
            if _utc(expires_at) > _utc(now):
                action = PaidOperationAction.BUSY
            else:
                expired = self._connection.execute(
                    f"""UPDATE full_ai_paid_operations
                           SET status = 'submit_unknown', lease_owner = NULL,
                               lease_token = NULL, lease_expires_at = NULL,
                               reconciliation_attempts = reconciliation_attempts + 1,
                               last_error = %s, updated_at = %s
                         WHERE workspace_id = %s AND id = %s
                           AND request_hash = %s AND status = 'submitting'
                           AND lease_expires_at <= %s
                         RETURNING {_returning_columns()}""",
                    (
                        Jsonb(
                            {
                                "code": "FULL_AI_SUBMIT_LEASE_EXPIRED",
                                "submit_outcome": "unknown",
                                "automatic_resubmission_forbidden": True,
                            }
                        ),
                        now,
                        row["workspace_id"],
                        row["id"],
                        row["request_hash"],
                        now,
                    ),
                ).fetchone()
                row = _updated(expired, "expire submit lease")
                action = (
                    PaidOperationAction.RECONCILE_EXISTING
                    if row["provider_request_id"]
                    else PaidOperationAction.MANUAL_RECONCILIATION_REQUIRED
                )
        elif status == "submitted":
            if not row["provider_request_id"]:
                raise PaidOperationStateError(
                    "FULL_AI_LEDGER_CORRUPT", "a submitted operation has no Provider task"
                )
            action = PaidOperationAction.POLL_EXISTING
        elif status == "submit_unknown":
            action = (
                PaidOperationAction.RECONCILE_EXISTING
                if row["provider_request_id"]
                else PaidOperationAction.MANUAL_RECONCILIATION_REQUIRED
            )
        elif status in {"succeeded", "failed", "cancelled"}:
            action = PaidOperationAction.TERMINAL
        else:  # pragma: no cover - database constraint prevents this
            raise PaidOperationStateError(
                "FULL_AI_LEDGER_CORRUPT", f"unsupported paid operation status: {status}"
            )
        return PaidOperationDecision(record=_record(row), action=action)

    def _locked_operation(
        self,
        workspace_id: str,
        operation_id: str,
        request_hash: str,
        *,
        require_submission_allowed: bool = False,
    ) -> Mapping[str, Any]:
        identity = self._connection.execute(
            """SELECT full_ai_run_id, request_hash
                 FROM full_ai_paid_operations
                WHERE workspace_id = %s AND id = %s""",
            (workspace_id, operation_id),
        ).fetchone()
        if identity is None:
            raise PaidOperationStateError(
                "FULL_AI_PAID_OPERATION_NOT_FOUND",
                "the paid operation does not exist in this workspace",
            )
        if str(identity["request_hash"]) != request_hash:
            raise PaidOperationConflictError(
                "FULL_AI_OPERATION_REQUEST_CONFLICT",
                "operation identity was reused with a different request hash",
            )
        parent = self._connection.execute(
            """SELECT status, requires_reconciliation
                 FROM full_ai_runs
                WHERE workspace_id = %s AND id = %s
                FOR UPDATE""",
            (workspace_id, identity["full_ai_run_id"]),
        ).fetchone()
        if parent is None:
            raise PaidOperationStateError(
                "FULL_AI_RUN_NOT_FOUND", "the parent Full-AI Run does not exist"
            )
        if require_submission_allowed and (
            bool(parent["requires_reconciliation"])
            or str(parent["status"])
            in {"reconciliation_required", "succeeded", "failed", "cancelled"}
        ):
            raise PaidOperationStateError(
                "FULL_AI_RECONCILIATION_REQUIRED",
                "the Run cannot begin another paid Provider submission",
            )
        row = self._connection.execute(
            f"""{_OPERATION_SELECT}
                 WHERE workspace_id = %s AND id = %s
                 FOR UPDATE""",
            (workspace_id, operation_id),
        ).fetchone()
        if row is None:
            raise PaidOperationStateError(
                "FULL_AI_PAID_OPERATION_NOT_FOUND",
                "the paid operation does not exist in this workspace",
            )
        if str(row["request_hash"]) != request_hash:
            raise PaidOperationConflictError(
                "FULL_AI_OPERATION_REQUEST_CONFLICT",
                "operation identity was reused with a different request hash",
            )
        return row

    @staticmethod
    def _validate_identity(
        row: Mapping[str, Any],
        *,
        operation_key: str,
        scene_key: str,
        variant_index: int,
        request_hash: str,
        provider_name: str,
        model_id: str,
        authorized_amount_minor: int,
    ) -> None:
        expected = (
            operation_key,
            scene_key,
            variant_index,
            request_hash,
            provider_name,
            model_id,
            authorized_amount_minor,
        )
        actual = (
            str(row["operation_key"]),
            str(row["scene_key"]),
            int(row["variant_index"]),
            str(row["request_hash"]),
            str(row["provider_name"]),
            str(row["model_id"]),
            int(row["authorized_amount_minor"]),
        )
        if actual != expected:
            code = (
                "FULL_AI_OPERATION_REQUEST_CONFLICT"
                if str(row["request_hash"]) != request_hash
                else "FULL_AI_OPERATION_IDENTITY_CONFLICT"
            )
            raise PaidOperationConflictError(
                code, "paid operation identity differs from the durable reservation"
            )

    @staticmethod
    def _require_status(
        row: Mapping[str, Any], allowed: set[str], action: str
    ) -> None:
        if str(row["status"]) not in allowed:
            raise PaidOperationStateError(
                "FULL_AI_PAID_OPERATION_STATE_INVALID",
                f"cannot {action} from paid operation state {row['status']}",
            )

    @staticmethod
    def _require_lease(row: Mapping[str, Any], lease_token: str) -> None:
        if str(row["lease_token"]) != lease_token:
            raise PaidOperationStateError(
                "FULL_AI_SUBMIT_LEASE_CONFLICT", "submit lease token does not match"
            )

    @staticmethod
    def _require_provider_request(
        row: Mapping[str, Any], provider_request_id: str
    ) -> None:
        if not provider_request_id.strip():
            raise ValueError("provider_request_id must not be empty")
        known = row["provider_request_id"]
        if known is None:
            raise PaidOperationStateError(
                "FULL_AI_PROVIDER_TASK_UNKNOWN",
                "the paid operation has no durable Provider task identity",
            )
        if str(known) != provider_request_id:
            raise PaidOperationConflictError(
                "FULL_AI_PROVIDER_TASK_CONFLICT",
                "Provider task identity differs from the durable paid operation",
            )

    @staticmethod
    def _require_compatible_provider_request(
        row: Mapping[str, Any], provider_request_id: str | None
    ) -> None:
        if provider_request_id is not None and not provider_request_id.strip():
            raise ValueError("provider_request_id must not be empty")
        known = row["provider_request_id"]
        if known is not None and provider_request_id is not None and str(known) != (
            provider_request_id
        ):
            raise PaidOperationConflictError(
                "FULL_AI_PROVIDER_TASK_CONFLICT",
                "Provider task identity differs from the durable paid operation",
            )

    @staticmethod
    def _validate_cost(row: Mapping[str, Any], incurred_amount_minor: int) -> None:
        if incurred_amount_minor < int(row["incurred_amount_minor"]):
            raise PaidOperationConflictError(
                "FULL_AI_COST_REGRESSION", "incurred Provider cost cannot decrease"
            )
        if incurred_amount_minor > int(row["authorized_amount_minor"]):
            raise PaidOperationStateError(
                "FULL_AI_BUDGET_EXCEEDED",
                "incurred Provider cost exceeds the paid operation authorization",
            )

    @staticmethod
    def _cost_conflict() -> None:
        raise PaidOperationConflictError(
            "FULL_AI_COST_CONFLICT",
            "an idempotent terminal transition supplied a different Provider cost",
        )

    def _transaction(self):
        transaction = getattr(self._connection, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()


def _returning_columns() -> str:
    return _OPERATION_SELECT.split("FROM full_ai_paid_operations", maxsplit=1)[0].replace(
        "SELECT", "", 1
    ).strip()


def _record(row: Mapping[str, Any]) -> PaidOperationRecord:
    return PaidOperationRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        request_hash=str(row["request_hash"]),
        authorized_amount_minor=int(row["authorized_amount_minor"]),
        incurred_amount_minor=int(row["incurred_amount_minor"]),
        provider_request_id=(
            str(row["provider_request_id"])
            if row["provider_request_id"] is not None
            else None
        ),
        result=_json_mapping(row.get("result")),
    )


def _updated(row: Mapping[str, Any] | None, action: str) -> Mapping[str, Any]:
    if row is None:
        raise PaidOperationStateError(
            "FULL_AI_PAID_OPERATION_CAS_CONFLICT",
            f"paid operation changed concurrently while attempting to {action}",
        )
    return row


def _provider_audit_key(
    workspace_id: str, full_ai_run_id: str, operation_key: str, request_hash: str
) -> str:
    domain = (
        f"{workspace_id}\0{full_ai_run_id}\0{operation_key}\0{request_hash}"
    ).encode()
    return f"audit-sha256:{hashlib.sha256(domain).hexdigest()}"


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("request_hash must be 64 lowercase hexadecimal characters")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _definite_rejection(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("definite_rejection") is True


def _validated_result(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "verification_status",
        "output_content_hash",
        "output_byte_size",
        "output_media_type",
        "output_filename",
        "accepted_artifact",
        "verification_evidence",
    }
    if set(value) != expected_keys:
        raise ValueError("paid operation result has an unsupported field shape")
    if value.get("schema_version") != "1.0.0":
        raise ValueError("paid operation result schema_version must be 1.0.0")
    verification_status = value.get("verification_status")
    if verification_status not in {"accepted", "rejected"}:
        raise ValueError("verification_status must be accepted or rejected")
    output_hash = value.get("output_content_hash")
    if not isinstance(output_hash, str):
        raise TypeError("output_content_hash must be a SHA-256 string")
    _require_hash(output_hash)
    output_size = value.get("output_byte_size")
    if isinstance(output_size, bool) or not isinstance(output_size, int) or output_size < 1:
        raise ValueError("output_byte_size must be a positive integer")
    output_media_type = value.get("output_media_type")
    if (
        not isinstance(output_media_type, str)
        or re.fullmatch(
            r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", output_media_type
        )
        is None
        or len(output_media_type) > 120
    ):
        raise ValueError("output_media_type must be a concrete media type")
    output_filename = value.get("output_filename")
    if (
        not isinstance(output_filename, str)
        or not output_filename.strip()
        or len(output_filename) > 255
        or "/" in output_filename
        or "\\" in output_filename
    ):
        raise ValueError("output_filename must be a basename")
    evidence = value.get("verification_evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ValueError("verification_evidence must be a complete non-empty object")
    accepted = evidence.get("accepted")
    if not isinstance(accepted, bool) or accepted is not (
        verification_status == "accepted"
    ):
        raise ValueError("verification evidence conflicts with verification_status")

    artifact = value.get("accepted_artifact")
    if verification_status == "rejected" and artifact is not None:
        raise ValueError("rejected output cannot reference an accepted Artifact")
    if artifact is not None:
        _validate_result_artifact(
            artifact,
            output_hash=output_hash,
            output_size=output_size,
            output_media_type=output_media_type,
            output_filename=output_filename,
        )
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("paid operation result must be JSON serializable") from exc
    if len(encoded.encode()) > _RESULT_MAX_BYTES:
        raise ValueError("paid operation result exceeds the one MiB evidence limit")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict):  # pragma: no cover - constructed above
        raise TypeError("paid operation result must be a JSON object")
    return normalized


def _validate_result_artifact(
    value: object,
    *,
    output_hash: str,
    output_size: int,
    output_media_type: str,
    output_filename: str,
) -> None:
    if not isinstance(value, Mapping):
        raise TypeError("accepted_artifact must be an ArtifactRef object or null")
    expected_keys = {
        "schema_version",
        "id",
        "workspace_id",
        "ownership_type",
        "run_id",
        "step_id",
        "kind",
        "media_type",
        "object_key",
        "byte_size",
        "content_hash",
        "filename",
        "created_at",
        "expires_at",
    }
    if set(value) != expected_keys:
        raise ValueError("accepted_artifact must contain the complete ArtifactRef")
    if value.get("schema_version") != "1.0.0" or value.get("ownership_type") != (
        "workspace"
    ):
        raise ValueError("accepted_artifact identity is invalid")
    for field in ("id", "workspace_id", "run_id"):
        try:
            UUID(str(value.get(field)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"accepted_artifact.{field} must be a UUID") from exc
    if value.get("step_id") is not None and not str(value["step_id"]).strip():
        raise ValueError("accepted_artifact.step_id must be non-empty or null")
    object_key = value.get("object_key")
    if (
        value.get("kind") != "asset"
        or not isinstance(object_key, str)
        or not object_key.strip()
        or len(object_key) > 2048
    ):
        raise ValueError("accepted_artifact must identify an immutable asset object")
    if (
        value.get("content_hash") != output_hash
        or value.get("byte_size") != output_size
        or value.get("media_type") != output_media_type
        or value.get("filename") != output_filename
    ):
        raise ValueError("accepted_artifact identity differs from the generated output")
    for field in ("created_at", "expires_at"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"accepted_artifact.{field} must be a timestamp or null")


def _json_mapping(value: object) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PaidOperationStateError(
            "FULL_AI_LEDGER_CORRUPT", "paid operation result is not a JSON object"
        )
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
