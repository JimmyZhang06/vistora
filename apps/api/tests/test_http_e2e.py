from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = REPOSITORY_ROOT / "apps" / "api"
API_SOURCE = API_ROOT / "src"
SCHEMA_ROOT = REPOSITORY_ROOT / "packages" / "contracts" / "schemas" / "v1"
CANONICAL_VERSION_FIELDS = (
    "input_schema",
    "research_policy",
    "writing_policy",
    "visual_policy",
    "asset_policy",
    "qc_policy",
    "capability_requirements",
    "output_contract",
    "default_pipeline_version_id",
)


def _contract_example(filename: str) -> dict[str, Any]:
    schema = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
    return deepcopy(schema["examples"][0])


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture(scope="module")
def http_client() -> Iterator[httpx.Client]:
    """Run the packaged ASGI application in another process over a real TCP socket."""
    port = _unused_tcp_port()
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(API_SOURCE), existing_pythonpath) if part
    )
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "framefactory_api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False)
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                pytest.fail(f"uvicorn exited before becoming healthy:\n{output}")
            try:
                response = client.get("/healthz")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("uvicorn did not become healthy on its TCP port within 15 seconds")

        yield client
    finally:
        client.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _idempotency(operation: str) -> dict[str, str]:
    return {"Idempotency-Key": f"http-e2e-{operation}-0001"}


def _assert_success(response: httpx.Response, status_code: int) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    return response.json()


def _skill_create_payload(slug: str) -> dict[str, Any]:
    return {
        "publisher_name": "HTTP E2E Studio",
        "name": "HTTP E2E Skill",
        "slug": slug,
        "description": "Created without any server-generated resource fields.",
        "visibility": "private",
    }


def _canonical_version_payload(skill_id: str, version: str) -> dict[str, Any]:
    example = _contract_example("skill-version.schema.json")
    payload = {field: example[field] for field in CANONICAL_VERSION_FIELDS}
    payload.update(
        skill_id=skill_id,
        version=version,
        test_topics=["How real HTTP boundaries prevent mock drift"],
        release_notes="Created by the real-port end-to-end test.",
    )
    return payload


def _create_skill(client: httpx.Client, *, slug: str, operation: str) -> tuple[dict[str, Any], str]:
    response = client.post(
        "/v1/skills", json=_skill_create_payload(slug), headers=_idempotency(operation)
    )
    skill = _assert_success(response, 201)
    assert skill["revision"] == 1
    assert response.headers["etag"] == '"1"'
    return skill, response.headers["etag"]


def _create_version(
    client: httpx.Client,
    *,
    skill_id: str,
    version: str,
    operation: str,
    source_version_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    if source_version_id is None:
        payload = _canonical_version_payload(skill_id, version)
    else:
        payload = {
            "skill_id": skill_id,
            "version": version,
            "source_version_id": source_version_id,
            "test_topics": ["Cloned draft over HTTP"],
            "release_notes": "Cloned through the public input DTO.",
        }
    response = client.post("/v1/skill-versions", json=payload, headers=_idempotency(operation))
    resource = _assert_success(response, 201)
    assert resource["state"] == "draft"
    assert resource["revision"] == 1
    assert response.headers["etag"] == '"1"'
    return resource, response.headers["etag"]


def _validate_version(
    client: httpx.Client,
    *,
    version: dict[str, Any],
    etag: str,
    operation: str,
) -> tuple[dict[str, Any], str]:
    response = client.post(
        f"/v1/skill-versions/{version['id']}/validate",
        headers={**_idempotency(operation), "If-Match": etag},
    )
    result = _assert_success(response, 200)
    assert result["skill_id"] == version["skill_id"]
    assert result["version_id"] == version["id"]
    assert result["ready"] is True
    assert result["checks"]
    assert all(check["passed"] for check in result["checks"])
    assert result["estimated_cost"]["currency"]
    return result, response.headers["etag"]


def test_live_openapi_advertises_the_control_workflow(http_client: httpx.Client) -> None:
    document = _assert_success(http_client.get("/openapi.json"), 200)
    expected_operations = {
        ("/v1/skills", "post"),
        ("/v1/skills/{skill_id}", "get"),
        ("/v1/skills/{skill_id}", "put"),
        ("/v1/skills/{skill_id}/fork", "post"),
        ("/v1/skill-versions", "post"),
        ("/v1/skill-versions/{skill_version_id}", "patch"),
        ("/v1/skill-versions/{skill_version_id}", "delete"),
        ("/v1/skill-versions/{skill_version_id}/validate", "post"),
        ("/v1/skill-versions/{skill_version_id}/publish", "post"),
        ("/v1/skill-test-executions", "post"),
        ("/v1/skill-test-executions/{execution_id}", "get"),
        ("/v1/runs", "post"),
        ("/v1/runs/{run_id}", "get"),
        ("/v1/runs/{run_id}/cancel", "post"),
        ("/v1/steps", "get"),
        ("/v1/steps/{step_id}", "get"),
        ("/v1/steps/{step_id}/review", "post"),
    }
    missing = {
        f"{method.upper()} {path}"
        for path, method in expected_operations
        if method not in document["paths"].get(path, {})
    }
    assert not missing, f"live OpenAPI document is missing: {sorted(missing)}"


def test_live_and_canonical_openapi_routes_match_bidirectionally(
    http_client: httpx.Client,
) -> None:
    live = _assert_success(http_client.get("/openapi.json"), 200)
    canonical_path = (
        Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi" / "v1.yaml"
    )
    canonical = yaml.safe_load(canonical_path.read_text(encoding="utf-8"))
    methods = {"get", "post", "put", "patch", "delete"}

    def operations(document: dict) -> set[tuple[str, str]]:
        return {
            (path, method)
            for path, item in document["paths"].items()
            if path.startswith("/v1/")
            for method in methods & set(item)
        }

    assert operations(live) == operations(canonical)


def test_skill_authoring_testing_publishing_and_forking_over_http(
    http_client: httpx.Client,
) -> None:
    context = _assert_success(http_client.get("/v1/context"), 200)
    ignored_workspace_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert context["mode"] == "single_workspace"
    assert context["workspace_id"] != ignored_workspace_id

    create_payload = _skill_create_payload("http-e2e-skill")
    create_payload["workspace_id"] = ignored_workspace_id
    headers = _idempotency("skill-create")
    first_create = http_client.post("/v1/skills", json=create_payload, headers=headers)
    skill = _assert_success(first_create, 201)
    replayed_create = http_client.post("/v1/skills", json=create_payload, headers=headers)
    assert _assert_success(replayed_create, 201) == skill
    assert skill["workspace_id"] == context["workspace_id"]
    assert skill["revision"] == 1

    fetched = http_client.get(f"/v1/skills/{skill['id']}")
    assert _assert_success(fetched, 200) == skill
    original_etag = fetched.headers["etag"]

    replacement = _skill_create_payload("http-e2e-skill-edited")
    replacement["name"] = "HTTP E2E Skill Edited"
    replaced = http_client.put(
        f"/v1/skills/{skill['id']}",
        json=replacement,
        headers={"If-Match": original_etag},
    )
    edited_skill = _assert_success(replaced, 200)
    assert edited_skill["revision"] == 2
    assert replaced.headers["etag"] == '"2"'

    stale_write = http_client.put(
        f"/v1/skills/{skill['id']}",
        json={**replacement, "name": "A stale writer must lose"},
        headers={"If-Match": original_etag},
    )
    stale_error = _assert_success(stale_write, 412)
    assert stale_error["code"] == "REVISION_CONFLICT"

    version, version_etag = _create_version(
        http_client,
        skill_id=skill["id"],
        version="0.1.0",
        operation="version-create",
    )
    changed_writing_policy = deepcopy(version["writing_policy"])
    changed_writing_policy["markdown_instructions"] = (
        "This edit crossed the real HTTP boundary and remains inert text."
    )
    patched = http_client.patch(
        f"/v1/skill-versions/{version['id']}",
        json={
            "writing_policy": changed_writing_policy,
            "release_notes": "Edited through PATCH.",
        },
        headers={"If-Match": version_etag},
    )
    patched_version = _assert_success(patched, 200)
    assert patched_version["revision"] == 2
    assert patched_version["content_hash"] != version["content_hash"]
    patched_etag = patched.headers["etag"]

    stale_patch = http_client.patch(
        f"/v1/skill-versions/{version['id']}",
        json={"release_notes": "This stale edit must not be stored."},
        headers={"If-Match": version_etag},
    )
    assert _assert_success(stale_patch, 412)["code"] == "REVISION_CONFLICT"

    validation, validated_etag = _validate_version(
        http_client,
        version=patched_version,
        etag=patched_etag,
        operation="version-validate",
    )
    validation_replay = http_client.post(
        f"/v1/skill-versions/{version['id']}/validate",
        headers={
            **_idempotency("version-validate"),
            "If-Match": patched_etag,
        },
    )
    assert _assert_success(validation_replay, 200) == validation

    published_response = http_client.post(
        f"/v1/skill-versions/{version['id']}/publish",
        json={},
        headers={**_idempotency("version-publish"), "If-Match": validated_etag},
    )
    published = _assert_success(published_response, 200)
    assert published["state"] == "published"
    assert published["published_at"] is not None

    comparison, comparison_etag = _create_version(
        http_client,
        skill_id=skill["id"],
        version="0.2.0",
        source_version_id=published["id"],
        operation="comparison-version-create",
    )
    _validate_version(
        http_client,
        version=comparison,
        etag=comparison_etag,
        operation="comparison-version-validate",
    )

    execution_payload = {
        "skill_id": skill["id"],
        "left_version_id": published["id"],
        "right_version_id": comparison["id"],
        "topic": "Why test execution needs a queryable status",
        "inputs": {"audience": "API maintainers"},
    }
    execution_headers = _idempotency("skill-test-execution")
    execution_response = http_client.post(
        "/v1/skill-test-executions",
        json=execution_payload,
        headers=execution_headers,
    )
    execution = _assert_success(execution_response, 201)
    assert execution["status"] == "succeeded"
    assert execution["left_version_id"] == published["id"]
    assert execution["right_version_id"] == comparison["id"]
    execution_replay = http_client.post(
        "/v1/skill-test-executions",
        json=execution_payload,
        headers=execution_headers,
    )
    assert _assert_success(execution_replay, 201) == execution
    queried_execution = http_client.get(f"/v1/skill-test-executions/{execution['id']}")
    assert _assert_success(queried_execution, 200) == execution

    fork_payload = {
        "source_version_id": published["id"],
        "name": "HTTP E2E Fork",
        "slug": "http-e2e-fork",
        "description": "Forked through the same API as every other Skill.",
        "publisher_name": "HTTP E2E Studio",
        "visibility": "private",
    }
    fork_headers = _idempotency("skill-fork")
    fork_response = http_client.post(
        f"/v1/skills/{skill['id']}/fork", json=fork_payload, headers=fork_headers
    )
    fork = _assert_success(fork_response, 201)
    assert fork["skill"]["forked_from_skill_id"] == skill["id"]
    assert fork["version"]["state"] == "draft"
    fork_replay = http_client.post(
        f"/v1/skills/{skill['id']}/fork", json=fork_payload, headers=fork_headers
    )
    assert _assert_success(fork_replay, 201) == fork

    disposable, disposable_etag = _create_version(
        http_client,
        skill_id=skill["id"],
        version="0.3.0",
        source_version_id=published["id"],
        operation="disposable-version-create",
    )
    deleted = http_client.delete(
        f"/v1/skill-versions/{disposable['id']}",
        headers={"If-Match": disposable_etag},
    )
    assert deleted.status_code == 204, deleted.text
    assert http_client.get(f"/v1/skill-versions/{disposable['id']}").status_code == 404


def test_run_creation_status_and_idempotency_over_http(http_client: httpx.Client) -> None:
    skill, _ = _create_skill(http_client, slug="http-e2e-run-skill", operation="run-skill-create")
    version, version_etag = _create_version(
        http_client,
        skill_id=skill["id"],
        version="1.0.0",
        operation="run-version-create",
    )
    _, validated_etag = _validate_version(
        http_client,
        version=version,
        etag=version_etag,
        operation="run-version-validate",
    )
    published = _assert_success(
        http_client.post(
            f"/v1/skill-versions/{version['id']}/publish",
            json={},
            headers={
                **_idempotency("run-version-publish"),
                "If-Match": validated_etag,
            },
        ),
        200,
    )

    example = _contract_example("run.schema.json")
    catalog = _assert_success(http_client.get("/v1/skill-versions?limit=100"), 200)["data"]
    pipeline_version_id = next(
        item["default_pipeline_version_id"]
        for item in catalog
        if item["ownership_type"] == "system" and item["default_pipeline_version_id"]
    )
    run_payload = {
        "channel_id": None,
        "input": {
            "topic": "A real HTTP run",
            "audience": "integration testers",
        },
        "composition": {
            "skill_version_id": published["id"],
            "pipeline_version_id": pipeline_version_id,
            "asset_library_ids": [],
            "voice_profile_id": None,
            "render_preset_version_id": None,
            "capabilities": example["composition_snapshot"]["capabilities"],
        },
    }
    headers = _idempotency("run-create")
    first = http_client.post("/v1/runs", json=run_payload, headers=headers)
    run = _assert_success(first, 201)
    assert run["status"] == "queued"
    assert run["composition_snapshot"]["skill_version"] == {
        "id": published["id"],
        "content_hash": published["content_hash"],
    }
    replay = http_client.post("/v1/runs", json=run_payload, headers=headers)
    assert _assert_success(replay, 201) == run

    queried = http_client.get(f"/v1/runs/{run['id']}")
    assert _assert_success(queried, 200) == run

    reused_key = http_client.post(
        "/v1/runs",
        json={**run_payload, "input": {**run_payload["input"], "audience": "another audience"}},
        headers=headers,
    )
    assert _assert_success(reused_key, 409)["code"] == "IDEMPOTENCY_KEY_REUSED"
