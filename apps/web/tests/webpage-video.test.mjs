import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { HttpFrameFactoryAdapter } from "../lib/api/http-adapter.ts";
import {
  readWebpageVideoSubmissionAttempt,
  saveWebpageVideoSubmissionAttempt,
  webpageVideoPendingAttemptKey,
} from "../lib/webpage-video-submission.ts";
import { safeBrowserMediaUrl } from "../lib/safe-media-url.ts";
import { buildWebpageVideoEvidenceReport } from "../lib/webpage-video-evidence.ts";

const root = new URL("../", import.meta.url);

test("media URLs stay HTTPS-only except for explicit loopback development pages", () => {
  assert.equal(
    safeBrowserMediaUrl("https://media.example/capture.png", "https://app.example/review"),
    "https://media.example/capture.png",
  );
  assert.equal(
    safeBrowserMediaUrl("http://127.0.0.1:59000/capture.png", "http://localhost:4173/review"),
    "http://127.0.0.1:59000/capture.png",
  );
  assert.equal(
    safeBrowserMediaUrl("http://127.0.0.1:59000/capture.png", "https://app.example/review"),
    undefined,
  );
  assert.equal(
    safeBrowserMediaUrl("http://192.168.1.10/capture.png", "http://localhost:4173/review"),
    undefined,
  );
});

async function source(pathname) {
  return readFile(new URL(pathname, root), "utf8");
}

function json(body, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: status === 204 ? {} : { "Content-Type": "application/json" },
  });
}

test("webpage-video HTTP adapter maps the dedicated API and concurrency evidence", async () => {
  const runId = "77777777-7777-4777-8777-777777777777";
  const sha = "a".repeat(64);
  const requests = [];
  const fetcher = async (url, init = {}) => {
    const parsed = new URL(String(url));
    const request = {
      pathname: parsed.pathname,
      method: init.method ?? "GET",
      headers: new Headers(init.headers),
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    };
    requests.push(request);
    if (parsed.pathname === "/v1/webpage-video/options") return json({
      schema_version: "1.0.0",
      status: "ready",
      pipeline: { slug: "webpage-capture-video", version: 1 },
      limits: {
        target_url_max_length: 2048,
        topic_max_length: 1200,
        viewports: [
          { aspect_ratio: "16:9", width: 1920, height: 1080 },
          { aspect_ratio: "9:16", width: 1080, height: 1920 },
          { aspect_ratio: "1:1", width: 1080, height: 1080 },
          { aspect_ratio: "4:3", width: 1440, height: 1080 },
          { aspect_ratio: "unsupported", width: 800, height: 600 },
        ],
        duration_seconds: [15, 30, 60],
        crawl_max_pages_default: 8,
        crawl_max_pages_limit: 12,
        crawl_max_depth_default: 1,
        crawl_max_depth_limit: 2,
      },
      voice_profiles: [{ id: "voice-cn-1", name: "清晰女声", locale: "zh-CN" }],
      subtitles: { supported: true, default_enabled: true },
      blockers: [],
    });
    if (parsed.pathname === "/v1/webpage-video/pilot-summary") return json({
      schema_version: "1.0.0",
      generated_at: "2026-09-02T00:00:00Z",
      total_records: 3,
      included_records: 3,
      truncated: false,
      recommended_minimum_pilots: 3,
      pilot_target_met: true,
      adopted_count: 2,
      evaluating_count: 1,
      rejected_count: 0,
      baseline_minutes_total: 300,
      assisted_minutes_total: 90,
      saved_minutes_total: 210,
      time_reduction_percent: 70,
      average_satisfaction_score: 4.5,
      satisfaction_response_count: 2,
      average_willingness_to_pay_hkd: 750,
      willingness_to_pay_response_count: 2,
      segments: [{ customer_segment: "香港中小型电商", pilot_count: 3, adopted_count: 2, saved_minutes: 210, average_time_reduction_percent: 70 }],
      items: [{ webpage_video_run_id: runId, customer_segment: "香港中小型电商", baseline_minutes: 120, assisted_minutes: 30, saved_minutes: 90, time_reduction_percent: 75, revision_count: 1, outcome: "adopted", satisfaction_score: 5, willingness_to_pay_hkd: 1000, updated_at: "2026-09-01T00:00:00Z" }],
    });
    if (parsed.pathname === "/v1/webpage-video/runs" && request.method === "POST") return json({
      id: runId,
      status: "queued",
      revision: 0,
      target_url: request.body.target_url,
      video: request.body.video,
    }, 201);
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}`) return json({
      id: runId,
      project_run_id: "88888888-8888-4888-8888-888888888888",
      status: "awaiting_capture_review",
      revision: 91,
      target_url: "https://example.com/product",
      final_url: "https://www.example.com/product",
      request: { crawl: { max_pages: 8, max_depth: 1, same_origin_only: true, include_sitemap: true } },
      video: { topic: "产品导览", aspect_ratio: "9:16", duration_seconds: 30, subtitles_enabled: true, voice_profile_id: "voice-cn-1" },
      capture: {
        capture_revision: 7,
        sha256: sha,
        requested_url: "https://example.com/product",
        final_url: "https://www.example.com/product",
      },
      final_video: { preview_url: "https://cdn.example/final.mp4", download_url: "https://cdn.example/final-download.mp4", captions_url: "https://cdn.example/final.vtt", media_type: "video/mp4" },
      pilot_feedback: { id: "99999999-9999-4999-8999-999999999999", webpage_video_run_id: runId, customer_segment: "香港中小型电商", baseline_minutes: 120, assisted_minutes: 30, saved_minutes: 90, time_reduction_percent: 75, revision_count: 1, outcome: "adopted", satisfaction_score: 5, willingness_to_pay_hkd: 1000, notes: null, revision: 1, created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z" },
    });
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/capture`) return json({
      revision: 99,
      capture: {
        capture_revision: 7,
        preview_url: "https://cdn.example/capture.png?signature=test",
        sha256: sha,
        requested_url: "https://example.com/product",
        final_url: "https://www.example.com/product",
        width: 1080,
        height: 1920,
      },
    });
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/site`) return json({
      schema_version: "2.0.0",
      scope: {
        status: "awaiting_review",
        revision: 3,
        sha256: "b".repeat(64),
        content: { root_url: "https://example.com", discovered_count: 1, selected_count: 1, pages: [{ id: "page-home", url: "https://example.com", canonical_url: "https://www.example.com", title: "首页", selected: true, score: 0.96, selection_reason: "核心入口", screenshot: { preview_url: "https://cdn.example/home.png", sha256: "d".repeat(64) }, regions: [{ id: "hero", kind: "hero", label: "核心标题", artifact: { preview_url: "https://cdn.example/hero.png", sha256: "e".repeat(64) } }] }] },
      },
      storyboard: { status: "awaiting_review", revision: 4, sha256: "c".repeat(64), content: { shots: [{ id: "shot-1", page_id: "page-home", region_id: "hero", ordinal: 1, narration_cue: "核心标题", motion: "zoom_out", transition: "cut", artifact: { preview_url: "https://cdn.example/hero.png" } }] } },
    });
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/scope/review`) return json(null, 204);
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/storyboard/review`) return json(null, 204);
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/capture/review`) return json(null, 204);
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/pilot-feedback`) return json({ id: "99999999-9999-4999-8999-999999999999", webpage_video_run_id: runId, ...request.body, saved_minutes: 90, time_reduction_percent: 75, revision: 1, created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z" }, 201);
    if (parsed.pathname === `/v1/webpage-video/runs/${runId}/cancel`) return json(null, 204);
    return json({ code: "NOT_FOUND", message: parsed.pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });

  const options = await adapter.getWebpageVideoOptions();
  assert.equal(options.ok, true);
  assert.deepEqual(options.ok && options.data.limits.aspectRatios, ["16:9", "9:16", "1:1", "4:3"]);
  assert.equal(options.ok && options.data.limits.urlMaxLength, 2048);
  assert.equal(options.ok && options.data.voices[0].language, "zh-CN");
  assert.equal(options.ok && options.data.subtitles.defaultEnabled, true);
  assert.equal(options.ok && options.data.limits.crawlMaxPagesDefault, 8);

  const createRequest = {
    targetUrl: "https://example.com/product",
    topic: "产品导览",
    aspectRatio: "9:16",
    durationSeconds: 30,
    subtitlesEnabled: true,
    voiceProfileId: "voice-cn-1",
    publicPageConfirmed: true,
    rightsConfirmed: true,
    crawl: { maxPages: 8, maxDepth: 1, sameOriginOnly: true, includeSitemap: true },
  };
  const created = await adapter.createWebpageVideoRun(createRequest, "webpage-video-create:fixed");
  assert.equal(created.ok && created.data.id, runId);
  const loaded = await adapter.getWebpageVideoRun(runId);
  assert.equal(loaded.ok && loaded.data.revision, 7, "capture_revision must win over wrapper revision");
  assert.equal(loaded.ok && loaded.data.finalUrl, "https://www.example.com/product");
  assert.equal(loaded.ok && loaded.data.finalVideo.downloadUrl, "https://cdn.example/final-download.mp4");
  assert.equal(loaded.ok && loaded.data.finalVideo.captionsUrl, "https://cdn.example/final.vtt");
  assert.equal(loaded.ok && loaded.data.siteMode, true);
  assert.equal(loaded.ok && loaded.data.pilotFeedback.timeReductionPercent, 75);
  const capture = await adapter.getWebpageVideoCapture(runId);
  assert.equal(capture.ok && capture.data.revision, 7, "capture endpoint must prioritize capture_revision");
  assert.equal(capture.ok && capture.data.sha256, sha);
  const site = await adapter.getWebpageVideoSite(runId);
  assert.equal(site.ok && site.data.scope.pages[0].title, "首页");
  assert.equal(site.ok && site.data.storyboard.shots[0].regionId, "hero");
  assert.equal(site.ok && site.data.storyboard.shots[0].motion, "zoom_out");
  assert.equal(site.ok && site.data.storyboard.shots[0].transition, "cut");
  await adapter.reviewWebpageVideoScope(runId, {
    decision: "approve", expectedRevision: 3, expectedSha256: "b".repeat(64), selectedPageIds: ["page-home"],
  }, "webpage-video-scope-approve:fixed");
  await adapter.reviewWebpageVideoStoryboard(runId, {
    decision: "approve", expectedRevision: 4, expectedSha256: "c".repeat(64), shots: [{ id: "shot-1", enabled: true, order: 1, motion: "zoom_out", transition: "cut" }],
  }, "webpage-video-storyboard-approve:fixed");
  await adapter.reviewWebpageVideoRun(runId, {
    decision: "approve",
    comment: "画面与来源一致",
    expectedRevision: 7,
    expectedSha256: sha,
  }, "webpage-video-approve:fixed");
  const pilot = await adapter.saveWebpageVideoPilotFeedback(runId, {
    customerSegment: "香港中小型电商", baselineMinutes: 120, assistedMinutes: 30, revisionCount: 1, outcome: "adopted", satisfactionScore: 5, willingnessToPayHkd: 1000, expectedRevision: 0,
  }, "webpage-pilot-feedback:fixed");
  assert.equal(pilot.ok && pilot.data.savedMinutes, 90);
  await adapter.cancelWebpageVideoRun(runId, "webpage-video-cancel:fixed");
  const summary = await adapter.getWebpageVideoPilotSummary();
  assert.equal(summary.ok && summary.data.totalRecords, 3);
  assert.equal(summary.ok && summary.data.segments[0].customerSegment, "香港中小型电商");
  assert.equal(summary.ok && summary.data.items[0].willingnessToPayHkd, 1000);

  assert.deepEqual(requests[1].body, {
    target_url: "https://example.com/product",
    capture: { mode: "viewport", aspect_ratio: "9:16", full_page: false },
    video: { topic: "产品导览", duration_seconds: 30, subtitles_enabled: true, voice_profile_id: "voice-cn-1" },
    rights: { public_page_confirmed: true, rights_confirmed: true },
    crawl: { max_pages: 8, max_depth: 1, same_origin_only: true, include_sitemap: true },
  });
  assert.equal(requests[1].headers.get("Idempotency-Key"), "webpage-video-create:fixed");
  assert.deepEqual(requests[7].body, {
    decision: "approve",
    comment: "画面与来源一致",
    expected_revision: 7,
    expected_sha256: sha,
  });
  assert.deepEqual(requests[5].body.selected_page_ids, ["page-home"]);
  assert.deepEqual(requests[6].body.shots, [{ id: "shot-1", enabled: true, order: 1, motion: "zoom_out", transition: "cut" }]);
  assert.equal(requests[7].headers.get("Idempotency-Key"), "webpage-video-approve:fixed");
  assert.equal(requests[8].headers.get("Idempotency-Key"), "webpage-pilot-feedback:fixed");
  assert.equal(requests[8].body.expected_revision, 0);
  assert.equal(requests[9].headers.get("Idempotency-Key"), "webpage-video-cancel:fixed");
  assert.equal(requests[10].pathname, "/v1/webpage-video/pilot-summary");
});

test("application evidence excludes signed URLs and marks missing proof honestly", () => {
  const report = buildWebpageVideoEvidenceReport({
    schemaVersion: "1.0.0", id: "run-1", status: "succeeded", rawStatus: "succeeded", revision: 2,
    targetUrl: "https://example.com", spec: { topic: "导览", aspectRatio: "9:16", durationSeconds: 30, subtitlesEnabled: true },
    siteMode: false, capture: { previewUrl: "https://signed.example/capture", sha256: "a".repeat(64), requestedUrl: "https://example.com", revision: 2 },
    finalVideo: { previewUrl: "https://signed.example/video", filename: "final.mp4", sha256: "b".repeat(64), byteSize: 42 },
  }, undefined, "2026-09-01T00:00:00.000Z");
  const serialized = JSON.stringify(report);
  assert.equal(report.completeness.complete, false);
  assert.deepEqual(report.completeness.missing, ["pilot_outcome"]);
  assert.doesNotMatch(serialized, /signed\.example/);
  assert.match(serialized, /vistora\.webpage-video\.application-evidence/);
});

test("application evidence builds an honest shot-level provenance chain", () => {
  const finalHash = "f".repeat(64);
  const report = buildWebpageVideoEvidenceReport({
    schemaVersion: "1.0.0", id: "run-site", status: "succeeded", rawStatus: "succeeded", revision: 4,
    targetUrl: "https://example.com", spec: { topic: "导览", aspectRatio: "9:16", durationSeconds: 30, subtitlesEnabled: true },
    siteMode: true, finalVideo: { sha256: finalHash },
    pilotFeedback: { id: "pilot-1", webpageVideoRunId: "run-site", customerSegment: "代理商", baselineMinutes: 60, assistedMinutes: 20, savedMinutes: 40, timeReductionPercent: 66.7, revisionCount: 1, outcome: "adopted", revision: 1, createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-01T00:00:00Z" },
  }, {
    schemaVersion: "2.0.0", mode: "site",
    scope: { status: "approved", revision: 2, sha256: "a".repeat(64), pages: [{ id: "page-1", url: "https://example.com/product", title: "产品", selected: true, capture: { sha256: "b".repeat(64), requestedUrl: "https://example.com/product", revision: 1 }, regions: [{ id: "hero", type: "hero", label: "主视觉", sha256: "c".repeat(64) }] }] },
    storyboard: { status: "approved", revision: 3, sha256: "d".repeat(64), shots: [{ id: "shot-1", pageId: "page-1", regionId: "hero", label: "主视觉", motion: "zoom_in", transition: "cut", enabled: true, order: 1 }] },
  }, "2026-09-02T00:00:00Z");
  assert.equal(report.schemaVersion, "1.1.0");
  assert.equal(report.evidenceChain.length, 1);
  assert.equal(report.evidenceChain[0].complete, true);
  assert.equal(report.evidenceChain[0].sourceUrl, "https://example.com/product");
  assert.equal(report.evidenceChain[0].regionSha256, "c".repeat(64));
  assert.equal(report.evidenceChain[0].outputSha256, finalHash);
  assert.equal(report.completeness.complete, true);
});

test("webpage-video create attempts retain the exact request and idempotency key", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const attempt = {
    idempotencyKey: "webpage-video-create:fixed-attempt",
    request: {
      targetUrl: "https://example.com/public",
      topic: "公开网页导览",
      aspectRatio: "16:9",
      durationSeconds: 30,
      subtitlesEnabled: true,
      publicPageConfirmed: true,
      rightsConfirmed: true,
      crawl: { maxPages: 8, maxDepth: 1, sameOriginOnly: true, includeSitemap: true },
    },
  };
  saveWebpageVideoSubmissionAttempt(storage, attempt);
  assert.deepEqual(readWebpageVideoSubmissionAttempt(storage), attempt);
  assert.match(storage.getItem(webpageVideoPendingAttemptKey), /fixed-attempt/);
  saveWebpageVideoSubmissionAttempt(storage, null);
  assert.equal(readWebpageVideoSubmissionAttempt(storage), null);
});

test("webpage-video routes and components keep an isolated, fail-closed UI contract", async () => {
  const [createRoute, applicationRoute, evidenceRoute, evidenceDashboard, detailRoute, studio, detail, adapter, contracts, composer, css] = await Promise.all([
    source("app/create/webpage-video/page.tsx"),
    source("app/create/application-demo/page.tsx"),
    source("app/application-evidence/page.tsx"),
    source("components/application-evidence-dashboard.tsx"),
    source("app/webpage-video/[id]/page.tsx"),
    source("components/webpage-video-studio.tsx"),
    source("components/webpage-video-detail.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/contracts.ts"),
    source("components/create-composer.tsx"),
    source("app/globals.css"),
  ]);
  assert.match(createRoute, /WebpageVideoStudio/);
  assert.match(applicationRoute, /mode="application-demo"/);
  assert.match(evidenceRoute, /ApplicationEvidenceDashboard/);
  assert.match(evidenceDashboard, /打印 \/ 保存 PDF/);
  assert.match(evidenceDashboard, /未经 Vistora 独立验证/);
  assert.match(detailRoute, /WebpageVideoDetail/);
  assert.match(composer, /href="\/create\/webpage-video"/);
  assert.match(composer, /href="\/create\/application-demo"/);
  for (const method of ["getWebpageVideoOptions", "getWebpageVideoPilotSummary", "createWebpageVideoRun", "getWebpageVideoRun", "getWebpageVideoCapture", "getWebpageVideoSite", "reviewWebpageVideoRun", "reviewWebpageVideoScope", "reviewWebpageVideoStoryboard", "saveWebpageVideoPilotFeedback", "cancelWebpageVideoRun"]) {
    assert.match(adapter, new RegExp(method));
  }
  for (const state of ["loading", "empty", "blocked", "error", "submitting", "submit_unknown"]) assert.match(studio, new RegExp(`"${state}"`));
  for (const decision of ["approve", "recapture", "reject"]) assert.match(detail, new RegExp(`review\\("${decision}"\\)`));
  assert.match(detail, /previewState === "loaded"/);
  assert.match(detail, /onLoad=\{\(\) => setPreviewResult\(\{ key: previewKey, status: "loaded" \}\)\}/);
  assert.match(detail, /onError=\{\(\) => setPreviewResult\(\{ key: previewKey, status: "error" \}\)\}/);
  for (const copy of ["仅公开 HTTPS 页面", "必须显式勾选", "Idempotency", "SHA-256", "capture_revision", "quality_review_required", "打开底层质检详情", "最终成片"]) {
    assert.match(studio + detail, new RegExp(copy, "i"));
  }
  assert.match(contracts, /publicPageConfirmed: true/);
  assert.match(contracts, /rightsConfirmed: true/);
  assert.match(contracts, /WebpageVideoSitePlan/);
  assert.match(studio, /maxPages.*8/);
  assert.match(studio, /maxDepth.*1/);
  assert.match(detail, /WebpageVideoSitePlanPanel/);
  assert.match(detail, /WebpageVideoEvidenceChain/);
  assert.match(studio, /不接收登录态、Cookie、自定义 Header/);
  assert.doesNotMatch(studio, /cookieValue|customHeaders|passwordValue|usernameValue/);
  assert.doesNotMatch(studio + detail, /createRun\(|reviewRunStep\(/);
  assert.doesNotMatch(studio + detail, /createMockAdapter|mock-adapter|mock-data/);
  assert.match(css, /\.web-video-workbench \{/);
  assert.match(css, /@media \(max-width: 767px\)[\s\S]*?\.web-video-page/);
});

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("webpage-video-test", `${process.pid}-${Date.now()}`);
  return (await import(workerUrl.href)).default;
}

async function render(worker, pathname) {
  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server renders the independent webpage-video create and detail routes", async () => {
  const worker = await loadWorker();
  const createResponse = await render(worker, "/create/webpage-video");
  assert.equal(createResponse.status, 200);
  const createHtml = await createResponse.text();
  assert.match(createHtml, /<title>网页截图成片(?: · Vistora)?<\/title>/i);
  assert.match(createHtml, /从一个网址，定向解析成多镜头视频/);
  assert.match(createHtml, /正在读取网页截图能力/);

  const applicationResponse = await render(worker, "/create/application-demo");
  assert.equal(applicationResponse.status, 200);
  const applicationHtml = await applicationResponse.text();
  assert.match(applicationHtml, /用一条真实任务，完成申请评审演示/);

  const evidenceResponse = await render(worker, "/application-evidence");
  assert.equal(evidenceResponse.status, 200);
  const evidenceHtml = await evidenceResponse.text();
  assert.match(evidenceHtml, /<title>申请证据中心(?: · Vistora)?<\/title>/i);
  assert.match(evidenceHtml, /申请证据中心/);

  const detailResponse = await render(worker, "/webpage-video/77777777-7777-4777-8777-777777777777");
  assert.equal(detailResponse.status, 200);
  const detailHtml = await detailResponse.text();
  assert.match(detailHtml, /<title>网页截图成片任务(?: · Vistora)?<\/title>/i);
  assert.match(detailHtml, /网页截图成片任务/);
  assert.match(detailHtml, /正在读取截图证据/);
});
