from __future__ import annotations

import json
from pathlib import Path

import pytest
from framefactory.worker.semantic_timeline_benchmark import evaluate_manifest, main

WORKER_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = WORKER_ROOT.parents[1]
MANIFEST = WORKER_ROOT / "benchmarks" / "semantic_timeline_v1.json"
GENERIC_PRODUCTION_FILES = (
    WORKER_ROOT / "framefactory" / "worker" / "semantic_text.py",
    WORKER_ROOT / "framefactory" / "worker" / "timeline.py",
    WORKER_ROOT / "framefactory" / "worker" / "asset_acquisition_plan.py",
    WORKER_ROOT / "framefactory" / "worker" / "adapters" / "database_assets.py",
)


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_semantic_story_and_incident_baseline_passes() -> None:
    result = evaluate_manifest(_manifest())

    assert result["lesson_count"] == 5
    assert result["case_count"] == 3
    assert result["passed_count"] == 3
    assert result["pass_rate"] == 1.0
    assert result["passed"] is True
    assert all(item["padding_seconds"] == 0 for item in result["cases"])
    assert all(item["story_order_preserved"] is True for item in result["cases"])


def test_every_incident_lesson_points_to_an_existing_regression_test() -> None:
    for lesson in _manifest()["lessons"]:
        for reference in lesson["regression_tests"]:
            path = REPOSITORY_ROOT / reference.partition("::")[0]
            assert path.is_file(), reference


def test_generic_editing_core_contains_no_case_specific_vocabulary() -> None:
    forbidden = ("耀斑", "日冕", "极光", "磁层", "张继科", "鹿特丹", "乒乓球")

    for path in GENERIC_PRODUCTION_FILES:
        source = path.read_text(encoding="utf-8")
        assert not any(term in source for term in forbidden), path


def test_semantic_benchmark_rejects_duplicate_incident_codes() -> None:
    manifest = _manifest()
    manifest["lessons"].append(dict(manifest["lessons"][0]))

    with pytest.raises(ValueError, match="duplicate lesson issue_code"):
        evaluate_manifest(manifest)


def test_semantic_benchmark_cli_reports_story_order_regression(tmp_path: Path) -> None:
    manifest = _manifest()
    case = manifest["cases"][0]
    case["required_scene_order"] = list(reversed(case["required_scene_order"]))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert main([str(broken)]) == 1
