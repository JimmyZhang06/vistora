"""Terminate a CDP client and its driver at a real process boundary.

Only newly spawned helper descendants belong to this process tree. The browser
is an existing, independently owned CDP server and is never attached or killed.
Inputs/results use private pipes, not arguments, files or provider logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .owned_process import WindowsProcessJob

_MAX_RESULT_BYTES = 4 * 1024 * 1024
_POLL_SECONDS = 0.1


class BrowserProcessError(RuntimeError):
    def __init__(self, *, cancelled: bool = False) -> None:
        super().__init__("The isolated browser operation was cancelled or unavailable")
        self.cancelled = cancelled


def _stop_tree(process: subprocess.Popen[bytes], job: WindowsProcessJob | None) -> None:
    if job is not None:
        job.close()  # The driver is included even if its Python parent already exited.
    elif os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _run_owned_process(
    command: Sequence[str], payload: bytes, *, timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Synchronous supervisor; every wait, including final reaping, is bounded."""
    deadline = time.monotonic() + timeout_seconds
    if cancel_event is not None and cancel_event.is_set():
        raise BrowserProcessError(cancelled=True)
    job = WindowsProcessJob() if os.name == "nt" else None
    process = None
    try:
        environment = dict(os.environ)
        # An editable installation may point to a different checkout. Spawn from
        # the same package directory as this API, independent of cwd/pytest sys.path.
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(Path(__file__).resolve().parent.parent), environment.get("PYTHONPATH"),
        )))
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=environment,
        )
        if job is not None:
            # The child blocks on stdin until its entire future tree has an owner.
            job.attach(process.pid)
        pending_input: bytes | None = payload
        while True:
            cancelled = cancel_event is not None and cancel_event.is_set()
            remaining = deadline - time.monotonic()
            if cancelled or remaining <= 0:
                raise BrowserProcessError(cancelled=cancelled)
            try:
                stdout, _ = process.communicate(
                    input=pending_input, timeout=min(_POLL_SECONDS, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        # Only our trusted helper writes this pipe. _main checks the serialized
        # size before its sole write; provider output is redirected to devnull.
        if process.returncode or len(stdout) > _MAX_RESULT_BYTES:
            raise BrowserProcessError()
        return stdout
    except (OSError, subprocess.SubprocessError):
        raise BrowserProcessError() from None
    finally:
        if process is not None:
            try:
                _stop_tree(process, job)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
        elif job is not None:
            job.close()


def run_browser_operation(
    operation: str, arguments: dict[str, Any], *, timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> Any:
    payload = json.dumps({
        "operation": operation, "arguments": arguments, "parent_pid": os.getpid(),
    }).encode()
    stdout = _run_owned_process(
        [sys.executable, "-m", "framefactory_api.browser_process"], payload,
        timeout_seconds=timeout_seconds, cancel_event=cancel_event,
    )
    try:
        envelope = json.loads(stdout)
        if envelope.get("ok") is True:
            return envelope["result"]
        code = envelope.get("code")
        if isinstance(code, str) and code.startswith("BENCHMARK_"):
            from .benchmark_accounts import BenchmarkAccountError

            raise BenchmarkAccountError(
                code, "The browser could not complete the requested collection",
                retryable=envelope.get("retryable") is True,
            )
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    raise BrowserProcessError()


async def run_browser_operation_async(
    operation: str, arguments: dict[str, Any], *, timeout_seconds: float,
) -> Any:
    cancel_event = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        run_browser_operation, operation, arguments,
        timeout_seconds=timeout_seconds, cancel_event=cancel_event,
    ))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            cancel_event.set()
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _dispatch(operation: str, arguments: dict[str, Any]) -> Any:
    if operation == "login":
        from .benchmark_auth import _fresh_platform_login

        return _fresh_platform_login(**arguments)
    if operation == "provider_request":
        from .benchmark_auth import _provider_request

        return _provider_request(**arguments)
    if operation == "profile":
        from .benchmark_accounts import _fetch_managed_profile

        return _fetch_managed_profile(**arguments).decode("utf-8")
    if operation == "note":
        from pathlib import Path

        from .benchmark_note_sources import XiaohongshuAuthenticatedNoteProvider

        provider = XiaohongshuAuthenticatedNoteProvider(**arguments["provider"])
        destination = arguments.get("destination")
        result = provider._collect(
            arguments["profile_url"], arguments["note_id"],
            destination=Path(destination) if destination is not None else None,
        )
        return result if isinstance(result, dict) else result.model_dump(mode="json")
    raise ValueError("unsupported isolated operation")


def _watch_parent(parent_pid: int) -> None:
    # Windows Job Object ownership handles API crashes. On POSIX, a crashed API
    # reparents its helper; kill this new session, including the Playwright driver.
    while True:
        time.sleep(_POLL_SECONDS)
        if os.getppid() != parent_pid:
            os.killpg(os.getpgrp(), signal.SIGKILL)


def _main() -> None:
    try:
        payload = sys.stdin.buffer.read(16_385)
        if len(payload) > 16_384:
            raise ValueError("oversized isolated request")
        request = json.loads(payload)
        if os.name != "nt":
            parent_pid = request["parent_pid"]
            threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True).start()
        with (
            open(os.devnull, "w", encoding="utf-8") as null_output,
            contextlib.redirect_stdout(null_output), contextlib.redirect_stderr(null_output),
        ):
            result = _dispatch(request["operation"], request["arguments"])
        envelope = {"ok": True, "result": result}
    except Exception as exc:
        # Never export exception text: it can contain signed URLs or browser state.
        envelope = {"ok": False, "code": getattr(exc, "code", None),
                    "retryable": getattr(exc, "retryable", False) is True}
    output = json.dumps(envelope, ensure_ascii=True).encode()
    if len(output) > _MAX_RESULT_BYTES:
        output = b'{"ok":false}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    _main()
