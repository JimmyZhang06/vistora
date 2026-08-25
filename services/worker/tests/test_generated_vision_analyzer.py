from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

import pytest
from framefactory.worker.generation import vision as vision_module
from framefactory.worker.generation.verifier import GeneratedVideoVerificationError
from framefactory.worker.generation.vision import (
    OpenAICompatibleGeneratedVisionAnalyzer,
    _dynamic_prompt,
)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        assert maximum > len(self.payload)
        return self.payload


def _contract() -> dict[str, Any]:
    return {
        "beat_id": "beat-001",
        "visual_description": "清晨海边的摄影师缓慢向前行走",
        "must_match": ["蓝色外套"],
        "must_not_match": ["品牌标志"],
    }


def _analysis() -> dict[str, Any]:
    return {
        "description": "摄影师在清晨海边行走",
        "labels": ["摄影师", "海边"],
        "confidence": 0.9,
        "visual_description_check": {"matched": True, "evidence": "三帧一致"},
        "constraint_checks": [
            {
                "kind": "must_match",
                "term": "蓝色外套",
                "matched": True,
                "evidence": "主体穿蓝色外套",
            },
            {
                "kind": "must_not_match",
                "term": "品牌标志",
                "matched": False,
                "evidence": "未见品牌标志",
            },
        ],
        "has_watermark": False,
        "has_embedded_text": False,
        "unsafe": False,
        "safety": {
            "adult": False,
            "violence": False,
            "self_harm": False,
            "hate_or_extremism": False,
            "illegal_activity": False,
            "recognizable_real_person_or_public_figure": False,
            "brand_or_logo": False,
            "protected_character": False,
        },
        "quality": {"usable": True, "score": 0.9},
        "cut_safe": True,
        "cut_safe_evidence": "首尾无截断",
    }


def test_dynamic_prompt_contains_exact_beat_constraints_and_fixed_safety_contract() -> None:
    prompt = _dynamic_prompt(_contract())

    assert '"beat_id":"beat-001"' in prompt
    assert '"must_match":["蓝色外套"]' in prompt
    assert '"must_not_match":["品牌标志"]' in prompt
    for key in (
        "adult",
        "violence",
        "self_harm",
        "hate_or_extremism",
        "illegal_activity",
        "recognizable_real_person_or_public_figure",
        "brand_or_logo",
        "protected_character",
    ):
        assert key in prompt


def test_analyzer_sends_frames_and_parses_structured_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-fixture")
    captured: dict[str, Any] = {}
    envelope = {
        "choices": [{"message": {"content": json.dumps(_analysis(), ensure_ascii=False)}}]
    }

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(json.dumps(envelope, ensure_ascii=False).encode())

    monkeypatch.setattr(vision_module, "urlopen", fake_urlopen)
    analyzer = OpenAICompatibleGeneratedVisionAnalyzer(
        base_url="https://vision.example.test/v1",
        api_key="secret-test-key",
        model="vision-qc",
        timeout_seconds=12,
    )

    result = analyzer.analyze(
        (frame,),
        {"verification_contract": _contract()},
    )

    assert result == _analysis()
    assert captured["url"] == "https://vision.example.test/v1/chat/completions"
    assert captured["timeout"] == 12
    assert captured["body"]["response_format"] == {"type": "json_object"}
    content = captured["body"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "secret-test-key" not in json.dumps(captured["body"])


def test_analyzer_rejects_plaintext_or_missing_dynamic_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleGeneratedVisionAnalyzer(
            base_url="http://vision.example.test/v1",
            api_key="secret",
            model="vision-qc",
        )
    analyzer = OpenAICompatibleGeneratedVisionAnalyzer(
        base_url="https://vision.example.test/v1",
        api_key="secret",
        model="vision-qc",
    )
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    with pytest.raises(GeneratedVideoVerificationError) as raised:
        analyzer.analyze((frame,), {})

    assert raised.value.code == "FULL_AI_VISION_CONTRACT_MISSING"
