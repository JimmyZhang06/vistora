"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type WebpageVideoRun, type WebpageVideoSitePlan } from "@/lib/api";
import { buildWebpageVideoShotEvidence } from "@/lib/webpage-video-evidence";

type State = "loading" | "ready" | "error";

function shortHash(value: string | null): string {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "待补";
}

function safeSourceUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

export function WebpageVideoEvidenceChain({ run }: { run: WebpageVideoRun }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [site, setSite] = useState<WebpageVideoSitePlan>();
  const [state, setState] = useState<State>("loading");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    const result = await adapter.getWebpageVideoSite(run.id);
    if (!result.ok) {
      setState("error");
      setError(result.error.message || "无法读取镜头来源证据。");
      return;
    }
    setSite(result.data);
    setState("ready");
  }, [adapter, run.id]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const chains = useMemo(() => buildWebpageVideoShotEvidence(run, site), [run, site]);
  const complete = chains.filter((item) => item.complete).length;

  return (
    <section className="panel web-video-evidence-chain" aria-labelledby="shot-evidence-title">
      <header>
        <div><p className="eyebrow">SHOT PROVENANCE</p><h2 id="shot-evidence-title">镜头证据链</h2></div>
        <span data-complete={state === "ready" && chains.length > 0 && complete === chains.length || undefined}>{state === "ready" ? `${complete}/${chains.length}` : "…"}</span>
      </header>
      <p>每个已启用镜头关联到原网页、视觉内容哈希、范围版本、镜头板版本和最终成片哈希。</p>
      {state === "loading" ? <div className="web-video-evidence-chain-state" role="status">正在整理镜头与来源证据…</div> : null}
      {state === "error" ? <div className="web-video-evidence-chain-state" role="alert"><span>{error}</span><button className="button-ghost" type="button" onClick={() => void load()}>重试</button></div> : null}
      {state === "ready" && !chains.length ? <div className="web-video-evidence-chain-state" role="status">镜头板尚无已启用镜头，当前不能形成完整证据链。</div> : null}
      {chains.length ? <ol className="web-video-evidence-chain-list">
        {chains.map((item) => {
          const href = safeSourceUrl(item.sourceUrl);
          return <li key={item.shotId} data-complete={item.complete || undefined}>
            <div className="web-video-evidence-chain-index"><span>{String(item.order).padStart(2, "0")}</span><i aria-hidden="true">→</i></div>
            <div className="web-video-evidence-chain-copy">
              <header><strong>{item.label}</strong><small>{item.complete ? "证据完整" : `待补 ${item.missing.length} 项`}</small></header>
              <p>{href ? <a href={href} target="_blank" rel="noreferrer noopener">{item.pageTitle || href}</a> : item.pageTitle || item.sourceUrl || "来源 URL 待补"}</p>
              <dl>
                <div><dt>视觉来源</dt><dd><code>{shortHash(item.regionSha256 || item.pageCaptureSha256)}</code></dd></div>
                <div><dt>范围版本</dt><dd><code>{shortHash(item.scopeSha256)}</code></dd></div>
                <div><dt>镜头板</dt><dd><code>{shortHash(item.storyboardSha256)}</code></dd></div>
                <div><dt>最终成片</dt><dd><code>{shortHash(item.outputSha256)}</code></dd></div>
              </dl>
            </div>
          </li>;
        })}
      </ol> : null}
      <small className="web-video-evidence-chain-note">当前版本证明视觉镜头来源；逐句旁白与网页原文的声明级映射仍属于下一阶段研发范围。</small>
    </section>
  );
}
