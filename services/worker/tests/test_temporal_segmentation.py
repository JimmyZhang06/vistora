from __future__ import annotations

import unittest

from framefactory.worker.adapters.asset_analysis import (
    _media_types_compatible,
    _representative_frame_count,
    _representative_timestamps,
    _signature_media_type,
    align_temporal_sources,
    parse_webvtt,
    semantic_cut_boundaries,
)


class TemporalSegmentationTests(unittest.TestCase):
    def test_matroska_signature_is_not_mislabeled_as_webm(self) -> None:
        header = b"\x1aE\xdf\xa3" + b"\x00" * 16 + b"matroska" + b"\x00" * 32

        self.assertEqual("video/x-matroska", _signature_media_type(header))

    def test_bounded_ebml_signature_accepts_matroska_browser_mime(self) -> None:
        self.assertTrue(_media_types_compatible("video/webm", "video/x-matroska"))
        self.assertTrue(_media_types_compatible("video/webm", "video/matroska"))
        self.assertFalse(_media_types_compatible("video/webm", "video/mp4"))

    def test_webvtt_parser_preserves_safe_timestamps_and_plain_text(self) -> None:
        cues = parse_webvtt(
            """WEBVTT

00:00:01.000 --> 00:00:03.400
<c.yellow>第一句话讲完整。</c>

2
00:00:03.600 --> 00:00:06.000 position:50%
第二句话也不能从中间切断。
"""
        )

        self.assertEqual(2, len(cues))
        self.assertEqual((1000, 3400), (cues[0]["start_ms"], cues[0]["end_ms"]))
        self.assertEqual("第一句话讲完整。", cues[0]["text"])
        self.assertEqual("embedded_subtitle", cues[1]["source"])

    def test_alignment_uses_subtitle_timing_and_asr_to_score_text(self) -> None:
        aligned = align_temporal_sources(
            {
                "status": "available",
                "language": "zh",
                "cues": [
                    {"start_ms": 0, "end_ms": 1800, "text": "这里应该完整切片"}
                ],
            },
            {
                "status": "available",
                "provider": "test-asr",
                "model": "word-clock",
                "segments": [
                    {"start_ms": 0, "end_ms": 1800, "text": "这里应该完整切片"}
                ],
                "words": [
                    {"start_ms": 0, "end_ms": 400, "text": "这里"},
                    {"start_ms": 450, "end_ms": 900, "text": "应该"},
                    {"start_ms": 950, "end_ms": 1800, "text": "完整切片"},
                ],
                "silences": [{"start_ms": 1800, "end_ms": 2200}],
            },
        )

        self.assertEqual("available", aligned["status"])
        self.assertEqual("aligned", aligned["source"])
        self.assertEqual(1.0, aligned["cues"][0]["confidence"])
        self.assertEqual(3, len(aligned["words"]))
        self.assertEqual("这里应该完整切片", aligned["full_text"])

    def test_text_only_asr_is_searchable_but_not_a_cut_timeline(self) -> None:
        aligned = align_temporal_sources(
            {"status": "unavailable", "cues": []},
            {
                "status": "unavailable",
                "reason": "word_timestamps_missing",
                "text": "这是可检索的完整转写，但不能据此确定切点。",
                "timing_precision": "text_only",
                "segments": [],
                "words": [],
                "silences": [],
            },
        )

        self.assertEqual("unavailable", aligned["status"])
        self.assertEqual("asr", aligned["source"])
        self.assertEqual("text_only", aligned["timing_precision"])
        self.assertIn("可检索", aligned["full_text"])

    def test_estimated_asr_boundaries_do_not_claim_semantic_cut_safety(self) -> None:
        boundaries = semantic_cut_boundaries(
            20_000,
            shot_boundaries=[0, 20_000],
            cues=[
                {
                    "start_ms": 0,
                    "end_ms": 10_000,
                    "text": "只有估算时间",
                    "semantic_complete": False,
                }
            ],
            words=[],
            silences=[],
        )

        estimated = next(item for item in boundaries if item["timestamp_ms"] == 10_000)
        self.assertIn("estimated_transcript_boundary", estimated["reasons"])
        self.assertFalse(estimated["cut_safe"])
        self.assertFalse(estimated["semantic_complete"])
        self.assertFalse(
            any(
                item["semantic_complete"]
                for item in boundaries
                if item["timestamp_ms"] not in {0, 20_000}
            )
        )

    def test_cut_scoring_rejects_boundaries_inside_spoken_words(self) -> None:
        boundaries = semantic_cut_boundaries(
            12_000,
            shot_boundaries=[0, 5000, 12_000],
            cues=[
                {"start_ms": 0, "end_ms": 5200, "text": "尚未说完"},
                {"start_ms": 5500, "end_ms": 9000, "text": "这一句完整结束"},
            ],
            words=[
                {"start_ms": 4900, "end_ms": 5500, "text": "连续发音"},
                {"start_ms": 7600, "end_ms": 8200, "text": "完整"},
            ],
            silences=[{"start_ms": 5000, "end_ms": 5400}],
        )

        timestamps = [item["timestamp_ms"] for item in boundaries]
        self.assertFalse(any(5000 < value < 5500 for value in timestamps))
        self.assertIn(9000, timestamps)
        safe = next(item for item in boundaries if item["timestamp_ms"] == 9000)
        self.assertTrue(safe["cut_safe"])
        self.assertTrue(safe["semantic_complete"])

    def test_duration_guard_moves_to_a_safe_word_gap(self) -> None:
        boundaries = semantic_cut_boundaries(
            10_000,
            shot_boundaries=[0, 10_000],
            cues=[],
            words=[
                {"start_ms": 4300, "end_ms": 4700, "text": "前一个词"},
                {"start_ms": 4800, "end_ms": 5200, "text": "跨过硬切点"},
            ],
            silences=[],
            maximum_segment_ms=5000,
        )

        self.assertEqual(4700, boundaries[1]["timestamp_ms"])
        self.assertEqual(["maximum_duration_word_gap"], boundaries[1]["reasons"])
        self.assertTrue(boundaries[1]["cut_safe"])

    def test_visual_only_boundaries_remain_candidates_without_speech_timing(self) -> None:
        boundaries = semantic_cut_boundaries(
            20_000,
            shot_boundaries=[0, 4200, 9900, 20_000],
            cues=[],
            words=[],
            silences=[],
        )

        visual = [item for item in boundaries if "shot_boundary" in item["reasons"]]
        self.assertTrue(visual)
        self.assertTrue(all(not item["cut_safe"] for item in visual))

    def test_long_form_sampling_expands_with_a_bounded_scene_aware_plan(self) -> None:
        duration = 7_000_000
        count = _representative_frame_count(duration)
        timestamps = _representative_timestamps(
            duration,
            count,
            [0, 100_000, 1_000_000, 3_000_000, 6_900_000, duration],
        )

        self.assertEqual(32, count)
        self.assertEqual(32, len(timestamps))
        self.assertEqual(len(timestamps), len(set(timestamps)))
        self.assertTrue(all(0 <= item < duration for item in timestamps))


if __name__ == "__main__":
    unittest.main()
