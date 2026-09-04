"use client";

import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type AssetIngestionJob,
  type AssetLibraryOption,
  type ComposerOptions,
  type LibraryBuildJob,
  type LibraryBuildSource,
} from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";
import { useSmartPolling } from "@/lib/use-smart-polling";

function librarySlug(value: string) {
  const slug = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return slug || `media-${Date.now().toString(36)}`;
}

function assetTitle(file: File) {
  return file.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ").trim() || file.name;
}

function ratio(value: number | undefined, total: number) {
  if (value === undefined) return undefined;
  return total ? Math.round(((value ?? 0) / total) * 100) : 0;
}

function jobProgress(job: AssetIngestionJob) {
  if (!job.discoveredCount) return job.status === "completed" ? 100 : 0;
  return Math.min(100, Math.round(((job.importedCount + job.deduplicatedCount + job.rejectedCount) / job.discoveredCount) * 100));
}

const MAX_LOCAL_FILES = 100;
const MAX_LOCAL_FILE_BYTES = 500 * 1024 * 1024;
const UPLOAD_CONCURRENCY = 3;
const BUILD_JOB_STORAGE_PREFIX = "vistora:last-library-build-job:";

type UploadTask = {
  id: string;
  file: File;
  name: string;
  relativePath: string;
  status: "waiting" | "uploading" | "queued" | "failed";
  message?: string;
};

type UploadContext = {
  libraryId: string;
  description: string;
  copyrightStatus: "owned" | "licensed" | "public_domain";
  tags: string[];
};

const uploadStatusLabel: Record<UploadTask["status"], string> = {
  waiting: "等待上传",
  uploading: "计算校验值并上传",
  queued: "上传完成，分析已入队",
  failed: "提交失败",
};

const buildStageLabel: Record<LibraryBuildJob["stage"], string> = {
  discover: "检索候选",
  transfer: "下载入库",
  analyze: "AI 分析与切片",
  index: "建立检索索引",
};

const terminalBuildStatuses = new Set<LibraryBuildJob["status"]>(["completed", "completed_with_errors", "failed", "cancelled"]);

function relativePath(file: File) {
  return file.webkitRelativePath || file.name;
}

function uploadTask(file: File, index: number): UploadTask {
  const path = relativePath(file);
  return { id: `${path}:${file.size}:${file.lastModified}:${index}`, file, name: file.name, relativePath: path, status: "waiting" };
}

export function AssetHub() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState("");
  const [jobs, setJobs] = useState<AssetIngestionJob[]>([]);
  const [error, setError] = useState("");
  const [offline, setOffline] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [libraryId, setLibraryId] = useState("__new__");
  const [libraryName, setLibraryName] = useState("通用视频素材");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [importMode, setImportMode] = useState<"file" | "url" | "topic">("file");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [copyrightStatus, setCopyrightStatus] = useState<"owned" | "licensed" | "public_domain">("owned");
  const [files, setFiles] = useState<File[]>([]);
  const [uploadContext, setUploadContext] = useState<UploadContext | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [uploadTasks, setUploadTasks] = useState<UploadTask[]>([]);
  const [buildTopic, setBuildTopic] = useState("");
  const [buildQueries, setBuildQueries] = useState("");
  const [buildSources, setBuildSources] = useState<LibraryBuildSource[]>(["wikimedia"]);
  const [buildMaxAssets, setBuildMaxAssets] = useState(6);
  const [buildCopyrightStatus, setBuildCopyrightStatus] = useState<"licensed" | "public_domain">("public_domain");
  const [buildRightsConfirmed, setBuildRightsConfirmed] = useState(false);
  const [buildJob, setBuildJob] = useState<LibraryBuildJob | null>(null);
  const [cancellingBuild, setCancellingBuild] = useState(false);

  const load = useCallback(async () => {
    const session = await adapter.getSession();
    if (!session.ok) {
      setError(session.error.message);
      return;
    }
    setActiveWorkspaceId(session.data.activeWorkspaceId);
    const [optionResult, jobResult] = await Promise.all([
      adapter.getComposerOptions(session.data.activeWorkspaceId),
      adapter.listAssetIngestionJobs({ limit: 20 }),
    ]);
    if (!optionResult.ok) {
      setError(optionResult.error.message);
      return;
    }
    setOptions(optionResult.data);
    setError("");
    setLibraryId((current) => current === "__new__" && optionResult.data.assetLibraries.length
      ? optionResult.data.assetLibraries[0].id
      : current);
    if (jobResult.ok) setJobs(jobResult.data.data);
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const sync = () => setOffline(!navigator.onLine);
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    sync();
    return () => {
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);

  useSmartPolling(load, {
    enabled: jobs.some((job) => job.status === "pending" || job.status === "running"),
    intervalMs: 8_000,
  });

  useEffect(() => {
    if (!activeWorkspaceId || buildJob) return;
    const storageKey = `${BUILD_JOB_STORAGE_PREFIX}${activeWorkspaceId}`;
    const jobId = window.localStorage.getItem(storageKey);
    if (!jobId) return;
    let stopped = false;
    void adapter.getLibraryBuildJob(jobId).then((result) => {
      if (stopped) return;
      if (result.ok) setBuildJob(result.data);
      else if (result.error.code === "RESOURCE_NOT_FOUND") window.localStorage.removeItem(storageKey);
      else setNotice(`上次主题素材任务恢复失败：${result.error.message}`);
    });
    return () => { stopped = true; };
  }, [activeWorkspaceId, adapter, buildJob]);

  const pollBuildJob = useCallback(async () => {
    if (!buildJob || terminalBuildStatuses.has(buildJob.status)) return;
    const result = await adapter.getLibraryBuildJob(buildJob.id);
    if (!result.ok) {
      setNotice(`素材采集任务状态同步失败：${result.error.message}`);
      return;
    }
    setBuildJob(result.data);
    if (terminalBuildStatuses.has(result.data.status)) {
      const ready = result.data.progress.indexed;
      setNotice(result.data.status === "completed"
        ? `主题素材任务已完成，${ready} 个素材已建立索引。`
        : `主题素材任务已结束：${ready} 个已索引，${result.data.progress.failed} 个失败。`);
      await load();
    }
  }, [adapter, buildJob, load]);

  useSmartPolling(pollBuildJob, {
    enabled: Boolean(buildJob && !terminalBuildStatuses.has(buildJob.status)),
    intervalMs: 3_000,
  });

  function selectLocalFiles(selected: File[]) {
    const supported = selected.filter((file) => file.type.startsWith("image/") || file.type.startsWith("video/"));
    const withinLimit = supported.filter((file) => file.size <= MAX_LOCAL_FILE_BYTES).slice(0, MAX_LOCAL_FILES);
    const skippedType = selected.length - supported.length;
    const skippedSize = supported.filter((file) => file.size > MAX_LOCAL_FILE_BYTES).length;
    const skippedCount = Math.max(0, supported.length - skippedSize - MAX_LOCAL_FILES);
    setFiles(withinLimit);
    setUploadTasks([]);
    setUploadContext(null);
    if (skippedType || skippedSize || skippedCount) {
      setNotice(`已选择 ${withinLimit.length} 个文件；跳过 ${skippedType} 个非图片/视频、${skippedSize} 个超过 500 MB、${skippedCount} 个超出每批 100 个的文件。`);
    } else {
      setNotice(withinLimit.length ? `已选择 ${withinLimit.length} 个文件。上传采用 3 路并发，失败项可在当前页面单独重试。` : "未选择可上传的图片或视频。 ");
    }
  }

  function updateUploadTask(taskId: string, patch: Partial<UploadTask>) {
    setUploadTasks((current) => current.map((task) => task.id === taskId ? { ...task, ...patch } : task));
  }

  async function runUploadTasks(tasks: UploadTask[], context: UploadContext) {
    let cursor = 0;
    const outcomes: Array<{ task: UploadTask; ok: boolean; message?: string }> = [];
    async function worker() {
      while (cursor < tasks.length) {
        const index = cursor;
        cursor += 1;
        const task = tasks[index];
        updateUploadTask(task.id, { status: "uploading", message: undefined });
        const result = await adapter.uploadAsset({
          libraryId: context.libraryId,
          file: task.file,
          title: assetTitle(task.file),
          description: context.description || assetTitle(task.file),
          copyrightStatus: context.copyrightStatus,
          tags: context.tags,
          // AssetUploadCreate is strict at the top level. Directory provenance is
          // carried through its existing `source` metadata object by the adapter.
          relativePath: task.file.webkitRelativePath || undefined,
        });
        if (result.ok) {
          updateUploadTask(task.id, { status: "queued", message: uploadStatusLabel.queued });
          outcomes.push({ task, ok: true });
        } else {
          updateUploadTask(task.id, { status: "failed", message: result.error.message });
          outcomes.push({ task, ok: false, message: result.error.message });
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(UPLOAD_CONCURRENCY, tasks.length) }, () => worker()));
    return outcomes;
  }

  async function retryFailedUploads() {
    if (!uploadContext || busy || offline) return;
    const failed = uploadTasks.filter((task) => task.status === "failed");
    if (!failed.length) return;
    setBusy(true);
    failed.forEach((task) => updateUploadTask(task.id, { status: "waiting", message: undefined }));
    setNotice(`正在重试 ${failed.length} 个失败文件；请保持当前页面打开。`);
    const outcomes = await runUploadTasks(failed, uploadContext);
    const failedAgain = outcomes.filter((item) => !item.ok).length;
    setBusy(false);
    setNotice(`重试完成：${outcomes.length - failedAgain} 个已提交，${failedAgain} 个仍失败。当前上传不支持关闭页面后的断点续传。`);
    await load();
  }

  function toggleBuildSource(source: LibraryBuildSource) {
    setBuildSources((current) => {
      if (current.includes(source)) return current.length === 1 ? current : current.filter((item) => item !== source);
      return [...current, source];
    });
    if (source !== "wikimedia") setBuildCopyrightStatus("licensed");
  }

  async function cancelBuildJob() {
    if (!buildJob || terminalBuildStatuses.has(buildJob.status)) return;
    setCancellingBuild(true);
    const result = await adapter.cancelLibraryBuildJob(buildJob.id, buildJob.revision);
    setCancellingBuild(false);
    if (!result.ok) {
      setNotice(result.error.message);
      return;
    }
    setBuildJob(result.data);
    setNotice("已请求停止素材采集任务；已入库素材仍保留并继续受分析与版权门禁约束。");
    await load();
  }

  async function importAssets(event: FormEvent) {
    event.preventDefault();
    if (offline) {
      setNotice("当前处于离线状态，恢复连接后再导入素材。");
      return;
    }
    if (importMode === "file" && !files.length) {
      setNotice("请选择至少一个图片或视频文件。");
      return;
    }
    if (importMode === "url" && !remoteUrl.trim()) {
      setNotice("请输入 YouTube、Bilibili 或小红书公开视频链接。");
      return;
    }
    if (importMode === "url" && !rightsConfirmed) {
      setNotice("请先确认你有权下载并在视频中使用该素材。");
      return;
    }
    if (importMode === "topic" && buildTopic.trim().length < 2) {
      setNotice("请填写要初始化或补充的素材主题。");
      return;
    }
    if (importMode === "topic" && !buildRightsConfirmed) {
      setNotice("请先确认所选来源和许可范围；任务只会采集符合该策略的候选素材。");
      return;
    }
    setBusy(true);
    setNotice("正在准备素材库…");
    let targetLibraryId = libraryId;
    if (targetLibraryId === "__new__") {
      const created = await adapter.createAssetLibrary({
        name: libraryName.trim(), slug: librarySlug(libraryName), description: "用于自动画面匹配的版权安全素材",
      });
      if (!created.ok) {
        setBusy(false);
        setNotice(created.error.message);
        return;
      }
      targetLibraryId = created.data.id;
      setLibraryId(targetLibraryId);
    }

    const normalizedTags = tags.split(/[，,\n]/).map((value) => value.trim()).filter(Boolean);
    if (importMode === "topic") {
      setNotice("正在创建异步素材采集任务…");
      const result = await adapter.createLibraryBuildJob({
        libraryId: targetLibraryId,
        topic: buildTopic.trim(),
        queries: buildQueries.split(/\r?\n/).map((value) => value.trim()).filter(Boolean).slice(0, 12).map((value) => value.slice(0, 500)),
        sources: buildSources,
        maxAssets: buildMaxAssets,
        copyrightStatus: buildCopyrightStatus,
        rightsConfirmed: true,
      }, `web-library-build-${crypto.randomUUID()}`);
      setBusy(false);
      if (!result.ok) {
        setNotice(result.error.message);
        return;
      }
      setBuildJob(result.data);
      window.localStorage.setItem(`${BUILD_JOB_STORAGE_PREFIX}${result.data.workspaceId}`, result.data.id);
      setBuildRightsConfirmed(false);
      setNotice("素材联网采集任务已排队；页面会轮询检索、下载、分析和索引的真实状态。");
      await load();
      return;
    }
    if (importMode === "url") {
      setNotice("正在解析、下载并写入素材库，请勿关闭页面…");
      const result = await adapter.importRemoteAsset({
        libraryId: targetLibraryId, sourceUrl: remoteUrl.trim(), description: description.trim(),
        copyrightStatus, tags: normalizedTags, rightsConfirmed: true,
      });
      setBusy(false);
      if (!result.ok) {
        setNotice(result.error.message);
        return;
      }
      setRemoteUrl("");
      setDescription("");
      setTags("");
      setRightsConfirmed(false);
      setNotice("网络素材已提交处理，可在后台任务中查看真实进度。");
      await load();
      return;
    }
    const tasks = files.map(uploadTask);
    const context: UploadContext = {
      libraryId: targetLibraryId,
      description: description.trim(),
      copyrightStatus,
      tags: normalizedTags,
    };
    setUploadTasks(tasks);
    setUploadContext(context);
    setNotice(`正在以最多 ${UPLOAD_CONCURRENCY} 路并发上传 ${tasks.length} 个文件；单个失败不会中断其余文件。`);
    const outcomes = await runUploadTasks(tasks, context);
    const failedCount = outcomes.filter((item) => !item.ok).length;
    const importedCount = outcomes.length - failedCount;
    setFiles([]);
    setDescription("");
    setTags("");
    setBusy(false);
    setNotice(`本次上传完成：${importedCount} 个已提交分析，${failedCount} 个失败${failedCount ? "，可在当前页面重试失败项" : ""}。不支持关闭页面后的断点续传。`);
    await load();
  }

  const importAction = (
    <button className="button" type="button" onClick={() => setShowImport((value) => !value)} disabled={offline}>
      {showImport ? "收起导入" : "导入素材"}
    </button>
  );

  return (
    <div className="page page--wide">
      <PageHeading
        eyebrow="05 / ASSETS"
        title="素材库管理"
        description="面向数千素材的版权、安全扫描、AI 分析与审核工作台。只有通过全部门禁的素材才可进入创作。"
        actions={importAction}
      />

      {offline ? <div className="connection-banner" role="status">你当前处于离线状态。已显示的数据仍可查看，写入操作已暂停。</div> : null}

      {showImport ? (
        <form className="panel asset-import-panel" onSubmit={importAssets} aria-label="导入素材">
          <div className="section-heading">
            <div><p className="eyebrow">INGEST</p><h2>导入版权安全素材</h2></div>
            <Badge tone="accent">浏览器直传 ≤ 500 MB / 文件</Badge>
          </div>
          <div className="toolbar asset-mode-switch" aria-label="素材导入方式">
            <button className={importMode === "file" ? "button-secondary button-small" : "button-ghost button-small"} type="button" aria-pressed={importMode === "file"} onClick={() => setImportMode("file")}>本地文件</button>
            <button className={importMode === "url" ? "button-secondary button-small" : "button-ghost button-small"} type="button" aria-pressed={importMode === "url"} onClick={() => setImportMode("url")}>公开视频链接</button>
            <button className={importMode === "topic" ? "button-secondary button-small" : "button-ghost button-small"} type="button" aria-pressed={importMode === "topic"} onClick={() => setImportMode("topic")}>按主题初始化 / 补充</button>
          </div>
          <div className="form-grid asset-import-grid">
            <div className="field">
              <span className="field-label">目标素材库</span>
              <UiSelect ariaLabel="目标素材库" value={libraryId} onChange={setLibraryId}>
                {options?.assetLibraries.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                <option value="__new__">＋ 创建新素材库</option>
              </UiSelect>
            </div>
            {libraryId === "__new__" ? (
              <label className="field"><span className="field-label">新素材库名称</span><input className="input" required maxLength={160} value={libraryName} onChange={(event) => setLibraryName(event.target.value)} /></label>
            ) : null}
            {importMode !== "topic" ? <div className="field">
              <span className="field-label">版权状态</span>
              <UiSelect ariaLabel="版权状态" value={copyrightStatus} onChange={(value) => setCopyrightStatus(value as typeof copyrightStatus)}>
                <option value="owned">自有素材</option><option value="licensed">已获得授权</option><option value="public_domain">公共领域</option>
              </UiSelect>
            </div> : null}
            {importMode === "file" ? (
              <div className="field field--full local-file-import">
                <span className="field-label">选择文件或目录</span>
                <div className="local-file-pickers">
                  <label><span>选择多个文件</span><input className="input asset-file-input" type="file" accept="image/*,video/*" multiple onChange={(event) => selectLocalFiles(Array.from(event.target.files ?? []))} /></label>
                  <label><span>选择一个目录</span><input ref={(node) => { if (node) { node.setAttribute("webkitdirectory", ""); node.setAttribute("directory", ""); } }} className="input asset-file-input" type="file" accept="image/*,video/*" multiple onChange={(event) => selectLocalFiles(Array.from(event.target.files ?? []))} /></label>
                </div>
                <small className="field-help">每批最多 100 个图片或视频，最多 3 路并发；目录相对路径会随上传元数据保存。相同内容按 SHA-256 去重，不支持关闭页面后的断点续传。</small>
              </div>
            ) : importMode === "url" ? (
              <>
                <label className="field field--full"><span className="field-label">公开视频链接</span><input className="input" type="url" required maxLength={2048} value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} placeholder="YouTube、Bilibili 或小红书公开视频链接" /></label>
                <div className="field field--full remote-rights-confirmation">
                  <input id="remote-rights-confirmed" type="checkbox" checked={rightsConfirmed} onChange={(event) => setRightsConfirmed(event.target.checked)} />
                  <label htmlFor="remote-rights-confirmed"><strong>我确认有权下载并将此素材用于生成视频</strong><small className="field-help">系统会保存原链接、平台、作者、许可证和内容哈希。</small></label>
                </div>
              </>
            ) : (
              <>
                <div className="field field--full material-research-note" role="note">
                  <strong>这是素材联网采集，不是内容联网研究</strong>
                  <p>系统会从所选来源检索并下载图片或视频，随后执行安全扫描、AI 切片和标签索引。批量生产中的“内容联网研究”只查证脚本事实，不会下载画面。</p>
                </div>
                <label className="field field--full"><span className="field-label">素材主题或需要的画面</span><textarea className="textarea" required minLength={2} maxLength={1000} value={buildTopic} onChange={(event) => setBuildTopic(event.target.value)} placeholder="例如：新能源汽车电池生产线、机械臂装配、质检人员检查电芯" /></label>
                <details className="field field--full build-query-details"><summary>高级：补充检索词（可选）</summary><textarea className="textarea" value={buildQueries} onChange={(event) => setBuildQueries(event.target.value)} placeholder={"每行一条，最多 12 条\n新能源汽车电池工厂\nrobotic battery assembly line"} /></details>
                <div className="field">
                  <span className="field-label">许可策略</span>
                  <UiSelect ariaLabel="主题素材许可策略" value={buildCopyrightStatus} onChange={(value) => {
                    const next = value as typeof buildCopyrightStatus;
                    setBuildCopyrightStatus(next);
                    if (next === "public_domain") setBuildSources(["wikimedia"]);
                    setBuildRightsConfirmed(false);
                  }}>
                    <option value="public_domain">公共领域（仅 Wikimedia）</option>
                    <option value="licensed">已授权来源</option>
                  </UiSelect>
                </div>
                <label className="field"><span className="field-label">最多采集</span><input className="input" type="number" min={1} max={6} value={buildMaxAssets} onChange={(event) => setBuildMaxAssets(Math.min(6, Math.max(1, Number(event.target.value) || 1)))} /><small className="field-help">单次任务最多 6 个候选；这是起始素材包，不代表主题已完整覆盖。</small></label>
                <fieldset className="field field--full build-source-picker"><legend>联网素材来源</legend>{(["wikimedia", "youtube", "bilibili"] as const).map((source) => <label key={source}><input type="checkbox" checked={buildSources.includes(source)} disabled={buildCopyrightStatus === "public_domain" && source !== "wikimedia"} onChange={() => toggleBuildSource(source)} /><span>{source === "wikimedia" ? "Wikimedia" : source === "youtube" ? "YouTube" : "Bilibili"}</span></label>)}</fieldset>
                <div className="field field--full remote-rights-confirmation">
                  <input id="build-rights-confirmed" type="checkbox" checked={buildRightsConfirmed} onChange={(event) => setBuildRightsConfirmed(event.target.checked)} />
                  <label htmlFor="build-rights-confirmed"><strong>我确认按所选来源与许可策略采集，并会在发布前复核使用权</strong><small className="field-help">勾选不会替代平台授权；系统会保存来源、许可和内容哈希供审核。</small></label>
                </div>
              </>
            )}
            {importMode !== "topic" ? <><label className="field field--full"><span className="field-label">画面描述</span><textarea className="textarea" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label><label className="field field--full"><span className="field-label">检索标签</span><input className="input" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="AI 视频，自动剪辑，电脑，工作室" /></label></> : null}
          </div>
          <div className="asset-import-actions">
            <button className="button" type="submit" disabled={busy || offline || (importMode === "topic" && Boolean(buildJob && !terminalBuildStatuses.has(buildJob.status)))}>{busy ? "正在提交" : importMode === "url" ? "下载并入库" : importMode === "topic" ? "开始初始化 / 补充" : `导入 ${files.length || "所选"} 个素材`}</button>
            {uploadTasks.some((task) => task.status === "failed") ? <button className="button-secondary" type="button" onClick={() => void retryFailedUploads()} disabled={busy || offline}>重试失败项</button> : null}
            {notice ? <p className="muted" role="status" aria-live="polite">{notice}</p> : null}
          </div>
          {uploadTasks.length ? <ol className="upload-task-list" aria-label="文件处理状态">{uploadTasks.map((task) => <li key={task.id} data-state={task.status}><span aria-hidden="true" /> <strong>{task.relativePath}</strong><small>{task.message || uploadStatusLabel[task.status]}</small></li>)}</ol> : null}
        </form>
      ) : null}

      {buildJob ? <section className="panel library-build-status" aria-labelledby="library-build-status-title" aria-live="polite">
        <div className="section-heading"><div><p className="eyebrow">MATERIAL ACQUISITION</p><h2 id="library-build-status-title">主题素材任务</h2></div><Badge tone={buildJob.status === "completed" ? "success" : ["failed", "completed_with_errors", "cancelled"].includes(buildJob.status) ? "warning" : "accent"}>{buildJob.status}</Badge></div>
        <div className="library-build-status-grid"><div><span>当前阶段</span><strong>{buildStageLabel[buildJob.stage]}</strong></div><div><span>发现</span><strong>{buildJob.progress.discovered}</strong></div><div><span>已下载</span><strong>{buildJob.progress.transferred}</strong></div><div><span>已分析</span><strong>{buildJob.progress.analyzed}</strong></div><div><span>已索引</span><strong>{buildJob.progress.indexed}</strong></div><div><span>失败</span><strong>{buildJob.progress.failed}</strong></div></div>
        <p>主题：{buildJob.spec.topic} · 目标最多 {buildJob.spec.maxAssets} 个 · {buildJob.spec.sources.join("、")}</p>
        {buildJob.error ? <p className="alert alert--error" role="alert">{String(buildJob.error.message ?? buildJob.error.code ?? "任务处理失败，请查看服务日志。")}</p> : null}
        <div className="button-row"><Link className="button-secondary button-small" href={`/assets/${encodeURIComponent(buildJob.libraryId)}`}>查看目标素材库</Link>{!terminalBuildStatuses.has(buildJob.status) ? <button className="button-ghost button-small" type="button" onClick={() => void cancelBuildJob()} disabled={cancellingBuild}>{cancellingBuild ? "正在停止…" : "停止任务"}</button> : null}</div>
      </section> : null}

      {error ? <StatePanel code="ERR" title="素材暂时不可用" description={error} error><button className="button-secondary" type="button" onClick={() => void load()}>重试</button></StatePanel> : null}
      {!error && !options ? <div className="loading-grid" aria-label="正在加载素材"><div className="loading-card" /><div className="loading-card" /></div> : null}
      {options?.assetLibraries.length === 0 && !showImport ? <StatePanel code="00" title="还没有素材库" description="先创建素材库并导入有明确版权状态的图片或视频。">{importAction}</StatePanel> : null}

      {options?.assetLibraries.length ? <div className="asset-library-grid">{options.assetLibraries.map((library) => <LibraryCard key={library.id} library={library} />)}</div> : null}

      {jobs.length ? (
        <section className="panel ingestion-panel" aria-labelledby="ingestion-heading">
          <div className="section-heading"><div><p className="eyebrow">BACKGROUND TASKS</p><h2 id="ingestion-heading">后台处理任务</h2></div><Badge>{jobs.some((job) => job.status === "running" || job.status === "pending") ? "自动刷新" : "最近任务"}</Badge></div>
          <div className="ingestion-list">{jobs.map((job) => {
            const progress = jobProgress(job);
            return <article key={job.id} className="ingestion-row">
              <div><strong>{options?.assetLibraries.find((item) => item.id === job.libraryId)?.name ?? "素材库任务"}</strong><span>{job.importedCount} 导入 · {job.deduplicatedCount} 去重 · {job.rejectedCount} 隔离</span></div>
              <div className="job-progress"><span><i style={{ width: `${progress}%` }} /></span><small>{progress}%</small></div>
              <Badge tone={job.status === "failed" ? "warning" : job.status === "completed" ? "success" : "accent"}>{job.status}</Badge>
            </article>;
          })}</div>
        </section>
      ) : null}
    </div>
  );
}

function LibraryCard({ library }: { library: AssetLibraryOption }) {
  const total = library.assetCount;
  const tagCoverage = ratio(library.taggedAssetCount, total);
  const copyrightCoverage = ratio(library.copyrightCompleteAssetCount, total);
  const metrics = [
    ["可用", library.readyAssetCount], ["隔离", library.quarantinedAssetCount], ["处理中", library.processingAssetCount],
  ] as const;
  return (
    <article className="asset-library-card">
      <header><div><p className="eyebrow">ASSET LIBRARY</p><h2>{library.name}</h2></div><strong>{total.toLocaleString("zh-CN")}</strong></header>
      <p>{library.description || "暂无素材库说明。"}</p>
      <dl>{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value === undefined ? "—" : value.toLocaleString("zh-CN")}</dd></div>)}</dl>
      <div className="coverage-grid">
        <span>标签覆盖<strong>{tagCoverage === undefined ? "—" : `${tagCoverage}%`}</strong><i><b style={{ width: `${tagCoverage ?? 0}%` }} /></i></span>
        <span>版权完成<strong>{copyrightCoverage === undefined ? "—" : `${copyrightCoverage}%`}</strong><i><b style={{ width: `${copyrightCoverage ?? 0}%` }} /></i></span>
      </div>
      <footer><span>{library.readyAssetCount === undefined ? "可用数量待服务端汇总" : `${library.readyAssetCount} 个素材可进入创作匹配`}</span><Link className="button-secondary button-small" href={`/assets/${encodeURIComponent(library.id)}`}>管理素材</Link></footer>
    </article>
  );
}
