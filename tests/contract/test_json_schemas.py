"""Versioned JSON Schema and SkillVersion safety-contract tests."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin

import pytest
from conftest import CONTRACTS_ROOT, normalize_identifier, walk_json
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
REQUIRED_RESOURCES = {
    "user",
    "workspace",
    "channel",
    "skill",
    "skillversion",
    "assetlibrary",
    "renderpreset",
    "pipeline",
    "run",
    "step",
    "artifact",
    "event",
    "narrationtiming",
    "candidatemanifest",
    "generatedmaterialmanifest",
    "materialselection",
    "editdecisionlist",
}
RESOURCE_SCHEMA_NAMES = {
    "user.schema.json",
    "workspace.schema.json",
    "channel.schema.json",
    "skill.schema.json",
    "skill-version.schema.json",
    "asset-library.schema.json",
    "render-preset.schema.json",
    "pipeline.schema.json",
    "run.schema.json",
    "step.schema.json",
    "artifact.schema.json",
    "event.schema.json",
    "narration-timing.schema.json",
    "candidate-manifest.schema.json",
    "generated-material-manifest.schema.json",
    "material-selection.schema.json",
    "edit-decision-list.schema.json",
}


def _schema_label(path: Path, schema: dict[str, Any]) -> str:
    return normalize_identifier(" ".join((path.stem, str(schema.get("title", "")), str(schema.get("$id", "")))))


def _validator(schema: dict[str, Any], store: dict[str, dict[str, Any]]) -> Draft202012Validator:
    registry = Registry().with_resources(
        (schema_id, Resource.from_contents(document)) for schema_id, document in store.items()
    )
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def _schema_nodes_with_examples(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for node in walk_json(schema):
        if isinstance(node, dict) and isinstance(node.get("examples"), list):
            yield node


def _declared_property_names(schema: Any) -> set[str]:
    result: set[str] = set()
    for node in walk_json(schema):
        if isinstance(node, dict) and isinstance(node.get("properties"), dict):
            result.update(str(name) for name in node["properties"])
    return result


def _find_skill_version(schemas: dict[Path, dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    matches = [
        (path, schema)
        for path, schema in schemas.items()
        if path.name == "skill-version.schema.json"
    ]
    assert len(matches) == 1, f"expected exactly one SkillVersion schema, found {[str(p) for p, _ in matches]}"
    return matches[0]


def test_all_required_versioned_resource_schemas_exist(schemas: dict[Path, dict[str, Any]]) -> None:
    labels = {resource: [] for resource in REQUIRED_RESOURCES}
    for path, schema in schemas.items():
        label = _schema_label(path, schema)
        for resource in REQUIRED_RESOURCES:
            if resource in label:
                labels[resource].append(path)
    missing = sorted(resource for resource, matches in labels.items() if not matches)
    assert not missing, f"missing resource schemas: {missing}"


@pytest.mark.parametrize("expected_draft", [DRAFT_2020_12])
def test_schemas_are_valid_draft_2020_12(
    schemas: dict[Path, dict[str, Any]], expected_draft: str
) -> None:
    for path, schema in schemas.items():
        assert schema.get("$schema") == expected_draft, f"{path} must explicitly use Draft 2020-12"
        assert "/v1/" in path.as_posix(), f"{path} is not stored in a v1 directory"
        Draft202012Validator.check_schema(schema)


def test_schema_ids_are_absolute_versioned_and_unique(schemas: dict[Path, dict[str, Any]]) -> None:
    ids: list[str] = []
    for path, schema in schemas.items():
        schema_id = schema.get("$id")
        assert isinstance(schema_id, str) and re.match(r"^https?://", schema_id), f"{path} needs an absolute $id"
        assert "/v1/" in schema_id, f"{path} $id must encode contract v1"
        ids.append(schema_id)
    assert len(ids) == len(set(ids)), "JSON Schema $id values must be unique"


def test_pipeline_and_artifact_enums_cover_timing_driven_editing(
    schemas: dict[Path, dict[str, Any]],
) -> None:
    enums = next(schema for path, schema in schemas.items() if path.name == "enums.schema.json")
    artifact_kinds = set(enums["$defs"]["ArtifactKind"]["enum"])
    assert {
        "manifest",
        "asset",
        "narration_timing",
        "candidate_manifest",
        "generated_material_manifest",
        "material_selection",
        "timeline",
    } <= artifact_kinds

    pipeline = next(schema for path, schema in schemas.items() if path.name == "pipeline.schema.json")
    operations = set(pipeline["$defs"]["PipelineNode"]["properties"]["operation"]["enum"])
    assert {"media.retrieve", "timeline.align", "render.edl"} <= operations


def test_editing_artifact_contracts_expose_implementation_boundaries(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    by_name = {path.name: schema for path, schema in schemas.items()}

    narration = by_name["narration-timing.schema.json"]
    assert {"audio_content_hash", "script_content_hash", "segments", "beats"} <= set(
        narration["required"]
    )

    candidates = by_name["candidate-manifest.schema.json"]
    assert {"coverage", "beats", "materialized_assets"} <= set(
        candidates["required"]
    )
    branches = candidates["allOf"][0]["oneOf"]
    assert any("acquisition" in branch.get("required", []) for branch in branches)
    assert any("generation" in branch.get("required", []) for branch in branches)
    acquisition = candidates["$defs"]["AcquisitionAudit"]
    assert {"provider_errors_total", "provider_errors_truncated"} <= set(
        acquisition["required"]
    )
    assert acquisition["properties"]["provider_errors"]["maxItems"] == 100
    assert candidates["properties"]["materialized_assets"]["maxItems"] == 100000

    generated = by_name["generated-material-manifest.schema.json"]
    assert {
        "plan",
        "source_audio_artifact_id",
        "terms_snapshot",
        "rights_snapshot",
        "pricing_snapshot",
        "safety",
        "total_cost",
        "jobs",
    } <= set(generated["required"])
    generated_job = generated["$defs"]["GenerationJob"]
    assert {
        "paid_operation_id",
        "provider_task_id",
        "request_hash",
        "prompt_hash",
        "prompt_text",
        "prompt_template_version",
        "reference_hashes",
        "output_content_hash",
        "duration_seconds",
        "incurred_cost_credits",
        "visual_verification",
    } <= set(generated_job["required"])
    candidate = candidates["$defs"]["Candidate"]
    assert {
        "asset_kind",
        "source_duration_ms",
        "source_window",
        "hard_constraints",
        "cut_evidence",
        "rights_evidence",
    } <= set(candidate["required"])
    assert {
        "evidence_required",
        "evidence_present",
        "verified",
        "verification_basis",
        "rejection_codes",
    } <= set(candidates["$defs"]["RightsEvidence"]["required"])
    missing_acquisition = copy.deepcopy(candidates["examples"][0])
    missing_acquisition.pop("acquisition")
    assert not _validator(candidates, schema_store).is_valid(missing_acquisition)
    candidate_validator = _validator(candidates, schema_store)
    retrieved = copy.deepcopy(candidates["examples"][0])
    generated_candidate = copy.deepcopy(candidates["examples"][1])
    assert candidate_validator.is_valid(retrieved)
    assert candidate_validator.is_valid(generated_candidate)

    generated_validator = _validator(generated, schema_store)
    generated_example = copy.deepcopy(generated["examples"][0])
    assert generated_validator.is_valid(generated_example)
    missing_audio_lineage = copy.deepcopy(generated_example)
    missing_audio_lineage.pop("source_audio_artifact_id")
    assert not generated_validator.is_valid(missing_audio_lineage)
    missing_rights_attestation = copy.deepcopy(generated_example)
    missing_rights_attestation.pop("rights_snapshot")
    assert not generated_validator.is_valid(missing_rights_attestation)
    accepted_unsafe = copy.deepcopy(generated_example)
    accepted_unsafe["jobs"][0]["visual_verification"]["brand_or_logo"] = True
    assert not generated_validator.is_valid(accepted_unsafe)
    accepted_safety_rejected = copy.deepcopy(generated_example)
    accepted_safety_rejected["jobs"][0]["safety"] = {
        "status": "rejected",
        "provider_moderation": "not_rejected",
        "failure_code": "brand_or_logo_detected",
    }
    assert not generated_validator.is_valid(accepted_safety_rejected)

    missing_generation = copy.deepcopy(generated_candidate)
    missing_generation.pop("generation")
    assert not candidate_validator.is_valid(missing_generation)

    generated_with_retrieval_audit = copy.deepcopy(generated_candidate)
    generated_with_retrieval_audit["retrieval_policy"] = retrieved["retrieval_policy"]
    generated_with_retrieval_audit["acquisition"] = retrieved["acquisition"]
    assert not candidate_validator.is_valid(generated_with_retrieval_audit)

    generated_with_catalog_identity = copy.deepcopy(generated_candidate)
    generated_with_catalog_identity["catalog_snapshot_id"] = (
        "99999999-9999-4999-8999-999999999999"
    )
    assert not candidate_validator.is_valid(generated_with_catalog_identity)

    unknown_operation = copy.deepcopy(generated_candidate)
    unknown_operation["operation"] = "media.search"
    assert not candidate_validator.is_valid(unknown_operation)

    selection = by_name["material-selection.schema.json"]
    assert "selections" in selection["required"]

    edl = by_name["edit-decision-list.schema.json"]
    assert {"output_frame_rate", "subtitle_cues", "edl_policy", "shots"} <= set(edl["required"])
    assert edl["properties"]["title"]["maxLength"] == 1000
    assert (
        selection["properties"]["selections"]["maxItems"]
        == edl["properties"]["shots"]["maxItems"]
        == 100000
    )
    shot_required = set(edl["$defs"]["Shot"]["required"])
    assert {
        "timeline_start_frame",
        "timeline_end_frame",
        "source_start_ms",
        "source_end_ms",
    } <= shot_required

    for schema in (narration, candidates, generated, selection, edl):
        undeclared = copy.deepcopy(schema["examples"][0])
        undeclared["implementation_detail"] = True
        assert not _validator(schema, schema_store).is_valid(undeclared)

    edl_validator = _validator(edl, schema_store)
    maximum_title = copy.deepcopy(edl["examples"][0])
    maximum_title["title"] = "标" * 1000
    assert edl_validator.is_valid(maximum_title)
    maximum_title["title"] += "题"
    assert not edl_validator.is_valid(maximum_title)
    for field, value in (
        ("padding_seconds", 0.1),
        ("speed", 1.01),
        ("loop", True),
    ):
        invalid_video = copy.deepcopy(edl["examples"][0])
        invalid_video["shots"][0][field] = value
        assert not edl_validator.is_valid(invalid_video), (
            f"video EDL must reject {field}={value!r}"
        )

    invalid_image = copy.deepcopy(edl["examples"][0])
    invalid_image["shots"][0]["media_kind"] = "image"
    invalid_image["shots"][0]["loop"] = False
    assert not edl_validator.is_valid(invalid_image), "image EDL must require loop=true"

    valid_image = copy.deepcopy(invalid_image)
    valid_image["shots"][0]["loop"] = True
    assert edl_validator.is_valid(valid_image), "image EDL must allow loop=true"


def test_all_local_schema_references_resolve(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    registry = Registry().with_resources(
        (schema_id, Resource.from_contents(document)) for schema_id, document in schema_store.items()
    )
    for path, schema in schemas.items():
        resolver = registry.resolver(schema["$id"])
        for node in walk_json(schema):
            if not isinstance(node, dict) or not isinstance(node.get("$ref"), str):
                continue
            ref = node["$ref"]
            external_id, _ = urldefrag(ref)
            if external_id:
                resolved_id = urljoin(schema["$id"], external_id)
                assert resolved_id in schema_store, f"{path} has an unbundled or unknown $ref: {ref}"
            resolver.lookup(ref)


def test_every_resource_schema_has_valid_examples(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    for path, schema in schemas.items():
        if path.name not in RESOURCE_SCHEMA_NAMES:
            continue
        examples = schema.get("examples")
        assert isinstance(examples, list) and examples, f"{path} needs at least one root example"
        validator = _validator(schema, schema_store)
        for index, example in enumerate(examples):
            errors = sorted(validator.iter_errors(example), key=lambda error: list(error.absolute_path))
            assert not errors, f"{path} example {index} invalid: {errors[0].message if errors else ''}"


def test_all_declared_examples_validate_against_their_subschema(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    for path, schema in schemas.items():
        for node in _schema_nodes_with_examples(schema):
            validator = _validator(node | {"$id": schema["$id"]}, schema_store)
            for example in node["examples"]:
                errors = list(validator.iter_errors(example))
                assert not errors, f"{path} contains an invalid nested example: {errors[0].message if errors else ''}"


def test_skill_version_is_closed_and_declarative(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    path, schema = _find_skill_version(schemas)
    required = set(schema.get("required", []))
    expected = {
        "input_schema",
        "research_policy",
        "writing_policy",
        "visual_policy",
        "asset_policy",
        "qc_policy",
        "capability_requirements",
        "output_contract",
        "content_hash",
    }
    assert expected <= required, f"{path} missing required declarative fields: {sorted(expected - required)}"
    assert schema.get("additionalProperties") is False, "SkillVersion must reject undeclared executable/path fields"

    properties = schema.get("properties", {})
    execution_kind = properties.get("execution_kind", {})
    assert execution_kind.get("const") == "declarative", "SkillVersion execution_kind must be fixed to declarative"
    hash_contract = properties.get("content_hash", {})
    assert hash_contract.get("readOnly") is True, "content_hash must be marked immutable/readOnly"
    hash_schema = hash_contract
    if isinstance(hash_contract.get("$ref"), str):
        registry = Registry().with_resources(
            (schema_id, Resource.from_contents(document)) for schema_id, document in schema_store.items()
        )
        hash_schema = registry.resolver(schema["$id"]).lookup(hash_contract["$ref"]).contents
    assert isinstance(hash_schema.get("pattern"), str), "content_hash needs an explicit digest pattern"

    forbidden = {"code", "script", "path", "file_path", "module", "command", "runtime", "entrypoint"}
    declared = _declared_property_names(schema)
    assert not forbidden.intersection(declared), f"{path} exposes executable/path properties: {forbidden & declared}"

    example = copy.deepcopy(schema["examples"][0])
    validator = _validator(schema, schema_store)
    example["code"] = "raise SystemExit"
    assert not validator.is_valid(example), "SkillVersion must reject arbitrary code fields"

    bad_hash = copy.deepcopy(schema["examples"][0])
    bad_hash["content_hash"] = "mutable-or-not-a-digest"
    assert not validator.is_valid(bad_hash), "SkillVersion must reject malformed content hashes"


def test_skill_version_hash_canonicalization_is_documented(
    schemas: dict[Path, dict[str, Any]]
) -> None:
    _, schema = _find_skill_version(schemas)
    hash_contract = schema["properties"]["content_hash"]
    description = str(hash_contract.get("description", "")).lower()
    identifies_canonicalization = "canonical" in description or bool(
        re.search(r"(?:rfc\s*8785|\bjcs\b)", description)
    )
    assert identifies_canonicalization and (
        "immutable" in description or hash_contract.get("readOnly") is True
    )

    readme = (CONTRACTS_ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert re.search(r"(?:rfc\s*8785|json\s+canonicalization\s+scheme|\bjcs\b)", readme), (
        "contracts README must identify RFC 8785/JCS canonical JSON"
    )
    assert re.search(r"sha[ -]?256", readme), "contracts README must identify SHA-256"
    assert re.search(r"exclud|omit|not\s+(?:part|included|hashed|covered)", readme), (
        "contracts README must state which metadata is excluded from content_hash"
    )
    excluded_metadata = {"id", "workspace_id", "skill_id", "version", "state", "created_at", "published_at"}
    missing = sorted(field for field in excluded_metadata if f"`{field}`" not in readme)
    assert not missing, f"content_hash documentation does not explicitly exclude metadata: {missing}"


def test_channel_default_composition_matches_web_contract_and_excludes_secrets(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]]
) -> None:
    channel = next(schema for path, schema in schemas.items() if path.name == "channel.schema.json")
    composition = channel["$defs"]["DefaultComposition"]
    assert set(composition["properties"]) == {
        "skill_version_id",
        "pipeline_version_id",
        "asset_library_ids",
        "voice_profile_id",
        "render_preset_version_id",
    }
    assert set(composition["required"]) == set(composition["properties"])
    assert "platform_connection_id" in channel["properties"]
    declared = _declared_property_names(channel)
    assert not {"secret", "token", "password", "credential", "api_key"}.intersection(declared)

    run_create = next(
        schema for path, schema in schemas.items() if path.name == "run-create.schema.json"
    )
    validator = _validator(run_create, schema_store)
    assert validator.is_valid(
        {
            "channel_id": "33333333-3333-4333-8333-333333333333",
            "input": {"topic": "Resolve Channel defaults"},
        }
    )
