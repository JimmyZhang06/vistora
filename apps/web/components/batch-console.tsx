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
import { Badge, LoadingScaffold, PageHeading, StatePanel } from "@/components/page-heading";
import { useConfirmDialog } from "@/components/confirm-dialog";
import { UiSelect } from "@/components/ui-select";
import { useSmartPolling } from "@/lib/use-smart-polling";

const PAGE_SIZE = 50;
const MAX_BATCH_SIZE = 5_000;
const STANDARD_PRODUCTION_V3_ID = "4c7d9777-d754-5fa3-bf85-4bf5c9746dba";
const STANDARD_PRODUCTION_PIPELINE_ID = "2c15b2d6-1460-5d30-a718-f4b06d8e28d7";
const defaultVideoSettings: VideoSettings = {
  language: "zh-CN", aspectRatio: "16:9", targetDurationSeconds: 180,
  visibility: "private", autoQualityCheck: true, layout: "full_frame",
  mediaFit: "cover", frameRate: 30,
  subtitles: { enabled: true, position: "bottom", size: "medium", maxLines: 2 },
  assetAcquisition: { enabled: false, sources: ["wikimedia"], maxAssets: 1, copyrightStatus: "public_domain", rightsConfirmed: false },
  noAssetDraft: { enabled: false },
};

type ResearchMode = "off" | "when_missing" | "required";
type BatchTopicRow = { topic: string; sourceUrls: string[]; invalidSources: string[] };

const researchModes: ReadonlyArray<{ value: ResearchMode; label: string; note: string }> = [
  { value: "off", label: "不联网", note: "不会发起联网搜索；若所选 Skill 要求来源，请在每行主题后提供 HTTPS URL。" },
  { value: "when_missing", label: "缺少来源时", note: "默认模式。已有可靠来源时复用，否则联网查证脚本事实。" },
  { value: "required", label: "始终联网", note: "每条任务都执行内容研究，适合时效性或事实要求较高的主题。" },
];

const runLabels: Record<RunStatus, string> = {
  queued: "排队中", running: "制作中", awaiting_review: "待审核", retrying: "重试中",
  succeeded: "已完成", failed: "失败", cancelled: "已取消",
};

const batchLabels: Record<GenerationBatch["status"], string> = {
  queued: "等待启动", running: "生产中", awaiting_review: "需要审核",
  succeeded: "全部完成", completed_with_errors: "已结束，有异常",
};

function BatchIcon({ name }: { name: "plus" | "arrow" | "upload" | "layers" | "search" | "check" | "clock" | "review" | "alert" }) {
  const paths = {
    plus: "M12 5v14M5 12h14",
    arrow: "M5 12h14m-6-6 6 6-6 6",
    upload: "M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4",
    layers: "m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5",
    search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    check: "m7 12 3 3 7-7M21 12a9 9 0 1 1-5-8",
    clock: "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
    review: "M9 3h6v4H9zM9 5H5v16h14V5h-4M8 12h8M8 16h5",
    alert: "M12 8v5m0 3v.1M10 4 2 18a1 1 0 0 0 1 2h18a1 1 0 0 0 1-2L14 4a2 2 0 0 0-4 0",
  };
  return <svg className="batch-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}

function isHttpsUrl(value: string) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function topicRowsFromText(value: string): BatchTopicRow[] {
  return value.split(/\r?\n/).map((line) => {
    const [rawTopic = "", ...rawSources] = line.split("\t");
    const declaredSources = rawSources.map((source) => source.trim()).filter(Boolean);
    return {
      topic: rawTopic.trim(),
      sourceUrls: [...new Set(declaredSources.filter(isHttpsUrl))],
      invalidSources: declaredSources.filter((source) => !isHttpsUrl(source)),
    };
  }).filter((row) => Boolean(row.topic));
}

function csvCells(line: string): string[] {
  const cells: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"' && quoted && line[index + 1] === '"') {
      cell += '"';
      index += 1;
    } else if (character === '"') quoted = !quoted;
    else if (character === "," && !quoted) {
      cells.push(cell.trim());
      cell = "";
    } else cell += character;
  }
  cells.push(cell.trim());
  return cells;
}

function importedTopicText(content: string, csv: boolean) {
  const rows = content.split(/\r?\n/).map((line) => csv ? csvCells(line) : line.split("\t").map((cell) => cell.trim()));
  if (csv && /^(?:topic|主题)$/i.test(rows[0]?.[0] ?? "")) rows.shift();
  return rows.filter((cells) => cells[0]).map((cells) => cells.filter(Boolean).join("\t")).join("\n");
}

function initialComposition(options: ComposerOptions, requestedChannelId = ""): RunComposition {
  const channel = options.channels.find((item) => item.id === requestedChannelId);
  const availableLibraryIds = new Set(options.assetLibraries.map((library) => library.id));
  return {
    skillVersionId: channel?.defaultComposition.skillVersionId ?? options.skills[0]?.versionId ?? "",
    pipelineVersionId: STANDARD_PRODUCTION_V3_ID,
    assetLibraryIds: (channel?.defaultComposition.assetLibraryIds ?? []).filter((id) => availableLibraryIds.has(id)),
    voiceProfileId: channel?.defaultComposition.voiceProfileId ?? options.voiceProfiles[0]?.id,
    renderPresetVersionId: channel?.defaultComposition.renderPresetVersionId ?? options.renderPresets[0]?.id,
  };
}

function BatchProgress({ batch }: { batch: GenerationBatch }) {
  const counts = batch.statusCounts;
  const terminal = counts.succeeded + counts.failed + counts.cancelled;
  const progress = batch.totalCount ? Math.round((terminal / batch.totalCount) * 100) : 0;
  const segmentWidth = (count: number) => batch.totalCount ? `${(count / batch.totalCount) * 100}%` : "0%";
  return (
    <div className="batch-progress" aria-label={`完成度 ${progress}%，成功 ${counts.succeeded}，失败 ${counts.failed}，取消 ${counts.cancelled}`}>
      <div className="batch-progress-track" aria-hidden="true">
        <span className="batch-progress-segment batch-progress-segment--success" style={{ width: segmentWidth(counts.succeeded) }} />
        <span className="batch-progress-segment batch-progress-segment--failed" style={{ width: segmentWidth(counts.failed) }} />
        <span className="batch-progress-segment batch-progress-segment--cancelled" style={{ width: segmentWidth(counts.cancelled) }} />
      </div>
      <p><strong>{progress}%</strong><span>{terminal} / {batch.totalCount} 已结束</span></p>
    </div>
  );
}

export function BatchConsole() {
  const { confirm, confirmationDialog } = useConfirmDialog();
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
  const [researchMode, setResearchMode] = useState<ResearchMode>("when_missing");
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retryingFailed, setRetryingFailed] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [message, setMessage] = useState("");
  const detailRequest = useRef(0);
  const settingsInitialized = useRef(false);

  const topicRows = useMemo(() => topicRowsFromText(topicText), [topicText]);
  const topics = useMemo(() => topicRows.map((row) => row.topic), [topicRows]);
  const duplicates = topics.length - new Set(topics).size;
  const invalidSourceCount = topicRows.reduce((total, row) => total + row.invalidSources.length, 0);
  const selectedLibraries = useMemo(
    () => options?.assetLibraries.filter((library) => composition.assetLibraryIds.includes(library.id)) ?? [],
    [composition.assetLibraryIds, options],
  );
  const selectedReadyAssetCount = selectedLibraries.reduce((total, library) => total + (library.readyAssetCount ?? 0), 0);
  const pipelineOptions = useMemo(() => {
    const configured = options?.pipelines.find((pipeline) => pipeline.id === STANDARD_PRODUCTION_V3_ID);
    const latest = configured ?? {
      id: STANDARD_PRODUCTION_V3_ID,
      workspaceId: "",
      pipelineId: STANDARD_PRODUCTION_PIPELINE_ID,
      name: "标准批量视频生产",
      version: "3",
      capabilities: [],
    };
    return [latest, ...(options?.pipelines.filter((pipeline) => pipeline.id !== STANDARD_PRODUCTION_V3_ID) ?? [])];
  }, [options]);

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
      if (!settingsInitialized.current) {
        setChannelId(composerOptions.channels.some((channel) => channel.id === requestedChannelId) ? requestedChannelId : "");
        if (parameters.get("create") === "1") setShowCreate(true);
      }
    }
    if (preferencesResult.ok && !settingsInitialized.current) {
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
    if (!preferencesResult.ok && !settingsInitialized.current) {
      setMessage("未能读取个人偏好，当前使用默认输出设置；创建前请确认画幅与目标时长。");
    }
    if (batchResult.ok) {
      setBatches(batchResult.data);
      setSelectedId((current) => current || batchResult.data[0]?.id || "");
      setError(optionsResult.ok ? "" : optionsResult.error.message);
    } else setError(batchResult.error.message);
    if (optionsResult.ok) settingsInitialized.current = true;
    setLoading(false);
  }, [adapter]);

  const loadSelected = useCallback(async () => {
    const request = ++detailRequest.current;
    if (!selectedId) { setSelected(null); setItems([]); return; }
    setItemsLoading(true);
    const [batchResult, itemResult] = await Promise.all([
      adapter.getGenerationBatch(selectedId),
      adapter.listGenerationBatchItems(selectedId, {
        status: statusFilter === "all" ? undefined : statusFilter,
        search: appliedSearch || undefined, cursor, limit: PAGE_SIZE,
      }),
    ]);
    if (request !== detailRequest.current) return;
    if (batchResult.ok) setSelected(batchResult.data);
    if (itemResult.ok) {
      setItems(itemResult.data.data);
      setTotalItems(itemResult.data.totalCount);
      setNextCursor(itemResult.data.nextCursor);
    }
    setDetailError(!batchResult.ok ? batchResult.error.message : !itemResult.ok ? itemResult.error.message : "");
    setItemsLoading(false);
  }, [adapter, appliedSearch, cursor, selectedId, statusFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadShell(), 0);
    return () => window.clearTimeout(timer);
  }, [loadShell]);
  useEffect(() => {
    const timer = window.setTimeout(() => void loadSelected(), 0);
    return () => { window.clearTimeout(timer); detailRequest.current += 1; };
  }, [loadSelected]);
  useSmartPolling(async () => { await Promise.all([loadShell(), loadSelected()]); }, {
    enabled: Boolean(batches?.some((batch) => ["queued", "running", "awaiting_review"].includes(batch.status))),
    intervalMs: 5_000,
  });

  function chooseChannel(value: string) {
    setChannelId(value);
    const channel = options?.channels.find((item) => item.id === value);
    if (channel) {
      const availableLibraryIds = new Set(options?.assetLibraries.map((library) => library.id) ?? []);
      setComposition({
        ...channel.defaultComposition,
        pipelineVersionId: STANDARD_PRODUCTION_V3_ID,
        assetLibraryIds: channel.defaultComposition.assetLibraryIds.filter((id) => availableLibraryIds.has(id)),
      });
    }
  }

  function toggleLibrary(libraryId: string) {
    setComposition((current) => ({
      ...current,
      assetLibraryIds: current.assetLibraryIds.includes(libraryId)
        ? current.assetLibraryIds.filter((id) => id !== libraryId)
        : [...current.assetLibraryIds, libraryId],
    }));
  }

  async function importTopics(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const content = await file.text();
    const csv = file.name.toLowerCase().endsWith(".csv") || file.type === "text/csv";
    const normalized = importedTopicText(content, csv);
    setTopicText(normalized);
    setMessage(`已读取 ${file.name}；${csv ? "首列作为主题，后续列作为 HTTPS 来源" : "制表符后的列作为 HTTPS 来源"}。`);
    event.target.value = "";
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || topics.length === 0 || topics.length > MAX_BATCH_SIZE) {
      setMessage(`请填写批次名称，并提供 1–${MAX_BATCH_SIZE} 个主题。`);
      return;
    }
    if (topics.some((topic) => topic.length > 1000)) {
      setMessage("单条主题最多 1,000 个字符，请缩短后再创建。");
      return;
    }
    if (invalidSourceCount) {
      setMessage(`发现 ${invalidSourceCount} 个无效来源；来源必须是完整的 HTTPS URL。`);
      return;
    }
    if (!composition.skillVersionId || !composition.pipelineVersionId) {
      setMessage("请先选择可用的 Skill 与 Pipeline。");
      return;
    }
    const selectedLibraryIds = selectedLibraries.map((library) => library.id);
    if (!selectedLibraryIds.length) {
      setMessage("标准批量生产 v3 必须至少选择一个当前空间中已准备好的素材库；执行中不会临时下载素材。");
      return;
    }
    if (selectedReadyAssetCount <= 0) {
      setMessage("所选素材库当前没有可用于剪辑的 ready 素材；请先完成导入、分析与审核，再创建批次。");
      return;
    }
    setSubmitting(true);
    setMessage("正在持久化批次并投递生产队列…");
    const result = await adapter.createGenerationBatch({
      workspaceId, name: name.trim(), channelId: channelId || undefined,
      items: topicRows.map((row) => ({
        topic: row.topic,
        ...(row.sourceUrls.length ? { inputs: { source_urls: row.sourceUrls } } : {}),
      })),
      composition: { ...composition, assetLibraryIds: selectedLibraryIds }, researchMode,
      videoSettings: {
        ...videoSettings,
        assetAcquisition: {
          enabled: false,
          sources: ["wikimedia"],
          maxAssets: 1,
          copyrightStatus: "public_domain",
          rightsConfirmed: false,
        },
      },
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
    if (!selected || !await confirm({ title: "停止批次", description: `将停止“${selected.name}”中所有未结束任务，已经完成的产物不会被删除。`, confirmLabel: "停止批次", tone: "danger" })) return;
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
      <PageHeading
        eyebrow="03 / BATCH PRODUCTION"
        title={showCreate ? "新建生产批次" : "批量生产"}
        description={showCreate ? "整理主题，统一制作配置，让每一条创作有序开始。" : "从一组主题到一组成片，在这里掌握每一步进度。"}
        actions={<button className={showCreate ? "button-secondary" : "button"} type="button" aria-expanded={showCreate} aria-controls="batch-create-form" onClick={() => setShowCreate((value) => !value)}>{showCreate ? "返回批次列表" : <><BatchIcon name="plus" />新建批次</>}</button>}
      />

      {options?.channelProblem ? <p className="alert" role="status">频道服务返回：{options.channelProblem.message}。请在服务恢复后重新加载。</p> : null}
      {message ? <p className="alert" role="status">{message}</p> : null}
      {error ? <div className="alert alert--error batch-error" role="alert"><span>{error}</span><button className="button-ghost button-small" type="button" onClick={() => { void loadShell(); void loadSelected(); }}>重新加载</button></div> : null}

      {showCreate ? (
        <form id="batch-create-form" className="batch-create" onSubmit={submit}>
          <div className="batch-create-layout">
            <section className="batch-compose-main" aria-labelledby="batch-topics-heading">
              <header className="batch-section-heading"><span className="batch-step">01</span><div><h2 id="batch-topics-heading">创作主题</h2><p>每行一个主题，每个主题生成一条独立视频。</p></div></header>
              <label className="field"><span>批次名称</span><input className="input" value={name} maxLength={160} onChange={(event) => setName(event.target.value)} placeholder="例如：九月知识栏目 · 第一轮" /></label>
              <div className="batch-topic-heading"><label htmlFor="batch-topic-input">主题清单</label><div><input ref={fileRef} className="sr-only" type="file" accept=".txt,.csv,text/plain,text/csv" aria-label="导入主题文件" onChange={(event) => void importTopics(event)} /><button className="button-ghost button-small" type="button" onClick={() => fileRef.current?.click()}><BatchIcon name="upload" />导入 TXT / CSV</button></div></div>
              <div className="batch-topic-editor">
                <textarea id="batch-topic-input" className="input" value={topicText} onChange={(event) => setTopicText(event.target.value)} aria-describedby="batch-topic-help" placeholder={"输入或粘贴你的创作主题…\n\nAI 如何改变小型制造业\n为什么人们重新开始阅读长文章\n一座城市的早餐，藏着怎样的生活方式"} />
                <div className="batch-topic-count"><span>{duplicates ? `${duplicates} 条重复主题（保留）` : "每行一条 · 支持附加来源"}</span><span><strong>{topics.length.toLocaleString("zh-CN")}</strong> / 5,000 条</span></div>
              </div>
              <p className="field-help" id="batch-topic-help">有参考来源时，可在主题后用 Tab 分隔 HTTPS 链接。CSV 首列为主题，后续列为来源。</p>
              <details className="batch-format-help"><summary>来源填写说明</summary><p>格式：主题&lt;Tab&gt;HTTPS 来源 1&lt;Tab&gt;HTTPS 来源 2。选择“不联网”时不会补查来源；所选 Skill 的 minimum_sources 由服务端精确校验。</p></details>
                        <fieldset className="batch-research-picker">
            <legend>内容联网研究</legend>
            <p>为脚本查证事实与来源，不下载图片或视频。批量剪辑内的自动补素材已关闭。</p>
            <div>
              {researchModes.map((mode) => (
                <label key={mode.value} data-selected={researchMode === mode.value || undefined}>
                  <input aria-label={`内容联网研究：${mode.label}`} type="radio" name="batch-research-mode" value={mode.value} checked={researchMode === mode.value} aria-describedby={`batch-research-${mode.value}-note`} onChange={() => setResearchMode(mode.value)} />
                  <span><strong>{mode.label}</strong><small id={`batch-research-${mode.value}-note`}>{mode.note}</small></span>
                </label>
              ))}
            </div>
          </fieldset>

            </section>
            <aside className="batch-compose-settings" aria-label="批次制作配置">
              <section className="batch-settings-section" aria-labelledby="batch-settings-heading">
                <header className="batch-section-heading"><span className="batch-step">02</span><div><h2 id="batch-settings-heading">生产配置</h2><p>此批次中的所有主题共用。</p></div></header>
                <div className="batch-create-grid">
                              <div className="field"><span className="field-label">发布频道</span><UiSelect ariaLabel="发布频道" value={channelId} onChange={chooseChannel}><option value="">不使用频道默认</option>{options?.channels.map((channel) => <option key={channel.id} value={channel.id}>{channel.name}</option>)}</UiSelect></div>
            <div className="field"><span className="field-label">Skill 版本</span><UiSelect ariaLabel="Skill 版本" value={composition.skillVersionId} onChange={(skillVersionId) => setComposition((value) => ({ ...value, skillVersionId }))}>{options?.skills.map((skill) => <option key={skill.versionId} value={skill.versionId}>{skill.skillName} · {skill.version}</option>)}</UiSelect></div>
            <div className="field"><span className="field-label">Pipeline 版本</span><UiSelect ariaLabel="Pipeline 版本" value={composition.pipelineVersionId} onChange={(pipelineVersionId) => setComposition((value) => ({ ...value, pipelineVersionId }))}>{pipelineOptions.map((pipeline) => <option key={pipeline.id} value={pipeline.id}>{pipeline.name} · v{pipeline.version}</option>)}</UiSelect></div>
                </div>
                          <fieldset className="batch-library-picker">
            <legend>剪辑素材库</legend>
            <p>为所有主题提供剪辑画面。创建后将锁定所选素材库。</p>
            <div>
              {options?.assetLibraries.length ? options.assetLibraries.map((library) => (
                <label key={library.id}>
                  <input
                    aria-label={`选择素材库 ${library.name}`}
                    type="checkbox"
                    checked={composition.assetLibraryIds.includes(library.id)}
                    onChange={() => toggleLibrary(library.id)}
                  />
                  <span><strong>{library.name}</strong><small>{library.readyAssetCount ?? 0} 个可用 · 共 {library.assetCount} 个</small></span>
                </label>
              )) : <span className="batch-library-empty">{options ? "当前空间没有素材库，请先到素材页初始化或导入素材。" : "素材库配置尚未载入，请等待服务连接或重新加载。"}</span>}
            </div>
            {!selectedLibraries.length ? <p className="batch-library-warning" role="status">请选择至少一个有可用素材的素材库。</p> : selectedReadyAssetCount <= 0 ? <p className="batch-library-warning" role="status">所选素材库暂无可用素材；请先完成导入、分析与审核。</p> : <p className="batch-library-ready" role="status">已选择 {selectedLibraries.length} 个素材库，共 {selectedReadyAssetCount.toLocaleString("zh-CN")} 个可用素材。</p>}
            <Link className="button-ghost button-small" href="/assets">管理素材库</Link>
          </fieldset>

              </section>
              <section className="batch-settings-section" aria-labelledby="batch-output-heading">
                <header className="batch-section-heading"><span className="batch-step">03</span><div><h2 id="batch-output-heading">视频输出</h2><p>统一画幅、版式与目标时长。</p></div></header>
                <div className="batch-create-grid batch-output-grid">
                              <div className="field"><span className="field-label">视频画幅</span><UiSelect ariaLabel="视频画幅" value={videoSettings.aspectRatio} onChange={(aspectRatio) => setVideoSettings((value) => ({ ...value, aspectRatio: aspectRatio as VideoSettings["aspectRatio"] }))}><option value="9:16">9:16 竖屏</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option><option value="4:3">4:3 经典</option></UiSelect></div>
            <div className="field"><span className="field-label">统一版式</span><UiSelect ariaLabel="统一版式" value={videoSettings.layout} onChange={(layout) => setVideoSettings((value) => ({ ...value, layout: layout as VideoSettings["layout"] }))}><option value="full_frame">沉浸全画面</option><option value="editorial">编辑分区</option></UiSelect></div>
            <label className="field"><span>目标时长（秒）</span><input className="input" type="number" min={15} max={3600} step={15} value={videoSettings.targetDurationSeconds} onChange={(event) => setVideoSettings((value) => ({ ...value, targetDurationSeconds: Math.min(3600, Math.max(15, Number(event.target.value) || 15)) }))} /></label>
                </div>
              </section>
            </aside>
          </div>
          <footer className="batch-create-footer"><div><span className="batch-submit-icon"><BatchIcon name="layers" /></span><div><strong>{topics.length ? `准备创建 ${topics.length.toLocaleString("zh-CN")} 条视频任务` : "添加主题，开始一组新的创作"}</strong><span>创建后制作配置将被锁定，建议先少量试跑。</span></div></div><button className="button" type="submit" disabled={submitting || !workspaceId || !options}>{submitting ? "正在创建…" : `创建 ${topics.length || 0} 条任务`}<BatchIcon name="arrow" /></button></footer>
        </form>
      ) : null}

      {!showCreate && loading ? <LoadingScaffold title="正在读取生产批次" description="同步批次进度与任务状态…" cards={2} /> : null}
      {!showCreate && !loading && batches?.length === 0 ? (
        <section className="batch-welcome" aria-labelledby="batch-welcome-title">
          <div className="batch-welcome-intro"><span className="batch-welcome-icon"><BatchIcon name="layers" /></span><p className="eyebrow">YOUR NEXT PRODUCTION</p><h2 id="batch-welcome-title">让一组好选题，<br />有序变成作品。</h2><p>把重复的制作交给批次，<br />把注意力留给内容与审核。</p><button className="button" type="button" onClick={() => setShowCreate(true)}>创建第一个批次<BatchIcon name="arrow" /></button></div>
          <ol className="batch-welcome-steps"><li><span>01</span><div><h3>整理创作主题</h3><p>直接粘贴，或导入 TXT / CSV 主题清单。</p></div></li><li><span>02</span><div><h3>选好制作方式</h3><p>统一 Skill、素材库、视频画幅与时长。</p></div></li><li><span>03</span><div><h3>跟进每一条进度</h3><p>集中审核、查看成片，按需重开失败项。</p></div></li><li className="batch-welcome-tip">首次使用？建议先用 3–5 条主题确认成片效果。</li></ol>
        </section>
      ) : null}
      {!showCreate && !loading && batches === null && error ? <StatePanel code="!" title="暂时无法读取生产批次" description="请确认控制服务可用后，点击上方“重新加载”。" error /> : null}

      {!showCreate && batches?.length ? (
        <div className="batch-console">
          <aside className="batch-list" aria-label="生产批次">
            <div className="batch-list-heading"><span><BatchIcon name="layers" />生产批次</span><strong>{batches.length}</strong></div>
            <div className="batch-list-items">{batches.map((batch) => <button type="button" className={selectedId === batch.id ? "is-selected" : ""} aria-pressed={selectedId === batch.id} key={batch.id} onClick={() => { if (selectedId !== batch.id) { setSelected(null); setItems([]); setItemsLoading(true); } setSelectedId(batch.id); resetPage(); }}><span className="batch-list-item-heading"><strong>{batch.name}</strong><BatchIcon name="arrow" /></span><span className="batch-list-item-meta"><Badge tone={batch.status === "succeeded" ? "success" : batch.status === "completed_with_errors" ? "warning" : "accent"}>{batchLabels[batch.status]}</Badge><time dateTime={batch.createdAt}>{new Date(batch.createdAt).toLocaleDateString("zh-CN", { month: "short", day: "numeric" })}</time></span><BatchProgress batch={batch} /></button>)}</div>
          </aside>

          <section className="batch-detail" aria-label="批次详情" aria-busy={itemsLoading}>
            {detailError ? <div className="alert alert--error batch-error" role="alert"><span>读取批次失败：{detailError}</span><button className="button-ghost button-small" type="button" onClick={() => void loadSelected()}>重试</button></div> : null}
            {selected ? <>
              <header className="batch-detail-heading"><div><div className="batch-detail-kicker"><span className="eyebrow">BATCH / {selected.id.slice(0, 8)}</span><Badge tone={selected.status === "succeeded" ? "success" : selected.status === "completed_with_errors" ? "warning" : "accent"}>{batchLabels[selected.status]}</Badge></div><h2>{selected.name}</h2><p>共 {selected.totalCount.toLocaleString("zh-CN")} 条任务<span>·</span>创建于 {new Date(selected.createdAt).toLocaleString("zh-CN")}</p></div><div className="button-row">{selected.statusCounts.failed ? <button className="button-secondary button-small" type="button" disabled={retryingFailed} onClick={() => void retryFailed()}>{retryingFailed ? "重开中…" : "重开失败项"}</button> : null}{["queued", "running", "awaiting_review"].includes(selected.status) ? <button className="button-ghost button-small batch-stop" type="button" disabled={cancelling} onClick={() => void cancelBatch()}>{cancelling ? "停止中…" : "停止批次"}</button> : null}</div></header>
              <div className="batch-metrics">
                <div><span><BatchIcon name="clock" />制作中</span><strong>{selected.statusCounts.running.toLocaleString("zh-CN")}</strong><small>{selected.statusCounts.queued} 条排队等待启动</small></div>
                <div data-tone="warning"><span><BatchIcon name="review" />待审核</span><strong>{selected.statusCounts.awaitingReview.toLocaleString("zh-CN")}</strong><small>等待你确认后继续</small></div>
                <div data-tone="success"><span><BatchIcon name="check" />已完成</span><strong>{selected.statusCounts.succeeded.toLocaleString("zh-CN")}</strong><small>进入任务查看产物</small></div>
                <div data-tone="danger"><span><BatchIcon name="alert" />失败</span><strong>{selected.statusCounts.failed.toLocaleString("zh-CN")}</strong><small>{selected.statusCounts.cancelled} 条已取消</small></div>
              </div>
              <div className="batch-detail-progress"><span>整体进度</span><BatchProgress batch={selected} /></div>
            </> : !detailError ? <LoadingScaffold title="正在读取批次详情" cards={1} /> : null}
            <div className="batch-tasks-heading"><h3>任务明细</h3><span>每页 {PAGE_SIZE} 条</span></div>
            <div className="batch-toolbar"><div className="toolbar" aria-label="任务状态筛选">{(["all", "queued", "running", "awaiting_review", "succeeded", "failed", "cancelled"] as const).map((value) => <button type="button" className={statusFilter === value ? "button-secondary button-small" : "button-ghost button-small"} aria-pressed={statusFilter === value} key={value} onClick={() => { setStatusFilter(value); resetPage(); }}>{value === "all" ? "全部" : runLabels[value]}</button>)}</div><form onSubmit={applySearch}><BatchIcon name="search" /><input className="input" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索主题…" aria-label="搜索批次中的主题" /><button className="button-ghost button-small" type="submit">搜索</button></form></div>
            <div className="batch-table-wrap"><table className="batch-table"><caption className="sr-only">批次任务明细</caption><thead><tr><th scope="col">序号</th><th scope="col">创作主题</th><th scope="col">状态</th><th scope="col">更新时间</th><th scope="col"><span className="sr-only">操作</span></th></tr></thead><tbody>{itemsLoading ? <tr className="batch-table-message"><td colSpan={5}>正在读取当前页…</td></tr> : items.map((item) => <tr key={item.id}><td>{String(item.ordinal + 1).padStart(4, "0")}</td><td><strong>{item.label}</strong><small>{item.runId.slice(0, 8)}</small></td><td><Badge tone={item.status === "succeeded" ? "success" : ["failed", "cancelled"].includes(item.status) ? "warning" : "accent"}>{runLabels[item.status]}</Badge></td><td><time dateTime={item.updatedAt}>{new Date(item.updatedAt).toLocaleString("zh-CN")}</time></td><td><Link className="button-ghost button-small" href={`/projects/${item.runId}`} aria-label={`查看任务：${item.label}`}>查看<BatchIcon name="arrow" /></Link></td></tr>)}</tbody></table></div>
            {!itemsLoading && !detailError && items.length === 0 ? <div className="batch-empty"><BatchIcon name="search" /><strong>没有符合条件的任务</strong><p>试试其他状态，或换一个主题关键词。</p><button type="button" className="button-secondary button-small" onClick={() => { setStatusFilter("all"); setSearch(""); setAppliedSearch(""); resetPage(); }}>清除筛选</button></div> : null}
            <footer className="batch-pagination"><span>当前筛选共 <strong>{totalItems.toLocaleString("zh-CN")}</strong> 条</span><div><span>第 {cursorHistory.length + 1} 页</span><button className="button-ghost button-small" type="button" disabled={itemsLoading || !cursorHistory.length} onClick={() => { const history = [...cursorHistory]; setCursor(history.pop()); setCursorHistory(history); }}>上一页</button><button className="button-secondary button-small" type="button" disabled={itemsLoading || !nextCursor} onClick={() => { if (!nextCursor) return; setCursorHistory((values) => [...values, cursor ?? ""]); setCursor(nextCursor); }}>下一页</button></div></footer>
          </section>
        </div>
      ) : null}
      {confirmationDialog}
    </div>
  );
}
