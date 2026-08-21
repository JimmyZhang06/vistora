from __future__ import annotations

import json
from pathlib import Path

from framefactory.worker.temporal_benchmark import evaluate_manifest, main

MANIFEST = Path(__file__).parents[1] / "benchmarks" / "temporal_cut_v1.json"


def test_hand_labeled_cut_baseline_passes_quality_gate() -> None:
    result = evaluate_manifest(json.loads(MANIFEST.read_text(encoding="utf-8")))

    assert result["case_count"] == 3
    assert result["f1"] == 1.0
    assert result["unsafe_cut_rate"] == 0.0
    assert all(item["missed"] == 0 for item in result["cases"])


def test_benchmark_cli_fails_when_threshold_is_stricter_than_result() -> None:
    assert main([str(MANIFEST), "--minimum-f1", "1.01"]) == 1
