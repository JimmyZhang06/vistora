"""Small dependency-free helpers shared by the release verification gates."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class GateFailure(RuntimeError):
    """A checked release invariant failed."""


class GateBlocked(RuntimeError):
    """The environment or a required product capability is not available."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GateBlocked(f"required environment variable {name} is not set")
    return value


def load_json(path: str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise GateBlocked(f"JSON fixture does not exist: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateBlocked(f"cannot load JSON fixture {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateBlocked(f"JSON fixture must contain an object: {source}")
    return value


def page_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("data")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GateFailure("expected an API page with an object-valued data array")
    return value


def changed_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    changed = deepcopy(payload)
    inputs = changed.setdefault("input", {})
    if not isinstance(inputs, dict):
        raise GateBlocked("run fixture field 'input' must be an object")
    inputs["release_gate_nonce"] = str(uuid4())
    return changed


def idempotency_key(label: str) -> str:
    return f"release-{label}-{uuid4()}"


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateFailure(
                f"HTTP {self.status} response was not valid UTF-8 JSON: {self.body[:200]!r}"
            ) from exc


class ApiClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GateBlocked("FF_RELEASE_API_URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and os.environ.get("FF_RELEASE_ALLOW_HTTP") != "1":
            raise GateBlocked(
                "release API must use HTTPS; set FF_RELEASE_ALLOW_HTTP=1 only for an isolated local gate"
            )
        self.timeout = timeout
        self.headers = {"Accept": "application/json"}
        token = os.environ.get("FF_RELEASE_API_TOKEN", "").strip()
        api_key = os.environ.get("FF_RELEASE_API_KEY", "").strip()
        workspace_id = os.environ.get("FF_RELEASE_WORKSPACE_ID", "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        if api_key:
            self.headers["X-API-Key"] = api_key
        if workspace_id:
            self.headers["X-Workspace-Id"] = workspace_id
        self._ssl_context = ssl.create_default_context()

    @classmethod
    def from_environment(cls) -> ApiClient:
        timeout = float(os.environ.get("FF_RELEASE_HTTP_TIMEOUT_SECONDS", "30"))
        return cls(require_env("FF_RELEASE_API_URL"), timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotency: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        if not path.startswith("/"):
            raise GateFailure(f"API path must start with '/': {path}")
        headers = dict(self.headers)
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                return HttpResponse(
                    response.status,
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code,
                {key.lower(): value for key, value in exc.headers.items()},
                exc.read(),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GateBlocked(f"cannot reach release API at {self.base_url}: {exc}") from exc

    def expect(
        self,
        method: str,
        path: str,
        statuses: set[int],
        *,
        payload: dict[str, Any] | None = None,
        idempotency: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        response = self.request(
            method,
            path,
            payload=payload,
            idempotency=idempotency,
            extra_headers=extra_headers,
        )
        if response.status not in statuses:
            summary = response.body[:500].decode("utf-8", errors="replace")
            if response.status in {404, 405, 501}:
                raise GateBlocked(
                    f"required API capability {method} {path} is unavailable "
                    f"(HTTP {response.status}): {summary}"
                )
            raise GateFailure(
                f"{method} {path} returned HTTP {response.status}; expected "
                f"{sorted(statuses)}: {summary}"
            )
        return response


def wait_for_run_status(
    client: ApiClient,
    run_id: str,
    accepted: set[str],
    *,
    timeout: float,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: Any = None
    while time.monotonic() < deadline:
        run = client.expect("GET", f"/v1/runs/{run_id}", {200}).json()
        if not isinstance(run, dict):
            raise GateFailure("run response must be a JSON object")
        last_status = run.get("status")
        if last_status in accepted:
            return run
        if last_status in {"failed", "cancelled"} and last_status not in accepted:
            raise GateFailure(f"run {run_id} reached unexpected terminal status {last_status}")
        time.sleep(poll_interval)
    raise GateFailure(
        f"run {run_id} did not reach {sorted(accepted)} within {timeout}s; "
        f"last status was {last_status!r}"
    )


def gate_main(function: Any) -> None:
    try:
        details = function()
    except GateBlocked as exc:
        print(json.dumps({"result": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    except GateFailure as exc:
        print(json.dumps({"result": "FAILED", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    except KeyboardInterrupt as exc:
        print(json.dumps({"result": "BLOCKED", "reason": "gate interrupted"}))
        raise SystemExit(2) from exc
    else:
        print(json.dumps({"result": "PASSED", "details": details}, ensure_ascii=False))


if __name__ == "__main__":
    print("This module is imported by release gates and is not a standalone gate.", file=sys.stderr)
    raise SystemExit(2)
