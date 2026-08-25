# 批量生产与素材库设计

> 文档状态：核心控制面与 Worker 路径已实现；规模化发布仍需环境 Gate
> 当前 Pipeline：`standard-production/v3`
> 适用入口：`/batches`、素材库与 Library Build Job

批量生产与素材建设是两条独立链路：素材建设负责导入、上传、分析、审核和索引；批量生产只读取创建批次时冻结的素材快照。内容联网研究与素材获取也是两个独立开关，不能互相代替。

## 1. 设计目标

1. 同一 Batch 的所有 Run 使用可审计、可复现的 `CatalogSnapshot`。
2. 新素材或重新分析不会改变已经创建的 Batch。
3. 批量剪辑期间不临时联网补素材，避免每条 Run 产生不可控下载与版权风险。
4. 单条 Run 素材不足只影响该条目，其他 Batch Item 继续执行。
5. 扩充素材库后通过“重试失败项”创建新 Batch 与新 Snapshot，而不是篡改旧 Run。

## 2. 不变量

- 已发布的 `standard-production/v2` 保持不可变；批量能力使用 v3。
- 历史 Run 没有 Snapshot 时保留兼容读取语义；新 Batch 必须绑定 Snapshot。
- 批量请求不能启用 `asset_acquisition`。
- 不增加为了批量而专用的 Run 状态；错误通过稳定类型码表达。
- 系统展示素材覆盖与缺口，不承诺素材库“完备”。
- 当前不依赖向量数据库、跨 Batch 全局选片器或素材库人工 Release 生命周期。

## 3. 批次输入

`GenerationBatchCreate` 使用三态研究模式：

```json
{
  "research_mode": "off | when_missing | required"
}
```

| 模式 | 语义 |
| --- | --- |
| `off` | 禁止在线研究；Skill 要求的来源必须由输入提供 |
| `when_missing` | 只有已有来源不足时才调用配置的研究搜索 |
| `required` | 每个主题都必须执行在线研究 |

内容研究只生成事实/来源证据，不负责下载视频或图片。请求中显式启用剪辑内 `asset_acquisition` 必须返回校验错误。

## 4. 素材快照

Batch 创建时，服务端对所选素材库当前 eligible 素材生成或复用内容寻址快照，并把 `catalog_snapshot_id` 写入 Batch 与每个 Run 的 composition snapshot。

核心记录：

- `catalog_snapshots`：workspace、内容哈希与创建时间；
- `catalog_snapshot_items`：library、asset、file、analysis 与内容哈希；
- `candidate-manifest`：逐 Beat 候选、权利证据、冻结 snapshot 与 coverage；
- `material-selection`：最终素材及源窗口；
- `edit-decision-list`：渲染器消费的帧/毫秒决策。

Snapshot 是运行证据，不是素材库发布界面。创建后新增、删除、重分析或重新审核素材都不能改变既有 Snapshot。

## 5. Library Build Job

素材建设任务通过持久控制面编排，复用现有远程导入、上传与分析能力：

| 方法 | 路径 |
| --- | --- |
| POST | `/v1/library-build-jobs` |
| GET | `/v1/library-build-jobs/{id}` |
| POST | `/v1/library-build-jobs/{id}/cancel` |

状态为 `queued | running | completed | completed_with_errors | failed | cancelled`；阶段为 `discover | transfer | analyze | index`。创建接口必须先持久化再安全入队，不得返回伪完成。

文件夹上传使用有界并发；单文件失败不能中断其余文件，并保留可重试项。外部素材仍需来源、许可、扫描、分析与人工审核，不能因由 Build Job 导入而自动变为 `ready`。

## 6. 执行流水线

```text
research.collect
  -> media.inventory
  -> writing.compose
       -> audio.synthesize
       -> media.retrieve
  -> timeline.align
  -> render.edl
  -> quality.evaluate
```

`media.inventory` 生成低成本库存摘要，使脚本阶段了解冻结目录覆盖面；它不执行完整 Beat 级选片。`media.retrieve` 只能读取当前 `CatalogSnapshot`，并保存候选与权利证据。时间线和渲染只消费冻结选择，不在后续阶段重新选材。

稳定错误语义包括素材覆盖不足、研究来源不足和研究搜索不可用；它们不能降级为占位素材或静默联网。

## 7. 用户流程

1. 在素材库上传或通过 Library Build Job 建设素材。
2. 等待扫描、分析、权利与审核满足 `ready`。
3. 创建 Batch，选择素材库、主题列表和研究模式。
4. 服务端冻结 Snapshot，并为每个 Batch Item 创建独立 Run。
5. 用户在 Batch/Projects 查看逐项进度、错误和产物。
6. 素材不足时扩充素材库，再重试失败项；新请求生成新 Batch/Snapshot。

## 8. 验收要求

- 同一 Batch 的 Run 使用相同 `catalog_snapshot_id`；
- Snapshot 后续不会因素材库变化而漂移；
- `research_mode=off` 不产生在线研究请求；
- 批量请求无法开启剪辑内素材补采；
- 单条失败不取消其他条目；
- 文件夹单文件失败不影响其余上传，并可只重试失败项；
- v1/v2 单条 Run 与无 Snapshot 历史数据保持兼容；
- 分页、背压、恢复和 1k/5k 规模必须由专用环境 Gate 证明。

## 9. 尚未宣称完成

语义/视觉向量检索、素材库人工 Release 生命周期、跨 Batch 全局素材配额、自动 Canary、跨刷新分片续传以及大规模成本/容量结论仍不属于当前已证明能力。
