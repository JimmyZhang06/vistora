import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { HttpFrameFactoryAdapter } from "../lib/api/http-adapter.ts";
import { BenchmarkConnectionFlow } from "../lib/benchmark-connection.ts";

// These are transport and lifecycle fixtures, never real QR codes or a live login.
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII=";
const NOW = Date.parse("2026-09-06T00:00:00Z");
const wire = (state, patch = {}) => ({
  schema_version: "1.0.0", platform: "xiaohongshu", mode: "local_browser", state,
  qr_image_data_url: state === "awaiting_scan" ? PNG : null,
  expires_at: state === "awaiting_scan" ? new Date(NOW + 120_000).toISOString() : null,
  checked_at: new Date(NOW).toISOString(), retry_after_seconds: 3, error_code: null, ...patch,
});
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const reply = (state, patch = {}) => ({ ok: true, data: {
  platform: "xiaohongshu", mode: "local_browser", state, qrImageDataUrl: state === "awaiting_scan" ? PNG : null,
  expiresAt: state === "awaiting_scan" ? new Date(NOW + 120_000).toISOString() : null,
  checkedAt: new Date(NOW).toISOString(), retryAfterSeconds: 3, errorCode: null, ...patch,
} });
const flush = async () => { for (let index = 0; index < 8; index += 1) await Promise.resolve(); };

test("connection methods use the research API, no-store, bounded cancellable requests and explicit QR creation", async () => {
  const calls = [];
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://main.test", benchmarkBaseUrl: "http://research.test", fetch: async (url, init) => {
    calls.push({ url, init });
    return json(wire(init.method === "POST" ? "awaiting_scan" : "login_required"));
  } });
  const controller = new AbortController();
  assert.equal((await adapter.getBenchmarkConnectionStatus(controller.signal)).data.state, "login_required");
  const qr = await adapter.startBenchmarkConnection("controlled-idempotency", controller.signal);
  assert.equal(qr.data.qrImageDataUrl, PNG);
  assert.equal(calls[0].url, "http://research.test/v1/benchmark-auth/xiaohongshu/status");
  assert.equal(calls[0].init.method, undefined);
  assert.equal(calls[1].url, "http://research.test/v1/benchmark-auth/xiaohongshu/qrcode");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.headers["Idempotency-Key"], "controlled-idempotency");
  assert.deepEqual(JSON.parse(calls[1].init.body), {});
  for (const call of calls) { assert.equal(call.init.cache, "no-store"); assert.ok(call.init.signal instanceof AbortSignal); }
  controller.abort();
  assert.ok(calls.every((call) => call.init.signal.aborted));
});

test("connection parser rejects unsafe QR images, incompatible schemas and missing expiration", async () => {
  for (const patch of [
    { qr_image_data_url: "https://third-party.test/login.png?token=private" },
    { qr_image_data_url: "data:image/svg+xml;base64,PHN2Zz4=" },
    { qr_image_data_url: "data:image/png;base64,broken" },
    { qr_image_data_url: PNG + "a".repeat(1_400_000) },
    { qr_image_data_url: null }, { expires_at: null }, { expires_at: "invalid" },
    { schema_version: "2" }, { platform: "other" }, { mode: "oauth" }, { state: "unknown" },
  ]) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => json(wire("awaiting_scan", patch)) });
    const result = await adapter.getBenchmarkConnectionStatus();
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "BENCHMARK_AUTH_INVALID_RESPONSE");
    assert.doesNotMatch(JSON.stringify(result), /private|third-party|PHN2Zz4=/);
  }
});

test("authorized and unavailable responses retain no QR or provider credentials and clamp polling intervals", async () => {
  for (const state of ["not_configured", "provider_unavailable", "checking", "login_required", "authorized", "expired", "error"]) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => json(wire(state, { qr_image_data_url: PNG, retry_after_seconds: 0, session_id: "private-session", cookie: "private-cookie" })) });
    const result = await adapter.getBenchmarkConnectionStatus();
    assert.equal(result.ok, true);
    assert.equal(result.data.state, state);
    assert.equal(result.data.qrImageDataUrl, null);
    assert.equal(result.data.retryAfterSeconds, 3);
    assert.doesNotMatch(JSON.stringify(result), /private-session|private-cookie/);
  }
});

function harness(t, options = {}) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let now = NOW;
  const calls = [];
  const views = [];
  let connected = 0;
  const adapter = {
    getBenchmarkConnectionStatus: async (signal) => { calls.push({ kind: "status", signal }); return options.status ? options.status(signal) : reply("login_required"); },
    startBenchmarkConnection: async (key, signal) => { calls.push({ kind: "start", key, signal }); return options.start ? options.start(signal) : reply("awaiting_scan"); },
  };
  const flow = new BenchmarkConnectionFlow(adapter, (view) => views.push(view), () => { connected += 1; }, () => now);
  t.after(() => flow.dispose());
  return { flow, calls, views, get view() { return views.at(-1); }, get connected() { return connected; }, async tick(ms) { now += ms; t.mock.timers.tick(ms); await flush(); } };
}

test("passive status reads never create QR, poll or resume operations; valid login is reused after user click", async (t) => {
  const h = harness(t, { status: async () => reply("authorized"), start: async () => reply("authorized") });
  await h.flow.check();
  await h.tick(30_000);
  assert.deepEqual(h.calls.map((call) => call.kind), ["status"]);
  assert.equal(h.connected, 0);
  await h.flow.start();
  assert.equal(h.connected, 1);
  assert.equal(h.view.active, false);
  assert.equal(h.view.connection.qrImageDataUrl, null);
  await h.tick(30_000);
  assert.equal(h.calls.length, 2);
});

test("successful QR login only notifies once and stops polling", async (t) => {
  const h = harness(t, { status: async () => reply("authorized") });
  await h.flow.start();
  assert.equal(h.view.connection.qrImageDataUrl, PNG);
  await h.tick(3_000);
  assert.equal(h.connected, 1);
  assert.equal(h.view.connection.qrImageDataUrl, null);
  assert.equal(h.view.active, false);
  await h.tick(30_000);
  assert.deepEqual(h.calls.map((call) => call.kind), ["start", "status"]);
});

test("hidden pages abort polling and resume with status only; canceled requests cannot resume another selection", async (t) => {
  let resolve;
  const h = harness(t, { status: () => new Promise((done) => { resolve = done; }) });
  await h.flow.start();
  await h.tick(3_000);
  const polling = h.calls.at(-1);
  h.flow.setVisible(false);
  assert.equal(polling.signal.aborted, true);
  resolve(reply("authorized"));
  await flush();
  assert.equal(h.connected, 0);
  await h.tick(20_000);
  assert.equal(h.calls.length, 2);
  h.flow.setVisible(true);
  await flush();
  assert.equal(h.calls.at(-1).kind, "status");
  assert.equal(h.calls.filter((call) => call.kind === "start").length, 1);
  h.flow.cancel();
  resolve(reply("authorized"));
  await flush();
  assert.equal(h.connected, 0);
  assert.equal(h.view.connection, null);
  assert.equal(h.view.active, false);
});

test("three consecutive status failures stop polling and require an explicit new attempt", async (t) => {
  const h = harness(t, { status: async () => ({ ok: false, error: { code: "NETWORK_ERROR", message: "unavailable" } }) });
  await h.flow.start();
  for (let attempt = 0; attempt < 3; attempt += 1) await h.tick(3_000);
  assert.equal(h.calls.length, 4);
  assert.equal(h.view.active, false);
  assert.equal(h.view.connection, null);
  await h.tick(120_000);
  assert.equal(h.calls.length, 4);
  assert.equal(h.connected, 0);
});

test("absolute three-minute deadline also bounds a provider stuck checking without an expiry", async (t) => {
  const h = harness(t, { start: async () => reply("checking"), status: async () => reply("checking") });
  await h.flow.start();
  h.flow.setVisible(false);
  await h.tick(180_000);
  h.flow.setVisible(true);
  await flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.view.active, false);
  assert.match(h.view.notice, /过期/);
});

test("disposing a selection invalidates in-flight authorization", async (t) => {
  let resolve;
  const h = harness(t, { start: () => new Promise((done) => { resolve = done; }) });
  const pending = h.flow.start();
  h.flow.dispose();
  assert.equal(h.calls[0].signal.aborted, true);
  resolve(reply("authorized"));
  await pending;
  assert.equal(h.connected, 0);
});

test("provider QR deadline stops polling without automatically creating a replacement", async (t) => {
  const h = harness(t, { start: async () => reply("awaiting_scan", { expiresAt: new Date(NOW + 2_000).toISOString() }) });
  await h.flow.start();
  await h.tick(2_000);
  assert.equal(h.view.connection, null);
  assert.equal(h.view.active, false);
  assert.equal(h.calls.length, 1);
  assert.match(h.view.notice, /过期/);
});

test("image loading errors clear the QR and stop polling", async (t) => {
  const h = harness(t);
  await h.flow.start();
  h.flow.qrUnavailable();
  await h.tick(30_000);
  assert.equal(h.view.connection, null);
  assert.equal(h.calls.length, 1);
  assert.match(h.view.notice, /图像无法显示/);
});

test("archived reports keep the read-only path and connection completion only restores free operations", async () => {
  const account = await readFile(new URL("../components/benchmark-account-demo.tsx", import.meta.url), "utf8");
  const panel = await readFile(new URL("../components/benchmark-connection.tsx", import.meta.url), "utf8");
  const video = await readFile(new URL("../components/benchmark-video-analysis.tsx", import.meta.url), "utf8");
  const flow = await readFile(new URL("../lib/benchmark-connection.ts", import.meta.url), "utf8");
  const recovery = account.slice(account.indexOf("function resumeAfterConnection()"), account.indexOf("  useEffect(() =>", account.indexOf("function resumeAfterConnection()")));
  assert.match(account, /!archivedReport \? <BenchmarkConnectionPanel/);
  assert.match(recovery, /pending\.profileEpoch !== profileRequest\.current \|\| pending\.selectionEpoch !== sourceRequest\.current/);
  assert.match(recovery, /loadSourceEvidence/);
  assert.match(recovery, /refreshNoteIdentity\(true\)/);
  assert.match(account, /result\.data\.snapshot\.acquisition\.identityErrorCode === "BENCHMARK_AUTHENTICATION_REQUIRED"/);
  assert.doesNotMatch(recovery + panel + flow, /createBenchmarkAnalysisJob|retryBenchmarkAnalysisJob|localStorage|sessionStorage|console\./);
  assert.match(panel, /仍需你再次点击开始或重试/);
  assert.match(panel, /visibilitychange/);
  assert.match(video, /connectionRequired && \(next === "create" \|\| next === "retry"\)/);
});
