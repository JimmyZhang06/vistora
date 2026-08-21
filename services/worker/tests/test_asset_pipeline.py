from __future__ import annotations

import unittest
from dataclasses import replace
from threading import Event
from typing import Any

from framefactory.worker.asset_pipeline import (
    PIPELINE_VERSION,
    STAGES,
    AssetAnalysisRunner,
    AssetPipelineError,
    AssetSource,
    AssetStage,
    AssetWork,
    BatchProgress,
    ReviewGate,
    analysis_tags,
    normalized_analysis,
    review_gate,
)


def work(
    item_id: str = "item-1",
    *,
    copyright_status: str = "licensed",
    completed: tuple[str, ...] = (),
    checkpoints: dict[str, Any] | None = None,
) -> AssetWork:
    return AssetWork(
        item_id=item_id,
        batch_id="batch",
        workspace_id="workspace",
        asset_id=f"asset-{item_id}",
        revision=1,
        attempts=1,
        max_attempts=3,
        completed_stages=completed,
        checkpoints=checkpoints or {},
        copyright_status=copyright_status,
        content_hash="a" * 64,
        media_type="video/mp4",
        bucket="bucket",
        object_key=f"assets/{item_id}",
        original_filename="source.mp4",
        sources=(AssetSource("upload", license="commercial-license"),),
    )


class FakeRepository:
    def __init__(self, items: list[AssetWork], *, dry_run: bool = False) -> None:
        self.items = items
        self.dry_run = dry_run
        self.cancelled = False
        self.checkpoints: list[tuple[str, AssetStage]] = []
        self.failures: list[tuple[str, AssetStage, str]] = []
        self.published: list[tuple[str, dict[str, Any], ReviewGate]] = []
        self.cancelled_items: list[str] = []
        self.renewed: list[tuple[str, str, float]] = []

    def is_dry_run(self, batch_id: str) -> bool:
        return self.dry_run

    def cancel_requested(self, batch_id: str) -> bool:
        return self.cancelled

    def claim(self, batch_id: str, worker_id: str, lease_seconds: float) -> AssetWork | None:
        return self.items.pop(0) if self.items else None

    def renew_lease(self, item_id: str, worker_id: str, lease_seconds: float) -> bool:
        self.renewed.append((item_id, worker_id, lease_seconds))
        return True

    def checkpoint(self, item, stage, value, *, worker_id, lease_seconds):
        self.checkpoints.append((item.item_id, stage))
        values = {**item.checkpoints, stage.value: dict(value)}
        completed = (*item.completed_stages, stage.value)
        return replace(item, revision=item.revision + 1, checkpoints=values, completed_stages=completed)

    def mark_cancelled(self, item, *, worker_id):
        self.cancelled_items.append(item.item_id)

    def mark_failure(self, item, stage, error, *, worker_id):
        self.failures.append((item.item_id, stage, error.code))

    def publish_analysis(self, item, analysis, gate, *, worker_id):
        self.published.append((item.item_id, dict(analysis), gate))

    def refresh_batch(self, batch_id: str) -> BatchProgress:
        return BatchProgress(2, 0, 0, len(self.published), 0, len(self.failures), 0)


class FakeProcessor:
    def __init__(self, *, fail_item: str | None = None) -> None:
        self.fail_item = fail_item
        self.calls: list[tuple[str, AssetStage]] = []

    def run(self, stage, item, checkpoints, cancelled):
        self.calls.append((item.item_id, stage))
        if item.item_id == self.fail_item and stage is AssetStage.FFPROBE:
            raise AssetPipelineError("ffprobe_rejected", "bad media", retryable=False)
        if stage is AssetStage.FFPROBE:
            return {"duration_ms": 5000, "width": 1920, "height": 1080}
        if stage is AssetStage.FINGERPRINT:
            return {"sha256": item.content_hash}
        if stage is AssetStage.VISUAL_ANALYSIS:
            return {
                "summary": "A wide historical street scene",
                "people": ["merchant"],
                "locations": ["street"],
                "eras": ["Ming"],
                "scene_types": ["market"],
                "actions": ["walking"],
                "moods": ["busy"],
                "visual_styles": ["documentary"],
                "keywords": ["shops"],
                "has_embedded_text": False,
                "has_watermark": False,
                "safety": {"adult": False, "violence": False},
                "quality": {"usable": True, "score": 0.9},
                "confidence": 0.91,
            }
        if stage is AssetStage.SHOT_SEGMENTATION:
            return {"segments": [{
                "start_ms": 0,
                "end_ms": 5000,
                "description": "merchant walking through market",
                "people": ["merchant"],
                "locations": ["street"],
                "keywords": ["shops"],
                "scene_type": "market",
                "action": "walking",
                "era": "Ming",
                "mood": "busy",
                "visual_style": "documentary",
                "shot_type": "wide",
                "confidence": 0.9,
            }]}
        return {"ok": True}


class AssetAnalysisRunnerTests(unittest.TestCase):
    def test_v3_pipeline_contains_recoverable_temporal_stages(self) -> None:
        self.assertEqual("asset-analysis-v3", PIPELINE_VERSION)
        self.assertIn(AssetStage.PREVIEW, STAGES)
        self.assertIn(AssetStage.SUBTITLE_EXTRACTION, STAGES)
        self.assertIn(AssetStage.AUDIO_TRANSCRIPTION, STAGES)
        self.assertIn(AssetStage.TEMPORAL_ALIGNMENT, STAGES)

    def test_item_failure_is_isolated_and_batch_continues(self) -> None:
        repository = FakeRepository([work("bad"), work("good")])
        processor = FakeProcessor(fail_item="bad")
        runner = AssetAnalysisRunner(
            repository,
            processor,
            "worker",
            rate_limit_per_minute=600,
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
        )

        progress = runner.run_batch("batch")

        self.assertEqual([("bad", AssetStage.FFPROBE, "ffprobe_rejected")], repository.failures)
        self.assertEqual("good", repository.published[0][0])
        self.assertEqual(1, progress.awaiting_review)
        self.assertEqual(1, progress.failed)

    def test_resume_skips_durable_completed_stages(self) -> None:
        completed = tuple(stage.value for stage in STAGES[:4])
        prior = {
            AssetStage.FFPROBE.value: {"duration_ms": 5000},
            AssetStage.FINGERPRINT.value: {"sha256": "a" * 64},
        }
        repository = FakeRepository([work(completed=completed, checkpoints=prior)])
        processor = FakeProcessor()
        runner = AssetAnalysisRunner(repository, processor, "worker", sleep=lambda _: None)

        runner.run_batch("batch")

        self.assertEqual(list(STAGES[4:]), [stage for _, stage in processor.calls])
        self.assertEqual(1, len(repository.published))

    def test_long_blocking_stage_renews_database_item_lease(self) -> None:
        repository = FakeRepository([work()])
        renewed = Event()

        class SlowProcessor(FakeProcessor):
            def run(self, stage, item, checkpoints, cancelled):
                if stage is AssetStage.FILE_DETECTION:
                    renewed.wait(timeout=0.5)
                return super().run(stage, item, checkpoints, cancelled)

        original_renew = repository.renew_lease

        def observe_renew(item_id, worker_id, lease_seconds):
            result = original_renew(item_id, worker_id, lease_seconds)
            renewed.set()
            return result

        repository.renew_lease = observe_renew
        runner = AssetAnalysisRunner(
            repository,
            SlowProcessor(),
            "worker",
            lease_seconds=0.15,
            sleep=lambda _: None,
        )

        runner.run_batch("batch", maximum_items=1)

        self.assertTrue(renewed.is_set())
        self.assertGreaterEqual(len(repository.renewed), 1)

    def test_dry_run_cannot_execute(self) -> None:
        runner = AssetAnalysisRunner(FakeRepository([], dry_run=True), FakeProcessor(), "worker")
        with self.assertRaisesRegex(ValueError, "dry-run"):
            runner.run_batch("batch")

    def test_review_rules_never_auto_clear_unknown_or_risky_content(self) -> None:
        analysis = {
            "confidence": 0.4,
            "has_watermark": True,
            "has_embedded_text": True,
            "safety": {"violence": True},
            "quality": {"usable": False, "score": 0.3},
        }

        gate = review_gate(work(copyright_status="unknown"), analysis)

        self.assertFalse(gate.eligible_for_auto_ready)
        self.assertEqual(
            {
                "copyright_unknown",
                "low_confidence",
                "watermark_detected",
                "embedded_text_detected",
                "content_safety_risk",
                "quality_review_required",
            },
            set(gate.reasons),
        )

    def test_scoreboard_text_does_not_block_otherwise_clean_sports_media(self) -> None:
        analysis = {
            "confidence": 0.95,
            "has_watermark": False,
            "has_embedded_text": True,
            "embedded_text_type": "scoreboard",
            "safety": {"adult": False, "violence": False},
            "quality": {"usable": True, "score": 0.95},
        }

        gate = review_gate(work(copyright_status="licensed"), analysis)

        self.assertTrue(gate.eligible_for_auto_ready)
        self.assertEqual((), gate.reasons)

    def test_music_only_b_roll_does_not_require_a_speech_timeline(self) -> None:
        analysis = {
            "confidence": 0.95,
            "language": "none",
            "people": [],
            "has_watermark": False,
            "has_embedded_text": False,
            "safety": {"adult": False, "violence": False},
            "quality": {"usable": True, "score": 0.95},
            "technical": {"has_audio": True},
            "temporal": {"status": "unavailable"},
        }

        gate = review_gate(work(copyright_status="licensed"), analysis)

        self.assertTrue(gate.eligible_for_auto_ready)
        self.assertNotIn("speech_timeline_missing", gate.reasons)

    def test_narrated_media_still_requires_a_speech_timeline(self) -> None:
        analysis = {
            "confidence": 0.95,
            "language": "zh-CN",
            "people": ["讲解者"],
            "has_watermark": False,
            "has_embedded_text": False,
            "safety": {"adult": False, "violence": False},
            "quality": {"usable": True, "score": 0.95},
            "technical": {"has_audio": True},
            "temporal": {"status": "unavailable"},
        }

        gate = review_gate(work(copyright_status="licensed"), analysis)

        self.assertFalse(gate.eligible_for_auto_ready)
        self.assertIn("speech_timeline_missing", gate.reasons)

    def test_normalization_and_tags_cover_all_required_dimensions(self) -> None:
        processor = FakeProcessor()
        item = work()
        checkpoints: dict[str, Any] = {}
        for stage in STAGES:
            checkpoints[stage.value] = processor.run(stage, item, checkpoints, lambda: False)

        analysis = normalized_analysis(checkpoints)
        tags = set(analysis_tags(analysis))

        self.assertIn("人物:merchant", tags)
        self.assertIn("地点:street", tags)
        self.assertIn("时代:Ming", tags)
        self.assertIn("场景:market", tags)
        self.assertIn("动作:walking", tags)
        self.assertIn("情绪:busy", tags)
        self.assertIn("视觉风格:documentary", tags)
        self.assertIn("景别:wide", tags)
        self.assertIn("嵌字:无", tags)
        self.assertIn("水印:无", tags)
        self.assertIn("安全:通过", tags)
        self.assertIn("质量:可用", tags)

    def test_long_form_normalization_keeps_segments_through_asset_end(self) -> None:
        duration_ms = 7_000_000
        segments = [
            {
                "start_ms": ordinal * 14_000,
                "end_ms": min(duration_ms, (ordinal + 1) * 14_000),
                "description": f"segment {ordinal}",
                "cut_safe": True,
            }
            for ordinal in range(500)
        ]

        analysis = normalized_analysis({
            AssetStage.FFPROBE.value: {"duration_ms": duration_ms},
            AssetStage.VISUAL_ANALYSIS.value: {"summary": "long-form source"},
            AssetStage.SHOT_SEGMENTATION.value: {"segments": segments},
        })

        self.assertEqual(500, len(analysis["segments"]))
        self.assertEqual(duration_ms, analysis["segments"][-1]["end_ms"])


if __name__ == "__main__":
    unittest.main()
