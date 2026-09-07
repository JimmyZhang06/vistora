# Vistora Control API

> 文档状态：当前组件说明（2026-09-05）
> 运行边界：单用户、单默认工作区；公网请求认证尚未内建

本包是 Vistora 的 FastAPI 控制面。Python 模块名仍为 `framefactory_api`，环境变量仍使用 `FRAMEFACTORY_` 前缀，以保持迁移兼容；产品数据和本地持久化栈已经使用独立的 Vistora 数据库、Bucket、Redis namespace 和 Compose 项目。

## 当前接口范围

- `/healthz`、`/readyz`、`/v1/context`
- 小红书规范主页首屏对标、笔记身份恢复、本机扫码连接、视频研究任务和只读历史归档
- 账户资料、偏好、Session、API Key 元数据和明确不可用的双因素入口
- Channel CRUD 与暂停/恢复
- Skill、可编辑草稿、校验、测试、发布、分叉和不可变版本
- Run 估算/创建/取消，Step 查询/重试/审核，Artifact 和 Event 查询
- Generation Batch 创建、列表、取消和失败项重试
- 独立 Full-AI 报价、创建、状态与付费对账控制面
- 独立网页发现、截图、范围审核、Storyboard 审核与成片控制面
- 网页试点反馈写入与跨 Run 证据汇总；反馈是团队录入数据，不是独立核验结果
- PDF Document Source 创建、预签名上传完成、来源读取，以及独立文档视频 Run 创建
- PDF retention、Legal Hold、异步 Purge 请求与进度查询
- Library Build Job 创建、进度、取消与失败状态
- 素材库、预签名上传、公开视频导入、分页/筛选、批量审核/标签/重分析、软删除/恢复
- 素材来源、分析、片段、分析任务、使用记录、下载变体和状态转换

持久化资源通过 `packages/contracts/schemas/v1` 校验。官方 Skill 和用户 Skill 使用相同的资源、Repository 和执行引用；服务端拥有 ID、工作区、生命周期、revision、时间戳、内容哈希和 Run 组合快照。

## 单工作区与并发边界

当前 `DefaultWorkspaceContextProvider` 返回安装时配置的默认用户和工作区。`X-Workspace-Id` 是预留字段，不能在当前模式中选择其他租户。工作区 CRUD、成员邀请、跨工作区管理、完整 RLS 和请求认证尚未开放。

草稿和可变资源更新使用响应 `ETag` 对应的 `If-Match`；过期 revision 返回 `412 REVISION_CONFLICT`。创建和动作请求使用 `Idempotency-Key`；相同键配不同请求体返回 `409 IDEMPOTENCY_KEY_REUSED`。

API Key 接口目前只管理哈希凭据记录，不会自动为所有 HTTP 请求启用认证。公网部署必须在 API 前增加 HTTPS 和身份网关。

## 本地运行

完整、持久化的本地环境请在仓库根目录运行：

```powershell
.\start.ps1
```

它使用下列独立连接，不复用旧 FrameFactory：

```text
PostgreSQL  postgresql://vistora:…@127.0.0.1:55433/vistora
Redis       redis://127.0.0.1:56380/0  (namespace: vistora-local)
MinIO       http://127.0.0.1:59002     (bucket: vistora-local)
API         http://127.0.0.1:8200
```

以上是 `start.ps1` 的集成端口。直接运行 `deploy/docker-compose.persistence.yml` 且不传覆盖变量时，使用 Compose 文件中的另一组开发默认端口。

只做 API 测试或 UI 联调时可使用非持久内存 Repository：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".\apps\api[dev]"
$env:FRAMEFACTORY_ENV = "development"
$env:FRAMEFACTORY_REPOSITORY_BACKEND = "memory"
.\.venv\Scripts\python.exe -m uvicorn framefactory_api.main:app --reload --port 8200
```

内存模式重启即丢数据，也不能代表 Worker、Redis 或对象存储集成通过。生产模式拒绝内存 Repository。

## 迁移

统一入口按顺序执行 `db/migrations/*.sql` 与 `services/worker/migrations/*.sql`：

```powershell
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://vistora:vistora-local-only@127.0.0.1:55433/vistora"
.\.venv\Scripts\python.exe -m framefactory_api.migrate --project-root .
```

迁移校验和记录在 `schema_migrations`，并由 PostgreSQL advisory lock 串行化。不要修改已执行迁移；应新增文件。生产 Compose 的一次性 `migrate` 服务使用同一入口。

## 素材边界

本地上传先创建记录和预签名 PUT，再完成上传并进入素材分析队列。素材只有在版权状态、扫描、分析和审核条件满足后才可进入 `ready`。分析结果、代表帧、预览、来源、人工审核和 Run 使用证据均持久化。

`POST /v1/asset-imports` 处理用户明确给出的公开视频链接并要求权利确认；平台解析器不是版权授权。自动补采只允许 Run 快照中启用的来源和预算，找不到权利与语义均合格的素材时会 fail closed 或请求审核。

旧媒体重建工具只应面向明确的源目录和独立测试计划，不能扫描整个仓库、`var/` 或生成产物目录，也不能在没有证据时把版权标为 `owned`、`licensed` 或 `public_domain`。

## 对标主页 Demo 边界

`GET /v1/benchmark-auth/xiaohongshu/status` 只检查当前本机浏览器会话；`POST /v1/benchmark-auth/xiaohongshu/qrcode` 在用户请求后创建短期二维码，已登录时保持原会话。`start.ps1 -Xiaohongshu` 自动启动项目内置的 `framefactory_api.xhs_browser`，无需外部采集服务。连接接口要求本机 peer、Host、允许的本机 Origin、默认用户/工作区和 `assets:write`，拒绝生产模式及转发请求。响应不缓存，二维码和 Provider task ID 不进入持久报告。平台风控返回 `BENCHMARK_AUTH_PLATFORM_RESTRICTED`，停止自动轮询。状态、幂等和操作边界见 [连接 SOP](../../docs/sop/xiaohongshu-connection.md)。

`/v1/benchmark-history` 返回按工作区隔离的账号报告及视频尝试归档。读取历史不会重新采集、登录或执行模型；旧数据库中的视频报告在启动时幂等补入历史。数据库和产物备份、媒体到期及容量限制仍按视频 SOP 执行。

本机可启用真实视频深析：`POST /v1/benchmark-analysis/jobs` 自动获取视频并执行场景/视觉/OCR/ASR/音轨/策略分析；结果按账号和笔记保存在独立 SQLite 研究域，支持恢复、幂等、重试、取消及受限产物读取。完整流程见 [视频深析 SOP](../../docs/sop/benchmark-video-analysis.md)。该通道必须保持回环访问；生产配置拒绝启用，与业务 PostgreSQL/Redis/素材库没有伪造的集成关系。

`POST /v1/benchmark-accounts/preview` 接受平台和主页 URL，通过 Provider 注册表路由。当前小红书 Provider 只允许精确 HTTPS 主机 `www.xiaohongshu.com` 与 `/user/profile/{24 位十六进制用户 ID}` 路径，拒绝凭据、显式端口、片段和重定向，并在采集前丢弃分享查询参数；因此不能把接口用作任意主机代理。默认先尝试公开 SSR；配置 `FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL=http://127.0.0.1:5556` 后，公开页面跳登录或结构缺失时会连接该回环地址报告的本机 CDP 端口，从用户已登录的持久化受管浏览器读取同一个规范主页。Provider 地址必须是显式 `http://127.0.0.1:<port>` Origin，CDP 主机也必须是 `127.0.0.1`，不会接收远程浏览器、任意 URL、Cookie 或客户端提供的 CDP 地址。采集限制为 8 秒、1 MiB，按规范化主页 URL 在同一进程内缓存 5 分钟，LRU 上限为 128 个主页。响应以 `acquisition.method` 区分 `public_profile_ssr` 和 `authenticated_managed_browser`，不包含 Cookie、短期令牌、简介联系方式、头像或媒体 URL，也不写入 Repository、对象存储或队列。`GET /v1/benchmark-accounts/demo` 保留为白昼小熊 Seed 的兼容入口。

`POST /v1/benchmark-accounts/report` 复用安全采集边界，并运行无外部模型依赖的确定性报告引擎。它返回原始快照、账号总体策略、账号内互动百分位、逐条标题策略、爆款机制假设、复用动作和限制。当前证据深度固定为 `public_metadata_only`；正文、封面、多图顺序、ASR、OCR 和镜头证据必须由后续本机详情 Provider 明确提供，缺失时不能推断为已经分析。

`POST /v1/benchmark-notes/source-evidence` 是正式的已登录详情 Provider。它只接受主页 URL 和该主页中发现的 24 位笔记 ID，在同一受管浏览器会话内点击平台生成的详情链接，返回页面可见正文、互动精度及媒体时长/分辨率探针。xsec token 与签名 CDN URL 不进入客户端或持久报告，响应设置 `private, no-store`。运维与兼容流程见 `docs/sop/xiaohongshu-benchmark-provider.md`。

`POST /v1/benchmark-notes/deep-report` 是媒体 Worker 与内容策略层之间的证据边界。请求必须提供顺序正确、互不重叠且不超过素材时长的时间段；每段可包含画面描述、景别、运镜、OCR、ASR、声音事件、置信度和证据帧对象键。`speech_status=absent` 用于区分“语音检测未发现语音活动”和“没有配置 ASR”，仍可能漏检轻声或音乐中的人声，不能据此证明全片无口播。ASR 文本未区分口播、唱词或背景人声。服务端据此计算镜头均长、前三秒信息密度、转写字速、低能量区间占比及证据化策略发现，并在当前 API 进程中按工作区保留最新一份结果，供 `GET /v1/benchmark-notes/deep-report/latest` 展示。该缓存不持久化，重启后会清空。`GET /v1/benchmark-notes/deep-report/demo` 的 `source_kind=synthetic_demo`，只用于产品结构验证，任何消费者都必须显著展示这一来源限制。

返回的互动数字保留来源页面的精度语义：例如 `10万+` 对应 `lower_bound=100000` 和 `precision=lower_bound`。未登录 SSR 可能把主页总量降精度；`initial_page_has_more=true` 表示仍有未采集内容。该接口不等同于全量爬虫、趋势数据库或平台官方数据源，来源结构变化、429 或网络错误会显式失败，不回退到旧快照或 Mock。

## 文档视频边界

`POST /v1/document-sources` 只接收声明为 `application/pdf`、最大 200 MiB、带浏览器计算 SHA-256 且明确确认使用权的来源描述。创建成功后返回短期预签名 PUT；`/complete` 会从对象存储读取并验证完整对象大小、媒体类型和哈希，只有 `uploaded` 来源才能通过 `/v1/document-video/runs` 创建冻结 Run。服务端解析官方 `document-video-director` 与 `document-hybrid-production` 版本，不接受客户端替换为任意 Skill/Pipeline。

Document Run 与普通 Run 共用 Step、Review、Artifact、Event、取消和重试 API。缺少文档 Worker capability、对象存储或发布 Seed 时会明确返回 blocked/unavailable，不创建“成功”占位结果。

`PATCH /v1/document-sources/{id}/retention`、`POST .../legal-hold` 和 `POST .../purge-requests` 都受工作区与 revision 约束；Purge 是持久化异步任务，不在请求线程直接删除对象。读取 Purge 进度不会返回 Bucket/object key。当前内建上下文只表达 `assets:read`、`assets:write`、`assets:review` 权限检查，真正的身份签发与公网认证仍必须由可信网关提供。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests
.\.venv\Scripts\python.exe -m ruff check apps/api
.\.venv\Scripts\python.exe -m pytest tests/contract
```

OpenAPI 运行时页面位于 <http://127.0.0.1:8200/docs>。发布前还必须执行 `tools/release/` 下与目标环境匹配的 fail-closed 门禁。
