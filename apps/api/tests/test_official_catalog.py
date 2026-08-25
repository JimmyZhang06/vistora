from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from framefactory_api.contracts import ContractValidator
from framefactory_api.main import create_app
from framefactory_api.seed_catalog import load_official_catalog

PIPELINE_V1_ID = "30b37ab7-9cf7-5d26-ac3e-6d66880b536c"
PIPELINE_V2_ID = "3df5479d-9eb2-583f-8531-66ba0bb61204"
PIPELINE_V3_ID = "4c7d9777-d754-5fa3-bf85-4bf5c9746dba"
FULL_AI_PIPELINE_ID = "7ed21b77-7e4c-5801-8fc0-bee133575101"
FULL_AI_PIPELINE_V1_ID = "fe7d52b9-7412-5c96-8214-398f18a2d682"
FULL_AI_PIPELINE_V2_ID = "6d6bad5a-e758-5d6c-9c39-e7c7713e5a4c"
WEBPAGE_VIDEO_PIPELINE_ID = "7b80a43b-5752-58f3-aaa4-d423610f848d"
WEBPAGE_VIDEO_PIPELINE_V1_ID = "eaf69761-8571-5404-a50a-8ef5c0aa90d3"
WEBPAGE_VIDEO_PIPELINE_V2_ID = "2110e922-329d-565f-ba7a-3616c9d5070b"
GENERAL_EXPLAINER_V2_ID = "ae752896-0fd1-5d6f-ba13-9d4bf504ea12"
GENERAL_EXPLAINER_V3_ID = "62c48333-d19f-57b0-9479-bb844b63ef08"


def _copy_official_package(tmp_path: Path) -> Path:
    source = (
        Path(__file__).resolve().parents[3]
        / "packages"
        / "seeds"
        / "official-skills"
        / "v1"
    )
    package = tmp_path / "official-skills"
    shutil.copytree(source, package)
    return package


def test_default_app_loads_the_validated_official_catalog() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/v1/skills")
        assert response.status_code == 200
        skills = response.json()["data"]
        assert len(skills) == 7
        assert all(skill["ownership_type"] == "system" for skill in skills)
        assert all(skill["publisher_type"] == "system" for skill in skills)


def test_official_catalog_supports_multiple_skill_and_pipeline_versions() -> None:
    skills, versions = load_official_catalog(ContractValidator())

    assert {version["default_pipeline_version_id"] for version in versions} == {
        PIPELINE_V1_ID,
        PIPELINE_V2_ID,
        FULL_AI_PIPELINE_V1_ID,
        FULL_AI_PIPELINE_V2_ID,
        WEBPAGE_VIDEO_PIPELINE_V1_ID,
        WEBPAGE_VIDEO_PIPELINE_V2_ID,
    }
    assert len(skills) == 7
    assert len(versions) == 11
    assert len(versions.pipelines) == 7

    standard_pipelines = [
        seed.version
        for seed in versions.pipelines
        if seed.pipeline_id == "2c15b2d6-1460-5d30-a718-f4b06d8e28d7"
    ]
    by_version = {pipeline["version"]: pipeline for pipeline in standard_pipelines}
    assert set(by_version) == {1, 2, 3}
    assert by_version[1]["id"] == PIPELINE_V1_ID
    pipeline = by_version[2]
    assert pipeline["id"] == PIPELINE_V2_ID
    assert [node["key"] for node in pipeline["nodes"]] == [
        "research",
        "write",
        "tts",
        "retrieve",
        "timeline",
        "render",
        "quality",
    ]
    nodes = {node["key"]: node for node in pipeline["nodes"]}
    assert nodes["retrieve"]["operation"] == "media.retrieve"
    assert nodes["timeline"]["operation"] == "timeline.align"
    assert nodes["timeline"]["depends_on"] == ["write", "tts", "retrieve"]
    assert nodes["render"]["operation"] == "render.edl"
    assert nodes["render"]["depends_on"] == ["tts", "retrieve", "timeline"]

    batch_pipeline = by_version[3]
    assert batch_pipeline["id"] == PIPELINE_V3_ID
    assert [node["key"] for node in batch_pipeline["nodes"]] == [
        "research",
        "inventory",
        "write",
        "tts",
        "retrieve",
        "timeline",
        "render",
        "quality",
    ]
    batch_nodes = {node["key"]: node for node in batch_pipeline["nodes"]}
    assert batch_nodes["inventory"]["operation"] == "media.inventory"
    assert batch_nodes["write"]["depends_on"] == ["research", "inventory"]
    assert "research.web_acquisition" not in batch_pipeline["capability_requirements"]
    assert pipeline["nodes"][-1]["review_gate"] is False

    historical_full_ai = next(
        seed.version
        for seed in versions.pipelines
        if seed.version["id"] == FULL_AI_PIPELINE_V1_ID
    )
    assert historical_full_ai["status"] == "archived"
    assert historical_full_ai["version"] == 1
    assert "media.retrieve" in historical_full_ai["capability_requirements"]

    full_ai = next(
        seed.version
        for seed in versions.pipelines
        if seed.version["id"] == FULL_AI_PIPELINE_V2_ID
    )
    assert full_ai["slug"] == "full-ai-production"
    assert [node["key"] for node in full_ai["nodes"]] == [
        "write",
        "tts",
        "generate",
        "timeline",
        "render",
        "quality",
    ]
    full_ai_nodes = {node["key"]: node for node in full_ai["nodes"]}
    assert full_ai_nodes["write"]["operation"] == "writing.compose.generated"
    assert full_ai_nodes["write"]["depends_on"] == []
    assert full_ai_nodes["generate"]["operation"] == "media.generate"
    assert full_ai_nodes["generate"]["depends_on"] == ["write", "tts"]
    assert full_ai_nodes["timeline"]["depends_on"] == ["write", "tts", "generate"]
    assert full_ai_nodes["render"]["depends_on"] == ["tts", "generate", "timeline"]
    assert {
        "model.video_generation",
        "model.generated_video_verification",
        "writing.compose.generated",
        "audio.synthesize",
        "media.generate",
        "timeline.align",
        "render.edl",
        "quality.evaluate",
    } <= set(full_ai["capability_requirements"])
    assert "research.web_acquisition" not in full_ai["capability_requirements"]
    assert "media.retrieve" not in full_ai["capability_requirements"]
    full_ai_skill = next(skill for skill in skills if skill["slug"] == "full-ai-video-director")
    assert full_ai_skill["status"] == "archived"
    full_ai_versions = {
        version["version"]: version
        for version in versions
        if version["skill_id"] == full_ai_skill["id"]
    }
    assert set(full_ai_versions) == {"1.0.0", "1.1.0"}
    assert (
        full_ai_versions["1.0.0"]["default_pipeline_version_id"]
        == FULL_AI_PIPELINE_V1_ID
    )
    full_ai_version = full_ai_versions["1.1.0"]
    assert full_ai_version["state"] == "published"
    assert full_ai_version["default_pipeline_version_id"] == FULL_AI_PIPELINE_V2_ID
    assert full_ai_version["visual_policy"]["generated_media_allowed"] is True
    assert full_ai_version["asset_policy"]["library_binding"] == "none"

    historical_successor = next(
        version for version in versions if version["id"] == GENERAL_EXPLAINER_V2_ID
    )
    assert historical_successor["version"] == "1.1.0"
    historical_web_research = next(
        requirement
        for requirement in historical_successor["capability_requirements"]
        if requirement["name"] == "research.web_acquisition"
    )
    assert historical_web_research["level"] == "required"
    assert "inventory" not in {
        artifact["kind"] for artifact in historical_successor["output_contract"]["artifacts"]
    }

    successor = next(version for version in versions if version["id"] == GENERAL_EXPLAINER_V3_ID)
    assert successor["version"] == "1.2.0"
    assert successor["default_pipeline_version_id"] == PIPELINE_V2_ID
    assert {
        "audio",
        "narration_timing",
        "asset",
        "candidate_manifest",
        "inventory",
        "material_selection",
        "timeline",
        "video",
        "qc_report",
    } <= {artifact["kind"] for artifact in successor["output_contract"]["artifacts"]}
    asset_output = next(
        artifact
        for artifact in successor["output_contract"]["artifacts"]
        if artifact["kind"] == "asset"
    )
    assert {"image/bmp", "video/webm"} <= set(asset_output["media_types"])
    web_research = next(
        requirement
        for requirement in successor["capability_requirements"]
        if requirement["name"] == "research.web_acquisition"
    )
    assert web_research["level"] == "optional"


def test_official_skill_can_create_a_run_in_memory() -> None:
    with TestClient(create_app()) as client:
        version = next(
            item
            for item in client.get("/v1/skill-versions").json()["data"]
            if item["id"] == GENERAL_EXPLAINER_V3_ID
        )
        library_response = client.post(
            "/v1/asset-libraries",
            json={
                "name": "Official Pipeline Assets",
                "slug": "official-pipeline-assets",
                "description": "Active local library required by media.retrieve.",
            },
            headers={"Idempotency-Key": "official-pipeline-library-0001"},
        )
        assert library_response.status_code == 201
        library = library_response.json()
        response = client.post(
            "/v1/runs",
            json={
                "input": {"topic": "验证官方默认流水线"},
                "composition": {
                    "skill_version_id": version["id"],
                    "pipeline_version_id": version["default_pipeline_version_id"],
                    "asset_library_ids": [library["id"]],
                },
            },
            headers={"Idempotency-Key": "official-default-pipeline-run-0001"},
        )

    assert response.status_code == 201
    assert response.json()["composition_snapshot"]["pipeline_version"]["id"] == (
        PIPELINE_V2_ID
    )
    assert response.json()["composition_snapshot"]["asset_library_ids"] == [
        library["id"]
    ]


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

    assert {seed.pipeline_id for seed in versions.pipelines} == {
        "2c15b2d6-1460-5d30-a718-f4b06d8e28d7",
        FULL_AI_PIPELINE_ID,
        WEBPAGE_VIDEO_PIPELINE_ID,
    }


def test_loader_rejects_tampered_pipeline_content(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    pipeline_path = package / "pipelines" / "standard-production" / "1.json"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    pipeline["nodes"][0]["timeout_seconds"] += 1
    pipeline_path.write_text(json.dumps(pipeline, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="content hash"):
        load_official_catalog(ContractValidator(), package / "manifest.json")


def test_loader_orders_pipeline_versions_independently_of_manifest_order(
    tmp_path: Path,
) -> None:
    package = _copy_official_package(tmp_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pipelines"].reverse()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    _, versions = load_official_catalog(ContractValidator(), manifest_path)

    assert [
        seed.version["version"]
        for seed in versions.pipelines
        if seed.pipeline_id == "2c15b2d6-1460-5d30-a718-f4b06d8e28d7"
    ] == [1, 2, 3]
    assert [
        seed.version["version"]
        for seed in versions.pipelines
        if seed.pipeline_id == FULL_AI_PIPELINE_ID
    ] == [1, 2]


def test_loader_rejects_duplicate_pipeline_version(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pipelines"].append(dict(manifest["pipelines"][0]))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="repeats a PipelineVersion"):
        load_official_catalog(ContractValidator(), manifest_path)


def test_loader_rejects_nondeterministic_skill_version_id(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    version_path = package / "general-topic-explainer" / "1.1.0.json"
    version = json.loads(version_path.read_text(encoding="utf-8"))
    version["id"] = "11111111-1111-4111-8111-111111111111"
    version_path.write_text(json.dumps(version, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="SkillVersion ID is not deterministic"):
        load_official_catalog(ContractValidator(), package / "manifest.json")


def test_loader_rejects_nondeterministic_skill_id(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    replacement = "11111111-1111-4111-8111-111111111111"
    skill_path = package / "general-topic-explainer" / "skill.json"
    skill = json.loads(skill_path.read_text(encoding="utf-8"))
    skill["id"] = replacement
    skill_path.write_text(json.dumps(skill, ensure_ascii=False), encoding="utf-8")
    for name in ("1.0.0.json", "1.1.0.json", "1.2.0.json"):
        version_path = package / "general-topic-explainer" / name
        version = json.loads(version_path.read_text(encoding="utf-8"))
        version["skill_id"] = replacement
        version_path.write_text(json.dumps(version, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Skill ID is not deterministic"):
        load_official_catalog(ContractValidator(), package / "manifest.json")


def test_loader_rejects_skill_slug_different_from_manifest(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["slug"] = "renamed-general-topic-explainer"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Skill slug differs"):
        load_official_catalog(ContractValidator(), manifest_path)


def test_loader_rejects_duplicate_skill_semantic_version(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["entries"][0])
    duplicate["order"] = 999
    manifest["entries"].append(duplicate)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="repeats a Skill semantic version"):
        load_official_catalog(ContractValidator(), manifest_path)


def test_loader_rejects_unpackaged_current_skill_version(tmp_path: Path) -> None:
    package = _copy_official_package(tmp_path)
    skill_path = package / "general-topic-explainer" / "skill.json"
    skill = json.loads(skill_path.read_text(encoding="utf-8"))
    skill["current_version_id"] = "11111111-1111-4111-8111-111111111111"
    skill_path.write_text(json.dumps(skill, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="current_version_id"):
        load_official_catalog(ContractValidator(), package / "manifest.json")
