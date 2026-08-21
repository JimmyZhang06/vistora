from __future__ import annotations

from typing import Any


def skill_payload(skill_id: str = "user-blank", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "skill_id": skill_id,
        "version": 1,
        "name": "Blank user Skill",
        "description": "",
        "state": "draft",
        "lineage": {"kind": "original"},
        "input_schema": {},
        "research_policy": {},
        "writing_instructions": {},
        "visual_policy": {},
        "asset_policy": {},
        "qc_rubric": {},
        "output_contract": {},
        "model_requirements": {},
        "capabilities": {},
    }
    payload.update(overrides)
    return payload


def official_seed_payload() -> dict[str, Any]:
    return skill_payload(
        "system-history-seed",
        name="History explainer seed",
        state="published",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
        research_policy={
            "instructions": "Use primary and authoritative secondary sources.",
            "source_requirements": ["Record claim-level source notes."],
            "fact_boundaries": ["Mark uncertainty explicitly."],
            "min_sources": 2,
            "citations_required": True,
        },
        writing_instructions={
            "instructions": "Open with the central tension, then explain mechanisms.",
            "constraints": ["Do not invent quotations."],
        },
        visual_policy={
            "instructions": "Prefer period-appropriate scenes.",
            "constraints": ["Avoid anachronisms."],
            "categories": ["portrait", "location", "document"],
        },
        asset_policy={
            "allowed_source_types": ["licensed-library", "public-domain"],
            "constraints": ["Retain provenance."],
        },
        qc_rubric={
            "instructions": "Check every factual claim.",
            "criteria": [
                {"name": "factuality", "description": "Claims are supported.", "severity": "error"}
            ],
        },
        output_contract={
            "writing": {
                "type": "object",
                "properties": {"sentences": {"type": "array"}},
                "required": ["sentences"],
            }
        },
        model_requirements={"writing": {"structured_output": True}},
        capabilities={
            "research": {
                "required": ["research.source_grounding"],
                "optional": ["research.web_search"],
            },
            "writing": {"required": ["model.text_generation"]},
        },
    )


def legacy_manifest() -> dict[str, Any]:
    return {
        "skill_id": "legacy-history-seed",
        "name": "Legacy history seed",
        "version": 2,
        "parent_version": 1,
        "origin": "route_A",
        "confidence": 0.6,
        "distilled_at": "2026-07-25",
        "corpus_ref": "D:/historical-machine/corpus",
        "赛道": ["历史", "人文"],
        "内容类型": ["人物解读"],
        "适用主题": ["历史人物"],
        "表达风格": ["故事化"],
        "目标受众": ["大众"],
        "视频规格": {"时长秒": {"短": [60, 120]}, "备注": "D:/old-machine/output"},
        "禁用范围": ["要求编造事实的内容"],
        "_note_schema": "legacy-only extension",
    }
