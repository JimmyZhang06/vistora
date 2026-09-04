"use client";

import Image from "next/image";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type WebpageVideoRun,
  type WebpageVideoSiteReviewDecision,
  type WebpageVideoSitePlan,
  type WebpageVideoStoryboardShot,
} from "@/lib/api";
import { safeBrowserMediaUrl } from "@/lib/safe-media-url";
import { UiSelect } from "@/components/ui-select";

type SiteState = "loading" | "ready" | "empty" | "error";
type Gate = "scope" | "storyboard";

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function validHash(value: string): boolean {
  return /^[a-f0-9]{64}$/i.test(value);
}

function scoreLabel(value?: number): string {
  if (typeof value !== "number") return "—";
  return value <= 1 ? `${Math.round(value * 100)}%` : String(Math.round(value));
}

function StoryboardShotCard({
  shot,
  index,
  total,
  editable,
  onMove,
  onUpdate,
  onPreviewLoaded,
  onPreviewFailed,
}: {
  shot: WebpageVideoStoryboardShot;
  index: number;
  total: number;
  editable: boolean;
  onMove: (direction: -1 | 1) => void;
  onUpdate: (patch: Partial<WebpageVideoStoryboardShot>) => void;
  onPreviewLoaded: (key: string) => void;
  onPreviewFailed: (key: string) => void;
}) {
  const url = safeBrowserMediaUrl(shot.previewUrl);
  const previewKey = url ? `${shot.id}|${url}` : "";
  return (
    <li data-disabled={!shot.enabled || undefined}>
      <div className="web-video-shot-order">
        <strong>{String(index + 1).padStart(2, "0")}</strong>
        <button type="button" aria-label={`上移 ${shot.label}`} disabled={!editable || index === 0} onClick={() => onMove(-1)}>↑</button>
        <button type="button" aria-label={`下移 ${shot.label}`} disabled={!editable || index === total - 1} onClick={() => onMove(1)}>↓</button>
      </div>
      {url ? (
        <Image
          unoptimized
          src={url}
          alt={`${shot.label} 镜头预览`}
          width={640}
          height={360}
          referrerPolicy="no-referrer"
          onLoad={() => onPreviewLoaded(previewKey)}
          onError={() => onPreviewFailed(previewKey)}
        />
      ) : <div className="web-video-shot-missing" role="alert">缺少安全预览</div>}
      <div className="web-video-shot-copy">
        <strong>{shot.label}</strong>
        <small>{shot.durationSeconds ? `${shot.durationSeconds}s · ` : ""}{shot.regionId ? "关键区域特写" : "页面全景"}</small>
        {shot.reason ? <p>{shot.reason}</p> : null}
      </div>
      <div className="web-video-shot-controls">
        <div><span>镜头运动</span><UiSelect ariaLabel={`${shot.label} 镜头运动`} value={shot.motion} disabled={!editable || !shot.enabled} onChange={(motion) => onUpdate({ motion: motion as WebpageVideoStoryboardShot["motion"] })}><option value="zoom_in">缓慢推近</option><option value="zoom_out">缓慢拉远</option><option value="pan">轻柔平移</option><option value="static">静止画面</option></UiSelect></div>
        <div><span>与前镜头</span><UiSelect ariaLabel={`${shot.label} 转场`} value={index === 0 ? "cut" : shot.transition} disabled={!editable || !shot.enabled || index === 0} onChange={(transition) => onUpdate({ transition: transition as WebpageVideoStoryboardShot["transition"] })}><option value="fade_black">淡黑过渡</option><option value="cut">直接切换</option></UiSelect></div>
      </div>
      <label className="web-video-shot-toggle">
        <input type="checkbox" checked={shot.enabled} disabled={!editable} onChange={(event) => onUpdate({ enabled: event.target.checked })} />
        <span>{shot.enabled ? "启用" : "停用"}</span>
      </label>
    </li>
  );
}

export function WebpageVideoSitePlanPanel({ run, onChanged }: { run: WebpageVideoRun; onChanged: () => void }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const loadingRef = useRef(false);
  const syncedScopeRevision = useRef(-1);
  const syncedStoryboardKey = useRef("");
  const [state, setState] = useState<SiteState>("loading");
  const [site, setSite] = useState<WebpageVideoSitePlan | null>(null);
  const [error, setError] = useState("");
  const [selectedPageIds, setSelectedPageIds] = useState<string[]>([]);
  const [shots, setShots] = useState<WebpageVideoStoryboardShot[]>([]);
  const [loadedPreviews, setLoadedPreviews] = useState<Set<string>>(() => new Set());
  const [comment, setComment] = useState("");
  const [action, setAction] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState("");
  const [conflict, setConflict] = useState(false);

  const load = useCallback(async () => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    const result = await adapter.getWebpageVideoSite(run.id);
    loadingRef.current = false;
    if (!result.ok) {
      setError(result.error.status === 404
        ? "定向解析清单尚未生成。系统会继续轮询；不会回退成单张截图并假装完成。"
        : result.error.message || "无法读取多页面解析结果。");
      setState(result.error.status === 404 ? "empty" : "error");
      return;
    }
    const next = result.data;
    setSite(next);
    setError("");
    setState(next.scope.pages.length || next.storyboard?.shots.length ? "ready" : "empty");
    if (next.scope.revision !== syncedScopeRevision.current) {
      syncedScopeRevision.current = next.scope.revision;
      setSelectedPageIds(next.scope.pages.filter((page) => page.selected).map((page) => page.id));
    }
    if (next.storyboard) {
      const storyboardKey = `${next.storyboard.revision}:${next.storyboard.sha256}:${next.storyboard.shots.map((shot) => shot.id).join(",")}`;
      if (storyboardKey === syncedStoryboardKey.current) return;
      syncedStoryboardKey.current = storyboardKey;
      setShots([...next.storyboard.shots].sort((a, b) => a.order - b.order));
      setLoadedPreviews(new Set());
    }
  }, [adapter, run.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    if (["succeeded", "failed", "cancelled"].includes(run.status)) return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 3_000);
    return () => window.clearInterval(timer);
  }, [load, run.status]);

  const enabledShots = shots.filter((shot) => shot.enabled);
  const previewsReady = enabledShots.length > 0 && enabledShots.every((shot) => {
    const url = safeBrowserMediaUrl(shot.previewUrl);
    return Boolean(url && loadedPreviews.has(`${shot.id}|${url}`));
  });
  const canReviewScope = run.status === "awaiting_scope_review"
    && Boolean(site && site.scope.revision > 0 && validHash(site.scope.sha256) && selectedPageIds.length > 0)
    && !action;
  const canReviewStoryboard = run.status === "awaiting_storyboard_review"
    && Boolean(site?.storyboard && site.storyboard.revision > 0 && validHash(site.storyboard.sha256))
    && previewsReady
    && !action;

  function togglePage(id: string) {
    setSelectedPageIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
  }

  function updateShot(id: string, patch: Partial<WebpageVideoStoryboardShot>) {
    setShots((current) => current.map((shot) => shot.id === id ? { ...shot, ...patch } : shot));
  }

  function moveShot(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= shots.length) return;
    setShots((current) => {
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next.map((shot, order) => ({ ...shot, order }));
    });
  }

  async function review(gate: Gate, decision: WebpageVideoSiteReviewDecision) {
    if (!site || (gate === "scope" ? !canReviewScope : !canReviewStoryboard)) return;
    setAction(`${gate}:${decision}`);
    setConflict(false);
    setActionMessage("正在提交与当前 revision/hash 绑定的决定…");
    const result = gate === "scope"
      ? await adapter.reviewWebpageVideoScope(run.id, {
        decision,
        comment: comment.trim() || undefined,
        expectedRevision: site.scope.revision,
        expectedSha256: site.scope.sha256,
        selectedPageIds,
      }, idempotencyKey(`webpage-video-scope-${decision}`))
      : await adapter.reviewWebpageVideoStoryboard(run.id, {
        decision,
        comment: comment.trim() || undefined,
        expectedRevision: site.storyboard!.revision,
        expectedSha256: site.storyboard!.sha256,
        shots: shots.map((shot, order) => ({
          id: shot.id,
          enabled: shot.enabled,
          order: order + 1,
          motion: shot.motion,
          transition: order === 0 ? "cut" : shot.transition,
        })),
      }, idempotencyKey(`webpage-video-storyboard-${decision}`));
    setAction(null);
    if (!result.ok) {
      if (result.error.status === 409) {
        setConflict(true);
        setActionMessage("清单已被重建或版本已变化；本次决定没有自动重放。请刷新后重新核对。");
      } else {
        setActionMessage(result.error.message || "审核提交失败，请重试。");
      }
      return;
    }
    setComment("");
    setActionMessage(decision === "approve" ? "已批准当前版本，正在进入下一阶段。" : decision === "request_changes" ? "已要求重新生成；旧版本批准不会复用。" : "已拒绝当前任务。");
    await load();
    onChanged();
  }

  if (state === "loading") return <section className="panel web-video-site-state" role="status"><span>…</span><div><h2>正在读取定向解析结果</h2><p>等待页面范围、截图证据与镜头板清单。</p></div></section>;
  if ((state === "error" || state === "empty") && !site) return <section className="panel web-video-site-state" role={state === "error" ? "alert" : "status"}><span>{state === "error" ? "!" : "⌁"}</span><div><h2>{state === "error" ? "多页面结果不可用" : "定向解析尚未产出清单"}</h2><p>{error}</p><button className="button-ghost" type="button" onClick={() => void load()}>重试读取</button></div></section>;
  if (!site) return null;

  return (
    <>
      {error ? <div className="web-video-inline-alert" role="alert"><span>!</span><p>{error} 当前保留最后一次成功读取的清单。</p><button type="button" onClick={() => void load()}>重试</button></div> : null}
      {actionMessage ? <div className="web-video-inline-alert" role={conflict ? "alert" : "status"} data-conflict={conflict || undefined}><span>{conflict ? "!" : "i"}</span><p>{actionMessage}</p>{conflict ? <button type="button" onClick={() => { setConflict(false); void load(); }}>刷新最新版本</button> : null}</div> : null}

      <section className="panel web-video-site-scope" aria-labelledby="site-scope-title">
        <header><div><p className="eyebrow">DIRECTED DISCOVERY · HUMAN GATE 1</p><h2 id="site-scope-title">页面范围清单</h2></div><span>{site.scope.status.toUpperCase()}</span></header>
        <p>选择本次允许截图的同源页面。批准时同时提交 scope revision、SHA-256 与精确 page IDs；页面清单变化会触发 409，不会静默扩大范围。</p>
        {site.scope.pages.length ? <div className="web-video-page-list">{site.scope.pages.map((page) => (
          <label key={page.id} data-selected={selectedPageIds.includes(page.id) || undefined}>
            <input type="checkbox" checked={selectedPageIds.includes(page.id)} disabled={run.status !== "awaiting_scope_review" || Boolean(action)} onChange={() => togglePage(page.id)} />
            <span><strong>{page.title || page.finalUrl || page.url}</strong><small>{page.pageType || "page"} · 相关度 {scoreLabel(page.score)}</small><code>{page.finalUrl || page.url}</code>{page.reason ? <em>{page.reason}</em> : null}</span>
          </label>
        ))}</div> : <div className="web-video-capture-empty" role="status"><span>⌁</span><h3>没有发现候选页面</h3><p>可能尚在解析，或安全规则过滤了所有候选。页面为空时不会开放批准。</p></div>}
        <dl className="web-video-evidence"><div><dt>Scope revision</dt><dd>{site.scope.revision || "—"}</dd></div><div><dt>Scope SHA-256</dt><dd><code>{site.scope.sha256 || "等待清单固化"}</code></dd></div><div><dt>已选择</dt><dd>{selectedPageIds.length} / {site.scope.pages.length} 页</dd></div></dl>
        {run.status === "awaiting_scope_review" ? <div className="web-video-site-gate">
          {!canReviewScope ? <p role="status">至少选择一个页面，并等待服务端提供有效 revision 与 64 位清单哈希。</p> : null}
          <div><button className="button" type="button" disabled={!canReviewScope} onClick={() => void review("scope", "approve")}>批准范围并逐页截图</button><button className="button-ghost" type="button" disabled={!canReviewScope} onClick={() => void review("scope", "request_changes")}>重新发现页面</button><button className="button-danger" type="button" disabled={!canReviewScope} onClick={() => void review("scope", "reject")}>拒绝并停止</button></div>
        </div> : null}
      </section>

      <section className="panel web-video-site-captures" aria-labelledby="site-captures-title">
        <header><div><p className="eyebrow">PAGE CAPTURES · KEY REGIONS</p><h2 id="site-captures-title">页面截图与关键区域</h2></div><span>{site.scope.pages.filter((page) => page.capture).length}/{selectedPageIds.length || site.scope.pages.length}</span></header>
        {site.scope.pages.some((page) => page.capture || page.failure) ? <div className="web-video-capture-gallery">{site.scope.pages.filter((page) => page.selected || selectedPageIds.includes(page.id)).map((page) => {
          const pageUrl = safeBrowserMediaUrl(page.capture?.previewUrl);
          return <article key={page.id} data-failed={Boolean(page.failure) || undefined}><header><div><strong>{page.title || page.finalUrl || page.url}</strong><small>{page.status || (page.capture ? "captured" : "waiting")}</small></div><span>{page.regions.length} 个关键区域</span></header>{page.failure ? <div className="web-video-page-failure" role="alert"><strong>{page.failure.code || "CAPTURE FAILED"}</strong><p>{page.failure.message}</p><small>{page.failure.retryable ? "可在镜头板门禁要求重截。" : "请从范围中移除或重新创建任务。"}</small></div> : pageUrl ? <figure><Image unoptimized src={pageUrl} alt={`${page.title || page.url} 完整页面截图`} width={page.capture?.width ?? 1600} height={page.capture?.height ?? 900} referrerPolicy="no-referrer" /><figcaption><code>{page.capture?.sha256 || "等待截图哈希"}</code></figcaption></figure> : <div className="web-video-page-pending" role="status">截图尚未完成或签名 URL 不可用。</div>}
            {page.regions.length ? <div className="web-video-region-grid">{page.regions.map((region) => { const url = safeBrowserMediaUrl(region.previewUrl); return <figure key={region.id}>{url ? <Image unoptimized src={url} alt={`${region.label} 关键区域`} width={region.width ?? 800} height={region.height ?? 450} referrerPolicy="no-referrer" /> : <div>预览尚未生成</div>}<figcaption><strong>{region.label}</strong><small>{region.type} · {scoreLabel(region.score)}</small>{region.reason ? <p>{region.reason}</p> : null}</figcaption></figure>; })}</div> : null}</article>;
        })}</div> : <div className="web-video-capture-empty" role="status"><span>⌁</span><h3>等待逐页截图</h3><p>范围批准后才会逐页截图并识别 Hero、产品界面、图表、指标和 CTA。每页失败都会单独显示。</p></div>}
      </section>

      <section className="panel web-video-storyboard" aria-labelledby="storyboard-title">
        <header><div><p className="eyebrow">STORYBOARD · HUMAN GATE 2</p><h2 id="storyboard-title">镜头板顺序</h2></div><span>{site.storyboard?.status?.toUpperCase() || "WAITING"}</span></header>
        {shots.length ? <ol>{shots.map((shot, index) => <StoryboardShotCard key={shot.id} shot={shot} index={index} total={shots.length} editable={run.status === "awaiting_storyboard_review" && !action} onMove={(direction) => moveShot(index, direction)} onUpdate={(patch) => updateShot(shot.id, patch)} onPreviewLoaded={(key) => setLoadedPreviews((current) => new Set(current).add(key))} onPreviewFailed={(key) => setLoadedPreviews((current) => { const next = new Set(current); next.delete(key); return next; })} />)}</ol> : <div className="web-video-capture-empty" role="status"><span>⌁</span><h3>镜头板尚未生成</h3><p>区域分析完成后，系统会把页面全景与高清局部组织成候选镜头。</p></div>}
        {site.storyboard ? <dl className="web-video-evidence"><div><dt>Storyboard revision</dt><dd>{site.storyboard.revision || "—"}</dd></div><div><dt>Storyboard SHA-256</dt><dd><code>{site.storyboard.sha256 || "等待清单固化"}</code></dd></div><div><dt>启用镜头</dt><dd>{enabledShots.length} / {shots.length}</dd></div></dl> : null}
        {run.status === "awaiting_storyboard_review" ? <div className="web-video-site-gate"><p className="web-video-storyboard-tip">先调整镜头顺序、运动和转场，再批准生成。运动会围绕系统识别的关键区域缓慢执行。</p><label className="field" htmlFor="web-video-site-review-comment"><span>审核备注（可选）</span><textarea id="web-video-site-review-comment" className="textarea" value={comment} maxLength={1000} disabled={Boolean(action)} onChange={(event) => setComment(event.target.value)} /></label>{!canReviewStoryboard ? <p role="status">至少启用一个镜头，且所有启用镜头必须在本页成功加载预览；还需要有效 revision 和 64 位镜头板哈希。</p> : null}<div><button className="button" type="button" disabled={!canReviewStoryboard} onClick={() => void review("storyboard", "approve")}>批准剪辑方案并生成视频</button><button className="button-ghost" type="button" disabled={!canReviewStoryboard} onClick={() => void review("storyboard", "request_changes")}>要求重新截图并重建镜头板</button><button className="button-danger" type="button" disabled={!canReviewStoryboard} onClick={() => void review("storyboard", "reject")}>拒绝并停止</button></div></div> : <p className="web-video-site-locked">镜头启停、顺序、运动与转场只在镜头板审核阶段可编辑；重截或重建会产生新 revision/hash。</p>}
      </section>
    </>
  );
}
