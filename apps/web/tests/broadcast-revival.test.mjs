import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function source(pathname) {
  return readFile(new URL(pathname, root), "utf8");
}

test("broadcast revival is a real fail-closed archive workflow", async () => {
  const [route, studio, composer, contracts, http, css] = await Promise.all([
    source("app/create/broadcast-revival/page.tsx"),
    source("components/broadcast-revival-studio.tsx"),
    source("components/create-composer.tsx"),
    source("lib/api/contracts.ts"),
    source("lib/api/http-adapter.ts"),
    source("app/globals.css"),
  ]);

  assert.match(route, /BroadcastRevivalStudio/);
  assert.match(composer, /href="\/create\/broadcast-revival"/);
  for (const method of ["getSession", "getComposerOptions", "listAssets", "estimateRun", "createRun"]) {
    assert.match(studio, new RegExp(method));
  }
  for (const copy of [
    "贵州广电历史媒资活化",
    "贵州广电历史媒资活化库",
    "只显示已就绪且人工审核通过的素材",
    "素材不足再补采",
    "不得用生成画面冒充历史档案",
    "Worker 能力状态未知",
    "页面不会用本地样例或 Mock 素材替代真实服务结果",
    "素材级快速筛查",
    "镜头片段级候选",
    "线索检索暂无精确结果",
  ]) assert.match(studio, new RegExp(copy));
  assert.match(studio, /status: "ready"/);
  assert.match(studio, /reviewStatus: "approved"/);
  assert.match(studio, /sourceUrls/);
  assert.match(studio, /inventoryConcepts/);
  assert.match(http, /inventory_concepts/);
  assert.match(studio, /source\?\.locator/);
  assert.match(studio, /startsWith\("https:\/\/"\)/);
  assert.match(http, /source_urls/);
  assert.match(http, /source\.canonical_url/);
  assert.match(studio, /onlineSupplement/);
  assert.match(studio, /sources: \["wikimedia"\]/);
  assert.match(studio, /copyrightStatus: "public_domain"/);
  assert.match(studio, /maxAssets: 6/);
  assert.match(studio, /检查素材并开始剪辑/);
  assert.match(studio, /nextSearchMode = "library"/);
  assert.match(studio, /item\.name === REVIVAL_LIBRARY_NAME/);
  assert.match(studio, /assetAcquisition: \{[\s\S]*?enabled: false/);
  assert.match(studio, /noAssetDraft: \{ enabled: false \}/);
  assert.match(contracts, /defaultPipelineVersionId\?: string/);
  assert.match(http, /defaultPipelineVersionId: text\(item\.default_pipeline_version_id\)/);
  assert.doesNotMatch(studio, /placeholder|setInterval/i);
  assert.match(css, /\.broadcast-revival-workbench \{/);
  assert.match(css, /@media \(max-width: 767px\)[\s\S]*?\.broadcast-revival-page/);
});
