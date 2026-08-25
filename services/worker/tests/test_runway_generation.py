from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import dataclass

from framefactory.worker.generation import (
    RUNWAY_API_VERSION,
    RunwayClient,
    RunwayHttpResponse,
    RunwayPermanentError,
    RunwayRetryableError,
    RunwaySubmitUnknown,
    RunwayTaskStatus,
    RunwayTextVideoRequest,
)
from framefactory.worker.generation.runway import RunwayTransportFailure

TASK_ID = "d2e3d1f4-1b3c-4b5c-8d46-1c1d7ee86892"


@dataclass(frozen=True)
class RequestRecord:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    maximum_response_bytes: int


class FakeTransport:
    def __init__(self, *responses: RunwayHttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[RequestRecord] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> RunwayHttpResponse:
        del timeout_seconds
        self.requests.append(
            RequestRecord(method, url, dict(headers), body, maximum_response_bytes)
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(
    status: int,
    value: object,
    *,
    headers: Mapping[str, str] | None = None,
    url: str = "https://api.dev.runwayml.com/v1/text_to_video",
) -> RunwayHttpResponse:
    return RunwayHttpResponse(
        status=status,
        headers=dict(headers or {"content-type": "application/json"}),
        body=(
            value
            if isinstance(value, bytes)
            else json.dumps(value, separators=(",", ":")).encode()
        ),
        final_url=url,
    )


def request() -> RunwayTextVideoRequest:
    return RunwayTextVideoRequest(
        prompt_text="A cinematic mountain sunrise with a slow camera push.",
        ratio="1280:720",
        duration=5,
        seed=42,
    )


class RunwayClientTests(unittest.TestCase):
    def client(self, transport: FakeTransport) -> RunwayClient:
        return RunwayClient(
            base_url="https://api.dev.runwayml.com",
            api_key="secret-for-test",
            timeout_seconds=10,
            transport=transport,
        )

    def test_client_rejects_plaintext_base_url_even_with_fake_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            RunwayClient(
                base_url="http://api.dev.runwayml.com",
                api_key="secret",
                timeout_seconds=10,
                transport=FakeTransport(),
            )

    def test_submit_uses_documented_text_to_video_shape_without_idempotency_header(self) -> None:
        transport = FakeTransport(
            response(200, {"id": TASK_ID, "estimatedCost": {"credits": 60}})
        )

        receipt = self.client(transport).submit_text_to_video(request())

        self.assertEqual(TASK_ID, receipt.task_id)
        self.assertEqual(60, receipt.estimated_cost_credits)
        sent = transport.requests[0]
        self.assertEqual("POST", sent.method)
        self.assertEqual(
            "https://api.dev.runwayml.com/v1/text_to_video", sent.url
        )
        self.assertEqual(RUNWAY_API_VERSION, sent.headers["X-Runway-Version"])
        self.assertEqual("Bearer secret-for-test", sent.headers["Authorization"])
        self.assertNotIn("Idempotency-Key", sent.headers)
        self.assertNotIn("X-Request-Id", sent.headers)
        self.assertEqual(
            {
                "duration": 5,
                "model": "gen4.5",
                "outputFormat": "mp4",
                "promptText": request().prompt_text,
                "ratio": "1280:720",
                "seed": 42,
            },
            json.loads(sent.body or b"{}"),
        )

    def test_submit_transport_failure_is_unknown_and_must_not_be_retried(self) -> None:
        transport = FakeTransport(RunwayTransportFailure("timeout"))

        with self.assertRaises(RunwaySubmitUnknown):
            self.client(transport).submit_text_to_video(request())

        self.assertEqual(1, len(transport.requests))

    def test_submit_malformed_success_is_unknown_because_task_may_exist(self) -> None:
        transport = FakeTransport(response(200, {"estimatedCost": {"credits": 60}}))

        with self.assertRaises(RunwaySubmitUnknown):
            self.client(transport).submit_text_to_video(request())

    def test_submit_rate_limit_is_the_only_retryable_submit_response(self) -> None:
        transport = FakeTransport(
            response(429, {"error": "rate limited"}, headers={"retry-after": "12"})
        )

        with self.assertRaises(RunwayRetryableError) as raised:
            self.client(transport).submit_text_to_video(request())

        self.assertEqual(12, raised.exception.retry_after_seconds)

    def test_submit_gateway_failures_are_unknown_not_retryable(self) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                transport = FakeTransport(response(status, {"error": "overloaded"}))
                with self.assertRaises(RunwaySubmitUnknown):
                    self.client(transport).submit_text_to_video(request())

    def test_submit_validation_and_auth_failures_are_permanent(self) -> None:
        for status in (400, 401, 404, 405):
            with self.subTest(status=status):
                transport = FakeTransport(response(status, {"error": "rejected"}))
                with self.assertRaises(RunwayPermanentError):
                    self.client(transport).submit_text_to_video(request())

    def test_get_task_parses_documented_running_and_succeeded_shapes(self) -> None:
        transport = FakeTransport(
            response(
                200,
                {
                    "id": TASK_ID,
                    "createdAt": "2026-08-23T10:00:00Z",
                    "status": "RUNNING",
                    "progress": 0.25,
                    "estimatedCost": {"credits": 60},
                },
            ),
            response(
                200,
                {
                    "id": TASK_ID,
                    "createdAt": "2026-08-23T10:00:00Z",
                    "status": "SUCCEEDED",
                    "output": ["https://cdn.example.test/generated.mp4?sig=x"],
                    "cost": {"credits": 60},
                },
            ),
        )
        client = self.client(transport)

        running = client.get_task(TASK_ID)
        succeeded = client.get_task(TASK_ID)

        self.assertEqual(RunwayTaskStatus.RUNNING, running.status)
        self.assertEqual(0.25, running.progress)
        self.assertEqual(RunwayTaskStatus.SUCCEEDED, succeeded.status)
        self.assertEqual(60, succeeded.final_cost_credits)
        self.assertEqual(
            ("https://cdn.example.test/generated.mp4?sig=x",), succeeded.output_urls
        )
        self.assertEqual("GET", transport.requests[0].method)

    def test_get_task_can_retry_gateway_failures_because_get_is_side_effect_free(self) -> None:
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                transport = FakeTransport(response(status, {"error": "temporary"}))
                with self.assertRaises(RunwayRetryableError):
                    self.client(transport).get_task(TASK_ID)

    def test_get_task_rejects_unknown_status_and_mismatched_identity(self) -> None:
        for value in (
            {
                "id": TASK_ID,
                "createdAt": "2026-08-23T10:00:00Z",
                "status": "QUEUED_BY_MAGIC",
            },
            {
                "id": "17f20503-6c24-4c16-946b-35dbbce2af2f",
                "createdAt": "2026-08-23T10:00:00Z",
                "status": "PENDING",
                "estimatedCost": {"credits": 60},
            },
        ):
            with self.subTest(value=value):
                transport = FakeTransport(response(200, value))
                with self.assertRaises(RunwayPermanentError):
                    self.client(transport).get_task(TASK_ID)

    def test_download_uses_signed_https_url_without_api_authorization(self) -> None:
        transport = FakeTransport(
            response(
                200,
                b"0000ftypisom-video-bytes",
                headers={"content-type": "video/mp4"},
                url="https://cdn.example.test/generated.mp4?sig=x",
            )
        )

        data, media_type = self.client(transport).download_output(
            "https://cdn.example.test/generated.mp4?sig=x", maximum_bytes=100
        )

        self.assertEqual("video/mp4", media_type)
        self.assertEqual(b"0000ftypisom-video-bytes", data)
        self.assertNotIn("Authorization", transport.requests[0].headers)
        self.assertEqual(100, transport.requests[0].maximum_response_bytes)

    def test_download_rejects_non_https_and_non_video_content_type(self) -> None:
        transport = FakeTransport(
            response(
                200,
                b"html",
                headers={"content-type": "text/html"},
                url="https://cdn.example.test/generated.mp4",
            )
        )
        client = self.client(transport)

        with self.assertRaises(RunwayPermanentError):
            client.download_output("http://cdn.example.test/generated.mp4", maximum_bytes=100)
        with self.assertRaises(RunwayPermanentError):
            client.download_output("https://cdn.example.test/generated.mp4", maximum_bytes=100)


class RunwayRequestTests(unittest.TestCase):
    def test_gen45_text_only_contract_is_strict(self) -> None:
        invalid = (
            {"prompt_text": "", "ratio": "1280:720", "duration": 5, "seed": 1},
            {"prompt_text": "ok", "ratio": "960:960", "duration": 5, "seed": 1},
            {"prompt_text": "ok", "ratio": "1280:720", "duration": 1, "seed": 1},
            {"prompt_text": "ok", "ratio": "1280:720", "duration": 5, "seed": -1},
            {
                "prompt_text": "ok",
                "ratio": "1280:720",
                "duration": 5,
                "seed": 1,
                "model": "gen4_turbo",
            },
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                RunwayTextVideoRequest(**kwargs)

    def test_prompt_limit_counts_utf16_code_units_like_official_contract(self) -> None:
        RunwayTextVideoRequest(
            prompt_text=chr(0x1F3AC) * 500,
            ratio="720:1280",
            duration=10,
            seed=4_294_967_295,
        )
        with self.assertRaises(ValueError):
            RunwayTextVideoRequest(
                prompt_text=chr(0x1F3AC) * 501,
                ratio="720:1280",
                duration=10,
                seed=4_294_967_295,
            )


if __name__ == "__main__":
    unittest.main()
