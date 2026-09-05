from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from framefactory_api import benchmark_accounts as accounts
from framefactory_api.benchmark_accounts import (
    BenchmarkAccountError,
    BenchmarkAccountGateway,
    XiaohongshuPublicProfileProvider,
    parse_public_profile_html,
)
from framefactory_api.benchmark_reports import build_benchmark_account_report
from framefactory_api.main import create_app
from framefactory_api.repository import InMemoryControlRepository

UID = "5db5be0e0000000001001e81"
URL = f"https://www.xiaohongshu.com/user/profile/{UID}"
NOTE = "111111111111111111111111"
OTHER = "222222222222222222222222"


def page(*, ids=(NOTE,), outer=False, author=UID, dom=None, kind="video"):
    cards = []
    for identity in ids:
        card = {
            "noteCard": {
                "displayTitle": "Duplicate title",
                "time": 1787972870000,
                "type": kind,
                "user": {"userId": author},
                "interactInfo": {"likedCount": "12"},
            }
        }
        if outer:
            card["id"] = identity
        else:
            card["noteCard"]["noteId"] = identity
        cards.append(card)
    state = {
        "user": {
            "userPageData": {
                "basicInfo": {
                    "nickname": "Fixture account",
                    "redId": "fixture",
                }
            },
            "notes": [cards],
        }
    }
    if dom is not None:
        state["_verified_dom_notes"] = dom
    return ("<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>").encode()


def parse(payload, **kwargs):
    return parse_public_profile_html(payload, profile_url=URL, expected_user_id=UID, **kwargs)


def test_observed_outer_id_resolves_without_note_card_id():
    snapshot = parse(page(outer=True))
    assert snapshot.notes[0].note_id == NOTE
    assert snapshot.notes[0].identity_status == "verified"
    assert snapshot.acquisition.note_identity_status == "complete"


def test_duplicate_titles_never_join_missing_identity_to_a_known_note():
    snapshot = parse(page(ids=(None, OTHER)))
    assert [n.note_id for n in snapshot.notes] == [None, OTHER]
    assert snapshot.acquisition.note_identity_status == "partial"
    assert snapshot.acquisition.unresolved_note_count == 1


def test_conflicting_structured_sources_fail_closed():
    payload = page().replace(b'"noteCard":', f'"id":"{OTHER}","noteCard":'.encode())
    with pytest.raises(BenchmarkAccountError, match="disagree") as caught:
        parse(payload)
    assert caught.value.code == "BENCHMARK_NOTE_IDENTITY_CONFLICT"


@pytest.mark.parametrize("invalid", ["A" * 24, "abc", "https://evil.test", 123])
def test_invalid_note_identity_is_a_diagnostic_not_a_model_exception(invalid):
    with pytest.raises(BenchmarkAccountError) as caught:
        parse(page(ids=(invalid,)))
    assert caught.value.code == "BENCHMARK_NOTE_IDENTITY_INVALID"
    assert str(invalid) not in str(caught.value)


def test_missing_author_is_not_verified_even_with_valid_id():
    with pytest.raises(BenchmarkAccountError) as caught:
        parse(page(author=""))
    assert caught.value.code == "BENCHMARK_SOURCE_CHANGED"


def test_dom_only_cards_have_no_fabricated_dates_or_format():
    dom = [{"profile_user_id": UID, "note_id": OTHER, "title": "Same title", "likes_display": "9"}]
    snapshot = parse(page(ids=(), dom=dom), acquisition_method="authenticated_managed_browser")
    assert snapshot.notes[0].published_at is None
    assert snapshot.notes[0].format == "unknown"
    assert snapshot.analysis.unknown_count == 1
    assert snapshot.analysis.image_count == snapshot.analysis.video_count == 0
    assert build_benchmark_account_report(snapshot).note_reports[0].published_at is None


def test_public_html_cannot_spoof_the_managed_dom_envelope():
    dom = [{"profile_user_id": UID, "note_id": OTHER}]
    snapshot = parse(page(ids=(None,), dom=dom))
    assert [n.note_id for n in snapshot.notes] == [None]


def test_incomplete_success_automatically_supplements_and_replaces_unidentified_sample():
    calls = []

    def public(*_):
        calls.append("public")
        return page(ids=(None,))

    def managed(*_):
        calls.append("managed")
        return page(ids=(OTHER, NOTE))

    provider = XiaohongshuPublicProfileProvider(fetcher=public, authenticated_fetcher=managed)
    result = provider.collect(provider.resolve(URL))
    assert calls == ["public", "managed"]
    assert [n.note_id for n in result.notes] == [OTHER, NOTE]
    assert result.acquisition.method == "authenticated_managed_browser"


@pytest.mark.parametrize(
    "code",
    [
        "BENCHMARK_AUTHENTICATION_REQUIRED",
        "BENCHMARK_SOURCE_CHANGED",
        "BENCHMARK_NOTE_INACCESSIBLE",
    ],
)
def test_partial_metadata_preserves_actionable_supplement_failure(code):
    def failed(*_):
        raise BenchmarkAccountError(code, "safe diagnostic")

    provider = XiaohongshuPublicProfileProvider(
        fetcher=lambda *_: page(ids=(None, NOTE)),
        authenticated_fetcher=failed,
    )
    snapshot = provider.collect(provider.resolve(URL))
    assert snapshot.acquisition.identity_error_code == code
    assert snapshot.acquisition.identified_note_count == 1
    assert snapshot.acquisition.unresolved_note_count == 1


def test_supplement_from_another_account_never_returns_old_success():
    provider = XiaohongshuPublicProfileProvider(
        fetcher=lambda *_: page(ids=(None,)),
        authenticated_fetcher=lambda *_: page(author="0" * 24),
    )
    with pytest.raises(BenchmarkAccountError) as caught:
        provider.collect(provider.resolve(URL))
    assert caught.value.code == "BENCHMARK_PROFILE_MISMATCH"


@pytest.mark.asyncio
async def test_missing_cache_expires_after_short_cooldown_and_refresh_is_deduplicated(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(accounts.time, "monotonic", lambda: clock[0])
    calls = []

    def fetcher(*_):
        calls.append(1)
        return page(ids=(None,)) if len(calls) == 1 else page()

    gateway = BenchmarkAccountGateway(fetcher=fetcher)
    first = await gateway.collect("xiaohongshu", URL)
    assert first.acquisition.unresolved_note_count == 1
    await gateway.collect("xiaohongshu", URL, refresh_note_identity=True)
    assert len(calls) == 1
    clock[0] += 11
    results = await asyncio.gather(
        *[gateway.collect("xiaohongshu", URL, refresh_note_identity=True) for _ in range(8)]
    )
    assert len(calls) == 2
    assert all(result.notes[0].note_id == NOTE for result in results)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_browser_serialization():
    started, release = threading.Event(), threading.Event()
    active = 0
    maximum = 0

    def fetcher(*_):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        release.wait(timeout=2)
        active -= 1
        return page()

    gateway = BenchmarkAccountGateway(fetcher=fetcher, cache_ttl_seconds=0)
    first = asyncio.create_task(gateway.collect("xiaohongshu", URL))
    await asyncio.to_thread(started.wait, 2)
    first.cancel()
    second = asyncio.create_task(gateway.collect("xiaohongshu", URL))
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert maximum == 1


def test_refresh_route_returns_new_contract_and_private_no_store():
    gateway = BenchmarkAccountGateway(fetcher=lambda *_: page(outer=True))
    with TestClient(
        create_app(repository=InMemoryControlRepository(), benchmark_account_gateway=gateway)
    ) as client:
        response = client.post(
            "/v1/benchmark-accounts/report",
            json={
                "platform": "xiaohongshu",
                "profile_url": URL + "?xsec_token=discard-me",
                "refresh_note_identity": True,
            },
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["snapshot"]["acquisition"]["discovery_version"] == "1"
    assert "discard-me" not in response.text
    assert "xsec_token" not in response.text


def test_bad_timestamps_and_unknown_format_remain_honest():
    snapshot = parse(
        page(kind="future_kind").replace(b"1787972870000", b"1e309"),
        captured_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    assert snapshot.notes[0].published_at is None
    assert snapshot.notes[0].format == "unknown"


def test_missing_author_triggers_managed_verification_instead_of_mismatch():
    calls = []

    def authenticated(*_):
        calls.append(1)
        return page()

    provider = XiaohongshuPublicProfileProvider(
        fetcher=lambda *_: page(author=""),
        authenticated_fetcher=authenticated,
    )
    assert provider.collect(provider.resolve(URL)).notes[0].note_id == NOTE
    assert calls == [1]


def test_combined_dom_and_ssr_sample_remains_bounded_without_positional_identity_assignment():
    dom = [
        {"profile_user_id": UID, "note_id": f"{index:024x}", "title": "DOM card"}
        for index in range(1, 61)
    ]
    snapshot = parse(
        page(ids=(None,) * 64, dom=dom), acquisition_method="authenticated_managed_browser"
    )
    assert len(snapshot.notes) == snapshot.analysis.sample_size == 64
    assert snapshot.acquisition.identified_note_count == 60
    assert snapshot.acquisition.unresolved_note_count == 4
    assert all(note.title == "DOM card" for note in snapshot.notes if note.note_id)
    assert [note.sample_index for note in snapshot.notes] == list(range(1, 65))


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_release_running_discovery_thread():
    started, release = threading.Event(), threading.Event()
    active, maximum = 0, 0

    def fetcher(*_):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        release.wait(timeout=2)
        active -= 1
        return page()

    gateway = BenchmarkAccountGateway(fetcher=fetcher, cache_ttl_seconds=0)
    first = asyncio.create_task(gateway.collect("xiaohongshu", URL))
    await asyncio.to_thread(started.wait, 2)
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    second = asyncio.create_task(gateway.collect("xiaohongshu", URL))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert maximum == 1


@pytest.mark.parametrize(
    "body",
    [
        {"platform": "xiaohongshu", "profile_url": URL + "?xsec_token=FIXTURE_SECRET" + "x" * 1000},
        {"platform": "xiaohongshu", "profile_url": URL, "cookie": "FIXTURE_SECRET"},
        {"platform": "xiaohongshu", "profile_url": URL, "refresh_note_identity": "FIXTURE_SECRET"},
    ],
)
def test_rejected_benchmark_requests_do_not_echo_credentials(body):
    with TestClient(create_app(repository=InMemoryControlRepository())) as client:
        response = client.post("/v1/benchmark-accounts/preview", json=body)
    assert response.status_code == 422
    assert "FIXTURE_SECRET" not in response.text
    assert "input" not in response.json()["details"]["errors"][0]
