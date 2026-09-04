"""Strict DashScope Wan 2.7 text-to-video provider adapter.

Wan does not document a client idempotency key.  A submit transport failure or
an accepted response without a durable task id is therefore submit-unknown and
must never be retried automatically.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from .runway import (
    PinnedRunwayOutputTransport,
    RunwayHttpResponse,
    RunwayOutputTransport,
    RunwayPermanentError,
    RunwayRetryableError,
    RunwaySubmitReceipt,
    RunwaySubmitUnknown,
    RunwayTask,
    RunwayTaskStatus,
    RunwayTransport,
    RunwayTransportFailure,
    UrllibRunwayTransport,
    download_public_mp4,
)

WAN_PROVIDER_NAME = "dashscope-wan"
WAN_MODEL = "wan2.7-t2v-2026-06-12"
WAN_API_VERSION = "dashscope-video-synthesis-v1"
_SUBMIT_PATH = "services/aigc/video-generation/video-synthesis"
_TASK_PATH = "tasks/"
_MAX_JSON_BYTES = 1_048_576
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_RETRYABLE = frozenset({429, 502, 503, 504})
_DEFINITE_REJECTION = frozenset({400, 401, 403, 404, 405, 422})


class WanPermanentError(RunwayPermanentError):
    """Definite Wan request or response contract failure."""


class WanRetryableError(RunwayRetryableError):
    """Wan operation known not to have been accepted."""


class WanSubmitUnknown(RunwaySubmitUnknown):
    """Wan paid submission may have been accepted without a task identity."""


@dataclass(frozen=True, slots=True)
class WanTextVideoRequest:
    prompt_text: str
    ratio: str
    duration: int
    seed: int
    model: str = WAN_MODEL
    resolution: str = "720P"

    def __post_init__(self) -> None:
        if not 1 <= len(self.prompt_text) <= 5_000:
            raise ValueError("Wan 2.7 prompt must contain 1 to 5000 characters")
        if self.model != WAN_MODEL:
            raise ValueError(f"Wan adapter requires pinned model {WAN_MODEL}")
        if self.ratio not in {"1280:720", "720:1280"}:
            raise ValueError("Wan 2.7 ratio is unsupported")
        if not 2 <= self.duration <= 15:
            raise ValueError("Wan 2.7 duration must be an integer from 2 to 15")
        if not 0 <= self.seed <= 2_147_483_647:
            raise ValueError("Wan seed must be between 0 and 2147483647")
        if self.resolution != "720P":
            raise ValueError("the Vistora Wan profile is frozen to 720P")

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": {"prompt": self.prompt_text},
            "parameters": {
                "resolution": self.resolution,
                "ratio": "16:9" if self.ratio == "1280:720" else "9:16",
                "duration": self.duration,
                "prompt_extend": False,
                "watermark": False,
                "seed": self.seed,
            },
        }


class WanClient:
    """DashScope async submit/poll client with fail-closed output download."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        cost_per_second_minor: int,
        timeout_seconds: float = 60.0,
        transport: RunwayTransport | None = None,
        output_transport: RunwayOutputTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Wan base URL must be a credential-free HTTPS URL")
        if not api_key.strip():
            raise ValueError("Wan API key must not be empty")
        if cost_per_second_minor < 1:
            raise ValueError("Wan CNY minor-unit rate must be positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Wan timeout must be positive and finite")
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key.strip()
        self._rate = cost_per_second_minor
        self._timeout = timeout_seconds
        self._transport = transport or UrllibRunwayTransport()
        self._output_transport = output_transport or PinnedRunwayOutputTransport()

    def submit_text_to_video(self, request: WanTextVideoRequest) -> RunwaySubmitReceipt:
        try:
            response = self._transport.request(
                "POST",
                urljoin(self._base_url, _SUBMIT_PATH),
                headers=self._headers(content_type=True, async_submit=True),
                body=_json_bytes(request.to_payload()),
                timeout_seconds=self._timeout,
                maximum_response_bytes=_MAX_JSON_BYTES,
            )
        except RunwayTransportFailure as exc:
            raise WanSubmitUnknown(
                "Wan submission outcome is unknown; automatic resubmission is forbidden"
            ) from exc
        if response.status == 200:
            try:
                output = _mapping(_json(response.body).get("output"))
                task_id = _task_id(output.get("task_id"))
            except (WanPermanentError, ValueError, TypeError) as exc:
                raise WanSubmitUnknown(
                    "Wan accepted a submission but returned no durable task identity"
                ) from exc
            return RunwaySubmitReceipt(
                task_id=task_id,
                estimated_cost_credits=float(request.duration * self._rate),
            )
        self._raise_submit(response)
        raise AssertionError("unreachable")

    def get_task(self, task_id: str) -> RunwayTask:
        identity = _task_id(task_id)
        try:
            response = self._transport.request(
                "GET",
                urljoin(self._base_url, _TASK_PATH + identity),
                headers=self._headers(content_type=False, async_submit=False),
                body=None,
                timeout_seconds=self._timeout,
                maximum_response_bytes=_MAX_JSON_BYTES,
            )
        except RunwayTransportFailure as exc:
            raise WanRetryableError("Wan task query did not return an HTTP response") from exc
        if response.status != 200:
            if response.status in _RETRYABLE:
                raise WanRetryableError(_error(response))
            raise WanPermanentError(_error(response))
        return self._parse_task(response.body, identity)

    def download_output(self, url: str, *, maximum_bytes: int) -> tuple[bytes, str]:
        # Delegate to the DNS-pinned, redirect-by-redirect public HTTPS transport.
        # Its download request contains only User-Agent and never this client's bearer.
        return download_public_mp4(
            url,
            maximum_bytes=maximum_bytes,
            timeout_seconds=self._timeout,
            output_transport=self._output_transport,
        )

    def _parse_task(self, payload: bytes, task_id: str) -> RunwayTask:
        value = _json(payload)
        output = _mapping(value.get("output"))
        if _task_id(output.get("task_id")) != task_id:
            raise WanPermanentError("Wan task response identity does not match the query")
        raw_status = str(output.get("task_status") or "").upper()
        if raw_status == "UNKNOWN":
            raise WanPermanentError(
                "Wan task is unknown or its 24-hour reconciliation window expired"
            )
        status_map = {
            "PENDING": RunwayTaskStatus.PENDING,
            "RUNNING": RunwayTaskStatus.RUNNING,
            "SUCCEEDED": RunwayTaskStatus.SUCCEEDED,
            "FAILED": RunwayTaskStatus.FAILED,
            "CANCELED": RunwayTaskStatus.CANCELLED,
            "CANCELLED": RunwayTaskStatus.CANCELLED,
        }
        try:
            status = status_map[raw_status]
        except KeyError as exc:
            raise WanPermanentError("Wan task response has an unknown status") from exc
        usage = value.get("usage") if isinstance(value.get("usage"), Mapping) else {}
        duration = usage.get("output_video_duration") or usage.get("duration")
        final_cost: int | None = None
        outputs: tuple[str, ...] = ()
        failure = None
        failure_code = None
        if status.terminal:
            if status is RunwayTaskStatus.SUCCEEDED:
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    raise WanPermanentError("Wan succeeded without billable duration")
                billed_seconds = float(duration)
                if not math.isfinite(billed_seconds) or billed_seconds <= 0:
                    raise WanPermanentError("Wan returned invalid billable duration")
                final_cost = math.ceil(billed_seconds * self._rate)
                url = str(output.get("video_url") or "").strip()
                if not url:
                    raise WanPermanentError("Wan succeeded without a video URL")
                outputs = (url,)
            else:
                final_cost = 0
                failure_code = str(output.get("code") or raw_status)
                failure = str(output.get("message") or raw_status)
        return RunwayTask(
            task_id=task_id,
            status=status,
            created_at=str(output.get("submit_time") or "provider-time-unavailable"),
            output_urls=outputs,
            estimated_cost_credits=None,
            final_cost_credits=final_cost,
            failure=failure,
            failure_code=failure_code,
        )

    def _headers(self, *, content_type: bool, async_submit: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "Vistora-FrameFactory/1.0",
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        if async_submit:
            headers["X-DashScope-Async"] = "enable"
        return headers

    @staticmethod
    def _raise_submit(response: RunwayHttpResponse) -> None:
        if response.status in _RETRYABLE:
            raise WanRetryableError(_error(response))
        if response.status in _DEFINITE_REJECTION:
            raise WanPermanentError(_error(response))
        raise WanSubmitUnknown(
            f"Wan submission returned ambiguous HTTP {response.status}; do not resubmit"
        )


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json(payload: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WanPermanentError("Wan returned invalid JSON") from exc
    return _mapping(value)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WanPermanentError("Wan response lacks a required object")
    return value


def _task_id(value: object) -> str:
    text = str(value or "").strip()
    if not _TASK_ID.fullmatch(text):
        raise WanPermanentError("Wan task id has an invalid format")
    return text


def _error(response: RunwayHttpResponse) -> str:
    try:
        value = _json(response.body)
        code = str(value.get("code") or "").strip()[:100]
        message = str(value.get("message") or "").strip()[:400]
    except WanPermanentError:
        code = message = ""
    detail = ": ".join(item for item in (code, message) if item)
    return f"Wan returned HTTP {response.status}" + (f": {detail}" if detail else "")
