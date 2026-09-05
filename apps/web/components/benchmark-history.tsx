"use client";

import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createFrameFactoryAdapter, type ApiProblem, type BenchmarkHistoryDetail, type BenchmarkHistoryQuery, type BenchmarkHistorySummary } from "@/lib/api";
import { BenchmarkAccountDemo } from "@/components/benchmark-account-demo";
import { BenchmarkVideoAnalysisPanel } from "@/components/benchmark-video-analysis";
import { Badge, LoadingScaffold, PageHeading, StatePanel } from "@/components/page-heading";

function historyError(problem: ApiProblem): string {
  if (problem.code === "BENCHMARK_HISTORY_NOT_FOUND") return "这条保存记录不存在或不属于当前工作区，请从列表重新选择。";
  if (problem.code === "BENCHMARK_HISTORY_CURSOR_INVALID") return "历史列表已变化，请点击“从头刷新”重新加载。";
  if (problem.status === 404) return "当前对标服务尚未提供历史接口。请重启或升级本机对标服务后重试。";
  if (problem.code === "BENCHMARK_ANALYSIS_UNAVAILABLE") return "本机对标分析与历史存储尚未启用，请按对标分析 SOP 启动服务后重试。";
  if (problem.status === 401 || problem.status === 403) return "当前会话无法读取这个工作区的历史记录，请恢复登录或工作区权限后重试。";
  return problem.message || "历史记录暂时不可用，请重试。";
}

function timestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "未知时间" : date.toLocaleString("zh-CN");
}

export function BenchmarkHistory() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState<"" | "account" | "video">("");
  const [criteria, setCriteria] = useState<BenchmarkHistoryQuery>({});
  const [items, setItems] = useState<BenchmarkHistorySummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<BenchmarkHistoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const listController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);

  const loadRecords = useCallback(async (query: BenchmarkHistoryQuery, cursor?: string) => {
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    setLoading(true);
    setListError("");
    if (!cursor) { setItems([]); setNextCursor(undefined); }
    const result = await adapter.listBenchmarkHistory({ ...query, cursor }, controller.signal);
    if (controller.signal.aborted || listController.current !== controller) return;
    if (result.ok) {
      setItems((previous) => cursor
        ? [...previous, ...result.data.items.filter((item) => !previous.some((saved) => saved.id === item.id))]
        : result.data.items);
      setNextCursor(result.data.nextCursor);
    } else setListError(historyError(result.error));
    setLoading(false);
  }, [adapter]);

  const openRecord = useCallback(async (recordId: string, updateUrl = true) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    setSelectedId(recordId);
    setDetail(null);
    setDetailError("");
    setDetailLoading(Boolean(recordId));
    if (updateUrl) {
      const url = new URL(window.location.href);
      if (recordId) url.searchParams.set("record", recordId);
      else url.searchParams.delete("record");
      window.history.replaceState(null, "", url);
    }
    if (!recordId) return;
    const result = await adapter.getBenchmarkHistory(recordId, controller.signal);
    if (controller.signal.aborted || detailController.current !== controller) return;
    if (result.ok) setDetail(result.data);
    else setDetailError(historyError(result.error));
    setDetailLoading(false);
  }, [adapter]);

  useEffect(() => {
    const restoreSelection = () => void openRecord(new URLSearchParams(window.location.search).get("record") ?? "", false);
    const timer = window.setTimeout(() => { void loadRecords({}); restoreSelection(); }, 0);
    window.addEventListener("popstate", restoreSelection);
    const lists = listController;
    const details = detailController;
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("popstate", restoreSelection);
      lists.current?.abort();
      details.current?.abort();
    };
  }, [loadRecords, openRecord]);

  function searchRecords(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = { kind: kind || undefined, q: search.trim() || undefined };
    setCriteria(query);
    void loadRecords(query);
  }

  return (
    <div className="page page--wide benchmark-demo-page">
      <PageHeading eyebrow="BENCHMARK HISTORY" title="历史分析记录"
        description="查看当前工作区已保存的账号策略和视频报告。搜索、翻页与打开记录只读取保存版本，不会重新采集或调用模型。"
        actions={<a className="button-secondary" href="/benchmarks">返回对标分析</a>} />
      <section className="panel" aria-label="搜索历史分析">
        <form className="benchmark-profile-form" onSubmit={searchRecords}>
          <label htmlFor="benchmark-history-search">标题、账号 ID 或笔记 ID</label>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
            <input id="benchmark-history-search" className="input" style={{ flex: "1 1 260px" }} type="search" maxLength={200} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索已保存的报告" />
            <label htmlFor="benchmark-history-kind">报告类型</label>
            <select id="benchmark-history-kind" className="select" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
              <option value="">全部类型</option><option value="account">账号策略</option><option value="video">视频分析</option>
            </select>
            <button type="submit" className="button">搜索</button>
          </div>
        </form>
        <button type="button" className="button-ghost" disabled={loading} onClick={() => void loadRecords(criteria)}>从头刷新</button>
      </section>

      {loading && !items.length ? <LoadingScaffold title="正在读取历史记录" description="从当前工作区读取保存的报告。" /> : null}
      {listError ? <StatePanel code="HIST" title="历史记录读取失败" description={listError} error>
        <button type="button" className="button" onClick={() => void loadRecords(criteria, nextCursor)}>重试读取</button>
      </StatePanel> : null}
      {!loading && !listError && !items.length ? <StatePanel code="0" title="没有匹配的历史记录" description={criteria.q || criteria.kind ? "调整搜索词或类型后重试。" : "完成账号策略报告或视频分析后，保存的版本会出现在这里。"} /> : null}
      {items.length ? <section className="panel" aria-label="已保存报告列表" aria-busy={loading}>
        <ul style={{ display: "grid", gap: 16, margin: 0, padding: 0, listStyle: "none" }}>
          {items.map((item) => <li key={item.id} style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 16, padding: "16px 0", borderBottom: "1px solid var(--border-subtle)" }}>
            <div style={{ flex: "1 1 260px", minWidth: 0, overflowWrap: "anywhere" }}>
              <Badge>{item.kind === "account" ? "账号策略" : "视频分析"}</Badge>{" "}
              <Badge tone={item.status === "partial" ? "warning" : "success"}>{item.status === "partial" ? "部分完成" : "已完成"}</Badge>
              <h3>{item.title || "未命名报告"}</h3>
              <p>分析于 {timestamp(item.analyzedAt)} · 保存于 {timestamp(item.savedAt)}</p>
              <small>账号 {item.profileUserId}{item.noteId ? ` · 笔记 ${item.noteId}` : ""}</small>
            </div>
            <button type="button" className="button-secondary" aria-pressed={selectedId === item.id} onClick={() => void openRecord(item.id)}>查看保存报告</button>
          </li>)}
        </ul>
        {nextCursor ? <button type="button" className="button" disabled={loading} onClick={() => void loadRecords(criteria, nextCursor)}>{loading ? "正在读取…" : "加载更多"}</button> : <p className="muted">已显示全部匹配记录。</p>}
      </section> : null}

      {selectedId ? <section aria-label="保存报告详情" aria-busy={detailLoading}>
        <button type="button" className="button-ghost" onClick={() => void openRecord("")}>关闭保存报告</button>
        {detailLoading ? <LoadingScaffold title="正在打开保存报告" description="读取当次版本中的证据和结论。" /> : null}
        {detailError ? <StatePanel code="HIST" title="保存报告暂时不可用" description={detailError} error>
          <button type="button" className="button" onClick={() => void openRecord(selectedId)}>重试打开</button>
        </StatePanel> : null}
        {detail?.kind === "account" && detail.accountReport ? <BenchmarkAccountDemo key={detail.id} archivedReport={detail.accountReport} /> : null}
        {detail?.kind === "video" && detail.videoJob ? <>
          {!detail.mediaAvailable ? <p className="muted" role="status">本次视频报告的媒体预览已不可用；保存的文字、时间码和分析结论仍可查看。</p> : null}
          <BenchmarkVideoAnalysisPanel key={detail.id} profileUrl={`https://www.xiaohongshu.com/user/profile/${detail.profileUserId}`} profileUserId={detail.profileUserId} noteId={detail.noteId} title={detail.title} archivedJob={detail.videoJob} />
        </> : null}
      </section> : null}
    </div>
  );
}
