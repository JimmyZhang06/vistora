# Vistora 数据契约

> 文档状态：当前公共契约说明（2026-09-05）
> 兼容策略：版本化、fail closed、已发布不可变载荷不原地修改

契约版本 `1.0.0` 使用 `schemas/v1/` 下的 JSON Schema 2020-12 和 `openapi/v1.yaml` 下的 OpenAPI 3.1。Schema 的规范 `$id` 仍位于 `https://schemas.framefactory.dev/v1/`，这是兼容标识，变更它会破坏已发布消费者。

持久资源 Schema 与客户端 Command Schema 分离。创建/更新请求不会接收服务端拥有的 ID、工作区、生命周期、revision、哈希和时间戳；OpenAPI 对请求使用 Command Schema，对响应使用资源 Schema。

当前运行时解析一个默认个人工作区。业务资源仍保留 `workspace_id`，但 `X-Workspace-Id` 只是未来模式的预留契约，不代表已提供工作区切换或成员权限。

## 不可变快照

`benchmark-auth-state` 定义本机小红书连接状态与短期 PNG 二维码，不包含 Cookie、登录用户资料或 Provider task ID；它不是持久资源或官方 OAuth 令牌。`benchmark-history-page/detail` 定义按工作区读取的报告归档，历史读取不触发采集与分析。

Worker 在投递前保存内容寻址的 `input_snapshot`；重试和崩溃恢复复用同一快照和幂等键。输出通过 Artifact 引用，而不是写入可变本地路径。Run 的组合快照包含实际 SkillVersion、PipelineVersion、素材库、声音、渲染预设与生产设置来源。

定时驱动剪辑使用五个独立的不可变载荷契约：`narration-timing` 将脚本与音频哈希绑定；`generated-material-manifest` 仅在全 AI 模式下记录 Run 范围的付费生成任务、Provider 输出和费用审计；`candidate-manifest` 保存逐 Beat 候选、权利证据、冻结的 `catalog_snapshot_id` 与 coverage；`material-selection` 冻结实际选用素材窗口；`edit-decision-list` 以帧边界和毫秒源窗口供 `render.edl` 消费。`media.inventory` 生成 kind=`inventory`、media type=`application/json` 的素材盘点 Artifact，供脚本阶段先理解冻结目录覆盖面。付费审计与下游候选决策不合并；这些载荷仍作为普通 Artifact 持久化，不替代 Run、Step 或 Artifact 聚合根。

`SkillVersion` 是声明式数据，不允许命令、模块、脚本、回调或本地路径。外部研究证据必须是 HTTPS URL，素材通过 UUID 和持久使用快照引用。

当前跨阶段载荷包括 narration timing、CandidateManifest、GeneratedMaterialManifest、MaterialSelection、EDL、LibraryBuildJob 与 GenerationBatch command。文档流程另有 `document-source-create` 和 `document-video-run-create` Command Schema；来源资源、retention、Legal Hold、Purge 状态以及文档 Run/Artifact 复用 OpenAPI 中的版本化资源契约。`benchmark-account-preview` 定义通用主页预览请求，`benchmark-account-snapshot` 描述公开主页首屏样本、精度语义、采集边界与派生指标，`benchmark-account-report` 定义账号级及逐条公开元数据报告。`benchmark-note-source-evidence` 是已登录浏览器 Provider 的脱敏详情证据包，不允许包含 Cookie、xsec 令牌或签名 CDN URL。`benchmark-media-evidence` 定义 Worker 到策略层的有序多模态时间轴（包括“检测到语音活动 / 未检测到语音活动 / 未知”的三态语义，不能据此判定人声属于口播或唱词），`benchmark-note-deep-report` 定义可追溯的深度单条报告；最新报告只保存在 API 进程内存中，不是持久资源。新增载荷必须进入 Schema 集合、OpenAPI/运行模型映射和合同测试，不能只在某一端增加自由字典。

Canonical OpenAPI 已覆盖 `/v1/document-sources` 的创建、读取、完成上传、保留期、Legal Hold、Purge 请求/进度，以及 `/v1/document-video/runs`。它也覆盖网页试点反馈与汇总。任何包含签名上传 URL 或 Artifact 下载 URL 的响应都只把 URL 视为短期交付信息；不可变契约保存哈希和服务端对象引用，不把会过期的 URL 写入长期证据报告。

## 内容哈希

补充：上述进程内最新报告仅指旧的 `benchmark-notes/deep-report/latest`。新版 `benchmark-analysis-job.schema.json` 和 `/v1/benchmark-analysis/*` 定义本机持久任务，按账号与笔记保留生命周期、来源、Worker 版本化分析、严格报告及尝试级产物。研究任务不复用生产 Run/Artifact 队列；`analysis` 保留可扩展的 Worker JSON 载荷，稳定呈现契约由 Web 类型映射和真实结果 Schema 校验共同验证，后续正式发布需收紧嵌套载荷 Schema。

`SkillVersion.content_hash` 是九个不可变策略字段组成对象的 RFC 8785 JCS UTF-8 字节 SHA-256：

1. `input_schema`
2. `research_policy`
3. `writing_policy`
4. `visual_policy`
5. `asset_policy`
6. `qc_policy`
7. `capability_requirements`
8. `output_contract`
9. `default_pipeline_version_id`

以下元数据明确排除（excluded）在哈希域之外：`id`、`workspace_id`、`skill_id`、`version`、`state`、`created_at`、`published_at`，以及 `content_hash` 自身。PipelineVersion 对其声明式节点和能力要求使用同一规范化原则。服务端在发布不可变版本时计算哈希；客户端和 Worker 可以复算验证，但不得自行覆盖。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract
```

合同测试同时检查静态 OpenAPI 与 FastAPI 实际路由的双向集合、Schema `$ref`、迁移顺序和关键 runtime wiring。它能发现结构漂移，但不能代替真实 PostgreSQL、对象存储或 Provider E2E。

消费者应按每个 Schema 的规范 `$id` 注册整个 `schemas/v1` 集合。示例 fixture 只用于契约说明和测试，不是生产 Seed 或用户数据。
