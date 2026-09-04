"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type Asset,
  type ComposerOptions,
  type RunDraft,
  type RunEstimate,
  type VideoSettings,
} from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

type OutputProfile = "television" | "short_video" | "senior";
type StudioState = "loading" | "ready" | "searching" | "checking" | "submitting" | "error";

const REVIVAL_SKILL_NAME = "贵州广电历史媒资活化";
const REVIVAL_LIBRARY_NAME = "贵州广电历史媒资活化库";
const SEARCH_LIMIT = 12;

const outputProfiles: Record<OutputProfile, { label: string; note: string; settings: VideoSettings }> = {
  television: {
    label: "电视纪实版",
    note: "16:9 · 180 秒 · 25fps · 原声与档案画面优先",
    settings: videoSettings("16:9", 180, 25, "medium", "full_frame"),
  },
  short_video: {
    label: "竖屏传播版",
    note: "9:16 · 60 秒 · 30fps · 移动端字幕安全区",
    settings: videoSettings("9:16", 60, 30, "medium", "full_frame"),
  },
  senior: {
    label: "适老观看版",
    note: "16:9 · 180 秒 · 大字幕 · 克制节奏与清晰主体",
    settings: videoSettings("16:9", 180, 25, "large", "editorial"),
  },
};

function videoSettings(
  aspectRatio: VideoSettings["aspectRatio"],
  targetDurationSeconds: number,
  frameRate: VideoSettings["frameRate"],
  subtitleSize: VideoSettings["subtitles"]["size"],
  layout: VideoSettings["layout"],
): VideoSettings {
  return {
    language: "zh-CN",
    aspectRatio,
    targetDurationSeconds,
    visibility: "private",
    autoQualityCheck: true,
    layout,
    mediaFit: layout === "editorial" ? "contain" : "cover",
    frameRate,
    subtitles: { enabled: true, position: "bottom", size: subtitleSize, maxLines: 2 },
    assetAcquisition: {
      enabled: false,
      sources: ["wikimedia"],
      maxAssets: 1,
      copyrightStatus: "licensed",
      rightsConfirmed: false,
    },
    noAssetDraft: { enabled: false },
  };
}

function copyrightLabel(value: Asset["copyrightStatus"]) {
  return {
    owned: "自有版权",
    licensed: "已授权",
    public_domain: "公共领域",
    restricted: "受限",
    unknown: "权利待确认",
  }[value];
}

function durationLabel(asset: Asset) {
  const milliseconds = asset.file?.durationMs;
  if (!milliseconds) return asset.kind.toUpperCase();
  const seconds = Math.round(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function BroadcastRevivalStudio() {
  const router = useRouter();
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [state, setState] = useState<StudioState>("loading");
  const [workspaceId, setWorkspaceId] = useState("");
  const [options, setOptions] = useState<ComposerOptions | null>(null);
  const [libraryId, setLibraryId] = useState("");
  const [topic, setTopic] = useState("贵州桥梁三十年：大山如何不再遥远");
  const [timeSpan, setTimeSpan] = useState("1990 年代至今");
  const [locality, setLocality] = useState("贵州山区、贵阳及典型桥梁所在地");
  const [cultureTerms, setCultureTerms] = useState("交通变迁、群众出行、建设者采访、今昔对照");
  const [profile, setProfile] = useState<OutputProfile>("television");
  const [onlineSupplement, setOnlineSupplement] = useState(true);
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [assetTotal, setAssetTotal] = useState(0);
  const [searchMode, setSearchMode] = useState<"exact" | "library">("exact");
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [problem, setProblem] = useState("");

  const revivalSkill = options?.skills.find((item) => item.skillName === REVIVAL_SKILL_NAME);
  const standardPipeline = options?.pipelines.find((item) => item.id === revivalSkill?.defaultPipelineVersionId);
  const library = options?.assetLibraries.find((item) => item.id === libraryId);

  const loadStudio = useCallback(async () => {
    setState("loading");
    setProblem("");
    const session = await adapter.getSession();
    if (!session.ok) {
      setProblem(session.error.message || "无法读取当前工作空间。");
      setState("error");
      return;
    }
    const composer = await adapter.getComposerOptions(session.data.activeWorkspaceId);
    if (!composer.ok) {
      setProblem(composer.error.message || "无法读取素材库和生产流程。");
      setState("error");
      return;
    }
    setWorkspaceId(session.data.activeWorkspaceId);
    setOptions(composer.data);
    setLibraryId((current) => current
      || composer.data.assetLibraries.find((item) => item.name === REVIVAL_LIBRARY_NAME)?.id
      || composer.data.assetLibraries.find((item) => (item.readyAssetCount ?? 0) > 0)?.id
      || composer.data.assetLibraries[0]?.id
      || "");
    setState("ready");
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadStudio(), 0);
    return () => window.clearTimeout(timer);
  }, [loadStudio]);

  function searchText() {
    return [topic, timeSpan, locality, cultureTerms]
      .map((value) => value.trim())
      .filter(Boolean)
      .join(" ")
      .slice(0, 1000);
  }

  function runTopic() {
    return [
      `活化主题：${topic.trim()}`,
      `时间范围：${timeSpan.trim() || "不限"}`,
      `地点范围：${locality.trim() || "贵州"}`,
      `文化与画面线索：${cultureTerms.trim() || "以素材证据为准"}`,
      `输出版本：${outputProfiles[profile].label}`,
      "只使用运行绑定素材库中已通过权利、安全与人工审核的真实媒资；所有历史事实、采访原话和镜头必须保留来源证据。覆盖不足时停止并请求编辑补充，不得用生成画面冒充历史档案。",
    ].join("\n");
  }

  function inventoryConcepts() {
    const culturalConcepts = cultureTerms.split(/[\r\n,，、;；|]+/);
    return [...new Set([topic, timeSpan, locality, ...culturalConcepts]
      .map((value) => value.trim())
      .filter(Boolean))].slice(0, 8);
  }

  function draft(): RunDraft | null {
    if (!workspaceId || !revivalSkill || !standardPipeline || !libraryId) return null;
    const sourceUrls = [...new Set((assets ?? [])
      .map((asset) => asset.source?.locator)
      .filter((locator): locator is string => Boolean(locator?.startsWith("https://"))))];
    return {
      workspaceId,
      topic: runTopic(),
      inventoryConcepts: inventoryConcepts(),
      sourceUrls,
      composition: {
        skillVersionId: revivalSkill.versionId,
        pipelineVersionId: standardPipeline.id,
        assetLibraryIds: [libraryId],
      },
      videoSettings: {
        ...outputProfiles[profile].settings,
        assetAcquisition: onlineSupplement ? {
          enabled: true,
          sources: ["wikimedia"],
          maxAssets: 6,
          copyrightStatus: "public_domain",
          rightsConfirmed: true,
        } : outputProfiles[profile].settings.assetAcquisition,
      },
    };
  }

  async function searchArchive(event: FormEvent) {
    event.preventDefault();
    if (!libraryId || topic.trim().length < 6) {
      setProblem("请选择素材库，并用至少 6 个字说明活化主题。");
      return;
    }
    setState("searching");
    setProblem("");
    setEstimate(null);
    let result = await adapter.listAssets(libraryId, {
      search: searchText(),
      status: "ready",
      reviewStatus: "approved",
      limit: SEARCH_LIMIT,
    });
    let nextSearchMode: "exact" | "library" = "exact";
    if (result.ok && result.data.data.length === 0) {
      const fallback = await adapter.listAssets(libraryId, {
        status: "ready",
        reviewStatus: "approved",
        limit: SEARCH_LIMIT,
      });
      if (fallback.ok) {
        result = fallback;
        nextSearchMode = "library";
      }
    }
    if (!result.ok) {
      setAssets(null);
      setProblem(result.error.message || "媒资检索失败。");
      setState("error");
      return;
    }
    setAssets(result.data.data);
    setAssetTotal(result.data.data.length);
    setSearchMode(nextSearchMode);
    setState("ready");
  }

  async function createRevivalRun() {
    const request = draft();
    if (!request || (!assets?.length && !onlineSupplement)) return;
    setState("checking");
    setProblem("");
    const checked = await adapter.estimateRun(request);
    if (!checked.ok) {
      setEstimate(null);
      setProblem(checked.error.message || "无法执行生产预检。");
      setState("error");
      return;
    }
    setEstimate(checked.data);
    if (checked.data.capabilitiesKnown === false || checked.data.capabilityGaps.length > 0) {
      setProblem(checked.data.capabilitiesKnown === false
        ? "Worker 能力状态未知，系统不会提交不可验证的历史媒资任务。"
        : checked.data.capabilityGaps.map((gap) => gap.message).join("；"));
      setState("ready");
      return;
    }
    setState("submitting");
    const created = await adapter.createRun(request, `broadcast-revival:${globalThis.crypto.randomUUID()}`);
    if (!created.ok) {
      setProblem(created.error.message || "任务创建失败，未提交任何替代或模拟任务。");
      setState("error");
      return;
    }
    router.push(`/projects/${encodeURIComponent(created.data.id)}`);
  }

  const setupBlocked = !revivalSkill || !standardPipeline || !libraryId;
  const readyCount = library?.readyAssetCount ?? 0;
  const createDisabled = setupBlocked || assets === null || (!assets.length && !onlineSupplement) || state === "checking" || state === "submitting";

  return (
    <div className="page page--wide broadcast-revival-page">
      <PageHeading
        eyebrow="01 / CREATE / BROADCAST MEMORY"
        title="让沉睡的贵州影像，再次被看见"
        description="先检索已授权、已审核的本地历史媒资；覆盖不足时补充并分析公共领域素材，再按最终可用库存写稿、配音、粗剪与质检。"
        actions={<Link className="button-ghost" href="/create">返回常规创作</Link>}
      />

      <section className="broadcast-revival-principles theme-inverse" aria-label="可信活化原则">
        <div><span>01</span><strong>真实档案优先</strong><small>生成画面不得冒充历史影像</small></div>
        <div><span>02</span><strong>镜头来源可追溯</strong><small>节目、时间码、权利与审核留证</small></div>
        <div><span>03</span><strong>素材不足再补采</strong><small>联网候选先导入、分析并核验权利</small></div>
      </section>

      {state === "loading" ? <div className="broadcast-revival-loading" role="status">正在读取真实素材库、官方 Skill 与 Worker 能力…</div> : null}
      {state === "error" && !options ? (
        <StatePanel code="OFFLINE" title="无法连接广电媒资控制服务" description={`${problem || "控制服务不可用"}。页面不会用本地样例或 Mock 素材替代真实服务结果。`} error>
          <button className="button-secondary" type="button" onClick={() => void loadStudio()}>重新连接</button>
          <Link className="button-ghost" href="/settings">检查服务设置</Link>
        </StatePanel>
      ) : null}
      {options && setupBlocked ? (
        <StatePanel code="SETUP" title="活化生产组合尚未就绪" description={!revivalSkill
          ? `未找到“${REVIVAL_SKILL_NAME}”官方 Skill。请重新启动 API 或执行官方 Seed 导入后重试。`
          : !standardPipeline ? "未找到具备素材检索与时间线能力的标准生产 Pipeline。" : "当前工作空间还没有可用素材库。"}>
          <button className="button-secondary" type="button" onClick={() => void loadStudio()}>重新读取配置</button>
          <Link className="button-ghost" href="/assets">建设历史媒资库</Link>
        </StatePanel>
      ) : null}

      {state !== "loading" && !setupBlocked ? (
        <div className="broadcast-revival-workbench">
          <form className="panel broadcast-revival-brief" onSubmit={searchArchive}>
            <header><div><p className="eyebrow">ARCHIVE BRIEF</p><h2>定义这次要唤醒的记忆</h2></div><Badge tone="accent">真实库检索</Badge></header>
            <label className="field" htmlFor="revival-topic"><span>活化主题</span><textarea id="revival-topic" className="textarea" value={topic} maxLength={600} onChange={(event) => setTopic(event.target.value)} /></label>
            <div className="broadcast-revival-fields">
              <label className="field" htmlFor="revival-time"><span>时间范围</span><input id="revival-time" className="input" value={timeSpan} maxLength={120} onChange={(event) => setTimeSpan(event.target.value)} /></label>
              <label className="field" htmlFor="revival-place"><span>地点范围</span><input id="revival-place" className="input" value={locality} maxLength={160} onChange={(event) => setLocality(event.target.value)} /></label>
            </div>
            <label className="field" htmlFor="revival-terms"><span>文化与画面线索</span><input id="revival-terms" className="input" value={cultureTerms} maxLength={300} onChange={(event) => setCultureTerms(event.target.value)} /></label>
            <div className="broadcast-revival-fields">
              <div className="field"><span className="field-label">历史媒资库</span><UiSelect ariaLabel="历史媒资库" value={libraryId} onChange={(value) => { setLibraryId(value); setAssets(null); setEstimate(null); setSearchMode("exact"); }}>
                {options?.assetLibraries.map((item) => <option key={item.id} value={item.id}>{item.name} · 可用 {item.readyAssetCount ?? 0}</option>)}
              </UiSelect></div>
              <div className="field"><span className="field-label">输出版本</span><UiSelect ariaLabel="输出版本" value={profile} onChange={(value) => setProfile(value as OutputProfile)}>
                {Object.entries(outputProfiles).map(([value, item]) => <option key={value} value={value}>{item.label}</option>)}
              </UiSelect></div>
            </div>
            <label className="acquisition-toggle broadcast-revival-acquisition" htmlFor="broadcast-revival-online-supplement">
              <input id="broadcast-revival-online-supplement" aria-label="本地素材不足时联网补充" type="checkbox" checked={onlineSupplement} onChange={(event) => setOnlineSupplement(event.target.checked)} />
              <span aria-hidden="true" />
              <div><strong>本地素材不足时联网补充</strong><small>仅检索 Wikimedia 公共领域素材，最多补充 6 条；导入后仍需完成分析、权利校验和镜头匹配。</small></div>
            </label>
            <div className="broadcast-revival-output-note"><strong>{outputProfiles[profile].label}</strong><span>{outputProfiles[profile].note}</span></div>
            <button className="button" type="submit" disabled={state === "searching"}>{state === "searching" ? "正在筛查媒资…" : "检索历史媒资"}</button>
          </form>

          <aside className="panel broadcast-revival-readiness" aria-label="生产就绪信息">
            <p className="eyebrow">PRODUCTION BOUNDARY</p>
            <h2>本次生产边界</h2>
            <dl>
              <div><dt>专用 Skill</dt><dd>{revivalSkill?.version ?? "—"}</dd></div>
              <div><dt>标准 Pipeline</dt><dd>{standardPipeline ? "服务端默认绑定" : "—"}</dd></div>
              <div><dt>素材库可用项</dt><dd>{readyCount}</dd></div>
              <div><dt>外部补采</dt><dd>{onlineSupplement ? "Wikimedia 公版 · 最多 6 条" : "关闭"}</dd></div>
              <div><dt>生成素材冒充档案</dt><dd>禁止</dd></div>
              <div><dt>人工终审</dt><dd>始终要求</dd></div>
            </dl>
            <p>这里先做素材级快速筛查；创建 Run 后先盘点覆盖度，再依据库存写稿并生成镜头片段级候选。若某个脚本 Beat 没有合格镜头且已开启补采，Worker 会联网检索、导入并分析公版素材，然后重新匹配，最后才进入时间线。</p>
          </aside>
        </div>
      ) : null}

      {problem && options ? <p className="alert broadcast-revival-alert" role="alert">{problem}</p> : null}

      {assets ? (
        <section className="broadcast-revival-results" aria-labelledby="revival-results-title">
          <header><div><p className="eyebrow">ARCHIVE EVIDENCE PREVIEW</p><h2 id="revival-results-title">历史媒资快速筛查</h2><p>{searchMode === "library" ? `线索检索暂无精确结果，改为展示素材库内 ${assetTotal} 项已审核候选；` : `精确线索匹配 ${assetTotal} 项；`}只显示已就绪且人工审核通过的素材。</p></div><Badge tone={assets.length && searchMode === "exact" ? "success" : "warning"}>{assets.length && searchMode === "exact" ? "本地线索命中" : "需要覆盖检查"}</Badge></header>
          {!assets.length ? <StatePanel code="00" title="本地素材库没有可用线索" description={onlineSupplement ? "可以继续创建任务；Worker 会先形成覆盖缺口，再联网补充公共领域候选，完成分析和权利校验后才写入剪辑链路。" : "请开启联网补充，或先导入并审核本地素材。系统不会用生成画面伪造档案覆盖。"}><Link className="button-secondary" href={`/assets/${encodeURIComponent(libraryId)}`}>进入素材库补充证据</Link></StatePanel> : null}
          {assets.length ? <div className="broadcast-revival-grid">{assets.map((asset, index) => (
            <article key={asset.id} className="broadcast-revival-card">
              <div className="broadcast-revival-card-index"><span>{String(index + 1).padStart(2, "0")}</span><small>{durationLabel(asset)}</small></div>
              <div><div className="broadcast-revival-card-meta"><Badge tone="success">审核通过</Badge><Badge>{copyrightLabel(asset.copyrightStatus)}</Badge></div><h3><Link href={`/assets/${encodeURIComponent(asset.libraryId)}/${encodeURIComponent(asset.id)}`}>{asset.title}</Link></h3><p>{asset.analysis?.summary || asset.description || "暂无摘要，进入素材详情查看分析证据。"}</p><div className="broadcast-revival-tags">{[...(asset.analysis?.eras ?? []), ...(asset.analysis?.locations ?? []), ...asset.tags.map((tag) => tag.name)].slice(0, 6).map((tag) => <span key={tag}>{tag}</span>)}</div></div>
            </article>
          ))}</div> : null}
          {assets.length || onlineSupplement ? <footer className="broadcast-revival-launch"><div><strong>下一步：盘点覆盖度，再写稿并剪辑</strong><p>{onlineSupplement ? "本地 Beat 覆盖不足时，系统会从 Wikimedia 补充公版素材；只有分析和权利门禁通过的素材才能进入时间线。" : "仅使用当前素材库；任何 Beat 覆盖不足都会暂停并请求补充。"}</p>{estimate ? <small>预计时长 {estimate.durationSeconds} 秒 · 能力缺口 {estimate.capabilityGaps.length}</small> : null}</div><button className="button" type="button" disabled={createDisabled} onClick={() => void createRevivalRun()}>{state === "checking" ? "正在检查能力…" : state === "submitting" ? "正在创建证据链…" : onlineSupplement ? "检查素材并开始剪辑" : "创建本地素材粗剪"}</button></footer> : null}
        </section>
      ) : null}
    </div>
  );
}
