"use client";

import Link from "next/link";
import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ComposerOptions,
  type GenerationBatch,
  type GenerationBatchItem,
  type RunComposition,
  type RunStatus,
  type VideoSettings,
} from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";

const PAGE_SIZE = 50;
const MAX_BATCH_SIZE = 5_000;
const defaultVideoSettings: VideoSettings = {
  language: "zh-CN", aspectRatio: "16:9", targetDurationSeconds: 180,
  visibility: "private", autoQualityCheck: true, layout: "full_frame",
  mediaFit: "cover", frameRate: 30,
  subtitles: { enabled: true, position: "bottom", size: "medium", maxLines: 2 },
  assetAcquisition: { enabled: false, sources: ["wikimedia", "youtube", "bilibili"], maxAssets: 3, copyrightStatus: "licensed", rightsConfirmed: false },
};

const runLabels: Record<RunStatus, string> = {
  queued: "排队中", running: "制作中", awaiting_review: "待审核", retrying: "重试中",
  succeeded: "已完成", failed: "失败", cancelled: "已取消",
};

const batchLabels: Record<GenerationBatch["status"], string> = {
  queued: "等待启动", running: "生产中", awaiting_review: "需要审核",
  succeeded: "全部完成", completed_with_errors: "已结束，有异常",
};

function topicsFromText(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function initialComposition(options: ComposerOptions, requestedChannelId = ""): RunComposition {
  const channel = options.channels.find((item) => item.id === requestedChannelId);
  return {
    skillVersionId: channel?.defaultComposition.skillVersionId ?? options.skills[0]?.versionId ?? "",
    pipelineVersionId: channel?.defaultComposition.pipelineVersionId ?? options.pipelines[0]?.id ?? "",
    assetLibraryIds: channel?.defaultComposition.assetLibraryIds ?? [],
    voiceProfileId: channel?.defaultComposition.voiceProfileId ?? options.voiceProfiles[0]?.id,
    renderPresetVersionId: channel?.defaultComposition.renderPresetVersionId ?? options.renderPresets[0]?.id,
  };
}

function BatchProgress({ batch }: { batch: GenerationBatch }) {
  const counts = batch.statusCounts;
  const terminal = counts.succeeded + counts.failed + counts.cancelled;
  const progress = batch.totalCount ? Math.round((terminal / batch.totalCount) * 100) : 0;
  return (
    <div className="batch-progress" aria-label={`完成度 ${progress}%`}>
      <div><span style={{ width: `${progress}%` }} /></div>
      <p><strong>{progress}%</strong><span>{terminal} / {batch.totalCount} 已结束</span></p>
    </div>
  );
}

export function BatchConsole() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const fileRef = useRef<HTMLInputElement>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [batches, setBatches] = useState<GenerationBatch[] | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [selected, setSelected] = useState<GenerationBatch | null>(null);
  const [items, setItems] = useState<GenerationBatchItem[]>([]);
  const [totalItems, setTotalItems] = useState(0);
  const [nextCursor, setNextCursor] = useState<string>();
  const [cursor, setCursor] = useState<string>();
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);
  const [statusFilter, setStatusFilter] = useState<"all" | RunStatus>("all");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [name, setName] = useState("");
  const [topicText, setTopicText] = useState("");
  const [composition, setComposition] = useState<RunComposition>({ skillVersionId: "", pipelineVersionId: "", assetLibraryIds: [] });
  const [channelId, setChannelId] = useState("");
  const [videoSettings, setVideoSettings] = useState<VideoSettings>(defaultVideoSettings);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retryingFailed, setRetryingFailed] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const topics = useMemo(() => topicsFromText(topicText), [topicText]);
  const duplicates = topics.length - new Set(topics).size;

  const loadShell = useCallback(async () => {
    const [sessionResult, batchResult] = await Promise.all([
      adapter.getSession(), adapter.listGenerationBatches(),
    ]);
    if (!sessionResult.ok) { setError(sessionResult.error.message); setLoading(false); return; }
    setWorkspaceId(sessionResult.data.activeWorkspaceId);
    const [optionsResult, preferencesResult] = await Promise.all([
      adapter.getComposerOptions(sessionResult.data.activeWorkspaceId),
      adapter.getCreationPreferences(),
    ]);
    if (optionsResult.ok) {
      const parameters = new URLSearchParams(window.location.search);
      const requestedChannelId = parameters.get("channel") ?? "";
      let composerOptions = optionsResult.data;
      if (requestedChannelId && !composerOptions.channels.some((channel) => channel.id === requestedChannelId)) {
        const channelResult = await adapter.getChannel(requestedChannelId);
        if (
          channelResult.ok
          && channelResult.data.value.workspaceId === sessionResult.data.activeWorkspaceId
          && channelResult.data.value.status === "active"
        ) {
          composerOptions = { ...composerOptions, channels: [channelResult.data.value, ...composerOptions.channels] };
        }
      }
      setOptions(composerOptions);
      setComposition((current) => current.skillVersionId ? current : initialComposition(composerOptions, requestedChannelId));
      setChannelId((current) => current || (composerOptions.channels.some((channel) => channel.id === requestedChannelId) ? requestedChannelId : ""));
      if (parameters.get("create") === "1") setShowCreate(true);
    }
    if (preferencesResult.ok) {
      const preferences = preferencesResult.data.value;
      setVideoSettings((current) => ({
        ...current,
        language: preferences.defaultLanguage,
        aspectRatio: preferences.defaultAspectRatio,
        targetDurationSeconds: preferences.defaultDurationSeconds,
        visibility: preferences.defaultVisibility,
        autoQualityCheck: preferences.autoQualityCheck,
      }));
    }
    if (batchResult.ok) {
      setBatches(batchResult.data);
      setSelectedId((current) => current || batchResult.data[0]?.id || "");
      setError("");
    } else setError(batchResult.error.message);
    setLoading(false);
  }, [adapter]);

  const loadSelected = useCallback(async () => {
    if (!selectedId) { setSelected(null); setItems([]); return; }
    setItemsLoading(true);
    const [batchResult, itemResult] = await Promise.all([
      adapter.getGenerationBatch(selectedId),
      adapter.listGenerationBatchItems(selectedId, {
        status: statusFilter === "all" ? undefined : statusFilter,
        search: appliedSearch || undefined, cursor, limit: PAGE_SIZE,
      }),
    ]);
    if (batchResult.ok) setSelected(batchResult.data);
    if (itemResult.ok) {
      setItems(itemResult.data.data);
      setTotalItems(itemResult.data.totalCount);
      setNextCursor(itemResult.data.nextCursor);
      setError("");
    } else setError(itemResult.error.message);
    setItemsLoading(false);
  }, [adapter, appliedSearch, cursor, selectedId, statusFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadShell(), 0);
    return () => window.clearTimeout(timer);
  }, [loadShell]);
  useEffect(() => {
    const timer = window.setTimeout(() => void loadSelected(), 0);
    return () => window.clearTimeout(timer);
  }, [loadSelected]);
  useEffect(() => {
    const active = batches?.some((batch) => ["queued", "running", "awaiting_review"].includes(batch.status));
    if (!active) return;
    const timer = window.setInterval(() => { void loadShell(); void loadSelected(); }, 5_000);
    return () => window.clearInterval(timer);
  }, [batches, loadSelected, loadShell]);

  function chooseChannel(value: string) {
    setChannelId(value);
    const channel = options?.channels.find((item) => item.id === value);
    if (channel) setComposition(channel.defaultComposition);
  }

  async function importTopics(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const content = await file.text();
    setTopicText(content.split(/\r?\n/).map((line) => line.split(",")[0]?.trim()).filter(Boolean).join("\n"));
    setMessage(`已读取 ${file.name}`);
    event.target.value = "";
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || topics.length === 0 || topics.length > MAX_BATCH_SIZE) {
      setMessage(`请填写批次名称，并提供 1–${MAX_BATCH_SIZE} 个主题。`);
      return;
    }
    if (!composition.skillVersionId || !composition.pipelineVersionId) {
      setMessage("请先选择可用的 Skill 与 Pipeline。");
      return;
    }
    setSubmitting(true);
    setMessage("正在持久化批次并投递生产队列…");
    const result = await adapter.createGenerationBatch({
      workspaceId, name: name.trim(), channelId: channelId || undefined,
      items: topics.map((topic) => ({ topic })), composition, videoSettings,
    }, `web-batch-${crypto.randomUUID()}`);
    setSubmitting(false);
    if (!result.ok) { setMessage(result.error.message); return; }
    setMessage(`批次已创建，共 ${result.data.totalCount} 条。`);
    setName(""); setTopicText(""); setShowCreate(false); setSelectedId(result.data.id);
    setCursor(undefined); setCursorHistory([]); await loadShell();
  }

  function resetPage() { setCursor(undefined); setCursorHistory([]); }
  function applySearch(event: FormEvent) { event.preventDefault(); setAppliedSearch(search.trim()); resetPage(); }

  async function cancelBatch() {
    if (!selected || !window.confirm(`确定停止“${selected.name}”中所有未结束任务吗？`)) return;
    setCancelling(true);
    setMessage("正在逐条持久化取消请求…");
    const result = await adapter.cancelGenerationBatch(
      selected.id,
      `web-cancel-batch-${selected.id}`,
    );
    setCancelling(false);
    if (!result.ok) { setMessage(result.error.message); return; }
    setSelected(result.data);
    setMessage("批次取消请求已保存；正在执行的步骤会在安全边界停止。");
    await loadShell(); await loadSelected();
  }

  async function retryFailed() {
    if (!selected || !selected.statusCounts.failed) return;
    setRetryingFailed(true);
    setMessage("正在把失败项复制到新的生产批次…");
    const result = await adapter.retryFailedGenerationBatch(
      selected.id,
      `web-retry-failed-${selected.id}`,
    );
    setRetryingFailed(false);
    if (!result.ok) { setMessage(result.error.message); return; }
    setSelectedId(result.data.id); resetPage();
    setMessage(`已重开 ${result.data.totalCount} 个失败任务，原批次保持不变。`);
    await loadShell();
  }

  return (
    <div className="page page--wide batch-page">
      <PageHeading eyebrow="03 / BATCH PRODUCTION" title="数千条视频，一张生产表" description="一次锁定 Skill 与制作流程，逐条生成独立 Run；状态、失败与审核节点按批次聚合，列表始终服务端分页。" actions={<button className="button" type="button" onClick={() => setShowCreate((value) => !value)}>{showCreate ? "收起创建器" : "创建生产批次"}</button>} />

      {showCreate ? (
        <form className="panel batch-create" onSubmit={submit}>
          <div className="batch-create-heading"><div><p className="eyebrow">NEW PRODUCTION</p><h2>批量输入创作主题</h2></div><strong>{topics.length.toLocaleString("zh-CN")}<small> / {MAX_BATCH_SIZE.toLocaleString("zh-CN")}</small></strong></div>
          <div className="batch-create-grid">
            <label className="field"><span>批次名称</span><input className="input" value={name} maxLength={200} onChange={(event) => setName(event.target.value)} placeholder="例如：九月知识栏目 · 第一轮" /></label>
            <label className="field"><span>发布频道</span><select className="select" value={channelId} onChange={(event) => chooseChannel(event.target.value)}><option value="">不使用频道默认</option>{options?.channels.map((channel) => <option key={channel.id} value={channel.id}>{channel.name}</option>)}</select></label>
            <label className="field"><span>Skill 版本</span><select className="select" value={composition.skillVersionId} onChange={(event) => setComposition((value) => ({ ...value, skillVersionId: event.target.value }))}>{options?.skills.map((skill) => <option key={skill.versionId} value={skill.versionId}>{skill.skillName} · {skill.version}</option>)}</select></label>
            <label className="field"><span>Pipeline 版本</span><select className="select" value={composition.pipelineVersionId} onChange={(event) => setComposition((value) => ({ ...value, pipelineVersionId: event.target.value }))}>{options?.pipelines.map((pipeline) => <option key={pipeline.id} value={pipeline.id}>{pipeline.name} · {pipeline.version}</option>)}</select></label>
            <label className="field"><span>视频画幅</span><select className="select" value={videoSettings.aspectRatio} onChange={(event) => setVideoSettings((value) => ({ ...value, aspectRatio: event.target.value as VideoSettings["aspectRatio"] }))}><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option><option value="4:3">4:3 经典</option></select></label>
            <label className="field"><span>统一版式</span><select className="select" value={videoSettings.layout} onChange={(event) => setVideoSettings((value) => ({ ...value, layout: event.target.value as VideoSettings["layout"] }))}><option value="full_frame">沉浸全画面</option><option value="editorial">编辑分区</option></select></label>
            <label className="field"><span>目标时长（秒）</span><input className="input" type="number" min={15} max={3600} step={15} value={videoSettings.targetDurationSeconds} onChange={(event) => setVideoSettings((value) => ({ ...value, targetDurationSeconds: Math.min(3600, Math.max(15, Number(event.target.value) || 15)) }))} /></label>
          </div>
          <label className="field batch-topics"><span>主题清单 · 每行一条</span><textarea className="input" value={topicText} onChange={(event) => setTopicText(event.target.value)} placeholder={"AI 如何改变小型制造业\n为什么人们重新开始阅读长文章\n一家公司从危机中恢复的三个决策"} /></label>
          <div className="batch-create-footer"><div><input ref={fileRef} className="sr-only" type="file" accept=".txt,.csv,text/plain,text/csv" onChange={(event) => void importTopics(event)} /><button className="button-ghost button-small" type="button" onClick={() => fileRef.current?.click()}>导入 TXT / CSV</button><span>{duplicates ? `${duplicates} 条重复主题（保留）` : "支持最多 5,000 条，创建后不可改组合"}</span></div><button className="button" type="submit" disabled={submitting || !workspaceId}>{submitting ? "正在创建…" : `创建 ${topics.length || 0} 条任务`}</button></div>
        </form>
      ) : null}

      {options?.channelProblem ? <p className="alert" role="status">频道服务返回：{options.channelProblem.message}。批量生产不会使用本地频道数据替代。</p> : null}

      {message ? <p className="alert" role="status">{message}</p> : null}
      {error ? <p className="alert alert--error" role="alert">{error}</p> : null}
      {loading ? <div className="loading-grid"><div className="loading-card" /><div className="loading-card" /></div> : null}
      {!loading && batches?.length === 0 ? <StatePanel code="00" title="还没有生产批次" description="创建第一个批次；少量试跑确认成片质量后，再扩大到数百或数千条。"><button className="button" type="button" onClick={() => setShowCreate(true)}>创建批次</button></StatePanel> : null}

      {batches?.length ? (
        <div className="batch-console">
          <aside className="batch-list" aria-label="生产批次">
            <div className="batch-list-heading"><span>生产批次</span><strong>{batches.length}</strong></div>
            {batches.map((batch) => <button type="button" className={selectedId === batch.id ? "is-selected" : ""} key={batch.id} onClick={() => { setSelectedId(batch.id); resetPage(); }}><span><strong>{batch.name}</strong><small>{new Date(batch.createdAt).toLocaleString("zh-CN")}</small></span><Badge tone={batch.status === "succeeded" ? "success" : batch.status === "completed_with_errors" ? "warning" : "accent"}>{batchLabels[batch.status]}</Badge><BatchProgress batch={batch} /></button>)}
          </aside>

          <section className="batch-detail" aria-live="polite">
            {selected ? <><header><div><p className="eyebrow">BATCH / {selected.id.slice(0, 8)}</p><h2>{selected.name}</h2></div><div className="button-row"><Badge tone={selected.status === "succeeded" ? "success" : selected.status === "completed_with_errors" ? "warning" : "accent"}>{batchLabels[selected.status]}</Badge>{selected.statusCounts.failed ? <button className="button-ghost button-small" type="button" disabled={retryingFailed} onClick={() => void retryFailed()}>{retryingFailed ? "重开中…" : `重开 ${selected.statusCounts.failed} 个失败项`}</button> : null}{["queued", "running", "awaiting_review"].includes(selected.status) ? <button className="button-ghost button-small" type="button" disabled={cancelling} onClick={() => void cancelBatch()}>{cancelling ? "停止中…" : "停止批次"}</button> : null}</div></header><BatchProgress batch={selected} /><div className="batch-metrics"><span><strong>{selected.statusCounts.running}</strong>制作中</span><span><strong>{selected.statusCounts.awaitingReview}</strong>待审核</span><span><strong>{selected.statusCounts.succeeded}</strong>已完成</span><span><strong>{selected.statusCounts.failed}</strong>失败</span></div></> : null}
            <div className="batch-toolbar"><div className="toolbar">{(["all", "queued", "running", "awaiting_review", "succeeded", "failed", "cancelled"] as const).map((value) => <button type="button" className={statusFilter === value ? "button-secondary button-small" : "button-ghost button-small"} aria-pressed={statusFilter === value} key={value} onClick={() => { setStatusFilter(value); resetPage(); }}>{value === "all" ? "全部" : runLabels[value]}</button>)}</div><form onSubmit={applySearch}><input className="input" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索主题" aria-label="搜索批次中的主题" /><button className="button-ghost button-small">搜索</button></form></div>
            <div className="batch-table-wrap"><table className="batch-table"><thead><tr><th>序号</th><th>主题</th><th>状态</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{itemsLoading ? <tr><td colSpan={5}>正在读取当前页…</td></tr> : items.map((item) => <tr key={item.id}><td>{String(item.ordinal + 1).padStart(4, "0")}</td><td><strong>{item.label}</strong><small>{item.runId.slice(0, 8)}</small></td><td><Badge tone={item.status === "succeeded" ? "success" : ["failed", "cancelled"].includes(item.status) ? "warning" : "accent"}>{runLabels[item.status]}</Badge></td><td>{new Date(item.updatedAt).toLocaleString("zh-CN")}</td><td><Link className="button-ghost button-small" href={`/projects/${item.runId}`}>查看</Link></td></tr>)}</tbody></table></div>
            {!itemsLoading && items.length === 0 ? <p className="batch-empty">这个筛选下没有任务。</p> : null}
            <footer className="batch-pagination"><span>当前筛选共 {totalItems.toLocaleString("zh-CN")} 条 · 每页 {PAGE_SIZE} 条</span><div><button className="button-ghost button-small" type="button" disabled={!cursorHistory.length} onClick={() => { const history = [...cursorHistory]; setCursor(history.pop()); setCursorHistory(history); }}>上一页</button><button className="button-ghost button-small" type="button" disabled={!nextCursor} onClick={() => { if (!nextCursor) return; setCursorHistory((values) => [...values, cursor ?? ""]); setCursor(nextCursor); }}>下一页</button></div></footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
