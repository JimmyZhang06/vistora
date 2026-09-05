import assert from "node:assert/strict";
import test from "node:test";
import { HttpFrameFactoryAdapter } from "../lib/api/http-adapter.ts";
import { benchmarkProfile, benchmarkRecoveryMessage, refreshedBenchmarkSelection, verifiedBenchmarkNote } from "../lib/benchmark-recovery.ts";

// Controlled transport fixtures exercise the Web contract, not the live platform.
const USER_A = "aaaaaaaaaaaaaaaaaaaaaaaa";
const USER_B = "bbbbbbbbbbbbbbbbbbbbbbbb";
const NOTE_A = "111111111111111111111111";
const NOTE_B = "222222222222222222222222";
const profileUrl = (userId = USER_A) => `https://www.xiaohongshu.com/user/profile/${userId}`;
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

function snapshot(notes = [], userId = USER_A) {
  const identified = notes.filter((note) => note.identity_status === "verified").length;
  return {
    schema_version: "1.0.0",
    profile: { platform: "xiaohongshu", user_id: userId, profile_url: profileUrl(userId), nickname: "受控测试账号" },
    acquisition: {
      discovery_version: "1", note_identity_status: !identified ? "unavailable" : identified === notes.length ? "complete" : "partial",
      identified_note_count: identified, unresolved_note_count: notes.length - identified, identity_error_code: identified < notes.length ? "BENCHMARK_NOTE_IDENTITY_INCOMPLETE" : null,
      captured_at: "2026-09-05T12:00:00Z", method: "authenticated_managed_browser", from_cache: false,
      initial_page_has_more: false, completeness: "initial_page_sample", limitations: [],
    },
    analysis: { sample_size: notes.length, video_count: 0, image_count: 0, unknown_count: notes.length, top_notes: notes },
    notes,
  };
}

const wireNote = (sampleIndex, noteId, format = "unknown") => ({ sample_index: sampleIndex, note_id: noteId ?? null, identity_status: noteId ? "verified" : "missing", format, published_at: null, title: "重复标题", likes: { display: "1", lower_bound: 1, precision: "exact" } });
const request = { platform: "xiaohongshu", profileUrl: profileUrl() };

test("profile refresh strips tokens, requests cache recovery, and preserves partial identity metadata", async () => {
  const seen = [];
  const raw = snapshot([wireNote(1, NOTE_A), wireNote(2)]);
  const adapter = new HttpFrameFactoryAdapter({ baseUrl: "http://main.test", benchmarkBaseUrl: "http://research.test", fetch: async (url, init) => {
    seen.push({ url: String(url), init });
    return json({ snapshot: raw, account_report: {}, note_reports: [] });
  } });
  const result = await adapter.generateBenchmarkAccountReport({ ...request, profileUrl: `${profileUrl()}?xsec_token=TEST_TOKEN_MUST_NOT_LEAVE_BROWSER&xsec_source=pc_search`, refreshNoteIdentity: true });
  assert.equal(result.ok, true);
  assert.equal(seen[0].url, "http://research.test/v1/benchmark-accounts/report");
  assert.deepEqual(JSON.parse(seen[0].init.body), { platform: "xiaohongshu", profile_url: profileUrl(), refresh_note_identity: true });
  assert.ok(seen[0].init.signal instanceof AbortSignal);
  assert.equal(result.data.snapshot.acquisition.noteIdentityStatus, "partial");
  assert.equal(result.data.snapshot.acquisition.identifiedNoteCount, 1);
  assert.equal(result.data.snapshot.acquisition.unresolvedNoteCount, 1);
  assert.equal(result.data.snapshot.acquisition.identityErrorCode, "BENCHMARK_NOTE_IDENTITY_INCOMPLETE");
  assert.equal(result.data.snapshot.notes[0].format, "unknown");
  assert.equal(result.data.snapshot.notes[0].publishedAt, null);
  assert.equal(result.data.snapshot.analysis.unknownCount, 2);
  assert.equal(verifiedBenchmarkNote(result.data.snapshot.notes[0]), true);
  assert.equal(verifiedBenchmarkNote(result.data.snapshot.notes[1]), false);
  assert.doesNotMatch(JSON.stringify({ seen, result }), /TEST_TOKEN/);
});

test("outdated or inconsistent discovery responses identify the connected API and never become platform limitations", async () => {
  for (const mutate of [
    (raw) => { delete raw.acquisition.discovery_version; },
    (raw) => { raw.acquisition.discovery_version = "2"; },
    (raw) => { raw.acquisition.identified_note_count = 9; },
    (raw) => { raw.notes[0].note_id = NOTE_A.toUpperCase().replace("1", "A"); },
    (raw) => { delete raw.notes[0].identity_status; },
  ]) {
    const raw = snapshot([wireNote(1, NOTE_A)]);
    mutate(raw);
    const adapter = new HttpFrameFactoryAdapter({ benchmarkBaseUrl: "https://user:password@research.test/local?secret=hidden", fetch: async () => json(raw) });
    const result = await adapter.previewBenchmarkAccount(request);
    assert.equal(result.ok, false);
    assert.equal(result.error.code, "BENCHMARK_API_VERSION_MISMATCH");
    assert.match(result.error.message, /https:\/\/research.test\/local/);
    assert.match(result.error.message, /NEXT_PUBLIC_FRAMEFACTORY_BENCHMARK_API_URL/);
    assert.doesNotMatch(result.error.message, /password|secret|hidden|user:/);
  }
});

test("profile and detail results from another account are rejected even if the note title matches", async () => {
  const adapter = new HttpFrameFactoryAdapter({ fetch: async () => json(snapshot([wireNote(1, NOTE_A)], USER_B)) });
  const result = await adapter.previewBenchmarkAccount(request);
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "BENCHMARK_NOTE_IDENTITY_CONFLICT");
  const detailAdapter = new HttpFrameFactoryAdapter({ fetch: async () => json({ platform: "xiaohongshu", profile_user_id: USER_B, note_id: NOTE_A, title: "重复标题", media: { kind: "video" } }) });
  const detail = await detailAdapter.collectBenchmarkNoteSourceEvidence({ ...request, noteId: NOTE_A });
  assert.equal(detail.ok, false);
  assert.equal(detail.error.code, "BENCHMARK_NOTE_IDENTITY_CONFLICT");
});

test("refresh requires explicit reselection when previous identity was missing and preserves only profile plus verified ID", () => {
  const first = { sampleIndex: 1, noteId: NOTE_A, identityStatus: "verified", title: "重复标题" };
  const second = { sampleIndex: 2, noteId: NOTE_B, identityStatus: "verified", title: "重复标题" };
  const report = { snapshot: { profile: { userId: USER_A }, notes: [first, second] } };
  assert.equal(refreshedBenchmarkSelection(report, USER_A), null);
  assert.equal(refreshedBenchmarkSelection(report, USER_A, NOTE_B), 2);
  assert.equal(refreshedBenchmarkSelection(report, USER_B, NOTE_B), null);
  assert.equal(refreshedBenchmarkSelection(report, USER_A, "333333333333333333333333"), null);
  const reordered = { snapshot: { profile: { userId: USER_A }, notes: [{ ...second, sampleIndex: 1 }, { ...first, sampleIndex: 2 }] } };
  assert.equal(refreshedBenchmarkSelection(reordered, USER_A, NOTE_A), 2);
  first.identityStatus = "missing";
  assert.equal(refreshedBenchmarkSelection(report, USER_A, NOTE_A), null);
});

test("abortable discovery allows switching accounts while a previous request is waiting", async () => {
  let abandonedSignal;
  const adapter = new HttpFrameFactoryAdapter({ fetch: async (_url, init) => {
    const userId = benchmarkProfile(JSON.parse(init.body).profile_url).userId;
    if (userId === USER_B) return json(snapshot([], USER_B));
    abandonedSignal = init.signal;
    return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true }));
  } });
  const controller = new AbortController();
  const abandoned = adapter.previewBenchmarkAccount(request, controller.signal);
  controller.abort();
  const next = await adapter.previewBenchmarkAccount({ ...request, profileUrl: profileUrl(USER_B) });
  assert.equal(abandonedSignal.aborted, true);
  assert.equal((await abandoned).ok, false);
  assert.equal(next.ok, true);
  assert.equal(next.data.profile.userId, USER_B);
});

test("source details require known media kind, preserve images, and retain recovery error codes", async () => {
  for (const kind of ["image", "video", undefined]) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => json({ platform: "xiaohongshu", profile_user_id: USER_A, note_id: NOTE_A, media: { kind } }) });
    const result = await adapter.collectBenchmarkNoteSourceEvidence({ ...request, noteId: NOTE_A });
    assert.equal(result.ok, kind !== undefined);
    if (result.ok) assert.equal(result.data.media.kind, kind);
    else assert.equal(result.error.code, "BENCHMARK_SOURCE_CHANGED");
  }
  for (const [code, status] of [["BENCHMARK_AUTHENTICATION_REQUIRED", 409], ["BENCHMARK_NOTE_INACCESSIBLE", 404], ["FORBIDDEN", 403]]) {
    const adapter = new HttpFrameFactoryAdapter({ fetch: async () => json({ code, message: "受控错误" }, status) });
    const result = await adapter.collectBenchmarkNoteSourceEvidence({ ...request, noteId: NOTE_A });
    assert.equal(result.ok, false);
    assert.equal(result.error.code, code);
    assert.equal(result.error.status, status);
    assert.notEqual(benchmarkRecoveryMessage(result.error), "受控错误");
  }
});

test("invalid origins and note IDs fail locally and cannot trigger media or paid job requests", async () => {
  let calls = 0;
  const adapter = new HttpFrameFactoryAdapter({ fetch: async () => { calls += 1; return json({}); } });
  for (const value of ["http://www.xiaohongshu.com/user/profile/" + USER_A, "https://evil.test/user/profile/" + USER_A, "https://user:pass@www.xiaohongshu.com/user/profile/" + USER_A]) {
    assert.equal(benchmarkProfile(value), null);
    assert.equal((await adapter.generateBenchmarkAccountReport({ ...request, profileUrl: value })).ok, false);
  }
  for (const noteId of ["not-a-note", "AAAAAAAAAAAAAAAAAAAAAAAA", NOTE_A + "?xsec_token=TEST_TOKEN"]) {
    assert.equal((await adapter.collectBenchmarkNoteSourceEvidence({ ...request, noteId })).ok, false);
    assert.equal((await adapter.createBenchmarkAnalysisJob({ ...request, noteId }, "controlled-idempotency-key")).ok, false);
  }
  assert.equal(calls, 0);
});
