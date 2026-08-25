"""Generated-only paid video orchestration.

Every paid scene/variant has a stable operation identity in the control-plane
ledger.  The capability never searches, imports, or writes to a shared asset
catalog; its only video inputs are the current Run's script and narration
timing, and its only video outputs are current-Run artifacts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext, StepResult
from framefactory.worker.providers import ArtifactStorage, ProviderArtifact
from framefactory.worker.retrieval import normalize_beats
from framefactory.worker.retrieval.models import RetrievalBeat

from .ports import (
    GeneratedVideoVerifier,
    PaidOperationAction,
    PaidOperationDecision,
    PaidOperationLedger,
    PaidOperationRecord,
)
from .runway import (
    RUNWAY_API_VERSION,
    RunwayClient,
    RunwayPermanentError,
    RunwayRetryableError,
    RunwaySubmitUnknown,
    RunwayTask,
    RunwayTaskStatus,
    RunwayTextVideoRequest,
)
from .verifier import GeneratedVideoVerificationError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PROMPT_TEMPLATE_VERSION = "runway-gen45-v1"
_CLIP_SECONDS = 5
_MAX_OUTPUT_BYTES = 512 * 1024 * 1024
_POLL_TIMEOUT_SECONDS = 2 * 60 * 60
_SUBMIT_LEASE_SECONDS = 120.0
_SAFETY_KEYS = (
    "adult",
    "violence",
    "self_harm",
    "hate_or_extremism",
    "illegal_activity",
    "recognizable_real_person_or_public_figure",
    "brand_or_logo",
    "protected_character",
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    full_ai_run_id: str
    provider_name: str
    model_id: str
    ratio: str
    variants_per_beat: int
    target_duration_seconds: int
    scene_count: int
    candidate_count: int
    billable_seconds: int
    max_cost_minor: int
    continuity_mode: str
    terms_snapshot: dict[str, Any]
    pricing_snapshot: dict[str, Any]
    credit_unit_minor: int
    cost_per_second_minor: int
    output_rights_license_basis: str
    brief: str
    direction: str


@dataclass(frozen=True, slots=True)
class _GenerationOutput:
    beat: RetrievalBeat
    variant_index: int
    operation: PaidOperationRecord
    task: RunwayTask
    request_hash: str
    prompt_text: str
    prompt_hash: str
    seed: int
    output_content_hash: str
    output_filename: str
    output_media_type: str
    output_byte_size: int
    artifact: ArtifactRef | None
    verification: dict[str, Any]


class RunwayMediaGenerationCapability:
    """Generate, independently verify, and publish every frozen candidate."""

    def __init__(
        self,
        client: RunwayClient,
        storage: ArtifactStorage,
        ledger: PaidOperationLedger,
        verifier: GeneratedVideoVerifier,
        *,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
        poll_timeout_seconds: float = _POLL_TIMEOUT_SECONDS,
        maximum_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("generated-media worker identity must not be empty")
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("Runway poll interval must be positive and finite")
        if not math.isfinite(poll_timeout_seconds) or poll_timeout_seconds <= 0:
            raise ValueError("Runway poll timeout must be positive and finite")
        if maximum_output_bytes < 1:
            raise ValueError("Runway maximum output bytes must be positive")
        self.client = client
        self.storage = storage
        self.ledger = ledger
        self.verifier = verifier
        self.worker_id = worker_id.strip()
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.maximum_output_bytes = maximum_output_bytes

    async def execute(self, context: StepContext) -> StepResult:
        await context.checkpoint()
        script_ref = _required_artifact(context, "script")
        audio_ref = _required_artifact(context, "audio")
        timing_ref = _required_artifact(context, "narration_timing")
        script = dict(self.storage.read_json(script_ref))
        timing = dict(self.storage.read_json(timing_ref))
        snapshot = _generation_snapshot(context.input_snapshot.to_dict())
        _validate_full_ai_script_contract(script)
        beats = normalize_beats(script)
        beat_timing_durations = _validate_plan_and_timing(
            snapshot,
            beats,
            timing,
            script_ref=script_ref,
            audio_ref=audio_ref,
        )
        _preflight_provider_requests(snapshot, beats)

        outputs: list[_GenerationOutput] = []
        for beat in beats:
            for variant_index in range(snapshot.variants_per_beat):
                await context.checkpoint()
                outputs.append(
                    await self._generate_one(
                        context,
                        snapshot,
                        beat,
                        variant_index,
                        beat_duration_seconds=beat_timing_durations[beat.id],
                    )
                )
        if len(outputs) != snapshot.candidate_count:
            raise PermanentStepError(
                "FULL_AI_PLAN_DRIFT: generated candidate count differs from the frozen plan",
                code="FULL_AI_PLAN_DRIFT",
            )

        audit_payload = _generated_material_manifest(
            snapshot,
            script_ref,
            audio_ref,
            timing_ref,
            outputs,
        )
        audit_ref = self.storage.publish(
            context,
            ProviderArtifact(
                "generated_material_manifest",
                "generated-material-manifest.json",
                "application/json",
                _json_bytes(audit_payload),
            ),
        )
        await _offload(
            self.ledger.settle_run,
            workspace_id=context.workspace_id,
            full_ai_run_id=snapshot.full_ai_run_id,
            expected_candidate_count=snapshot.candidate_count,
            now=_now(),
        )
        missing_beats = [
            beat.id
            for beat in beats
            if not any(
                item.beat.id == beat.id and item.verification["accepted"] is True
                for item in outputs
            )
        ]
        if missing_beats:
            raise PermanentStepError(
                "independent visual verification rejected every variant for one or more Beats",
                code="FULL_AI_VISUAL_COVERAGE_FAILED",
                details={
                    "missing_beat_ids": missing_beats,
                    "generated_material_manifest_artifact_id": audit_ref.id,
                },
            )
        candidate_payload = _candidate_manifest(
            snapshot,
            script_ref,
            audit_ref,
            beats,
            outputs,
        )
        candidate_ref = self.storage.publish(
            context,
            ProviderArtifact(
                "candidate_manifest",
                "candidate-manifest.json",
                "application/json",
                _json_bytes(candidate_payload),
            ),
        )
        await context.checkpoint()
        return StepResult(
            artifacts=(
                *(
                    item.artifact
                    for item in outputs
                    if item.artifact is not None
                ),
                audit_ref,
                candidate_ref,
            ),
            output_summary={
                "provider": snapshot.provider_name,
                "model_id": snapshot.model_id,
                "generated_only": True,
                "continuity_mode": snapshot.continuity_mode,
                "scenes": snapshot.scene_count,
                "candidates": snapshot.candidate_count,
                "accepted_candidates": sum(
                    item.verification["accepted"] is True for item in outputs
                ),
                "rejected_candidates": sum(
                    item.verification["accepted"] is False for item in outputs
                ),
                "billable_seconds": snapshot.billable_seconds,
                "generated_material_manifest_artifact_id": audit_ref.id,
                "candidate_manifest_artifact_id": candidate_ref.id,
                "semantic_verification": "passed",
            },
        )

    async def _generate_one(
        self,
        context: StepContext,
        snapshot: _Snapshot,
        beat: RetrievalBeat,
        variant_index: int,
        *,
        beat_duration_seconds: float,
    ) -> _GenerationOutput:
        prompt = _prompt(snapshot, beat, variant_index)
        prompt_hash = _sha256(prompt.encode("utf-8"))
        seed = int(
            _sha256(
                f"{snapshot.full_ai_run_id}\0{beat.id}\0{variant_index}".encode()
            )[:8],
            16,
        )
        provider_request = RunwayTextVideoRequest(
            prompt_text=prompt,
            ratio=snapshot.ratio,
            duration=_CLIP_SECONDS,
            seed=seed,
            model=snapshot.model_id,
        )
        scene_key = f"beat-{beat.sequence:04d}-{_sha256(beat.id.encode())[:20]}"
        operation_key = f"media.generate:{scene_key}:variant:{variant_index}"
        request_hash = _sha256(
            _json_bytes(
                {
                    "schema_version": "1.0.0",
                    "full_ai_run_id": snapshot.full_ai_run_id,
                    "operation": "media.generate",
                    "operation_key": operation_key,
                    "scene_key": scene_key,
                    "variant_index": variant_index,
                    "provider": snapshot.provider_name,
                    "provider_api_version": RUNWAY_API_VERSION,
                    "model": snapshot.model_id,
                    "request": provider_request.to_payload(),
                    "prompt_hash": prompt_hash,
                    "prompt_template_version": _PROMPT_TEMPLATE_VERSION,
                    "terms_snapshot_hash": snapshot.terms_snapshot["content_hash"],
                    "pricing_snapshot_hash": snapshot.pricing_snapshot["content_hash"],
                }
            )
        )
        authorized = _CLIP_SECONDS * snapshot.cost_per_second_minor
        decision = await _offload(
            self.ledger.reserve,
            workspace_id=context.workspace_id,
            underlying_run_id=context.run_id,
            full_ai_run_id=snapshot.full_ai_run_id,
            scene_key=scene_key,
            variant_index=variant_index,
            operation_key=operation_key,
            request_hash=request_hash,
            provider_name=snapshot.provider_name,
            model_id=snapshot.model_id,
            authorized_amount_minor=authorized,
            now=_now(),
        )
        await context.checkpoint()
        operation, task_id = await self._submit_or_resume(
            context,
            decision,
            provider_request,
            request_hash=request_hash,
            credit_unit_minor=snapshot.credit_unit_minor,
        )
        if operation.status == "succeeded":
            return _output_from_checkpoint(
                context,
                snapshot,
                beat,
                variant_index,
                operation,
                request_hash=request_hash,
                prompt_text=prompt,
                prompt_hash=prompt_hash,
                seed=seed,
                beat_duration_seconds=beat_duration_seconds,
            )
        if task_id is None:
            raise PermanentStepError(
                "paid operation lacks both a durable result and Provider task identity",
                code="FULL_AI_LEDGER_CONTRACT_INVALID",
            )
        task = await self._wait_for_task(
            context,
            operation,
            task_id,
            request_hash,
            credit_unit_minor=snapshot.credit_unit_minor,
        )
        final_credits = task.final_cost_credits
        if final_credits is None:  # guarded by Runway's terminal response parser
            raise PermanentStepError(
                "Runway succeeded without a final cost",
                code="RUNWAY_TASK_RESPONSE_INVALID",
            )
        incurred_amount = final_credits * snapshot.credit_unit_minor
        if incurred_amount > operation.authorized_amount_minor:
            await _offload_durable(
                self.ledger.mark_submit_unknown,
                workspace_id=context.workspace_id,
                operation_id=operation.id,
                request_hash=request_hash,
                lease_token=None,
                provider_request_id=task.task_id,
                error={
                    "code": "FULL_AI_PROVIDER_COST_OVER_QUOTE",
                    "actual_amount_minor": incurred_amount,
                    "authorized_amount_minor": operation.authorized_amount_minor,
                },
                now=_now(),
            )
            raise PermanentStepError(
                "Runway cost exceeded the frozen authorization; manual reconciliation is required",
                code="FULL_AI_RECONCILIATION_REQUIRED",
            )
        await context.checkpoint()
        try:
            video_bytes, media_type = await _offload(
                self.client.download_output,
                task.output_urls[0],
                maximum_bytes=self.maximum_output_bytes,
            )
        except RunwayRetryableError as exc:
            raise RetryableStepError(
                str(exc),
                retry_after_seconds=exc.retry_after_seconds,
                code="RUNWAY_OUTPUT_RETRYABLE",
            ) from exc
        except RunwayPermanentError as exc:
            await self._mark_paid_output_failed(
                context,
                operation,
                request_hash=request_hash,
                provider_request_id=task.task_id,
                incurred_amount_minor=incurred_amount,
                code="RUNWAY_OUTPUT_INVALID",
                message=str(exc),
            )
            raise PermanentStepError(
                str(exc), code="RUNWAY_OUTPUT_INVALID"
            ) from exc
        await context.checkpoint()
        try:
            raw_verification = await self.verifier.verify(
                video_bytes,
                beat=beat,
                expected_duration_seconds=_CLIP_SECONDS,
                ratio=snapshot.ratio,
                context=context,
            )
        except GeneratedVideoVerificationError as exc:
            if exc.retryable:
                raise RetryableStepError(
                    str(exc),
                    code=exc.code,
                    details=exc.evidence,
                ) from exc
            await self._mark_paid_output_failed(
                context,
                operation,
                request_hash=request_hash,
                provider_request_id=task.task_id,
                incurred_amount_minor=incurred_amount,
                code=exc.code,
                message=str(exc),
                evidence=exc.evidence,
            )
            raise PermanentStepError(
                str(exc),
                code=exc.code,
                details=exc.evidence,
            ) from exc
        await context.checkpoint()
        try:
            verification = _validated_verification(
                _verification_mapping(raw_verification),
                beat,
                snapshot.ratio,
                beat_duration_seconds=beat_duration_seconds,
            )
        except PermanentStepError as exc:
            await self._mark_paid_output_failed(
                context,
                operation,
                request_hash=request_hash,
                provider_request_id=task.task_id,
                incurred_amount_minor=incurred_amount,
                code=exc.code or "FULL_AI_VISUAL_VERIFIER_INVALID",
                message=str(exc),
            )
            raise
        filename = (
            f"generated-{beat.sequence:04d}-{_sha256(beat.id.encode())[:10]}-"
            f"v{variant_index + 1:02d}.mp4"
        )
        artifact = (
            self.storage.publish(
                context,
                ProviderArtifact("asset", filename, media_type, video_bytes),
            )
            if verification["accepted"] is True
            else None
        )
        operation = await _offload_durable(
            self.ledger.mark_succeeded,
            workspace_id=context.workspace_id,
            operation_id=operation.id,
            request_hash=request_hash,
            provider_request_id=task.task_id,
            incurred_amount_minor=incurred_amount,
            result={
                "schema_version": "1.0.0",
                "verification_status": (
                    "accepted" if verification["accepted"] else "rejected"
                ),
                "output_content_hash": _sha256(video_bytes),
                "output_byte_size": len(video_bytes),
                "output_media_type": media_type,
                "output_filename": filename,
                "accepted_artifact": (
                    artifact.to_dict() if artifact is not None else None
                ),
                "verification_evidence": verification,
            },
            now=_now(),
        )
        # The accepted ArtifactRef and verification result now have one durable
        # paid-operation checkpoint; only then expose another cancellation point.
        await context.checkpoint()
        return _GenerationOutput(
            beat=beat,
            variant_index=variant_index,
            operation=operation,
            task=task,
            request_hash=request_hash,
            prompt_text=prompt,
            prompt_hash=prompt_hash,
            seed=seed,
            output_content_hash=_sha256(video_bytes),
            output_filename=filename,
            output_media_type=media_type,
            output_byte_size=len(video_bytes),
            artifact=artifact,
            verification=verification,
        )

    async def _submit_or_resume(
        self,
        context: StepContext,
        decision: PaidOperationDecision,
        request: RunwayTextVideoRequest,
        *,
        request_hash: str,
        credit_unit_minor: int,
    ) -> tuple[PaidOperationRecord, str | None]:
        operation = decision.record
        action = decision.action
        if action is PaidOperationAction.BUSY:
            raise RetryableStepError(
                "the paid operation is held by another live submit lease",
                retry_after_seconds=min(10.0, self.poll_interval_seconds),
                code="FULL_AI_PAID_OPERATION_BUSY",
            )
        if action is PaidOperationAction.MANUAL_RECONCILIATION_REQUIRED:
            raise PermanentStepError(
                "a prior Runway submission has no recoverable task identity; manual reconciliation is required",
                code="FULL_AI_RECONCILIATION_REQUIRED",
            )
        if (
            action is PaidOperationAction.TERMINAL
            and operation.status == "succeeded"
            and operation.provider_request_id
        ):
            # Provider completion and billing are durable, while the Run-scoped
            # output/audit artifacts may still need deterministic reconstruction
            # after a process or object-storage failure.  Replay only GETs the
            # known task; it never submits a new paid request.
            if operation.result is None:
                raise PermanentStepError(
                    "succeeded paid operation lacks its durable output checkpoint",
                    code="FULL_AI_LEDGER_CONTRACT_INVALID",
                )
            return operation, None
        if action is PaidOperationAction.TERMINAL:
            raise PermanentStepError(
                f"paid generation operation is terminal ({operation.status})",
                code="FULL_AI_PAID_OPERATION_TERMINAL",
            )
        if action in {
            PaidOperationAction.POLL_EXISTING,
            PaidOperationAction.RECONCILE_EXISTING,
        }:
            if not operation.provider_request_id:
                raise PermanentStepError(
                    "paid generation reconciliation lacks a provider task identity",
                    code="FULL_AI_RECONCILIATION_REQUIRED",
                )
            return operation, operation.provider_request_id
        if action is not PaidOperationAction.SUBMIT_NEW:
            raise PermanentStepError(
                "paid operation ledger returned an unknown action",
                code="FULL_AI_LEDGER_CONTRACT_INVALID",
            )

        lease_token = str(uuid4())
        operation = await _offload(
            self.ledger.begin_submit,
            workspace_id=context.workspace_id,
            operation_id=operation.id,
            request_hash=request_hash,
            lease_owner=self.worker_id,
            lease_token=lease_token,
            lease_seconds=_SUBMIT_LEASE_SECONDS,
            now=_now(),
        )
        await context.checkpoint()
        try:
            receipt = await _offload(self.client.submit_text_to_video, request)
        except RunwayRetryableError as exc:
            await _offload(
                self.ledger.release_definite_rejection,
                workspace_id=context.workspace_id,
                operation_id=operation.id,
                request_hash=request_hash,
                lease_token=lease_token,
                error={
                    "code": "RUNWAY_DEFINITE_REJECTION",
                    "definite_rejection": True,
                    "message": str(exc),
                },
                now=_now(),
            )
            raise RetryableStepError(
                str(exc),
                retry_after_seconds=exc.retry_after_seconds,
                code="RUNWAY_DEFINITE_REJECTION",
            ) from exc
        except RunwaySubmitUnknown as exc:
            await _offload(
                self.ledger.mark_submit_unknown,
                workspace_id=context.workspace_id,
                operation_id=operation.id,
                request_hash=request_hash,
                lease_token=lease_token,
                provider_request_id=None,
                error={"code": "RUNWAY_SUBMIT_UNKNOWN", "message": str(exc)},
                now=_now(),
            )
            raise PermanentStepError(
                "Runway submission outcome is unknown; automatic resubmission is forbidden",
                code="FULL_AI_RECONCILIATION_REQUIRED",
            ) from exc
        except RunwayPermanentError as exc:
            await _offload(
                self.ledger.mark_failed,
                workspace_id=context.workspace_id,
                operation_id=operation.id,
                request_hash=request_hash,
                provider_request_id=None,
                incurred_amount_minor=0,
                cancelled=False,
                error={"code": "RUNWAY_SUBMIT_REJECTED", "message": str(exc)},
                now=_now(),
            )
            raise PermanentStepError(
                str(exc), code="RUNWAY_SUBMIT_REJECTED"
            ) from exc
        estimated_amount = receipt.estimated_cost_credits * credit_unit_minor
        if estimated_amount > operation.authorized_amount_minor + 1e-9:
            await _offload_durable(
                self.ledger.mark_submit_unknown,
                workspace_id=context.workspace_id,
                operation_id=operation.id,
                request_hash=request_hash,
                lease_token=lease_token,
                provider_request_id=receipt.task_id,
                error={
                    "code": "FULL_AI_PROVIDER_ESTIMATE_OVER_QUOTE",
                    "estimated_amount_minor": estimated_amount,
                    "authorized_amount_minor": operation.authorized_amount_minor,
                    "automatic_resubmission_forbidden": True,
                },
                now=_now(),
            )
            raise PermanentStepError(
                "Runway estimate exceeded the frozen authorization; the known task requires reconciliation",
                code="FULL_AI_RECONCILIATION_REQUIRED",
            )
        operation = await _offload_durable(
            self.ledger.mark_submitted,
            workspace_id=context.workspace_id,
            operation_id=operation.id,
            request_hash=request_hash,
            lease_token=lease_token,
            provider_request_id=receipt.task_id,
            now=_now(),
        )
        # Once a paid POST returns a task id, its durable identity must be
        # committed before any cooperative cancellation/heartbeat boundary.
        await context.checkpoint()
        return operation, receipt.task_id

    async def _mark_paid_output_failed(
        self,
        context: StepContext,
        operation: PaidOperationRecord,
        *,
        request_hash: str,
        provider_request_id: str,
        incurred_amount_minor: int,
        code: str,
        message: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        await _offload_durable(
            self.ledger.mark_failed,
            workspace_id=context.workspace_id,
            operation_id=operation.id,
            request_hash=request_hash,
            provider_request_id=provider_request_id,
            incurred_amount_minor=incurred_amount_minor,
            cancelled=False,
            error={
                "code": code,
                "message": message,
                "provider_task_succeeded": True,
                "local_evidence": dict(evidence or {}),
            },
            now=_now(),
        )

    async def _wait_for_task(
        self,
        context: StepContext,
        operation: PaidOperationRecord,
        task_id: str,
        request_hash: str,
        *,
        credit_unit_minor: int,
    ) -> RunwayTask:
        deadline = time.monotonic() + self.poll_timeout_seconds
        while True:
            await context.checkpoint()
            try:
                task = await _offload(self.client.get_task, task_id)
            except RunwayRetryableError as exc:
                raise RetryableStepError(
                    str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                    code="RUNWAY_TASK_QUERY_RETRYABLE",
                ) from exc
            except RunwayPermanentError as exc:
                await _offload(
                    self.ledger.mark_submit_unknown,
                    workspace_id=context.workspace_id,
                    operation_id=operation.id,
                    request_hash=request_hash,
                    lease_token=None,
                    provider_request_id=task_id,
                    error={"code": "RUNWAY_TASK_QUERY_INVALID", "message": str(exc)},
                    now=_now(),
                )
                raise PermanentStepError(
                    "Runway task could not be reconciled safely",
                    code="FULL_AI_RECONCILIATION_REQUIRED",
                ) from exc
            if task.status is RunwayTaskStatus.SUCCEEDED:
                return task
            if task.status in {RunwayTaskStatus.FAILED, RunwayTaskStatus.CANCELLED}:
                incurred_credits = task.final_cost_credits or 0
                incurred_amount = incurred_credits * credit_unit_minor
                if incurred_amount > operation.authorized_amount_minor:
                    await _offload_durable(
                        self.ledger.mark_submit_unknown,
                        workspace_id=context.workspace_id,
                        operation_id=operation.id,
                        request_hash=request_hash,
                        lease_token=None,
                        provider_request_id=task_id,
                        error={
                            "code": "FULL_AI_PROVIDER_COST_OVER_QUOTE",
                            "actual_amount_minor": incurred_amount,
                            "authorized_amount_minor": operation.authorized_amount_minor,
                        },
                        now=_now(),
                    )
                    raise PermanentStepError(
                        "failed Runway task exceeded its frozen authorization",
                        code="FULL_AI_RECONCILIATION_REQUIRED",
                    )
                await _offload_durable(
                    self.ledger.mark_failed,
                    workspace_id=context.workspace_id,
                    operation_id=operation.id,
                    request_hash=request_hash,
                    provider_request_id=task.task_id,
                    incurred_amount_minor=incurred_amount,
                    cancelled=task.status is RunwayTaskStatus.CANCELLED,
                    error={
                        "code": task.failure_code or task.status.value,
                        "message": task.failure or task.status.value,
                        "provider_cost_credits": incurred_credits,
                    },
                    now=_now(),
                )
                raise PermanentStepError(
                    task.failure or f"Runway task {task.status.value.casefold()}",
                    code="RUNWAY_TASK_FAILED",
                )
            if time.monotonic() >= deadline:
                raise RetryableStepError(
                    "Runway task is still active after the local polling window",
                    retry_after_seconds=self.poll_interval_seconds,
                    code="RUNWAY_TASK_STILL_ACTIVE",
                )
            await asyncio.sleep(self.poll_interval_seconds)


def _output_from_checkpoint(
    context: StepContext,
    snapshot: _Snapshot,
    beat: RetrievalBeat,
    variant_index: int,
    operation: PaidOperationRecord,
    *,
    request_hash: str,
    prompt_text: str,
    prompt_hash: str,
    seed: int,
    beat_duration_seconds: float,
) -> _GenerationOutput:
    """Rebuild a prior candidate from the local paid-result checkpoint only."""

    result = operation.result
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
    if (
        operation.status != "succeeded"
        or not operation.provider_request_id
        or not isinstance(result, Mapping)
        or set(result) != expected_keys
        or result.get("schema_version") != "1.0.0"
    ):
        raise _invalid_checkpoint("succeeded operation lacks a canonical result")
    status = result.get("verification_status")
    output_hash = result.get("output_content_hash")
    output_size = result.get("output_byte_size")
    media_type = result.get("output_media_type")
    filename = result.get("output_filename")
    if (
        status not in {"accepted", "rejected"}
        or not isinstance(output_hash, str)
        or _SHA256.fullmatch(output_hash) is None
        or isinstance(output_size, bool)
        or not isinstance(output_size, int)
        or output_size < 1
        or not isinstance(media_type, str)
        or not media_type.strip()
        or len(media_type) > 120
        or not isinstance(filename, str)
        or not filename.strip()
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
    ):
        raise _invalid_checkpoint("generated output identity is invalid")
    raw_verification = result.get("verification_evidence")
    if not isinstance(raw_verification, Mapping) or not raw_verification:
        raise _invalid_checkpoint("visual verification evidence is missing")
    verification = _checkpoint_verification(
        raw_verification,
        beat,
        snapshot.ratio,
        beat_duration_seconds=beat_duration_seconds,
    )
    if (
        verification["accepted"] is not (status == "accepted")
    ):
        raise _invalid_checkpoint("visual verification checkpoint is non-canonical")

    artifact: ArtifactRef | None = None
    raw_artifact = result.get("accepted_artifact")
    if status == "accepted":
        if not isinstance(raw_artifact, Mapping) or set(raw_artifact) != set(
            ArtifactRef.__dataclass_fields__
        ):
            raise _invalid_checkpoint("accepted output lacks its complete ArtifactRef")
        try:
            artifact = ArtifactRef.from_mapping(raw_artifact)
            UUID(artifact.id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise _invalid_checkpoint("accepted ArtifactRef is invalid") from exc
        if (
            artifact.workspace_id != context.workspace_id
            or artifact.run_id != context.run_id
            or artifact.kind != "asset"
            or artifact.content_hash != output_hash
            or artifact.byte_size != output_size
            or artifact.media_type != media_type
            or artifact.filename != filename
        ):
            raise _invalid_checkpoint(
                "accepted ArtifactRef does not identify this generated output"
            )
    elif raw_artifact is not None:
        raise _invalid_checkpoint("rejected output must not publish an Artifact")

    if (
        operation.incurred_amount_minor < 0
        or operation.incurred_amount_minor > operation.authorized_amount_minor
        or operation.incurred_amount_minor % snapshot.credit_unit_minor
    ):
        raise _invalid_checkpoint("settled Provider cost is invalid")
    final_credits = operation.incurred_amount_minor // snapshot.credit_unit_minor
    task = RunwayTask(
        task_id=operation.provider_request_id,
        status=RunwayTaskStatus.SUCCEEDED,
        created_at="durable-ledger-checkpoint",
        estimated_cost_credits=(
            operation.authorized_amount_minor / snapshot.credit_unit_minor
        ),
        final_cost_credits=final_credits,
    )
    return _GenerationOutput(
        beat=beat,
        variant_index=variant_index,
        operation=operation,
        task=task,
        request_hash=request_hash,
        prompt_text=prompt_text,
        prompt_hash=prompt_hash,
        seed=seed,
        output_content_hash=output_hash,
        output_filename=filename,
        output_media_type=media_type,
        output_byte_size=output_size,
        artifact=artifact,
        verification=verification,
    )


def _invalid_checkpoint(message: str) -> PermanentStepError:
    return PermanentStepError(
        f"FULL_AI_LEDGER_CONTRACT_INVALID: {message}",
        code="FULL_AI_LEDGER_CONTRACT_INVALID",
    )


def _checkpoint_verification(
    value: Mapping[str, Any],
    beat: RetrievalBeat,
    ratio: str,
    *,
    beat_duration_seconds: float,
) -> dict[str, Any]:
    """Strictly rehydrate canonical ledger evidence without any Provider call."""

    expected_keys = {
        "verifier_provider",
        "verifier_model",
        "verifier_version",
        "frame_hashes",
        "description",
        "labels",
        "confidence",
        "minimum_confidence",
        "quality_score",
        "minimum_quality_score",
        "visual_description_matched",
        "visual_description_evidence",
        "must_match",
        "must_not_match",
        "has_watermark",
        "has_embedded_text",
        "unsafe",
        *_SAFETY_KEYS,
        "quality_usable",
        "cut_safe",
        "cut_safe_evidence",
        "semantic_complete",
        "safety",
        "duration_seconds",
        "width",
        "height",
        "codec",
        "accepted",
        "rejection_codes",
    }
    if set(value) != expected_keys:
        raise _invalid_checkpoint("visual verification field set is invalid")
    try:
        canonical = _validated_verification(
            value,
            beat,
            ratio,
            beat_duration_seconds=beat_duration_seconds,
        )
    except (PermanentStepError, TypeError, ValueError) as exc:
        raise _invalid_checkpoint("visual verification evidence is invalid") from exc
    if _json_bytes(canonical) != _json_bytes(dict(value)):
        raise _invalid_checkpoint("visual verification evidence is non-canonical")
    return canonical


def _generation_snapshot(snapshot: Mapping[str, Any]) -> _Snapshot:
    framefactory = _mapping(snapshot.get("_framefactory"), "_framefactory")
    composition = _mapping(
        framefactory.get("composition_snapshot"),
        "_framefactory.composition_snapshot",
    )
    raw = _mapping(
        composition.get("full_ai_generation"),
        "_framefactory.composition_snapshot.full_ai_generation",
    )
    full_ai_run_id = _uuid(raw.get("full_ai_run_id"), "full_ai_run_id")
    if _uuid(snapshot.get("full_ai_run_id"), "input.full_ai_run_id") != full_ai_run_id:
        raise _plan_drift("Full-AI Run identity differs across immutable snapshots")
    provider = str(raw.get("provider_name") or "").strip().casefold()
    model = str(raw.get("model_id") or "").strip()
    ratio = str(raw.get("ratio") or "").strip()
    continuity = str(raw.get("continuity_mode") or "").strip()
    if provider != "runway" or model != "gen4.5":
        raise PermanentStepError(
            "the generated-only Worker supports only Runway gen4.5",
            code="FULL_AI_PROVIDER_UNSUPPORTED",
        )
    if ratio not in {"1280:720", "720:1280"}:
        raise _plan_drift("Runway ratio is outside the quoted P1 profile")
    if continuity not in {"none", "prompt_pack"}:
        raise _plan_drift("continuity mode overstates the implemented P1 capability")
    terms = _snapshot_reference(raw.get("terms_snapshot"), "terms_snapshot")
    pricing = _snapshot_reference(raw.get("pricing_snapshot"), "pricing_snapshot")
    if pricing.get("currency") != "USD":
        raise _plan_drift("pricing currency must be USD")
    credit_unit = _integer(pricing.get("credit_unit_minor"), "credit_unit_minor")
    cost_per_second = _integer(
        pricing.get("cost_per_second_minor"), "cost_per_second_minor"
    )
    if credit_unit != 1 or cost_per_second < 1:
        raise _plan_drift("pricing units are inconsistent with the frozen Runway quote")
    if raw.get("output_rights_confirmed") is not True:
        raise PermanentStepError(
            "provider output rights were not explicitly confirmed before Run creation",
            code="FULL_AI_OUTPUT_RIGHTS_NOT_CONFIRMED",
        )
    rights_basis = str(raw.get("output_rights_license_basis") or "").strip()
    if not rights_basis or len(rights_basis) > 1_000:
        raise PermanentStepError(
            "provider output-rights attestation lacks a bounded license basis",
            code="FULL_AI_OUTPUT_RIGHTS_BASIS_MISSING",
        )
    return _Snapshot(
        full_ai_run_id=full_ai_run_id,
        provider_name=provider,
        model_id=model,
        ratio=ratio,
        variants_per_beat=_integer(raw.get("variants_per_beat"), "variants_per_beat"),
        target_duration_seconds=_integer(
            raw.get("target_duration_seconds"), "target_duration_seconds"
        ),
        scene_count=_integer(raw.get("scene_count"), "scene_count"),
        candidate_count=_integer(raw.get("candidate_count"), "candidate_count"),
        billable_seconds=_integer(raw.get("billable_seconds"), "billable_seconds"),
        max_cost_minor=_integer(raw.get("max_cost_minor"), "max_cost_minor"),
        continuity_mode=continuity,
        terms_snapshot=terms,
        pricing_snapshot=pricing,
        credit_unit_minor=credit_unit,
        cost_per_second_minor=cost_per_second,
        output_rights_license_basis=rights_basis,
        brief=str(snapshot.get("brief") or "").strip(),
        direction=str(snapshot.get("direction") or "").strip(),
    )


def _validate_plan_and_timing(
    snapshot: _Snapshot,
    beats: Sequence[RetrievalBeat],
    timing: Mapping[str, Any],
    *,
    script_ref: ArtifactRef,
    audio_ref: ArtifactRef,
) -> dict[str, float]:
    expected_scenes = math.ceil(snapshot.target_duration_seconds / _CLIP_SECONDS)
    if (
        snapshot.target_duration_seconds < 1
        or snapshot.scene_count != expected_scenes
        or len(beats) != expected_scenes
        or not 1 <= snapshot.variants_per_beat <= 3
        or snapshot.candidate_count
        != snapshot.scene_count * snapshot.variants_per_beat
        or snapshot.billable_seconds != snapshot.candidate_count * _CLIP_SECONDS
    ):
        raise _plan_drift("Beat/candidate/billable counts differ from the quoted plan")
    quoted_cost = snapshot.billable_seconds * snapshot.cost_per_second_minor
    if snapshot.max_cost_minor < quoted_cost:
        raise _plan_drift("maximum authorized cost is below the frozen quote")
    if timing.get("operation") != "audio.synthesize":
        raise _plan_drift("narration timing operation is not audio.synthesize")
    if str(timing.get("script_content_hash") or "") != script_ref.content_hash:
        raise _plan_drift("narration timing does not belong to the generated script")
    if str(timing.get("audio_content_hash") or "") != audio_ref.content_hash:
        raise _plan_drift("narration timing does not belong to the supplied audio")
    if timing.get("estimated") is not False or timing.get(
        "word_timing_estimated"
    ) is not False:
        raise _timing_unverified("native top-level timing evidence is required")
    if not str(timing.get("source") or "").strip():
        raise _timing_unverified("timing source is missing")
    raw_beats = timing.get("beats")
    if (
        not isinstance(raw_beats, Sequence)
        or isinstance(raw_beats, (str, bytes))
        or len(raw_beats) != len(beats)
    ):
        raise _plan_drift("narration timing does not cover every generated Beat")
    cursor = 0.0
    durations: dict[str, float] = {}
    for expected, raw_timing in zip(beats, raw_beats, strict=True):
        if not isinstance(raw_timing, Mapping):
            raise _plan_drift("narration timing Beat is not an object")
        alignment_source = str(raw_timing.get("alignment_source") or "").strip()
        if raw_timing.get("estimated") is not False or not alignment_source.startswith(
            "native_"
        ):
            raise _timing_unverified(
                f"Beat {expected.id} lacks native alignment evidence"
            )
        start = _finite_number(raw_timing.get("start_seconds"))
        end = _finite_number(raw_timing.get("end_seconds"))
        if (
            str(raw_timing.get("id") or "") != expected.id
            or raw_timing.get("sequence") != expected.sequence
            or _identity_text(raw_timing.get("text"))
            != _identity_text(expected.narration)
            or start is None
            or end is None
            or abs(start - cursor) > 0.001
            or end <= start
            or end - start > _CLIP_SECONDS + 0.001
        ):
            raise _plan_drift(
                f"narration timing for Beat {expected.id} cannot fit one five-second clip"
            )
        durations[expected.id] = end - start
        cursor = end
    declared_duration = _finite_number(timing.get("duration_seconds"))
    if (
        declared_duration is None
        or abs(cursor - declared_duration) > 0.001
        or declared_duration > snapshot.scene_count * _CLIP_SECONDS + 0.001
    ):
        raise _plan_drift("narration duration exceeds the frozen clip plan")
    return durations


def _validate_full_ai_script_contract(script: Mapping[str, Any]) -> None:
    raw_beats = script.get("beats")
    if not isinstance(raw_beats, Sequence) or isinstance(raw_beats, (str, bytes)):
        raise _plan_drift("generated script Beats must be an array")
    for index, raw_beat in enumerate(raw_beats, start=1):
        if not isinstance(raw_beat, Mapping):
            raise _plan_drift(f"generated script Beat {index} must be an object")
        for field in ("must_match", "must_not_match"):
            raw_terms = raw_beat.get(field)
            if not isinstance(raw_terms, Sequence) or isinstance(
                raw_terms, (str, bytes)
            ):
                raise _plan_drift(f"generated script Beat {index}.{field} must be an array")
            terms = [str(item).strip() for item in raw_terms]
            if (
                len(terms) > 12
                or any(not item or len(item) > 240 for item in terms)
                or len(set(terms)) != len(terms)
            ):
                raise _plan_drift(
                    f"generated script Beat {index}.{field} exceeds the verifier contract"
                )


def _preflight_provider_requests(
    snapshot: _Snapshot, beats: Sequence[RetrievalBeat]
) -> None:
    """Prove every paid request can be constructed before reserving the first one."""

    for beat in beats:
        for variant_index in range(snapshot.variants_per_beat):
            _prompt(snapshot, beat, variant_index)


def _prompt(snapshot: _Snapshot, beat: RetrievalBeat, variant_index: int) -> str:
    continuity = (
        " Preserve the same subject, wardrobe, palette, environment and camera bible "
        "described in this scene; this is prompt-pack continuity, not identity locking."
        if snapshot.continuity_mode == "prompt_pack"
        else ""
    )
    constraints = ""
    if beat.must_match:
        constraints += " Must visibly include: " + "; ".join(beat.must_match) + "."
    if beat.must_not_match:
        constraints += " Must not include: " + "; ".join(beat.must_not_match) + "."
    suffix = (
        f" Direction: {snapshot.direction or 'cinematic'}. Variant {variant_index + 1}."
        f"{continuity}{constraints} No captions, embedded text, logos, or watermarks."
    )
    budget = 1_000 - _utf16_units(suffix)
    visual = _truncate_utf16(beat.visual_description, budget)
    prompt = visual.strip() + suffix
    if not visual.strip() or _utf16_units(prompt) > 1_000:
        raise _plan_drift(f"Beat {beat.id} cannot fit the provider prompt contract")
    return prompt


def _verification_mapping(value: object) -> dict[str, Any]:
    """Adapt the concrete verifier result into the manifest producer contract."""

    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        serialize = getattr(value, "to_dict", None)
        if not callable(serialize):
            raise PermanentStepError(
                "generated-video verifier returned an unsupported value",
                code="FULL_AI_VISUAL_VERIFIER_INVALID",
            )
        serialized = serialize()
        if not isinstance(serialized, Mapping):
            raise PermanentStepError(
                "generated-video verifier serialization is not an object",
                code="FULL_AI_VISUAL_VERIFIER_INVALID",
            )
        raw = dict(serialized)
    if "constraint_checks" not in raw:
        return raw

    raw_checks = raw.get("constraint_checks")
    if not isinstance(raw_checks, Sequence) or isinstance(raw_checks, (str, bytes)):
        raise PermanentStepError(
            "generated-video verifier constraint checks are invalid",
            code="FULL_AI_VISUAL_VERIFIER_INVALID",
        )
    must_match: list[dict[str, Any]] = []
    must_not_match: list[dict[str, Any]] = []
    for item in raw_checks:
        if not isinstance(item, Mapping):
            raise PermanentStepError(
                "generated-video verifier constraint check is not an object",
                code="FULL_AI_VISUAL_VERIFIER_INVALID",
            )
        target = (
            must_match
            if item.get("kind") == "must_match"
            else must_not_match
            if item.get("kind") == "must_not_match"
            else None
        )
        if target is not None:
            target.append(
                {"term": item.get("term"), "matched": item.get("matched")}
            )
    evidence = raw.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    quality = evidence.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    visual_description_check = evidence.get("visual_description_check")
    visual_description_check = (
        visual_description_check
        if isinstance(visual_description_check, Mapping)
        else {}
    )
    safety = raw.get("safety")
    safety = safety if isinstance(safety, Mapping) else {}
    return {
        "duration_seconds": raw.get("duration_seconds"),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "codec": raw.get("codec"),
        "description": raw.get("description"),
        "labels": raw.get("labels"),
        "confidence": raw.get("confidence"),
        "minimum_confidence": evidence.get("minimum_confidence"),
        "quality_score": quality.get("score"),
        "minimum_quality_score": evidence.get("minimum_quality_score"),
        "visual_description_matched": visual_description_check.get("matched"),
        "visual_description_evidence": visual_description_check.get("evidence"),
        "must_match": must_match,
        "must_not_match": must_not_match,
        "has_watermark": raw.get("has_watermark"),
        "has_embedded_text": raw.get("has_embedded_text"),
        "unsafe": raw.get("unsafe"),
        "safety": safety,
        "quality_usable": quality.get("usable"),
        "cut_safe": raw.get("cut_safe"),
        "cut_safe_evidence": evidence.get("cut_safe_evidence"),
        "semantic_complete": raw.get("semantic_complete"),
        "verifier_provider": raw.get("provider"),
        "verifier_model": raw.get("model"),
        "verifier_version": "generated-video-verifier-v1",
        "frame_hashes": raw.get("frame_hashes"),
    }


def _validated_verification(
    value: Mapping[str, Any],
    beat: RetrievalBeat,
    ratio: str,
    *,
    beat_duration_seconds: float,
) -> dict[str, Any]:
    description = str(value.get("description") or "").strip()
    confidence = _unit_score(value.get("confidence"))
    minimum_confidence = _unit_score(value.get("minimum_confidence"), positive=True)
    quality_score = _unit_score(value.get("quality_score"))
    minimum_quality_score = _unit_score(
        value.get("minimum_quality_score"), positive=True
    )
    visual_description_matched = value.get("visual_description_matched")
    visual_description_evidence = str(
        value.get("visual_description_evidence") or ""
    ).strip()
    cut_safe_evidence = str(value.get("cut_safe_evidence") or "").strip()
    raw_labels = value.get("labels")
    labels = (
        tuple(dict.fromkeys(str(item).strip() for item in raw_labels if str(item).strip()))
        if isinstance(raw_labels, Sequence) and not isinstance(raw_labels, (str, bytes))
        else ()
    )
    must_match = _constraint_evidence(value.get("must_match"), beat.must_match)
    must_not_match = _constraint_evidence(value.get("must_not_match"), beat.must_not_match)
    haystack = " ".join((description, *labels)).casefold()
    expected_match = tuple((term, term.casefold() in haystack) for term in beat.must_match)
    expected_not = tuple((term, term.casefold() in haystack) for term in beat.must_not_match)
    width = _positive_integer(value.get("width"))
    height = _positive_integer(value.get("height"))
    duration = _finite_number(value.get("duration_seconds"))
    codec = str(value.get("codec") or "").strip()
    expected_ratio = (1280, 720) if ratio == "1280:720" else (720, 1280)
    frame_hashes = _sha_list(value.get("frame_hashes"))
    safety = _mapping(value.get("safety"), "verification.safety")
    booleans = {
        "has_watermark": value.get("has_watermark"),
        "has_embedded_text": value.get("has_embedded_text"),
        "unsafe": value.get("unsafe"),
        **{key: safety.get(key) for key in _SAFETY_KEYS},
        "quality_usable": value.get("quality_usable"),
        "cut_safe": value.get("cut_safe"),
        "semantic_complete": value.get("semantic_complete"),
    }
    contract_valid = (
        bool(description)
        and confidence is not None
        and minimum_confidence is not None
        and quality_score is not None
        and minimum_quality_score is not None
        and isinstance(visual_description_matched, bool)
        and bool(visual_description_evidence)
        and bool(cut_safe_evidence)
        and bool(frame_hashes)
        and width is not None
        and height is not None
        and duration is not None
        and duration > 0
        and bool(codec)
        and must_match == expected_match
        and must_not_match == expected_not
        and all(isinstance(flag, bool) for flag in booleans.values())
        and set(safety) == set(_SAFETY_KEYS)
        and (
            booleans["unsafe"] is True
            or not any(bool(safety[key]) for key in _SAFETY_KEYS)
        )
        and bool(str(value.get("verifier_provider") or "").strip())
        and bool(str(value.get("verifier_model") or "").strip())
        and bool(str(value.get("verifier_version") or "").strip())
    )
    if not contract_valid:
        raise PermanentStepError(
            f"independent visual verifier returned an invalid result for Beat {beat.id}",
            code="FULL_AI_VISUAL_VERIFIER_INVALID",
        )
    rejection_codes: list[str] = []
    if visual_description_matched is False:
        rejection_codes.append("visual_description_mismatch")
    if confidence < minimum_confidence:
        rejection_codes.append("low_visual_confidence")
    if booleans["quality_usable"] is False or quality_score < minimum_quality_score:
        rejection_codes.append("visual_quality_unusable")
    if width * expected_ratio[1] != height * expected_ratio[0]:
        rejection_codes.append("generated_ratio_mismatch")
    if not _duration_is_five_seconds(duration):
        rejection_codes.append("generated_duration_out_of_bounds")
    if duration + 1e-6 < beat_duration_seconds:
        rejection_codes.append("generated_duration_insufficient_for_beat")
    if not all(matched for _term, matched in must_match):
        rejection_codes.append("must_match_missing")
    if any(matched for _term, matched in must_not_match):
        rejection_codes.append("must_not_match_present")
    if booleans["has_watermark"] is True:
        rejection_codes.append("watermark_detected")
    if booleans["has_embedded_text"] is True:
        rejection_codes.append("embedded_text_detected")
    if booleans["unsafe"] is True:
        rejection_codes.append("unsafe_content")
    for key in _SAFETY_KEYS:
        if booleans[key] is True:
            rejection_codes.append(f"{key}_detected")
    if booleans["cut_safe"] is False:
        rejection_codes.append("cut_unsafe")
    if booleans["semantic_complete"] is False:
        rejection_codes.append("semantic_incomplete")
    return {
        "verifier_provider": str(value["verifier_provider"]),
        "verifier_model": str(value["verifier_model"]),
        "verifier_version": str(value["verifier_version"]),
        "frame_hashes": list(frame_hashes),
        "description": description,
        "labels": list(labels[:256]),
        "confidence": confidence,
        "minimum_confidence": minimum_confidence,
        "quality_score": quality_score,
        "minimum_quality_score": minimum_quality_score,
        "visual_description_matched": visual_description_matched,
        "visual_description_evidence": visual_description_evidence,
        "must_match": [
            {"term": term, "matched": matched} for term, matched in must_match
        ],
        "must_not_match": [
            {"term": term, "matched": matched} for term, matched in must_not_match
        ],
        **booleans,
        "safety": {key: booleans[key] for key in _SAFETY_KEYS},
        "cut_safe_evidence": cut_safe_evidence,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "codec": codec,
        "accepted": not rejection_codes,
        "rejection_codes": list(dict.fromkeys(rejection_codes)),
    }


def _generated_material_manifest(
    snapshot: _Snapshot,
    script_ref: ArtifactRef,
    audio_ref: ArtifactRef,
    timing_ref: ArtifactRef,
    outputs: Sequence[_GenerationOutput],
) -> dict[str, Any]:
    authorized = sum(item.operation.authorized_amount_minor for item in outputs)
    incurred = sum(item.operation.incurred_amount_minor for item in outputs)
    estimated_credits = sum(
        item.operation.authorized_amount_minor / snapshot.credit_unit_minor
        for item in outputs
    )
    incurred_credits = sum(item.task.final_cost_credits or 0 for item in outputs)
    return {
        "schema_version": "1.0.0",
        "operation": "media.generate",
        "generated_only": True,
        "provider": snapshot.provider_name,
        "provider_api_version": RUNWAY_API_VERSION,
        "model_id": snapshot.model_id,
        "source_script_artifact_id": script_ref.id,
        "source_audio_artifact_id": audio_ref.id,
        "source_narration_timing_artifact_id": timing_ref.id,
        "plan": {
            "target_duration_seconds": snapshot.target_duration_seconds,
            "scene_count": snapshot.scene_count,
            "variants_per_beat": snapshot.variants_per_beat,
            "candidate_count": snapshot.candidate_count,
            "clip_duration_seconds": _CLIP_SECONDS,
            "billable_seconds": snapshot.billable_seconds,
            "max_cost_minor": snapshot.max_cost_minor,
            "ratio": snapshot.ratio,
        },
        "continuity_mode": snapshot.continuity_mode,
        "terms_snapshot": snapshot.terms_snapshot,
        "rights_snapshot": {
            "output_rights_confirmed": True,
            "output_rights_license_basis": snapshot.output_rights_license_basis,
            "terms_content_hash": snapshot.terms_snapshot["content_hash"],
        },
        "pricing_snapshot": snapshot.pricing_snapshot,
        "safety": {
            "provider_moderation": "required",
            "accepted_outputs": sum(
                item.verification["accepted"] is True for item in outputs
            ),
            "rejected_outputs": sum(
                item.verification["accepted"] is False for item in outputs
            ),
        },
        "total_cost": {
            "estimated_credits": estimated_credits,
            "incurred_credits": incurred_credits,
            "incurred_amount_minor": incurred,
            "authorized_amount_minor": authorized,
            "currency": snapshot.pricing_snapshot["currency"],
        },
        "jobs": [
            {
                "beat_id": item.beat.id,
                "beat_sequence": item.beat.sequence,
                "variant": item.variant_index + 1,
                "paid_operation_id": item.operation.id,
                "operation_key": (
                    f"media.generate:beat-{item.beat.sequence:04d}-"
                    f"{_sha256(item.beat.id.encode())[:20]}:variant:{item.variant_index}"
                ),
                "request_hash": item.request_hash,
                "provider_task_id": item.task.task_id,
                "status": "succeeded",
                "model_id": snapshot.model_id,
                "seed": item.seed,
                "prompt_hash": item.prompt_hash,
                "prompt_text": item.prompt_text,
                "prompt_template_version": _PROMPT_TEMPLATE_VERSION,
                "reference_hashes": [],
                "output_artifact_id": (
                    item.artifact.id if item.artifact is not None else None
                ),
                "output_content_hash": item.output_content_hash,
                "output_filename": item.output_filename,
                "output_media_type": item.output_media_type,
                "output_byte_size": item.output_byte_size,
                "duration_seconds": item.verification["duration_seconds"],
                "estimated_cost_credits": (
                    item.operation.authorized_amount_minor
                    / snapshot.credit_unit_minor
                ),
                "incurred_cost_credits": item.task.final_cost_credits or 0,
                "verification_status": (
                    "accepted" if item.verification["accepted"] else "rejected"
                ),
                "rejection_codes": item.verification["rejection_codes"],
                "safety": {
                    "status": (
                        "rejected" if _safety_failure_code(item.verification) else "passed"
                    ),
                    "provider_moderation": "not_rejected",
                    "failure_code": _safety_failure_code(item.verification),
                },
                "visual_verification": {
                    key: item.verification[key]
                    for key in (
                        "verifier_provider",
                        "verifier_model",
                        "verifier_version",
                        "frame_hashes",
                        "description",
                        "labels",
                        "confidence",
                        "minimum_confidence",
                        "quality_score",
                        "minimum_quality_score",
                        "visual_description_matched",
                        "visual_description_evidence",
                        "must_match",
                        "must_not_match",
                        "has_watermark",
                        "has_embedded_text",
                        "unsafe",
                        *_SAFETY_KEYS,
                        "quality_usable",
                        "cut_safe",
                        "cut_safe_evidence",
                        "semantic_complete",
                    )
                },
            }
            for item in outputs
        ],
    }


def _safety_failure_code(verification: Mapping[str, Any]) -> str | None:
    for key in _SAFETY_KEYS:
        if verification.get(key) is True:
            return f"{key}_detected"
    return "unsafe_content" if verification.get("unsafe") is True else None


def _candidate_manifest(
    snapshot: _Snapshot,
    script_ref: ArtifactRef,
    audit_ref: ArtifactRef,
    beats: Sequence[RetrievalBeat],
    outputs: Sequence[_GenerationOutput],
) -> dict[str, Any]:
    by_beat: dict[str, list[_GenerationOutput]] = {beat.id: [] for beat in beats}
    materialized: list[dict[str, Any]] = []
    for item in outputs:
        if item.verification["accepted"] is not True or item.artifact is None:
            continue
        by_beat[item.beat.id].append(item)
        asset_id = _generated_id(snapshot, item, "asset")
        asset_file_id = _generated_id(snapshot, item, "asset-file")
        materialized.append(
            {
                "artifact_id": item.artifact.id,
                "kind": "asset",
                "filename": item.artifact.filename,
                "media_type": item.artifact.media_type,
                "content_hash": item.artifact.content_hash,
                "byte_size": item.artifact.byte_size,
                "asset_id": asset_id,
                "asset_file_id": asset_file_id,
            }
        )
    rights = {
        "copyright_status": "licensed",
        "status_allowed": True,
        "evidence_required": True,
        "evidence_present": True,
        "verified": True,
        "verification_basis": ["operator_confirmed_provider_terms_snapshot"],
        "rejection_codes": [],
        "sources": [
            {
                "evidence_type": "operator_confirmed_provider_terms",
                "license": snapshot.output_rights_license_basis,
                "locator": snapshot.terms_snapshot["ref"],
                "verified_at": snapshot.terms_snapshot["captured_at"],
                "content_hash": snapshot.terms_snapshot["content_hash"],
                "metadata": {
                    "output_rights_confirmed": True,
                    "output_rights_license_basis": (
                        snapshot.output_rights_license_basis
                    ),
                    "terms_content_hash": snapshot.terms_snapshot["content_hash"],
                },
            }
        ],
    }
    manifest_beats: list[dict[str, Any]] = []
    for beat in beats:
        candidates: list[dict[str, Any]] = []
        for rank, item in enumerate(by_beat[beat.id], start=1):
            duration_ms = round(float(item.verification["duration_seconds"]) * 1000)
            window_ms = min(_CLIP_SECONDS * 1000, duration_ms)
            labels = list(item.verification["labels"])
            candidates.append(
                {
                    "candidate_id": "candidate-"
                    + _sha256(
                        f"{snapshot.full_ai_run_id}\0{beat.id}\0{item.variant_index}".encode()
                    )[:20],
                    "rank": rank,
                    "eligible": True,
                    "materialized": True,
                    "artifact_id": item.artifact.id,
                    "artifact_filename": item.artifact.filename,
                    "asset_id": _generated_id(snapshot, item, "asset"),
                    "asset_file_id": _generated_id(snapshot, item, "asset-file"),
                    "analysis_id": _generated_id(snapshot, item, "analysis"),
                    "segment_id": _generated_id(snapshot, item, "segment"),
                    "title": beat.visual_description[:1000],
                    "description": item.verification["description"],
                    "labels": labels,
                    "source_transcript": "",
                    "asset_kind": "video",
                    "media_type": item.artifact.media_type,
                    "content_hash": item.artifact.content_hash,
                    "byte_size": item.artifact.byte_size,
                    "source_duration_ms": duration_ms,
                    "source_window": {
                        "start_ms": 0,
                        "end_ms": window_ms,
                        "duration_ms": window_ms,
                    },
                    "score": None,
                    "score_breakdown": {
                        "lexical_similarity": None,
                        "label_hits": sum(
                            term.casefold() in " ".join(labels).casefold()
                            for term in beat.must_match
                        ),
                        "label_score": None,
                        "cut_safe_bonus": None,
                        "semantic_complete_bonus": None,
                        "total": None,
                    },
                    "hard_constraints": {
                        "passed": True,
                        "must_match": item.verification["must_match"],
                        "must_not_match": item.verification["must_not_match"],
                    },
                    "rejection_codes": [],
                    "cut_evidence": {
                        "cut_safe": item.verification["cut_safe"],
                        "semantic_complete": item.verification["semantic_complete"],
                    },
                    "rights_evidence": rights,
                }
            )
        manifest_beats.append(
            {
                "id": beat.id,
                "sequence": beat.sequence,
                "narration": beat.narration,
                "visual_description": beat.visual_description,
                "query": beat.query(),
                "must_match": list(beat.must_match),
                "must_not_match": list(beat.must_not_match),
                "coverage_status": "covered",
                "candidates": candidates,
            }
        )
    return {
        "schema_version": "1.0.0",
        "operation": "media.generate",
        "provider": snapshot.provider_name,
        "catalog_scope": "run",
        "local_catalog_only": False,
        "source_script_artifact_id": script_ref.id,
        "coverage": {
            "status": "complete",
            "total_beats": len(beats),
            "covered_beats": len(beats),
            "missing_beat_ids": [],
        },
        "rights_status": "verified",
        "generation": {
            "mode": "generated_only",
            "continuity_mode": snapshot.continuity_mode,
            "generated_material_manifest_artifact_id": audit_ref.id,
            "generated_material_manifest_content_hash": audit_ref.content_hash,
        },
        "beats": manifest_beats,
        "materialized_assets": materialized,
    }


def _generated_id(
    snapshot: _Snapshot, item: _GenerationOutput, identity_type: str
) -> str:
    if item.artifact is None:
        raise ValueError("rejected generated output has no downstream asset identity")
    return str(
        uuid5(
            NAMESPACE_URL,
            f"framefactory-generated:{identity_type}:{snapshot.full_ai_run_id}:"
            f"{item.beat.id}:{item.variant_index}:{item.artifact.content_hash}",
        )
    )


def _required_artifact(context: StepContext, kind: str) -> ArtifactRef:
    values = [artifact for artifact in context.input_artifacts if artifact.kind == kind]
    if len(values) != 1:
        raise PermanentStepError(
            f"media.generate requires exactly one {kind} artifact",
            code="FULL_AI_INPUT_ARTIFACT_INVALID",
        )
    return values[0]


def _snapshot_reference(value: object, field: str) -> dict[str, Any]:
    result = dict(_mapping(value, field))
    ref = str(result.get("ref") or "").strip()
    digest = str(result.get("content_hash") or "").strip()
    captured = str(result.get("captured_at") or "").strip()
    if not ref or len(ref) > 2_048 or not _SHA256.fullmatch(digest):
        raise _plan_drift(f"{field} is not a content-addressed provider snapshot")
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _plan_drift(f"{field}.captured_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _plan_drift(f"{field}.captured_at must include a timezone")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _plan_drift(f"{field} must be an object")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _plan_drift(f"{field} must be an integer")
    return value


def _uuid(value: object, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _plan_drift(f"{field} must be a UUID") from exc


def _plan_drift(message: str) -> PermanentStepError:
    return PermanentStepError(
        f"FULL_AI_PLAN_DRIFT: {message}", code="FULL_AI_PLAN_DRIFT"
    )


def _timing_unverified(message: str) -> PermanentStepError:
    return PermanentStepError(
        f"FULL_AI_TIMING_UNVERIFIED: {message}", code="FULL_AI_TIMING_UNVERIFIED"
    )


def _constraint_evidence(
    value: object, expected_terms: Sequence[str]
) -> tuple[tuple[str, bool], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return () if not expected_terms else (("__invalid__", False),)
    result: list[tuple[str, bool]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return (("__invalid__", False),)
        term = str(item.get("term") or "").strip()
        matched = item.get("matched")
        if not term or not isinstance(matched, bool):
            return (("__invalid__", False),)
        result.append((term, matched))
    if tuple(term for term, _matched in result) != tuple(expected_terms):
        return (("__invalid__", False),)
    return tuple(result)


def _sha_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    result = tuple(dict.fromkeys(str(item) for item in value))
    if not 1 <= len(result) <= 8 or any(not _SHA256.fullmatch(item) for item in result):
        return ()
    return result


def _duration_is_five_seconds(value: object) -> bool:
    duration = _finite_number(value)
    return duration is not None and abs(duration - _CLIP_SECONDS) <= (1 / 30) + 1e-6


def _positive_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _unit_score(value: object, *, positive: bool = False) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0 or number > 1 or (positive and number <= 0):
        return None
    return round(number, 4)


def _identity_text(value: object) -> str:
    return "".join(str(value or "").split())


def _truncate_utf16(value: str, maximum_units: int) -> str:
    if maximum_units < 1:
        return ""
    result: list[str] = []
    used = 0
    for character in value.strip():
        units = _utf16_units(character)
        if used + units > maximum_units:
            break
        result.append(character)
        used += units
    return "".join(result)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


async def _offload(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(function, *args, **kwargs)


async def _offload_durable(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Finish a critical ledger write before propagating task cancellation."""

    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise
