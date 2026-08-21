from pathlib import Path

import pytest

from framefactory_api.asset_ingest import discover_media, normalize_analysis, validate_source_root
from framefactory_api.models import AssetUploadCreate


def test_source_guard_rejects_repository_root_and_generated_outputs(tmp_path: Path) -> None:
    (tmp_path / "src" / "runs").mkdir(parents=True)
    (tmp_path / "var" / "exports").mkdir(parents=True)
    (tmp_path / "artifacts").mkdir()
    with pytest.raises(ValueError):
        validate_source_root(tmp_path, repository_root=tmp_path)
    with pytest.raises(ValueError):
        validate_source_root(tmp_path / "src" / "runs", repository_root=tmp_path)


def test_discovery_ignores_old_outputs_and_non_media(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "clips").mkdir(parents=True)
    (source / "_fin").mkdir()
    (source / "clips" / "one.mp4").write_bytes(b"video")
    (source / "clips" / "cover.jpg").write_bytes(b"image")
    (source / "clips" / "old-tags.json").write_text("{}", encoding="utf-8")
    (source / "_fin" / "render.mp4").write_bytes(b"generated")

    assert {item.name for item in discover_media((source,))} == {"one.mp4", "cover.jpg"}


def test_single_matroska_file_is_an_explicit_supported_source(tmp_path: Path) -> None:
    movie = tmp_path / "feature.mkv"
    movie.write_bytes(b"matroska")

    assert validate_source_root(movie, repository_root=tmp_path / "repository") == movie
    assert discover_media((movie,)) == (movie,)


def test_local_long_form_upload_contract_accepts_two_gibibytes() -> None:
    command = AssetUploadCreate(
        library_id="efd3f8da-57bd-565a-b34c-2040edb7d5a4",
        filename="feature.mkv",
        title="Feature film",
        kind="video",
        content_type="video/x-matroska",
        byte_size=2_128_311_438,
        sha256="a" * 64,
        copyright_status="owned",
    )

    assert command.byte_size == 2_128_311_438


def test_analysis_normalization_clamps_confidence_and_rejects_bad_segments() -> None:
    result = normalize_analysis(
        {
            "summary": "一位运动员在赛场挥拍",
            "people": ["运动员", "运动员", ""],
            "keywords": ["乒乓球"],
            "confidence": 2,
            "segments": [
                {
                    "start_seconds": 0,
                    "end_seconds": 3.5,
                    "description": "运动员发球",
                    "keywords": ["发球"],
                    "confidence": -1,
                },
                {"start_seconds": 2, "end_seconds": 3, "description": "重叠片段"},
                {"start_seconds": 4, "end_seconds": 20, "description": "超出时长"},
            ],
        },
        duration_ms=10_000,
    )

    assert result["people"] == ["运动员"]
    assert result["confidence"] == 1
    assert result["segments"] == [
        {
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": 3500,
            "description": "运动员发球",
            "people": [],
            "locations": [],
            "keywords": ["发球"],
            "scene_type": None,
            "action": None,
            "era": None,
            "mood": None,
            "visual_style": None,
            "shot_type": None,
            "confidence": 0,
        },
        {
            "ordinal": 2,
            "start_ms": 4000,
            "end_ms": 10000,
            "description": "超出时长",
            "people": [],
            "locations": [],
            "keywords": [],
            "scene_type": None,
            "action": None,
            "era": None,
            "mood": None,
            "visual_style": None,
            "shot_type": None,
            "confidence": None,
        },
    ]
