"""Project-owned, single-user Xiaohongshu browser and ephemeral login QR bridge.

Run with ``python -m framefactory_api.xhs_browser``. No platform cookies, URLs,
page bodies or QR payloads are logged. This is a local development service only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .benchmark_auth import LOGIN_STATE_SCRIPT, _png_data_url

PLATFORM_URL = "https://www.xiaohongshu.com/explore"
QR_SELECTOR = (
    ".login-container .qrcode-img, .login-container .qrcode img, "
    ".login-container .qr-code img, .login-container canvas"
)


def platform_restricted(url: str) -> bool:
    return urlsplit(url).path.startswith(("/website-login/error", "/website-login/captcha"))


class LocalXhsBrowser:
    def __init__(self, profile_dir: Path, *, headless: bool = False) -> None:
        self.profile_dir = profile_dir.resolve()
        self.headless = headless
        self.context = None
        self.playwright = None
        self.cdp_port: int | None = None
        self.task: asyncio.Task | None = None
        self.task_id: str | None = None
        self.request_ids: set[str] = set()
        self.expired_requests: dict[str, tuple[str, float]] = {}
        self.result: dict | None = None
        self.deadline = 0.0
        self.page = None
        self.janitor: asyncio.Task | None = None
        self._launch_lock = asyncio.Lock()
        self._interactive_page = None

    async def ensure_running(self) -> None:
        # Only an explicit login POST may reopen a browser the user closed.
        async with self._launch_lock:
            if self.status()["state"] == "running":
                return
            await self.close()
            await self.start()

    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless,
                chromium_sandbox=True, accept_downloads=False,
                args=["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
                timeout=30_000,
            )
            # Chromium chooses an unused CDP port; never attach a guessed/stale port.
            active_port = self.profile_dir / "DevToolsActivePort"
            self.cdp_port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
            if not 1 <= self.cdp_port <= 65535:
                raise ValueError("invalid browser port")
            self.janitor = asyncio.create_task(self._expire())
        except BaseException:
            await self.close()
            raise

    def status(self) -> dict:
        connected = bool(self.context and self.context.browser.is_connected())
        page = self.page or self._interactive_page
        return {"state": "running" if connected else "unavailable",
                "cdp_host": "127.0.0.1",
                "cdp_port": self.cdp_port if connected else None,
                "verification_open": bool(connected and page and not page.is_closed()),
                "provider": "vistora-local-xhs", "protocol_version": 1}

    async def _clear(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.task_id:
            for request_id in self.request_ids:
                self.expired_requests[request_id] = (self.task_id, time.monotonic() + 300)
            while len(self.expired_requests) > 32:
                del self.expired_requests[next(iter(self.expired_requests))]
        self.task = None
        self.task_id = None
        self.result = None
        self.request_ids.clear()
        if self.page is not None:
            page, self.page = self.page, None
            if page is not self._interactive_page:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(page.close(), timeout=3)

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.task_id and time.monotonic() >= self.deadline:
                # Erase QR material even when the caller stops polling.
                await self._clear()

    async def close(self) -> None:
        if self.janitor:
            self.janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.janitor
        await self._clear()
        if self.context:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.context.close(), timeout=5)
        if self.playwright:
            await self.playwright.stop()
        self.context = self.playwright = self.janitor = self._interactive_page = None
        self.cdp_port = None

    def request_qr(self, request_id: str) -> dict:
        # Called without awaits on one event loop: requests cannot enqueue duplicate
        # browser work. Other API clients share the active operation, never a new QR.
        self.expired_requests = {key: value for key, value in self.expired_requests.items()
                                 if value[1] > time.monotonic()}
        if request_id in self.expired_requests:
            return {"task_id": self.expired_requests[request_id][0],
                    "kind": "get_login_qrcode", "target_driver": "managed", "status": "failed"}
        if self.task is None:
            self.task_id = f"xhs-{uuid4().hex}"
            self.deadline = time.monotonic() + 120
            self.task = asyncio.create_task(self._generate())
        if len(self.request_ids) < 32:
            self.request_ids.add(request_id)
        return {"task_id": self.task_id, "kind": "get_login_qrcode",
                "target_driver": "managed", **(self.result or {"status": "running"})}

    async def _generate(self) -> None:
        try:
            async with asyncio.timeout(35):
                self.result = {"status": "succeeded", "result": await self._read_qr()}
        except PlaywrightTimeoutError:
            # A changed QR selector must leave a usable, user-controlled login page.
            try:
                self.result = {"status": "succeeded", "result": await self._manual_verification()}
            except Exception:
                self.result = {"status": "failed"}
                self.deadline = min(self.deadline, time.monotonic() + 3)
        except Exception:
            # No browser exceptions or signed URLs cross the service boundary.
            self.result = {"status": "failed"}
            self.deadline = min(self.deadline, time.monotonic() + 3)

    async def _manual_verification(self) -> dict:
        current = urlsplit(self.page.url) if self.page else None
        if (self.headless or current is None or current.scheme != "https"
                or current.netloc != "www.xiaohongshu.com"):
            raise ValueError("interactive login unavailable")
        await asyncio.wait_for(self._show_window(), timeout=5)
        self._interactive_page = self.page
        self.deadline = time.monotonic() + 180
        return {"is_logged_in": False, "requires_verification": True,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=180)).isoformat()}

    async def _show_window(self) -> None:
        # bring_to_front activates the tab; restore the actual native window too.
        session = await self.context.new_cdp_session(self.page)
        try:
            window = await session.send("Browser.getWindowForTarget")
            await session.send("Browser.setWindowBounds", {
                "windowId": window["windowId"], "bounds": {"windowState": "normal"},
            })
            await session.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"left": 80, "top": 80, "width": 1100, "height": 800},
            })
            await self.page.bring_to_front()
        finally:
            await session.detach()

    async def _read_qr(self) -> dict:
        reuse = self._interactive_page is not None and not self._interactive_page.is_closed()
        self.page = self._interactive_page if reuse else await self.context.new_page()
        page = self.page
        if not self.headless:
            # Expire QR material, not the window in which the user is signing in.
            self._interactive_page = page
        page.set_default_timeout(10_000)
        response = None if reuse else await page.goto(
            PLATFORM_URL, wait_until="domcontentloaded", timeout=20_000,
        )
        current = urlsplit(page.url)
        if current.scheme != "https" or current.netloc != "www.xiaohongshu.com":
            raise ValueError("unexpected platform navigation")
        if platform_restricted(page.url):
            return await self._manual_verification()
        if not reuse and (response is None or response.status >= 400):
            raise ValueError("platform unavailable")
        await page.wait_for_function(f"() => ({LOGIN_STATE_SCRIPT})() !== null", timeout=5_000)
        current = urlsplit(page.url)
        if current.scheme != "https" or current.netloc != "www.xiaohongshu.com":
            raise ValueError("unexpected platform navigation")
        state = await page.evaluate(LOGIN_STATE_SCRIPT)
        if state == "restricted":
            return await self._manual_verification()
        if state is True:
            return {"is_logged_in": True}
        if not await page.locator(".login-container").is_visible():
            await page.get_by_text("登录", exact=True).first.click()
        qr = page.locator(QR_SELECTOR).first
        await qr.wait_for(state="visible")
        await page.wait_for_function(
            "selector => { const el = document.querySelector(selector); "
            "return el && (el.tagName === 'CANVAS' || (el.complete && el.naturalWidth > 0)); }",
            arg=QR_SELECTOR,
        )
        box = await qr.bounding_box()
        if not box or not 1 <= box["width"] <= 1024 or not 1 <= box["height"] <= 1024:
            raise ValueError("invalid QR dimensions")
        data = await qr.screenshot(type="png", timeout=5_000)
        image = _png_data_url("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
        return {"is_logged_in": False, "image_data_url": image,
                "expires_at": (datetime.now(UTC) + timedelta(
                    seconds=max(0, self.deadline - time.monotonic())
                )).isoformat()}


def create_browser_app(browser: LocalXhsBrowser) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await browser.start()
        try:
            yield
        finally:
            await browser.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        if (not local or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or any(request.headers.get(key) for key in (
                    "origin", "forwarded", "x-forwarded-for", "x-forwarded-host",
                )) or request.headers.get("sec-fetch-site") == "cross-site"):
            response = JSONResponse({"error": "local_only"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/browser/managed/status")
    async def status():
        return browser.status()

    @app.post("/xhs/login/qrcode")
    async def qrcode(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "json_required"}, status_code=415)
        raw = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 1024:
                        return JSONResponse({"error": "request_too_large"}, status_code=413)
        except TimeoutError:
            return JSONResponse({"error": "request_timeout"}, status_code=408)
        try:
            body = json.loads(raw)
            if (not isinstance(body, dict) or set(body) != {"request_id"}
                    or not isinstance(body["request_id"], str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["request_id"])):
                raise ValueError
        except (ValueError, UnicodeError):
            return JSONResponse({"error": "invalid_request"}, status_code=422)
        try:
            await browser.ensure_running()
            if browser.page and browser.page.is_closed():
                await browser._clear()
            elif (browser.page is not None and browser.page is browser._interactive_page
                  and browser.result and browser.result.get("result", {}).get(
                      "requires_verification") is True):
                browser.result = {"status": "succeeded",
                                  "result": await browser._manual_verification()}
        except Exception:
            return JSONResponse({"error": "browser_unavailable"}, status_code=503)
        return browser.request_qr(body["request_id"])

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Vistora local Xiaohongshu browser")
    parser.add_argument("--port", type=int, default=5556)
    parser.add_argument("--profile-dir", type=Path, default=Path("var/browser/xiaohongshu"))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if os.getenv("FRAMEFACTORY_ENV", "development").strip().lower() in {"prod", "production"}:
        parser.error("the browser service is restricted to local development")
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    browser = LocalXhsBrowser(args.profile_dir, headless=args.headless)
    app = create_browser_app(browser)
    # asyncio.run keeps the Windows Proactor loop required by Playwright subprocesses.
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False,
        log_level="error", limit_concurrency=16, timeout_keep_alive=3,
    ))
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
