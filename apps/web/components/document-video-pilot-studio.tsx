"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useMemo, useRef, useState } from "react";
import { createFrameFactoryAdapter, type DocumentVideoCreateRequest } from "@/lib/api";
import { Badge, PageHeading } from "@/components/page-heading";

type SubmitState = "ready" | "hashing" | "uploading" | "unknown" | "error";

function submissionKey() {
  return `document-video:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
}

export function DocumentVideoPilotStudio() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [topic, setTopic] = useState("");
  const [duration, setDuration] = useState(120);
  const [aspectRatio, setAspectRatio] = useState<DocumentVideoCreateRequest["aspectRatio"]>("16:9");
  const [generatedBackground, setGeneratedBackground] = useState(true);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [state, setState] = useState<SubmitState>("ready");
  const [message, setMessage] = useState("选择 PDF 并确认使用权后即可提交真实任务。");
  const keyRef = useRef("");
  const requestRef = useRef<DocumentVideoCreateRequest | null>(null);

  const validFile = Boolean(
    file
    && file.name.toLowerCase().endsWith(".pdf")
    && (!file.type || file.type === "application/pdf")
    && file.size > 0
    && file.size <= 200 * 1024 * 1024,
  );
  const frozen = state === "hashing" || state === "uploading" || state === "unknown";
  const canSubmit = validFile && topic.trim().length > 0 && rightsConfirmed && !["hashing", "uploading"].includes(state);

  async function submitAttempt(request: DocumentVideoCreateRequest, key: string) {
    setState("hashing");
    setMessage("正在计算本地 SHA-256；文件内容尚未离开浏览器。");
    const progress = window.setTimeout(() => {
      setState("uploading");
      setMessage("正在上传并验证不可变 PDF，然后创建可恢复 Run DAG…");
    }, 250);
    const result = await adapter.createDocumentVideoRun(request, key);
    window.clearTimeout(progress);
    if (result.ok) {
      setMessage("任务已持久化并进入队列，正在打开审核详情页。");
      router.push(`/projects/${encodeURIComponent(result.data.runId)}`);
      return;
    }
    if (result.error.retryable || result.error.code.includes("DISPATCH")) {
      setState("unknown");
      setMessage(`${result.error.message} 服务器结果可能已落库；请保留当前文件并使用原幂等键安全重试。`);
      return;
    }
    setState("error");
    setMessage(result.error.message);
    keyRef.current = "";
    requestRef.current = null;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (state === "unknown" && requestRef.current && keyRef.current) {
      void submitAttempt(requestRef.current, keyRef.current);
      return;
    }
    if (!file || !canSubmit) return;
    const request: DocumentVideoCreateRequest = {
      file,
      topic: topic.trim(),
      durationSeconds: duration,
      aspectRatio,
      generatedBackgroundEnabled: generatedBackground,
    };
    requestRef.current = request;
    keyRef.current = submissionKey();
    void submitAttempt(request, keyRef.current);
  }

  return (
    <div className="page page--wide document-video-page">
      <PageHeading
        eyebrow="01 / CREATE / DOCUMENT VIDEO"
        title="把一份 PDF，变成有出处的讲解视频"
        description="浏览器安全上传，Worker 按不可变哈希绑定文件；脚本、镜头、字幕和成片都保留页码证据。"
        actions={<Link className="button-ghost" href="/create">返回创作入口</Link>}
      />

      <section className="document-video-principles" aria-label="制作原则">
        <div><span>01</span><strong>文档是证据</strong><small>旁白与画面逐场景绑定原始哈希和页码。</small></div>
        <div><span>02</span><strong>底图不讲事实</strong><small>Agnes 缺失时明确降级为程序化装饰背景。</small></div>
        <div><span>03</span><strong>审核后交付</strong><small>分镜和质量步骤都会进入真实人工门禁。</small></div>
      </section>

      <form className="document-video-workbench" onSubmit={submit} aria-busy={state === "hashing" || state === "uploading"}>
        <section className="panel document-video-command" aria-labelledby="document-video-create-title">
          <header>
            <div><p className="eyebrow">PRODUCTION RUN</p><h2 id="document-video-create-title">上传并创建任务</h2></div>
            <Badge tone={state === "error" || state === "unknown" ? "warning" : "success"}>真实 API</Badge>
          </header>
          <div className="document-video-fields">
            <label className="field">
              <span className="field-label">PDF 文件</span>
              <input
                className="input theme-input"
                type="file"
                accept="application/pdf,.pdf"
                disabled={frozen}
                required
                onChange={(event) => {
                  const selected = event.currentTarget.files?.[0] ?? null;
                  setFile(selected);
                  setState("ready");
                  setMessage(selected && selected.size > 200 * 1024 * 1024 ? "文件超过 200 MiB 限制。" : "文件将在本地计算哈希后直接上传到对象存储。");
                }}
              />
              <span className="field-help">只接受未加密、无 JavaScript、≤200 MiB、≤100 页且带可提取文本层的 PDF。</span>
            </label>
            <label className="field">
              <span className="field-label">讲解重点</span>
              <textarea className="textarea" value={topic} maxLength={1600} disabled={frozen} required onChange={(event) => setTopic(event.target.value)} placeholder="例如：用两分钟解释方案的核心价值、实施路径和关键数据" />
            </label>
            <div className="settings-grid">
              <label className="field"><span className="field-label">目标时长</span><select className="select" value={duration} disabled={frozen} onChange={(event) => setDuration(Number(event.target.value))}><option value={60}>60 秒</option><option value={120}>120 秒</option><option value={180}>180 秒</option><option value={300}>300 秒</option></select></label>
              <label className="field"><span className="field-label">画幅</span><select className="select" value={aspectRatio} disabled={frozen} onChange={(event) => setAspectRatio(event.target.value as DocumentVideoCreateRequest["aspectRatio"])}><option value="16:9">16:9 横屏</option><option value="9:16">9:16 竖屏</option><option value="1:1">1:1 方形</option></select></label>
            </div>
            <label className="check-row"><input type="checkbox" checked={generatedBackground} disabled={frozen} onChange={(event) => setGeneratedBackground(event.target.checked)} /><span>允许 Agnes 装饰背景；未配置时使用有审计记录的程序化背景</span></label>
            <label className="check-row"><input type="checkbox" checked={rightsConfirmed} disabled={frozen} required onChange={(event) => setRightsConfirmed(event.target.checked)} /><span>我确认有权上传、处理并生成这份文件的讲解视频</span></label>
          </div>
          <div className="alert" role={state === "error" || state === "unknown" ? "alert" : "status"}>{message}</div>
          <button className="button" type="submit" disabled={!canSubmit}>
            {state === "hashing" ? "正在计算哈希…" : state === "uploading" ? "正在上传与创建…" : state === "unknown" ? "用原幂等键安全重试" : "上传并创建生产任务"}
          </button>
        </section>

        <aside className="panel document-video-readiness" aria-labelledby="document-video-readiness-title">
          <p className="eyebrow">EXECUTION CONTRACT</p>
          <h2 id="document-video-readiness-title">真实执行边界</h2>
          <dl>
            <div><dt>哈希与版本绑定</dt><dd><Badge tone="success">强制</Badge></dd></div>
            <div><dt>页面文本与截图</dt><dd><Badge tone="success">Worker</Badge></dd></div>
            <div><dt>分镜与质量审核</dt><dd><Badge tone="success">人工门禁</Badge></dd></div>
            <div><dt>Agnes 动态底图</dt><dd><Badge tone="warning">可选降级</Badge></dd></div>
          </dl>
          <p>扫描件 OCR、自动证据可读性评分和 Agnes 付费调用仍不会伪装成已完成；相应缺口会失败或写入质量报告。</p>
        </aside>
      </form>
    </div>
  );
}
