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
from framefactory.worker.generation.runway import (
    PinnedRunwayOutputTransport,
    RunwayOutputPolicyFailure,
    RunwayTransportFailure,
)
from framefactory.worker.web_capture.security import (
    PublicHttpsPolicy,
    PublicHttpsUrlValidator,
)

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
        method_or_url: str,
        url: str | None = None,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> RunwayHttpResponse:
        del timeout_seconds
        method = method_or_url if url is not None else "GET"
        requested_url = url or method_or_url
        self.requests.append(
            RequestRecord(method, requested_url, dict(headers), body, maximum_response_bytes)
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


class FakeHttpResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        headers: Mapping[str, str],
    ) -> None:
        self.status = status
        self._body = body
        self._headers = dict(headers)

    def read(self, maximum_bytes: int) -> bytes:
        return self._body[:maximum_bytes]

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers.items())


class FakePinnedConnection:
    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, Mapping[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str],
    ) -> None:
        self.requests.append((method, target, dict(headers)))

    def getresponse(self) -> FakeHttpResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FakePinnedConnectionFactory:
    def __init__(self, *responses: FakeHttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, int, str, float]] = []
        self.connections: list[FakePinnedConnection] = []

    def __call__(
        self,
        hostname: str,
        port: int,
        resolved_ip: str,
        *,
        timeout_seconds: float,
    ) -> FakePinnedConnection:
        self.calls.append((hostname, port, resolved_ip, timeout_seconds))
        connection = FakePinnedConnection(self.responses.pop(0))
        self.connections.append(connection)
        return connection


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
            output_transport=transport,
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

    def test_download_validates_and_pins_every_https_redirect_hop(self) -> None:
        resolved_hosts: list[tuple[str, int]] = []

        def resolve(hostname: str, port: int) -> tuple[str, ...]:
            resolved_hosts.append((hostname, port))
            return ("93.184.216.34",)

        factory = FakePinnedConnectionFactory(
            FakeHttpResponse(
                302,
                b"",
                {"Location": "https://media.example.test/final.mp4?signature=redacted"},
            ),
            FakeHttpResponse(
                200,
                b"0000ftypisom-video-bytes",
                {"Content-Type": "video/mp4"},
            ),
        )
        output_transport = PinnedRunwayOutputTransport(
            validator=PublicHttpsUrlValidator(
                PublicHttpsPolicy(allow_non_standard_ports=True),
                resolver=resolve,
            ),
            connection_factory=factory,
        )
        client = RunwayClient(
            base_url="https://api.dev.runwayml.com",
            api_key="-".join(("bearer", "secret", "for", "test")),  # noqa: FLY002
            timeout_seconds=10,
            transport=FakeTransport(),
            output_transport=output_transport,
        )

        data, media_type = client.download_output(
            "https://cdn.example.test/start.mp4", maximum_bytes=100
        )

        self.assertEqual("video/mp4", media_type)
        self.assertEqual(b"0000ftypisom-video-bytes", data)
        self.assertEqual(
            [("cdn.example.test", 443), ("media.example.test", 443)], resolved_hosts
        )
        self.assertEqual(
            [
                ("cdn.example.test", 443, "93.184.216.34", 10),
                ("media.example.test", 443, "93.184.216.34", 10),
            ],
            factory.calls,
        )
        self.assertEqual("/start.mp4", factory.connections[0].requests[0][1])
        self.assertEqual(
            "/final.mp4?signature=redacted", factory.connections[1].requests[0][1]
        )
        for connection in factory.connections:
            self.assertTrue(connection.closed)
            self.assertNotIn("Authorization", connection.requests[0][2])
            self.assertNotIn("bearer-secret-for-test", repr(connection.requests))

    def test_download_rejects_any_private_dns_answer_before_connecting(self) -> None:
        factory = FakePinnedConnectionFactory()
        output_transport = PinnedRunwayOutputTransport(
            validator=PublicHttpsUrlValidator(
                PublicHttpsPolicy(allow_non_standard_ports=True),
                resolver=lambda _hostname, _port: ("93.184.216.34", "127.0.0.1"),
            ),
            connection_factory=factory,
        )
        client = RunwayClient(
            base_url="https://api.dev.runwayml.com",
            api_key="-".join(("bearer", "secret", "for", "test")),  # noqa: FLY002
            timeout_seconds=10,
            transport=FakeTransport(),
            output_transport=output_transport,
        )

        with self.assertRaises(RunwayOutputPolicyFailure) as captured:
            client.download_output(
                "https://cdn.example.test/generated.mp4?token=must-not-leak",
                maximum_bytes=100,
            )

        self.assertEqual([], factory.calls)
        self.assertNotIn("must-not-leak", str(captured.exception))
        self.assertNotIn("bearer-secret-for-test", str(captured.exception))

    def test_download_rejects_private_redirect_before_second_connection(self) -> None:
        def resolve(hostname: str, _port: int) -> tuple[str, ...]:
            if hostname == "cdn.example.test":
                return ("93.184.216.34",)
            return ("10.0.0.8",)

        factory = FakePinnedConnectionFactory(
            FakeHttpResponse(
                302,
                b"",
                {"Location": "https://private.example.test/video.mp4?token=hidden"},
            )
        )
        output_transport = PinnedRunwayOutputTransport(
            validator=PublicHttpsUrlValidator(
                PublicHttpsPolicy(allow_non_standard_ports=True),
                resolver=resolve,
            ),
            connection_factory=factory,
        )
        client = RunwayClient(
            base_url="https://api.dev.runwayml.com",
            api_key="secret-for-test",
            timeout_seconds=10,
            transport=FakeTransport(),
            output_transport=output_transport,
        )

        with self.assertRaises(RunwayOutputPolicyFailure) as captured:
            client.download_output(
                "https://cdn.example.test/start.mp4", maximum_bytes=100
            )

        self.assertEqual(1, len(factory.calls))
        self.assertNotIn("hidden", str(captured.exception))

    def test_download_redirect_limit_is_fail_closed(self) -> None:
        redirects = [
            response(
                302,
                b"",
                headers={"location": f"https://cdn.example.test/{index}.mp4"},
            )
            for index in range(6)
        ]
        transport = FakeTransport(*redirects)

        with self.assertRaisesRegex(RunwayOutputPolicyFailure, "redirect limit"):
            self.client(transport).download_output(
                "https://cdn.example.test/start.mp4", maximum_bytes=100
            )

        self.assertEqual(6, len(transport.requests))


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
