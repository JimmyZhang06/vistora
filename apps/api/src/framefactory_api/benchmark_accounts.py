from __future__ import annotations

import asyncio
import contextlib
import json
import re
import statistics
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field

from .benchmark_seeds import BAIZHOU_XIAOXIONG
from .browser_process import BrowserProcessError, run_browser_operation

_INITIAL_STATE_MARKER = "window.__INITIAL_STATE__="
_XIAOHONGSHU_PROFILE_PATH = re.compile(r"^/user/profile/([0-9a-f]{24})/?$")
_COUNT = re.compile(r"^(\d+(?:\.\d+)?)(万|亿)?(\+)?$")
_COUNT_MULTIPLIERS = {None: Decimal(1), "万": Decimal(10_000), "亿": Decimal(100_000_000)}
_THEMES: dict[str, tuple[str, ...]] = {
    "旅行与城市": ("旅行", "vlog", "香港", "巴黎", "清迈", "世界"),
    "夏日与自然": ("夏日", "夏天", "绿色", "阳光", "自然", "茉莉", "薄荷"),
    "镜头与转场": ("转场", "封面", "视角", "拍"),
    "穿搭与造型": ("时尚", "美物", "发型", "短发", "天使"),
    "日常与美食": ("美食", "厨房", "日常"),
}


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicMetric(BenchmarkModel):
    display: str
    lower_bound: int | None = Field(default=None, ge=0)
    precision: Literal["exact", "rounded", "lower_bound", "unknown"]


class BenchmarkProfile(BenchmarkModel):
    platform: Literal["xiaohongshu"] = "xiaohongshu"
    user_id: str
    profile_url: str
    nickname: str
    red_id: str
    tags: list[str]
    following: PublicMetric
    followers: PublicMetric
    likes_and_collections: PublicMetric


class BenchmarkNote(BenchmarkModel):
    sample_index: int = Field(ge=1)
    note_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{24}$")
    identity_status: Literal["verified", "missing"] = "missing"
    title: str
    format: Literal["video", "image", "unknown"]
    published_at: datetime | None
    likes: PublicMetric
    pinned: bool


class ThemeSignal(BenchmarkModel):
    theme: str
    matching_notes: int = Field(ge=0)


class BenchmarkAnalysis(BenchmarkModel):
    sample_size: int = Field(ge=0)
    video_count: int = Field(ge=0)
    image_count: int = Field(ge=0)
    unknown_count: int = Field(default=0, ge=0)
    video_share_percent: float = Field(ge=0, le=100)
    median_likes_lower_bound: int | None = Field(default=None, ge=0)
    posts_last_30_days: int = Field(ge=0)
    median_publish_interval_days: float | None = Field(default=None, ge=0)
    themes: list[ThemeSignal]
    top_notes: list[BenchmarkNote]


class BenchmarkAcquisition(BenchmarkModel):
    discovery_version: Literal["1"] = "1"
    note_identity_status: Literal["complete", "partial", "unavailable"] = "unavailable"
    identified_note_count: int = Field(default=0, ge=0)
    unresolved_note_count: int = Field(default=0, ge=0)
    identity_error_code: str | None = None
    captured_at: datetime
    method: Literal["public_profile_ssr", "authenticated_managed_browser"] = "public_profile_ssr"
    from_cache: bool
    initial_page_has_more: bool
    completeness: Literal["initial_page_sample"] = "initial_page_sample"
    limitations: list[str]


class BenchmarkSnapshot(BenchmarkModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    profile: BenchmarkProfile
    acquisition: BenchmarkAcquisition
    analysis: BenchmarkAnalysis
    notes: list[BenchmarkNote]


class BenchmarkAccountPreview(BenchmarkModel):
    platform: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_-]+$")
    profile_url: str = Field(min_length=1, max_length=1000)
    refresh_note_identity: bool = False


class BenchmarkAccountError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


ProfileFetcher = Callable[[str, float, int], bytes]


@dataclass(frozen=True, slots=True)
class BenchmarkAccountIdentity:
    platform: str
    external_user_id: str
    profile_url: str


class BenchmarkAccountProvider(Protocol):
    platform: str

    def resolve(self, profile_url: str) -> BenchmarkAccountIdentity: ...

    def collect(self, identity: BenchmarkAccountIdentity) -> BenchmarkSnapshot: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class XiaohongshuPublicProfileProvider:
    platform = "xiaohongshu"

    def __init__(
        self,
        *,
        fetcher: ProfileFetcher | None = None,
        authenticated_fetcher: ProfileFetcher | None = None,
        timeout_seconds: float = 8.0,
        managed_timeout_seconds: float = 25.0,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        self._fetcher = fetcher or _fetch_public_profile
        self._authenticated_fetcher = authenticated_fetcher
        self._timeout_seconds = timeout_seconds
        self._managed_timeout_seconds = managed_timeout_seconds
        self._max_response_bytes = max_response_bytes

    def resolve(self, profile_url: str) -> BenchmarkAccountIdentity:
        return resolve_xiaohongshu_profile(profile_url)

    def collect(self, identity: BenchmarkAccountIdentity) -> BenchmarkSnapshot:
        snapshot = None
        try:
            payload = self._fetcher(
                identity.profile_url,
                self._timeout_seconds,
                self._max_response_bytes,
            )
            snapshot = parse_public_profile_html(
                payload,
                profile_url=identity.profile_url,
                expected_user_id=identity.external_user_id,
            )
        except BenchmarkAccountError as exc:
            if self._authenticated_fetcher is None or exc.code not in {
                "BENCHMARK_SOURCE_UNAVAILABLE",
                "BENCHMARK_SOURCE_CHANGED",
            }:
                raise
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            raise BenchmarkAccountError(
                "BENCHMARK_SOURCE_UNAVAILABLE",
                "The Xiaohongshu public profile page could not be fetched",
                retryable=True,
            ) from exc
        if snapshot is not None and snapshot.acquisition.note_identity_status == "complete":
            return snapshot
        if self._authenticated_fetcher is None:
            assert snapshot is not None
            return _identity_diagnostics(snapshot, "BENCHMARK_NOTE_IDENTITY_INCOMPLETE")
        try:
            payload = self._authenticated_fetcher(
                identity.profile_url,
                self._managed_timeout_seconds,
                self._max_response_bytes,
            )
            discovered = parse_public_profile_html(
                payload,
                profile_url=identity.profile_url,
                expected_user_id=identity.external_user_id,
                acquisition_method="authenticated_managed_browser",
            )
            # The authenticated sample is authoritative. Only known identities can be
            # carried across snapshots; never attach new IDs to title/index matches.
            return _merge_verified_snapshots(snapshot, discovered)
        except BenchmarkAccountError as exc:
            if snapshot is None or exc.code in {
                "BENCHMARK_PROFILE_MISMATCH",
                "BENCHMARK_NOTE_IDENTITY_CONFLICT",
                "BENCHMARK_NOTE_MISMATCH",
                "BENCHMARK_SOURCE_INVALID",
            }:
                raise
            return _identity_diagnostics(snapshot, exc.code)
        except Exception as exc:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "The authenticated profile provider could not complete discovery",
                retryable=True,
            ) from exc


class BenchmarkProviderRegistry:
    def __init__(self, providers: Iterable[BenchmarkAccountProvider]) -> None:
        self._providers: dict[str, BenchmarkAccountProvider] = {}
        for provider in providers:
            key = provider.platform.strip().lower()
            if not key or key in self._providers:
                raise ValueError(f"duplicate or empty benchmark provider: {key!r}")
            self._providers[key] = provider

    def get(self, platform: str) -> BenchmarkAccountProvider:
        normalized = platform.strip().lower()
        provider = self._providers.get(normalized)
        if provider is None:
            raise BenchmarkAccountError(
                "BENCHMARK_PLATFORM_UNSUPPORTED",
                f"Benchmark collection is not available for platform {normalized or '<empty>'}",
            )
        return provider

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class BenchmarkAccountGateway:
    """Provider-neutral orchestration and short-lived request deduplication."""

    def __init__(
        self,
        *,
        providers: Iterable[BenchmarkAccountProvider] | None = None,
        fetcher: ProfileFetcher | None = None,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = 1_048_576,
        cache_ttl_seconds: float = 300.0,
        cache_max_entries: int = 128,
        managed_browser_base_url: str | None = None,
    ) -> None:
        if cache_max_entries < 1:
            raise ValueError("cache_max_entries must be positive")
        default_providers = (
            XiaohongshuPublicProfileProvider(
                fetcher=fetcher,
                authenticated_fetcher=(
                    partial(
                        _fetch_managed_profile_isolated,
                        base_url=_managed_browser_base_url(managed_browser_base_url),
                    )
                    if managed_browser_base_url
                    else None
                ),
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            ),
        )
        self.registry = BenchmarkProviderRegistry(providers or default_providers)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._lock = asyncio.Lock()
        self._pending = 0
        self._cache: OrderedDict[tuple[str, str], tuple[float, BenchmarkSnapshot]] = OrderedDict()

    async def collect(
        self,
        platform: str,
        profile_url: str,
        *,
        refresh_note_identity: bool = False,
    ) -> BenchmarkSnapshot:
        provider = self.registry.get(platform)
        identity = provider.resolve(profile_url)
        cache_key = (identity.platform, identity.profile_url)
        cached = self._fresh_cache(cache_key, refresh=refresh_note_identity)
        if cached is not None:
            return _with_cache_state(cached, from_cache=True)

        if self._pending >= 32:
            raise BenchmarkAccountError(
                "BENCHMARK_DISCOVERY_BUSY",
                "Profile discovery is busy; retry later",
                retryable=True,
            )
        self._pending += 1
        try:
            async with asyncio.timeout(30):
                await self._lock.acquire()
        except TimeoutError as exc:
            self._pending -= 1
            raise BenchmarkAccountError(
                "BENCHMARK_DISCOVERY_BUSY",
                "Profile discovery queue timed out",
                retryable=True,
            ) from exc
        except BaseException:
            self._pending -= 1
            raise
        try:
            cached = self._fresh_cache(cache_key, refresh=refresh_note_identity)
            if cached is not None:
                return _with_cache_state(cached, from_cache=True)
            task = asyncio.create_task(asyncio.to_thread(provider.collect, identity))
            cancelled = False
            while True:
                try:
                    snapshot = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    # Repeated disconnects must not cancel the wrapper of a still
                    # running thread and release its browser serialization early.
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            self._cache[cache_key] = (time.monotonic(), snapshot)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)
            return snapshot
        finally:
            self._pending -= 1
            self._lock.release()

    async def collect_demo(self) -> BenchmarkSnapshot:
        return await self.collect(
            BAIZHOU_XIAOXIONG.platform,
            BAIZHOU_XIAOXIONG.profile_url,
        )

    def _fresh_cache(
        self,
        key: tuple[str, str],
        *,
        refresh: bool = False,
    ) -> BenchmarkSnapshot | None:
        cached = self._cache.get(key)
        if cached is None:
            return None
        cached_at, snapshot = cached
        ttl = self._cache_ttl_seconds
        if refresh or snapshot.acquisition.note_identity_status != "complete":
            ttl = min(ttl, 10.0)
        if time.monotonic() - cached_at >= ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return snapshot


def _with_cache_state(snapshot: BenchmarkSnapshot, *, from_cache: bool) -> BenchmarkSnapshot:
    return snapshot.model_copy(
        update={"acquisition": snapshot.acquisition.model_copy(update={"from_cache": from_cache})}
    )


def _identity_diagnostics(
    snapshot: BenchmarkSnapshot,
    error_code: str | None = None,
) -> BenchmarkSnapshot:
    identified = sum(note.note_id is not None for note in snapshot.notes)
    unresolved = len(snapshot.notes) - identified
    status = (
        "complete" if identified and not unresolved else "partial" if identified else "unavailable"
    )
    return snapshot.model_copy(
        update={
            "acquisition": snapshot.acquisition.model_copy(
                update={
                    "note_identity_status": status,
                    "identified_note_count": identified,
                    "unresolved_note_count": unresolved,
                    "identity_error_code": (error_code or "BENCHMARK_NOTE_IDENTITY_INCOMPLETE")
                    if unresolved or not identified
                    else None,
                }
            )
        }
    )


def _merge_verified_snapshots(
    previous: BenchmarkSnapshot | None,
    current: BenchmarkSnapshot,
) -> BenchmarkSnapshot:
    if previous is None:
        return _identity_diagnostics(current)
    if previous.profile.user_id != current.profile.user_id:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_MISMATCH", "Discovery returned another account"
        )
    # Use fresh notes only: an old ID absent from the new visible page may be deleted.
    known = {note.note_id: note for note in previous.notes if note.note_id}
    for note in current.notes:
        old = known.get(note.note_id)
        if (
            old
            and old.format != "unknown"
            and note.format != "unknown"
            and old.format != note.format
        ):
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_IDENTITY_CONFLICT",
                "Verified note sources disagree on media type",
            )
    return _identity_diagnostics(current)


def resolve_xiaohongshu_profile(profile_url: str) -> BenchmarkAccountIdentity:
    value = profile_url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_INVALID", "The Xiaohongshu profile URL is invalid"
        ) from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "www.xiaohongshu.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_INVALID",
            "Use an HTTPS www.xiaohongshu.com profile URL without credentials, port, or fragment",
        )
    match = _XIAOHONGSHU_PROFILE_PATH.fullmatch(parsed.path)
    if match is None:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_INVALID",
            "The URL must point to /user/profile/{24-character-user-id}",
        )
    external_user_id = match.group(1)
    return BenchmarkAccountIdentity(
        platform="xiaohongshu",
        external_user_id=external_user_id,
        profile_url=(f"https://www.xiaohongshu.com/user/profile/{external_user_id}"),
    )


def _fetch_public_profile(url: str, timeout_seconds: float, max_bytes: int) -> bytes:
    identity = resolve_xiaohongshu_profile(url)
    if identity.profile_url != url:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_INVALID",
            "The provider only fetches canonical Xiaohongshu profile URLs",
        )
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"
            ),
        },
        method="GET",
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise BenchmarkAccountError(
                    "BENCHMARK_SOURCE_UNAVAILABLE",
                    f"Xiaohongshu returned HTTP {response.status}",
                    retryable=response.status >= 500,
                )
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise BenchmarkAccountError(
                    "BENCHMARK_SOURCE_INVALID",
                    "Xiaohongshu returned an unexpected content type",
                )
            payload = response.read(max_bytes + 1)
    except HTTPError as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_UNAVAILABLE",
            f"Xiaohongshu returned HTTP {exc.code}",
            retryable=exc.code == 429 or exc.code >= 500,
        ) from exc
    except (TimeoutError, URLError) as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_UNAVAILABLE",
            "The Xiaohongshu public profile page did not respond in time",
            retryable=True,
        ) from exc
    if len(payload) > max_bytes:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_TOO_LARGE",
            "The Xiaohongshu profile response exceeded the demo safety limit",
        )
    return payload


def _managed_browser_base_url(value: str | None) -> str:
    if value is None:
        raise ValueError("managed browser base URL is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ValueError("managed browser base URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "managed browser base URL must be an explicit http://127.0.0.1:<port> origin"
        )
    return f"http://127.0.0.1:{port}"


def _fetch_managed_profile_isolated(
    url: str, timeout_seconds: float, max_bytes: int, *, base_url: str,
) -> bytes:
    try:
        result = run_browser_operation(
            "profile", {"url": url, "timeout_seconds": timeout_seconds,
                        "max_bytes": max_bytes, "base_url": base_url},
            timeout_seconds=max(1, min(timeout_seconds, 60)) + 5,
        )
        return result.encode("utf-8")
    except BrowserProcessError:
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_UNAVAILABLE", "Profile discovery timed out or was unavailable",
            retryable=True,
        ) from None


def _fetch_managed_profile(
    url: str,
    timeout_seconds: float,
    max_bytes: int,
    *,
    base_url: str,
) -> bytes:
    """Read one profile through an explicitly configured loopback browser provider."""
    deadline = time.monotonic() + timeout_seconds

    def remaining_ms() -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "Profile discovery timed out",
                retryable=True,
            )
        return max(1, round(remaining * 1_000))

    identity = resolve_xiaohongshu_profile(url)
    if identity.profile_url != url:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_INVALID",
            "The managed provider only opens canonical Xiaohongshu profile URLs",
        )
    status_request = Request(
        f"{base_url}/browser/managed/status",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with build_opener(_RejectRedirects()).open(
            status_request, timeout=timeout_seconds
        ) as response:
            status_payload = response.read(65_537)
    except (HTTPError, TimeoutError, URLError) as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_UNAVAILABLE",
            "The local authenticated browser provider is unavailable",
            retryable=True,
        ) from exc
    if len(status_payload) > 65_536:
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_INVALID",
            "The local browser provider returned an oversized status response",
        )
    try:
        browser_status = json.loads(status_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_INVALID",
            "The local browser provider returned invalid status data",
        ) from exc
    cdp_port = browser_status.get("cdp_port") if isinstance(browser_status, dict) else None
    if (
        not isinstance(cdp_port, int)
        or not 1 <= cdp_port <= 65_535
        or browser_status.get("cdp_host") != "127.0.0.1"
        or browser_status.get("state") != "running"
    ):
        raise BenchmarkAccountError(
            "BENCHMARK_AUTHENTICATION_REQUIRED",
            "Start and sign in to the local Xiaohongshu browser provider",
            retryable=True,
        )
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - production packaging guard
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_UNAVAILABLE",
            "Playwright is required for authenticated benchmark collection",
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{cdp_port}", timeout=remaining_ms()
            )
            if not browser.contexts:
                raise BenchmarkAccountError(
                    "BENCHMARK_AUTHENTICATION_REQUIRED",
                    "The local Xiaohongshu browser has no active profile",
                    retryable=True,
                )
            context = browser.contexts[0]
            page = context.new_page()
            try:
                page.set_default_timeout(remaining_ms())
                response = page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms())
                current = urlsplit(page.url)
                if (
                    current.scheme != "https"
                    or current.hostname != "www.xiaohongshu.com"
                    or current.port is not None
                    or current.username
                    or current.password
                    or current.path != urlsplit(url).path
                ):
                    login = current.hostname == "www.xiaohongshu.com" and current.path.startswith(
                        ("/login", "/signin")
                    )
                    raise BenchmarkAccountError(
                        "BENCHMARK_AUTHENTICATION_REQUIRED"
                        if login
                        else "BENCHMARK_PROFILE_MISMATCH",
                        "The local browser did not resolve to the requested profile",
                        retryable=True,
                    )
                if response is not None and response.status != 200:
                    raise BenchmarkAccountError(
                        "BENCHMARK_AUTHENTICATION_REQUIRED"
                        if response.status == 401
                        else "BENCHMARK_NOTE_INACCESSIBLE"
                        if response.status in {403, 404, 410}
                        else "BENCHMARK_SOURCE_UNAVAILABLE",
                        f"Xiaohongshu returned HTTP {response.status} in the managed browser",
                        retryable=response.status == 429 or response.status >= 500,
                    )
                try:
                    page.wait_for_function(
                        """userId => {
                          const user = window.__INITIAL_STATE__?.user;
                          if (!user?.userPageData?.basicInfo) return false;
                          const valid = value => typeof value === 'string'
                            && /^[0-9a-f]{24}$/.test(value);
                          if ((user.notes ?? []).slice(0,2).some(group =>
                            Array.isArray(group) && group.slice(0,64).some(item =>
                              item.noteCard?.user?.userId === userId &&
                              (valid(item.noteCard?.noteId) || valid(item.id))))) return true;
                          return [...document.querySelectorAll('section.note-item a[href]')]
                            .slice(0,1024).some(link => {
                              try {
                                const url = new URL(link.getAttribute('href'), location.origin);
                                return url.origin === 'https://www.xiaohongshu.com'
                                  && new RegExp('^/user/profile/' + userId +
                                    '/[0-9a-f]{24}/?$').test(url.pathname);
                              } catch { return false; }
                            });
                        }""",
                        arg=identity.external_user_id,
                        timeout=remaining_ms(),
                    )
                except PlaywrightTimeoutError as exc:
                    raise BenchmarkAccountError(
                        "BENCHMARK_NOTE_IDENTITY_INCOMPLETE",
                        "Profile note identity fields did not become available within the budget",
                        retryable=True,
                    ) from exc
                # Only public metadata leaves the page. No href, token, cookie,
                # cover URL, full HTML, storage state or network response is read.
                projected = page.evaluate("""() => {
                  const user = window.__INITIAL_STATE__?.user ?? {};
                  const data = user.userPageData ?? {};
                  const text = (v, n=300) => typeof v === 'string' ? v.slice(0,n) : '';
                  return {user: {
                    userPageData: {
                      basicInfo: {nickname:text(data.basicInfo?.nickname),
                        redId:text(data.basicInfo?.redId)},
                      interactions:(data.interactions ?? []).slice(0,10).map(x=>({
                        type:text(x.type,32),count:text(x.count,32)})),
                      tags:(data.tags ?? []).slice(0,20).map(x=>({name:text(x.name)}))
                    },
                    notes:(user.notes ?? []).slice(0,2).map(g=>g.slice(0,60).map(x=>{
                      const c=x.noteCard ?? {};
                      return {id:text(x.id,64),noteCard:{
                        noteId:text(c.noteId,64), displayTitle:text(c.displayTitle),
                        type:text(c.type,32), time:typeof c.time==='number' ? c.time : null,
                        user:{userId:text(c.user?.userId,64)},
                        interactInfo:{likedCount:text(c.interactInfo?.likedCount,32),sticky:c.interactInfo?.sticky===true}
                      }};
                    })),
                    noteQueries:(user.noteQueries ?? []).slice(0,1).map(x=>({
                      hasMore:x.hasMore===true}))
                  }};
                }""")
                from .benchmark_note_identity import NoteIdentityError, discover_profile_note_links

                try:
                    projected["_verified_dom_notes"] = discover_profile_note_links(
                        page,
                        identity.external_user_id,
                    )
                except NoteIdentityError as exc:
                    raise BenchmarkAccountError(exc.code, str(exc)) from exc
                remaining_ms()
                safe_state = json.dumps(projected, ensure_ascii=False).replace("<", "\\u003c")
                payload = f"<script>{_INITIAL_STATE_MARKER}{safe_state}</script>".encode()
            finally:
                page.close()
    except BenchmarkAccountError:
        raise
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_PROVIDER_UNAVAILABLE",
            "The local authenticated browser could not load the profile",
            retryable=True,
        ) from exc
    if len(payload) > max_bytes:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_TOO_LARGE",
            "The authenticated profile response exceeded the configured safety limit",
        )
    return payload


def parse_public_profile_html(
    payload: bytes,
    *,
    profile_url: str,
    expected_user_id: str,
    captured_at: datetime | None = None,
    acquisition_method: Literal[
        "public_profile_ssr", "authenticated_managed_browser"
    ] = "public_profile_ssr",
) -> BenchmarkSnapshot:
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_INVALID", "The profile page was not valid UTF-8"
        ) from exc
    marker_start = html.find(_INITIAL_STATE_MARKER)
    if marker_start < 0:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The public profile state was not present in the page",
            retryable=True,
        )
    json_start = marker_start + len(_INITIAL_STATE_MARKER)
    json_end = html.find("</script>", json_start)
    if json_end < 0:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The public profile state was incomplete",
            retryable=True,
        )
    try:
        state = json.loads(_json_compatible_initial_state(html[json_start:json_end]))
    except json.JSONDecodeError as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The public profile state could not be decoded",
            retryable=True,
        ) from exc

    collected_at = (captured_at or datetime.now(UTC)).astimezone(UTC)
    user = _mapping(_mapping(state).get("user"))
    page_data = _mapping(user.get("userPageData"))
    basic = _mapping(page_data.get("basicInfo"))
    nickname = _text(basic.get("nickname"))
    red_id = _text(basic.get("redId"))
    if not nickname or not red_id:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The profile identity was missing from the public page",
            retryable=True,
        )

    notes = _parse_notes(
        user.get("notes"),
        collected_at=collected_at,
        expected_user_id=expected_user_id,
    )
    # This field is generated only by the local managed-page projection below.
    # Never trust a matching marker injected into public HTML.
    if acquisition_method == "authenticated_managed_browser":
        known_ids = {note.note_id for note in notes if note.note_id}
        for item in _sequence(_mapping(state).get("_verified_dom_notes"))[:60]:
            item = _mapping(item)
            note_id = _text(item.get("note_id"))
            if (
                not re.fullmatch(r"[0-9a-f]{24}", note_id)
                or item.get("profile_user_id") != expected_user_id
            ):
                raise BenchmarkAccountError(
                    "BENCHMARK_NOTE_IDENTITY_CONFLICT",
                    "DOM discovery returned an invalid identity",
                )
            if note_id in known_ids:
                continue
            known_ids.add(note_id)
            notes.append(
                BenchmarkNote(
                    sample_index=len(notes) + 1,
                    note_id=note_id,
                    identity_status="verified",
                    title=_text(item.get("title"))[:300] or "未提供标题",
                    format="unknown",
                    published_at=None,
                    likes=metric_from_display(_text(item.get("likes_display"))[:32]),
                    pinned=False,
                )
            )
    capped = len(notes) > 64
    if capped:
        # Prioritize independently verified cards. This is a bounded new sample,
        # never an ID assignment to an unidentified card at the same position.
        notes = [note for note in notes if note.note_id] + [
            note for note in notes if not note.note_id
        ]
        notes = notes[:64]
        notes = [note.model_copy(update={"sample_index": i + 1}) for i, note in enumerate(notes)]
    if not notes:
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The public profile page did not contain an analysable note sample",
            retryable=True,
        )
    discovered_user_ids = {
        _text(_mapping(_mapping(_mapping(item).get("noteCard")).get("user")).get("userId"))
        for group in _sequence(user.get("notes"))
        for item in _sequence(group)
    }
    discovered_user_ids.discard("")
    if discovered_user_ids - {expected_user_id}:
        raise BenchmarkAccountError(
            "BENCHMARK_PROFILE_MISMATCH",
            "The public page did not resolve to the requested account",
        )
    if not discovered_user_ids and not any(note.note_id for note in notes):
        raise BenchmarkAccountError(
            "BENCHMARK_SOURCE_CHANGED",
            "The profile sample lacked author verification fields",
            retryable=True,
        )

    interactions = {
        _text(_mapping(item).get("type")): metric_from_display(_text(_mapping(item).get("count")))
        for item in _sequence(page_data.get("interactions"))
    }
    query = _mapping(_first(_sequence(user.get("noteQueries"))))
    tags = [
        name
        for item in _sequence(page_data.get("tags"))
        if (name := _text(_mapping(item).get("name")))
    ]
    profile = BenchmarkProfile(
        user_id=expected_user_id,
        profile_url=profile_url,
        nickname=nickname,
        red_id=red_id,
        tags=tags,
        following=interactions.get("follows", metric_from_display("")),
        followers=interactions.get("fans", metric_from_display("")),
        likes_and_collections=interactions.get("interaction", metric_from_display("")),
    )
    acquisition = BenchmarkAcquisition(
        captured_at=collected_at,
        method=acquisition_method,
        from_cache=False,
        initial_page_has_more=bool(query.get("hasMore")),
        limitations=[
            (
                "通过本机已登录受管浏览器读取主页首屏, 不滚动、不抓评论, 也不访问收藏内容。"
                if acquisition_method == "authenticated_managed_browser"
                else "只读取公开主页首屏 SSR 快照, 不滚动、不抓评论, 也不访问收藏内容。"
            ),
            "未登录访问时, 粉丝与获赞收藏总数可能被平台降精度展示。",
            "带“+”的互动量按下界计算; 分析结果不代表完整历史表现。",
            "数据未写入数据库; 进程内仅作短时缓存以减少对来源站点的请求。",
        ],
    )
    if capped:
        acquisition.limitations.append(
            "合并后的首屏样本最多保留 64 条, 优先保留已核验身份; 未纳入卡片不计入样本统计。"
        )
    if any(note.published_at is None for note in notes):
        acquisition.limitations.append(
            "部分卡片缺少可核验发布时间, 已从近 30 天数量及发布间隔计算中排除。"
        )
    return _identity_diagnostics(
        BenchmarkSnapshot(
            profile=profile,
            acquisition=acquisition,
            analysis=_analyse(notes, collected_at=collected_at),
            notes=notes,
        )
    )


def metric_from_display(value: str) -> PublicMetric:
    cleaned = value.strip().replace(",", "")
    match = _COUNT.fullmatch(cleaned)
    if match is None:
        return PublicMetric(display=cleaned or "—", lower_bound=None, precision="unknown")
    try:
        lower_bound = int(Decimal(match.group(1)) * _COUNT_MULTIPLIERS[match.group(2)])
    except (InvalidOperation, ValueError):
        return PublicMetric(display=cleaned or "—", lower_bound=None, precision="unknown")
    precision: Literal["exact", "rounded", "lower_bound", "unknown"]
    if match.group(3):
        precision = "lower_bound"
    elif match.group(2):
        precision = "rounded"
    else:
        precision = "exact"
    return PublicMetric(display=cleaned, lower_bound=lower_bound, precision=precision)


def _parse_notes(
    raw_groups: Any,
    *,
    collected_at: datetime,
    expected_user_id: str,
) -> list[BenchmarkNote]:
    from .benchmark_note_identity import NoteIdentityError, resolve_structured_note_id

    output: list[BenchmarkNote] = []
    known: dict[str, BenchmarkNote] = {}
    for group in _sequence(raw_groups):
        for raw_item in _sequence(group):
            card = _mapping(_mapping(raw_item).get("noteCard"))
            try:
                note_id = resolve_structured_note_id(raw_item, expected_user_id)
            except NoteIdentityError as exc:
                raise BenchmarkAccountError(exc.code, str(exc)) from exc
            title = _text(card.get("displayTitle"))
            timestamp = card.get("time")
            if not title:
                continue
            published_at = None
            if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                with contextlib.suppress(ValueError, OverflowError, OSError):
                    published_at = datetime.fromtimestamp(float(timestamp) / 1000, tz=UTC)
            if published_at and published_at > collected_at + timedelta(days=1):
                published_at = None
            note_type = _text(card.get("type"))
            interact = _mapping(card.get("interactInfo"))
            note = BenchmarkNote(
                sample_index=len(output) + 1,
                note_id=note_id,
                identity_status="verified" if note_id else "missing",
                title=title,
                format={"video": "video", "normal": "image"}.get(note_type, "unknown"),
                published_at=published_at,
                likes=metric_from_display(_text(interact.get("likedCount"))),
                pinned=bool(interact.get("sticky")),
            )
            if note_id and note_id in known:
                old = known[note_id]
                if old.title != note.title or old.format != note.format:
                    raise BenchmarkAccountError(
                        "BENCHMARK_NOTE_IDENTITY_CONFLICT",
                        "Duplicate note sources disagree",
                    )
                continue
            output.append(note)
            if note_id:
                known[note_id] = note
            if len(output) >= 64:
                return output
    return output


def _analyse(notes: list[BenchmarkNote], *, collected_at: datetime) -> BenchmarkAnalysis:
    videos = sum(note.format == "video" for note in notes)
    likes = [note.likes.lower_bound for note in notes if note.likes.lower_bound is not None]
    recent_cutoff = collected_at - timedelta(days=30)
    recent = [
        note
        for note in notes
        if not note.pinned and note.published_at and note.published_at >= recent_cutoff
    ]
    chronological = sorted(
        (note.published_at for note in notes if not note.pinned and note.published_at), reverse=True
    )
    gaps = [
        (chronological[index] - chronological[index + 1]).total_seconds() / 86_400
        for index in range(len(chronological) - 1)
    ]
    themes = [
        ThemeSignal(
            theme=theme,
            matching_notes=sum(
                any(keyword.casefold() in note.title.casefold() for keyword in keywords)
                for note in notes
            ),
        )
        for theme, keywords in _THEMES.items()
    ]
    themes.sort(key=lambda item: (-item.matching_notes, item.theme))
    top_notes = sorted(
        notes,
        key=lambda note: (
            note.likes.lower_bound or -1,
            not note.pinned,
            note.published_at or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )[:5]
    return BenchmarkAnalysis(
        sample_size=len(notes),
        video_count=videos,
        image_count=sum(note.format == "image" for note in notes),
        unknown_count=sum(note.format == "unknown" for note in notes),
        video_share_percent=round(videos / len(notes) * 100, 1) if notes else 0,
        median_likes_lower_bound=int(statistics.median(likes)) if likes else None,
        posts_last_30_days=len(recent),
        median_publish_interval_days=(round(statistics.median(gaps), 1) if gaps else None),
        themes=themes,
        top_notes=top_notes,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first(value: list[Any]) -> Any:
    return value[0] if value else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_compatible_initial_state(value: str) -> str:
    """Normalize known inert JS literals without evaluating code or changing strings."""

    output: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(value):
        character = value[index]
        if quoted:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
            continue
        if character == '"':
            quoted = True
            output.append(character)
            index += 1
            continue
        if value.startswith("new Map([])", index):
            output.append("{}")
            index += len("new Map([])")
            continue
        if value.startswith("undefined", index):
            before = value[index - 1] if index else ""
            after_index = index + len("undefined")
            after = value[after_index] if after_index < len(value) else ""
            if not (before.isalnum() or before in "_$") and not (after.isalnum() or after in "_$"):
                output.append("null")
                index = after_index
                continue
        output.append(character)
        index += 1
    return "".join(output)
