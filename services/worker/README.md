# Vistora Worker

Worker 从 Redis `runs` 队列接收唤醒，将不可变 Pipeline 图物化为 PostgreSQL `run_steps`，再通过 `run-steps` 队列执行。PostgreSQL 扫描会恢复漏发唤醒和过期租约；状态转换使用 revision/CAS 和租约 fencing，支持重试、取消、人工审核与重启恢复。

Python 包名仍为 `framefactory.worker`，配置仍使用 `FRAMEFACTORY_` 前缀。完整本地环境由根目录 `start.ps1` 配置；单独命令为：

```powershell
.\.venv\Scripts\python.exe -m framefactory.worker healthcheck
.\.venv\Scripts\python.exe -m framefactory.worker run
```

## 当前 Provider

| 操作 | 当前实现 | 启用条件 |
| --- | --- | --- |
| `research.collect` | OpenAI-compatible 结构化研究 | 文本 Provider 三个模型及对象存储完整配置 |
| `writing.compose` | OpenAI-compatible 结构化脚本 | 同上 |
| `audio.synthesize` | Edge TTS | `FRAMEFACTORY_LEGACY_MEDIA_ENABLED=true` |
| `media.select` | PostgreSQL 素材库与可选自动补采 | `FRAMEFACTORY_ASSET_LIBRARY_ENABLED=true` |
| `render.compose` | FFmpeg 时间线与字幕渲染 | 本地媒体桥与 FFmpeg/ffprobe 可用 |
| `quality.evaluate` | FFmpeg 解码级流、时长、黑屏和静音检查 | 本地媒体桥可用；否则可用文本质量 Provider |
| `web.capture.validate` / `web.capture.screenshot` | Playwright Chromium 公开网页校验与定尺寸 PNG | 仅独立 `browser-capture` Worker，且外部 egress 策略已断言 |
| `writing.compose.webpage` / `web.materialize` | 不可信页面引用写稿与已批准截图的精确字节物化 | 普通 Worker 的文本 Provider与对象存储可用 |
| 素材视觉分析 | OpenAI-compatible vision | `FRAMEFACTORY_ASSET_VISION_*` |
| 素材转写 | OpenAI-compatible ASR | `FRAMEFACTORY_ASR_*`，可选 |

未配置的操作由 `UnsupportedCapability` 明确失败，不产生占位 Artifact。`media.generate`、独立 `timeline.align`、`research.verify` 和交付打包等操作仍需要对应部署实现；不能仅凭操作名存在就宣称可用。

根启动器检测 Edge TTS 和 FFmpeg 后启用本地媒体桥，默认启用数据库素材库；只有 `var/secrets/worker-provider.env` 中允许的文本、视觉和 ASR 字段会进入 Worker 环境。

## 事实与产物边界

研究能力只引用不可变 Run 输入中明确出现的 HTTPS URL，不自行编造来源。来源少于 Skill 最低要求或确定性校验失败时进入人工审核。写作读取研究 Artifact，并检查直接引语、禁止词、未支持数量和镜头断言。

Artifact 内容寻址后写入 S3/R2/MinIO并记录数据库。重试会核对既有对象哈希。没有持久对象存储时，不调用会产生 Artifact 的 Provider。

## 素材分析与选择

上传完成后，素材分析管线依次执行文件识别、恶意软件扫描、FFprobe、SHA-256、预览、关键帧、视觉分析、时间片段、标签标准化和发布。静态图片按单帧处理；视频使用代表帧和场景边界。每个阶段保存 checkpoint，同一不可变源哈希在重启后跳过已完成阶段。

自动进入 `ready` 仍需同时满足版权 allowlist、干净扫描、完整分析、质量和审核门。嵌字、水印、安全问题、未知版权或其他 review reason 会停在 `awaiting_review`。人工批准记录 actor、revision、理由和证据哈希。

`media.select` 只读取 Run 绑定素材库中 `ready + clean + completed analysis + allowed rights` 的候选，并保存来源/许可/分析/审核快照。脚本场景与现有图片或视频语义不匹配时会以 `asset_coverage` 请求审核；人工审核不能把零覆盖伪装成可用 manifest，应补素材、改标签或退回脚本。

## 生产安全

生产模式要求 PostgreSQL/Redis 使用 TLS 或在可信私网显式允许不安全传输。Provider 超时、网络错误、408/409/425/429 和 5xx 可重试；拒绝或无效结构化输出永久失败。错误日志不得包含请求/响应正文、凭据或私密 URL。

浏览器能力使用独立 `browser-capture` 队列和进程；普通 Worker 固定消费 `run-steps`。capture-only 配置拒绝文本模型、Runway、本地媒体、素材库、素材分析和研究 Provider，避免浏览器进程持有无关凭据。应用层会校验每次导航、重定向和子资源的公开 HTTPS DNS 答案，但这不能消除 DNS rebinding 的检查/连接竞态。生产必须由连接路径上的代理或防火墙拒绝私网、loopback、link-local、Unix socket 和云 metadata 地址，并仅在真实策略生效后设置 `FRAMEFACTORY_BROWSER_EGRESS_POLICY_ENFORCED=true`。

`Content-Length` 预检、CDP 传输计数和资源数是纵深限制，不是对 chunked 或伪造长度响应的硬字节配额。硬边界由强制 egress proxy、容器 memory/pids、只读文件系统、受限 tmpfs 和全作业 timeout 共同提供。每个任务创建全新的 Chromium context，并在结束时关闭整个 browser。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest services/worker/tests
.\.venv\Scripts\python.exe -m ruff check services/worker
```

素材 CLI 的修改命令仅允许隔离测试库并要求明确确认。生产素材操作应通过 Control API 和发布门禁，不直接改数据库状态。
