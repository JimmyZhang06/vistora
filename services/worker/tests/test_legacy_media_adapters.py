from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from framefactory.runtime import PermanentStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.legacy_media import (
    _VIDEO_SUFFIXES,
    EdgeSpeechCapability,
    FFmpegQualityCapability,
    FFmpegRenderCapability,
    LegacyAssetCapability,
    _build_ass,
    _media_filter,
    _render_profile,
    _segment_command,
)
from framefactory.worker.config import LegacyMediaSettings, WorkerSettings
from framefactory.worker.providers import ProviderArtifact
from framefactory.worker.timeline import plan_edit_timeline


class MemoryStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.materialized: list[str] = []

    def publish(self, context: StepContext, artifact: ProviderArtifact) -> ArtifactRef:
        artifact_id = str(uuid4())
        self.values[artifact_id] = artifact.data
        return ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=str(uuid4()),
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"test/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=hashlib.sha256(artifact.data).hexdigest(),
            filename=artifact.filename,
        )

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        return self.values[artifact.id]

    def read_json(self, artifact: ArtifactRef) -> dict[str, Any]:
        return json.loads(self.read_bytes(artifact))

    def materialize(self, artifact: ArtifactRef, destination: Path) -> None:
        self.materialized.append(artifact.id)
        destination.write_bytes(self.values[artifact.id])


def context(*artifacts: ArtifactRef, input_snapshot: dict[str, Any] | None = None) -> StepContext:
    return StepContext(
        workspace_id="11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        step_id="run:media",
        input_snapshot=input_snapshot or {},
        input_artifacts=artifacts,
    )


class LegacyMediaAdapterTests(unittest.TestCase):
    def test_matroska_sources_are_normalized_by_the_render_pipeline(self) -> None:
        self.assertIn(".mkv", _VIDEO_SUFFIXES)

    def test_ass_uses_redistributable_noto_cjk_font(self) -> None:
        document = _build_ass(
            {"narration": "中文字幕。"},
            3.0,
            width=1920,
            height=1080,
            layout="full_frame",
            subtitle_enabled=True,
            subtitle_position="bottom",
            subtitle_size="medium",
            max_lines=2,
        )
        self.assertIn("Noto Sans CJK SC", document)
        self.assertNotIn("Microsoft YaHei", document)

    def test_ass_uses_native_narration_word_boundaries(self) -> None:
        narration = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
        words = [
            {
                "text": character,
                "start_seconds": float(index),
                "end_seconds": float(index + 1),
                "estimated": False,
            }
            for index, character in enumerate(narration)
        ]
        document = _build_ass(
            {"narration": narration},
            20.0,
            timing={"words": words, "estimated": False},
            width=320,
            height=180,
            subtitle_enabled=True,
            max_lines=1,
        )
        dialogue = [
            line for line in document.splitlines() if ",Default," in line
        ]
        self.assertGreater(len(dialogue), 1)
        self.assertIn("0:00:08.00", dialogue[1])

    def test_edge_tts_publishes_real_provider_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps(
                        {"title": "Title", "narration": "真实配音文本", "scenes": []}
                    ).encode(),
                ),
            )

            async def synthesize(text: str, output: Path, voice: str, rate: str) -> None:
                self.assertEqual("真实配音文本", text)
                self.assertEqual("zh-CN-YunjianNeural", voice)
                self.assertEqual("+25%", rate)
                output.write_bytes(b"ID3" + b"a" * 2048)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return 2.1

            result = asyncio.run(
                EdgeSpeechCapability(
                    settings,
                    storage,
                    synthesizer=synthesize,
                    duration_probe=duration_probe,
                ).execute(context(script))
            )
            self.assertEqual("audio", result.artifacts[0].kind)
            self.assertEqual("edge-tts", result.summary_dict()["provider"])
            self.assertEqual(2.1, result.summary_dict()["duration_seconds"])

    def test_webpage_tts_missing_native_words_fails_instead_of_awaiting_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps({"narration": "网页旁白", "scenes": ["唯一画面"]}).encode(),
                ),
            )

            async def synthesize(
                _text: str, output: Path, _voice: str, _rate: str
            ) -> None:
                output.write_bytes(b"ID3" + b"a" * 2048)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return 15.0

            snapshot = {
                "webpage_video_run_id": "33333333-3333-4333-8333-333333333333",
                "duration_seconds": 15,
            }
            with self.assertRaisesRegex(PermanentStepError, "native WordBoundary"):
                asyncio.run(
                    EdgeSpeechCapability(
                        settings,
                        storage,
                        synthesizer=synthesize,
                        duration_probe=duration_probe,
                    ).execute(context(script, input_snapshot=snapshot))
                )
            self.assertEqual(1, len(storage.values), "failed webpage TTS must publish no audio")

    def test_webpage_tts_duration_mismatch_fails_instead_of_awaiting_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps({"narration": "网页旁白", "scenes": ["唯一画面"]}).encode(),
                ),
            )

            async def synthesize(
                text: str, output: Path, _voice: str, _rate: str
            ) -> tuple[dict[str, object], ...]:
                output.write_bytes(b"ID3" + b"a" * 2048)
                return ({"text": text, "start_seconds": 0.0, "end_seconds": 100.0},)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return 100.0

            snapshot = {
                "webpage_video_run_id": "33333333-3333-4333-8333-333333333333",
                "duration_seconds": 15,
            }
            with self.assertRaisesRegex(PermanentStepError, "requested duration"):
                asyncio.run(
                    EdgeSpeechCapability(
                        settings,
                        storage,
                        synthesizer=synthesize,
                        duration_probe=duration_probe,
                    ).execute(context(script, input_snapshot=snapshot))
                )
            self.assertEqual(1, len(storage.values), "failed webpage TTS must publish no audio")

    def test_webpage_tts_accepts_native_words_with_proportional_sentence_alignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps(
                        {"narration": "第一段。第二段。", "scenes": ["甲", "乙"]},
                        ensure_ascii=False,
                    ).encode(),
                ),
            )

            async def synthesize(
                _text: str, output: Path, _voice: str, _rate: str
            ) -> tuple[dict[str, object], ...]:
                output.write_bytes(b"ID3" + b"a" * 2048)
                # Provider tokens intentionally omit authored punctuation, so
                # sentence grouping cannot use exact token aggregation.
                return (
                    {"text": "第一", "start_seconds": 0.0, "end_seconds": 6.5},
                    {"text": "第二段", "start_seconds": 7.0, "end_seconds": 14.5},
                )

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return 15.0

            snapshot = {
                "webpage_video_run_id": "33333333-3333-4333-8333-333333333333",
                "duration_seconds": 15,
            }
            result = asyncio.run(
                EdgeSpeechCapability(
                    settings,
                    storage,
                    synthesizer=synthesize,
                    duration_probe=duration_probe,
                ).execute(context(script, input_snapshot=snapshot))
            )
            timing = storage.read_json(result.artifacts[1])

            self.assertFalse(result.requires_review)
            self.assertTrue(timing["estimated"])
            self.assertFalse(timing["word_timing_estimated"])
            self.assertEqual(
                "word_boundary_proportional_estimate",
                timing["segments"][0]["alignment_source"],
            )
            self.assertTrue(all(not word["estimated"] for word in timing["words"]))

    def test_edge_tts_adjusts_rate_once_against_run_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,), tts_rate="+25%")
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps({"narration": "需要匹配时长的旁白", "scenes": []}).encode(),
                ),
            )
            rates: list[str] = []
            durations = iter((60.0, 92.0))

            async def synthesize(_text: str, output: Path, _voice: str, rate: str) -> None:
                rates.append(rate)
                output.write_bytes(b"ID3" + b"a" * 2048)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return next(durations)

            snapshot = {
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"target_duration_seconds": 90}
                    }
                }
            }
            result = asyncio.run(
                EdgeSpeechCapability(
                    settings,
                    storage,
                    synthesizer=synthesize,
                    duration_probe=duration_probe,
                ).execute(context(script, input_snapshot=snapshot))
            )

            self.assertEqual(["+25%", "-17%"], rates)
            self.assertTrue(result.summary_dict()["duration_fit"])
            self.assertTrue(result.requires_review)
            self.assertTrue(result.summary_dict()["timing_estimated"])

    def test_edge_tts_uses_measured_feedback_when_first_rate_correction_misses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,), tts_rate="+25%")
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps({"narration": "需要迭代匹配时长的旁白", "scenes": []}).encode(),
                ),
            )
            rates: list[str] = []
            durations = iter((37.5, 34.56, 44.5))

            async def synthesize(_text: str, output: Path, _voice: str, rate: str) -> None:
                rates.append(rate)
                output.write_bytes(b"ID3" + b"a" * 2048)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return next(durations)

            snapshot = {
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"target_duration_seconds": 45}
                    }
                }
            }
            result = asyncio.run(
                EdgeSpeechCapability(
                    settings,
                    storage,
                    synthesizer=synthesize,
                    duration_probe=duration_probe,
                ).execute(context(script, input_snapshot=snapshot))
            )

            self.assertEqual(["+25%", "+4%", "-20%"], rates)
            self.assertTrue(result.summary_dict()["duration_fit"])
            self.assertEqual(3, result.summary_dict()["synthesis_attempts"])

    def test_edge_tts_retries_a_transient_provider_failure_within_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps({"narration": "需要可靠重试的旁白", "scenes": []}).encode(),
                ),
            )
            attempts = 0

            async def synthesize(
                _text: str, output: Path, _voice: str, _rate: str
            ) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TimeoutError("temporary provider throttle")
                output.write_bytes(b"ID3" + b"a" * 2048)

            async def duration_probe(_path: Path, _context: StepContext) -> float:
                return 4.0

            result = asyncio.run(
                EdgeSpeechCapability(
                    settings,
                    storage,
                    synthesizer=synthesize,
                    duration_probe=duration_probe,
                ).execute(context(script))
            )

            self.assertEqual(2, attempts)
            self.assertEqual("audio", result.artifacts[0].kind)

    def test_timeline_uses_narration_semantics_and_variable_shot_lengths(self) -> None:
        timeline = plan_edit_timeline(
            {
                "narration": "城市在夜色中苏醒。随后镜头转向海边，海浪拍打礁石很久很久。"
            },
            [
                {
                    "artifact_id": "city",
                    "description": "城市夜景与街道",
                    "labels": ["城市", "夜景"],
                    "cut_safe": True,
                },
                {
                    "artifact_id": "sea",
                    "description": "海边礁石和海浪",
                    "labels": ["海边", "海浪"],
                    "semantic_complete": True,
                },
            ],
            18.0,
        )

        self.assertAlmostEqual(18.0, sum(item["duration_seconds"] for item in timeline), 2)
        self.assertGreater(len({item["duration_seconds"] for item in timeline}), 1)
        self.assertEqual("city", timeline[0]["artifact_id"])
        self.assertTrue(any(item["artifact_id"] == "sea" for item in timeline[1:]))
        self.assertTrue(all(item["duration_seconds"] <= 8.0 for item in timeline))

    def test_timeline_keeps_sentences_inside_authored_storyboard_scenes(self) -> None:
        flare_scene = "太阳表面耀斑爆发"
        cme_scene = "日冕物质抛射并向外扩散"
        satellite_scene = "卫星观测粒子抵达地球"
        aurora_scene = "夜空出现极光"
        timeline = plan_edit_timeline(
            {
                "narration": (
                    "太阳表面出现耀斑。太阳黑子附近磁场重连。"
                    "等离子体被抛入太空。抛射云继续向外扩散。"
                    "卫星持续观测。粒子抵达地球。磁层发生扰动。"
                    "高层大气开始发光。最终夜空出现极光。"
                ),
                "scenes": [flare_scene, cme_scene, satellite_scene, aurora_scene],
            },
            [
                {
                    "artifact_id": "flare",
                    "selected_for_scene": flare_scene,
                    "description": "太阳表面耀斑",
                    "semantic_complete": True,
                },
                {
                    "artifact_id": "cme",
                    "selected_for_scene": cme_scene,
                    "description": "等离子体抛射云",
                    "semantic_complete": True,
                },
                {
                    "artifact_id": "satellite",
                    "selected_for_scene": satellite_scene,
                    "description": "卫星绕地球飞行",
                    "semantic_complete": True,
                },
                {
                    "artifact_id": "aurora",
                    "selected_for_scene": aurora_scene,
                    "description": "夜空绿色极光",
                    "semantic_complete": True,
                },
            ],
            36.0,
        )

        self.assertEqual(
            [
                "flare",
                "flare",
                "flare",
                "cme",
                "cme",
                "satellite",
                "satellite",
                "satellite",
                "aurora",
                "aurora",
            ],
            [shot["artifact_id"] for shot in timeline],
        )

    def test_timeline_advances_on_semantic_beat_before_equal_sentence_partition(self) -> None:
        flare_scene = "太阳表面耀斑爆发"
        cme_scene = "日冕等离子体抛射云向外扩散"
        satellite_scene = "卫星观测粒子抵达地球"
        aurora_scene = "夜空出现绿色极光"
        timeline = plan_edit_timeline(
            {
                "narration": (
                    "太阳表面突然爆发耀斑。"
                    "能量释放伴随日冕物质抛射，大量等离子体进入太空。"
                    "粒子流继续向外扩散。"
                    "卫星开始追踪它的轨迹。"
                    "粒子抵达地球磁层。"
                    "高能粒子进入两极大气并形成极光。"
                    "绿色光带在夜空中舞动。"
                ),
                "scenes": [flare_scene, cme_scene, satellite_scene, aurora_scene],
            },
            [
                {"artifact_id": "flare", "selected_for_scene": flare_scene},
                {"artifact_id": "cme", "selected_for_scene": cme_scene},
                {"artifact_id": "satellite", "selected_for_scene": satellite_scene},
                {"artifact_id": "aurora", "selected_for_scene": aurora_scene},
            ],
            35.0,
        )

        by_sentence = {
            shot["narration"]: shot["artifact_id"]
            for shot in timeline
        }
        self.assertEqual(
            "cme",
            by_sentence["能量释放伴随日冕物质抛射，大量等离子体进入太空。"],
        )
        self.assertEqual("satellite", by_sentence["卫星开始追踪它的轨迹。"])
        self.assertEqual("aurora", by_sentence["绿色光带在夜空中舞动。"])

    def test_timeline_uses_selector_recovered_storyboard_beat(self) -> None:
        flare_scene = "太阳表面耀斑爆发"
        satellite_scene = "卫星观测粒子抵达地球"
        magnetic_scene = "地球磁层被压缩并产生地磁扰动"
        aurora_scene = "夜空出现极光"
        timeline = plan_edit_timeline(
            {
                "narration": (
                    "太阳表面突然爆发耀斑。卫星持续观测。"
                    "当CME抵达地球，其磁场与地球磁场相互作用，引发地磁暴。"
                    "物质云抵达后，地球磁层被压缩并产生地磁扰动。"
                    "最终夜空出现极光。"
                ),
                # The writing model omitted the magnetic beat here.
                "scenes": [flare_scene, satellite_scene, aurora_scene],
            },
            [
                {"artifact_id": "flare", "selected_for_scene": flare_scene},
                {"artifact_id": "satellite", "selected_for_scene": satellite_scene},
                {
                    "artifact_id": "magnetic",
                    "selected_for_scene": magnetic_scene,
                    "selected_for_narration": (
                        "当CME抵达地球，其磁场与地球磁场相互作用，引发地磁暴。"
                        "物质云抵达后，地球磁层被压缩并产生地磁扰动。"
                    ),
                },
                {"artifact_id": "aurora", "selected_for_scene": aurora_scene},
            ],
            20.0,
        )

        by_sentence = {shot["narration"]: shot["artifact_id"] for shot in timeline}
        self.assertEqual(
            "magnetic",
            by_sentence["当CME抵达地球，其磁场与地球磁场相互作用，引发地磁暴。"],
        )
        self.assertEqual(
            "magnetic",
            by_sentence["物质云抵达后，地球磁层被压缩并产生地磁扰动。"],
        )

    def test_timeline_never_reads_past_an_ingested_virtual_clip(self) -> None:
        timeline = plan_edit_timeline(
            {"narration": "一段需要更长画面的旁白。"},
            [
                {
                    "asset_id": "movie",
                    "artifact_id": "source",
                    "description": "建筑教室中的对话",
                    "start_ms": 12_000,
                    "end_ms": 14_500,
                    "cut_safe": True,
                }
            ],
            5.0,
        )

        self.assertEqual(12.0, timeline[0]["source_start_seconds"])
        self.assertEqual(14.5, timeline[0]["source_end_seconds"])
        self.assertEqual(2.5, timeline[0]["source_duration_seconds"])
        self.assertEqual(0.0, timeline[0]["padding_seconds"])
        self.assertEqual(2, len(timeline))
        self.assertEqual(0.0, sum(shot["padding_seconds"] for shot in timeline))

    def test_timeline_plans_a_coherent_film_story_path(self) -> None:
        timeline = plan_edit_timeline(
            {
                "narration": (
                    "大学校园里，他们第一次相遇。"
                    "因为误会，两人在门口争执后分开。"
                    "多年后，他成为设计师，在办公室重新看见那张图纸。"
                    "最终，他们回到老屋完成改造。"
                )
            },
            [
                {
                    "asset_id": "film",
                    "artifact_id": "campus",
                    "description": "大学校园里年轻学生初次相遇",
                    "labels": ["校园", "学生", "年轻"],
                    "start_ms": 10_000,
                    "end_ms": 18_000,
                    "cut_safe": True,
                },
                {
                    "asset_id": "film",
                    "artifact_id": "door",
                    "description": "门口争执后独自离开",
                    "labels": ["冲突", "门口", "离开"],
                    "start_ms": 60_000,
                    "end_ms": 68_000,
                    "semantic_complete": True,
                },
                {
                    "asset_id": "film",
                    "artifact_id": "office",
                    "description": "多年后成年设计师在办公室看图纸",
                    "labels": ["成年", "办公室", "工作"],
                    "start_ms": 120_000,
                    "end_ms": 128_000,
                    "semantic_complete": True,
                },
                {
                    "asset_id": "film",
                    "artifact_id": "home",
                    "description": "最终两人在老屋重逢并完成住宅改造",
                    "labels": ["住宅", "重逢", "建筑"],
                    "start_ms": 180_000,
                    "end_ms": 188_000,
                    "semantic_complete": True,
                },
            ],
            28.0,
        )

        sequence = [item["artifact_id"] for item in timeline]
        self.assertEqual("campus", sequence[0])
        self.assertLess(sequence.index("door"), sequence.index("office"))
        self.assertEqual("home", sequence[-1])
        self.assertEqual("time_forward", timeline[-1]["transition"])
        self.assertTrue(all(item["cut_evidence"] != "candidate" for item in timeline))

    def test_timeline_holds_relevant_scene_instead_of_cutting_to_unrelated_asset(self) -> None:
        timeline = plan_edit_timeline(
            {"narration": "海边的风吹过礁石，海浪一次次涌来，持续了很久很久。"},
            [
                {
                    "artifact_id": "city",
                    "description": "城市道路与办公楼",
                    "labels": ["城市", "街道"],
                    "cut_safe": True,
                },
                {
                    "artifact_id": "sea",
                    "description": "海边礁石和海浪",
                    "labels": ["海边", "海浪"],
                    "semantic_complete": True,
                },
            ],
            18.0,
        )

        self.assertEqual({"sea"}, {item["artifact_id"] for item in timeline})

    def test_timeline_keeps_slow_zoom_continuous_across_internal_holds(self) -> None:
        timeline = plan_edit_timeline(
            {"narration": "一段需要持续展示并缓慢放大的网页重点区域。"},
            [
                {
                    "artifact_id": "web-region",
                    "selected_for_scene": "shot-01",
                    "selected_for_narration": "一段需要持续展示并缓慢放大的网页重点区域。",
                    "motion": "zoom_in",
                    "motion_focus": {"x": 0.3, "y": 0.6},
                    "transition": "fade_black",
                }
            ],
            10.0,
        )

        self.assertEqual(1, len(timeline))
        self.assertEqual(10.0, timeline[0]["duration_seconds"])
        self.assertEqual("zoom_in", timeline[0]["motion"])
        self.assertEqual({"x": 0.3, "y": 0.6}, timeline[0]["motion_focus"])
        self.assertEqual("opening", timeline[0]["transition"])

    def test_timeline_applies_authored_transition_only_when_asset_changes(self) -> None:
        timeline = plan_edit_timeline(
            {"narration": "先看产品全景。接着聚焦核心功能。"},
            [
                {
                    "artifact_id": "overview",
                    "selected_for_scene": "shot-01",
                    "selected_for_narration": "先看产品全景。",
                    "description": "产品全景",
                    "transition": "cut",
                },
                {
                    "artifact_id": "feature",
                    "selected_for_scene": "shot-02",
                    "selected_for_narration": "接着聚焦核心功能。",
                    "description": "核心功能",
                    "transition": "fade_black",
                },
            ],
            8.0,
        )

        self.assertEqual(["overview", "feature"], [item["artifact_id"] for item in timeline])
        self.assertEqual(["opening", "fade_black"], [item["transition"] for item in timeline])

    def test_catalog_selection_publishes_assets_and_truthful_rights_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "clips").mkdir()
            (root / "clips" / "reform.mp4").write_bytes(b"video-one")
            (root / "clips" / "city.mp4").write_bytes(b"video-two")
            catalog = root / "index.json"
            catalog.write_text(
                json.dumps(
                    [
                        {
                            "file": "clips/reform.mp4",
                            "scene": "改革会议",
                            "style": "真人实拍",
                            "has_text": "无",
                        },
                        {
                            "file": "clips/city.mp4",
                            "scene": "城市",
                            "style": "纪录片实景",
                            "has_text": "角落水印",
                        },
                        {"file": "../escape.mp4", "has_text": "无"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = LegacyMediaSettings(root, (catalog,))
            storage = MemoryStorage()
            seed = context()
            script = storage.publish(
                seed,
                ProviderArtifact(
                    "script",
                    "script.json",
                    "application/json",
                    json.dumps(
                        {
                            "title": "改革",
                            "narration": "一次改革如何改变城市",
                            "scenes": ["改革会议", "城市"],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
            )
            result = asyncio.run(LegacyAssetCapability(settings, storage).execute(context(script)))
            self.assertTrue(result.requires_review)
            self.assertEqual(2, result.summary_dict()["selected_assets"])
            self.assertEqual(["asset", "asset", "manifest"], [a.kind for a in result.artifacts])
            manifest = storage.read_json(result.artifacts[-1])
            self.assertEqual("review_required", manifest["rights_status"])
            self.assertNotIn(str(root), json.dumps(manifest))

    def test_worker_settings_require_explicit_catalog_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "index.json"
            catalog.write_text("[]", encoding="utf-8")
            environment = {
                "FRAMEFACTORY_ENV": "test",
                "FRAMEFACTORY_DATABASE_URL": "postgresql://user:pass@db/framefactory",
                "FRAMEFACTORY_REDIS_URL": "redis://redis/0",
                "FRAMEFACTORY_WORKER_ID": "worker-test",
                "FRAMEFACTORY_LEGACY_MEDIA_ENABLED": "true",
                "FRAMEFACTORY_LEGACY_ASSET_ROOT": str(root),
                "FRAMEFACTORY_LEGACY_ASSET_CATALOGS": str(catalog),
            }
            with self.assertRaisesRegex(ValueError, "S3_BUCKET"):
                WorkerSettings.from_environment(environment)
            configured = WorkerSettings.from_environment(
                {**environment, "FRAMEFACTORY_S3_BUCKET": "artifacts"}
            )
            self.assertEqual((catalog.resolve(),), configured.legacy_media.catalog_paths)

    def test_subtitle_builder_includes_ai_label_and_escapes_ass_control_text(self) -> None:
        value = _build_ass(
            {"narration": "第一句。第二{句}。"},
            4.0,
            width=360,
            height=640,
        )
        self.assertIn("内容由AI生成", value)
        self.assertIn("第二（句）。", value)
        self.assertNotIn("第二{句}", value)

    def test_subtitle_builder_hard_wraps_long_cjk_and_ascii_tokens(self) -> None:
        value = _build_ass(
            {
                "title": "通用版式",
                "narration": (
                    "这是一条很长的中文字幕用于核验安全断行和链接"
                    "https://example.com/research/very-long-reference-2026。"
                ),
            },
            8.0,
            width=1080,
            height=1920,
            layout="editorial",
            max_lines=2,
        )
        dialogue = [line for line in value.splitlines() if ",Default," in line]
        self.assertGreater(len(dialogue), 1)
        self.assertTrue(all(line.count("\\N") <= 1 for line in dialogue))
        self.assertIn(",Title,", value)

    def test_render_profile_uses_immutable_run_video_settings(self) -> None:
        fallback = LegacyMediaSettings(width=1080, height=1920, frame_rate=30)
        profile = _render_profile(
            {
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {
                            "resolution": {"width": 1920, "height": 1080},
                            "frame_rate": 25,
                            "layout": "editorial",
                            "media_fit": "contain",
                            "subtitles": {
                                "enabled": True,
                                "position": "lower_third",
                                "size": "large",
                                "max_lines": 3,
                            },
                        }
                    }
                }
            },
            fallback,
        )
        self.assertEqual((1920, 1080, 25), (profile.width, profile.height, profile.frame_rate))
        self.assertEqual("editorial", profile.layout)
        self.assertEqual("contain", profile.media_fit)
        self.assertEqual(3, profile.subtitle_max_lines)

    def test_webpage_render_profile_preserves_page_over_blurred_fill(self) -> None:
        fallback = LegacyMediaSettings(width=1080, height=1920, frame_rate=30)
        profile = _render_profile(
            {
                "webpage_video_run_id": "web-run-01",
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"media_fit": "contain"}
                    }
                },
            },
            fallback,
        )

        self.assertEqual("blurred_contain", profile.media_fit)

    def test_media_filter_supports_generic_layouts_and_fit_modes(self) -> None:
        cover = _media_filter(
            1080, 1920, 30, layout="full_frame", media_fit="cover", background_color="101218"
        )
        contain = _media_filter(
            1080, 1080, 30, layout="editorial", media_fit="contain", background_color="101218"
        )
        blurred = _media_filter(
            1080,
            1920,
            30,
            layout="full_frame",
            media_fit="blurred_contain",
            background_color="101218",
        )
        self.assertIn("crop=1080:1920", cover)
        self.assertIn("force_original_aspect_ratio=decrease", contain)
        self.assertIn("pad=1080:1080", contain)
        self.assertIn("split=2[bg][fg]", blurred)
        self.assertIn("gblur=sigma=28", blurred)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", blurred)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_image_segment_applies_continuous_slow_zoom_toward_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            output = root / "zoom.mp4"
            generated = subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=320x180",
                    "-frames:v",
                    "1",
                    "-y",
                    str(source),
                ),
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, generated.returncode, generated.stderr.decode(errors="replace"))
            command = _segment_command(
                "ffmpeg",
                source,
                output,
                1.2,
                width=320,
                height=180,
                frame_rate=24,
                media_fit="blurred_contain",
                image_motion="zoom_in",
                motion_focus={"x": 0.75, "y": 0.4},
            )
            video_filter = command[command.index("-vf") + 1]
            self.assertIn("zoompan=", video_filter)
            self.assertIn("gblur=sigma=28", video_filter)
            self.assertIn("cos(PI*", video_filter)
            self.assertIn("0.750000*iw", video_filter)
            rendered = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, rendered.returncode, rendered.stderr.decode(errors="replace"))
            self.assertGreater(output.stat().st_size, 2_000)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required")
    def test_image_segment_renders_zoom_out_pan_and_fade_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            generated = subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=320x180",
                    "-frames:v",
                    "1",
                    "-y",
                    str(source),
                ),
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, generated.returncode, generated.stderr.decode(errors="replace"))
            for motion in ("zoom_out", "pan"):
                output = root / f"{motion}.mp4"
                command = _segment_command(
                    "ffmpeg",
                    source,
                    output,
                    1.2,
                    width=320,
                    height=180,
                    frame_rate=24,
                    media_fit="blurred_contain",
                    image_motion=motion,
                    motion_focus={"x": 0.2, "y": 0.7},
                    fade_in=True,
                    fade_out=True,
                )
                video_filter = command[command.index("-vf") + 1]
                self.assertIn("zoompan=", video_filter)
                self.assertIn("cos(PI*", video_filter)
                self.assertIn("fade=t=in", video_filter)
                self.assertIn("fade=t=out", video_filter)
                if motion == "zoom_out":
                    self.assertIn("1.10-0.10", video_filter)
                else:
                    self.assertIn("z='1.08'", video_filter)
                rendered = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(
                    0,
                    rendered.returncode,
                    rendered.stderr.decode(errors="replace"),
                )
                self.assertGreater(output.stat().st_size, 2_000)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_renderer_executes_variable_timeline_and_publishes_edit_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "narration.mp3"
            sources = [root / "city.mp4", root / "sea.mp4"]
            commands = [
                (
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=16000", "-t", "7.3", "-y",
                    str(audio_path),
                ),
                *[
                    (
                        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s=320x180:r=24", "-t", "6", "-c:v",
                        "libx264", "-pix_fmt", "yuv420p", "-y", str(path),
                    )
                    for color, path in zip(("red", "teal"), sources, strict=True)
                ],
            ]
            for command in commands:
                completed = subprocess.run(command, capture_output=True, check=False, timeout=60)
                self.assertEqual(0, completed.returncode, completed.stderr.decode(errors="replace"))

            storage = MemoryStorage()
            seed = context()
            audio = storage.publish(
                seed,
                ProviderArtifact("audio", "narration.mp3", "audio/mpeg", audio_path.read_bytes()),
            )
            assets = [
                storage.publish(
                    seed,
                    ProviderArtifact("asset", path.name, "video/mp4", path.read_bytes()),
                )
                for path in sources
            ]
            manifest = storage.publish(
                seed,
                ProviderArtifact(
                    "manifest",
                    "asset-manifest.json",
                    "application/json",
                    json.dumps(
                        {
                            "script": {
                                "title": "城市与海",
                                "narration": "城市在清晨苏醒。海浪随后拍打礁石，故事继续向前。",
                            },
                            "assets": [
                                {
                                    "artifact_id": assets[0].id,
                                    "description": "清晨城市",
                                    "labels": ["城市", "清晨"],
                                    "cut_safe": True,
                                },
                                {
                                    "artifact_id": assets[1].id,
                                    "description": "海浪礁石",
                                    "labels": ["海浪", "礁石"],
                                    "semantic_complete": True,
                                },
                                {
                                    "artifact_id": assets[0].id,
                                    "description": "城市远景",
                                    "labels": ["城市", "远景"],
                                    "cut_safe": True,
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
            )
            settings = LegacyMediaSettings(
                width=320,
                height=180,
                frame_rate=24,
                ffmpeg_command="ffmpeg",
                ffprobe_command="ffprobe",
            )
            result = asyncio.run(
                FFmpegRenderCapability(settings, storage).execute(
                    context(audio, *assets, manifest)
                )
            )

            self.assertEqual(["video", "timeline"], [item.kind for item in result.artifacts])
            self.assertEqual(Counter({assets[0].id: 1, assets[1].id: 1}), Counter(storage.materialized))
            timeline = storage.read_json(result.artifacts[1])
            self.assertGreaterEqual(len(timeline["shots"]), len(assets))
            self.assertGreater(len({shot["duration_seconds"] for shot in timeline["shots"]}), 1)
            self.assertGreater(len(storage.read_bytes(result.artifacts[0])), 10_000)
            quality = asyncio.run(
                FFmpegQualityCapability(settings, storage).execute(
                    context(
                        result.artifacts[0],
                        input_snapshot={
                            "_framefactory": {
                                "composition_snapshot": {
                                    "production_settings": {
                                        "resolution": {"width": 320, "height": 180},
                                        "frame_rate": 24,
                                    }
                                }
                            }
                        },
                    )
                )
            )
            self.assertFalse(quality.requires_review)
            self.assertEqual("pass", quality.output_summary["verdict"])
            self.assertEqual("decoded_audio_video", quality.output_summary["scope"])

    @unittest.skipUnless(
        os.getenv("FRAMEFACTORY_RUN_LEGACY_MEDIA_INTEGRATION") == "1",
        "set FRAMEFACTORY_RUN_LEGACY_MEDIA_INTEGRATION=1 for live Edge/FFmpeg coverage",
    )
    def test_live_edge_catalog_and_ffmpeg_round_trip(self) -> None:
        root = Path(os.environ["FRAMEFACTORY_LEGACY_ASSET_ROOT"])
        catalog = Path(os.environ["FRAMEFACTORY_LEGACY_ASSET_CATALOGS"])
        settings = LegacyMediaSettings(
            root,
            (catalog,),
            width=360,
            height=640,
            maximum_assets=1,
        )
        storage = MemoryStorage()
        seed = context()
        script = storage.publish(
            seed,
            ProviderArtifact(
                "script",
                "script.json",
                "application/json",
                json.dumps(
                    {
                        "title": "迁移适配器验证",
                        "narration": "这是一段真实配音和视频渲染测试。",
                        "scenes": ["朝堂"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            ),
        )
        audio = asyncio.run(EdgeSpeechCapability(settings, storage).execute(context(script)))
        assets = asyncio.run(LegacyAssetCapability(settings, storage).execute(context(script)))
        video = asyncio.run(
            FFmpegRenderCapability(settings, storage).execute(
                context(audio.artifacts[0], *assets.artifacts)
            )
        )
        payload = storage.read_bytes(video.artifacts[0])
        self.assertGreater(len(payload), 10_000)
        self.assertEqual(b"ftyp", payload[4:8])


if __name__ == "__main__":
    unittest.main()
