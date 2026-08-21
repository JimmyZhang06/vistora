from __future__ import annotations

import hashlib
import inspect
import io
import json
import math
import unittest
from dataclasses import replace
from typing import Any

from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.database_assets import (
    AssetAcquisitionResult,
    AssetMatch,
    ControlApiAssetAcquirer,
    DatabaseAssetCapability,
    PostgresAssetCatalog,
    _acquisition_queries,
    _matches_beat_constraints,
    _matches_visual_anchor,
    _scene_queries,
    _scene_requirements,
    _semantic_query,
)
from framefactory.worker.config import AssetLibrarySettings
from framefactory.worker.providers import ProviderArtifact


class FakeStorage:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.published: list[ProviderArtifact] = []

    def read_json(self, artifact: ArtifactRef) -> dict[str, Any]:
        return self.script

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        raise NotImplementedError

    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef:
        self.published.append(artifact)
        digest = hashlib.sha256(artifact.data).hexdigest()
        return ArtifactRef(
            id=f"artifact-{len(self.published)}",
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"objects/{len(self.published)}",
            byte_size=len(artifact.data),
            content_hash=digest,
            filename=artifact.filename,
        )


class FakeObjects:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values
        self.reads: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.reads.append(Key)
        return {"Body": io.BytesIO(self.values[Key])}


def script_ref() -> ArtifactRef:
    return ArtifactRef(
        id="script",
        workspace_id="workspace",
        run_id="run",
        step_id="write",
        kind="script",
        media_type="application/json",
        object_key="scripts/one",
        byte_size=1,
        content_hash="0" * 64,
        filename="script.json",
    )


def context() -> StepContext:
    return StepContext(
        workspace_id="workspace",
        run_id="run",
        step_id="assets",
        input_snapshot={
            "_framefactory": {
                "composition_snapshot": {"asset_library_ids": ["library-one"]}
            }
        },
        input_artifacts=(script_ref(),),
    )


def acquisition_context() -> StepContext:
    base = context()
    return replace(
        base,
        input_snapshot={
            "_framefactory": {
                "composition_snapshot": {
                    "asset_library_ids": ["library-one"],
                    "production_settings": {
                        "asset_acquisition": {
                            "enabled": True,
                            "sources": ["youtube", "bilibili"],
                            "max_assets": 2,
                            "copyright_status": "licensed",
                            "rights_confirmed": True,
                        }
                    },
                }
            }
        },
    )


class FakeAcquirer:
    def __init__(self, on_acquire=None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.on_acquire = on_acquire

    async def acquire(self, **request: Any) -> AssetAcquisitionResult:
        self.calls.append(request)
        if self.on_acquire:
            self.on_acquire()
        return AssetAcquisitionResult(1, ())


class FakeControlApiAcquirer(ControlApiAssetAcquirer):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8200", timeout_seconds=5)
        self.request = None

    def _send(self, request):
        self.request = request
        return json.dumps({
            "imported_count": 2,
            "unresolved_queries": ["缺少的片尾"],
            "provider_errors": [],
        }, ensure_ascii=False).encode()


def match(asset_id: str, key: str, data: bytes) -> AssetMatch:
    return AssetMatch(
        asset_id=asset_id,
        title=asset_id,
        bucket="bucket",
        object_key=key,
        media_type="video/mp4",
        content_hash=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        original_filename="source.mp4",
        start_ms=1000,
        end_ms=4000,
        description=f"description {asset_id}",
        labels=(asset_id,),
        score=0.72,
    )


class DatabaseAssetCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_timing_markers_are_not_treated_as_missing_visual_scenes(self) -> None:
        self.assertEqual(
            ("许昕正手拉球", "赛后挥手"),
            _scene_queries(
                {"scenes": ["0.00-3.00", "许昕正手拉球", "00:03.000—00:08.000", "赛后挥手"]}
            ),
        )

    def test_verbose_storyboards_become_short_subject_media_queries(self) -> None:
        queries = _acquisition_queries(
            {"topic": "许昕，天才直板"},
            (
                "画面2：许昕训练画面，左手持拍，快速拧拉，镜头拉近手部动作。",
                "画面4：技术分析图示，显示关键分决策时间对比。",
            ),
            limit=3,
        )

        self.assertIn("训练", queries[0])
        self.assertIn("技术", queries[1])
        self.assertEqual("许昕 相关画面", queries[-1])
        self.assertEqual(len(queries), len(set(queries)))

    def test_acquisition_query_preserves_authored_beat_without_topic_dictionary(self) -> None:
        queries = _acquisition_queries(
            {"topic": "任意新领域"},
            ("透明容器中的液体由蓝色逐渐变成紫色",),
            limit=4,
        )

        self.assertEqual("透明容器中的液体由蓝色逐渐变成紫色", queries[0])
        self.assertNotIn("任意新领域", queries[0])

    def test_structured_beats_define_atomic_visual_units_for_any_subject(self) -> None:
        self.assertEqual(
            ("传送带送入原料", "机械臂完成组装", "成品通过检测"),
            _scene_queries(
                {
                    "beats": [
                        {"sequence": 1, "visual_description": "传送带送入原料"},
                        {"sequence": 2, "visual_description": "机械臂完成组装"},
                        {"sequence": 3, "visual_description": "成品通过检测"},
                    ]
                }
            ),
        )

    def test_transition_language_splits_legacy_compound_scene_generically(self) -> None:
        self.assertEqual(
            ("原料进入反应器。", "随后液体颜色发生改变"),
            _scene_queries({"scenes": ["原料进入反应器，随后液体颜色发生改变"]}),
        )

    def test_structured_beat_keeps_narration_and_evidence_constraints(self) -> None:
        requirements = _scene_requirements(
            {
                "beats": [
                    {
                        "id": "beat-1",
                        "sequence": 1,
                        "narration": "阀门打开后液体进入容器。",
                        "visual_description": "银色阀门向透明容器注入液体",
                        "must_match": ["阀门", "透明容器"],
                        "must_not_match": ["木质水桶"],
                    }
                ]
            }
        )

        self.assertEqual("阀门打开后液体进入容器。", requirements["银色阀门向透明容器注入液体"]["narration"])
        self.assertEqual(("阀门", "透明容器"), requirements["银色阀门向透明容器注入液体"]["must_match"])

    async def test_control_api_acquirer_sends_scoped_rights_confirmed_request(self) -> None:
        acquirer = FakeControlApiAcquirer()

        result = await acquirer.acquire(
            workspace_id="workspace-one",
            run_id="run-one",
            step_id="assets",
            library_id="library-one",
            queries=("许昕 直板",),
            sources=("bilibili",),
            max_assets=2,
            copyright_status="licensed",
        )

        self.assertEqual(2, result.imported_count)
        self.assertEqual(("缺少的片尾",), result.unresolved_queries)
        self.assertEqual("workspace-one", acquirer.request.get_header("X-workspace-id"))
        payload = json.loads(acquirer.request.data)
        self.assertTrue(payload["rights_confirmed"])
        self.assertEqual(["许昕 直板"], payload["queries"])
        self.assertEqual(["bilibili"], payload["sources"])

    def test_catalog_sql_enforces_scope_safety_and_complete_segment_labels(self) -> None:
        source = inspect.getsource(PostgresAssetCatalog.search)
        self.assertIn("a.library_id=ANY", source)
        self.assertIn("a.kind IN ('image','video')", source)
        self.assertIn("a.status='ready'", source)
        self.assertIn("af.scan_status='clean'", source)
        self.assertIn("a.copyright_status IN ('owned','licensed','public_domain')", source)
        self.assertIn("COALESCE(s.confidence,0) >= 0.5", source)
        self.assertIn("s.start_ms >= 5000", source)
        self.assertIn("f.duration_ms <= 10000", source)
        self.assertIn("s.cut_safe AND s.semantic_complete", source)
        self.assertIn("s.end_ms - s.start_ms >= 3000", source)
        self.assertIn("count(DISTINCT lower(label))", source)
        self.assertIn("制作名单", source)
        self.assertIn("f.byte_size", source)
        for field_name in ("s.mood", "s.visual_style", "s.shot_type"):
            self.assertIn(field_name, source)

    def test_semantic_query_removes_timing_without_inventing_domain_synonyms(self) -> None:
        query = _semantic_query(
            "00:10-00:20: 济州岛旧屋的客厅里，建筑师打开图纸。"
        )

        self.assertNotIn("00:10", query)
        self.assertEqual("济州岛旧屋的客厅里，建筑师打开图纸。", query)
        self.assertNotIn("办公室", query)

    def test_beat_constraints_are_domain_neutral_and_use_segment_evidence(self) -> None:
        relevant = replace(
            match("candidate-a", "a", b"video"),
            description="银色阀门向透明容器注入蓝色液体",
            labels=("阀门", "透明容器", "蓝色液体"),
        )
        wrong = replace(
            match("candidate-b", "b", b"video"),
            description="木质水桶放在户外地面",
            labels=("木质水桶", "户外"),
        )
        requirement = {
            "must_match": ("阀门", "透明容器"),
            "must_not_match": ("木质水桶",),
        }

        self.assertTrue(_matches_beat_constraints(requirement, relevant))
        self.assertFalse(_matches_beat_constraints(requirement, wrong))

    def test_generic_identity_gate_requires_named_subject_and_year(self) -> None:
        rotterdam = replace(
            match("zhang-jike-rotterdam", "rotterdam", b"video"),
            title="张继科 2011 鹿特丹世乒赛男单决赛",
            description="2011 鹿特丹世乒赛比赛",
            labels=("张继科", "2011", "鹿特丹", "世乒赛", "比赛", "发球"),
        )

        self.assertTrue(
            _matches_visual_anchor("张继科在2011年鹿特丹世乒赛比赛中击球", rotterdam)
        )
        self.assertFalse(
            _matches_visual_anchor("张继科在2012年伦敦奥运会比赛中对拉", rotterdam)
        )
        self.assertTrue(_matches_visual_anchor("张继科在训练中发球", rotterdam))

    def test_sports_anchor_requires_the_named_person(self) -> None:
        other_athlete = replace(
            match("other-athlete", "training", b"video"),
            title="国家队乒乓球训练",
            description="运动员进行发球训练",
            labels=("训练", "发球", "乒乓球"),
        )

        self.assertFalse(_matches_visual_anchor("张继科在训练中发球", other_athlete))

    def test_citation_doi_year_is_not_treated_as_footage_year(self) -> None:
        current_b_roll = replace(
            match("current-b-roll", "training", b"image"),
            title="樊振东乒乓球比赛",
            description="运动员在比赛中挥拍击球",
            labels=("樊振东", "乒乓球", "比赛", "击球"),
        )

        self.assertTrue(
            _matches_visual_anchor(
                "显示研究来源 DOI: 10.1080/17461391.2018.1534993",
                current_b_roll,
            )
        )

    async def test_selects_one_verified_segment_per_scene(self) -> None:
        files = {"one": b"first video", "two": b"second video"}
        matches = {
            "scene one": (match("asset-one", "one", files["one"]),),
            "scene two": (match("asset-two", "two", files["two"]),),
        }
        storage = FakeStorage({"title": "Story", "narration": "Words", "scenes": list(matches)})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda _workspace, _libraries, query, _limit, _threshold: matches[query],
            object_client=FakeObjects(files),
        )

        result = await capability.execute(context())

        self.assertEqual(3, len(result.artifacts))
        manifest = json.loads(storage.published[-1].data)
        self.assertEqual("database-asset-library", manifest["provider"])
        self.assertEqual(["scene one", "scene two"], [a["selected_for_scene"] for a in manifest["assets"]])
        self.assertTrue(all(item["rights_verified"] for item in manifest["assets"]))

    async def test_pauses_for_asset_input_instead_of_failing_the_run(self) -> None:
        storage = FakeStorage({"scenes": ["uncovered scene"], "narration": "Words"})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (),
            object_client=FakeObjects({}),
        )

        result = await capability.execute(context())

        self.assertTrue(result.requires_review)
        self.assertEqual("asset_coverage", result.output_summary["blocking_reason"])
        self.assertEqual(("uncovered scene",), result.output_summary["missing_scenes"])
        self.assertEqual((), result.artifacts)
        self.assertEqual([], storage.published)

    async def test_acquires_missing_assets_and_repeats_matching_before_render(self) -> None:
        data = b"newly acquired video"
        available = False

        def enable_asset() -> None:
            nonlocal available
            available = True

        acquirer = FakeAcquirer(enable_asset)
        storage = FakeStorage({"scenes": ["许昕 直板反手技术"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (
                (match("asset-xuxin", "xuxin", data),) if available else ()
            ),
            object_client=FakeObjects({"xuxin": data}),
            acquire_assets=acquirer,
        )

        result = await capability.execute(acquisition_context())

        self.assertFalse(result.requires_review)
        self.assertEqual(1, result.output_summary["acquired_assets"])
        self.assertTrue(result.output_summary["auto_acquisition_enabled"])
        self.assertEqual(1, len(acquirer.calls))
        self.assertIn("反手", acquirer.calls[0]["queries"][0])
        self.assertIn("许昕 直板反手技术", acquirer.calls[0]["queries"][0])
        self.assertEqual(("youtube", "bilibili"), acquirer.calls[0]["sources"])

    async def test_reports_imported_assets_waiting_for_analysis_without_false_failure(self) -> None:
        storage = FakeStorage({"scenes": ["许昕 技术分解"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (),
            object_client=FakeObjects({}),
            acquire_assets=FakeAcquirer(),
        )

        result = await capability.execute(acquisition_context())

        self.assertTrue(result.requires_review)
        self.assertEqual(1, result.output_summary["acquired_assets"])
        self.assertEqual(
            "auto_resume_after_asset_analysis",
            result.output_summary["action_required"],
        )
        self.assertTrue(result.output_summary["auto_resume_pending"])

    def test_timing_only_storyboard_uses_narration_sentences_as_scene_queries(self) -> None:
        self.assertEqual(
            ("大学里初次相识。", "多年后重建海边旧屋。", "建筑保存了记忆。"),
            _scene_queries(
                {
                    "scenes": ["00:00:00 - 00:00:08", "00:00:08 - 00:00:16"],
                    "narration": "大学里初次相识。多年后重建海边旧屋。建筑保存了记忆。",
                }
            ),
        )

    async def test_settled_acquisition_that_still_misses_scenes_requires_human_action(self) -> None:
        storage = FakeStorage({"scenes": ["许昕 技术分解"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (),
            object_client=FakeObjects({}),
            acquire_assets=FakeAcquirer(),
        )

        result = await capability.execute(replace(acquisition_context(), attempt=3))

        self.assertTrue(result.requires_review)
        self.assertFalse(result.output_summary["auto_resume_pending"])
        self.assertEqual(
            "review_acquired_assets_then_request_changes",
            result.output_summary["action_required"],
        )

    async def test_second_acquisition_round_can_still_resume_without_a_human(self) -> None:
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            FakeStorage({"scenes": ["极光夜空"]}),
            search_assets=lambda *_: (),
            object_client=FakeObjects({}),
            acquire_assets=FakeAcquirer(),
        )

        result = await capability.execute(replace(acquisition_context(), attempt=2))

        self.assertTrue(result.output_summary["auto_resume_pending"])
        self.assertEqual(
            "auto_resume_after_asset_analysis",
            result.output_summary["action_required"],
        )

    async def test_topic_fallback_reuses_a_verified_subject_asset_for_visual_directions(self) -> None:
        data = b"verified subject video"
        storage = FakeStorage({"scenes": ["黑屏标题卡", "赛后远景"]})
        fallback = "许昕 相关画面"
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda _workspace, _libraries, query, _limit, _threshold: (
                (match("asset-xuxin", "xuxin", data),) if query == fallback else ()
            ),
            object_client=FakeObjects({"xuxin": data}),
        )
        run_context = replace(
            context(),
            input_snapshot={
                "topic": "许昕：直板艺术家",
                "_framefactory": {"composition_snapshot": {"asset_library_ids": ["library-one"]}},
            },
        )

        result = await capability.execute(run_context)

        self.assertFalse(result.requires_review)
        self.assertEqual(2, len(json.loads(storage.published[-1].data)["assets"]))

    async def test_reuses_a_relevant_asset_across_multiple_scenes(self) -> None:
        files = {"one": b"first video"}
        relevant = match("asset-one", "one", files["one"])
        storage = FakeStorage({"scenes": ["scene one", "scene two"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (relevant,),
            object_client=FakeObjects(files),
        )

        result = await capability.execute(context())

        self.assertFalse(result.requires_review)
        self.assertEqual(2, len(result.artifacts))
        manifest = json.loads(storage.published[-1].data)
        self.assertEqual(2, len(manifest["assets"]))
        self.assertEqual(
            manifest["assets"][0]["artifact_id"],
            manifest["assets"][1]["artifact_id"],
        )

    async def test_spreads_scenes_across_distinct_segments_of_the_same_movie(self) -> None:
        data = b"one complete movie"
        first = match("movie", "movie", data)
        second = replace(first, start_ms=9_000, end_ms=13_000, score=0.70)
        third = replace(first, start_ms=21_000, end_ms=26_000, score=0.68)
        storage = FakeStorage({"scenes": ["opening", "middle", "ending"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (first, second, third),
            object_client=FakeObjects({"movie": data}),
        )

        result = await capability.execute(context())

        self.assertFalse(result.requires_review)
        manifest = json.loads(storage.published[-1].data)
        self.assertEqual(
            [(1_000, 4_000), (9_000, 13_000), (21_000, 26_000)],
            [(item["start_ms"], item["end_ms"]) for item in manifest["assets"]],
        )
        self.assertEqual(1, result.output_summary["selected_assets"])

    async def test_penalizes_repeated_visual_descriptions_across_scenes(self) -> None:
        data = b"one complete movie"
        repeated = match("movie", "movie", data)
        alternative = replace(
            repeated,
            start_ms=120_000,
            end_ms=125_000,
            description="a genuinely different visual",
            score=0.60,
        )
        storage = FakeStorage({"scenes": ["opening", "ending"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda *_: (repeated, alternative),
            object_client=FakeObjects({"movie": data}),
        )

        result = await capability.execute(context())

        self.assertFalse(result.requires_review)
        manifest = json.loads(storage.published[-1].data)
        self.assertEqual(
            [(1_000, 4_000), (120_000, 125_000)],
            [(item["start_ms"], item["end_ms"]) for item in manifest["assets"]],
        )

    async def test_partial_coverage_fails_safely_without_reading_or_publishing(self) -> None:
        data = b"relevant video"
        objects = FakeObjects({"one": data})
        storage = FakeStorage({"scenes": ["covered", "missing"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory"),
            storage,
            search_assets=lambda _workspace, _libraries, query, _limit, _threshold: (
                (match("asset-one", "one", data),) if query == "covered" else ()
            ),
            object_client=objects,
        )

        result = await capability.execute(context())

        self.assertTrue(result.requires_review)
        self.assertEqual(("missing",), result.output_summary["missing_scenes"])
        self.assertEqual((), result.artifacts)
        self.assertEqual([], storage.published)
        self.assertEqual([], objects.reads)

    async def test_rejects_low_and_non_finite_provider_scores(self) -> None:
        data = b"irrelevant video"
        low = match("asset-low", "one", data)
        low = replace(low, score=0.34)
        nan = replace(low, asset_id="asset-nan", score=math.nan)
        storage = FakeStorage({"scenes": ["specific scene"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory", minimum_similarity=0.35),
            storage,
            search_assets=lambda *_: (low, nan),
            object_client=FakeObjects({"one": data}),
        )

        result = await capability.execute(context())

        self.assertTrue(result.requires_review)
        self.assertEqual(("specific scene",), result.output_summary["missing_scenes"])
        self.assertEqual([], storage.published)

    async def test_prefers_relevance_and_does_not_silently_truncate_scenes(self) -> None:
        data = b"video"
        high = match("asset-used", "one", data)
        high = replace(high, score=0.95)
        low = match("asset-unused", "one", data)
        low = replace(low, score=0.36)
        observed_libraries: list[tuple[str, ...]] = []

        def search(_workspace, libraries, query, _limit, _threshold):
            observed_libraries.append(libraries)
            return (high,) if query == "first" else (low, high)

        storage = FakeStorage({"scenes": ["first", "second"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory", maximum_assets=1),
            storage,
            search_assets=search,
            object_client=FakeObjects({"one": data}),
        )

        result = await capability.execute(context())

        self.assertFalse(result.requires_review)
        manifest = json.loads(storage.published[-1].data)
        self.assertEqual(["asset-used", "asset-used"], [item["asset_id"] for item in manifest["assets"]])
        self.assertEqual([("library-one",), ("library-one",)], observed_libraries)

    async def test_maximum_unique_assets_marks_later_uncovered_scene_for_review(self) -> None:
        first = match("asset-one", "one", b"one")
        second = match("asset-two", "two", b"two")
        storage = FakeStorage({"scenes": ["first", "second"]})
        capability = DatabaseAssetCapability(
            AssetLibrarySettings("postgresql://db.example/framefactory", maximum_assets=1),
            storage,
            search_assets=lambda _workspace, _libraries, query, _limit, _threshold: (
                (first,) if query == "first" else (second,)
            ),
            object_client=FakeObjects({"one": b"one", "two": b"two"}),
        )

        result = await capability.execute(context())

        self.assertTrue(result.requires_review)
        self.assertEqual(("second",), result.output_summary["missing_scenes"])
        self.assertEqual([], storage.published)


if __name__ == "__main__":
    unittest.main()
