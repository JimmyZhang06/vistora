# Vistora 文档索引

现行操作说明尽量与实现放在同一目录，避免跨模块文档与代码脱节：

- [项目总览与本地启动](../README.md)
- [非商业软件许可](../LICENSE.md)
- [Control API](../apps/api/README.md)
- [Web](../apps/web/README.md)
- [Worker 与 Provider](../services/worker/README.md)
- [数据库迁移](../db/README.md)
- [生产部署](../deploy/production/README.md)
- [OpenAPI 与 JSON Schema](../packages/contracts/README.md)
- [官方 Seed](../packages/seeds/README.md)
- [素材发布验收矩阵](../tools/release/ASSET_ACCEPTANCE_MATRIX.md)
- [本地运行数据边界](../var/README.md)

文档位置约定：组件 README 跟随组件；可执行门禁说明跟随脚本；跨模块长期设计放在 `docs/`；本地日志、密钥和测试输入只放 `var/`。

## v3 历史经验

`reference-v3/` 是旧仓库 `v3/docs` 的历史快照，用来保留重构、架构评审、剪辑优化和运行复盘经验。每份文件顶部都标明历史边界。其旧路径、端口、Run ID、测试结论和“当前/现行”措辞只描述快照日期，不得作为新仓库的启动或发布依据。

### 规划

- [通用化重构计划（历史）](reference-v3/planning/REFACTOR_PLAN.md)

### 架构

- [v3 技术架构与剪辑成片评审（历史）](reference-v3/architecture/V3_ARCHITECTURE_AND_EDITING_REVIEW.md)
- [v3 素材检索与剪辑流程优化方案（历史）](reference-v3/architecture/EDITING_AND_ASSET_RETRIEVAL_OPTIMIZATION_PLAN.md)

### 运行复盘

- [自动视频任务监督与素材补足手册（历史）](reference-v3/operations/automatic-video-runbook.md)
- [Architecture 解说视频冒烟测试（历史）](reference-v3/operations/architecture-video-smoke-test.md)

旧仓库的运行产物、发布结果 JSON、用户素材和密钥不属于文档资产，因此没有迁入。
