"""Executable regression gate for narration-to-picture story alignment."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .timeline import plan_edit_timeline


def evaluate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate durable incident lessons against the production timeline planner."""

    if str(manifest.get("schema_version", "")) != "1":
        raise ValueError("semantic timeline benchmark requires schema_version 1")
    lessons = _lessons(manifest.get("lessons"))
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        raise TypeError("benchmark cases must be an array")
    if not raw_cases:
        raise ValueError("benchmark requires at least one case")

    case_results: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise TypeError("each benchmark case must be an object")
        case_id = _required_text(raw_case.get("id"), "case.id")
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate benchmark case id: {case_id}")
        seen_case_ids.add(case_id)
        case_results.append(_evaluate_case(case_id, raw_case))

    passed_count = sum(1 for item in case_results if item["passed"])
    return {
        "schema_version": "1",
        "lesson_count": len(lessons),
        "case_count": len(case_results),
        "passed_count": passed_count,
        "pass_rate": round(passed_count / len(case_results), 4),
        "passed": passed_count == len(case_results),
        "cases": case_results,
    }


def _evaluate_case(case_id: str, case: Mapping[str, Any]) -> dict[str, Any]:
    script = case.get("script")
    if not isinstance(script, Mapping):
        raise TypeError(f"{case_id}.script must be an object")
    assets = case.get("assets")
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)) or not assets:
        raise TypeError(f"{case_id}.assets must be a non-empty array")
    if any(not isinstance(item, Mapping) for item in assets):
        raise TypeError(f"{case_id}.assets entries must be objects")
    duration_seconds = _positive_number(
        case.get("duration_seconds"), f"{case_id}.duration_seconds"
    )
    expected_order = _text_array(
        case.get("required_scene_order"), f"{case_id}.required_scene_order"
    )
    if not expected_order:
        raise ValueError(f"{case_id}.required_scene_order must not be empty")
    maximum_padding = _non_negative_number(
        case.get("maximum_padding_seconds", 0),
        f"{case_id}.maximum_padding_seconds",
    )
    maximum_shot = _positive_number(
        case.get("maximum_shot_seconds", 8), f"{case_id}.maximum_shot_seconds"
    )

    timeline = plan_edit_timeline(
        script,
        assets,
        duration_seconds,
        maximum_shot_seconds=maximum_shot,
    )
    observed_order = _collapsed_scene_order(timeline, assets)
    missing_scenes = [scene for scene in expected_order if scene not in observed_order]
    ordered = _is_ordered_subsequence(expected_order, observed_order)
    padding_seconds = round(
        sum(float(item.get("padding_seconds", 0)) for item in timeline), 3
    )
    invalid_windows = sum(
        1
        for item in timeline
        if float(item.get("source_end_seconds", 0))
        < float(item.get("source_start_seconds", 0))
        or float(item.get("duration_seconds", 0)) <= 0
        or float(item.get("duration_seconds", 0)) > maximum_shot + 0.001
    )
    passed = (
        bool(timeline)
        and not missing_scenes
        and ordered
        and padding_seconds <= maximum_padding + 0.001
        and invalid_windows == 0
    )
    return {
        "id": case_id,
        "passed": passed,
        "shot_count": len(timeline),
        "observed_scene_order": observed_order,
        "required_scene_order": expected_order,
        "missing_scenes": missing_scenes,
        "story_order_preserved": ordered,
        "padding_seconds": padding_seconds,
        "invalid_windows": invalid_windows,
    }


def _collapsed_scene_order(
    timeline: Sequence[Mapping[str, Any]], assets: Sequence[Mapping[str, Any]]
) -> list[str]:
    result: list[str] = []
    for shot in timeline:
        asset_index = int(shot.get("asset_index", -1))
        if not 0 <= asset_index < len(assets):
            scene = "<invalid-asset>"
        else:
            asset = assets[asset_index]
            scene = str(
                asset.get("selected_for_scene")
                or asset.get("description")
                or asset.get("asset_id")
                or "<unknown-scene>"
            ).strip()
        if not result or result[-1] != scene:
            result.append(scene)
    return result


def _is_ordered_subsequence(expected: Sequence[str], observed: Sequence[str]) -> bool:
    cursor = 0
    for scene in observed:
        if cursor < len(expected) and scene == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def _lessons(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise TypeError("benchmark lessons must be a non-empty array")
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise TypeError("each benchmark lesson must be an object")
        issue_code = _required_text(raw.get("issue_code"), "lesson.issue_code")
        if issue_code in seen:
            raise ValueError(f"duplicate lesson issue_code: {issue_code}")
        seen.add(issue_code)
        for field in ("symptom", "root_cause", "fix"):
            _required_text(raw.get(field), f"{issue_code}.{field}")
        if not _text_array(raw.get("regression_tests"), f"{issue_code}.regression_tests"):
            raise ValueError(f"{issue_code}.regression_tests must not be empty")
        result.append(raw)
    return result


def _text_array(value: Any, name: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be an array")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise ValueError(f"{name} must not contain empty values")
    return result


def _required_text(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _positive_number(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative_number(value: Any, name: str) -> float:
    number = float(value)
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framefactory-semantic-timeline-benchmark")
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args(argv)
    try:
        value = json.loads(arguments.manifest.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise TypeError("benchmark manifest must be an object")
        result = evaluate_manifest(value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    result["status"] = "passed" if result["passed"] else "failed"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
