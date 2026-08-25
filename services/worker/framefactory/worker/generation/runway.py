"""Strict Runway text-to-video API port.

The adapter intentionally implements only the documented, text-only Gen-4.5
shape.  It does not send an undocumented idempotency header: the current Runway
OpenAPI has no provider idempotency or client-request lookup contract.  A
transport failure while submitting is therefore an ambiguous paid side effect
and is surfaced as :class:`RunwaySubmitUnknown`, never as an ordinary retry.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

RUNWAY_API_VERSION = "2024-11-06"
_TEXT_TO_VIDEO_PATH = "v1/text_to_video"
_TASK_PATH = "v1/tasks/"
_MAX_JSON_RESPONSE_BYTES = 1_048_576
_SUBMIT_SAFE_RETRY_HTTP_STATUSES = frozenset({429})
_QUERY_SAFE_RETRY_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_PERMANENT_HTTP_STATUSES = frozenset({400, 401, 404, 405})
_GEN45_RATIOS = frozenset({"1280:720", "720:1280"})
_OUTPUT_MEDIA_TYPES = frozenset({"video/mp4", "application/octet-stream"})


class RunwayError(Exception):
    """Base class for provider-contract failures."""


class RunwayPermanentError(RunwayError):
    """The request or provider response cannot become valid by retrying."""


class RunwayRetryableError(RunwayError):
    """A side-effect-free request, or a documented safe response, may be retried."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RunwaySubmitUnknown(RunwayError):
    """A POST may have created a paid task, but no durable task ID was obtained."""


class RunwayTransportFailure(Exception):
    """Low-level network failure with no trustworthy HTTP response."""


@dataclass(frozen=True, slots=True)
class RunwayHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str


class RunwayTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> RunwayHttpResponse: ...


class UrllibRunwayTransport:
    """Dependency-free HTTPS transport with bounded response bodies."""

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
        if maximum_response_bytes < 1:
            raise ValueError("maximum_response_bytes must be positive")
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = _bounded_read(response, maximum_response_bytes)
                return RunwayHttpResponse(
                    status=int(response.status),
                    headers={key.casefold(): value for key, value in response.headers.items()},
                    body=payload,
                    final_url=str(response.geturl()),
                )
        except HTTPError as exc:
            try:
                payload = _bounded_read(exc, maximum_response_bytes)
            except RunwayPermanentError:
                payload = b""
            return RunwayHttpResponse(
                status=int(exc.code),
                headers={key.casefold(): value for key, value in exc.headers.items()},
                body=payload,
                final_url=str(exc.geturl()),
            )
        except (TimeoutError, URLError, OSError) as exc:
            raise RunwayTransportFailure("Runway transport did not return an HTTP response") from exc


@dataclass(frozen=True, slots=True)
class RunwayTextVideoRequest:
    prompt_text: str
    ratio: str
    duration: int
    seed: int
    model: str = "gen4.5"

    def __post_init__(self) -> None:
        prompt = self.prompt_text.strip()
        if not prompt or _utf16_code_units(prompt) > 1_000:
            raise ValueError("Runway Gen-4.5 prompt must contain 1 to 1000 UTF-16 code units")
        if self.model != "gen4.5":
            raise ValueError("the generated-only adapter supports only Runway model gen4.5")
        if self.ratio not in _GEN45_RATIOS:
            raise ValueError("Runway Gen-4.5 text-to-video ratio is unsupported")
        if isinstance(self.duration, bool) or not 2 <= self.duration <= 10:
            raise ValueError("Runway Gen-4.5 duration must be an integer from 2 to 10")
        if isinstance(self.seed, bool) or not 0 <= self.seed <= 4_294_967_295:
            raise ValueError("Runway seed must be an integer from 0 to 4294967295")
        object.__setattr__(self, "prompt_text", prompt)

    def to_payload(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "model": self.model,
            "outputFormat": "mp4",
            "promptText": self.prompt_text,
            "ratio": self.ratio,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class RunwaySubmitReceipt:
    task_id: str
    estimated_cost_credits: float


class RunwayTaskStatus(StrEnum):
    PENDING = "PENDING"
    THROTTLED = "THROTTLED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


@dataclass(frozen=True, slots=True)
class RunwayTask:
    task_id: str
    status: RunwayTaskStatus
    created_at: str
    output_urls: tuple[str, ...] = ()
    progress: float | None = None
    estimated_cost_credits: float | None = None
    final_cost_credits: int | None = None
    failure: str | None = None
    failure_code: str | None = None


class RunwayClient:
    """Small strict client for documented Runway async task semantics."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        transport: RunwayTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Runway base URL must be an HTTPS URL with a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Runway base URL must not contain credentials, query, or fragment")
        if not api_key.strip():
            raise ValueError("Runway API key must not be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Runway timeout must be positive and finite")
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibRunwayTransport()

    def submit_text_to_video(self, request: RunwayTextVideoRequest) -> RunwaySubmitReceipt:
        payload = _canonical_json_bytes(request.to_payload())
        try:
            response = self._transport.request(
                "POST",
                urljoin(self._base_url, _TEXT_TO_VIDEO_PATH),
                headers=self._api_headers(content_type=True),
                body=payload,
                timeout_seconds=self._timeout_seconds,
                maximum_response_bytes=_MAX_JSON_RESPONSE_BYTES,
            )
        except RunwayTransportFailure as exc:
            raise RunwaySubmitUnknown(
                "Runway submission outcome is unknown; do not submit this operation again"
            ) from exc
        if response.status == 200:
            try:
                value = _json_object(response.body)
                return RunwaySubmitReceipt(
                    task_id=_uuid4(value.get("id"), field="Runway task id"),
                    estimated_cost_credits=_non_negative_number(
                        _mapping(value.get("estimatedCost")).get("credits"),
                        field="Runway estimated cost",
                    ),
                )
            except (RunwayPermanentError, TypeError, ValueError) as exc:
                # A 200 means the paid side effect may already exist.  An invalid
                # body cannot be retried without risking a duplicate charge.
                raise RunwaySubmitUnknown(
                    "Runway accepted a submission but returned no durable task identity"
                ) from exc
        self._raise_submit_http(response)
        raise AssertionError("unreachable")

    def get_task(self, task_id: str) -> RunwayTask:
        identity = _uuid4(task_id, field="Runway task id")
        try:
            response = self._transport.request(
                "GET",
                urljoin(self._base_url, _TASK_PATH + identity),
                headers=self._api_headers(content_type=False),
                body=None,
                timeout_seconds=self._timeout_seconds,
                maximum_response_bytes=_MAX_JSON_RESPONSE_BYTES,
            )
        except RunwayTransportFailure as exc:
            raise RunwayRetryableError("Runway task query did not return an HTTP response") from exc
        if response.status == 200:
            return _parse_task(response.body, expected_task_id=identity)
        self._raise_query_http(response)
        raise AssertionError("unreachable")

    def download_output(self, url: str, *, maximum_bytes: int) -> tuple[bytes, str]:
        _https_url(url, field="Runway output URL")
        if maximum_bytes < 1:
            raise ValueError("maximum output bytes must be positive")
        try:
            response = self._transport.request(
                "GET",
                url,
                headers={"User-Agent": "Vistora-FrameFactory/1.0"},
                body=None,
                timeout_seconds=self._timeout_seconds,
                maximum_response_bytes=maximum_bytes,
            )
        except RunwayTransportFailure as exc:
            raise RunwayRetryableError("Runway output download failed before an HTTP response") from exc
        if response.status in _QUERY_SAFE_RETRY_HTTP_STATUSES:
            raise RunwayRetryableError(
                "Runway output download is temporarily unavailable",
                retry_after_seconds=_retry_after(response.headers),
            )
        if response.status != 200:
            raise RunwayPermanentError(
                f"Runway output download returned HTTP {response.status}"
            )
        _https_url(response.final_url, field="Runway output redirect")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in _OUTPUT_MEDIA_TYPES:
            raise RunwayPermanentError("Runway output is not a supported MP4 response")
        if not response.body:
            raise RunwayPermanentError("Runway output is empty")
        return response.body, "video/mp4"

    def _api_headers(self, *, content_type: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "Vistora-FrameFactory/1.0",
            "X-Runway-Version": RUNWAY_API_VERSION,
        }
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _raise_submit_http(response: RunwayHttpResponse) -> None:
        message = _provider_error_message(response)
        if response.status in _SUBMIT_SAFE_RETRY_HTTP_STATUSES:
            raise RunwayRetryableError(
                message,
                retry_after_seconds=_retry_after(response.headers),
            )
        if response.status in _PERMANENT_HTTP_STATUSES:
            raise RunwayPermanentError(message)
        # An undocumented response cannot prove whether a task was created.
        raise RunwaySubmitUnknown(
            f"Runway submission returned ambiguous HTTP {response.status}; do not resubmit"
        )

    @staticmethod
    def _raise_query_http(response: RunwayHttpResponse) -> None:
        message = _provider_error_message(response)
        if response.status in _QUERY_SAFE_RETRY_HTTP_STATUSES:
            raise RunwayRetryableError(
                message,
                retry_after_seconds=_retry_after(response.headers),
            )
        raise RunwayPermanentError(message)


def _parse_task(payload: bytes, *, expected_task_id: str) -> RunwayTask:
    value = _json_object(payload)
    task_id = _uuid4(value.get("id"), field="Runway task id")
    if task_id != expected_task_id:
        raise RunwayPermanentError("Runway task response identity does not match the query")
    try:
        status = RunwayTaskStatus(str(value.get("status")))
    except ValueError as exc:
        raise RunwayPermanentError("Runway task response has an unknown status") from exc
    created_at = str(value.get("createdAt") or "").strip()
    if not created_at:
        raise RunwayPermanentError("Runway task response lacks createdAt")

    progress: float | None = None
    estimated: float | None = None
    final: int | None = None
    output_urls: tuple[str, ...] = ()
    failure: str | None = None
    failure_code: str | None = None
    if status in {RunwayTaskStatus.PENDING, RunwayTaskStatus.THROTTLED}:
        estimated = _non_negative_number(
            _mapping(value.get("estimatedCost")).get("credits"),
            field="Runway estimated cost",
        )
    elif status is RunwayTaskStatus.RUNNING:
        estimated = _non_negative_number(
            _mapping(value.get("estimatedCost")).get("credits"),
            field="Runway estimated cost",
        )
        progress = _bounded_number(value.get("progress"), field="Runway task progress")
    else:
        final = _non_negative_integer(
            _mapping(value.get("cost")).get("credits"),
            field="Runway final cost",
        )
        if status is RunwayTaskStatus.SUCCEEDED:
            raw_outputs = value.get("output")
            if not isinstance(raw_outputs, list) or len(raw_outputs) != 1:
                raise RunwayPermanentError(
                    "Runway MP4 generation must return exactly one output URL"
                )
            output_urls = tuple(
                _https_url(item, field="Runway output URL") for item in raw_outputs
            )
        elif status is RunwayTaskStatus.FAILED:
            failure = str(value.get("failure") or "").strip()
            if not failure:
                raise RunwayPermanentError("Runway failed task lacks failure detail")
            raw_code = value.get("failureCode")
            failure_code = str(raw_code).strip() if raw_code is not None else None
    return RunwayTask(
        task_id=task_id,
        status=status,
        created_at=created_at,
        output_urls=output_urls,
        progress=progress,
        estimated_cost_credits=estimated,
        final_cost_credits=final,
        failure=failure,
        failure_code=failure_code,
    )


def _bounded_read(response: Any, maximum_bytes: int) -> bytes:
    payload = response.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise RunwayPermanentError("Runway response exceeds the configured byte limit")
    return payload


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_object(payload: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunwayPermanentError("Runway returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise RunwayPermanentError("Runway JSON response must be an object")
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunwayPermanentError("Runway response lacks a required object")
    return value


def _uuid4(value: object, *, field: str) -> str:
    try:
        identity = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RunwayPermanentError(f"{field} must be a UUID") from exc
    if identity.version != 4 or identity.variant != "specified in RFC 4122":
        raise RunwayPermanentError(f"{field} must be an RFC 4122 UUIDv4")
    return str(identity)


def _utf16_code_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _non_negative_number(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise RunwayPermanentError(f"{field} must be a non-negative number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RunwayPermanentError(f"{field} must be a non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise RunwayPermanentError(f"{field} must be a non-negative number")
    return number


def _non_negative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunwayPermanentError(f"{field} must be a non-negative integer")
    return value


def _bounded_number(value: object, *, field: str) -> float:
    number = _non_negative_number(value, field=field)
    if number > 1:
        raise RunwayPermanentError(f"{field} must be between zero and one")
    return number


def _https_url(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RunwayPermanentError(f"{field} must be a credential-free HTTPS URL")
    return text


def _provider_error_message(response: RunwayHttpResponse) -> str:
    try:
        value = _json_object(response.body)
        detail = str(value.get("error") or "").strip()
    except RunwayPermanentError:
        detail = ""
    suffix = f": {detail[:500]}" if detail else ""
    return f"Runway returned HTTP {response.status}{suffix}"


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None
