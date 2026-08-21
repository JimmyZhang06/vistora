from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry, Resource

from .errors import ValidationError


def discover_schema_dir() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "packages" / "contracts" / "schemas" / "v1"
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        "Contract schemas were not found. Set FRAMEFACTORY_CONTRACT_SCHEMA_DIR "
        "in packaged deployments."
    )


class ContractValidator:
    """Validates persisted API resources against the canonical JSON Schemas."""

    RESOURCE_SCHEMAS: ClassVar[dict[str, str]] = {
        "skill": "skill.schema.json",
        "skill_version": "skill-version.schema.json",
        "channel": "channel.schema.json",
        "run": "run.schema.json",
        "skill_test_execution": "skill-test-execution.schema.json",
    }

    def __init__(self, schema_dir: Path | None = None) -> None:
        self.schema_dir = schema_dir or discover_schema_dir()
        resources: list[tuple[str, Resource[Any]]] = []
        schemas: dict[str, dict[str, Any]] = {}
        for path in self.schema_dir.glob("*.schema.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name] = schema
            resources.append((schema["$id"], Resource.from_contents(schema)))
        registry = Registry().with_resources(resources)
        self._validators: dict[str, Validator] = {}
        for resource_name, filename in self.RESOURCE_SCHEMAS.items():
            schema = schemas[filename]
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            self._validators[resource_name] = validator_class(
                schema,
                registry=registry,
                format_checker=FormatChecker(),
            )

    def validate(self, resource_name: str, value: dict[str, Any]) -> None:
        validator = self._validators[resource_name]
        try:
            validator.validate(value)
        except JsonSchemaValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or "$"
            raise ValidationError(exc.message, path=path, resource=resource_name) from exc
