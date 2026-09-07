"""Login bridge boundary tests; fixtures do not claim a real platform login."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from framefactory_api import benchmark_auth
from framefactory_api.benchmark_auth import BenchmarkAuthService, _fresh_platform_login
from framefactory_api.context import WorkspaceContext
from framefactory_api.contracts import ContractValidator
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository
from framefactory_api.settings import Settings

ROOT = "/v1/benchmark-auth/xiaohongshu"
PNG = (
    "data:image/png;base64,"
    + base64.b64encode(
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 128, 128)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 4,
    ).decode()
)


def qr_task(**updates):
    task = {
        "task_id": "private-provider-task",
        "kind": "get_login_qrcode",
        "target_driver": "managed",
        "status": "succeeded",
        "result": {
            "is_logged_in": False,
            "image_data_url": PNG,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=90)).isoformat(),
            "cookie": "private-cookie",
            "token": "private-token",
        },
    }
    task.update(updates)
    return task


class FixtureProvider:
    def __init__(self, logged_in=False):
        self.logged_in = logged_in
        self.calls = []
        self.checks = 0
        self.task = qr_task()
        self.failure = None

    async def check(self):
        self.checks += 1
        return self.logged_in

    async def request(self, method, path, body):
        self.calls.append((method, path, body))
        if self.failure:
            raise self.failure
        return self.task

    def service(self, **kwargs):
        return BenchmarkAuthService(
            "http://127.0.0.1:5556", transport=self.request, platform_check=self.check, **kwargs
        )


class Context:
    def __init__(self):
        settings = Settings()
        self.context = WorkspaceContext(
            user_id=settings.default_user_id,
            workspace_id=settings.default_workspace_id,
            workspace_name="Local fixture",
        )

    def resolve(self, requested_workspace_id=None):
        return self.context


def app(service=None, context=None, settings=None):
    return create_app(
        settings=settings or Settings(),
        repository=InMemoryControlRepository(),
        benchmark_auth_service=service,
        context_provider=context,
    )


def client(application, *, peer="127.0.0.1", host="127.0.0.1"):
    return TestClient(application, client=(peer, 51000), base_url=f"http://{host}:8210")


def test_unconfigured_has_honest_state_no_store_and_no_sniff():
    with client(app()) as session:
        for response in (session.get(ROOT + "/status"), session.post(ROOT + "/qrcode", json={})):
            assert response.status_code == 200
            assert response.json()["state"] == "not_configured"
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_projected_login_states_match_canonical_schema():
    class AuthValidator(ContractValidator):
        RESOURCE_SCHEMAS: ClassVar[dict[str, str]] = {"auth": "benchmark-auth-state.schema.json"}

    validator = AuthValidator()
    provider = FixtureProvider()
    service = provider.service()
    for result in (await service.status(), await service.status(start=True)):
        validator.validate("auth", result.model_dump(mode="json"))
    provider.logged_in = True
    service._last_check = 0
    validator.validate("auth", (await service.status()).model_dump(mode="json"))
    await service.close()
    assert service._qr is None
    assert (await service.status(start=True)).error_code == "BENCHMARK_AUTH_STOPPED"


@pytest.mark.parametrize(
    ("peer", "host", "headers"),
    [
        ("198.51.100.9", "127.0.0.1", {}),
        ("127.0.0.1", "evil.example", {}),
        ("127.0.0.1", "127.0.0.1", {"Origin": "https://evil.example"}),
        ("127.0.0.1", "127.0.0.1", {"Origin": "null"}),
        ("127.0.0.1", "127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
        ("127.0.0.1", "127.0.0.1", {"Sec-Fetch-Site": "cross-site"}),
    ],
)
def test_remote_rebinding_and_cross_site_never_reach_provider(peer, host, headers):
    provider = FixtureProvider()
    with client(app(provider.service()), peer=peer, host=host) as session:
        for response in (
            session.get(ROOT + "/status", headers=headers),
            session.post(ROOT + "/qrcode", headers=headers, json={}),
        ):
            assert response.status_code == 403
            assert response.headers["cache-control"] == "private, no-store"
    assert provider.checks == 0
    assert not provider.calls


def test_configured_remote_origin_still_cannot_manage_local_login():
    settings = replace(Settings(), cors_allow_origins=("https://trusted.example",))
    with client(app(settings=settings)) as session:
        response = session.post(
            ROOT + "/qrcode", json={}, headers={"Origin": "https://trusted.example"}
        )
        assert response.status_code == 403


def test_local_origin_is_supported_but_readonly_and_foreign_context_are_denied():
    provider, context = FixtureProvider(), Context()
    with client(app(provider.service(), context)) as session:
        assert (
            session.get(ROOT + "/status", headers={"Origin": "http://localhost:3000"}).status_code
            == 200
        )
        original = context.context
        for changed in (
            replace(original, permissions=frozenset({"assets:read"})),
            replace(original, user_id=uuid4()),
            replace(original, workspace_id=uuid4()),
        ):
            context.context = changed
            assert session.get(ROOT + "/status").status_code == 403
            assert session.post(ROOT + "/qrcode", json={}).status_code == 403
    assert provider.checks == 1
    assert not provider.calls


def test_request_secrets_are_rejected_and_redacted():
    with client(app()) as session:
        response = session.post(ROOT + "/qrcode", json={"cookie": "private-cookie"})
        assert response.status_code == 422
        assert "private-cookie" not in response.text
        assert (
            session.post(
                ROOT + "/qrcode", json={}, headers={"Idempotency-Key": "a" * 129}
            ).status_code
            == 422
        )
        assert session.post(
            ROOT + "/qrcode", content="{}", headers={"Content-Type": "text/plain"}
        ).status_code in {415, 422}


@pytest.mark.asyncio
async def test_status_only_checks_platform_and_never_initializes_qr():
    provider = FixtureProvider()
    service = provider.service()
    for _ in range(5):
        assert (await service.status()).state == "login_required"
    assert provider.checks == 1
    assert not provider.calls


@pytest.mark.asyncio
async def test_existing_login_does_not_create_or_reset_qr():
    provider = FixtureProvider(logged_in=True)
    result = await provider.service().status(start=True)
    assert result.state == "authorized"
    assert result.qr_image_data_url is None
    assert not provider.calls


@pytest.mark.asyncio
async def test_one_qr_is_reused_and_only_sanitized_projection_leaves_api():
    provider = FixtureProvider()
    service = provider.service()
    first = await service.status(start=True)
    second = await service.status(start=True)
    assert first.state == second.state == "awaiting_scan"
    assert first.qr_image_data_url == PNG
    assert len(provider.calls) == 1
    wire = first.model_dump_json()
    assert not any(
        secret in wire for secret in ("private-provider-task", "private-cookie", "private-token")
    )
    provider.logged_in = True
    service._last_check = 0
    authorized = await service.status()
    assert authorized.state == "authorized"
    assert authorized.qr_image_data_url is None
    assert service._qr is None


@pytest.mark.asyncio
async def test_pending_qr_reuses_post_to_consume_and_does_not_trust_provider_login_claim():
    provider = FixtureProvider()

    async def progressing_request(method, path, body):
        provider.calls.append((method, path, body))
        if len(provider.calls) == 1:
            return qr_task(status="running", result=None)
        return qr_task(result={"is_logged_in": True})

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556", transport=progressing_request, platform_check=provider.check
    )
    assert (await service.status(start=True)).state == "checking"
    assert [call[0] for call in provider.calls] == ["POST", "POST"]
    assert len({call[2]["request_id"] for call in provider.calls}) == 1
    assert (await service.status()).state == "login_required"


@pytest.mark.asyncio
async def test_provider_login_claim_requires_platform_verification_before_authorized():
    provider = FixtureProvider()

    async def two_step_request(method, path, body):
        provider.calls.append((method, path, body))
        if len(provider.calls) == 1:
            return qr_task(result={"is_logged_in": True})
        return qr_task(result={"is_logged_in": False})

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556", transport=two_step_request, platform_check=provider.check
    )
    assert (await service.status(start=True)).state == "checking"
    assert (await service.status()).state == "login_required"
    provider.logged_in = True
    service._last_check = 0
    assert (await service.status()).state == "authorized"


@pytest.mark.asyncio
async def test_pending_timeout_does_not_duplicate_uncertain_qr_operation():
    provider = FixtureProvider()
    provider.task = qr_task(status="running", result=None)
    service = provider.service(qr_operation_seconds=0)
    assert (await service.status(start=True)).error_code == "BENCHMARK_AUTH_TASK_TIMEOUT"
    assert (await service.status()).error_code == "BENCHMARK_AUTH_RETRY_REQUIRED"
    service._last_auth_start = 0
    result = await service.status(start=True)
    assert result.error_code == "BENCHMARK_AUTH_TASK_TIMEOUT"
    assert len({call[2]["request_id"] for call in provider.calls}) == 1


@pytest.mark.asyncio
async def test_uncertain_transport_failure_reuses_provider_key_on_explicit_retry():
    provider = FixtureProvider()
    provider.failure = RuntimeError("http://user:private-token@host/secret private-cookie")
    service = provider.service()
    result = await service.status(start=True)
    assert result.state == "provider_unavailable"
    assert "private-" not in result.model_dump_json()
    assert (await service.status()).error_code == "BENCHMARK_AUTH_RETRY_REQUIRED"
    first_key = provider.calls[0][2]["request_id"]
    provider.failure = None
    service._last_qr_start = 0
    service._last_auth_start = 0
    assert (await service.status(start=True)).state == "awaiting_scan"
    assert provider.calls[-1][2]["request_id"] == first_key


@pytest.mark.asyncio
async def test_expiry_requires_explicit_restart_and_is_capped():
    provider = FixtureProvider()
    provider.task["result"]["expires_at"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    service = provider.service()
    result = await service.status(start=True)
    assert result.expires_at <= datetime.now(UTC) + timedelta(seconds=120)
    service._expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = await service.status()
    assert result.state == "expired"
    assert result.qr_image_data_url is None
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    [
        "https://evil.example/qr.png",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,cHJpdmF0ZS10b2tlbg==",
        PNG + "!",
        "x" * 512_001,
    ],
    ids=["remote", "svg", "fake-png", "invalid-base64", "oversized"],
)
async def test_untrusted_qr_images_are_never_forwarded(image):
    provider = FixtureProvider()
    provider.task["result"]["image_data_url"] = image
    result = await provider.service().status(start=True)
    assert result.error_code == "BENCHMARK_AUTH_QR_INVALID"
    assert result.qr_image_data_url is None


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_operation():
    gate = asyncio.Event()
    provider = FixtureProvider()

    async def slow_check():
        await gate.wait()
        return False

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556", transport=provider.request, platform_check=slow_check
    )
    first = asyncio.create_task(service.status(start=True))
    await asyncio.sleep(0)
    assert (await service.status(start=True)).state == "checking"
    assert not provider.calls
    gate.set()
    assert (await first).state == "awaiting_scan"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_http_caller_keeps_guard_until_browser_operation_finishes():
    gate = asyncio.Event()
    provider = FixtureProvider()

    async def slow_check():
        await gate.wait()
        return False

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556", transport=provider.request, platform_check=slow_check
    )
    caller = asyncio.create_task(service.status(start=True))
    for _ in range(10):
        if service._operations:
            break
        await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert service._lock.locked()
    assert (await service.status(start=True)).state == "checking"
    gate.set()
    await asyncio.gather(*service._operations)
    await asyncio.sleep(0)
    assert not service._lock.locked()
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_post_completes_slow_qr_and_consumes_without_get_side_effects():
    provider = FixtureProvider()
    consumed = False

    async def slow_request(method, path, body):
        nonlocal consumed
        provider.calls.append((method, path, body))
        if len(provider.calls) == 1:
            await asyncio.sleep(5.05)
            return qr_task(status="running", result=None)
        consumed = True  # Provider POST delivery clears its stored QR image.
        return qr_task()

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556",
        transport=slow_request,
        platform_check=provider.check,
        response_wait_seconds=0.02,
    )
    assert (await service.status(start=True)).state == "checking"
    assert (await service.status()).state == "checking"
    assert len(provider.calls) == 1
    await asyncio.gather(*service._operations)
    await asyncio.sleep(0)
    result = await service.status()
    assert result.state == "awaiting_scan"
    assert consumed
    assert [call[0] for call in provider.calls] == ["POST", "POST"]
    assert len({call[2]["request_id"] for call in provider.calls}) == 1


@pytest.mark.asyncio
async def test_indefinite_platform_initialize_state_stays_checking():
    checks = 0

    async def never_ready():
        nonlocal checks
        checks += 1
        return None

    service = BenchmarkAuthService(
        "http://127.0.0.1:5556",
        transport=lambda *_args, **_kwargs: asyncio.sleep(
            0, result={"unexpected": "provider_called"},
        ),
        platform_check=never_ready,
    )
    assert (await service.status()).state == "checking"
    assert (await service.status(start=False)).state == "checking"
    assert checks == 1


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:5556",
        "http://localhost:5556",
        "http://198.51.100.2:5556",
        "http://user:pass@127.0.0.1:5556",
        "http://127.0.0.1:5556/path",
    ],
)
def test_provider_origin_cannot_be_remote_or_carry_credentials(base_url):
    with pytest.raises(ValueError):
        BenchmarkAuthService(base_url)


@pytest.mark.parametrize(
    ("url", "state", "expected"),
    [
        ("https://www.xiaohongshu.com/login?redirectPath=explore", True, False),
        ("https://www.xiaohongshu.com/explore", False, False),
        ("https://www.xiaohongshu.com/explore", True, True),
        ("https://www.xiaohongshu.com/explore", None, None),
        ("https://www.xiaohongshu.com/website-login/error", None, "restricted"),
        ("https://www.xiaohongshu.com/explore", "redirect-restricted", "restricted"),
        ("https://evil.example/login", True, None),
    ],
)
def test_fresh_platform_check_uses_http_without_changing_tabs(monkeypatch, url, state, expected):
    calls = []
    viewer = {"guest": not state, "userId": "2" * 24} if type(state) is bool else {}
    payload = ('<script>window.__INITIAL_STATE__=' + json.dumps({
        "user": {"userInfo": viewer, "userPageData": {"basicInfo": {"userId": "1" * 24}}},
    }) + '</script>').encode()
    response = SimpleNamespace(
        url=url,
        status=302 if state == "redirect-restricted" else 200,
        headers={"location": "/website-login/captcha"},
        body=lambda: payload,
        dispose=lambda: calls.append("response-disposed"),
    )

    def get(target, **kwargs):
        assert kwargs["max_redirects"] == 0
        assert kwargs["timeout"] == 10_000
        assert kwargs["headers"]["Cache-Control"] == "no-cache"
        calls.append(target)
        return response

    browser = SimpleNamespace(
        contexts=[SimpleNamespace(request=SimpleNamespace(get=get))],
        close=lambda: calls.append("disconnected"),
    )

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(
                chromium=SimpleNamespace(
                    connect_over_cdp=lambda endpoint, **kwargs: browser,
                )
            )

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    monkeypatch.setattr(
        benchmark_auth,
        "_provider_request",
        lambda *args: {
            "state": "running",
            "cdp_port": 9222,
            "cookie_present": True,
        },
    )
    if expected == "restricted":
        from framefactory_api.benchmark_accounts import BenchmarkAccountError

        with pytest.raises(BenchmarkAccountError) as error:
            _fresh_platform_login("http://127.0.0.1:5556")
        assert error.value.code == "BENCHMARK_AUTH_PLATFORM_RESTRICTED"
    elif expected is None and url.startswith("https://evil.example"):
        with pytest.raises(ValueError):
            _fresh_platform_login("http://127.0.0.1:5556")
    else:
        assert _fresh_platform_login("http://127.0.0.1:5556") is expected
    assert "https://www.xiaohongshu.com/explore" in calls
    assert calls[-2:] == ["response-disposed", "disconnected"]


def test_delayed_platform_restriction_crosses_browser_process_boundary(monkeypatch):
    from framefactory_api.benchmark_accounts import BenchmarkAccountError
    from framefactory_api.browser_process import run_browser_operation

    # The helper serializes only a known error code, never platform response text.
    monkeypatch.setattr(
        "framefactory_api.browser_process._run_owned_process",
        lambda *args, **kwargs: b'{"ok":false,"code":"BENCHMARK_AUTH_PLATFORM_RESTRICTED"}',
    )
    with pytest.raises(BenchmarkAccountError, match="browser could not complete") as error:
        run_browser_operation("login", {}, timeout_seconds=1)
    assert error.value.code == "BENCHMARK_AUTH_PLATFORM_RESTRICTED"


@pytest.mark.asyncio
async def test_explicit_login_queued_during_passive_check_is_not_lost():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def check():
        entered.set()
        await release.wait()
        return False

    async def transport(method, path, body):
        calls.append(path)
        return qr_task()

    service = BenchmarkAuthService("http://127.0.0.1:5556", transport=transport,
                                   platform_check=check, response_wait_seconds=0.001)
    try:
        assert (await service.status()).state == "checking"
        await entered.wait()
        assert (await service.status(start=True)).state == "checking"
        release.set()
        await asyncio.gather(*tuple(service._operations))
        result = await service.status()
        assert result.state == "awaiting_scan"
        assert len(calls) == 1
    finally:
        await service.close()
