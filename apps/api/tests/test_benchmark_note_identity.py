from __future__ import annotations

import json

import pytest

from framefactory_api.benchmark_accounts import BenchmarkAccountError
from framefactory_api.benchmark_note_identity import (
    NoteIdentityError,
    click_verified_profile_note,
    discover_profile_note_links,
    merge_verified_cards,
    parse_profile_note_link,
    reconcile_note_identity,
    resolve_structured_note_id,
)
from framefactory_api.benchmark_note_sources import (
    _VIDEO_PROBE_SCRIPT,
    XiaohongshuAuthenticatedNoteProvider,
)

PROFILE = "a" * 24
NOTE = "b" * 24
OTHER = "c" * 24
PROFILE_URL = f"https://www.xiaohongshu.com/user/profile/{PROFILE}"


def card(**changes: object) -> dict:
    return {"noteCard": {"noteId": NOTE, "user": {"userId": PROFILE}, **changes}}


def test_structured_id_paths_require_same_card_author_and_agreement() -> None:
    assert resolve_structured_note_id(card(), PROFILE) == NOTE
    assert resolve_structured_note_id({**card(noteId=None), "id": NOTE}, PROFILE) == NOTE
    assert resolve_structured_note_id({**card(), "id": NOTE}, PROFILE) == NOTE
    assert resolve_structured_note_id(card(noteId=None), PROFILE) is None
    assert resolve_structured_note_id(card(user={}), PROFILE) is None
    with pytest.raises(NoteIdentityError) as error:
        resolve_structured_note_id({**card(), "id": OTHER}, PROFILE)
    assert error.value.code == "BENCHMARK_NOTE_IDENTITY_CONFLICT"


@pytest.mark.parametrize("value", ["ABCDEF" * 4, "b" * 23, "b" * 25, " b" * 12, 123, True])
def test_malformed_structured_ids_fail_without_echoing_source(value: object) -> None:
    with pytest.raises(NoteIdentityError) as error:
        resolve_structured_note_id(card(noteId=value), PROFILE)
    assert error.value.code == "BENCHMARK_NOTE_IDENTITY_INVALID"
    assert str(value) not in str(error.value)


def test_author_conflict_is_not_repaired_by_a_matching_note_id() -> None:
    with pytest.raises(NoteIdentityError) as error:
        resolve_structured_note_id(card(user={"userId": OTHER}), PROFILE)
    assert error.value.code == "BENCHMARK_PROFILE_MISMATCH"


def test_profile_link_token_is_used_only_to_resolve_canonical_identity() -> None:
    relative = f"/user/profile/{PROFILE}/{NOTE}"
    assert parse_profile_note_link(relative, PROFILE) == NOTE
    assert parse_profile_note_link(f"{PROFILE_URL}/{NOTE}?xsec_token=secret", PROFILE) == NOTE
    assert parse_profile_note_link(f"/explore/{NOTE}?xsec_token=secret", PROFILE) is None
    assert reconcile_note_identity(None, NOTE, NOTE) == NOTE
    with pytest.raises(NoteIdentityError, match="disagree"):
        reconcile_note_identity(NOTE, OTHER)


@pytest.mark.parametrize(
    "prefix",
    [
        "http://www.xiaohongshu.com",
        "https://www.xiaohongshu.com.attacker.invalid",
        "https://evil@www.xiaohongshu.com",
        "https://www.xiaohongshu.com:443",
        "https://www.xiaohongshu.com:bad",
    ],
)
def test_untrusted_card_sources_fail_closed(prefix: str) -> None:
    with pytest.raises(NoteIdentityError) as error:
        parse_profile_note_link(
            f"{prefix}/user/profile/{PROFILE}/{NOTE}?xsec_token=never-echo", PROFILE
        )
    assert error.value.code == "BENCHMARK_NOTE_IDENTITY_INVALID"
    assert "never-echo" not in str(error.value)


def test_other_author_and_fragment_links_are_not_accepted() -> None:
    with pytest.raises(NoteIdentityError) as error:
        parse_profile_note_link(f"/user/profile/{OTHER}/{NOTE}", PROFILE)
    assert error.value.code == "BENCHMARK_PROFILE_MISMATCH"
    with pytest.raises(NoteIdentityError):
        parse_profile_note_link(f"{PROFILE_URL}/{NOTE}#fake", PROFILE)


def test_merge_never_uses_duplicate_titles_or_sample_index_as_identity() -> None:
    first = {"profile_user_id": PROFILE, "note_id": NOTE, "title": "same", "sample_index": 1}
    second = {"profile_user_id": PROFILE, "note_id": OTHER, "title": "same", "sample_index": 1}
    unidentified = {"profile_user_id": PROFILE, "note_id": None, "title": "same"}
    merged = merge_verified_cards(PROFILE, [first, unidentified], [second])
    assert [entry["note_id"] for entry in merged] == [NOTE, OTHER]
    assert all("sample_index" not in entry for entry in merged)
    with pytest.raises(NoteIdentityError):
        merge_verified_cards(PROFILE, [{**second, "profile_user_id": OTHER}])


def test_merge_allows_identity_refresh_but_discards_tokens_and_inferred_type() -> None:
    initial = {"profile_user_id": PROFILE, "note_id": NOTE, "title": "before"}
    refreshed = {
        **initial,
        "title": "after",
        "href": "?xsec_token=never-export",
        "token": "secret",
        "format": "video",
        "published_at": "2000-01-01",
        "likes_display": "10万+",
    }
    merged = merge_verified_cards(PROFILE, [initial], [refreshed])
    assert len(merged) == 1
    assert merged[0]["title"] == "after"
    assert merged[0]["format"] == "unknown" and merged[0]["published_at"] is None
    assert "never-export" not in json.dumps(merged) and "token" not in json.dumps(merged)


class PageResult:
    def __init__(self, result: object):
        self.result = result

    def evaluate(self, script: str, argument: object):
        assert "section.note-item" in script
        assert "link.click()" in script
        assert "xsec_token" not in script
        assert argument["profileUserId"] == PROFILE
        return self.result


def test_discovery_boundary_is_bounded_allowlisted_and_handles_partial_identity() -> None:
    page = PageResult(
        {
            "cards": [
                {"profile_user_id": PROFILE, "note_id": NOTE, "title": "known", "href": "secret"},
                {"profile_user_id": PROFILE, "note_id": None, "title": "unresolved"},
            ]
        }
    )
    result = discover_profile_note_links(page, PROFILE)
    assert len(result) == 1 and result[0]["note_id"] == NOTE
    assert "secret" not in json.dumps(result)
    with pytest.raises(NoteIdentityError):
        discover_profile_note_links(
            PageResult(
                {
                    "cards": [
                        {"profile_user_id": PROFILE, "note_id": f"{index:024x}"}
                        for index in range(65)
                    ]
                }
            ),
            PROFILE,
        )


def test_discovery_errors_and_click_result_never_export_provider_strings() -> None:
    assert click_verified_profile_note(PageResult({"clicked": True}), PROFILE, NOTE)
    assert not click_verified_profile_note(PageResult({"clicked": False}), PROFILE, NOTE)
    with pytest.raises(NoteIdentityError) as error:
        discover_profile_note_links(PageResult({"error": "token-secret"}), PROFILE)
    assert error.value.code == "BENCHMARK_SOURCE_CHANGED"
    assert "token-secret" not in str(error.value)


class DetailIdentityPage:
    def __init__(self, identity: object):
        self.identity = identity

    def evaluate(self, script: str, note_id: str):
        assert "noteDetailMap" in script
        assert note_id == NOTE
        return self.identity

    def locator(self, selector: str):
        raise AssertionError("Do not read detail content until author and note have verified")


@pytest.mark.parametrize(
    "identity,code",
    [
        (None, "BENCHMARK_NOTE_FIELDS_INSUFFICIENT"),
        ({"note_id": NOTE}, "BENCHMARK_NOTE_FIELDS_INSUFFICIENT"),
        ({"note_id": NOTE, "profile_user_id": OTHER}, "BENCHMARK_NOTE_MISMATCH"),
        ({"note_id": OTHER, "profile_user_id": PROFILE}, "BENCHMARK_NOTE_MISMATCH"),
    ],
)
def test_detail_identity_and_author_verified_before_any_content(identity: object, code: str):
    provider = XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url="http://127.0.0.1:5556"
    )
    with pytest.raises(BenchmarkAccountError) as error:
        provider._extract_evidence(
            DetailIdentityPage(identity),
            profile_user_id=PROFILE,
            note_id=NOTE,
            trusted_media_seen=False,
        )
    assert error.value.code == code


class ProfilePage:
    def __init__(self, status: int, url: str = PROFILE_URL):
        self.status = status
        self.url = url

    def goto(self, *args, **kwargs):
        return self

    def wait_for_timeout(self, timeout: int):
        pass


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "BENCHMARK_AUTHENTICATION_REQUIRED"),
        (403, "BENCHMARK_NOTE_INACCESSIBLE"),
        (404, "BENCHMARK_NOTE_INACCESSIBLE"),
        (410, "BENCHMARK_NOTE_INACCESSIBLE"),
    ],
)
def test_profile_access_diagnostics_distinguish_denial_from_logged_out(status: int, code: str):
    provider = XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url="http://127.0.0.1:5556"
    )
    with pytest.raises(BenchmarkAccountError) as error:
        provider._open_profile(ProfilePage(status), PROFILE_URL, 1000)
    assert error.value.code == code


@pytest.fixture(scope="module")
def isolated_dom_browser():
    """Real DOM execution against intercepted local fixtures; no live-site traffic."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def isolated_dom_page(isolated_dom_browser):
    context = isolated_dom_browser.new_context()
    context.route(
        "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body="")
    )
    page = context.new_page()
    page.goto(PROFILE_URL)
    yield page
    context.close()


def test_real_dom_discovers_same_card_links_and_never_merges_duplicate_titles(isolated_dom_page):
    page = isolated_dom_page
    page.set_content(f"""
      <a href="/user/profile/{OTHER}/{OTHER}">unrelated page navigation</a>
      <section class="note-item">
        <a href="/user/profile/{PROFILE}/{NOTE}?xsec_token=private-fixture-token">cover</a>
        <a class="title" href="/user/profile/{PROFILE}/{NOTE}">same title</a>
        <span class="count">10万+</span>
      </section>
      <section class="note-item"><a class="title"
        href="/user/profile/{PROFILE}/{OTHER}">same title</a></section>
      <section class="note-item"><span class="title">same title</span></section>
    """)
    cards = discover_profile_note_links(page, PROFILE)
    assert [item["note_id"] for item in cards] == [NOTE, OTHER]
    assert [item["title"] for item in cards] == ["same title", "same title"]
    assert cards[0]["likes_display"] == "10万+"
    assert all(item["format"] == "unknown" and item["published_at"] is None for item in cards)
    serialized = json.dumps(cards)
    assert "xsec_token" not in serialized and "private-fixture-token" not in serialized


@pytest.mark.parametrize(
    "href,code",
    [
        (f"/user/profile/{OTHER}/{NOTE}", "BENCHMARK_PROFILE_MISMATCH"),
        (f"https://evil.invalid/user/profile/{PROFILE}/{NOTE}", "BENCHMARK_NOTE_IDENTITY_INVALID"),
        (
            f"https://www.xiaohongshu.com:443/user/profile/{PROFILE}/{NOTE}",
            "BENCHMARK_NOTE_IDENTITY_INVALID",
        ),
        (f"/user/profile/{PROFILE}/{NOTE}#false-source", "BENCHMARK_NOTE_IDENTITY_INVALID"),
    ],
)
def test_real_dom_rejects_untrusted_or_cross_account_sources(isolated_dom_page, href, code):
    isolated_dom_page.set_content(f'<section class="note-item"><a href="{href}">x</a></section>')
    with pytest.raises(NoteIdentityError) as error:
        discover_profile_note_links(isolated_dom_page, PROFILE)
    assert error.value.code == code


def test_real_dom_rejects_disagreeing_sources_on_one_card(isolated_dom_page):
    isolated_dom_page.set_content(f"""<section class="note-item">
      <a href="/user/profile/{PROFILE}/{NOTE}">cover</a>
      <a href="/user/profile/{PROFILE}/{OTHER}">title</a>
    </section>""")
    with pytest.raises(NoteIdentityError) as error:
        discover_profile_note_links(isolated_dom_page, PROFILE)
    assert error.value.code == "BENCHMARK_NOTE_IDENTITY_CONFLICT"


@pytest.mark.parametrize("query", ["", "?xsec_token=private-fixture-token"])
def test_real_dom_clicks_identity_with_or_without_token(isolated_dom_page, query):
    page = isolated_dom_page
    page.set_content(f"""<section class="note-item"><a
      href="/user/profile/{PROFILE}/{NOTE}{query}"
      onclick="event.preventDefault(); window.fixtureClicked = true;">note</a></section>""")
    assert click_verified_profile_note(page, PROFILE, NOTE)
    assert page.evaluate("window.fixtureClicked") is True


@pytest.mark.parametrize("note_type,expected_kind", [("normal", "image"), ("video", "video")])
def test_real_dom_media_type_comes_from_verified_detail_not_absent_video_element(
    isolated_dom_page,
    note_type,
    expected_kind,
):
    page = isolated_dom_page
    page.set_content('<h1 class="title">fixture title</h1>')
    page.evaluate(
        "state => window.__INITIAL_STATE__ = state",
        {
            "note": {
                "noteDetailMap": {
                    NOTE: {
                        "note": {
                            "noteId": NOTE,
                            "user": {"userId": PROFILE},
                            "type": note_type,
                        }
                    }
                }
            },
        },
    )
    provider = XiaohongshuAuthenticatedNoteProvider(
        managed_browser_base_url="http://127.0.0.1:5556"
    )
    evidence = provider._extract_evidence(
        page,
        profile_user_id=PROFILE,
        note_id=NOTE,
        trusted_media_seen=False,
    )
    assert evidence.media.kind == expected_kind
    assert not evidence.media.video_available


@pytest.mark.parametrize(
    "origin,trusted",
    [
        ("https://video.xhscdn.com", True),
        ("https://xhscdn.net", True),
        ("http://video.xhscdn.com", False),
        ("https://video.xhscdn.com.attacker.invalid", False),
        ("https://attacker@video.xhscdn.com", False),
        ("https://video.xhscdn.com:443", False),
        ("https://127.0.0.1", False),
    ],
)
def test_video_probe_exports_booleans_without_signed_locator(isolated_dom_page, origin, trusted):
    page = isolated_dom_page
    page.set_content('<video preload="none"></video>')
    page.locator("video").evaluate(
        "(element, source) => element.src = source", f"{origin}/video.mp4?token=probe-secret"
    )
    probe = page.locator("video").evaluate(_VIDEO_PROBE_SCRIPT)
    assert probe["source_available"] is True
    assert probe["trusted_origin"] is trusted
    assert set(probe) == {"duration", "width", "height", "source_available", "trusted_origin"}
    assert "probe-secret" not in json.dumps(probe) and "video.mp4" not in json.dumps(probe)
