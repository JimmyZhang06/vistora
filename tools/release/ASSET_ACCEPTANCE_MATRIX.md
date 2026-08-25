# Vistora 素材发布验收矩阵

> 文档状态：规范性发布要求
> 结果来源：同一候选提交生成的机器报告；本文不记录一次性通过状态

本文件定义发布要求，不保存会迅速过期的“当前通过”截图。每次发布的真实状态以同一提交上生成的机器可读 Gate 报告为准。

所有门禁使用 fail-closed 退出语义：`PASSED=0`、`FAILED=1`、`BLOCKED=2`。缺少专用环境、fixture、权限或外部能力必须是 `BLOCKED`，不能降级为内存 Repository、跳过或视为通过。

## 当前实现基线

仓库现已具备预签名上传、素材分页/筛选、批量审核/标签/重分析、分析任务查询、revision/CAS 审核、软删除/恢复、来源/许可记录、代表帧与预览描述、Run 素材使用快照、数据库素材库范围选择和 Web 素材控制面。

这只说明功能入口存在，不等于生产发布通过。RLS/工作区隔离、队列背压、并发规模、恶意文件、对象一致性、恢复、全量哈希、外部 Provider 和真实媒体 E2E 仍必须由目标环境 Gate 证明。

## 验收矩阵

| 链路 | 必须证明 | 主要 Gate |
| --- | --- | --- |
| 创建→签名→PUT→完成 | 大小/hash/MIME、幂等、孤儿对象、图片和视频真实上传 | contract + asset E2E |
| 扫描→分析→审核→ready | 失败隔离、clean 扫描、完整分析、revision/CAS、唯一审核记录 | API/Worker tests + asset E2E |
| 版权与安全 | unknown/restricted、恶意文件、水印/嵌字/安全 review 不得自动泄漏 | contract + integrity audit |
| 工作区与素材库 | 所有查询、对象、批量操作和 Run 选择严格限制到上下文与绑定库 | protected E2E |
| 分页/筛选/批量 | 1k/5k 遍历无重漏，游标不可跨筛选复用，批量计数可核对 | asset load gate |
| 队列与恢复 | 高低水位、429/Retry-After、独立重试、租约恢复和幂等恢复 | load + recovery gate |
| Scene 检索 | 只使用 eligible 候选，保留评分/来源/审核快照，零覆盖 fail closed | Worker integration + video E2E |
| 软删除与历史 | 删除源素材后旧 Run 证据和 Artifact 仍按策略可追溯 | asset E2E + integrity audit |
| DB/Object 一致性 | object key、workspace 前缀、metadata、代表帧和内容哈希一致 | S3 + asset integrity audit |
| 大文件与资源限制 | 声明/实传超限拒绝，转码/分析不会绕过预算或拖垮队列 | contract + load gate |
| 网页截图素材 | 页面稳定、公开网络边界、截图哈希、范围/Storyboard 审核和权利声明 | browser sandbox + webpage E2E |
| Full-AI 生成素材 | 无共享素材回退、付费幂等、费用上限、生成来源、独立验证与披露 | generated-only E2E + paid ledger audit |

## 规模硬断言

- 1k/5k 分页必须遍历出与独立数据库计数相同的 ID 集合，游标不得循环。
- Bulk 操作必须报告总数、成功数和失败数；重复请求保持幂等。
- 背压阈值和并发数由 Gate fixture 固定，过载只允许契约规定的 429，不允许以 5xx 冒充。
- 隔离 sentinel 的数量和固定 ID/hash 集合必须精确一致；任何一个进入 Worker eligible 或 Run snapshot 都是发布失败。

不要在文档中硬编码生产 sentinel 数量；具体清单由受保护环境的 fixture manifest 和报告保存。

## 命令

```powershell
# 静态契约与测试夹具
.\.venv\Scripts\python.exe -m pytest tools/tests/test_asset_release_gate.py
.\.venv\Scripts\python.exe tools/release/asset_contract_gate.py `
  --report artifacts/release/asset-contract-gate.json

# 专用环境真实链路
.\.venv\Scripts\python.exe tools/release/asset_e2e_gate.py
.\.venv\Scripts\python.exe tools/release/asset_load_gate.py
.\.venv\Scripts\python.exe tools/release/asset_integrity_audit.py `
  --rehash-all --report artifacts/release/asset-integrity-audit.json
```

Live Gate 的环境变量、workspace scope、fixture manifest 和只读/静默要求以各脚本 `--help` 及源码为准。完整性审计期间必须冻结目标写入，避免数据库和对象存储跨查询竞态。

## 发布判定

1. 单元、契约、Web 和 Worker 集成测试通过。
2. 同一候选提交的静态报告为 `PASSED`。
3. 专用 PostgreSQL/Redis/MinIO 环境的 E2E、负载、恢复和完整性 Gate 全部为 `PASSED`。
4. Provider、备份恢复、监控和安全边界均有独立证据。
5. 任一 `FAILED` 或 `BLOCKED` 都阻止发布；不得用人工文字覆盖机器报告。
