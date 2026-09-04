# Vistora 数据库迁移

> 文档状态：当前迁移规则（2026-09-05）
> 迁移方向：仅前向；恢复依赖备份，不依赖反向 SQL

`db/migrations/` 保存 Control API 的 PostgreSQL 前向迁移；`services/worker/migrations/` 保存 Worker 运行时表迁移。当前根迁移从 `0001_initial.sql` 连续到 `0027_document_retention_and_purge.sql`，Worker 另有 `0001_runtime_state.sql`。实际文件列表是唯一顺序来源。

不要逐个手工执行某一个 SQL 文件。统一迁移入口会按文件名排序执行两组迁移、在 `schema_migrations` 记录 SHA-256，并使用 PostgreSQL advisory lock 串行化部署：

```powershell
$env:FRAMEFACTORY_DATABASE_URL = "postgresql://vistora:vistora-local-only@127.0.0.1:55433/vistora"
.\.venv\Scripts\python.exe -m framefactory_api.migrate --project-root .
```

本地 `start.ps1` 会自动运行同一入口。生产 Compose 使用一次性 `migrate` 服务，并要求迁移成功后才启动 API 和 Worker。

## 不可变规则

- 已登记的 SQL 不得修改；校验和漂移会阻止启动。变更 Schema 必须新增迁移。
- 迁移是前向的；数据库回滚依赖经过验证的备份恢复，不依赖反向 SQL。
- `workspace_id` 当前是单默认工作区的数据边界，不代表已启用工作区切换、成员权限或完整 RLS。
- `app.system_actor` 只供可信内部事务使用，不得成为客户端可控参数。
- 队列消费者通过事务、租约和 `FOR UPDATE SKIP LOCKED` 领取工作。

## 最近的控制面迁移

- `0025_webpage_video_pilot_feedback.sql`：保存网页试点反馈、匿名化客户指标和汇总所需索引；录入值与系统哈希证据保持区分。
- `0026_document_video_control_plane.sql`：增加不可变 Document Source、上传状态和文档 Run 来源绑定。
- `0027_document_retention_and_purge.sql`：增加 retention、Legal Hold、上传授权到期 fence、Purge 请求、租约、重试状态和 sanitized tombstone 约束。

文档 Purge 的数据库状态不是对象删除的替代品。Worker 必须确认源对象、派生 Artifact、所有对象版本与 delete marker 均已删除，才能提交 `purged`；Run、Step、Review、哈希 lineage 和审计事件继续保留。生产升级必须在启用 Bucket versioning 的隔离环境演练崩溃/重试和空库恢复。

官方 Skill 和 Pipeline 的运行种子位于 `packages/seeds/official-skills/v1/`，不在 `db/` 内复制媒体、用户数据、运行产物或密钥。`db/seeds/official-seed.example.json` 仅是中性导入信封示例；正式资源由 manifest、契约和迁移共同约束。
