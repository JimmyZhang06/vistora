# Full-AI 视频管线

> 文档状态：已实现、默认 fail closed
> 当前可用性：无真实 Provider/条款/定价/权利配置时为 `blocked`
> 产品入口：`/create/ai`

Full-AI 是与标准素材生产、网页截图成片相互隔离的生成管线。它只使用本次 Run 生成并验证的媒体，不读取共享素材库，也不会回退到 Wikimedia、YouTube、Bilibili 或其他远程素材路径。

## 1. 产品与版本边界

| 项目 | 当前定义 |
| --- | --- |
| Pipeline slug | `full-ai-production` |
| 新任务版本 | `2` |
| 历史版本 | `1` 保持不可变，不用于新任务 |
| 视觉来源策略 | `generated_only` |
| Control API | `/v1/full-ai/*` |
| 默认状态 | `blocked`，不提交付费任务 |

标准 `/v1/runs`、Generation Batch 和 Channel 默认组合不能创建 Full-AI Pipeline；错误必须在持久化或入队前以 `FULL_AI_ENDPOINT_REQUIRED` 明确返回。

## 2. 执行图

```text
writing.compose.generated
  -> audio.synthesize
       -> media.generate
            -> timeline.align
                 -> render.edl
                      -> quality.evaluate
```

`media.generate` 同时依赖脚本和真实旁白 timing，并按场景提交有界任务；系统不把整条视频简化为一个不可审计的长 Provider 请求。

| 节点 | 直接输入 | 必须产物 |
| --- | --- | --- |
| `write` | 创意 brief、冻结计划 | 与授权 Beat 数一致的原创脚本 |
| `tts` | 脚本 | 音频与原生/验收后的 narration timing |
| `generate` | 脚本、音频、timing | Run 范围生成素材、付费审计、CandidateManifest |
| `timeline` | timing、候选、素材 | SelectionRevision 与确定性 EDL |
| `render` | 音频、已选素材、EDL | 可解码视频 |
| `quality` | 视频与生成证据 | 技术、语义与安全 QC 报告 |

## 3. 不可破坏的不变量

1. Provider 输出必须先下载为不可变 Run Artifact，验证哈希、媒体结构和安全状态后才能进入时间线。
2. “已提交 Provider job”不是 Step 成功；只有结果、费用审计和 generated-only CandidateManifest 全部持久化后才成功。
3. Pipeline 不包含 `media.retrieve`，也不接收 `asset_library_ids`。
4. Prompt 是来源证据，不是输出满足视觉约束的证明；硬约束必须从实际生成媒体重新分析。
5. CandidateManifest、SelectionRevision 与 EDL 是三个独立不可变 Artifact；渲染器只消费冻结的 EDL。
6. 覆盖不足、内容不安全、来源不可验证、Provider 拒绝或预算耗尽必须进入失败/审核，不允许占位成功。
7. 每个生成场景记录 Provider、模型版本、Prompt 哈希、Seed/参考哈希、任务 ID、条款快照、费用和安全决策。
8. AI 内容披露进入 Run 快照并传播到交付元数据。
9. Full-AI 写作不执行网页研究，也不继承标准素材管线的库存/版权检索语义。

## 4. Provider 事务模型

Pipeline 只声明领域操作 `media.generate`，供应商名称不进入执行图。Adapter 必须实现：

```text
plan -> reserve spend -> submit_once -> reconcile
     -> download_once -> validate -> publish Run scope
```

关键可靠性要求：

- 以 workspace、Run、Beat、variant 和 generation round 建立持久 ledger；
- 同一业务键的 request hash 不同必须冲突；
- submit lease 过期后进入 `submit_unknown`，不能自动创建第二个付费任务；
- 有 Provider task ID 时只轮询原任务；没有 task ID 且供应商不支持 client-key 查询时进入人工对账；
- 提交前执行费用、并发、速率、总生成秒数和 variant 上限；
- 下载采用流式读取、内容哈希和原子对象发布；
- 模型、价格、条款、输出权利声明与验证结果保持不可变快照。

Provider 模型 ID、价格、配额和政策是部署配置，不能硬编码在 Web 或 PipelineVersion。

## 5. Control API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/v1/full-ai/options` | readiness、模型、时长、比例、政策与价格版本 |
| POST | `/v1/full-ai/estimate` | Beat/场景计划、计费秒数、上限与过期报价 |
| POST | `/v1/full-ai/runs` | 以 `Idempotency-Key` 创建 Run |
| GET | `/v1/full-ai/runs/{id}` | 读取状态并对账未知提交 |

提交按钮只有在 capability 已知、生成 Provider 可用、报价仍有效且不超过用户确认上限、权利/披露/安全要求通过、没有 capability gap 时才启用。

Create replay 先按 workspace、`Idempotency-Key` 和精确请求哈希恢复原 Run，再检查当前报价。这样即使客户端丢失 `201`，也不会因报价过期或 Provider 配置变化重复付费。

## 6. 生产启用条件

真实提交至少要求：

- generated writing 与独立配置的 Full-AI vision verifier；
- 受支持的视频生成 Provider、HTTPS endpoint 与 Secret file；
- PostgreSQL、对象存储、FFmpeg/FFprobe、TTS 和完整 Worker capability；
- 内容寻址的条款/定价快照、计费单位和操作方输出权利声明；
- 监控、费用上限、限速、取消/对账流程和投诉处理机制。

本地 Secret 只能放在被忽略的 `var/secrets/worker-provider.env`。Provider API Key 只进入 Worker；Control API 只接收非敏感 readiness、价格、条款和权利字段。具体变量见 `services/worker/.env.example` 与 `apps/api/.env.example`。

## 7. 验收层级

| 层级 | 当前判断 |
| --- | --- |
| 隔离 UI/API/Pipeline、Ledger 与 fail-closed | 已实现 |
| Fake Provider + 真实 MP4 解码/视觉验证的无费用 E2E | 已实现并由测试覆盖 |
| 真实供应商小额 Canary | 尚未由仓库默认环境证明 |
| Golden Set、自动局部修复与质量标定 | 后续工作 |
| 多 Provider 路由、连续性参考与规模化预算 | 后续工作 |

首个真实 Run 必须证明：没有共享或远程下载素材；所有 timing 来自已验收旁白；每个选中片段可追溯到唯一付费任务和本地哈希；Beat 覆盖完整；无隐式循环、定格或 padding；最终音视频可解码且没有阻断性语义/安全问题；实际费用不超过确认上限。
