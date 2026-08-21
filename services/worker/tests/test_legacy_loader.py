from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from framefactory.skills import (
    SkillValidationError,
    load_legacy_skill,
    load_skill_version_file,
)

from tests._fixtures import legacy_manifest, skill_payload


class LegacyAdapterTests(unittest.TestCase):
    def _write_triplet(self, root: Path) -> Path:
        nested = root / "财经" / "legacy-seed"
        nested.mkdir(parents=True)
        (nested / "manifest.json").write_text(
            json.dumps(legacy_manifest(), ensure_ascii=False), encoding="utf-8"
        )
        (nested / "写作公式.md").write_text(
            "# Formula\r\n\r\n```python\r\nprint('inert example')\r\n```\r\nD:/old-machine/example",
            encoding="utf-8",
        )
        (nested / "USAGE.md").write_text("# Usage\n\nUse for verified history.", encoding="utf-8")
        return nested

    def test_read_only_adapter_converts_exact_triplet_and_keeps_markdown_inert(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trusted = Path(temp)
            directory = self._write_triplet(trusted)

            version = load_legacy_skill(directory, trusted_root=trusted)

        self.assertEqual("legacy-history-seed", version.skill_id)
        self.assertEqual("legacy", version.lineage.kind)
        self.assertEqual(1, version.lineage.parent_version)
        self.assertIn("```python", version.writing_instructions.instructions)
        self.assertIn("D:/old-machine/example", version.writing_instructions.instructions)
        self.assertIn("```python", version.qc_rubric.instructions)
        self.assertIn("Use for verified history", version.description)
        self.assertNotIn("D:/historical-machine/corpus", version.research_policy.instructions)

    def test_usage_changes_the_semantic_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trusted = Path(temp)
            directory = self._write_triplet(trusted)
            first = load_legacy_skill(directory, trusted_root=trusted)
            (directory / "USAGE.md").write_text("# Different usage", encoding="utf-8")
            second = load_legacy_skill(directory, trusted_root=trusted)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_dangerous_legacy_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trusted = Path(temp)
            directory = self._write_triplet(trusted)
            manifest = legacy_manifest()
            manifest["formula_path"] = "../outside.md"
            (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(SkillValidationError, "dangerous_field"):
                load_legacy_skill(directory, trusted_root=trusted)

    def test_incomplete_triplet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "skill"
            directory.mkdir()
            (directory / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SkillValidationError, "incomplete"):
                load_legacy_skill(directory, trusted_root=temp)

    def test_adapter_does_not_scan_or_execute_sibling_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trusted = Path(temp)
            directory = self._write_triplet(trusted)
            marker = directory / "payload.py"
            marker.write_text("raise RuntimeError('must never run')", encoding="utf-8")

            version = load_legacy_skill(directory, trusted_root=trusted)

            self.assertEqual("legacy-history-seed", version.skill_id)
            self.assertTrue(marker.exists())


class FileLoaderTests(unittest.TestCase):
    def test_file_loader_requires_trusted_root_containment(self) -> None:
        with tempfile.TemporaryDirectory() as trusted_temp, tempfile.TemporaryDirectory() as outside_temp:
            trusted = Path(trusted_temp)
            inside = trusted / "skill.json"
            inside.write_text(json.dumps(skill_payload()), encoding="utf-8")
            outside = Path(outside_temp) / "skill.json"
            outside.write_text(json.dumps(skill_payload()), encoding="utf-8")

            self.assertEqual("user-blank", load_skill_version_file(inside, trusted_root=trusted).skill_id)
            with self.assertRaisesRegex(SkillValidationError, "trusted_root"):
                load_skill_version_file(outside, trusted_root=trusted)


if __name__ == "__main__":
    unittest.main()
