from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from framefactory.steps import StepContext
from framefactory.worker.generation.verifier import (
    GeneratedVideoVerificationError,
    GeneratedVideoVerifier,
)
from framefactory.worker.retrieval.models import RetrievalBeat


class FakeVisionAnalyzer:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.requests: list[dict[str, Any]] = []

    def analyze(
        self,
        frames: Sequence[Path],
        technical: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.requests.append(
            {
                "frame_bytes": [frame.read_bytes() for frame in frames],
                "technical": dict(technical),
            }
        )
        return self.response


class FakeCommandRunner:
    def __init__(
        self,
        *,
        probe: Mapping[str, Any] | None = None,
        frame_payloads: Sequence[bytes] = (b"first", b"middle", b"last"),
        decode_returncode: int = 0,
    ) -> None:
        self.probe = dict(probe or _probe_payload())
        self.frame_payloads = tuple(frame_payloads)
        self.decode_returncode = decode_returncode
        self.calls: list[tuple[str, ...]] = []
        self.frame_index = 0

    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check, timeout
        call = tuple(command)
        self.calls.append(call)
        if "-show_entries" in call:
            return subprocess.CompletedProcess(
                call,
                0,
                stdout=json.dumps(self.probe).encode(),
                stderr=b"",
            )
        if "-frames:v" not in call:
            return subprocess.CompletedProcess(
                call,
                self.decode_returncode,
                stdout=b"",
                stderr=b"decode failed" if self.decode_returncode else b"",
            )
        output = Path(call[-1])
        payload = self.frame_payloads[self.frame_index]
        self.frame_index += 1
        output.write_bytes(payload)
        return subprocess.CompletedProcess(call, 0, stdout=b"", stderr=b"")


def _probe_payload(
    *,
    duration: str = "5.0",
    width: int = 720,
    height: int = 1280,
    frame_rate: str = "25/1",
) -> dict[str, Any]:
    return {
        "streams": [
            {
                "codec_name": "h264",
                "width": width,
                "height": height,
                "avg_frame_rate": frame_rate,
                "duration": duration,
            }
        ],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration},
    }


def _beat() -> RetrievalBeat:
    return RetrievalBeat(
        id="beat-001",
        sequence=1,
        narration="清晨，摄影师走过海边。",
        visual_description="清晨海边的摄影师缓慢向前行走",
        must_match=("蓝色外套",),
        must_not_match=("品牌标志",),
    )


def _analysis() -> dict[str, Any]:
    return {
        "description": "一位穿蓝色外套的摄影师在清晨海边向前行走。",
        "labels": ["海边", "摄影师", "清晨"],
        "confidence": 0.94,
        "visual_description_check": {
            "matched": True,
            "evidence": "首中尾三帧均显示主体沿海岸缓慢前行。",
        },
        "constraint_checks": [
            {
                "kind": "must_match",
                "term": "蓝色外套",
                "matched": True,
                "evidence": "三帧中的主体均穿着蓝色外套。",
            },
            {
                "kind": "must_not_match",
                "term": "品牌标志",
                "matched": False,
                "evidence": "三帧均未检测到品牌标志。",
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
        "quality": {"usable": True, "score": 0.91},
        "cut_safe": True,
        "cut_safe_evidence": "首尾帧没有突变、黑帧或截断动作。",
    }


def _context() -> StepContext:
    return StepContext(
        workspace_id=str(uuid4()),
        run_id=str(uuid4()),
        step_id="media.generate.verify",
        input_snapshot={"mode": "generated_only"},
    )


def _verifier(
    analysis: Mapping[str, Any],
    runner: FakeCommandRunner | None = None,
) -> tuple[GeneratedVideoVerifier, FakeVisionAnalyzer, FakeCommandRunner]:
    vision = FakeVisionAnalyzer(analysis)
    commands = runner or FakeCommandRunner()
    verifier = GeneratedVideoVerifier(
        vision,
        vision_provider="independent-vision",
        vision_model="vision-qc-v1",
        command_runner=commands,
        command_resolver=lambda command: command,
    )
    return verifier, vision, commands


def _verify(
    verifier: GeneratedVideoVerifier,
    *,
    expected_duration_seconds: float = 5.0,
    ratio: str = "9:16",
):
    return asyncio.run(
        verifier.verify(
            b"generated-video-bytes",
            _beat(),
            expected_duration_seconds,
            ratio,
            _context(),
        )
    )


def test_verifier_collects_independent_technical_and_visual_evidence() -> None:
    verifier, vision, commands = _verifier(_analysis())

    result = _verify(verifier)

    assert result.eligible is True
    assert result.semantic_complete is True
    assert result.cut_safe is True
    assert (result.duration_seconds, result.width, result.height, result.codec) == (
        5.0,
        720,
        1280,
        "h264",
    )
    assert result.provider == "independent-vision"
    assert result.model == "vision-qc-v1"
    assert len(result.frame_hashes) == len(set(result.frame_hashes)) == 3
    assert [item.passed for item in result.constraint_checks] == [True, True, True]
    assert result.evidence["video_sha256"]
    assert len(vision.requests) == 1
    request = vision.requests[0]["technical"]
    assert request["verification_contract"]["must_match"] == ["蓝色外套"]
    assert request["verification_contract"]["must_not_match"] == ["品牌标志"]
    assert request["frame_hashes"] == list(result.frame_hashes)
    assert len([call for call in commands.calls if "-frames:v" in call]) == 3
    assert any("-xerror" in call and call[-1] == "-" for call in commands.calls)


@pytest.mark.parametrize(
    ("field", "value", "rejection"),
    (
        ("has_watermark", True, "watermark_detected"),
        ("has_embedded_text", True, "embedded_text_detected"),
        ("unsafe", True, "unsafe_content"),
    ),
)
def test_watermark_text_or_unsafe_content_is_never_eligible(
    field: str,
    value: bool,
    rejection: str,
) -> None:
    analysis = _analysis()
    analysis[field] = value
    verifier, _, _ = _verifier(analysis)

    result = _verify(verifier)

    assert result.eligible is False
    assert rejection in result.rejection_codes


@pytest.mark.parametrize(
    "policy_key",
    (
        "adult",
        "violence",
        "self_harm",
        "hate_or_extremism",
        "illegal_activity",
        "recognizable_real_person_or_public_figure",
        "brand_or_logo",
        "protected_character",
    ),
)
def test_each_fixed_safety_policy_flag_rejects_the_candidate(policy_key: str) -> None:
    analysis = _analysis()
    analysis["safety"][policy_key] = True
    verifier, _, _ = _verifier(analysis)

    result = _verify(verifier)

    assert result.eligible is False
    assert result.unsafe is True
    assert "unsafe_content" in result.rejection_codes


def test_safety_contract_rejects_missing_or_extra_policy_flags() -> None:
    for mutate in (
        lambda value: value.pop("brand_or_logo"),
        lambda value: value.__setitem__("unknown_policy", False),
    ):
        analysis = _analysis()
        mutate(analysis["safety"])
        verifier, _, _ = _verifier(analysis)

        with pytest.raises(GeneratedVideoVerificationError) as raised:
            _verify(verifier)

        assert raised.value.code == "FULL_AI_VISION_EVIDENCE_INVALID"


def test_every_beat_constraint_controls_semantic_eligibility() -> None:
    analysis = _analysis()
    analysis["constraint_checks"][0]["matched"] = False
    analysis["constraint_checks"][1]["matched"] = True
    verifier, _, _ = _verifier(analysis)

    result = _verify(verifier)

    assert result.eligible is False
    assert result.semantic_complete is False
    assert "must_match_failed" in result.rejection_codes
    assert "must_not_match_detected" in result.rejection_codes


def test_low_confidence_or_unusable_quality_fails_closed() -> None:
    analysis = _analysis()
    analysis["confidence"] = 0.2
    analysis["quality"] = {"usable": False, "score": 0.1}
    verifier, _, _ = _verifier(analysis)

    result = _verify(verifier)

    assert result.eligible is False
    assert result.cut_safe is False
    assert result.semantic_complete is False
    assert "low_visual_confidence" in result.rejection_codes
    assert "visual_quality_unusable" in result.rejection_codes


def test_missing_per_term_evidence_is_a_hard_verification_failure() -> None:
    analysis = _analysis()
    analysis["constraint_checks"] = analysis["constraint_checks"][:1]
    verifier, _, _ = _verifier(analysis)

    with pytest.raises(GeneratedVideoVerificationError) as raised:
        _verify(verifier)

    assert raised.value.code == "FULL_AI_VISION_EVIDENCE_INVALID"
    assert raised.value.retryable is False
    assert raised.value.evidence["expected"] == [
        ["must_match", "蓝色外套"],
        ["must_not_match", "品牌标志"],
    ]


def test_no_independent_vision_configuration_never_passes() -> None:
    verifier = GeneratedVideoVerifier(
        None,
        vision_provider="",
        vision_model="",
        command_runner=FakeCommandRunner(),
        command_resolver=lambda command: command,
    )

    with pytest.raises(GeneratedVideoVerificationError) as raised:
        _verify(verifier)

    assert raised.value.code == "FULL_AI_VISION_UNCONFIGURED"
    assert raised.value.retryable is False


def test_duplicate_representative_frames_are_not_auditable_evidence() -> None:
    runner = FakeCommandRunner(frame_payloads=(b"same", b"same", b"same"))
    verifier, vision, _ = _verifier(_analysis(), runner)

    with pytest.raises(GeneratedVideoVerificationError) as raised:
        _verify(verifier)

    assert raised.value.code == "FULL_AI_VISUAL_EVIDENCE_INSUFFICIENT"
    assert len(raised.value.evidence["frame_hashes"]) == 3
    assert vision.requests == []


@pytest.mark.parametrize(
    ("runner", "expected_duration", "ratio", "code"),
    (
        (
            FakeCommandRunner(probe=_probe_payload(duration="5.1")),
            5.0,
            "9:16",
            "FULL_AI_VIDEO_DURATION_MISMATCH",
        ),
        (
            FakeCommandRunner(probe=_probe_payload(width=1280, height=720)),
            5.0,
            "9:16",
            "FULL_AI_VIDEO_RATIO_MISMATCH",
        ),
        (
            FakeCommandRunner(decode_returncode=1),
            5.0,
            "9:16",
            "FULL_AI_VIDEO_DECODE_FAILED",
        ),
    ),
)
def test_duration_ratio_and_complete_decode_are_hard_technical_gates(
    runner: FakeCommandRunner,
    expected_duration: float,
    ratio: str,
    code: str,
) -> None:
    verifier, vision, _ = _verifier(_analysis(), runner)

    with pytest.raises(GeneratedVideoVerificationError) as raised:
        _verify(
            verifier,
            expected_duration_seconds=expected_duration,
            ratio=ratio,
        )

    assert raised.value.code == code
    assert vision.requests == []
