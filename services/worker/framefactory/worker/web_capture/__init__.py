"""Independent single-page and multi-page webpage-video capabilities."""

from .capabilities import (
    WebCaptureScreenshotCapability,
    WebCaptureValidateCapability,
    WebMaterializeCapability,
    WebpageWritingCapability,
)
from .models import (
    CaptureAdapter,
    CaptureResult,
    CaptureViewport,
    RegionCandidate,
    SiteCaptureAdapter,
    SitePageResult,
)
from .playwright_adapter import PlaywrightChromiumCaptureAdapter
from .security import (
    PublicHttpsPolicy,
    PublicHttpsUrlValidator,
    UnsafeWebUrl,
    ValidatedWebUrl,
    redact_url,
)
from .site_v2 import (
    WebPageCaptureBatchCapability,
    WebpageStoryWritingCapability,
    WebRegionAnalyzeCapability,
    WebRegionsMaterializeCapability,
    WebSiteDiscoverCapability,
    WebStoryboardPlanCapability,
)

__all__ = [
    "CaptureAdapter",
    "CaptureResult",
    "CaptureViewport",
    "PlaywrightChromiumCaptureAdapter",
    "PublicHttpsPolicy",
    "PublicHttpsUrlValidator",
    "RegionCandidate",
    "SiteCaptureAdapter",
    "SitePageResult",
    "UnsafeWebUrl",
    "ValidatedWebUrl",
    "WebCaptureScreenshotCapability",
    "WebCaptureValidateCapability",
    "WebMaterializeCapability",
    "WebPageCaptureBatchCapability",
    "WebRegionAnalyzeCapability",
    "WebRegionsMaterializeCapability",
    "WebSiteDiscoverCapability",
    "WebStoryboardPlanCapability",
    "WebpageStoryWritingCapability",
    "WebpageWritingCapability",
    "redact_url",
]
