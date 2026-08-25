"""Playwright Chromium adapter for the isolated browser-capture worker."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from framefactory.runtime import PermanentStepError, RetryableStepError

from .models import CaptureResult, CaptureViewport, RegionCandidate, SitePageResult
from .security import PublicHttpsUrlValidator, UnsafeWebUrl, redact_url

_SITE_EVIDENCE_SCRIPT = r"""
({maximumLinks, maximumRegions}) => {
  const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
  const seen = new Set();
  const links = [];
  for (const anchor of document.querySelectorAll('a[href]')) {
    let url;
    try { url = new URL(anchor.href, location.href); } catch (_) { continue; }
    url.hash = '';
    if (!['http:', 'https:'].includes(url.protocol)) continue;
    const key = url.href;
    if (!seen.has(key)) { seen.add(key); links.push(key); }
    if (links.length >= maximumLinks) break;
  }
  const selectorFor = element => {
    if (element.id && /^[A-Za-z][A-Za-z0-9_-]{0,80}$/.test(element.id)) {
      return `#${CSS.escape(element.id)}`;
    }
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      const tag = node.tagName.toLowerCase();
      const siblings = node.parentElement
        ? Array.from(node.parentElement.children).filter(item => item.tagName === node.tagName)
        : [];
      parts.unshift(siblings.length > 1 ? `${tag}:nth-of-type(${siblings.indexOf(node) + 1})` : tag);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const roles = [
    ['hero', 'main > section:first-of-type, [class*="hero" i], header + section'],
    ['heading', 'h1, h2'],
    ['chart', 'canvas, svg, [class*="chart" i], [class*="graph" i]'],
    ['feature', 'main section, [class*="feature" i], [class*="product" i]'],
    ['cta', 'a[href], button']
  ];
  const candidates = [];
  const used = new Set();
  const documentWidth = Math.max(
    document.documentElement?.scrollWidth || 0,
    document.body?.scrollWidth || 0,
    innerWidth,
  );
  const documentHeight = Math.max(
    document.documentElement?.scrollHeight || 0,
    document.body?.scrollHeight || 0,
    innerHeight,
  );
  for (const [role, selector] of roles) {
    for (const element of document.querySelectorAll(selector)) {
      if (used.has(element)) continue;
      const rect = element.getBoundingClientRect();
      if (rect.width < 180 || rect.height < 70 || rect.width * rect.height < 25000) continue;
      const style = getComputedStyle(element);
      if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
      used.add(element);
      const rawText = (element.innerText || element.getAttribute('aria-label') || '')
        .replace(/\s+/g, ' ').trim();
      // Large page-shell containers rank highly by area but contain navigation,
      // cookie banners and many unrelated products. They are not a meaningful
      // visual detail and previously displaced the actual hero region.
      if (
        role === 'feature'
        && rect.width >= innerWidth * .94
        && rect.height >= innerHeight * .70
      ) continue;
      if (
        rect.width >= innerWidth * .94
        && rect.height >= innerHeight * .70
        && rawText.length > 350
      ) continue;
      const text = rawText.slice(0, 500);
      const area = Math.min(1, (rect.width * rect.height) / (innerWidth * innerHeight));
      const roleWeight = {hero: 1, chart: .95, heading: .9, feature: .82, cta: .72}[role] || .6;
      const aboveFold = rect.top < innerHeight ? .12 : 0;
      const score = Math.min(1, roleWeight * .65 + area * .23 + aboveFold);
      candidates.push({
        selector: selectorFor(element), role, text, score,
        x: Math.max(0, rect.left + scrollX), y: Math.max(0, rect.top + scrollY),
        width: rect.width, height: rect.height, documentWidth, documentHeight
      });
    }
  }
  candidates.sort((a, b) => b.score - a.score || a.y - b.y || a.selector.localeCompare(b.selector));
  const distinct = [];
  for (const candidate of candidates) {
    const duplicate = distinct.some(existing => {
      const left = Math.max(candidate.x, existing.x);
      const top = Math.max(candidate.y, existing.y);
      const right = Math.min(candidate.x + candidate.width, existing.x + existing.width);
      const bottom = Math.min(candidate.y + candidate.height, existing.y + existing.height);
      const intersection = Math.max(0, right - left) * Math.max(0, bottom - top);
      const union = candidate.width * candidate.height + existing.width * existing.height - intersection;
      const iou = union > 0 ? intersection / union : 0;
      return iou >= .90 || (iou >= .78 && candidate.text === existing.text);
    });
    if (!duplicate) distinct.push(candidate);
    if (distinct.length >= maximumRegions) break;
  }
  return {canonical, links, regions: distinct};
}
"""

_PAGE_STABILITY_SCRIPT = r"""
async ({timeoutMs, sampleIntervalMs, requiredStableSamples}) => {
  const captureStability = true;
  const startedAt = performance.now();
  const deadline = startedAt + timeoutMs;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => {
    if (!(element instanceof Element)) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 1 && rect.height > 1 && rect.bottom >= 0 && rect.top <= innerHeight
      && rect.right >= 0 && rect.left <= innerWidth && style.display !== 'none'
      && style.visibility !== 'hidden' && Number(style.opacity) !== 0;
  };
  const busySelector = [
    '[aria-busy="true"]', '[role="progressbar"]', '[data-loading="true"]',
    '[data-state="loading"]', '.skeleton', '[class*="skeleton-" i]',
    '[class*="_skeleton" i]', '.loading', '[class*="loading-" i]',
    '[class*="_loading" i]'
  ].join(',');
  const busyText = /^(loading|加载中|正在加载|正在读取|请稍候|数据加载中)(?:[.。…!！\s]*)$/i;
  const visibleBusy = () => {
    const candidates = Array.from(document.querySelectorAll(busySelector));
    for (const element of document.querySelectorAll('main *, body > *')) {
      const text = (element.textContent || '').replace(/\s+/g, ' ').trim();
      if (text.length <= 40 && busyText.test(text)) candidates.push(element);
    }
    return Array.from(new Set(candidates)).filter(visible).length;
  };
  const visibleImagesReady = () => Array.from(document.images)
    .filter(visible).every(image => image.complete && image.naturalWidth > 0);
  const signature = () => {
    const body = document.body;
    const elements = Array.from(document.querySelectorAll(
      'main, header, section, article, h1, h2, img, canvas, svg'
    )).filter(visible).slice(0, 48);
    const geometry = elements.map(element => {
      const rect = element.getBoundingClientRect();
      return [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)];
    });
    return JSON.stringify([
      body?.scrollWidth || 0,
      body?.scrollHeight || 0,
      (body?.innerText || '').replace(/\s+/g, ' ').trim().length,
      document.images.length,
      document.querySelectorAll('canvas, svg').length,
      geometry,
    ]);
  };

  if (document.fonts) {
    await Promise.race([
      document.fonts.ready.catch(() => null),
      sleep(Math.max(1, Math.min(timeoutMs, 10000))),
    ]);
  }
  await Promise.race([
    Promise.allSettled(Array.from(document.images).filter(visible).map(image =>
      image.decode ? image.decode() : Promise.resolve()
    )),
    sleep(Math.max(1, Math.min(timeoutMs, 10000))),
  ]);

  let prior = '';
  let stableSamples = 0;
  let lastBusyCount = 0;
  while (performance.now() < deadline) {
    lastBusyCount = visibleBusy();
    // A third-party request can prevent the browser's global `load` event even
    // after the DOM and every visible image are ready.  Visual stability is the
    // capture gate, so `interactive` is sufficient once fonts, images, busy
    // indicators and layout signatures have all settled.
    const ready = document.readyState !== 'loading' && visibleImagesReady()
      && lastBusyCount === 0;
    const next = signature();
    stableSamples = ready && next === prior ? stableSamples + 1 : 0;
    prior = next;
    if (stableSamples >= requiredStableSamples) {
      return {
        stable: true,
        readyState: document.readyState,
        stableSamples,
        visibleBusyCount: 0,
        elapsedMs: Math.round(performance.now() - startedAt),
      };
    }
    await sleep(sampleIntervalMs);
  }
  return {
    stable: false,
    readyState: document.readyState,
    stableSamples,
    visibleBusyCount: lastBusyCount,
    visibleImagesReady: visibleImagesReady(),
    elapsedMs: Math.round(performance.now() - startedAt),
  };
}
"""


class PlaywrightChromiumCaptureAdapter:
    """Capture one fresh context and close the complete browser after the job.

    Request routing validates every redirect and subresource.  It is deliberately
    documented as defense in depth: only the deployment egress proxy/firewall can
    close the DNS-rebinding time-of-check/time-of-use gap.
    """

    def __init__(self, settings: Any, validator: PublicHttpsUrlValidator) -> None:
        if not settings.egress_policy_enforced or not settings.proxy_url:
            raise ValueError("browser capture requires an enforced egress proxy policy")
        self.settings = settings
        self.validator = validator

    async def capture(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
    ) -> CaptureResult:
        result = await self._capture_job(
            url,
            viewport,
            maximum_body_characters=maximum_body_characters,
            maximum_links=0,
            maximum_regions=0,
        )
        return result.capture

    async def capture_site_page(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
        maximum_links: int,
        maximum_regions: int,
    ) -> SitePageResult:
        if not 1 <= maximum_links <= 256:
            raise ValueError("maximum_links must be between 1 and 256")
        if not 0 <= maximum_regions <= 8:
            raise ValueError("maximum_regions must be between 0 and 8")
        return await self._capture_job(
            url,
            viewport,
            maximum_body_characters=maximum_body_characters,
            maximum_links=maximum_links,
            maximum_regions=maximum_regions,
        )

    async def _capture_job(
        self,
        url: str,
        viewport: CaptureViewport,
        *,
        maximum_body_characters: int,
        maximum_links: int,
        maximum_regions: int,
    ) -> SitePageResult:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - deployment/package gate
            raise PermanentStepError(
                "browser_capture_runtime_missing: install the pinned Playwright runtime"
            ) from exc

        initial = await asyncio.to_thread(self.validator.validate, url)
        state: dict[str, Any] = {
            "resource_count": 0,
            "transferred_bytes": 0,
            "blocked": None,
            "close_task": None,
            "main_frame_navigations": 0,
            "websocket_attempts": 0,
            "blocked_non_idempotent_requests": 0,
            "blocked_unsafe_subresources": 0,
            "unsafe_subresource_codes": {},
            "network_idle_timed_out": False,
        }
        proxy: dict[str, str] = {"server": self.settings.proxy_url}
        if self.settings.proxy_username:
            proxy["username"] = self.settings.proxy_username
            proxy["password"] = self.settings.proxy_password

        try:
            async with asyncio.timeout(self.settings.job_timeout_seconds):
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        headless=True,
                        chromium_sandbox=not bool(
                            getattr(self.settings, "disable_chromium_sandbox", False)
                        ),
                        proxy=proxy,
                        args=(
                            "--disable-dev-shm-usage",
                            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                        ),
                    )
                    try:
                        context = await browser.new_context(
                            viewport={
                                "width": viewport.width,
                                "height": viewport.height,
                            },
                            screen={"width": viewport.width, "height": viewport.height},
                            device_scale_factor=1,
                            color_scheme="light",
                            reduced_motion="reduce",
                            locale="zh-CN",
                            timezone_id="Asia/Shanghai",
                            service_workers="block",
                            accept_downloads=False,
                        )
                        try:
                            result = await self._capture_context(
                                context,
                                initial.navigation_url,
                                viewport,
                                maximum_body_characters,
                                state,
                                PlaywrightTimeoutError,
                                PlaywrightError,
                                browser_version=str(browser.version),
                                playwright_version=_playwright_version(),
                                maximum_links=maximum_links,
                                maximum_regions=maximum_regions,
                            )
                        finally:
                            await context.close()
                    finally:
                        await browser.close()
        except TimeoutError as exc:
            raise RetryableStepError(
                "webpage capture exceeded its hard timeout"
            ) from exc
        return result

    async def _capture_context(
        self,
        context: Any,
        navigation_url: str,
        viewport: CaptureViewport,
        maximum_body_characters: int,
        state: dict[str, Any],
        playwright_timeout_error: type[Exception],
        playwright_error: type[Exception],
        *,
        browser_version: str,
        playwright_version: str,
        maximum_links: int = 0,
        maximum_regions: int = 0,
    ) -> SitePageResult:
        page: Any | None = None

        async def guard_route(route: Any, request: Any) -> None:
            state["resource_count"] += 1
            main_navigation = (
                page is not None
                and request.is_navigation_request()
                and request.frame == page.main_frame
            )
            if str(request.method).upper() not in {"GET", "HEAD"}:
                state["blocked_non_idempotent_requests"] += 1
                if main_navigation:
                    state["blocked"] = "non_idempotent_navigation"
                await route.abort("blockedbyclient")
                return
            if state["resource_count"] > self.settings.maximum_resources:
                state["blocked"] = "resource_limit"
                await route.abort("blockedbyclient")
                return
            if _redirect_count(request) > self.settings.maximum_redirects:
                if main_navigation:
                    state["blocked"] = "redirect_limit"
                else:
                    state["blocked_unsafe_subresources"] += 1
                    codes = state["unsafe_subresource_codes"]
                    codes["redirect_limit"] = int(codes.get("redirect_limit", 0)) + 1
                await route.abort("blockedbyclient")
                return
            try:
                validated = await asyncio.to_thread(
                    self.validator.validate, request.url
                )
            except UnsafeWebUrl as exc:
                if main_navigation:
                    state["blocked"] = "unsafe_navigation"
                else:
                    state["blocked_unsafe_subresources"] += 1
                    codes = state["unsafe_subresource_codes"]
                    codes[exc.code] = int(codes.get(exc.code, 0)) + 1
                await route.abort("blockedbyclient")
                return
            if main_navigation:
                chain = state.setdefault("redirect_chain", [])
                redacted = validated.redacted_url
                if not chain or chain[-1] != redacted:
                    chain.append(redacted)
                if len(chain) > self.settings.maximum_redirects + 1:
                    state["blocked"] = "redirect_limit"
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await context.route("**/*", guard_route)
        page = await context.new_page()

        async def block_websocket(websocket: Any) -> None:
            # HTTP request routing does not cover WebSocket handshakes.  A
            # screenshot has no legitimate need for a bidirectional socket, so
            # close every attempted connection before any page data is relayed.
            state["websocket_attempts"] += 1
            await websocket.close(code=1008, reason="blocked by capture policy")

        await page.route_web_socket("**/*", block_websocket)
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send("Page.enable")

        def frame_navigated(event: Mapping[str, Any]) -> None:
            frame = event.get("frame", {})
            if isinstance(frame, Mapping) and not frame.get("parentId"):
                state["main_frame_navigations"] += 1

        cdp.on("Page.frameNavigated", frame_navigated)

        async def guard_response(response: Any) -> None:
            length = _content_length(response.headers)
            if length is None:
                return
            if (
                length > self.settings.maximum_transfer_bytes
                or int(state["transferred_bytes"]) + length
                > self.settings.maximum_transfer_bytes
            ):
                state["blocked"] = "declared_transfer_limit"
                if not page.is_closed():
                    await page.close()

        page.on("response", guard_response)

        def loading_finished(event: Mapping[str, Any]) -> None:
            length = event.get("encodedDataLength", 0)
            if isinstance(length, (int, float)) and length > 0:
                state["transferred_bytes"] += int(length)
                if state["transferred_bytes"] > self.settings.maximum_transfer_bytes:
                    state["blocked"] = "transfer_limit"
                    if state["close_task"] is None and not page.is_closed():
                        state["close_task"] = asyncio.create_task(page.close())

        cdp.on("Network.loadingFinished", loading_finished)
        try:
            response = await page.goto(
                navigation_url,
                wait_until="domcontentloaded",
                timeout=self.settings.navigation_timeout_seconds * 1_000,
            )
        except playwright_timeout_error as exc:
            raise RetryableStepError("webpage navigation timed out") from exc
        except playwright_error as exc:
            if state["blocked"]:
                raise PermanentStepError(
                    f"webpage capture blocked by {state['blocked']}",
                    code="browser_capture_policy_blocked",
                ) from exc
            raise RetryableStepError(
                "browser navigation failed before a document was available",
                code="browser_navigation_failed",
            ) from exc
        if response is None:
            raise PermanentStepError(
                "webpage navigation produced no document response",
                code="browser_no_document",
            )
        if state["blocked"]:
            raise PermanentStepError(
                f"webpage capture blocked by {state['blocked']}",
                code="browser_capture_policy_blocked",
            )
        status = int(response.status)
        if status in {401, 403, 407, 429}:
            raise PermanentStepError(
                "webpage requires authentication or blocked automation",
                code="browser_authentication_required",
            )
        if status >= 500:
            raise RetryableStepError("webpage origin returned a temporary server error")
        if status >= 400:
            raise PermanentStepError(
                "webpage origin rejected the capture request",
                code="browser_origin_rejected",
            )

        await asyncio.to_thread(self.validator.validate, page.url)
        redirect_count = _redirect_count(response.request)
        if redirect_count > self.settings.maximum_redirects:
            raise PermanentStepError(
                "webpage exceeded the redirect limit",
                code="browser_capture_policy_blocked",
            )
        settle_timeout_ms = self.settings.settle_timeout_seconds * 1_000
        try:
            await page.wait_for_load_state("load", timeout=settle_timeout_ms)
        except playwright_timeout_error:
            # Some public sites keep optional frames or trackers pending and
            # never emit the global load event. Continue to the stricter visual
            # stability gate below instead of rejecting usable pixels.
            state["network_idle_timed_out"] = True
        try:
            await page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
        except playwright_timeout_error:
            # Analytics, telemetry and polling can keep a legitimate public site
            # permanently network-active.  Treat network-idle as a preferred
            # signal; the DOM/font/image/loading-indicator stability gate below
            # remains authoritative for whether pixels are ready to capture.
            state["network_idle_timed_out"] = True
        stability = await page.evaluate(
            _PAGE_STABILITY_SCRIPT,
            {
                "timeoutMs": settle_timeout_ms,
                "sampleIntervalMs": 250,
                "requiredStableSamples": 4,
            },
        )
        if not isinstance(stability, Mapping) or stability.get("stable") is not True:
            raise RetryableStepError(
                "webpage DOM, images, fonts, or loading indicators did not become stable",
                code="browser_page_not_stable",
            )
        if state["blocked"]:
            raise PermanentStepError(
                f"webpage capture blocked by {state['blocked']}",
                code="browser_capture_policy_blocked",
            )
        stable_before = await asyncio.to_thread(self.validator.validate, page.url)
        navigation_generation = int(state["main_frame_navigations"])
        if await page.locator('input[type="password"]').count() > 0:
            raise PermanentStepError(
                "webpage authentication interfaces are not supported",
                code="browser_authentication_required",
            )
        title = _bounded_text(await page.title(), 512)
        body_text = _bounded_text(
            await page.locator("body").inner_text(timeout=5_000),
            maximum_body_characters,
        )
        site_evidence: Mapping[str, Any] = {}
        if maximum_links or maximum_regions:
            raw_evidence = await page.evaluate(
                _SITE_EVIDENCE_SCRIPT,
                {"maximumLinks": maximum_links, "maximumRegions": maximum_regions},
            )
            if isinstance(raw_evidence, Mapping):
                site_evidence = raw_evidence
        if int(state["transferred_bytes"]) > self.settings.maximum_transfer_bytes:
            raise PermanentStepError(
                "webpage capture blocked by transfer_limit",
                code="browser_capture_policy_blocked",
            )
        try:
            png = await page.screenshot(
                type="png",
                full_page=False,
                animations="disabled",
                caret="hide",
                scale="css",
                timeout=self.settings.screenshot_timeout_seconds * 1_000,
            )
        except playwright_error as exc:
            if state["blocked"]:
                raise PermanentStepError(
                    f"webpage capture blocked by {state['blocked']}",
                    code="browser_capture_policy_blocked",
                ) from exc
            raise RetryableStepError(
                "browser screenshot failed before stable bytes were available",
                code="browser_screenshot_failed",
            ) from exc
        close_task = state.get("close_task")
        if close_task is not None:
            with suppress(Exception):
                await close_task
        if state["blocked"]:
            raise PermanentStepError(
                f"webpage capture blocked by {state['blocked']}",
                code="browser_capture_policy_blocked",
            )
        if int(state["transferred_bytes"]) > self.settings.maximum_transfer_bytes:
            raise PermanentStepError(
                "webpage capture blocked by transfer_limit",
                code="browser_capture_policy_blocked",
            )
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PermanentStepError(
                "browser capture did not produce a PNG",
                code="browser_invalid_capture",
            )
        regions: list[RegionCandidate] = []
        raw_regions = site_evidence.get("regions", ())
        if isinstance(raw_regions, list):
            for index, value in enumerate(raw_regions[:maximum_regions]):
                if not isinstance(value, Mapping):
                    continue
                region_capture = _region_clip(value, viewport)
                if region_capture is None:
                    continue
                clip = region_capture["clip"]
                focus = region_capture["focus"]
                try:
                    crop = await page.screenshot(
                        type="png",
                        clip=clip,
                        animations="disabled",
                        caret="hide",
                        scale="device",
                        timeout=self.settings.screenshot_timeout_seconds * 1_000,
                    )
                except playwright_error:
                    continue
                if not crop.startswith(b"\x89PNG\r\n\x1a\n"):
                    continue
                pixel_width, pixel_height = _png_pixel_size(crop)
                # Chromium can intersect a requested clip with the actual
                # document surface. Keep the DOM focus in the coordinate
                # system of the PNG that was actually emitted.
                scale_x = pixel_width / float(clip["width"])
                scale_y = pixel_height / float(clip["height"])
                regions.append(
                    RegionCandidate(
                        candidate_id=f"region-{index + 1:02d}",
                        selector=_bounded_text(value.get("selector"), 512),
                        role=_bounded_text(value.get("role"), 64) or "section",
                        text=_bounded_text(value.get("text"), 500),
                        x=float(focus["x"]) * scale_x,
                        y=float(focus["y"]) * scale_y,
                        width=min(
                            float(pixel_width) - float(focus["x"]) * scale_x,
                            float(focus["width"]) * scale_x,
                        ),
                        height=min(
                            float(pixel_height) - float(focus["y"]) * scale_y,
                            float(focus["height"]) * scale_y,
                        ),
                        score=float(value.get("score") or 0),
                        png=crop,
                    )
                )
        stable_after = await asyncio.to_thread(self.validator.validate, page.url)
        if (
            stable_after.navigation_url != stable_before.navigation_url
            or int(state["main_frame_navigations"]) != navigation_generation
        ):
            raise PermanentStepError(
                "webpage navigated while its screenshot was being captured",
                code="browser_capture_navigation_unstable",
            )
        redirect_chain = list(state.get("redirect_chain") or ())
        if not redirect_chain or redirect_chain[-1] != stable_after.redacted_url:
            redirect_chain.append(stable_after.redacted_url)
        if len(redirect_chain) > self.settings.maximum_redirects + 1:
            raise PermanentStepError(
                "webpage exceeded the redirect limit",
                code="browser_capture_redirect_limit",
            )
        # Playwright 1.62 can deliver late CDP events while the surrounding
        # context is closing.  Detach explicitly before returning so browser
        # teardown cannot race a live session callback.
        with suppress(Exception):
            await cdp.detach()
        capture = CaptureResult(
            png=png,
            title=title,
            body_text=body_text,
            requested_url=redact_url(navigation_url),
            final_url=stable_after.redacted_url,
            viewport=viewport,
            response_status=status,
            resource_count=int(state["resource_count"]),
            transferred_bytes=int(state["transferred_bytes"]),
            redirect_count=max(0, len(redirect_chain) - 1),
            redirect_chain=tuple(redirect_chain),
            engine="chromium",
            browser_version=browser_version[:128],
            playwright_version=playwright_version[:128],
            websocket_attempts=int(state["websocket_attempts"]),
            blocked_non_idempotent_requests=int(
                state["blocked_non_idempotent_requests"]
            ),
            blocked_unsafe_subresources=int(state["blocked_unsafe_subresources"]),
            network_idle_timed_out=bool(state["network_idle_timed_out"]),
        )
        links = site_evidence.get("links", ())
        discovered_urls = tuple(
            str(value)[:2_048]
            for value in links
            if isinstance(links, list) and isinstance(value, str)
        )
        canonical = site_evidence.get("canonical")
        return SitePageResult(
            capture=capture,
            canonical_url=str(canonical)[:2_048]
            if isinstance(canonical, str)
            else page.url,
            discovered_urls=discovered_urls,
            regions=tuple(regions),
        )


def _redirect_count(request: Any) -> int:
    count = 0
    current = request.redirected_from
    while current is not None:
        count += 1
        current = current.redirected_from
    return count


def _bounded_text(value: object, maximum: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:maximum]


def _png_pixel_size(value: bytes) -> tuple[int, int]:
    if len(value) < 24 or not value.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("value is not a PNG")
    return int.from_bytes(value[16:20], "big"), int.from_bytes(value[20:24], "big")


def _region_clip(
    value: Mapping[str, Any], viewport: CaptureViewport
) -> dict[str, dict[str, float]] | None:
    try:
        x = max(0.0, float(value.get("x") or 0))
        y = max(0.0, float(value.get("y") or 0))
        width = float(value.get("width") or 0)
        height = float(value.get("height") or 0)
        document_width = max(float(value.get("documentWidth") or 0), width)
        document_height = max(float(value.get("documentHeight") or 0), height)
    except (TypeError, ValueError, OverflowError):
        return None
    if width < 80 or height < 50 or width * height > 40_000_000:
        return None
    # Capture a complete viewport-shaped context around the region.  The
    # renderer can then move continuously from overview to detail inside one
    # immutable image instead of hard-cutting to a pre-cropped fragment.
    clip_width = min(float(viewport.width), document_width)
    clip_height = min(float(viewport.height), document_height)
    clip_x = min(
        max(0.0, x + width / 2 - clip_width / 2),
        max(0.0, document_width - clip_width),
    )
    clip_y = min(
        max(0.0, y + height / 2 - clip_height / 2),
        max(0.0, document_height - clip_height),
    )
    if clip_width < 80 or clip_height < 50:
        return None
    focus_x = min(max(0.0, x - clip_x), clip_width)
    focus_y = min(max(0.0, y - clip_y), clip_height)
    focus_width = min(width, max(0.0, clip_width - focus_x))
    focus_height = min(height, max(0.0, clip_height - focus_y))
    if focus_width < 1 or focus_height < 1:
        return None
    return {
        "clip": {
            "x": clip_x,
            "y": clip_y,
            "width": clip_width,
            "height": clip_height,
        },
        "focus": {
            "x": focus_x,
            "y": focus_y,
            "width": focus_width,
            "height": focus_height,
        },
    }


def _content_length(headers: Mapping[str, Any]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(str(raw))
    except ValueError:
        return None
    return value if value >= 0 else None


def _playwright_version() -> str:
    try:
        return version("playwright")
    except (
        PackageNotFoundError
    ):  # pragma: no cover - import gate catches packaging defects
        return "unknown"


_SANDBOX_HEALTHCHECK = r"""
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        chromium_sandbox=True,
        args=[
            "--disable-dev-shm-usage",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    )
    try:
        page = browser.new_page(viewport={"width": 320, "height": 240})
        page.set_content("<!doctype html><title>capture-health</title><p>local</p>")
        if page.title() != "capture-health":
            raise RuntimeError("local browser document did not load")
    finally:
        browser.close()
"""


def playwright_runtime_healthcheck(*, timeout_seconds: float = 20.0) -> None:
    """Launch a local, sandboxed Chromium without making a network request.

    The subprocess avoids mixing Playwright's synchronous API with the worker's
    asyncio loop and proves that the image/user/seccomp combination can launch
    with ``chromium_sandbox=True``.  There is intentionally no ``--no-sandbox``
    fallback.
    """

    try:
        completed = subprocess.run(
            [sys.executable, "-c", _SANDBOX_HEALTHCHECK],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("sandboxed Chromium healthcheck could not run") from exc
    if completed.returncode != 0:
        raise RuntimeError("sandboxed Chromium healthcheck failed to launch")
