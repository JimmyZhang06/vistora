"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ApiProblem,
  type Channel,
  type ChannelBrandConfig,
  type ComposerOptions,
  type RunComposition,
} from "@/lib/api";
import { CHANNEL_PLATFORM_OPTIONS, normalizeChannelPlatform } from "@/lib/channel-platforms";
import { PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

const emptyComposition: RunComposition = { skillVersionId: "", pipelineVersionId: "", assetLibraryIds: [] };
const emptyBrand: ChannelBrandConfig = { profile: {} };

function isConflict(problem: ApiProblem) {
  return problem.status === 409 || problem.status === 412 || /conflict|precondition|revision/i.test(problem.code);
}

function compositionFromOptions(options: ComposerOptions): RunComposition {
  return {
    skillVersionId: options.skills[0]?.versionId ?? "",
    pipelineVersionId: options.pipelines[0]?.id ?? "",
    assetLibraryIds: [],
    voiceProfileId: undefined,
    renderPresetVersionId: undefined,
  };
}

export function ChannelForm({ channelId }: { channelId?: string }) {
  const editing = Boolean(channelId);
  const router = useRouter();
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [workspaceId, setWorkspaceId] = useState("");
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [etag, setEtag] = useState("");
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [platform, setPlatform] = useState("");
  const [handle, setHandle] = useState("");
  const [platformConnectionId, setPlatformConnectionId] = useState("");
  const [status, setStatus] = useState<Channel["status"]>("active");
  const [composition, setComposition] = useState<RunComposition>(emptyComposition);
  const [brandConfig, setBrandConfig] = useState<ChannelBrandConfig>(emptyBrand);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [conflict, setConflict] = useState(false);

  const applyChannel = useCallback((channel: Channel, versionEtag: string) => {
    setName(channel.name);
    setSlug(channel.slug);
    setDescription(channel.description);
    setPlatform(channel.platform ?? "");
    setHandle(channel.handle ?? "");
    setPlatformConnectionId(channel.platformConnectionId ?? "");
    setStatus(channel.status);
    setComposition(channel.defaultComposition);
    setBrandConfig(channel.brandConfig);
    setEtag(versionEtag);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setConflict(false);
    const session = await adapter.getSession();
    if (!session.ok) {
      setError(session.error.message || "无法读取当前工作空间。");
      setLoading(false);
      return;
    }
    const activeWorkspaceId = session.data.activeWorkspaceId;
    setWorkspaceId(activeWorkspaceId);
    const [optionsResult, channelResult] = await Promise.all([
      adapter.getComposerOptions(activeWorkspaceId),
      channelId ? adapter.getChannel(channelId) : Promise.resolve(null),
    ]);
    if (!optionsResult.ok) {
      setError(optionsResult.error.message || "无法读取频道可用资源。");
      setLoading(false);
      return;
    }
    setOptions(optionsResult.data);
    if (channelResult) {
      if (!channelResult.ok) {
        setError(channelResult.error.status === 404 || channelResult.error.status === 422
          ? "这个频道不存在，可能已被归档、删除，或不属于当前创作空间。"
          : channelResult.error.message || "无法读取频道。");
        setLoading(false);
        return;
      }
      applyChannel(channelResult.data.value, channelResult.data.etag);
    } else {
      setComposition(compositionFromOptions(optionsResult.data));
    }
    setLoading(false);
  }, [adapter, applyChannel, channelId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setMessage("请输入频道名称。");
      document.querySelector<HTMLInputElement>("#channel-name")?.focus();
      return;
    }
    if (!composition.skillVersionId || !composition.pipelineVersionId) {
      setMessage("默认组合必须包含 SkillVersion 和 PipelineVersion。");
      return;
    }
    if ((handle.trim() || platformConnectionId.trim()) && !platform) {
      setMessage("填写平台账号或连接 ID 时，必须先选择平台。");
      document.querySelector<HTMLSelectElement>("#channel-platform")?.focus();
      return;
    }
    setSubmitting(true);
    setMessage("正在保存频道…");
    setConflict(false);
    const result = await adapter.saveChannel({
      id: channelId,
      workspaceId,
      slug: slug.trim() || undefined,
      name: name.trim(),
      description: description.trim(),
      platform: normalizeChannelPlatform(platform) || undefined,
      handle: handle.trim() || undefined,
      platformConnectionId: platformConnectionId.trim() || undefined,
      status,
      defaultComposition: composition,
      brandConfig,
    }, editing ? etag : undefined, `web-channel-${editing ? "replace" : "create"}-${crypto.randomUUID()}`);
    setSubmitting(false);
    if (!result.ok) {
      setConflict(isConflict(result.error));
      setMessage(result.error.message || "频道保存失败。");
      return;
    }
    setEtag(result.data.etag);
    setMessage("频道已保存。");
    router.push(`/channels/${encodeURIComponent(result.data.value.id)}`);
  }

  function toggleLibrary(libraryId: string) {
    setComposition((current) => ({
      ...current,
      assetLibraryIds: current.assetLibraryIds.includes(libraryId)
        ? current.assetLibraryIds.filter((id) => id !== libraryId)
        : [...current.assetLibraryIds, libraryId],
    }));
  }

  if (loading) return <div className="page"><h1 className="sr-only" tabIndex={-1}>{editing ? "编辑频道" : "新建频道"}</h1><div className="channel-form-skeleton" aria-label="正在加载频道表单" aria-live="polite" /></div>;
  if (error) return <div className="page"><h1 className="sr-only" tabIndex={-1}>{editing ? "编辑频道" : "新建频道"}</h1><StatePanel code="ERR" title={editing ? "无法打开频道编辑器" : "无法创建频道"} description={error} error><button className="button" type="button" onClick={() => void load()}>重新加载</button><Link className="button-ghost" href="/channels">返回频道列表</Link></StatePanel></div>;

  const missingRequiredOptions = !options?.skills.length || !options.pipelines.length;

  return (
    <div className="page channel-form-page">
      <PageHeading
        eyebrow={editing ? "06 / CHANNEL / EDIT" : "06 / CHANNEL / NEW"}
        title={editing ? "编辑频道" : "创建频道"}
        description="保存平台身份、品牌资产与默认创作组合。所有默认值都可在单次 Run 中覆盖。"
        actions={<Link className="button-ghost" href={channelId ? `/channels/${encodeURIComponent(channelId)}` : "/channels"}>取消</Link>}
      />

      {options?.channelProblem ? <p className="alert" role="status">频道列表服务返回：{options.channelProblem.message}。这不会用本地数据替代当前表单。</p> : null}
      {missingRequiredOptions ? <StatePanel code="SETUP" title="缺少可用的默认组合" description="创建频道前，需要至少一个已发布 SkillVersion 和 PipelineVersion。"><Link className="button" href="/skills">查看 Skill</Link></StatePanel> : null}

      {!missingRequiredOptions ? (
        <form className="channel-form" onSubmit={submit}>
          {message ? <div className={`alert${conflict ? " alert--error" : ""}`} role={conflict ? "alert" : "status"}><strong>{conflict ? "检测到版本冲突" : message}</strong>{conflict ? <><p>{message}。你的表单内容仍保留；刷新后可基于服务器最新版本重新编辑。</p><button className="button-secondary button-small" type="button" onClick={() => void load()}>载入最新版本</button></> : null}</div> : null}

          <section className="panel channel-form-section" aria-labelledby="channel-identity-heading">
            <div className="section-heading"><div><p className="eyebrow">01 / IDENTITY</p><h2 id="channel-identity-heading">概览与平台身份</h2></div></div>
            <div className="channel-fields channel-fields--two">
              <label className="field" htmlFor="channel-name"><span>频道名称 *</span><input id="channel-name" className="input" value={name} maxLength={160} required onChange={(event) => setName(event.target.value)} /></label>
              <label className="field" htmlFor="channel-slug"><span>Slug</span><input id="channel-slug" className="input" value={slug} maxLength={80} pattern="^[a-z0-9]+(?:-[a-z0-9]+)*$" onChange={(event) => setSlug(event.target.value.toLocaleLowerCase("en-US"))} placeholder="留空时按名称自动生成" /><small className="field-help">仅允许小写字母、数字和连字符，创建后仍可显式修改。</small></label>
              <div className="field"><span className="field-label">平台</span><UiSelect ariaLabel="平台" value={platform} onChange={setPlatform}><option value="">未设置</option>{platform && !CHANNEL_PLATFORM_OPTIONS.some((option) => option.value === platform) ? <option value={platform}>{platform}</option> : null}{CHANNEL_PLATFORM_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}</UiSelect></div>
              <label className="field"><span>平台账号 / Handle</span><input className="input" value={handle} maxLength={300} onChange={(event) => setHandle(event.target.value)} placeholder="例如 @vistora" /></label>
              <label className="field"><span>平台连接 ID</span><input className="input" value={platformConnectionId} onChange={(event) => setPlatformConnectionId(event.target.value)} placeholder="仅保存连接引用，不保存凭据" /></label>
              {editing ? <div className="field"><span className="field-label">状态</span><UiSelect ariaLabel="频道状态" value={status} onChange={(value) => setStatus(value as Channel["status"])}><option value="active">运行中</option><option value="paused">已暂停</option><option value="archived">已归档</option></UiSelect></div> : null}
            </div>
            <label className="field"><span>频道简介</span><textarea className="textarea" value={description} maxLength={4000} onChange={(event) => setDescription(event.target.value)} placeholder="说明频道受众、主题与发布边界" /></label>
          </section>

          <section className="panel channel-form-section" aria-labelledby="channel-composition-heading">
            <div className="section-heading"><div><p className="eyebrow">02 / DEFAULT COMPOSITION</p><h2 id="channel-composition-heading">创作默认值</h2></div><small className="muted">本次 Run 可覆盖</small></div>
            <div className="channel-fields channel-fields--two">
              <div className="field"><span className="field-label">SkillVersion *</span><UiSelect ariaLabel="默认 SkillVersion" value={composition.skillVersionId} onChange={(value) => setComposition((current) => ({ ...current, skillVersionId: value }))}>{options?.skills.map((skill) => <option value={skill.versionId} key={skill.versionId}>{skill.skillName} · {skill.version}</option>)}</UiSelect></div>
              <div className="field"><span className="field-label">PipelineVersion *</span><UiSelect ariaLabel="默认 PipelineVersion" value={composition.pipelineVersionId} onChange={(value) => setComposition((current) => ({ ...current, pipelineVersionId: value }))}>{composition.pipelineVersionId && !options?.pipelines.some((item) => item.id === composition.pipelineVersionId) ? <option value={composition.pipelineVersionId}>当前引用 · {composition.pipelineVersionId}</option> : null}{options?.pipelines.map((pipeline) => <option value={pipeline.id} key={pipeline.id}>{pipeline.name} · {pipeline.version}</option>)}</UiSelect></div>
              <div className="field"><span className="field-label">声音</span><UiSelect ariaLabel="默认声音" value={composition.voiceProfileId ?? ""} onChange={(value) => setComposition((current) => ({ ...current, voiceProfileId: value || undefined }))}><option value="">不设置</option>{composition.voiceProfileId && !options?.voiceProfiles.some((item) => item.id === composition.voiceProfileId) ? <option value={composition.voiceProfileId}>当前引用 · {composition.voiceProfileId}</option> : null}{options?.voiceProfiles.map((voice) => <option value={voice.id} key={voice.id}>{voice.name} · {voice.provider}</option>)}</UiSelect></div>
              <div className="field"><span className="field-label">渲染预设</span><UiSelect ariaLabel="默认渲染预设" value={composition.renderPresetVersionId ?? ""} onChange={(value) => setComposition((current) => ({ ...current, renderPresetVersionId: value || undefined }))}><option value="">不设置</option>{composition.renderPresetVersionId && !options?.renderPresets.some((item) => item.id === composition.renderPresetVersionId) ? <option value={composition.renderPresetVersionId}>当前引用 · {composition.renderPresetVersionId}</option> : null}{options?.renderPresets.map((preset) => <option value={preset.id} key={preset.id}>{preset.name} · {preset.version}</option>)}</UiSelect></div>
            </div>
            <fieldset className="channel-library-picker"><legend>素材库</legend>{options?.assetLibraries.length ? options.assetLibraries.map((library) => <label key={library.id}><input type="checkbox" aria-label={`选择素材库 ${library.name}`} checked={composition.assetLibraryIds.includes(library.id)} onChange={() => toggleLibrary(library.id)} /><span><strong>{library.name}</strong><small>{library.description || `${library.assetCount} 个素材`}</small></span></label>) : <p className="channel-inline-empty">当前工作空间没有素材库，可先保存空素材组合。</p>}</fieldset>
          </section>

          <section className="panel channel-form-section" aria-labelledby="channel-brand-heading">
            <div className="section-heading"><div><p className="eyebrow">03 / BRAND ASSETS</p><h2 id="channel-brand-heading">品牌资产</h2></div></div>
            <div className="channel-fields channel-fields--two">
              <label className="field"><span>Logo 素材 ID</span><input className="input" value={brandConfig.logoAssetId ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, logoAssetId: event.target.value || undefined }))} /></label>
              <label className="field"><span>品牌强调色</span><input className="input" value={brandConfig.accentColor ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, accentColor: event.target.value || undefined }))} placeholder="#FF6B35" pattern="^#[0-9A-Fa-f]{6}$" /></label>
              <label className="field"><span>片头素材 ID</span><input className="input" value={brandConfig.introAssetId ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, introAssetId: event.target.value || undefined }))} /></label>
              <label className="field"><span>片尾素材 ID</span><input className="input" value={brandConfig.outroAssetId ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, outroAssetId: event.target.value || undefined }))} /></label>
              <label className="field"><span>品牌字体</span><input className="input" value={brandConfig.fontFamily ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, fontFamily: event.target.value || undefined }))} /></label>
            </div>
            <label className="field"><span>品牌规范</span><textarea className="textarea" value={brandConfig.guidelines ?? ""} onChange={(event) => setBrandConfig((current) => ({ ...current, guidelines: event.target.value || undefined }))} placeholder="Logo 安全区、用色、字幕、片头片尾等规则" /></label>
          </section>

          <footer className="channel-form-actions theme-inverse"><Link className="button-ghost" href={channelId ? `/channels/${encodeURIComponent(channelId)}` : "/channels"}>取消</Link><button className="button" type="submit" disabled={submitting || !workspaceId}>{submitting ? "正在保存…" : editing ? "保存频道" : "创建频道"}</button></footer>
        </form>
      ) : null}
    </div>
  );
}
