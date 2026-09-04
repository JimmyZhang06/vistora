"use client";

import Link from "next/link";
import Image from "next/image";
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createFrameFactoryAdapter, type Asset, type AssetPoster, type AssetPreview, type AssetSegment } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { useConfirmDialog } from "@/components/confirm-dialog";
import { UiSelect } from "@/components/ui-select";

function formatBytes(bytes = 0) {
  if (!bytes) return "0 B";
  const unit = Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / (1024 ** unit)).toFixed(unit ? 1 : 0)} ${["B", "KB", "MB", "GB"][unit]}`;
}

function formatTime(milliseconds = 0) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

const boundaryReasonLabels: Record<string, string> = {
  asset_start: "素材起点",
  asset_end: "素材终点",
  sentence_end: "句意结束",
  estimated_transcript_boundary: "估算文本边界（不作为安全切点）",
  silence: "静音间隙",
  shot_boundary: "镜头变化",
  maximum_duration_word_gap: "词间时长保护",
  maximum_duration_guard: "最大时长保护",
};

const processingStages = [
  ["file_detection", "文件识别"], ["malware_scan", "安全扫描"], ["ffprobe", "媒体信息"],
  ["fingerprint", "内容指纹"], ["preview", "预览转码"], ["keyframes", "关键帧提取"],
  ["subtitle_extraction", "字幕提取"], ["audio_transcription", "音频转文字"],
  ["temporal_alignment", "字幕与镜头对齐"], ["shot_segmentation", "镜头切片"],
  ["visual_analysis", "画面理解"], ["normalize_tags", "标签入库"], ["rights_gate", "版权与质量门禁"],
] as const;

function stageLabel(stage?: string) {
  return processingStages.find(([id]) => id === stage)?.[1] ?? (stage || "等待 Worker 接收");
}

function cutLabel(segment: AssetSegment) {
  if (segment.semanticComplete) return "语义完整";
  if (isEvidenceBackedSafe(segment)) return "音频安全";
  return "候选切点";
}

function isEvidenceBackedSafe(segment: AssetSegment) {
  return segment.cutSafe && segment.boundaryReasons.some((reason) => ["sentence_end", "silence", "maximum_duration_word_gap"].includes(reason));
}

export function AssetDetail({ libraryId, assetId }: { libraryId: string; assetId: string }) {
  const { confirm, confirmationDialog } = useConfirmDialog();
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [asset, setAsset] = useState<Asset | null>(null);
  const [preview, setPreview] = useState<AssetPreview | null>(null);
  const [poster, setPoster] = useState<AssetPoster | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [segments, setSegments] = useState<AssetSegment[]>([]);
  const [segmentFilter, setSegmentFilter] = useState<"all" | "semantic" | "safe" | "candidate">("all");
  const [visibleSegments, setVisibleSegments] = useState(120);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [copyrightStatus, setCopyrightStatus] = useState<Asset["copyrightStatus"]>("unknown");
  const [tags, setTags] = useState("");
  const [reviewComment, setReviewComment] = useState("");
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [rightsEvidence, setRightsEvidence] = useState("");
  const mutationKeys = useRef(new Map<string, string>());

  function mutationKey(action: string, payload: unknown = null) {
    const signature = `${action}:${JSON.stringify(payload)}`;
    const key = mutationKeys.current.get(signature) ?? `${action}:${globalThis.crypto.randomUUID()}`;
    mutationKeys.current.set(signature, key);
    return { key, signature };
  }

  const load = useCallback(async () => {
    const [assetResult, previewResult, posterResult, segmentResult] = await Promise.all([
      adapter.getAsset(assetId), adapter.getAssetPreview(assetId), adapter.getAssetPoster(assetId), adapter.listAssetSegments(assetId),
    ]);
    if (!assetResult.ok) {
      setError(assetResult.error.message);
      setLoading(false);
      return;
    }
    setAsset(assetResult.data);
    setTitle(assetResult.data.title);
    setDescription(assetResult.data.description);
    setCopyrightStatus(assetResult.data.copyrightStatus);
    setTags(assetResult.data.tags.map((tag) => tag.name).join("，"));
    setPreview(previewResult.ok ? previewResult.data : null);
    setPoster(posterResult.ok ? posterResult.data : null);
    setPreviewError(previewResult.ok ? "" : previewResult.error.message);
    setSegments(segmentResult.ok ? segmentResult.data : []);
    setVisibleSegments(120);
    setError("");
    setLoading(false);
  }, [adapter, assetId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const sync = () => setOffline(!navigator.onLine);
    window.addEventListener("online", sync); window.addEventListener("offline", sync); sync();
    return () => { window.removeEventListener("online", sync); window.removeEventListener("offline", sync); };
  }, []);

  useEffect(() => {
    if (!asset || !["processing"].includes(asset.status) || ["completed", "failed"].includes(asset.analysisStatus)) return;
    const timer = window.setInterval(() => {
      void adapter.getAsset(assetId).then((result) => {
        if (!result.ok) return;
        setAsset(result.data);
        if (["completed", "failed"].includes(result.data.analysisStatus)) void load();
      });
    }, 4000);
    return () => window.clearInterval(timer);
  }, [adapter, asset, assetId, load]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!asset || offline) return;
    setBusy(true); setNotice("正在保存…");
    const assertedRights = ["owned", "licensed", "public_domain"].includes(copyrightStatus);
    if (copyrightStatus !== asset.copyrightStatus && assertedRights && (!rightsConfirmed || !rightsEvidence.trim())) {
      setBusy(false);
      setNotice("变更为自有、已授权或公共领域时，必须确认权利并填写凭证说明。");
      return;
    }
    const patch = {
      title: title.trim(), description: description.trim(),
      tags: tags.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean),
      ...(copyrightStatus !== asset.copyrightStatus ? {
        copyrightStatus,
        ...(assertedRights ? { rightsConfirmed: true, rightsEvidence: { note: rightsEvidence.trim() } } : {}),
      } : {}),
    };
    const operation = mutationKey("update-asset", patch);
    const result = await adapter.updateAsset(assetId, asset.revision, patch, operation.key);
    setBusy(false);
    if (!result.ok) { setNotice(result.error.message); return; }
    mutationKeys.current.delete(operation.signature);
    setAsset(result.data); setNotice("素材信息已保存。可用状态仍由服务端门禁决定。");
  }

  async function review(decision: "approve" | "reject") {
    if (!asset || offline) return;
    if (decision === "reject" && !await confirm({ title: "拒绝并隔离素材", description: "该素材会离开可用集合，但审核记录会被保留。", confirmLabel: "确认拒绝", tone: "danger" })) return;
    setBusy(true); setNotice("正在提交审核决定…");
    const reviewRequest = { decision, comment: reviewComment };
    const operation = mutationKey("review-asset", reviewRequest);
    const result = await adapter.reviewAsset(assetId, asset.revision, reviewRequest, operation.key);
    setBusy(false);
    if (!result.ok) { setNotice(result.error.message); return; }
    mutationKeys.current.delete(operation.signature);
    setAsset(result.data); setReviewComment(""); setNotice(decision === "approve" ? "审核已通过；服务端仍会核验版权、扫描和分析门禁。" : "素材已拒绝。");
  }

  async function reanalyze() {
    if (!asset || offline) return;
    setBusy(true); setNotice("正在创建重新分析任务…");
    const operation = mutationKey("reanalyze-asset");
    const result = await adapter.reanalyzeAsset(assetId, asset.revision, operation.key);
    setBusy(false);
    if (result.ok) mutationKeys.current.delete(operation.signature);
    setNotice(result.ok ? `已创建后台任务 ${result.data.id}，状态：${result.data.status}。` : result.error.message);
  }

  async function disable() {
    if (!asset || offline || !await confirm({ title: "禁用素材", description: "素材会被软删除并从可用集合移除，历史引用仍保留且稍后可以恢复。", confirmLabel: "确认禁用", tone: "danger" })) return;
    setBusy(true); setNotice("正在禁用…");
    const operation = mutationKey("delete-asset");
    const result = await adapter.deleteAsset(assetId, asset.revision, operation.key);
    setBusy(false);
    if (!result.ok) { setNotice(result.error.message); return; }
    mutationKeys.current.delete(operation.signature);
    setNotice("素材已软删除。"); await load();
  }

  async function restore() {
    if (!asset || offline || !await confirm({ title: "恢复素材", description: "恢复后仍需重新通过扫描、分析、版权和人工审核门禁。", confirmLabel: "确认恢复" })) return;
    setBusy(true); setNotice("正在恢复…");
    const operation = mutationKey("restore-asset");
    const result = await adapter.restoreAsset(assetId, asset.revision, operation.key);
    setBusy(false);
    if (!result.ok) { setNotice(result.error.message); return; }
    mutationKeys.current.delete(operation.signature);
    setAsset(result.data); setNotice("素材已恢复，服务端正在重新判定可用状态。");
  }

  if (loading) return <div className="page"><div className="asset-detail-skeleton" aria-label="正在加载素材详情" /></div>;
  if (error || !asset) return <div className="page"><StatePanel code="ERR" title="无法打开素材" description={error || "素材不存在"} error><Link className="button-secondary" href={`/assets/${encodeURIComponent(libraryId)}`}>返回素材库</Link></StatePanel></div>;

  const analysis = asset.analysis;
  const filteredSegments = segments.filter((segment) => segmentFilter === "all"
    || (segmentFilter === "semantic" && segment.semanticComplete)
    || (segmentFilter === "safe" && isEvidenceBackedSafe(segment) && !segment.semanticComplete)
    || (segmentFilter === "candidate" && !segment.semanticComplete && !isEvidenceBackedSafe(segment)));
  const renderedSegments = filteredSegments.slice(0, visibleSegments);
  const semanticCount = segments.filter((segment) => segment.semanticComplete).length;
  const safeCount = segments.filter((segment) => isEvidenceBackedSafe(segment) && !segment.semanticComplete).length;
  const candidateCount = segments.length - semanticCount - safeCount;
  const completedStageCount = Math.min(processingStages.length, asset.processing?.completedStages.length ?? (asset.analysisStatus === "completed" ? processingStages.length : 0));
  return <div className="page page--wide">
    <PageHeading eyebrow="05 / ASSET DETAIL" title={asset.title} description={`素材 ID ${asset.id}`} actions={<Link className="button-secondary" href={`/assets/${encodeURIComponent(libraryId)}`}>返回列表</Link>} />
    {offline ? <div className="connection-banner" role="status">离线状态：当前详情只读，恢复连接后可保存或审核。</div> : null}
    {notice ? <p className="operation-notice" role="status" aria-live="polite">{notice}</p> : null}

    <div className="asset-detail-layout">
      <main className="asset-detail-main">
        {asset.status === "processing" || asset.processing ? <section className="panel asset-processing-panel" aria-labelledby="processing-heading" aria-live="polite">
          <div className="section-heading"><div><p className="eyebrow">PROCESSING</p><h2 id="processing-heading">素材处理进度</h2></div><Badge tone={asset.analysisStatus === "failed" ? "warning" : asset.analysisStatus === "completed" ? "success" : "accent"}>{asset.analysisStatus === "failed" ? "处理失败" : asset.analysisStatus === "completed" ? "13 / 13 · 处理完成" : `${completedStageCount} / ${processingStages.length} · ${stageLabel(asset.processing?.currentStage)}`}</Badge></div>
          <div className="asset-processing-track">{processingStages.map(([id, label], index) => {
            const completed = asset.processing?.completedStages.includes(id) || asset.analysisStatus === "completed";
            const active = asset.processing?.currentStage === id && !completed;
            return <div key={id} data-state={completed ? "completed" : active ? "active" : "pending"}><span>{String(index + 1).padStart(2, "0")}</span><strong>{label}</strong></div>;
          })}</div>
          <p className="muted">{asset.analysisStatus === "completed" ? "全部处理已完成，镜头、转写、标签和可用性门禁均已持久化。" : asset.processing?.error ? `失败原因：${asset.processing.error}` : asset.processing?.jobStatus === "queued" ? "文件已上传，任务正在等待 Worker 接收。" : asset.processing?.currentStage ? `正在执行：${stageLabel(asset.processing.currentStage)}。页面每 4 秒同步一次真实状态。` : "文件已上传，正在创建分析执行记录。"}</p>
        </section> : null}
        <section className="asset-preview-panel" aria-label="素材预览">
          {preview?.url && !previewError && asset.kind === "video" ? <><video key={preview.url} controls playsInline preload="metadata" poster={poster?.url} src={preview.url} onError={() => setPreviewError("浏览器无法读取这条视频，请刷新签名地址或重新生成预览。")}><track default kind="captions" src="data:text/vtt,WEBVTT" srcLang="zh" label="无可用字幕" />浏览器不支持视频预览。</video>{preview.isFallback ? <p className="asset-preview-note" role="status">预览转码尚未生成，当前通过受控签名地址播放原片，首次加载和拖动可能较慢。</p> : null}</> : null}
          {preview?.url && !previewError && asset.kind === "image" ? <Image unoptimized src={preview.url} alt={asset.title} width={asset.file?.width ?? 1600} height={asset.file?.height ?? 900} onError={() => setPreviewError("浏览器无法读取这张图片，请刷新签名地址或重新生成预览。")} /> : null}
          {previewError ? <div><span aria-hidden="true">PREVIEW</span><p>{previewError}</p><button className="button-secondary button-small" type="button" onClick={() => { setPreviewError(""); void load(); }}>刷新播放地址</button></div> : null}
          {!preview?.url && !previewError ? <div><span aria-hidden="true">PREVIEW</span><p>预览正在生成；素材达到 ready 后可通过受控原片地址临时播放。</p></div> : null}
        </section>

        <section className="panel asset-section" aria-labelledby="timeline-heading">
          <div className="section-heading"><div><p className="eyebrow">SHOT TIMELINE</p><h2 id="timeline-heading">镜头时间线</h2></div><Badge>{segments.length} 段</Badge></div>
          {segments.length ? <>
            <div className="segment-summary" aria-label="切点质量摘要">
              <div><strong>{semanticCount}</strong><span>语义完整</span></div>
              <div><strong>{safeCount}</strong><span>音频安全</span></div>
              <div><strong>{candidateCount}</strong><span>仅候选，需复核</span></div>
              <UiSelect ariaLabel="筛选镜头切点" value={segmentFilter} onChange={(value) => { setSegmentFilter(value as typeof segmentFilter); setVisibleSegments(120); }}>
                <option value="all">全部切片</option><option value="semantic">语义完整</option><option value="safe">音频安全</option><option value="candidate">候选切点</option>
              </UiSelect>
            </div>
            {candidateCount ? <p className="segment-guidance">候选切点只有画面变化或时长保护证据，不代表对白已说完；自动剪辑会优先使用带字幕、ASR 或静音证据的切点。</p> : null}
            <ol className="segment-timeline">{renderedSegments.map((segment) => <li key={segment.id} data-cut-quality={segment.semanticComplete ? "semantic" : isEvidenceBackedSafe(segment) ? "safe" : "candidate"}><time>{formatTime(segment.startMs)} — {formatTime(segment.endMs)}</time><div><strong>{segment.description}</strong>{segment.transcript ? <p>对白：{segment.transcript}</p> : <p>该段暂无可用对白时间轴。</p>}<p>{[segment.sceneType, segment.shotType, segment.action, ...segment.keywords.slice(0, 3)].filter(Boolean).join(" · ")}</p><p>依据：{segment.boundaryReasons.map((reason) => boundaryReasonLabels[reason] ?? reason).join("、") || "未记录"}{segment.boundaryScore !== undefined ? ` · ${Math.round(segment.boundaryScore * 100)}%` : ""}</p></div><span>{cutLabel(segment)}</span></li>)}</ol>
            {renderedSegments.length < filteredSegments.length ? <button className="button-secondary segment-load-more" type="button" onClick={() => setVisibleSegments((current) => current + 120)}>继续显示 120 段（剩余 {(filteredSegments.length - renderedSegments.length).toLocaleString("zh-CN")}）</button> : null}
            {!filteredSegments.length ? <p className="muted">当前筛选下没有切片。</p> : null}
          </> : <p className="muted">尚无镜头分析数据。可重新分析，任务进度以服务端返回为准。</p>}
        </section>

        <section className="panel asset-section" aria-labelledby="analysis-heading">
          <div className="section-heading"><div><p className="eyebrow">AI ANALYSIS</p><h2 id="analysis-heading">AI 分析</h2></div><button className="button-secondary button-small" type="button" onClick={() => void reanalyze()} disabled={busy || offline}>重新分析</button></div>
          {analysis ? <><p className="analysis-summary">{analysis.summary || "分析未提供摘要。"}</p><dl className="analysis-facts"><div><dt>模型</dt><dd>{analysis.provider} / {analysis.model}</dd></div><div><dt>状态</dt><dd>{analysis.status}</dd></div><div><dt>置信度</dt><dd>{analysis.confidence !== undefined ? `${Math.round(analysis.confidence * 100)}%` : "—"}</dd></div><div><dt>水印 / 内嵌文字</dt><dd>{analysis.hasWatermark ? "有水印" : "无水印"} / {analysis.hasEmbeddedText ? "有文字" : "无文字"}</dd></div></dl><div className="analysis-groups">{[["人物", analysis.people], ["地点", analysis.locations], ["场景", analysis.sceneTypes], ["动作", analysis.actions], ["情绪", analysis.moods], ["关键词", analysis.keywords]].map(([label, values]) => <div key={label as string}><strong>{label as string}</strong><p>{(values as string[]).join("、") || "—"}</p></div>)}</div></> : <p className="muted">暂无已完成的 AI 分析。</p>}
        </section>

        <section className="panel asset-section" aria-labelledby="history-heading">
          <div className="section-heading"><div><p className="eyebrow">HISTORY</p><h2 id="history-heading">使用与审核记录</h2></div></div>
          <div className="asset-history-grid"><div><h3>使用记录</h3>{asset.usageHistory.length ? <ol>{asset.usageHistory.map((item) => <li key={item.id}><span>{item.runTopic || item.runId || "创作项目"}</span><time>{new Date(item.usedAt).toLocaleString("zh-CN")}</time></li>)}</ol> : <p>尚未被创作项目引用。</p>}</div><div><h3>审核历史</h3>{asset.reviewHistory.length ? <ol>{asset.reviewHistory.map((item) => <li key={item.id}><span>{item.decision} · {item.actorName || "系统用户"}<small>{item.comment}</small></span><time>{new Date(item.createdAt).toLocaleString("zh-CN")}</time></li>)}</ol> : <p>尚无审核记录。</p>}</div></div>
        </section>
      </main>

      <aside className="asset-detail-aside">
        <section className="panel gate-panel"><p className="eyebrow">READINESS GATES</p><h2>可用性门禁</h2><ul><li data-pass={asset.copyrightStatus !== "unknown" && asset.copyrightStatus !== "restricted"}>版权：{asset.copyrightStatus}</li><li data-pass={asset.file?.scanStatus === "clean"}>安全扫描：{asset.file?.scanStatus ?? "pending"}</li><li data-pass={asset.analysisStatus === "completed"}>AI 分析：{stageLabel(asset.processing?.currentStage)} · {asset.analysisStatus}</li><li data-pass={asset.reviewStatus === "approved"}>人工审核：{asset.reviewStatus}</li></ul><Badge tone={asset.status === "ready" ? "success" : asset.status === "quarantined" ? "warning" : "neutral"}>{asset.deletedAt ? "已删除" : asset.status}</Badge><p>前端不会直接设置 ready；最终状态由服务端统一判定。</p></section>

        <form className="panel asset-edit-form" onSubmit={save} aria-label="编辑素材信息"><p className="eyebrow">METADATA</p><h2>素材信息</h2><label className="field"><span className="field-label">标题</span><input className="input" required maxLength={240} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label className="field"><span className="field-label">描述</span><textarea className="textarea" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label><div className="field"><span className="field-label">版权状态</span><UiSelect ariaLabel="版权状态" value={copyrightStatus} onChange={(value) => { setCopyrightStatus(value as Asset["copyrightStatus"]); setRightsConfirmed(false); }}><option value="unknown">待补充</option><option value="owned">自有素材</option><option value="licensed">已授权</option><option value="public_domain">公共领域</option><option value="restricted">受限</option></UiSelect></div>{copyrightStatus !== asset.copyrightStatus && ["owned", "licensed", "public_domain"].includes(copyrightStatus) ? <><label className="field"><span className="field-label">权利凭证说明</span><textarea className="textarea" required maxLength={1000} value={rightsEvidence} onChange={(event) => setRightsEvidence(event.target.value)} placeholder="授权合同、来源页面或公共领域依据" /></label><label className="select-page"><input type="checkbox" checked={rightsConfirmed} onChange={(event) => setRightsConfirmed(event.target.checked)} />我确认拥有使用该素材的合法权利</label></> : null}<label className="field"><span className="field-label">标签</span><textarea className="textarea asset-tags-editor" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="人物，场景，动作" /></label><button className="button" type="submit" disabled={busy || offline}>保存信息</button></form>

        <section className="panel asset-tech"><p className="eyebrow">TECHNICAL</p><h2>技术信息</h2><dl><div><dt>类型</dt><dd>{asset.file?.mediaType || asset.kind}</dd></div><div><dt>尺寸</dt><dd>{asset.file?.width && asset.file.height ? `${asset.file.width} × ${asset.file.height}` : "—"}</dd></div><div><dt>时长</dt><dd>{asset.file?.durationMs ? formatTime(asset.file.durationMs) : "—"}</dd></div><div><dt>大小</dt><dd>{formatBytes(asset.file?.byteSize)}</dd></div><div><dt>SHA-256</dt><dd title={asset.file?.contentHash}>{asset.file?.contentHash ? `${asset.file.contentHash.slice(0, 12)}…` : "—"}</dd></div></dl><h3>版权来源</h3><dl><div><dt>来源</dt><dd>{asset.source?.provider || asset.source?.type || "—"}</dd></div><div><dt>许可</dt><dd>{asset.source?.license || "—"}</dd></div><div><dt>署名</dt><dd>{asset.source?.attribution || "—"}</dd></div>{asset.source?.locator ? <div><dt>原始地址</dt><dd><a href={asset.source.locator} target="_blank" rel="noreferrer">打开来源</a></dd></div> : null}</dl></section>

        <section className="panel review-panel"><p className="eyebrow">HUMAN REVIEW</p><h2>人工审核</h2><textarea className="textarea" value={reviewComment} onChange={(event) => setReviewComment(event.target.value)} placeholder="审核备注（可选）" /><div className="button-row"><button className="button" type="button" onClick={() => void review("approve")} disabled={busy || offline}>审核通过</button><button className="button-secondary" type="button" onClick={() => void review("reject")} disabled={busy || offline}>拒绝</button></div></section>

        <section className="panel danger-zone">
          <div className="danger-zone-layout">
            <div className="danger-zone-copy">
              <p className="eyebrow">DANGER ZONE</p>
              <h2>{asset.deletedAt ? "恢复素材" : "禁用素材"}</h2>
              <p>{asset.deletedAt ? "恢复不会自动跳过扫描、分析或版权门禁。" : "禁用采用软删除，历史引用保持可审计。"}</p>
            </div>
            <button className="button-secondary danger-zone-action" type="button" onClick={() => void (asset.deletedAt ? restore() : disable())} disabled={busy || offline}>{asset.deletedAt ? "恢复素材" : "确认禁用"}</button>
          </div>
        </section>
      </aside>
    </div>
    {confirmationDialog}
  </div>;
}
