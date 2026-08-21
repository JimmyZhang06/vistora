# Vistora Control API

本包是 Vistora 的 FastAPI 控制面。Python 模块名仍为 `framefactory_api`，环境变量仍使用 `FRAMEFACTORY_` 前缀，以保持迁移兼容；产品数据和本地持久化栈已经使用独立的 Vistora 数据库、Bucket、Redis namespace 和 Compose 项目。

## 当前接口范围

- `/healthz`、`/readyz`、`/v1/context`
- 账户资料、偏好、Session、API Key 元数据和明确不可用的双因素入口
- Channel CRUD 与暂停/恢复
- Skill、可编辑草稿、校验、测试、发布、分叉和不可变版本
- Run 估算/创建/取消，Step 查询/重试/审核，Artifact 和 Event 查询
- Generation Batch 创建、列表、取消和失败项重试
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
PostgreSQL  postgresql://vistora:…@127.0.0.1:55432/vistora
Redis       redis://127.0.0.1:56379/0  (namespace: vistora-local)
MinIO       http://127.0.0.1:59000     (bucket: vistora-local)
API         http://127.0.0.1:8200
```

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
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://vistora:vistora-local-only@127.0.0.1:55432/vistora"
.\.venv\Scripts\python.exe -m framefactory_api.migrate --project-root .
```

迁移校验和记录在 `schema_migrations`，并由 PostgreSQL advisory lock 串行化。不要修改已执行迁移；应新增文件。生产 Compose 的一次性 `migrate` 服务使用同一入口。

## 素材边界

本地上传先创建记录和预签名 PUT，再完成上传并进入素材分析队列。素材只有在版权状态、扫描、分析和审核条件满足后才可进入 `ready`。分析结果、代表帧、预览、来源、人工审核和 Run 使用证据均持久化。

`POST /v1/asset-imports` 处理用户明确给出的公开视频链接并要求权利确认；平台解析器不是版权授权。自动补采只允许 Run 快照中启用的来源和预算，找不到权利与语义均合格的素材时会 fail closed 或请求审核。

旧媒体重建工具只应面向明确的源目录和独立测试计划，不能扫描整个仓库、`var/` 或生成产物目录，也不能在没有证据时把版权标为 `owned`、`licensed` 或 `public_domain`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests
.\.venv\Scripts\python.exe -m ruff check apps/api
.\.venv\Scripts\python.exe -m pytest tests/contract
```

OpenAPI 运行时页面位于 <http://127.0.0.1:8200/docs>。发布前还必须执行 `tools/release/` 下与目标环境匹配的 fail-closed 门禁。
