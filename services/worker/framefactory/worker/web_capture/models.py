"""Typed values shared by webpage capture capabilities and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from framefactory.runtime import PermanentStepError

_VIEWPORTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
}


@dataclass(frozen=True, slots=True)
class CaptureViewport:
    aspect_ratio: str
    width: int
    height: int

    @classmethod
    def for_aspect_ratio(cls, aspect_ratio: object) -> CaptureViewport:
        value = str(aspect_ratio or "16:9")
        try:
            width, height = _VIEWPORTS[value]
        except KeyError as exc:
            raise PermanentStepError(
                "unsupported webpage capture aspect ratio"
            ) from exc
        return cls(value, width, height)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    png: bytes
    title: str
    body_text: str
    requested_url: str
    final_url: str
    viewport: CaptureViewport
    response_status: int
    resource_count: int
    transferred_bytes: int
    redirect_count: int
    redirect_chain: tuple[str, ...]
    engine: str
    browser_version: str
    playwright_version: str
    websocket_attempts: int
    blocked_non_idempotent_requests: int
    blocked_unsafe_subresources: int = 0
    network_idle_timed_out: bool = False


@dataclass(frozen=True, slots=True)
class RegionCandidate:
    """A DOM-derived visual candidate and its exact high-resolution PNG crop."""

    candidate_id: str
    selector: str
    role: str
    text: str
    x: float
    y: float
    width: float
    height: float
    score: float
    png: bytes


@dataclass(frozen=True, slots=True)
class SitePageResult:
    """One safely navigated page with discovery evidence and visual candidates."""

    capture: CaptureResult
    canonical_url: str
    discovered_urls: tuple[str, ...]
    regions: tuple[RegionCandidate, ...]


class CaptureAdapter(Protocol):
    async def capture(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
    ) -> CaptureResult: ...


class SiteCaptureAdapter(Protocol):
    async def capture_site_page(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
        maximum_links: int,
        maximum_regions: int,
    ) -> SitePageResult: ...
