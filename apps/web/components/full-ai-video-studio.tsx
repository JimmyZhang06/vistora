"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ApiProblem,
  type FullAiEstimate,
  type FullAiOptions,
  type FullAiRun,
  type FullAiSpec,
} from "@/lib/api";
import {
  type FullAiSubmissionAttempt,
  readFullAiSubmissionAttempt,
  saveFullAiSubmissionAttempt,
  shouldRetainFullAiAttempt,
} from "@/lib/full-ai-submission";

const storyStarters = [
  "一座漂浮在云海上的未来城市，清晨开始苏醒",
  "用电影化微距讲述一颗咖啡豆的完整旅程",
  "在陌生星球上发现一片会发光的森林",
] as const;

const directionPresentation: Record<string, { label: string; note: string; art: number }> = {
  cinematic: { label: "电影叙事", note: "写实光影 · 稳定运镜 · 连贯场景", art: 1 },
  graphic: { label: "视觉设计", note: "大胆构图 · 图形转场 · 品牌感", art: 2 },
  illustrated: { label: "风格插画", note: "统一画风 · 柔和运动 · 非写实", art: 3 },
};

const pipelineStages = [
  ["01", "创意脚本", "Creative brief + structured Beats"],
  ["02", "旁白定时", "Native timing artifact"],
  ["03", "镜头提示", "Prompt continuity pack"],
  ["04", "逐镜头生成", "Generated-only candidates"],
  ["05", "下载与验证", "Run-scoped hash + probe + safety"],
  ["06", "剪辑与质检", "Selection + EDL + final QC"],
] as const;

export type FullAiStudioState =
  | "loading"
  | "unavailable"
  | "editing"
  | "estimating"
  | "ready"
  | "blocked"
  | "submitting"
  | "submit_unknown"
  | "created"
  | "failed";

function readPendingAttempt(): FullAiSubmissionAttempt | null {
  try {
    return readFullAiSubmissionAttempt(window.localStorage);
  } catch {
    return null;
  }
}

function savePendingAttempt(attempt: FullAiSubmissionAttempt | null) {
  try {
    saveFullAiSubmissionAttempt(window.localStorage, attempt);
  } catch {
    // A private browser context can disable storage. The in-memory key remains fixed.
  }
}

function directionCopy(direction: string) {
  return directionPresentation[direction] ?? {
    label: direction || "未选择",
    note: "由生成服务提供的视觉方向",
    art: 1,
  };
}

function formatMoney(currency: string, amountMinor: number): string {
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    }).format(amountMinor / 100);
  } catch {
    return `${currency} ${(amountMinor / 100).toFixed(2)}`;
  }
}

function isAmbiguousSubmission(problem: ApiProblem): boolean {
  if (problem.code === "FULL_AI_PROVIDER_UNAVAILABLE") return false;
  if (problem.code === "FULL_AI_ESTIMATE_STALE") return false;
  if (problem.code === "FULL_AI_BUDGET_EXCEEDED") return false;
  if (problem.code === "IDEMPOTENCY_KEY_REUSED") return false;
  return problem.code === "NETWORK_ERROR" || Boolean(problem.retryable);
}

function problemFullAiRunId(problem: ApiProblem): string | undefined {
  const value = problem.details?.full_ai_run_id;
  return typeof value === "string" && value ? value : undefined;
}

export function FullAiVideoStudio() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [studioState, setStudioState] = useState<FullAiStudioState>("loading");
  const [options, setOptions] = useState<FullAiOptions | null>(null);
  const [estimate, setEstimate] = useState<FullAiEstimate | null>(null);
  const [createdRun, setCreatedRun] = useState<FullAiRun | null>(null);
  const [message, setMessage] = useState("正在读取生成服务的模型、限制与可用状态…");
  const [briefError, setBriefError] = useState("");
  const [brief, setBrief] = useState("");
  const [direction, setDirection] = useState("");
  const [aspectRatio, setAspectRatio] = useState("");
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [variants, setVariants] = useState(0);
  const [continuity, setContinuity] = useState(true);
  const [disclosure, setDisclosure] = useState(true);
  const submissionRef = useRef<FullAiSubmissionAttempt | null>(null);

  const providerReady = options?.status === "ready" && options.provider.status === "ready";
  const formFrozen = studioState === "estimating"
    || studioState === "submitting"
    || studioState === "submit_unknown"
    || studioState === "created";
  const previewSceneCount = options?.limits.clipSeconds && durationSeconds
    ? Math.ceil(durationSeconds / options.limits.clipSeconds)
    : 0;
  const previewCandidateCount = previewSceneCount * variants;
  const selectedDirection = directionCopy(direction);

  const currentSpec = useMemo<FullAiSpec>(() => ({
    brief: brief.trim(),
    direction,
    aspectRatio,
    durationSeconds,
    variantsPerScene: variants,
    continuity,
    aiDisclosure: disclosure,
  }), [aspectRatio, brief, continuity, direction, disclosure, durationSeconds, variants]);

  const selectionValid = Boolean(
    options
    && currentSpec.brief
    && currentSpec.brief.length <= options.limits.briefMaxLength
    && options.limits.directions.includes(direction)
    && options.limits.aspectRatios.includes(aspectRatio)
    && options.limits.durationSeconds.includes(durationSeconds)
    && options.limits.variantsPerScene.includes(variants)
    && disclosure,
  );

  const loadOptions = useCallback(async () => {
    const pendingAttempt = readPendingAttempt();
    setStudioState("loading");
    setMessage("正在读取生成服务的模型、限制与可用状态…");
    setEstimate(null);
    setCreatedRun(null);
    submissionRef.current = pendingAttempt;
    const result = await adapter.getFullAiOptions();
    if (!result.ok) {
      setOptions(null);
      setStudioState(pendingAttempt ? "submit_unknown" : "unavailable");
      setMessage(pendingAttempt
        ? "发现尚未确认的创建请求。原幂等键已保留；服务恢复后只能核对这一请求。"
        : result.error.message || "无法读取全 AI 生成服务。请确认 Control API 已启动。");
      return;
    }

    const next = result.data;
    setOptions(next);
    setDirection((current) => next.limits.directions.includes(current) ? current : next.limits.directions[0] ?? "");
    setAspectRatio((current) => next.limits.aspectRatios.includes(current) ? current : next.limits.aspectRatios[0] ?? "");
    setDurationSeconds((current) => next.limits.durationSeconds.includes(current) ? current : next.limits.durationSeconds[0] ?? 0);
    setVariants((current) => next.limits.variantsPerScene.includes(current) ? current : next.limits.variantsPerScene[0] ?? 0);
    if (pendingAttempt) {
      const pending = pendingAttempt.request;
      setBrief(pending.brief);
      setDirection(pending.direction);
      setAspectRatio(pending.aspectRatio);
      setDurationSeconds(pending.durationSeconds);
      setVariants(pending.variantsPerScene);
      setContinuity(pending.continuity);
      setDisclosure(pending.aiDisclosure);
      if (pendingAttempt.fullAiRunId) {
        const runResult = await adapter.getFullAiRun(pendingAttempt.fullAiRunId);
        if (runResult.ok) {
          setCreatedRun(runResult.data);
          if (runResult.data.status === "reconciliation_required" || runResult.data.billing.requiresReconciliation) {
            setStudioState("submit_unknown");
            setMessage("已恢复需人工核账的已知任务；只会查询它的状态，不会重新下单。");
            return;
          }
          if (shouldRetainFullAiAttempt(runResult.data)) {
            const knownAttempt = { ...pendingAttempt, fullAiRunId: runResult.data.id };
            submissionRef.current = knownAttempt;
            savePendingAttempt(knownAttempt);
            setStudioState("created");
            setMessage(`已恢复全 AI 任务，当前状态为 ${runResult.data.status}；可手动检查最新状态。`);
            return;
          } else {
            savePendingAttempt(null);
            submissionRef.current = null;
            setStudioState("created");
            setMessage(`已恢复并确认此前任务已进入终态：${runResult.data.status}。`);
            return;
          }
        }
      }
      setStudioState("submit_unknown");
      setMessage(pendingAttempt.fullAiRunId
        ? "已恢复需核账的已知任务；只会查询它的状态，不会重新下单。"
        : "已恢复结果未知的创建请求。确认时只会复用原请求和原幂等键，不会创建新订单标识。");
      return;
    }
    if (next.status === "ready" && next.provider.status === "ready") {
      setStudioState("editing");
      setMessage("生成服务已就绪。填写创意后，由服务端计算镜头计划和最终报价。");
    } else {
      setStudioState("unavailable");
      setMessage(next.blockers[0]?.message || "生成 Provider 尚未完成配置。");
    }
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadOptions(), 0);
    return () => window.clearTimeout(timer);
  }, [loadOptions]);

  useEffect(() => {
    if (studioState !== "ready" || !estimate?.quote?.expiresAt) return;
    const expiresIn = Date.parse(estimate.quote.expiresAt) - Date.now();
    const timer = window.setTimeout(() => {
      setStudioState("editing");
      setMessage("服务端报价已过期，请重新估算后再创建任务。");
      submissionRef.current = null;
    }, Number.isFinite(expiresIn) ? Math.max(0, Math.min(expiresIn, 2_147_000_000)) : 0);
    return () => window.clearTimeout(timer);
  }, [estimate, studioState]);

  function invalidateEstimate() {
    setEstimate(null);
    setCreatedRun(null);
    setBriefError("");
    submissionRef.current = null;
    savePendingAttempt(null);
    if (providerReady) {
      setStudioState("editing");
      setMessage("配置已改变，请获取新的服务端报价。");
    } else {
      setStudioState("unavailable");
    }
  }

  async function requestEstimate() {
    if (!options || !providerReady) return;
    if (!currentSpec.brief) {
      setBriefError("请先描述想生成的影片。");
      document.querySelector<HTMLTextAreaElement>("#ai-video-brief")?.focus();
      return;
    }
    if (!selectionValid) {
      setMessage("当前规格不在服务端允许范围内，请重新选择。");
      return;
    }

    setBriefError("");
    setStudioState("estimating");
    setMessage("服务端正在计算镜头数、计费生成量与有效报价…");
    submissionRef.current = null;
    savePendingAttempt(null);
    const result = await adapter.estimateFullAiRun(currentSpec);
    if (!result.ok) {
      setEstimate(null);
      setStudioState("failed");
      setMessage(result.error.message || "服务端估算失败，请稍后重试。");
      return;
    }

    setEstimate(result.data);
    if (
      result.data.status === "ready"
      && result.data.quote
      && result.data.requestFingerprint
      && result.data.blockers.length === 0
    ) {
      setStudioState("ready");
      setMessage("服务端报价已锁定。创建前请核对价格和生成规模。");
      return;
    }
    setStudioState("blocked");
    setMessage(result.data.blockers[0]?.message || "服务端预检未通过，当前不能创建任务。");
  }

  async function submitRun() {
    if (studioState !== "ready" || !estimate?.quote || !estimate.requestFingerprint) return;
    if (Date.parse(estimate.quote.expiresAt) <= Date.now()) {
      setStudioState("editing");
      setMessage("服务端报价已过期，请重新估算后再创建任务。");
      return;
    }

    const attempt = submissionRef.current ?? {
      idempotencyKey: `full-ai-create:${globalThis.crypto.randomUUID()}`,
      request: {
        ...currentSpec,
        estimateFingerprint: estimate.requestFingerprint,
        maxCostMinor: estimate.quote.amountMinor,
        currency: estimate.quote.currency,
      },
    };
    submissionRef.current = attempt;
    savePendingAttempt(attempt);
    setStudioState("submitting");
    setMessage("正在原子创建任务并锁定付费上限，请勿重复提交…");
    const result = await adapter.createFullAiRun(attempt.request, attempt.idempotencyKey);
    if (!result.ok) {
      if (isAmbiguousSubmission(result.error)) {
        const knownRunId = problemFullAiRunId(result.error);
        if (knownRunId) {
          const knownAttempt = { ...attempt, fullAiRunId: knownRunId };
          submissionRef.current = knownAttempt;
          savePendingAttempt(knownAttempt);
        }
        setStudioState("submit_unknown");
        setMessage(knownRunId
          ? "服务端已保存任务 ID，但后续状态未知；只会 GET 查询该任务，不会再次发送创建请求。"
          : "提交结果未知。原请求和幂等键已保存；系统不会自动重试，只有你确认后才会复用同一请求核对。"
        );
      } else {
        savePendingAttempt(null);
        submissionRef.current = null;
        setStudioState("failed");
        setMessage(result.error.message || "任务创建失败，未产生新的生成订单。");
      }
      return;
    }

    setCreatedRun(result.data);
    const knownAttempt = { ...attempt, fullAiRunId: result.data.id };
    if (result.data.status === "reconciliation_required" || result.data.billing.requiresReconciliation) {
      submissionRef.current = knownAttempt;
      savePendingAttempt(knownAttempt);
      setStudioState("submit_unknown");
      setMessage("Provider 提交状态需要核账。系统已停止自动重试；仅可查询这个已知任务，不会再次下单。");
      return;
    }
    if (shouldRetainFullAiAttempt(result.data)) {
      submissionRef.current = knownAttempt;
      savePendingAttempt(knownAttempt);
    } else {
      submissionRef.current = null;
      savePendingAttempt(null);
    }
    setStudioState("created");
    setMessage(shouldRetainFullAiAttempt(result.data)
      ? `全 AI 任务已创建，当前状态为 ${result.data.status}；任务标识会保留到执行终态。`
      : `全 AI 任务已创建并进入终态：${result.data.status}。`);
  }

  async function reconcileKnownRun() {
    const runId = createdRun?.id ?? submissionRef.current?.fullAiRunId;
    if (!runId) return;
    setStudioState("submitting");
    setMessage("正在查询已知任务状态；不会重新发送创建请求…");
    const result = await adapter.getFullAiRun(runId);
    if (!result.ok) {
      setStudioState("submit_unknown");
      setMessage("暂时无法读取已知任务，已停止自动重试，请继续人工核账。");
      return;
    }
    setCreatedRun(result.data);
    if (result.data.status === "reconciliation_required" || result.data.billing.requiresReconciliation) {
      setStudioState("submit_unknown");
      setMessage("任务仍需人工核账；系统不会重复下单。");
      return;
    }
    if (shouldRetainFullAiAttempt(result.data)) {
      const attempt = submissionRef.current;
      if (attempt) {
        const knownAttempt = { ...attempt, fullAiRunId: result.data.id };
        submissionRef.current = knownAttempt;
        savePendingAttempt(knownAttempt);
      }
    } else {
      savePendingAttempt(null);
      submissionRef.current = null;
    }
    setStudioState("created");
    setMessage(shouldRetainFullAiAttempt(result.data)
      ? `已确认已知任务仍在执行，当前状态为 ${result.data.status}；未发送创建请求。`
      : `已知任务已进入终态：${result.data.status}。`);
  }

  async function retryUnknownCreate() {
    const attempt = submissionRef.current;
    if (!attempt || attempt.fullAiRunId) return;
    setStudioState("submitting");
    setMessage("正在复用已保存的原请求与原幂等键核对结果；不会生成新的订单标识…");
    const result = await adapter.createFullAiRun(attempt.request, attempt.idempotencyKey);
    if (!result.ok) {
      if (isAmbiguousSubmission(result.error)) {
        const knownRunId = problemFullAiRunId(result.error);
        if (knownRunId) {
          const knownAttempt = { ...attempt, fullAiRunId: knownRunId };
          submissionRef.current = knownAttempt;
          savePendingAttempt(knownAttempt);
        }
        setStudioState("submit_unknown");
        setMessage(knownRunId
          ? "已取得服务端任务 ID；后续只会 GET 查询，不会再次发送创建请求。"
          : "结果仍然未知。原请求与幂等键继续保留，系统不会自动重试。"
        );
      } else {
        savePendingAttempt(null);
        submissionRef.current = null;
        setStudioState("failed");
        setMessage(result.error.message || "服务端已确认该创建请求失败。");
      }
      return;
    }
    setCreatedRun(result.data);
    const knownAttempt = { ...attempt, fullAiRunId: result.data.id };
    if (result.data.status === "reconciliation_required" || result.data.billing.requiresReconciliation) {
      submissionRef.current = knownAttempt;
      savePendingAttempt(knownAttempt);
      setStudioState("submit_unknown");
      setMessage("已找到对应任务，但 Provider 状态仍需人工核账；后续只会 GET 查询。");
      return;
    }
    if (shouldRetainFullAiAttempt(result.data)) {
      submissionRef.current = knownAttempt;
      savePendingAttempt(knownAttempt);
    } else {
      submissionRef.current = null;
      savePendingAttempt(null);
    }
    setStudioState("created");
    setMessage(shouldRetainFullAiAttempt(result.data)
      ? `已用原幂等键确认任务，当前状态为 ${result.data.status}；后续只会 GET 查询。`
      : `已用原幂等键确认任务已进入终态：${result.data.status}。`);
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (studioState === "ready") void submitRun();
    else if (studioState === "submit_unknown") {
      if (createdRun?.id || submissionRef.current?.fullAiRunId) void reconcileKnownRun();
      else void retryUnknownCreate();
    }
    else void requestEstimate();
  }

  const finalPlan = estimate?.plan;
  const displayedBlockers = estimate?.blockers.length ? estimate.blockers : options?.blockers ?? [];
  const statusLabel: Record<FullAiStudioState, string> = {
    loading: "LOADING",
    unavailable: "UNAVAILABLE",
    editing: "EDITING",
    estimating: "ESTIMATING",
    ready: "QUOTE READY",
    blocked: "BLOCKED",
    submitting: "SUBMITTING",
    submit_unknown: "MANUAL CHECK",
    created: "CREATED",
    failed: "FAILED",
  };
  const submitDisabled = studioState === "loading"
    || studioState === "unavailable"
    || studioState === "estimating"
    || studioState === "submitting"
    || studioState === "created"
    || (!["ready", "submit_unknown"].includes(studioState) && !selectionValid);
  const submitLabel = studioState === "ready"
    ? "按服务端报价创建任务"
    : studioState === "loading"
      ? "正在读取服务状态"
    : studioState === "estimating"
      ? "正在获取服务端报价"
      : studioState === "submitting"
        ? "正在处理已知请求"
        : studioState === "submit_unknown"
          ? "核对原请求状态"
          : studioState === "created"
            ? "任务已经创建"
            : studioState === "unavailable"
              ? "生成 Provider 不可用"
              : "获取服务端报价";
  const gateTitle = studioState === "loading"
    ? "正在读取生成服务"
    : studioState === "submit_unknown"
      ? "已停止自动重试，需人工核账"
      : studioState === "created"
        ? "任务创建成功"
        : providerReady
          ? "独立生成服务"
          : "生成 Provider 不可用";

  return (
    <div className="page page--wide ai-studio-page" data-studio-state={studioState}>
      <header className="ai-studio-hero" aria-labelledby="ai-studio-title">
        <div className="ai-studio-hero-copy">
          <p className="eyebrow">AI STUDIO · GENERATED ONLY</p>
          <h1 id="ai-studio-title" tabIndex={-1}>从一句话，生成一支完整影片</h1>
          <p>
            这是一条独立的全 AI 管线。系统先形成创意脚本并锁定旁白时间，
            再逐镜头生成、下载验证、剪辑和质检。
          </p>
          <div className="ai-studio-hero-actions">
            <a className="button" href="#ai-studio-workbench">设计影片</a>
            <Link className="button-ghost" href="/create">返回素材剪辑</Link>
          </div>
          <div className="ai-studio-mode-note" role="status">
            <span aria-hidden="true" />
            <strong>独立生成管线</strong>
            <small>浏览器不能指定旧 Pipeline、Skill 或素材来源</small>
          </div>
        </div>
      </header>

      <form id="ai-studio-workbench" className="ai-studio-workbench" onSubmit={handleSubmit} aria-busy={studioState === "loading" || studioState === "estimating" || studioState === "submitting"}>
        <div className="ai-studio-editor">
          <section className="panel ai-studio-section ai-studio-brief">
            <div className="ai-studio-section-heading">
              <span>01</span>
              <div><p className="eyebrow">STORY BRIEF</p><h2>你想看到怎样的故事？</h2></div>
              <small>必填</small>
            </div>
            <label className="sr-only" htmlFor="ai-video-brief">影片创意</label>
            <textarea
              id="ai-video-brief"
              className="textarea ai-studio-brief-input"
              value={brief}
              onChange={(event) => { setBrief(event.target.value); invalidateEstimate(); }}
              placeholder="写下主题、故事走向、主体、地点与希望观众感受到的情绪…"
              maxLength={options?.limits.briefMaxLength || undefined}
              disabled={formFrozen}
              aria-invalid={Boolean(briefError)}
              aria-describedby={briefError ? "ai-video-brief-error" : "ai-video-brief-help"}
            />
            <div className="ai-studio-brief-meta">
              <span id={briefError ? "ai-video-brief-error" : "ai-video-brief-help"}>{briefError || "具体描述主体、动作与环境，有助于形成清晰的镜头提示"}</span>
              <span>{brief.length} / {options?.limits.briefMaxLength || "—"}</span>
            </div>
            <div className="ai-studio-starters" aria-label="创意示例">
              <span>灵感</span>
              {storyStarters.map((starter) => (
                <button key={starter} type="button" disabled={formFrozen} onClick={() => { setBrief(starter); invalidateEstimate(); }}>{starter}</button>
              ))}
            </div>
          </section>

          <section className="panel ai-studio-section">
            <div className="ai-studio-section-heading">
              <span>02</span>
              <div><p className="eyebrow">VISUAL DIRECTION</p><h2>导演风格</h2></div>
              <small>服务端允许项</small>
            </div>
            <fieldset className="ai-direction-grid" disabled={formFrozen || !options}>
              <legend className="sr-only">选择导演风格</legend>
              {(options?.limits.directions ?? []).map((profileId) => {
                const profile = directionCopy(profileId);
                return (
                  <label key={profileId} data-selected={direction === profileId || undefined}>
                    <input type="radio" name="ai-direction" value={profileId} checked={direction === profileId} onChange={() => { setDirection(profileId); invalidateEstimate(); }} />
                    <span className={`ai-direction-art ai-direction-art--${profile.art}`} aria-hidden="true"><i /></span>
                    <span><strong>{profile.label}</strong><small>{profile.note}</small></span>
                    <i aria-hidden="true">{direction === profileId ? "✓" : "+"}</i>
                  </label>
                );
              })}
            </fieldset>
            <label className="ai-continuity-toggle">
              <input
                type="checkbox"
                checked={continuity}
                disabled={formFrozen || !options?.provider.continuityModes.includes("none")}
                onChange={(event) => { setContinuity(event.target.checked); invalidateEstimate(); }}
              />
              <span aria-hidden="true" />
              <strong>提示词连续性</strong>
              <small>{continuity ? "尽量统一主体、色板与摄影规则，仍可能发生跨镜头漂移" : "不复用跨镜头提示规则，允许更自由的视觉变化"}</small>
            </label>
          </section>

          <section className="panel ai-studio-section">
            <div className="ai-studio-section-heading">
              <span>03</span>
              <div><p className="eyebrow">MODEL, OUTPUT & QUOTE</p><h2>模型与成片规格</h2></div>
              <small>以 API 返回为准</small>
            </div>
            <div className="ai-provider-model" aria-label="服务端生成模型">
              <span aria-hidden="true">AI</span>
              <div><small>Provider / Model</small><strong>{options?.provider.name || "未配置"} · {options?.provider.modelId || "—"}</strong></div>
              <i data-ready={providerReady || undefined}>{providerReady ? "READY" : options?.provider.status?.toUpperCase() || "LOADING"}</i>
            </div>
            <div className="ai-output-grid">
              <fieldset disabled={formFrozen || !options}>
                <legend>画幅</legend>
                <div className="ai-segmented-control">
                  {(options?.limits.aspectRatios ?? []).map((ratio) => (
                    <label key={ratio} data-selected={aspectRatio === ratio || undefined}>
                      <input type="radio" name="ai-aspect" checked={aspectRatio === ratio} onChange={() => { setAspectRatio(ratio); invalidateEstimate(); }} />
                      <span>{ratio}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={formFrozen || !options}>
                <legend>目标时长</legend>
                <div className="ai-segmented-control">
                  {(options?.limits.durationSeconds ?? []).map((duration) => (
                    <label key={duration} data-selected={durationSeconds === duration || undefined}>
                      <input type="radio" name="ai-duration" checked={durationSeconds === duration} onChange={() => { setDurationSeconds(duration); invalidateEstimate(); }} />
                      <span>{duration}s</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={formFrozen || !options}>
                <legend>每镜头候选</legend>
                <div className="ai-segmented-control">
                  {(options?.limits.variantsPerScene ?? []).map((count) => (
                    <label key={count} data-selected={variants === count || undefined}>
                      <input type="radio" name="ai-variants" checked={variants === count} onChange={() => { setVariants(count); invalidateEstimate(); }} />
                      <span>{count} 版</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="ai-budget-summary" aria-label="生成规模摘要">
                <span><small>{finalPlan ? "服务端镜头" : "本地预览镜头"}</small><strong>{(finalPlan?.sceneCount ?? previewSceneCount) || "—"}</strong></span>
                <span><small>{finalPlan ? "服务端候选" : "本地预览候选"}</small><strong>{(finalPlan?.candidateCount ?? previewCandidateCount) || "—"}</strong></span>
                <span><small>{finalPlan ? "服务端计费量" : "等待服务端报价"}</small><strong>{finalPlan ? `${finalPlan.billableSeconds}s` : "—"}</strong></span>
              </div>
              <p className="ai-preview-disclaimer">镜头数与候选数在估算前仅为界面预览，不能作为报价或付费依据。</p>
            </div>
          </section>

          <section className="panel ai-studio-section ai-studio-policy">
            <div className="ai-studio-section-heading">
              <span>04</span>
              <div><p className="eyebrow">DISCLOSURE & SAFETY</p><h2>生成内容声明</h2></div>
              <small>强制门禁</small>
            </div>
            <label htmlFor="ai-content-disclosure">
              <span className="sr-only">生成内容声明</span>
              <input id="ai-content-disclosure" type="checkbox" checked={disclosure} disabled={formFrozen} onChange={(event) => { setDisclosure(event.target.checked); invalidateEstimate(); }} />
              <span><strong>在成片元数据中标记 AI 生成内容</strong><small>发布前仍会执行真人肖像、品牌、敏感内容与平台政策检查。</small></span>
            </label>
          </section>
        </div>

        <aside className="panel ai-pipeline-panel" aria-label="全 AI 生产管线">
          <div className="ai-pipeline-panel-head">
            <div><p className="eyebrow">{options?.pipeline.slug || "FULL-AI"} / V{options?.pipeline.version || "—"}</p><h2>运行计划</h2></div>
            <span>{statusLabel[studioState]}</span>
          </div>
          <ol className="ai-pipeline-stages">
            {pipelineStages.map(([index, title, note], stageIndex) => (
              <li key={index}>
                <span>{index}</span>
                <div><strong>{title}</strong><small>{note}</small></div>
                <i aria-hidden="true">{stageIndex < 3 ? "●" : "○"}</i>
              </li>
            ))}
          </ol>
          <dl className="ai-run-summary">
            <div><dt>视觉来源</dt><dd>Generated only</dd></div>
            <div><dt>模型</dt><dd>{options?.provider.modelId || "—"}</dd></div>
            <div><dt>视觉方向</dt><dd>{selectedDirection.label}</dd></div>
            <div><dt>输出</dt><dd>{aspectRatio || "—"} · {durationSeconds || "—"}s</dd></div>
            <div><dt>服务端生成量</dt><dd>{finalPlan ? `${finalPlan.candidateCount} 个候选 / ${finalPlan.billableSeconds}s` : "等待估算"}</dd></div>
            <div><dt>服务端报价</dt><dd>{estimate?.quote ? formatMoney(estimate.quote.currency, estimate.quote.amountMinor) : "等待估算"}</dd></div>
          </dl>
          <div className="ai-provider-gate" role={studioState === "unavailable" || studioState === "blocked" || studioState === "submit_unknown" || studioState === "failed" ? "alert" : "status"} aria-live="polite">
            <span aria-hidden="true">{studioState === "created" ? "✓" : studioState === "editing" || studioState === "ready" ? "i" : "!"}</span>
            <div>
              <strong>{gateTitle}</strong>
              <p>{message}</p>
              {displayedBlockers.length > 1 ? <ul>{displayedBlockers.map((blocker) => <li key={blocker.code}>{blocker.message}</li>)}</ul> : null}
            </div>
          </div>
          <button className="button ai-pipeline-submit" type="submit" disabled={submitDisabled}>
            <span>{submitLabel}</span><i aria-hidden="true">↗</i>
          </button>
          {studioState === "unavailable" ? <button className="button-ghost ai-pipeline-reload" type="button" onClick={() => void loadOptions()}>重新检查生成能力</button> : null}
          {studioState === "created" && createdRun && shouldRetainFullAiAttempt(createdRun) ? <button className="button-ghost ai-pipeline-reload" type="button" onClick={() => void reconcileKnownRun()}>检查任务状态（仅查询）</button> : null}
          {studioState === "created" && createdRun?.projectRunId ? <Link className="button-ghost ai-pipeline-result" href={`/projects/${createdRun.projectRunId}`}>查看任务详情</Link> : null}
          <p className="ai-pipeline-footnote">
            生成片段只存在于当前 Run 范围；完成哈希、媒体探测、安全分析和 CandidateManifest 冻结后，才会进入 EDL 与最终渲染。
          </p>
        </aside>
      </form>
    </div>
  );
}
