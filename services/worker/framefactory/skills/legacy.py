"""Read-only adapter for the legacy manifest/formula/usage triplet."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import MAX_DOCUMENT_BYTES, normalize_markdown
from .errors import SkillValidationError, ValidationIssue
from .loader import _duplicate_rejecting_object, _reject_constant
from .model import SkillVersion, parse_skill_version

LEGACY_FILES = ("manifest.json", "写作公式.md", "USAGE.md")
_LEGACY_REQUIRED = {
    "skill_id", "name", "version", "origin", "confidence", "赛道", "内容类型", "适用主题",
    "表达风格", "目标受众", "视频规格", "禁用范围",
}
_LEGACY_OPTIONAL = {"parent_version", "distilled_at", "corpus_ref"}
_LEGACY_DANGEROUS_KEYS = {
    "code", "script", "python", "javascript", "command", "cmd", "shell", "entrypoint",
    "hook", "handler", "module", "plugin", "path", "file_path", "formula_path", "directory", "cwd",
}


def _legacy_error(path: str, code: str, message: str) -> None:
    raise SkillValidationError(ValidationIssue(path, code, message))


def _read_exact_file(root: Path, filename: str) -> str:
    candidate = root / filename
    if candidate.is_symlink():
        _legacy_error(f"$legacy.{filename}", "symlink_forbidden", "legacy files must not be symlinks")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        _legacy_error(f"$legacy.{filename}", "missing_file", "legacy triplet is incomplete")
        raise AssertionError from error
    if resolved.parent != root or not resolved.is_file():
        _legacy_error(f"$legacy.{filename}", "untrusted_path", "legacy file escaped the selected directory")
    raw = resolved.read_bytes()
    if len(raw) > MAX_DOCUMENT_BYTES:
        _legacy_error(f"$legacy.{filename}", "document_too_large", "legacy file exceeds size limit")
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise SkillValidationError(
            ValidationIssue(f"$legacy.{filename}", "invalid_encoding", str(error))
        ) from error


def _legacy_list(manifest: Mapping[str, Any], key: str) -> list[str]:
    value = manifest.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        _legacy_error(f"$legacy.manifest.{key}", "invalid_type", "must be a non-empty string array")
    return value


def adapt_legacy_triplet(manifest: Mapping[str, Any], formula: str, usage: str) -> SkillVersion:
    """Convert already-read legacy data without retaining any filesystem references."""

    missing = sorted(_LEGACY_REQUIRED - set(manifest))
    if missing:
        _legacy_error("$legacy.manifest", "missing_field", f"missing required fields: {', '.join(missing)}")
    for field in manifest:
        if not isinstance(field, str):
            _legacy_error("$legacy.manifest", "non_string_key", "manifest keys must be strings")
        if field in _LEGACY_DANGEROUS_KEYS:
            _legacy_error(f"$legacy.manifest.{field}", "dangerous_field", "executable and path fields are forbidden")
        if field not in _LEGACY_REQUIRED | _LEGACY_OPTIONAL and not field.startswith("_note_"):
            _legacy_error(f"$legacy.manifest.{field}", "unknown_field", "legacy field is not allowed")
    if manifest.get("origin") not in {"route_A", "route_B"}:
        _legacy_error("$legacy.manifest.origin", "invalid_enum", "must be route_A or route_B")
    confidence = manifest.get("confidence")
    if type(confidence) not in {int, float} or not 0 <= confidence <= 1:
        _legacy_error("$legacy.manifest.confidence", "invalid_range", "must be a number from 0 to 1")
    version = manifest.get("version")
    if type(version) is not int or version < 1:
        _legacy_error("$legacy.manifest.version", "invalid_version", "must be a positive integer")

    tracks = _legacy_list(manifest, "赛道")
    content_types = _legacy_list(manifest, "内容类型")
    topics = _legacy_list(manifest, "适用主题")
    styles = _legacy_list(manifest, "表达风格")
    audiences = _legacy_list(manifest, "目标受众")
    prohibited = _legacy_list(manifest, "禁用范围")
    video_spec = manifest.get("视频规格")
    if not isinstance(video_spec, Mapping):
        _legacy_error("$legacy.manifest.视频规格", "invalid_type", "must be an object")

    parent_version = manifest.get("parent_version")
    if parent_version is not None and (type(parent_version) is not int or parent_version < 1):
        _legacy_error("$legacy.manifest.parent_version", "invalid_version", "must be null or a positive integer")

    description = normalize_markdown(usage)
    legacy_context = (
        f"赛道：{', '.join(tracks)}\n内容类型：{', '.join(content_types)}\n"
        f"适用主题：{', '.join(topics)}\n表达风格：{', '.join(styles)}\n"
        f"目标受众：{', '.join(audiences)}\n视频规格："
        + json.dumps(video_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    lineage: dict[str, Any] = {
        "kind": "legacy",
        "parent_skill_id": manifest["skill_id"],
    }
    if parent_version is not None:
        lineage["parent_version"] = parent_version

    return parse_skill_version(
        {
            "schema_version": "1.0",
            "skill_id": manifest["skill_id"],
            "version": version,
            "name": manifest["name"],
            "description": description,
            "state": "draft",
            "lineage": lineage,
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
                "additionalProperties": True,
            },
            "research_policy": {
                "instructions": legacy_context,
                "fact_boundaries": prohibited,
            },
            "writing_instructions": {
                "instructions": formula,
                "constraints": prohibited,
            },
            "visual_policy": {},
            "asset_policy": {},
            "qc_rubric": {
                "instructions": formula,
                "criteria": [
                    {
                        "name": "legacy-formula-compliance",
                        "description": "The result follows the declared writing method and avoids every prohibited scope.",
                        "severity": "error",
                    }
                ]
            },
            "output_contract": {
                "writing": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "sentences": {"type": "array"},
                    },
                    "required": ["title", "sentences"],
                }
            },
            "model_requirements": {},
            "capabilities": {},
        }
    )


def load_legacy_skill(directory: str | Path, *, trusted_root: str | Path) -> SkillVersion:
    """Read only the exact legacy triplet; never scan, import, or execute sibling files."""

    selected = Path(directory)
    if selected.is_symlink():
        _legacy_error("$legacy", "symlink_forbidden", "legacy directory must not be a symlink")
    try:
        root = selected.resolve(strict=True)
    except FileNotFoundError as error:
        raise SkillValidationError(ValidationIssue("$legacy", "missing_directory", str(error))) from error
    if not root.is_dir():
        _legacy_error("$legacy", "invalid_directory", "expected a directory")
    trusted = Path(trusted_root).resolve(strict=True)
    try:
        root.relative_to(trusted)
    except ValueError as error:
        raise SkillValidationError(
            ValidationIssue("$legacy", "untrusted_path", "legacy directory must be inside trusted_root")
        ) from error

    manifest_text = _read_exact_file(root, "manifest.json")
    formula = _read_exact_file(root, "写作公式.md")
    usage = _read_exact_file(root, "USAGE.md")
    try:
        manifest = json.loads(
            manifest_text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_constant,
        )
    except SkillValidationError:
        raise
    except json.JSONDecodeError as error:
        raise SkillValidationError(
            ValidationIssue("$legacy.manifest", "invalid_json", str(error))
        ) from error
    if not isinstance(manifest, Mapping):
        _legacy_error("$legacy.manifest", "invalid_type", "manifest must be an object")
    return adapt_legacy_triplet(manifest, normalize_markdown(formula), normalize_markdown(usage))
