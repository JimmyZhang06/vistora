"""Immutable, declarative SkillVersion model and validation."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .canonical import (
    MAX_STRING_LENGTH,
    FrozenMap,
    normalize_json,
    normalize_markdown,
    normalize_text,
    sha256_content_hash,
    thaw_json,
)
from .capabilities import STAGES, CapabilityManifest
from .errors import SkillValidationError, ValidationIssue

SCHEMA_VERSION = "1.0"
STATES = {"draft", "validating", "ready", "published", "deprecated", "rejected"}
_SKILL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PATH_VALUE_RE = re.compile(
    r"(?:^|[\\/])\.\.(?:[\\/]|$)|^[a-zA-Z]:|^(?:\\\\|//)|^~[\\/]|^/|^(?:file|data|javascript):",
    re.IGNORECASE,
)
_DANGEROUS_KEYS = {
    "code", "source_code", "python", "python_source", "javascript", "javascript_source", "script",
    "executable", "plugin", "command", "cmd", "shell",
    "entrypoint", "hook", "hooks", "handler", "callable", "module", "import", "eval", "exec",
    "environment", "env", "secret", "secrets", "credential", "credentials", "token",
    "path", "file_path", "filepath", "filename", "directory", "cwd", "root",
}
_MODEL_REQUIREMENT_FIELDS = {
    "provider", "model", "modalities", "structured_output", "temperature", "max_output_tokens",
    "context_window", "reasoning_effort",
}
_JSON_SCHEMA_TYPES = {"null", "boolean", "object", "array", "number", "integer", "string"}
_JSON_SCHEMA_FIELDS = {
    "$schema", "type", "properties", "required", "additionalProperties", "items", "prefixItems",
    "enum", "const", "description", "title", "default", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern", "format", "minItems",
    "maxItems", "uniqueItems", "minProperties", "maxProperties", "allOf", "anyOf", "oneOf", "not",
}
_TOP_LEVEL_FIELDS = {
    "schema_version", "skill_id", "version", "name", "description", "state", "lineage",
    "input_schema", "research_policy", "writing_instructions", "visual_policy", "asset_policy",
    "qc_rubric", "output_contract", "model_requirements", "capabilities", "content_hash",
}


def _fail(path: str, code: str, message: str) -> None:
    raise SkillValidationError(ValidationIssue(path, code, message))


def _mapping(value: object, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "invalid_type", "must be an object")
    return value


def _strict_fields(value: Mapping[str, Any], allowed: set[str], *, path: str) -> None:
    if not all(isinstance(field, str) for field in value):
        _fail(path, "non_string_key", "object keys must be strings")
    unknown = set(value) - allowed
    if unknown:
        field = min(unknown)
        code = "dangerous_field" if normalize_text(field).lower() in _DANGEROUS_KEYS else "unknown_field"
        _fail(f"{path}.{field}", code, "field is not allowed")


def _string(value: object, *, path: str, allow_empty: bool = True, markdown: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, "invalid_type", "must be a string")
    normalized = normalize_markdown(value) if markdown else normalize_text(value, strip=True)
    if not allow_empty and not normalized:
        _fail(path, "empty_value", "must not be empty")
    if len(normalized) > MAX_STRING_LENGTH:
        _fail(path, "string_too_long", f"string exceeds {MAX_STRING_LENGTH} characters")
    if "\x00" in normalized:
        _fail(path, "nul_character", "NUL characters are not allowed")
    if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        _fail(path, "control_character", "control characters other than newline and tab are not allowed")
    return normalized


def _string_tuple(value: object, *, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _fail(path, "invalid_type", "must be an array")
    return tuple(_string(item, path=f"{path}[{index}]", allow_empty=False) for index, item in enumerate(value))


def _validate_machine_json(value: object, *, path: str) -> FrozenMap:
    normalized = normalize_json(value, path=path)
    if not isinstance(normalized, FrozenMap):
        _fail(path, "invalid_type", "must be an object")

    def visit(node: object, node_path: str) -> None:
        if isinstance(node, FrozenMap):
            for key, child in node.items():
                lowered = key.casefold()
                if key in {"$ref", "$dynamicRef", "$id"}:
                    _fail(f"{node_path}.{key}", "external_reference", "schema references are not allowed")
                if lowered in _DANGEROUS_KEYS:
                    _fail(f"{node_path}.{key}", "dangerous_field", "executable and server-path fields are forbidden")
                visit(child, f"{node_path}.{key}")
        elif isinstance(node, tuple):
            for index, child in enumerate(node):
                visit(child, f"{node_path}[{index}]")
        elif isinstance(node, str) and _PATH_VALUE_RE.search(node.strip()):
            _fail(node_path, "dangerous_path", "server paths, traversal, and active URI schemes are forbidden")

    visit(normalized, path)
    return normalized


def _validate_json_schema(value: object, *, path: str) -> FrozenMap:
    """Validate the safe JSON Schema subset used by the first worker kernel."""

    normalized = normalize_json(value, path=path)
    if not isinstance(normalized, FrozenMap):
        _fail(path, "invalid_type", "must be an object")

    def schema(node: object, node_path: str) -> None:
        if not isinstance(node, FrozenMap):
            _fail(node_path, "invalid_schema", "schema must be an object")
        unknown = set(node) - _JSON_SCHEMA_FIELDS - {"$ref", "$dynamicRef", "$id"}
        if unknown:
            field = min(unknown)
            code = "dangerous_field" if field.casefold() in _DANGEROUS_KEYS else "unknown_schema_field"
            _fail(f"{node_path}.{field}", code, "JSON Schema field is not allowed")
        for forbidden in ("$ref", "$dynamicRef", "$id"):
            if forbidden in node:
                _fail(f"{node_path}.{forbidden}", "external_reference", "schema references are not allowed")
        declared_type = node.get("type")
        if declared_type is not None:
            types = (declared_type,) if isinstance(declared_type, str) else declared_type
            if not isinstance(types, tuple) or not types or any(item not in _JSON_SCHEMA_TYPES for item in types):
                _fail(f"{node_path}.type", "invalid_schema", "invalid JSON Schema type")
        properties = node.get("properties")
        if properties is not None:
            if not isinstance(properties, FrozenMap):
                _fail(f"{node_path}.properties", "invalid_schema", "properties must be an object")
            for key, child in properties.items():
                schema(child, f"{node_path}.properties.{key}")
        for key in ("items", "not", "additionalProperties"):
            child = node.get(key)
            if child is not None and type(child) is not bool:
                schema(child, f"{node_path}.{key}")
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = node.get(key)
            if children is not None:
                if not isinstance(children, tuple):
                    _fail(f"{node_path}.{key}", "invalid_schema", "must be an array of schemas")
                for index, child in enumerate(children):
                    schema(child, f"{node_path}.{key}[{index}]")
        required = node.get("required")
        if required is not None and (
            not isinstance(required, tuple) or any(not isinstance(item, str) for item in required)
        ):
            _fail(f"{node_path}.required", "invalid_schema", "required must be a string array")
        default = node.get("default")
        if isinstance(default, str) and _PATH_VALUE_RE.search(default.strip()):
            _fail(f"{node_path}.default", "dangerous_path", "server paths are forbidden in schema defaults")

    schema(normalized, path)
    return normalized


def _validate_model_requirements(value: object) -> FrozenMap:
    data = _mapping({} if value is None else value, path="$.model_requirements")
    unknown_stages = set(data) - set(STAGES)
    if unknown_stages:
        field = min(unknown_stages)
        _fail(f"$.model_requirements.{field}", "unknown_stage", "unknown stage")
    normalized: dict[str, Any] = {}
    for stage, raw in data.items():
        stage_path = f"$.model_requirements.{stage}"
        requirements = _mapping(raw, path=stage_path)
        _strict_fields(requirements, _MODEL_REQUIREMENT_FIELDS, path=stage_path)
        for key, item in requirements.items():
            if isinstance(item, str) and _PATH_VALUE_RE.search(item.strip()):
                _fail(f"{stage_path}.{key}", "dangerous_path", "server paths and active URI schemes are forbidden")
            if key in {"provider", "model", "reasoning_effort"} and not isinstance(item, str):
                _fail(f"{stage_path}.{key}", "invalid_type", "must be a string")
            if key == "modalities" and (
                not isinstance(item, (list, tuple)) or any(not isinstance(entry, str) for entry in item)
            ):
                _fail(f"{stage_path}.{key}", "invalid_type", "must be a string array")
            if key == "structured_output" and type(item) is not bool:
                _fail(f"{stage_path}.{key}", "invalid_type", "must be a boolean")
            if key == "temperature" and (
                type(item) not in {int, float} or not 0 <= item <= 2
            ):
                _fail(f"{stage_path}.{key}", "invalid_range", "must be a number from 0 to 2")
            if key in {"max_output_tokens", "context_window"} and (
                type(item) is not int or item < 1
            ):
                _fail(f"{stage_path}.{key}", "invalid_range", "must be a positive integer")
        normalized[stage] = requirements
    result = normalize_json(normalized, path="$.model_requirements")
    assert isinstance(result, FrozenMap)
    return result


@dataclass(frozen=True, slots=True)
class Lineage:
    kind: str = "original"
    parent_skill_id: str | None = None
    parent_version: int | None = None
    parent_content_hash: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> Lineage:
        if value is None:
            return cls()
        data = _mapping(value, path="$.lineage")
        _strict_fields(data, {"kind", "parent_skill_id", "parent_version", "parent_content_hash"}, path="$.lineage")
        kind = _string(data.get("kind", "original"), path="$.lineage.kind", allow_empty=False)
        if kind not in {"original", "fork", "legacy"}:
            _fail("$.lineage.kind", "invalid_enum", "must be original, fork, or legacy")
        parent_skill_id = data.get("parent_skill_id")
        if parent_skill_id is not None:
            parent_skill_id = _validated_skill_id(parent_skill_id, path="$.lineage.parent_skill_id")
        parent_version = data.get("parent_version")
        if parent_version is not None and (type(parent_version) is not int or parent_version < 1):
            _fail("$.lineage.parent_version", "invalid_version", "must be a positive integer")
        parent_hash = data.get("parent_content_hash")
        if parent_hash is not None:
            parent_hash = _string(parent_hash, path="$.lineage.parent_content_hash", allow_empty=False)
            if not _HASH_RE.fullmatch(parent_hash):
                _fail("$.lineage.parent_content_hash", "invalid_hash", "must be a sha256 content hash")
        if kind == "fork" and not (parent_skill_id and parent_version and parent_hash):
            _fail("$.lineage", "incomplete_lineage", "fork lineage requires parent skill, version, and content hash")
        return cls(kind, parent_skill_id, parent_version, parent_hash)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.parent_skill_id is not None:
            result["parent_skill_id"] = self.parent_skill_id
        if self.parent_version is not None:
            result["parent_version"] = self.parent_version
        if self.parent_content_hash is not None:
            result["parent_content_hash"] = self.parent_content_hash
        return result


@dataclass(frozen=True, slots=True)
class ResearchPolicy:
    instructions: str = ""
    source_requirements: tuple[str, ...] = ()
    fact_boundaries: tuple[str, ...] = ()
    min_sources: int = 0
    citations_required: bool = False

    @classmethod
    def from_mapping(cls, value: object) -> ResearchPolicy:
        if value is None:
            return cls()
        data = _mapping(value, path="$.research_policy")
        allowed = {"instructions", "source_requirements", "fact_boundaries", "min_sources", "citations_required"}
        _strict_fields(data, allowed, path="$.research_policy")
        min_sources = data.get("min_sources", 0)
        if type(min_sources) is not int or min_sources < 0 or min_sources > 100:
            _fail("$.research_policy.min_sources", "invalid_range", "must be an integer from 0 to 100")
        citations = data.get("citations_required", False)
        if type(citations) is not bool:
            _fail("$.research_policy.citations_required", "invalid_type", "must be a boolean")
        return cls(
            instructions=_string(data.get("instructions", ""), path="$.research_policy.instructions", markdown=True),
            source_requirements=_string_tuple(data.get("source_requirements"), path="$.research_policy.source_requirements"),
            fact_boundaries=_string_tuple(data.get("fact_boundaries"), path="$.research_policy.fact_boundaries"),
            min_sources=min_sources,
            citations_required=citations,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instructions": self.instructions,
            "source_requirements": list(self.source_requirements),
            "fact_boundaries": list(self.fact_boundaries),
            "min_sources": self.min_sources,
            "citations_required": self.citations_required,
        }


@dataclass(frozen=True, slots=True)
class InstructionBlock:
    instructions: str = ""
    constraints: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object, *, path: str) -> InstructionBlock:
        if value is None:
            return cls()
        if isinstance(value, str):
            return cls(instructions=_string(value, path=path, markdown=True))
        data = _mapping(value, path=path)
        _strict_fields(data, {"instructions", "constraints"}, path=path)
        return cls(
            instructions=_string(data.get("instructions", ""), path=f"{path}.instructions", markdown=True),
            constraints=_string_tuple(data.get("constraints"), path=f"{path}.constraints"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"instructions": self.instructions, "constraints": list(self.constraints)}


@dataclass(frozen=True, slots=True)
class VisualPolicy:
    instructions: str = ""
    constraints: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> VisualPolicy:
        if value is None:
            return cls()
        if isinstance(value, str):
            return cls(instructions=_string(value, path="$.visual_policy", markdown=True))
        data = _mapping(value, path="$.visual_policy")
        _strict_fields(data, {"instructions", "constraints", "categories"}, path="$.visual_policy")
        return cls(
            instructions=_string(data.get("instructions", ""), path="$.visual_policy.instructions", markdown=True),
            constraints=_string_tuple(data.get("constraints"), path="$.visual_policy.constraints"),
            categories=_string_tuple(data.get("categories"), path="$.visual_policy.categories"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instructions": self.instructions,
            "constraints": list(self.constraints),
            "categories": list(self.categories),
        }


@dataclass(frozen=True, slots=True)
class AssetPolicy:
    allowed_source_types: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> AssetPolicy:
        if value is None:
            return cls()
        data = _mapping(value, path="$.asset_policy")
        _strict_fields(data, {"allowed_source_types", "constraints"}, path="$.asset_policy")
        return cls(
            allowed_source_types=_string_tuple(data.get("allowed_source_types"), path="$.asset_policy.allowed_source_types"),
            constraints=_string_tuple(data.get("constraints"), path="$.asset_policy.constraints"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"allowed_source_types": list(self.allowed_source_types), "constraints": list(self.constraints)}


@dataclass(frozen=True, slots=True)
class QCCriterion:
    name: str
    description: str
    severity: str = "error"

    @classmethod
    def from_mapping(cls, value: object, *, index: int) -> QCCriterion:
        path = f"$.qc_rubric.criteria[{index}]"
        data = _mapping(value, path=path)
        _strict_fields(data, {"name", "description", "severity"}, path=path)
        severity = _string(data.get("severity", "error"), path=f"{path}.severity", allow_empty=False)
        if severity not in {"error", "warning"}:
            _fail(f"{path}.severity", "invalid_enum", "must be error or warning")
        return cls(
            name=_string(data.get("name", ""), path=f"{path}.name", allow_empty=False),
            description=_string(data.get("description", ""), path=f"{path}.description", markdown=True),
            severity=severity,
        )

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class QCRubric:
    instructions: str = ""
    criteria: tuple[QCCriterion, ...] = ()

    @classmethod
    def from_mapping(cls, value: object) -> QCRubric:
        if value is None:
            return cls()
        if isinstance(value, str):
            return cls(instructions=_string(value, path="$.qc_rubric", markdown=True))
        data = _mapping(value, path="$.qc_rubric")
        _strict_fields(data, {"instructions", "criteria"}, path="$.qc_rubric")
        raw_criteria = data.get("criteria", ())
        if not isinstance(raw_criteria, (list, tuple)):
            _fail("$.qc_rubric.criteria", "invalid_type", "must be an array")
        return cls(
            instructions=_string(data.get("instructions", ""), path="$.qc_rubric.instructions", markdown=True),
            criteria=tuple(QCCriterion.from_mapping(item, index=index) for index, item in enumerate(raw_criteria)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"instructions": self.instructions, "criteria": [item.to_dict() for item in self.criteria]}


def _validated_skill_id(value: object, *, path: str = "$.skill_id") -> str:
    skill_id = _string(value, path=path, allow_empty=False)
    if not _SKILL_ID_RE.fullmatch(skill_id):
        _fail(path, "invalid_skill_id", "must be a lowercase kebab-case identifier")
    return skill_id


def _stage_schema_mapping(value: object, *, path: str) -> FrozenMap:
    data = _mapping({} if value is None else value, path=path)
    unknown = set(data) - set(STAGES)
    if unknown:
        field = min(unknown)
        _fail(f"{path}.{field}", "unknown_stage", "unknown stage")
    normalized: dict[str, Any] = {}
    for stage, child in data.items():
        normalized[stage] = _validate_json_schema(child, path=f"{path}.{stage}")
    return normalize_json(normalized, path=path)


@dataclass(frozen=True, slots=True)
class SkillVersion:
    schema_version: str
    skill_id: str
    version: int
    name: str
    description: str
    state: str
    lineage: Lineage
    input_schema: FrozenMap
    research_policy: ResearchPolicy
    writing_instructions: InstructionBlock
    visual_policy: VisualPolicy
    asset_policy: AssetPolicy
    qc_rubric: QCRubric
    output_contract: FrozenMap
    model_requirements: FrozenMap
    capabilities: CapabilityManifest
    content_hash: str

    @classmethod
    def from_mapping(cls, value: object) -> SkillVersion:
        data = _mapping(value, path="$")
        _strict_fields(data, _TOP_LEVEL_FIELDS, path="$")
        schema_version = _string(data.get("schema_version", SCHEMA_VERSION), path="$.schema_version", allow_empty=False)
        if schema_version != SCHEMA_VERSION:
            _fail("$.schema_version", "unsupported_schema", f"only schema version {SCHEMA_VERSION} is supported")
        skill_id = _validated_skill_id(data.get("skill_id"))
        version = data.get("version")
        if type(version) is not int or version < 1:
            _fail("$.version", "invalid_version", "must be a positive integer")
        name = _string(data.get("name", ""), path="$.name", allow_empty=False)
        state = _string(data.get("state", "draft"), path="$.state", allow_empty=False)
        if state not in STATES:
            _fail("$.state", "invalid_enum", f"must be one of {sorted(STATES)}")

        instance = cls(
            schema_version=schema_version,
            skill_id=skill_id,
            version=version,
            name=name,
            description=_string(data.get("description", ""), path="$.description", markdown=True),
            state=state,
            lineage=Lineage.from_mapping(data.get("lineage")),
            input_schema=_validate_json_schema(data.get("input_schema", {}), path="$.input_schema"),
            research_policy=ResearchPolicy.from_mapping(data.get("research_policy")),
            writing_instructions=InstructionBlock.from_mapping(
                data.get("writing_instructions"), path="$.writing_instructions"
            ),
            visual_policy=VisualPolicy.from_mapping(data.get("visual_policy")),
            asset_policy=AssetPolicy.from_mapping(data.get("asset_policy")),
            qc_rubric=QCRubric.from_mapping(data.get("qc_rubric")),
            output_contract=_stage_schema_mapping(data.get("output_contract", {}), path="$.output_contract"),
            model_requirements=_validate_model_requirements(data.get("model_requirements")),
            capabilities=CapabilityManifest.from_mapping(data.get("capabilities")),
            content_hash="",
        )
        computed_hash = instance.compute_content_hash()
        supplied_hash = data.get("content_hash")
        if supplied_hash is not None:
            supplied_hash = _string(supplied_hash, path="$.content_hash", allow_empty=False)
            if not _HASH_RE.fullmatch(supplied_hash) or not hmac.compare_digest(supplied_hash, computed_hash):
                _fail("$.content_hash", "hash_mismatch", "content_hash does not match executable content")
        return replace(instance, content_hash=computed_hash)

    def executable_payload(self) -> dict[str, Any]:
        """Return the identity-free data that fully determines execution behavior."""

        return {
            "schema_version": self.schema_version,
            "input_schema": thaw_json(self.input_schema),
            "research_policy": self.research_policy.to_dict(),
            "writing_instructions": self.writing_instructions.to_dict(),
            "visual_policy": self.visual_policy.to_dict(),
            "asset_policy": self.asset_policy.to_dict(),
            "qc_rubric": self.qc_rubric.to_dict(),
            "output_contract": thaw_json(self.output_contract),
            "model_requirements": thaw_json(self.model_requirements),
            "capabilities": self.capabilities.to_dict(),
        }

    def content_payload(self) -> dict[str, Any]:
        """Return semantic SkillVersion content, excluding identity and lineage metadata."""

        return {"description": self.description, **self.executable_payload()}

    def compute_content_hash(self) -> str:
        return sha256_content_hash(self.content_payload())

    def require_integrity(self) -> SkillVersion:
        computed = self.compute_content_hash()
        if not _HASH_RE.fullmatch(self.content_hash) or not hmac.compare_digest(self.content_hash, computed):
            _fail("$.content_hash", "hash_mismatch", "SkillVersion object was modified or constructed unsafely")
        return self

    def to_dict(self, *, include_content_hash: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "state": self.state,
            "lineage": self.lineage.to_dict(),
            **self.executable_payload(),
        }
        if include_content_hash:
            result["content_hash"] = self.content_hash
        return result


def parse_skill_version(value: object) -> SkillVersion:
    return SkillVersion.from_mapping(value)


def normalize_skill_version(value: SkillVersion | object) -> dict[str, Any]:
    version = value if isinstance(value, SkillVersion) else parse_skill_version(value)
    return version.to_dict()


def compute_content_hash(value: SkillVersion | object) -> str:
    version = value if isinstance(value, SkillVersion) else parse_skill_version(value)
    return version.compute_content_hash()
