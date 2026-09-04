"use client";

import { FormEvent, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type WebpageVideoPilotFeedbackSaveRequest,
  type WebpageVideoRun,
} from "@/lib/api";

function idempotencyKey(): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `webpage-pilot-feedback:${suffix}`;
}

export function WebpageVideoPilotPanel({ run, onSaved }: { run: WebpageVideoRun; onSaved: () => void }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const saved = run.pilotFeedback;
  const [customerSegment, setCustomerSegment] = useState(saved?.customerSegment ?? "");
  const [baselineMinutes, setBaselineMinutes] = useState(String(saved?.baselineMinutes ?? 120));
  const [assistedMinutes, setAssistedMinutes] = useState(String(saved?.assistedMinutes ?? 30));
  const [revisionCount, setRevisionCount] = useState(String(saved?.revisionCount ?? 0));
  const [outcome, setOutcome] = useState<WebpageVideoPilotFeedbackSaveRequest["outcome"]>(saved?.outcome ?? "evaluating");
  const [satisfactionScore, setSatisfactionScore] = useState(saved?.satisfactionScore ? String(saved.satisfactionScore) : "");
  const [willingnessToPayHkd, setWillingnessToPayHkd] = useState(saved?.willingnessToPayHkd !== undefined ? String(saved.willingnessToPayHkd) : "");
  const [notes, setNotes] = useState(saved?.notes ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("正在保存试点证据…");
    const result = await adapter.saveWebpageVideoPilotFeedback(run.id, {
      customerSegment: customerSegment.trim(),
      baselineMinutes: Number(baselineMinutes),
      assistedMinutes: Number(assistedMinutes),
      revisionCount: Number(revisionCount),
      outcome,
      satisfactionScore: satisfactionScore ? Number(satisfactionScore) : undefined,
      willingnessToPayHkd: willingnessToPayHkd ? Number(willingnessToPayHkd) : undefined,
      notes: notes.trim() || undefined,
      expectedRevision: saved?.revision ?? 0,
    }, idempotencyKey());
    setSubmitting(false);
    if (!result.ok) {
      setMessage(result.error.status === 412
        ? "反馈已被其他会话更新。请刷新任务后再修改，当前内容未覆盖新版本。"
        : result.error.message || "无法保存试点反馈。");
      return;
    }
    setMessage("试点证据已保存，并会进入申请证据报告。正在刷新最新版本…");
    onSaved();
  }

  return (
    <section className="panel web-video-pilot" aria-labelledby="pilot-feedback-title">
      <header><div><p className="eyebrow">PILOT OUTCOME</p><h2 id="pilot-feedback-title">试点成效证据</h2></div><span>{saved ? `REV ${saved.revision}` : "NEW"}</span></header>
      <p>记录匿名化客户类型和前后耗时即可；不要填写姓名、邮箱或其他个人资料。</p>
      {saved ? <dl className="web-video-pilot-metrics"><div><dt>节省时间</dt><dd>{saved.savedMinutes} 分钟</dd></div><div><dt>时间降幅</dt><dd>{saved.timeReductionPercent}%</dd></div><div><dt>采用状态</dt><dd>{saved.outcome}</dd></div></dl> : null}
      <form onSubmit={submit}>
        <div className="web-video-pilot-grid">
          <label className="field"><span>客户类型（不含名称）</span><input className="input" required maxLength={120} value={customerSegment} onChange={(event) => setCustomerSegment(event.target.value)} placeholder="例如：香港中小型电商团队" /></label>
          <label className="field"><span>试点结果</span><select className="select" value={outcome} onChange={(event) => setOutcome(event.target.value as typeof outcome)}><option value="evaluating">评估中</option><option value="adopted">已采用</option><option value="rejected">未采用</option></select></label>
          <label className="field"><span>原流程耗时（分钟）</span><input className="input" type="number" min={1} max={10080} required value={baselineMinutes} onChange={(event) => setBaselineMinutes(event.target.value)} /></label>
          <label className="field"><span>使用后耗时（分钟）</span><input className="input" type="number" min={1} max={10080} required value={assistedMinutes} onChange={(event) => setAssistedMinutes(event.target.value)} /></label>
          <label className="field"><span>人工修改轮次</span><input className="input" type="number" min={0} max={100} required value={revisionCount} onChange={(event) => setRevisionCount(event.target.value)} /></label>
        </div>
        <details className="web-video-pilot-optional">
          <summary>补充可选商业信号</summary>
          <div className="web-video-pilot-grid">
            <label className="field"><span>满意度（1–5）</span><input className="input" type="number" min={1} max={5} value={satisfactionScore} onChange={(event) => setSatisfactionScore(event.target.value)} /></label>
            <label className="field"><span>付费意愿 HKD</span><input className="input" type="number" min={0} max={1000000} value={willingnessToPayHkd} onChange={(event) => setWillingnessToPayHkd(event.target.value)} /></label>
          </div>
          <label className="field"><span>观察备注</span><textarea className="textarea" maxLength={2000} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="记录使用场景、主要收益或未采用原因；不要写个人资料。" /></label>
        </details>
        <div className="web-video-pilot-actions"><button className="button" type="submit" disabled={submitting}>{submitting ? "保存中…" : saved ? "更新试点证据" : "保存试点证据"}</button>{message ? <p role="status">{message}</p> : null}</div>
      </form>
    </section>
  );
}
