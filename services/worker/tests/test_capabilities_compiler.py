from __future__ import annotations

import unittest

from framefactory.skills import (
    CapabilityDeclaration,
    MissingCapabilityError,
    SkillValidationError,
    compile_skill,
    parse_skill_version,
    resolve_capabilities,
)

from tests._fixtures import official_seed_payload


class CapabilityTests(unittest.TestCase):
    def test_resolution_is_exact_and_deterministic(self) -> None:
        declaration = CapabilityDeclaration.from_mapping(
            {
                "required": ["render.subtitle_karaoke", "timeline.word_level"],
                "optional": ["media.web_acquisition"],
            },
            path="$.capabilities.qc",
        )

        result = resolve_capabilities(
            declaration,
            ["media.web_acquisition", "timeline.word_level", "unrelated.capability"],
        )

        self.assertFalse(result.satisfied)
        self.assertEqual(("render.subtitle_karaoke",), result.missing_required)
        self.assertEqual(("media.web_acquisition",), result.enabled_optional)
        with self.assertRaises(MissingCapabilityError):
            result.require_satisfied()

    def test_wildcards_and_required_optional_overlap_are_rejected(self) -> None:
        with self.assertRaises(SkillValidationError):
            CapabilityDeclaration.from_mapping({"required": ["render.*"]}, path="$.caps")
        with self.assertRaisesRegex(SkillValidationError, "overlap"):
            CapabilityDeclaration.from_mapping(
                {"required": ["model.text_generation"], "optional": ["model.text_generation"]},
                path="$.caps",
            )
        with self.assertRaisesRegex(SkillValidationError, "overlap"):
            CapabilityDeclaration(
                required=("model.text_generation", "model.text_generation"),
                optional=("model.text_generation",),
            )


class CompilerTests(unittest.TestCase):
    def test_compilation_does_not_depend_on_special_ids(self) -> None:
        first_payload = official_seed_payload()
        second_payload = official_seed_payload()
        first_payload.update({"skill_id": "biography", "name": "One"})
        second_payload.update({"skill_id": "genshin", "name": "Two", "version": 99})

        first = parse_skill_version(first_payload)
        second = parse_skill_version(second_payload)

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(compile_skill(first).to_dict(), compile_skill(second).to_dict())
        rendered = str(compile_skill(first).to_dict())
        self.assertNotIn("biography", rendered)
        self.assertNotIn("genshin", rendered)

    def test_each_stage_receives_only_declared_stage_capabilities(self) -> None:
        version = parse_skill_version(official_seed_payload())
        compiled = compile_skill(version)

        self.assertEqual(("research.source_grounding",), compiled.research.required_capabilities)
        self.assertEqual(("research.web_search",), compiled.research.optional_capabilities)
        self.assertEqual(("model.text_generation",), compiled.writing.required_capabilities)
        self.assertEqual((), compiled.visual.required_capabilities)

    def test_compilation_is_repeatable(self) -> None:
        version = parse_skill_version(official_seed_payload())
        self.assertEqual(compile_skill(version).to_dict(), compile_skill(version).to_dict())


if __name__ == "__main__":
    unittest.main()
