"""Fail-closed static contract gate for the governed asset lifecycle.

The gate deliberately reports missing parallel implementation as a release failure.
It does not mutate application data and writes only the requested JSON report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = Path(__file__).with_name("asset_release_contract.json")
DEFAULT_REPORT = ROOT / "artifacts" / "release" / "asset-contract-gate.json"


@dataclass(frozen=True, slots=True)
class Finding:
    capability: str
    component: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "capability": self.capability,
            "component": self.component,
            "message": self.message,
        }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read contract {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"contract {path} must contain a JSON object")
    return value


def _load_openapi(path: Path) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - caught in packaging/CI
        raise RuntimeError("PyYAML is required to run the asset contract gate") from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read OpenAPI contract {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise TypeError(f"OpenAPI contract {path} must contain an object")
    return value


def _parameter_names(
    operation: Mapping[str, Any],
    path_item: Mapping[str, Any],
    openapi: Mapping[str, Any],
) -> set[str]:
    components = openapi.get("components", {})
    components = components if isinstance(components, Mapping) else {}
    definitions = components.get("parameters", {})
    definitions = definitions if isinstance(definitions, Mapping) else {}
    parameters: list[Any] = []
    for owner in (path_item, operation):
        values = owner.get("parameters", [])
        if isinstance(values, list):
            parameters.extend(values)
    names: set[str] = set()
    for value in parameters:
        parameter = value if isinstance(value, Mapping) else {}
        reference = parameter.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/parameters/"):
            parameter = definitions.get(reference.rsplit("/", 1)[-1], {})
            parameter = parameter if isinstance(parameter, Mapping) else {}
        if parameter.get("in") == "query" and parameter.get("name"):
            names.add(str(parameter["name"]))
    return names


def _text(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _runtime_routes() -> set[tuple[str, str]]:
    api_source = ROOT / "apps" / "api" / "src"
    sys.path.insert(0, str(api_source))
    try:
        from framefactory_api.main import create_app
    except ImportError as exc:
        raise RuntimeError("API runtime dependencies are required by the contract gate") from exc
    app = create_app()
    return {
        (str(route.path), method.lower())
        for route in app.routes
        for method in getattr(route, "methods", set())
    }


def evaluate_contract(
    spec: Mapping[str, Any],
    *,
    openapi: Mapping[str, Any],
    openapi_text: str,
    database_text: str,
    worker_text: str,
    web_text: str,
    runtime_routes: set[tuple[str, str]] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    paths = openapi.get("paths", {})
    if not isinstance(paths, Mapping):
        paths = {}

    api = spec.get("api", {})
    for expectation in api.get("required_operations", []):
        path = str(expectation["path"])
        method = str(expectation["method"]).lower()
        path_item = paths.get(path)
        operation = path_item.get(method) if isinstance(path_item, Mapping) else None
        capability = f"{method.upper()} {path}"
        if not isinstance(operation, Mapping):
            findings.append(Finding(capability, "api", "operation is absent from OpenAPI"))
        else:
            required_parameters = set(expectation.get("parameters", []))
            missing_parameters = required_parameters - _parameter_names(
                operation, path_item, openapi
            )
            if missing_parameters:
                findings.append(
                    Finding(
                        capability,
                        "api",
                        f"missing query parameters: {sorted(missing_parameters)}",
                    )
                )
        if runtime_routes is not None and (path, method) not in runtime_routes:
            findings.append(
                Finding(capability, "api-runtime", "operation is absent from the FastAPI router")
            )

    for token in api.get("required_tokens", []):
        if str(token) not in openapi_text:
            findings.append(
                Finding(str(token), "api", "lifecycle token is absent from OpenAPI")
            )

    database = spec.get("database", {})
    for token in database.get("required_tokens", []):
        if str(token) not in database_text:
            findings.append(
                Finding(str(token), "database", "durable schema token is absent")
            )
    for pattern in database.get("forbidden_patterns", []):
        if re.search(str(pattern), database_text, flags=re.IGNORECASE | re.MULTILINE):
            findings.append(
                Finding(
                    str(pattern),
                    "database",
                    "tenant isolation is explicitly disabled by a migration",
                )
            )

    for component, text_value in (("worker", worker_text), ("web", web_text)):
        for token in spec.get(component, {}).get("required_tokens", []):
            if str(token) not in text_value:
                findings.append(
                    Finding(str(token), component, "required integration token is absent")
                )
    return findings


def run_gate(spec_path: Path = DEFAULT_SPEC) -> dict[str, Any]:
    spec = _load_json(spec_path)
    openapi_path = ROOT / "packages" / "contracts" / "openapi" / "v1.yaml"
    migrations = sorted((ROOT / "db" / "migrations").glob("*.sql"))
    worker_paths = [
        ROOT / "services" / "worker" / "framefactory" / "worker" / "adapters" / "database_assets.py",
        ROOT / "services" / "worker" / "framefactory" / "worker" / "adapters" / "postgres.py",
    ]
    web_paths = [
        ROOT / "apps" / "web" / "lib" / "api" / "adapter.ts",
        ROOT / "apps" / "web" / "lib" / "api" / "http-adapter.ts",
        ROOT / "apps" / "web" / "lib" / "api" / "contracts.ts",
        ROOT / "apps" / "web" / "components" / "asset-hub.tsx",
    ]
    openapi_text = openapi_path.read_text(encoding="utf-8")
    findings = evaluate_contract(
        spec,
        openapi=_load_openapi(openapi_path),
        openapi_text=openapi_text,
        database_text=_text(migrations),
        worker_text=_text(worker_paths),
        web_text=_text(web_paths),
        runtime_routes=_runtime_routes(),
    )
    component_counts = {
        component: sum(finding.component == component for finding in findings)
        for component in ("api", "api-runtime", "database", "worker", "web")
    }
    return {
        "result": "PASSED" if not findings else "FAILED",
        "schema_version": spec.get("schema_version"),
        "quarantine_expected_count": spec.get("quarantine_inventory", {}).get(
            "expected_count"
        ),
        "finding_count": len(findings),
        "component_finding_counts": component_counts,
        "findings": [finding.as_dict() for finding in findings],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = run_gate(arguments.spec)
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["result"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
