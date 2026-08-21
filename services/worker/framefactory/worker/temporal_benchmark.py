"""Deterministic quality gate for semantic video-cut boundaries."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .adapters.asset_analysis import semantic_cut_boundaries


def evaluate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    tolerance_ms = _positive_int(manifest.get("tolerance_ms"), "tolerance_ms")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes)):
        raise TypeError("benchmark cases must be an array")

    true_positive = 0
    predicted_total = 0
    expected_total = 0
    unsafe_total = 0
    absolute_errors: list[int] = []
    case_results: list[dict[str, Any]] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping):
            raise TypeError("each benchmark case must be an object")
        case_id = str(raw_case.get("id") or "").strip()
        if not case_id:
            raise ValueError("each benchmark case requires an id")
        duration_ms = _positive_int(raw_case.get("duration_ms"), f"{case_id}.duration_ms")
        expected = _timestamps(raw_case.get("expected_boundaries_ms"), duration_ms)
        forbidden = _ranges(raw_case.get("forbidden_ranges_ms"), duration_ms)
        boundaries = semantic_cut_boundaries(
            duration_ms,
            shot_boundaries=_timestamps(
                raw_case.get("shot_boundaries_ms", [0, duration_ms]),
                duration_ms,
                include_edges=True,
            ),
            cues=raw_case.get("cues", []),
            words=raw_case.get("words", []),
            silences=raw_case.get("silences", []),
            minimum_segment_ms=int(raw_case.get("minimum_segment_ms", 900)),
            maximum_segment_ms=int(raw_case.get("maximum_segment_ms", 15_000)),
        )
        predicted_items = [
            item for item in boundaries if int(item["timestamp_ms"]) not in {0, duration_ms}
        ]
        predicted = [int(item["timestamp_ms"]) for item in predicted_items]
        matched, errors = _match_boundaries(predicted, expected, tolerance_ms)
        unsafe = sum(
            1
            for item in predicted_items
            if not item.get("cut_safe", False)
            or any(start < int(item["timestamp_ms"]) < end for start, end in forbidden)
        )
        true_positive += matched
        predicted_total += len(predicted)
        expected_total += len(expected)
        unsafe_total += unsafe
        absolute_errors.extend(errors)
        case_results.append(
            {
                "id": case_id,
                "predicted_boundaries_ms": predicted,
                "expected_boundaries_ms": expected,
                "matched": matched,
                "false_positive": len(predicted) - matched,
                "missed": len(expected) - matched,
                "unsafe": unsafe,
            }
        )

    precision = true_positive / predicted_total if predicted_total else 0.0
    recall = true_positive / expected_total if expected_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    unsafe_rate = unsafe_total / predicted_total if predicted_total else 0.0
    return {
        "schema_version": "1",
        "case_count": len(case_results),
        "tolerance_ms": tolerance_ms,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "unsafe_cut_rate": round(unsafe_rate, 4),
        "mean_absolute_error_ms": (
            round(sum(absolute_errors) / len(absolute_errors), 2) if absolute_errors else None
        ),
        "cases": case_results,
    }


def load_and_evaluate(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError("benchmark manifest must be an object")
    return evaluate_manifest(value)


def _match_boundaries(
    predicted: Sequence[int], expected: Sequence[int], tolerance_ms: int
) -> tuple[int, list[int]]:
    available = set(range(len(predicted)))
    errors: list[int] = []
    for target in expected:
        candidates = [
            (abs(predicted[index] - target), index)
            for index in available
            if abs(predicted[index] - target) <= tolerance_ms
        ]
        if not candidates:
            continue
        error, index = min(candidates)
        available.remove(index)
        errors.append(error)
    return len(errors), errors


def _timestamps(value: Any, duration_ms: int, *, include_edges: bool = False) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("boundary values must be an array")
    result: list[int] = []
    for raw in value:
        timestamp = int(raw)
        if (include_edges and 0 <= timestamp <= duration_ms) or 0 < timestamp < duration_ms:
            result.append(timestamp)
        else:
            raise ValueError("boundary timestamp is outside the asset duration")
    return sorted(set(result))


def _ranges(value: Any, duration_ms: int) -> list[tuple[int, int]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("forbidden ranges must be an array")
    result: list[tuple[int, int]] = []
    for raw in value:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 2:
            raise ValueError("each forbidden range must contain start and end")
        start, end = int(raw[0]), int(raw[1])
        if start < 0 or end <= start or end > duration_ms:
            raise ValueError("forbidden range is outside the asset duration")
        result.append((start, end))
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a positive integer")
    number = int(value)
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framefactory-cut-benchmark")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--minimum-f1", type=float, default=0.9)
    parser.add_argument("--maximum-unsafe-rate", type=float, default=0.0)
    arguments = parser.parse_args(argv)
    try:
        result = load_and_evaluate(arguments.manifest)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    passed = (
        result["f1"] >= arguments.minimum_f1
        and result["unsafe_cut_rate"] <= arguments.maximum_unsafe_rate
    )
    result["status"] = "passed" if passed else "failed"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
