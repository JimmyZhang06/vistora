import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  fullAiPendingAttemptKey,
  readFullAiSubmissionAttempt,
  saveFullAiSubmissionAttempt,
  shouldRetainFullAiAttempt,
} from "../lib/full-ai-submission.ts";

const root = new URL("../", import.meta.url);
const rootPath = fileURLToPath(root);

async function source(pathname) {
  return readFile(new URL(pathname, root), "utf8");
}

async function collectSources(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const output = [];
  for (const entry of entries) {
    if (["node_modules", "dist", ".next", ".wrangler", "tests"].includes(entry.name)) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) output.push(...await collectSources(path));
    else if ([".ts", ".tsx", ".css", ".md", ".json"].includes(extname(entry.name))) output.push(path);
  }
  return output;
}

test("shell exposes seven primary destinations and accessible navigation", async () => {
  const [shell, catalogs] = await Promise.all([
    source("components/app-shell.tsx"),
    source("lib/i18n/catalogs.ts"),
  ]);
  for (const key of ["create", "projects", "batches", "skills", "assets", "channels", "settings"]) {
    assert.match(shell, new RegExp(`label: "shell\\.nav\\.${key}"`));
  }
  for (const label of ["创作", "项目", "批量生产", "素材", "频道", "账户与设置", "Create", "Projects", "Batch production", "Assets", "Channels", "Account & settings"]) assert.match(catalogs, new RegExp(label));
  assert.match(shell, /className="skip-link"/);
  assert.match(shell, /aria-current=\{current \? "page"/);
  assert.match(shell, /aria-expanded=\{menuOpen\}/);
  assert.match(shell, /showModal\(\)/);
});

test("page identity and review workbench follow the shared layout grid", async () => {
  const [create, skills, creator, assets, channels, settings, detail, css] = await Promise.all([
    source("components/create-composer.tsx"),
    source("components/skill-library.tsx"),
    source("components/skill-creator.tsx"),
    source("components/asset-hub.tsx"),
    source("components/channels-view.tsx"),
    source("components/settings-view.tsx"),
    source("components/run-detail.tsx"),
    source("app/globals.css"),
  ]);
  assert.match(skills, /04 \/ SKILL STUDIO/);
  assert.match(creator, /04 \/ SKILL \/ NEW/);
  assert.match(assets, /05 \/ ASSETS/);
  assert.match(channels, /06 \/ CHANNELS/);
  assert.match(settings, /07 \/ ACCOUNT & SETTINGS/);
  assert.doesNotMatch(create, /cinema-board|film-frame|research-planning\.webp|CONTROLLED WORKFLOW/);
  for (const destination of ["ai", "webpage-video", "projects"]) {
    assert.match(create, new RegExp(`/${destination}`));
  }
  assert.match(detail, /review-drawer-header/);
  assert.match(detail, /review-artifact-grid--single/);
  assert.match(detail, /查看最终成片/);
  assert.match(detail, /evidenceLabel/);
  assert.match(detail, /review-technical-details/);
  assert.match(detail, /function EvidenceValue/);
  assert.match(css, /\.run-step-action \{[^}]*width: 126px/);
  assert.match(css, /\.review-drawer-header \{[^}]*grid-template-columns/);
  assert.match(css, /\.review-evidence \{[^}]*align-items: start/);
  assert.doesNotMatch(css, /\.review-evidence dd \{[^}]*overflow: auto/);
});

test("full-AI creation uses only its typed generated-only API and fail-closed state machine", async () => {
  const [route, studio, composer, adapter, contracts, css] = await Promise.all([
    source("app/create/ai/page.tsx"),
    source("components/full-ai-video-studio.tsx"),
    source("components/create-composer.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/contracts.ts"),
    source("app/globals.css"),
  ]);
  assert.match(route, /FullAiVideoStudio/);
  assert.match(composer, /href="\/create\/ai"/);
  for (const copy of [
    "GENERATED ONLY",
    "生成 Provider 不可用",
    "服务端报价",
    "本地预览",
    "CandidateManifest",
    "已停止自动重试，需人工核账",
    "Run-scoped hash \\+ probe \\+ safety",
    "提示词连续性",
  ]) assert.match(studio, new RegExp(copy));
  for (const state of ["loading", "unavailable", "editing", "estimating", "ready", "blocked", "submitting", "submit_unknown", "created", "failed"]) {
    assert.match(studio, new RegExp(`"${state}"`));
  }
  for (const method of ["getFullAiOptions", "estimateFullAiRun", "createFullAiRun", "getFullAiRun"]) {
    assert.match(adapter, new RegExp(method));
    assert.match(studio, new RegExp(method));
  }
  assert.match(contracts, /mode: "generated_only"/);
  assert.match(studio, /localStorage/);
  assert.match(studio, /idempotencyKey/);
  assert.match(studio, /projectRunId/);
  assert.match(studio, /检查任务状态（仅查询）/);
  assert.match(studio, /shouldRetainFullAiAttempt/);
  assert.doesNotMatch(studio, /createRun\(|estimateRun\(/);
  assert.doesNotMatch(studio, /assetLibrary|asset_library|assetAcquisition|media\.retrieve|Runway/i);
  assert.match(css, /\.ai-studio-workbench \{/);
  assert.match(css, /@media \(max-width: 767px\)[\s\S]*?\.ai-studio-page/);
});

test("full-AI known runs persist until a clean terminal status", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const request = {
    brief: "一支完全生成的短片", direction: "cinematic", aspectRatio: "9:16",
    durationSeconds: 15, variantsPerScene: 2, continuity: true, aiDisclosure: true,
    estimateFingerprint: "a".repeat(64), maxCostMinor: 900, currency: "USD",
  };
  const attempt = {
    idempotencyKey: "full-ai-create:fixed-attempt",
    request,
    fullAiRunId: "11111111-1111-4111-8111-111111111111",
  };

  saveFullAiSubmissionAttempt(storage, attempt);
  assert.deepEqual(readFullAiSubmissionAttempt(storage), attempt);
  assert.match(storage.getItem(fullAiPendingAttemptKey), /fixed-attempt/);
  for (const status of ["queued", "planning", "generating", "assembling", "quality_check"]) {
    assert.equal(shouldRetainFullAiAttempt({ status, billing: { requiresReconciliation: false } }), true, status);
  }
  assert.equal(shouldRetainFullAiAttempt({ status: "failed", billing: { requiresReconciliation: true } }), true);
  for (const status of ["succeeded", "failed", "cancelled"]) {
    assert.equal(shouldRetainFullAiAttempt({ status, billing: { requiresReconciliation: false } }), false, status);
  }

  saveFullAiSubmissionAttempt(storage, null);
  assert.equal(readFullAiSubmissionAttempt(storage), null);
});

test("Skill creation only advertises implemented flows and imports the selected JSON package", async () => {
  const [creator, adapter] = await Promise.all([
    source("components/skill-creator.tsx"),
    source("lib/api/http-adapter.ts"),
  ]);
  assert.match(creator, /file\.text\(\)/);
  assert.match(creator, /parseSkillPackage/);
  assert.match(creator, /disabled=\{item\.available === false\}/);
  assert.match(creator, /package: \{ \.\.\.importPackage, identity \}/);
  assert.match(adapter, /distill_not_available/);
  assert.match(adapter, /method: "DELETE"/);
});

test("tabs, keyboard shortcuts, live regions, and responsive preferences are present", async () => {
  const [tabs, studio, composer, css] = await Promise.all([
    source("components/accessible-tabs.tsx"),
    source("components/skill-studio.tsx"),
    source("components/create-composer.tsx"),
    source("app/globals.css"),
  ]);
  assert.match(tabs, /role="tablist"/);
  assert.match(tabs, /aria-selected/);
  for (const key of ["ArrowRight", "ArrowLeft", "Home", "End"]) assert.match(tabs, new RegExp(key));
  assert.match(studio, /aria-live="polite"/);
  assert.match(studio, /event\.key\.toLowerCase\(\) === "s"/);
  assert.match(composer, /event\.key === "Enter"/);
  for (const copy of ["画面与字幕", "库内语义匹配", "仅检索缺口", "分析后重新匹配", "来源与下载预算", "许可可核验", "未配置下载供应商时任务会返回明确错误"]) {
    assert.match(composer, new RegExp(copy));
  }
  assert.match(css, /container-type: inline-size/);
  assert.match(css, /@container \(max-width: 680px\)/);
  assert.match(css, /@media \(max-width: 1439px\)[\s\S]*?\.composer-workbench \{ grid-template-columns: 1fr; \}/);
  assert.match(css, /@media \(max-width: 767px\)/);
  assert.match(css, /@media \(max-width: 479px\)/);
  assert.match(css, /\.select \{[\s\S]*?appearance: none/);
  assert.match(css, /\.select:focus-visible/);
  assert.match(css, /\.select option/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /forced-colors: active/);
  assert.match(css, /env\(safe-area-inset-bottom\)/);
});

test("custom selects preserve keyboard and listbox semantics", async () => {
  const [component, composer, settings, creator, studio] = await Promise.all([
    source("components/ui-select.tsx"),
    source("components/create-composer.tsx"),
    source("components/settings-view.tsx"),
    source("components/skill-creator.tsx"),
    source("components/skill-studio.tsx"),
  ]);
  assert.match(component, /role="combobox"/);
  assert.match(component, /role="listbox"/);
  assert.match(component, /role="option"/);
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "Escape"]) {
    assert.match(component, new RegExp(key));
  }
  for (const sourceText of [composer, settings, creator, studio]) {
    assert.doesNotMatch(sourceText, /<select\b/);
  }
});

test("typed adapter has a production HTTP transport and isolated mock fixture", async () => {
  const [contracts, adapter, http, mock] = await Promise.all([
    source("lib/api/contracts.ts"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
    source("lib/api/mock-adapter.ts"),
  ]);
  assert.match(adapter, /interface FrameFactoryAdapter/);
  assert.match(contracts, /publisher: Publisher/);
  assert.match(contracts, /permissions: SkillPermissions/);
  assert.match(http, /implements FrameFactoryAdapter/);
  assert.match(http, /Idempotency-Key/);
  assert.match(http, /If-Match/);
  assert.match(mock, /"normal" \| "empty" \| "error"/);
  assert.match(mock, /getComposerOptions/);
  assert.match(mock, /channel\.status === "active"/);
  assert.match(mock, /archiveChannel/);
  assert.match(mock, /"If-Match", 412/);
  assert.match(mock, /compareVersions/);
  assert.match(mock, /query\.channelId[^\n]+run\.channelId === query\.channelId/);
  assert.ok((mock.match(/platformConnectionId: draft\.platformConnectionId/g) ?? []).length >= 2);
});

test("interface locale is cookie-restored, account-synchronized, and separate from content language", async () => {
  const [layout, context, shell, settings, contracts] = await Promise.all([
    source("app/layout.tsx"),
    source("lib/i18n/context.tsx"),
    source("components/app-shell.tsx"),
    source("components/settings-view.tsx"),
    source("lib/api/contracts.ts"),
  ]);
  assert.match(layout, /<I18nProvider initialLocale="zh-CN">/);
  assert.match(context, /vistora_locale/);
  assert.match(context, /localeFromCookie/);
  assert.match(context, /document\.documentElement\.lang = locale/);
  assert.match(shell, /setLocale\(result\.data\.user\.locale\)/);
  assert.match(settings, /setLocale\(result\.data\.value\.locale\)/);
  assert.match(settings, /settings\.interfaceLanguage/);
  assert.match(settings, /settings\.contentLanguage/);
  assert.match(settings, /defaultVisibility: "private"/);
  assert.match(contracts, /locale: string/);
  assert.match(contracts, /timezone: string/);
});

test("Skill Studio exposes the complete lifecycle without replacing the adapter", async () => {
  const [library, studio, overviewRoute, adapter] = await Promise.all([
    source("components/skill-library.tsx"),
    source("components/skill-studio.tsx"),
    source("app/skills/[skillId]/page.tsx"),
    source("lib/api/adapter.ts"),
  ]);
  for (const label of ["我的 Skill", "官方 Skill", "分叉 Skill", "草稿", "已发布", "已停用"]) {
    assert.match(library, new RegExp(label));
  }
  for (const label of ["概览", "编辑器", "测试实验室", "版本", "使用情况", "发布检查"]) {
    assert.match(studio, new RegExp(label));
  }
  for (const label of ["基本信息", "输入 Schema", "研究与事实策略", "写作策略", "视觉与素材策略", "配音与字幕", "剪辑策略", "QC 门禁", "Pipeline", "输出契约"]) {
    assert.match(studio, new RegExp(label));
  }
  assert.match(overviewRoute, /initialView="overview"/);
  assert.doesNotMatch(overviewRoute, /redirect\(/);
  for (const method of ["saveDraft", "validateVersion", "publishVersion", "compareVersions", "createDraft"]) {
    assert.match(adapter, new RegExp(method));
    assert.match(studio, new RegExp(method));
  }
  assert.match(studio, /listChannels/);
  assert.match(studio, /listRuns/);
  assert.match(studio, /不会生成 Mock/);
  assert.match(studio, /JSON 高级模式/);
  assert.match(studio, /第 \{issue\.line\} 行/);
  assert.match(studio, /\["draft", "ready", "rejected"\]/);
  assert.match(studio, /至少 1 个测试主题/);
  assert.doesNotMatch(studio, /onClick=\{\(\) => void rollback/);
  assert.doesNotMatch(studio, /id="preferred-model"/);
  assert.doesNotMatch(studio, /id="output-language"|id="output-duration"|id="forbidden-treatments"/);
});

test("channel management uses canonical responses, multi-library defaults, and optimistic concurrency", async () => {
  const [list, detail, form, composer, contracts, adapter, http] = await Promise.all([
    source("components/channels-view.tsx"),
    source("components/channel-detail.tsx"),
    source("components/channel-form.tsx"),
    source("components/create-composer.tsx"),
    source("lib/api/contracts.ts"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
  ]);
  for (const label of ["概览", "内容", "品牌资产", "创作默认值", "平台连接状态", "操作记录"]) assert.match(detail, new RegExp(label));
  for (const field of ["SkillVersion", "PipelineVersion", "素材库", "声音", "渲染预设"]) assert.match(form + detail, new RegExp(field));
  assert.match(list, /listChannels/);
  assert.match(list, /type="search"/);
  assert.match(adapter, /VersionedResource<Channel>/);
  assert.match(adapter, /archiveChannel/);
  assert.match(http, /\/v1\/channels/);
  assert.match(http, /"If-Match"/);
  assert.match(http, /parameters\.set\("search"/);
  assert.doesNotMatch(http, /new URLSearchParams\(\{ workspace_id: query\.workspaceId \}\)/);
  assert.match(composer, /CompositionMultiSelect/);
  assert.match(composer, /assetLibraryIds: selection\.assetLibraryIds/);
  assert.match(detail, /连接状态 API 尚未提供/);
  assert.match(detail, /操作记录 API 尚未提供/);
  assert.doesNotMatch(contracts, /platformConnections:|operationRecords:/);
  assert.doesNotMatch(http, /控制 API 暂不支持频道/);
  assert.doesNotMatch(list + detail + form, /即将接入|createMockAdapter|mock-data/);
});

test("Run controls use the real adapter contract without simulated progress", async () => {
  const [detail, adapter, http] = await Promise.all([
    source("components/run-detail.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
  ]);
  assert.match(adapter, /cancelRun/);
  assert.match(adapter, /reviewRunStep/);
  assert.match(http, /\/v1\/steps\?run_id=/);
  assert.match(http, /\/cancel/);
  assert.match(http, /\/review/);
  assert.match(detail, /Worker 尚未创建步骤记录/);
  assert.match(detail, /不会展示模拟进度/);
  assert.doesNotMatch(detail, /setTimeout\([^)]*succeeded/);
});

test("batch production stays paginated and uses durable batch endpoints", async () => {
  const [component, adapter, http] = await Promise.all([
    source("components/batch-console.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
  ]);
  assert.match(component, /PAGE_SIZE = 50/);
  assert.match(component, /MAX_BATCH_SIZE = 5_000/);
  assert.match(component, /listGenerationBatchItems/);
  assert.match(component, /researchMode/);
  assert.match(component, /缺少来源时/);
  assert.match(component, /4c7d9777-d754-5fa3-bf85-4bf5c9746dba/);
  assert.match(component, /pipelineVersionId: STANDARD_PRODUCTION_V3_ID/);
  assert.match(component, /source_urls/);
  assert.match(component, /topicRowsFromText/);
  assert.match(component, /source_urls: row\.sourceUrls/);
  assert.match(component, /minimum_sources/);
  assert.match(component, /maxLength=\{160\}/);
  assert.match(component, /剪辑素材库/);
  assert.match(component, /标准批量生产 v3 必须至少选择一个/);
  assert.match(component, /selectedReadyAssetCount <= 0/);
  assert.match(component, /批量剪辑内的自动补素材已关闭/);
  assert.match(component, /assetLibraryIds/);
  assert.doesNotMatch(component, /createMockAdapter|mock-data/);
  assert.match(adapter, /createGenerationBatch/);
  assert.match(adapter, /cancelGenerationBatch/);
  assert.match(http, /\/v1\/generation-batches/);
  assert.match(http, /research_mode: request\.researchMode/);
  assert.match(http, /asset_acquisition: \{[\s\S]*?enabled: false/);
});

test("asset hub exposes durable themed library builds and bounded directory uploads", async () => {
  const [hub, adapter, http] = await Promise.all([
    source("components/asset-hub.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
  ]);
  for (const method of ["createLibraryBuildJob", "getLibraryBuildJob", "cancelLibraryBuildJob"]) {
    assert.match(adapter, new RegExp(method));
    assert.match(hub, new RegExp(method));
  }
  assert.match(http, /\/v1\/library-build-jobs/);
  assert.match(http, /"If-Match": `"\$\{revision\}"`/);
  assert.match(hub, /按主题初始化 \/ 补充/);
  assert.match(hub, /这是素材联网采集，不是内容联网研究/);
  assert.match(hub, /webkitdirectory/);
  assert.match(hub, /relativePath/);
  assert.match(http, /source: \{ type: "local_directory", relative_path: request\.relativePath \}/);
  assert.match(hub, /UPLOAD_CONCURRENCY = 3/);
  assert.match(hub, /MAX_LOCAL_FILES = 100/);
  assert.match(hub, /retryFailedUploads/);
  assert.match(hub, /BUILD_JOB_STORAGE_PREFIX/);
  assert.match(hub, /localStorage\.getItem/);
  assert.match(hub, /RESOURCE_NOT_FOUND/);
  assert.match(hub, /不支持关闭页面后的断点续传/);
  assert.doesNotMatch(hub, /最大 5 GB \/ 文件/);
});

test("asset operations use cursor pagination, durable endpoints, and server-owned readiness", async () => {
  const [workbench, detail, adapter, http] = await Promise.all([
    source("components/asset-library-workbench.tsx"),
    source("components/asset-detail.tsx"),
    source("lib/api/adapter.ts"),
    source("lib/api/http-adapter.ts"),
  ]);
  assert.match(workbench, /PAGE_SIZE = 50/);
  assert.match(workbench, /listAssets/);
  assert.match(workbench, /bulkUpdateAssets/);
  assert.match(detail, /listAssetSegments/);
  assert.match(detail, /getAssetPreview/);
  for (const endpoint of ["asset-libraries", "status-transitions", "/reanalyze", "/restore", "assets/batch-review", "assets/batch-tags", "assets/batch-reanalyze", "downloads/preview", "/segments", "import-jobs"]) assert.match(http, new RegExp(endpoint));
  assert.match(adapter, /AssetPatchRequest/);
  assert.doesNotMatch(workbench + detail, /status\s*:\s*["']ready["']/);
  assert.doesNotMatch(workbench + detail, /setTimeout\([^)]*(succeeded|completed)/);
});

test("single-workspace UI resolves identity through the typed session boundary", async () => {
  const paths = [
    "components/app-shell.tsx",
    "components/create-composer.tsx",
    "components/skill-library.tsx",
    "components/skill-creator.tsx",
    "components/settings-view.tsx",
    "components/asset-hub.tsx",
    "components/channels-view.tsx",
  ];
  for (const pathname of paths) {
    const content = await source(pathname);
    assert.equal(content.includes("workspace_personal_demo"), false, pathname);
  }
  const shell = await source("components/app-shell.tsx");
  assert.doesNotMatch(shell, /切换当前工作区/);
  assert.match(shell, /getSession\(\)/);
  const library = await source("components/skill-library.tsx");
  assert.doesNotMatch(library, /label: "团队 Skill"/);
  assert.match(library, /label: "我的 Skill"/);
  assert.match(library, /label: "官方 Skill"/);
});

test("new frontend contains no legacy special business identifiers", async () => {
  const paths = await collectSources(rootPath);
  const forbidden = ["bio" + "graphy", "gen" + "eric", "gen" + "shin", "book" + "list", "ai-" + "frontier"];
  for (const path of paths) {
    const content = await readFile(path, "utf8");
    for (const token of forbidden) assert.equal(content.toLowerCase().includes(token), false, `${token} in ${path}`);
  }
});
