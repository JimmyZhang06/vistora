import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const root = new URL("../", import.meta.url);
const source = (path) => readFile(new URL(path, root), "utf8");

test("benchmark reports scope complete video analysis to the selected note", async () => {
  const [page, component, video, adapter, contracts] = await Promise.all([
    source("app/benchmarks/page.tsx"),
    source("components/benchmark-account-demo.tsx"),
    source("components/benchmark-video-analysis.tsx"),
    source("lib/api/http-adapter.ts"),
    source("lib/api/contracts.ts"),
  ]);

  assert.match(page, /BenchmarkAccountDemo/);
  assert.match(component, /generateBenchmarkAccountReport/);
  assert.match(component, /benchmark-profile-url/);
  assert.match(component, /使用白昼小熊示例/);
  assert.match(component, /总体报告/);
  assert.match(component, /单条报告/);
  assert.match(component, /为什么可能成为爆款/);
  assert.match(component, /当前缺失证据/);
  assert.match(video, /视频画面、OCR、口播与配音深析/);
  assert.match(component, /BenchmarkVideoAnalysisPanel/);
  assert.match(component, /URLSearchParams\(window.location.search\).get\("note"\)/);
  assert.match(component, /note.noteId === targetNoteId/);
  assert.match(component, /key=\{`\$\{snapshot.profile.userId\}:\$\{selectedSnapshotNote\?\.noteId/);
  assert.match(video, /开始完整视频分析/);
  assert.match(video, /取消分析/);
  assert.match(video, /重试分析/);
  assert.match(video, /重新分析/);
  assert.match(video, /received\.profileUserId !== profileUserId \|\| received\.noteId !== noteId/);
  assert.match(video, /epoch !== requestEpoch.current/);
  assert.match(video, /controller\.abort\(\)/);
  assert.match(video, /空转写不代表视频没有口播/);
  assert.match(video, /部分分析完成/);
  assert.doesNotMatch(component, /getLatestBenchmarkDeepReport|loadLatestDeepReport|loadDeepDemo/);
  assert.doesNotMatch(video, /getLatestBenchmarkDeepReport|getBenchmarkDeepReportDemo/);
  assert.match(component, /已登录浏览器真实证据/);
  assert.match(video, /证据时间轴/);
  assert.match(component, /不把相关性包装成平台推荐因果/);
  assert.match(component, /没有回退到 Mock 或旧榜单数据/);
  assert.match(component, /已登录浏览器实时获取/);
  assert.match(component, /获取真实详情证据/);
  assert.match(component, /凭据、短期令牌和签名媒体地址不会进入响应/);
  assert.match(component, /打开真实主页/);
  assert.match(adapter, /\/v1\/benchmark-accounts\/report/);
  assert.match(adapter, /\/v1\/benchmark-notes\/deep-report\/demo/);
  assert.match(adapter, /\/v1\/benchmark-notes\/deep-report\/latest/);
  assert.match(adapter, /\/v1\/benchmark-notes\/source-evidence/);
  assert.match(adapter, /\/v1\/benchmark-analysis\/jobs/);
  assert.match(adapter, /new URLSearchParams\(\{ profile_user_id: profileUserId, note_id: noteId \}\)/);
  assert.match(adapter, /profile_url: profile\.url/);
  assert.match(contracts, /initial_page_sample/);
});

function wireJob(overrides = {}) {
  return {
    schema_version: "1.0.0", job_id: "11111111-1111-4111-8111-111111111111",
    workspace_id: "workspace-test", profile_user_id: "5a8cf39111be10466d285d6b",
    note_id: "6a94178b0000000025026802", title: "真实媒体分析契约测试", status: "analyzing",
    progress: { stage: "ocr", percent: 40, message: "识别画面文字" },
    created_at: "2026-09-05T12:00:00Z", updated_at: "2026-09-05T12:00:10Z", attempt: 1,
    source_evidence: null, analysis: null, report: null, error: null, artifacts: [],
    ...overrides,
  };
}

test("video job transport preserves note identity, idempotency and lifecycle routes", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const requests = [];
  const job = wireJob();
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: async (url, init = {}) => {
    requests.push({ url: new URL(url), init });
    return new Response(JSON.stringify(job), { status: 200, headers: { "Content-Type": "application/json" } });
  } });
  const key = "22222222-2222-4222-8222-222222222222";
  const request = { platform: "xiaohongshu", profileUrl: `https://www.xiaohongshu.com/user/profile/${job.profile_user_id}`, noteId: job.note_id };
  const created = await adapter.createBenchmarkAnalysisJob(request, key);
  assert.equal(created.ok, true);
  assert.equal(created.data.id, job.job_id);
  assert.equal(created.data.noteId, job.note_id);
  assert.equal(created.data.profileUserId, job.profile_user_id);
  assert.equal(created.data.progress.percent, 40);
  assert.equal(requests[0].init.headers["Idempotency-Key"], key);
  assert.deepEqual(JSON.parse(requests[0].init.body), { platform: request.platform, profile_url: request.profileUrl, note_id: job.note_id });
  await adapter.getLatestBenchmarkAnalysisJob(job.profile_user_id, job.note_id);
  assert.equal(requests[1].url.pathname, "/v1/benchmark-analysis/latest");
  assert.equal(requests[1].url.searchParams.get("profile_user_id"), job.profile_user_id);
  assert.equal(requests[1].url.searchParams.get("note_id"), job.note_id);
  assert.equal(requests[1].init.cache, "no-store");
  await adapter.getBenchmarkAnalysisJob(job.job_id);
  await adapter.cancelBenchmarkAnalysisJob(job.job_id);
  await adapter.retryBenchmarkAnalysisJob(job.job_id);
  assert.deepEqual(requests.slice(2).map(({ url, init }) => [url.pathname, init.method ?? "GET"]), [
    [`/v1/benchmark-analysis/jobs/${job.job_id}`, "GET"],
    [`/v1/benchmark-analysis/jobs/${job.job_id}/cancel`, "POST"],
    [`/v1/benchmark-analysis/jobs/${job.job_id}/retry`, "POST"],
  ]);
});

test("video job mapping exposes only generated artifacts and preserves missing speech evidence", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const frame = "attempt-1/frames/0001.jpg";
  const job = wireJob({
    status: "partial",
    artifacts: [
      { filename: frame, media_type: "image/jpeg" },
      { filename: "attempt-1/audio.wav", media_type: "audio/wav" },
      { filename: "source.mp4", media_type: "video/mp4" },
      { filename: "../secrets.json", media_type: "application/json" },
      { filename: "https://untrusted.test/private.jpg", media_type: "image/jpeg" },
      { filename: "attempt-1/frames/%2e%2e%2fsecret.jpg", media_type: "image/jpeg" },
    ],
    analysis: {
      status: "partial", technical: { duration_ms: 18000 },
      frames: [{ key: frame, timestamp_ms: 2600, ocr: { text: "真实 OCR" }, vision: { description: "素材画面", confidence: 0.8 } }],
      capabilities: { vision: { status: "unavailable", provider: "", limitations: ["尚未配置视觉模型"] } },
      temporal: { speech_status: "unknown" },
      transcript: { text: "未提供时间码的全文", segments: [{ text: "未对齐的文本" }, { start_ms: 50, end_ms: 1050, text: "已对齐的语音" }] },
      audio_analysis: { rms_dbfs: -21.4, vad_speech_ratio: null, findings: ["可测量声音电平"] },
      creative_insights: [{ claim: "视觉推断", evidence_frame_keys: [frame], confidence: 0.7 }],
    },
  });
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: async () => new Response(JSON.stringify(job)) });
  const result = await adapter.getBenchmarkAnalysisJob(job.job_id);
  assert.equal(result.ok, true);
  assert.equal(result.data.status, "partial");
  assert.equal(result.data.artifacts.length, 2);
  assert.equal(result.data.analysis.frames[0].artifactUrl, `http://api.test/v1/benchmark-analysis/jobs/${job.job_id}/artifacts/${frame}`);
  assert.equal(result.data.analysis.frames[0].ocrText, "真实 OCR");
  assert.deepEqual(result.data.analysis.frames[0].vision, { description: "素材画面" });
  assert.equal(result.data.analysis.speechStatus, "unknown");
  assert.equal(result.data.analysis.audioAnalysis.vadSpeechRatio, undefined);
  assert.deepEqual(result.data.analysis.transcript.segments, [{ startMs: 50, endMs: 1050, text: "已对齐的语音" }]);
  assert.equal(result.data.analysis.capabilities.vision.status, "unavailable");
  assert.deepEqual(result.data.analysis.creativeInsights[0].evidenceFrameKeys, [frame]);
  assert.equal(result.data.report, null);
});

test("missing saved video and failed actions are not replaced with demo reports", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://api.test", fetch: async () => new Response(JSON.stringify({ code: "BENCHMARK_ANALYSIS_NOT_FOUND", message: "这篇笔记暂无已保存分析" }), { status: 404 }) });
  const result = await adapter.getLatestBenchmarkAnalysisJob("profile", "note");
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "BENCHMARK_ANALYSIS_NOT_FOUND");
  assert.equal(result.error.status, 404);
  assert.equal("data" in result, false);
});

test("a dedicated benchmark API does not redirect the rest of the application", async () => {
  const { HttpFrameFactoryAdapter } = await import("../lib/api/http-adapter.ts");
  const seen = [];
  const job = wireJob({ artifacts: [{ filename: "attempt-1/frames/0000.jpg", media_type: "image/jpeg" }] });
  const adapter = new HttpFrameFactoryAdapter({
    baseUrl: "http://main.test", benchmarkBaseUrl: "http://benchmark.test",
    fetch: async (url) => { seen.push(String(url)); return new Response(JSON.stringify(job)); },
  });
  const result = await adapter.getBenchmarkAnalysisJob(job.job_id);
  assert.equal(result.ok, true);
  assert.ok(seen[0].startsWith("http://benchmark.test/v1/benchmark-analysis/"));
  assert.ok(result.data.artifacts[0].contentUrl.startsWith("http://benchmark.test/"));
  await adapter.getAccountProfile();
  assert.ok(seen[1].startsWith("http://main.test/"));
});
