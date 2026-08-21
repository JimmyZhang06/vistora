"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type JsonObject,
  type JsonValue,
  type Run,
  type RunArtifact,
  type RunReviewDecision,
  type RunStatus,
  type RunStep,
} from "@/lib/api";
import { Badge, StatePanel } from "@/components/page-heading";

const statusLabels: Record<RunStatus, string> = {
  queued: "排队中",
  running: "执行中",
  awaiting_review: "待人工审核",
  retrying: "准备重试",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
};

const activeStatuses = new Set<RunStatus>(["queued", "running", "awaiting_review", "retrying"]);
const terminalStatuses = new Set<RunStatus>(["succeeded", "failed", "cancelled"]);

function key(prefix: string) {
  return `${prefix}:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
}

function tone(status: RunStatus): "accent" | "success" | "warning" {
  if (status === "succeeded") return "success";
  if (status === "failed" || status === "cancelled") return "warning";
  return "accent";
}

function formatDate(value?: string) {
  if (!value) return "尚未记录";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString("zh-CN");
}

const operationOrder: Record<string, number> = {
  "research.collect": 10,
  "writing.compose": 20,
  "audio.synthesize": 30,
  "media.select": 31,
  "render.compose": 40,
  "quality.evaluate": 50,
};

const reviewDecisionLabels: Record<RunReviewDecision, string> = {
  approve: "已批准",
  revise: "已要求修改",
  reject: "已拒绝",
};

const issueOptions = [
  { code: "factuality.unsupported", label: "事实或来源不充分", operations: ["research.collect", "writing.compose", "quality.evaluate"] },
  { code: "content.structure", label: "结构或表达需要调整", operations: ["writing.compose", "quality.evaluate"] },
  { code: "audio.pronunciation", label: "发音、停顿或语速问题", operations: ["audio.synthesize", "render.compose", "quality.evaluate"] },
  { code: "visual.mismatch", label: "画面与解说不匹配", operations: ["media.select", "render.compose", "quality.evaluate"] },
  { code: "rights.unverified", label: "素材授权无法确认", operations: ["media.select", "render.compose", "quality.evaluate"] },
  { code: "render.defect", label: "字幕、画幅或渲染异常", operations: ["render.compose", "quality.evaluate"] },
  { code: "other", label: "其他问题", operations: [] },
] as const;

function orderSteps(steps: RunStep[]): RunStep[] {
  const remaining = new Map(steps.map((step) => [step.key, step]));
  const known = new Set(remaining.keys());
  const resolved = new Set<string>();
  const ordered: RunStep[] = [];
  while (remaining.size) {
    const ready = [...remaining.values()]
      .filter((step) => step.dependencies.every((dependency) => !known.has(dependency) || resolved.has(dependency)))
      .sort((left, right) => (operationOrder[left.type] ?? 999) - (operationOrder[right.type] ?? 999) || left.key.localeCompare(right.key));
    const next = ready[0] ?? [...remaining.values()].sort((left, right) => (operationOrder[left.type] ?? 999) - (operationOrder[right.type] ?? 999))[0];
    ordered.push(next);
    remaining.delete(next.key);
    resolved.add(next.key);
  }
  return ordered;
}

function record(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function displayValue(value: JsonValue): string {
  if (value === null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

const evidenceLabels: Record<string, string> = {
  angle: "创作角度",
  topic: "创作主题",
  audience: "目标受众",
  sources: "可靠来源",
  provider_protocol: "服务协议",
  width: "画面宽度",
  height: "画面高度",
  assets: "素材片段",
  provider: "处理引擎",
  frame_rate: "视频帧率",
  duration_seconds: "成片时长",
  voice: "配音音色",
  segments: "内容片段",
};

function evidenceLabel(name: string): string {
  return evidenceLabels[name] ?? name.replaceAll("_", " ");
}

function evidenceValue(name: string, value: JsonValue): string {
  const rendered = displayValue(value);
  if (name === "width" || name === "height") return `${rendered} px`;
  if (name === "assets" || name === "sources" || name === "segments") return `${rendered} 个`;
  if (name === "frame_rate") return `${rendered} fps`;
  if (name === "duration_seconds") return `${rendered} 秒`;
  return rendered;
}

function stepStatusMessage(step: RunStep): string {
  const summary = step.outputSummary;
  if (step.error) {
    const message = step.error.message.toLowerCase();
    if (message.includes("narration duration") || message.includes("duration mismatch")) {
      return "脚本长度与目标成片时长不匹配。新版会保留草稿并按本次 Run 时长重新生成；可重试此步骤，无需重建项目。";
    }
    if (message.includes("capability") || message.includes("provider")) {
      return `当前生产能力或供应商配置不可用，请检查对应 Worker 配置后重试（${step.error.code}）。`;
    }
    if (message.includes("asset") && (message.includes("coverage") || message.includes("match"))) {
      return "素材覆盖不足或语义匹配不合格。请补充标签、完成素材分析，或启用有明确授权的自动补素材来源后重试。";
    }
    return `${step.error.message}（错误码：${step.error.code}）`;
  }
  if (summary?.blocking_reason === "asset_coverage") {
    if (summary.auto_resume_pending === true) {
      return `已自动获取 ${Number(summary.acquired_assets ?? 0)} 个素材；完成安全分析和标签入库后，系统会自动重新匹配并继续制作`;
    }
    return Number(summary.acquired_assets ?? 0) > 0
      ? "候选素材分析已结束，但仍未覆盖全部场景，需要检查素材、版权状态或标签后重试"
      : `缺少 ${Array.isArray(summary.missing_scenes) ? summary.missing_scenes.length : "部分"} 个场景素材，请补充后重试`;
  }
  if (summary?.duration_fit === false) {
    if (step.type === "writing.compose") {
      const characters = typeof summary.narration_characters === "number" ? `${summary.narration_characters} 字` : "当前篇幅";
      const target = typeof summary.target_duration_seconds === "number" ? `${summary.target_duration_seconds} 秒` : "目标时长";
      return `脚本 ${characters} 未落入 ${target} 的建议篇幅，草稿已保留并进入人工审核；可审核后继续或重试改写。`;
    }
    const actual = typeof summary.duration_seconds === "number" ? `${summary.duration_seconds.toFixed(1)} 秒` : "当前实测时长";
    const target = typeof summary.target_duration_seconds === "number" ? `${summary.target_duration_seconds} 秒` : "目标时长";
    return `配音为 ${actual}，未落入 ${target} 的允许偏差，已保留产物等待人工审核。`;
  }
  if (step.review?.decision) return `${reviewDecisionLabels[step.review.decision]}${step.review.comment ? ` · ${step.review.comment}` : ""}`;
  if (step.cancellationRequestedAt) return "已收到取消请求，等待执行器抵达安全停止点";
  if (step.status === "retrying") return `计划重试：${formatDate(step.nextAttemptAt)}`;
  if (summary && Object.keys(summary).length) return `已记录 ${Object.keys(summary).length} 项输出摘要与 ${step.artifacts?.length ?? 0} 个产物`;
  return `步骤类型 ${step.type || step.key}`;
}

function JsonArtifactPreview({ artifact }: { artifact: RunArtifact }) {
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    const separator = artifact.contentUrl.includes("?") ? "&" : "?";
    fetch(`${artifact.contentUrl}${separator}inline=true`)
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((value) => { if (!cancelled) setContent(JSON.stringify(value, null, 2)); })
      .catch(() => { if (!cancelled) setError("无法读取内容，请下载原文件检查。"); });
    return () => { cancelled = true; };
  }, [artifact.contentUrl]);
  if (error) return <p className="review-artifact-error">{error}</p>;
  return <pre className="review-json-preview">{content || "正在读取内容…"}</pre>;
}

function ArtifactPreview({ artifact }: { artifact: RunArtifact }) {
  if (artifact.mediaType.startsWith("video/")) {
    // Rendered and selected media are the evidence under review.
    // eslint-disable-next-line jsx-a11y/media-has-caption
    return <video controls playsInline preload="metadata" src={artifact.contentUrl} />;
  }
  if (artifact.mediaType.startsWith("audio/")) {
    // The matching transcript is available in the script artifact.
    // eslint-disable-next-line jsx-a11y/media-has-caption
    return <audio controls preload="metadata" src={artifact.contentUrl} />;
  }
  if (artifact.mediaType.startsWith("image/")) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={artifact.contentUrl} alt={artifact.filename} loading="lazy" />;
  }
  if (artifact.mediaType === "application/json") return <JsonArtifactPreview artifact={artifact} />;
  return <p className="review-artifact-error">该格式暂不支持内嵌预览，请下载后检查。</p>;
}

export function RunDetail({ runId }: { runId: string }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [run, setRun] = useState<Run | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"cancel" | string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [reviewing, setReviewing] = useState<RunStep | null>(null);
  const [decision, setDecision] = useState<RunReviewDecision>("approve");
  const [comment, setComment] = useState("");
  const [issueCodes, setIssueCodes] = useState<string[]>([]);
  const statusRef = useRef<RunStatus | null>(null);
  const reviewDrawerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    statusRef.current = run?.status ?? null;
  }, [run?.status]);

  useEffect(() => {
    if (!reviewing || !reviewDrawerRef.current) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    reviewDrawerRef.current.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    reviewDrawerRef.current.focus({ preventScroll: true });
  }, [reviewing]);

  const refresh = useCallback(async (background = false) => {
    if (!background) setInitialLoading(true);
    const result = await adapter.getRun(runId);
    if (result.ok) {
      setRun(result.data);
      setLoadError("");
    } else {
      setLoadError(result.error.message);
    }
    if (!background) setInitialLoading(false);
    return result.ok;
  }, [adapter, runId]);

  useEffect(() => {
    let stopped = false;
    const initial = window.setTimeout(() => void refresh(), 0);
    const interval = window.setInterval(() => {
      if (!stopped && !document.hidden && (!statusRef.current || activeStatuses.has(statusRef.current))) void refresh(true);
    }, 4_000);
    return () => { stopped = true; window.clearTimeout(initial); window.clearInterval(interval); };
  }, [refresh]);

  async function cancel() {
    setBusy("cancel");
    setActionError("");
    setNotice("");
    const result = await adapter.cancelRun(runId, key("cancel-run"));
    if (result.ok) {
      setRun((current) => current ? {
        ...current,
        ...result.data,
        steps: current.steps,
        artifacts: current.artifacts,
      } : result.data);
      setNotice(result.data.cancellationRequestedAt ? "取消请求已送达，Worker 会在安全边界停止；页面将继续同步最终状态。" : `项目当前为${statusLabels[result.data.status]}，没有创建新的取消请求。`);
      setConfirmCancel(false);
      await refresh(true);
    } else {
      setActionError(result.error.message);
    }
    setBusy(null);
  }

  async function retryStep(step: RunStep) {
    setBusy(step.id);
    setActionError("");
    setNotice("");
    const result = await adapter.retryRunStep(step.id, key("retry-step"));
    if (result.ok) {
      setNotice(`「${step.label}」已重新加入执行队列。`);
      await refresh(true);
    } else {
      setActionError(result.error.message);
    }
    setBusy(null);
  }

  function openReview(step: RunStep) {
    const needsAssets = step.outputSummary?.blocking_reason === "asset_coverage";
    setReviewing(step);
    setDecision(needsAssets ? "revise" : "approve");
    setComment(needsAssets ? "已补充或重新标注素材，请重新执行场景匹配。" : "");
    setIssueCodes(needsAssets ? ["visual.mismatch"] : []);
    setActionError("");
    setNotice("");
  }

  async function submitReview() {
    if (!reviewing) return;
    if (decision !== "approve" && !comment.trim()) {
      setActionError("退回修改或拒绝时，请填写审核原因。此说明会进入执行审计记录。");
      return;
    }
    setBusy(reviewing.id);
    setActionError("");
    setNotice("");
    if (decision !== "approve" && issueCodes.length === 0) {
      setActionError("请至少选择一个问题类型，便于重试和后续审计定位。");
      return;
    }
    const result = await adapter.reviewRunStep(reviewing.id, {
      decision,
      comment: comment.trim() || undefined,
      issueCodes,
      expectedRevision: reviewing.revision,
    }, key("review-step"));
    if (result.ok) {
      setNotice("审核决定已由服务端接受，正在同步 Worker 的后续状态。");
      setReviewing(null);
      await refresh(true);
    } else {
      setActionError(result.error.message);
    }
    setBusy(null);
  }

  if (initialLoading && !run) {
    return (
      <div className="page run-detail-page" aria-busy="true">
        <h1 className="sr-only">项目执行状态</h1>
        <div className="run-detail-skeleton" aria-label="正在加载项目执行状态"><div /><div /><div /></div>
      </div>
    );
  }

  if (!run) {
    return (
      <div className="page run-detail-page">
        <StatePanel code="ERR" title="无法读取这个项目" description={loadError || "项目状态接口暂时不可用。"} error>
          <button className="button-secondary" type="button" onClick={() => void refresh()}>重新连接</button>
          <Link className="button-ghost" href="/projects">返回项目列表</Link>
        </StatePanel>
      </div>
    );
  }

  const completed = run.steps.filter((step) => step.status === "succeeded").length;
  const canCancel = !terminalStatuses.has(run.status) && !run.cancellationRequestedAt;
  const finalVideo = run.artifacts.find((artifact) => artifact.kind === "video" && artifact.mediaType === "video/mp4");
  const audio = run.artifacts.find((artifact) => artifact.kind === "audio");
  const selectedAssets = run.artifacts.filter((artifact) => artifact.kind === "asset").length;
  const orderedSteps = orderSteps(run.steps);
  const reviewingArtifacts = reviewing?.artifacts ?? [];
  const inputEntries = Object.entries(reviewing?.inputSnapshot ?? {})
    .filter(([name]) => !name.startsWith("_"));
  const summaryEntries = Object.entries(reviewing?.outputSummary ?? {});
  const framefactory = record(reviewing?.inputSnapshot?._framefactory);
  const skillVersion = record(framefactory.skill_version);
  const qcPolicy = record(skillVersion.qc_policy);
  const qcRules = Array.isArray(qcPolicy.rules)
    ? qcPolicy.rules.map((value) => record(value)).filter((value) => Object.keys(value).length)
    : [];
  const reviewableNow = Boolean(reviewing?.reviewRequired && reviewing.status === "awaiting_review");
  const assetCoverageBlocked = reviewing?.outputSummary?.blocking_reason === "asset_coverage";
  const acquiredAssets = Number(reviewing?.outputSummary?.acquired_assets ?? 0);
  const availableIssues = issueOptions.filter((option) =>
    option.operations.length === 0
    || (option.operations as readonly string[]).includes(reviewing?.type ?? ""));

  return (
    <div className="page page--wide run-detail-page">
      <header className="run-detail-header">
        <div>
          <Link className="run-detail-back" href="/projects">← 返回项目</Link>
          <p className="eyebrow">RUN / {run.id.slice(0, 8)}</p>
          <h1>{run.topic}</h1>
          <p>真实执行状态由控制 API 与 Worker 持久化记录同步；离开页面不会中断任务。</p>
        </div>
        <div className="run-detail-actions">
          <Badge tone={tone(run.status)}>{statusLabels[run.status]}</Badge>
          {canCancel && !confirmCancel ? <button className="button-ghost" type="button" onClick={() => setConfirmCancel(true)}>取消执行</button> : null}
          {canCancel && confirmCancel ? (
            <div className="cancel-confirm" role="group" aria-label="确认取消项目">
              <span>确认请求安全停止？</span>
              <button className="button-ghost button-small" type="button" onClick={() => setConfirmCancel(false)} disabled={busy === "cancel"}>保留执行</button>
              <button className="button-secondary button-small" type="button" onClick={() => void cancel()} disabled={busy === "cancel"}>{busy === "cancel" ? "正在请求…" : "确认取消"}</button>
            </div>
          ) : null}
          {run.cancellationRequestedAt ? <span className="run-cancel-pending">取消处理中</span> : null}
        </div>
      </header>

      <div className="run-live-region" aria-live="polite" aria-atomic="true">
        {loadError ? <p className="alert alert--error">状态刷新失败，当前保留上次成功读取的数据：{loadError}</p> : null}
        {actionError ? <p className="alert alert--error">{actionError}</p> : null}
        {notice ? <p className="alert alert--success">{notice}</p> : null}
      </div>

      <section className="run-overview" aria-label="项目执行概览">
        <article><span>总体状态</span><strong>{statusLabels[run.status]}</strong><small>{activeStatuses.has(run.status) ? "每 4 秒自动同步" : "已进入终态"}</small></article>
        <article><span>步骤进度</span><strong>{completed} / {run.steps.length || "—"}</strong><small>{run.steps.length ? "以 Worker 步骤记录为准" : "Worker 尚未物化步骤"}</small></article>
        <article><span>最近更新</span><strong>{formatDate(run.updatedAt)}</strong><small>创建于 {formatDate(run.createdAt)}</small></article>
        <article><span>执行快照</span><strong>{run.composition.skillVersionId.slice(0, 8)}</strong><small>Pipeline {run.composition.pipelineVersionId.slice(0, 8)}</small></article>
      </section>

      {finalVideo ? (
        <section id="final-video" className="run-output" aria-labelledby="run-output-title">
          <div className="run-output-stage">
            {/* Subtitles are burned into the rendered MP4 by the production renderer. */}
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <video
              controls
              playsInline
              preload="metadata"
              src={finalVideo.contentUrl}
              aria-label="最终成片预览"
            />
          </div>
          <div className="run-output-copy">
            <p className="eyebrow">DELIVERABLE</p>
            <h2 id="run-output-title">最终成片</h2>
            <p>由真实素材、旁白和渲染流程生成。下载地址为短期签名链接，原始对象保持私有。</p>
            <dl>
              <div><dt>格式</dt><dd>{finalVideo.mediaType}</dd></div>
              <div><dt>文件大小</dt><dd>{(finalVideo.byteSize / 1024 / 1024).toFixed(1)} MB</dd></div>
              <div><dt>素材</dt><dd>{selectedAssets} 个已选片段</dd></div>
              <div><dt>配音</dt><dd>{audio ? "已合成并混入" : "未发现音频制品"}</dd></div>
            </dl>
            <a className="button-secondary" href={finalVideo.contentUrl} download={finalVideo.filename}>下载 MP4</a>
          </div>
        </section>
      ) : run.status === "succeeded" ? (
        <StatePanel code="MEDIA" title="成片记录尚未出现" description="执行已完成，但没有可播放的视频制品。请检查渲染步骤输出与对象存储。" error />
      ) : null}

      <div className="run-detail-grid">
        <section className="run-timeline" aria-labelledby="run-steps-title">
          <div className="section-heading">
            <div><p className="eyebrow">EXECUTION</p><h2 id="run-steps-title">生产步骤</h2></div>
            <button className="button-ghost button-small" type="button" onClick={() => void refresh(true)} disabled={busy !== null}>立即刷新</button>
          </div>

          {run.steps.length ? (
            <ol>
              {orderedSteps.map((step, index) => {
                const reviewable = step.status === "awaiting_review" && step.reviewRequired;
                const retryable = step.status === "failed" && step.attemptCount < 20;
                return (
                  <li className={`run-step run-step--${step.status}`} key={step.id}>
                    <div className="run-step-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
                    <div className="run-step-copy">
                      <div><h3>{step.label}</h3><Badge tone={tone(step.status)}>{statusLabels[step.status]}</Badge></div>
                      <p>{stepStatusMessage(step)}</p>
                      <small>尝试 {step.attemptCount} / {step.maxAttempts}{step.queueName ? ` · 队列 ${step.queueName}` : ""} · 更新于 {formatDate(step.updatedAt)}</small>
                    </div>
                    <div className="run-step-action">
                      <button className={reviewable ? "button-secondary button-small" : "button-ghost button-small"} type="button" onClick={() => openReview(step)} disabled={busy !== null}>{reviewable ? "审核产物" : "查看详情"}</button>
                      {retryable ? <button className="button-secondary button-small" type="button" onClick={() => void retryStep(step)} disabled={busy !== null}>{busy === step.id ? "正在重试…" : "重试此步骤"}</button> : null}
                      {step.review?.decidedAt ? <time dateTime={step.review.decidedAt}>审核于 {formatDate(step.review.decidedAt)}</time> : null}
                    </div>
                  </li>
                );
              })}
            </ol>
          ) : (
            <div className="run-steps-empty">
              <span>WAIT</span>
              <h3>Worker 尚未创建步骤记录</h3>
              <p>项目已持久化，但执行器可能仍在接单或暂时不可用。页面会继续查询真实状态，不会展示模拟进度。</p>
            </div>
          )}
        </section>

        <aside className="run-audit panel" aria-label="执行信息">
          <p className="eyebrow">TRACE</p>
          <h2>执行信息</h2>
          <dl>
            <div><dt>Run ID</dt><dd>{run.id}</dd></div>
            <div><dt>工作区</dt><dd>{run.workspaceId}</dd></div>
            <div><dt>创建者</dt><dd>{run.createdBy}</dd></div>
            <div><dt>频道</dt><dd>{run.channelId || "未绑定"}</dd></div>
            <div><dt>SkillVersion</dt><dd>{run.composition.skillVersionId}</dd></div>
            <div><dt>PipelineVersion</dt><dd>{run.composition.pipelineVersionId}</dd></div>
            <div><dt>取消请求</dt><dd>{formatDate(run.cancellationRequestedAt)}</dd></div>
          </dl>
        </aside>
      </div>

      {reviewing ? (
        <section ref={reviewDrawerRef} className="review-drawer" aria-labelledby="review-title" tabIndex={-1}>
          <header className="review-drawer-header">
            <div className="review-drawer-copy">
              <p className="eyebrow">REVIEW EVIDENCE · REV {reviewing.revision ?? "—"}</p>
              <h2 id="review-title">{reviewableNow ? "审核" : "查看"}「{reviewing.label}」</h2>
              <p>{reviewableNow ? "请先检查输入、自动检查规则和实际产物，再提交决定。服务端会将当前步骤版本与产物哈希写入审计记录。" : "这里保留该步骤的输入、输出摘要、产物和历史审核决定，便于回溯当时依据。"}</p>
            </div>
            <div className="review-drawer-side">
              <dl className="review-drawer-meta">
                <div><dt>步骤状态</dt><dd>{statusLabels[reviewing.status]}</dd></div>
                <div><dt>步骤类型</dt><dd>{reviewing.type || reviewing.key}</dd></div>
                <div><dt>执行尝试</dt><dd>{reviewing.attemptCount} / {reviewing.maxAttempts}</dd></div>
              </dl>
              <button className="button-ghost button-small" type="button" onClick={() => setReviewing(null)} disabled={busy === reviewing.id}>关闭详情</button>
            </div>
          </header>

          <div className="review-evidence">
            <section>
              <div className="review-section-heading"><span>01</span><h3>本步输入</h3></div>
              {inputEntries.length ? <dl>{inputEntries.map(([name, value]) => <div key={name}><dt>{evidenceLabel(name)}</dt><dd>{evidenceValue(name, value)}</dd></div>)}</dl> : <p>没有额外业务输入。</p>}
            </section>
            <section>
              <div className="review-section-heading"><span>02</span><h3>输出摘要</h3></div>
              {summaryEntries.length ? <dl>{summaryEntries.map(([name, value]) => <div key={name}><dt>{evidenceLabel(name)}</dt><dd>{evidenceValue(name, value)}</dd></div>)}</dl> : <p>执行器没有提供结构化摘要，请直接检查产物。</p>}
            </section>
            {qcRules.length ? <section className="review-policy"><div className="review-section-heading"><span>03</span><h3>本次适用的检查规则</h3></div><ul>{qcRules.map((rule, index) => <li key={String(rule.id ?? index)}><span className="review-policy-index">{String(index + 1).padStart(2, "0")}</span><div><strong>{String(rule.description ?? rule.id ?? "检查规则")}</strong><small>{String(rule.category ?? "general")} · {String(rule.severity ?? "info")}</small></div><span className="review-policy-action">{String(rule.action ?? "review") === "fail" ? "必须拦截" : "需要复核"}</span></li>)}</ul></section> : null}
          </div>

          <section className="review-artifacts" aria-label="步骤产物">
            <div className="review-artifacts-heading"><div className="review-section-heading"><span>04</span><h3>实际产物</h3></div><span>{reviewingArtifacts.length} 个文件</span></div>
            {finalVideo && !reviewingArtifacts.some((artifact) => artifact.kind === "video") ? <div className="review-artifact-context"><div><strong>当前展示的是「{reviewing.label}」步骤产物</strong><p>这里的 JSON、音频或素材是中间文件，不是最终成片。</p></div><a className="button-secondary button-small" href="#final-video" onClick={() => setReviewing(null)}>查看最终成片</a></div> : null}
            {reviewingArtifacts.length ? <div className={`review-artifact-grid${reviewingArtifacts.length === 1 ? " review-artifact-grid--single" : ""}`}>{reviewingArtifacts.slice(0, 12).map((artifact) => <article key={artifact.id}><header><div><strong>{artifact.kind}</strong><span>{artifact.filename}</span></div><a className="button-ghost button-small" href={artifact.contentUrl} download={artifact.filename}>下载原文件</a></header><ArtifactPreview artifact={artifact} /><footer><span>{(artifact.byteSize / 1024).toFixed(1)} KB</span><code>SHA {artifact.contentHash.slice(0, 12)}</code></footer></article>)}</div> : <p className="review-empty-evidence">这个步骤没有产生可预览文件，请根据输出摘要和执行记录判断。</p>}
            {reviewingArtifacts.length > 12 ? <p className="review-empty-evidence">当前展示前 12 个产物，其余 {reviewingArtifacts.length - 12} 个可从项目制品列表访问。</p> : null}
          </section>

          {reviewing.review?.decision ? <section className="review-history"><div className="review-section-heading"><span>05</span><h3>审核记录</h3></div><p><strong>{reviewDecisionLabels[reviewing.review.decision]}</strong> · {formatDate(reviewing.review.decidedAt)}</p>{reviewing.review.issueCodes?.length ? <p>问题分类：{reviewing.review.issueCodes.join("、")}</p> : null}{reviewing.review.comment ? <blockquote>{reviewing.review.comment}</blockquote> : null}<small>审核版本 {reviewing.review.reviewedRevision ?? "—"} · 审核人 {reviewing.review.actorId ?? "系统记录"}</small></section> : null}

          {assetCoverageBlocked ? <div className={`alert${acquiredAssets > 0 ? "" : " alert--error"}`}><strong>{acquiredAssets > 0 ? "素材已自动获取，等待分析" : "需要补充可匹配素材"}</strong><p>{acquiredAssets > 0 ? `已自动入库 ${acquiredAssets} 个候选素材。完成安全分析和标签后可退回本步重新匹配。` : "当前步骤没有产生素材清单，不能直接批准进入渲染。请先导入或重新标注相关画面，再选择“退回本步修改”。"}</p><Link className="button-secondary button-small" href="/assets">查看素材库</Link></div> : null}

          {reviewableNow ? <div className="review-decision-panel">
            <fieldset className="review-options">
              <legend>审核决定</legend>
              <label htmlFor="review-decision-approve"><input id="review-decision-approve" type="radio" name="review-decision" value="approve" checked={decision === "approve"} disabled={assetCoverageBlocked} onChange={() => { setDecision("approve"); setIssueCodes([]); }} />批准并继续<small>{assetCoverageBlocked ? "缺少素材清单，当前不可批准" : "接受当前版本与产物"}</small></label>
              <label htmlFor="review-decision-revise"><input id="review-decision-revise" type="radio" name="review-decision" value="revise" checked={decision === "revise"} onChange={() => setDecision("revise")} />退回本步修改<small>保留旧产物并创建新尝试</small></label>
              <label htmlFor="review-decision-reject"><input id="review-decision-reject" type="radio" name="review-decision" value="reject" checked={decision === "reject"} onChange={() => setDecision("reject")} />拒绝并终止<small>将当前 Run 标记为失败</small></label>
            </fieldset>
            {decision !== "approve" ? <fieldset className="review-issues"><legend>发现的问题（至少选择一项）</legend>{availableIssues.map((option) => <label key={option.code}><input type="checkbox" checked={issueCodes.includes(option.code)} onChange={(event) => setIssueCodes((current) => event.target.checked ? [...current, option.code] : current.filter((value) => value !== option.code))} />{option.label}</label>)}</fieldset> : null}
            <div className="field"><label htmlFor="review-comment">审核说明{decision === "approve" ? "（可选）" : "（必填）"}</label><textarea id="review-comment" className="textarea" value={comment} onChange={(event) => setComment(event.target.value)} maxLength={1000} placeholder="指出具体段落、素材或时间点，并说明如何调整" /><span className="field-help">{comment.length} / 1000，决定将绑定步骤版本 {reviewing.revision ?? "—"}。</span></div>
          </div>
          : null}
          <div className="button-row review-drawer-actions">
            <button className="button-ghost" type="button" onClick={() => setReviewing(null)} disabled={busy === reviewing.id}>关闭</button>
            {reviewableNow ? <button className="button" type="button" onClick={() => void submitReview()} disabled={busy === reviewing.id}>{busy === reviewing.id ? "正在提交…" : "提交审核决定"}</button> : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}
