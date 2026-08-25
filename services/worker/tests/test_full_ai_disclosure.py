from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from framefactory.runtime import PermanentStepError
from framefactory.steps import StepContext
from framefactory.worker.adapters.edl_media import (
    _AI_DISCLOSURE,
    _ai_disclosure_metadata_args,
    _full_ai_disclosure,
    _verify_ai_disclosure,
)


def _context(*, disclosure: bool = True) -> StepContext:
    return StepContext(
        workspace_id=str(uuid4()),
        run_id=str(uuid4()),
        step_id="render",
        input_snapshot={
            "visual_source_mode": "generated_only",
            "ai_disclosure": disclosure,
        },
    )


class FullAiDisclosureTests(unittest.TestCase):
    def test_standard_edl_command_gets_no_metadata_arguments(self) -> None:
        self.assertIsNone(_full_ai_disclosure(_context(), {"operation": "media.retrieve"}))
        self.assertEqual((), _ai_disclosure_metadata_args(None))

    def test_generated_manifest_requires_and_emits_canonical_disclosure(self) -> None:
        with self.assertRaisesRegex(PermanentStepError, "disclosure"):
            _full_ai_disclosure(
                _context(disclosure=False), {"operation": "media.generate"}
            )
        disclosure = _full_ai_disclosure(
            _context(), {"operation": "media.generate"}
        )
        self.assertEqual(_AI_DISCLOSURE, disclosure)
        self.assertEqual(
            (
                "-metadata",
                f"comment={_AI_DISCLOSURE}",
                "-metadata",
                f"description={_AI_DISCLOSURE}",
            ),
            _ai_disclosure_metadata_args(disclosure),
        )

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
    )
    def test_ffprobe_confirms_disclosure_in_mp4_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "disclosed.mp4"
            command = (
                shutil.which("ffmpeg") or "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=25",
                "-t",
                "0.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                *_ai_disclosure_metadata_args(_AI_DISCLOSURE),
                "-y",
                str(output),
            )
            completed = subprocess.run(
                command, capture_output=True, check=False, timeout=60
            )
            self.assertEqual(
                0, completed.returncode, completed.stderr.decode(errors="replace")
            )
            asyncio.run(
                _verify_ai_disclosure(
                    shutil.which("ffprobe") or "ffprobe",
                    output,
                    _context(),
                    cwd=root,
                )
            )


if __name__ == "__main__":
    unittest.main()
