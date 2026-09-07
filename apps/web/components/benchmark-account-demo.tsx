"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type BenchmarkAccountReport,
  type BenchmarkNoteReport,
  type BenchmarkNoteSourceEvidence,
  type BenchmarkPublicMetric,
} from "@/lib/api";
import { Badge, LoadingScaffold, PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

import { BenchmarkVideoAnalysisPanel } from "@/components/benchmark-video-analysis";
import { BenchmarkConnectionPanel, type BenchmarkConnectionHandle } from "@/components/benchmark-connection";
import { benchmarkProfile, benchmarkRecoveryMessage, refreshedBenchmarkSelection, verifiedBenchmarkNote } from "@/lib/benchmark-recovery";

const SEED_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b";

interface SavedVideoSelection {
  profileUrl: string; profileUserId: string; noteId: string; title: string;
}

type PendingConnectionOperation = {
  profileEpoch: number; selectionEpoch: number;
} & ({ kind: "profile"; profileUrl: string; preferredNoteId?: string }
  | { kind: "source"; noteId: string } | { kind: "identity" });

function savedVideoSelection(): SavedVideoSelection | null {
  try {
    const value = JSON.parse(window.localStorage.getItem("vistora.benchmark.last-video") ?? "null") as SavedVideoSelection | null;
    if (!value || !/^[a-f0-9]{24}$/.test(value.profileUserId) || !/^[a-f0-9]{24}$/.test(value.noteId)) return null;
    if (value.profileUrl !== `https://www.xiaohongshu.com/user/profile/${value.profileUserId}` || typeof value.title !== "string") return null;
    return { ...value, title: value.title.slice(0, 200) };
  } catch {
    return null;
  }
}

function metricHint(metric: BenchmarkPublicMetric) {
  if (metric.precision === "lower_bound") return "平台下界";
  if (metric.precision === "rounded") return "平台约数";
  if (metric.precision === "unknown") return "精度未知";
  return "页面展示值";
}

function compactNumber(value?: number) {
  if (value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function dateTime(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "未知时间";
  return parsed.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

function noteDate(value: string | null) {
  return value ? new Date(value).toLocaleDateString("zh-CN") : "发布时间未知";
}

function formatLabel(format: BenchmarkNoteReport["format"]) {
  return format === "video" ? "视频" : format === "image" ? "图文" : "待获取详情";
}

function performanceLabel(tier: BenchmarkNoteReport["performance"]["tier"]) {
  if (tier === "top_candidate") return "高表现候选";
  if (tier === "above_baseline") return "高于基线";
  if (tier === "baseline") return "账号基线";
  if (tier === "below_baseline") return "低于基线";
  return "无法比较";
}

function evidenceLabel(level: BenchmarkNoteReport["viralMechanisms"][number]["level"]) {
  if (level === "observed") return "页面事实";
  if (level === "derived") return "样本推导";
  if (level === "inference") return "机制推断";
  return "证据限制";
}

function mediaTime(value: number) {
  const seconds = Math.max(0, Math.floor(value / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

export function BenchmarkAccountDemo({ archivedReport }: { archivedReport?: BenchmarkAccountReport } = {}) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [profileUrl, setProfileUrl] = useState(SEED_PROFILE_URL);
  const [report, setReport] = useState<BenchmarkAccountReport | null>(archivedReport ?? null);
  const [reportView, setReportView] = useState<"account" | "note">("account");
  const [selectedNoteIndex, setSelectedNoteIndex] = useState<number | null>(archivedReport?.noteReports[0]?.sampleIndex ?? 1);
  const [sourceEvidence, setSourceEvidence] = useState<BenchmarkNoteSourceEvidence | null>(null);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState("");
  const [loading, setLoading] = useState(!archivedReport);
  const [interactive, setInteractive] = useState(false);
  const [error, setError] = useState("");
  const [errorCode, setErrorCode] = useState("");
  const [identityLoading, setIdentityLoading] = useState(false);
  const [identityMessage, setIdentityMessage] = useState("");
  const [savedVideo, setSavedVideo] = useState<SavedVideoSelection | null>(null);
  const [connectionRequired, setConnectionRequired] = useState(false);
  const pendingConnection = useRef<PendingConnectionOperation | null>(null);
  const connectionPanel = useRef<BenchmarkConnectionHandle | null>(null);
  const profileRequest = useRef(0);
  const sourceRequest = useRef(0);
  const profileController = useRef<AbortController | null>(null);
  const sourceController = useRef<AbortController | null>(null);
  const identityController = useRef<AbortController | null>(null);
  const identityLastStarted = useRef(0);

  const load = useCallback(async (requestedUrl: string, preferredNoteId?: string, refreshIdentity = false) => {
    pendingConnection.current = null;
    setConnectionRequired(false);
    const requestId = ++profileRequest.current;
    const requestedProfile = benchmarkProfile(requestedUrl);
    const saved = savedVideoSelection();
    setSavedVideo(saved?.profileUserId === requestedProfile?.userId ? saved : null);
    profileController.current?.abort();
    sourceController.current?.abort();
    sourceController.current = null;
    identityController.current?.abort();
    identityController.current = null;
    const controller = new AbortController();
    profileController.current = controller;
    ++sourceRequest.current;
    setSourceLoading(false);
    setIdentityLoading(false);
    setIdentityMessage("");
    setLoading(true);
    setError("");
    setErrorCode("");
    setReport(null);
    setSourceEvidence(null);
    setSourceError("");
    const result = await adapter.generateBenchmarkAccountReport({
      platform: "xiaohongshu",
      profileUrl: requestedProfile?.url ?? requestedUrl.trim(),
      refreshNoteIdentity: refreshIdentity,
    }, controller.signal);
    if (requestId !== profileRequest.current) return;
    if (result.ok) {
      setReport(result.data);
      setProfileUrl(result.data.snapshot.profile.profileUrl);
      const targetNoteId = preferredNoteId ?? (saved?.profileUserId === result.data.snapshot.profile.userId ? saved.noteId : undefined);
      const savedNoteIndex = result.data.snapshot.notes.find((note) => verifiedBenchmarkNote(note) && note.noteId === targetNoteId)?.sampleIndex;
      setSelectedNoteIndex(
        savedNoteIndex ?? result.data.accountReport.topCandidateNoteIndexes[0]
          ?? result.data.noteReports[0]?.sampleIndex
          ?? 1,
      );
      if (result.data.snapshot.acquisition.identityErrorCode === "BENCHMARK_AUTHENTICATION_REQUIRED") {
        setConnectionRequired(true);
        pendingConnection.current = { kind: "identity", profileEpoch: requestId, selectionEpoch: sourceRequest.current };
      }
    }
    else {
      setReport(null);
      setError(benchmarkRecoveryMessage(result.error));
      setErrorCode(result.error.code);
      if (result.error.code === "BENCHMARK_AUTHENTICATION_REQUIRED") {
        setConnectionRequired(true);
        pendingConnection.current = { kind: "profile", profileUrl: requestedUrl, preferredNoteId, profileEpoch: requestId, selectionEpoch: sourceRequest.current };
      }
    }
    setLoading(false);
  }, [adapter]);

  const loadSourceEvidence = useCallback(async (noteId: string) => {
    if (sourceController.current) return;
    pendingConnection.current = null;
    setConnectionRequired(false);
    const requestId = ++sourceRequest.current;
    const expectedProfileId = report?.snapshot.profile.userId;
    const controller = new AbortController();
    sourceController.current = controller;
    setSourceLoading(true);
    setSourceError("");
    const result = await adapter.collectBenchmarkNoteSourceEvidence({
      platform: "xiaohongshu",
      profileUrl: report?.snapshot.profile.profileUrl ?? profileUrl.trim(),
      noteId,
    }, controller.signal);
    if (requestId !== sourceRequest.current) return;
    sourceController.current = null;
    if (result.ok && result.data.profileUserId === expectedProfileId && result.data.noteId === noteId) setSourceEvidence(result.data);
    else if (result.ok) {
      setSourceEvidence(null);
      setSourceError("返回的详情不属于当前账号和笔记，已停止展示。");
    }
    else {
      setSourceEvidence(null);
      setSourceError(`${benchmarkRecoveryMessage(result.error)}（${result.error.code}）`);
      if (result.error.code === "BENCHMARK_AUTHENTICATION_REQUIRED") {
        setConnectionRequired(true);
        pendingConnection.current = { kind: "source", noteId, profileEpoch: profileRequest.current, selectionEpoch: requestId };
      }
    }
    setSourceLoading(false);
  }, [adapter, profileUrl, report]);

  async function refreshNoteIdentity(afterConnection = false) {
    if (!report || identityController.current) return;
    if (!afterConnection && Date.now() - identityLastStarted.current < 10_000) {
      setIdentityMessage("笔记信息刚刚补采过，请至少等待 10 秒后重试。");
      return;
    }
    pendingConnection.current = null;
    identityLastStarted.current = Date.now();
    setConnectionRequired(false);
    const controller = new AbortController();
    identityController.current = controller;
    const profileEpoch = profileRequest.current;
    const selectionEpoch = ++sourceRequest.current;
    const currentProfile = report.snapshot.profile;
    const currentNote = report.snapshot.notes.find((note) => note.sampleIndex === selectedNoteIndex);
    const preserveNoteId = verifiedBenchmarkNote(currentNote) ? currentNote.noteId : undefined;
    sourceController.current?.abort();
    sourceController.current = null;
    setSourceLoading(false);
    setSourceEvidence(null);
    setSourceError("");
    setIdentityLoading(true);
    setIdentityMessage("");
    const result = await adapter.generateBenchmarkAccountReport({
      platform: "xiaohongshu", profileUrl: currentProfile.profileUrl, refreshNoteIdentity: true,
    }, controller.signal);
    if (identityController.current === controller) identityController.current = null;
    if (profileEpoch !== profileRequest.current || selectionEpoch !== sourceRequest.current) return;
    setIdentityLoading(false);
    if (!result.ok) {
      setIdentityMessage(`${benchmarkRecoveryMessage(result.error)}（${result.error.code}）`);
      if (result.error.code === "BENCHMARK_AUTHENTICATION_REQUIRED") {
        setConnectionRequired(true);
        pendingConnection.current = { kind: "identity", profileEpoch, selectionEpoch };
      }
      return;
    }
    if (result.data.snapshot.profile.userId !== currentProfile.userId) {
      setIdentityMessage("补采结果不属于当前账号，已停止合并。请核验主页后重试。");
      return;
    }
    setReport(result.data);
    const selection = refreshedBenchmarkSelection(result.data, currentProfile.userId, preserveNoteId);
    setSelectedNoteIndex(selection);
    const acquisition = result.data.snapshot.acquisition;
    if (acquisition.identityErrorCode === "BENCHMARK_AUTHENTICATION_REQUIRED") {
      setConnectionRequired(true);
      pendingConnection.current = { kind: "identity", profileEpoch, selectionEpoch };
    }
    setIdentityMessage(selection === null
      ? `已核验 ${acquisition.identifiedNoteCount} 篇，仍有 ${acquisition.unresolvedNoteCount} 篇缺少身份。请从列表重新选择已核验的笔记；补采不会自动开始付费分析。`
      : `笔记身份已更新。已核验 ${acquisition.identifiedNoteCount} 篇，仍有 ${acquisition.unresolvedNoteCount} 篇缺少身份。`);
  }

  const requireConnectionForVideo = useCallback(() => setConnectionRequired(true), []);

  function resumeAfterConnection() {
    const pending = pendingConnection.current;
    pendingConnection.current = null;
    setConnectionRequired(false);
    if (!pending) {
      void load(profileUrl, undefined, true);
      return;
    }
    if (pending.profileEpoch !== profileRequest.current || pending.selectionEpoch !== sourceRequest.current) return;
    if (pending.kind === "profile") void load(pending.profileUrl, pending.preferredNoteId, true);
    else if (pending.kind === "source") void loadSourceEvidence(pending.noteId);
    else void refreshNoteIdentity(true);
  }

  useEffect(() => {
    if (archivedReport) return;
    const profileEpoch = profileRequest;
    const sourceEpoch = sourceRequest;
    const timer = window.setTimeout(() => {
      setInteractive(true);
      const saved = savedVideoSelection();
      const queryNoteId = new URLSearchParams(window.location.search).get("note");
      const preferredNoteId = queryNoteId && /^[0-9a-f]{24}$/.test(queryNoteId) ? queryNoteId : undefined;
      if (saved || preferredNoteId) setReportView("note");
      void load(preferredNoteId ? SEED_PROFILE_URL : saved?.profileUrl ?? SEED_PROFILE_URL, preferredNoteId);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      ++profileEpoch.current;
      ++sourceEpoch.current;
      profileController.current?.abort();
      sourceController.current?.abort();
      identityController.current?.abort();
    };
  }, [load, archivedReport]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Freeze this click's target and invalidate any passive discovery in flight.
    profileController.current?.abort();
    sourceController.current?.abort();
    sourceController.current = null;
    identityController.current?.abort();
    identityController.current = null;
    pendingConnection.current = { kind: "profile", profileUrl,
      profileEpoch: ++profileRequest.current, selectionEpoch: ++sourceRequest.current };
    setLoading(false);
    setSourceLoading(false);
    setIdentityLoading(false);
    setConnectionRequired(true);
    void connectionPanel.current?.start();
  }

  function useSeed() {
    setProfileUrl(SEED_PROFILE_URL);
    void load(SEED_PROFILE_URL);
  }

  function connectForIdentity() {
    pendingConnection.current = { kind: "identity", profileEpoch: profileRequest.current,
      selectionEpoch: sourceRequest.current };
    setConnectionRequired(true);
    setIdentityMessage("");
    void connectionPanel.current?.start();
    document.getElementById("benchmark-connection")?.scrollIntoView({ block: "start" });
  }

  const snapshot = report?.snapshot ?? null;
  const identityNeedsLogin = snapshot?.acquisition.identityErrorCode === "BENCHMARK_AUTHENTICATION_REQUIRED";
  const selectedNote = selectedNoteIndex === null ? null : report?.noteReports.find((item) => item.sampleIndex === selectedNoteIndex) ?? null;
  const selectedSnapshotNote = snapshot?.notes.find(
    (item) => item.sampleIndex === selectedNote?.sampleIndex,
  ) ?? null;
  const verifiedNoteId = verifiedBenchmarkNote(selectedSnapshotNote) ? selectedSnapshotNote.noteId : undefined;
  const currentSourceEvidence = sourceEvidence?.profileUserId === snapshot?.profile.userId && sourceEvidence?.noteId === verifiedNoteId ? sourceEvidence : null;
  const selectedMediaKind = currentSourceEvidence?.media.kind ?? selectedNote?.format;
  const maxThemeCount = Math.max(1, ...(snapshot?.analysis.themes.map((item) => item.matchingNotes) ?? []));
  const externalProfileUrl = snapshot?.profile.profileUrl ?? SEED_PROFILE_URL;

  return (
    <div className="page page--wide benchmark-demo-page">
      <PageHeading
        eyebrow="07 / BENCHMARK LAB"
        title={archivedReport ? "已保存的账号策略报告" : "从公开主页，拆解内容策略与爆款机制"}
        description={archivedReport ? `分析于 ${dateTime(archivedReport.generatedAt)}。展示当次保存的账号与笔记证据，打开报告不会重新采集或开始分析。` : "同时生成账号总体报告和逐条笔记报告。每个结论都标注页面事实、样本推导、机制推断或证据限制，不把相关性包装成平台推荐因果。"}
        actions={<><a className="button-secondary" href="/benchmarks/history">历史分析记录</a><a className="button-ghost" href={externalProfileUrl} target="_blank" rel="noreferrer">打开真实主页 ↗</a></>}
      />

      {!archivedReport ? <BenchmarkConnectionPanel
        ref={connectionPanel}
        key={`${benchmarkProfile(profileUrl)?.userId ?? profileUrl}:${selectedNoteIndex ?? "none"}`}
        adapter={adapter} required={connectionRequired} onConnected={resumeAfterConnection}
      /> : null}

      {!archivedReport ? <section className="benchmark-source panel" aria-label="对标账号数据源">
        <div>
          <p className="eyebrow">PUBLIC PROFILE PREVIEW</p>
          <h2>{snapshot?.profile.nickname ?? "小红书公开主页"}</h2>
          <p className="muted">
            {snapshot
              ? `小红书号 ${snapshot.profile.redId} · 用户 ID ${snapshot.profile.userId}`
              : "当前仅支持 www.xiaohongshu.com/user/profile/{用户 ID}"}
          </p>
        </div>
        <form className="benchmark-profile-form" onSubmit={submit}>
          <label htmlFor="benchmark-profile-url">主页链接</label>
          <div>
            <input
              id="benchmark-profile-url"
              className="input"
              type="url"
              disabled={!interactive}
              value={profileUrl}
              onChange={(event) => {
                pendingConnection.current = null;
                setConnectionRequired(false);
                const value = event.target.value;
                setProfileUrl(benchmarkProfile(value)?.url ?? value);
                ++profileRequest.current;
                ++sourceRequest.current;
                profileController.current?.abort();
                sourceController.current?.abort();
                sourceController.current = null;
                identityController.current?.abort();
                identityController.current = null;
                setLoading(false);
                setIdentityLoading(false);
                setSourceLoading(false);
                setReport(null);
                setSavedVideo(null);
                setSourceEvidence(null);
                setError("");
                setErrorCode("");
              }}
              placeholder="https://www.xiaohongshu.com/user/profile/..."
              required
            />
            <button className="button" type="submit" disabled={loading}>
              {loading ? "正在采集…" : "连接小红书并采集"}
            </button>
          </div>
          <p>
            <Badge tone="accent">公开首屏样本</Badge>
            <button className="button-ghost" type="button" onClick={useSeed} disabled={loading}>使用白昼小熊示例</button>
          </p>
        </form>
      </section> : null}

      {loading ? <LoadingScaffold title="正在发现并补全笔记信息" description="采集公开首屏；字段不足时尝试本机浏览器核验。此阶段生成主页元数据报告，尚未开始视频深析。" cards={3} /> : null}
      {!archivedReport && !loading && report?.historyRecordId ? <p role="status" className="muted">账号报告已自动保存 · <a href={`/benchmarks/history?record=${encodeURIComponent(report.historyRecordId)}`}>查看历史版本</a></p> : null}
      {!archivedReport && !loading && report?.historyWarning ? <p role="alert" className="benchmark-deep-error">{report.historyWarning}</p> : null}

      {!loading && error ? (
        <StatePanel
          code={errorCode || "SRC"}
          title={errorCode === "BENCHMARK_API_VERSION_MISMATCH" ? "研究 API 需要更新或重新连接" : "公开数据源暂时不可用"}
          description={`${error} 页面没有回退到 Mock 或旧榜单数据。`}
          error
        >
          <button className="button" type="button" onClick={() => void load(profileUrl)}>重新尝试</button>
          <a className="button-ghost" href={benchmarkProfile(profileUrl)?.url ?? externalProfileUrl} target="_blank" rel="noreferrer">手动核验当前主页</a>
        </StatePanel>
      ) : null}

      {!loading && !snapshot && savedVideo ? (
        <section className="panel" aria-label="已保存视频分析恢复">
          <h2>上次查看的视频</h2>
          <p className="muted">主页当前不可用，仍可读取「{savedVideo.title}」已经保存的分析。</p>
          <BenchmarkVideoAnalysisPanel
            key={`${savedVideo.profileUserId}:${savedVideo.noteId}`}
            profileUrl={savedVideo.profileUrl}
            profileUserId={savedVideo.profileUserId}
            noteId={savedVideo.noteId}
            title={savedVideo.title}
            onAuthenticationRequired={requireConnectionForVideo}
            connectionRequired={connectionRequired}
          />
        </section>
      ) : null}

      {!loading && snapshot ? (
        <>
          <section className="benchmark-profile-card">
            <header>
              <div>
                <p className="eyebrow">LIVE PROFILE SNAPSHOT</p>
                <h2>{snapshot.profile.nickname}</h2>
                <p>{snapshot.profile.tags.length ? snapshot.profile.tags.join(" · ") : "主页未提供公开标签"}</p>
              </div>
              <div className="benchmark-capture-meta">
                <Badge tone={snapshot.acquisition.fromCache ? "neutral" : "success"}>
                  {archivedReport ? "已保存的采集快照" : snapshot.acquisition.fromCache
                    ? "5 分钟缓存"
                    : snapshot.acquisition.method === "authenticated_managed_browser"
                      ? "已登录浏览器实时获取"
                      : "公开页面实时获取"}
                </Badge>
                <small>采集于 {dateTime(snapshot.acquisition.capturedAt)}</small>
              </div>
            </header>
            <div className="benchmark-profile-metrics">
              {([
                ["关注", snapshot.profile.following],
                ["粉丝", snapshot.profile.followers],
                ["获赞与收藏", snapshot.profile.likesAndCollections],
              ] as const).map(([label, metric]) => (
                <article key={label}>
                  <span>{label}</span>
                  <strong>{metric.display}</strong>
                  <small>{metricHint(metric)}</small>
                </article>
              ))}
            </div>
          </section>

          <section className="benchmark-kpis" aria-label="样本分析">
            <article><span>首屏有效样本</span><strong>{snapshot.analysis.sampleSize}</strong><small>篇笔记</small></article>
            <article><span>视频占比</span><strong>{snapshot.analysis.videoSharePercent}%</strong><small>{snapshot.analysis.videoCount} 视频 / {snapshot.analysis.imageCount} 图文 / {snapshot.analysis.unknownCount} 待获取详情</small></article>
            <article><span>近 30 天发布</span><strong>{snapshot.notes.some((note) => note.publishedAt) ? snapshot.analysis.postsLast30Days : "—"}</strong><small>已排除置顶及缺少日期的笔记</small></article>
            <article><span>发布间隔中位数</span><strong>{snapshot.analysis.medianPublishIntervalDays ?? "—"}</strong><small>天 · 首屏样本</small></article>
            <article><span>点赞中位数下界</span><strong>{compactNumber(snapshot.analysis.medianLikesLowerBound)}</strong><small>“10万+”按 10 万计算</small></article>
          </section>

          {!archivedReport ? <section className="panel benchmark-identity" aria-label="笔记发现完整性" aria-busy={identityLoading}>
            <header><div><h3>笔记信息</h3><p>核验笔记归属后可获取媒体，图文不会进入视频分析。</p></div>
              <div className="benchmark-identity-counts"><Badge tone="neutral">已核验 {snapshot.acquisition.identifiedNoteCount} 篇</Badge><Badge tone={snapshot.acquisition.unresolvedNoteCount ? "warning" : "success"}>待补全 {snapshot.acquisition.unresolvedNoteCount} 篇</Badge></div>
            </header>
            <div className="benchmark-identity-recovery" data-warning={Boolean(snapshot.acquisition.identityErrorCode) || undefined}>
              <div role="status">
                <strong>{identityNeedsLogin ? "连接小红书后继续补全" : snapshot.acquisition.identityErrorCode ? "部分笔记信息暂时无法获取" : snapshot.acquisition.unresolvedNoteCount ? "部分笔记需要补充信息" : "当前样本的笔记信息已核验"}</strong>
                <p>{identityNeedsLogin ? "需要确认小红书登录。点击“连接并补全”，扫码或验证成功后会自动继续。" : snapshot.acquisition.identityErrorCode ? benchmarkRecoveryMessage({ code: snapshot.acquisition.identityErrorCode, message: "请补全笔记信息后重试。" }) : "可重新检查当前主页，更新笔记信息。"}</p>
              </div>
              <button className={identityNeedsLogin ? "button" : "button-secondary"} type="button" disabled={identityLoading} onClick={() => identityNeedsLogin ? connectForIdentity() : void refreshNoteIdentity()}>{identityLoading ? "正在补全…" : identityNeedsLogin ? "连接并补全" : "补全笔记信息"}</button>
            </div>
            {snapshot.acquisition.identityErrorCode ? <details className="benchmark-diagnostic"><summary>查看诊断信息</summary><code>{snapshot.acquisition.identityErrorCode}</code></details> : null}
            {identityLoading ? <p role="status">正在重新读取主页并核验笔记，请稍候。</p> : null}
            {identityMessage ? <p role="status">{identityMessage}</p> : null}
          </section> : null}

          {report ? (
            <section className="panel benchmark-report-workbench" aria-label="内容策略报告">
              <header className="benchmark-report-header">
                <div>
                  <p className="eyebrow">EVIDENCE-LED REPORT</p>
                  <h2>内容策略报告</h2>
                  <p className="muted">公开元数据证据 · {report.noteReports.length} 篇逐条报告</p>
                </div>
                <div className="benchmark-report-tabs" role="tablist" aria-label="报告视图">
                  <button
                    type="button"
                    id="benchmark-account-report-tab"
                    role="tab"
                    aria-controls="benchmark-account-report-panel"
                    aria-selected={reportView === "account"}
                    className={reportView === "account" ? "is-active" : ""}
                    onClick={() => setReportView("account")}
                  >总体报告</button>
                  <button
                    type="button"
                    id="benchmark-note-report-tab"
                    role="tab"
                    aria-controls="benchmark-note-report-panel"
                    aria-selected={reportView === "note"}
                    className={reportView === "note" ? "is-active" : ""}
                    onClick={() => setReportView("note")}
                  >单条报告</button>
                </div>
              </header>

              {reportView === "account" ? (
                <div
                  id="benchmark-account-report-panel"
                  className="benchmark-account-report"
                  role="tabpanel"
                  aria-labelledby="benchmark-account-report-tab"
                >
                  <article className="benchmark-report-lead">
                    <span>总体判断</span>
                    <strong>{report.accountReport.executiveSummary}</strong>
                  </article>
                  <div className="benchmark-report-summary-grid">
                    <article><span>内容形态</span><p>{report.accountReport.formatStrategy}</p></article>
                    <article><span>发布节奏</span><p>{report.accountReport.publishingStrategy}</p></article>
                  </div>
                  <div className="benchmark-account-report-grid">
                    <section>
                      <h3>标题策略与表现对照</h3>
                      <ol className="benchmark-pattern-list">
                        {report.accountReport.titlePatterns.map((pattern) => (
                          <li key={pattern.key}>
                            <div>
                              <strong>{pattern.label}</strong>
                              <span>{pattern.matchingNotes} 篇 · 高表现候选 {pattern.topCandidateMatches} 篇</span>
                            </div>
                            <b>{pattern.liftVsSampleMedian === undefined ? "—" : `${pattern.liftVsSampleMedian}×`}</b>
                          </li>
                        ))}
                      </ol>
                    </section>
                    <section>
                      <h3>可复测打法</h3>
                      <ol className="benchmark-playbook">
                        {report.accountReport.playbook.map((item, index) => (
                          <li key={item}><span>{String(index + 1).padStart(2, "0")}</span><p>{item}</p></li>
                        ))}
                      </ol>
                    </section>
                  </div>
                  <div className="benchmark-report-limitations">
                    <strong>结论边界</strong>
                    <ul>{report.accountReport.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                  </div>
                </div>
              ) : null}

              {reportView === "note" ? (
                <div
                  id="benchmark-note-report-panel"
                  className="benchmark-note-report"
                  role="tabpanel"
                  aria-labelledby="benchmark-note-report-tab"
                >
                  <div className="benchmark-note-picker">
                    <span className="field-label">选择一篇笔记</span>
                    <UiSelect
                      ariaLabel="选择一篇笔记"
                      value={String(selectedNote?.sampleIndex ?? "")}
                      onChange={(value) => {
                        pendingConnection.current = null;
                        ++sourceRequest.current;
                        sourceController.current?.abort();
                        sourceController.current = null;
                        identityController.current?.abort();
                        identityController.current = null;
                        setIdentityLoading(false);
                        setSourceLoading(false);
                        setSelectedNoteIndex(value ? Number(value) : null);
                        setSourceEvidence(null);
                        setSourceError("");
                      }}
                    >
                      <option value="">请选择已核验的笔记</option>
                      {report.noteReports.map((item) => (
                        <option key={item.sampleIndex} value={item.sampleIndex}>
                          {String(item.sampleIndex).padStart(2, "0")} · {verifiedBenchmarkNote(snapshot.notes.find((note) => note.sampleIndex === item.sampleIndex)) ? "已核验" : "身份待补全"} · {item.likesDisplay} · {item.title}
                        </option>
                      ))}
                    </UiSelect>
                  </div>

                  {selectedNote ? (
                    <article className="benchmark-note-report-body">
                      <header>
                        <div>
                          <p className="eyebrow">NOTE {String(selectedNote.sampleIndex).padStart(2, "0")}</p>
                          <h3>{selectedNote.title}</h3>
                          <p className="muted">{formatLabel(selectedMediaKind ?? "unknown")} · {noteDate(selectedNote.publishedAt)} · {selectedNote.likesDisplay}</p>
                        </div>
                        <div className="benchmark-performance">
                          <Badge tone={selectedNote.performance.tier === "top_candidate" ? "accent" : "neutral"}>{performanceLabel(selectedNote.performance.tier)}</Badge>
                          <strong>{selectedNote.performance.percentile === undefined ? "—" : `P${selectedNote.performance.percentile}`}</strong>
                          <small>账号内互动下界百分位</small>
                        </div>
                      </header>

                      {!archivedReport ? selectedMediaKind === "video" ? (
                        <BenchmarkVideoAnalysisPanel
                          key={`${snapshot.profile.userId}:${selectedSnapshotNote?.noteId ?? selectedNote.sampleIndex}`}
                          profileUrl={snapshot.profile.profileUrl}
                          profileUserId={snapshot.profile.userId}
                          noteId={verifiedNoteId}
                          title={selectedNote.title}
                          identityLoading={identityLoading}
                          onRefreshIdentity={() => void refreshNoteIdentity()}
                          onAuthenticationRequired={requireConnectionForVideo}
                          connectionRequired={connectionRequired}
                        />
                      ) : <p className="muted">{selectedMediaKind === "image" ? "这是一篇图文笔记，可获取正文与图片信息；视频深析不适用于图文。" : "尚未确认媒体类型，请先获取真实详情。确认是视频后才开放视频深析。"}</p> : null}

                      <div className="benchmark-note-report-grid">
                        <section>
                          <h4>写作策略</h4>
                          {selectedNote.strategySignals.length ? selectedNote.strategySignals.map((signal) => (
                            <article className="benchmark-strategy-card" key={signal.key}>
                              <strong>{signal.label}</strong>
                              <p>{signal.evidence}</p>
                              <small>{signal.likelyEffect}</small>
                              <b>可复用：{signal.reusableMove}</b>
                            </article>
                          )) : <p className="muted">标题没有命中当前策略规则，需要正文或画面证据。</p>}
                        </section>
                        <section>
                          <h4>为什么可能成为爆款</h4>
                          {selectedNote.viralMechanisms.map((insight, index) => (
                            <article className={`benchmark-evidence benchmark-evidence--${insight.level}`} key={`${insight.level}-${index}`}>
                              <header><span>{evidenceLabel(insight.level)}</span><small>{insight.confidence === "high" ? "高置信" : insight.confidence === "medium" ? "中置信" : "低置信"}</small></header>
                              <strong>{insight.claim}</strong>
                              <ul>{insight.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
                            </article>
                          ))}
                        </section>
                      </div>
                      <div className="benchmark-note-actions">
                        <section><h4>你的复用动作</h4><ul>{selectedNote.recommendations.map((item) => <li key={item}>{item}</li>)}</ul></section>
                        <section><h4>当前缺失证据</h4><ul>{selectedNote.limitations.map((item) => <li key={item}>{item}</li>)}</ul></section>
                      </div>

                      {!archivedReport ? <section className="benchmark-deep-lab" aria-label="已登录详情证据">
                        <header>
                          <div>
                            <p className="eyebrow">AUTHENTICATED SOURCE PROVIDER</p>
                            <h4>正文与媒体探针</h4>
                            <p className="muted">通过你已登录的本机浏览器打开这一篇笔记；凭据、短期令牌和签名媒体地址不会进入响应。</p>
                          </div>
                          <button
                            className="button-secondary"
                            type="button"
                            disabled={sourceLoading || identityLoading}
                            onClick={() => verifiedNoteId
                              ? void loadSourceEvidence(verifiedNoteId) : void refreshNoteIdentity()}
                          >{identityLoading ? "正在补全笔记信息…" : sourceLoading ? "正在获取真实详情…" : verifiedNoteId ? "获取真实详情证据" : "补全笔记信息后获取详情"}</button>
                        </header>
                        {sourceError ? <p className="benchmark-deep-error">{sourceError}</p> : null}
                        {currentSourceEvidence && sourceEvidence ? (
                          <div className="benchmark-deep-report">
                            <div className="benchmark-deep-source">
                              <div>
                                <Badge tone="success">已登录浏览器真实证据</Badge>
                                <strong>{sourceEvidence.title}</strong>
                                <small>
                                  赞 {sourceEvidence.likes.display} · 藏 {sourceEvidence.collects.display} · 评 {sourceEvidence.comments.display}
                                </small>
                              </div>
                              <p>{sourceEvidence.description || "页面没有可见正文。"}</p>
                            </div>
                            <div className="benchmark-deep-metrics">
                              <article><span>媒体类型</span><strong>{sourceEvidence.media.kind === "video" ? "视频" : "图文"}</strong><small>来源主机{sourceEvidence.media.trustedMediaOrigin ? "已通过白名单校验" : "未通过白名单校验"}</small></article>
                              <article><span>媒体长度</span><strong>{sourceEvidence.media.durationMs === undefined ? "—" : mediaTime(sourceEvidence.media.durationMs)}</strong><small>来自浏览器媒体元素探针</small></article>
                              <article><span>分辨率</span><strong>{sourceEvidence.media.width && sourceEvidence.media.height ? `${sourceEvidence.media.width}×${sourceEvidence.media.height}` : "—"}</strong><small>不返回媒体下载地址</small></article>
                            </div>
                            <div className="benchmark-report-limitations"><strong>Provider 边界</strong><ul>{sourceEvidence.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div>
                          </div>
                        ) : (
                          <div className="benchmark-deep-empty"><strong>{identityLoading ? "正在补全笔记信息" : sourceLoading ? "正在获取媒体信息" : "等待详情证据"}</strong><p>{verifiedNoteId ? "点击后由真实 Provider 获取，不使用 Seed。" : "当前笔记身份尚未核验。点击补全后从列表重新选择已核验笔记，无需手动复制笔记 ID。"}</p></div>
                        )}
                      </section> : null}

                    </article>
                  ) : <p className="muted">{report.noteReports.length ? "笔记信息已更新，请从上方列表重新选择。" : "当前样本没有可生成的单条报告。"}</p>}
                </div>
              ) : null}
            </section>
          ) : null}

          <div className="benchmark-analysis-grid">
            <section className="panel benchmark-themes">
              <header><div><p className="eyebrow">CONTENT SIGNALS</p><h2>标题主题信号</h2></div><small>关键词规则 · 非语义模型</small></header>
              {snapshot.analysis.themes.length ? (
                <ol>
                  {snapshot.analysis.themes.map((item) => (
                    <li key={item.theme}>
                      <div><strong>{item.theme}</strong><span>{item.matchingNotes} 篇</span></div>
                      <i aria-hidden="true"><b style={{ width: `${item.matchingNotes / maxThemeCount * 100}%` }} /></i>
                    </li>
                  ))}
                </ol>
              ) : <p className="muted">当前样本没有命中预设主题词。</p>}
            </section>

            <section className="panel benchmark-top-notes">
              <header><div><p className="eyebrow">TOP SAMPLE</p><h2>互动量下界最高</h2></div><small>仅首屏可见笔记</small></header>
              <ol>
                {snapshot.analysis.topNotes.map((note) => (
                  <li key={note.sampleIndex}>
                    <span>{String(note.sampleIndex).padStart(2, "0")}</span>
                    <div><strong>{note.title}</strong><small>{formatLabel(note.format)} · {noteDate(note.publishedAt)}{note.pinned ? " · 置顶" : ""}</small></div>
                    <b>{note.likes.display}</b>
                  </li>
                ))}
              </ol>
            </section>
          </div>

          <section className="panel benchmark-boundary">
            <div>
              <p className="eyebrow">DATA BOUNDARY</p>
              <h2>这份预览能说明什么</h2>
              <p className="muted">可以验证“主页 → 结构化快照 → 可解释指标”的技术链路；不能据此声称已覆盖全部 {snapshot.profile.nickname} 内容、精确粉丝趋势或评论语义。</p>
            </div>
            <ul>{snapshot.acquisition.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
          </section>
        </>
      ) : null}
    </div>
  );
}
