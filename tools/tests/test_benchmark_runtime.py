from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "verify_benchmark_runtime", Path(__file__).resolve().parents[1] / "verify_benchmark_runtime.py"
)
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.fixture
def endpoint():
    state = {
        "requests": [], "routes": dict(runtime.REQUIRED_ROUTES), "cache": "private, no-store",
        "origin": "http://127.0.0.1:4173", "state": "login_required", "auth_http": 200,
        "schema_version": "1.0.0", "redirect": False,
        "preflight": True, "post_headers": "Content-Type, Idempotency-Key", "bad_status": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(self.path)
            if state["bad_status"]:
                self.request.sendall(b"dummy-private-invalid-status\r\n\r\n")
                return
            code = 200
            if self.path == "/openapi.json":
                body = {"paths": {path: {method: {}} for path, method in state["routes"].items()}}
            elif self.path == runtime.AUTH:
                code = state["auth_http"]
                body = {
                    "schema_version": state["schema_version"], "platform": "xiaohongshu", "mode": "local_browser",
                    "state": state["state"], "message": "fixture", "qr_image_data_url": None,
                    "expires_at": None, "checked_at": "2026-09-06T00:00:00Z", "retry_after_seconds": 3, "error_code": None,
                }
            else:
                body = {"schema_version": "1.0.0", "items": [], "next_cursor": None}
            self.send_response(302 if state["redirect"] else code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", state["cache"])
            self.send_header("Access-Control-Allow-Origin", state["origin"])
            if state["redirect"]:
                self.send_header("Location", "http://example.invalid/private")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_OPTIONS(self):
            state["requests"].append("OPTIONS:" + self.path)
            self.send_response(200 if state["preflight"] else 501)
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:4173")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", state["post_headers"])
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("connection", ["login_required", "authorized", "provider_unavailable"])
def test_real_http_checks_contract_cors_history_without_starting_login(endpoint, connection):
    url, state = endpoint
    state["state"] = connection
    result = runtime.verify(url, state["origin"])
    assert result["compatible"] is True
    assert result["connection_state"] == connection
    assert result["live_login_verified"] is (connection == "authorized")
    assert result["scan_flow_verified"] is False
    assert state["requests"] == [
        "/openapi.json", "OPTIONS:" + runtime.AUTH,
        "OPTIONS:/v1/benchmark-auth/xiaohongshu/qrcode", "OPTIONS:" + runtime.HISTORY,
        runtime.AUTH, runtime.HISTORY + "?limit=1",
    ]


def test_healthy_old_api_missing_login_route_is_not_accepted(endpoint):
    url, state = endpoint
    del state["routes"][runtime.AUTH]
    with pytest.raises(runtime.RuntimeCheckError, match="BENCHMARK_API_VERSION_MISMATCH"):
        runtime.verify(url, state["origin"])
    assert state["requests"] == ["/openapi.json"]


@pytest.mark.parametrize(("key", "value", "code"), [
    ("auth_http", 404, "BENCHMARK_API_VERSION_MISMATCH"),
    ("auth_http", 403, "BENCHMARK_RUNTIME_HTTP_403"),
    ("state", "not_configured", "BENCHMARK_AUTH_NOT_CONFIGURED"),
    ("schema_version", "2.0.0", "BENCHMARK_RUNTIME_CONTRACT_MISMATCH"),
    ("cache", "public", "BENCHMARK_RUNTIME_CACHE_UNSAFE"),
    ("origin", "http://localhost:9999", "BENCHMARK_RUNTIME_ORIGIN_MISMATCH"),
    ("redirect", True, "BENCHMARK_RUNTIME_REDIRECT_REJECTED"),
    ("preflight", False, "BENCHMARK_RUNTIME_PREFLIGHT_REJECTED"),
    ("post_headers", "Content-Type", "BENCHMARK_RUNTIME_PREFLIGHT_REJECTED"),
    ("bad_status", True, "BENCHMARK_RUNTIME_INVALID_RESPONSE"),
])
def test_incompatible_runtime_fails_with_safe_diagnostic(endpoint, key, value, code):
    url, state = endpoint
    state[key] = value
    with pytest.raises(runtime.RuntimeCheckError, match=code):
        runtime.verify(url, "http://127.0.0.1:4173")


@pytest.mark.parametrize("url", [
    "http://example.invalid:8210", "http://127.0.0.1:8210/private",
    "http://dummy:dummy@localhost:8210", "http://127.0.0.1:8210?private=1",
    "http://127.0.0.1:invalid", "file:///private",
])
def test_remote_or_credential_bearing_origin_is_rejected_before_network(url):
    with pytest.raises(runtime.RuntimeCheckError, match="BENCHMARK_RUNTIME_LOCAL_ORIGIN_REQUIRED"):
        runtime.verify(url, "http://127.0.0.1:4173")


def test_malformed_wire_error_is_sanitized_on_stderr_for_launcher(endpoint, monkeypatch, capsys):
    url, state = endpoint
    state["bad_status"] = True
    monkeypatch.setattr(runtime.sys, "argv", ["verify", "--api-url", url, "--web-origin", state["origin"]])
    assert runtime.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dummy-private" not in captured.err
    assert json.loads(captured.err) == {"compatible": False, "code": "BENCHMARK_RUNTIME_INVALID_RESPONSE"}
