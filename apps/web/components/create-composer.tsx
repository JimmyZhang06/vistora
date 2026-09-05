"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  createFrameFactoryAdapter,
  type ComposerOptions,
  type CreationPreferences,
  type RunEstimate,
  type VideoSettings,
} from "@/lib/api";
import {
  emptyComposerSelection,
  selectionFromComposerOptions,
  toggleAssetLibrarySelection,
  type ComposerSelection,
} from "@/lib/channel-composition";
import { Badge, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

type EstimateState = "idle" | "checking" | "ready" | "server" | "error";

const promptStarters = [
  "解释一个正在改变行业的趋势",
  "讲述一个品牌从零开始的转折点",
  "把复杂知识改写成三分钟故事",
] as const;

const acquisitionSources = [
  { value: "wikimedia", mark: "W", name: "Wikimedia 公版", note: "优先检索公有领域与可复用条目" },
  { value: "youtube", mark: "Y", name: "YouTube", note: "仅在下载器已配置且许可可核验时使用" },
  { value: "bilibili", mark: "B", name: "Bilibili", note: "仅在下载器已配置且许可可核验时使用" },
] as const;

const fallbackVideoSettings: VideoSettings = {
  language: "zh-CN",
  aspectRatio: "16:9",
  targetDurationSeconds: 180,
  visibility: "private",
  autoQualityCheck: true,
  layout: "full_frame",
  mediaFit: "cover",
  frameRate: 30,
  subtitles: { enabled: true, position: "bottom", size: "medium", maxLines: 2 },
  assetAcquisition: {
    enabled: true,
    sources: ["wikimedia"],
    maxAssets: 3,
    copyrightStatus: "public_domain",
    rightsConfirmed: false,
  },
  noAssetDraft: { enabled: false },
};

function videoSettingsFromPreferences(preferences: CreationPreferences): VideoSettings {
  return {
    ...fallbackVideoSettings,
    language: preferences.defaultLanguage,
    aspectRatio: preferences.defaultAspectRatio,
    targetDurationSeconds: preferences.defaultDurationSeconds,
    visibility: preferences.defaultVisibility,
    autoQualityCheck: preferences.autoQualityCheck,
  };
}

export function CreateComposer() {
  const router = useRouter();
  const [workspaceId, setWorkspaceId] = useState("");
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [selection, setSelection] = useState<ComposerSelection>(emptyComposerSelection);
  const [topic, setTopic] = useState("");
  const [researchMode, setResearchMode] = useState<"off" | "when_missing" | "required">("when_missing");
  const [researchSources, setResearchSources] = useState("");
  const [topicError, setTopicError] = useState("");
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [estimateState, setEstimateState] = useState<EstimateState>("idle");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [message, setMessage] = useState("");
  const [createdRunId, setCreatedRunId] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [videoSettings, setVideoSettings] = useState<VideoSettings>(fallbackVideoSettings);
  const [replacementSourceId, setReplacementSourceId] = useState("");

  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const researchSourceUrls = useMemo(
    () => researchSources.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
    [researchSources],
  );

  const loadComposer = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    const sessionResult = await adapter.getSession();
    if (!sessionResult.ok) {
      setWorkspaceId("");
      setOptions(null);
      setLoadError(sessionResult.error.message || "无法连接控制服务");
      setLoading(false);
      return;
    }

    const activeWorkspaceId = sessionResult.data.activeWorkspaceId;
    setWorkspaceId(activeWorkspaceId);
    const [optionsResult, preferencesResult] = await Promise.all([
      adapter.getComposerOptions(activeWorkspaceId),
      adapter.getCreationPreferences(),
    ]);
    if (!optionsResult.ok) {
      setOptions(null);
      setLoadError(optionsResult.error.message || "无法读取创作资源");
      setLoading(false);
      return;
    }
    const searchParams = new URLSearchParams(window.location.search);
    const requestedChannelId = searchParams.get("channel") ?? "";
    const requestedReplacementId = searchParams.get("replace") ?? "";
    let composerOptions = optionsResult.data;
    if (requestedChannelId && !composerOptions.channels.some((channel) => channel.id === requestedChannelId)) {
      const channelResult = await adapter.getChannel(requestedChannelId);
      if (
        channelResult.ok
        && channelResult.data.value.workspaceId === activeWorkspaceId
        && channelResult.data.value.status === "active"
      ) {
        composerOptions = { ...composerOptions, channels: [channelResult.data.value, ...composerOptions.channels] };
      }
    }
    let nextSelection = selectionFromComposerOptions(composerOptions, requestedChannelId);
    let nextVideoSettings = preferencesResult.ok
      ? videoSettingsFromPreferences(preferencesResult.data.value)
      : fallbackVideoSettings;
    setReplacementSourceId("");
    if (requestedReplacementId) {
      const sourceResult = await adapter.getRun(requestedReplacementId);
      if (!sourceResult.ok) {
        setOptions(null);
        setLoadError(sourceResult.error.message || "无法读取待替换素材的草案");
        setLoading(false);
        return;
      }
      const source = sourceResult.data;
      const skillAvailable = composerOptions.skills.some((item) => item.versionId === source.composition.skillVersionId);
      const pipelineAvailable = composerOptions.pipelines.some((item) => item.id === source.composition.pipelineVersionId);
      if (!skillAvailable || !pipelineAvailable) {
        setOptions(null);
        setLoadError("草案使用的 Skill 或 Pipeline 已不可用，无法保持同一制作方案重剪。");
        setLoading(false);
        return;
      }
      const availableLibraryIds = new Set(composerOptions.assetLibraries.map((item) => item.id));
      nextSelection = {
        channelId: composerOptions.channels.some((item) => item.id === source.channelId) ? source.channelId ?? "" : "",
        skillVersionId: source.composition.skillVersionId,
        pipelineVersionId: source.composition.pipelineVersionId,
        assetLibraryIds: source.composition.assetLibraryIds.filter((id) => availableLibraryIds.has(id)),
        voiceProfileId: source.composition.voiceProfileId ?? "",
        renderPresetVersionId: source.composition.renderPresetVersionId ?? "",
      };
      nextVideoSettings = {
        ...(source.videoSettings ?? nextVideoSettings),
        noAssetDraft: { enabled: false },
      };
      setTopic(source.topic);
      setShowAdvanced(true);
      setReplacementSourceId(source.id);
      setMessage("已载入原草案的主题、版本和视频规格。可选择真实素材库，或由系统自动补充公版素材后重剪。");
    }
    setOptions(composerOptions);
    setSelection(nextSelection);
    setVideoSettings(nextVideoSettings);
    setLoading(false);
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        setShowAdvanced(window.localStorage.getItem("framefactory.create.advanced") === "true");
      } catch {
        // Storage can be unavailable in private browser contexts; the default is safe.
      }
      void loadComposer();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadComposer]);

  useEffect(() => {
    if (!options || !workspaceId || !selection.skillVersionId || !selection.pipelineVersionId) {
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(() => {
      setEstimateState("checking");
      adapter.estimateRun({
        workspaceId,
        topic: topic.trim() || "未命名主题",
        researchMode,
        sourceUrls: researchSourceUrls,
        channelId: selection.channelId || undefined,
        composition: {
          skillVersionId: selection.skillVersionId,
          assetLibraryIds: selection.assetLibraryIds,
          voiceProfileId: selection.voiceProfileId || undefined,
          renderPresetVersionId: selection.renderPresetVersionId || undefined,
          pipelineVersionId: selection.pipelineVersionId,
        },
        videoSettings,
      }).then((result) => {
        if (cancelled) return;
        if (result.ok) {
          setEstimate(result.data);
          setEstimateState("ready");
        } else {
          setEstimate(null);
          setEstimateState(result.error.code === "ESTIMATE_NOT_SUPPORTED" ? "server" : "error");
        }
      });
    }, 280);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [adapter, options, researchMode, researchSourceUrls, selection, topic, videoSettings, workspaceId]);

  function chooseChannel(channelId: string) {
    const channel = options?.channels.find((item) => item.id === channelId);
    if (!channel) {
      setSelection((current) => ({ ...current, channelId }));
      return;
    }
    setSelection({
      channelId,
      skillVersionId: channel.defaultComposition.skillVersionId,
      assetLibraryIds: [...channel.defaultComposition.assetLibraryIds],
      voiceProfileId: channel.defaultComposition.voiceProfileId ?? "",
      renderPresetVersionId: channel.defaultComposition.renderPresetVersionId ?? "",
      pipelineVersionId: channel.defaultComposition.pipelineVersionId,
    });
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (topic.trim().length < 6) {
      setTopicError("请用至少 6 个字说明这次要创作的主题。");
      document.querySelector<HTMLTextAreaElement>("#creation-topic")?.focus();
      return;
    }
    if (!workspaceId || !selection.skillVersionId || !selection.pipelineVersionId) {
      setMessage("当前组合不完整，请先选择已发布的 Skill 和 Pipeline。");
      return;
    }
    if (
      replacementSourceId
      && selection.assetLibraryIds.length === 0
      && !videoSettings.assetAcquisition.enabled
    ) {
      setMessage("替换重剪需要选择真实素材库或开启自动补充；原草案不会被覆盖。");
      return;
    }

    setTopicError("");
    setSubmitting(true);
    setCreatedRunId("");
    setMessage("正在创建项目并保存版本快照…");
    const result = await adapter.createRun({
      workspaceId,
      topic: topic.trim(),
      researchMode,
      sourceUrls: researchSourceUrls,
      channelId: selection.channelId || undefined,
      composition: {
        skillVersionId: selection.skillVersionId,
        assetLibraryIds: selection.assetLibraryIds,
        voiceProfileId: selection.voiceProfileId || undefined,
        renderPresetVersionId: selection.renderPresetVersionId || undefined,
        pipelineVersionId: selection.pipelineVersionId,
      },
      videoSettings,
    }, `web-create-${crypto.randomUUID()}`);
    setSubmitting(false);
    if (result.ok) {
      setCreatedRunId(result.data.id);
      setMessage("项目已创建，并进入执行队列。");
      router.push(`/projects/${encodeURIComponent(result.data.id)}`);
    } else {
      setMessage(result.error.message || "项目创建失败，请稍后重试。");
    }
  }

  function updateAdvancedPreference(open: boolean) {
    setShowAdvanced(open);
    try {
      window.localStorage.setItem("framefactory.create.advanced", String(open));
    } catch {
      // The control remains usable even when storage is unavailable.
    }
  }

  const hasOptions = Boolean(options && options.skills.length > 0 && options.pipelines.length > 0);
  const selectedSkill = options?.skills.find((item) => item.versionId === selection.skillVersionId);
  const selectedChannel = options?.channels.find((item) => item.id === selection.channelId);
  const providerVerifiedPublicDomain = (
    videoSettings.assetAcquisition.sources.length === 1
    && videoSettings.assetAcquisition.sources[0] === "wikimedia"
    && videoSettings.assetAcquisition.copyrightStatus === "public_domain"
  );
  const acquisitionIncomplete = videoSettings.assetAcquisition.enabled && (
    (!videoSettings.assetAcquisition.rightsConfirmed && !providerVerifiedPublicDomain)
    || videoSettings.assetAcquisition.sources.length === 0
  );
  const capabilitiesUnknown = estimateState === "ready" && estimate?.capabilitiesKnown === false;
  const preflightBlocked = estimateState === "checking"
    || estimateState === "error"
    || capabilitiesUnknown
    || Boolean(estimate?.capabilityGaps.length)
    || acquisitionIncomplete;

  return (
    <div className="page page--wide create-page">
      <section className="create-cinematic" aria-labelledby="create-title">
        <div className="create-intro">
          <p className="eyebrow">01 / CREATE · DIRECTOR&apos;S DESK</p>
          <h1 id="create-title" tabIndex={-1} aria-label="把一个想法，变成可发布的内容"><span>把一个想法，</span><span>变成可发布的内容</span></h1>
          <p>从输入、生成与人工审核，到最终成片和证据报告。每次运行都保存独立版本快照，过程可追踪、可恢复。</p>
          <div className="create-intro-actions">
            <Link className="button" href="/create/application-demo">开始推荐演示</Link>
            <a className="button-secondary" href="#standard-composer">进入标准创作</a>
            <Link className="button-ghost" href="/projects">查看制作队列</Link>
          </div>
        </div>

      </section>

      <section className="create-paths" aria-labelledby="create-paths-title">
        <div className="create-paths-heading">
          <p className="eyebrow">WORKFLOW / 创作路径</p>
          <h2 id="create-paths-title">选择最接近目标的起点</h2>
          <p>首次体验建议从申请演示开始；每条路径都会明确展示输入、人工门禁、成本和最终产物。</p>
        </div>
        <div className="create-path-grid">
          <Link className="create-path-card create-path-card--recommended" href="/create/application-demo">
            <span>推荐</span><h3>申请评审演示</h3><p>用公开网页完成截图、镜头板审核、成片和证据报告闭环。</p><strong>约 3–5 分钟完成配置 →</strong>
          </Link>
          <Link className="create-path-card" href="/create/ai">
            <span>全链路生成</span><h3>全 AI 影片</h3><p>无现有素材时，从脚本、画面到声音全流程生成。</p><strong>进入生成工作台 →</strong>
          </Link>
          <Link className="create-path-card" href="/create/webpage-video">
            <span>网页采集</span><h3>网页截图成片</h3><p>将网站内容整理为可审核、可追溯的短视频。</p><strong>创建网页视频 →</strong>
          </Link>
          <Link className="create-path-card" href="/create/document-video">
            <span>本地文件</span><h3>文件讲解视频</h3><p>以 PDF 页面为证据层，叠加旁白、字幕与非事实性动态底图。</p><strong>打开本地试运行入口 →</strong>
          </Link>
          <Link className="create-path-card" href="/create/broadcast-revival">
            <span>行业方案</span><h3>广电记忆活化</h3><p>面向历史影像整理、审核和再创作的行业流程。</p><strong>打开行业工作流 →</strong>
          </Link>
        </div>
      </section>

      {loading ? (
        <section className="composer-loading" aria-label="正在读取创作资源" aria-live="polite">
          <div className="loading-line"><span /></div>
          <div className="loading-grid"><div className="loading-card" /><div className="loading-card" /></div>
          <p>正在读取你的 Skill 与制作流程…</p>
        </section>
      ) : null}

      {!loading && loadError ? (
        <StatePanel code="OFFLINE" title="创作服务暂时不可用" description="无法连接到控制服务。你的输入尚未提交，也不会丢失。" error>
          <button className="button" type="button" onClick={() => void loadComposer()}>重新连接</button>
          <Link className="button-ghost" href="/settings">查看连接状态</Link>
        </StatePanel>
      ) : null}

      {!loading && options?.channelProblem ? (
        <p className="alert" role="status">频道服务返回：{options.channelProblem.message}。其他创作资源仍来自真实服务。</p>
      ) : null}

      {!loading && options && !hasOptions ? (
        <StatePanel code="SETUP" title="完成一次配置，就可以正式开拍" description="当前空间还没有同时具备已发布 Skill 与 Pipeline 的运行组合。创建并发布 Skill 后，它会出现在这里。">
          <Link className="button" href="/skills/new">创建第一个 Skill</Link>
          <Link className="button-secondary" href="/skills">查看 Skill 库</Link>
        </StatePanel>
      ) : null}

      {!loading && options && hasOptions ? (
        <form id="standard-composer" className="composer-workbench" onSubmit={submit}>
          <div className="composer-main">
            {replacementSourceId ? <div className="alert" role="status"><strong>正在从草案派生替换重剪</strong><p>原 Run {replacementSourceId.slice(0, 8)} 保持不可变；新 Run 复用其主题、版本和规格，并强制关闭占位画面降级。</p></div> : null}
            <section className="panel composer-hero">
              <div className="composer-section-label">
                <span>01</span>
                <p>创作主题</p>
                <small>必填</small>
              </div>
              <div className="field composer-topic-field">
                <label htmlFor="creation-topic">这次想讲什么？</label>
                <textarea
                  id="creation-topic"
                  className="textarea theme-input"
                  value={topic}
                  onChange={(event) => {
                    setTopic(event.target.value);
                    if (topicError) setTopicError("");
                  }}
                  placeholder="写下主题、观点或一个值得被看见的问题…"
                  aria-invalid={Boolean(topicError)}
                  aria-describedby={topicError ? "creation-topic-error" : "creation-topic-help"}
                  onKeyDown={(event) => {
                    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
                <div className="topic-footer">
                  <span id="creation-topic-help">描述越具体，研究与写作方向越准确 · Ctrl / ⌘ + Enter 创建</span>
                  <span>{topic.trim().length} 字</span>
                </div>
                {topicError ? <span id="creation-topic-error" className="field-error" role="alert">{topicError}</span> : null}
              </div>
              <div className="prompt-starters" aria-label="选题灵感">
                <span>快速开始</span>
                {promptStarters.map((starter) => (
                  <button key={starter} type="button" onClick={() => setTopic(starter)}>{starter}</button>
                ))}
              </div>
            </section>

            <section className="panel composer-config">
              <div className="composer-section-label">
                <span>02</span>
                <p>制作组合</p>
                <small>已锁定版本</small>
              </div>
              <div className="composition-overview">
                <div className="field">
                  <span className="field-label">发布频道</span>
                  <UiSelect ariaLabel="发布频道" value={selection.channelId} onChange={chooseChannel}>
                    <option value="">不使用频道默认</option>
                    {options.channels.map((channel) => <option key={channel.id} value={channel.id}>{channel.name}</option>)}
                  </UiSelect>
                  <span className="field-help">频道保存品牌与平台默认值，不限制你的 Skill。</span>
                </div>
                <div className="composition-summary" aria-label="当前组合摘要">
                  <span><i aria-hidden="true">S</i><small>Skill</small><strong>{selectedSkill?.skillName ?? "未选择"}</strong></span>
                  <span><i aria-hidden="true">V</i><small>版本</small><strong>{selectedSkill?.version ?? "—"}</strong></span>
                  <span><i aria-hidden="true">C</i><small>频道</small><strong>{selectedChannel?.name ?? "自定义"}</strong></span>
                </div>
              </div>

              <details open={showAdvanced} onToggle={(event) => updateAdvancedPreference(event.currentTarget.open)}>
                <summary>调整本次制作参数 <span>{showAdvanced ? "收起" : "展开"}</span></summary>
                <div className="video-settings-grid" aria-label="本次视频设置">
                  <div className="video-settings-head field--wide">
                    <div>
                      <span className="eyebrow">VIDEO OUTPUT</span>
                      <strong>画面与字幕</strong>
                      <small>只覆盖本次项目，不修改账户默认值或频道配置。</small>
                    </div>
                    <span>RUN OVERRIDE</span>
                  </div>
                  <div className="field video-setting-field">
                    <span className="field-label">内容研究</span>
                    <UiSelect ariaLabel="内容研究模式" value={researchMode} onChange={(value) => setResearchMode(value as typeof researchMode)}>
                      <option value="when_missing">缺少来源时联网</option>
                      <option value="required">始终联网查证</option>
                      <option value="off">不联网，使用所填来源</option>
                    </UiSelect>
                    <span className="field-help">不联网模式只接受 HTTPS 来源，数量必须满足所选 Skill。</span>
                  </div>
                  {researchMode === "off" ? (
                    <label className="field field--wide">
                      <span className="field-label">事实来源 URL</span>
                      <textarea
                        className="textarea theme-input"
                        value={researchSources}
                        onChange={(event) => setResearchSources(event.target.value)}
                        placeholder={"每行一个 HTTPS URL\nhttps://example.com/source"}
                        aria-describedby="research-sources-help"
                      />
                      <span className="field-help" id="research-sources-help">已填写 {researchSourceUrls.length} 条；服务端会去重、校验 HTTPS 并执行 Skill 的 minimum_sources 门禁。</span>
                    </label>
                  ) : null}
                  <div className="field video-setting-field">
                    <span className="field-label">画幅</span>
                    <UiSelect ariaLabel="本次视频画幅" value={videoSettings.aspectRatio} onChange={(aspectRatio) => setVideoSettings((current) => ({ ...current, aspectRatio: aspectRatio as VideoSettings["aspectRatio"] }))}>
                      <option value="9:16">9:16 竖屏</option>
                      <option value="16:9">16:9 横屏</option>
                      <option value="1:1">1:1 方形</option>
                      <option value="4:3">4:3 经典</option>
                    </UiSelect>
                    <span className="field-help">决定输出分辨率、裁切方向与字幕安全区</span>
                  </div>
                  <label className="field video-setting-field">
                    <span className="field-label">目标时长</span>
                    <input className="input" type="number" min={15} max={3600} step={15} value={videoSettings.targetDurationSeconds} onChange={(event) => setVideoSettings((current) => ({ ...current, targetDurationSeconds: Math.min(3600, Math.max(15, Number(event.target.value) || 15)) }))} />
                    <span className="field-help">秒；用于写稿和创建前预检</span>
                  </label>
                  <div className="field video-setting-field">
                    <span className="field-label">版式</span>
                    <UiSelect ariaLabel="本次视频版式" value={videoSettings.layout} onChange={(layout) => setVideoSettings((current) => ({ ...current, layout: layout as VideoSettings["layout"] }))}>
                      <option value="full_frame">沉浸全画面</option>
                      <option value="editorial">编辑分区</option>
                    </UiSelect>
                    <span className="field-help">编辑分区会保留标题、素材和字幕的独立区域</span>
                  </div>
                  <div className="field video-setting-field">
                    <span className="field-label">素材适配</span>
                    <UiSelect ariaLabel="素材适配方式" value={videoSettings.mediaFit} onChange={(mediaFit) => setVideoSettings((current) => ({ ...current, mediaFit: mediaFit as VideoSettings["mediaFit"] }))}>
                      <option value="cover">铺满并裁切</option>
                      <option value="contain">完整显示并留边</option>
                    </UiSelect>
                    <span className="field-help">{videoSettings.mediaFit === "cover" ? "优先铺满画布，边缘可能被裁切" : "保留完整画面，空白区域使用背景填充"}</span>
                  </div>
                  <div className="field video-setting-field">
                    <span className="field-label">字幕位置</span>
                    <UiSelect ariaLabel="字幕位置" value={videoSettings.subtitles.position} onChange={(position) => setVideoSettings((current) => ({ ...current, subtitles: { ...current.subtitles, position: position as VideoSettings["subtitles"]["position"] } }))}>
                      <option value="bottom">底部安全区</option>
                      <option value="lower_third">下三分之一</option>
                    </UiSelect>
                    <span className="field-help">位置会按画幅换算并避开底部平台控件</span>
                  </div>
                  <div className="field video-setting-field">
                    <span className="field-label">字幕字号</span>
                    <UiSelect ariaLabel="字幕字号" value={videoSettings.subtitles.size} onChange={(size) => setVideoSettings((current) => ({ ...current, subtitles: { ...current.subtitles, size: size as VideoSettings["subtitles"]["size"] } }))}>
                      <option value="small">小</option>
                      <option value="medium">中</option>
                      <option value="large">大</option>
                    </UiSelect>
                    <span className="field-help">渲染时按输出高度映射为实际字号</span>
                  </div>
                  <label className="checkbox-field field--wide" aria-label="允许无素材编辑草案">
                    <input
                      type="checkbox"
                      checked={videoSettings.noAssetDraft.enabled}
                      disabled={Boolean(replacementSourceId)}
                      onChange={(event) => setVideoSettings((current) => ({
                        ...current,
                        noAssetDraft: { enabled: event.target.checked },
                      }))}
                    />
                    <span>
                      <strong>{replacementSourceId ? "替换重剪必须使用真实素材" : "无素材时先生成可剪草案"}</strong>
                      <small>{replacementSourceId ? "该选项已关闭，素材仍不足时会明确停止，避免再次生成占位草案。" : "没有可用画面时，用程序化编辑卡片、旁白和字幕完成 MP4 与视频计划；该产物必须复核并替换画面后才能发布。"}</small>
                    </span>
                  </label>
                  <div className="field field--wide acquisition-control">
                    <div className="acquisition-card">
                      <span className="acquisition-mark" aria-hidden="true">↗</span>
                      <div className="acquisition-copy">
                        <div><strong id="asset-acquisition-label">无素材或素材不足时自动补充</strong><Badge tone={videoSettings.assetAcquisition.enabled ? "success" : "warning"}>{videoSettings.assetAcquisition.enabled ? "默认开启" : "已关闭"}</Badge></div>
                        <p>没有素材库时会自动创建私有补材库；系统按脚本缺口检索、下载、分析入库，再重新执行语义匹配。</p>
                      </div>
                      <label className="acquisition-toggle" htmlFor="asset-acquisition-enabled">
                      <input
                        id="asset-acquisition-enabled"
                        aria-labelledby="asset-acquisition-label"
                        type="checkbox"
                        checked={videoSettings.assetAcquisition.enabled}
                        onChange={(event) => setVideoSettings((current) => ({
                          ...current,
                          assetAcquisition: {
                            ...current.assetAcquisition,
                            enabled: event.target.checked,
                            rightsConfirmed: event.target.checked
                              ? current.assetAcquisition.rightsConfirmed
                              : false,
                          },
                        }))}
                      />
                        <span aria-hidden="true" />
                        <strong>{videoSettings.assetAcquisition.enabled ? "自动补充已开启" : "开启自动补充"}</strong>
                      </label>
                    </div>
                    <ol className="acquisition-flow" aria-label="自动补充执行顺序">
                      <li><span>01</span>库内语义匹配</li>
                      <li><span>02</span>仅检索缺口</li>
                      <li><span>03</span>分析后重新匹配</li>
                    </ol>
                    <span className="field-help">{selection.assetLibraryIds.length ? "补充素材会进入已选的第一个素材库；未配置下载供应商时任务会返回明确错误。" : "无需预先建立素材库；创建任务时会自动建立并复用工作区私有补材库。"}</span>
                  </div>
                  {videoSettings.assetAcquisition.enabled ? <>
                    <div className="field field--wide acquisition-options">
                      <div className="acquisition-options-head">
                        <div><strong>来源与下载预算</strong><small>来源越多召回率越高，但每条素材仍需通过授权与内容分析。</small></div>
                        <Badge tone={videoSettings.assetAcquisition.sources.length ? "accent" : "warning"}>{videoSettings.assetAcquisition.sources.length} / {acquisitionSources.length} 个来源</Badge>
                      </div>
                      <div className="acquisition-options-layout">
                        <fieldset className="acquisition-sources">
                          <legend className="sr-only">允许来源</legend>
                          {acquisitionSources.map((source) => <label className="acquisition-source" htmlFor={`asset-source-${source.value}`} key={source.value}>
                            <input
                              id={`asset-source-${source.value}`}
                              type="checkbox"
                              checked={videoSettings.assetAcquisition.sources.includes(source.value)}
                              onChange={(event) => setVideoSettings((current) => {
                                const sources = event.target.checked
                                  ? Array.from(new Set([...current.assetAcquisition.sources, source.value]))
                                  : current.assetAcquisition.sources.filter((item) => item !== source.value);
                                return { ...current, assetAcquisition: { ...current.assetAcquisition, sources } };
                              })}
                            />
                            <span className="acquisition-source-mark" aria-hidden="true">{source.mark}</span>
                            <span><strong>{source.name}</strong><small>{source.note}</small></span>
                          </label>)}
                        </fieldset>
                        <label className="acquisition-budget">
                          <span><strong>最多补充</strong><small>这是单次任务的下载上限，不代表脚本场景数量。</small></span>
                          <span className="acquisition-budget-input">
                            <input aria-label="每个任务最多补充素材数" type="number" min={1} max={6} value={videoSettings.assetAcquisition.maxAssets} onChange={(event) => setVideoSettings((current) => ({ ...current, assetAcquisition: { ...current.assetAcquisition, maxAssets: Math.min(6, Math.max(1, Number(event.target.value) || 1)) } }))} />
                            <span>个 / 任务</span>
                          </span>
                          <small>允许范围 1–6</small>
                        </label>
                      </div>
                    </div>
                    {providerVerifiedPublicDomain ? <div className="field field--wide acquisition-rights">
                      <div className="check-row">
                        <span aria-hidden="true">✓</span>
                        <p><strong>Wikimedia 公版证据自动核验</strong><small>只有 Commons API 可证明为 Public Domain 或 CC0，且下载文件与证据页面一致的素材才会入库。</small></p>
                      </div>
                    </div> : <div className="field field--wide acquisition-rights">
                      <div className="check-row">
                        <input
                          id="asset-rights-confirmed"
                          aria-labelledby="asset-rights-label"
                          type="checkbox"
                          checked={videoSettings.assetAcquisition.rightsConfirmed}
                          onChange={(event) => setVideoSettings((current) => ({ ...current, assetAcquisition: { ...current.assetAcquisition, rightsConfirmed: event.target.checked } }))}
                        />
                        <label id="asset-rights-label" htmlFor="asset-rights-confirmed"><strong>我确认有权使用所选来源素材</strong><small>可访问不等于获授权；发布前仍需核验来源许可、人物权利及水印。</small></label>
                      </div>
                    </div>}
                  </> : null}
                </div>
                <div className="composition-list">
                  <div className="composition-settings-head">
                    <div>
                      <span className="eyebrow">PRODUCTION STACK</span>
                      <strong>版本与制作资源</strong>
                      <small>锁定本次运行使用的方法、流程与内容资源，不修改频道默认值。</small>
                    </div>
                    <span>RUN COMPOSITION</span>
                  </div>
                  <CompositionSelect label="Skill" note="创作方法与输出规范" value={selection.skillVersionId} onChange={(value) => setSelection((current) => ({ ...current, skillVersionId: value }))}>
                    {options.skills.map((item) => <option key={item.versionId} value={item.versionId}>{item.skillName} · {item.version}</option>)}
                  </CompositionSelect>
                  <CompositionSelect label="Pipeline" note="可恢复的执行步骤" value={selection.pipelineVersionId} onChange={(value) => setSelection((current) => ({ ...current, pipelineVersionId: value }))}>
                    {options.pipelines.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.version}</option>)}
                  </CompositionSelect>
                  <CompositionSelect label="声音" note="旁白声音配置" value={selection.voiceProfileId} onChange={(value) => setSelection((current) => ({ ...current, voiceProfileId: value }))} optional>
                    {options.voiceProfiles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                  </CompositionSelect>
                  <CompositionSelect label="渲染预设" note="画布、字幕与品牌视觉" value={selection.renderPresetVersionId} onChange={(value) => setSelection((current) => ({ ...current, renderPresetVersionId: value }))} optional>
                    {options.renderPresets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.version}</option>)}
                  </CompositionSelect>
                  <CompositionMultiSelect
                    label="素材库"
                    note="可同时覆盖多个已授权内容资源库"
                    values={selection.assetLibraryIds}
                    options={options.assetLibraries.map((item) => ({ id: item.id, label: item.name }))}
                    onToggle={(libraryId) => setSelection((current) => ({
                      ...current,
                      assetLibraryIds: toggleAssetLibrarySelection(current.assetLibraryIds, libraryId),
                    }))}
                  />
                </div>
              </details>
            </section>
          </div>

          <aside className="panel estimate-card" aria-label="运行前检查">
            <div className="estimate-heading">
              <p className="eyebrow">PRE-FLIGHT</p>
              <Badge tone={preflightBlocked ? "warning" : "success"}>
                {estimateState === "checking" ? "检查中" : preflightBlocked ? "需要处理" : "可以创建"}
              </Badge>
            </div>
            <h2>开拍前确认</h2>
            <dl>
              <div><dt>预计成本</dt><dd>{estimate ? `${estimate.cost.currency === "CNY" ? "¥" : "$"} ${estimate.cost.amount.toFixed(2)}` : estimateState === "server" ? "创建时核算" : "—"}</dd></div>
              <div><dt>预计耗时</dt><dd>{estimate ? `约 ${Math.max(1, Math.ceil(estimate.durationSeconds / 60))} 分钟` : estimateState === "server" ? "创建时评估" : "—"}</dd></div>
              <div><dt>视频规格</dt><dd>{videoSettings.aspectRatio} · {videoSettings.targetDurationSeconds} 秒</dd></div>
              <div><dt>画面版式</dt><dd>{videoSettings.layout === "editorial" ? "编辑分区" : "沉浸全画面"}</dd></div>
              <div><dt>无素材策略</dt><dd>{videoSettings.assetAcquisition.enabled ? "自动检索、入库并重匹配" : videoSettings.noAssetDraft.enabled ? "生成可替换草案" : "素材不足即停止"}</dd></div>
              <div><dt>执行方式</dt><dd>版本快照</dd></div>
            </dl>
            {capabilitiesUnknown ? (
              <div className="alert alert--error" role="alert">
                <strong>无法确认执行器能力</strong>
                <p>控制服务尚未收到 Worker 的能力清单。请先完成 Provider 配置并重启本地服务。</p>
              </div>
            ) : estimate?.capabilityGaps.length ? (
              <div className="alert alert--error" role="alert">
                <strong>组合存在能力缺口</strong>
                {estimate.capabilityGaps.map((gap) => <p key={gap.capability}>{gap.message}</p>)}
              </div>
            ) : acquisitionIncomplete ? (
              <div className="alert alert--error" role="alert">
                <strong>自动补素材设置尚未完成</strong>
                <p>{videoSettings.assetAcquisition.sources.length === 0 ? "请至少选择一个素材来源。" : "请确认自动获取素材的许可策略。"}</p>
              </div>
            ) : (
              <ul className="capability-list">
                <li>Skill 与 Pipeline 已选择</li>
                <li>运行版本将被完整记录</li>
                <li>{estimateState === "server" ? "服务端将在创建时完成预检" : "能力检查通过"}</li>
              </ul>
            )}
            <button className="button composer-submit" type="submit" disabled={submitting || preflightBlocked}>
              <span>{submitting ? "正在创建项目" : replacementSourceId ? "创建替换重剪 Run" : "创建项目并开始"}</span>
              <i aria-hidden="true">↗</i>
            </button>
            <div className={`composer-status${createdRunId ? " composer-status--success" : ""}`} role="status" aria-live="polite">
              <span aria-hidden="true" />
              <p>{message || "创建后可在项目页查看运行进度、重试与审核节点。"}</p>
            </div>
            {createdRunId ? <Link className="run-link" href="/projects">查看项目 <span aria-hidden="true">→</span></Link> : null}
          </aside>
        </form>
      ) : null}
    </div>
  );
}

function CompositionSelect({
  label,
  note,
  value,
  onChange,
  children,
  optional = false,
}: {
  label: string;
  note: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
  optional?: boolean;
}) {
  return (
    <div className="composition-row">
      <span className="field-label">{label}</span>
      <UiSelect className="composition-control" ariaLabel={`覆盖${label}`} value={value} onChange={onChange}>
        {optional ? <option value="">不使用</option> : null}
        {children}
      </UiSelect>
      <span className="field-help">{note}</span>
    </div>
  );
}

function CompositionMultiSelect({
  label,
  note,
  values,
  options,
  onToggle,
}: {
  label: string;
  note: string;
  values: readonly string[];
  options: readonly { id: string; label: string }[];
  onToggle: (id: string) => void;
}) {
  const popoverId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selectedOptions = options.filter((option) => values.includes(option.id));
  const filteredOptions = options.filter((option) => option.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (!open) return;
    function closeOnOutsidePointer(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div className="composition-row composition-row--multi">
      <span className="field-label">{label}</span>
      <div ref={rootRef} className="composition-library-picker" data-open={open || undefined}>
        <button
          className="composition-library-trigger"
          type="button"
          aria-expanded={open}
          aria-controls={popoverId}
          onClick={() => setOpen((current) => !current)}
        >
          <span>
            <strong>{values.length ? `已选 ${values.length} 个素材库` : "选择素材库"}</strong>
            <small>{selectedOptions.length ? selectedOptions.map((option) => option.label).join("、") : "可多选，按脚本场景进行语义匹配"}</small>
          </span>
          <i aria-hidden="true" />
        </button>
        {open ? (
          <div id={popoverId} className="composition-library-popover">
            <div className="composition-library-search">
              <span aria-hidden="true">⌕</span>
              <input aria-label="搜索素材库" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索素材库" />
              <small>{values.length} / {options.length}</small>
            </div>
            <fieldset className="composition-library-options">
              <legend className="sr-only">覆盖素材库</legend>
              {filteredOptions.length ? filteredOptions.map((option) => (
                <label key={option.id}>
                  <input
                    type="checkbox"
                    checked={values.includes(option.id)}
                    onChange={() => onToggle(option.id)}
                  />
                  <span><strong>{option.label}</strong><small>{values.includes(option.id) ? "已加入本次制作" : "点击加入组合"}</small></span>
                  <i aria-hidden="true">{values.includes(option.id) ? "✓" : "+"}</i>
                </label>
              )) : <span className="composition-library-empty">{options.length ? "没有匹配的素材库" : "暂无素材库"}</span>}
            </fieldset>
            <footer><span>支持同时调用多个内容资源库</span><button type="button" onClick={() => setOpen(false)}>完成</button></footer>
          </div>
        ) : null}
      </div>
      <span className="field-help">{note}</span>
    </div>
  );
}
