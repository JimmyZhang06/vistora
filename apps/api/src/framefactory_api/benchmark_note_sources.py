# ruff: noqa: RUF001 - Chinese punctuation is intentional in user-facing limitations.

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .benchmark_accounts import (
    BenchmarkAccountError,
    PublicMetric,
    _managed_browser_base_url,
    _RejectRedirects,
    metric_from_display,
    resolve_xiaohongshu_profile,
)
from .benchmark_note_identity import NoteIdentityError, click_verified_profile_note
from .browser_process import (
    BrowserProcessError,
    run_browser_operation,
    run_browser_operation_async,
)

_NOTE_ID = re.compile(r"^[0-9a-f]{24}$")
_NOTE_MAX_PENDING = 32
_NOTE_QUEUE_TIMEOUT_SECONDS = 30.0
_VIDEO_PROBE_SCRIPT = r"""element => {
  const source = element.currentSrc || element.src || '';
  let trustedOrigin = false;
  try {
    const url = new URL(source);
    const declaredSource = element.getAttribute('src') || source;
    const authority = declaredSource.match(/^https:\/\/([^/?#]+)/);
    trustedOrigin = url.protocol === 'https:' &&
      ['xhscdn.com', 'xhscdn.net'].some(suffix =>
        url.hostname === suffix || url.hostname.endsWith('.' + suffix)) &&
      !url.username && !url.password && !url.port &&
      !(authority && authority[1].includes(':'));
  } catch {}
  return {
    duration: Number.isFinite(element.duration) ? element.duration : null,
    width: element.videoWidth || null,
    height: element.videoHeight || null,
    source_available: Boolean(source),
    trusted_origin: trustedOrigin
  };
}"""


class BenchmarkNoteSourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkNoteSourceRequest(BenchmarkNoteSourceModel):
    platform: Literal["xiaohongshu"]
    profile_url: str = Field(min_length=1, max_length=1000)
    note_id: str = Field(pattern=r"^[0-9a-f]{24}$")

    @field_validator("profile_url")
    @classmethod
    def canonical_profile(cls, value: str) -> str:
        try:
            return resolve_xiaohongshu_profile(value).profile_url
        except BenchmarkAccountError as exc:
            raise ValueError("Use a canonical Xiaohongshu profile URL") from exc


class BenchmarkMediaProbe(BenchmarkNoteSourceModel):
    kind: Literal["video", "image"]
    video_available: bool
    image_count: int = Field(ge=0, le=100)
    duration_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    width: int | None = Field(default=None, ge=0, le=16_384)
    height: int | None = Field(default=None, ge=0, le=16_384)
    trusted_media_origin: bool


class BenchmarkNoteSourceEvidence(BenchmarkNoteSourceModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    platform: Literal["xiaohongshu"] = "xiaohongshu"
    profile_user_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    note_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    canonical_url: str
    captured_at: datetime
    acquisition_method: Literal["authenticated_managed_browser"] = "authenticated_managed_browser"
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    likes: PublicMetric
    collects: PublicMetric
    comments: PublicMetric
    media: BenchmarkMediaProbe
    limitations: list[str] = Field(min_length=1, max_length=16)


class BenchmarkNoteProvider(Protocol):
    platform: str

    def collect(self, profile_url: str, note_id: str) -> BenchmarkNoteSourceEvidence: ...


class BenchmarkNoteSourceGateway:
    """Async boundary for authenticated note evidence providers."""

    def __init__(self, provider: BenchmarkNoteProvider) -> None:
        self._provider = provider
        self._lock = asyncio.Lock()
        self._pending = 0

    async def collect(
        self, platform: str, profile_url: str, note_id: str
    ) -> BenchmarkNoteSourceEvidence:
        if platform.strip().lower() != self._provider.platform:
            raise BenchmarkAccountError(
                "BENCHMARK_PLATFORM_UNSUPPORTED",
                f"Benchmark note collection is not available for platform {platform}",
            )
        if self._pending >= _NOTE_MAX_PENDING:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_BUSY", "Note collection is busy; retry later", retryable=True,
            )
        self._pending += 1
        try:
            async with asyncio.timeout(_NOTE_QUEUE_TIMEOUT_SECONDS):
                await self._lock.acquire()
        except TimeoutError as exc:
            self._pending -= 1
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_BUSY", "Note collection queue timed out", retryable=True,
            ) from exc
        except BaseException:
            self._pending -= 1
            raise
        try:
            if isinstance(self._provider, XiaohongshuAuthenticatedNoteProvider):
                # This await does not release the lock until cancellation has
                # terminated and reaped the isolated client and its driver.
                return await self._provider.collect_async(profile_url, note_id)
            task = asyncio.create_task(asyncio.to_thread(
                self._provider.collect, profile_url, note_id,
            ))
            cancelled = False
            while True:
                try:
                    evidence = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    # A disconnected request cannot stop the browser thread. Even
                    # repeated cancellation must keep its slot until that thread exits.
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            if cancelled:
                raise asyncio.CancelledError
            return evidence
        finally:
            self._pending -= 1
            self._lock.release()


class XiaohongshuAuthenticatedNoteProvider:
    """Collect a sanitized note-detail evidence envelope from a signed-in browser.

    The short-lived xsec token and signed CDN URL are used only inside the browser
    session. Neither value crosses this provider boundary.
    """

    platform = "xiaohongshu"

    def __init__(
        self,
        *,
        managed_browser_base_url: str,
        timeout_seconds: float = 18.0,
        max_scrolls: int = 12,
    ) -> None:
        self._base_url = _managed_browser_base_url(managed_browser_base_url)
        self._timeout_seconds = max(5.0, min(timeout_seconds, 60.0))
        self._max_scrolls = max(0, min(max_scrolls, 30))

    def collect(self, profile_url: str, note_id: str) -> BenchmarkNoteSourceEvidence:
        result = self._isolated_collect(profile_url, note_id)
        return BenchmarkNoteSourceEvidence.model_validate(result)

    async def collect_async(self, profile_url: str, note_id: str) -> BenchmarkNoteSourceEvidence:
        try:
            result = await run_browser_operation_async(
                "note", self._operation_arguments(profile_url, note_id), timeout_seconds=115,
            )
        except BrowserProcessError:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE", "Note collection timed out or was unavailable",
                retryable=True,
            ) from None
        return BenchmarkNoteSourceEvidence.model_validate(result)

    def acquire_media(
        self, profile_url: str, note_id: str, destination: Path, *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Acquire one video into caller-owned isolated research storage.

        Source-supplied media URLs are ephemeral in-process values. No credentials
        are exported from the browser and no signed URL is returned or persisted.
        """
        return self._isolated_collect(
            profile_url, note_id, destination=destination, cancel_event=cancel_event,
        )

    def _operation_arguments(
        self, profile_url: str, note_id: str, destination: Path | None = None,
    ) -> dict[str, Any]:
        identity = resolve_xiaohongshu_profile(profile_url)
        if not _NOTE_ID.fullmatch(note_id):
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_INVALID", "The note ID must be 24 lowercase hexadecimal characters",
            )
        return {
            "provider": {
                "managed_browser_base_url": self._base_url,
                "timeout_seconds": self._timeout_seconds, "max_scrolls": self._max_scrolls,
            },
            "profile_url": identity.profile_url, "note_id": note_id,
            "destination": str(destination.resolve()) if destination is not None else None,
        }

    def _isolated_collect(
        self, profile_url: str, note_id: str, *, destination: Path | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        try:
            return run_browser_operation(
                "note", self._operation_arguments(profile_url, note_id, destination),
                timeout_seconds=115, cancel_event=cancel_event,
            )
        except BrowserProcessError as exc:
            raise BenchmarkAccountError(
                "BENCHMARK_MEDIA_CANCELLED" if exc.cancelled else "BENCHMARK_PROVIDER_UNAVAILABLE",
                "Note collection was cancelled, timed out or was unavailable", retryable=True,
            ) from None

    def _collect(
        self, profile_url: str, note_id: str, *, destination: Path | None = None
    ) -> BenchmarkNoteSourceEvidence | dict[str, Any]:
        deadline = time.monotonic() + 110
        identity = resolve_xiaohongshu_profile(profile_url)
        if not _NOTE_ID.fullmatch(note_id):
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_INVALID", "The note ID must be 24 lowercase hexadecimal characters"
            )
        cdp_port = self._read_cdp_port()
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "Playwright is required for authenticated note collection",
            ) from exc

        timeout_ms = round(self._timeout_seconds * 1000)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{cdp_port}", timeout=timeout_ms
                )
                if not browser.contexts:
                    raise BenchmarkAccountError(
                        "BENCHMARK_AUTHENTICATION_REQUIRED",
                        "The local Xiaohongshu browser has no active profile",
                        retryable=True,
                    )
                page = browser.contexts[0].new_page()
                page.set_default_timeout(min(8_000, timeout_ms))
                trusted_media_seen = {"value": False}

                def observe_media_origin(response: Any) -> None:
                    parsed = urlsplit(response.url)
                    if _is_trusted_media_url(parsed) and response.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0] in {"video/mp4", "video/webm", "application/octet-stream"}:
                        trusted_media_seen["value"] = True

                try:
                    self._open_profile(page, identity.profile_url, timeout_ms)
                    page.on("response", observe_media_origin)
                    self._open_note(page, identity.external_user_id, note_id, timeout_ms)
                    evidence = self._extract_evidence(
                        page,
                        profile_user_id=identity.external_user_id,
                        note_id=note_id,
                        trusted_media_seen=trusted_media_seen["value"],
                    )
                    if destination is None:
                        return evidence
                    media = page.evaluate(
                        """noteId => {
                          const n = window.__INITIAL_STATE__?.note?.noteDetailMap?.[noteId]?.note;
                          if (!n) return null;
                          return {id:n.noteId, author:n.user?.userId, type:n.type,
                            streams:Object.values(n.video?.media?.stream || {}).flat()
                              .map(s=>({url:s.masterUrl,size:s.size,duration:s.duration,
                                width:s.width,height:s.height,format:s.format}))};
                        }""",
                        note_id,
                    )
                    stream = _select_video_stream(media, identity.external_user_id, note_id)
                    downloaded = _download_research_video(
                        stream["url"], destination, deadline=deadline
                    )
                    return {**evidence.model_dump(mode="json"), **downloaded}
                finally:
                    page.close()
        except BenchmarkAccountError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError):
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "The authenticated browser could not collect the note detail",
                retryable=True,
            ) from None

    def _read_cdp_port(self) -> int:
        request = Request(
            f"{self._base_url}/browser/managed/status",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with build_opener(_RejectRedirects()).open(
                request, timeout=self._timeout_seconds
            ) as response:
                payload = response.read(65_537)
        except (HTTPError, TimeoutError, URLError) as exc:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_UNAVAILABLE",
                "The local authenticated browser provider is unavailable",
                retryable=True,
            ) from exc
        if len(payload) > 65_536:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_INVALID",
                "The local browser provider returned an oversized status response",
            )
        try:
            status = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BenchmarkAccountError(
                "BENCHMARK_PROVIDER_INVALID",
                "The local browser provider returned invalid status data",
            ) from exc
        port = status.get("cdp_port") if isinstance(status, dict) else None
        if (
            not isinstance(port, int)
            or not 1 <= port <= 65_535
            or status.get("cdp_host") != "127.0.0.1"
            or status.get("state") != "running"
        ):
            raise BenchmarkAccountError(
                "BENCHMARK_AUTHENTICATION_REQUIRED",
                "Start and sign in to the local Xiaohongshu browser provider",
                retryable=True,
            )
        return port

    def _open_profile(self, page: Any, profile_url: str, timeout_ms: int) -> None:
        response = page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(min(4_000, timeout_ms // 3))
        current = urlsplit(page.url)
        if response is not None and response.status == 401:
            raise BenchmarkAccountError(
                "BENCHMARK_AUTHENTICATION_REQUIRED",
                "Sign in again to the local Xiaohongshu browser provider",
                retryable=True,
            )
        if response is not None and response.status in {403, 404, 410}:
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_INACCESSIBLE",
                "The profile is unavailable or access is restricted",
                retryable=True,
            )
        if current.hostname != "www.xiaohongshu.com" or current.path != urlsplit(profile_url).path:
            raise BenchmarkAccountError(
                (
                    "BENCHMARK_AUTHENTICATION_REQUIRED"
                    if current.path.rstrip("/") == "/login"
                    else "BENCHMARK_PROFILE_MISMATCH"
                ),
                "The local browser did not resolve to the requested profile",
                retryable=True,
            )
        if response is not None and response.status != 200:
            raise BenchmarkAccountError(
                "BENCHMARK_SOURCE_UNAVAILABLE",
                f"Xiaohongshu returned HTTP {response.status} for the profile",
                retryable=response.status == 429 or response.status >= 500,
            )

    def _open_note(self, page: Any, profile_user_id: str, note_id: str, timeout_ms: int) -> None:
        clicked = False
        for attempt in range(self._max_scrolls + 1):
            try:
                clicked = click_verified_profile_note(page, profile_user_id, note_id)
            except NoteIdentityError as exc:
                raise BenchmarkAccountError(exc.code, str(exc)) from None
            if clicked:
                break
            if attempt < self._max_scrolls:
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(650)
        if not clicked:
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_NOT_IN_SAMPLE",
                "The note was not found within the bounded profile scan",
            )
        # The helper clicks inside the page: signed hrefs stay browser-local and
        # a missing token is not treated as missing identity.
        page.wait_for_url(
            lambda url: urlsplit(str(url)).path in {f"/explore/{note_id}", "/login"},
            timeout=timeout_ms,
        )
        page.wait_for_timeout(min(4_000, timeout_ms // 3))
        current = urlsplit(page.url)
        if current.hostname == "www.xiaohongshu.com" and current.path == "/login":
            raise BenchmarkAccountError(
                "BENCHMARK_AUTHENTICATION_REQUIRED",
                "Sign in again to the local Xiaohongshu browser provider",
                retryable=True,
            )
        if (
            current.scheme != "https"
            or current.hostname != "www.xiaohongshu.com"
            or current.path != f"/explore/{note_id}"
        ):
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_MISMATCH",
                "The browser did not resolve to the requested note",
            )

    def _extract_evidence(
        self,
        page: Any,
        *,
        profile_user_id: str,
        note_id: str,
        trusted_media_seen: bool,
    ) -> BenchmarkNoteSourceEvidence:
        identity = page.evaluate(
            """noteId => {
              const note = window.__INITIAL_STATE__?.note?.noteDetailMap?.[noteId]?.note;
              if (!note) return null;
              return {note_id: note.noteId, profile_user_id: note.user?.userId, type: note.type};
            }""",
            note_id,
        )
        if (
            not isinstance(identity, dict)
            or not identity.get("note_id")
            or not identity.get("profile_user_id")
        ):
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_FIELDS_INSUFFICIENT",
                "The note page did not provide a verifiable note identity and author",
                retryable=True,
            )
        if identity["note_id"] != note_id or identity["profile_user_id"] != profile_user_id:
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_MISMATCH",
                "The note detail does not belong to the requested note and account",
            )
        title = _first_text(page, "h1.title")
        if not title:
            raise BenchmarkAccountError(
                "BENCHMARK_SOURCE_CHANGED",
                "The note title was not present in the authenticated page",
                retryable=True,
            )
        description = _first_text(page, ".note-content .note-text")
        interactions = page.locator(".interactions.engage-bar").first
        likes = _child_text(interactions, ".like-wrapper .count")
        collects = _child_text(interactions, ".collect-wrapper .count")
        comments = _child_text(interactions, ".chat-wrapper .count")

        video = page.locator("video").first
        if identity.get("type") == "video":
            raw_probe = video.evaluate(_VIDEO_PROBE_SCRIPT) if video.count() else {}
            if not isinstance(raw_probe, dict):
                raw_probe = {}
            trusted_origin = trusted_media_seen or raw_probe.get("trusted_origin") is True
            duration = raw_probe.get("duration") if isinstance(raw_probe, dict) else None
            media = BenchmarkMediaProbe(
                kind="video",
                video_available=raw_probe.get("source_available") is True,
                image_count=0,
                duration_ms=(round(float(duration) * 1000) if duration is not None else None),
                width=_safe_int(raw_probe.get("width")),
                height=_safe_int(raw_probe.get("height")),
                trusted_media_origin=trusted_origin,
            )
        elif identity.get("type") == "normal":
            image_count = page.locator(".note-slider img, .swiper-slide img").evaluate_all(
                "elements => Math.min(100, "
                "new Set(elements.map(item => item.currentSrc || item.src)"
                ".filter(Boolean)).size)"
            )
            media = BenchmarkMediaProbe(
                kind="image",
                video_available=False,
                image_count=_safe_int(image_count) or 0,
                trusted_media_origin=True,
            )
        else:
            raise BenchmarkAccountError(
                "BENCHMARK_NOTE_FIELDS_INSUFFICIENT",
                "The note page did not provide a supported media type",
                retryable=True,
            )

        return BenchmarkNoteSourceEvidence(
            profile_user_id=profile_user_id,
            note_id=note_id,
            canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            captured_at=datetime.now(UTC),
            title=title[:300],
            description=description[:10_000],
            likes=metric_from_display(likes),
            collects=metric_from_display(collects),
            comments=metric_from_display(comments),
            media=media,
            limitations=[
                "详情证据来自用户已登录的本机受管浏览器，不代表平台官方数据接口。",
                "只返回页面可见正文、互动下界和媒体探针；"
                "不返回身份凭据、短期访问令牌或签名媒体 URL。",
                "评论正文、推荐流和私密内容不进入证据包。",
            ],
        )


def _first_text(page: Any, selector: str) -> str:
    locator = page.locator(selector)
    if not locator.count():
        return ""
    return (locator.first.inner_text() or "").strip()


def _child_text(parent: Any, selector: str) -> str:
    locator = parent.locator(selector)
    if not locator.count():
        return ""
    return (locator.first.inner_text() or "").strip()


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _is_trusted_media_url(parsed: Any) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    return bool(
        parsed.scheme == "https"
        and hostname
        and any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in ("xhscdn.com", "xhscdn.net")
        )
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _select_video_stream(media: Any, profile_user_id: str, note_id: str) -> dict[str, Any]:
    if (
        not isinstance(media, dict)
        or media.get("id") != note_id
        or media.get("author") != profile_user_id
    ):
        raise BenchmarkAccountError(
            "BENCHMARK_NOTE_MISMATCH", "The media does not belong to the requested note and account"
        )
    if media.get("type") != "video":
        raise BenchmarkAccountError(
            "BENCHMARK_MEDIA_UNSUPPORTED", "Full video analysis requires a video note"
        )
    candidates = []
    for stream in media.get("streams", [])[:32]:
        if not isinstance(stream, dict) or stream.get("format") != "mp4":
            continue
        try:
            parsed = urlsplit(str(stream.get("url", "")))
            # XHS page state advertises HTTP CDN locators; require TLS on download.
            if parsed.scheme == "http":
                parsed = parsed._replace(scheme="https")
            if not _is_trusted_media_url(parsed) or parsed.fragment:
                continue
            duration = float(stream.get("duration", 0))
            size = int(stream.get("size", 0))
            height = int(stream.get("height", 0))
            if not 0 < duration <= 600_000 or not 0 < size <= 209_715_200:
                continue
            candidates.append({**stream, "url": parsed.geturl(), "height": height, "size": size})
        except (TypeError, ValueError):
            continue
    if not candidates:
        raise BenchmarkAccountError(
            "BENCHMARK_MEDIA_UNAVAILABLE",
            "No supported video stream within the ten-minute and 200 MiB limits was found",
        )
    # Prefer 1080p for OCR, then the smallest suitable transfer. Never pick a 4K
    # stream merely because the platform marked it as the playback default.
    return min(candidates, key=lambda s: (abs(s["height"] - 1080), s["size"]))


class _PinnedMediaConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, *, timeout: float) -> None:
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        connection = socket.create_connection((self._address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except Exception:
            connection.close()
            raise


def _download_research_video(
    url: str, destination: Path, *, deadline: float, maximum_bytes: int = 209_715_200
) -> dict[str, Any]:
    """TLS and public-IP pinned streaming download, without redirects or cookies."""
    parsed = urlsplit(url)
    if not _is_trusted_media_url(parsed) or parsed.fragment:
        raise BenchmarkAccountError("BENCHMARK_MEDIA_INVALID", "Unsupported media origin")
    destination = destination.resolve()
    if destination.exists():
        raise BenchmarkAccountError("BENCHMARK_MEDIA_EXISTS", "Research destination already exists")
    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise BenchmarkAccountError(
                "BENCHMARK_MEDIA_INVALID", "Media host is not publicly routed"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        host = str(parsed.hostname)
        connection = _PinnedMediaConnection(host, sorted(addresses)[0], timeout=min(20, remaining))
        try:
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "video/mp4,application/octet-stream",
                    "Referer": "https://www.xiaohongshu.com/",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if response.status != 200 or content_type not in {
                "video/mp4",
                "application/octet-stream",
                "binary/octet-stream",
            }:
                raise BenchmarkAccountError(
                    "BENCHMARK_MEDIA_REJECTED",
                    "Media origin rejected the bounded video download",
                    retryable=response.status == 429 or response.status >= 500,
                )
            declared_size = int(response.getheader("Content-Length", "0"))
            if declared_size > maximum_bytes:
                raise BenchmarkAccountError("BENCHMARK_MEDIA_TOO_LARGE", "Video exceeds 200 MiB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".part")
            temporary_created = False
            digest = hashlib.sha256()
            count = 0
            try:
                with temporary.open("xb") as output:
                    temporary_created = True
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError()
                        if connection.sock is not None:
                            connection.sock.settimeout(min(10, remaining))
                        chunk = response.read(65_536)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > maximum_bytes:
                            raise BenchmarkAccountError(
                                "BENCHMARK_MEDIA_TOO_LARGE", "Video exceeds 200 MiB"
                            )
                        if count == len(chunk) and (len(chunk) < 12 or chunk[4:8] != b"ftyp"):
                            raise BenchmarkAccountError(
                                "BENCHMARK_MEDIA_INVALID", "Downloaded bytes are not an MP4 video"
                            )
                        output.write(chunk)
                        digest.update(chunk)
                if not count or (declared_size and count != declared_size):
                    raise BenchmarkAccountError(
                        "BENCHMARK_MEDIA_INCOMPLETE",
                        "Video download was incomplete",
                        retryable=True,
                    )
                temporary.replace(destination)
            finally:
                if temporary_created:
                    temporary.unlink(missing_ok=True)
        finally:
            connection.close()
    except BenchmarkAccountError:
        raise
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:
        raise BenchmarkAccountError(
            "BENCHMARK_MEDIA_UNAVAILABLE", "Video download could not complete", retryable=True
        ) from exc
    return {"sha256": digest.hexdigest(), "byte_size": count, "media_type": "video/mp4"}
