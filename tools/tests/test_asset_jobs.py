from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "asset_jobs.py"
SPEC = importlib.util.spec_from_file_location("asset_jobs", TOOL)
assert SPEC is not None and SPEC.loader is not None
asset_jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(asset_jobs)


class AssetJobsToolTests(unittest.TestCase):
    def test_plan_defaults_to_small_read_only_canary(self) -> None:
        arguments = asset_jobs.parser().parse_args(
            ["--database-url", "postgresql://db/framefactory", "plan", "--workspace-id", "workspace"]
        )
        self.assertEqual("plan", arguments.command)
        self.assertEqual(10, arguments.limit)

    def test_mutation_requires_both_confirmation_and_test_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "test-data"):
            asset_jobs._require_test_data("postgresql://db/framefactory", True)
        with self.assertRaisesRegex(ValueError, "test-data"):
            asset_jobs._require_test_data("postgresql://db/framefactory_test", False)
        asset_jobs._require_test_data("postgresql://db/framefactory_test", True)

    def test_limit_cannot_select_all_quarantined_assets(self) -> None:
        with self.assertRaises(SystemExit):
            asset_jobs.parser().parse_args(
                ["plan", "--workspace-id", "workspace", "--limit", "1247"]
            )


if __name__ == "__main__":
    unittest.main()
