from __future__ import annotations

import json
from pathlib import Path

import pytest
from framefactory.worker.document_hybrid.pilot import (
    PipelineError,
    Tools,
    _load_storyboard,
    _subtitle_chunks,
    execute_pilot,
)


def _write_storyboard(path: Path, *, page: int = 1, crop: list[float] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "title": "测试文档",
                "scenes": [
                    {
                        "id": "scene-001",
                        "page": page,
                        "narration": "这是一段严格依据文件的测试旁白。",
                        "crop": crop or [0.05, 0.05, 0.9, 0.85],
                        "layout": "evidence_card",
                        "background_policy": "agnes_preferred",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_storyboard_parses_grounded_scene(tmp_path: Path) -> None:
    path = tmp_path / "storyboard.json"
    _write_storyboard(path)

    title, scenes = _load_storyboard(path, page_count=2)

    assert title == "测试文档"
    assert scenes[0].page == 1
    assert scenes[0].background_policy == "agnes_preferred"


def test_storyboard_rejects_page_outside_document(tmp_path: Path) -> None:
    path = tmp_path / "storyboard.json"
    _write_storyboard(path, page=3)

    with pytest.raises(PipelineError, match="outside 1..2"):
        _load_storyboard(path, page_count=2)


def test_storyboard_rejects_crop_outside_page(tmp_path: Path) -> None:
    path = tmp_path / "storyboard.json"
    _write_storyboard(path, crop=[0.8, 0.1, 0.4, 0.5])

    with pytest.raises(PipelineError, match="inside the source page"):
        _load_storyboard(path, page_count=2)


def test_subtitle_chunks_preserve_narration() -> None:
    narration = "第一句说明。第二句继续解释，最后给出结论。"

    chunks = _subtitle_chunks(narration, maximum=8)

    assert "".join(chunks) == narration
    assert len(chunks) >= 3


def test_pilot_refuses_nonempty_output_directory(tmp_path: Path) -> None:
    input_pdf = tmp_path / "source.pdf"
    input_pdf.write_bytes(b"%PDF-1.4\n")
    storyboard = tmp_path / "storyboard.json"
    _write_storyboard(storyboard)
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(
        json.dumps({"slug": "document-hybrid-production"}), encoding="utf-8"
    )
    output = tmp_path / "output"
    output.mkdir()
    (output / "existing.txt").write_text("keep", encoding="utf-8")
    unavailable = tmp_path / "unavailable"

    with pytest.raises(PipelineError, match="must be empty"):
        execute_pilot(
            input_pdf=input_pdf,
            storyboard_path=storyboard,
            pipeline_path=pipeline,
            output_dir=output,
            tools=Tools(
                pdfinfo=unavailable,
                pdftoppm=unavailable,
                ffmpeg=unavailable,
                ffprobe=unavailable,
                powershell=unavailable,
            ),
            voice="unused",
        )

    assert (output / "existing.txt").read_text(encoding="utf-8") == "keep"
