"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { FormEvent, useEffect, useMemo, useState } from "react";
import { createFrameFactoryAdapter, type CreateSkillRequest, type Skill, type SkillSpec } from "@/lib/api";
import { Badge, PageHeading } from "@/components/page-heading";
import { UiSelect } from "@/components/ui-select";

type Method = "blank" | "fork" | "distill" | "import";

const methods: Array<{ id: Method; index: string; title: string; copy: string }> = [
  { id: "blank", index: "01", title: "从空白创建", copy: "从目标、受众、事实边界和输出契约开始，建立完全属于工作区的草稿。" },
  { id: "fork", index: "02", title: "分叉官方 Skill", copy: "复制一个可查看的已发布版本为工作区草稿，不修改原 Skill。" },
  { id: "distill", index: "03", title: "从示例蒸馏", copy: "提交文本、链接或文件示例，先形成研究报告，再生成同构草稿。" },
  { id: "import", index: "04", title: "导入 Skill 包", copy: "仅导入声明式 JSON / Markdown 规格；校验 schema，不执行任意代码。" },
];

const starterSpec: SkillSpec = {
  inputSchema: { type: "object", required: ["topic"], properties: { topic: { type: "string" } } },
  researchPolicy: { factBoundary: "balanced", requireCitations: true, preferredSources: ["一手资料"], excludedSources: [] },
  writingInstructions: "围绕主题建立清晰论点，区分事实、判断与建议。",
  visualPolicy: { direction: "克制的信息编辑风格", shotGuidance: ["优先使用可溯源画面"], forbiddenTreatments: ["误导性重演"] },
  assetPolicy: { strategy: "workspace_libraries", requiredTags: [], allowExternalAcquisition: false },
  qcRubric: { criteria: [{ id: "clarity", label: "清晰度", description: "核心观点可准确复述", minimumScore: 0.8 }] },
  outputContract: { format: "video_script", fields: ["title", "hook", "sections", "sources"], constraints: { maxDurationSeconds: 90 } },
  modelRequirements: { capabilities: ["text.structured_output"] },
};

export function SkillCreator() {
  const searchParams = useSearchParams();
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const [workspaceId, setWorkspaceId] = useState("");
  const [officialSkills, setOfficialSkills] = useState<Skill[]>([]);
  const [sourceSkillId, setSourceSkillId] = useState("");
  const [method, setMethod] = useState<Method>(() => {
    const requested = searchParams.get("method");
    return methods.some((item) => item.id === requested) ? requested as Method : "blank";
  });
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [audience, setAudience] = useState("");
  const [example, setExample] = useState("");
  const [created, setCreated] = useState<Skill | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    Promise.all([adapter.getSession(), adapter.listSkills({ scope: "official" })]).then(([sessionResult, skillsResult]) => {
      if (cancelled) return;
      if (sessionResult.ok) setWorkspaceId(sessionResult.data.activeWorkspaceId);
      if (skillsResult.ok) {
        setOfficialSkills(skillsResult.data);
        const requestedSource = searchParams.get("source");
        setSourceSkillId(skillsResult.data.some((item) => item.id === requestedSource) ? requestedSource! : skillsResult.data[0]?.id ?? "");
      }
    });
    return () => { cancelled = true; };
  }, [adapter, searchParams]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim().length < 2 || description.trim().length < 8) {
      setError("请补充 Skill 名称和至少 8 个字的目标说明。")
      return;
    }

    const identity = { name: name.trim(), description: description.trim(), visibility: "private" as const };
    if (!workspaceId) {
      setError("创作空间仍在载入，请稍后重试。");
      return;
    }
    let request: CreateSkillRequest;
    if (method === "fork") {
      const source = officialSkills.find((item) => item.id === sourceSkillId);
      if (!source?.currentVersionId) {
        setError("请选择一个可分叉的官方 Skill。");
        return;
      }
      request = { kind: "fork", workspaceId, identity, sourceSkillId: source.id, sourceVersionId: source.currentVersionId };
    } else if (method === "distill") {
      request = {
        kind: "distill",
        workspaceId,
        identity,
        examples: [{ id: "local-example", kind: "text", label: "示例内容", value: example || "一段用于本地交互演示的示例。" }],
      };
    } else if (method === "import") {
      request = {
        kind: "import",
        workspaceId,
        package: { schemaVersion: "1.0", identity, spec: starterSpec, testTopics: [] },
      };
    } else {
      request = { kind: "blank", workspaceId, identity, initialSpec: { ...starterSpec, writingInstructions: `${starterSpec.writingInstructions}\n目标受众：${audience || "待补充"}` } };
    }

    setSubmitting(true);
    setError("");
    const result = await adapter.createSkill(request);
    setSubmitting(false);
    if (result.ok) setCreated(result.data);
    else setError(result.error.message);
  }

  return (
    <div className="page">
      <PageHeading
        eyebrow="04 / SKILL / NEW"
        title="创建属于你的 Skill"
        description="选择最合适的起点，先生成私有草稿；经过编辑、测试和校验后，再决定是否发布为稳定版本。"
        actions={<Link className="button-ghost" href="/skills">返回 Skill 列表</Link>}
      />

      <div className="method-grid" role="group" aria-label="选择 Skill 创建方式">
        {methods.map((item) => (
          <button
            className="method-card"
            type="button"
            key={item.id}
            aria-pressed={method === item.id}
            onClick={() => { setMethod(item.id); setCreated(null); setError(""); }}
          >
            <span className="method-index">{item.index}</span>
            <strong>{item.title}</strong>
            <p>{item.copy}</p>
          </button>
        ))}
      </div>

      <form className="creation-form" onSubmit={submit}>
        <section className="panel">
          <div className="section-heading">
            <div><p className="eyebrow">{method.toUpperCase()} FLOW</p><h2>{methods.find((item) => item.id === method)?.title}</h2></div>
            <Badge tone="accent">我的私有草稿</Badge>
          </div>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="skill-name">Skill 名称</label>
              <input id="skill-name" className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：观点拆解工作流" autoComplete="off" />
            </div>
            <div className="field">
              <label htmlFor="skill-audience">目标受众</label>
              <input id="skill-audience" className="input" value={audience} onChange={(event) => setAudience(event.target.value)} placeholder="他们已知什么、关心什么？" />
            </div>
            <div className="field field--full">
              <label htmlFor="skill-description">目标与内容形式</label>
              <textarea id="skill-description" className="textarea" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="这个 Skill 要稳定解决什么问题？期望产出什么形式？" />
            </div>
            {method === "fork" ? (
              <div className="field field--full">
                <span className="field-label">官方起点</span>
                <UiSelect ariaLabel="官方起点" value={sourceSkillId} onChange={setSourceSkillId}>
                  {officialSkills.map((skill) => <option key={skill.id} value={skill.id}>{skill.name} · {skill.publisher.displayName}</option>)}
                </UiSelect>
                <span className="field-help">会复制为你所有的私有草稿；官方发布版本保持不变。</span>
              </div>
            ) : null}
            {method === "distill" ? (
              <div className="field field--full">
                <label htmlFor="skill-example">示例内容</label>
                <textarea id="skill-example" className="textarea" value={example} onChange={(event) => setExample(event.target.value)} placeholder="粘贴示例文本或链接，系统会先提炼结构与风格，再生成可编辑草稿。" />
              </div>
            ) : null}
            {method === "import" ? (
              <div className="field field--full">
                <label htmlFor="skill-package">Skill 包</label>
                <input id="skill-package" className="input" type="file" accept=".json,.md,application/json,text/markdown" aria-describedby="package-help" />
                <span id="package-help" className="field-help">仅解析受控字段；拒绝脚本、服务端路径和未知 schema。</span>
              </div>
            ) : null}
          </div>
          {error ? <div className="alert alert--error" role="alert" style={{ marginTop: 16 }}>{error}</div> : null}
          {created ? (
            <div className="alert alert--success" role="status" style={{ marginTop: 16 }}>
              已创建“{created.name}”及草稿版本。<Link href={`/skills/${created.id}/edit`}>进入结构化编辑器 →</Link>
            </div>
          ) : null}
          <div className="button-row" style={{ marginTop: 18 }}>
            <button className="button" type="submit" disabled={submitting || !workspaceId}>{submitting ? "正在创建…" : "创建 Skill 草稿"}</button>
            <Link className="button-ghost" href="/skills">取消</Link>
          </div>
        </section>

        <aside className="panel creation-aside" aria-label="发布前检查要求">
          <p className="eyebrow">VALIDATION GATES</p>
          <h2>发布前仍需通过</h2>
          <ol>
            <li>Schema 与输出契约检查</li>
            <li>危险指令与路径检查</li>
            <li>至少 1 个测试主题</li>
            <li>事实与版权风险提示</li>
            <li>成本与能力缺口估算</li>
          </ol>
          <p className="inspector-note" style={{ marginTop: 18 }}>已发布版本不可原地修改；继续编辑会创建新的草稿版本。</p>
        </aside>
      </form>
    </div>
  );
}
