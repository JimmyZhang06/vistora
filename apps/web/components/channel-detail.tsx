"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ApiProblem,
  type Channel,
  type ComposerOptions,
  type Run,
} from "@/lib/api";
import { channelPlatformLabel } from "@/lib/channel-platforms";
import { AccessibleTabs, type TabOption } from "@/components/accessible-tabs";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";

type DetailTab = "overview" | "content" | "brand" | "defaults" | "connections" | "activity";
const tabLabel = "频道详情";
const tabs: readonly TabOption<DetailTab>[] = [
  { id: "overview", label: "概览" },
  { id: "content", label: "内容" },
  { id: "brand", label: "品牌资产" },
  { id: "defaults", label: "创作默认值" },
  { id: "connections", label: "平台连接状态" },
  { id: "activity", label: "操作记录" },
];

const runStatusLabels: Record<Run["status"], string> = {
  queued: "排队中", running: "制作中", awaiting_review: "待审核", retrying: "重试中",
  succeeded: "已完成", failed: "失败", cancelled: "已取消",
};

function isConflict(problem: ApiProblem) {
  return problem.status === 409 || problem.status === 412 || /conflict|precondition|revision/i.test(problem.code);
}

function statusPresentation(status: Channel["status"]) {
  if (status === "active") return { label: "运行中", tone: "success" as const };
  if (status === "paused") return { label: "已暂停", tone: "warning" as const };
  return { label: "已归档", tone: "neutral" as const };
}

function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

export function ChannelDetail({ channelId }: { channelId: string }) {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [channel, setChannel] = useState<Channel | null>(null);
  const [etag, setEtag] = useState("");
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [activeTab, setActiveTab] = useState<DetailTab>("overview");
  const [loading, setLoading] = useState(true);
  const [savingStatus, setSavingStatus] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archiveConfirming, setArchiveConfirming] = useState(false);
  const [error, setError] = useState("");
  const [contentError, setContentError] = useState("");
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setContentError("");
    setConflict(false);
    const session = await adapter.getSession();
    if (!session.ok) {
      setError(session.error.message || "无法读取当前工作空间。");
      setLoading(false);
      return;
    }
    const [channelResult, runsResult, optionsResult] = await Promise.all([
      adapter.getChannel(channelId),
      adapter.listRuns({ workspaceId: session.data.activeWorkspaceId, channelId }),
      adapter.getComposerOptions(session.data.activeWorkspaceId),
    ]);
    if (!channelResult.ok) {
      setError(channelResult.error.message || "无法读取频道。");
      setLoading(false);
      return;
    }
    setChannel(channelResult.data.value);
    setEtag(channelResult.data.etag);
    if (runsResult.ok) setRuns(runsResult.data);
    else {
      setRuns(null);
      setContentError(runsResult.error.message || "无法读取频道内容。");
    }
    if (optionsResult.ok) setOptions(optionsResult.data);
    setLoading(false);
  }, [adapter, channelId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function toggleStatus() {
    if (!channel || channel.status === "archived") return;
    const nextStatus = channel.status === "active" ? "paused" : "active";
    setSavingStatus(true);
    setMessage(nextStatus === "paused" ? "正在暂停频道…" : "正在恢复频道…");
    setConflict(false);
    const result = await adapter.saveChannel({
      id: channel.id,
      workspaceId: channel.workspaceId,
      slug: channel.slug,
      name: channel.name,
      description: channel.description,
      platform: channel.platform,
      handle: channel.handle,
      platformConnectionId: channel.platformConnectionId,
      status: nextStatus,
      defaultComposition: channel.defaultComposition,
      brandConfig: channel.brandConfig,
    }, etag, `web-channel-status-${channel.id}-${crypto.randomUUID()}`);
    setSavingStatus(false);
    if (!result.ok) {
      setConflict(isConflict(result.error));
      setMessage(result.error.message || "频道状态更新失败。");
      return;
    }
    setChannel(result.data.value);
    setEtag(result.data.etag);
    setMessage(nextStatus === "paused" ? "频道已暂停。" : "频道已恢复。 ");
  }

  async function archive() {
    if (!channel || channel.status === "archived" || !etag) return;
    setArchiving(true);
    setConflict(false);
    setMessage("正在归档频道…");
    const result = await adapter.archiveChannel(channel.id, etag);
    setArchiving(false);
    if (!result.ok) {
      setConflict(isConflict(result.error));
      setMessage(result.error.message || "频道归档失败。");
      return;
    }
    setArchiveConfirming(false);
    await load();
    setMessage("频道已归档。");
  }

  if (loading) return <div className="page"><h1 className="sr-only" tabIndex={-1}>频道详情</h1><div className="channel-detail-skeleton" aria-label="正在加载频道详情" aria-live="polite" /></div>;
  if (error || !channel) return <div className="page"><h1 className="sr-only" tabIndex={-1}>频道详情</h1><StatePanel code="ERR" title="无法打开频道" description={error || "服务器没有返回频道记录。"} error><button className="button" type="button" onClick={() => void load()}>重新加载</button><Link className="button-ghost" href="/channels">返回频道列表</Link></StatePanel></div>;

  const displayStatus = statusPresentation(channel.status);
  const skill = options?.skills.find((item) => item.versionId === channel.defaultComposition.skillVersionId);
  const pipeline = options?.pipelines.find((item) => item.id === channel.defaultComposition.pipelineVersionId);
  const voice = options?.voiceProfiles.find((item) => item.id === channel.defaultComposition.voiceProfileId);
  const renderPreset = options?.renderPresets.find((item) => item.id === channel.defaultComposition.renderPresetVersionId);
  const libraries = channel.defaultComposition.assetLibraryIds.map((id) => options?.assetLibraries.find((item) => item.id === id));
  const completedRuns = runs?.filter((run) => run.status === "succeeded").length ?? 0;

  return (
    <div className="page page--wide channel-detail-page">
      <PageHeading
        eyebrow={`06 / CHANNEL · ${channelPlatformLabel(channel.platform) || "UNASSIGNED"}`}
        title={channel.name}
        description={channel.description || "这个频道还没有简介。"}
        actions={<><Badge tone={displayStatus.tone}>{displayStatus.label}</Badge><Link className="button-ghost" href={`/channels/${encodeURIComponent(channel.id)}/edit`}>编辑</Link>{channel.status !== "archived" ? <button className="button-secondary" type="button" disabled={savingStatus || archiving || !etag} onClick={() => void toggleStatus()}>{savingStatus ? "正在更新…" : channel.status === "active" ? "暂停频道" : "恢复频道"}</button> : null}{channel.status !== "archived" ? archiveConfirming ? <span className="channel-archive-confirm" role="group" aria-label="确认归档频道"><button className="button-secondary" type="button" disabled={archiving || !etag} onClick={() => void archive()}>{archiving ? "正在归档…" : "确认归档"}</button><button className="button-ghost" type="button" disabled={archiving} onClick={() => setArchiveConfirming(false)}>取消</button></span> : <button className="button-ghost" type="button" disabled={savingStatus || archiving || !etag} onClick={() => setArchiveConfirming(true)}>归档频道</button> : null}</>}
      />

      {message ? <div className={`alert${conflict ? " alert--error" : ""}`} role={conflict ? "alert" : "status"}><strong>{conflict ? "频道已被其他人更新" : message}</strong>{conflict ? <><p>{message}。刷新后可基于最新 ETag 再次操作。</p><button className="button-secondary button-small" type="button" onClick={() => void load()}>刷新频道</button></> : null}</div> : null}

      <div className="channel-launch-bar" aria-label="频道创作入口"><div><strong>从此频道开始</strong><span>自动带入默认组合，进入表单后仍可逐项覆盖。</span></div><Link className="button" href={`/create?channel=${encodeURIComponent(channel.id)}`}>单条创作</Link><Link className="button-secondary" href={`/batches?channel=${encodeURIComponent(channel.id)}&create=1`}>批量生产</Link></div>

      <div className="channel-tabs-wrap"><AccessibleTabs label={tabLabel} tabs={tabs} activeTab={activeTab} onChange={setActiveTab} /></div>
      <section id={`${tabLabel}-${activeTab}-panel`} role="tabpanel" tabIndex={0} aria-labelledby={`${tabLabel}-${activeTab}-tab`} className="channel-tab-panel">
        {activeTab === "overview" ? <div className="channel-overview-grid">
          <article className="panel channel-metric"><span>内容总数</span><strong>{runs?.length ?? "—"}</strong><small>{contentError ? "读取失败" : "真实 Run 记录"}</small></article>
          <article className="panel channel-metric"><span>已完成</span><strong>{runs ? completedRuns : "—"}</strong><small>成功生成内容</small></article>
          <article className="panel channel-metric"><span>平台账号</span><strong>{channel.handle || "未设置"}</strong><small>{channelPlatformLabel(channel.platform) || "未选择平台"}</small></article>
          <article className="panel channel-summary-card"><p className="eyebrow">CHANNEL INFO</p><dl><div><dt>创建时间</dt><dd>{formatDate(channel.createdAt)}</dd></div><div><dt>最近更新</dt><dd>{formatDate(channel.updatedAt)}</dd></div><div><dt>Slug</dt><dd>{channel.slug}</dd></div><div><dt>Revision</dt><dd>{channel.revision}</dd></div><div><dt>版本标识</dt><dd>{etag || "服务器未返回 ETag"}</dd></div><div><dt>频道 ID</dt><dd>{channel.id}</dd></div></dl></article>
        </div> : null}

        {activeTab === "content" ? <div className="panel channel-section-panel"><div className="section-heading"><div><p className="eyebrow">CONTENT</p><h2>内容</h2></div><Link className="button-secondary button-small" href={`/create?channel=${encodeURIComponent(channel.id)}`}>创建内容</Link></div>{contentError ? <StatePanel code="ERR" title="内容读取失败" description={contentError} error><button className="button" type="button" onClick={() => void load()}>重试</button></StatePanel> : runs?.length ? <div className="channel-content-list">{runs.map((run) => <article key={run.id}><div><h3><Link href={`/projects/${encodeURIComponent(run.id)}`}>{run.topic}</Link></h3><p>{formatDate(run.createdAt)} · {run.id}</p></div><Badge tone={run.status === "succeeded" ? "success" : ["failed", "cancelled"].includes(run.status) ? "warning" : "accent"}>{runStatusLabels[run.status]}</Badge><Link className="button-ghost button-small" href={`/projects/${encodeURIComponent(run.id)}`}>查看</Link></article>)}</div> : <div className="channel-inline-empty"><strong>暂无内容</strong><p>从此频道创建的 Run 会出现在这里。</p><Link className="button" href={`/create?channel=${encodeURIComponent(channel.id)}`}>创建第一条内容</Link></div>}</div> : null}

        {activeTab === "brand" ? <div className="panel channel-section-panel"><div className="section-heading"><div><p className="eyebrow">BRAND ASSETS</p><h2>品牌资产</h2></div><Link className="button-ghost button-small" href={`/channels/${encodeURIComponent(channel.id)}/edit`}>编辑</Link></div>{Object.keys(channel.brandConfig.profile).length || [channel.brandConfig.logoAssetId, channel.brandConfig.introAssetId, channel.brandConfig.outroAssetId, channel.brandConfig.accentColor, channel.brandConfig.fontFamily, channel.brandConfig.guidelines].some(Boolean) ? <dl className="channel-definition-grid"><div><dt>Logo 素材</dt><dd>{channel.brandConfig.logoAssetId || "未设置"}</dd></div><div><dt>片头素材</dt><dd>{channel.brandConfig.introAssetId || "未设置"}</dd></div><div><dt>片尾素材</dt><dd>{channel.brandConfig.outroAssetId || "未设置"}</dd></div><div><dt>强调色</dt><dd>{channel.brandConfig.accentColor || "未设置"}</dd></div><div><dt>字体</dt><dd>{channel.brandConfig.fontFamily || "未设置"}</dd></div><div className="channel-definition-wide"><dt>品牌规范</dt><dd>{channel.brandConfig.guidelines || "未设置"}</dd></div></dl> : <div className="channel-inline-empty"><strong>暂无品牌资产</strong><p>服务器返回的频道记录中没有品牌配置。</p><Link className="button-secondary" href={`/channels/${encodeURIComponent(channel.id)}/edit`}>添加品牌资产</Link></div>}</div> : null}

        {activeTab === "defaults" ? <div className="panel channel-section-panel"><div className="section-heading"><div><p className="eyebrow">DEFAULT COMPOSITION</p><h2>创作默认值</h2></div><Link className="button-ghost button-small" href={`/channels/${encodeURIComponent(channel.id)}/edit`}>编辑</Link></div><dl className="channel-definition-grid"><div><dt>SkillVersion</dt><dd>{skill ? `${skill.skillName} · ${skill.version}` : channel.defaultComposition.skillVersionId || "未设置"}</dd></div><div><dt>PipelineVersion</dt><dd>{pipeline ? `${pipeline.name} · ${pipeline.version}` : channel.defaultComposition.pipelineVersionId || "未设置"}</dd></div><div><dt>声音</dt><dd>{voice ? `${voice.name} · ${voice.provider}` : channel.defaultComposition.voiceProfileId || "未设置"}</dd></div><div><dt>渲染预设</dt><dd>{renderPreset ? `${renderPreset.name} · ${renderPreset.version}` : channel.defaultComposition.renderPresetVersionId || "未设置"}</dd></div><div className="channel-definition-wide"><dt>素材库</dt><dd>{libraries.length ? libraries.map((library, index) => library?.name || channel.defaultComposition.assetLibraryIds[index]).join("、") : "未设置"}</dd></div></dl><p className="channel-section-note">这些值只作为 Run 的初始组合；单条创作和批量生产均可在提交前覆盖。</p></div> : null}

        {activeTab === "connections" ? <div className="panel channel-section-panel"><div className="section-heading"><div><p className="eyebrow">PLATFORM CONNECTIONS</p><h2>平台连接状态</h2></div></div><div className="channel-inline-empty"><strong>连接状态 API 尚未提供</strong><p>{channel.platformConnectionId ? `频道仅保存连接引用 ${channel.platformConnectionId}；当前 Channel API 不返回连接健康状态。` : channel.platform || channel.handle ? `已保存 ${channelPlatformLabel(channel.platform) || "平台"} ${channel.handle || ""}，但当前 Channel API 不返回连接健康状态。` : "当前频道尚未保存平台身份；连接管理接口接入后将在这里显示真实状态。"}</p></div></div> : null}

        {activeTab === "activity" ? <div className="panel channel-section-panel"><div className="section-heading"><div><p className="eyebrow">AUDIT LOG</p><h2>操作记录</h2></div></div><div className="channel-inline-empty"><strong>操作记录 API 尚未提供</strong><p>当前 Channel API 只返回频道自身的 revision 与更新时间；页面不会据此生成或猜测审计事件。</p></div></div> : null}
      </section>
      {tabs.filter((tab) => tab.id !== activeTab).map((tab) => <section key={tab.id} id={`${tabLabel}-${tab.id}-panel`} role="tabpanel" aria-labelledby={`${tabLabel}-${tab.id}-tab`} hidden />)}
    </div>
  );
}
