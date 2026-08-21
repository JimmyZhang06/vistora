# 素材管理与分析发布验收矩阵

本门禁采用 fail-closed 语义：`PASSED=0`、`FAILED=1`、`BLOCKED=2`。缺少并行实现、测试环境或夹具时不得视为通过。受保护门禁把 E2E、load 与 1247 审计拆成三个 workspace：前两者必须通过 `/v1/context` 精确匹配 ID 和 `ff-assets-*` 一次性 scope；load 的每条记录还必须匹配固定 fixture ID/ID 集合哈希。1247 workspace 只执行读操作。任何上下文或清单不一致都会在首次写入前阻塞。

## 当前结论

截至 2026-08-17，素材链路不满足发布条件。`asset_contract_gate.py` 的机器可读结果是权威阻塞清单；当前主要阻塞为：

- 完成上传直接写入 `scan_status=clean`、`asset.status=ready` 和 metadata-only completed analysis，绕过真实扫描、分析及人工审核。
- 没有素材明细分页/筛选、批量操作、素材审核、分析任务查询 API，也没有并发审核 revision/CAS 契约。
- 数据库缺少素材审核日志、Run 素材不可变快照、代表帧哈希/大小，以及防止 unknown/restricted/未审核素材进入 ready 的数据库级约束。
- 素材相关 RLS 在迁移中被显式关闭。
- Worker 虽过滤 workspace、ready、clean、completed analysis 和版权 allowlist，但忽略 Run 指定的素材库；manifest 也缺少完整来源/审核快照。
- Web 尚无素材列表筛选、审核、批量操作与软删除控制面。
- 现有 S3 审计只覆盖 Run artifacts；新增只读审计在上述 schema 合并前会正确失败。

## 验收矩阵

| 链路/边界 | PR 契约门禁 | 临时服务 1k | Nightly/Release 5k | 受保护 E2E | 当前状态 |
|---|---|---|---|---|---|
| 建库→签名→PUT→完成 | OpenAPI、幂等键、大小/hash/MIME 契约 | PostgreSQL+MinIO 实传图片/视频 | 并发重复上传、孤儿对象 | 两种媒体全链路 | 部分实现；完成上传错误地直接 ready |
| 扫描→分析→人工审核→ready | 状态枚举、审核表、revision/CAS | 损坏/截断/polyglot、分析失败 | 32 路重复审核 | 恰一条批准记录、其余 409 | 阻塞：无真实状态机/审核 API |
| 未知版权与 1247 隔离集合 | DB 约束及固定清单契约 | 精确 1247 sentinel，eligible=0 | 批量/重试后仍精确 1247 | Worker/Run 不得命中任一 sentinel | 阻塞：无数据库级不可放行约束 |
| 跨 workspace/素材库 | RLS、全部 library ID 校验契约 | 实际 SQL 与对象读取返回 0/403/404 | 交错 5k 数据 | Run 仅命中指定库 | 阻塞：RLS 关闭，Worker 忽略 library IDs |
| 分页、筛选、批量 | 必需参数和异步 bulk job 契约 | 1k，100/页，ID 无重/无漏 | 5k，组合筛选、并发插删、5k bulk | 发布数据抽查 | 阻塞：素材 list/filter/bulk API 缺失 |
| 队列背压 | high/low watermark、429+Retry-After 契约 | high=1000/low=750 | 5k、8 consumer、lease recovery | 发布队列探针 | 阻塞：无素材分析队列背压 |
| Worker 场景检索 | SQL 安全条件与库范围契约 | 真 PostgreSQL 查询而非 stub | 5k 优先级与相关性 | 场景命中后产出 manifest | 部分实现；缺 library scope/快照 |
| Run 快照/软删除历史 | `run_asset_snapshots` 与 artifact content 契约 | 删除源后旧 Run/制品 hash 可读 | 5k 历史引用检查 | E2E 删除后再次下载 | 阻塞：缺不可变素材证据快照 |
| DB/MinIO locator/hash | schema、前缀、metadata 契约 | active asset files + artifacts | 加代表帧、全量下载复算 SHA-256 | 全量只读审计 | 阻塞：代表帧无 hash/size，审计会失败 |
| 超大文件 | 500 MiB 上限与签名长度绑定契约 | 伪报 1B/上传超大对象 | 并发资源消耗探针 | 声明 >500 MiB 必须拒绝 | 部分实现；业务模型拒绝声明，签名 PUT 未绑定大小 |

## 规模与背压硬断言

- 1k：每页最多 100；20×50 或 10×100 全量遍历后 ID 集合与独立总数完全一致；游标不得循环、跨 workspace 或跨筛选条件复用。
- 5k：50×100 遍历无重复/遗漏；5k bulk 必须返回异步 job，最终 `processed_count=5000`、`failed_count=0`。
- 队列：high=1000、low=750；深度不得超过 high；过载仅返回 429 且带 `Retry-After`；降到 low 后恢复接纳；不得以 5xx 表示背压。
- 1247 sentinel：数量以及固定 asset ID/file ID/content hash 清单必须精确一致；production Worker eligibility=0、Run snapshot 引用=0。用一条新隔离记录替换一条被放行记录也必须失败。任何一条泄漏均为 P0 发布失败。

## 可执行命令

快速框架与规模夹具（不访问用户数据）：

```text
python -m pytest tools/tests/test_asset_release_gate.py
```

静态 API/DB/Worker/Web scaffold 契约加 FastAPI 实际路由检查（当前预期返回 1，并生成真实阻塞报告；它不是 live gate 的替代品）：

```text
python tools/release/asset_contract_gate.py --report artifacts/release/asset-contract-gate.json
```

专用 workspace 的真实素材 E2E：

```text
python tools/release/asset_e2e_gate.py
```

预置可重复 1k/5k 数据后的分页、bulk 与背压：

```text
python tools/release/asset_load_gate.py
```

生产发布前只读 PostgreSQL/MinIO 全量审计（默认精确要求 1247 个隔离素材）：

```text
python tools/release/asset_integrity_audit.py --rehash-all --report artifacts/release/asset-integrity-audit.json
```

所有 live 命令缺少专用环境变量/夹具时返回 `BLOCKED`，不会降级为内存仓库或跳过。1247 审计还要求操作员先将该 workspace 置为只读并显式设置 `FF_RELEASE_ASSET_AUDIT_QUIESCED=1`，避免 DB/S3 跨查询竞态。CI 在 PR 上运行 scaffold/API/Worker/Web/contract 门禁，在 `main` push 上强制进入受保护 live job；环境未配置时发布保持非绿。

## 推荐合并顺序

1. 数据库状态机、审核日志、Run snapshot、代表帧 hash/size、RLS 与 ready 约束。
2. API 扫描/分析任务、审核 CAS、list/filter/bulk/soft-delete 及全部 workspace/library 校验。
3. Worker 按 Run library snapshot 检索并写完整 immutable manifest。
4. Web 对齐 OpenAPI 的审核、筛选、批量与删除控制面。
5. 本门禁分支；先让静态契约转绿，再启用临时 PostgreSQL/Redis/MinIO 1k，最后启用 5k 和受保护全量审计。
