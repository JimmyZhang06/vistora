"use client";

import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type AssetIngestionJob, type AssetLibraryOption, type ComposerOptions } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

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

type UploadTask = { name: string; status: "waiting" | "uploading" | "queued" | "failed"; message?: string };

const uploadStatusLabel: Record<UploadTask["status"], string> = {
  waiting: "等待上传",
  uploading: "计算校验值并上传",
  queued: "上传完成，分析已入队",
  failed: "提交失败",
};

export function AssetHub() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [jobs, setJobs] = useState<AssetIngestionJob[]>([]);
  const [error, setError] = useState("");
  const [offline, setOffline] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [libraryId, setLibraryId] = useState("__new__");
  const [libraryName, setLibraryName] = useState("通用视频素材");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [importMode, setImportMode] = useState<"file" | "url">("file");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [copyrightStatus, setCopyrightStatus] = useState<"owned" | "licensed" | "public_domain">("owned");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [uploadTasks, setUploadTasks] = useState<UploadTask[]>([]);

  const load = useCallback(async () => {
    const session = await adapter.getSession();
    if (!session.ok) {
      setError(session.error.message);
      return;
    }
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

  useEffect(() => {
    if (!jobs.some((job) => job.status === "pending" || job.status === "running")) return;
    const timer = window.setInterval(() => void load(), 8000);
    return () => window.clearInterval(timer);
  }, [jobs, load]);

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
    setUploadTasks(files.map((file) => ({ name: file.name, status: "waiting" })));
    for (const [index, file] of files.entries()) {
      setUploadTasks((current) => current.map((task, taskIndex) => taskIndex === index ? { ...task, status: "uploading" } : task));
      setNotice(`正在上传 ${index + 1} / ${files.length}：${file.name}`);
      const result = await adapter.uploadAsset({
        libraryId: targetLibraryId, file, title: assetTitle(file),
        description: description.trim() || assetTitle(file), copyrightStatus, tags: normalizedTags,
      });
      if (!result.ok) {
        setUploadTasks((current) => current.map((task, taskIndex) => taskIndex === index ? { ...task, status: "failed", message: result.error.message } : task));
        setBusy(false);
        setNotice(`${file.name}：${result.error.message}`);
        return;
      }
      setUploadTasks((current) => current.map((task, taskIndex) => taskIndex === index ? { ...task, status: "queued" } : task));
    }
    const importedCount = files.length;
    setFiles([]);
    setDescription("");
    setTags("");
    setBusy(false);
    setNotice(`已提交 ${importedCount} 个素材；通过版权、扫描和分析门禁后才会变为可用。`);
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
            <Badge tone="accent">最大 5 GB / 文件</Badge>
          </div>
          <div className="toolbar asset-mode-switch" aria-label="素材导入方式">
            <button className={importMode === "file" ? "button-secondary button-small" : "button-ghost button-small"} type="button" aria-pressed={importMode === "file"} onClick={() => setImportMode("file")}>本地文件</button>
            <button className={importMode === "url" ? "button-secondary button-small" : "button-ghost button-small"} type="button" aria-pressed={importMode === "url"} onClick={() => setImportMode("url")}>公开视频链接</button>
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
            <div className="field">
              <span className="field-label">版权状态</span>
              <UiSelect ariaLabel="版权状态" value={copyrightStatus} onChange={(value) => setCopyrightStatus(value as typeof copyrightStatus)}>
                <option value="owned">自有素材</option><option value="licensed">已获得授权</option><option value="public_domain">公共领域</option>
              </UiSelect>
            </div>
            {importMode === "file" ? (
              <label className="field field--full">
                <span className="field-label">选择文件</span>
                <input className="input asset-file-input" type="file" accept="image/*,video/*" multiple required onChange={(event) => setFiles(Array.from(event.target.files ?? []))} />
                <small className="field-help">支持常见图片与视频格式；相同内容会按 SHA-256 去重。</small>
              </label>
            ) : (
              <>
                <label className="field field--full"><span className="field-label">公开视频链接</span><input className="input" type="url" required maxLength={2048} value={remoteUrl} onChange={(event) => setRemoteUrl(event.target.value)} placeholder="YouTube、Bilibili 或小红书公开视频链接" /></label>
                <div className="field field--full remote-rights-confirmation">
                  <input id="remote-rights-confirmed" type="checkbox" checked={rightsConfirmed} onChange={(event) => setRightsConfirmed(event.target.checked)} />
                  <label htmlFor="remote-rights-confirmed"><strong>我确认有权下载并将此素材用于生成视频</strong><small className="field-help">系统会保存原链接、平台、作者、许可证和内容哈希。</small></label>
                </div>
              </>
            )}
            <label className="field field--full"><span className="field-label">画面描述</span><textarea className="textarea" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
            <label className="field field--full"><span className="field-label">检索标签</span><input className="input" value={tags} onChange={(event) => setTags(event.target.value)} placeholder="AI 视频，自动剪辑，电脑，工作室" /></label>
          </div>
          <div className="asset-import-actions">
            <button className="button" type="submit" disabled={busy || offline}>{busy ? "正在导入" : importMode === "url" ? "下载并入库" : `导入 ${files.length || "所选"} 个素材`}</button>
            {notice ? <p className="muted" role="status" aria-live="polite">{notice}</p> : null}
          </div>
          {uploadTasks.length ? <ol className="upload-task-list" aria-label="文件处理状态">{uploadTasks.map((task) => <li key={task.name} data-state={task.status}><span aria-hidden="true" /> <strong>{task.name}</strong><small>{task.message || uploadStatusLabel[task.status]}</small></li>)}</ol> : null}
        </form>
      ) : null}

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
