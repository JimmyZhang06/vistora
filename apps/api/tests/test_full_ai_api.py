from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from framefactory_api.contracts import ContractValidator
from framefactory_api.main import create_app
from framefactory_api.queue import QueueError
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.seed_catalog import load_official_catalog
from framefactory_api.service import ControlService
from framefactory_api.settings import Settings

SPEC = {
    "brief": "一座漂浮在云海上的未来城市在清晨苏醒",
    "direction": "cinematic",
    "aspect_ratio": "9:16",
    "duration_seconds": 30,
    "variants_per_scene": 2,
    "continuity": True,
    "ai_disclosure": True,
}

FULL_AI_CAPABILITIES = (
    "model.text_generation",
    "writing.compose.generated",
    "audio.synthesize",
    "media.generate",
    "model.video_generation",
    "model.generated_video_verification",
    "timeline.align",
    "render.edl",
    "render.subtitle_sentence",
    "quality.evaluate",
)
FULL_AI_SKILL_VERSION_ID = "0979a0d0-4e92-589b-9559-5655b4fbaee0"
FULL_AI_PIPELINE_VERSION_ID = "6d6bad5a-e758-5d6c-9c39-e7c7713e5a4c"


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def healthcheck(self) -> None:
        return None

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        existing = next(
            (
                queued
                for queued in self.enqueued
                if queued["deduplication_key"] == command["deduplication_key"]
            ),
            None,
        )
        if existing is not None:
            return SimpleNamespace(id="full-ai-job"), False
        self.enqueued.append(command)
        return SimpleNamespace(id="full-ai-job"), True


class _FailOnceQueue(_Queue):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def enqueue(self, **command: Any) -> tuple[object, bool]:
        self.attempts += 1
        if self.attempts == 1:
            raise QueueError("simulated queue outage after database commit")
        return await super().enqueue(**command)


class _Storage:
    async def healthcheck(self) -> object:
        return SimpleNamespace(status="ok")


def _repository() -> InMemoryControlRepository:
    skills, versions = load_official_catalog(ContractValidator())
    return InMemoryControlRepository(
        skills=skills,
        skill_versions=list(versions),
        pipelines=[seed.version for seed in versions.pipelines],
    )


def _ready_settings() -> Settings:
    return Settings(
        worker_capabilities=FULL_AI_CAPABILITIES,
        full_ai_provider_name="runway",
        full_ai_model_id="gen4.5",
        full_ai_cost_per_second_minor=12,
        full_ai_credit_unit_minor=1,
        full_ai_terms_reference="https://docs.dev.runwayml.com/terms",
        full_ai_terms_content_hash="a" * 64,
        full_ai_terms_captured_at="2026-01-01T00:00:00Z",
        full_ai_pricing_reference="https://docs.dev.runwayml.com/guides/pricing/",
        full_ai_pricing_content_hash="b" * 64,
        full_ai_pricing_captured_at="2026-01-01T00:00:00Z",
        full_ai_output_rights_confirmed=True,
        full_ai_output_rights_license_basis="operator-confirmed provider terms",
    )


def test_default_full_ai_options_and_estimate_are_explicitly_blocked() -> None:
    with TestClient(create_app()) as client:
        options = client.get("/v1/full-ai/options")
        assert options.status_code == 200
        assert options.json()["status"] == "blocked"
        assert options.json()["provider"] == {
            "name": None,
            "model_id": None,
            "status": "unconfigured",
            "supports_reconciliation": False,
            "submit_unknown_policy": "manual_only",
            "continuity_modes": ["prompt_pack", "none"],
        }
        assert options.json()["limits"]["aspect_ratios"] == ["9:16", "16:9"]
        assert options.json()["blockers"][0]["code"] == (
            "FULL_AI_PROVIDER_NOT_CONFIGURED"
        )

        estimate = client.post("/v1/full-ai/estimate", json=SPEC)
        assert estimate.status_code == 200
        assert estimate.json()["status"] == "blocked"
        assert estimate.json()["quote"] is None
        assert estimate.json()["plan"] == {
            "scene_count": 6,
            "clip_seconds": 5,
            "candidate_count": 12,
            "billable_seconds": 60,
        }

        create = client.post(
            "/v1/full-ai/runs",
            headers={"Idempotency-Key": "full-ai-blocked-001"},
            json={
                **SPEC,
                "estimate_fingerprint": "0" * 64,
                "max_cost_minor": 10_000,
                "currency": "USD",
            },
        )
        assert create.status_code == 503
        assert create.json()["code"] == "FULL_AI_PROVIDER_UNAVAILABLE"


def test_full_ai_rejects_unsupported_ratio_and_asset_library_inputs() -> None:
    with TestClient(create_app()) as client:
        unsupported_ratio = client.post(
            "/v1/full-ai/estimate", json={**SPEC, "aspect_ratio": "1:1"}
        )
        assert unsupported_ratio.status_code == 422
        assert unsupported_ratio.json()["code"] == "REQUEST_VALIDATION_FAILED"

        old_asset_binding = client.post(
            "/v1/full-ai/estimate",
            json={**SPEC, "asset_library_ids": ["66666666-6666-4666-8666-666666666666"]},
        )
        assert old_asset_binding.status_code == 422
        assert old_asset_binding.json()["code"] == "REQUEST_VALIDATION_FAILED"


def test_full_ai_malformed_terms_and_pricing_snapshots_never_become_ready() -> None:
    settings = Settings(
        worker_capabilities=FULL_AI_CAPABILITIES,
        full_ai_provider_name="runway",
        full_ai_model_id="gen4.5",
        full_ai_cost_per_second_minor=12,
        full_ai_credit_unit_minor=1,
        full_ai_terms_reference="https://user:secret@example.com/terms",
        full_ai_terms_content_hash="NOT-A-HASH",
        full_ai_terms_captured_at="tomorrow",
        full_ai_pricing_reference="http://example.com/pricing",
        full_ai_pricing_content_hash="b" * 64,
        full_ai_pricing_captured_at="2999-01-01T00:00:00Z",
        full_ai_output_rights_confirmed=True,
        full_ai_output_rights_license_basis="operator-confirmed provider terms",
    )
    with TestClient(
        create_app(
            settings=settings,
            repository=_repository(),
            job_queue=_Queue(),
            object_storage=_Storage(),
        )
    ) as client:
        body = client.get("/v1/full-ai/options").json()
        assert body["status"] == "blocked"
        assert body["provider"]["status"] == "incomplete"
        assert {item["code"] for item in body["blockers"]} >= {
            "FULL_AI_PROVIDER_TERMS_NOT_FROZEN",
            "FULL_AI_PROVIDER_PRICING_NOT_FROZEN",
        }


def test_full_ai_requires_server_side_rights_attestation_and_video_verifier() -> None:
    settings = replace(
        _ready_settings(),
        worker_capabilities=tuple(
            capability
            for capability in FULL_AI_CAPABILITIES
            if capability != "model.generated_video_verification"
        ),
        full_ai_output_rights_confirmed=False,
        full_ai_output_rights_license_basis=None,
    )
    with TestClient(
        create_app(
            settings=settings,
            repository=_repository(),
            job_queue=_Queue(),
            object_storage=_Storage(),
        )
    ) as client:
        body = client.get("/v1/full-ai/options").json()
        assert body["status"] == "blocked"
        blockers = {item["code"] for item in body["blockers"]}
        assert "FULL_AI_OUTPUT_RIGHTS_UNCONFIRMED" in blockers
        assert "FULL_AI_GENERATION_CAPABILITY_UNAVAILABLE" in blockers

        cannot_self_attest = client.post(
            "/v1/full-ai/estimate",
            json={**SPEC, "output_rights_confirmed": True},
        )
        assert cannot_self_attest.status_code == 422


def test_ready_full_ai_create_is_atomic_scheduler_bound_and_idempotent() -> None:
    repository = _repository()
    queue = _Queue()
    with TestClient(
        create_app(
            settings=_ready_settings(),
            repository=repository,
            job_queue=queue,
            object_storage=_Storage(),
        )
    ) as client:
        options = client.get("/v1/full-ai/options")
        assert options.status_code == 200
        assert options.json()["status"] == "ready"

        estimate = client.post("/v1/full-ai/estimate", json=SPEC)
        assert estimate.status_code == 200
        estimate_body = estimate.json()
        assert estimate_body["status"] == "ready"
        assert estimate_body["quote"]["amount_minor"] == 720
        without_continuity = client.post(
            "/v1/full-ai/estimate", json={**SPEC, "continuity": False}
        )
        assert without_continuity.status_code == 200
        assert without_continuity.json()["status"] == "ready"

        request = {
            **SPEC,
            "estimate_fingerprint": estimate_body["request_fingerprint"],
            "max_cost_minor": 800,
            "currency": "USD",
        }
        headers = {"Idempotency-Key": "full-ai-create-001"}
        created = client.post("/v1/full-ai/runs", json=request, headers=headers)
        assert created.status_code == 201
        body = created.json()
        assert body["mode"] == "generated_only"
        assert body["project_run_id"] != body["id"]
        assert body["billing"] == {
            "status": "not_started",
            "authorized_amount_minor": 800,
            "incurred_amount_minor": 0,
            "requires_reconciliation": False,
        }

        # A lost 201 response must remain recoverable with the original key even
        # after the frozen quote's time bucket has changed.
        client.app.state.full_ai_service._now = lambda: (
            datetime.now(UTC) + timedelta(minutes=20)
        )
        replay = client.post("/v1/full-ai/runs", json=request, headers=headers)
        assert replay.status_code == 201
        assert replay.json() == body
        assert len(queue.enqueued) == 1

        conflict = client.post(
            "/v1/full-ai/runs",
            json={**request, "max_cost_minor": 801},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

        fetched = client.get(f"/v1/full-ai/runs/{body['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == body

        unsafe_cancel = client.post(
            f"/v1/runs/{body['project_run_id']}/cancel",
            headers={"Idempotency-Key": "full-ai-cancel-001"},
        )
        assert unsafe_cancel.status_code == 409
        assert unsafe_cancel.json()["code"] == "FULL_AI_CANCELLATION_UNSUPPORTED"

    assert len(repository._full_ai_runs) == 1
    full_ai_record = next(iter(repository._full_ai_runs.values()))
    scheduler_run = repository._runs[full_ai_record["underlying_run_id"]]
    composition = scheduler_run["composition_snapshot"]
    assert composition["asset_library_ids"] == []
    assert composition["visual_source_mode"] == "generated_only"
    assert composition["production_settings"]["target_duration_seconds"] == 30
    assert composition["production_settings"]["asset_acquisition"]["enabled"] is False
    generation = composition["full_ai_generation"]
    assert generation["scene_count"] == 6
    assert generation["candidate_count"] == 12
    assert generation["billable_seconds"] == 60
    assert generation["max_cost_minor"] == 800
    assert generation["ratio"] == "720:1280"
    assert generation["continuity_mode"] == "prompt_pack"
    assert generation["pricing_snapshot"]["credit_unit_minor"] == 1
    assert generation["pricing_snapshot"]["cost_per_second_minor"] == 12
    assert generation["output_rights_confirmed"] is True
    assert generation["output_rights_license_basis"] == (
        "operator-confirmed provider terms"
    )
    assert body["project_run_id"] == scheduler_run["id"]
    assert scheduler_run["input"]["full_ai_run_id"] == full_ai_record["id"]
    assert queue.enqueued[0]["queue_name"] == "runs"
    assert queue.enqueued[0]["payload"]["run_id"] == scheduler_run["id"]
    assert queue.enqueued[0]["max_attempts"] == 1


def test_full_ai_create_fails_on_stale_quote_and_budget_without_persisting() -> None:
    repository = _repository()
    with TestClient(
        create_app(
            settings=_ready_settings(),
            repository=repository,
            job_queue=_Queue(),
            object_storage=_Storage(),
        )
    ) as client:
        estimate = client.post("/v1/full-ai/estimate", json=SPEC).json()
        stale = client.post(
            "/v1/full-ai/runs",
            headers={"Idempotency-Key": "full-ai-stale-001"},
            json={
                **SPEC,
                "estimate_fingerprint": "0" * 64,
                "max_cost_minor": 800,
                "currency": "USD",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "FULL_AI_ESTIMATE_STALE"

        over_budget = client.post(
            "/v1/full-ai/runs",
            headers={"Idempotency-Key": "full-ai-budget-001"},
            json={
                **SPEC,
                "estimate_fingerprint": estimate["request_fingerprint"],
                "max_cost_minor": 719,
                "currency": "USD",
            },
        )
        assert over_budget.status_code == 409
        assert over_budget.json()["code"] == "FULL_AI_BUDGET_EXCEEDED"

    assert repository._full_ai_runs == {}


def test_full_ai_idempotent_replay_recovers_dispatch_after_queue_failure() -> None:
    repository = _repository()
    queue = _FailOnceQueue()
    with TestClient(
        create_app(
            settings=_ready_settings(),
            repository=repository,
            job_queue=queue,
            object_storage=_Storage(),
        )
    ) as client:
        estimate = client.post("/v1/full-ai/estimate", json=SPEC).json()
        request = {
            **SPEC,
            "estimate_fingerprint": estimate["request_fingerprint"],
            "max_cost_minor": 800,
            "currency": "USD",
        }
        headers = {"Idempotency-Key": "full-ai-dispatch-recovery-001"}

        first = client.post("/v1/full-ai/runs", json=request, headers=headers)
        assert first.status_code == 503
        saved_id = first.json()["details"]["full_ai_run_id"]
        assert len(repository._full_ai_runs) == 1
        assert queue.enqueued == []

        replay = client.post("/v1/full-ai/runs", json=request, headers=headers)
        assert replay.status_code == 201
        assert replay.json()["id"] == saved_id
        assert len(repository._full_ai_runs) == 1
        assert len(repository._runs) == 1
        assert len(queue.enqueued) == 1
        assert queue.attempts == 2


def test_standard_estimate_run_and_batch_reject_full_ai_only_operations() -> None:
    assert ControlService._pipeline_requires_full_ai_endpoint(
        {"nodes": [{"operation": "media.generate", "required": True}]}
    )
    assert ControlService._pipeline_requires_full_ai_endpoint(
        {"nodes": [{"operation": "writing.compose.generated", "required": True}]}
    )
    # Worker graph materialization creates every declared node, including nodes
    # marked optional.  Standard endpoints therefore cannot treat `required`
    # as an isolation boundary for paid Full-AI operations.
    assert ControlService._pipeline_requires_full_ai_endpoint(
        {"nodes": [{"operation": "media.generate", "required": False}]}
    )

    repository = _repository()
    queue = _Queue()
    composition = {
        "skill_version_id": FULL_AI_SKILL_VERSION_ID,
        "pipeline_version_id": FULL_AI_PIPELINE_VERSION_ID,
        "asset_library_ids": [],
    }
    run_request = {
        "input": {"topic": "must use dedicated endpoint"},
        "composition": composition,
    }
    with TestClient(
        create_app(
            settings=_ready_settings(),
            repository=repository,
            job_queue=queue,
            object_storage=_Storage(),
        )
    ) as client:
        estimate = client.post("/v1/runs/estimate", json=run_request)
        create = client.post(
            "/v1/runs",
            json=run_request,
            headers={"Idempotency-Key": "standard-full-ai-forbidden-001"},
        )
        batch = client.post(
            "/v1/generation-batches",
            json={
                "name": "Full AI bypass",
                "items": [{"topic": "must use dedicated endpoint", "inputs": {}}],
                "composition": composition,
                "research_mode": "off",
            },
            headers={"Idempotency-Key": "batch-full-ai-forbidden-001"},
        )

    for response in (estimate, create, batch):
        assert response.status_code == 422
        assert response.json()["code"] == "FULL_AI_ENDPOINT_REQUIRED"
        assert response.json()["details"]["required_endpoint"] == (
            "/v1/full-ai/runs"
        )
    assert repository._runs == {}
    assert repository._generation_batches == {}
    assert queue.enqueued == []
