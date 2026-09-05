"""Public discovery contracts distinguish metadata from usable note identities."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def validator(
    schemas: dict[Path, dict[str, Any]],
    schema_store: dict[str, dict[str, Any]],
    name: str,
) -> tuple[Draft202012Validator, dict[str, Any]]:
    schema = next(value for path, value in schemas.items() if path.name == name)
    registry = Registry().with_resources(
        (key, Resource.from_contents(value)) for key, value in schema_store.items()
    )
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()), schema


def test_recovery_request_accepts_optional_refresh_but_rejects_credentials(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]],
) -> None:
    check, schema = validator(schemas, schema_store, "benchmark-account-preview.schema.json")
    request = copy.deepcopy(schema["examples"][0])
    assert check.is_valid(request)
    assert check.is_valid(request | {"refresh_note_identity": True})
    assert not check.is_valid(request | {"refresh_note_identity": "true"})
    for forbidden in ("xsec_token", "cookie", "media_url", "cdp_url"):
        assert not check.is_valid(request | {forbidden: "must-not-enter-contract"})


def test_partial_discovery_can_preserve_unknown_card_metadata(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]],
) -> None:
    check, schema = validator(schemas, schema_store, "benchmark-account-snapshot.schema.json")
    snapshot = copy.deepcopy(schema["examples"][0])
    missing = copy.deepcopy(snapshot["notes"][0])
    missing.update(sample_index=2, note_id=None, identity_status="missing", format="unknown", published_at=None)
    snapshot["notes"].append(missing)
    snapshot["acquisition"].update(
        note_identity_status="partial", unresolved_note_count=1,
        identity_error_code="BENCHMARK_NOTE_IDENTITY_INCOMPLETE",
    )
    snapshot["analysis"].update(sample_size=2, unknown_count=1, video_share_percent=50)
    assert check.is_valid(snapshot)
    snapshot["notes"][1]["xsec_token"] = "must-not-enter-response"
    assert not check.is_valid(snapshot)


@pytest.mark.parametrize("field", [
    "discovery_version", "note_identity_status", "identified_note_count",
    "unresolved_note_count", "identity_error_code",
])
def test_old_response_without_discovery_capabilities_is_detectable(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]], field: str,
) -> None:
    check, schema = validator(schemas, schema_store, "benchmark-account-snapshot.schema.json")
    snapshot = copy.deepcopy(schema["examples"][0])
    del snapshot["acquisition"][field]
    assert not check.is_valid(snapshot)


@pytest.mark.parametrize(("field", "value"), [
    ("discovery_version", "2"), ("note_identity_status", "ready"),
    ("identified_note_count", -1), ("unresolved_note_count", -1),
])
def test_discovery_diagnostics_reject_invalid_values(
    schemas: dict[Path, dict[str, Any]], schema_store: dict[str, dict[str, Any]],
    field: str, value: Any,
) -> None:
    check, schema = validator(schemas, schema_store, "benchmark-account-snapshot.schema.json")
    snapshot = copy.deepcopy(schema["examples"][0])
    snapshot["acquisition"][field] = value
    assert not check.is_valid(snapshot)
