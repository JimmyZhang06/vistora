"""OpenAPI v1 surface and official/user Skill isomorphism tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag

import pytest
import yaml
from conftest import CONTRACTS_ROOT, walk_json

CORE_COLLECTIONS = {
    "users",
    "workspaces",
    "channels",
    "skills",
    "skill-versions",
    "asset-libraries",
    "render-presets",
    "pipelines",
    "runs",
    "steps",
    "artifacts",
    "events",
}


def _load_openapi(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    document = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    assert isinstance(document, dict)
    return document


def _resolve_json_pointer(document: Any, fragment: str) -> Any:
    if not fragment:
        return document
    pointer = unquote(fragment)
    assert pointer.startswith("/"), f"unsupported non-JSON-Pointer fragment: #{fragment}"
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        else:
            assert isinstance(current, dict) and token in current, f"unresolved JSON Pointer token: {token}"
            current = current[token]
    return current


@pytest.fixture(scope="session")
def openapi_path() -> Path:
    openapi_root = CONTRACTS_ROOT / "openapi"
    candidates = sorted(
        path
        for pattern in ("**/*.yaml", "**/*.yml", "**/*.json")
        for path in openapi_root.glob(pattern)
    )
    assert len(candidates) == 1, f"expected exactly one OpenAPI document, found {candidates}"
    return candidates[0]


@pytest.fixture(scope="session")
def openapi(openapi_path: Path) -> dict[str, Any]:
    return _load_openapi(openapi_path)


def test_openapi_declares_v1_and_openapi_31(openapi: dict[str, Any]) -> None:
    assert str(openapi.get("openapi", "")).startswith("3.1"), "JSON Schema 2020-12 requires OpenAPI 3.1"
    assert re.match(r"^(v?1)(?:\.|$)", str(openapi.get("info", {}).get("version", "")))
    paths = openapi.get("paths")
    assert isinstance(paths, dict) and paths
    assert all(path.startswith("/v1/") for path in paths), "every public operation must be namespaced under /v1"


def test_every_openapi_reference_and_json_pointer_resolves(
    openapi: dict[str, Any], openapi_path: Path
) -> None:
    cache: dict[Path, Any] = {openapi_path.resolve(): openapi}
    contracts_root = CONTRACTS_ROOT.resolve()
    for node in walk_json(openapi):
        if not isinstance(node, dict) or not isinstance(node.get("$ref"), str):
            continue
        reference = node["$ref"]
        target_name, fragment = urldefrag(reference)
        if target_name:
            target_path = (openapi_path.parent / unquote(target_name)).resolve()
            assert target_path.is_relative_to(contracts_root), f"OpenAPI $ref escapes the contracts package: {reference}"
            assert target_path.is_file(), f"OpenAPI $ref target does not exist: {reference}"
            if target_path not in cache:
                cache[target_path] = _load_openapi(target_path)
            target = cache[target_path]
        else:
            target = openapi
        _resolve_json_pointer(target, fragment)


def test_operation_ids_are_present_and_unique(openapi: dict[str, Any]) -> None:
    operation_ids: list[str] = []
    for path, path_item in openapi["paths"].items():
        for method in ("get", "post", "put", "patch", "delete"):
            if method not in path_item:
                continue
            operation_id = path_item[method].get("operationId")
            assert isinstance(operation_id, str) and operation_id, f"{method.upper()} {path} needs operationId"
            operation_ids.append(operation_id)
    assert len(operation_ids) == len(set(operation_ids)), "OpenAPI operationId values must be unique"


def test_global_security_supports_bearer_and_workspace_api_keys(openapi: dict[str, Any]) -> None:
    schemes = openapi.get("components", {}).get("securitySchemes", {})
    assert schemes.get("bearerAuth", {}).get("type") == "http"
    assert schemes.get("workspaceApiKey", {}).get("type") == "apiKey"
    global_alternatives = openapi.get("security")
    assert isinstance(global_alternatives, list)
    names = {name for alternative in global_alternatives for name in alternative}
    assert {"bearerAuth", "workspaceApiKey"} <= names, (
        "global security must allow either a bearer token or a workspace API key"
    )


def test_workspace_header_is_reserved_and_optional_in_single_workspace_mode(
    openapi: dict[str, Any],
) -> None:
    parameter = openapi["components"]["parameters"]["WorkspaceHeader"]
    assert parameter["required"] is False
    description = parameter["description"].lower()
    assert "single-workspace" in description
    assert "configured workspace" in description


def test_openapi_exposes_all_core_resource_collections(openapi: dict[str, Any]) -> None:
    paths = set(openapi["paths"])
    missing = sorted(resource for resource in CORE_COLLECTIONS if f"/v1/{resource}" not in paths)
    assert not missing, f"missing OpenAPI v1 core collection paths: {missing}"


def test_skill_create_read_and_version_routes_share_one_model(openapi: dict[str, Any]) -> None:
    paths = openapi["paths"]
    assert "post" in paths["/v1/skills"] and "get" in paths["/v1/skills"]
    assert "/v1/skills/{skill_id}" in paths
    assert "/v1/skill-versions" in paths

    skill_refs = {
        node["$ref"]
        for node in walk_json(paths["/v1/skills"])
        if isinstance(node, dict) and isinstance(node.get("$ref"), str) and "skill" in node["$ref"].lower()
    }
    assert skill_refs, "shared Skill collection must reference the canonical Skill contracts"


def test_official_and_user_skills_have_no_parallel_api_or_schema(openapi: dict[str, Any]) -> None:
    special_paths = [
        path
        for path in openapi["paths"]
        if "skill" in path.lower() and re.search(r"(?:official|system|builtin|user)[-_]?skills?", path.lower())
    ]
    assert not special_paths, f"official/user Skills must use /v1/skills, not {special_paths}"

    schemas = openapi.get("components", {}).get("schemas", {})
    split_models = [
        name
        for name in schemas
        if "skill" in name.lower() and re.search(r"(?:official|system|builtin|userskill)", name.lower())
    ]
    assert not split_models, f"official/user Skills must use the same component schemas: {split_models}"


def test_channel_routes_are_crud_and_revision_fenced(openapi: dict[str, Any]) -> None:
    collection = openapi["paths"]["/v1/channels"]
    detail = openapi["paths"]["/v1/channels/{channel_id}"]
    assert {"get", "post"} <= set(collection)
    assert {"get", "put", "delete"} <= set(detail)
    assert collection["post"]["requestBody"]["$ref"].endswith("/ChannelWrite")
    assert any(
        parameter.get("$ref", "").endswith("/IdempotencyKey")
        for parameter in collection["post"]["parameters"]
    )
    list_parameters = {
        parameter["$ref"].rsplit("/", 1)[-1]
        for parameter in collection["get"]["parameters"]
        if "$ref" in parameter
    }
    assert {"ChannelSearch", "ChannelPlatform", "ChannelStatus"} <= list_parameters
    for operation in (detail["put"], detail["delete"]):
        assert any(
            parameter.get("$ref", "").endswith("/IfMatch")
            for parameter in operation["parameters"]
        )
        assert "428" in operation["responses"]
    assert all("Reserved" not in operation.get("tags", []) for operation in collection.values())


def test_channel_filter_parameters_match_runtime_contract(openapi: dict[str, Any]) -> None:
    parameters = openapi["components"]["parameters"]
    assert parameters["ChannelSearch"]["name"] == "search"
    assert parameters["ChannelPlatform"]["name"] == "platform"
    assert parameters["ChannelStatus"]["name"] == "status"
    assert parameters["ChannelStatus"]["schema"]["enum"] == [
        "active",
        "paused",
        "archived",
    ]
