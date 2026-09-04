from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import unittest
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from framefactory.runtime import PermanentStepError, RetryableStepError
from framefactory.steps import ArtifactRef, StepContext
from framefactory.worker.providers import ProviderArtifact
from framefactory.worker.queue_routing import operation_queue_name
from framefactory.worker.web_capture.models import (
    CaptureResult,
    CaptureViewport,
    RegionCandidate,
    SitePageResult,
)
from framefactory.worker.web_capture.security import PublicHttpsUrlValidator
from framefactory.worker.web_capture.site_v2 import (
    WebPageCaptureBatchCapability,
    WebpageStoryWritingCapability,
    WebRegionAnalyzeCapability,
    WebRegionsMaterializeCapability,
    WebSiteDiscoverCapability,
    WebStoryboardPlanCapability,
    _bounded_ui_label,
    _canonical_scene_narration,
)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"x" * 5_000
    )


class _MemoryStorage:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def publish(
        self,
        context: StepContext,
        artifact: ProviderArtifact,
        *,
        attempt_scoped: bool = False,
    ) -> ArtifactRef:
        del attempt_scoped
        artifact_id = str(uuid4())
        self.values[artifact_id] = artifact.data
        return ArtifactRef(
            id=artifact_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            step_id=context.step_id,
            kind=artifact.kind,
            media_type=artifact.media_type,
            object_key=f"browser-capture/test/{artifact_id}/{artifact.filename}",
            byte_size=len(artifact.data),
            content_hash=hashlib.sha256(artifact.data).hexdigest(),
            filename=artifact.filename,
        )

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        return self.values[artifact.id]

    def read_json(self, artifact: ArtifactRef) -> dict[str, Any]:
        return json.loads(self.read_bytes(artifact))


class _SiteAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def capture_site_page(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
        maximum_links: int,
        maximum_regions: int,
    ) -> SitePageResult:
        del maximum_body_characters, maximum_links
        self.calls.append(url)
        path = urlsplit(url).path
        links = (
            (
                "https://example.test/about/",
                "https://example.test/login",
                "https://example.test/pricing?campaign=secret",
                "https://outside.test/escape",
            )
            if path == "/"
            else ("https://example.test/features",)
        )
        region = RegionCandidate(
            candidate_id="region-01",
            selector="main > section",
            role="hero" if path == "/" else "feature",
            text=f"content for {path}",
            x=0,
            y=0,
            width=640,
            height=360,
            score=0.95 if path == "/" else 0.8,
            png=_png(640, 360),
        )
        capture = CaptureResult(
            png=_png(viewport.width, viewport.height),
            title=f"Title {path}",
            body_text=(
                "https://example.test/pricing https://outside.test/x "
                "https://example.test/login"
                if path == "/sitemap.xml"
                else f"Body {path}"
            ),
            requested_url=url,
            final_url=url,
            viewport=viewport,
            response_status=200,
            resource_count=5,
            transferred_bytes=1_024,
            redirect_count=0,
            redirect_chain=(url,),
            engine="chromium",
            browser_version="140",
            playwright_version="1.62",
            websocket_attempts=0,
            blocked_non_idempotent_requests=0,
        )
        return SitePageResult(
            capture=capture,
            canonical_url=url,
            discovered_urls=links,
            regions=(region,)[:maximum_regions],
        )


class _StoryClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def structured(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        count = kwargs["schema"]["properties"]["scenes"]["minItems"]
        return {
            "title": "站点介绍",
            "scenes": [
                {
                    "narration": (
                        f"镜头 {index} 展示经过审核的网页信息，并保持来源和画面绑定，"
                        "让观众可以直接核对。"
                    )
                }
                for index in range(count)
            ],
        }


def _snapshot(**overrides: Any) -> dict[str, Any]:
    return {
        "target_url": "https://example.test/",
        "topic": "产品介绍",
        "aspect_ratio": "16:9",
        "duration_seconds": 15,
        "max_pages": 3,
        "max_depth": 2,
        "max_shots": 3,
        "public_page_confirmed": True,
        "rights_confirmed": True,
        "webpage_video_run_id": "33333333-3333-4333-8333-333333333333",
        **overrides,
    }


def _context(
    step_id: str,
    *artifacts: ArtifactRef,
    approved: tuple[str, ...] = (),
    approved_reviews: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> StepContext:
    return StepContext(
        workspace_id="11111111-1111-4111-8111-111111111111",
        run_id="22222222-2222-4222-8222-222222222222",
        step_id=step_id,
        input_snapshot=snapshot or _snapshot(),
        input_artifacts=artifacts,
        approved_dependency_step_ids=approved,
        approved_dependency_reviews=approved_reviews or {},
    )


def _storyboard_review(manifest: ArtifactRef) -> dict[str, Any]:
    return {
        "schema_version": "2.0.0",
        "kind": "storyboard_selection",
        "manifest_sha256": manifest.content_hash,
        "shots": [
            {
                "id": "shot-01",
                "enabled": True,
                "order": 2,
                "motion": "pan",
                "transition": "fade_black",
            },
            {
                "id": "shot-02",
                "enabled": False,
                "order": 1,
                "motion": "static",
                "transition": "cut",
            },
            {
                "id": "shot-03",
                "enabled": True,
                "order": 0,
                "motion": "zoom_out",
                "transition": "fade_black",
            },
        ],
    }


class WebCaptureSiteV2Tests(unittest.TestCase):
    def test_scene_narration_discards_provider_envelope_and_explanation(self) -> None:
        value = (
            "“热门手持设备，总有一款适合你。”}]}] # 严格遵循要求，"
            "最终输出为 JSON 格式，并且所有内容均来自页面。"
        )

        self.assertEqual(
            "热门手持设备，总有一款适合你。",
            _canonical_scene_narration(value),
        )

    def test_scene_narration_preserves_multiple_spoken_sentences(self) -> None:
        self.assertEqual(
            "这是第一句。这是第二句。",
            _canonical_scene_narration("这是第一句。这是第二句。"),
        )

    def setUp(self) -> None:
        self.storage = _MemoryStorage()
        self.adapter = _SiteAdapter()
        self.validator = PublicHttpsUrlValidator(
            resolver=lambda host, _port: (
                ("93.184.216.34",) if host == "example.test" else ("93.184.216.35",)
            )
        )

    def test_discovery_is_bounded_same_origin_and_excludes_sensitive_flows(
        self,
    ) -> None:
        result = asyncio.run(
            WebSiteDiscoverCapability(
                self.adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(_context("discover-step"))
        )
        self.assertTrue(result.requires_review)
        manifest = self.storage.read_json(result.artifacts[0])
        self.assertEqual("site_manifest", manifest["kind"])
        self.assertEqual(
            ["/", "/pricing", "/about/"],
            [urlsplit(page["url"]).path for page in manifest["pages"]],
        )
        serialized = json.dumps(manifest)
        self.assertNotIn("login", serialized)
        self.assertNotIn("campaign", serialized)
        self.assertNotIn("outside", serialized)
        self.assertEqual(
            result.artifacts[0].id, result.summary_dict()["site_manifest_artifact_id"]
        )

        with self.assertRaisesRegex(PermanentStepError, "max_pages"):
            asyncio.run(
                WebSiteDiscoverCapability(
                    self.adapter,
                    self.validator,
                    self.storage,
                    maximum_body_characters=12_000,
                ).execute(_context("discover-step", snapshot=_snapshot(max_pages=13)))
            )

    def test_discovery_falls_back_when_optional_sitemap_navigation_fails(
        self,
    ) -> None:
        class _DownloadSitemapAdapter(_SiteAdapter):
            async def capture_site_page(self, url: str, *args: Any, **kwargs: Any):
                if urlsplit(url).path == "/sitemap.xml":
                    self.calls.append(url)
                    raise RetryableStepError(
                        "browser navigation failed before a document was available"
                    )
                return await super().capture_site_page(url, *args, **kwargs)

        adapter = _DownloadSitemapAdapter()
        result = asyncio.run(
            WebSiteDiscoverCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(_context("discover-step"))
        )

        manifest = self.storage.read_json(result.artifacts[0])
        self.assertEqual("https://example.test/", adapter.calls[0])
        self.assertEqual("https://example.test/sitemap.xml", adapter.calls[1])
        self.assertGreaterEqual(len(manifest["pages"]), 1)

    def test_discovery_strips_known_tracking_queries_but_rejects_unknown_queries(
        self,
    ) -> None:
        class _TrackingLinkAdapter(_SiteAdapter):
            async def capture_site_page(self, url: str, *args: Any, **kwargs: Any):
                result = await super().capture_site_page(url, *args, **kwargs)
                if urlsplit(url).path == "/":
                    return SitePageResult(
                        capture=result.capture,
                        canonical_url=result.canonical_url,
                        discovered_urls=(
                            "https://example.test/camera-drones?site=brandsite&from=nav",
                            "https://example.test/search?q=private",
                        ),
                        regions=result.regions,
                    )
                return result

        adapter = _TrackingLinkAdapter()
        result = asyncio.run(
            WebSiteDiscoverCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(
                _context(
                    "discover-step",
                    snapshot=_snapshot(
                        include_sitemap=False,
                        max_pages=2,
                        max_depth=1,
                    ),
                )
            )
        )

        manifest = self.storage.read_json(result.artifacts[0])
        self.assertEqual(
            ["/", "/camera-drones"],
            [urlsplit(page["url"]).path for page in manifest["pages"]],
        )
        self.assertNotIn("brandsite", json.dumps(manifest))
        self.assertNotIn("private", json.dumps(manifest))

    def test_discovery_adopts_validated_final_origin_after_brand_redirect(self) -> None:
        class _BrandRedirectAdapter(_SiteAdapter):
            async def capture_site_page(self, url: str, *args: Any, **kwargs: Any):
                result = await super().capture_site_page(url, *args, **kwargs)
                if url == "https://brand.test/":
                    redirected = "https://www.brand.test/cn"
                    capture = result.capture
                    return SitePageResult(
                        capture=CaptureResult(
                            png=capture.png,
                            title=capture.title,
                            body_text=capture.body_text,
                            requested_url=url,
                            final_url=redirected,
                            viewport=capture.viewport,
                            response_status=capture.response_status,
                            resource_count=capture.resource_count,
                            transferred_bytes=capture.transferred_bytes,
                            redirect_count=1,
                            redirect_chain=(url, redirected),
                            engine=capture.engine,
                            browser_version=capture.browser_version,
                            playwright_version=capture.playwright_version,
                            websocket_attempts=capture.websocket_attempts,
                            blocked_non_idempotent_requests=(
                                capture.blocked_non_idempotent_requests
                            ),
                        ),
                        canonical_url=redirected,
                        discovered_urls=(
                            "https://www.brand.test/cn/products",
                            "https://www.brand.test/de/products",
                        ),
                        regions=result.regions,
                    )
                return result

        adapter = _BrandRedirectAdapter()
        validator = PublicHttpsUrlValidator(resolver=lambda _host, _port: ("93.184.216.34",))
        result = asyncio.run(
            WebSiteDiscoverCapability(
                adapter,
                validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(
                _context(
                    "discover-step",
                    snapshot=_snapshot(
                        target_url="https://brand.test/",
                        include_sitemap=False,
                        max_pages=2,
                    ),
                )
            )
        )

        manifest = self.storage.read_json(result.artifacts[0])
        self.assertEqual("https://www.brand.test/cn", manifest["root_url"])
        self.assertEqual(
            ["www.brand.test", "www.brand.test"],
            [urlsplit(page["url"]).hostname for page in manifest["pages"]],
        )
        self.assertEqual(
            ["/cn", "/cn/products"],
            [urlsplit(page["url"]).path for page in manifest["pages"]],
        )
        self.assertEqual("/cn", manifest["scope"]["locale_path_prefix"])

    def test_region_analysis_uses_viewports_when_dom_regions_are_absent(self) -> None:
        class _NoRegionAdapter(_SiteAdapter):
            async def capture_site_page(self, url: str, *args: Any, **kwargs: Any):
                result = await super().capture_site_page(url, *args, **kwargs)
                return SitePageResult(
                    capture=result.capture,
                    canonical_url=result.canonical_url,
                    discovered_urls=result.discovered_urls,
                    regions=(),
                )

        adapter = _NoRegionAdapter()
        discover = asyncio.run(
            WebSiteDiscoverCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(_context("discover-step"))
        )
        capture = asyncio.run(
            WebPageCaptureBatchCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(
                _context(
                    "capture-step",
                    *discover.artifacts,
                    approved=("discover-step",),
                    approved_reviews={
                        "discover-step": {
                            "schema_version": "2.0.0",
                            "kind": "scope_selection",
                            "manifest_sha256": discover.artifacts[0].content_hash,
                            "selected_page_ids": ["page-01", "page-02", "page-03"],
                        }
                    },
                )
            )
        )
        regions = asyncio.run(
            WebRegionAnalyzeCapability(self.storage).execute(
                _context("region-step", *capture.artifacts)
            )
        )
        document = self.storage.read_json(regions.artifacts[0])
        self.assertEqual(
            "dom_region_with_viewport_fallback_v2", document["ranking"]
        )
        self.assertEqual(3, len(document["candidates"]))
        self.assertTrue(all(item["fallback"] for item in document["candidates"]))

        storyboard = asyncio.run(
            WebStoryboardPlanCapability(self.storage).execute(
                _context("storyboard-step", *capture.artifacts, *regions.artifacts)
            )
        )
        shots = self.storage.read_json(storyboard.artifacts[0])["shots"]
        self.assertEqual(3, len(shots))
        self.assertTrue(all(shot["reason"].startswith("页面概览：") for shot in shots))
        ui_manifest = storyboard.output_summary["manifest"]
        self.assertEqual(3, len(ui_manifest["shots"]))
        self.assertTrue(
            all(shot["region_id"] is None for shot in ui_manifest["shots"])
        )

    def test_region_analysis_rejects_low_information_dom_capture(self) -> None:
        class _BlankRegionAdapter(_SiteAdapter):
            async def capture_site_page(self, url: str, *args: Any, **kwargs: Any):
                result = await super().capture_site_page(url, *args, **kwargs)
                maximum_regions = int(kwargs.get("maximum_regions", 0))
                blank = RegionCandidate(
                    candidate_id="blank-region",
                    selector="main > section",
                    role="feature",
                    text="unloaded feature",
                    x=0,
                    y=0,
                    width=1920,
                    height=1080,
                    score=0.99,
                    png=_png(1920, 1080)[:24],
                )
                return SitePageResult(
                    capture=result.capture,
                    canonical_url=result.canonical_url,
                    discovered_urls=result.discovered_urls,
                    regions=(blank,)[:maximum_regions],
                )

        adapter = _BlankRegionAdapter()
        discover = asyncio.run(
            WebSiteDiscoverCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(_context("discover-step"))
        )
        capture = asyncio.run(
            WebPageCaptureBatchCapability(
                adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(
                _context(
                    "capture-step",
                    *discover.artifacts,
                    approved=("discover-step",),
                    approved_reviews={
                        "discover-step": {
                            "schema_version": "2.0.0",
                            "kind": "scope_selection",
                            "manifest_sha256": discover.artifacts[0].content_hash,
                            "selected_page_ids": ["page-01", "page-02", "page-03"],
                        }
                    },
                )
            )
        )
        regions = asyncio.run(
            WebRegionAnalyzeCapability(self.storage).execute(
                _context("region-step", *capture.artifacts)
            )
        )
        document = self.storage.read_json(regions.artifacts[0])
        self.assertEqual(3, document["rejected_low_information"])
        self.assertTrue(all(item.get("fallback") for item in document["candidates"]))

    def test_storyboard_ui_region_labels_follow_public_contract_limit(self) -> None:
        label = _bounded_ui_label("  repeated   words " * 80)
        self.assertEqual(240, len(label))
        self.assertNotIn("  ", label)

    def test_full_manifest_chain_binds_approval_and_exact_region_bytes(self) -> None:
        discover = asyncio.run(
            WebSiteDiscoverCapability(
                self.adapter,
                self.validator,
                self.storage,
                maximum_body_characters=12_000,
            ).execute(_context("discover-step"))
        )
        capture_capability = WebPageCaptureBatchCapability(
            self.adapter,
            self.validator,
            self.storage,
            maximum_body_characters=12_000,
        )
        with self.assertRaisesRegex(PermanentStepError, "approved site scope"):
            asyncio.run(
                capture_capability.execute(
                    _context("capture-step", *discover.artifacts)
                )
            )
        with self.assertRaisesRegex(PermanentStepError, "bind the current manifest"):
            asyncio.run(
                capture_capability.execute(
                    _context(
                        "capture-step",
                        *discover.artifacts,
                        approved=("discover-step",),
                        approved_reviews={
                            "discover-step": {
                                "schema_version": "2.0.0",
                                "kind": "scope_selection",
                                "manifest_sha256": "0" * 64,
                                "selected_page_ids": ["page-01"],
                            }
                        },
                    )
                )
            )
        capture = asyncio.run(
            capture_capability.execute(
                _context(
                    "capture-step",
                    *discover.artifacts,
                    approved=("discover-step",),
                    approved_reviews={
                        "discover-step": {
                            "schema_version": "2.0.0",
                            "kind": "scope_selection",
                            "manifest_sha256": discover.artifacts[0].content_hash,
                            "selected_page_ids": ["page-01", "page-02", "page-03"],
                        }
                    },
                )
            )
        )
        capture_manifest = next(
            item
            for item in capture.artifacts
            if item.filename == "page-capture-manifest.json"
        )
        self.assertEqual(1, self.storage.read_json(capture_manifest)["revision"])

        regions = asyncio.run(
            WebRegionAnalyzeCapability(self.storage).execute(
                _context("region-step", *capture.artifacts)
            )
        )
        region_document = self.storage.read_json(regions.artifacts[0])
        self.assertEqual(
            "dom_region_with_viewport_fallback_v2", region_document["ranking"]
        )

        storyboard = asyncio.run(
            WebStoryboardPlanCapability(self.storage).execute(
                _context("storyboard-step", *capture.artifacts, *regions.artifacts)
            )
        )
        self.assertTrue(storyboard.requires_review)
        storyboard_document = self.storage.read_json(storyboard.artifacts[0])
        self.assertEqual(3, len(storyboard_document["shots"]))
        self.assertEqual(
            3, len({shot["page_id"] for shot in storyboard_document["shots"]})
        )
        self.assertEqual("zoom_out", storyboard_document["shots"][0]["motion"])
        self.assertEqual("cut", storyboard_document["shots"][0]["transition"])
        self.assertTrue(
            all(
                shot["transition"] == "fade_black"
                for shot in storyboard_document["shots"][1:]
            )
        )

        sources = next(
            item for item in capture.artifacts if item.filename == "page-sources.json"
        )
        story_client = _StoryClient()
        write = asyncio.run(
            WebpageStoryWritingCapability(story_client, self.storage).execute(
                _context(
                    "write-step",
                    storyboard.artifacts[0],
                    sources,
                    approved=("storyboard-step",),
                    approved_reviews={
                        "storyboard-step": _storyboard_review(storyboard.artifacts[0])
                    },
                )
            )
        )
        script = self.storage.read_json(write.artifacts[0])
        self.assertEqual(
            storyboard.artifacts[0].content_hash, script["storyboard_sha256"]
        )
        self.assertEqual(
            ["shot-03", "shot-01"],
            [scene["shot_id"] for scene in script["scenes"]],
        )
        self.assertTrue(
            all(scene["narration"].endswith("。") for scene in script["scenes"])
        )
        self.assertEqual(
            [45, 97],
            story_client.requests[0]["payload"]["task"][
                "narration_character_bounds"
            ],
        )

        images = tuple(item for item in capture.artifacts if item.kind == "image")
        materialized = asyncio.run(
            WebRegionsMaterializeCapability(self.storage).execute(
                _context(
                    "materialize-step",
                    *images,
                    storyboard.artifacts[0],
                    write.artifacts[0],
                    approved=("storyboard-step",),
                    approved_reviews={
                        "storyboard-step": _storyboard_review(storyboard.artifacts[0])
                    },
                )
            )
        )
        assets = [item for item in materialized.artifacts if item.kind == "asset"]
        self.assertEqual(2, len(assets))
        asset_manifest = self.storage.read_json(materialized.artifacts[-1])
        self.assertEqual(
            ["zoom_out", "pan"],
            [item["motion"] for item in asset_manifest["assets"]],
        )
        self.assertEqual(
            ["cut", "fade_black"],
            [item["transition"] for item in asset_manifest["assets"]],
        )
        self.assertEqual(
            ["shot-03", "shot-01"],
            [item["selected_for_scene"] for item in asset_manifest["assets"]],
        )
        self.assertTrue(
            all(
                0 <= item["motion_focus"][axis] <= 1
                for item in asset_manifest["assets"]
                for axis in ("x", "y")
            )
        )
        for asset in assets:
            source_id = next(
                item["source_capture_artifact_id"]
                for item in asset_manifest["assets"]
                if item["artifact_id"] == asset.id
            )
            self.assertEqual(
                self.storage.values[source_id], self.storage.read_bytes(asset)
            )

    def test_v2_browser_operations_use_isolated_queue(self) -> None:
        self.assertEqual("browser-capture", operation_queue_name("web.site.discover"))
        self.assertEqual(
            "browser-capture", operation_queue_name("web.page.capture_batch")
        )
        self.assertEqual("run-steps", operation_queue_name("web.region.analyze"))
        self.assertEqual("run-steps", operation_queue_name("web.storyboard.plan"))
        self.assertEqual("run-steps", operation_queue_name("web.materialize.regions"))


if __name__ == "__main__":
    unittest.main()
