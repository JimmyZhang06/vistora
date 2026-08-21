from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from framefactory.worker.adapters.asset_analysis import (
    LocalAssetStageProcessor,
    S3MediaObjectStore,
)
from framefactory.worker.asset_pipeline import AssetSource, AssetStage, AssetWork


def work() -> AssetWork:
    return AssetWork(
        item_id="item",
        batch_id="batch",
        workspace_id="11111111-1111-4111-8111-111111111111",
        asset_id="22222222-2222-4222-8222-222222222222",
        revision=1,
        attempts=1,
        max_attempts=3,
        completed_stages=(),
        checkpoints={},
        copyright_status="licensed",
        content_hash="a" * 64,
        media_type="video/mp4",
        bucket="assets",
        object_key="workspaces/11111111-1111-4111-8111-111111111111/source.mp4",
        original_filename="source.mp4",
        sources=(AssetSource("upload", license="commercial"),),
    )


class FakeStore:
    def __init__(self) -> None:
        self.preview: bytes | None = None

    def download(self, _work: AssetWork, destination: Path) -> None:
        destination.write_bytes(b"source")

    def publish_preview(self, _work: AssetWork, preview: Path):
        self.preview = preview.read_bytes()
        return {
            "object_key": "workspaces/11111111-1111-4111-8111-111111111111/preview.mp4",
            "content_hash": hashlib.sha256(self.preview).hexdigest(),
            "media_type": "video/mp4",
            "byte_size": len(self.preview),
        }

    def publish_frame(self, _work: AssetWork, _frame: Path, _ordinal: int) -> str:
        return (
            "workspaces/11111111-1111-4111-8111-111111111111/"
            f"frames/{_ordinal:03d}.jpg"
        )


class FakeS3:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def put_object(self, *, Key, Body, **_kwargs) -> None:
        self.values[Key] = Body.read()


class AssetPreviewTests(unittest.TestCase):
    def test_video_preview_is_browser_compatible_and_published(self) -> None:
        store = FakeStore()
        processor = LocalAssetStageProcessor(store, SimpleNamespace(), SimpleNamespace())
        commands: list[tuple[str, ...]] = []

        def run(command, **_kwargs):
            commands.append(tuple(command))
            Path(command[-1]).write_bytes(b"preview-video")
            return SimpleNamespace(returncode=0)

        try:
            with patch("shutil.which", return_value="ffmpeg"), patch(
                "subprocess.run", side_effect=run
            ):
                result = processor.run(
                    AssetStage.PREVIEW,
                    work(),
                    {"ffprobe": {"width": 1280, "duration_ms": 433_600}},
                    lambda: False,
                )
        finally:
            processor.close()

        self.assertEqual(b"preview-video", store.preview)
        self.assertEqual("video/mp4", result["media_type"])
        self.assertEqual(960, result["width"])
        self.assertIn("libx264", commands[0])
        self.assertIn("yuv420p", commands[0])
        self.assertIn("+faststart", commands[0])

    def test_s3_preview_descriptor_is_content_addressed(self) -> None:
        client = FakeS3()
        store = S3MediaObjectStore(client)
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.mp4"
            preview.write_bytes(b"browser-preview")
            result = store.publish_preview(work(), preview)

        self.assertEqual("video/mp4", result["media_type"])
        self.assertEqual(len(b"browser-preview"), result["byte_size"])
        self.assertTrue(result["object_key"].endswith(".mp4"))
        self.assertEqual(b"browser-preview", client.values[result["object_key"]])

    def test_keyframes_include_a_persistable_poster_descriptor(self) -> None:
        store = FakeStore()
        processor = LocalAssetStageProcessor(store, SimpleNamespace(), SimpleNamespace())

        def run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"representative-frame")
            return SimpleNamespace(returncode=0)

        try:
            with patch("shutil.which", return_value="ffmpeg"), patch(
                "subprocess.run", side_effect=run
            ):
                result = processor.run(
                    AssetStage.KEYFRAMES,
                    work(),
                    {"ffprobe": {"duration_ms": 30_000}},
                    lambda: False,
                )
        finally:
            processor.close()

        self.assertEqual(3, len(result["frames"]))
        self.assertEqual(result["frames"][1]["object_key"], result["poster"]["object_key"])
        self.assertEqual("image/jpeg", result["poster"]["media_type"])
        self.assertEqual(len(b"representative-frame"), result["poster"]["byte_size"])


if __name__ == "__main__":
    unittest.main()
