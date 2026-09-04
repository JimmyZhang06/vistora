"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type WebpageVideoPilotSummary } from "@/lib/api";

type State = "loading" | "ready" | "error";

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "short" }).format(date);
}

function formatNumber(value?: number, suffix = ""): string {
  return value === undefined ? "待补" : `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value)}${suffix}`;
}

const outcomeLabel = { adopted: "已采用", evaluating: "评估中", rejected: "未采用" } as const;

export function ApplicationEvidenceDashboard() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [state, setState] = useState<State>("loading");
  const [summary, setSummary] = useState<WebpageVideoPilotSummary>();
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    setError("");
    const result = await adapter.getWebpageVideoPilotSummary();
    if (!result.ok) {
      setState("error");
      setError(result.error.message || "无法读取申请试点证据。");
      return;
    }
    setSummary(result.data);
    setState("ready");
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  return (
    <div className="page page--wide application-evidence-page">
      <header className="application-evidence-head">
        <div><p className="eyebrow">APPLICATION EVIDENCE · PILOT OUTCOMES</p><h1 tabIndex={-1}>申请证据中心</h1><p>把匿名化客户试点、时间节省、采用结果和付费信号汇总为评委可打印的一页报告。</p></div>
        <div className="application-evidence-actions print-hidden"><Link className="button-ghost" href="/create/application-demo">新增申请演示</Link><button className="button" type="button" disabled={state !== "ready"} onClick={() => window.print()}>打印 / 保存 PDF</button></div>
      </header>

      {state === "loading" ? <section className="panel application-evidence-state" role="status"><span>正在汇总工作区试点证据…</span></section> : null}
      {state === "error" ? <section className="panel application-evidence-state" role="alert"><span>{error}</span><button className="button-ghost" type="button" onClick={() => void load()}>重新读取</button></section> : null}
      {state === "ready" && summary ? <>
        <section className="application-evidence-verdict" data-ready={summary.pilotTargetMet || undefined}>
          <div><span>{summary.pilotTargetMet ? "READY" : "BUILDING"}</span><strong>{summary.pilotTargetMet ? "已达到建议的最低试点数量" : `还需 ${Math.max(0, summary.recommendedMinimumPilots - summary.totalRecords)} 个真实试点`}</strong></div>
          <p>建议门槛是申请准备目标，不代表 HKSTP 官方评分或录取保证。所有数据均为团队录入，未经 Vistora 独立验证。</p>
        </section>

        <section className="application-evidence-metrics" aria-label="试点核心指标">
          <article><span>真实试点</span><strong>{summary.totalRecords}</strong><small>建议至少 {summary.recommendedMinimumPilots} 个</small></article>
          <article><span>已采用</span><strong>{summary.adoptedCount}</strong><small>评估中 {summary.evaluatingCount} · 未采用 {summary.rejectedCount}</small></article>
          <article><span>累计节省</span><strong>{formatNumber(summary.savedMinutesTotal, " 分钟")}</strong><small>基线 {summary.baselineMinutesTotal} · 辅助后 {summary.assistedMinutesTotal}</small></article>
          <article><span>时间降幅</span><strong>{formatNumber(summary.timeReductionPercent, "%")}</strong><small>按纳入记录的总耗时加权</small></article>
          <article><span>平均满意度</span><strong>{formatNumber(summary.averageSatisfactionScore, "/5")}</strong><small>{summary.satisfactionResponseCount} 个有效回答</small></article>
          <article><span>平均付费意愿</span><strong>{summary.averageWillingnessToPayHkd === undefined ? "待补" : `HK$${formatNumber(summary.averageWillingnessToPayHkd)}`}</strong><small>{summary.willingnessToPayResponseCount} 个有效回答</small></article>
        </section>

        <div className="application-evidence-grid">
          <section className="panel application-evidence-segments" aria-labelledby="segment-title">
            <header><div><p className="eyebrow">SEGMENT SIGNALS</p><h2 id="segment-title">客户细分验证</h2></div><span>{summary.segments.length}</span></header>
            {summary.segments.length ? <table><thead><tr><th>客户细分</th><th>试点</th><th>采用</th><th>平均降幅</th></tr></thead><tbody>{summary.segments.map((segment) => <tr key={segment.customerSegment}><td>{segment.customerSegment}</td><td>{segment.pilotCount}</td><td>{segment.adoptedCount}</td><td>{formatNumber(segment.averageTimeReductionPercent, "%")}</td></tr>)}</tbody></table> : <p className="application-evidence-empty">尚无客户细分数据。完成一个成功 Run 后，在任务详情填写匿名化试点成效。</p>}
          </section>

          <section className="panel application-evidence-method" aria-labelledby="method-title">
            <p className="eyebrow">EVIDENCE METHOD</p><h2 id="method-title">证据口径</h2>
            <ol><li>仅成功成片任务可以录入试点结果。</li><li>耗时降幅按基线总耗时加权计算。</li><li>客户名称不进入本报告，只保留细分。</li><li>每条记录保留 revision 和更新时间。</li></ol>
          </section>
        </div>

        <section className="panel application-evidence-pilots" aria-labelledby="pilot-list-title">
          <header><div><p className="eyebrow">PILOT RECORDS</p><h2 id="pilot-list-title">匿名化试点记录</h2></div><span>{summary.includedRecords}/{summary.totalRecords}</span></header>
          {summary.truncated ? <p className="application-evidence-warning" role="status">数据超过报告上限；以下指标只汇总最近 {summary.includedRecords} 条，不能视为完整总体。</p> : null}
          {summary.items.length ? <ol>{summary.items.map((item) => <li key={item.webpageVideoRunId}><div><strong>{item.customerSegment}</strong><small>{formatDate(item.updatedAt)}</small></div><dl><div><dt>节省</dt><dd>{item.savedMinutes} 分钟</dd></div><div><dt>降幅</dt><dd>{item.timeReductionPercent}%</dd></div><div><dt>修改</dt><dd>{item.revisionCount} 次</dd></div><div><dt>结果</dt><dd>{outcomeLabel[item.outcome]}</dd></div></dl><Link className="print-hidden" href={`/webpage-video/${encodeURIComponent(item.webpageVideoRunId)}`}>查看证据链</Link></li>)}</ol> : <p className="application-evidence-empty">暂无真实试点证据。本页面不会显示示例数字或推算成果。</p>}
        </section>

        <footer className="application-evidence-footer"><span>报告生成时间：{formatDate(summary.generatedAt)}</span><span>Schema {summary.schemaVersion}</span></footer>
      </> : null}
    </div>
  );
}
