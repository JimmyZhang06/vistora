from __future__ import annotations

import json
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path

from framefactory.skills import (
    SkillValidationError,
    compile_skill,
    load_skill_version_json,
    normalize_skill_version,
    parse_skill_version,
)

from tests._fixtures import official_seed_payload, skill_payload


class SkillVersionModelTests(unittest.TestCase):
    def test_batch_skill_declares_v3_inventory_without_making_v2_require_it(self) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        package = repository_root / "packages" / "seeds" / "official-skills" / "v1"
        skill_version = json.loads(
            (package / "general-topic-explainer" / "1.2.0.json").read_text(
                encoding="utf-8"
            )
        )
        pipeline = json.loads(
            (package / "pipelines" / "standard-production" / "3.json").read_text(
                encoding="utf-8"
            )
        )

        inventory = next(
            artifact
            for artifact in skill_version["output_contract"]["artifacts"]
            if artifact["kind"] == "inventory"
        )
        self.assertEqual("material-inventory", inventory["name"])
        self.assertFalse(inventory["required"])
        self.assertIn(
            "media.inventory", {node["operation"] for node in pipeline["nodes"]}
        )
        self.assertNotIn(
            "research.web_acquisition", pipeline["capability_requirements"]
        )
        web_research = next(
            requirement
            for requirement in skill_version["capability_requirements"]
            if requirement["name"] == "research.web_acquisition"
        )
        self.assertEqual("optional", web_research["level"])

    def test_blank_user_skill_loads_and_compiles_all_stages(self) -> None:
        version = parse_skill_version(skill_payload())

        compiled = compile_skill(version)

        self.assertTrue(version.content_hash.startswith("sha256:"))
        self.assertEqual({"research", "writing", "visual", "qc"}, set(compiled.to_dict()))
        self.assertNotIn("历史", json.dumps(compiled.to_dict(), ensure_ascii=False))
        self.assertNotIn("biography", json.dumps(compiled.to_dict(), ensure_ascii=False))

    def test_official_seed_uses_the_same_public_loader(self) -> None:
        document = json.dumps(official_seed_payload(), ensure_ascii=False)

        version = load_skill_version_json(document)

        self.assertEqual("system-history-seed", version.skill_id)
        self.assertEqual("published", version.state)
        self.assertIn("central tension", version.writing_instructions.instructions)

    def test_fork_keeps_content_hash_and_does_not_mutate_parent(self) -> None:
        parent = parse_skill_version(official_seed_payload())
        parent_snapshot = parent.to_dict()
        fork_payload = official_seed_payload()
        fork_payload.update(
            {
                "skill_id": "user-history-fork",
                "version": 1,
                "name": "My fork",
                "state": "draft",
                "lineage": {
                    "kind": "fork",
                    "parent_skill_id": parent.skill_id,
                    "parent_version": parent.version,
                    "parent_content_hash": parent.content_hash,
                },
            }
        )

        fork = parse_skill_version(fork_payload)

        self.assertEqual(parent.content_hash, fork.content_hash)
        self.assertEqual(parent_snapshot, parent.to_dict())
        changed = fork.to_dict(include_content_hash=False)
        changed["writing_instructions"]["instructions"] += " Add a counterargument."
        changed_version = parse_skill_version(changed)
        self.assertNotEqual(fork.content_hash, changed_version.content_hash)

    def test_hash_is_deterministic_across_order_newlines_and_unicode(self) -> None:
        composed = "Caf\u00e9\r\nmethod"
        decomposed = unicodedata.normalize("NFD", "Café") + "\nmethod"
        first = official_seed_payload()
        first["writing_instructions"]["instructions"] = composed
        first["capabilities"]["research"]["required"] = [
            "research.source_grounding",
            "model.text_generation",
            "research.source_grounding",
        ]
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["writing_instructions"]["instructions"] = decomposed
        second["capabilities"]["research"]["required"] = [
            "model.text_generation",
            "research.source_grounding",
        ]
        second = dict(reversed(list(second.items())))

        one = parse_skill_version(first)
        two = parse_skill_version(second)

        self.assertEqual(one.content_hash, two.content_hash)
        self.assertEqual(normalize_skill_version(one)["writing_instructions"], normalize_skill_version(two)["writing_instructions"])

    def test_nested_json_is_immutable(self) -> None:
        version = parse_skill_version(official_seed_payload())

        with self.assertRaises(TypeError):
            version.input_schema["type"] = "array"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            version.writing_instructions.instructions = "changed"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            version.input_schema._items = (("type", "array"),)  # type: ignore[attr-defined]

    def test_compiler_rejects_a_skill_object_with_a_stale_hash(self) -> None:
        version = parse_skill_version(official_seed_payload())
        forged = replace(
            version,
            writing_instructions=replace(version.writing_instructions, instructions="FORGED"),
        )
        with self.assertRaisesRegex(SkillValidationError, "modified or constructed unsafely"):
            compile_skill(forged)

    def test_supplied_hash_must_match(self) -> None:
        payload = skill_payload(content_hash="sha256:" + "0" * 64)
        with self.assertRaisesRegex(SkillValidationError, "hash"):
            parse_skill_version(payload)

    def test_duplicate_json_key_and_non_finite_number_are_rejected(self) -> None:
        duplicate = '{"skill_id":"one","skill_id":"two"}'
        with self.assertRaisesRegex(SkillValidationError, "duplicate"):
            load_skill_version_json(duplicate)

        document = json.dumps(skill_payload()).replace('"version": 1', '"version": NaN')
        with self.assertRaisesRegex(SkillValidationError, "NaN"):
            load_skill_version_json(document)


class SkillSecurityTests(unittest.TestCase):
    def test_unknown_executable_field_is_rejected(self) -> None:
        payload = skill_payload(command="python payload.py")
        with self.assertRaisesRegex(SkillValidationError, "dangerous_field"):
            parse_skill_version(payload)

    def test_nested_executable_field_is_rejected(self) -> None:
        for field in ("command", "python", "script", "plugin", "server_path"):
            with self.subTest(field=field):
                payload = skill_payload(model_requirements={"writing": {field: "run this"}})
                with self.assertRaises(SkillValidationError):
                    parse_skill_version(payload)

    def test_paths_and_schema_references_are_rejected(self) -> None:
        for schema in (
            {"default": "../secrets.txt"},
            {"default": "C:\\server\\secret"},
            {"$ref": "file:///etc/passwd"},
        ):
            with self.subTest(schema=schema), self.assertRaises(SkillValidationError):
                parse_skill_version(skill_payload(input_schema=schema))

    def test_boolean_is_not_accepted_as_version(self) -> None:
        with self.assertRaisesRegex(SkillValidationError, "positive integer"):
            parse_skill_version(skill_payload(version=True))

    def test_invalid_contracts_and_falsey_non_objects_are_rejected(self) -> None:
        with self.assertRaisesRegex(SkillValidationError, "invalid JSON Schema type"):
            parse_skill_version(skill_payload(input_schema={"type": "not-a-type"}))
        with self.assertRaisesRegex(SkillValidationError, "invalid JSON Schema type"):
            parse_skill_version(skill_payload(output_contract={"writing": {"type": 7}}))
        for field in ("output_contract", "model_requirements"):
            with self.subTest(field=field), self.assertRaisesRegex(
                SkillValidationError, "must be an object"
            ):
                parse_skill_version(skill_payload(**{field: False}))


if __name__ == "__main__":
    unittest.main()
