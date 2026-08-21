"""Strict JSON loading for SkillVersion snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import MAX_DOCUMENT_BYTES
from .errors import SkillValidationError, ValidationIssue
from .model import SkillVersion, parse_skill_version


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SkillValidationError(
                ValidationIssue(f"$.{key}", "duplicate_key", "duplicate JSON object key")
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise SkillValidationError(ValidationIssue("$", "non_finite_number", f"{value} is not valid JSON"))


def load_skill_version_json(document: str | bytes) -> SkillVersion:
    if isinstance(document, bytes):
        if len(document) > MAX_DOCUMENT_BYTES:
            raise SkillValidationError(
                ValidationIssue("$", "document_too_large", f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
            )
        try:
            document = document.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as error:
            raise SkillValidationError(ValidationIssue("$", "invalid_encoding", str(error))) from error
    elif not isinstance(document, str):
        raise SkillValidationError(ValidationIssue("$", "invalid_type", "document must be str or bytes"))
    try:
        encoded = document.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SkillValidationError(ValidationIssue("$", "invalid_encoding", str(error))) from error
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise SkillValidationError(
            ValidationIssue("$", "document_too_large", f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
        )
    try:
        value = json.loads(
            document,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_constant,
        )
    except SkillValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SkillValidationError(ValidationIssue("$", "invalid_json", str(error))) from error
    return parse_skill_version(value)


def load_skill_version_file(path: str | Path, *, trusted_root: str | Path) -> SkillVersion:
    """Read one explicitly selected JSON snapshot from a trusted application root."""

    root = Path(trusted_root).resolve(strict=True)
    candidate = Path(path)
    if candidate.is_symlink():
        raise SkillValidationError(ValidationIssue("$file", "symlink_forbidden", "symlinks are not allowed"))
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SkillValidationError(
            ValidationIssue("$file", "untrusted_path", "file must be contained by trusted_root")
        ) from error
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        raise SkillValidationError(ValidationIssue("$file", "invalid_file", "expected a regular .json file"))
    if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
        raise SkillValidationError(
            ValidationIssue("$file", "document_too_large", f"document exceeds {MAX_DOCUMENT_BYTES} bytes")
        )
    return load_skill_version_json(resolved.read_bytes())
