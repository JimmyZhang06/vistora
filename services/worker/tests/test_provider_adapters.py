from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.adapters.openai_compatible import (
    HttpRequest,
    HttpResponse,
    OpenAICompatibleClient,
    OpenAIQualityCapability,
    OpenAIResearchCapability,
    OpenAIWritingCapability,
    _qualitatively_redact_research_quantities,
    _qualitatively_redact_script_quantities,
    _research_validation_issues,
    _script_grounding_issues,
    _supplied_https_urls,
)
from framefactory.worker.adapters.s3_artifacts import S3ArtifactStorage
from framefactory.worker.config import ObjectStorageSettings
from framefactory.worker.providers import ProviderArtifact


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class MemoryArtifacts:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def publish(self, context: StepContext, artifact: Any) -> ArtifactRef:
        artifact_id = str(uuid4())
        self.values[artifact_id] = artifact.data
        return ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=str(uuid4()),
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"workspaces/{context.workspace_id}/runs/{context.run_id}/artifacts/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash="a" * 64,
            filename=artifact.filename,
        )

    def read_json(self, artifact: ArtifactRef) -> dict[str, Any]:
        return json.loads(self.values[artifact.id])

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        return self.values[artifact.id]


def response(value: dict[str, Any], status: int = 200) -> HttpResponse:
    envelope = {"choices": [{"message": {"content": json.dumps(value)}}]}
    return HttpResponse(status=status, body=json.dumps(envelope).encode())


class OpenAICompatibleAdapterTests(unittest.TestCase):
    def client(self, transport: FakeTransport) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            base_url="https://models.example.test/v1",
            api_key="dummy-provider-key",
            model="configured-model",
            timeout_seconds=12,
            transport=transport,
        )

    def context(self, *, artifacts: tuple[ArtifactRef, ...] = ()) -> StepContext:
        return StepContext(
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="run:step",
            input_snapshot={
                "topic": "durability",
                "_framefactory": {"skill_version": {"research_policy": {"depth": 2}}},
            },
            input_artifacts=artifacts,
        )

    def test_research_uses_structured_request_and_publishes_real_json(self) -> None:
        transport = FakeTransport(
            [response({"brief": "Verified brief", "sources": []})]
        )
        storage = MemoryArtifacts()
        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(self.context())
        )

        self.assertEqual("research", result.artifacts[0].kind)
        self.assertTrue(result.requires_review)
        request = transport.requests[0]
        body = json.loads(request.body)
        self.assertEqual("configured-model", body["model"])
        self.assertEqual("json_schema", body["response_format"]["type"])
        self.assertEqual("Bearer dummy-provider-key", request.headers["Authorization"])
        self.assertNotIn("dummy-provider-key", request.body.decode())
        self.assertNotIn("dummy-provider-key", repr(request))

    def test_research_retries_empty_result_with_exact_supplied_sources(self) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        transport = FakeTransport(
            [
                response({"brief": "Draft", "sources": []}),
                response(
                    {
                        "brief": "Grounded brief",
                        "sources": [
                            {"title": "Solar flares", "url": first_url, "claim": "Claim one"},
                            {"title": "Space weather", "url": second_url, "claim": "Claim two"},
                        ],
                    }
                ),
            ]
        )
        storage = MemoryArtifacts()
        base = self.context()
        sourced = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": f"Solar activity {first_url} {second_url}",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 2}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(sourced)
        )

        self.assertFalse(result.requires_review)
        self.assertEqual(2, result.output_summary["sources"])
        self.assertEqual(2, len(transport.requests))
        retry_payload = json.loads(
            json.loads(transport.requests[1].body)["messages"][1]["content"]
        )
        self.assertEqual([first_url, second_url], retry_payload["supplied_source_urls"])

    def test_research_repairs_unsupported_numeric_claim_without_human_review(self) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        sources = [
            {"title": "Solar flares", "url": first_url, "claim": "Solar activity"},
            {"title": "Space weather", "url": second_url, "claim": "Earth effects"},
        ]
        transport = FakeTransport(
            [
                response({"brief": "Particles may arrive in 1 to 5 days.", "sources": sources}),
                response({"brief": "Particles may arrive after travelling through space.", "sources": sources}),
            ]
        )
        storage = MemoryArtifacts()
        base = self.context()
        sourced = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": f"Solar activity {first_url} {second_url}",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 2}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(sourced)
        )

        self.assertFalse(result.requires_review)
        self.assertEqual((), result.output_summary["validation_issues"])
        self.assertEqual(2, len(transport.requests))
        repair_payload = json.loads(
            json.loads(transport.requests[1].body)["messages"][1]["content"]
        )
        self.assertEqual(
            ["unsupported_numeric_claim:1", "unsupported_numeric_claim:5"],
            repair_payload["validation_issues"],
        )

    def test_research_discovers_sources_outside_the_topic_field(self) -> None:
        first_url = "https://records.example.test/event-2011"
        second_url = "https://records.example.test/event-2012"

        self.assertEqual(
            (first_url, second_url),
            _supplied_https_urls(
                {
                    "topic": "A concise title",
                    "angle": f"Use {first_url} and {second_url}",
                    "_framefactory": {"internal_url": "https://ignored.example.test"},
                }
            ),
        )

    def test_research_flags_source_year_and_date_span_contradictions(self) -> None:
        world_cup = "https://records.example.test/2011-world-cup"
        research = {
            "brief": (
                "2011年5月15日至2012年8月5日合计445天；"
                "2011年5月15日至2011年10月23日相隔159天。"
            ),
            "sources": [
                {
                    "title": "2011 World Cup",
                    "url": world_cup,
                    "claim": "2012年奥运会男单冠军",
                }
            ],
        }

        issues = _research_validation_issues(research, (world_cup,))

        self.assertIn("source_1_year_conflict", issues)
        self.assertIn("day_span_conflict:445", issues)
        self.assertIn("day_span_conflict:159", issues)

    def test_research_rejects_exact_numbers_not_present_or_derived_from_input(self) -> None:
        url = "https://records.example.test/athlete"
        research = {
            "brief": (
                "2011年5月15日夺冠，2011年10月1日再夺冠，"
                "2012年8月2日完成445天纪录。"
            ),
            "sources": [{"title": "Athlete", "url": url, "claim": "445天纪录"}],
        }

        issues = _research_validation_issues(
            research,
            (url,),
            {
                "topic": "445天纪录",
                "angle": "2011年5月15日至2012年8月2日相隔445天。",
            },
        )

        self.assertIn("unsupported_numeric_claim:10", issues)
        self.assertIn("unsupported_numeric_claim:1", issues)
        self.assertNotIn("unsupported_numeric_claim:445", issues)

    def test_research_rejects_unsupported_chinese_quantities(self) -> None:
        url = "https://science.example.test/space-weather"
        issues = _research_validation_issues(
            {
                "brief": "粒子以每秒数百至数千公里传播，通常数天后抵达。",
                "sources": [{"title": "Weather", "url": url, "claim": "空间天气"}],
            },
            (url,),
            {"topic": "解释空间天气"},
        )

        self.assertIn("unsupported_quantity_claim:数百至数千公里", issues)
        self.assertIn("unsupported_quantity_claim:数天", issues)

    def test_research_allows_indefinite_one_classifier(self) -> None:
        url = "https://science.example.test/space-weather"
        issues = _research_validation_issues(
            {
                "brief": "这是一个由太阳活动驱动的连续过程。",
                "sources": [{"title": "Weather", "url": url, "claim": "空间天气"}],
            },
            (url,),
            {"topic": "解释空间天气"},
        )

        self.assertNotIn("unsupported_quantity_claim:一个", issues)

    def test_research_quantity_fallback_keeps_meaning_without_false_precision(self) -> None:
        cleaned = _qualitatively_redact_research_quantities(
            {
                "brief": "粒子以每秒数百至数千公里传播，通常数天后抵达。",
                "sources": [{"title": "Weather", "url": "https://x", "claim": "数天"}],
            },
            (
                "unsupported_quantity_claim:数百至数千公里",
                "unsupported_quantity_claim:数天",
            ),
        )

        self.assertIn("较高速度", cleaned["brief"])
        self.assertNotIn("每秒较高速度", cleaned["brief"])
        self.assertIn("一段时间", cleaned["brief"])
        self.assertNotIn("数百至数千公里", cleaned["brief"])
        self.assertNotIn("数天", cleaned["sources"][0]["claim"])

    def test_script_quantity_fallback_removes_persistent_provider_precision(self) -> None:
        cleaned = _qualitatively_redact_script_quantities(
            {
                "title": "空间天气",
                "narration": "物质云持续数日，并达到数百万摄氏度。",
                "scenes": ["数日后抵达地球"],
            },
            (
                "unsupported_quantity:数日",
                "unsupported_quantity:数百万摄氏度",
            ),
        )

        self.assertEqual("物质云持续一段时间，并达到较高温度。", cleaned["narration"])
        self.assertEqual(["一段时间后抵达地球"], cleaned["scenes"])

    def test_script_flags_non_footage_and_invented_rally_outcomes(self) -> None:
        issues = _script_grounding_issues(
            {
                "narration": "张继科在决赛中夺冠。",
                "scenes": [
                    "官网页面显示无数据。",
                    "张继科发球，对手接球失误。",
                ],
            },
            {"brief": "张继科在决赛中夺冠。", "sources": [{"url": "https://x"}]},
            {"topic": "张继科夺冠", "angle": "禁止虚构具体回合"},
        )

        self.assertIn("non_footage_scene:1", issues)
        self.assertIn("unsupported_shot_detail:2:接球失误", issues)

    def test_script_rejects_quantities_missing_from_research_and_user_input(self) -> None:
        issues = _script_grounding_issues(
            {
                "narration": "能量相当于数亿颗氢弹，粒子每秒数百至数千公里，数天后抵达。",
                "scenes": ["粒子流持续数天"],
            },
            {
                "brief": "太阳活动会影响地球空间环境。",
                "sources": [{"url": "https://science.example", "claim": "存在影响"}],
            },
            {"topic": "解释太阳活动与极光"},
        )

        self.assertIn("unsupported_quantity:数亿颗", issues)
        self.assertIn("unsupported_quantity:数百至数千公里", issues)
        self.assertIn("unsupported_quantity:数天", issues)

        day_variant = _script_grounding_issues(
            {"narration": "物质云持续数日后抵达。", "scenes": []},
            {"brief": "物质云随后抵达。", "sources": [{"url": "https://science.example"}]},
            {"topic": "解释太阳活动"},
        )
        self.assertIn("unsupported_quantity:数日", day_variant)

        temperature_variant = _script_grounding_issues(
            {"narration": "日冕被加热至数百万摄氏度。", "scenes": []},
            {"brief": "日冕会被加热。", "sources": [{"url": "https://science.example"}]},
            {"topic": "解释太阳活动"},
        )
        self.assertIn("unsupported_quantity:数百万摄氏度", temperature_variant)

    def test_script_does_not_treat_a_place_name_as_globally_forbidden(self) -> None:
        issues = _script_grounding_issues(
            {"narration": "他在巴黎赢得世界杯。", "scenes": ["巴黎世界杯比赛"]},
            {"brief": "他在巴黎赢得世界杯。", "sources": [{"url": "https://x"}]},
            {"topic": "巴黎世界杯", "angle": "不得扩展退役与收入信息。"},
        )

        self.assertNotIn("forbidden_term:巴黎", issues)

    def test_structured_request_falls_back_to_json_object_when_schema_mode_is_unsupported(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(status=400, body=b'{"error":"unsupported response_format"}'),
                response({"brief": "Fallback brief", "sources": []}),
            ]
        )
        storage = MemoryArtifacts()

        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(self.context())
        )

        self.assertEqual("Fallback brief", storage.read_json(result.artifacts[0])["brief"])
        self.assertEqual(2, len(transport.requests))
        first_body = json.loads(transport.requests[0].body)
        fallback_body = json.loads(transport.requests[1].body)
        self.assertEqual("json_schema", first_body["response_format"]["type"])
        self.assertEqual("json_object", fallback_body["response_format"]["type"])
        self.assertIn("conforms exactly to this schema", fallback_body["messages"][0]["content"])

    def test_writing_reads_research_and_publishes_script(self) -> None:
        storage = MemoryArtifacts()
        seed_context = self.context()
        research = storage.publish(
            seed_context,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        transport = FakeTransport(
            [response({"title": "Title", "narration": "Words", "scenes": ["one"]})]
        )
        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(
                self.context(artifacts=(research,))
            )
        )
        self.assertEqual("script", result.artifacts[0].kind)
        script = storage.read_json(result.artifacts[0])
        self.assertEqual("Title", script["title"])
        self.assertEqual("one", script["beats"][0]["visual_description"])
        self.assertEqual([], script["beats"][0]["must_match"])

    def test_writing_revises_an_unsupported_direct_quote(self) -> None:
        storage = MemoryArtifacts()
        seed_context = self.context()
        research = storage.publish(
            seed_context,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        transport = FakeTransport(
            [
                response(
                    {
                        "title": "Title",
                        "narration": "他说：‘请帮我重建老宅。’",
                        "scenes": ["住宅入口"],
                    }
                ),
                response(
                    {
                        "title": "Title",
                        "narration": "她请他重建老宅。",
                        "scenes": ["住宅入口"],
                    }
                ),
            ]
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(
                self.context(artifacts=(research,))
            )
        )

        self.assertEqual(2, len(transport.requests))
        self.assertFalse(result.requires_review)
        self.assertEqual((), result.output_summary["grounding_issues"])
        self.assertEqual(
            "她请他重建老宅。",
            storage.read_json(result.artifacts[0])["narration"],
        )

    def test_strict_unsourced_writing_is_revised_and_held_for_review(self) -> None:
        storage = MemoryArtifacts()
        base = self.context()
        research = storage.publish(
            base,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"user supplied facts","sources":[]}',
            ),
        )
        draft = {"title": "Title", "narration": "事实转述。", "scenes": ["校园"]}
        transport = FakeTransport([response(draft), response(draft)])
        strict = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={"topic": "不得虚构细节，旁白只可转述用户提供的事实。"},
            input_artifacts=(research,),
            review_feedback="删除所有无来源的细节。",
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(strict)
        )

        self.assertEqual(2, len(transport.requests))
        self.assertTrue(result.requires_review)
        self.assertEqual(
            ("strict_grounding_requires_review",),
            result.output_summary["grounding_issues"],
        )
        for request in transport.requests:
            body = json.loads(request.body)
            payload = json.loads(body["messages"][1]["content"])
            self.assertEqual("删除所有无来源的细节。", payload["human_review_feedback"])

    def test_writing_revises_again_when_a_forbidden_term_survives(self) -> None:
        storage = MemoryArtifacts()
        base = self.context()
        research = storage.publish(
            base,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        responses = [
            {"title": "Title", "narration": "邮件里有图纸。", "scenes": ["办公室"]},
            {"title": "Title", "narration": "电脑上展开图纸。", "scenes": ["办公室"]},
            {"title": "Title", "narration": "她请他重建旧宅。", "scenes": ["办公室"]},
        ]
        transport = FakeTransport([response(item) for item in responses])
        strict = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={"topic": "不得虚构邮件或图纸，只可转述明确事实。"},
            input_artifacts=(research,),
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(strict)
        )

        self.assertEqual(3, len(transport.requests))
        self.assertTrue(result.requires_review)
        self.assertEqual(
            ("strict_grounding_requires_review",),
            result.output_summary["grounding_issues"],
        )
        self.assertEqual(
            "她请他重建旧宅。",
            storage.read_json(result.artifacts[0])["narration"],
        )

    def test_writing_revises_a_draft_that_cannot_fill_requested_duration(self) -> None:
        storage = MemoryArtifacts()
        base = self.context()
        research = storage.publish(
            base,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        valid_narration = "许昕的直板技术兼具速度与旋转。" * 10
        transport = FakeTransport(
            [
                response({"title": "Title", "narration": "太短", "scenes": ["one"]}),
                response(
                    {
                        "title": "Title",
                        "narration": valid_narration,
                        "scenes": ["0-30秒：完整人物短片"],
                    }
                ),
            ]
        )
        timed = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "许昕",
                "_framefactory": {
                    "skill_version": {
                        "writing_policy": {"target_duration_seconds": 90}
                    },
                    "composition_snapshot": {
                        "production_settings": {"target_duration_seconds": 30}
                    }
                },
            },
            input_artifacts=(research,),
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(timed)
        )

        self.assertEqual(2, len(transport.requests))
        self.assertEqual(valid_narration, storage.read_json(result.artifacts[0])["narration"])
        first_body = json.loads(transport.requests[0].body)
        provider_payload = json.loads(first_body["messages"][1]["content"])
        self.assertEqual(
            30,
            provider_payload["run"]["_framefactory"]["skill_version"]
            ["writing_policy"]["target_duration_seconds"],
        )
        self.assertEqual(
            90,
            timed.input_snapshot.to_dict()["_framefactory"]["skill_version"]
            ["writing_policy"]["target_duration_seconds"],
        )

    def test_writing_duration_mismatch_preserves_the_draft_for_review(self) -> None:
        storage = MemoryArtifacts()
        base = self.context()
        research = storage.publish(
            base,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        transport = FakeTransport(
            [
                response({"title": "Title", "narration": "太短", "scenes": ["one"]}),
                response({"title": "Title", "narration": "依然太短", "scenes": ["one"]}),
                response({"title": "Title", "narration": "还是太短", "scenes": ["one"]}),
            ]
        )
        timed = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "short",
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"target_duration_seconds": 30}
                    }
                },
            },
            input_artifacts=(research,),
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(timed)
        )

        self.assertTrue(result.requires_review)
        self.assertEqual(4, result.output_summary["narration_characters"])
        self.assertEqual((105, 195), result.output_summary["narration_character_bounds"])
        self.assertFalse(result.output_summary["duration_fit"])
        self.assertEqual(3, len(transport.requests))
        self.assertEqual("还是太短", storage.read_json(result.artifacts[0])["narration"])

    def test_overlong_writing_is_trimmed_at_a_sentence_boundary(self) -> None:
        storage = MemoryArtifacts()
        base = self.context()
        research = storage.publish(
            base,
            ProviderArtifact(
                "research",
                "research.json",
                "application/json",
                b'{"brief":"facts","sources":[]}',
            ),
        )
        sentence = "太阳活动影响空间天气。"
        overlong = sentence * 40
        transport = FakeTransport(
            [
                response({"title": "Title", "narration": overlong, "scenes": ["太阳"]}),
                response({"title": "Title", "narration": overlong, "scenes": ["太阳"]}),
                response({"title": "Title", "narration": overlong, "scenes": ["太阳"]}),
            ]
        )
        timed = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "太阳活动",
                "_framefactory": {
                    "composition_snapshot": {
                        "production_settings": {"target_duration_seconds": 30}
                    }
                },
            },
            input_artifacts=(research,),
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(timed)
        )
        narration = storage.read_json(result.artifacts[0])["narration"]

        self.assertFalse(result.requires_review)
        self.assertTrue(narration.endswith("。"))
        self.assertGreaterEqual(result.output_summary["narration_characters"], 105)
        self.assertLessEqual(result.output_summary["narration_characters"], 195)

    def test_retryable_and_permanent_http_classes_do_not_leak_body_or_key(self) -> None:
        for status, error in ((429, RetryableStepError), (401, PermanentStepError)):
            with self.subTest(status=status):
                transport = FakeTransport([HttpResponse(status, b"secret provider body")])
                with self.assertRaises(error) as caught:
                    asyncio.run(
                        self.client(transport).structured(
                            operation="research.collect",
                            system="safe",
                            payload={},
                            schema={"type": "object"},
                        )
                    )
                self.assertNotIn("secret provider body", str(caught.exception))
                self.assertNotIn("dummy-provider-key", str(caught.exception))

    def test_structured_response_is_validated_locally(self) -> None:
        transport = FakeTransport([response({"unexpected": "value"})])
        with self.assertRaisesRegex(PermanentStepError, "missing brief"):
            asyncio.run(
                OpenAIResearchCapability(self.client(transport), MemoryArtifacts()).execute(
                    self.context()
                )
            )

    def test_quality_is_truthfully_metadata_only_and_requires_review(self) -> None:
        video = ArtifactRef(
            id=str(uuid4()),
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id=str(uuid4()),
            kind="video",
            media_type="video/mp4",
            object_key=(
                "workspaces/11111111-1111-4111-8111-111111111111/"
                "runs/22222222-2222-4222-8222-222222222222/artifacts/video/video.mp4"
            ),
            byte_size=100,
            content_hash="b" * 64,
            filename="video.mp4",
        )
        storage = MemoryArtifacts()
        transport = FakeTransport([response({"risks": ["playback not inspected"], "notes": "manual"})])
        result = asyncio.run(
            OpenAIQualityCapability(self.client(transport), storage).execute(
                self.context(artifacts=(video,))
            )
        )
        self.assertTrue(result.requires_review)
        self.assertEqual("metadata_only", result.summary_dict()["scope"])
        self.assertEqual(
            "needs_manual_review",
            storage.read_json(result.artifacts[0])["verdict"],
        )


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.last_body: FakeBody | None = None

    def head_bucket(self, **_: Any) -> None:
        return None

    def put_object(self, *, Key: str, Body: bytes, Metadata: dict[str, str], **_: Any) -> None:
        self.objects[Key] = (Body, Metadata)

    def get_object(self, *, Key: str, **_: Any) -> dict[str, Any]:
        self.last_body = FakeBody(self.objects[Key][0])
        return {"Body": self.last_body}

    def head_object(self, *, Key: str, **_: Any) -> dict[str, Any]:
        if Key not in self.objects:
            raise KeyError(Key)
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": metadata}

    def copy_object(
        self,
        *,
        Key: str,
        CopySource: dict[str, str],
        Metadata: dict[str, str],
        **_: Any,
    ) -> None:
        source = self.objects[CopySource["Key"]][0]
        self.objects[Key] = (source, Metadata)


class Recorder:
    def __init__(self) -> None:
        self.values: list[ArtifactRef] = []

    def record_artifact(self, artifact: ArtifactRef, *, bucket: str) -> None:
        self.values.append(artifact)
        self.bucket = bucket


class S3ArtifactStorageTests(unittest.TestCase):
    def test_publishes_content_addressed_object_and_records_reference(self) -> None:
        s3 = FakeS3()
        recorder = Recorder()
        storage = S3ArtifactStorage(
            ObjectStorageSettings(bucket="artifacts"), recorder=recorder, client=s3
        )
        context = StepContext(
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="run:research",
            input_snapshot={},
        )
        artifact = storage.publish(
            context,
            ProviderArtifact(
                "research", "research.json", "application/json", b'{"brief":"real"}'
            ),
        )
        repeated = storage.publish(
            context,
            ProviderArtifact(
                "research", "research.json", "application/json", b'{"brief":"real"}'
            ),
        )

        self.assertEqual("artifacts", recorder.bucket)
        self.assertEqual(artifact, recorder.values[0])
        self.assertEqual(artifact.id, repeated.id)
        self.assertEqual("run:research", artifact.step_id)
        self.assertEqual(artifact.object_key, repeated.object_key)
        self.assertEqual({"brief": "real"}, storage.read_json(artifact))
        self.assertIn(f"/artifacts/{artifact.id}/research.json", artifact.object_key)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "research.json"
            storage.materialize(artifact, destination)
            self.assertEqual(b'{"brief":"real"}', destination.read_bytes())
            self.assertTrue(s3.last_body and s3.last_body.closed)

    def test_copies_existing_object_without_downloading_it(self) -> None:
        payload = b"large-video-placeholder"
        digest = hashlib.sha256(payload).hexdigest()
        s3 = FakeS3()
        s3.objects["library/movie.mkv"] = (payload, {"sha256": digest})
        recorder = Recorder()
        storage = S3ArtifactStorage(
            ObjectStorageSettings(bucket="artifacts"), recorder=recorder, client=s3
        )
        context = StepContext(
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="run:media",
            input_snapshot={},
        )

        artifact = storage.publish_copy(
            context,
            kind="asset",
            filename="asset-001.mkv",
            media_type="video/matroska",
            source_bucket="artifacts",
            source_key="library/movie.mkv",
            content_hash=digest,
            byte_size=len(payload),
        )

        self.assertEqual(payload, s3.objects[artifact.object_key][0])
        self.assertIsNone(s3.last_body)
        self.assertEqual(artifact, recorder.values[-1])


if __name__ == "__main__":
    unittest.main()
