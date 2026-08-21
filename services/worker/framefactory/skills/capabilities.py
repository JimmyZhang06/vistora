"""Capability declarations and identity-free capability resolution."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .canonical import normalize_text
from .errors import MissingCapabilityError, SkillValidationError, ValidationIssue

STAGES = ("research", "writing", "visual", "qc")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")


def normalize_capability(value: str, *, path: str = "$.capabilities") -> str:
    if not isinstance(value, str):
        raise SkillValidationError(ValidationIssue(path, "invalid_type", "capability must be a string"))
    normalized = normalize_text(value, strip=True).lower()
    if not _CAPABILITY_RE.fullmatch(normalized):
        raise SkillValidationError(
            ValidationIssue(path, "invalid_capability", "use a dotted lowercase capability name; wildcards are forbidden")
        )
    return normalized


def _capability_tuple(values: object, *, path: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise SkillValidationError(ValidationIssue(path, "invalid_type", "must be an array"))
    return tuple(sorted({normalize_capability(value, path=f"{path}[{index}]") for index, value in enumerate(values)}))


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = _capability_tuple(self.required, path="$.capabilities.required")
        optional = _capability_tuple(self.optional, path="$.capabilities.optional")
        overlap = tuple(sorted(set(required) & set(optional)))
        if overlap:
            raise SkillValidationError(
                ValidationIssue("$.capabilities", "capability_overlap", f"required and optional overlap: {', '.join(overlap)}")
            )
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "optional", optional)

    @classmethod
    def from_mapping(cls, value: object, *, path: str) -> CapabilityDeclaration:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise SkillValidationError(ValidationIssue(path, "invalid_type", "must be an object"))
        unknown = set(value) - {"required", "optional"}
        if unknown:
            field = min(unknown)
            raise SkillValidationError(ValidationIssue(f"{path}.{field}", "unknown_field", "field is not allowed"))
        required = _capability_tuple(value.get("required"), path=f"{path}.required")
        optional = _capability_tuple(value.get("optional"), path=f"{path}.optional")
        overlap = tuple(sorted(set(required) & set(optional)))
        if overlap:
            raise SkillValidationError(
                ValidationIssue(path, "capability_overlap", f"required and optional overlap: {', '.join(overlap)}")
            )
        return cls(required=required, optional=optional)

    def to_dict(self) -> dict[str, list[str]]:
        return {"required": list(self.required), "optional": list(self.optional)}


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    research: CapabilityDeclaration = CapabilityDeclaration()
    writing: CapabilityDeclaration = CapabilityDeclaration()
    visual: CapabilityDeclaration = CapabilityDeclaration()
    qc: CapabilityDeclaration = CapabilityDeclaration()

    @classmethod
    def from_mapping(cls, value: object, *, path: str = "$.capabilities") -> CapabilityManifest:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise SkillValidationError(ValidationIssue(path, "invalid_type", "must be an object"))
        unknown = set(value) - set(STAGES)
        if unknown:
            field = min(unknown)
            raise SkillValidationError(ValidationIssue(f"{path}.{field}", "unknown_stage", "unknown stage"))
        declarations = {
            stage: CapabilityDeclaration.from_mapping(value.get(stage), path=f"{path}.{stage}")
            for stage in STAGES
        }
        return cls(**declarations)

    def for_stage(self, stage: str) -> CapabilityDeclaration:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        return getattr(self, stage)

    def to_dict(self) -> dict[str, dict[str, list[str]]]:
        return {stage: self.for_stage(stage).to_dict() for stage in STAGES}


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    required: tuple[str, ...]
    optional: tuple[str, ...]
    available: tuple[str, ...]
    enabled_required: tuple[str, ...]
    enabled_optional: tuple[str, ...]
    missing_required: tuple[str, ...]

    @property
    def satisfied(self) -> bool:
        return not self.missing_required

    def require_satisfied(self) -> CapabilityResolution:
        if self.missing_required:
            raise MissingCapabilityError(self.missing_required)
        return self


def resolve_capabilities(
    declaration: CapabilityDeclaration,
    available: Iterable[str],
) -> CapabilityResolution:
    """Resolve a declaration using exact names only; no IDs or implicit fallbacks."""

    normalized_available = tuple(
        sorted({normalize_capability(value, path="$.available_capabilities") for value in available})
    )
    available_set = set(normalized_available)
    required_set = set(declaration.required)
    optional_set = set(declaration.optional)
    return CapabilityResolution(
        required=declaration.required,
        optional=declaration.optional,
        available=normalized_available,
        enabled_required=tuple(sorted(required_set & available_set)),
        enabled_optional=tuple(sorted(optional_set & available_set)),
        missing_required=tuple(sorted(required_set - available_set)),
    )
