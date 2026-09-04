# Vistora 生产部署基线

> 文档状态：自托管后端基线（2026-09-05），不是完整公网部署
> 发布结论：缺少身份、出站策略、Secret 或恢复证据时必须视为 `BLOCKED`

`deploy/production/` 提供 API、Worker、PostgreSQL、Redis 和 S3-compatible 存储的自托管基线。它不是完整公网安全边界；Web 需独立构建，并通过 HTTPS `NEXT_PUBLIC_FRAMEFACTORY_API_URL` 访问受保护的 API。

当前数据库迁移集合到 `0027_document_retention_and_purge.sql`，官方 Seed manifest 为 `1.12.0`。配置文件或 Compose 健康检查通过只证明声明和启动探针成立；真实 Provider、身份网关、浏览器出站策略、备份恢复、版本化 Purge 和用户端成片仍须在同一候选版本的目标环境执行发布门禁。

本目录与根目录的 `deploy/docker-compose.persistence.yml` 用途不同：后者直接运行时默认使用 `55432/56379/59000/59001`；根 `start.ps1` 会显式覆盖为 `55433/56380/59002/59003`。两者都只属于开发环境，生产文件不得复用其凭据或数据卷。

## 必需边界

- API 容器只绑定受控网络或 loopback，由 HTTPS 反向代理和身份网关对外服务。
- API Key 设置接口只是凭据管理数据，不替代请求认证。
- 每个 Secret 使用独立文件或部署平台 Secret；`.env` 和 Secret 文件不得提交。
- 为默认用户/工作区设置稳定 UUID，并配置唯一浏览器 Origin。
- PostgreSQL、Redis 和对象存储使用生产专用账户、TLS/私网和备份策略。
- 文本、视觉、ASR、语音、素材与渲染 Provider 按实际 Pipeline 配置；缺失能力必须 fail closed。
- `browser-capture-worker` 只消费 `browser-capture`；普通 `worker` 明确只消费 `run-steps`。浏览器容器不接收模型、Runway、素材分析或媒体 Provider Secret。
- API 的 `FF_WORKER_CAPABILITIES` 是两个 Worker 的能力并集。Worker 启动时会把其负责的声明能力与真实 Provider registry 对照；只有占位实现时直接失败。该检查不等同于 Provider 在线探测，也不代替消费者心跳。

## 模型、字幕与公开对象 URL

网页写稿由普通 Worker 的 OpenAI-compatible structured-text Provider 执行。必须配置独立的 `FF_OPENAI_API_KEY_FILE`、HTTPS `FF_OPENAI_BASE_URL` 和三个明确模型名；这些值不会注入浏览器 Worker。网页流水线同时要求 `FF_LEGACY_MEDIA_ENABLED=true`，否则音频和 FFmpeg 渲染能力的启动探针会失败。普通 Worker 镜像固定安装可再分发的 Noto CJK 字体，并用 `Noto Sans CJK SC` 渲染中文字幕；镜像构建会通过 Fontconfig 检查字体可解析。

标准主题创作还要求独立的结构化搜索凭据和 PostgreSQL 素材库。`FF_RESEARCH_SEARCH_URL` 与
`FF_RESEARCH_SEARCH_TOKEN_FILE` 必须成对配置；`FF_RESEARCH_SEARCH_PROTOCOL=dashscope` 时还必须指定 `FF_RESEARCH_SEARCH_MODEL`。普通 Worker 会把搜索结果限定为明确返回的
HTTPS URL，文本模型不能自行冒充搜索。生产 Compose 固定启用数据库素材库，并要求独立的视觉
分析与 ASR Secret，使新补采素材只有在完成扫描、分析和权利门禁后才进入 `media.select`。
素材字节通过 ClamAV `INSTREAM` 协议发送到仅位于后端网络的 `clamav` 服务；Worker 不共享源文件
路径给扫描容器。ClamAV 签名库存放在独立持久卷并由 `freshclam` 更新。超过
`FF_CLAMD_MAXIMUM_STREAM_BYTES` 的文件会失败关闭；该值必须与 ClamAV 的 `StreamMaxLength`
运维配置保持一致。

内部对象 CRUD 继续使用 `http://s3:9000`。API 给 Web 返回的 presigned URL 必须使用 `FF_S3_PUBLIC_ENDPOINT_URL` 指定的浏览器可达 HTTPS 反向代理，例如 `https://media.example.com`。反向代理必须原样传递签名所依据的外部 `Host`（以及 path/query），不得在验签前改写，否则 SigV4 会失败。这个 public endpoint 只注入 API，不注入 capture Worker。

PDF 文件讲解还要求普通 Worker 镜像中的 `pdfinfo`、`pdftoppm`、`ffmpeg`、`ffprobe` 和 Python `pypdf` 同时可用，并要求 S3 artifact storage 与 legacy media renderer 已配置。只有这些启动条件满足时，API 的 `FF_WORKER_CAPABILITIES` 才可声明 `document.inspect,document.extract,writing.compose.document,document.storyboard.plan,document.materialize,media.augment,document.timeline.align,render.composite,quality.evaluate.document`（另加共享的 `audio.synthesize`）。缺少任一工具必须保持能力不可用；不得用空文件或模拟成片通过门禁。当前 `media.augment` 会在 Agnes 未配置时写入明确的 provider report 并使用非事实程序化背景，不会消耗检测到但未受治理的凭据。

## 文档保留、legal hold 与删除

迁移 `0027_document_retention_and_purge.sql` 增加修订号保护的 retention/legal-hold
控制和持久化 purge 请求。删除 API 只把来源冻结为 `deletion_pending`；普通 Worker 通过
PostgreSQL `SKIP LOCKED` 与到期租约认领请求。它会删除原 PDF、所有关联 document Run
artifact，以及版本化 Bucket 中同名对象的历史版本和 delete marker。所有对象均确认不可读后，
才把来源和 artifact 行写成 sanitized tombstone。Run、步骤、review、hash lineage 和 audit 行保留，
但用户文件名、活动对象 locator 和 artifact metadata 会被清除。

API 会持久化 presigned PUT 的实际到期时间，并把 purge 的最早认领时间推迟到该时间之后，
防止旧上传 URL 在删除完成后重放同一 object key。升级时无法恢复旧 URL 的精确 TTL，因此迁移对
历史行使用 `created_at + 7 days` 的保守 fence。完成前 Worker 还会锁定关联 Run、重新核对 artifact
manifest；若有晚到 artifact 或 S3 批量删除逐项返回错误，事务不会提交 tombstone，而是安全重试。

- legal hold 使用 `assets:review`，retention/purge 使用 `assets:write`，读取进度使用
  `assets:read`；目标身份网关必须真正签发并限制这些 scope。
- purge 凭据除普通对象读写外，还必须只在业务 Bucket 上允许 `GetBucketVersioning`、
  `ListBucketVersions`、`DeleteObject` 和 `DeleteObjectVersion`。缺少任一权限时请求应进入
  `retrying/failed`，不得提前把 PostgreSQL 标为已删除。
- 活跃 Run、未到期 retention 或 legal hold 必须返回冲突。purge 已进入 `purging` 后不允许
  竞态添加 hold；先取消/完成 Run，再由有权用户发起删除。
- `failed` 请求可能已经删掉部分对象；来源保持冻结，必须先调查 `last_error`，修复凭据/存储，
  再通过受控运维流程恢复该请求。禁止直接物理删除数据库 lineage 或用 factory reset 清理。
- 上线前必须在启用 Bucket versioning 的隔离环境验证：Worker 在源对象删除后崩溃、租约到期、
  重试后幂等完成、对象各版本均不存在、下载 API 返回 404、review/audit 仍可查询。

## 网页截图 egress 边界

`browser-capture-worker` 使用与 Python Playwright `1.62.0` 匹配的官方 Chromium 镜像，非 root、只读根文件系统、`cap_drop=ALL`，并限制 CPU、内存、PID 与 tmpfs。Compose 仍不是出站安全策略：部署方必须在宿主机或编排平台上把 `capture-egress` 网络限制为只能访问 `FF_BROWSER_EGRESS_PROXY_URL`，并让代理在每次连接时拒绝 RFC1918、loopback、link-local、IPv6 ULA、Unix socket、Kubernetes/容器网段和云 metadata 地址。代理还必须自行重新解析并阻断 DNS rebinding，不能信任 Worker 的预解析结果。

只有上述策略已实际部署并验证后，才把 `FF_BROWSER_EGRESS_POLICY_ENFORCED` 改为 `true`。该变量是运维断言，不是技术证明；为空或 false 时 capture Worker 拒绝启动。应用中的 URL/redirect/subresource 校验只是第二层防护。

响应 `Content-Length`、CDP 已传输字节与资源数量用于尽早中止，但 chunked 响应或错误长度仍可能在浏览器进程内先占用内存。容器 `mem_limit`、全任务 timeout 和 egress proxy 的响应上限才是硬资源边界；上线前应对代理的单响应/总响应上限、metadata 拒绝和 DNS-rebinding 测试进行留档。

Chromium 始终以非 root 用户和 `chromium_sandbox=True` 启动，没有 `--no-sandbox` 回退。capture 服务启动与容器 healthcheck 都会实际启动一个不联网的本地页面，连同 PostgreSQL、Redis、S3 一起验证；在目标 Docker/seccomp/内核组合上失败即为发布阻断。

## capture 专用凭据与网络

Compose 不会为内置 PostgreSQL、Redis 或 MinIO 自动创建 capture 角色、ACL 或访问密钥。部署前必须由管理员预置 `FF_CAPTURE_DATABASE_URL_FILE`、`FF_CAPTURE_REDIS_URL_FILE`、`FF_CAPTURE_S3_ACCESS_KEY_FILE`、`FF_CAPTURE_S3_SECRET_KEY_FILE` 指向的独立凭据；不能填普通 Worker URL、Redis 密码或 MinIO root key。未预置时 healthcheck 必须失败，不能降级复用。

- capture 数据库角色只授予 scheduler 所需 run/run-step/review/artifact 读写和 `webpage_capture_attempts` INSERT/SELECT；不得授予 migration、账户、API 控制面或任意 DDL。当前表没有为该进程提供可表达的逐 Run RLS，因此浏览器沙箱逃逸后的数据库行级影响仍是 P1 残余，需由外部数据库代理/独立实例进一步隔离。
- capture S3 key 固定为 `browser-capture/workspaces/...`；capture key 只可在该前缀读写，API/普通 Worker只需读取该前缀以审核和 materialize。任何其他 `FRAMEFACTORY_S3_KEY_PREFIX` 都会拒绝启动。
- capture Redis 使用独立 URL/ACL，只允许消费 `browser-capture` 所需命令和 key。现有兼容队列的 job hash 不把 queue 名编码进 key，因此 Redis ACL 无法仅凭 key pattern 完全隔开 normal job hash；严格多租户部署必须使用队列代理或独立、显式路由的 Redis，这是上线前需接受或消除的 P1 残余。
- capture Worker 只连接 internal `capture-backend`（PostgreSQL/Redis/S3）与 `capture-egress`；API、普通 Worker 不加入 `capture-backend`，浏览器进程不能直接命中无鉴权内部 API。

页面公开性和素材使用权仅记录用户在提交时的明确声明（`rights_basis=user_attestation`），不冒充平台完成了版权或许可证独立核验；renderer manifest 保持 `rights_verified=false`。运营方仍需提供投诉、下架与审计流程。

监控至少应告警：`browser-capture` 最老 ready job 的等待时长超过 60 秒、连续失败/策略拒绝率、capture healthcheck 连续两次失败、内存 OOM、任务 timeout，以及截图审核长期停留。API 的静态能力声明不能证明消费者存活；必须结合容器健康、队列等待时长和 Worker 日志告警。

复制 `.env.example` 到机器本地 `.env`，填入 `FF_DEFAULT_USER_ID`、`FF_DEFAULT_WORKSPACE_ID`、`FF_CORS_ALLOW_ORIGINS` 以及各 URL/密钥文件。URL 内密码的保留字符必须百分号编码。

## 部署

```powershell
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml config --quiet
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml build --pull
docker compose --env-file deploy/production/.env `
  -f deploy/production/compose.yml up -d
```

一次性 `migrate` 服务拥有 Schema 变更权；API 和 Worker 只在校验和迁移成功后启动。已应用 SQL 不可修改，数据库迁移保持前向。

## 发布门禁

先运行仓库单元/构建检查，再根据目标环境运行 `tools/release/` 中的安全、API E2E、恢复、PostgreSQL 恢复、S3 完整性和素材验收工具。示例：

```powershell
python tools/release/security_gate.py
python tools/release/api_e2e_gate.py
python tools/release/recovery_gate.py
python tools/release/postgres_restore_gate.py
python tools/release/s3_integrity_gate.py
python tools/release/asset_contract_gate.py --report artifacts/release/asset-contract-gate.json
python tools/release/document_video_e2e_gate.py
```

门禁缺少专用 URL、fixture、隔离恢复库、服务控制权限、对象存储凭据或 Provider 能力时返回 `BLOCKED`；这不是通过。恢复门禁会停止并重启指定 Worker，只能使用专用非生产目标。

PDF 成片门禁必须使用有文本层、已获合法使用授权的真实 PDF，并显式设置
`FF_RELEASE_DOCUMENT_RIGHTS_CONFIRMED=1` 与
`FF_RELEASE_DOCUMENT_AUTO_APPROVE=1`。后者只允许用于受保护验收工作区：脚本会在读取并核对
storyboard/quality 的不可变哈希证据后批准两个审核点。最小配置如下；API URL 在非本机环境必须为
HTTPS，HTTP 仅可在隔离本地环境配合 `FF_RELEASE_ALLOW_HTTP=1` 使用。

```powershell
$env:FF_RELEASE_API_URL = 'https://release-api.example.com'
$env:FF_RELEASE_API_TOKEN = '<release identity token>'
$env:FF_RELEASE_WORKSPACE_ID = '<isolated release workspace UUID>'
$env:FF_RELEASE_DOCUMENT_PDF = 'D:\release-fixtures\rights-approved-text.pdf'
$env:FF_RELEASE_DOCUMENT_RIGHTS_CONFIRMED = '1'
$env:FF_RELEASE_DOCUMENT_AUTO_APPROVE = '1'
$env:FF_RELEASE_DOCUMENT_VERIFY_PURGE = '1'
python tools/release/document_video_e2e_gate.py
```

成功结果同时证明：PDF 全字节 SHA-256 上传/完成、Run 幂等重放、服务端来源快照、storyboard
页码/哈希绑定、按顺序完成两个人工审核点、必需交付物清单、final MP4 下载后的大小/哈希、H.264
视频、AAC 音频、目标分辨率、时长与 quality report 一致，以及持久化事件序列。它验证浏览器所调用的同一组
API 和 presigned URL，但不替代浏览器自动化、恶意 PDF 语料、容量、故障注入或恢复门禁。
`FF_RELEASE_DOCUMENT_VERIFY_PURGE=1` 是破坏性、仅限隔离验收工作区的收尾步骤：它先证明
retention 和 legal hold 会阻止删除，再删除该 fixture 的源文件与全部派生产物，核对下载均为 404，
同时确认 Run、两次 review 和事件 lineage 仍可查询。受保护 CI 固定启用此项。

## 备份与回滚

- PostgreSQL 使用 `pg_dump --format=custom`，保留匹配的应用版本和迁移集合。
- 对象 Bucket 启用版本化或不可变异地复制。
- 定期恢复到空的隔离数据库，并比较表、行数、对象和内容哈希。
- 应用回滚必须确认 Schema 向后兼容；数据库回滚依赖恢复经过验证的备份。

反向代理证书、DNS、身份策略、监控告警、容量规划和异地保留周期属于安装环境责任，不内嵌在仓库默认值中。
