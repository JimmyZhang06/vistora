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
    HttpsJsonResearchSearchGateway,
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
from framefactory.worker.capabilities import configured_capabilities
from framefactory.worker.config import (
    ObjectStorageSettings,
    OpenAICompatibleSettings,
    ResearchSearchSettings,
    WorkerSettings,
)
from framefactory.worker.providers import ProviderArtifact


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeResearchSearch:
    def __init__(self, results: tuple[dict[str, str], ...]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    async def search(self, *, query: str, limit: int):
        self.calls.append({"query": query, "limit": limit})
        return self.results[:limit]


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
            OpenAIResearchCapability(self.client(transport), storage).execute(
                self.context()
            )
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

    def test_research_off_never_calls_model_or_search(self) -> None:
        url = "https://source.example/offline"
        storage = MemoryArtifacts()
        transport = FakeTransport([])
        search = FakeResearchSearch(
            (
                {
                    "title": "Must not run",
                    "url": "https://search.example/x",
                    "claim": "x",
                },
            )
        )
        base = self.context()
        offline = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "User-authored offline facts",
                "source_urls": [url],
                "research_mode": "off",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 1}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(
                self.client(transport), storage, search_gateway=search
            ).execute(offline)
        )

        self.assertEqual([], transport.requests)
        self.assertEqual([], search.calls)
        self.assertFalse(result.requires_review)
        self.assertFalse(storage.read_json(result.artifacts[0])["network_accessed"])

    def test_research_when_missing_fails_typed_without_real_search(self) -> None:
        base = self.context()
        missing = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "Needs evidence",
                "research_mode": "when_missing",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 1}}
                },
            },
        )
        transport = FakeTransport([])

        with self.assertRaises(PermanentStepError) as raised:
            asyncio.run(
                OpenAIResearchCapability(
                    self.client(transport), MemoryArtifacts()
                ).execute(missing)
            )

        self.assertEqual("research_search_unavailable", raised.exception.code)
        self.assertEqual([], transport.requests)

    def test_research_when_missing_skips_search_when_supplied_sources_are_enough(
        self,
    ) -> None:
        source = "https://source.example/supplied"
        search = FakeResearchSearch(
            ({"title": "Unused", "url": "https://search.example/unused", "claim": "x"},)
        )
        transport = FakeTransport(
            [
                response(
                    {
                        "brief": "Grounded brief",
                        "sources": [
                            {
                                "title": "Supplied",
                                "url": source,
                                "claim": "Grounded claim",
                            }
                        ],
                    }
                )
            ]
        )
        base = self.context()
        enough = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "Evidence topic",
                "source_urls": [source, source + "#duplicate"],
                "research_mode": "when_missing",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 1}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(
                self.client(transport), MemoryArtifacts(), search_gateway=search
            ).execute(enough)
        )

        self.assertEqual([], search.calls)
        self.assertFalse(result.output_summary["search_attempted"])

    def test_research_required_uses_only_gateway_https_results(self) -> None:
        source = "https://search.example/evidence"
        search = FakeResearchSearch(
            ({"title": "Evidence", "url": source, "snippet": "Grounded claim"},)
        )
        transport = FakeTransport(
            [
                response(
                    {
                        "brief": "Grounded brief",
                        "sources": [
                            {
                                "title": "Evidence",
                                "url": source,
                                "claim": "Grounded claim",
                            }
                        ],
                    }
                )
            ]
        )
        storage = MemoryArtifacts()
        base = self.context()
        required = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "Evidence topic",
                "research_mode": "required",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 1}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(
                self.client(transport), storage, search_gateway=search
            ).execute(required)
        )

        self.assertEqual([{"query": "Evidence topic", "limit": 1}], search.calls)
        self.assertEqual(
            source, storage.read_json(result.artifacts[0])["sources"][0]["url"]
        )
        self.assertFalse(result.requires_review)

    def test_https_json_search_gateway_bounds_and_authenticates_request(self) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    200,
                    json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Result",
                                    "url": "https://source.example/result",
                                    "snippet": "Claim",
                                }
                            ]
                        }
                    ).encode(),
                )
            ]
        )
        gateway = HttpsJsonResearchSearchGateway(
            url="https://search.example/v1/search",
            bearer_token="search-secret",
            timeout_seconds=5,
            maximum_response_bytes=4096,
            transport=transport,
        )

        results = asyncio.run(gateway.search(query="topic", limit=1))

        self.assertEqual("https://source.example/result", results[0]["url"])
        request = transport.requests[0]
        self.assertEqual({"query": "topic", "limit": 1}, json.loads(request.body))
        self.assertEqual("Bearer search-secret", request.headers["Authorization"])
        self.assertNotIn("search-secret", repr(request))

    def test_configured_capabilities_injects_configured_search_gateway(self) -> None:
        settings = WorkerSettings(
            database_url="postgresql://user:pass@db/framefactory",
            redis_url="redis://redis/0",
            environment="test",
            worker_id="worker-test",
            openai_compatible=OpenAICompatibleSettings(
                base_url="https://models.example/v1",
                api_key="model-secret",
                research_model="research",
                writing_model="writing",
                quality_model="quality",
            ),
            research_search=ResearchSearchSettings(
                url="https://search.example/v1/search",
                bearer_token="search-secret",
            ),
            object_storage=ObjectStorageSettings(bucket="artifacts"),
        )

        registry = configured_capabilities(
            settings,
            artifact_storage=MemoryArtifacts(),
            transport=FakeTransport([]),
        )
        capability = registry.resolve("research.collect")

        self.assertIsInstance(capability, OpenAIResearchCapability)
        self.assertIsInstance(capability.search_gateway, HttpsJsonResearchSearchGateway)

    def test_optional_research_policy_accepts_zero_sources_without_review(self) -> None:
        transport = FakeTransport(
            [response({"brief": "A fictional visual story brief", "sources": []})]
        )
        storage = MemoryArtifacts()
        base = self.context()
        optional = StepContext(
            workspace_id=base.workspace_id,
            run_id=base.run_id,
            step_id=base.step_id,
            input_snapshot={
                "topic": "A fictional floating city",
                "_framefactory": {
                    "skill_version": {"research_policy": {"minimum_sources": 0}}
                },
            },
        )

        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(optional)
        )

        self.assertFalse(result.requires_review)
        self.assertEqual(0, result.output_summary["minimum_sources"])
        self.assertEqual(0, result.output_summary["sources"])
        self.assertEqual(1, len(transport.requests))

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
                            {
                                "title": "Solar flares",
                                "url": first_url,
                                "claim": "Claim one",
                            },
                            {
                                "title": "Space weather",
                                "url": second_url,
                                "claim": "Claim two",
                            },
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

    def test_research_counts_unique_allowed_https_urls_and_repairs_duplicates(
        self,
    ) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        duplicate = {
            "title": "Solar flares duplicate",
            "url": first_url,
            "claim": "A second claim does not make a second source",
        }
        transport = FakeTransport(
            [
                response(
                    {
                        "brief": "Draft",
                        "sources": [
                            {
                                "title": "Solar flares",
                                "url": first_url,
                                "claim": "Claim one",
                            },
                            duplicate,
                            {
                                "title": "Invented",
                                "url": "https://not-supplied.example.test/source",
                                "claim": "Not allowed",
                            },
                        ],
                    }
                ),
                response(
                    {
                        "brief": "Grounded brief",
                        "sources": [
                            {
                                "title": "Solar flares",
                                "url": first_url,
                                "claim": "Claim one",
                            },
                            {
                                "title": "Space weather",
                                "url": second_url,
                                "claim": "Claim two",
                            },
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
        research = storage.read_json(result.artifacts[0])

        self.assertFalse(result.requires_review)
        self.assertEqual(2, result.output_summary["sources"])
        self.assertEqual(
            [first_url, second_url], [item["url"] for item in research["sources"]]
        )
        self.assertEqual(2, len(transport.requests))

    def test_research_duplicate_urls_remain_one_source_and_require_review(self) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        duplicate_result = {
            "brief": "Draft",
            "sources": [
                {"title": "One", "url": first_url, "claim": "Claim one"},
                {
                    "title": "Two",
                    "url": f"{first_url}/#duplicate-claim",
                    "claim": "Claim two",
                },
                {
                    "title": "Off list",
                    "url": "https://not-supplied.example.test/source",
                    "claim": "Not allowed",
                },
                {
                    "title": "Not HTTPS",
                    "url": "http://science.example.test/insecure",
                    "claim": "Not allowed",
                },
            ],
        }
        transport = FakeTransport(
            [response(duplicate_result), response(duplicate_result)]
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
        research = storage.read_json(result.artifacts[0])

        self.assertTrue(result.requires_review)
        self.assertEqual(1, result.output_summary["sources"])
        self.assertEqual(1, result.output_summary["unique_sources"])
        self.assertEqual([first_url], [item["url"] for item in research["sources"]])
        self.assertEqual(2, len(transport.requests))

    def test_research_recovers_allowed_source_already_cited_in_brief(self) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        incomplete = {
            "brief": (
                f"References: [{first_url}]({first_url})\n"
                f"- [{second_url}]({second_url})"
            ),
            "sources": [
                {"title": "Solar flares", "url": first_url, "claim": "Claim one"},
            ],
        }
        transport = FakeTransport([response(incomplete), response(incomplete)])
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
        research = storage.read_json(result.artifacts[0])

        self.assertFalse(result.requires_review)
        self.assertEqual(1, result.output_summary["recovered_brief_sources"])
        self.assertEqual(
            [first_url, second_url],
            [item["url"] for item in research["sources"]],
        )
        self.assertIn("downstream fact verification", research["sources"][1]["claim"])

    def test_research_repairs_unsupported_numeric_claim_without_human_review(
        self,
    ) -> None:
        first_url = "https://science.example.test/solar-flares"
        second_url = "https://science.example.test/space-weather"
        sources = [
            {"title": "Solar flares", "url": first_url, "claim": "Solar activity"},
            {"title": "Space weather", "url": second_url, "claim": "Earth effects"},
        ]
        transport = FakeTransport(
            [
                response(
                    {
                        "brief": "Particles may arrive in 1 to 5 days.",
                        "sources": sources,
                    }
                ),
                response(
                    {
                        "brief": "Particles may arrive after travelling through space.",
                        "sources": sources,
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

    def test_research_rejects_exact_numbers_not_present_or_derived_from_input(
        self,
    ) -> None:
        url = "https://records.example.test/athlete"
        research = {
            "brief": (
                "2011年5月15日夺冠，2011年10月1日再夺冠，2012年8月2日完成445天纪录。"
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

    def test_research_quantity_fallback_keeps_meaning_without_false_precision(
        self,
    ) -> None:
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

    def test_research_numeric_fallback_cleans_real_brief_without_touching_sources(
        self,
    ) -> None:
        overview_url = "https://www.nasa.gov/history/apollo-11-mission-overview/"
        mission_url = "https://www.nasa.gov/mission/apollo-11/"
        run_input = {
            "topic": "阿波罗11号任务",
            "angle": (
                "阿波罗11号于1969年7月16日发射，1969年7月20日登月，"
                "并于1969年7月24日返回。"
            ),
            "source_urls": [overview_url, mission_url],
        }
        research = {
            "brief": (
                "阿波罗11号于1969年7月16日发射，1969年7月20日登月，"
                "并于1969年7月24日返回。首次月面行走时间："
                "美国东部时间7月20日20:56。"
                f"参考：{overview_url} 与 {mission_url}。"
                "两条来源均符合‘require_https’与‘freshness_days=365’要求。"
            ),
            "sources": [
                {
                    "title": "Apollo 11 Mission Overview",
                    "url": overview_url,
                    "claim": "1969年7月16日发射；7月20日20:56进行月面活动。",
                },
                {
                    "title": "Apollo 11",
                    "url": mission_url,
                    "claim": "1969年7月24日返回，符合 freshness_days=365 要求。",
                },
            ],
        }

        issues = _research_validation_issues(
            research,
            (overview_url, mission_url),
            run_input,
        )
        cleaned = _qualitatively_redact_research_quantities(research, issues)

        self.assertEqual(
            ("unsupported_numeric_claim:56", "unsupported_numeric_claim:365"),
            issues,
        )
        self.assertEqual(
            [overview_url, mission_url],
            [item["url"] for item in cleaned["sources"]],
        )
        self.assertIn(overview_url, cleaned["brief"])
        self.assertIn(mission_url, cleaned["brief"])
        for allowed_date in (
            "1969年7月16日",
            "1969年7月20日",
            "1969年7月24日",
        ):
            self.assertIn(allowed_date, cleaned["brief"])
        self.assertNotIn("20:56", cleaned["brief"])
        self.assertIn("7月20日具体时刻", cleaned["brief"])
        self.assertNotIn("freshness_days=365", cleaned["brief"])
        self.assertIn("来源时效要求", cleaned["brief"])
        cleaned_claims = " ".join(item["claim"] for item in cleaned["sources"])
        self.assertNotIn("20:56", cleaned_claims)
        self.assertNotIn("365", cleaned_claims)
        self.assertIn("具体时刻", cleaned_claims)
        self.assertIn("来源时效要求", cleaned_claims)
        self.assertEqual(
            (),
            _research_validation_issues(
                cleaned,
                (overview_url, mission_url),
                run_input,
            ),
        )

    def test_research_numeric_fallback_distinguishes_aspect_ratios_from_clock(
        self,
    ) -> None:
        research = {
            "brief": (
                "短片时长控制在一段时间内，采用 9:16 竖屏格式，语言为中文；"
                "横版可使用16:9横屏格式，方形版使用1:1方形格式，"
                "传统横版可使用4:3横屏格式，传统竖版可使用3:4竖屏格式。"
                "首次活动发生在7月20日20:56。"
            ),
            "sources": [],
        }
        run_input = {"angle": "仅允许陈述7月20日发生首次活动。"}

        issues = _research_validation_issues(research, (), run_input)
        cleaned = _qualitatively_redact_research_quantities(research, issues)

        self.assertEqual(
            {
                "unsupported_numeric_claim:1",
                "unsupported_numeric_claim:3",
                "unsupported_numeric_claim:4",
                "unsupported_numeric_claim:9",
                "unsupported_numeric_claim:16",
                "unsupported_numeric_claim:56",
            },
            set(issues),
        )
        self.assertIn("采用 竖屏画幅", cleaned["brief"])
        self.assertEqual(2, cleaned["brief"].count("竖屏画幅"))
        self.assertEqual(2, cleaned["brief"].count("横屏画幅"))
        self.assertEqual(1, cleaned["brief"].count("方形画幅"))
        for ratio in ("9:16", "16:9", "1:1", "4:3", "3:4"):
            self.assertNotIn(ratio, cleaned["brief"])
        self.assertIn("7月20日具体时刻", cleaned["brief"])
        self.assertNotIn("20:56", cleaned["brief"])
        self.assertEqual(
            (),
            _research_validation_issues(cleaned, (), run_input),
        )

    def test_script_quantity_fallback_removes_persistent_provider_precision(
        self,
    ) -> None:
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

    def test_script_quantity_fallback_collapses_real_consecutive_date(self) -> None:
        narration = (
            "1969年7月20日，人类首次登月任务成功着陆月面。"
            "两名宇航员执行舱外活动，采集样本并部署科学设备。"
            "另一名宇航员在轨道上运行。任务完成，为载人登月树立里程碑。"
        )
        script = {
            "title": "阿波罗时代月面活动视觉档案",
            "narration": narration,
            "scenes": ["1969年，7月，20日的月面活动档案"],
        }
        research = {
            "brief": "阿波罗任务完成登月并开展月面活动。",
            "sources": [{"url": "https://www.nasa.gov/mission/apollo-11/"}],
        }
        run = {"topic": "NASA 阿波罗时代月面活动的视觉档案"}

        issues = _script_grounding_issues(script, research, run)
        cleaned = _qualitatively_redact_script_quantities(script, issues)

        self.assertEqual(
            (
                "unsupported_quantity:1969年",
                "unsupported_quantity:7月",
                "unsupported_quantity:20日",
            ),
            issues,
        )
        self.assertEqual(
            (
                "一段时间，人类首次登月任务成功着陆月面。"
                "两名宇航员执行舱外活动，采集样本并部署科学设备。"
                "另一名宇航员在轨道上运行。任务完成，为载人登月树立里程碑。"
            ),
            cleaned["narration"],
        )
        self.assertEqual(["一段时间的月面活动档案"], cleaned["scenes"])
        self.assertEqual(
            (),
            _script_grounding_issues(cleaned, research, run),
        )

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

    def test_script_rejects_quantities_missing_from_research_and_user_input(
        self,
    ) -> None:
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
            {
                "brief": "物质云随后抵达。",
                "sources": [{"url": "https://science.example"}],
            },
            {"topic": "解释太阳活动"},
        )
        self.assertIn("unsupported_quantity:数日", day_variant)

        temperature_variant = _script_grounding_issues(
            {"narration": "日冕被加热至数百万摄氏度。", "scenes": []},
            {
                "brief": "日冕会被加热。",
                "sources": [{"url": "https://science.example"}],
            },
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

    def test_structured_request_falls_back_to_json_object_when_schema_mode_is_unsupported(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                HttpResponse(
                    status=400, body=b'{"error":"unsupported response_format"}'
                ),
                response({"brief": "Fallback brief", "sources": []}),
            ]
        )
        storage = MemoryArtifacts()

        result = asyncio.run(
            OpenAIResearchCapability(self.client(transport), storage).execute(
                self.context()
            )
        )

        self.assertEqual(
            "Fallback brief", storage.read_json(result.artifacts[0])["brief"]
        )
        self.assertEqual(2, len(transport.requests))
        first_body = json.loads(transport.requests[0].body)
        fallback_body = json.loads(transport.requests[1].body)
        self.assertEqual("json_schema", first_body["response_format"]["type"])
        self.assertEqual("json_object", fallback_body["response_format"]["type"])
        self.assertIn(
            "conforms exactly to this schema", fallback_body["messages"][0]["content"]
        )

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

    def test_writing_passes_frozen_inventory_summary_to_the_model(self) -> None:
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
        inventory = storage.publish(
            seed_context,
            ProviderArtifact(
                "inventory",
                "inventory.json",
                "application/json",
                b'{"catalog_snapshot_id":"11111111-1111-4111-8111-111111111111","coverage":{"status":"partial","missing_concepts":["night"]},"concepts":[]}',
            ),
        )
        transport = FakeTransport(
            [response({"title": "Title", "narration": "Words", "scenes": ["one"]})]
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(
                self.context(artifacts=(research, inventory))
            )
        )

        payload = json.loads(
            json.loads(transport.requests[0].body)["messages"][1]["content"]
        )
        self.assertEqual("partial", payload["inventory"]["coverage"]["status"])
        self.assertTrue(result.output_summary["inventory_constrained"])
        self.assertEqual(
            ("night",), result.output_summary["inventory_missing_concepts"]
        )

    def test_writing_repairs_repeated_full_narration_and_keeps_visual_evidence(
        self,
    ) -> None:
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

        narration = "Alpha beta. Gamma delta."
        transport = FakeTransport(
            [
                response(
                    {
                        "title": "Title",
                        "narration": narration,
                        "scenes": ["first visual", "second visual"],
                        "beats": [
                            {
                                "id": "duplicate-id",
                                "sequence": 20,
                                "narration": narration,
                                "visual_description": "second visual",
                                "must_match": ["second"],
                                "must_not_match": ["forbidden-second"],
                            },
                            {
                                "id": "duplicate-id",
                                "sequence": 5,
                                "narration": narration,
                                "visual_description": "first visual",
                                "must_match": ["first"],
                                "must_not_match": ["forbidden-first"],
                            },
                        ],
                    }
                )
            ]
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(
                self.context(artifacts=(research,))
            )
        )
        script = storage.read_json(result.artifacts[0])

        self.assertFalse(result.requires_review)
        self.assertEqual(
            ["Alpha beta.", "Gamma delta."],
            [beat["narration"] for beat in script["beats"]],
        )
        self.assertEqual([1, 2], [beat["sequence"] for beat in script["beats"]])
        self.assertEqual(2, len({beat["id"] for beat in script["beats"]}))
        self.assertEqual(
            ["first visual", "second visual"],
            [beat["visual_description"] for beat in script["beats"]],
        )
        self.assertEqual(["first"], script["beats"][0]["must_match"])
        self.assertEqual(["forbidden-second"], script["beats"][1]["must_not_match"])

    def test_supplied_sources_deduplicate_equivalent_https_urls(self) -> None:
        first = "https://EXAMPLE.test:443/source/#first"
        equivalent = "HTTPS://example.test/source#second"
        other = "https://example.test/other"

        self.assertEqual(
            (first, other),
            _supplied_https_urls(
                {
                    "topic": f"Use {first} and {equivalent} and {other}",
                    "angle": "Ignore http://example.test/not-https",
                }
            ),
        )

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
                    },
                },
            },
            input_artifacts=(research,),
        )

        result = asyncio.run(
            OpenAIWritingCapability(self.client(transport), storage).execute(timed)
        )

        self.assertEqual(2, len(transport.requests))
        self.assertEqual(
            valid_narration, storage.read_json(result.artifacts[0])["narration"]
        )
        first_body = json.loads(transport.requests[0].body)
        provider_payload = json.loads(first_body["messages"][1]["content"])
        self.assertEqual(
            30,
            provider_payload["run"]["_framefactory"]["skill_version"]["writing_policy"][
                "target_duration_seconds"
            ],
        )
        self.assertEqual(
            90,
            timed.input_snapshot.to_dict()["_framefactory"]["skill_version"][
                "writing_policy"
            ]["target_duration_seconds"],
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
                response(
                    {"title": "Title", "narration": "依然太短", "scenes": ["one"]}
                ),
                response(
                    {"title": "Title", "narration": "还是太短", "scenes": ["one"]}
                ),
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
        self.assertEqual(
            (105, 195), result.output_summary["narration_character_bounds"]
        )
        self.assertFalse(result.output_summary["duration_fit"])
        self.assertEqual(3, len(transport.requests))
        self.assertEqual(
            "还是太短", storage.read_json(result.artifacts[0])["narration"]
        )

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
                transport = FakeTransport(
                    [HttpResponse(status, b"secret provider body")]
                )
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
                OpenAIResearchCapability(
                    self.client(transport), MemoryArtifacts()
                ).execute(self.context())
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
        transport = FakeTransport(
            [response({"risks": ["playback not inspected"], "notes": "manual"})]
        )
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

    def put_object(
        self, *, Key: str, Body: bytes, Metadata: dict[str, str], **_: Any
    ) -> None:
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

    def delete_object(self, *, Key: str, **_: Any) -> None:
        self.objects.pop(Key, None)


class Recorder:
    def __init__(self) -> None:
        self.values: list[ArtifactRef] = []

    def record_artifact(self, artifact: ArtifactRef, *, bucket: str) -> None:
        self.values.append(artifact)
        self.bucket = bucket


class FailingRecorder:
    def record_artifact(self, artifact: ArtifactRef, *, bucket: str) -> None:
        del artifact, bucket
        raise PermanentStepError(
            "artifact object key violates tenant isolation",
            code="artifact_tenant_key_violation",
        )


class S3ArtifactStorageTests(unittest.TestCase):
    def test_removes_new_object_when_durable_record_fails(self) -> None:
        s3 = FakeS3()
        storage = S3ArtifactStorage(
            ObjectStorageSettings(bucket="artifacts", key_prefix="browser-capture"),
            recorder=FailingRecorder(),
            client=s3,
        )
        context = StepContext(
            workspace_id="11111111-1111-4111-8111-111111111111",
            run_id="22222222-2222-4222-8222-222222222222",
            step_id="run:validate",
            input_snapshot={},
        )

        with self.assertRaises(PermanentStepError) as raised:
            storage.publish(
                context,
                ProviderArtifact(
                    "manifest",
                    "capture-validation.json",
                    "application/json",
                    b'{"valid":true}',
                ),
            )

        self.assertEqual("artifact_tenant_key_violation", raised.exception.code)
        self.assertEqual({}, s3.objects)

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
        original_object = s3.objects[artifact.object_key]
        renamed = storage.publish(
            context,
            ProviderArtifact(
                "research",
                "research-retry.json",
                "application/json",
                b'{"brief":"real"}',
            ),
        )

        self.assertEqual("artifacts", recorder.bucket)
        self.assertEqual(artifact, recorder.values[0])
        self.assertEqual(artifact.id, repeated.id)
        self.assertEqual("run:research", artifact.step_id)
        self.assertEqual(artifact.object_key, repeated.object_key)
        self.assertNotEqual(artifact.id, renamed.id)
        self.assertNotEqual(artifact.object_key, renamed.object_key)
        self.assertEqual(original_object, s3.objects[artifact.object_key])
        self.assertEqual(2, len(s3.objects))
        self.assertEqual({"brief": "real"}, storage.read_json(artifact))
        self.assertIn(f"/artifacts/{artifact.id}/research.json", artifact.object_key)

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "research.json"
            storage.materialize(artifact, destination)
            self.assertEqual(b'{"brief":"real"}', destination.read_bytes())
            self.assertTrue(s3.last_body and s3.last_body.closed)

    def test_copy_retry_uses_filename_identity_without_overwriting_old_object(
        self,
    ) -> None:
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
        repeated = storage.publish_copy(
            context,
            kind="asset",
            filename="asset-001.mkv",
            media_type="video/matroska",
            source_bucket="artifacts",
            source_key="library/movie.mkv",
            content_hash=digest,
            byte_size=len(payload),
        )
        original_object = s3.objects[artifact.object_key]
        reordered_retry = storage.publish_copy(
            context,
            kind="asset",
            filename="asset-002.mkv",
            media_type="video/matroska",
            source_bucket="artifacts",
            source_key="library/movie.mkv",
            content_hash=digest,
            byte_size=len(payload),
        )

        self.assertEqual(payload, s3.objects[artifact.object_key][0])
        self.assertIsNone(s3.last_body)
        self.assertEqual(artifact, recorder.values[0])
        self.assertEqual(reordered_retry, recorder.values[-1])
        self.assertEqual(artifact.id, repeated.id)
        self.assertEqual(artifact.object_key, repeated.object_key)
        self.assertNotEqual(artifact.id, reordered_retry.id)
        self.assertNotEqual(artifact.object_key, reordered_retry.object_key)
        self.assertEqual(original_object, s3.objects[artifact.object_key])
        self.assertEqual(3, len(s3.objects))


if __name__ == "__main__":
    unittest.main()
