# Vistora 文档中心

> 文档状态：当前索引（2026-09-05）
> 适用范围：本仓库当前 `main` 工作树
> 事实优先级：代码与迁移 > 自动化测试 > 当前组件文档 > 历史资料

Vistora 的文档按用途分层维护。根 README 负责产品与启动总览；组件 README 负责可执行操作；本目录保存跨模块架构、管线设计和历史参考。文档不能替代运行时 capability/readiness、发布 Gate 或用户的素材权利审核。

## 快速入口

| 目标 | 文档 |
| --- | --- |
| 了解产品、能力边界与本地启动 | [项目总览](../README.md) |
| 理解当前系统拓扑与可靠性边界 | [当前工程架构](ACTUAL_ENGINEERING_ARCHITECTURE.md) |
| 理解网页截图成片管线 | [项目总览：网页截图成片](../README.md#网页截图成片) |
| 理解 Full-AI 生成管线 | [Full-AI 视频管线](FULL_AI_VIDEO_PIPELINE.md) |
| 理解批量生产与素材快照 | [批量生产与素材库](BATCH_LIBRARY_IMPLEMENTATION_PLAN.md) |
| 查看 2026-09-04 的逐功能工程就绪快照与发布阻断 | [产品工程就绪矩阵](PRODUCT_ENGINEERING_READINESS_MATRIX.md) |
| 运行 PDF 文档本地渲染 Pilot | [Document hybrid pilot](../examples/document-hybrid/README.md) |
| 准备 HKSTP Ideation 申请材料 | [申请包索引](application/hkstp-ideation-26-23/README.md) |
| 部署受保护的后端基线 | [生产部署](../deploy/production/README.md) |
| 判断是否可以发布 | [素材发布验收矩阵](../tools/release/ASSET_ACCEPTANCE_MATRIX.md) |
| 查看许可 | [PolyForm Noncommercial License](../LICENSE.md) |

## 当前组件文档

| 组件 | 责任边界 | 文档 |
| --- | --- | --- |
| Web | 创建、审核、项目、素材、Skill 与设置工作台 | [Web](../apps/web/README.md) |
| Control API | 资源、幂等、并发、控制面与 readiness | [Control API](../apps/api/README.md) |
| Worker | DAG 调度、Provider、浏览器采集、渲染与恢复 | [Worker](../services/worker/README.md) |
| PostgreSQL | 前向迁移、校验和与恢复约束 | [数据库迁移](../db/README.md) |
| Contracts | OpenAPI、JSON Schema 与不可变载荷 | [数据契约](../packages/contracts/README.md) |
| Official Seeds | Skill/Pipeline 声明式启动数据 | [官方 Seed](../packages/seeds/README.md) |
| Local data | 本机日志、Secret、测试输入与运行状态 | [本地数据边界](../var/README.md) |

## 当前跨模块材料

- [HKSTP 申请总底稿](HKSTP_IDEATION_APPLICATION_MASTER_BRIEF.md)：统一产品、工程、商业与申请主张；其中待验证信息不能直接变成公开事实。
- [产品工程就绪矩阵](PRODUCT_ENGINEERING_READINESS_MATRIX.md)：按用户入口映射 Web、API、Worker、恢复语义和外部验收缺口；其中 Gate 结论是评估日快照，每个候选版本必须重新执行，不能沿用旧的“通过”。
- [无素材编辑草案](no-asset-editorial-draft.md)：标准素材不足时的编辑策略研究稿，不是已上线能力说明。
- `application/hkstp-ideation-26-23/`：申请表、Deck、视频稿、客户验证、尽调和证据登记材料；属于内部审阅资产，不是产品运行文档。

## 文档维护规则

1. 当前能力必须标成“可用”“条件可用”或“未实现”，不能用路由、枚举或接口名称推断能力已经接通。
2. 端口以根 `start.ps1` 为本地集成入口；直接运行 Compose 时使用 Compose 自身默认值，二者必须明确区分。
3. 不在长期文档中固定测试通过数量、临时 Run ID、供应商价格或本机容器状态；这些属于发布报告。
4. API、Schema、Pydantic、TypeScript 和 Worker 模型发生变化时，必须同时检查相应组件文档。
5. 文档中的 PowerShell 命令默认从仓库根目录执行，除非代码块前明确改变目录。
6. Secret、真实用户素材、截图、视频、日志和 Gate 报告不进入文档目录。

## 历史参考

`reference-v3/` 保存 2026-08-14 至 2026-08-20 的旧仓库设计与运行复盘。它们仅用于解释设计来路；旧路径、端口、Run ID、时间计划、测试数量和“当前”措辞均不是现行操作依据。

- [通用化重构计划](reference-v3/planning/REFACTOR_PLAN.md)
- [v3 技术架构与剪辑成片评审](reference-v3/architecture/V3_ARCHITECTURE_AND_EDITING_REVIEW.md)
- [v3 素材检索与剪辑优化方案](reference-v3/architecture/EDITING_AND_ASSET_RETRIEVAL_OPTIMIZATION_PLAN.md)
- [自动视频任务监督与素材补足手册](reference-v3/operations/automatic-video-runbook.md)
- [Architecture 解说视频冒烟测试](reference-v3/operations/architecture-video-smoke-test.md)

旧仓库的运行产物、用户数据、下载素材、密钥和发布报告不是文档资产，没有迁入本目录。
