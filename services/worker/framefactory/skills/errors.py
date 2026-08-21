"""Errors raised by the declarative Skill engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A machine-readable validation failure."""

    path: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


class SkillValidationError(ValueError):
    """Raised when a SkillVersion is not safe or does not match the schema."""

    def __init__(self, issues: ValidationIssue | tuple[ValidationIssue, ...]):
        if isinstance(issues, ValidationIssue):
            issues = (issues,)
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


class MissingCapabilityError(RuntimeError):
    """Raised before execution when required capabilities are unavailable."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = tuple(missing)
        super().__init__(f"missing required capabilities: {', '.join(self.missing)}")
