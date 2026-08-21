from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from framefactory.worker.adapters.asset_analysis import (
    LocalAssetStageProcessor,
    align_temporal_sources,
    semantic_cut_boundaries,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class TemporalMediaIntegrationTests(unittest.TestCase):
    def test_embedded_subtitles_drive_real_video_cut_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitles = root / "captions.srt"
            subtitles.write_text(
                """1
00:00:00,200 --> 00:00:02,000
第一句话在这里完整结束。

2
00:00:02,600 --> 00:00:05,000
第二句话结束后再切换画面。
""",
                encoding="utf-8",
            )
            source = root / "source.mp4"
            result = subprocess.run(
                (
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=25",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000",
                    "-i",
                    str(subtitles),
                    "-t",
                    "6",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-map",
                    "2:s:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-c:a",
                    "aac",
                    "-c:s",
                    "mov_text",
                    "-y",
                    str(source),
                ),
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
            processor = LocalAssetStageProcessor(
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
            )

            technical = processor._probe(source)
            extracted = processor._subtitle_extraction(source, technical)
            temporal = align_temporal_sources(extracted, {})
            boundaries = semantic_cut_boundaries(
                technical["duration_ms"],
                shot_boundaries=[0, technical["duration_ms"]],
                cues=temporal["cues"],
                words=[],
                silences=[],
            )

            self.assertTrue(technical["has_audio"])
            self.assertEqual("available", extracted["status"])
            self.assertEqual(2, len(extracted["cues"]))
            self.assertEqual([0, 2000, 5000, 6000], [item["timestamp_ms"] for item in boundaries])
            self.assertTrue(all(item["cut_safe"] for item in boundaries))


if __name__ == "__main__":
    unittest.main()
