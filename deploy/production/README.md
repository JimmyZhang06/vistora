# Vistora 生产部署基线

`deploy/production/` 提供 API、Worker、PostgreSQL、Redis 和 S3-compatible 存储的自托管基线。它不是完整公网安全边界；Web 需独立构建，并通过 HTTPS `NEXT_PUBLIC_FRAMEFACTORY_API_URL` 访问受保护的 API。

本目录与根目录的 `deploy/docker-compose.persistence.yml` 用途不同：后者是 Compose 项目 `vistora-local` 的本机开发基础设施，使用 `55432/56379/59000/59001` 和独立数据卷；生产文件不得复用这些开发凭据。

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

内部对象 CRUD 继续使用 `http://s3:9000`。API 给 Web 返回的 presigned URL 必须使用 `FF_S3_PUBLIC_ENDPOINT_URL` 指定的浏览器可达 HTTPS 反向代理，例如 `https://media.example.com`。反向代理必须原样传递签名所依据的外部 `Host`（以及 path/query），不得在验签前改写，否则 SigV4 会失败。这个 public endpoint 只注入 API，不注入 capture Worker。

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
```

门禁缺少专用 URL、fixture、隔离恢复库、服务控制权限、对象存储凭据或 Provider 能力时返回 `BLOCKED`；这不是通过。恢复门禁会停止并重启指定 Worker，只能使用专用非生产目标。

## 备份与回滚

- PostgreSQL 使用 `pg_dump --format=custom`，保留匹配的应用版本和迁移集合。
- 对象 Bucket 启用版本化或不可变异地复制。
- 定期恢复到空的隔离数据库，并比较表、行数、对象和内容哈希。
- 应用回滚必须确认 Schema 向后兼容；数据库回滚依赖恢复经过验证的备份。

反向代理证书、DNS、身份策略、监控告警、容量规划和异地保留周期属于安装环境责任，不内嵌在仓库默认值中。
