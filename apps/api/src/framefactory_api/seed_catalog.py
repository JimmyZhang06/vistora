from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import rfc8785
from jsonschema import FormatChecker
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from .contracts import ContractValidator

SKILL_VERSION_HASH_FIELDS = (
    "input_schema",
    "research_policy",
    "writing_policy",
    "visual_policy",
    "asset_policy",
    "qc_policy",
    "capability_requirements",
    "output_contract",
    "default_pipeline_version_id",
)
PIPELINE_HASH_FIELDS = ("nodes", "capability_requirements")


@dataclass(frozen=True, slots=True)
class OfficialPipelineSeed:
    """Validated Pipeline identity plus the public version contract document."""

    pipeline_id: str
    version: dict[str, Any]


class OfficialSkillVersions(list[dict[str, Any]]):
    """Backward-compatible version list carrying validated Pipeline seeds.

    ``load_official_catalog`` remains destructurable as ``skills, versions`` so the
    existing application composition root needs no special-case wiring. Persistence
    adapters can read ``pipelines`` from this sequence during bootstrap.
    """

    def __init__(
        self,
        values: list[dict[str, Any]],
        *,
        pipelines: tuple[OfficialPipelineSeed, ...],
    ) -> None:
        super().__init__(values)
        self.pipelines = pipelines


def discover_official_seed_manifest() -> Path | None:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = (
            parent
            / "packages"
            / "seeds"
            / "official-skills"
            / "v1"
            / "manifest.json"
        )
        if candidate.is_file():
            return candidate
    return None


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Official seed must contain a JSON object: {path}")
    return value


def _resolve_entry(package_root: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Official seed entry path must be a non-empty string")
    candidate = (package_root / raw_path).resolve()
    try:
        candidate.relative_to(package_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Official seed path escapes its package: {raw_path}") from exc
    return candidate


def _content_hash(value: dict[str, Any], fields: tuple[str, ...]) -> str:
    try:
        domain = {field: value[field] for field in fields}
    except KeyError as exc:
        raise RuntimeError(f"Official seed is missing hash field: {exc.args[0]}") from exc
    return hashlib.sha256(rfc8785.dumps(domain)).hexdigest()


def _pipeline_validator(validator: ContractValidator) -> Validator:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    for path in validator.schema_dir.glob("*.schema.json"):
        schema = _read_object(path)
        schemas[path.name] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    schema = schemas["pipeline.schema.json"]
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(
        schema,
        registry=Registry().with_resources(resources),
        format_checker=FormatChecker(),
    )


def _validate_pipeline_graph(pipeline: dict[str, Any]) -> None:
    nodes = pipeline["nodes"]
    keys = [node["key"] for node in nodes]
    if len(keys) != len(set(keys)):
        raise RuntimeError("Official Pipeline node keys must be unique")
    known = set(keys)
    dependencies = {node["key"]: tuple(node["depends_on"]) for node in nodes}
    for key, values in dependencies.items():
        unknown = set(values) - known
        if unknown:
            raise RuntimeError(
                f"Official Pipeline node {key} has unknown dependencies: {sorted(unknown)}"
            )

    visited: set[str] = set()
    active: set[str] = set()

    def visit(key: str) -> None:
        if key in active:
            raise RuntimeError("Official Pipeline graph must be acyclic")
        if key in visited:
            return
        active.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        active.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


def load_official_catalog(
    validator: ContractValidator,
    manifest_path: Path | None = None,
) -> tuple[list[dict[str, Any]], OfficialSkillVersions]:
    """Load validated system-owned Skills through the same repository model as user Skills."""

    resolved_manifest = manifest_path or discover_official_seed_manifest()
    if resolved_manifest is None:
        return [], OfficialSkillVersions([], pipelines=())

    manifest = _read_object(resolved_manifest)
    package_root = resolved_manifest.parent
    publisher = manifest.get("publisher")
    entries = manifest.get("entries")
    if not isinstance(publisher, dict) or not isinstance(entries, list):
        raise RuntimeError("Official seed manifest is missing publisher or entries")

    workspace_id = publisher.get("workspace_id")
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise RuntimeError("Official seed manifest is missing its identity strategy")
    try:
        namespace = UUID(str(identity["namespace_uuid"]))
    except (KeyError, ValueError) as exc:
        raise RuntimeError("Official seed namespace UUID is invalid") from exc
    pipeline_hash_strategy = manifest.get("pipeline_content_hash")
    if not isinstance(pipeline_hash_strategy, dict) or pipeline_hash_strategy.get(
        "algorithm"
    ) != "sha256":
        raise RuntimeError("Official seed Pipeline hash strategy must use SHA-256")
    if pipeline_hash_strategy.get("canonicalization") != "RFC 8785 JCS" or (
        pipeline_hash_strategy.get("included_fields") != list(PIPELINE_HASH_FIELDS)
    ):
        raise RuntimeError("Official seed Pipeline hash domain must match the contract")
    pipeline_entries = manifest.get("pipelines")
    if not isinstance(pipeline_entries, list) or not pipeline_entries:
        raise RuntimeError("Official seed manifest must declare at least one Pipeline")
    pipeline_contract_validator = _pipeline_validator(validator)
    pipelines: list[OfficialPipelineSeed] = []
    pipeline_version_ids: set[str] = set()
    for entry in pipeline_entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Official Pipeline manifest entries must be objects")
        pipeline = _read_object(_resolve_entry(package_root, entry.get("version")))
        errors = sorted(
            pipeline_contract_validator.iter_errors(pipeline),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise RuntimeError(
                f"Official Pipeline violates pipeline.schema.json at {path}: {error.message}"
            )
        if pipeline["ownership_type"] != "system":
            raise RuntimeError("Official Pipelines must use system ownership metadata")
        if pipeline["workspace_id"] != workspace_id:
            raise RuntimeError("Official Pipelines must use the manifest system workspace")
        slug = entry.get("slug")
        version_number = pipeline["version"]
        expected_pipeline_id = str(uuid5(namespace, f"pipeline:{slug}"))
        expected_version_id = str(
            uuid5(namespace, f"pipeline-version:{slug}:{version_number}")
        )
        if entry.get("pipeline_id") != expected_pipeline_id:
            raise RuntimeError("Official Pipeline ID is not deterministic")
        if entry.get("version_id") != expected_version_id or pipeline["id"] != expected_version_id:
            raise RuntimeError("Official Pipeline version ID differs from the manifest")
        digest = _content_hash(pipeline, PIPELINE_HASH_FIELDS)
        if pipeline["content_hash"] != digest or entry.get("expected_content_hash") != digest:
            raise RuntimeError("Official Pipeline content hash differs from canonical content")
        _validate_pipeline_graph(pipeline)
        pipeline_version_ids.add(pipeline["id"])
        pipelines.append(OfficialPipelineSeed(str(entry.get("pipeline_id")), pipeline))

    skills: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Official seed manifest entries must be objects")
        skill = _read_object(_resolve_entry(package_root, entry.get("skill")))
        version = _read_object(_resolve_entry(package_root, entry.get("version")))
        validator.validate("skill", skill)
        validator.validate("skill_version", version)
        if skill["ownership_type"] != "system" or version["ownership_type"] != "system":
            raise RuntimeError("Official catalog resources must use system ownership metadata")
        if skill["workspace_id"] != workspace_id or version["workspace_id"] != workspace_id:
            raise RuntimeError("Official catalog resources must use the manifest system workspace")
        if version["skill_id"] != skill["id"]:
            raise RuntimeError("Official SkillVersion does not belong to its manifest Skill")
        if version["default_pipeline_version_id"] not in pipeline_version_ids:
            raise RuntimeError("Official SkillVersion must reference a packaged Pipeline")
        digest = _content_hash(version, SKILL_VERSION_HASH_FIELDS)
        if version["content_hash"] != digest or entry.get("expected_content_hash") != digest:
            raise RuntimeError("Official SkillVersion content hash differs from canonical content")
        skills.append(skill)
        versions.append(version)

    return skills, OfficialSkillVersions(versions, pipelines=tuple(pipelines))
