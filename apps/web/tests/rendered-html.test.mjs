import assert from "node:assert/strict";
import test from "node:test";

const routes = [
  ["/create", "创作"],
  ["/create/ai", "全 AI 影片"],
  ["/projects", "项目"],
  ["/projects/99999999-9999-4999-8999-999999999999", "项目执行状态"],
  ["/skills", "Skill"],
  ["/skills/new", "创建 Skill"],
  ["/skills/skill_topic_insight", "Skill 概览"],
  ["/skills/skill_topic_insight/edit", "Skill 编辑器"],
  ["/skills/skill_topic_insight/test", "Skill 测试台"],
  ["/skills/skill_topic_insight/versions", "Skill 版本历史"],
  ["/skills/skill_topic_insight/usage", "Skill 使用情况"],
  ["/skills/skill_topic_insight/publish", "Skill 发布检查"],
  ["/assets", "素材"],
  ["/channels", "频道"],
  ["/channels/new", "新建频道"],
  ["/channels/channel_personal_main", "频道详情"],
  ["/channels/channel_personal_main/edit", "编辑频道"],
  ["/settings", "账户与设置"],
];

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  return (await import(workerUrl.href)).default;
}

async function render(worker, pathname) {
  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders every primary route with the shared information architecture", async () => {
  const worker = await loadWorker();
  for (const [pathname, title] of routes) {
    const response = await render(worker, pathname);
    assert.equal(response.status, 200, pathname);
    assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i, pathname);
    const html = await response.text();
    assert.match(html, new RegExp(`<title>${title}(?: · Vistora)?<\\/title>`, "i"), pathname);
    assert.match(html, /<main[^>]*id="main-content"/i, pathname);
    assert.match(html, /<h1\b/i, pathname);
    assert.match(html, /aria-label="主导航"/i, pathname);
    assert.match(html, /账户/i, pathname);
    assert.match(html, /创作空间/i, pathname);
  }
});

test("renders product-specific creation, Skill, channel, and state copy", async () => {
  const worker = await loadWorker();
  const createHtml = await (await render(worker, "/create")).text();
  assert.match(createHtml, /把一个想法，变成可发布的内容/);
  assert.match(createHtml, /进入全 AI 影片/);

  const aiCreateHtml = await (await render(worker, "/create/ai")).text();
  assert.match(aiCreateHtml, /从一句话，生成一支完整影片/);
  assert.match(aiCreateHtml, /正在读取生成服务/);
  assert.match(aiCreateHtml, /正在读取服务状态/);
  assert.match(aiCreateHtml, /服务端报价/);

  const skillHtml = await (await render(worker, "/skills")).text();
  assert.match(skillHtml, /我的 Skill/);
  assert.match(skillHtml, /官方 Skill/);

  const newSkillHtml = await (await render(worker, "/skills/new")).text();
  for (const label of ["从空白创建", "分叉官方 Skill", "从示例蒸馏", "导入 Skill 包"]) {
    assert.match(newSkillHtml, new RegExp(label));
  }

  const channelsHtml = await (await render(worker, "/channels")).text();
  assert.match(channelsHtml, /把常用发布配置保存成频道/);
  assert.match(channelsHtml, /频道只保存默认值/);

  const settingsHtml = await (await render(worker, "/settings")).text();
  assert.match(settingsHtml, /账户、安全与偏好/);
  assert.match(settingsHtml, /尚未接入的功能会明确标注/);
});
