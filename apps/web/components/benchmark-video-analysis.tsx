"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ApiProblem,
  type BenchmarkAnalysisCapability,
  type BenchmarkAnalysisJob,
  type BenchmarkAnalysisJobStatus,
  type BenchmarkDeepFinding,
  type BenchmarkDeepNoteReport,
  type BenchmarkVideoAnalysis,
} from "@/lib/api";
import { Badge } from "@/components/page-heading";
import { benchmarkRecoveryMessage } from "@/lib/benchmark-recovery";

const STATUS_LABELS: Record<BenchmarkAnalysisJobStatus, string> = {
  pending: "等待分析", collecting: "获取视频", analyzing: "正在深度分析",
  ready: "分析完成", partial: "部分分析完成", failed: "分析失败",
  cancelled: "已取消", interrupted: "运行中断",
};

const CAPABILITY_LABELS: Record<string, string> = {
  probe: "视频信息", scene_detection: "镜头与节奏", ocr: "画面文字 OCR",
  vision: "视觉内容理解", asr: "口播转写", audio_metrics: "配音与声音测量",
  creative_strategy: "叙事与视听策略",
};

const CAPABILITY_STATUS: Record<BenchmarkAnalysisCapability["status"], string> = {
  complete: "已完成", partial: "部分完成", unavailable: "不可用", failed: "失败", not_applicable: "不适用",
};

const VISION_LABELS: Record<string, string> = {
  description: "画面", subject: "主体", action: "动作", composition: "构图",
  lighting: "光线", color: "色彩", shot_type: "景别", camera_motion: "运镜", visual_style: "风格",
};

function problemMessage(problem: ApiProblem) {
  const messages: Record<string, string> = {
    BENCHMARK_ANALYSIS_UNAVAILABLE: "本机的视频分析服务尚未启用。",
    BENCHMARK_AUTHENTICATION_REQUIRED: "小红书登录已失效，请先使用页面上方的“连接小红书”，完成后再次点击开始或重试分析。",
    BENCHMARK_PROVIDER_UNAVAILABLE: "小红书采集服务暂时不可用，请恢复服务后重试。",
    BENCHMARK_JOB_QUEUE_FULL: "当前分析队列已满，请等待已有任务完成。",
    BENCHMARK_JOB_STORAGE_FULL: "分析存储空间已满，请处理存储空间后重试。",
    BENCHMARK_JOB_INTERRUPTED: "上次运行中断，可重试继续分析。",
    BENCHMARK_JOB_CANCELLED: "这次分析已取消。",
    BENCHMARK_JOB_STILL_STOPPING: "正在结束上次运行，请稍后再重试。",
    BENCHMARK_JOB_ATTEMPTS_EXHAUSTED: "这项任务已达到重试次数上限。",
    BENCHMARK_MEDIA_TIMEOUT: "获取视频超时，请稍后重试。",
    BENCHMARK_WORKER_TIMEOUT: "视频处理超时，请检查分析服务后重试。",
    BENCHMARK_MEDIA_TOO_LARGE: "这条视频超过当前分析大小限制。",
    BENCHMARK_MEDIA_UNAVAILABLE: "没有取得这篇笔记的可用视频，请检查原笔记是否仍可访问。",
    BENCHMARK_NOTE_NOT_IN_SAMPLE: "主页当前可见样本中找不到这篇笔记，请重新获取主页报告。",
  };
  return messages[problem.code] ?? benchmarkRecoveryMessage(problem);
}

function progressMessage(job: BenchmarkAnalysisJob) {
  const messages: Record<string, string> = {
    pending: "任务已保存，正在等待可用的分析时段。",
    collecting: "正在获取选中笔记的真实视频。",
    analyzing: "正在分析画面、屏幕文字和音轨。",
    ready: "视频分析与报告已保存。",
    partial: "已保存可用结果，部分能力未完成。",
    cancelled: "分析已取消。",
    interrupted: "运行已中断，可重试恢复。",
  };
  return messages[job.progress.stage] ?? job.progress.message;
}

function timecode(milliseconds: number) {
  const seconds = Math.max(0, milliseconds / 1000);
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

function running(status?: BenchmarkAnalysisJobStatus) {
  return status === "pending" || status === "collecting" || status === "analyzing";
}

function findingLabel(category: BenchmarkDeepFinding["category"]) {
  return { hook: "开场钩子", visual: "视觉策略", narration: "口播策略", rhythm: "视听节奏", limitation: "证据限制" }[category];
}

function FrameImage({ src, timestampMs }: { src: string; timestampMs: number }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <p className="muted">{timecode(timestampMs)} 的画面预览暂时不可用。</p>;
  // Research frames use the local artifact route directly so optimization does not cache them elsewhere.
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={`${timecode(timestampMs)} 视频采样画面`} loading="lazy" onError={() => setFailed(true)} style={{ width: "100%", maxHeight: 280, objectFit: "contain", borderRadius: 8, background: "#111" }} />;
}

function StrategyReport({ report }: { report: BenchmarkDeepNoteReport }) {
  return (
    <section className="benchmark-deep-report" aria-label="单条视频策略报告">
      <h4>内容策略与可复用动作</h4>
      <p>{report.summary}</p>
      <div className="benchmark-deep-metrics">
        {report.metrics.map((metric) => <article key={metric.key}><span>{metric.label}</span><strong>{metric.value}</strong><small>{metric.interpretation}</small></article>)}
      </div>
      <div className="benchmark-deep-findings">
        {report.findings.map((finding, index) => (
          <article key={`${finding.category}-${index}`}>
            <header><span>{findingLabel(finding.category)}</span><small>{finding.confidence === "high" ? "高置信" : finding.confidence === "medium" ? "中置信" : "低置信"}</small></header>
            <strong>{finding.claim}</strong>
            <ul>{finding.evidence.map((item) => <li key={item}>{item}</li>)}</ul>
            {finding.reusableMove ? <p>可复用：{finding.reusableMove}</p> : null}
          </article>
        ))}
      </div>
      <details>
        <summary>证据时间轴 · {report.timeline.length} 段</summary>
        <ol className="benchmark-deep-timeline">
          {report.timeline.map((item, index) => (
            <li key={`${item.startMs}-${index}`}>
              <div className="benchmark-deep-time"><b>{timecode(item.startMs)}</b><i aria-hidden="true" /><small>{timecode(item.endMs)}</small></div>
              <article>
                <strong>{item.label}</strong><p>{item.description}</p>
                {item.transcript ? <blockquote>口播：{item.transcript}</blockquote> : null}
                {item.ocrText.length ? <p>屏幕文字：{item.ocrText.join(" / ")}</p> : null}
                {item.audioEvents.length ? <p>声音：{item.audioEvents.join(" / ")}</p> : null}
              </article>
            </li>
          ))}
        </ol>
      </details>
      {report.limitations.length ? <div className="benchmark-report-limitations"><strong>报告证据边界</strong><ul>{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
    </section>
  );
}

function AudioEvidence({ analysis, job }: { analysis: BenchmarkVideoAnalysis; job: BenchmarkAnalysisJob }) {
  const audio = analysis.audioAnalysis;
  const audioArtifact = job.artifacts.find((artifact) => artifact.mediaType.startsWith("audio/"));
  const [audioFailed, setAudioFailed] = useState(false);
  // The timecoded transcript is the audio player's text alternative immediately below it.
  // eslint-disable-next-line jsx-a11y/media-has-caption
  const player = audioArtifact ? <audio controls preload="none" src={audioArtifact.contentUrl} onError={() => setAudioFailed(true)} aria-label="分析音轨，转写文本见下方" /> : null;
  const metrics = [
    ["平均声音电平", audio.rmsDbfs, " dBFS"],
    ["声音峰值", audio.peakDbfs, " dBFS"],
    ["峰均比", audio.crestFactorDb, " dB"],
    ["语音活动占比", audio.vadSpeechRatio === undefined ? undefined : audio.vadSpeechRatio * 100, "%"],
    ["转写语速", audio.transcribedCharactersPerSecond, " 字/秒"],
    ["削波采样占比", audio.clippedSampleRatio === undefined ? undefined : audio.clippedSampleRatio * 100, "%"],
    ["语音区间周期候选中位值", audio.pitchAnalysis.medianHz, " Hz"],
    ["周期候选 P90–P10 范围", audio.pitchAnalysis.rangeHz, " Hz"],
  ] as const;
  return (
    <section aria-label="口播与配音分析">
      <h4>口播、配音与声音</h4>
      <p>{analysis.speechStatus === "present" ? "检测到语音活动" : analysis.speechStatus === "absent" ? "语音检测未发现活动片段" : "语音是否存在仍未确认"}</p>
      {audioArtifact ? (
        <div>
          {audioFailed ? <p className="muted">音轨预览暂时不可用，已保存的转写与测量结果仍可阅读。</p> : player}
        </div>
      ) : null}
      <div className="benchmark-deep-metrics">
        {metrics.filter((metric) => metric[1] !== undefined).map(([label, value, unit]) => (
          <article key={label}><span>{label}</span><strong>{value?.toFixed(2)}{unit}</strong></article>
        ))}
      </div>
      {audio.findings.length ? <ul>{audio.findings.map((finding) => <li key={finding}>{finding}</li>)}</ul> : null}
      {audio.pitchAnalysis.limitations.length ? <p className="muted">{audio.pitchAnalysis.limitations.join("；")}</p> : null}
      {analysis.transcript.segments.length ? (
        <ol className="benchmark-deep-timeline">
          {analysis.transcript.segments.map((segment, index) => (
            <li key={`${segment.startMs}-${index}`}><div className="benchmark-deep-time"><b>{timecode(segment.startMs)}</b><small>{timecode(segment.endMs)}</small></div><article><p>{segment.text}</p></article></li>
          ))}
        </ol>
      ) : analysis.transcript.text ? (
        <div><p className="muted">转写已完成，当前结果没有可验证的分段时间码。</p><blockquote>{analysis.transcript.text}</blockquote></div>
      ) : (
        <p className="muted">没有可展示的转写文本。{analysis.speechStatus === "absent" ? "请结合音轨与检测范围理解此结果。" : "空转写不代表视频没有口播。"}</p>
      )}
      {audio.silences.length ? <details><summary>低音量片段 · {audio.silences.length} 段</summary><p>{audio.silences.map((silence) => `${timecode(silence.startMs)}–${timecode(silence.endMs)}`).join("、")}</p></details> : null}
      {audio.limitations.length ? <div className="benchmark-report-limitations"><strong>声音结论边界</strong><ul>{audio.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
    </section>
  );
}

function FrameEvidenceLinks({ keys, analysis }: { keys: string[]; analysis: BenchmarkVideoAnalysis }) {
  const frames = keys.map((key) => analysis.frames.find((frame) => frame.key === key)).filter((frame) => frame !== undefined);
  if (!frames.length) return null;
  return <p>画面证据：{frames.map((frame) => <a key={frame.key} href={`#frame-${frame.timestampMs}`} style={{ marginRight: 10 }}>{timecode(frame.timestampMs)}</a>)}</p>;
}

function NarrativeEvidence({ analysis }: { analysis: BenchmarkVideoAnalysis }) {
  const narrative = analysis.narrativeAnalysis;
  if (!narrative.sections.length && !narrative.voiceoverFindings.length && !narrative.audioVisualFindings.length) return null;
  return (
    <section aria-label="叙事与视听深度分析">
      <h4>叙事结构、口播写作与视听配合</h4>
      {narrative.sections.length ? <ol className="benchmark-deep-timeline">
        {narrative.sections.map((section, index) => <li key={`${section.startMs}-${index}`}>
          <div className="benchmark-deep-time"><b>{timecode(section.startMs)}</b><small>{timecode(section.endMs)}</small></div>
          <article><strong>{section.role}</strong><p>观察：{section.observation}</p><p>策略推断：{section.strategyHypothesis}</p>
            {section.transcriptQuotes.map((quote) => <blockquote key={quote}>原话：{quote}</blockquote>)}
            <FrameEvidenceLinks keys={section.evidenceFrameKeys} analysis={analysis} />
          </article>
        </li>)}
      </ol> : null}
      <div className="benchmark-deep-columns">
        {narrative.voiceoverFindings.length ? <section><h4>口播与配音策略</h4><div className="benchmark-deep-findings">
          {narrative.voiceoverFindings.map((finding, index) => <article key={index}>
            <strong>{finding.claim}</strong><small>{finding.evidenceKind === "transcript" ? "依据转写文本" : "依据音频测量"}</small>
            {finding.transcriptQuote ? <blockquote>原话：{finding.transcriptQuote}</blockquote> : null}
          </article>)}
        </div></section> : null}
        {narrative.audioVisualFindings.length ? <section><h4>声音与画面如何配合</h4><div className="benchmark-deep-findings">
          {narrative.audioVisualFindings.map((finding, index) => <article key={index}>
            <strong>{finding.claim}</strong>
            {finding.transcriptQuote ? <blockquote>原话：{finding.transcriptQuote}</blockquote> : null}
            <FrameEvidenceLinks keys={finding.evidenceFrameKeys} analysis={analysis} />
          </article>)}
        </div></section> : null}
      </div>
      {narrative.limitations.length ? <div className="benchmark-report-limitations"><strong>叙事推断边界</strong><ul>{narrative.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
    </section>
  );
}

export function BenchmarkVideoAnalysisPanel({ profileUrl, profileUserId, noteId, title, archivedJob, identityLoading = false, onRefreshIdentity, onAuthenticationRequired, connectionRequired = false }: {
  profileUrl: string; profileUserId: string; noteId?: string; title: string;
  archivedJob?: BenchmarkAnalysisJob;
  identityLoading?: boolean; onRefreshIdentity?: () => void;
  onAuthenticationRequired?: () => void;
  connectionRequired?: boolean;
}) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [job, setJob] = useState<BenchmarkAnalysisJob | null>(archivedJob ?? null);
  const [restoring, setRestoring] = useState(Boolean(noteId) && !archivedJob);
  const [action, setAction] = useState<"create" | "cancel" | "retry" | "refresh" | null>(null);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const mounted = useRef(false);
  const requestEpoch = useRef(0);
  const actionController = useRef<AbortController | null>(null);
  const creationKey = useRef<string | null>(null);
  const actionPending = useRef(false);

  const acceptJob = useCallback((received: BenchmarkAnalysisJob) => {
    if (received.profileUserId !== profileUserId || received.noteId !== noteId) {
      setError("返回的分析不属于当前笔记，请刷新后重试。");
      return false;
    }
    setJob(received);
    return true;
  }, [noteId, profileUserId]);

  useEffect(() => {
    mounted.current = true;
    const epoch = requestEpoch;
    return () => {
      mounted.current = false;
      ++epoch.current;
      actionController.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!noteId || archivedJob) return;
    try {
      window.localStorage.setItem("vistora.benchmark.last-video", JSON.stringify({ profileUrl, profileUserId, noteId, title }));
    } catch {
      // A blocked browser storage policy does not prevent server-side job persistence.
    }
  }, [profileUrl, profileUserId, noteId, title, archivedJob]);

  useEffect(() => {
    if (!noteId || archivedJob) return;
    const controller = new AbortController();
    const epoch = requestEpoch.current;
    let stopped = false;
    void adapter.getLatestBenchmarkAnalysisJob(profileUserId, noteId, controller.signal).then((result) => {
      if (stopped || epoch !== requestEpoch.current) return;
      if (result.ok) acceptJob(result.data);
      else if (result.error.code !== "BENCHMARK_ANALYSIS_NOT_FOUND") setError(problemMessage(result.error));
      setRestoring(false);
    });
    return () => { stopped = true; controller.abort(); };
  }, [adapter, profileUserId, noteId, acceptJob, archivedJob]);

  const jobId = job?.id;
  const jobStatus = job?.status;
  useEffect(() => {
    if (!archivedJob && job?.error?.code === "BENCHMARK_AUTHENTICATION_REQUIRED") onAuthenticationRequired?.();
  }, [archivedJob, job?.error?.code, onAuthenticationRequired]);
  useEffect(() => {
    if (archivedJob || !jobId || !running(jobStatus) || action) return;
    const controller = new AbortController();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    const poll = async () => {
      const epoch = requestEpoch.current;
      const result = await adapter.getBenchmarkAnalysisJob(jobId, controller.signal);
      if (stopped || epoch !== requestEpoch.current) return;
      if (result.ok) {
        failures = 0;
        setPollError("");
        if (!acceptJob(result.data) || !running(result.data.status)) return;
      } else {
        ++failures;
        const mustRefresh = result.error.status === 401 || result.error.status === 403 || result.error.status === 404;
        setPollError(`进度连接暂时中断：${problemMessage(result.error)} 已保存的任务不会因此取消，${mustRefresh ? "请刷新进度确认任务状态。" : "正在重新连接。"}`);
        if (mustRefresh) return;
      }
      timer = setTimeout(poll, Math.min(20_000, 2500 * 2 ** Math.min(failures, 3)));
    };
    timer = setTimeout(poll, 1500);
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [adapter, jobId, jobStatus, action, acceptJob, archivedJob]);

  async function runAction(next: "create" | "cancel" | "retry" | "refresh") {
    if (archivedJob || !noteId || !/^[a-f0-9]{24}$/.test(noteId) || actionPending.current || identityLoading) return;
    if (connectionRequired && (next === "create" || next === "retry")) return;
    actionPending.current = true;
    const epoch = ++requestEpoch.current;
    actionController.current?.abort();
    const controller = new AbortController();
    actionController.current = controller;
    setAction(next);
    setError("");
    setPollError("");
    if (next === "create") creationKey.current ??= globalThis.crypto.randomUUID();
    const result = next === "create"
      ? await adapter.createBenchmarkAnalysisJob({ platform: "xiaohongshu", profileUrl, noteId }, creationKey.current!, controller.signal)
      : next === "cancel" && job
        ? await adapter.cancelBenchmarkAnalysisJob(job.id, controller.signal)
        : next === "retry" && job
          ? await adapter.retryBenchmarkAnalysisJob(job.id, controller.signal)
          : await adapter.getLatestBenchmarkAnalysisJob(profileUserId, noteId, controller.signal);
    if (!mounted.current || epoch !== requestEpoch.current) return;
    if (result.ok) {
      acceptJob(result.data);
      if (next === "create") creationKey.current = null;
    } else if (next === "refresh" && result.error.code === "BENCHMARK_ANALYSIS_NOT_FOUND") {
      setJob(null);
    } else {
      setError(problemMessage(result.error));
      if (result.error.code === "BENCHMARK_AUTHENTICATION_REQUIRED") onAuthenticationRequired?.();
    }
    setRestoring(false);
    setAction(null);
    actionPending.current = false;
  }

  const busy = restoring || action !== null || identityLoading;
  const active = running(jobStatus);
  const retryable = job && ["failed", "cancelled", "interrupted"].includes(job.status) && job.attempt < 3 && job.error?.retryable !== false;
  const analysis = job?.analysis;
  const report = job?.report?.sourceKind === "worker_asset_analysis" ? job.report : null;
  const limits = [...new Set([...(analysis?.limitations ?? []), ...(job?.sourceEvidence?.limitations ?? [])])];

  return (
    <section className="benchmark-deep-lab" aria-label="完整视频分析">
      <header>
        <div>
          <p className="eyebrow">VIDEO ANALYSIS</p>
          <h4>视频画面、OCR、口播与配音深析</h4>
          <p className="muted">{archivedJob ? "这是当次分析的保存版本，仅展示已有证据；打开报告不会重新采集或调用模型。" : `分析「${title}」的真实视频，提取画面、屏幕文字、转写与声音证据，生成带时间码的策略报告。离开页面后任务继续，回来可读取已保存的进度与结果。`}</p>
        </div>
        {!archivedJob ? <div className="benchmark-deep-buttons">
          {!noteId && onRefreshIdentity ? <button className="button" type="button" disabled={identityLoading} onClick={onRefreshIdentity}>{identityLoading ? "正在补全笔记信息…" : "补全笔记信息后分析"}</button> : !active && (!job || job.status === "ready" || job.status === "partial") ? <button className="button" type="button" disabled={busy || !noteId || connectionRequired} onClick={() => void runAction("create")}>{action === "create" ? "正在提交…" : job ? "重新分析" : "开始完整视频分析"}</button> : null}
          {active ? <button className="button-secondary" type="button" disabled={busy} onClick={() => void runAction("cancel")}>{action === "cancel" ? "正在取消…" : "取消分析"}</button> : null}
          {retryable ? <button className="button" type="button" disabled={busy || connectionRequired} onClick={() => void runAction("retry")}>{action === "retry" ? "正在重试…" : "重试分析"}</button> : null}
          {connectionRequired ? <a className="button-secondary" href="#benchmark-connection">连接小红书后再开始分析</a> : null}
          {noteId ? <button className="button-ghost" type="button" disabled={busy} onClick={() => void runAction("refresh")}>{restoring || action === "refresh" ? "读取已保存的分析…" : "刷新进度与报告"}</button> : null}
          <a className="button-ghost" href="/benchmarks/history">历史分析记录</a>
        </div> : null}
      </header>

      {!archivedJob ? <><p>流程：连接小红书 → 补全笔记信息 → 获取媒体 → 分析 → 报告</p>
      <p role="status">当前：{identityLoading ? "正在补全笔记信息" : !noteId ? "等待补全笔记信息" : active ? jobStatus === "collecting" ? "正在获取媒体" : jobStatus === "analyzing" ? "正在分析媒体证据" : "等待分析" : report ? "报告已保存" : "笔记身份已核验"}</p></> : null}
      {!noteId ? <p className="muted">这篇笔记缺少可核验身份。点击补全后重新选择已核验笔记，再开始分析。</p> : null}
      {error ? <p className="benchmark-deep-error" role="alert">{error}</p> : null}
      {!archivedJob && (error || job?.error) && onRefreshIdentity ? <button className="button-secondary" type="button" disabled={busy || active} onClick={onRefreshIdentity}>补全笔记信息后重试</button> : null}
      {pollError ? <p className="benchmark-deep-error" role="status">{pollError}</p> : null}
      {job ? (
        <div className="benchmark-deep-report">
          <div className="benchmark-deep-source" aria-live="polite">
            <div>
              <Badge tone={job.status === "ready" ? "success" : job.status === "partial" || job.status === "failed" || job.status === "interrupted" ? "warning" : "accent"}>{STATUS_LABELS[job.status] ?? "未知状态"}</Badge>
              <strong>{job.title || title}</strong>
              <small>第 {job.attempt} 次执行 · 更新于 {new Date(job.updatedAt).toLocaleString("zh-CN")}</small>
            </div>
            <p>{progressMessage(job)}</p>
          </div>
          {active ? <progress aria-label="视频分析进度" max={100} value={job.progress.percent} style={{ width: "100%" }}>{job.progress.percent}%</progress> : null}
          {job.error ? <p className="benchmark-deep-error" role="alert">{problemMessage(job.error)}{!retryable && job.attempt >= 3 ? " 已达到重试次数上限。" : ""}</p> : null}
          {job.status === "partial" ? <p className="muted">当前只完成了下方标记成功的分析项。{archivedJob ? "历史版本保留当时的能力缺口，不代表完整分析。" : "补齐不可用的分析能力后，可点击“重新分析”。"}</p> : null}
          {job.sourceEvidence?.description ? <details><summary>原笔记正文</summary><p style={{ whiteSpace: "pre-wrap" }}>{job.sourceEvidence.description}</p></details> : null}

          {analysis ? (
            <>
              <div className="benchmark-deep-metrics" aria-label="分析能力完成状态">
                {Object.entries(analysis.capabilities).map(([key, capability]) => (
                  <article key={key}>
                    <span>{CAPABILITY_LABELS[key] ?? key}</span>
                    <strong>{CAPABILITY_STATUS[capability.status] ?? "未知"}</strong>
                    {capability.limitations.length ? <small>{capability.limitations.join("；")}</small> : null}
                  </article>
                ))}
              </div>
              <section aria-label="画面与 OCR 证据">
                <h4>画面与屏幕文字 · {analysis.frames.length} 个采样点</h4>
                <p className="muted">时间码对应视频采样位置；采样之外的短暂画面或字幕可能未被覆盖。</p>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 260px), 1fr))", gap: 16 }}>
                  {analysis.frames.map((frame) => (
                    <article className="benchmark-strategy-card" id={`frame-${frame.timestampMs}`} key={frame.key}>
                      <strong>{timecode(frame.timestampMs)}</strong>
                      {frame.artifactUrl ? <FrameImage key={frame.artifactUrl} src={frame.artifactUrl} timestampMs={frame.timestampMs} /> : <p className="muted">此采样点暂无画面预览。</p>}
                      {Object.entries(frame.vision).filter(([key, value]) => VISION_LABELS[key] && value).map(([key, value]) => <p key={key}><span>{VISION_LABELS[key]}：</span>{value}</p>)}
                      <p><span>屏幕文字：</span>{frame.ocrText || (analysis.capabilities.ocr?.status === "complete" ? "该采样点未识别到文字。" : "该采样点暂无可用 OCR 结果。")}</p>
                    </article>
                  ))}
                </div>
              </section>
              {analysis.creativeInsights.length ? (
                <section><h4>跨镜头视觉策略</h4><div className="benchmark-deep-findings">
                  {analysis.creativeInsights.map((insight, index) => <article key={index}><strong>{insight.claim}</strong><p>证据：{insight.evidenceFrameKeys.map((key) => {
                    const frame = analysis.frames.find((item) => item.key === key);
                    return frame ? <a key={key} href={`#frame-${frame.timestampMs}`} style={{ marginRight: 10 }}>{timecode(frame.timestampMs)}</a> : null;
                  })}</p><small>基于画面的策略推断</small></article>)}
                </div></section>
              ) : null}
              <AudioEvidence key={`${job.id}-${job.attempt}`} analysis={analysis} job={job} />
              <NarrativeEvidence analysis={analysis} />
            </>
          ) : active ? <p className="muted">媒体证据正在处理，完成后在这里显示画面、文字与声音结果。</p> : null}
          {report ? <StrategyReport report={report} /> : !active && (job.status === "ready" || job.status === "partial") ? <p className="muted">媒体处理已结束，当前尚无可用的策略报告。</p> : null}
          {limits.length ? <div className="benchmark-report-limitations"><strong>本次分析覆盖范围</strong><ul>{limits.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
        </div>
      ) : !restoring ? <div className="benchmark-deep-empty"><strong>这篇视频还没有已保存的深析报告</strong><p>点击“开始完整视频分析”，自动获取这篇视频并逐步生成证据与报告。</p></div> : <p className="muted" role="status">正在读取这篇笔记的历史分析…</p>}
    </section>
  );
}
