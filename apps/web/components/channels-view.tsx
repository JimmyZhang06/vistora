"use client";

import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type Channel, type ChannelStatus } from "@/lib/api";
import { CHANNEL_PLATFORM_OPTIONS, channelPlatformLabel } from "@/lib/channel-platforms";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";

const statusOptions: { value: "" | ChannelStatus; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "active", label: "运行中" },
  { value: "paused", label: "已暂停" },
  { value: "archived", label: "已归档" },
];

function channelStatus(channel: Channel) {
  if (channel.status === "active") return { label: "运行中", tone: "success" as const };
  if (channel.status === "paused") return { label: "已暂停", tone: "warning" as const };
  return { label: "已归档", tone: "neutral" as const };
}

export function ChannelsView() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [workspaceId, setWorkspaceId] = useState("");
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState<"" | ChannelStatus>("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const loadChannels = useCallback(async () => {
    setLoading(true);
    setError("");
    let activeWorkspaceId = workspaceId;
    if (!activeWorkspaceId) {
      const session = await adapter.getSession();
      if (!session.ok) {
        setError(session.error.message || "无法读取当前工作空间。");
        setLoading(false);
        return;
      }
      activeWorkspaceId = session.data.activeWorkspaceId;
      setWorkspaceId(activeWorkspaceId);
    }
    const result = await adapter.listChannels({
      workspaceId: activeWorkspaceId,
      search: appliedSearch || undefined,
      platform: platform || undefined,
      status: status || undefined,
    });
    if (result.ok) setChannels(result.data);
    else {
      setChannels(null);
      setError(result.error.message || "频道读取失败。");
    }
    setLoading(false);
  }, [adapter, appliedSearch, platform, status, workspaceId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadChannels(), 0);
    return () => window.clearTimeout(timer);
  }, [loadChannels]);

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setAppliedSearch(search.trim());
  }

  const platformOptions = useMemo(() => {
    const values = new Set<string>(CHANNEL_PLATFORM_OPTIONS.map((option) => option.value));
    channels?.forEach((channel) => {
      if (channel.platform) values.add(channel.platform);
    });
    return [...values].map((value) => ({ value, label: channelPlatformLabel(value) || value }));
  }, [channels]);

  const filtersActive = Boolean(appliedSearch || platform || status);
  const createAction = <Link className="button" href="/channels/new">新建频道</Link>;

  return (
    <div className="page page--wide channel-page">
      <PageHeading
        eyebrow="06 / CHANNELS"
        title="把常用发布配置保存成频道"
        description="管理平台身份、品牌资产与默认创作组合；从频道发起任务时自动带入默认值，本次运行仍可覆盖。"
        actions={createAction}
      />

      <section className="channel-toolbar" aria-label="频道筛选">
        <form className="channel-search" role="search" onSubmit={submitSearch}>
          <label className="sr-only" htmlFor="channel-search">搜索频道</label>
          <input id="channel-search" className="input" type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、简介或账号" />
          <button className="button-secondary button-small" type="submit">搜索</button>
        </form>
        <label className="field channel-filter">
          <span className="sr-only">平台筛选</span>
          <select className="select" value={platform} onChange={(event) => setPlatform(event.target.value)}>
            <option value="">全部平台</option>
            {platformOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
          </select>
        </label>
        <label className="field channel-filter">
          <span className="sr-only">状态筛选</span>
          <select className="select" value={status} onChange={(event) => setStatus(event.target.value as "" | ChannelStatus)}>
            {statusOptions.map((option) => <option value={option.value} key={option.value || "all"}>{option.label}</option>)}
          </select>
        </label>
        {filtersActive ? <button className="button-ghost button-small" type="button" onClick={() => { setSearch(""); setAppliedSearch(""); setPlatform(""); setStatus(""); }}>清除筛选</button> : null}
      </section>

      {error ? <StatePanel code="ERR" title="频道暂时不可用" description={`${error}。页面没有使用本地或 Mock 数据替代服务器结果。`} error><button className="button" type="button" onClick={() => void loadChannels()}>重新加载</button></StatePanel> : null}
      {loading ? <div className="loading-grid" aria-label="正在加载频道" aria-live="polite"><div className="loading-card" /><div className="loading-card" /><div className="loading-card" /></div> : null}

      {!loading && !error && channels?.length === 0 ? (
        <StatePanel code="00" title={filtersActive ? "没有匹配的频道" : "还没有频道"} description={filtersActive ? "调整搜索词或筛选条件后重试。" : "创建频道，保存平台身份、品牌资产与创作默认值。"}>
          {filtersActive ? <button className="button-secondary" type="button" onClick={() => { setSearch(""); setAppliedSearch(""); setPlatform(""); setStatus(""); }}>清除筛选</button> : createAction}
        </StatePanel>
      ) : null}

      {!loading && channels && channels.length > 0 ? (
        <section aria-label="频道列表">
          <div className="channel-list-summary" aria-live="polite"><span>{channels.length} 个频道</span><small>{filtersActive ? "当前筛选结果" : "按最近更新排序"}</small></div>
          <div className="channel-grid">
            {channels.map((channel) => {
              const displayStatus = channelStatus(channel);
              return (
                <article className="channel-card" key={channel.id}>
                  <header><div><p className="eyebrow">{channelPlatformLabel(channel.platform) || "未设置平台"}</p><h2><Link href={`/channels/${encodeURIComponent(channel.id)}`}>{channel.name}</Link></h2></div><Badge tone={displayStatus.tone}>{displayStatus.label}</Badge></header>
                  <p>{channel.description || "暂无频道简介。"}</p>
                  <dl>
                    <div><dt>平台账号</dt><dd>{channel.handle || "未设置"}</dd></div>
                    <div><dt>素材库</dt><dd>{channel.defaultComposition.assetLibraryIds.length}</dd></div>
                    <div><dt>最近更新</dt><dd>{channel.updatedAt ? new Date(channel.updatedAt).toLocaleDateString("zh-CN") : "—"}</dd></div>
                  </dl>
                  <footer><Link className="button-ghost button-small" href={`/channels/${encodeURIComponent(channel.id)}`}>查看详情</Link><Link className="button-secondary button-small" href={`/create?channel=${encodeURIComponent(channel.id)}`}>用此频道创作</Link></footer>
                </article>
              );
            })}
          </div>
        </section>
      ) : null}

      <section className="panel channel-guide"><div><p className="eyebrow">DEFAULT COMPOSITION</p><h2>频道只保存默认值</h2><p className="muted">SkillVersion、PipelineVersion、素材库、声音和渲染预设会作为起点；每次 Run 可以独立覆盖，并保存最终组合快照。</p></div><Link className="button-ghost" href="/create">直接开始创作</Link></section>
    </div>
  );
}
