"""Local browser login bridge; credentials never cross this API boundary."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import json
import re
import struct
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from fastapi import Request as ApiRequest
from pydantic import BaseModel, ConfigDict, Field

from .benchmark_accounts import _managed_browser_base_url, _RejectRedirects
from .context import WorkspaceContext
from .errors import ApiError
from .settings import Settings

AuthState = Literal[
    "not_configured",
    "provider_unavailable",
    "checking",
    "login_required",
    "awaiting_scan",
    "authorized",
    "expired",
    "error",
]


class BenchmarkAuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkAuthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0.0"] = "1.0.0"
    platform: Literal["xiaohongshu"] = "xiaohongshu"
    mode: Literal["local_browser"] = "local_browser"
    state: AuthState
    message: str
    qr_image_data_url: str | None = Field(default=None, repr=False)
    expires_at: datetime | None = None
    checked_at: datetime | None = None
    retry_after_seconds: Literal[3] = 3
    error_code: str | None = None


_MESSAGES: dict[AuthState, str] = {
    "not_configured": "尚未配置本机小红书采集服务。请按启动说明配置后重试。",
    "provider_unavailable": "本机小红书采集服务暂不可用。请确认服务和受管浏览器已启动。",
    "checking": "正在核验小红书登录状态。请稍候。",
    "login_required": "需要登录小红书后继续采集。点击授权。在小红书 App 中扫码确认。",
    "awaiting_scan": "请使用小红书 App 扫码并确认登录。登录凭据仅保存在本机浏览器。",
    "authorized": "已通过小红书页面核验登录。可以继续对标采集。",
    "expired": "本次登录二维码已过期。请重新获取。",
    "error": "暂时无法确认小红书登录状态。请重试。",
}


def require_local_auth_access(
    request: ApiRequest,
    context: WorkspaceContext,
    settings: Settings,
) -> None:
    """This single-installation browser session is never a remote/tenant API."""
    if (
        "assets:write" not in context.permissions
        or context.workspace_id != settings.default_workspace_id
        or context.user_id != settings.default_user_id
    ):
        raise ApiError(403, "BENCHMARK_AUTH_FORBIDDEN", "Local owner permission is required")
    peer = request.client.host if request.client else ""
    try:
        local_peer = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local_peer = False
    if (
        not local_peer
        or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or any(
            request.headers.get(name)
            for name in (
                "forwarded",
                "x-forwarded-for",
                "x-forwarded-host",
            )
        )
    ):
        raise ApiError(403, "BENCHMARK_AUTH_LOCAL_ONLY", "Login management requires local access")
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise ApiError(
                403,
                "BENCHMARK_ORIGIN_REJECTED",
                "This origin cannot manage login",
            ) from None
        allowed = origin in settings.cors_allow_origins or bool(
            settings.cors_allow_origin_regex
            and re.fullmatch(settings.cors_allow_origin_regex, origin)
        )
        if (
            not allowed
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ApiError(403, "BENCHMARK_ORIGIN_REJECTED", "This origin cannot manage login")
    elif request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "BENCHMARK_ORIGIN_REJECTED", "This origin cannot manage login")
    if (
        request.method == "POST"
        and request.headers.get("content-type", "").split(";")[0] != "application/json"
    ):
        raise ApiError(415, "BENCHMARK_AUTH_JSON_REQUIRED", "Login requests require JSON")


def _provider_request(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    request = Request(
        base_url + path,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    # Ignore machine proxy configuration: requests must stay on the configured loopback origin.
    with build_opener(ProxyHandler({}), _RejectRedirects()).open(request, timeout=8) as response:
        payload = response.read(600_001)
    if len(payload) > 600_000:
        raise ValueError("oversized provider response")
    result = json.loads(payload)
    if not isinstance(result, dict):
        raise ValueError("invalid provider response")
    return result


def _fresh_platform_login(base_url: str) -> bool:
    """Check a fresh platform response, never cookie presence or a stale provider tab."""
    from playwright.sync_api import sync_playwright

    provider = _provider_request(base_url, "GET", "/browser/managed/status", None)
    port = provider.get("cdp_port")
    if provider.get("state") != "running" or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("managed browser unavailable")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5_000)
        page = None
        try:
            if not browser.contexts:
                raise ValueError("managed browser context unavailable")
            page = browser.contexts[0].new_page()
            page.set_default_timeout(5_000)
            page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in {"image", "media", "font"}
                    else route.continue_()
                ),
            )
            response = page.goto(
                "https://www.xiaohongshu.com/explore", timeout=10_000, wait_until="domcontentloaded"
            )
            current = urlsplit(page.url)
            if current.scheme != "https" or current.hostname != "www.xiaohongshu.com":
                raise ValueError("unexpected platform navigation")
            if current.path.startswith("/login"):
                return False
            if response is None or response.status >= 400:
                raise ValueError("platform unavailable")
            # Wait for actual account state from this fresh page. Missing/ambiguous state
            # is an error, not proof that authentication succeeded or failed.
            page.wait_for_function("""() => {
                const state = window.__INITIAL_STATE__?.user?.userInfo;
                const user = state?.value ?? state;
                return typeof user?.guest === 'boolean'
                    || Boolean(document.querySelector('.login-container'));
            }""")
            result = page.evaluate("""() => {
                if (document.querySelector('.login-container')) return false;
                const state = window.__INITIAL_STATE__?.user?.userInfo;
                const user = state?.value ?? state;
                if (user?.guest === true) return false;
                const id = user?.userId ?? user?.user_id ?? '';
                if (user?.guest === false && /^[0-9a-f]{24}$/.test(id)) return true;
                return null;
            }""")
            if type(result) is not bool:
                raise ValueError("ambiguous platform login state")
            return result
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    page.close()
            # Disconnect the CDP client; the existing user's browser remains running.
            with contextlib.suppress(Exception):
                browser.close()


Transport = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
PlatformCheck = Callable[[], Awaitable[bool]]


class BenchmarkAuthService:
    """One ephemeral QR operation per local installation, with bounded provider I/O."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        platform_check: PlatformCheck | None = None,
        response_wait_seconds: float = 12.0,
        qr_operation_seconds: float = 45.0,
    ) -> None:
        self.base_url = _managed_browser_base_url(base_url)
        self._transport = transport or self._request
        self._platform_check = platform_check or self._check
        self._response_wait_seconds = response_wait_seconds
        self._qr_operation_seconds = qr_operation_seconds
        self._lock = asyncio.Lock()
        self._checked_at: datetime | None = None
        self._last_check = 0.0
        self._logged_in: bool | None = None
        self._qr: str | None = None
        self._expires_at: datetime | None = None
        self._task_id: str | None = None
        self._request_id: str | None = None
        self._pending_since = 0.0
        self._last_qr_poll = 0.0
        self._last_qr_start = 0.0
        self._last_auth_start = 0.0
        self._operations: set[asyncio.Task[BenchmarkAuthStatus]] = set()
        self._closed = False

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return await asyncio.to_thread(_provider_request, self.base_url, method, path, body)

    async def _check(self) -> bool:
        return await asyncio.to_thread(_fresh_platform_login, self.base_url)

    async def close(self) -> None:
        self._closed = True
        if self._operations:
            await asyncio.gather(
                *(asyncio.shield(task) for task in self._operations), return_exceptions=True
            )
        self._clear_qr()

    def _view(self, state: AuthState, *, error_code: str | None = None) -> BenchmarkAuthStatus:
        if state == "awaiting_scan" and self._expires_at and self._expires_at <= datetime.now(UTC):
            self._clear_qr()
            state = "expired"
        return BenchmarkAuthStatus(
            state=state,
            message=_MESSAGES[state],
            checked_at=self._checked_at,
            qr_image_data_url=self._qr if state == "awaiting_scan" else None,
            expires_at=self._expires_at if state == "awaiting_scan" else None,
            error_code=error_code,
        )

    def _clear_qr(self) -> None:
        self._qr = None
        self._expires_at = None
        self._task_id = None
        self._request_id = None
        self._pending_since = 0.0

    async def status(self, *, start: bool = False) -> BenchmarkAuthStatus:
        if self._closed:
            return self._view("provider_unavailable", error_code="BENCHMARK_AUTH_STOPPED")
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.1)
        except TimeoutError:
            return self._view("checking")
        operation = asyncio.create_task(self._safe_status(start=start))
        self._operations.add(operation)
        operation.add_done_callback(self._operations.discard)
        try:
            # The browser operation survives HTTP timeout/cancellation. Polls receive
            # checking until the explicit POST's own QR operation has completed.
            done, _ = await asyncio.wait({operation}, timeout=self._response_wait_seconds)
            return operation.result() if done else self._view("checking")
        finally:
            # Cancelled HTTP callers must not release the browser guard while their
            # to_thread operation is still navigating or waiting for provider I/O.
            if operation.done():
                self._lock.release()
            else:
                operation.add_done_callback(lambda _: self._lock.release())

    async def _safe_status(self, *, start: bool) -> BenchmarkAuthStatus:
        try:
            return await self._status(start=start)
        except Exception:
            # Never relay provider exceptions, raw responses, paths, credentials or task IDs.
            self._logged_in = None
            self._last_check = 0.0
            return self._view(
                "provider_unavailable",
                error_code="BENCHMARK_AUTH_PROVIDER_UNAVAILABLE",
            )

    async def _status(self, *, start: bool) -> BenchmarkAuthStatus:
        now = time.monotonic()
        if start:
            if now - self._last_auth_start < 3:
                return self._view("awaiting_scan" if self._qr else "checking")
            self._last_auth_start = now
        if start or self._logged_in is None or now - self._last_check >= 3:
            checked = await self._platform_check()
            if type(checked) is not bool:
                return self._view("error", error_code="BENCHMARK_AUTH_STATUS_INVALID")
            self._logged_in = checked
            self._last_check = time.monotonic()
            self._checked_at = datetime.now(UTC)
        if self._logged_in:
            self._clear_qr()
            return self._view("authorized")
        expired = bool(self._expires_at and self._expires_at <= datetime.now(UTC))
        if expired:
            self._clear_qr()
            if not start:
                return self._view("expired")
        if self._qr:
            return self._view("awaiting_scan")
        if self._task_id:
            if start:
                return await self._finish_qr_operation()
            # A timed-out operation requires an explicit POST to resume its SAME
            # idempotency key and consume the provider's QR. GET never calls /qrcode.
            return self._view("error", error_code="BENCHMARK_AUTH_RETRY_REQUIRED")
        if self._request_id and not start:
            return self._view("error", error_code="BENCHMARK_AUTH_RETRY_REQUIRED")
        if not start:
            return self._view("login_required")
        if now - self._last_qr_start < 3:
            return self._view("checking")
        self._last_qr_start = now
        # Keep this identifier after uncertain transport failures. An explicit retry
        # reuses the provider's idempotency key and cannot create a second QR task.
        self._request_id = self._request_id or f"vistora-login-{uuid4().hex}"
        return await self._finish_qr_operation()

    async def _finish_qr_operation(self) -> BenchmarkAuthStatus:
        self._pending_since = time.monotonic()
        while True:
            if self._closed:
                return self._view("provider_unavailable", error_code="BENCHMARK_AUTH_STOPPED")
            task = await self._transport(
                "POST", "/xhs/login/qrcode?wait_seconds=5", {"request_id": self._request_id}
            )
            result = self._consume_task(task)
            if result.state != "checking" or self._task_id is None:
                return result
            if time.monotonic() - self._pending_since >= self._qr_operation_seconds:
                return self._view("error", error_code="BENCHMARK_AUTH_TASK_TIMEOUT")
            await asyncio.sleep(0.25)

    def _consume_task(self, task: dict[str, Any]) -> BenchmarkAuthStatus:
        task_id = task.get("task_id")
        if (
            not isinstance(task_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id)
            or task.get("kind") != "get_login_qrcode"
            or task.get("target_driver") != "managed"
            or (self._task_id is not None and self._task_id != task_id)
        ):
            return self._view("error", error_code="BENCHMARK_AUTH_PROVIDER_INVALID")
        self._task_id = task_id
        if task.get("status") in {"queued", "claimed", "running"}:
            if time.monotonic() - self._pending_since > self._qr_operation_seconds:
                # Do not enqueue another operation when the provider may still execute this one.
                return self._view("error", error_code="BENCHMARK_AUTH_TASK_TIMEOUT")
            return self._view("checking")
        if task.get("status") != "succeeded":
            self._clear_qr()
            return self._view("error", error_code="BENCHMARK_AUTH_TASK_FAILED")
        result = task.get("result")
        if not isinstance(result, dict):
            return self._view("error", error_code="BENCHMARK_AUTH_PROVIDER_INVALID")
        if result.get("is_logged_in") is True:
            self._clear_qr()
            self._last_check = 0.0
            return self._view("checking")  # Only the fresh platform check can authorize.
        if result.get("is_logged_in") is not False:
            self._clear_qr()
            return self._view("error", error_code="BENCHMARK_AUTH_PROVIDER_INVALID")
        try:
            qr = _png_data_url(result.get("image_data_url"))
            expires = datetime.fromisoformat(str(result.get("expires_at")).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, TypeError, binascii.Error):
            self._clear_qr()
            return self._view("error", error_code="BENCHMARK_AUTH_QR_INVALID")
        self._qr = qr
        self._expires_at = min(expires, datetime.now(UTC) + timedelta(seconds=120))
        if self._expires_at <= datetime.now(UTC):
            self._clear_qr()
            return self._view("expired")
        return self._view("awaiting_scan")


def _png_data_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512_000:
        raise ValueError("invalid QR image")
    if not value.startswith("data:image/png;base64,"):
        raise ValueError("QR image must be PNG")
    content = base64.b64decode(value.removeprefix("data:image/png;base64,"), validate=True)
    if len(content) < 33 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
        raise ValueError("invalid PNG header")
    width, height = struct.unpack(">II", content[16:24])
    if not 1 <= width <= 1024 or not 1 <= height <= 1024:
        raise ValueError("QR image dimensions exceed limit")
    return value
