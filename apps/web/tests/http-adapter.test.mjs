import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function source(pathname) {
  return readFile(new URL(pathname, root), "utf8");
}

test("production runtime factory is HTTP-only", async () => {
  const [runtime, exports] = await Promise.all([
    source("lib/api/runtime-adapter.ts"),
    source("lib/api/index.ts"),
  ]);
  assert.match(runtime, /createHttpAdapter/);
  assert.doesNotMatch(runtime, /mock-adapter|createMockAdapter/);
  assert.doesNotMatch(exports, /mock-adapter|createMockAdapter/);
});

test("HTTP transport covers control-plane lifecycle and concurrency headers", async () => {
  const adapter = await source("lib/api/http-adapter.ts");
  for (const endpoint of [
    "/v1/context",
    "/v1/skills",
    "/v1/skill-versions",
    "/v1/skill-test-executions",
    "/v1/runs",
  ]) assert.match(adapter, new RegExp(endpoint.replaceAll("/", "\\/")));
  assert.match(adapter, /"Idempotency-Key"/);
  assert.match(adapter, /"If-Match"/);
  assert.match(adapter, /NETWORK_ERROR/);
  assert.match(adapter, /response\.status === 429 \|\| response\.status >= 500/);
});

test("application components do not instantiate the mock fixture", async () => {
  const paths = [
    "components/app-shell.tsx",
    "components/batch-console.tsx",
    "components/asset-hub.tsx",
    "components/asset-library-workbench.tsx",
    "components/asset-detail.tsx",
    "components/channels-view.tsx",
    "components/channel-detail.tsx",
    "components/channel-form.tsx",
    "components/create-composer.tsx",
    "components/projects-view.tsx",
    "components/run-detail.tsx",
    "components/settings-view.tsx",
    "components/skill-creator.tsx",
    "components/skill-library.tsx",
    "components/skill-studio.tsx",
  ];
  for (const pathname of paths) {
    const content = await source(pathname);
    assert.doesNotMatch(content, /createMockAdapter|mock-adapter|mock-data/, pathname);
    assert.match(content, /createFrameFactoryAdapter/, pathname);
  }
});

test("asset operations adapter maps cursor pages and all durable asset commands", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const assetId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const libraryId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const now = "2026-08-17T12:00:00Z";
  const asset = {
    id: assetId, workspace_id: "22222222-2222-4222-8222-222222222222", library_id: libraryId,
    kind: "video", title: "历史街景", description: "街道镜头", status: "processing", revision: 3,
    copyright_status: "licensed", review_status: "pending",
    metadata: { tags: ["街景"], poster: { object_key: "workspaces/test/poster.jpg" }, source: { id: "source-1", source_type: "website", provider: "archive", license: "CC-BY" } },
    file: { id: "file-1", original_filename: "street.mp4", media_type: "video/mp4", byte_size: 2048, content_hash: "a".repeat(64), scan_status: "clean", width: 1920, height: 1080, duration_ms: 8000 },
    latest_analysis: { id: "analysis-1", analysis_version: 2, provider: "vision", model: "v2", status: "completed", summary: "街道", people: [], organizations: [], locations: ["北京"], eras: [], scene_types: ["street"], actions: [], moods: [], visual_styles: [], keywords: ["历史"], has_embedded_text: false, has_watermark: false, confidence: 0.91, created_at: now },
    usage_history: [], review_history: [], created_at: now, updated_at: now,
  };
  const job = { id: "job-1", library_id: libraryId, source_root: "reanalyze", status: "pending", discovered_count: 1, imported_count: 0, deduplicated_count: 0, tagged_count: 0, rejected_count: 0, created_at: now, updated_at: now };
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fetcher = async (url, init = {}) => {
    const parsed = new URL(String(url));
    const request = { url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
    requests.push(request);
    if (parsed.pathname === `/v1/asset-libraries/${libraryId}`) return json({ id: libraryId, asset_count: 1265 });
    if (parsed.pathname === `/v1/asset-libraries/${libraryId}/assets`) return json({ data: [asset], page: { has_more: true, next_cursor: "cursor-50", limit: 50 } });
    if (parsed.pathname === `/v1/assets/${assetId}/downloads/preview`) return json({ url: "https://media.test/original.mp4", variant: "original", requested_variant: "preview", fallback: true, media_type: "video/mp4", byte_size: 2048, expires_at: now });
    if (parsed.pathname === `/v1/assets/${assetId}/downloads/poster`) return json({ url: "https://media.test/poster.jpg", variant: "poster", requested_variant: "poster", fallback: false, media_type: "image/jpeg", byte_size: 512, expires_at: now });
    if (parsed.pathname === `/v1/assets/${assetId}/segments`) {
      const ordinal = parsed.searchParams.get("cursor") ? 1 : 0;
      return json({ data: [{ id: `segment-${ordinal}`, ordinal, start_ms: ordinal * 4000, end_ms: (ordinal + 1) * 4000, description: `镜头 ${ordinal}`, people: [], locations: [], keywords: [] }], page: { has_more: ordinal === 0, next_cursor: ordinal === 0 ? "segment-next" : null } });
    }
    if (parsed.pathname === `/v1/asset-libraries/${libraryId}/import-jobs`) return json({ data: [job], page: { has_more: false, next_cursor: null } });
    if (parsed.pathname === "/v1/assets/batch-reanalyze") return json({ succeeded_count: request.body.items.length, failed_count: 0, succeeded: [], failures: [] }, 202);
    if (parsed.pathname === `/v1/assets/${assetId}/reanalyze`) return json(job, 202);
    if (parsed.pathname === `/v1/assets/${assetId}/status-transitions`) return json({ ...asset, revision: 4, status: request.body.target_status });
    if (parsed.pathname === `/v1/assets/${assetId}/analyses`) return json({ data: [asset.latest_analysis], page: { has_more: false, next_cursor: null } });
    if (parsed.pathname === `/v1/assets/${assetId}/sources`) return json({ data: [asset.metadata.source], page: { has_more: false, next_cursor: null } });
    if (parsed.pathname === `/v1/assets/${assetId}/usage-records` || parsed.pathname === `/v1/assets/${assetId}/audit-events`) return json({ data: [], page: { has_more: false, next_cursor: null } });
    if (parsed.pathname === `/v1/assets/${assetId}/restore`) return json({ ...asset, deleted_at: null });
    if (parsed.pathname === `/v1/assets/${assetId}` && request.method === "DELETE") return json({ ...asset, revision: 4, status: "deleted" });
    if (parsed.pathname === `/v1/assets/${assetId}` && request.method === "PATCH") return json({ ...asset, ...request.body });
    if (parsed.pathname === `/v1/assets/${assetId}`) return json(asset);
    return json({ code: "NOT_FOUND", message: parsed.pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });

  const listed = await adapter.listAssets(libraryId, { search: "街景", status: "processing", kind: "video", copyrightStatus: "licensed", reviewStatus: "pending", tag: "历史" });
  assert.equal(listed.ok && listed.data.totalCount, 1265);
  assert.equal(listed.ok && listed.data.nextCursor, "cursor-50");
  assert.deepEqual(listed.ok && listed.data.data[0].tags.map((tag) => tag.name), ["街景"]);
  assert.equal(listed.ok && listed.data.data[0].posterAvailable, true);
  assert.equal(listed.ok && listed.data.data[0].source.provider, "archive");
  const listUrl = new URL(requests[0].url);
  assert.equal(listUrl.searchParams.get("limit"), "50");
  for (const [key, value] of [["q", "街景"], ["status", "processing"], ["kind", "video"], ["copyright_status", "licensed"], ["tag", "历史"]]) assert.equal(listUrl.searchParams.get(key), value);
  assert.equal(listUrl.searchParams.has("review_status"), false);

  await adapter.getAsset(assetId);
  await adapter.updateAsset(assetId, 3, { title: "更新标题", tags: ["街景", "历史"] }, "asset-update-key");
  await adapter.reviewAsset(assetId, 3, { decision: "approve", comment: "来源清晰" }, "asset-review-key");
  await adapter.reanalyzeAsset(assetId, 3, "asset-reanalyze-key");
  await adapter.deleteAsset(assetId, 3, "asset-delete-key");
  await adapter.restoreAsset(assetId, 3, "asset-restore-key");
  const bulk = await adapter.bulkUpdateAssets({ items: [{ assetId, revision: 3 }], action: "reanalyze" }, "asset-bulk-key");
  assert.equal(bulk.ok && bulk.data.acceptedCount, 1);
  const preview = await adapter.getAssetPreview(assetId);
  assert.equal(preview.ok && preview.data.url, "https://media.test/original.mp4");
  assert.equal(preview.ok && preview.data.sourceVariant, "original");
  assert.equal(preview.ok && preview.data.isFallback, true);
  assert.equal(preview.ok && preview.data.byteSize, 2048);
  const poster = await adapter.getAssetPoster(assetId);
  assert.equal(poster.ok && poster.data.url, "https://media.test/poster.jpg");
  assert.equal(poster.ok && poster.data.mediaType, "image/jpeg");
  const segments = await adapter.listAssetSegments(assetId);
  assert.equal(segments.ok && segments.data.length, 2);
  const jobs = await adapter.listAssetIngestionJobs({ libraryId });
  assert.equal(jobs.ok && jobs.data.data[0].id, "job-1");

  const patchRequest = requests.find((item) => item.method === "PATCH");
  assert.equal(patchRequest.headers.get("Idempotency-Key"), "asset-update-key");
  assert.equal(patchRequest.headers.get("If-Match"), '"3"');
  assert.deepEqual(patchRequest.body, { title: "更新标题", tags: ["街景", "历史"] });
  assert.equal("status" in patchRequest.body, false);
  for (const key of ["asset-review-key", "asset-reanalyze-key", "asset-delete-key", "asset-restore-key", "asset-bulk-key"]) {
    assert.ok(requests.some((item) => item.headers.get("Idempotency-Key") === key), key);
  }
});

test("HTTP adapter maps real resources and sends mutation control headers", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const skill = {
    id: "44444444-4444-4444-8444-444444444444",
    workspace_id: "22222222-2222-4222-8222-222222222222",
    ownership_type: "workspace",
    publisher_type: "user",
    publisher_name: "My Vistora",
    name: "HTTP Skill",
    slug: "http-skill",
    description: "Transport fixture",
    visibility: "private",
    status: "draft",
    current_version_id: null,
    forked_from_skill_id: null,
    created_by: "11111111-1111-4111-8111-111111111111",
    created_at: "2026-08-15T00:00:00Z",
    updated_at: "2026-08-15T00:00:00Z",
  };
  const version = {
    schema_version: "1.0.0",
    id: "55555555-5555-4555-8555-555555555555",
    skill_id: skill.id,
    version: "0.1.0",
    state: "draft",
    revision: 7,
    input_schema: { type: "object", title: "Input", properties: {}, required: [], additionalProperties: false },
    research_policy: { fact_treatment: "cite", allowed_source_types: ["official"], blocked_domains: [] },
    writing_policy: { markdown_instructions: "Write clearly." },
    visual_policy: { visual_tags: ["editorial"] },
    asset_policy: { allowed_kinds: ["image"], fallback: "fail" },
    qc_policy: { rules: [] },
    capability_requirements: [],
    output_contract: { artifacts: [] },
    default_pipeline_version_id: null,
    test_topics: [],
    release_notes: "",
    content_hash: "a".repeat(64),
    created_by: skill.created_by,
    created_at: skill.created_at,
    published_at: null,
  };
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fetcher = async (url, init = {}) => {
    requests.push({ url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined });
    const pathname = new URL(String(url)).pathname;
    if (pathname === `/v1/skills/${skill.id}`) return json(skill);
    if (pathname === "/v1/skill-versions" && (init.method ?? "GET") === "GET") return json({ data: [version], page: { has_more: false, next_cursor: null, limit: 100 } });
    if (pathname === "/v1/skill-versions" && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      return json({ ...version, ...body, id: "66666666-6666-4666-8666-666666666666", revision: 1 }, 201);
    }
    if (pathname.endsWith("/validate")) return json({ skill_id: skill.id, version_id: version.id, ready: true, revision: 8, checks: [], estimated_cost: { amount: 0, currency: "CNY" } });
    if (pathname.endsWith("/publish")) return json({ ...version, state: "published", revision: 8, release_notes: init.body ? JSON.parse(String(init.body)).release_notes : "" });
    if (pathname === "/v1/runs" && (init.method ?? "GET") === "GET") return json({ data: [], page: { has_more: false, next_cursor: null, limit: 100 } });
    if (pathname === "/v1/runs" && init.method === "POST") return json({
      id: "99999999-9999-4999-8999-999999999999", workspace_id: skill.workspace_id, channel_id: null,
      status: "queued", input: { topic: "HTTP run topic" }, composition_snapshot: {
        skill_version: { id: version.id, content_hash: "a".repeat(64) }, asset_library_ids: [], voice_profile_id: null,
        render_preset: null, pipeline_version: { id: "88888888-8888-4888-8888-888888888888", content_hash: "b".repeat(64) }, capabilities: [],
      }, created_by: skill.created_by, created_at: skill.created_at, updated_at: skill.updated_at,
    }, 201);
    return json({ code: "NOT_FOUND", message: pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher, testPollIntervalMs: 0 });

  const detail = await adapter.getSkill(skill.id);
  assert.equal(detail.ok, true);
  assert.equal(detail.ok && detail.data.skill.name, "HTTP Skill");

  const nextDraft = await adapter.createDraft(skill.id, version.id);
  assert.equal(nextDraft.ok, true);
  const createDraftRequest = requests.find((item) => item.url.endsWith("/v1/skill-versions") && item.method === "POST");
  assert.equal(createDraftRequest.body.version, "0.1.1");
  assert.equal(createDraftRequest.body.source_version_id, version.id);

  const validation = await adapter.validateVersion(skill.id, version.id);
  assert.equal(validation.ok, true);
  assert.equal(validation.ok && validation.data.revision, 8);
  const validateRequest = requests.find((item) => item.url.endsWith("/validate"));
  assert.equal(validateRequest.headers.get("If-Match"), "7");
  assert.ok(validateRequest.headers.get("Idempotency-Key"));

  const published = await adapter.publishVersion(skill.id, version.id, "ready");
  assert.equal(published.ok, true);
  const publishRequest = requests.find((item) => item.url.endsWith("/publish"));
  assert.equal(publishRequest.headers.get("If-Match"), "8");
  assert.deepEqual(publishRequest.body, { release_notes: "ready" });

  const run = await adapter.createRun({
    workspaceId: skill.workspace_id,
    topic: "HTTP run topic",
    composition: { skillVersionId: version.id, assetLibraryIds: [], pipelineVersionId: "88888888-8888-4888-8888-888888888888" },
    videoSettings: {
      language: "zh-CN", aspectRatio: "1:1", targetDurationSeconds: 75,
      visibility: "private", autoQualityCheck: true, layout: "editorial",
      mediaFit: "contain", frameRate: 25,
      subtitles: { enabled: true, position: "lower_third", size: "large", maxLines: 2 },
      assetAcquisition: { enabled: true, sources: ["bilibili"], maxAssets: 2, copyrightStatus: "licensed", rightsConfirmed: true },
    },
  }, "run-key-0001");
  assert.equal(run.ok && run.data.topic, "HTTP run topic");
  const runRequest = requests.find((item) => item.url.endsWith("/v1/runs") && item.method === "POST");
  assert.equal(runRequest.headers.get("Idempotency-Key"), "run-key-0001");
  assert.equal(runRequest.body.composition.skill_version_id, version.id);
  assert.equal("content_hash" in runRequest.body.composition, false);
  assert.equal(runRequest.body.video_settings.aspect_ratio, "1:1");
  assert.equal(runRequest.body.video_settings.layout, "editorial");
  assert.equal(runRequest.body.video_settings.subtitles.position, "lower_third");
  assert.equal(runRequest.body.video_settings.asset_acquisition.enabled, true);
  assert.deepEqual(runRequest.body.video_settings.asset_acquisition.sources, ["bilibili"]);
});

test("account transport preserves ETags and never reads plaintext from API key listings", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const now = "2026-08-16T08:00:00Z";
  const profile = {
    user_id: "11111111-1111-4111-8111-111111111111",
    workspace_id: "22222222-2222-4222-8222-222222222222",
    email: "creator@example.com",
    display_name: "Creator",
    avatar_url: null,
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
    revision: 3,
    updated_at: now,
  };
  const preferences = {
    user_id: profile.user_id,
    workspace_id: profile.workspace_id,
    default_language: "zh-CN",
    default_aspect_ratio: "16:9",
    default_duration_seconds: 180,
    default_visibility: "private",
    auto_quality_check: true,
    revision: 5,
    updated_at: now,
  };
  const key = {
    id: "33333333-3333-4333-8333-333333333333",
    workspace_id: profile.workspace_id,
    name: "Automation",
    key_prefix: "ffk_example",
    scopes: ["skills:read"],
    created_at: now,
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
  };
  const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
  const fetcher = async (url, init = {}) => {
    const request = { url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
    requests.push(request);
    const pathname = new URL(request.url).pathname;
    if (pathname === "/v1/account/profile") return json({ ...profile, display_name: request.body?.display_name ?? profile.display_name, revision: request.method === "PUT" ? 4 : 3 }, 200, { ETag: request.method === "PUT" ? '"4"' : '"3"' });
    if (pathname === "/v1/account/creation-preferences") return json({ ...preferences, default_aspect_ratio: request.body?.default_aspect_ratio ?? preferences.default_aspect_ratio, revision: request.method === "PUT" ? 6 : 5 }, 200, { ETag: request.method === "PUT" ? '"6"' : '"5"' });
    if (pathname === "/v1/account/api-keys" && request.method === "POST") return json({ key, api_key: "ffk_one_time_secret" }, 201);
    if (pathname === "/v1/account/api-keys") return json([key]);
    if (pathname.endsWith(key.id) && request.method === "DELETE") return new Response(null, { status: 204 });
    return json({ code: "NOT_FOUND", message: pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });

  const loadedProfile = await adapter.getAccountProfile();
  assert.equal(loadedProfile.ok && loadedProfile.data.etag, '"3"');
  const updatedProfile = await adapter.replaceAccountProfile({ displayName: "Director", locale: "zh-CN", timezone: "Asia/Shanghai" }, '"3"');
  assert.equal(updatedProfile.ok && updatedProfile.data.value.displayName, "Director");
  const profilePut = requests.find((item) => item.url.endsWith("/v1/account/profile") && item.method === "PUT");
  assert.equal(profilePut.headers.get("If-Match"), '"3"');
  assert.equal(profilePut.body.display_name, "Director");

  const loadedPreferences = await adapter.getCreationPreferences();
  assert.equal(loadedPreferences.ok && loadedPreferences.data.etag, '"5"');
  await adapter.replaceCreationPreferences({ defaultLanguage: "zh-CN", defaultAspectRatio: "9:16", defaultDurationSeconds: 90, defaultVisibility: "private", autoQualityCheck: true }, '"5"');
  const preferencesPut = requests.find((item) => item.url.endsWith("/creation-preferences") && item.method === "PUT");
  assert.equal(preferencesPut.headers.get("If-Match"), '"5"');
  assert.equal(preferencesPut.body.default_aspect_ratio, "9:16");

  const listedKeys = await adapter.listApiKeys();
  assert.equal(listedKeys.ok && "plaintext" in listedKeys.data[0], false);
  const created = await adapter.createApiKey({ name: "Automation", scopes: ["skills:read"] });
  assert.equal(created.ok && created.data.plaintext, "ffk_one_time_secret");
  const revoked = await adapter.revokeApiKey(key.id);
  assert.equal(revoked.ok, true);
});

test("SkillSpec canonical mapping round-trips fields the form does not edit", async () => {
  const { canonicalVersionToSkillSpec, skillSpecToCanonicalPatch } = await import("../lib/api/http-adapter.ts");
  const canonical = {
    input_schema: {
      type: "object", title: "Deep input", description: "Preserve constraints",
      properties: { topic: { type: "string", title: "Topic", minLength: 3, maxLength: 300, format: "multiline" } },
      required: ["topic"], additionalProperties: false,
    },
    research_policy: {
      mode: "optional", allowed_source_types: ["academic", "user_provided"], require_https: true,
      fact_treatment: "exclude_unverified", citation_style: "endnotes", minimum_sources: 2,
      maximum_sources: 17, freshness_days: null, blocked_domains: ["example.com"],
    },
    writing_policy: {
      language: "en-US", target_duration_seconds: 317, tone_tags: ["precise", "warm"],
      structure: [{ name: "opening", purpose: "Open", target_share: 1 }],
      markdown_instructions: "Keep every canonical field while editing this instruction.",
      prohibited_content: ["fabrication"],
    },
    visual_policy: {
      aspect_ratio: "4:5", frame_rate: 25, visual_tags: ["documentary", "measured"],
      shot_duration_seconds: { minimum: 1.2, target: 4.5, maximum: 13 },
      text_safe_area_percent: 7, generated_media_allowed: true,
    },
    asset_policy: {
      library_binding: "none", allowed_kinds: ["video", "audio"], minimum_assets: 8,
      license_required: false, fallback: "generated", deduplicate_by_hash: true,
    },
    qc_policy: {
      rules: [{ id: "rights-check", category: "copyright", description: "Rights recorded", severity: "warning", action: "request_review" }],
      human_review: "always", minimum_pass_rate: 0.87,
    },
    capability_requirements: [{ name: "render.video", level: "optional", minimum_version: "2.3.4" }],
    output_contract: {
      artifacts: [
        { name: "final-video", kind: "video", media_types: ["video/mp4"], required: true, maximum_bytes: 987654321 },
        { name: "quality-report", kind: "qc_report", media_types: ["application/json"], required: false, maximum_bytes: 7654321 },
      ],
      allow_undeclared_artifacts: true,
    },
    default_pipeline_version_id: "88888888-8888-4888-8888-888888888888",
  };
  const ui = canonicalVersionToSkillSpec(canonical);
  const patch = skillSpecToCanonicalPatch(ui, canonical);
  for (const key of [
    "input_schema", "research_policy", "writing_policy", "visual_policy", "asset_policy",
    "qc_policy", "capability_requirements", "output_contract", "default_pipeline_version_id",
  ]) assert.deepEqual(patch[key], canonical[key], key);
  assert.equal(ui.modelRequirements.preferredModel, undefined);
  assert.deepEqual(ui.visualPolicy.shotGuidance, ["measured"]);
});

test("failed validation revision can immediately save the same mutable version", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const now = "2026-08-20T00:00:00Z";
  const skill = {
    id: "44444444-4444-4444-8444-444444444444", workspace_id: "22222222-2222-4222-8222-222222222222",
    ownership_type: "workspace", publisher_type: "user", publisher_name: "Me", name: "Mutable Skill",
    slug: "mutable-skill", description: "test", visibility: "private", status: "draft", current_version_id: null,
    created_by: "11111111-1111-4111-8111-111111111111", created_at: now, updated_at: now,
  };
  const version = {
    schema_version: "1.0.0", id: "55555555-5555-4555-8555-555555555555", skill_id: skill.id,
    version: "0.1.0", state: "draft", revision: 4,
    input_schema: { type: "object", title: "Input", properties: { topic: { type: "string", title: "Topic" } }, required: ["topic"], additionalProperties: false },
    research_policy: { mode: "required", allowed_source_types: ["official"], require_https: true, fact_treatment: "cite", citation_style: "artifact_only", minimum_sources: 1, maximum_sources: 10, freshness_days: 365, blocked_domains: [] },
    writing_policy: { language: "en-US", target_duration_seconds: 42, tone_tags: ["clear"], structure: [{ name: "main", purpose: "Explain", target_share: 1 }], markdown_instructions: "too short", prohibited_content: [] },
    visual_policy: { aspect_ratio: "9:16", frame_rate: 30, visual_tags: ["editorial"], shot_duration_seconds: { minimum: 1, target: 3, maximum: 8 }, text_safe_area_percent: 10, generated_media_allowed: false },
    asset_policy: { library_binding: "run_composition", allowed_kinds: ["video"], minimum_assets: 2, license_required: true, fallback: "fail", deduplicate_by_hash: true },
    qc_policy: { rules: [{ id: "clarity", category: "writing", description: "Clear", severity: "error", action: "fail" }], human_review: "always", minimum_pass_rate: 0.9 },
    capability_requirements: [{ name: "render.video", level: "required", minimum_version: "3.0.0" }],
    output_contract: { artifacts: [{ name: "final-video", kind: "video", media_types: ["video/mp4"], required: true, maximum_bytes: 1024 }], allow_undeclared_artifacts: false },
    default_pipeline_version_id: null, test_topics: [], release_notes: "", content_hash: "a".repeat(64),
    created_by: skill.created_by, created_at: now, published_at: null,
  };
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fetcher = async (url, init = {}) => {
    const request = { url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
    requests.push(request);
    const pathname = new URL(request.url).pathname;
    if (pathname === `/v1/skills/${skill.id}`) return json(skill);
    if (pathname === "/v1/skill-versions" && request.method === "GET") return json({ data: [version], page: { has_more: false, next_cursor: null } });
    if (pathname === "/v1/runs") return json({ data: [], page: { has_more: false, next_cursor: null } });
    if (pathname.endsWith("/validate")) return json({ skill_id: skill.id, version_id: version.id, ready: false, revision: 5, checks: [{ id: "instructions", label: "Writing", passed: false, severity: "error", message: "too short" }], estimated_cost: { amount: 0, currency: "CNY" } });
    if (pathname === `/v1/skill-versions/${version.id}` && request.method === "PATCH") return json({ ...version, ...request.body, state: "draft", revision: 6 });
    return json({ code: "NOT_FOUND", message: pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });
  const detail = await adapter.getSkill(skill.id);
  assert.equal(detail.ok, true);
  const validation = await adapter.validateVersion(skill.id, version.id);
  assert.equal(validation.ok && validation.data.ready, false);
  assert.equal(validation.ok && validation.data.revision, 5);
  const saved = await adapter.saveDraft(skill.id, version.id, { spec: { ...detail.data.versions[0].spec, writingInstructions: "This instruction is now long enough to validate successfully." } }, 5);
  assert.equal(saved.ok && saved.data.state, "draft");
  const patchRequest = requests.find((request) => request.method === "PATCH");
  assert.equal(patchRequest.headers.get("If-Match"), "5");
  assert.equal(patchRequest.body.writing_policy.language, "en-US");
  assert.equal(patchRequest.body.writing_policy.target_duration_seconds, 42);
  assert.equal(patchRequest.body.qc_policy.human_review, "always");
  assert.equal(patchRequest.body.capability_requirements[0].minimum_version, "3.0.0");
});

test("channel transport follows the canonical response, search, and archive contracts", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const channelId = "33333333-3333-4333-8333-333333333333";
  const workspaceId = "22222222-2222-4222-8222-222222222222";
  const now = "2026-08-20T08:00:00Z";
  const channel = {
    schema_version: "1.0.0",
    id: channelId,
    workspace_id: workspaceId,
    ownership_type: "workspace",
    slug: "daily-stories",
    name: "日常故事",
    description: "每天发布一个真实故事",
    platform: "youtube",
    handle: "@daily-stories",
    platform_connection_id: "44444444-4444-4444-8444-444444444444",
    status: "active",
    revision: 4,
    default_composition: {
      skill_version_id: "skill-version-1",
      pipeline_version_id: "pipeline-version-1",
      asset_library_ids: ["library-1", "library-2"],
      voice_profile_id: "voice-1",
      render_preset_version_id: "render-version-1",
    },
    brand_profile: { logo_asset_id: "asset-logo", tone: "clear", primary_color: "#FF6B35", accent_color: "#000000" },
    created_by: "11111111-1111-4111-8111-111111111111",
    created_at: now,
    updated_at: now,
  };
  const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
  const fetcher = async (url, init = {}) => {
    const request = { url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
    requests.push(request);
    const parsed = new URL(request.url);
    if (parsed.pathname === "/v1/channels" && request.method === "GET") return json({ data: [channel], page: { has_more: false, next_cursor: null } });
    if (parsed.pathname === `/v1/channels/${channelId}` && request.method === "GET") return json(channel, 200, { ETag: '"4"' });
    if (parsed.pathname === `/v1/channels/${channelId}` && request.method === "PUT") return json({ ...channel, status: request.body.status, revision: 5 }, 200, { ETag: '"5"' });
    if (parsed.pathname === `/v1/channels/${channelId}` && request.method === "DELETE") return new Response(null, { status: 204 });
    return json({ code: "NOT_FOUND", message: parsed.pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });

  const listed = await adapter.listChannels({ workspaceId, search: "故事", platform: "youtube", status: "active" });
  assert.equal(listed.ok && listed.data[0].defaultComposition.voiceProfileId, "voice-1");
  assert.deepEqual(listed.ok && listed.data[0].defaultComposition.assetLibraryIds, ["library-1", "library-2"]);
  const listUrl = new URL(requests[0].url);
  assert.equal(listUrl.searchParams.has("workspace_id"), false);
  assert.equal(listUrl.searchParams.has("q"), false);
  assert.equal(listUrl.searchParams.get("search"), "故事");
  assert.equal(listUrl.searchParams.get("platform"), "youtube");
  assert.equal(listUrl.searchParams.get("status"), "active");

  const loaded = await adapter.getChannel(channelId);
  assert.equal(loaded.ok && loaded.data.etag, '"4"');
  assert.equal(loaded.ok && loaded.data.value.slug, "daily-stories");
  assert.equal(loaded.ok && loaded.data.value.revision, 4);
  assert.equal(loaded.ok && loaded.data.value.createdBy, "11111111-1111-4111-8111-111111111111");
  assert.equal(loaded.ok && Object.hasOwn(loaded.data.value, "platformConnections"), false);
  assert.equal(loaded.ok && Object.hasOwn(loaded.data.value, "operationRecords"), false);
  assert.equal(loaded.ok && loaded.data.value.brandConfig.profile.tone, "clear");
  assert.equal(loaded.ok && Object.hasOwn(loaded.data.value.brandConfig.profile, "primary_color"), false);
  assert.equal(loaded.ok && Object.hasOwn(loaded.data.value.brandConfig.profile, "accent_color"), false);
  assert.equal(loaded.ok && loaded.data.value.brandConfig.accentColor, "#FF6B35");
  const saved = await adapter.saveChannel({
    id: channelId,
    workspaceId,
    slug: channel.slug,
    name: channel.name,
    description: channel.description,
    platform: channel.platform,
    handle: channel.handle,
    platformConnectionId: channel.platform_connection_id,
    status: "paused",
    defaultComposition: loaded.ok ? loaded.data.value.defaultComposition : { skillVersionId: "", pipelineVersionId: "", assetLibraryIds: [] },
    brandConfig: loaded.ok ? loaded.data.value.brandConfig : { profile: {} },
  }, '"4"', "channel-status-key");
  assert.equal(saved.ok && saved.data.value.status, "paused");
  assert.equal(saved.ok && saved.data.etag, '"5"');
  const put = requests.find((request) => request.method === "PUT");
  assert.equal(put.headers.get("If-Match"), '"4"');
  assert.equal(put.headers.get("Idempotency-Key"), "channel-status-key");
  assert.equal(put.body.default_composition.skill_version_id, "skill-version-1");
  assert.equal(put.body.default_composition.pipeline_version_id, "pipeline-version-1");
  assert.equal(put.body.default_composition.voice_profile_id, "voice-1");
  assert.equal(put.body.default_composition.render_preset_version_id, "render-version-1");
  assert.deepEqual(put.body.default_composition.asset_library_ids, ["library-1", "library-2"]);
  assert.equal(put.body.platform_connection_id, channel.platform_connection_id);
  assert.equal(put.body.brand_profile.primary_color, "#FF6B35");
  assert.equal(put.body.brand_profile.tone, "clear");
  assert.equal(Object.hasOwn(put.body.brand_profile, "accent_color"), false);

  const archived = await adapter.archiveChannel(channelId, '"5"');
  assert.equal(archived.ok, true);
  const deletion = requests.find((request) => request.method === "DELETE");
  assert.equal(deletion.headers.get("If-Match"), '"5"');
});

test("Run detail transport reads durable steps and sends cancel and review commands", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const now = "2026-08-16T09:00:00Z";
  const runId = "99999999-9999-4999-8999-999999999999";
  const stepId = "77777777-7777-4777-8777-777777777777";
  const run = {
    id: runId,
    workspace_id: "22222222-2222-4222-8222-222222222222",
    channel_id: null,
    status: "awaiting_review",
    input: { topic: "需要真实审核的项目" },
    composition_snapshot: {
      skill_version: { id: "55555555-5555-4555-8555-555555555555" },
      pipeline_version: { id: "88888888-8888-4888-8888-888888888888" },
      asset_library_ids: [],
      voice_profile_id: null,
      render_preset: null,
    },
    created_by: "11111111-1111-4111-8111-111111111111",
    created_at: now,
    updated_at: now,
    cancel_requested_at: null,
  };
  const step = {
    id: stepId,
    run_id: runId,
    node_key: "quality",
    operation: "quality",
    status: "awaiting_review",
    attempt: 1,
    maximum_attempts: 3,
    dependencies: ["render"],
    required_capabilities: ["quality.evaluate"],
    input_snapshot: { topic: "需要真实审核的项目" },
    output_summary: { verdict: "needs_review" },
    output_artifacts: [],
    revision: 2,
    review_required: true,
    review: { requested_at: now },
    error: null,
    updated_at: now,
  };
  const artifactId = "66666666-6666-4666-8666-666666666666";
  const artifact = {
    id: artifactId,
    run_id: runId,
    kind: "video",
    filename: "final-video.mp4",
    media_type: "video/mp4",
    byte_size: 1024,
    content_hash: "a".repeat(64),
    created_at: now,
  };
  step.output_artifacts = [artifact];
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const fetcher = async (url, init = {}) => {
    const request = { url: String(url), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body ? JSON.parse(String(init.body)) : undefined };
    requests.push(request);
    const parsed = new URL(request.url);
    if (parsed.pathname === `/v1/runs/${runId}/cancel`) return json({ ...run, cancel_requested_at: now }, 202);
    if (parsed.pathname === `/v1/runs/${runId}`) return json(run);
    if (parsed.pathname === "/v1/steps" && parsed.searchParams.get("run_id") === runId) return json({ data: [step], page: { has_more: false, next_cursor: null, limit: 100 } });
    if (parsed.pathname === "/v1/artifacts" && parsed.searchParams.get("run_id") === runId) return json({ data: [artifact], page: { has_more: false, next_cursor: null, limit: 100 } });
    if (parsed.pathname === `/v1/steps/${stepId}/review`) return json({ ...step, status: "retrying", review: { decision: "revise", reason: request.body.reason, decided_at: now } }, 202);
    return json({ code: "NOT_FOUND", message: parsed.pathname }, 404);
  };
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: fetcher });

  const loaded = await adapter.getRun(runId);
  assert.equal(loaded.ok, true);
  assert.equal(loaded.ok && loaded.data.steps[0].label, "质量检查");
  assert.equal(loaded.ok && loaded.data.steps[0].reviewRequired, true);
  assert.equal(loaded.ok && loaded.data.steps[0].revision, 2);
  assert.equal(loaded.ok && loaded.data.steps[0].outputSummary.verdict, "needs_review");
  assert.equal(loaded.ok && loaded.data.steps[0].artifacts[0].contentUrl, `http://api.test/v1/artifacts/${artifactId}/content`);
  assert.equal(loaded.ok && loaded.data.artifacts[0].contentUrl, `http://api.test/v1/artifacts/${artifactId}/content`);

  const cancelled = await adapter.cancelRun(runId, "cancel-key-0001");
  assert.equal(cancelled.ok && cancelled.data.cancellationRequestedAt, now);
  const cancelRequest = requests.find((item) => item.url.endsWith(`/v1/runs/${runId}/cancel`));
  assert.equal(cancelRequest.headers.get("Idempotency-Key"), "cancel-key-0001");

  const reviewed = await adapter.reviewRunStep(stepId, {
    decision: "revise",
    comment: "补充引用",
    issueCodes: ["factuality.unsupported"],
    expectedRevision: 2,
  }, "review-key-0001");
  assert.equal(reviewed.ok && reviewed.data.status, "retrying");
  const reviewRequest = requests.find((item) => item.url.endsWith(`/v1/steps/${stepId}/review`));
  assert.equal(reviewRequest.headers.get("Idempotency-Key"), "review-key-0001");
  assert.deepEqual(reviewRequest.body, {
    decision: "revise",
    reason: "补充引用",
    issue_codes: ["factuality.unsupported"],
    expected_revision: 2,
  });
});
