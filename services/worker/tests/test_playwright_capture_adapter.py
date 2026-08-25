from __future__ import annotations

import asyncio
import ipaddress
import struct
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.worker.web_capture.models import CaptureViewport
from framefactory.worker.web_capture.playwright_adapter import (
    PlaywrightChromiumCaptureAdapter,
    _png_pixel_size,
    _region_clip,
)
from framefactory.worker.web_capture.security import PublicHttpsUrlValidator


class _FakePlaywrightError(Exception):
    pass


class _FakePlaywrightTimeout(_FakePlaywrightError):
    pass


class _Request:
    def __init__(
        self,
        url: str,
        *,
        frame: object,
        method: str = "GET",
        navigation: bool = False,
        redirected_from: _Request | None = None,
    ) -> None:
        self.url = url
        self.frame = frame
        self.method = method
        self.redirected_from = redirected_from
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class _Route:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self, _reason: str) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _Response:
    def __init__(self, request: _Request, *, headers: dict[str, str] | None = None) -> None:
        self.request = request
        self.status = 200
        self.headers = headers or {}


class _WebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self, **_kwargs: object) -> None:
        self.closed = True


class _Locator:
    def __init__(self, selector: str) -> None:
        self.selector = selector

    async def count(self) -> int:
        return 0

    async def inner_text(self, *, timeout: int) -> str:
        del timeout
        return "safe body"


class _Cdp:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    async def send(self, _method: str) -> None:
        return None

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    def emit(self, event: str, payload: dict[str, object]) -> None:
        handler = self.handlers.get(event)
        if callable(handler):
            handler(payload)


class _Page:
    def __init__(self, context: _Context, scenario: str) -> None:
        self.context = context
        self.scenario = scenario
        self.main_frame = object()
        self.url = "https://example.test/start"
        self.closed = False
        self.handlers: dict[str, object] = {}
        self.websocket_handler: object | None = None
        self.websocket = _WebSocket()

    async def route_web_socket(self, _pattern: str, handler: object) -> None:
        self.websocket_handler = handler

    async def goto(self, url: str, **_kwargs: object) -> _Response:
        self.url = url
        initial = _Request(url, frame=self.main_frame, navigation=True)
        await self._request(initial)
        self.context.cdp.emit("Page.frameNavigated", {"frame": {"id": "main"}})
        if self.scenario == "unsafe_redirect":
            unsafe = _Request(
                "https://127.0.0.1/latest/meta-data",
                frame=self.main_frame,
                navigation=True,
                redirected_from=initial,
            )
            await self._request(unsafe, raise_when_aborted=True)
        if self.scenario == "post":
            post = _Request(
                "https://example.test/mutate",
                frame=self.main_frame,
                method="POST",
            )
            await self._request(post)
        if self.scenario == "post_navigation":
            post_navigation = _Request(
                "https://example.test/form-submit",
                frame=self.main_frame,
                method="POST",
                navigation=True,
            )
            await self._request(post_navigation, raise_when_aborted=True)
        if self.scenario == "oversized_content_length":
            handler = self.handlers["response"]
            assert callable(handler)
            await handler(_Response(initial, headers={"content-length": "999999999"}))
            raise _FakePlaywrightError("page was closed")
        if self.scenario == "websocket":
            assert callable(self.websocket_handler)
            await self.websocket_handler(self.websocket)
        return _Response(initial)

    async def _request(self, request: _Request, *, raise_when_aborted: bool = False) -> None:
        route = _Route()
        assert callable(self.context.route_handler)
        await self.context.route_handler(route, request)
        if route.aborted and raise_when_aborted:
            raise _FakePlaywrightError("request blocked")

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    async def wait_for_load_state(self, state: str, **_kwargs: object) -> None:
        if self.scenario == "load_timeout" and state == "load":
            raise _FakePlaywrightTimeout("document never loaded")
        if self.scenario == "network_busy" and state == "networkidle":
            raise _FakePlaywrightTimeout("network remained busy")

    async def evaluate(self, script: str, _argument: object) -> object:
        if "captureStability" in script:
            if self.scenario == "page_unsettled":
                return {
                    "stable": False,
                    "readyState": "complete",
                    "stableSamples": 0,
                    "visibleBusyCount": 1,
                }
            return {
                "stable": True,
                "readyState": "complete",
                "stableSamples": 4,
                "visibleBusyCount": 0,
            }
        return None

    def locator(self, selector: str) -> _Locator:
        return _Locator(selector)

    async def title(self) -> str:
        return "safe title"

    async def screenshot(self, **_kwargs: object) -> bytes:
        if self.scenario == "late_unsafe_subresource":
            await self._request(
                _Request("https://10.0.0.7/secret", frame=self.main_frame)
            )
        if self.scenario == "late_navigation":
            self.url = "https://other.example.test/changed"
            self.context.cdp.emit("Page.frameNavigated", {"frame": {"id": "main"}})
        return (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 1920, 1080)
        )

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _Context:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.route_handler: object | None = None
        self.cdp = _Cdp()
        self.page = _Page(self, scenario)

    async def route(self, _pattern: str, handler: object) -> None:
        self.route_handler = handler

    async def new_page(self) -> _Page:
        return self.page

    async def new_cdp_session(self, _page: _Page) -> _Cdp:
        return self.cdp

    async def close(self) -> None:
        return None


class _Browser:
    version = "140.0.0"

    def __init__(self, scenario: str) -> None:
        self.context = _Context(scenario)

    async def new_context(self, **_kwargs: object) -> _Context:
        return self.context

    async def close(self) -> None:
        return None


class _Chromium:
    def __init__(self, scenario: str) -> None:
        self.browser = _Browser(scenario)
        self.launch_kwargs: dict[str, object] = {}

    async def launch(self, **kwargs: object) -> _Browser:
        self.launch_kwargs = kwargs
        return self.browser


class _Manager:
    def __init__(self, chromium: _Chromium) -> None:
        self.chromium = chromium

    async def __aenter__(self) -> SimpleNamespace:
        return SimpleNamespace(chromium=self.chromium)

    async def __aexit__(self, *_args: object) -> None:
        return None


def _resolver(host: str, _port: int) -> tuple[str, ...]:
    try:
        return (str(ipaddress.ip_address(host)),)
    except ValueError:
        return ("93.184.216.34",)


class PlaywrightCaptureAdapterTests(unittest.TestCase):
    def test_region_capture_keeps_viewport_context_and_relative_focus(self) -> None:
        capture = _region_clip(
            {
                "x": 0,
                "y": 1792,
                "width": 1080,
                "height": 728,
                "documentWidth": 1080,
                "documentHeight": 5000,
            },
            CaptureViewport("9:16", 1080, 1920),
        )

        self.assertIsNotNone(capture)
        assert capture is not None
        self.assertEqual(
            {"x": 0.0, "y": 1196.0, "width": 1080.0, "height": 1920.0},
            capture["clip"],
        )
        self.assertEqual(
            {"x": 0.0, "y": 596.0, "width": 1080.0, "height": 728.0},
            capture["focus"],
        )

    def test_png_pixel_size_reads_emitted_surface_dimensions(self) -> None:
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (995).to_bytes(4, "big")
            + (780).to_bytes(4, "big")
        )

        self.assertEqual((995, 780), _png_pixel_size(png))

    def _capture(self, scenario: str):
        chromium = _Chromium(scenario)
        async_api = types.ModuleType("playwright.async_api")
        async_api.Error = _FakePlaywrightError
        async_api.TimeoutError = _FakePlaywrightTimeout
        async_api.async_playwright = lambda: _Manager(chromium)
        package = types.ModuleType("playwright")
        package.async_api = async_api
        settings = SimpleNamespace(
            egress_policy_enforced=True,
            proxy_url="https://proxy.example:8443",
            proxy_username="",
            proxy_password="",
            job_timeout_seconds=5,
            navigation_timeout_seconds=1,
            settle_timeout_seconds=1,
            screenshot_timeout_seconds=1,
            maximum_resources=20,
            maximum_redirects=3,
            maximum_transfer_bytes=1_000_000,
        )
        adapter = PlaywrightChromiumCaptureAdapter(
            settings, PublicHttpsUrlValidator(resolver=_resolver)
        )
        with patch.dict(
            sys.modules,
            {"playwright": package, "playwright.async_api": async_api},
        ):
            result = asyncio.run(
                adapter.capture(
                    "https://example.test/start",
                    CaptureViewport("16:9", 1920, 1080),
                    maximum_body_characters=12_000,
                )
            )
        return result, chromium

    def test_sandbox_webrtc_and_websocket_guards_are_enabled(self) -> None:
        result, chromium = self._capture("websocket")
        self.assertTrue(chromium.launch_kwargs["chromium_sandbox"])
        arguments = tuple(chromium.launch_kwargs["args"])
        self.assertIn("--webrtc-ip-handling-policy=disable_non_proxied_udp", arguments)
        self.assertNotIn("--no-sandbox", arguments)
        self.assertEqual(1, result.websocket_attempts)
        self.assertTrue(chromium.browser.context.page.websocket.closed)

    def test_private_redirect_is_a_permanent_policy_failure(self) -> None:
        with self.assertRaises(PermanentStepError) as caught:
            self._capture("unsafe_redirect")
        self.assertEqual("browser_capture_policy_blocked", caught.exception.code)
        self.assertNotIn("127.0.0.1", str(caught.exception))

    def test_non_idempotent_subrequest_is_blocked_without_discarding_capture(self) -> None:
        result, _chromium = self._capture("post")
        self.assertEqual(1, result.blocked_non_idempotent_requests)

    def test_non_idempotent_navigation_is_a_permanent_policy_failure(self) -> None:
        with self.assertRaises(PermanentStepError) as caught:
            self._capture("post_navigation")
        self.assertEqual("browser_capture_policy_blocked", caught.exception.code)

    def test_declared_oversized_response_is_rejected_before_capture(self) -> None:
        with self.assertRaises(PermanentStepError) as caught:
            self._capture("oversized_content_length")
        self.assertEqual("browser_capture_policy_blocked", caught.exception.code)

    def test_late_unsafe_subresource_is_blocked_without_discarding_page(self) -> None:
        result, _chromium = self._capture("late_unsafe_subresource")
        self.assertEqual(1, result.blocked_unsafe_subresources)

    def test_delayed_navigation_cannot_change_screenshot_provenance(self) -> None:
        with self.assertRaises(PermanentStepError) as caught:
            self._capture("late_navigation")
        self.assertEqual("browser_capture_navigation_unstable", caught.exception.code)

    def test_document_load_timeout_can_capture_after_visual_stability(self) -> None:
        result, _chromium = self._capture("load_timeout")

        self.assertTrue(result.network_idle_timed_out)

    def test_polling_page_can_capture_after_visual_stability(self) -> None:
        result, _chromium = self._capture("network_busy")
        self.assertTrue(result.network_idle_timed_out)

    def test_visible_loading_state_is_retryable_and_never_screenshots(self) -> None:
        with self.assertRaises(RetryableStepError) as caught:
            self._capture("page_unsettled")
        self.assertEqual("browser_page_not_stable", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
