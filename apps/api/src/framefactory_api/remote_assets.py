from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import mimetypes
import re
import socket
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

RemotePlatform = Literal["youtube", "bilibili", "rednote", "wikimedia"]

_ALLOWED_HOSTS: dict[RemotePlatform, frozenset[str]] = {
    "youtube": frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}),
    "bilibili": frozenset({"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}),
    "rednote": frozenset(
        {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}
    ),
    "wikimedia": frozenset({"upload.wikimedia.org"}),
}
_VIDEO_SUFFIXES = {
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_PUBLIC_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
)
_MUTUALLY_EXCLUSIVE_EVENT_ENTITIES = (
    "奥运会",
    "世界杯",
    "世乒赛",
    "世锦赛",
    "亚运会",
    "全运会",
)
_SEARCH_YEAR = re.compile(r"(?:19|20)\d{2}")
_LATIN_OR_NUMERIC_QUERY_TERM = re.compile(
    r"[A-Za-z][A-Za-z0-9-]{1,40}|[0-9]{1,10}"
)
_ALPHA_NUMERIC_ENTITY = re.compile(r"\b([a-z]{2,40})\s+([0-9]{1,10})\b")


class RemoteAssetError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DownloadedAsset:
    path: Path
    platform: RemotePlatform
    source_url: str
    canonical_url: str
    external_id: str | None
    title: str
    description: str
    author: str | None
    license_name: str | None
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    duration_seconds: float | None
    subtitle_languages: tuple[str, ...] = ()
    rights_evidence_type: str | None = None
    rights_evidence_locator: str | None = None
    rights_verified_at: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteSearchResult:
    platform: Literal["youtube", "bilibili", "wikimedia"]
    source_url: str
    external_id: str | None
    title: str
    author: str | None
    duration_seconds: float | None


class _PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class RemoteAssetGateway:
    """Download public, user-authorized media without cookies or access-control bypasses."""

    def __init__(
        self,
        *,
        maximum_bytes: int = 524_288_000,
        maximum_duration_seconds: int = 3600,
        timeout_seconds: int = 600,
        rednote_endpoint: str = "https://rednote-downloader.online/api/check",
    ) -> None:
        self.maximum_bytes = maximum_bytes
        self.maximum_duration_seconds = maximum_duration_seconds
        self.timeout_seconds = timeout_seconds
        self.rednote_endpoint = rednote_endpoint

    async def download(self, source_url: str, destination: Path) -> DownloadedAsset:
        platform = classify_source(source_url)
        destination.mkdir(parents=True, exist_ok=True)
        if platform == "rednote":
            return await asyncio.to_thread(self._download_rednote, source_url, destination)
        if platform == "wikimedia":
            return await asyncio.to_thread(self._download_wikimedia, source_url, destination)
        return await self._download_with_ytdlp(platform, source_url, destination)

    async def search(
        self,
        platform: Literal["youtube", "bilibili", "wikimedia"],
        query: str,
        *,
        limit: int = 3,
    ) -> tuple[RemoteSearchResult, ...]:
        cleaned = query.strip()
        if not cleaned or not 1 <= limit <= 10:
            raise RemoteAssetError("REMOTE_SEARCH_INVALID", "Search query or limit is invalid")
        if platform == "wikimedia":
            return await asyncio.to_thread(self._search_wikimedia, cleaned, limit)
        prefix = "ytsearch" if platform == "youtube" else "bilisearch"
        search_limit = min(10, max(5, limit * 3))
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--skip-download",
            "--no-warnings",
            "--quiet",
            "--no-cookies-from-browser",
            "--socket-timeout",
            "20",
            "--playlist-end",
            str(search_limit),
            "--user-agent",
            _PUBLIC_BROWSER_USER_AGENT,
            "--referer",
            f"https://www.{platform}.com/" if platform == "youtube" else "https://www.bilibili.com/",
            "--dump-json",
            f"{prefix}{search_limit}:{cleaned}",
        ]
        if platform == "youtube":
            command.insert(3, "--flat-playlist")
        try:
            process = await asyncio.create_subprocess_exec(
                *tuple(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(), timeout=min(self.timeout_seconds, 90)
            )
        except OSError as exc:
            raise RemoteAssetError(
                "REMOTE_DOWNLOADER_UNAVAILABLE",
                "The configured video search provider could not be started",
                retryable=True,
            ) from exc
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RemoteAssetError(
                "REMOTE_SEARCH_TIMEOUT", "Remote video search timed out", retryable=True
            ) from exc
        if process.returncode != 0:
            raise RemoteAssetError(
                "REMOTE_SEARCH_FAILED", "Remote platform search failed", retryable=True
            )
        candidates = parse_search_results(platform, stdout, limit=search_limit)
        ranked = sorted(
            ((_query_relevance(cleaned, item.title), item) for item in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return tuple(item for score, item in ranked if score >= 0.5)[:limit]

    def _search_wikimedia(
        self, query: str, limit: int
    ) -> tuple[RemoteSearchResult, ...]:
        """Search only Commons videos whose metadata declares public-domain use."""

        search_query = _wikimedia_search_query(query)
        parameters = urlencode(
            {
                "action": "query",
                "generator": "search",
                "gsrsearch": f"{search_query} filetype:video",
                "gsrnamespace": "6",
                "gsrlimit": str(min(30, max(10, limit * 4))),
                "prop": "videoinfo",
                "viprop": "url|mime|size|extmetadata|derivatives",
                "format": "json",
                "origin": "*",
            }
        )
        request = Request(
            f"https://commons.wikimedia.org/w/api.php?{parameters}",
            headers={"User-Agent": "Vistora/1.0 (public-domain media search)"},
        )
        try:
            with build_opener(_PublicRedirectHandler()).open(request, timeout=30) as response:
                body = response.read(4_194_305)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RemoteAssetError(
                "REMOTE_SEARCH_FAILED",
                "Wikimedia Commons search failed",
                retryable=True,
            ) from exc
        if len(body) > 4_194_304:
            raise RemoteAssetError("REMOTE_SEARCH_FAILED", "Wikimedia response is too large")
        try:
            payload = json.loads(body)
            pages = payload.get("query", {}).get("pages", {})
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise RemoteAssetError("REMOTE_SEARCH_FAILED", "Wikimedia response is invalid") from exc
        results: list[RemoteSearchResult] = []
        for page in pages.values() if isinstance(pages, Mapping) else ():
            if not isinstance(page, Mapping):
                continue
            image_info = page.get("videoinfo")
            info = image_info[0] if isinstance(image_info, list) and image_info else None
            if not isinstance(info, Mapping):
                continue
            metadata = info.get("extmetadata", {})
            if not _wikimedia_declares_public_domain(metadata):
                continue
            duration = _optional_number(info.get("duration"))
            source_url, media_type, byte_size = _wikimedia_derivative(info, duration)
            if (
                not media_type.startswith("video/")
                or not source_url
                or byte_size <= 0
                or byte_size > self.maximum_bytes
                or (duration is not None and duration > self.maximum_duration_seconds)
            ):
                continue
            try:
                if classify_source(source_url) != "wikimedia":
                    continue
            except RemoteAssetError:
                continue
            title = str(page.get("title") or "Wikimedia Commons video").removeprefix("File:")
            results.append(
                RemoteSearchResult(
                    platform="wikimedia",
                    source_url=source_url,
                    external_id=str(page.get("pageid") or "") or None,
                    title=title[:300],
                    author=_metadata_value(metadata, "Artist")[:500] or "Wikimedia Commons",
                    duration_seconds=duration,
                )
            )
        ranked = sorted(
            ((_query_relevance(search_query, item.title), item) for item in results),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return tuple(item for score, item in ranked if score >= 0.34)[:limit]

    def _download_wikimedia(self, source_url: str, destination: Path) -> DownloadedAsset:
        evidence = self._verify_wikimedia_public_domain(source_url)
        filename = _safe_filename(unquote(Path(urlparse(source_url).path).name))
        path = destination / filename
        final_url, content_type = self._stream_download(source_url, path)
        return self._describe_download(
            path,
            platform="wikimedia",
            source_url=source_url,
            metadata={
                "id": hashlib.sha256(source_url.encode()).hexdigest()[:20],
                "title": evidence["title"],
                "webpage_url": evidence["locator"],
                "url": final_url,
                "uploader": evidence["attribution"],
                "license": evidence["license"],
                "content_type": content_type,
                "rights_evidence_type": "verified_public_domain",
                "rights_evidence_locator": evidence["locator"],
                "rights_verified_at": evidence["verified_at"],
            },
        )

    def _verify_wikimedia_public_domain(self, source_url: str) -> dict[str, str]:
        """Re-verify the exact Commons file before assigning trusted rights."""

        filename = _wikimedia_original_filename(source_url)
        parameters = urlencode(
            {
                "action": "query",
                "titles": f"File:{filename}",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata",
                "format": "json",
                "origin": "*",
            }
        )
        request = Request(
            f"https://commons.wikimedia.org/w/api.php?{parameters}",
            headers={"User-Agent": "Vistora/1.0 (public-domain rights verification)"},
        )
        try:
            with build_opener(_PublicRedirectHandler()).open(
                request, timeout=30
            ) as response:
                body = response.read(4_194_305)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RemoteAssetError(
                "REMOTE_RIGHTS_VERIFICATION_UNAVAILABLE",
                "Wikimedia rights metadata could not be verified",
                retryable=True,
            ) from exc
        if len(body) > 4_194_304:
            raise RemoteAssetError(
                "REMOTE_RIGHTS_VERIFICATION_UNAVAILABLE",
                "Wikimedia rights metadata response is too large",
                retryable=True,
            )
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteAssetError(
                "REMOTE_RIGHTS_VERIFICATION_UNAVAILABLE",
                "Wikimedia rights metadata response is invalid",
                retryable=True,
            ) from exc
        return _wikimedia_public_domain_evidence(
            payload,
            expected_filename=filename,
            verified_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    async def _download_with_ytdlp(
        self, platform: RemotePlatform, source_url: str, destination: Path
    ) -> DownloadedAsset:
        output_template = str(destination / "%(id)s.%(ext)s")
        command = (
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--no-cookies-from-browser",
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--user-agent",
            _PUBLIC_BROWSER_USER_AGENT,
            "--referer",
            (
                "https://www.youtube.com/"
                if platform == "youtube"
                else "https://www.bilibili.com/"
            ),
            "--max-filesize",
            str(self.maximum_bytes),
            "--match-filter",
            f"!is_live & duration <= {self.maximum_duration_seconds}",
            "--merge-output-format",
            "mp4",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "zh.*,en.*,ja.*,ko.*",
            "--sub-format",
            "vtt/best",
            "--convert-subs",
            "vtt",
            "--embed-subs",
            "--format",
            "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]",
            "--output",
            output_template,
            "--print-json",
            source_url,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RemoteAssetError(
                "REMOTE_DOWNLOADER_UNAVAILABLE",
                "The configured video downloader could not be started",
                retryable=True,
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RemoteAssetError(
                "REMOTE_DOWNLOAD_TIMEOUT", "The remote video download timed out", retryable=True
            ) from exc
        if process.returncode != 0:
            raise _ytdlp_download_error(stderr)

        metadata = _last_json_object(stdout.decode("utf-8", errors="replace"))
        media_path = _find_downloaded_video(destination)
        return self._describe_download(
            media_path,
            platform=platform,
            source_url=source_url,
            metadata=metadata,
        )

    def _download_rednote(self, source_url: str, destination: Path) -> DownloadedAsset:
        payload = json.dumps({"url": source_url}).encode("utf-8")
        request = Request(
            self.rednote_endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Vistora/1.0"},
            method="POST",
        )
        try:
            with build_opener(_PublicRedirectHandler()).open(request, timeout=30) as response:
                body = response.read(1_048_577)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RemoteAssetError(
                "REDNOTE_PROVIDER_UNAVAILABLE",
                "The configured RedNote resolver is unavailable",
                retryable=True,
            ) from exc
        if len(body) > 1_048_576:
            raise RemoteAssetError(
                "REDNOTE_RESPONSE_INVALID", "RedNote resolver response is too large"
            )
        result = parse_rednote_response(body)
        videos = result.get("videoList")
        if not isinstance(videos, list) or not videos or not isinstance(videos[0], str):
            raise RemoteAssetError("REDNOTE_VIDEO_NOT_FOUND", "The RedNote post has no video")
        media_url = videos[0]
        _validate_public_https_url(media_url)
        external_id = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:20]
        path = destination / f"rednote-{external_id}.mp4"
        final_url, content_type = self._stream_download(media_url, path)
        metadata: dict[str, Any] = {
            "id": external_id,
            "title": result.get("title") or result.get("desc") or "RedNote video",
            "description": result.get("desc") or "",
            "webpage_url": source_url,
            "url": final_url,
            "thumbnail": result.get("coverImage"),
            "content_type": content_type,
        }
        return self._describe_download(
            path, platform="rednote", source_url=source_url, metadata=metadata
        )

    def _stream_download(self, media_url: str, destination: Path) -> tuple[str, str | None]:
        request = Request(media_url, headers={"User-Agent": "Vistora/1.0"})
        try:
            with build_opener(_PublicRedirectHandler()).open(request, timeout=60) as response:
                final_url = response.geturl()
                _validate_public_https_url(final_url)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > self.maximum_bytes:
                    raise RemoteAssetError("REMOTE_ASSET_TOO_LARGE", "Remote video exceeds 500 MB")
                total = 0
                with destination.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > self.maximum_bytes:
                            raise RemoteAssetError(
                                "REMOTE_ASSET_TOO_LARGE", "Remote video exceeds 500 MB"
                            )
                        output.write(chunk)
                return final_url, response.headers.get_content_type()
        except RemoteAssetError:
            destination.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            destination.unlink(missing_ok=True)
            raise RemoteAssetError(
                "REMOTE_DOWNLOAD_FAILED", "Could not download the resolved video", retryable=True
            ) from exc

    def _describe_download(
        self,
        path: Path,
        *,
        platform: RemotePlatform,
        source_url: str,
        metadata: Mapping[str, Any],
    ) -> DownloadedAsset:
        if not path.is_file() or path.is_symlink():
            raise RemoteAssetError("REMOTE_DOWNLOAD_FAILED", "Downloader produced no regular file")
        byte_size = path.stat().st_size
        if byte_size <= 0 or byte_size > self.maximum_bytes:
            raise RemoteAssetError("REMOTE_ASSET_TOO_LARGE", "Remote video has an invalid size")
        duration = _optional_number(metadata.get("duration"))
        if duration is not None and duration > self.maximum_duration_seconds:
            raise RemoteAssetError("REMOTE_ASSET_TOO_LONG", "Remote video exceeds one hour")
        suffix = path.suffix.lower()
        media_type = _VIDEO_SUFFIXES.get(suffix) or mimetypes.guess_type(path.name)[0]
        if media_type is None or not media_type.startswith("video/"):
            raise RemoteAssetError("REMOTE_MEDIA_UNSUPPORTED", "Downloaded file is not a video")
        digest = _sha256_file(path)
        title = str(metadata.get("title") or path.stem).strip()[:300]
        canonical = str(metadata.get("webpage_url") or metadata.get("original_url") or source_url)
        return DownloadedAsset(
            path=path,
            platform=platform,
            source_url=source_url,
            canonical_url=canonical,
            external_id=_optional_text(metadata.get("id")),
            title=title or "Remote video",
            description=str(metadata.get("description") or "").strip()[:2000],
            author=_optional_text(metadata.get("uploader") or metadata.get("channel")),
            license_name=_optional_text(metadata.get("license")),
            filename=_safe_filename(path.name),
            media_type=media_type,
            byte_size=byte_size,
            sha256=digest,
            duration_seconds=duration,
            subtitle_languages=_subtitle_languages(metadata),
            rights_evidence_type=_optional_text(
                metadata.get("rights_evidence_type")
            ),
            rights_evidence_locator=_optional_text(
                metadata.get("rights_evidence_locator")
            ),
            rights_verified_at=_optional_text(metadata.get("rights_verified_at")),
        )


def _ytdlp_download_error(stderr: bytes) -> RemoteAssetError:
    """Classify provider policy/auth failures without leaking downloader output."""

    message = stderr.decode("utf-8", errors="replace").casefold()
    if any(
        marker in message
        for marker in (
            "sign in to confirm you\u2019re not a bot",
            "sign in to confirm you're not a bot",
            "use --cookies-from-browser",
            "login required",
            "this video is private",
        )
    ):
        return RemoteAssetError(
            "REMOTE_AUTH_REQUIRED",
            "Remote provider requires explicitly configured authentication",
            retryable=False,
        )
    return RemoteAssetError(
        "REMOTE_DOWNLOAD_FAILED",
        "Remote provider rejected or could not download this public video",
        retryable=True,
    )


def classify_source(source_url: str) -> RemotePlatform:
    parsed = urlparse(source_url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteAssetError(
            "REMOTE_SOURCE_INVALID", "Source URL must be a public HTTPS video page"
        )
    host = parsed.hostname.lower().rstrip(".")
    for platform, hosts in _ALLOWED_HOSTS.items():
        if host in hosts:
            return platform
    raise RemoteAssetError(
        "REMOTE_SOURCE_UNSUPPORTED",
        "Only YouTube, Bilibili, Wikimedia Commons, and Xiaohongshu/RedNote links are supported",
    )


def parse_rednote_response(body: bytes) -> dict[str, Any]:
    try:
        value: Any = json.loads(body.decode("utf-8"))
        if isinstance(value, Mapping) and "data" in value:
            value = value["data"]
        for _ in range(3):
            if isinstance(value, str):
                value = json.loads(value)
        if not isinstance(value, Mapping) or value.get("code") not in {0, "0"}:
            raise ValueError
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RemoteAssetError(
            "REDNOTE_RESPONSE_INVALID", "RedNote resolver returned an unsupported response"
        ) from exc
    return dict(value)


def parse_search_results(
    platform: Literal["youtube", "bilibili"], output: bytes, *, limit: int
) -> tuple[RemoteSearchResult, ...]:
    results: list[RemoteSearchResult] = []
    seen: set[str] = set()
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        external_id = _optional_text(value.get("id"))
        candidate = _optional_text(value.get("webpage_url") or value.get("url"))
        if candidate and not candidate.startswith("http"):
            candidate = None
        if candidate and platform == "bilibili" and candidate.startswith("http://"):
            # yt-dlp's BiliBiliSearch extractor still emits an http canonical URL.
            # Normalize the known platform URL before the strict public-HTTPS gate.
            candidate = "https://" + candidate.removeprefix("http://")
        if candidate is None and external_id:
            candidate = (
                f"https://www.youtube.com/watch?v={external_id}"
                if platform == "youtube"
                else f"https://www.bilibili.com/video/{external_id}"
            )
        if candidate is None:
            continue
        try:
            if classify_source(candidate) != platform:
                continue
        except RemoteAssetError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        results.append(
            RemoteSearchResult(
                platform=platform,
                source_url=candidate,
                external_id=external_id,
                title=str(value.get("title") or external_id or "Remote video")[:300],
                author=_optional_text(value.get("uploader") or value.get("channel")),
                duration_seconds=_optional_number(value.get("duration")),
            )
        )
        if len(results) >= limit:
            break
    return tuple(results)


def _validate_public_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteAssetError("REMOTE_URL_UNSAFE", "Remote media URL must use public HTTPS")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RemoteAssetError(
            "REMOTE_HOST_UNAVAILABLE", "Remote media host could not be resolved", retryable=True
        ) from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise RemoteAssetError("REMOTE_URL_UNSAFE", "Remote media host is not public")


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return dict(value)
    raise RemoteAssetError("REMOTE_METADATA_INVALID", "Downloader returned no metadata")


def _find_downloaded_video(directory: Path) -> Path:
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES
    ]
    if not candidates:
        raise RemoteAssetError("REMOTE_DOWNLOAD_FAILED", "Downloader produced no video")
    return max(candidates, key=lambda value: value.stat().st_size)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(value: str) -> str:
    cleaned = _SAFE_FILENAME.sub("-", value).strip(".-")
    return (cleaned or "remote-video.mp4")[:255]


def _query_relevance(query: str, title: str) -> float:
    """Conservative lexical gate; returning no asset is safer than a wrong clip."""

    generic_terms = {
        "video",
        "footage",
        "clip",
        "match",
        "比赛",
        "视频",
        "素材",
        "画面",
        "table",
        "tennis",
        "乒乓球",
    }
    query_terms = [term for term in _search_terms(query) if term not in generic_terms]
    if not query_terms:
        query_terms = list(_search_terms(query))
    title_value = _normalized_search_text(title)
    if not query_terms or not title_value:
        return 0.0
    query_events = {
        entity for entity in _MUTUALLY_EXCLUSIVE_EVENT_ENTITIES if entity in query
    }
    title_events = {
        entity for entity in _MUTUALLY_EXCLUSIVE_EVENT_ENTITIES if entity in title
    }
    if query_events and not query_events.issubset(title_events):
        return 0.0
    query_years = set(_SEARCH_YEAR.findall(query))
    title_years = set(_SEARCH_YEAR.findall(title))
    if query_years and title_years and query_years.isdisjoint(title_years):
        return 0.0
    query_identifiers = _alpha_numeric_entities(query)
    title_identifiers = _alpha_numeric_entities(title)
    if any(
        title_numbers and query_numbers.isdisjoint(title_numbers)
        for entity, query_numbers in query_identifiers.items()
        if (title_numbers := title_identifiers.get(entity))
    ):
        return 0.0
    return sum(1 for term in query_terms if term in title_value) / len(query_terms)


def _alpha_numeric_entities(value: str) -> dict[str, frozenset[str]]:
    """Return generic named identifiers such as ``apollo 11`` or ``boeing 747``."""

    identifiers: dict[str, set[str]] = {}
    normalized = _normalized_search_text(value)
    for entity, number in _ALPHA_NUMERIC_ENTITY.findall(normalized):
        identifiers.setdefault(entity, set()).add(str(int(number)))
    return {entity: frozenset(numbers) for entity, numbers in identifiers.items()}


def _search_terms(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term for term in _normalized_search_text(value).split() if len(term) >= 2
        )
    )


def _subtitle_languages(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    requested = metadata.get("requested_subtitles")
    if not isinstance(requested, Mapping):
        return ()
    return tuple(
        sorted(
            {
                str(language).strip()[:32]
                for language, value in requested.items()
                if str(language).strip() and isinstance(value, Mapping)
            }
        )
    )


def _normalized_search_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.casefold()))


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:500] or None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _metadata_value(metadata: object, name: str) -> str:
    if not isinstance(metadata, Mapping):
        return ""
    value = metadata.get(name, {})
    return str(value.get("value") or "").strip() if isinstance(value, Mapping) else ""


def _wikimedia_declares_public_domain(metadata: object) -> bool:
    license_value = _metadata_value(metadata, "LicenseShortName").casefold()
    copyrighted = _metadata_value(metadata, "Copyrighted").casefold()
    if "not public domain" in license_value:
        return False
    return (
        license_value
        in {
            "public domain",
            "public domain mark",
            "public domain mark 1.0",
            "cc0",
            "cc0 1.0",
            "pdm",
            "pdm 1.0",
        }
        or copyrighted == "false"
    )


def _wikimedia_original_filename(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org":
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia rights can only be verified for an official upload URL",
        )
    parts = [unquote(part) for part in Path(parsed.path).parts if part not in {"/", "\\"}]
    try:
        transcode_index = parts.index("transcoded")
    except ValueError:
        filename = parts[-1] if parts else ""
    else:
        original_index = transcode_index + 3
        filename = parts[original_index] if original_index < len(parts) else ""
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia source does not identify an original Commons file",
        )
    return filename


def _wikimedia_public_domain_evidence(
    payload: object,
    *,
    expected_filename: str,
    verified_at: str,
) -> dict[str, str]:
    """Validate a Commons API response and return auditable trusted evidence."""

    try:
        pages = payload.get("query", {}).get("pages", {})  # type: ignore[union-attr]
    except AttributeError as exc:
        raise RemoteAssetError(
            "REMOTE_RIGHTS_VERIFICATION_UNAVAILABLE",
            "Wikimedia rights metadata response is invalid",
            retryable=True,
        ) from exc
    page = next(
        (item for item in pages.values() if isinstance(item, Mapping)),
        None,
    ) if isinstance(pages, Mapping) else None
    image_info = page.get("imageinfo") if isinstance(page, Mapping) else None
    info = image_info[0] if isinstance(image_info, list) and image_info else None
    if not isinstance(page, Mapping) or not isinstance(info, Mapping):
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia file has no verifiable rights metadata",
        )
    actual_title = str(page.get("title") or "").removeprefix("File:")
    normalize = lambda value: " ".join(  # noqa: E731 - compact canonical comparison
        unquote(str(value)).replace("_", " ").casefold().split()
    )
    if normalize(actual_title) != normalize(expected_filename):
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia rights metadata does not match the downloaded file",
        )
    original_url = str(info.get("url") or "")
    try:
        original_filename = _wikimedia_original_filename(original_url)
    except RemoteAssetError as exc:
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia metadata does not identify an official source file",
        ) from exc
    if normalize(original_filename) != normalize(expected_filename):
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia metadata source does not match the downloaded file",
        )
    metadata = info.get("extmetadata", {})
    if not _wikimedia_declares_public_domain(metadata):
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia file is not verified as public domain or CC0",
        )
    locator = str(info.get("descriptionurl") or "").strip()
    parsed_locator = urlparse(locator)
    if (
        parsed_locator.scheme != "https"
        or parsed_locator.hostname != "commons.wikimedia.org"
        or parsed_locator.username
        or parsed_locator.password
    ):
        raise RemoteAssetError(
            "REMOTE_RIGHTS_UNVERIFIED",
            "Wikimedia rights metadata has no canonical Commons evidence page",
        )
    license_name = _metadata_value(metadata, "LicenseShortName") or "Public domain"
    attribution = _metadata_value(metadata, "Artist") or "Wikimedia Commons"
    return {
        "title": actual_title[:300] or expected_filename[:300],
        "locator": locator[:2048],
        "license": license_name[:500],
        "attribution": attribution[:500],
        "verified_at": verified_at,
    }


def _wikimedia_search_query(value: str) -> str:
    """Do not make an English Commons query impossible with CJK scene directions."""

    generic = {"public", "domain", "footage", "video", "clip", "material", "media"}
    terms = [
        item
        for item in _LATIN_OR_NUMERIC_QUERY_TERM.findall(value)
        if item.casefold() not in generic
    ]
    latin = " ".join(terms)
    has_alpha = any(item[0].isalpha() for item in terms)
    return latin[:300] if len(terms) >= 2 and has_alpha else value[:300]


def _wikimedia_derivative(
    info: Mapping[str, Any], duration: float | None
) -> tuple[str | None, str, int]:
    """Prefer a <=1080p Commons transcode over disruptive multi-gigabyte originals."""

    candidates: list[tuple[int, str, str, int]] = []
    derivatives = info.get("derivatives", ())
    for item in derivatives if isinstance(derivatives, list) else ():
        if not isinstance(item, Mapping):
            continue
        source = _optional_text(item.get("src"))
        media_type = str(item.get("type") or "").split(";", maxsplit=1)[0]
        width = int(item.get("width") or 0)
        bandwidth = int(item.get("bandwidth") or 0)
        if not source or not media_type.startswith("video/") or not 240 <= width <= 1280:
            continue
        estimated_size = (
            max(1, math.ceil(duration * bandwidth / 8))
            if duration is not None and bandwidth > 0
            else 0
        )
        candidates.append((width, source, media_type, estimated_size))
    if candidates:
        # Commons explicitly asks automated clients to use generated
        # transcodes instead of repeatedly pulling disruptive originals. A
        # 720p-or-lower source is ample for short B-roll and can be upscaled by
        # the renderer when the final canvas is 1080p.
        _width, source, media_type, size = max(
            candidates,
            key=lambda item: (
                "/transcoded/" in item[1],
                item[0],
                -(item[3] or sys.maxsize),
            ),
        )
        return source, media_type, size
    return (
        _optional_text(info.get("url")),
        str(info.get("mime") or ""),
        int(info.get("size") or 0),
    )
