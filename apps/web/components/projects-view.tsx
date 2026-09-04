"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type Run, type RunStatus } from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { useSmartPolling } from "@/lib/use-smart-polling";

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
const activeStatuses = new Set<RunStatus>(["queued", "running", "awaiting_review", "retrying"]);

function projectKindLabel(project: Run) {
  if (project.projectKind === "document_video") return "文件讲解视频";
  if (project.projectKind === "webpage_video") return "网页截图成片";
  if (project.projectKind === "full_ai") return "全 AI 影片";
  return "标准视频制作";
}

export function ProjectsView() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [filter, setFilter] = useState<Filter>("all");
  const [projects, setProjects] = useState<Run[] | null>(null);
  const [error, setError] = useState("");
  const visible = (projects ?? []).filter((project) => filter === "all" || project.status === filter);
  const hasActiveProjects = projects === null || projects.some((project) => activeStatuses.has(project.status));

  const refresh = useCallback(async () => {
    const result = await adapter.listRuns();
    if (result.ok) { setProjects(result.data); setError(""); }
    else { setError(result.error.message); }
  }, [adapter]);

  useSmartPolling(refresh, { enabled: hasActiveProjects, intervalMs: 5_000, runImmediately: projects === null });

  function retry() {
    setError("");
    setProjects(null);
    void refresh();
  }

  return (
    <div className="page projects-page">
      <PageHeading
        eyebrow="02 / PROJECTS"
        title="项目快照"
        description="查看每次创作实际使用的 Skill、素材、声音与 Pipeline。每个运行都可追踪、可恢复。"
        actions={<Link className="button" href="/create">开始创作</Link>}
      />

      {projects !== null ? <div className="skill-toolbar projects-toolbar">
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
      </div> : null}

      {projects === null && !error ? (
        <section className="projects-loading" aria-label="正在加载项目" aria-live="polite">
          <header><span aria-hidden="true" /><div><strong>正在读取项目</strong><small>同步运行状态与组合快照…</small></div></header>
          <div className="project-loading-row" aria-hidden="true" />
          <div className="project-loading-row" aria-hidden="true" />
          <div className="project-loading-row" aria-hidden="true" />
        </section>
      ) : null}
      {error && projects === null ? (
        <StatePanel code="!" title="暂时无法读取项目" description="控制服务没有响应。你的项目数据不会受到影响，可以在服务恢复后重新加载。" error>
          <button className="button-secondary" type="button" onClick={retry}>重新加载</button>
        </StatePanel>
      ) : null}
      {error && projects !== null ? <p className="alert alert--error" role="status">刷新失败，正在保留上次读取的真实状态：{error}</p> : null}
      {projects !== null && visible.length ? (
        <section className="project-list" aria-label="项目列表">
          {visible.map((project) => (
            <article className="project-row" key={project.id}>
              <div><h2>{project.topic}</h2><p>{projectKindLabel(project)}{project.channelId ? " · 已绑定频道" : " · 独立项目"}</p></div>
              <Badge tone={project.status === "succeeded" ? "success" : ["failed", "cancelled"].includes(project.status) ? "warning" : "accent"}>{labels[project.status]}</Badge>
              <p>{project.steps.length} 个步骤 · {project.artifacts.length} 个产物</p>
              <time dateTime={project.updatedAt}>{new Date(project.updatedAt).toLocaleString("zh-CN")}</time>
              <Link
                className="button-ghost button-small"
                href={project.projectKind === "webpage_video" && project.controlRunId
                  ? `/webpage-video/${project.controlRunId}`
                  : `/projects/${project.id}`}
              >查看状态</Link>
            </article>
          ))}
        </section>
      ) : null}
      {!error && projects !== null && visible.length === 0 ? (
        <StatePanel
          code="00"
          title={filter === "all" ? "还没有项目" : "这个筛选下还没有项目"}
          description={filter === "all" ? "从一个主题开始创作，首个项目会出现在这里。" : "换一个状态，或开始新的创作任务。"}
        >
          {filter !== "all" ? <button className="button-secondary" type="button" onClick={() => setFilter("all")}>清除筛选</button> : null}
          <Link className="button" href="/create">开始创作</Link>
        </StatePanel>
      ) : null}
    </div>
  );
}
