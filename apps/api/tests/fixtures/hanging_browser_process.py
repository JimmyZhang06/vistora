"""Test-only Playwright fixture in a REAL isolated process and driver tree."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace


def heartbeat(path: str) -> None:
    with open(path, "ab", buffering=0) as stream:
        while True:
            stream.write(b".")
            time.sleep(0.02)


def main() -> None:
    from framefactory_api import benchmark_auth, browser_process

    stage, marker, driver_marker = sys.argv[1:]

    def hang() -> None:
        Path(marker).write_text("started", encoding="utf-8")
        while True:
            time.sleep(1)

    def request(*_args, **_kwargs):
        # Mimic Playwright's newly spawned Node driver in this helper's tree.
        subprocess.Popen(
            [sys.executable, __file__, "heartbeat", driver_marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if stage == "new_page":
            hang()
        if stage == "exit":
            deadline = time.monotonic() + 5
            while not Path(driver_marker).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            Path(marker).write_text("started", encoding="utf-8")
        return SimpleNamespace(
            url="https://www.xiaohongshu.com/explore", status=200, headers={},
            body=lambda: (b'<script>window.__INITIAL_STATE__={"user":{"userInfo":'
                          b'{"guest":false,"userId":"222222222222222222222222"}}}</script>'),
            dispose=hang if stage == "close" else lambda: None,
        )

    browser = SimpleNamespace(
        contexts=[SimpleNamespace(request=SimpleNamespace(get=request))], close=lambda: None,
    )

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(
                chromium=SimpleNamespace(connect_over_cdp=lambda *_a, **_k: browser),
            )

        def __exit__(self, *_):
            return None

    import playwright.sync_api

    playwright.sync_api.sync_playwright = Playwright
    benchmark_auth._provider_request = lambda *_: {"state": "running", "cdp_port": 9222}
    browser_process._main()


if __name__ == "__main__":
    if sys.argv[1] == "heartbeat":
        heartbeat(sys.argv[2])
    else:
        main()
