"use client";

import Link from "next/link";
import Image from "next/image";
import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createFrameFactoryAdapter, type ApiResult, type Asset, type AssetBulkRequest, type AssetPoster, type AssetQuery } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

const PAGE_SIZE = 50;
type ViewMode = "grid" | "table";

const statusLabel: Record<string, string> = { ready: "可用", processing: "处理中", quarantined: "隔离", archived: "已禁用", deleted: "已删除" };
const copyrightLabel: Record<string, string> = { unknown: "待补充", owned: "自有", licensed: "已授权", public_domain: "公共领域", restricted: "受限" };

function processingLabel(asset: Asset) {
  if (asset.analysisStatus === "failed" || asset.file?.scanStatus === "failed" || asset.file?.scanStatus === "rejected") return "处理失败";
  if (asset.analysisStatus === "completed") return "分析完成";
  if (asset.analysisStatus === "running") return "字幕 / 镜头 / 标签分析中";
  if (asset.file?.scanStatus === "pending") return "已上传，等待安全扫描";
  return "等待分析任务";
}

export function AssetLibraryWorkbench({ libraryId }: { libraryId: string }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [libraryName, setLibraryName] = useState("素材库");
  const [totalCount, setTotalCount] = useState(0);
  const [cursor, setCursor] = useState<string>();
  const cursorRef = useRef<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [offline, setOffline] = useState(false);
  const [view, setView] = useState<ViewMode>("grid");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState<AssetQuery>({ limit: PAGE_SIZE });
  const [bulkAction, setBulkAction] = useState("review_approve");
  const [bulkTags, setBulkTags] = useState("");
  const [notice, setNotice] = useState("");
  const [mutating, setMutating] = useState(false);
  const mutationKeys = useRef(new Map<string, string>());
  const loadPoster = useCallback((assetId: string) => adapter.getAssetPoster(assetId), [adapter]);

  const loadAssets = useCallback(async (append = false) => {
    if (append) setLoadingMore(true);
    else setLoading(true);
    const result = await adapter.listAssets(libraryId, { ...query, cursor: append ? cursorRef.current : undefined, limit: PAGE_SIZE });
    if (!result.ok) {
      setError(result.error.message);
    } else {
      setAssets((current) => append ? [...current, ...result.data.data] : result.data.data);
      setCursor(result.data.nextCursor);
      cursorRef.current = result.data.nextCursor;
      setTotalCount(result.data.totalCount);
      setError("");
    }
    setLoading(false);
    setLoadingMore(false);
  }, [adapter, libraryId, query]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadAssets(false), 0);
    return () => window.clearTimeout(timer);
  }, [libraryId, query, loadAssets]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void adapter.getSession().then(async (session) => {
        if (!session.ok) return;
        const options = await adapter.getComposerOptions(session.data.activeWorkspaceId);
        if (options.ok) setLibraryName(options.data.assetLibraries.find((item) => item.id === libraryId)?.name ?? "素材库");
      });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [adapter, libraryId]);

  useEffect(() => {
    const sync = () => setOffline(!navigator.onLine);
    window.addEventListener("online", sync); window.addEventListener("offline", sync); sync();
    return () => { window.removeEventListener("online", sync); window.removeEventListener("offline", sync); };
  }, []);

  useEffect(() => {
    if (!assets.some((asset) => asset.status === "processing" || asset.analysisStatus === "running" || asset.analysisStatus === "pending")) return;
    const timer = window.setInterval(() => void loadAssets(false), 5000);
    return () => window.clearInterval(timer);
  }, [assets, loadAssets]);

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setSelected(new Set());
    setQuery((current) => ({ ...current, search: searchInput.trim() || undefined }));
  }

  function updateFilter<K extends keyof AssetQuery>(key: K, value: AssetQuery[K]) {
    setSelected(new Set());
    setQuery((current) => ({ ...current, [key]: value || undefined }));
  }

  function toggle(assetId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  }

  async function applyBulk() {
    if (!selected.size || offline) return;
    const items = assets
      .filter((asset) => selected.has(asset.id))
      .map((asset) => ({ assetId: asset.id, revision: asset.revision }));
    const ids = items.map((item) => item.assetId);
    let request: AssetBulkRequest;
    if (bulkAction === "review_approve") request = { items, action: "review", decision: "approve" };
    else if (bulkAction === "review_reject") request = { items, action: "review", decision: "reject" };
    else if (bulkAction === "reanalyze") request = { items, action: "reanalyze" };
    else if (bulkAction === "disable") request = { items, action: "disable" };
    else {
      const tags = bulkTags.split(/[，,\n]/).map((item) => item.trim()).filter(Boolean);
      if (!tags.length) { setNotice("请输入至少一个标签。"); return; }
      request = { items, action: "add_tags", tags };
    }
    if (["review_reject", "disable"].includes(bulkAction) && !window.confirm(`确认对 ${ids.length} 个素材执行此危险操作？`)) return;
    setMutating(true);
    setNotice("正在提交批量操作…");
    const signature = JSON.stringify(request);
    const key = mutationKeys.current.get(signature) ?? `bulk-assets:${globalThis.crypto.randomUUID()}`;
    mutationKeys.current.set(signature, key);
    const result = await adapter.bulkUpdateAssets(request, key);
    setMutating(false);
    if (!result.ok) { setNotice(result.error.message); return; }
    mutationKeys.current.delete(signature);
    setNotice(`服务端已接受 ${result.data.acceptedCount} 个素材${result.data.jobId ? `，任务 ${result.data.jobId}` : ""}。`);
    setSelected(new Set());
    await loadAssets(false);
  }

  const allLoadedSelected = assets.length > 0 && assets.every((asset) => selected.has(asset.id));
  return (
    <div className="page page--wide page--asset-workbench">
      <PageHeading eyebrow="05 / ASSETS / LIBRARY" title={libraryName} description={`默认每页 ${PAGE_SIZE} 条，按 cursor 增量加载；当前匹配 ${totalCount.toLocaleString("zh-CN")} 条。`} actions={<Link className="button-secondary" href="/assets">返回素材库</Link>} />
      {offline ? <div className="connection-banner" role="status">离线状态：筛选和已加载内容仍可查看，审核与批量操作已暂停。</div> : null}

      <section className="asset-workbench-toolbar" aria-label="素材筛选">
        <form className="asset-search" onSubmit={applySearch} role="search">
          <label className="sr-only" htmlFor="asset-search">搜索素材</label>
          <input id="asset-search" className="input" type="search" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="搜索标题、描述、人物、地点或关键词" />
          <button className="button-secondary asset-search-submit" type="submit">搜索</button>
        </form>
        <div className="asset-filter-grid">
          <UiSelect ariaLabel="状态筛选" value={query.status ?? ""} onChange={(value) => updateFilter("status", value as AssetQuery["status"])}><option value="">全部状态</option><option value="ready">可用</option><option value="processing">处理中</option><option value="quarantined">隔离</option><option value="archived">已禁用</option><option value="deleted">已删除</option></UiSelect>
          <UiSelect ariaLabel="类型筛选" value={query.kind ?? ""} onChange={(value) => updateFilter("kind", value as AssetQuery["kind"])}><option value="">全部类型</option><option value="video">视频</option><option value="image">图片</option><option value="audio">音频</option></UiSelect>
          <UiSelect ariaLabel="版权筛选" value={query.copyrightStatus ?? ""} onChange={(value) => updateFilter("copyrightStatus", value as AssetQuery["copyrightStatus"])}><option value="">全部版权</option><option value="unknown">待补充</option><option value="owned">自有</option><option value="licensed">已授权</option><option value="public_domain">公共领域</option><option value="restricted">受限</option></UiSelect>
          <UiSelect ariaLabel="审核筛选" value={query.reviewStatus ?? ""} onChange={(value) => updateFilter("reviewStatus", value as AssetQuery["reviewStatus"])}><option value="">全部审核</option><option value="pending">待审核</option><option value="approved">已通过</option><option value="rejected">已拒绝</option></UiSelect>
          <input className="input" aria-label="标签筛选" value={query.tag ?? ""} onChange={(event) => updateFilter("tag", event.target.value)} placeholder="标签" />
        </div>
        <div className="view-switch" role="group" aria-label="视图模式"><button type="button" className={view === "grid" ? "button-secondary button-small" : "button-ghost button-small"} aria-pressed={view === "grid"} onClick={() => setView("grid")}>网格</button><button type="button" className={view === "table" ? "button-secondary button-small" : "button-ghost button-small"} aria-pressed={view === "table"} onClick={() => setView("table")}>表格</button></div>
      </section>

      {selected.size ? <section className="bulk-bar" aria-label="批量操作"><strong>已选 {selected.size} 项</strong><UiSelect ariaLabel="批量操作" value={bulkAction} onChange={setBulkAction}><option value="review_approve">批量审核通过</option><option value="review_reject">批量审核拒绝</option><option value="add_tags">批量添加标签</option><option value="reanalyze">批量重新分析</option><option value="disable">批量禁用</option></UiSelect>{bulkAction === "add_tags" ? <input className="input" value={bulkTags} onChange={(event) => setBulkTags(event.target.value)} placeholder="标签，以逗号分隔" aria-label="要添加的标签" /> : null}<button className="button" type="button" onClick={() => void applyBulk()} disabled={mutating || offline}>{mutating ? "提交中" : "执行"}</button><button className="button-ghost" type="button" onClick={() => setSelected(new Set())}>取消选择</button></section> : null}
      {notice ? <p className="operation-notice" role="status" aria-live="polite">{notice}</p> : null}

      {loading ? <div className="asset-skeleton-grid" aria-label="正在加载素材">{Array.from({ length: 8 }, (_, index) => <i key={index} />)}</div> : null}
      {!loading && error ? <StatePanel code="ERR" title="素材列表加载失败" description={error} error><button className="button-secondary" type="button" onClick={() => void loadAssets(false)}>重试</button></StatePanel> : null}
      {!loading && !error && !assets.length ? <StatePanel code="00" title="没有匹配的素材" description="调整搜索或筛选条件，或返回素材库导入新素材。"><button className="button-secondary" type="button" onClick={() => { setSearchInput(""); setQuery({ limit: PAGE_SIZE }); }}>清除筛选</button></StatePanel> : null}

      {!loading && assets.length ? <>
        <label className="select-page"><input type="checkbox" checked={allLoadedSelected} onChange={() => setSelected(allLoadedSelected ? new Set() : new Set(assets.map((asset) => asset.id)))} />选择当前已加载的 {assets.length} 项</label>
        {view === "grid" ? <div className="asset-grid">{assets.map((asset) => <AssetCard key={asset.id} asset={asset} selected={selected.has(asset.id)} onToggle={() => toggle(asset.id)} loadPoster={loadPoster} />)}</div> : <AssetTable assets={assets} selected={selected} onToggle={toggle} />}
        <div className="load-more"><span>已加载 {assets.length} / {totalCount}</span>{cursor ? <button className="button-secondary" type="button" onClick={() => void loadAssets(true)} disabled={loadingMore}>{loadingMore ? "加载中" : `再加载 ${PAGE_SIZE} 条`}</button> : <Badge tone="success">已到末尾</Badge>}</div>
      </> : null}
    </div>
  );
}

function AssetCard({ asset, selected, onToggle, loadPoster }: { asset: Asset; selected: boolean; onToggle: () => void; loadPoster: (assetId: string) => Promise<ApiResult<AssetPoster>> }) {
  return <article className="asset-card" data-selected={selected || undefined}>
    <div className="asset-card-media"><LazyAssetPoster asset={asset} loadPoster={loadPoster} /><label><input type="checkbox" checked={selected} onChange={onToggle} aria-label={`选择 ${asset.title}`} /></label><Badge tone={asset.status === "ready" ? "success" : asset.status === "quarantined" ? "warning" : "neutral"}>{statusLabel[asset.deletedAt ? "deleted" : asset.status]}</Badge></div>
    <div className="asset-card-copy"><p>{asset.kind.toUpperCase()} · {copyrightLabel[asset.copyrightStatus]}</p><h2><Link href={`/assets/${encodeURIComponent(asset.libraryId)}/${encodeURIComponent(asset.id)}`}>{asset.title}</Link></h2>{asset.status === "processing" ? <p className="asset-processing-label" role="status">{processingLabel(asset)}</p> : null}<div className="asset-tags">{asset.tags.slice(0, 4).map((tag) => <span key={`${tag.source}-${tag.name}`}>{tag.name}</span>)}{!asset.tags.length ? <span>无标签</span> : null}</div></div>
  </article>;
}

function LazyAssetPoster({ asset, loadPoster }: { asset: Asset; loadPoster: (assetId: string) => Promise<ApiResult<AssetPoster>> }) {
  const container = useRef<HTMLDivElement | null>(null);
  const [url, setUrl] = useState(asset.thumbnailUrl ?? "");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (url || !asset.posterAvailable || failed) return;
    const node = container.current;
    if (!node) return;
    let cancelled = false;
    const load = () => void loadPoster(asset.id).then((result) => {
      if (cancelled) return;
      if (result.ok) setUrl(result.data.url);
      else setFailed(true);
    });
    if (!("IntersectionObserver" in window)) {
      load();
      return () => { cancelled = true; };
    }
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      load();
    }, { rootMargin: "320px" });
    observer.observe(node);
    return () => { cancelled = true; observer.disconnect(); };
  }, [asset.id, asset.posterAvailable, failed, loadPoster, url]);

  return <div ref={container} className="asset-poster" data-loading={asset.posterAvailable && !url && !failed || undefined}>
    {url ? <Image unoptimized src={url} alt="" width={640} height={400} onError={() => { setUrl(""); setFailed(true); }} /> : <span aria-hidden="true">{asset.kind === "video" ? "▶" : "▧"}</span>}
  </div>;
}

function AssetTable({ assets, selected, onToggle }: { assets: Asset[]; selected: Set<string>; onToggle: (id: string) => void }) {
  return <div className="asset-table-wrap"><table className="asset-table"><thead><tr><th scope="col">选择</th><th scope="col">素材</th><th scope="col">状态</th><th scope="col">当前阶段</th><th scope="col">版权</th><th scope="col">扫描</th><th scope="col">标签</th><th scope="col">更新</th></tr></thead><tbody>{assets.map((asset) => <tr key={asset.id}><td><input type="checkbox" checked={selected.has(asset.id)} onChange={() => onToggle(asset.id)} aria-label={`选择 ${asset.title}`} /></td><th scope="row"><Link href={`/assets/${encodeURIComponent(asset.libraryId)}/${encodeURIComponent(asset.id)}`}>{asset.title}</Link><small>{asset.kind}</small></th><td><Badge tone={asset.status === "ready" ? "success" : asset.status === "quarantined" ? "warning" : "neutral"}>{statusLabel[asset.deletedAt ? "deleted" : asset.status]}</Badge></td><td>{processingLabel(asset)}</td><td>{copyrightLabel[asset.copyrightStatus]}</td><td>{asset.file?.scanStatus ?? "—"}</td><td>{asset.tags.slice(0, 3).map((tag) => tag.name).join("、") || "—"}</td><td><time dateTime={asset.updatedAt}>{asset.updatedAt ? new Date(asset.updatedAt).toLocaleDateString("zh-CN") : "—"}</time></td></tr>)}</tbody></table></div>;
}
