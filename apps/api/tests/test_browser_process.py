"""OS integration tests: real spawned helpers, process trees and cancellation.

The Playwright protocol is a fixture. These are not live Xiaohongshu tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from framefactory_api import benchmark_auth, benchmark_note_sources, browser_process
from framefactory_api.benchmark_accounts import BenchmarkAccountError
from framefactory_api.browser_process import BrowserProcessError, _run_owned_process

FIXTURE = Path(__file__).parent / "fixtures" / "hanging_browser_process.py"
PROFILE = "https://www.xiaohongshu.com/user/profile/" + "a" * 24
NOTE = "b" * 24


def payload() -> bytes:
    return json.dumps({"operation": "login", "arguments": {"base_url": "http://127.0.0.1:5556"},
                       "parent_pid": os.getpid()}).encode()


def wait_file(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


@pytest.mark.parametrize("stage", ["new_page", "close"])
def test_hung_cdp_call_reaps_only_owned_helper_and_driver(tmp_path, stage):
    marker = tmp_path / "started"
    driver_marker = tmp_path / "driver"
    outside_marker = tmp_path / "existing-browser"
    outside = subprocess.Popen(
        [sys.executable, str(FIXTURE), "heartbeat", str(outside_marker)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        wait_file(outside_marker)
        before = time.monotonic()
        with pytest.raises(BrowserProcessError):
            _run_owned_process(
                [sys.executable, str(FIXTURE), stage, str(marker), str(driver_marker)],
                payload(), timeout_seconds=3,
            )
        assert time.monotonic() - before < 8
        wait_file(marker)
        wait_file(driver_marker)
        driver_stopped = driver_marker.read_bytes()
        outside_before = outside_marker.read_bytes()
        time.sleep(0.2)
        assert driver_marker.read_bytes() == driver_stopped
        assert outside.poll() is None
        assert outside_marker.read_bytes() != outside_before
    finally:
        outside.kill()
        outside.wait(timeout=5)


def test_cancel_event_interrupts_hung_process_without_waiting_for_deadline(tmp_path):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    cancelled = threading.Event()
    def cancel_after_start():
        wait_file(driver)
        cancelled.set()

    canceller = threading.Thread(target=cancel_after_start, daemon=True)
    canceller.start()
    before = time.monotonic()
    try:
        with pytest.raises(BrowserProcessError) as error:
            _run_owned_process(
                [sys.executable, str(FIXTURE), "new_page", str(marker), str(driver)],
                payload(), timeout_seconds=60, cancel_event=cancelled,
            )
        assert error.value.cancelled
        assert time.monotonic() - before < 7
        wait_file(driver)
        stopped = driver.read_bytes()
        time.sleep(0.2)
        assert driver.read_bytes() == stopped
    finally:
        cancelled.set()
        canceller.join(timeout=6)


def test_driver_is_reaped_even_after_helper_exits_successfully(tmp_path):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    result = _run_owned_process(
        [sys.executable, str(FIXTURE), "exit", str(marker), str(driver)],
        payload(), timeout_seconds=10,
    )
    assert json.loads(result) == {"ok": True, "result": True}
    wait_file(driver)
    stopped = driver.read_bytes()
    time.sleep(0.2)
    assert driver.read_bytes() == stopped


def test_api_hard_crash_reaps_owned_helper_and_driver(tmp_path):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    script = """
import json, os, sys
from framefactory_api.browser_process import _run_owned_process
payload = json.dumps({'operation':'login','arguments':{'base_url':'http://127.0.0.1:5556'},
                      'parent_pid':os.getpid()}).encode()
_run_owned_process([sys.executable, sys.argv[1], 'close', sys.argv[2], sys.argv[3]],
                   payload, timeout_seconds=60)
"""
    api = subprocess.Popen(
        [sys.executable, "-c", script, str(FIXTURE), str(marker), str(driver)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        wait_file(driver, timeout=10)
        api.kill()  # No Python finally/lifespan/explicit child cleanup runs here.
        api.wait(timeout=5)
        time.sleep(0.3)
        stopped = driver.read_bytes()
        time.sleep(0.2)
        assert driver.read_bytes() == stopped
    finally:
        if api.poll() is None:
            api.kill()
            api.wait(timeout=5)


@pytest.mark.asyncio
async def test_repeated_async_cancellation_waits_for_real_process_reaping(tmp_path, monkeypatch):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    original = browser_process._run_owned_process
    finished = threading.Event()

    def fixture_command(_command, input_data, **kwargs):
        try:
            return original(
                [sys.executable, str(FIXTURE), "close", str(marker), str(driver)],
                input_data, **kwargs,
            )
        finally:
            finished.set()

    monkeypatch.setattr(browser_process, "_run_owned_process", fixture_command)
    operation = asyncio.create_task(browser_process.run_browser_operation_async(
        "login", {"base_url": "http://127.0.0.1:5556"}, timeout_seconds=60,
    ))
    await asyncio.to_thread(wait_file, driver)
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, timeout=6)
    assert finished.is_set()
    stopped = driver.read_bytes()
    await asyncio.sleep(0.2)
    assert driver.read_bytes() == stopped


@pytest.mark.asyncio
async def test_auth_shutdown_cancels_process_and_releases_guard(tmp_path, monkeypatch):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    original = browser_process._run_owned_process

    def fixture_command(_command, input_data, **kwargs):
        return original(
            [sys.executable, str(FIXTURE), "close", str(marker), str(driver)], input_data, **kwargs,
        )

    monkeypatch.setattr(browser_process, "_run_owned_process", fixture_command)
    service = benchmark_auth.BenchmarkAuthService(
        "http://127.0.0.1:5556", response_wait_seconds=0.01,
    )
    assert (await service.status()).state == "checking"
    await asyncio.to_thread(wait_file, driver)
    await asyncio.wait_for(service.close(), timeout=6)
    assert not service._operations
    assert not service._lock.locked()
    assert (await service.status()).error_code == "BENCHMARK_AUTH_STOPPED"


@pytest.mark.asyncio
async def test_note_gateway_reclaims_serial_slot_only_after_child_is_reaped(tmp_path, monkeypatch):
    marker, driver = tmp_path / "started", tmp_path / "driver"
    original = browser_process._run_owned_process
    reaped = threading.Event()
    calls = 0

    def fixture_command(_command, _input_data, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            assert reaped.is_set()
            raise BrowserProcessError()
        try:
            return original(
                [sys.executable, str(FIXTURE), "close", str(marker), str(driver)],
                payload(), **kwargs,
            )
        finally:
            reaped.set()

    monkeypatch.setattr(browser_process, "_run_owned_process", fixture_command)
    gateway = benchmark_note_sources.BenchmarkNoteSourceGateway(
        benchmark_note_sources.XiaohongshuAuthenticatedNoteProvider(
            managed_browser_base_url="http://127.0.0.1:5556",
        ),
    )
    first = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
    await asyncio.to_thread(wait_file, driver)
    second = asyncio.create_task(gateway.collect("xiaohongshu", PROFILE, NOTE))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=6)
    with pytest.raises(BenchmarkAccountError) as error:
        await second
    assert error.value.code == "BENCHMARK_PROVIDER_UNAVAILABLE"
    assert calls == 2
    assert not gateway._lock.locked()
    assert gateway._pending == 0


def test_media_cancellation_error_and_child_arguments_preserve_contract(tmp_path, monkeypatch):
    cancel_event = threading.Event()
    seen = {}

    def capture(operation, arguments, **kwargs):
        seen.update(operation=operation, arguments=arguments, **kwargs)
        raise BrowserProcessError(cancelled=True)

    monkeypatch.setattr(benchmark_note_sources, "run_browser_operation", capture)
    provider = benchmark_note_sources.XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url="http://127.0.0.1:5556",
    )
    with pytest.raises(BenchmarkAccountError) as error:
        provider.acquire_media(PROFILE, NOTE, tmp_path / "source.mp4", cancel_event=cancel_event)
    assert error.value.code == "BENCHMARK_MEDIA_CANCELLED"
    assert seen["cancel_event"] is cancel_event
    assert seen["arguments"]["destination"] == str((tmp_path / "source.mp4").resolve())
    assert seen["timeout_seconds"] == 115


def test_actual_spawn_preserves_known_error_from_same_checkout_without_pythonpath(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BenchmarkAccountError) as error:
        browser_process.run_browser_operation(
            "note", {"provider": {"managed_browser_base_url": "http://127.0.0.1:5556"},
                     "profile_url": PROFILE, "note_id": "invalid"}, timeout_seconds=5,
        )
    assert error.value.code == "BENCHMARK_NOTE_INVALID"
    assert "http" not in str(error.value)
    with pytest.raises(BrowserProcessError) as unknown:
        browser_process.run_browser_operation("private-canary", {}, timeout_seconds=5)
    assert "private-canary" not in str(unknown.value)
