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
