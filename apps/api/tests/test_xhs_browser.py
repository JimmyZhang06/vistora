"""Offline browser/HTTP regression. Fixtures are not real platform login evidence."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from framefactory_api.benchmark_accounts import BenchmarkAccountError
from framefactory_api.benchmark_auth import LOGIN_STATE_SCRIPT, BenchmarkAuthService
from framefactory_api.xhs_browser import LocalXhsBrowser, create_browser_app


@pytest.mark.asyncio
async def test_local_http_rejects_remote_origins_and_bounds_input(tmp_path):
    browser = LocalXhsBrowser(tmp_path)
    app = create_browser_app(browser)
    local = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
    async with httpx.AsyncClient(transport=local, base_url="http://127.0.0.1") as client:
        for headers in ({"Origin": "https://evil.test"}, {"Origin": "http://localhost"},
                        {"Host": "evil.test"}, {"X-Forwarded-For": "127.0.0.1"},
                        {"Sec-Fetch-Site": "cross-site"}):
            response = await client.post("/xhs/login/qrcode", json={"request_id": "test"},
                                         headers=headers)
            assert response.status_code == 403
            assert response.headers["cache-control"] == "private, no-store"
        assert (await client.post("/xhs/login/qrcode", content="{}")).status_code == 415
        for body in ({}, {"request_id": "bad?token"}, {"request_id": 3},
                     {"request_id": "ok", "url": "http://127.0.0.1/private"}):
            assert (await client.post("/xhs/login/qrcode", json=body)).status_code == 422
        assert (await client.post("/xhs/login/qrcode", json={"request_id": "x" * 1025}
                                  )).status_code == 413
        assert (await client.get("/browser/managed/status")).json()["state"] == "unavailable"
        assert browser.task is None  # All GETs and rejected POSTs are non-destructive.
    remote = httpx.ASGITransport(app=app, client=("203.0.113.2", 1234))
    async with httpx.AsyncClient(transport=remote, base_url="http://localhost") as client:
        assert (await client.get("/browser/managed/status")).status_code == 403


@pytest.mark.asyncio
async def test_duplicate_timeout_and_expiry_do_not_create_extra_browser_work(tmp_path):
    browser = LocalXhsBrowser(tmp_path)
    calls = []
    ready = asyncio.Event()

    async def generate():
        calls.append("generated")
        await ready.wait()
        return {"is_logged_in": True}

    browser._read_qr = generate
    first = browser.request_qr("one")
    second = browser.request_qr("one")
    third = browser.request_qr("two")
    assert first == second == third
    await asyncio.sleep(0)
    assert calls == ["generated"]
    ready.set()
    await browser.task
    assert browser.request_qr("one")["result"] == {"is_logged_in": True}
    await browser._clear()
    assert browser.request_qr("one")["status"] == "failed"
    assert browser.task is None
    assert browser.request_qr("fresh")["task_id"] != first["task_id"]
    await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("headless", [True, pytest.param(
    False, marks=pytest.mark.skipif(os.name != "nt", reason="Windows headed QR lifecycle"),
)])
async def test_real_chromium_qr_projection_cdp_and_persistent_profile(tmp_path, headless):
    browser = LocalXhsBrowser(tmp_path / "profile", headless=headless)
    await browser.start()
    original_pages = list(browser.context.pages)
    try:
        # Deny all actual network traffic; only this explicit fixture is fulfilled.
        async def fixture(route):
            if route.request.is_navigation_request():
                await route.fulfill(content_type="text/html", body="""
                    <script>window.__INITIAL_STATE__={user:{userInfo:{guest:true}}};</script>
                    <div class="login-container"><canvas width="160" height="160"></canvas></div>
                    <script>document.querySelector('canvas').getContext('2d').fillRect(0,0,160,160);
                    localStorage.setItem('vistora-offline-fixture', 'saved');</script>
                """)
            else:
                await route.abort()

        await browser.context.route("**/*", fixture)
        assert browser.status()["state"] == "running"
        # Both profile and note collectors require this exact loopback contract.
        assert browser.status()["cdp_host"] == "127.0.0.1"
        cdp = await browser.playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{browser.cdp_port}"
        )
        assert cdp.contexts
        await cdp.close()  # Disconnecting an API helper must preserve this context.
        first = browser.request_qr("offline-qr")
        await browser.task
        qr = browser.request_qr("offline-qr")
        assert qr["task_id"] == first["task_id"]
        assert qr["status"] == "succeeded"
        assert qr["result"]["is_logged_in"] is False
        assert qr["result"]["image_data_url"].startswith("data:image/png;base64,")
        assert "xsec" not in json.dumps(qr).lower()
        browser.deadline = time.monotonic() - 1
        await asyncio.sleep(1.1)
        assert browser.page is None and browser.result is None
        if headless:
            assert browser.context.pages == original_pages
        else:
            assert browser._interactive_page is not None
            assert not browser._interactive_page.is_closed()
            assert len(browser.context.pages) == len(original_pages) + 1
    finally:
        await browser.close()
    # Reopen the same dedicated profile: browser login storage survives service exit.
    restarted = LocalXhsBrowser(tmp_path / "profile", headless=True)
    await restarted.start()
    try:
        await restarted.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body="<p>offline</p>"
        ))
        page = await restarted.context.new_page()
        await page.goto("https://www.xiaohongshu.com/explore")
        assert await page.evaluate("localStorage.getItem('vistora-offline-fixture')") == "saved"
        await page.evaluate("window.__INITIAL_STATE__={user:{userPageData:{basicInfo:{"
                            "userId:'111111111111111111111111'}}}}")
        assert await page.evaluate(LOGIN_STATE_SCRIPT) is None
        await page.evaluate("window.__INITIAL_STATE__.user.userInfo={guest:false,"
                            "userId:'222222222222222222222222'}")
        assert await page.evaluate(LOGIN_STATE_SCRIPT) is True
        await page.evaluate("history.replaceState({}, '', '/website-login/error')")
        assert await page.evaluate(LOGIN_STATE_SCRIPT) == "restricted"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_platform_restriction_is_actionable_and_does_not_request_qr():
    async def blocked():
        raise BenchmarkAccountError("BENCHMARK_AUTH_PLATFORM_RESTRICTED", "secret-query")

    async def forbidden_transport(method, path, body):
        if method == "GET" and path == "/browser/managed/status":
            return {"provider": "external-provider"}
        pytest.fail("risk checks must not request repeated QR operations")

    service = BenchmarkAuthService("http://127.0.0.1:5556", platform_check=blocked,
                                   transport=forbidden_transport)
    try:
        for start in (False, True):
            result = await service.status(start=start)
            assert result.state == "error"
            assert result.error_code == "BENCHMARK_AUTH_PLATFORM_RESTRICTED"
            assert "IP" in result.message
            assert "secret-query" not in result.model_dump_json()
            assert result.qr_image_data_url is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_explicit_native_login_retains_verification_and_authorizes_only_after_fresh_check():
    verified = False
    calls = []

    async def check():
        if verified:
            return True
        raise BenchmarkAccountError("BENCHMARK_AUTH_PLATFORM_RESTRICTED", "redacted")

    async def transport(method, path, body):
        calls.append((method, path))
        if method == "GET":
            return {"provider": "vistora-local-xhs"}
        return {"task_id": "manual-one", "kind": "get_login_qrcode", "target_driver": "managed",
                "status": "succeeded", "result": {"is_logged_in": False,
                "requires_verification": True,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=180)).isoformat()}}

    service = BenchmarkAuthService("http://127.0.0.1:5556", transport=transport,
                                   platform_check=check)
    try:
        assert (await service.status()).state == "error"
        assert calls == []  # Reading the page alone never opens an interactive browser.
        result = await service.status(start=True)
        assert result.state == "checking"
        assert result.error_code == "BENCHMARK_AUTH_MANUAL_VERIFICATION"
        assert result.qr_image_data_url is None
        for _ in range(3):
            result = await service.status()
            assert result.error_code == "BENCHMARK_AUTH_MANUAL_VERIFICATION"
        assert len([call for call in calls if call[0] == "POST"]) == 1
        # Polling verifies window liveness, but never opens additional pages.
        verified = True
        assert (await service.status()).state == "authorized"
        assert service._manual_until is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_manual_login_window_is_bounded_and_never_promises_headless_interaction(
    tmp_path, monkeypatch,
):
    # A fixed module-local clock checks the exact bound without floating-point
    # subtraction noise or changing asyncio's event-loop clock.
    monkeypatch.setattr(
        "framefactory_api.xhs_browser.time", SimpleNamespace(monotonic=lambda: 100.25),
    )
    browser = LocalXhsBrowser(tmp_path)
    shown = []

    async def show():
        shown.append(True)

    browser._show_window = show
    browser.page = SimpleNamespace(url="https://www.xiaohongshu.com/website-login/error",
                                   bring_to_front=show)
    result = await browser._manual_verification()
    assert result["requires_verification"] is True
    assert result["is_logged_in"] is False
    assert browser.deadline == 280.25
    assert shown == [True]
    browser.headless = True
    with pytest.raises(ValueError, match="interactive login unavailable"):
        await browser._manual_verification()
    browser.headless = False
    browser.page.url = "https://untrusted.test/"
    with pytest.raises(ValueError, match="interactive login unavailable"):
        await browser._manual_verification()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BenchmarkAccountError, RuntimeError, None])
async def test_disconnected_native_browser_recovers_only_on_explicit_login(failure):
    calls = []

    async def disconnected():
        if failure is None:
            return None
        if failure is BenchmarkAccountError:
            raise failure("BENCHMARK_AUTHENTICATION_REQUIRED", "closed")
        raise failure("closed")

    async def transport(method, path, body):
        calls.append((method, path))
        if method == "GET":
            return {"provider": "vistora-local-xhs", "state": "unavailable"}
        return {"task_id": "reopened", "status": "succeeded",
                "kind": "get_login_qrcode", "target_driver": "managed",
                "result": {"is_logged_in": False, "requires_verification": True,
                           "expires_at": (datetime.now(UTC) + timedelta(seconds=180)).isoformat()}}

    service = BenchmarkAuthService("http://127.0.0.1:5556", transport=transport,
                                   platform_check=disconnected)
    try:
        assert (await service.status()).state == ("checking" if failure is None
                                                 else "provider_unavailable")
        assert calls == []
        result = await service.status(start=True)
        assert result.error_code == "BENCHMARK_AUTH_MANUAL_VERIFICATION"
        assert result.expires_at is not None
        assert [method for method, _ in calls] == ["GET", "POST"]
        result = await service.status()  # A closed window must not keep the old success hint.
        if failure is not None:
            assert result.state == "provider_unavailable"
            assert result.error_code != "BENCHMARK_AUTH_MANUAL_VERIFICATION"
            assert service._manual_until is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_real_disconnected_browser_reopens_once_with_same_profile(tmp_path):
    browser = LocalXhsBrowser(tmp_path / "profile", headless=True)
    await browser.start()
    try:
        old_context = browser.context
        await old_context.close()
        assert browser.status()["state"] == "unavailable"
        await asyncio.gather(browser.ensure_running(), browser.ensure_running())
        assert browser.context is not old_context
        assert browser.status()["state"] == "running"
        assert browser.profile_dir == (tmp_path / "profile").resolve()
        assert browser.janitor and not browser.janitor.done()
    finally:
        await browser.close()


@pytest.mark.asyncio
async def test_closed_manual_tab_clears_opened_hint():
    async def blocked():
        raise BenchmarkAccountError("BENCHMARK_AUTH_PLATFORM_RESTRICTED", "restricted")

    async def status_only(method, path, body):
        assert method == "GET"
        return {"provider": "vistora-local-xhs", "verification_open": False}

    service = BenchmarkAuthService("http://127.0.0.1:5556", transport=status_only,
                                   platform_check=blocked)
    service._manual_until = datetime.now(UTC) + timedelta(seconds=180)
    try:
        result = await service.status()
        assert result.error_code == "BENCHMARK_AUTH_WINDOW_CLOSED"
        assert result.state == "provider_unavailable"
        assert service._manual_until is None
    finally:
        await service.close()


@pytest.mark.skipif(os.name != "nt", reason="real Windows native-window regression")
@pytest.mark.asyncio
async def test_real_manual_window_restores_and_survives_polling_expiry(tmp_path):
    browser = LocalXhsBrowser(tmp_path / "profile")
    await browser.start()
    try:
        await browser.context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body="<h1>Vistora offline window regression</h1>",
        ))
        browser.page = await browser.context.new_page()
        await browser.page.goto("https://www.xiaohongshu.com/website-login/error")
        page = browser.page
        session = await browser.context.new_cdp_session(page)
        window = await session.send("Browser.getWindowForTarget")
        await session.send("Browser.setWindowBounds", {
            "windowId": window["windowId"], "bounds": {"windowState": "minimized"},
        })
        result = await browser._manual_verification()
        assert result["requires_verification"] is True
        bounds = await session.send("Browser.getWindowBounds", {"windowId": window["windowId"]})
        assert bounds["bounds"]["windowState"] == "normal"
        browser.task_id = "expiring-test"
        browser.deadline = time.monotonic() - 1
        await asyncio.sleep(1.1)
        assert browser.result is None and browser.task_id is None
        assert not page.is_closed()  # Expire polling material, never interrupt human verification.
        assert browser._interactive_page is page
        await browser._read_qr()
        assert browser.page is page  # Retrying reuses the retained window.
        await session.detach()
    finally:
        await browser.close()
