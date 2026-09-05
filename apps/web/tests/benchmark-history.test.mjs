import assert from "node:assert/strict";
import test from "node:test";
import { HttpFrameFactoryAdapter } from "../lib/api/http-adapter.ts";

const RECORD = "11111111-1111-4111-8111-111111111111";
const OTHER_RECORD = "22222222-2222-4222-8222-222222222222";
const USER = "aaaaaaaaaaaaaaaaaaaaaaaa";
const OTHER_USER = "bbbbbbbbbbbbbbbbbbbbbbbb";
const NOTE = "111111111111111111111111";
const response = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const summary = (kind = "account") => ({ record_id: RECORD, kind, title: "已保存报告", profile_user_id: USER, note_id: kind === "video" ? NOTE : null, status: "partial", saved_at: "2026-09-05T12:00:00Z", analyzed_at: "2026-09-05T11:00:00Z" });
const accountDetail = () => ({ ...summary(), account_report: {
  generated_at: "2026-09-05T11:00:00Z", snapshot: {
    profile: { platform: "xiaohongshu", user_id: USER, profile_url: `https://www.xiaohongshu.com/user/profile/${USER}`, nickname: "保存账号" },
    acquisition: {}, notes: [], analysis: {},
  }, account_report: { executive_summary: "当次已保存结论" }, note_reports: [],
}, video_job: null, media_available: false });
const videoDetail = () => ({ ...summary("video"), account_report: null, video_job: {
  job_id: OTHER_RECORD, workspace_id: "controlled-workspace", profile_user_id: USER, note_id: NOTE, title: "保存的视频分析", status: "partial",
  progress: { stage: "report", percent: 100, message: "部分完成" }, attempt: 1,
  created_at: "2026-09-05T11:00:00Z", updated_at: "2026-09-05T11:00:30Z", artifacts: [],
  analysis: { transcript: { text: "保存的语音候选" }, frames: [{ key: "attempt-1/frames/0001.jpg", timestamp_ms: 1000 }] },
  report: { source_kind: "worker_asset_analysis", status: "partial", summary: "视频保存结论", limitations: ["视觉能力未配置"] }, source_evidence: null, error: null,
}, media_available: false });

test("history search and pagination stay on the research API and issue only no-store reads", async () => {
  const seen = [];
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://main.test", benchmarkBaseUrl: "http://research.test", fetch: async (url, init) => {
    seen.push({ url: new URL(url), init });
    return response({ items: [summary()], next_cursor: RECORD });
  } });
  const result = await adapter.listBenchmarkHistory({ kind: "account", q: "旅行 & 音乐", cursor: OTHER_RECORD });
  assert.equal(result.ok, true);
  assert.equal(result.data.items[0].id, RECORD);
  assert.equal(result.data.nextCursor, RECORD);
  assert.equal(seen[0].url.origin, "http://research.test");
  assert.equal(seen[0].url.pathname, "/v1/benchmark-history");
  assert.equal(seen[0].url.searchParams.get("q"), "旅行 & 音乐");
  assert.equal(seen[0].url.searchParams.get("kind"), "account");
  assert.equal(seen[0].url.searchParams.get("cursor"), OTHER_RECORD);
  assert.equal(seen[0].init.method ?? "GET", "GET");
  assert.equal(seen[0].init.cache, "no-store");
  assert.ok(seen[0].init.signal instanceof AbortSignal);
});

test("saved account reports remain readable without the current live discovery schema", async () => {
  const seen = [];
  const adapter = new HttpFrameFactoryAdapter({ fetch: async (url, init) => { seen.push({ url: String(url), init }); return response(accountDetail()); } });
  const result = await adapter.getBenchmarkHistory(RECORD);
  assert.equal(result.ok, true);
  assert.equal(result.data.accountReport.accountReport.executiveSummary, "当次已保存结论");
  assert.equal(result.data.accountReport.snapshot.profile.userId, USER);
  assert.equal(result.data.videoJob, null);
  assert.equal(seen.length, 1);
  assert.ok(seen[0].url.endsWith(`/v1/benchmark-history/${RECORD}`));
  assert.equal(seen[0].init.method ?? "GET", "GET");
});

test("expired media does not fabricate playable artifacts or lose saved video conclusions", async () => {
  const adapter = new HttpFrameFactoryAdapter({ fetch: async () => response(videoDetail()) });
  const result = await adapter.getBenchmarkHistory(RECORD);
  assert.equal(result.ok, true);
  assert.equal(result.data.mediaAvailable, false);
  assert.deepEqual(result.data.videoJob.artifacts, []);
  assert.equal(result.data.videoJob.analysis.frames[0].artifactUrl, undefined);
  assert.equal(result.data.videoJob.analysis.transcript.text, "保存的语音候选");
  assert.equal(result.data.videoJob.report.summary, "视频保存结论");
  assert.equal(result.data.videoJob.report.status, "partial");
});

test("history rejects stale record responses and account or video identity mismatches", async () => {
  const cases = [
    () => ({ ...accountDetail(), record_id: OTHER_RECORD }),
    () => ({ ...accountDetail(), kind: "other" }),
    () => ({ ...accountDetail(), profile_user_id: OTHER_USER }),
    () => ({ ...videoDetail(), profile_user_id: OTHER_USER }),
    () => ({ ...videoDetail(), note_id: "222222222222222222222222" }),
    () => ({ ...videoDetail(), video_job: null }),
  ];
  for (const makeDetail of cases) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => response(makeDetail()) });
    const result = await adapter.getBenchmarkHistory(RECORD);
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "BENCHMARK_HISTORY_IDENTITY_CONFLICT");
  }
});

test("history missing records and authorization failures stay explicit, and abandoned requests abort", async () => {
  for (const [code, status] of [["BENCHMARK_HISTORY_NOT_FOUND", 404], ["FORBIDDEN", 403]]) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => response({ code, message: "受控错误" }, status) });
    const result = await adapter.getBenchmarkHistory(RECORD);
    assert.equal(result.ok, false);
    assert.equal(result.error.code, code);
    assert.equal(result.error.status, status);
  }
  let seenSignal;
  const adapter = new HttpFrameFactoryAdapter({ fetch: async (_url, init) => {
    seenSignal = init.signal;
    return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true }));
  } });
  const controller = new AbortController();
  const pending = adapter.getBenchmarkHistory(RECORD, controller.signal);
  controller.abort();
  assert.equal((await pending).ok, false);
  assert.equal(seenSignal.aborted, true);
});
