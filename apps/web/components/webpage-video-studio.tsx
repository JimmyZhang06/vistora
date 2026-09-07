"use client";

import { UiSelect } from "@/components/ui-select";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ApiProblem,
  type WebpageVideoAspectRatio,
  type WebpageVideoOptions,
  type WebpageVideoRunCreateRequest,
} from "@/lib/api";
import {
  readWebpageVideoSubmissionAttempt,
  saveWebpageVideoSubmissionAttempt,
  type WebpageVideoSubmissionAttempt,
} from "@/lib/webpage-video-submission";

type StudioState =
  | "loading"
  | "ready"
  | "empty"
  | "blocked"
  | "error"
  | "submitting"
  | "submit_unknown";

const canonicalRatios: WebpageVideoAspectRatio[] = ["16:9", "9:16", "1:1", "4:3"];

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function loadPendingAttempt(): WebpageVideoSubmissionAttempt | null {
  try {
    return readWebpageVideoSubmissionAttempt(window.sessionStorage);
  } catch {
    return null;
  }
}

function persistPendingAttempt(attempt: WebpageVideoSubmissionAttempt | null) {
  try {
    saveWebpageVideoSubmissionAttempt(window.sessionStorage, attempt);
  } catch {
    // Private browsing can disable storage; the in-memory attempt remains stable.
  }
}

function publicHttpsProblem(value: string): string {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "请输入完整的 HTTPS 页面地址。";
  }
  if (parsed.protocol !== "https:") return "仅支持公开 HTTPS 页面。";
  if (parsed.username || parsed.password) return "链接中不能包含账号或密码。";
  const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) {
    return "不支持本机或内网页面。";
  }
  if (host === "::1" || host === "0:0:0:0:0:0:0:1" || host.startsWith("fe80:") || host.startsWith("fc") || host.startsWith("fd")) {
    return "不支持私有、回环或链路本地地址。";
  }
  const ipv4 = host.split(".").map(Number);
  if (ipv4.length === 4 && ipv4.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
    const [a, b] = ipv4;
    if (a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254)
      || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)) {
      return "不支持私有、回环、链路本地或保留地址。";
    }
  }
  return "";
}

function knownRunId(problem: ApiProblem): string | undefined {
  for (const key of ["webpage_video_run_id", "run_id", "id"]) {
    const value = problem.details?.[key];
    if (typeof value === "string" && value) return value;
  }
  return undefined;
}

function submissionMayHaveSucceeded(problem: ApiProblem): boolean {
  return problem.code === "NETWORK_ERROR" || problem.status === 503 || Boolean(problem.retryable);
}

function ratioLabel(ratio: WebpageVideoAspectRatio): string {
  return ratio === "16:9" ? "横屏"
    : ratio === "9:16" ? "竖屏"
      : ratio === "1:1" ? "方形"
        : "经典";
}

export function WebpageVideoStudio({ mode = "standard" }: { mode?: "standard" | "application-demo" }) {
  const applicationDemo = mode === "application-demo";
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const router = useRouter();
  const attemptRef = useRef<WebpageVideoSubmissionAttempt | null>(null);
  const [state, setState] = useState<StudioState>("loading");
  const [options, setOptions] = useState<WebpageVideoOptions | null>(null);
  const [message, setMessage] = useState("正在读取网页截图能力与可用输出规格…");
  const [targetUrl, setTargetUrl] = useState("");
  const [topic, setTopic] = useState("");
  const [aspectRatio, setAspectRatio] = useState<WebpageVideoAspectRatio>("16:9");
  const [durationSeconds, setDurationSeconds] = useState(0);
  const [subtitlesEnabled, setSubtitlesEnabled] = useState(false);
  const [voiceProfileId, setVoiceProfileId] = useState("");
  const [maxPages, setMaxPages] = useState(8);
  const [maxDepth, setMaxDepth] = useState(1);
  const [includeSitemap, setIncludeSitemap] = useState(true);
  const [publicPageConfirmed, setPublicPageConfirmed] = useState(false);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [urlTouched, setUrlTouched] = useState(false);

  const restoreRequest = useCallback((request: WebpageVideoRunCreateRequest) => {
    setTargetUrl(request.targetUrl);
    setTopic(request.topic);
    setAspectRatio(request.aspectRatio);
    setDurationSeconds(request.durationSeconds);
    setSubtitlesEnabled(request.subtitlesEnabled);
    setVoiceProfileId(request.voiceProfileId ?? "");
    setMaxPages(request.crawl.maxPages);
    setMaxDepth(request.crawl.maxDepth);
    setIncludeSitemap(request.crawl.includeSitemap);
    setPublicPageConfirmed(true);
    setRightsConfirmed(true);
  }, []);

  const loadOptions = useCallback(async () => {
    const pending = loadPendingAttempt();
    attemptRef.current = pending;
    if (pending) restoreRequest(pending.request);
    setState("loading");
    setMessage("正在读取网页截图能力与可用输出规格…");
    const result = await adapter.getWebpageVideoOptions();
    if (!result.ok) {
      setOptions(null);
      if (pending) {
        setState("submit_unknown");
        setMessage("发现一条结果未确认的创建请求。只能用原请求和原幂等键核对，避免重复创建。");
      } else {
        setState("error");
        setMessage(result.error.message || "无法读取网页截图成片服务。");
      }
      return;
    }
    const next = result.data;
    setOptions(next);
    if (!pending) {
      const demoRatio = next.limits.aspectRatios.includes("9:16") ? "9:16" : next.limits.aspectRatios[0] ?? "16:9";
      const demoDuration = next.limits.durationSeconds.includes(30) ? 30 : next.limits.durationSeconds[0] ?? 0;
      setAspectRatio((current) => applicationDemo
        ? demoRatio
        : next.limits.aspectRatios.includes(current) ? current : next.limits.aspectRatios[0] ?? "16:9");
      setDurationSeconds((current) => applicationDemo
        ? demoDuration
        : next.limits.durationSeconds.includes(current) ? current : next.limits.durationSeconds[0] ?? 0);
      setSubtitlesEnabled(next.subtitles.supported && (applicationDemo || next.subtitles.defaultEnabled));
      setVoiceProfileId((current) => current && next.voices.some((voice) => voice.id === current) ? current : "");
      setMaxPages(applicationDemo
        ? Math.min(4, next.limits.crawlMaxPagesLimit)
        : Math.min(next.limits.crawlMaxPagesDefault, next.limits.crawlMaxPagesLimit));
      setMaxDepth(Math.min(next.limits.crawlMaxDepthDefault, next.limits.crawlMaxDepthLimit));
      setIncludeSitemap(!applicationDemo);
      if (applicationDemo) {
        setTopic("30 秒竖屏产品导览：展示目标客户痛点、核心功能、可信工作流与明确行动号召。");
      }
    }
    if (pending) {
      setState("submit_unknown");
      setMessage("发现一条结果未确认的创建请求。继续时会复用原请求和原幂等键，不会生成新的任务标识。");
    } else if (next.status !== "ready") {
      setState("blocked");
      setMessage(next.blockers[0]?.message || "网页截图成片服务尚未就绪。");
    } else if (!next.limits.aspectRatios.length || !next.limits.durationSeconds.length) {
      setState("empty");
      setMessage("服务已连接，但没有返回可用画幅或时长；创建入口保持关闭。");
    } else {
      setState("ready");
      setMessage("服务已就绪。提交后先生成网页截图，只有你批准精确截图后才会进入素材与视频阶段。");
    }
  }, [adapter, applicationDemo, restoreRequest]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadOptions(), 0);
    return () => window.clearTimeout(timer);
  }, [loadOptions]);

  const urlProblem = targetUrl.trim() ? publicHttpsProblem(targetUrl.trim()) : "请输入目标页面 URL。";
  const supportedRatio = Boolean(options?.limits.aspectRatios.includes(aspectRatio));
  const supportedDuration = Boolean(options?.limits.durationSeconds.includes(durationSeconds));
  const requestValid = state === "ready"
    && !urlProblem
    && Boolean(topic.trim())
    && topic.trim().length <= (options?.limits.topicMaxLength ?? 0)
    && supportedRatio
    && supportedDuration
    && maxPages >= 1
    && maxPages <= (options?.limits.crawlMaxPagesLimit ?? 0)
    && maxDepth >= 0
    && maxDepth <= (options?.limits.crawlMaxDepthLimit ?? 0)
    && publicPageConfirmed
    && rightsConfirmed;
  const formFrozen = state === "submitting" || state === "submit_unknown";
  const displayedRatios = canonicalRatios.filter((ratio) => options?.limits.aspectRatios.includes(ratio));

  function currentRequest(): WebpageVideoRunCreateRequest {
    return {
      targetUrl: targetUrl.trim(),
      topic: topic.trim(),
      aspectRatio,
      durationSeconds,
      subtitlesEnabled: Boolean(options?.subtitles.supported && subtitlesEnabled),
      voiceProfileId: voiceProfileId || undefined,
      publicPageConfirmed: true,
      rightsConfirmed: true,
      crawl: { maxPages, maxDepth, sameOriginOnly: true, includeSitemap },
    };
  }

  async function submitAttempt(attempt: WebpageVideoSubmissionAttempt) {
    setState("submitting");
    setMessage("正在创建独立网页截图任务；请勿重复提交…");
    const result = await adapter.createWebpageVideoRun(attempt.request, attempt.idempotencyKey);
    if (result.ok) {
      attemptRef.current = null;
      persistPendingAttempt(null);
      router.push(`/webpage-video/${encodeURIComponent(result.data.id)}`);
      return;
    }
    const runId = knownRunId(result.error);
    if (runId) {
      attemptRef.current = null;
      persistPendingAttempt(null);
      router.push(`/webpage-video/${encodeURIComponent(runId)}`);
      return;
    }
    if (submissionMayHaveSucceeded(result.error)) {
      setState("submit_unknown");
      setMessage("网络或队列响应不明确。原请求与原幂等键已保留；重试只会核对同一次创建。");
      return;
    }
    attemptRef.current = null;
    persistPendingAttempt(null);
    setState("error");
    setMessage(result.error.message || "服务端已确认创建失败，请检查输入后重试。");
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state === "submit_unknown" && attemptRef.current) {
      void submitAttempt(attemptRef.current);
      return;
    }
    setUrlTouched(true);
    if (!requestValid) {
      setMessage("请完成 URL、用途、输出规格和两项明确确认后再创建。");
      return;
    }
    const attempt = { idempotencyKey: idempotencyKey(applicationDemo ? "application-demo-create" : "webpage-video-create"), request: currentRequest() };
    attemptRef.current = attempt;
    persistPendingAttempt(attempt);
    void submitAttempt(attempt);
  }

  const stateLabel: Record<StudioState, string> = {
    loading: "LOADING",
    ready: "READY",
    empty: "NO OPTIONS",
    blocked: "BLOCKED",
    error: "ERROR",
    submitting: "CREATING",
    submit_unknown: "SAFE RETRY",
  };
  const blockers = options?.blockers ?? [];
  const statusIsProblem = ["empty", "blocked", "error", "submit_unknown"].includes(state);
  const readinessCount = [
    !urlProblem,
    Boolean(topic.trim()),
    publicPageConfirmed && rightsConfirmed,
    state === "ready",
  ].filter(Boolean).length;

  return (
    <div className="page page--wide web-video-page" data-studio-state={state} data-application-demo={applicationDemo || undefined}>
      <header className={`web-video-hero${applicationDemo ? " web-video-hero--compact" : ""}`} aria-labelledby="web-video-title">
        <div>
          <p className="eyebrow">{applicationDemo ? "APPLICATION DEMO · EVIDENCE-READY" : "DIRECTED SITE CAPTURE · REVIEWED STORYBOARD"}</p>
          <h1 id="web-video-title" tabIndex={-1}>{applicationDemo ? "用一条真实任务，完成申请评审演示" : "从一个网址，定向解析成多镜头视频"}</h1>
          <p>{applicationDemo ? "预设为 30 秒竖屏、四页以内和字幕输出，重点展示可控生成、人工审核、来源哈希与试点指标；所有结果仍由真实生产链路生成。" : "系统先发现同站候选页面，由你确认范围；随后逐页截图、识别关键区域并生成镜头板。两次审核通过后才进入成片阶段。"}</p>
          <div className="web-video-hero-actions">
            <a className="button" href="#web-video-form">{applicationDemo ? "填写产品网址" : "配置定向解析"}</a>
            <Link className="button-ghost" href={applicationDemo ? "/create/webpage-video" : "/create"}>{applicationDemo ? "切换标准模式" : "返回标准创作"}</Link>
            {applicationDemo ? <Link className="button-ghost" href="/application-evidence">查看申请证据中心</Link> : null}
          </div>
          <div className="web-video-boundary">
            <span aria-hidden="true">◎</span>
            <div><strong>仅公开 HTTPS 页面</strong><small>不接收登录态、Cookie、自定义 Header、脚本或内网地址</small></div>
          </div>
        </div>
      </header>

      <form id="web-video-form" className="web-video-workbench" onSubmit={handleSubmit} aria-busy={state === "loading" || state === "submitting"}>
        <div className="web-video-editor">
          <section className="panel web-video-section">
            <header><span>01</span><div><p className="eyebrow">PUBLIC SOURCE</p><h2>目标页面</h2></div><small>HTTPS ONLY</small></header>
            <div className="web-video-fields">
              <label className="field web-video-url-field" htmlFor="web-video-url">
                <span>页面 URL</span>
                <input
                  id="web-video-url"
                  className="input"
                  type="url"
                  inputMode="url"
                  autoComplete="url"
                  placeholder="https://example.com/public-page"
                  value={targetUrl}
                  maxLength={options?.limits.urlMaxLength ?? 2048}
                  disabled={formFrozen}
                  aria-invalid={urlTouched && Boolean(urlProblem)}
                  aria-describedby="web-video-url-help"
                  onBlur={() => setUrlTouched(true)}
                  onChange={(event) => setTargetUrl(event.target.value)}
                />
                <small id="web-video-url-help" data-error={urlTouched && Boolean(urlProblem) || undefined}>
                  {urlTouched && urlProblem ? urlProblem : "服务端会重新校验 DNS、重定向和最终地址；不要提交带访问令牌、签名密钥或其他秘密的 URL。"}
                </small>
              </label>
              <label className="field" htmlFor="web-video-topic">
                <span>网页用途 / 视频主题</span>
                <textarea
                  id="web-video-topic"
                  className="textarea"
                  value={topic}
                  maxLength={options?.limits.topicMaxLength ?? 1600}
                  disabled={formFrozen}
                  placeholder="例如：把产品发布页做成 30 秒竖屏功能导览，突出核心卖点与行动号召。"
                  onChange={(event) => setTopic(event.target.value)}
                />
                <small>{topic.length} / {options?.limits.topicMaxLength ?? "—"}</small>
              </label>
              <details className="web-video-advanced" data-collapsible={applicationDemo || undefined} open={applicationDemo ? undefined : true}>
                <summary><span>高级抓取设置</span><small>{maxPages} 页 · 深度 {maxDepth}{includeSitemap ? " · Sitemap" : ""}</small></summary>
                <fieldset className="web-video-crawl" disabled={formFrozen || state !== "ready"}>
                <legend>定向解析范围</legend>
                <div>
                  <div className="field"><span>最多页面</span><UiSelect ariaLabel="最多页面" value={String(maxPages)} onChange={(value) => setMaxPages(Number(value))}>{[1, 4, 6, 8, 10, 12].filter((value) => value <= (options?.limits.crawlMaxPagesLimit ?? 12)).map((value) => <option key={value} value={value}>{value} 页</option>)}</UiSelect><small>{applicationDemo ? "演示预设 4 页，控制耗时与现场风险。" : "发现更多候选时会在上限处停止。"}</small></div>
                  <div className="field"><span>链接深度</span><UiSelect ariaLabel="链接深度" value={String(maxDepth)} onChange={(value) => setMaxDepth(Number(value))}>{[0, 1, 2].filter((value) => value <= (options?.limits.crawlMaxDepthLimit ?? 2)).map((value) => <option key={value} value={value}>{value === 0 ? "仅目标页" : value === 1 ? "导航与一层链接" : "最多两层"}</option>)}</UiSelect><small>不会无限遍历分页或日历 URL。</small></div>
                </div>
                <label className="web-video-switch">
                  <input aria-label="使用 sitemap 发现候选页面" type="checkbox" checked={includeSitemap} onChange={(event) => setIncludeSitemap(event.target.checked)} />
                  <span aria-hidden="true" />
                  <div><strong>参考 sitemap.xml</strong><small>仅用于补充同源候选页面，仍受页数和深度上限约束。</small></div>
                </label>
                <p>固定为同源发现；不会跨域跟随链接，也不会登录、提交表单或执行自定义交互。页面范围会在批量截图前再次交给你审核。</p>
                </fieldset>
              </details>
            </div>
          </section>

          <section className="panel web-video-section">
            <header><span>02</span><div><p className="eyebrow">VIDEO OUTPUT</p><h2>成片规格</h2></div><small>由 OPTIONS 提供</small></header>
            {applicationDemo ? <div className="web-video-preset"><span>推荐预设</span><strong>{aspectRatio} · {durationSeconds || 30} 秒 · {subtitlesEnabled ? "字幕开启" : "无字幕"}</strong><small>适合现场评审和手机端展示，可在下方修改。</small></div> : null}
            <details className="web-video-advanced web-video-advanced--output" data-collapsible={applicationDemo || undefined} open={applicationDemo ? undefined : true}>
              <summary><span>修改成片规格</span><small>{aspectRatio} · {durationSeconds || "—"} 秒</small></summary>
              <div className="web-video-output-grid">
              <fieldset disabled={formFrozen || state !== "ready"}>
                <legend>画幅</legend>
                <div className="web-video-choice-grid">
                  {displayedRatios.map((ratio) => (
                    <label key={ratio} data-selected={aspectRatio === ratio || undefined}>
                      <input aria-label={`${ratio} ${ratioLabel(ratio)}画幅`} type="radio" name="web-video-aspect" checked={aspectRatio === ratio} onChange={() => setAspectRatio(ratio)} />
                      <span><strong>{ratio}</strong><small>{ratioLabel(ratio)}</small></span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={formFrozen || state !== "ready"}>
                <legend>目标时长</legend>
                <div className="web-video-duration-grid">
                  {(options?.limits.durationSeconds ?? []).map((duration) => (
                    <label key={duration} data-selected={durationSeconds === duration || undefined}>
                      <input aria-label={`${duration} 秒`} type="radio" name="web-video-duration" checked={durationSeconds === duration} onChange={() => setDurationSeconds(duration)} />
                      <span>{duration}<small>秒</small></span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="field">
                <span>旁白声音</span>
                <UiSelect ariaLabel="旁白声音" value={String(voiceProfileId)} disabled={formFrozen || state !== "ready"} onChange={(value) => setVoiceProfileId(value)}>
                  <option value="">系统默认声音</option>
                  {(options?.voices ?? []).map((voice) => <option key={voice.id} value={voice.id}>{voice.name}{voice.language ? ` · ${voice.language}` : ""}</option>)}
                </UiSelect>
                <small>{options?.voices.length ? "只显示后端当前允许的 voice。" : "后端未返回可选 voice，将使用系统默认值。"}</small>
              </div>
              <label className="web-video-switch" aria-disabled={!options?.subtitles.supported || undefined}>
                <input aria-label="生成字幕" type="checkbox" checked={subtitlesEnabled} disabled={formFrozen || !options?.subtitles.supported} onChange={(event) => setSubtitlesEnabled(event.target.checked)} />
                <span aria-hidden="true" />
                <div><strong>生成字幕</strong><small>{options?.subtitles.supported ? "字幕将按旁白时间轴生成。" : "当前后端 options 未启用字幕。"}</small></div>
              </label>
              </div>
            </details>
          </section>

          <section className="panel web-video-section web-video-policy">
            <header><span>03</span><div><p className="eyebrow">ACCESS & RIGHTS</p><h2>公开访问与版权确认</h2></div><small>必须显式勾选</small></header>
            <div>
              <label>
                <input aria-label="确认页面无需登录即可公开访问" type="checkbox" checked={publicPageConfirmed} disabled={formFrozen} onChange={(event) => setPublicPageConfirmed(event.target.checked)} />
                <span><strong>我确认这是无需登录即可访问的公开页面</strong><small>不会提交账号、Cookie、验证码、自定义 Header，也不会尝试访问本机或内网资源。</small></span>
              </label>
              <label>
                <input aria-label="确认有权截图并用于视频" type="checkbox" checked={rightsConfirmed} disabled={formFrozen} onChange={(event) => setRightsConfirmed(event.target.checked)} />
                <span><strong>我确认有权截图并将页面画面用于本视频</strong><small>页面标题和正文会作为不可信引用数据发送给已配置的文本模型生成旁白；审核截图不替代商标、肖像、字体、图片与网页内容的授权责任。</small></span>
              </label>
            </div>
          </section>
        </div>

        <aside className="panel web-video-control" aria-label="网页截图成片控制面">
          <header><div><p className="eyebrow">{options?.pipeline?.slug ?? "WEBPAGE-CAPTURE-VIDEO"}</p><h2>{applicationDemo ? "提交前检查" : "独立运行计划"}</h2></div><span data-state={state}>{stateLabel[state]}</span></header>
          {!applicationDemo ? <><ol>
            <li><span>01</span><div><strong>URL 安全校验</strong><small>HTTPS · DNS · Redirect · SSRF</small></div></li>
            <li><span>02</span><div><strong>定向发现与范围审核</strong><small>同源 · {maxPages} 页 · 深度 {maxDepth}</small></div></li>
            <li><span>03</span><div><strong>逐页截图与区域识别</strong><small>全景 · Hero · 图表 · CTA</small></div></li>
            <li><span>04</span><div><strong>镜头板审核</strong><small>启停 · 排序 · 重截</small></div></li>
            <li><span>05</span><div><strong>视频合成与质检</strong><small>旁白 · 字幕 · EDL · MP4</small></div></li>
          </ol>
          <dl>
            <div><dt>网页</dt><dd>{targetUrl.trim() ? "公开 HTTPS" : "待输入"}</dd></div>
            <div><dt>范围</dt><dd>同源 · ≤ {maxPages} 页 · 深度 {maxDepth}</dd></div>
            <div><dt>截图</dt><dd>{aspectRatio} viewport</dd></div>
            <div><dt>成片</dt><dd>{durationSeconds ? `${durationSeconds}s` : "待选择"}</dd></div>
            <div><dt>字幕</dt><dd>{subtitlesEnabled ? "开启" : "关闭"}</dd></div>
            <div><dt>声音</dt><dd>{options?.voices.find((voice) => voice.id === voiceProfileId)?.name ?? "系统默认"}</dd></div>
          </dl></> : null}
          {applicationDemo ? (
            <section className="web-video-readiness" aria-labelledby="application-readiness-title">
              <header><h3 id="application-readiness-title">申请演示就绪检查</h3><strong>{readinessCount}/4</strong></header>
              <ul>
                <li data-ready={!urlProblem || undefined}><span>{!urlProblem ? "✓" : "·"}</span>公开 HTTPS 产品页</li>
                <li data-ready={Boolean(topic.trim()) || undefined}><span>{topic.trim() ? "✓" : "·"}</span>问题、创新与客户价值叙事</li>
                <li data-ready={publicPageConfirmed && rightsConfirmed || undefined}><span>{publicPageConfirmed && rightsConfirmed ? "✓" : "·"}</span>访问权与内容使用权确认</li>
                <li data-ready={state === "ready" || undefined}><span>{state === "ready" ? "✓" : "·"}</span>真实队列、存储与 Worker 就绪</li>
              </ul>
              <p>成功后可在任务详情下载证据报告并填写匿名化试点成效。</p>
            </section>
          ) : null}
          <div className="web-video-status" role={statusIsProblem ? "alert" : "status"} aria-live="polite" data-problem={statusIsProblem || undefined}>
            <span aria-hidden="true">{state === "ready" ? "✓" : "!"}</span>
            <div><strong>{state === "submit_unknown" ? "安全重试已锁定" : state === "ready" ? "等待你的配置" : "当前状态"}</strong><p>{message}</p></div>
          </div>
          {blockers.length > 1 ? <ul className="web-video-blockers">{blockers.map((blocker) => <li key={blocker.code}>{blocker.message}</li>)}</ul> : null}
          <button className="button web-video-submit" type="submit" disabled={state === "loading" || state === "submitting" || (state !== "submit_unknown" && !requestValid)}>
            <span>{state === "submitting" ? "正在创建" : state === "submit_unknown" ? "用原幂等键安全重试" : "创建多页面视频任务"}</span><i aria-hidden="true">↗</i>
          </button>
          {["error", "blocked", "empty"].includes(state) ? <button className="button-ghost web-video-retry" type="button" onClick={() => void loadOptions()}>重新读取服务能力</button> : null}
          <p className="web-video-footnote">{applicationDemo ? "任务会保留来源哈希与人工审核记录；完成后再填写试点结果并导出证据。" : "页面范围与镜头板分别绑定 revision/hash。任何重新发现、重截或重排都会产生新版本，旧批准不会自动套用。"}</p>
        </aside>
      </form>
    </div>
  );
}
