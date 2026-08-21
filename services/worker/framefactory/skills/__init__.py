"""Public API for FrameFactory's declarative Skill execution kernel."""

from .capabilities import (
    CapabilityDeclaration,
    CapabilityManifest,
    CapabilityResolution,
    resolve_capabilities,
)
from .compiler import (
    CompiledSkillInstructions,
    StageInstructions,
    compile_qc,
    compile_research,
    compile_skill,
    compile_stage,
    compile_visual,
    compile_writing,
)
from .errors import MissingCapabilityError, SkillValidationError, ValidationIssue
from .legacy import adapt_legacy_triplet, load_legacy_skill
from .loader import load_skill_version_file, load_skill_version_json
from .model import (
    SCHEMA_VERSION,
    SkillVersion,
    compute_content_hash,
    normalize_skill_version,
    parse_skill_version,
)

__all__ = [
    "SCHEMA_VERSION",
    "CapabilityDeclaration",
    "CapabilityManifest",
    "CapabilityResolution",
    "CompiledSkillInstructions",
    "MissingCapabilityError",
    "SkillValidationError",
    "SkillVersion",
    "StageInstructions",
    "ValidationIssue",
    "adapt_legacy_triplet",
    "compile_qc",
    "compile_research",
    "compile_skill",
    "compile_stage",
    "compile_visual",
    "compile_writing",
    "compute_content_hash",
    "load_legacy_skill",
    "load_skill_version_file",
    "load_skill_version_json",
    "normalize_skill_version",
    "parse_skill_version",
    "resolve_capabilities",
]
