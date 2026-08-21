"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type Run, type RunStatus } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";

type Filter = "all" | RunStatus;

const labels: Record<RunStatus, string> = {
  queued: "排队中",
  running: "执行中",
  awaiting_review: "待审核",
  retrying: "准备重试",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const filters: Filter[] = ["all", "queued", "running", "awaiting_review", "retrying", "succeeded", "failed", "cancelled"];

export function ProjectsView() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [filter, setFilter] = useState<Filter>("all");
  const [projects, setProjects] = useState<Run[] | null>(null);
  const [error, setError] = useState("");
  const visible = (projects ?? []).filter((project) => filter === "all" || project.status === filter);

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      const result = await adapter.listRuns();
      if (cancelled) return;
      if (result.ok) { setProjects(result.data); setError(""); }
      else { setError(result.error.message); }
    }
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5_000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [adapter]);

  return (
    <div className="page">
      <PageHeading
        eyebrow="02 / PROJECTS"
        title="项目保留每次组合快照"
        description="每个项目记录实际使用的 SkillVersion、素材库、声音、渲染预设和 Pipeline，服务重启后仍可继续。"
        actions={<Link className="button" href="/create">开始创作</Link>}
      />

      <div className="skill-toolbar">
        <div className="toolbar" aria-label="项目状态筛选">
          {filters.map((value) => (
            <button
              className={filter === value ? "button-secondary button-small" : "button-ghost button-small"}
              type="button"
              aria-pressed={filter === value}
              key={value}
              onClick={() => setFilter(value)}
            >
              {value === "all" ? "全部" : labels[value]}
            </button>
          ))}
        </div>
        <span className="muted">{visible.length} 个项目</span>
      </div>

      {projects === null ? <div className="loading-grid" aria-label="正在加载项目"><div className="loading-card" /><div className="loading-card" /></div> : null}
      {error && projects === null ? <StatePanel code="ERR" title="项目列表暂时不可用" description={error} error /> : null}
      {error && projects !== null ? <p className="alert alert--error" role="status">刷新失败，正在保留上次读取的真实状态：{error}</p> : null}
      {projects !== null && visible.length ? (
        <section className="project-list" aria-label="项目列表">
          {visible.map((project) => (
            <article className="project-row" key={project.id}>
              <div><h2>{project.topic}</h2><p>{project.channelId ? `频道 ${project.channelId.slice(0, 8)}` : "未绑定频道"}</p></div>
              <Badge tone={project.status === "succeeded" ? "success" : ["failed", "cancelled"].includes(project.status) ? "warning" : "accent"}>{labels[project.status]}</Badge>
              <p>Skill {project.composition.skillVersionId.slice(0, 8)} · Pipeline {project.composition.pipelineVersionId.slice(0, 8)}</p>
              <time dateTime={project.updatedAt}>{new Date(project.updatedAt).toLocaleString("zh-CN")}</time>
              <Link className="button-ghost button-small" href={`/projects/${project.id}`}>查看状态</Link>
            </article>
          ))}
        </section>
      ) : null}
      {!error && projects !== null && visible.length === 0 ? (
        <StatePanel code="00" title="这个筛选下还没有项目" description="换一个状态，或从一个主题开始新的创作任务。">
          <button className="button-secondary" type="button" onClick={() => setFilter("all")}>清除筛选</button>
          <Link className="button" href="/create">开始创作</Link>
        </StatePanel>
      ) : null}
    </div>
  );
}
