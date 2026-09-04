"use client";

import Link from "next/link";
import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type WebpageVideoCapture,
  type WebpageVideoReviewRequest,
  type WebpageVideoRun,
} from "@/lib/api";
import { safeBrowserMediaUrl } from "@/lib/safe-media-url";
import { WebpageVideoSitePlanPanel } from "@/components/webpage-video-site-plan";
import { WebpageVideoPilotPanel } from "@/components/webpage-video-pilot-panel";
import { WebpageVideoEvidenceChain } from "@/components/webpage-video-evidence-chain";
import { useConfirmDialog } from "@/components/confirm-dialog";
import {
  buildWebpageVideoEvidenceReport,
  webpageVideoEvidenceFilename,
} from "@/lib/webpage-video-evidence";

type PageState = "loading" | "ready" | "error";
type CaptureState = "waiting" | "loading" | "ready" | "error";
type PreviewState = "idle" | "loading" | "loaded" | "error";
type PreviewResult = { key: string; status: "loaded" | "error" };
type Action = WebpageVideoReviewRequest["decision"] | "cancel" | null;

const terminalStates = new Set(["succeeded", "failed", "cancelled"]);
const captureStates = new Set([
  "awaiting_capture_review",
  "promoting_asset",
  "analyzing_asset",
  "asset_review_required",
  "composing",
  "rendering",
  "quality_check",
  "quality_review_required",
  "succeeded",
  "failed",
]);

const statusCopy: Record<string, { label: string; note: string }> = {
  queued: { label: "已排队", note: "等待隔离截图 Worker 接单。" },
  validating_url: { label: "校验 URL", note: "正在校验协议、DNS、重定向与最终网络地址。" },
  capturing: { label: "网页截图中", note: "隔离浏览器正在生成与目标画幅一致的 viewport 截图。" },
  discovering_pages: { label: "定向发现页面", note: "正在同源范围内发现候选页面，并执行去重、限深与安全过滤。" },
  discovering: { label: "定向发现页面", note: "正在同源范围内发现候选页面，并执行去重、限深与安全过滤。" },
  awaiting_scope_review: { label: "等待页面范围审核", note: "请选择允许逐页截图的页面；批准前不会启动批量截图。" },
  capturing_pages: { label: "逐页截图中", note: "正在为已批准页面生成完整截图与可追溯内容哈希。" },
  analyzing_regions: { label: "识别关键区域", note: "正在结合页面结构与画面识别 Hero、图表、功能和 CTA。" },
  planning_storyboard: { label: "规划镜头板", note: "正在从页面全景与关键区域生成候选镜头顺序。" },
  awaiting_storyboard_review: { label: "等待镜头板审核", note: "请核对所有启用镜头的预览、顺序与来源，再批准生成视频。" },
  awaiting_capture_review: { label: "等待截图审核", note: "请核对画面、URL 与 SHA-256，再决定是否进入视频阶段。" },
  promoting_asset: { label: "素材固化中", note: "正在固化你批准的精确截图哈希与来源证据。" },
  analyzing_asset: { label: "素材处理中", note: "系统正在准备已批准截图供本次成片使用。" },
  asset_review_required: { label: "素材门禁需处理", note: "素材存在服务端门禁，当前页面不会绕过它继续渲染。" },
  composing: { label: "内容编排中", note: "正在生成旁白、字幕与剪辑时间线。" },
  rendering: { label: "视频渲染中", note: "正在把已批准截图、声音和字幕合成为 MP4。" },
  quality_check: { label: "成片质检中", note: "正在检查成片尺寸、时长和技术质量。" },
  quality_review_required: { label: "等待最终质检处理", note: "最终 QC 需要人工处理；这不是截图审核，截图决定按钮不会重新开放。" },
  succeeded: { label: "成片完成", note: "最终视频已生成。" },
  failed: { label: "运行失败", note: "任务已停止；请查看失败原因后刷新或重新创建。" },
  cancelled: { label: "已取消", note: "任务已取消，不会继续截图、固化或渲染。" },
};

const stages = [
  { key: "validating", label: "URL 校验", statuses: ["queued", "validating_url"] },
  { key: "capture", label: "隔离截图", statuses: ["capturing"] },
  { key: "review", label: "截图审核", statuses: ["awaiting_capture_review"] },
  { key: "materialize", label: "素材固化", statuses: ["promoting_asset", "analyzing_asset", "asset_review_required"] },
  { key: "compose", label: "内容编排", statuses: ["composing"] },
  { key: "render", label: "渲染质检", statuses: ["rendering", "quality_check", "quality_review_required", "succeeded"] },
] as const;

const siteStages = [
  { key: "validating", label: "URL 校验", statuses: ["queued", "validating_url"] },
  { key: "discover", label: "定向发现", statuses: ["discovering", "discovering_pages"] },
  { key: "scope", label: "范围审核", statuses: ["awaiting_scope_review"] },
  { key: "capture", label: "逐页截图", statuses: ["capturing_pages"] },
  { key: "regions", label: "区域识别", statuses: ["analyzing_regions", "planning_storyboard"] },
  { key: "storyboard", label: "镜头审核", statuses: ["awaiting_storyboard_review"] },
  { key: "compose", label: "合成质检", statuses: ["promoting_asset", "analyzing_asset", "asset_review_required", "composing", "rendering", "quality_check", "quality_review_required", "succeeded"] },
] as const;

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function formatTime(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

function safeExternalUrl(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

function statusStage(status: string, activeStages: typeof stages | typeof siteStages): number {
  if (status === "cancelled") return -1;
  if (status === "failed") return -1;
  return activeStages.findIndex((stage) => (stage.statuses as readonly string[]).includes(status));
}

function StatusSkeleton() {
  return (
    <section className="web-video-detail-loading" aria-live="polite" aria-label="正在读取网页截图成片任务">
      <div className="loading-line"><span /></div>
      <div><i /><i /><i /></div>
      <p>正在读取截图证据、审核版本与运行状态…</p>
    </section>
  );
}

export function WebpageVideoDetail({ runId }: { runId: string }) {
  const { confirm, confirmationDialog } = useConfirmDialog();
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const loadInFlightRef = useRef(false);
  const captureFetchedAtRef = useRef(0);
  const captureRef = useRef<WebpageVideoCapture | null>(null);
  const [pageState, setPageState] = useState<PageState>("loading");
  const [run, setRun] = useState<WebpageVideoRun | null>(null);
  const [capture, setCapture] = useState<WebpageVideoCapture | null>(null);
  const [captureState, setCaptureState] = useState<CaptureState>("waiting");
  const [previewResult, setPreviewResult] = useState<PreviewResult | null>(null);
  const [pageError, setPageError] = useState("");
  const [captureError, setCaptureError] = useState("");
  const [action, setAction] = useState<Action>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [reviewConflict, setReviewConflict] = useState(false);
  const [comment, setComment] = useState("");
  const [evidenceBusy, setEvidenceBusy] = useState(false);

  const loadCapture = useCallback(async (currentRun: WebpageVideoRun, force: boolean) => {
    if (!captureStates.has(currentRun.status) && !currentRun.captureSha256 && !currentRun.capture) return;
    if (currentRun.capture?.previewUrl) {
      const embeddedCapture = {
        ...currentRun.capture,
        requestedUrl: currentRun.capture.requestedUrl || currentRun.targetUrl,
        finalUrl: currentRun.capture.finalUrl || currentRun.finalUrl,
      };
      captureRef.current = embeddedCapture;
      setCapture(embeddedCapture);
      setCaptureState("ready");
    }
    const signatureFresh = Date.now() - captureFetchedAtRef.current < 4 * 60 * 1000;
    if (!force && signatureFresh && currentRun.captureSha256 && currentRun.captureSha256 === captureRef.current?.sha256) return;
    setCaptureState((current) => current === "ready" ? current : "loading");
    const result = await adapter.getWebpageVideoCapture(runId);
    if (!result.ok) {
      if (result.error.status === 404 && currentRun.status !== "awaiting_capture_review") {
        setCaptureState("waiting");
        setCaptureError("");
      } else {
        setCaptureState("error");
        setCaptureError(result.error.status === 404
          ? "任务已进入审核状态，但截图证据尚未可读。请刷新；在证据完整前审核按钮保持关闭。"
          : result.error.message || "无法读取截图签名预览。");
      }
      return;
    }
    captureFetchedAtRef.current = Date.now();
    const fetchedCapture = {
      ...result.data,
      requestedUrl: result.data.requestedUrl || currentRun.targetUrl,
      finalUrl: result.data.finalUrl || currentRun.finalUrl,
    };
    captureRef.current = fetchedCapture;
    setCapture(fetchedCapture);
    setCaptureState("ready");
    setCaptureError("");
  }, [adapter, runId]);

  const loadRun = useCallback(async (forceCapture = false) => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    const result = await adapter.getWebpageVideoRun(runId);
    if (!result.ok) {
      loadInFlightRef.current = false;
      setPageError(result.error.status === 404 || result.error.status === 422
        ? "这个网页成片任务不存在，可能已被清理，或不属于当前创作空间。"
        : result.error.message || "无法读取网页截图成片任务。");
      setPageState((current) => current === "loading" ? "error" : current);
      return;
    }
    const next = { ...result.data, id: result.data.id || runId };
    setRun(next);
    setPageState("ready");
    setPageError("");
    if (next.capture && (!captureRef.current || next.capture.sha256 !== captureRef.current.sha256)) {
      const embeddedCapture = {
        ...next.capture,
        requestedUrl: next.capture.requestedUrl || next.targetUrl,
        finalUrl: next.capture.finalUrl || next.finalUrl,
      };
      captureRef.current = embeddedCapture;
      setCapture(embeddedCapture);
    }
    await loadCapture(next, forceCapture);
    loadInFlightRef.current = false;
  }, [adapter, loadCapture, runId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadRun(true), 0);
    return () => window.clearTimeout(timer);
  }, [loadRun]);

  useEffect(() => {
    if (!run || terminalStates.has(run.status)) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadRun(false);
    }, 3_000);
    return () => window.clearInterval(timer);
  }, [loadRun, run]);

  const currentStatus = run ? statusCopy[run.status] ?? {
    label: run.rawStatus || run.status || "未知状态",
    note: "服务端返回了新版领域状态；页面会继续轮询，但不会猜测已完成的步骤。",
  } : null;
  const activeStages = run?.siteMode ? siteStages : stages;
  const currentStage = run ? statusStage(run.status, activeStages) : -1;
  const reviewRevision = capture?.revision ?? 0;
  const reviewSha = capture?.sha256 ?? "";
  const requestedHref = safeExternalUrl(capture?.requestedUrl || run?.targetUrl);
  const finalHref = safeExternalUrl(capture?.finalUrl || run?.finalUrl);
  const capturePreviewUrl = safeBrowserMediaUrl(capture?.previewUrl);
  const finalVideoPreviewUrl = safeBrowserMediaUrl(run?.finalVideo?.previewUrl);
  const finalVideoDownloadUrl = safeBrowserMediaUrl(run?.finalVideo?.downloadUrl);
  const captionsUrl = safeBrowserMediaUrl(run?.finalVideo?.captionsUrl);
  const previewKey = capturePreviewUrl ? `${capturePreviewUrl}|${reviewRevision}|${reviewSha}` : "";
  const previewState: PreviewState = !capturePreviewUrl
    ? "idle"
    : previewResult?.key === previewKey
      ? previewResult.status
      : "loading";

  const canReview = run?.status === "awaiting_capture_review"
    && captureState === "ready"
    && Boolean(capture?.previewUrl)
    && previewState === "loaded"
    && reviewRevision > 0
    && /^[a-f0-9]{64}$/i.test(reviewSha)
    && !action;
  const canCancel = Boolean(run && !terminalStates.has(run.status) && !action);

  async function review(decision: WebpageVideoReviewRequest["decision"]) {
    if (!run || !canReview || !capture) return;
    setAction(decision);
    setActionMessage(decision === "approve" ? "正在提交批准…" : decision === "recapture" ? "正在请求重新截图…" : "正在拒绝截图…");
    setReviewConflict(false);
    const result = await adapter.reviewWebpageVideoRun(run.id, {
      decision,
      comment: comment.trim() || undefined,
      expectedRevision: capture.revision,
      expectedSha256: capture.sha256,
    }, idempotencyKey(`webpage-video-${decision}`));
    setAction(null);
    if (!result.ok) {
      if (result.error.status === 409) {
        setReviewConflict(true);
        setActionMessage("截图或审核版本已经变化。请刷新证据后再决定；当前操作没有自动重放。");
      } else {
        setActionMessage(result.error.message || "审核操作失败，请重试。");
      }
      return;
    }
    setActionMessage(decision === "approve"
      ? "已批准该精确 SHA-256 截图，正在读取后续状态。"
      : decision === "recapture"
        ? "已请求重新截图；旧截图与旧批准不会被复用。"
        : "已拒绝截图，任务将停止。"
    );
    setComment("");
    if (decision === "recapture") {
      captureRef.current = null;
      setCapture(null);
      setCaptureState("waiting");
      captureFetchedAtRef.current = 0;
    }
    await loadRun(true);
  }

  async function cancelRun() {
    if (!run || !canCancel) return;
    if (!await confirm({ title: "取消网页成片任务", description: "已经完成的截图证据会保留用于审计，但任务不会继续渲染。", confirmLabel: "取消任务", tone: "danger" })) return;
    setAction("cancel");
    setActionMessage("正在请求取消…");
    const result = await adapter.cancelWebpageVideoRun(run.id, idempotencyKey("webpage-video-cancel"));
    setAction(null);
    if (!result.ok) {
      if (result.error.status === 409) {
        setReviewConflict(true);
        setActionMessage("任务状态已变化，取消没有自动重放。请刷新后确认最新状态。");
      } else {
        setActionMessage(result.error.message || "取消失败，请刷新后重试。");
      }
      return;
    }
    setActionMessage("取消请求已接受，正在读取最终状态。");
    await loadRun(true);
  }

  async function downloadEvidenceReport() {
    if (!run || evidenceBusy) return;
    setEvidenceBusy(true);
    setActionMessage("正在汇总运行、哈希、人工门禁与试点指标…");
    let site;
    let siteEvidenceError: string | undefined;
    if (run.siteMode) {
      const result = await adapter.getWebpageVideoSite(run.id);
      if (result.ok) site = result.data;
      else siteEvidenceError = result.error.message || "Site evidence was unavailable at export time.";
    }
    const report = buildWebpageVideoEvidenceReport(run, site, new Date().toISOString(), siteEvidenceError);
    const blobUrl = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = blobUrl;
    anchor.download = webpageVideoEvidenceFilename(run.id);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(blobUrl);
    setEvidenceBusy(false);
    setActionMessage(report.completeness.complete
      ? "证据报告已下载，未包含会过期的签名链接。"
      : `证据报告已下载，并明确标注 ${report.completeness.missing.length} 项待补证据。`);
  }

  if (pageState === "loading") {
    return <div className="page page--wide web-video-detail-page"><h1 tabIndex={-1}>网页截图成片任务</h1><StatusSkeleton /></div>;
  }

  if (pageState === "error" || !run) {
    return (
      <div className="page page--wide web-video-detail-page">
        <p className="eyebrow">WEBPAGE VIDEO · RUN</p>
        <h1 tabIndex={-1}>无法读取任务</h1>
        <section className="panel web-video-fatal" role="alert">
          <span aria-hidden="true">!</span><div><h2>任务状态不可用</h2><p>{pageError || "服务端没有返回任务。"}</p></div>
          <button className="button" type="button" onClick={() => { setPageState("loading"); void loadRun(true); }}>重试读取</button>
          <Link className="button-ghost" href="/create/webpage-video">返回创建页</Link>
        </section>
      </div>
    );
  }

  return (
    <div className="page page--wide web-video-detail-page" data-run-status={run.status}>
      <header className="web-video-detail-head">
        <div>
          <p className="eyebrow">WEBPAGE VIDEO · RUN {run.id.slice(0, 8).toUpperCase()}</p>
          <h1 tabIndex={-1}>{currentStatus?.label}</h1>
          <p>{currentStatus?.note}</p>
        </div>
        <div className="web-video-detail-actions">
          <button className="button-ghost" type="button" disabled={Boolean(action)} onClick={() => void loadRun(true)}>刷新状态</button>
          <Link className="button-ghost" href="/create/webpage-video">新建任务</Link>
          {canCancel ? <button className="button-danger" type="button" disabled={Boolean(action)} onClick={() => void cancelRun()}>取消任务</button> : null}
        </div>
      </header>

      {pageError ? <div className="web-video-inline-alert" role="alert"><span>!</span><p>{pageError} 当前展示的是最后一次成功读取的状态。</p><button type="button" onClick={() => void loadRun(true)}>重试</button></div> : null}
      {actionMessage ? <div className="web-video-inline-alert" role={reviewConflict ? "alert" : "status"} data-conflict={reviewConflict || undefined}><span>{reviewConflict ? "!" : "i"}</span><p>{actionMessage}</p>{reviewConflict ? <button type="button" onClick={() => { setReviewConflict(false); setActionMessage(""); void loadRun(true); }}>刷新截图与版本</button> : null}</div> : null}
      {run.status === "quality_review_required" ? (
        <div className="web-video-inline-alert" role="status"><span>QC</span><p>最终成片质检需要人工处理；该状态绝不会重新开放截图批准按钮。</p>{run.projectRunId ? <Link href={`/projects/${encodeURIComponent(run.projectRunId)}`}>打开底层质检详情</Link> : null}</div>
      ) : null}

      <ol className="web-video-progress" aria-label="运行进度">
        {activeStages.map((stage, index) => {
          const active = currentStage === index;
          const done = run.status === "succeeded" || (currentStage > index);
          return <li key={stage.key} data-active={active || undefined} data-done={done || undefined}><span>{done ? "✓" : String(index + 1).padStart(2, "0")}</span><strong>{stage.label}</strong></li>;
        })}
      </ol>

      <div className="web-video-detail-grid">
        <main className="web-video-detail-main">
          {run.siteMode ? <WebpageVideoSitePlanPanel run={run} onChanged={() => void loadRun(true)} /> : <>
          <section className="panel web-video-capture-panel" aria-labelledby="capture-title">
            <header><div><p className="eyebrow">CAPTURE EVIDENCE</p><h2 id="capture-title">截图审核证据</h2></div><span data-state={captureState}>{captureState.toUpperCase()}</span></header>
            {captureState === "loading" || captureState === "waiting" ? (
              <div className="web-video-capture-empty" role="status"><span aria-hidden="true">⌁</span><h3>{captureState === "loading" ? "正在获取签名预览" : "截图尚未生成"}</h3><p>截图与清单完整落盘并取得内容哈希后，审核控制才会开放。</p></div>
            ) : null}
            {captureState === "error" ? (
              <div className="web-video-capture-empty" role="alert"><span aria-hidden="true">!</span><h3>截图证据不可用</h3><p>{captureError}</p><button className="button-ghost" type="button" onClick={() => void loadRun(true)}>重试读取证据</button></div>
            ) : null}
            {captureState === "ready" && capturePreviewUrl ? (
              <figure className="web-video-capture-preview">
                <Image
                  unoptimized
                  src={capturePreviewUrl}
                  alt="待审核的目标网页截图"
                  width={capture?.width ?? 1600}
                  height={capture?.height ?? 900}
                  sizes="(max-width: 1199px) 100vw, 70vw"
                  referrerPolicy="no-referrer"
                  onLoad={() => setPreviewResult({ key: previewKey, status: "loaded" })}
                  onError={() => setPreviewResult({ key: previewKey, status: "error" })}
                />
                <figcaption>{capture?.width && capture?.height ? `${capture.width} × ${capture.height} px` : "服务端签名截图预览"}</figcaption>
              </figure>
            ) : null}
            {captureState === "ready" && capturePreviewUrl && previewState === "loading" ? (
              <div className="web-video-review-disabled" role="status">截图正在加载；浏览器确认显示完成前，审核按钮保持关闭。</div>
            ) : null}
            {captureState === "ready" && capturePreviewUrl && previewState === "error" ? (
              <div className="web-video-capture-empty" role="alert"><span aria-hidden="true">!</span><h3>截图预览加载失败</h3><p>签名可能已过期或网络阻止了图片。未实际看到截图时不能批准。</p><button className="button-ghost" type="button" onClick={() => { captureFetchedAtRef.current = 0; void loadRun(true); }}>刷新签名预览</button></div>
            ) : null}
            {captureState === "ready" && !capturePreviewUrl ? (
              <div className="web-video-capture-empty" role="alert"><span aria-hidden="true">!</span><h3>缺少签名预览 URL</h3><p>页面不会在看不到精确截图时开放批准按钮。请刷新或等待服务端补齐证据。</p></div>
            ) : null}
            <dl className="web-video-evidence">
              <div><dt>Requested URL</dt><dd>{requestedHref ? <a href={requestedHref} target="_blank" rel="noreferrer noopener">{capture?.requestedUrl || run.targetUrl}</a> : capture?.requestedUrl || run.targetUrl || "—"}</dd></div>
              <div><dt>Final URL</dt><dd>{finalHref ? <a href={finalHref} target="_blank" rel="noreferrer noopener">{capture?.finalUrl || run.finalUrl}</a> : capture?.finalUrl || run.finalUrl || "等待截图"}</dd></div>
              <div><dt>SHA-256</dt><dd><code>{capture?.sha256 || run.captureSha256 || "等待截图"}</code></dd></div>
              <div><dt>Capture revision</dt><dd>{capture?.revision || "—"}</dd></div>
              <div><dt>Captured at</dt><dd>{formatTime(capture?.capturedAt)}</dd></div>
            </dl>
          </section>

          <section className="panel web-video-review-panel" aria-labelledby="review-title">
            <header><div><p className="eyebrow">HUMAN GATE</p><h2 id="review-title">截图决定</h2></div><span>{run.status === "awaiting_capture_review" ? "ACTION REQUIRED" : "LOCKED"}</span></header>
            <p>决定与当前 <strong>capture_revision</strong> 和 <strong>SHA-256</strong> 同时提交。刷新、重截或后台更新后，旧决定会以 409 拒绝，不会套用到新截图。</p>
            <label className="field" htmlFor="web-video-review-comment"><span>审核备注（可选）</span><textarea id="web-video-review-comment" className="textarea" value={comment} maxLength={1000} disabled={Boolean(action) || run.status !== "awaiting_capture_review"} placeholder="记录批准理由，或说明需要重截/拒绝的问题。" onChange={(event) => setComment(event.target.value)} /></label>
            {!canReview && run.status === "awaiting_capture_review" ? <div className="web-video-review-disabled" role="status">截图必须在本页真实加载完成，且签名预览、64 位 SHA-256 与 capture_revision 全部可用，审核按钮才会开放。</div> : null}
            <div className="web-video-review-actions">
              <button className="button" type="button" disabled={!canReview} onClick={() => void review("approve")}>批准并进入视频阶段</button>
              <button className="button-ghost" type="button" disabled={!canReview} onClick={() => void review("recapture")}>要求重新截图</button>
              <button className="button-danger" type="button" disabled={!canReview} onClick={() => void review("reject")}>拒绝并停止</button>
            </div>
          </section>
          </>}

          {run.status === "succeeded" ? (
            <section className="panel web-video-final" aria-labelledby="final-video-title">
              <header><div><p className="eyebrow">FINAL OUTPUT</p><h2 id="final-video-title">最终成片</h2></div><span>SUCCEEDED</span></header>
              {finalVideoPreviewUrl ? (
                // The server returns a separate VTT track only when subtitles were requested; other runs can be intentionally narration-only.
                // eslint-disable-next-line jsx-a11y/media-has-caption
                <video controls preload="metadata" src={finalVideoPreviewUrl}>{captionsUrl ? <track kind="captions" src={captionsUrl} srcLang="zh" label="字幕" default /> : null}浏览器不支持视频播放，请使用下载链接。</video>
              ) : <div className="web-video-capture-empty" role="status"><span aria-hidden="true">…</span><h3>成片已完成，签名预览尚未返回</h3><p>点击刷新重新获取最终视频签名地址。</p></div>}
              <div className="web-video-final-actions">
                {finalVideoDownloadUrl ? <a className="button" href={finalVideoDownloadUrl} target="_blank" rel="noreferrer noopener" referrerPolicy="no-referrer" download={run.finalVideo?.filename}>下载最终视频</a> : <button className="button-ghost" type="button" onClick={() => void loadRun(true)}>刷新成片链接</button>}
                <button className="button-ghost" type="button" disabled={evidenceBusy} onClick={() => void downloadEvidenceReport()}>{evidenceBusy ? "汇总证据中…" : run.pilotFeedback ? "下载完整申请证据" : "下载证据（待补试点数据）"}</button>
              </div>
            </section>
          ) : null}

          {run.status === "succeeded" && run.siteMode ? <WebpageVideoEvidenceChain run={run} /> : null}

          {run.status === "succeeded" ? <WebpageVideoPilotPanel key={run.pilotFeedback?.revision ?? 0} run={run} onSaved={() => void loadRun(true)} /> : null}
        </main>

        <aside className="panel web-video-run-card">
          <p className="eyebrow">RUN SUMMARY</p><h2>任务信息</h2>
          <dl>
            <div><dt>任务 ID</dt><dd><code>{run.id}</code></dd></div>
            <div><dt>采集模式</dt><dd>{run.siteMode ? "定向多页面 v2" : "单截图 v1"}</dd></div>
            <div><dt>主题</dt><dd>{run.spec.topic || "—"}</dd></div>
            <div><dt>画幅</dt><dd>{run.spec.aspectRatio || "—"}</dd></div>
            <div><dt>目标时长</dt><dd>{run.spec.durationSeconds ? `${run.spec.durationSeconds}s` : "—"}</dd></div>
            <div><dt>字幕</dt><dd>{run.spec.subtitlesEnabled ? "开启" : "关闭"}</dd></div>
            <div><dt>创建时间</dt><dd>{formatTime(run.createdAt)}</dd></div>
          </dl>
          {run.failure ? <div className="web-video-run-failure" role="alert"><strong>{run.failure.code || "RUN FAILED"}</strong><p>{run.failure.message}</p><small>{run.failure.retryable ? "可先刷新确认恢复状态；本页面不会自动新建任务。" : "请修正来源或配置后新建任务。"}</small></div> : null}
          <p className="web-video-run-note">预览和下载链接会过期；刷新状态可重新获取。</p>
          <Link className="button-ghost web-video-evidence-center-link" href="/application-evidence">打开申请证据中心</Link>
        </aside>
      </div>
      {confirmationDialog}
    </div>
  );
}
