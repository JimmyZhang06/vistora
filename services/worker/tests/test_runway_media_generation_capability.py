from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.capabilities import (
    UnsupportedCapability,
    configured_capabilities,
)
from framefactory.worker.config import (
    AssetAnalysisSettings,
    FullAiVisionSettings,
    ObjectStorageSettings,
    RunwaySettings,
    WorkerSettings,
)
from framefactory.worker.generation.capability import (
    RunwayMediaGenerationCapability,
    _checkpoint_verification,
    _validated_verification,
)
from framefactory.worker.generation.ports import (
    PaidOperationAction,
    PaidOperationDecision,
    PaidOperationRecord,
)
from framefactory.worker.generation.runway import (
    RunwayPermanentError,
    RunwaySubmitReceipt,
    RunwayTask,
    RunwayTaskStatus,
    RunwayTextVideoRequest,
)
from framefactory.worker.generation.verifier import GeneratedVideoVerificationError
from framefactory.worker.retrieval.models import RetrievalBeat

_WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
_RUN_ID = "22222222-2222-4222-8222-222222222222"
_FULL_AI_RUN_ID = "33333333-3333-4333-8333-333333333333"


class _Storage:
    def __init__(self, values: Mapping[str, Mapping[str, Any]]) -> None:
        self.values = dict(values)
        self.published: list[Any] = []

    def read_json(self, artifact: ArtifactRef) -> Mapping[str, Any]:
        return self.values[artifact.id]

    def publish(self, _context: StepContext, artifact: Any) -> ArtifactRef:
        self.published.append(artifact)
        digest = hashlib.sha256(artifact.data).hexdigest()
        return ArtifactRef(
            id=str(uuid4()),
            workspace_id=_WORKSPACE_ID,
            run_id=_RUN_ID,
            step_id="media.generate",
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"test/{digest}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=digest,
            filename=artifact.filename,
        )


class _Ledger:
    def __init__(self) -> None:
        self.reserve_calls = 0
        self.unknown: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.operation = PaidOperationRecord(
            id="44444444-4444-4444-8444-444444444444",
            status="reserved",
            request_hash="0" * 64,
            authorized_amount_minor=60,
            incurred_amount_minor=0,
            provider_request_id=None,
        )

    def reserve(self, **values: Any) -> PaidOperationDecision:
        self.reserve_calls += 1
        self.operation = replace(
            self.operation,
            request_hash=str(values["request_hash"]),
            authorized_amount_minor=int(values["authorized_amount_minor"]),
        )
        return PaidOperationDecision(self.operation, PaidOperationAction.SUBMIT_NEW)

    def begin_submit(self, **_values: Any) -> PaidOperationRecord:
        self.operation = replace(self.operation, status="submitting")
        return self.operation

    def mark_submitted(self, **values: Any) -> PaidOperationRecord:
        self.operation = replace(
            self.operation,
            status="submitted",
            provider_request_id=str(values["provider_request_id"]),
        )
        return self.operation

    def mark_submit_unknown(self, **values: Any) -> PaidOperationRecord:
        self.unknown.append(dict(values))
        self.operation = replace(
            self.operation,
            status="submit_unknown",
            provider_request_id=(
                str(values["provider_request_id"])
                if values.get("provider_request_id")
                else None
            ),
        )
        return self.operation

    def mark_failed(self, **values: Any) -> PaidOperationRecord:
        self.failed.append(dict(values))
        self.operation = replace(
            self.operation,
            status="failed",
            provider_request_id=str(values["provider_request_id"]),
            incurred_amount_minor=int(values["incurred_amount_minor"]),
        )
        return self.operation

    def release_definite_rejection(self, **_values: Any) -> PaidOperationRecord:
        raise AssertionError("not expected")

    def mark_succeeded(self, **values: Any) -> PaidOperationRecord:
        self.operation = replace(
            self.operation,
            status="succeeded",
            provider_request_id=str(values["provider_request_id"]),
            incurred_amount_minor=int(values["incurred_amount_minor"]),
            result=dict(values["result"]),
        )
        return self.operation

    def settle_run(self, **_values: Any) -> None:
        raise AssertionError("not expected")


class _BlockingSubmittedLedger(_Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.mark_started = threading.Event()
        self.allow_mark = threading.Event()

    def mark_submitted(self, **values: Any) -> PaidOperationRecord:
        self.mark_started.set()
        assert self.allow_mark.wait(timeout=5)
        return super().mark_submitted(**values)


class _BlockingSucceededLedger(_Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.mark_started = threading.Event()
        self.allow_mark = threading.Event()

    def mark_succeeded(self, **values: Any) -> PaidOperationRecord:
        self.mark_started.set()
        assert self.allow_mark.wait(timeout=5)
        return super().mark_succeeded(**values)


class _BlockingTerminalLedger(_Ledger):
    def __init__(self, mutation: str) -> None:
        super().__init__()
        self.mutation = mutation
        self.mark_started = threading.Event()
        self.allow_mark = threading.Event()

    def _block(self, mutation: str) -> None:
        if self.mutation == mutation:
            self.mark_started.set()
            assert self.allow_mark.wait(timeout=5)

    def mark_submit_unknown(self, **values: Any) -> PaidOperationRecord:
        self._block("mark_submit_unknown")
        return super().mark_submit_unknown(**values)

    def mark_failed(self, **values: Any) -> PaidOperationRecord:
        self._block("mark_failed")
        return super().mark_failed(**values)


class _Client:
    def __init__(
        self,
        *,
        estimate: float = 60,
        final_cost: int = 60,
        task_status: RunwayTaskStatus = RunwayTaskStatus.SUCCEEDED,
        download_error: bool = False,
    ) -> None:
        self.estimate = estimate
        self.final_cost = final_cost
        self.task_status = task_status
        self.download_error = download_error
        self.submit_calls = 0
        self.task_calls = 0
        self.download_calls = 0
        self.task_id = "55555555-5555-4555-8555-555555555555"

    def submit_text_to_video(self, _request: Any) -> RunwaySubmitReceipt:
        self.submit_calls += 1
        return RunwaySubmitReceipt(self.task_id, self.estimate)

    def get_task(self, _task_id: str) -> RunwayTask:
        self.task_calls += 1
        return RunwayTask(
            task_id=self.task_id,
            status=self.task_status,
            created_at="2026-08-24T00:00:00Z",
            output_urls=(
                ("https://cdn.example.test/output.mp4",)
                if self.task_status is RunwayTaskStatus.SUCCEEDED
                else ()
            ),
            final_cost_credits=self.final_cost,
            failure=(
                "provider task did not succeed"
                if self.task_status
                in {RunwayTaskStatus.FAILED, RunwayTaskStatus.CANCELLED}
                else None
            ),
            failure_code=(
                self.task_status.value
                if self.task_status
                in {RunwayTaskStatus.FAILED, RunwayTaskStatus.CANCELLED}
                else None
            ),
        )

    def download_output(self, _url: str, **_values: Any) -> tuple[bytes, str]:
        self.download_calls += 1
        if self.download_error:
            raise RunwayPermanentError("invalid output")
        return b"video", "video/mp4"


class _VerifierFailure:
    async def verify(self, *_args: Any, **_kwargs: Any) -> Any:
        raise GeneratedVideoVerificationError(
            "FULL_AI_VIDEO_DECODE_FAILED",
            "video cannot be decoded",
            retryable=False,
            evidence={"decoder": "ffmpeg"},
        )


def _artifact(kind: str, digest: str | None = None) -> ArtifactRef:
    payload_hash = digest or hashlib.sha256(kind.encode()).hexdigest()
    return ArtifactRef(
        id=str(uuid4()),
        workspace_id=_WORKSPACE_ID,
        run_id=_RUN_ID,
        step_id=kind,
        kind=kind,
        media_type="application/json" if kind != "audio" else "audio/wav",
        object_key=f"test/{kind}",
        byte_size=10,
        content_hash=payload_hash,
        filename=f"{kind}.json" if kind != "audio" else "audio.wav",
    )


def _snapshot(*, rights_basis: str = "operator-confirmed provider terms") -> dict[str, Any]:
    generation = {
        "provider_name": "runway",
        "model_id": "gen4.5",
        "ratio": "1280:720",
        "variants_per_beat": 1,
        "target_duration_seconds": 5,
        "scene_count": 1,
        "candidate_count": 1,
        "billable_seconds": 5,
        "max_cost_minor": 60,
        "continuity_mode": "none",
        "full_ai_run_id": _FULL_AI_RUN_ID,
        "output_rights_confirmed": True,
        "output_rights_license_basis": rights_basis,
        "terms_snapshot": {
            "ref": "fixture://terms",
            "content_hash": "a" * 64,
            "captured_at": "2026-08-24T00:00:00Z",
        },
        "pricing_snapshot": {
            "ref": "fixture://pricing",
            "content_hash": "b" * 64,
            "captured_at": "2026-08-24T00:00:00Z",
            "currency": "USD",
            "credit_unit_minor": 1,
            "cost_per_second_minor": 12,
        },
    }
    return {
        "full_ai_run_id": _FULL_AI_RUN_ID,
        "brief": "test",
        "direction": "cinematic",
        "_framefactory": {
            "composition_snapshot": {"full_ai_generation": generation}
        },
    }


def _script(*, constraints: int = 1) -> dict[str, Any]:
    return {
        "narration": "这是一段足够长且可在五秒内完成的测试旁白。",
        "beats": [
            {
                "id": "beat-001",
                "sequence": 1,
                "narration": "这是一段足够长且可在五秒内完成的测试旁白。",
                "visual_description": "清晨海边的摄影师缓慢行走",
                "must_match": [f"required-{index}" for index in range(constraints)],
                "must_not_match": [],
            }
        ],
    }


def _timing(script: ArtifactRef, audio: ArtifactRef, *, estimated: bool = False) -> dict[str, Any]:
    return {
        "operation": "audio.synthesize",
        "source": "native_word_boundary",
        "estimated": estimated,
        "word_timing_estimated": False,
        "script_content_hash": script.content_hash,
        "audio_content_hash": audio.content_hash,
        "duration_seconds": 4.99,
        "beats": [
            {
                "id": "beat-001",
                "sequence": 1,
                "text": "这是一段足够长且可在五秒内完成的测试旁白。",
                "start_seconds": 0.0,
                "end_seconds": 4.99,
                "estimated": estimated,
                "alignment_source": (
                    "proportional_estimate" if estimated else "native_word_aggregation"
                ),
            }
        ],
    }


def _inputs(*, constraints: int = 1, estimated: bool = False):
    script_ref = _artifact("script")
    audio_ref = _artifact("audio")
    timing_ref = _artifact("narration_timing")
    storage = _Storage(
        {
            script_ref.id: _script(constraints=constraints),
            timing_ref.id: _timing(script_ref, audio_ref, estimated=estimated),
        }
    )
    context = StepContext(
        workspace_id=_WORKSPACE_ID,
        run_id=_RUN_ID,
        step_id="media.generate",
        input_snapshot=_snapshot(),
        input_artifacts=(script_ref, audio_ref, timing_ref),
    )
    return storage, context


@pytest.mark.parametrize(
    ("constraints", "estimated", "expected_code"),
    (
        (13, False, "FULL_AI_PLAN_DRIFT"),
        (1, True, "FULL_AI_TIMING_UNVERIFIED"),
    ),
)
def test_prepaid_contract_failures_never_reserve(
    constraints: int, estimated: bool, expected_code: str
) -> None:
    storage, context = _inputs(constraints=constraints, estimated=estimated)
    ledger = _Ledger()
    capability = RunwayMediaGenerationCapability(
        _Client(), storage, ledger, _VerifierFailure(), worker_id="worker-test"
    )

    with pytest.raises(PermanentStepError) as raised:
        asyncio.run(capability.execute(context))

    assert raised.value.code == expected_code
    assert ledger.reserve_calls == 0


def test_media_generation_uses_dedicated_vision_not_asset_analysis_settings() -> None:
    storage, _context_value = _inputs()
    ledger = _Ledger()
    verifier = _VerifierFailure()
    base = WorkerSettings(
        database_url="postgresql://user:pass@db/framefactory",
        redis_url="redis://redis/0",
        environment="test",
        worker_id="worker-test",
        object_storage=ObjectStorageSettings(bucket="artifacts"),
        runway=RunwaySettings(
            base_url="https://api.dev.runwayml.com",
            api_key="runway-secret",
        ),
        asset_analysis=AssetAnalysisSettings(
            base_url="https://asset-vision.example/v1",
            api_key="asset-secret",
            model="asset-model",
        ),
    )
    isolated = configured_capabilities(
        base,
        artifact_storage=storage,
        paid_operation_ledger=ledger,
        generated_video_verifier=verifier,
    )
    assert isinstance(isolated.resolve("media.generate"), UnsupportedCapability)

    generated = configured_capabilities(
        replace(
            base,
            asset_analysis=None,
            full_ai_vision=FullAiVisionSettings(
                base_url="https://full-ai-vision.example/v1",
                api_key="full-ai-secret",
                model="generated-qc",
            ),
        ),
        artifact_storage=storage,
        paid_operation_ledger=ledger,
        generated_video_verifier=verifier,
    )
    assert isinstance(
        generated.resolve("media.generate"), RunwayMediaGenerationCapability
    )


def _raw_verification(*, duration: float = 5.0) -> dict[str, Any]:
    safety = {
        "adult": False,
        "violence": False,
        "self_harm": False,
        "hate_or_extremism": False,
        "illegal_activity": False,
        "recognizable_real_person_or_public_figure": False,
        "brand_or_logo": False,
        "protected_character": False,
    }
    return {
        "verifier_provider": "independent-vision",
        "verifier_model": "vision-v1",
        "verifier_version": "generated-video-verifier-v1",
        "frame_hashes": ["c" * 64, "d" * 64, "e" * 64],
        "description": "清晨海边的摄影师缓慢行走 required-0",
        "labels": ["摄影师", "required-0"],
        "confidence": 0.2,
        "minimum_confidence": 0.75,
        "quality_score": 0.1,
        "minimum_quality_score": 0.65,
        "visual_description_matched": False,
        "visual_description_evidence": "画面不完全匹配描述",
        "must_match": [{"term": "required-0", "matched": True}],
        "must_not_match": [],
        "has_watermark": False,
        "has_embedded_text": False,
        "unsafe": False,
        "safety": safety,
        **safety,
        "quality_usable": False,
        "cut_safe": False,
        "cut_safe_evidence": "尾帧动作被截断",
        "semantic_complete": False,
        "duration_seconds": duration,
        "width": 1280,
        "height": 720,
        "codec": "h264",
    }


class _VerifierAccepted:
    async def verify(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        result = _raw_verification()
        result.update(
            confidence=0.95,
            quality_score=0.9,
            visual_description_matched=True,
            visual_description_evidence="画面与描述一致",
            quality_usable=True,
            cut_safe=True,
            cut_safe_evidence="首尾帧均可安全剪切",
            semantic_complete=True,
        )
        return result


def test_rejected_checkpoint_recomputes_visual_quality_and_timing_rejections() -> None:
    beat = RetrievalBeat(
        id="beat-001",
        sequence=1,
        narration="旁白",
        visual_description="清晨海边的摄影师缓慢行走",
        must_match=("required-0",),
    )
    canonical = _validated_verification(
        _raw_verification(duration=4.967),
        beat,
        "1280:720",
        beat_duration_seconds=4.99,
    )

    replay = _checkpoint_verification(
        canonical,
        beat,
        "1280:720",
        beat_duration_seconds=4.99,
    )

    assert replay == canonical
    assert {
        "visual_description_mismatch",
        "low_visual_confidence",
        "visual_quality_unusable",
        "generated_duration_insufficient_for_beat",
    } <= set(replay["rejection_codes"])
    assert replay["accepted"] is False


def test_submit_estimate_over_quote_persists_known_task_and_never_polls() -> None:
    storage, context = _inputs()
    ledger = _Ledger()
    client = _Client(estimate=61)
    capability = RunwayMediaGenerationCapability(
        client, storage, ledger, _VerifierFailure(), worker_id="worker-test"
    )

    with pytest.raises(PermanentStepError) as raised:
        asyncio.run(capability.execute(context))

    assert raised.value.code == "FULL_AI_RECONCILIATION_REQUIRED"
    assert client.submit_calls == 1
    assert client.task_calls == client.download_calls == 0
    assert ledger.unknown[0]["provider_request_id"] == client.task_id


def test_cancellation_after_paid_receipt_waits_for_task_identity_persistence() -> None:
    storage, context = _inputs()
    ledger = _BlockingSubmittedLedger()
    client = _Client()
    capability = RunwayMediaGenerationCapability(
        client, storage, ledger, _VerifierFailure(), worker_id="worker-test"
    )
    decision = PaidOperationDecision(ledger.operation, PaidOperationAction.SUBMIT_NEW)
    request = RunwayTextVideoRequest(
        prompt_text="A cinematic sunrise over a quiet coast.",
        ratio="1280:720",
        duration=5,
        seed=42,
    )

    async def cancel_during_ledger_write() -> None:
        task = asyncio.create_task(
            capability._submit_or_resume(
                context,
                decision,
                request,
                request_hash="a" * 64,
                credit_unit_minor=1,
            )
        )
        while not ledger.mark_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        ledger.allow_mark.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_ledger_write())

    assert client.submit_calls == 1
    assert ledger.operation.status == "submitted"
    assert ledger.operation.provider_request_id == client.task_id


def test_cancellation_after_artifact_publish_waits_for_durable_result() -> None:
    storage, context = _inputs()
    ledger = _BlockingSucceededLedger()
    client = _Client()
    capability = RunwayMediaGenerationCapability(
        client, storage, ledger, _VerifierAccepted(), worker_id="worker-test"
    )

    async def cancel_during_result_write() -> None:
        task = asyncio.create_task(capability.execute(context))
        while not ledger.mark_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        ledger.allow_mark.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_result_write())

    assert len(storage.published) == 1
    assert ledger.operation.status == "succeeded"
    assert ledger.operation.result is not None
    assert ledger.operation.result["verification_status"] == "accepted"
    assert ledger.operation.result["accepted_artifact"] is not None


@pytest.mark.parametrize(
    (
        "failure_mode",
        "task_status",
        "final_cost",
        "download_error",
        "ledger_mutation",
    ),
    (
        ("provider_failed", RunwayTaskStatus.FAILED, 60, False, "mark_failed"),
        ("provider_cancelled", RunwayTaskStatus.CANCELLED, 60, False, "mark_failed"),
        ("download_permanent", RunwayTaskStatus.SUCCEEDED, 60, True, "mark_failed"),
        ("verification_permanent", RunwayTaskStatus.SUCCEEDED, 60, False, "mark_failed"),
        (
            "succeeded_cost_over_quote",
            RunwayTaskStatus.SUCCEEDED,
            61,
            False,
            "mark_submit_unknown",
        ),
        (
            "failed_cost_over_quote",
            RunwayTaskStatus.FAILED,
            61,
            False,
            "mark_submit_unknown",
        ),
    ),
)
def test_cancellation_waits_for_known_provider_terminal_ledger_mutation(
    failure_mode: str,
    task_status: RunwayTaskStatus,
    final_cost: int,
    download_error: bool,
    ledger_mutation: str,
) -> None:
    storage, context = _inputs()
    ledger = _BlockingTerminalLedger(ledger_mutation)
    client = _Client(
        task_status=task_status,
        final_cost=final_cost,
        download_error=download_error,
    )
    verifier = (
        _VerifierFailure()
        if failure_mode == "verification_permanent"
        else _VerifierAccepted()
    )
    capability = RunwayMediaGenerationCapability(
        client, storage, ledger, verifier, worker_id="worker-test"
    )

    async def cancel_during_terminal_ledger_write() -> None:
        task = asyncio.create_task(capability.execute(context))
        while not ledger.mark_started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        ledger.allow_mark.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_terminal_ledger_write())

    assert client.submit_calls == client.task_calls == 1
    if ledger_mutation == "mark_submit_unknown":
        assert ledger.operation.status == "submit_unknown"
        assert ledger.unknown[-1]["provider_request_id"] == client.task_id
        assert ledger.unknown[-1]["error"]["code"] == "FULL_AI_PROVIDER_COST_OVER_QUOTE"
    else:
        assert ledger.operation.status == "failed"
        assert ledger.failed[-1]["provider_request_id"] == client.task_id
        assert ledger.failed[-1]["incurred_amount_minor"] == final_cost
        assert ledger.failed[-1]["cancelled"] is (
            task_status is RunwayTaskStatus.CANCELLED
        )


@pytest.mark.parametrize("failure_mode", ("download", "verification"))
def test_paid_provider_success_with_permanent_local_failure_records_cost(
    failure_mode: str,
) -> None:
    storage, context = _inputs()
    ledger = _Ledger()
    client = _Client(download_error=failure_mode == "download")
    verifier = _VerifierFailure()
    capability = RunwayMediaGenerationCapability(
        client, storage, ledger, verifier, worker_id="worker-test"
    )

    with pytest.raises(PermanentStepError):
        asyncio.run(capability.execute(context))

    assert client.submit_calls == client.task_calls == 1
    assert ledger.failed[0]["provider_request_id"] == client.task_id
    assert ledger.failed[0]["incurred_amount_minor"] == 60
    assert ledger.failed[0]["error"]["provider_task_succeeded"] is True
