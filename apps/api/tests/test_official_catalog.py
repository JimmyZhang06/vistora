from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from framefactory_api.contracts import ContractValidator
from framefactory_api.main import create_app
from framefactory_api.seed_catalog import load_official_catalog

PIPELINE_VERSION_ID = "30b37ab7-9cf7-5d26-ac3e-6d66880b536c"


def test_default_app_loads_the_validated_official_catalog() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/v1/skills")
        assert response.status_code == 200
        skills = response.json()["data"]
        assert len(skills) == 5
        assert all(skill["ownership_type"] == "system" for skill in skills)
        assert all(skill["publisher_type"] == "system" for skill in skills)


def test_official_versions_share_a_validated_declarative_pipeline() -> None:
    _, versions = load_official_catalog(ContractValidator())

    assert {version["default_pipeline_version_id"] for version in versions} == {
        PIPELINE_VERSION_ID
    }
    assert len(versions.pipelines) == 1
    pipeline = versions.pipelines[0].version
    assert pipeline["id"] == PIPELINE_VERSION_ID
    assert [node["key"] for node in pipeline["nodes"]] == [
        "research",
        "write",
        "tts",
        "assets",
        "render",
        "quality",
    ]
    assert pipeline["nodes"][-1]["review_gate"] is False


def test_official_skill_can_create_a_run_in_memory() -> None:
    with TestClient(create_app()) as client:
        version = client.get("/v1/skill-versions").json()["data"][0]
        response = client.post(
            "/v1/runs",
            json={
                "input": {"topic": "验证官方默认流水线"},
                "composition": {
                    "skill_version_id": version["id"],
                    "pipeline_version_id": version["default_pipeline_version_id"],
                    "asset_library_ids": [],
                },
            },
            headers={"Idempotency-Key": "official-default-pipeline-run-0001"},
        )

    assert response.status_code == 201
    assert response.json()["composition_snapshot"]["pipeline_version"]["id"] == (
        PIPELINE_VERSION_ID
    )


def test_official_pipeline_seed_is_packaged_with_the_manifest() -> None:
    manifest = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "seeds"
        / "official-skills"
        / "v1"
        / "manifest.json"
    )
    _, versions = load_official_catalog(ContractValidator(), manifest)

    assert versions.pipelines[0].pipeline_id == "2c15b2d6-1460-5d30-a718-f4b06d8e28d7"


def test_loader_rejects_tampered_pipeline_content(tmp_path: Path) -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "seeds"
        / "official-skills"
        / "v1"
    )
    package = tmp_path / "official-skills"
    shutil.copytree(source, package)
    pipeline_path = package / "pipelines" / "standard-production" / "1.json"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    pipeline["nodes"][0]["timeout_seconds"] += 1
    pipeline_path.write_text(json.dumps(pipeline, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="content hash"):
        load_official_catalog(ContractValidator(), package / "manifest.json")
