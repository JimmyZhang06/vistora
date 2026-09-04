from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from framefactory.worker.generation import (
    WAN_MODEL,
    WanClient,
    WanTextVideoRequest,
)
from framefactory.worker.generation.capability import _strip_audio
from framefactory.worker.generation.runway import (
    RunwayHttpResponse,
    RunwayTaskStatus,
    RunwayTransportFailure,
)
from framefactory.worker.generation.wan import WanPermanentError, WanSubmitUnknown


@dataclass
class _Request:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class _Transport:
    def __init__(self, *responses: RunwayHttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[_Request] = []

    def request(
        self,
        method: str,
        url: str | None = None,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> RunwayHttpResponse:
        del timeout_seconds, maximum_response_bytes
        target = url or method
        verb = method if url else "GET"
        self.requests.append(_Request(verb, target, dict(headers), body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(status: int, value: object, *, content_type: str = "application/json") -> RunwayHttpResponse:
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return RunwayHttpResponse(status, {"content-type": content_type}, body, "https://example.test")


def _request() -> WanTextVideoRequest:
    return WanTextVideoRequest(
        prompt_text="A restrained documentary establishing shot",
        ratio="1280:720",
        duration=5,
        seed=42,
        model=WAN_MODEL,
    )


def test_wan_submit_and_poll_use_documented_async_contract_and_cny_minor_cost() -> None:
    task_id = "0385dc79-5ff8-4d82-bcb6-abcdefabcdef"
    transport = _Transport(
        _response(200, {"output": {"task_id": task_id, "task_status": "PENDING"}}),
        _response(
            200,
            {
                "output": {
                    "task_id": task_id,
                    "task_status": "SUCCEEDED",
                    "video_url": "https://cdn.example.test/result.mp4?Expires=1",
                },
                "usage": {"output_video_duration": 5, "SR": 720},
            },
        ),
    )
    client = WanClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-secret",
        cost_per_second_minor=60,
        transport=transport,
    )

    receipt = client.submit_text_to_video(_request())
    task = client.get_task(receipt.task_id)

    assert receipt.estimated_cost_credits == 300
    assert task.status is RunwayTaskStatus.SUCCEEDED
    assert task.final_cost_credits == 300
    submitted = transport.requests[0]
    assert submitted.method == "POST"
    assert submitted.url.endswith("/api/v1/services/aigc/video-generation/video-synthesis")
    assert submitted.headers["X-DashScope-Async"] == "enable"
    payload = json.loads((submitted.body or b"").decode())
    assert payload == {
        "input": {"prompt": "A restrained documentary establishing shot"},
        "model": WAN_MODEL,
        "parameters": {
            "duration": 5,
            "prompt_extend": False,
            "ratio": "16:9",
            "resolution": "720P",
            "seed": 42,
            "watermark": False,
        },
    }
    assert transport.requests[1].url.endswith(f"/api/v1/tasks/{task_id}")


def test_wan_submit_transport_failure_and_missing_task_id_are_submit_unknown() -> None:
    client = WanClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-secret",
        cost_per_second_minor=60,
        transport=_Transport(RunwayTransportFailure("timeout")),
    )
    with pytest.raises(WanSubmitUnknown):
        client.submit_text_to_video(_request())

    client = WanClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-secret",
        cost_per_second_minor=60,
        transport=_Transport(_response(200, {"output": {"task_status": "PENDING"}})),
    )
    with pytest.raises(WanSubmitUnknown):
        client.submit_text_to_video(_request())


def test_wan_unknown_task_fails_closed_for_manual_reconciliation() -> None:
    task_id = "0385dc79-5ff8-4d82-bcb6-abcdefabcdef"
    client = WanClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="test-secret",
        cost_per_second_minor=60,
        transport=_Transport(
            _response(200, {"output": {"task_id": task_id, "task_status": "UNKNOWN"}})
        ),
    )
    with pytest.raises(WanPermanentError, match="24-hour"):
        client.get_task(task_id)


def test_wan_output_download_never_forwards_provider_bearer() -> None:
    output = _Transport(_response(200, b"mp4", content_type="video/mp4"))
    client = WanClient(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        api_key="provider-secret",
        cost_per_second_minor=60,
        output_transport=output,
    )

    body, media_type = client.download_output(
        "https://cdn.example.test/result.mp4?Expires=1", maximum_bytes=100
    )

    assert body == b"mp4"
    assert media_type == "video/mp4"
    assert "Authorization" not in output.requests[0].headers


def test_wan_audio_is_removed_with_stream_copy() -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        assert "-an" in command
        assert command[command.index("-c:v") + 1] == "copy"
        Path(command[-1]).write_bytes(b"silent-mp4")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with patch("framefactory.worker.generation.capability.subprocess.run", fake_run):
        assert _strip_audio(b"provider-mp4", "ffmpeg") == b"silent-mp4"
