from __future__ import annotations

from copy import deepcopy

from conftest import contract_example
from fastapi.testclient import TestClient

from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import DEVELOPMENT_WORKSPACE_ID


def _repository() -> tuple[InMemoryControlRepository, dict, dict]:
    version = contract_example("skill-version.schema.json")
    version["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    version["ownership_type"] = "workspace"
    version["state"] = "published"
    version["published_at"] = version["created_at"]
    pipeline = contract_example("pipeline.schema.json")
    pipeline["workspace_id"] = str(DEVELOPMENT_WORKSPACE_ID)
    pipeline["ownership_type"] = "workspace"
    pipeline["state"] = "published"
    pipeline["status"] = "active"
    pipeline["published_at"] = pipeline["created_at"]
    return (
        InMemoryControlRepository(
            skill_versions=[deepcopy(version)], pipelines=[deepcopy(pipeline)]
        ),
        version,
        pipeline,
    )


def _composition(version: dict, pipeline: dict) -> dict:
    return {
        "skill_version_id": version["id"],
        "pipeline_version_id": pipeline["id"],
        "asset_library_ids": [],
        "voice_profile_id": None,
        "render_preset_version_id": None,
        "capabilities": [],
    }


def test_run_resolves_account_defaults_and_explicit_video_overrides() -> None:
    repository, version, pipeline = _repository()
    with TestClient(create_app(repository=repository)) as client:
        preferences = client.get("/v1/account/creation-preferences")
        changed = client.put(
            "/v1/account/creation-preferences",
            headers={"If-Match": preferences.headers["etag"]},
            json={
                "default_language": "zh-CN",
                "default_aspect_ratio": "9:16",
                "default_duration_seconds": 120,
                "default_visibility": "private",
                "auto_quality_check": True,
            },
        )
        assert changed.status_code == 200

        inherited = client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "video-defaults-0001"},
            json={
                "input": {"topic": "Inherited video settings"},
                "composition": _composition(version, pipeline),
            },
        )
        overridden = client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "video-overrides-0001"},
            json={
                "input": {"topic": "Explicit video settings"},
                "composition": _composition(version, pipeline),
                "video_settings": {
                    "aspect_ratio": "1:1",
                    "target_duration_seconds": 75,
                    "layout": "editorial",
                    "media_fit": "contain",
                    "frame_rate": 25,
                    "subtitles": {
                        "enabled": True,
                        "position": "lower_third",
                        "size": "large",
                        "max_lines": 2,
                    },
                },
            },
        )

    inherited_settings = inherited.json()["composition_snapshot"]["production_settings"]
    assert inherited.status_code == 201
    assert inherited_settings["aspect_ratio"] == "9:16"
    assert inherited_settings["resolution"] == {"width": 1080, "height": 1920}
    assert inherited_settings["target_duration_seconds"] == 120
    assert inherited_settings["sources"]["aspect_ratio"] == "account_default"

    overridden_settings = overridden.json()["composition_snapshot"]["production_settings"]
    assert overridden.status_code == 201
    assert overridden_settings["aspect_ratio"] == "1:1"
    assert overridden_settings["resolution"] == {"width": 1080, "height": 1080}
    assert overridden_settings["layout"] == "editorial"
    assert overridden_settings["media_fit"] == "contain"
    assert overridden_settings["frame_rate"] == 25
    assert overridden_settings["sources"]["aspect_ratio"] == "run_override"
