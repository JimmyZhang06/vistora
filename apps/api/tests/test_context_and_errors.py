from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from framefactory_api.main import create_app


def test_health_and_context_are_single_workspace(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "framefactory-api",
        "persistence": "process_memory",
    }

    default_context = client.get("/v1/context").json()
    hinted_context = client.get(
        "/v1/context", headers={"X-Workspace-Id": str(uuid4())}
    ).json()
    assert default_context == hinted_context
    assert default_context["mode"] == "single_workspace"
    assert default_context["workspace_header_required"] is False
    assert default_context["multi_tenant_features_enabled"] is False


def test_request_errors_use_control_api_error_shape(client: TestClient) -> None:
    response = client.get("/v1/skills/not-a-uuid")
    assert response.status_code == 422
    body = response.json()
    assert body["schema_version"] == "1.0.0"
    assert body["code"] == "REQUEST_VALIDATION_FAILED"
    assert body["request_id"] == response.headers["X-Request-Id"]


def test_development_cors_allows_browser_adapter_headers(client: TestClient) -> None:
    response = client.options(
        "/v1/skills",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key,x-workspace-id",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    exposed = client.get(
        "/v1/context", headers={"Origin": "http://localhost:3000"}
    ).headers["access-control-expose-headers"]
    assert {"etag", "location", "x-request-id"} <= {
        value.strip().lower() for value in exposed.split(",")
    }


def test_invalid_cursor_uses_contract_error(client: TestClient) -> None:
    response = client.get("/v1/skills", params={"cursor": "not-a-cursor"})
    assert response.status_code == 422
    assert response.json()["code"] == "CONTRACT_VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/users"),
        ("post", "/v1/workspaces"),
        ("post", "/v1/render-presets"),
        ("get", "/v1/pipelines"),
    ],
)
def test_future_resources_are_explicitly_reserved(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 501
    assert response.json()["code"] == "CAPABILITY_UNAVAILABLE"
    assert response.json()["details"]["mode"] == "single_workspace"


def test_implemented_v1_routes_are_declared_in_the_canonical_openapi() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "contracts"
        / "openapi"
        / "v1.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    app = create_app()
    live_operations: set[tuple[str, str]] = set()
    for route in app.routes:
        if not route.path.startswith("/v1/"):
            continue
        declared = contract["paths"].get(route.path)
        assert declared is not None, f"canonical OpenAPI is missing {route.path}"
        for method in route.methods or ():
            if method == "HEAD":
                continue
            live_operations.add((route.path, method.lower()))
            assert method.lower() in declared, (
                f"canonical OpenAPI is missing {method} {route.path}"
            )
    canonical_operations = {
        (path, method)
        for path, item in contract["paths"].items()
        for method in ("get", "post", "put", "patch", "delete")
        if method in item
    }
    assert canonical_operations == live_operations
