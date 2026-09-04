"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type Skill } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";

type LibraryFilter = "mine" | "official" | "forked" | "draft" | "published" | "deprecated";

const libraryFilters: Array<{ id: LibraryFilter; label: string }> = [
  { id: "mine", label: "我的 Skill" },
  { id: "official", label: "官方 Skill" },
  { id: "forked", label: "分叉 Skill" },
  { id: "draft", label: "草稿" },
  { id: "published", label: "已发布" },
  { id: "deprecated", label: "已停用" },
] as const;

const visibilityLabels = {
  private: "私有",
  workspace: "工作区可见",
  public_readonly: "公开只读",
};

const statusLabels = {
  draft: "草稿",
  validating: "校验中",
  ready: "可发布",
  published: "已发布",
  deprecated: "已停用",
};

export function SkillLibrary() {
  const [filter, setFilter] = useState<LibraryFilter>("mine");
  const [workspaceId, setWorkspaceId] = useState("");
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);

  useEffect(() => {
    let cancelled = false;
    adapter.getSession().then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setWorkspaceId(result.data.activeWorkspaceId);
        return;
      }
      setSkills([]);
      setError(`无法确定当前工作区：${result.error.message}`);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [adapter]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    if (!workspaceId) return;
    adapter.listSkills({ scope: "all", workspaceId, search: debouncedQuery }).then((result) => {
      if (cancelled) return;
      setLoading(false);
      if (result.ok) setSkills(result.data);
      else {
        setSkills([]);
        setError(result.error.message);
      }
    });
    return () => { cancelled = true; };
  }, [adapter, debouncedQuery, reloadToken, workspaceId]);

  const visibleSkills = useMemo(() => skills.filter((skill) => {
    if (filter === "mine") return skill.publisher.type !== "system";
    if (filter === "official") return skill.publisher.type === "system";
    if (filter === "forked") return Boolean(skill.forkedFrom);
    if (filter === "draft") return ["draft", "validating", "ready"].includes(skill.status);
    return skill.status === filter;
  }), [filter, skills]);

  return (
    <div className="page page--wide">
      <PageHeading
        eyebrow="04 / SKILL STUDIO"
        title="方法可以被创建、测试与复用"
        description="在“我的 Skill”中沉淀自己的创作方法；官方 Skill 保持只读，可分叉后按你的工作方式继续演进。"
        actions={<Link className="button" href="/skills/new">创建 Skill</Link>}
      />

      <div className="skill-toolbar">
        <div className="skill-filter-list" role="group" aria-label="筛选 Skill">
          {libraryFilters.map((item) => (
            <button
              className="filter-chip"
              type="button"
              key={item.id}
              aria-pressed={filter === item.id}
              onClick={() => setFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="toolbar">
          <label className="sr-only" htmlFor="skill-search">搜索 Skill</label>
          <input id="skill-search" className="input" type="search" value={query} onChange={(event) => { setQuery(event.target.value); setLoading(true); setError(""); }} placeholder="搜索名称、发布者或说明" />
          <span className="muted" aria-live="polite">{visibleSkills.length} 个 Skill</span>
        </div>
      </div>

      <div
        id="skill-list-results"
        aria-busy={loading}
      >
        {loading ? (
          <div className="loading-grid" aria-label="正在加载 Skill">
            <div className="loading-card" /><div className="loading-card" /><div className="loading-card" />
          </div>
        ) : null}

        {!loading && error ? (
          <StatePanel code="ERR" title="Skill 列表暂时不可用" description={`${error}。筛选和搜索词会保留。`} error>
            <button className="button-secondary" type="button" onClick={() => { setLoading(true); setError(""); setReloadToken((value) => value + 1); }}>重新载入</button>
          </StatePanel>
        ) : null}

        {!loading && !error && visibleSkills.length === 0 ? (
          <StatePanel
            code="00"
            title={query ? `没有匹配“${query}”的 Skill` : filter === "mine" ? "还没有自己的 Skill" : "这个筛选目前为空"}
            description={query ? "清除搜索词，或切换到其他发布范围。" : "从四种创建入口任选一种，最终都会生成同一形态的私有草稿。"}
          >
            {query ? <button className="button-secondary" type="button" onClick={() => setQuery("")}>清除搜索</button> : <Link className="button" href="/skills/new">创建第一个 Skill</Link>}
          </StatePanel>
        ) : null}

        {!loading && !error && visibleSkills.length > 0 ? (
          <div className="skill-grid">
            {visibleSkills.map((skill) => <SkillCard key={skill.id} skill={skill} />)}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SkillCard({ skill }: { skill: Skill }) {
  const version = skill.currentVersionId ? `${skill.stats.versionCount} 个版本` : "首个草稿";
  const publisherKind = skill.publisher.type === "system" ? "官方 Skill" : "我的 Skill";
  return (
    <article className="card skill-card">
      <div className="skill-card-head">
        <div className="skill-publisher">
          <span className="publisher-mark" aria-hidden="true">{skill.publisher.type === "system" ? "FF" : "ME"}</span>
          <span>{skill.publisher.displayName} · {publisherKind}</span>
        </div>
        {skill.publisher.verified ? <Badge tone="accent">已验证</Badge> : null}
      </div>
      <h2><Link href={`/skills/${skill.id}`}>{skill.name}</Link></h2>
      <p>{skill.description}</p>
      <div className="skill-card-meta">
        <Badge tone={skill.status === "published" ? "success" : "neutral"}>{statusLabels[skill.status]}</Badge>
        <Badge>{visibilityLabels[skill.visibility]}</Badge>
        {skill.forkedFrom ? <Badge tone="accent">分叉</Badge> : null}
        <Badge>{version}</Badge>
      </div>
      <div className="skill-card-actions">
        <span>{skill.stats.runCount} 次使用</span>
        <div className="button-row">
          <Link className="button-ghost button-small" href={`/skills/${skill.id}`}>概览</Link>
          {skill.permissions.edit ? (
            <Link className="button-secondary button-small" href={`/skills/${skill.id}/edit`}>编辑</Link>
          ) : (
            <Link className="button-ghost button-small" href={`/skills/${skill.id}/edit`}>查看</Link>
          )}
          {skill.permissions.fork ? <Link className="button-ghost button-small" href={`/skills/new?method=fork&source=${encodeURIComponent(skill.id)}`}>分叉</Link> : null}
        </div>
      </div>
    </article>
  );
}
