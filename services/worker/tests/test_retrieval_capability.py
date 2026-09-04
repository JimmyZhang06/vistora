from __future__ import annotations

import hashlib
import inspect
import json
import re
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.database_assets import AssetAcquisitionResult
from framefactory.worker.config import AssetLibrarySettings
from framefactory.worker.providers import ProviderArtifact
from framefactory.worker.retrieval import (
    DatabaseInventoryCapability,
    DatabaseRetrievalCapability,
    PostgresRetrievalCatalog,
    RetrievalBeat,
    RetrievalCandidate,
    ScoreBreakdown,
    evaluate_candidate,
    normalize_beats,
    rank_candidates,
)
from framefactory.worker.retrieval.capability import (
    _acquisition_query_plan,
    _ensure_materialized_asset_limit,
    _rights_evaluation,
)
from framefactory.worker.retrieval.evidence import rights_evidence_valid
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_SCHEMA_DIRECTORY = _REPOSITORY_ROOT / "packages" / "contracts" / "schemas" / "v1"
_SCRIPT_ARTIFACT_ID = "11111111-1111-4111-8111-111111111111"
_LIBRARY_ID = "22222222-2222-4222-8222-222222222222"


class FakeStorage:
    def __init__(self, script: Mapping[str, Any]) -> None:
        self.script = dict(script)
        self.published: list[ProviderArtifact] = []
        self.copies: list[dict[str, Any]] = []

    def read_json(self, artifact: ArtifactRef) -> Mapping[str, Any]:
        return self.script

    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef:
        self.published.append(artifact)
        digest = hashlib.sha256(artifact.data).hexdigest()
        return _artifact_ref(
            context,
            artifact_id=f"66666666-6666-4666-8666-{len(self.published):012d}",
            kind=artifact.kind,
            filename=artifact.filename,
            media_type=artifact.media_type,
            byte_size=len(artifact.data),
            content_hash=digest,
        )

    def publish_copy(
        self,
        context: StepContext,
        *,
        kind: str,
        filename: str,
        media_type: str,
        source_bucket: str,
        source_key: str,
        content_hash: str,
        byte_size: int,
    ) -> ArtifactRef:
        request = {
            "kind": kind,
            "filename": filename,
            "media_type": media_type,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "content_hash": content_hash,
            "byte_size": byte_size,
        }
        self.copies.append(request)
        return _artifact_ref(
            context,
            artifact_id=f"77777777-7777-4777-8777-{len(self.copies):012d}",
            kind=kind,
            filename=filename,
            media_type=media_type,
            byte_size=byte_size,
            content_hash=content_hash,
        )


def _artifact_ref(
    context: StepContext,
    *,
    artifact_id: str,
    kind: str,
    filename: str,
    media_type: str,
    byte_size: int,
    content_hash: str,
) -> ArtifactRef:
    return ArtifactRef(
        id=artifact_id,
        workspace_id=context.workspace_id,
        run_id=context.run_id,
        step_id=context.step_id,
        kind=kind,
        media_type=media_type,
        object_key=f"artifacts/{artifact_id}",
        byte_size=byte_size,
        content_hash=content_hash,
        filename=filename,
    )


def script_ref() -> ArtifactRef:
    return ArtifactRef(
        id=_SCRIPT_ARTIFACT_ID,
        workspace_id="workspace-one",
        run_id="run-one",
        step_id="writing",
        kind="script",
        media_type="application/json",
        object_key="scripts/script.json",
        byte_size=1,
        content_hash="0" * 64,
        filename="script.json",
    )


def context() -> StepContext:
    return StepContext(
        workspace_id="workspace-one",
        run_id="run-one",
        step_id="retrieve",
        input_snapshot={
            "_framefactory": {
                "composition_snapshot": {"asset_library_ids": [_LIBRARY_ID]}
            }
        },
        input_artifacts=(script_ref(),),
    )


def acquisition_context(
    *,
    rights_confirmed: bool = True,
    sources: tuple[str, ...] = ("wikimedia",),
    copyright_status: str = "public_domain",
    attempt: int = 1,
    maximum_attempts: int = 3,
    review_feedback: str | None = None,
) -> StepContext:
    return StepContext(
        workspace_id="workspace-one",
        run_id="run-one",
        step_id="retrieve",
        input_snapshot={
            "topic": "Apollo 11 moon landing",
            "_framefactory": {
                "composition_snapshot": {
                    "asset_library_ids": [_LIBRARY_ID],
                    "production_settings": {
                        "asset_acquisition": {
                            "enabled": True,
                            "sources": list(sources),
                            "max_assets": 2,
                            "copyright_status": copyright_status,
                            "rights_confirmed": rights_confirmed,
                        }
                    },
                }
            },
        },
        input_artifacts=(script_ref(),),
        attempt=attempt,
        maximum_attempts=maximum_attempts,
        review_feedback=review_feedback,
    )


class FakeAcquirer:
    def __init__(
        self,
        result: AssetAcquisitionResult | None = None,
        *,
        on_acquire: Any | None = None,
    ) -> None:
        self.result = result or AssetAcquisitionResult(1, ())
        self.on_acquire = on_acquire
        self.calls: list[dict[str, Any]] = []

    async def acquire(self, **request: Any) -> AssetAcquisitionResult:
        self.calls.append(request)
        if self.on_acquire is not None:
            self.on_acquire()
        return self.result


def candidate(
    asset_id: str,
    *,
    score: float = 0.75,
    text: str = "matching visual",
    asset_file_id: str | None = None,
    analysis_id: str | None = None,
    segment_id: str | None = None,
    start_ms: int = 5_000,
    end_ms: int | None = None,
    source_duration_ms: int | None = 60_000,
    media_type: str = "video/mp4",
    copyright_status: str = "licensed",
    rights_evidence: tuple[Mapping[str, Any], ...] | None = None,
    content_hash: str | None = None,
    labels: tuple[str, ...] | None = None,
    original_filename: str | None = None,
) -> RetrievalCandidate:
    data = f"bytes:{asset_file_id or asset_id}".encode()
    file_id = asset_file_id or f"file-{asset_id}"
    return RetrievalCandidate(
        asset_id=asset_id,
        asset_file_id=file_id,
        analysis_id=analysis_id or f"analysis-{asset_id}",
        segment_id=segment_id or f"segment-{asset_id}",
        title=f"title {text}",
        bucket="local-assets",
        object_key=f"objects/{file_id}",
        media_type=media_type,
        asset_kind="image" if media_type.startswith("image/") else "video",
        content_hash=content_hash or hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        original_filename=original_filename
        or (f"{file_id}.jpg" if media_type.startswith("image/") else f"{file_id}.mp4"),
        start_ms=start_ms,
        end_ms=end_ms if end_ms is not None else start_ms + 4_000,
        source_duration_ms=source_duration_ms,
        description=text,
        labels=labels if labels is not None else (text,),
        transcript=f"transcript {text}",
        score_breakdown=ScoreBreakdown(lexical_similarity=score),
        cut_safe=True,
        semantic_complete=True,
        copyright_status=copyright_status,
        rights_evidence=(
            rights_evidence
            if rights_evidence is not None
            else (
                {
                    "source_id": f"source-{asset_id}",
                    "source_type": "upload",
                    "license": "project-license",
                    "evidence_type": "license",
                    "verified_at": "2026-08-22T00:00:00Z",
                },
            )
        ),
    )


def acquisition_script() -> dict[str, Any]:
    return {
        "beats": [
            {
                "id": "covered",
                "sequence": 1,
                "narration": "The mission leaves Earth.",
                "visual_description": "Apollo 11 Saturn V launch",
                "must_match": [],
                "must_not_match": [],
            },
            {
                "id": "missing",
                "sequence": 2,
                "narration": "Astronauts step onto the Moon.",
                "visual_description": "Apollo 11 moon landing first steps",
                "must_match": ["Apollo 11", "moon"],
                "must_not_match": [],
            },
        ]
    }


def manifest(storage: FakeStorage) -> dict[str, Any]:
    artifact = next(
        item for item in storage.published if item.kind == "candidate_manifest"
    )
    return json.loads(artifact.data)


def assert_candidate_manifest_valid(value: Mapping[str, Any]) -> None:
    schema = json.loads(
        (_CONTRACT_SCHEMA_DIRECTORY / "candidate-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    common = json.loads(
        (_CONTRACT_SCHEMA_DIRECTORY / "common.schema.json").read_text(encoding="utf-8")
    )
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    errors = sorted(
        Draft202012Validator(schema, registry=registry).iter_errors(dict(value)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        raise AssertionError(
            f"candidate manifest schema violation at {list(first.absolute_path)}: "
            f"{first.message}"
        )


class RetrievalBeatTests(unittest.TestCase):
    def test_normalizes_stable_ordered_beats_and_derives_missing_id(self) -> None:
        script = {
            "narration": "先打开阀门。随后液体流入容器。",
            "beats": [
                {
                    "id": "authored-second",
                    "sequence": 2,
                    "narration": "随后液体流入容器。",
                    "visual_description": "液体流入透明容器",
                    "must_match": ["液体", "透明容器"],
                    "must_not_match": [],
                },
                {
                    "sequence": 1,
                    "narration": "先打开阀门。",
                    "visual_description": "手打开银色阀门",
                    "must_match": ["阀门"],
                    "must_not_match": ["木桶"],
                },
            ],
        }

        first = normalize_beats(script)
        second = normalize_beats(script)

        self.assertEqual(first, second)
        self.assertEqual([1, 2], [beat.sequence for beat in first])
        self.assertTrue(first[0].id.startswith("beat-001-"))
        self.assertEqual("authored-second", first[1].id)
        self.assertTrue(
            all(beat.narration and beat.visual_description for beat in first)
        )

    def test_oversized_authored_ids_use_stable_collision_resistant_fallbacks(self) -> None:
        prefix = "authored-" + "x" * 152
        script = {
            "beats": [
                {
                    "id": prefix + suffix,
                    "sequence": index,
                    "narration": f"Narration {index}.",
                    "visual_description": f"Visual {index}",
                }
                for index, suffix in enumerate(("a", "b"), start=1)
            ]
        }

        first = normalize_beats(script)
        second = normalize_beats(script)

        self.assertEqual(first, second)
        self.assertEqual(2, len({beat.id for beat in first}))
        self.assertTrue(all(beat.id.startswith("beat-") for beat in first))
        self.assertTrue(all(len(beat.id) <= 160 for beat in first))

    def test_beat_count_and_constraint_term_contract_limits_fail_closed(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "10000 Beat"):
            normalize_beats({"beats": [{}] * 10_001})
        with self.assertRaisesRegex(PermanentStepError, "10000 Beat"):
            normalize_beats({"scenes": [f"visual-{index}" for index in range(10_001)]})
        with self.assertRaisesRegex(PermanentStepError, "240 characters"):
            normalize_beats(
                {
                    "beats": [
                        {
                            "narration": "Narration.",
                            "visual_description": "Visual",
                            "must_match": ["x" * 241],
                        }
                    ]
                }
            )

    def test_repeated_full_narration_is_repaired_into_an_exact_stable_partition(self) -> None:
        narration = "甲乙。丙丁。戊己。庚辛。壬癸。子丑。"
        script = {
            "narration": narration,
            "beats": [
                {
                    "id": f"authored-{index}",
                    "sequence": index,
                    "narration": narration,
                    "visual_description": f"visual-{index}",
                    "must_match": [f"must-{index}"],
                    "must_not_match": [f"not-{index}"],
                }
                for index in range(1, 9)
            ],
        }

        first = normalize_beats(script)
        second = normalize_beats(script)

        self.assertEqual(first, second)
        self.assertEqual(8, len(first))
        self.assertEqual(narration, "".join(beat.narration for beat in first))
        self.assertTrue(all(beat.narration != narration for beat in first))
        self.assertEqual(list(range(1, 9)), [beat.sequence for beat in first])
        self.assertEqual(
            [f"visual-{index}" for index in range(1, 9)],
            [beat.visual_description for beat in first],
        )
        self.assertEqual(("must-4",), first[3].must_match)
        self.assertEqual(("not-4",), first[3].must_not_match)

    def test_mixed_language_narration_partitions_without_overlap_or_omission(self) -> None:
        narration = "Alpha beta。甲乙，丙丁！Gamma delta."
        script = {
            "narration": narration,
            "beats": [
                {
                    "sequence": index,
                    "narration": "",
                    "visual_description": f"visual-{index}",
                }
                for index in range(1, 5)
            ],
        }

        beats = normalize_beats(script)

        def normalized(value: str) -> str:
            return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value).casefold()

        self.assertEqual(
            normalized(narration),
            "".join(normalized(beat.narration) for beat in beats),
        )
        self.assertEqual(4, len(beats))
        self.assertTrue(all(beat.narration.strip() for beat in beats))

    def test_must_match_is_and_and_any_must_not_match_rejects(self) -> None:
        beat = RetrievalBeat(
            id="beat-one",
            sequence=1,
            narration="阀门打开。",
            visual_description="银色阀门连接透明容器",
            must_match=("阀门", "透明容器"),
            must_not_match=("木桶", "户外"),
        )

        missing_one = evaluate_candidate(
            beat,
            candidate("partial", text="银色阀门"),
            minimum_similarity=0.35,
        )
        forbidden = evaluate_candidate(
            beat,
            candidate("forbidden", text="阀门 透明容器 户外"),
            minimum_similarity=0.35,
        )
        eligible = evaluate_candidate(
            beat,
            candidate("eligible", text="阀门 透明容器 室内"),
            minimum_similarity=0.35,
        )

        self.assertIn("must_match_missing", missing_one.rejection_codes)
        self.assertIn("must_not_match_hit", forbidden.rejection_codes)
        self.assertTrue(eligible.eligible)
        self.assertTrue(eligible.constraints.passed)

    def test_same_score_tie_break_is_deterministic(self) -> None:
        beat = RetrievalBeat("beat", 1, "narration", "visual")
        ranked = rank_candidates(
            beat,
            (candidate("zeta"), candidate("alpha"), candidate("middle")),
            minimum_similarity=0.35,
        )

        self.assertEqual(
            ["alpha", "middle", "zeta"],
            [item.candidate.asset_id for item in ranked],
        )

    def test_image_without_source_duration_is_eligible(self) -> None:
        beat = RetrievalBeat("still", 1, "Still narration.", "A still image")
        still = candidate(
            "still",
            media_type="image/jpeg",
            source_duration_ms=None,
            start_ms=0,
            end_ms=1,
        )

        evaluated = evaluate_candidate(beat, still, minimum_similarity=0.35)

        self.assertTrue(evaluated.eligible)
        self.assertNotIn("source_duration_missing", evaluated.rejection_codes)

    def test_video_window_must_fit_known_source_duration(self) -> None:
        beat = RetrievalBeat("video", 1, "Video narration.", "A video")
        missing_duration = evaluate_candidate(
            beat,
            candidate("missing-duration", source_duration_ms=None),
            minimum_similarity=0.35,
        )
        out_of_bounds = evaluate_candidate(
            beat,
            candidate(
                "out-of-bounds",
                start_ms=5_000,
                end_ms=10_001,
                source_duration_ms=10_000,
            ),
            minimum_similarity=0.35,
        )

        self.assertIn("source_duration_missing", missing_duration.rejection_codes)
        self.assertIn("source_window_out_of_bounds", out_of_bounds.rejection_codes)

    def test_rights_evidence_is_fail_closed_but_owned_is_allowed(self) -> None:
        beat = RetrievalBeat("rights", 1, "Rights narration.", "Rights visual")
        licensed_missing = evaluate_candidate(
            beat,
            candidate("licensed-missing", rights_evidence=()),
            minimum_similarity=0.35,
        )
        licensed_unverified = evaluate_candidate(
            beat,
            candidate(
                "licensed-unverified",
                rights_evidence=(
                    {
                        "source_type": "website",
                        "license": "CC BY 4.0",
                        "evidence_type": "license",
                    },
                ),
            ),
            minimum_similarity=0.35,
        )
        licensed_verified_type = evaluate_candidate(
            beat,
            candidate(
                "licensed-verified-type",
                rights_evidence=(
                    {
                        "source_type": "website",
                        "license": "CC BY 4.0",
                        "evidence_type": "verified_license",
                    },
                ),
            ),
            minimum_similarity=0.35,
        )
        owned = evaluate_candidate(
            beat,
            candidate(
                "owned",
                copyright_status="owned",
                rights_evidence=(),
            ),
            minimum_similarity=0.35,
        )
        public_domain = evaluate_candidate(
            beat,
            candidate(
                "public-domain",
                copyright_status="public_domain",
                rights_evidence=(
                    {
                        "source_type": "website",
                        "locator": (
                            "https://creativecommons.org/publicdomain/mark/1.0/"
                        ),
                        "evidence_type": "provenance",
                    },
                ),
            ),
            minimum_similarity=0.35,
        )

        self.assertIn("rights_evidence_missing", licensed_missing.rejection_codes)
        self.assertIn("rights_evidence_unverified", licensed_unverified.rejection_codes)
        self.assertTrue(licensed_verified_type.eligible)
        self.assertTrue(owned.eligible)
        self.assertTrue(public_domain.eligible)

    def test_rights_source_bound_preserves_verdict_evidence_and_round_trips(self) -> None:
        noise = tuple(
            {
                "source_id": f"noise-{index:04d}",
                "license": "unknown",
                "evidence_type": "copyright",
            }
            for index in range(1_000)
        )
        verified_source = {
            "source_id": "verified-license",
            "license": "CC BY 4.0",
            "evidence_type": "verified_license",
        }
        verified = _rights_evaluation(
            candidate(
                "rights-cap-verified",
                rights_evidence=(*noise, verified_source),
            )
        )
        unverified_source = {
            "source_id": "unverified-license",
            "license": "CC BY 4.0",
            "evidence_type": "copyright",
        }
        unverified = _rights_evaluation(
            candidate(
                "rights-cap-unverified",
                rights_evidence=(*noise, unverified_source),
            )
        )
        noc_locator_only = _rights_evaluation(
            candidate(
                "rights-noc-locator",
                copyright_status="public_domain",
                rights_evidence=(
                    {
                        "source_id": "noc-locator",
                        "locator": "https://rightsstatements.org/page/NoC-US/1.0/",
                        "evidence_type": "provenance",
                    },
                ),
            )
        )

        self.assertEqual(1_000, len(verified["sources"]))
        self.assertEqual("verified-license", verified["sources"][0]["source_id"])
        self.assertTrue(verified["verified"])
        self.assertTrue(rights_evidence_valid(verified))
        self.assertEqual(1_000, len(unverified["sources"]))
        self.assertEqual("unverified-license", unverified["sources"][0]["source_id"])
        self.assertEqual(["rights_evidence_unverified"], unverified["rejection_codes"])
        self.assertTrue(rights_evidence_valid(unverified))
        self.assertFalse(noc_locator_only["verified"])
        self.assertEqual(
            ["rights_evidence_missing"], noc_locator_only["rejection_codes"]
        )
        self.assertTrue(rights_evidence_valid(noc_locator_only))

    def test_materialized_asset_global_contract_limit_fails_before_copy(self) -> None:
        _ensure_materialized_asset_limit(str(index) for index in range(100_000))
        with self.assertRaisesRegex(PermanentStepError, "100000 materialized"):
            _ensure_materialized_asset_limit(str(index) for index in range(100_001))


class DatabaseRetrievalCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_library_can_publish_truthful_editorial_draft_plan(self) -> None:
        script = acquisition_script()
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: self.fail("catalog must not be queried"),
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="retrieve",
            input_snapshot={
                "topic": "Apollo 11",
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [],
                        "production_settings": {
                            "no_asset_draft": {"enabled": True}
                        },
                    }
                },
            },
            input_artifacts=(script_ref(),),
        )

        result = await capability.execute(step_context)
        generated = manifest(storage)

        self.assertFalse(result.requires_review)
        self.assertEqual("editorial_fallback", result.output_summary["visual_source_mode"])
        self.assertEqual("procedural-editorial-cards", generated["provider"])
        self.assertEqual("complete", generated["coverage"]["status"])
        self.assertTrue(generated["editorial_fallback"]["replacement_required"])
        assert_candidate_manifest_valid(generated)
        self.assertEqual(
            ["asset", "asset", "video_plan", "candidate_manifest"],
            [artifact.kind for artifact in result.artifacts],
        )
        for beat in generated["beats"]:
            evidence = beat["candidates"][0]["rights_evidence"]
            self.assertTrue(rights_evidence_valid(evidence))

    async def test_editorial_draft_bounds_card_objects_for_large_plans(self) -> None:
        script = {
            "beats": [
                {
                    "id": f"beat-{index:03d}",
                    "sequence": index,
                    "narration": f"Narration {index}.",
                    "visual_description": f"Visual {index}.",
                    "must_match": [],
                    "must_not_match": [],
                }
                for index in range(1, 18)
            ]
        }
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: self.fail("catalog must not be queried"),
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="retrieve",
            input_snapshot={
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [],
                        "production_settings": {
                            "no_asset_draft": {"enabled": True}
                        },
                    }
                }
            },
            input_artifacts=(script_ref(),),
        )

        result = await capability.execute(step_context)
        generated = manifest(storage)

        self.assertEqual(8, result.output_summary["materialized_assets"])
        self.assertEqual(8, len(generated["materialized_assets"]))
        self.assertEqual(17, len(generated["beats"]))
        self.assertEqual(
            generated["beats"][0]["candidates"][0]["artifact_id"],
            generated["beats"][8]["candidates"][0]["artifact_id"],
        )
        assert_candidate_manifest_valid(generated)

    async def test_inventory_without_snapshot_can_plan_editorial_draft(self) -> None:
        storage = FakeStorage({})
        capability = DatabaseInventoryCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: self.fail("catalog must not be queried"),
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="inventory",
            input_snapshot={
                "topic": "uncovered",
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [],
                        "production_settings": {
                            "no_asset_draft": {"enabled": True}
                        },
                    }
                },
            },
        )

        result = await capability.execute(step_context)
        inventory = json.loads(storage.published[0].data)

        self.assertEqual("editorial_fallback", inventory["coverage"]["status"])
        self.assertTrue(inventory["editorial_fallback"]["replacement_required"])
        self.assertEqual("editorial_fallback", result.output_summary["visual_source_mode"])

    async def test_inventory_without_materials_plans_live_auto_acquisition(self) -> None:
        calls: list[tuple[Any, ...]] = []

        def search(*arguments: Any):
            calls.append(arguments)
            return ()

        storage = FakeStorage({})
        capability = DatabaseInventoryCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="inventory",
            input_snapshot={
                "topic": "城市低碳交通",
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [_LIBRARY_ID],
                        "production_settings": {
                            "asset_acquisition": {
                                "enabled": True,
                                "sources": ["wikimedia"],
                                "max_assets": 3,
                                "copyright_status": "public_domain",
                                "rights_confirmed": False,
                            },
                            "no_asset_draft": {"enabled": True},
                        },
                    }
                },
            },
        )

        result = await capability.execute(step_context)
        inventory = json.loads(storage.published[0].data)

        self.assertEqual("pending_auto_acquisition", inventory["coverage"]["status"])
        self.assertEqual("live", inventory["catalog_mode"])
        self.assertTrue(inventory["asset_acquisition"]["pending"])
        self.assertEqual(
            "provider_verified_public_domain",
            inventory["asset_acquisition"]["rights_mode"],
        )
        self.assertEqual(
            "automatic_acquisition_after_script",
            result.output_summary["action_required"],
        )
        self.assertTrue(calls)
        self.assertTrue(all(call[-1] is None for call in calls))

    async def test_frozen_snapshot_missing_coverage_never_calls_acquirer(self) -> None:
        snapshot_id = "33333333-3333-4333-8333-333333333333"
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="retrieve",
            input_snapshot={
                "topic": "missing footage",
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [_LIBRARY_ID],
                        "catalog_snapshot_id": snapshot_id,
                    }
                },
            },
            input_artifacts=(script_ref(),),
        )
        acquirer = FakeAcquirer()
        calls: list[tuple[Any, ...]] = []

        def search(*arguments: Any):
            calls.append(arguments)
            return ()

        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            FakeStorage(acquisition_script()),
            search_candidates=search,
            acquire_assets=acquirer,
        )

        with self.assertRaises(PermanentStepError) as raised:
            await capability.execute(step_context)

        self.assertEqual("asset_coverage_insufficient", raised.exception.code)
        self.assertEqual(snapshot_id, raised.exception.details["catalog_snapshot_id"])
        self.assertEqual([], acquirer.calls)
        self.assertTrue(calls)
        self.assertTrue(all(call[-1] == snapshot_id for call in calls))

    async def test_inventory_summarizes_partial_frozen_coverage(self) -> None:
        snapshot_id = "33333333-3333-4333-8333-333333333333"
        calls: list[tuple[Any, ...]] = []

        def search(*arguments: Any):
            calls.append(arguments)
            return (candidate("covered", text="launch"),) if arguments[2] == "launch" else ()

        storage = FakeStorage({})
        capability = DatabaseInventoryCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="inventory",
            input_snapshot={
                "topic": "mission",
                "inventory_concepts": ["launch", "night"],
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [_LIBRARY_ID],
                        "catalog_snapshot_id": snapshot_id,
                    }
                },
            },
        )

        result = await capability.execute(step_context)
        inventory = json.loads(storage.published[0].data)

        self.assertEqual("partial", inventory["coverage"]["status"])
        self.assertEqual(["night"], inventory["coverage"]["missing_concepts"])
        self.assertEqual(snapshot_id, inventory["catalog_snapshot_id"])
        self.assertEqual("inventory", result.artifacts[0].kind)
        self.assertTrue(all(call[-1] == snapshot_id for call in calls))

    async def test_inventory_zero_coverage_is_typed_and_non_reviewable(self) -> None:
        snapshot_id = "33333333-3333-4333-8333-333333333333"
        capability = DatabaseInventoryCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            FakeStorage({}),
            search_candidates=lambda *_: (),
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="inventory",
            input_snapshot={
                "topic": "uncovered",
                "_framefactory": {
                    "composition_snapshot": {"catalog_snapshot_id": snapshot_id}
                },
            },
        )

        with self.assertRaises(PermanentStepError) as raised:
            await capability.execute(step_context)

        self.assertEqual("asset_coverage_insufficient", raised.exception.code)
        self.assertEqual(["uncovered"], raised.exception.details["missing_concepts"])

    async def test_saves_top_five_but_only_materializes_first_three(self) -> None:
        script = {
            "beats": [
                {
                    "id": "beat-one",
                    "sequence": 1,
                    "narration": "A narration.",
                    "visual_description": "visual one",
                    "must_match": [],
                    "must_not_match": [],
                }
            ]
        }
        candidates = tuple(
            candidate(f"asset-{index}", score=0.90 - index / 100) for index in range(7)
        )
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: candidates,
        )

        result = await capability.execute(context())

        payload = manifest(storage)
        self.assertFalse(result.requires_review)
        self.assertEqual("verified", payload["rights_status"])
        self.assertEqual(5, len(payload["beats"][0]["candidates"]))
        self.assertEqual(3, len(storage.copies))
        self.assertEqual(
            ["objects/file-asset-0", "objects/file-asset-1", "objects/file-asset-2"],
            [item["source_key"] for item in storage.copies],
        )
        self.assertEqual(
            [True, True, True, False, False],
            [item["materialized"] for item in payload["beats"][0]["candidates"]],
        )
        self.assertEqual(
            ["asset", "asset", "asset", "candidate_manifest"],
            [artifact.kind for artifact in result.artifacts],
        )

    async def test_deduplicates_materialized_files_across_beats(self) -> None:
        script = {
            "beats": [
                {
                    "id": "first",
                    "sequence": 1,
                    "narration": "First.",
                    "visual_description": "first visual",
                    "must_match": [],
                    "must_not_match": [],
                },
                {
                    "id": "second",
                    "sequence": 2,
                    "narration": "Second.",
                    "visual_description": "second visual",
                    "must_match": [],
                    "must_not_match": [],
                },
            ]
        }
        shared_first = candidate(
            "shared", asset_file_id="shared-file", segment_id="shared-first"
        )
        shared_second = candidate(
            "shared",
            asset_file_id="shared-file",
            segment_id="shared-second",
            start_ms=20_000,
        )
        results = {
            "first visual": (shared_first, candidate("first-only")),
            "second visual": (shared_second, candidate("second-only")),
        }
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda _workspace, _libraries, query, _limit: results[
                query
            ],
        )

        result = await capability.execute(context())

        payload = manifest(storage)
        self.assertFalse(result.requires_review)
        self.assertEqual(3, len(storage.copies))
        first_shared = next(
            item
            for item in payload["beats"][0]["candidates"]
            if item["asset_file_id"] == "shared-file"
        )
        second_shared = next(
            item
            for item in payload["beats"][1]["candidates"]
            if item["asset_file_id"] == "shared-file"
        )
        self.assertEqual(first_shared["artifact_id"], second_shared["artifact_id"])
        self.assertEqual("shared-file", first_shared["asset_file_id"])
        self.assertEqual(60_000, first_shared["source_duration_ms"])
        self.assertEqual(
            {"start_ms": 5_000, "end_ms": 9_000, "duration_ms": 4_000},
            first_shared["source_window"],
        )
        self.assertIn("score_breakdown", first_shared)
        self.assertIn("hard_constraints", first_shared)
        self.assertIn("rejection_codes", first_shared)
        self.assertIn("rights_evidence", first_shared)
        self.assertIn("cut_evidence", first_shared)
        self.assertEqual(3, len(payload["materialized_assets"]))

    async def test_deduplicates_different_files_with_the_same_content_hash(
        self,
    ) -> None:
        script = {
            "beats": [
                {
                    "id": "first-hash",
                    "sequence": 1,
                    "narration": "First hash.",
                    "visual_description": "first hash visual",
                    "must_match": [],
                    "must_not_match": [],
                },
                {
                    "id": "second-hash",
                    "sequence": 2,
                    "narration": "Second hash.",
                    "visual_description": "second hash visual",
                    "must_match": [],
                    "must_not_match": [],
                },
            ]
        }
        shared_hash = "a" * 64
        first = candidate("asset-a", asset_file_id="file-a", content_hash=shared_hash)
        second = candidate("asset-b", asset_file_id="file-b", content_hash=shared_hash)
        results = {
            "first hash visual": (first,),
            "second hash visual": (second,),
        }
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda _workspace, _libraries, query, _limit: results[
                query
            ],
        )

        result = await capability.execute(context())

        payload = manifest(storage)
        first_item = payload["beats"][0]["candidates"][0]
        second_item = payload["beats"][1]["candidates"][0]
        self.assertFalse(result.requires_review)
        self.assertEqual(1, len(storage.copies))
        self.assertEqual(shared_hash, storage.copies[0]["content_hash"])
        self.assertEqual("file-a", first_item["asset_file_id"])
        self.assertEqual("file-b", second_item["asset_file_id"])
        self.assertEqual(first_item["artifact_id"], second_item["artifact_id"])
        self.assertEqual(1, len(payload["materialized_assets"]))

    async def test_coverage_gap_returns_review_without_network_acquisition(
        self,
    ) -> None:
        script = {
            "beats": [
                {
                    "id": "covered",
                    "sequence": 1,
                    "narration": "Covered.",
                    "visual_description": "covered visual",
                    "must_match": [],
                    "must_not_match": [],
                },
                {
                    "id": "missing",
                    "sequence": 2,
                    "narration": "Missing.",
                    "visual_description": "missing visual",
                    "must_match": ["required-a", "required-b"],
                    "must_not_match": [],
                },
            ]
        }
        storage = FakeStorage(script)

        def search(
            _workspace: str, _libraries: tuple[str, ...], query: str, _limit: int
        ):
            if query == "covered visual":
                return (candidate("covered"),)
            return (candidate("partial", text="required-a only"),)

        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
        )

        result = await capability.execute(context())

        payload = manifest(storage)
        self.assertTrue(result.requires_review)
        self.assertEqual("asset_coverage", result.output_summary["blocking_reason"])
        self.assertFalse(result.output_summary["auto_acquisition_enabled"])
        self.assertEqual(["missing"], payload["coverage"]["missing_beat_ids"])
        self.assertEqual("requires_review", payload["coverage"]["status"])
        self.assertEqual(
            "must_match_missing",
            payload["beats"][1]["candidates"][0]["rejection_codes"][0],
        )
        self.assertEqual(1, len(storage.copies))
        self.assertEqual(
            ["asset", "candidate_manifest"],
            [artifact.kind for artifact in result.artifacts],
        )

    async def test_partial_coverage_keeps_real_media_and_fills_only_missing_beats(
        self,
    ) -> None:
        script = {
            "beats": [
                {
                    "id": "covered",
                    "sequence": 1,
                    "narration": "Covered.",
                    "visual_description": "covered visual",
                    "must_match": [],
                    "must_not_match": [],
                },
                {
                    "id": "missing",
                    "sequence": 2,
                    "narration": "Missing.",
                    "visual_description": "missing visual",
                    "must_match": ["required"],
                    "must_not_match": [],
                },
            ]
        }
        storage = FakeStorage(script)

        def search(
            _workspace: str, _libraries: tuple[str, ...], query: str, _limit: int
        ):
            return (candidate("covered"),) if query == "covered visual" else ()

        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
        )
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="retrieve",
            input_snapshot={
                "_framefactory": {
                    "composition_snapshot": {
                        "asset_library_ids": [_LIBRARY_ID],
                        "production_settings": {
                            "no_asset_draft": {"enabled": True}
                        },
                    }
                }
            },
            input_artifacts=(script_ref(),),
        )

        result = await capability.execute(step_context)

        payload = manifest(storage)
        self.assertFalse(result.requires_review)
        self.assertEqual("hybrid-local-and-editorial", payload["provider"])
        self.assertEqual("complete", payload["coverage"]["status"])
        self.assertEqual([], payload["coverage"]["missing_beat_ids"])
        self.assertEqual("asset-001.mp4", payload["beats"][0]["candidates"][0]["artifact_filename"])
        self.assertEqual("editorial-card-002.bmp", payload["beats"][1]["candidates"][0]["artifact_filename"])
        self.assertEqual("hybrid_editorial_fallback", result.output_summary["visual_source_mode"])
        self.assertEqual(1, result.output_summary["temporary_visuals"])
        self.assertEqual(2, result.output_summary["materialized_assets"])
        self.assertEqual(
            ["asset", "asset", "video_plan", "candidate_manifest"],
            [artifact.kind for artifact in result.artifacts],
        )
        assert_candidate_manifest_valid(payload)

    async def test_complete_local_coverage_never_calls_enabled_acquisition(self) -> None:
        storage = FakeStorage(acquisition_script())
        acquirer = FakeAcquirer()
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda _workspace, _libraries, query, _limit: (
                candidate("local", text=query),
            ),
            acquire_assets=acquirer,
        )

        result = await capability.execute(acquisition_context())

        self.assertFalse(result.requires_review)
        self.assertEqual([], acquirer.calls)
        self.assertTrue(result.output_summary["auto_acquisition_enabled"])
        self.assertFalse(result.output_summary["acquisition_attempted"])
        self.assertEqual(
            "local_coverage_complete",
            result.output_summary["acquisition"]["skipped_reason"],
        )

    async def test_acquires_only_missing_beats_then_requeries_them(self) -> None:
        available = False
        searches: list[str] = []

        def search(
            _workspace: str,
            _libraries: tuple[str, ...],
            query: str,
            _limit: int,
        ) -> tuple[RetrievalCandidate, ...]:
            searches.append(query)
            if query == "Apollo 11 Saturn V launch":
                return (candidate("launch"),)
            if available:
                return (
                    candidate(
                        "landing",
                        text="Apollo 11 moon landing first steps",
                        copyright_status="public_domain",
                        rights_evidence=(
                            {
                                "source_type": "website",
                                "locator": "https://commons.wikimedia.org/wiki/File:Apollo_11_Landing_first_steps.ogv",
                                "license": "Public domain",
                                "evidence_type": "verified_public_domain",
                                "verified_at": "2026-08-22T12:00:00Z",
                            },
                        ),
                    ),
                )
            return ()

        def enable_candidate() -> None:
            nonlocal available
            available = True

        acquirer = FakeAcquirer(on_acquire=enable_candidate)
        storage = FakeStorage(acquisition_script())
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
            acquire_assets=acquirer,
        )

        result = await capability.execute(acquisition_context())

        self.assertFalse(result.requires_review)
        self.assertEqual(1, len(acquirer.calls))
        self.assertEqual(
            ("Apollo 11 moon landing first steps Apollo 11 moon",),
            acquirer.calls[0]["queries"],
        )
        self.assertEqual(("wikimedia",), acquirer.calls[0]["sources"])
        self.assertEqual(1, searches.count("Apollo 11 Saturn V launch"))
        self.assertEqual(
            2,
            searches.count("Apollo 11 moon landing first steps Apollo 11 moon"),
        )
        self.assertTrue(result.output_summary["local_catalog_only"])
        self.assertTrue(result.output_summary["external_acquisition_involved"])
        audit = manifest(storage)["acquisition"]
        self.assertTrue(audit["attempted"])
        self.assertEqual(["missing"], audit["queried_beat_ids"])
        self.assertEqual(1, audit["imported_count"])

    async def test_chinese_beats_use_user_authored_latin_provider_fallbacks(
        self,
    ) -> None:
        available = False

        def search(
            _workspace: str,
            _libraries: tuple[str, ...],
            _query: str,
            _limit: int,
        ) -> tuple[RetrievalCandidate, ...]:
            if not available:
                return ()
            return (
                candidate(
                    "moonwalk",
                    text="宇航员站在月球表面，远处可见登月舱",
                    copyright_status="public_domain",
                    rights_evidence=(
                        {
                            "source_type": "website",
                            "locator": (
                                "https://commons.wikimedia.org/wiki/"
                                "File:Apollo_11_moonwalk.ogv"
                            ),
                            "license": "Public domain",
                            "evidence_type": "verified_public_domain",
                            "verified_at": "2026-08-22T12:00:00Z",
                        },
                    ),
                ),
            )

        def enable_candidate() -> None:
            nonlocal available
            available = True

        script = {
            "beats": [
                {
                    "id": "moonwalk",
                    "sequence": 1,
                    "narration": "宇航员踏上月面。",
                    "visual_description": "宇航员在月面缓慢行走，背景可见登月舱。",
                    "must_match": [],
                    "must_not_match": [],
                }
            ]
        }
        step_context = StepContext(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="retrieve",
            input_snapshot={
                "topic": "首次月面行走",
                "angle": (
                    "检索 Neil Armstrong first steps、Moon surface、"
                    "Lunar Module Eagle 的公版画面。"
                ),
                "source_urls": ["https://www.nasa.gov/mission/apollo-11/"],
                "_framefactory": {
                    "private_provider_hint": "DO NOT LEAK SECRET TOKEN",
                    "composition_snapshot": {
                        "asset_library_ids": [_LIBRARY_ID],
                        "production_settings": {
                            "asset_acquisition": {
                                "enabled": True,
                                "sources": ["wikimedia"],
                                "max_assets": 2,
                                "copyright_status": "public_domain",
                                "rights_confirmed": True,
                            }
                        },
                    },
                },
            },
            input_artifacts=(script_ref(),),
            attempt=1,
            maximum_attempts=3,
        )
        acquirer = FakeAcquirer(on_acquire=enable_candidate)
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
            acquire_assets=acquirer,
        )

        result = await capability.execute(step_context)

        self.assertFalse(result.requires_review)
        queries = acquirer.calls[0]["queries"]
        self.assertEqual(1, len(queries))
        self.assertTrue(queries[0].startswith("Neil Armstrong first steps "))
        self.assertTrue(queries[0].endswith(script["beats"][0]["visual_description"]))
        joined = " ".join(queries).casefold()
        self.assertNotIn("https://", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("token", joined)
        audit = manifest(storage)["acquisition"]
        self.assertEqual(list(queries), audit["queries"])
        self.assertEqual(
            script["beats"][0]["visual_description"],
            manifest(storage)["beats"][0]["query"],
        )

    async def test_imported_asset_waits_for_analysis_and_requests_auto_resume(self) -> None:
        storage = FakeStorage(
            {
                "beats": [acquisition_script()["beats"][1]],
            }
        )
        acquirer = FakeAcquirer(AssetAcquisitionResult(1, ()))
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (),
            acquire_assets=acquirer,
        )

        result = await capability.execute(acquisition_context())

        self.assertTrue(result.requires_review)
        self.assertTrue(result.output_summary["auto_resume_pending"])
        self.assertEqual(
            "auto_resume_after_asset_analysis",
            result.output_summary["action_required"],
        )
        self.assertEqual("requires_review", result.output_summary["rights_status"])
        self.assertEqual(
            ["candidate_manifest"], [artifact.kind for artifact in result.artifacts]
        )

    async def test_unverified_acquired_rights_remain_fail_closed(self) -> None:
        available = False

        def search(*_: Any) -> tuple[RetrievalCandidate, ...]:
            return (
                candidate(
                    "unverified",
                    text="Apollo 11 moon landing first steps",
                    copyright_status="public_domain",
                    rights_evidence=(
                        {
                            "source_type": "website",
                            "locator": "https://upload.wikimedia.org/example.webm",
                            "license": "Public domain",
                            "evidence_type": "copyright",
                            "verified_at": None,
                        },
                    ),
                ),
            ) if available else ()

        def enable_candidate() -> None:
            nonlocal available
            available = True

        storage = FakeStorage({"beats": [acquisition_script()["beats"][1]]})
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=search,
            acquire_assets=FakeAcquirer(on_acquire=enable_candidate),
        )

        result = await capability.execute(acquisition_context())

        recalled = manifest(storage)["beats"][0]["candidates"][0]
        self.assertTrue(result.requires_review)
        self.assertIn("rights_evidence_unverified", recalled["rejection_codes"])
        self.assertEqual([], storage.copies)

    async def test_unconfirmed_acquisition_is_rejected_before_provider_call(self) -> None:
        acquirer = FakeAcquirer()
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            FakeStorage({"beats": [acquisition_script()["beats"][1]]}),
            search_candidates=lambda *_: (),
            acquire_assets=acquirer,
        )

        with self.assertRaisesRegex(PermanentStepError, "rights confirmation"):
            await capability.execute(
                acquisition_context(
                    rights_confirmed=False,
                    sources=("youtube",),
                    copyright_status="licensed",
                )
            )

        self.assertEqual([], acquirer.calls)

    async def test_retryable_provider_outage_uses_worker_retry_budget(self) -> None:
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            FakeStorage({"beats": [acquisition_script()["beats"][1]]}),
            search_candidates=lambda *_: (),
            acquire_assets=FakeAcquirer(
                AssetAcquisitionResult(
                    0,
                    ("Apollo 11 moon landing first steps",),
                    ({"platform": "wikimedia", "retryable": True},),
                )
            ),
        )

        with self.assertRaises(RetryableStepError):
            await capability.execute(acquisition_context())

    async def test_provider_error_audit_is_bounded_and_schema_valid(self) -> None:
        provider_errors = tuple(
            {
                "platform": "wikimedia",
                "code": f"REMOTE_CANDIDATE_REJECTED_{index:03d}",
                "retryable": False,
            }
            for index in range(137)
        )
        storage = FakeStorage({"beats": [acquisition_script()["beats"][1]]})
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (),
            acquire_assets=FakeAcquirer(
                AssetAcquisitionResult(
                    0,
                    ("Apollo 11 moon landing first steps",),
                    provider_errors,
                )
            ),
        )

        result = await capability.execute(acquisition_context())

        generated = manifest(storage)
        audit = generated["acquisition"]
        summary_audit = result.output_summary["acquisition"]
        self.assertEqual(
            audit["provider_errors"],
            [dict(item) for item in summary_audit["provider_errors"]],
        )
        self.assertEqual(
            audit["provider_errors_total"], summary_audit["provider_errors_total"]
        )
        self.assertEqual(
            audit["provider_errors_truncated"],
            summary_audit["provider_errors_truncated"],
        )
        self.assertEqual(100, len(audit["provider_errors"]))
        self.assertEqual(137, audit["provider_errors_total"])
        self.assertTrue(audit["provider_errors_truncated"])
        self.assertEqual(
            "REMOTE_CANDIDATE_REJECTED_099",
            audit["provider_errors"][-1]["code"],
        )
        assert_candidate_manifest_valid(generated)

    async def test_manifest_bounds_long_authored_id_and_excess_candidate_labels(
        self,
    ) -> None:
        labels = (
            " duplicate ",
            "duplicate",
            "x" * 300,
            *(f"label-{index:03d}" for index in range(300)),
        )
        rights_sources = tuple(
            {
                "source_id": f"noise-{index:04d}",
                "license": "unknown",
                "evidence_type": "copyright",
            }
            for index in range(1_000)
        ) + (
            {
                "source_id": "verified-license",
                "license": "CC BY 4.0",
                "evidence_type": "verified_license",
            },
        )
        storage = FakeStorage(
            {
                "beats": [
                    {
                        "id": "authored-" + "x" * 200,
                        "sequence": 1,
                        "narration": "A schema boundary is exercised.",
                        "visual_description": "schema boundary visual",
                        "must_match": [],
                        "must_not_match": [],
                    }
                ]
            }
        )
        recalled = candidate(
            "33333333-3333-4333-8333-333333333333",
            asset_file_id="44444444-4444-4444-8444-444444444444",
            analysis_id="55555555-5555-4555-8555-555555555555",
            segment_id="66666666-6666-4666-8666-666666666666",
            score=0.1,
            labels=labels,
            rights_evidence=rights_sources,
        )
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (recalled,),
        )

        await capability.execute(context())

        generated = manifest(storage)
        beat = generated["beats"][0]
        emitted_labels = beat["candidates"][0]["labels"]
        rights = beat["candidates"][0]["rights_evidence"]
        self.assertTrue(beat["id"].startswith("beat-"))
        self.assertLessEqual(len(beat["id"]), 160)
        self.assertEqual(256, len(emitted_labels))
        self.assertEqual("duplicate", emitted_labels[0])
        self.assertEqual("x" * 240, emitted_labels[1])
        self.assertEqual(len(emitted_labels), len(set(emitted_labels)))
        self.assertTrue(all(1 <= len(label) <= 240 for label in emitted_labels))
        self.assertEqual(1_000, len(rights["sources"]))
        self.assertEqual("verified-license", rights["sources"][0]["source_id"])
        self.assertTrue(rights_evidence_valid(rights))
        assert_candidate_manifest_valid(generated)

    async def test_materialized_filename_and_media_type_are_schema_safe(self) -> None:
        storage = FakeStorage(
            {
                "beats": [
                    {
                        "id": "misleading-suffix",
                        "sequence": 1,
                        "narration": "The first source is a video.",
                        "visual_description": "misleading video suffix",
                    },
                    {
                        "id": "oversized-suffix",
                        "sequence": 2,
                        "narration": "The second source is also a video.",
                        "visual_description": "oversized video suffix",
                    },
                ]
            }
        )
        recalled = {
            "misleading video suffix": candidate(
                "33333333-3333-4333-8333-333333333333",
                asset_file_id="44444444-4444-4444-8444-444444444444",
                analysis_id="55555555-5555-4555-8555-555555555555",
                segment_id="66666666-6666-4666-8666-666666666666",
                media_type="Video/MP4",
                original_filename="misleading.jpg",
            ),
            "oversized video suffix": candidate(
                "83333333-3333-4333-8333-333333333333",
                asset_file_id="84444444-4444-4444-8444-444444444444",
                analysis_id="85555555-5555-4555-8555-555555555555",
                segment_id="86666666-6666-4666-8666-666666666666",
                original_filename="source." + "x" * 248,
            ),
        }
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda _workspace, _libraries, query, _limit: (
                recalled[query],
            ),
        )

        result = await capability.execute(context())

        self.assertFalse(result.requires_review)
        self.assertEqual(
            ["asset-001.mp4", "asset-002.mp4"],
            [item["filename"] for item in storage.copies],
        )
        self.assertTrue(
            all(len(item["filename"]) <= 255 for item in storage.copies)
        )
        self.assertEqual(
            ["video/mp4", "video/mp4"],
            [item["media_type"] for item in storage.copies],
        )
        generated = manifest(storage)
        self.assertTrue(
            all(
                item["asset_kind"] == "video"
                for beat in generated["beats"]
                for item in beat["candidates"]
            )
        )
        assert_candidate_manifest_valid(generated)

    async def test_permanent_no_result_and_exhausted_budget_are_reviewable(self) -> None:
        acquirer = FakeAcquirer(
            AssetAcquisitionResult(
                0,
                ("Apollo 11 moon landing first steps",),
                ({"platform": "wikimedia", "retryable": False},),
            )
        )
        storage = FakeStorage({"beats": [acquisition_script()["beats"][1]]})
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (),
            acquire_assets=acquirer,
        )

        no_result = await capability.execute(acquisition_context())
        exhausted = await capability.execute(
            acquisition_context(attempt=3, maximum_attempts=3)
        )

        self.assertTrue(no_result.requires_review)
        self.assertEqual(
            "review_provider_results_or_upload_assets_then_request_changes",
            no_result.output_summary["action_required"],
        )
        self.assertTrue(exhausted.requires_review)
        self.assertEqual(1, len(acquirer.calls))
        self.assertEqual(
            "automatic_attempt_budget_exhausted",
            exhausted.output_summary["acquisition"]["skipped_reason"],
        )

    async def test_successful_resume_preserves_confirmed_acquisition_lineage(self) -> None:
        storage = FakeStorage({"beats": [acquisition_script()["beats"][1]]})
        acquirer = FakeAcquirer()
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (
                candidate(
                    "analyzed",
                    text="Apollo 11 moon landing first steps",
                    copyright_status="public_domain",
                    rights_evidence=(
                        {
                            "source_type": "website",
                            "locator": "https://commons.wikimedia.org/wiki/File:Apollo_11_Landing_first_steps.ogv",
                            "license": "Public domain",
                            "evidence_type": "verified_public_domain",
                            "verified_at": "2026-08-22T12:00:00Z",
                        },
                    ),
                ),
            ),
            acquire_assets=acquirer,
        )

        result = await capability.execute(
            acquisition_context(
                attempt=2,
                review_feedback=(
                    "自动补充素材已完成安全分析与打标，系统正在重新匹配素材"
                ),
            )
        )

        self.assertFalse(result.requires_review)
        self.assertEqual([], acquirer.calls)
        self.assertTrue(result.output_summary["local_catalog_only"])
        self.assertTrue(result.output_summary["external_acquisition_involved"])
        self.assertTrue(
            manifest(storage)["acquisition"]["resumed_after_asset_analysis"]
        )

    def test_acquisition_queries_are_stable_deduplicated_and_bounded(self) -> None:
        repeated = RetrievalBeat("same", 1, "Narration", "Apollo 11 landing")
        duplicate = RetrievalBeat("duplicate", 2, "Narration", "Apollo 11 landing")
        unique = tuple(
            RetrievalBeat(f"beat-{index}", index + 3, "Narration", f"shot {index}")
            for index in range(20)
        )

        first = _acquisition_query_plan((repeated, duplicate, *unique))
        second = _acquisition_query_plan((repeated, duplicate, *unique))

        self.assertEqual(first, second)
        self.assertEqual(12, len(first))
        self.assertEqual(12, len({query.casefold() for _beat, query in first}))

    def test_acquisition_query_hints_are_stable_bounded_and_private(self) -> None:
        beats = tuple(
            RetrievalBeat(
                f"beat-{index}",
                index + 1,
                "旁白",
                f"第{index + 1}个中文月面镜头",
            )
            for index in range(5)
        )
        snapshot = {
            "topic": "Apollo 11 首次月面行走",
            "angle": (
                "使用 Neil Armstrong first steps、astronaut on Moon、"
                "Moon surface、Lunar Module Eagle；禁止使用 modern movie clip。"
                "不得使用 fake reconstruction footage。"
                "不要把 modern simulation footage 当素材。"
                "请避开 staged reenactment video。"
                "without synthetic animation。"
            ),
            "source_urls": ["https://www.nasa.gov/mission/apollo-11/"],
            "private_notes": "SECRET PROJECT HELIOS",
            "provider_metadata": {"search_hint": "CONFIDENTIAL LAUNCH WINDOW"},
            "_framefactory": {
                "provider_secret": "SECRET TOKEN MUST NOT LEAK",
                "composition_snapshot": {"language": "zh-CN"},
            },
        }

        first = _acquisition_query_plan(beats, snapshot=snapshot)
        second = _acquisition_query_plan(beats, snapshot=snapshot)
        queries = [query for _beat_id, query in first]

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 12)
        self.assertEqual(len(beats), len(queries))
        self.assertTrue(queries[0].startswith("Apollo 11 "))
        self.assertTrue(queries[1].startswith("Neil Armstrong first steps "))
        self.assertTrue(queries[2].startswith("astronaut Moon "))
        self.assertTrue(queries[3].startswith("Moon surface "))
        self.assertTrue(queries[4].startswith("Lunar Module Eagle "))
        for beat, query in zip(beats, queries, strict=True):
            self.assertTrue(query.endswith(beat.visual_description))
        joined = " ".join(queries).casefold()
        self.assertNotIn("https://", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("token", joined)
        self.assertNotIn("modern movie", joined)
        self.assertNotIn("fake reconstruction", joined)
        self.assertNotIn("modern simulation", joined)
        self.assertNotIn("staged reenactment", joined)
        self.assertNotIn("synthetic animation", joined)
        self.assertNotIn("project helios", joined)
        self.assertNotIn("confidential launch", joined)

    async def test_missing_rights_evidence_creates_review_gap(self) -> None:
        script = {
            "beats": [
                {
                    "id": "rights-gap",
                    "sequence": 1,
                    "narration": "Rights gap.",
                    "visual_description": "licensed footage",
                    "must_match": [],
                    "must_not_match": [],
                }
            ]
        }
        storage = FakeStorage(script)
        capability = DatabaseRetrievalCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_candidates=lambda *_: (
                candidate("missing-rights", rights_evidence=()),
            ),
        )

        result = await capability.execute(context())

        payload = manifest(storage)
        recalled = payload["beats"][0]["candidates"][0]
        self.assertTrue(result.requires_review)
        self.assertEqual("asset_coverage", result.output_summary["blocking_reason"])
        self.assertEqual("requires_review", result.output_summary["rights_status"])
        self.assertEqual("requires_review", payload["rights_status"])
        self.assertEqual([], payload["materialized_assets"])
        self.assertEqual([], storage.copies)
        self.assertIn("rights_evidence_missing", recalled["rejection_codes"])
        self.assertFalse(recalled["rights_evidence"]["verified"])

    def test_catalog_query_returns_lineage_and_rights_and_fails_closed(self) -> None:
        source = inspect.getsource(PostgresRetrievalCatalog.search)
        for field in (
            "asset_file_id",
            "analysis_id",
            "segment_id",
            "rights_evidence",
            "af.duration_ms AS source_duration_ms",
            "a.kind AS asset_kind",
            "a.library_id=ANY",
            "a.kind IN ('image','video')",
            "a.kind='image'",
            "a.kind='video'",
            "af.duration_ms IS NOT NULL",
            "s.end_ms <= af.duration_ms",
            "a.status='ready'",
            "a.analysis_status='completed'",
            "f.scan_status='clean'",
            "aa.analysis_version=(",
            "a.copyright_status IN ('owned','licensed','public_domain')",
        ):
            self.assertIn(field, source)
        self.assertNotIn("acquire", source.casefold())
        self.assertIn("af.id=csi.asset_file_id", source)
        self.assertIn("csi.analysis_id=aa.id", source)
        self.assertIn("af.content_hash=csi.content_hash", source)


if __name__ == "__main__":
    unittest.main()
