#!/usr/bin/env python3
"""Local mock managed-browser provider for xiaohongshu auth dev flow.

The mock keeps the same HTTP contract used by `start-benchmark-automation`:
- GET /browser/managed/status
- POST /xhs/login/qrcode

It also launches a Chromium process with a remote-debugging endpoint so
`playwright.connect_over_cdp` checks in the API can succeed.
"""

from __future__ import annotations

import argparse
import base64
import asyncio
import json
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any
from uuid import uuid4
from hashlib import sha256

from PIL import Image, ImageDraw
from playwright.async_api import async_playwright


@dataclass
class LoginTask:
    task_id: str
    request_id: str
    status: str
    is_logged_in: bool
    image_data_url: str
    expires_at: str
    created_at: float
    logged_in_at: float | None = None


def _next_expiry(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _build_mock_qr(seed: str, size: int = 220) -> str:
    digest = sha256(seed.encode("utf-8")).digest()
    image = Image.new("RGB", (size, size), "white")
    canvas = ImageDraw.Draw(image)

    # simple, deterministic tile pattern to resemble a QR-like square
    tile_size = 4
    margin = 8
    for row in range(margin, size - margin, tile_size):
        for col in range(margin, size - margin, tile_size):
            index = ((row // tile_size) * 1000 + (col // tile_size)) % len(digest)
            if digest[index] % 2:
                r = 30 + (digest[(index + 1) % len(digest)] % 200)
                g = 40 + (digest[(index + 2) % len(digest)] % 180)
                b = 40 + (digest[(index + 3) % len(digest)] % 180)
                canvas.rectangle((col, row, col + tile_size - 1, row + tile_size - 1), fill=(r, g, b))

    # Finder-style corner markers for easier human confirmation.
    marker_fill = (0, 0, 0)
    for x, y in ((0, 0), (size - 28, 0), (0, size - 28)):
        canvas.rectangle((x, y, x + 27, y + 27), outline=(0, 0, 0), width=1)
        canvas.rectangle((x + 4, y + 4, x + 23, y + 23), fill=(255, 255, 255), outline=(0, 0, 0), width=1)
        canvas.rectangle((x + 8, y + 8, x + 19, y + 19), fill=marker_fill)

    label = "Vistora mock login"
    canvas.text((size // 2 - 44, size - 18), label, fill=(60, 60, 60))

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{payload}"


class MockProviderState:
    def __init__(self, cdp_port: int, auto_logged_in_after: int | None = None) -> None:
        self.cdp_port = cdp_port
        self.auto_logged_in_after = auto_logged_in_after
        self._lock = threading.Lock()
        self._tasks: dict[str, LoginTask] = {}
        self._request_to_task: dict[str, str] = {}
        self._cdp_running = threading.Event()
        self._stop = threading.Event()

    @property
    def cdp_running(self) -> bool:
        return self._cdp_running.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_shutdown(self) -> None:
        self._stop.set()

    def mark_cdp_running(self) -> None:
        self._cdp_running.set()

    def mark_cdp_stopped(self) -> None:
        self._cdp_running.clear()

    def status_payload(self) -> dict[str, Any]:
        return {
            "state": "running" if self.cdp_running else "starting",
            "cdp_port": self.cdp_port,
            "connected": self.cdp_running,
        }

    def qrcode_payload(self, request_id: str | None) -> LoginTask:
        request_id = (request_id or f"vistora-mock-{uuid4().hex}")[:128]

        # Keep one task per request_id.
        with self._lock:
            task_id = self._request_to_task.get(request_id)
            if task_id is not None and task_id in self._tasks:
                task = self._tasks[task_id]
                expired = datetime.fromisoformat(task.expires_at.replace("Z", "+00:00")).astimezone(
                    timezone.utc
                ) <= datetime.now(timezone.utc)
                if not expired:
                    if self.auto_logged_in_after is not None and not task.is_logged_in:
                        elapsed = self._auto_logged_in_seconds(task)
                        if elapsed >= self.auto_logged_in_after:
                            task.status = "succeeded"
                            task.is_logged_in = True
                            task.image_data_url = ""
                            task.logged_in_at = time.monotonic()
                    return task

            is_logged_in = False
            task = LoginTask(
                task_id=f"mock-task-{uuid4().hex[:16]}",
                request_id=request_id,
                status="succeeded",
                is_logged_in=is_logged_in,
                image_data_url=_build_mock_qr(request_id, size=220),
                expires_at=_next_expiry(120),
                created_at=time.monotonic(),
            )
            self._tasks[task.task_id] = task
            self._request_to_task[request_id] = task.task_id
            if self.auto_logged_in_after is not None and task.expires_at:
                elapsed = self._auto_logged_in_seconds(task)
                if elapsed >= self.auto_logged_in_after:
                    task.status = "succeeded"
                    task.is_logged_in = True
                    task.image_data_url = ""
                    task.logged_in_at = time.monotonic()
            return task

    def _auto_logged_in_seconds(self, task: LoginTask) -> int:
        if not self.auto_logged_in_after:
            return 0
        return int(max(0.0, time.monotonic() - task.created_at))


def _run_provider_http(host: str, port: int, state: MockProviderState) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "VistoraMockProvider/1.0"
        protocol_version = "HTTP/1.1"

        def _json(self, payload: dict[str, Any], status_code: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: str) -> None:
            return

        def _read_json(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            try:
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    return None
                return payload
            except Exception:
                return None

        def do_GET(self) -> None:
            if self.path.startswith("/browser/managed/status"):
                self._json(state.status_payload())
                return
            if self.path in {"/", "/health", "/healthz"}:
                self._json({"ok": True})
                return
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path.startswith("/xhs/login/qrcode"):
                body = self._read_json()
                if body is None:
                    self._json({"error": "invalid_json"}, HTTPStatus.UNPROCESSABLE_ENTITY)
                    return
                request_id = body.get("request_id")
                if request_id is None:
                    request_id = f"vistora-mock-request-{uuid4().hex}"
                if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                    self._json({"error": "invalid_request_id"}, HTTPStatus.UNPROCESSABLE_ENTITY)
                    return
                task = state.qrcode_payload(request_id)
                payload: dict[str, Any] = {
                    "task_id": task.task_id,
                    "kind": "get_login_qrcode",
                    "target_driver": "managed",
                    "status": task.status,
                    "result": {
                        "is_logged_in": task.is_logged_in,
                        "expires_at": task.expires_at,
                    },
                }
                if not task.is_logged_in:
                    payload["result"]["image_data_url"] = task.image_data_url
                self._json(payload)
                return
            self._json({"error": "method_not_allowed"}, HTTPStatus.METHOD_NOT_ALLOWED)

    server = ThreadingHTTPServer((host, port), Handler)
    server.timeout = 0.25
    try:
        while not state.stop_requested:
            server.handle_request()
    finally:
        server.server_close()


def _run_cdp_server(port: int, headful: bool, ready: threading.Event, stop: threading.Event) -> None:
    async def _runner() -> None:
        try:
            playwright = await async_playwright().start()
        except Exception:
            # keep failure visible in logs; caller will stop gracefully.
            return
        browser = None
        context = None
        try:
            browser = await playwright.chromium.launch(
                headless=not headful,
                args=[f"--remote-debugging-port={port}"],
            )
            context = await browser.new_context()
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.25)
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            await playwright.stop()

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        return
    finally:
        if not stop.is_set():
            # A launch/runtime exception means managed CDP is unavailable.
            ready.clear()



def main() -> int:
    parser = argparse.ArgumentParser(description="Mock managed browser provider")
    parser.add_argument("--api-host", default="127.0.0.1", help="Provider HTTP bind host")
    parser.add_argument("--api-port", type=int, default=5556, help="Provider HTTP port")
    parser.add_argument("--cdp-port", type=int, default=9222, help="CDP port")
    parser.add_argument("--headful", action="store_true", help="Keep browser window visible")
    parser.add_argument(
        "--auto-login-after",
        type=int,
        default=None,
        help="Optional seconds after which mock auth returns is_logged_in=true",
    )
    args = parser.parse_args()

    state = MockProviderState(cdp_port=args.cdp_port, auto_logged_in_after=args.auto_login_after)

    # Start the CDP browser in a background thread for API compatibility.
    cdp_thread = threading.Thread(
        target=_run_cdp_server,
        args=(args.cdp_port, args.headful, state._cdp_running, state._stop),
        name="cdp-provider",
        daemon=True,
    )
    cdp_thread.start()

    stop_requested = state._stop

    def _stop_all(*_: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGINT, _stop_all)
    try:
        signal.signal(signal.SIGTERM, _stop_all)
    except Exception:
        pass

    try:
        # Wait for CDP bootstrap so callers receive deterministic running status.
        deadline = time.time() + 20
        while not state.cdp_running and time.time() < deadline:
            time.sleep(0.1)
        if not state.cdp_running:
            # Keep running state and still expose HTTP status/health; API status checks
            # will return provider_unavailable until CDP is truly available.
            return 1

        http_thread = threading.Thread(
            target=_run_provider_http,
            args=(args.api_host, args.api_port, state),
            name="mock-provider-http",
            daemon=True,
        )
        http_thread.start()
        http_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop_requested.set()
        state.mark_cdp_stopped()
        if cdp_thread.is_alive():
            cdp_thread.join(timeout=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
