# Architecture 解说视频冒烟测试记录

> 文档分类：历史参考（一次性测试记录）

> **历史快照边界**：这是旧 v3 环境的一次运行复盘。文中的 Run 已留在旧数据库，失败原因和后续修复只代表当时快照，不能用来判断新 Vistora 的当前 Provider、素材库或发布状态。现行入口见 [`../../README.md`](../../README.md)。

测试时间：2026-08-18（Asia/Shanghai）

测试环境：本地 v3，Web `http://localhost:4173`，Control API `http://127.0.0.1:8200`

测试目标：验证从创作主题到解说视频产物的完整链路，并记录阻塞点。

## 测试输入

### 180 秒版本

- Run ID：`216777be-bca0-497e-93f3-f2c1af65119b`
- 主题：用三分钟解释软件架构、分层、模块化、微服务和事件驱动的取舍，受众为非技术产品经理。
- 默认组合：公司商业案例拆解 `1.0.0`、标准 Pipeline `1.0.0`、16:9、沉浸全画面。

### 30 秒降级版本

- Run ID：`2146b1b1-72d0-4e55-b2a7-4883c1c8783a`
- 主题：用城市道路规划类比解释模块边界、扩展能力和技术取舍。
- 目的：排除 180 秒目标时长导致失败的可能。

## 实际结果

两个新建运行均成功写入数据库并进入 Worker，但都在第 1 步“研究与事实核验”永久失败，进度为 `0 / 6`：

```text
step_permanent_error · capability 'research.collect' has no configured production provider;
the step was not executed and no artifact was created
```

脚本、配音、素材、渲染和质量检查随后全部被取消，没有生成任何视频产物。30 秒版本与 180 秒版本结果一致，因此本次主阻塞与目标时长无关。

## 卡点

### P0：生产文本 Provider 未配置

Worker 需要通过以下环境变量配置 OpenAI-compatible `/chat/completions` Provider：

- `FRAMEFACTORY_OPENAI_BASE_URL`
- `FRAMEFACTORY_OPENAI_API_KEY` 或 `FRAMEFACTORY_OPENAI_API_KEY_FILE`
- `FRAMEFACTORY_OPENAI_RESEARCH_MODEL`
- `FRAMEFACTORY_OPENAI_WRITING_MODEL`
- `FRAMEFACTORY_OPENAI_QUALITY_MODEL`

本地启动流程没有提供这些值，因此 `research.collect` 被绑定到 fail-closed 的 `UnsupportedCapability`。这是符合“不得伪造成功或占位产物”的服务端设计，但当前环境无法完成视频生产。

### P0：运行前检查误报“能力检查通过”

创作页在 Provider 未配置时仍显示“可以创建”和“能力检查通过”。项目提交后才在 Worker 第一步永久失败。预检没有覆盖实际生产能力注册状态，导致用户在已知必败的情况下仍能创建运行。

### P1：创建后没有进入本次运行详情

创建成功后页面进入项目列表或只提供指向 `/projects` 的“查看项目”入口，没有直接进入 `/projects/{run_id}`。用户需要在全部项目中再次寻找刚创建的运行。

### P1：项目列表按旧到新排列

测试时共有 60 个项目；两个新运行分别位于第 59、60 位。新建项目不在首屏，且页面没有搜索框或“最新优先”排序控制，显著增加追踪成本。

### P1：失败详情缺少修复路径

永久失败详情只提供“立即刷新”和“查看详情”，没有：

- 跳转到 Provider 配置或配置文档的入口；
- 修复配置后的“从失败步骤重试”；
- 保留原参数并复制为新运行的快捷动作。

### P2：项目标题直接使用完整主题

新项目标题直接采用完整输入文本，180 秒版本的标题是一整段长提示词。列表识别和详情页层级都不够清晰，建议生成短标题，同时保留原始主题作为独立字段。

## 补充旁证

环境中已有较早的 `Architecture解说` 运行（Run ID `01009b7c-ac97-4dc6-867b-307879ed43b5`）曾完成研究步骤，但脚本步骤因“configured model provider could not satisfy the requested narration duration”永久失败。这不是本次新建运行，但说明 Provider 配齐后还需要对目标时长做能力预检，并在创建前提示可支持范围。

## 建议验收标准

1. Provider 未配置时，创作页明确阻止提交，并展示可执行的配置指引。
2. Provider 已配置时，预检实际探测 `research.collect`、`writing.compose`、`audio.synthesize`、`media.select` 与 `render.compose` 的可用性。
3. 目标时长超出模型能力时，在创建前给出支持范围和可修改建议。
4. 创建成功后直接进入本次 `/projects/{run_id}` 详情。
5. 项目列表默认最新优先，并支持搜索主题或 Run ID。
6. Provider 修复后可从失败步骤安全重试，不必重新创建整个项目。
7. 30 秒 Architecture 用例最终产生研究、脚本、配音、素材清单、视频和质量检查记录。

## 2026-08-19 修复后复测

### 已修复

- 新增由 Control API 执行的 `/v1/runs/estimate` 能力预检；Provider 能力未知或缺失时，创作页阻止提交，创建接口也返回 `RUN_CAPABILITY_UNAVAILABLE`。
- 新版启动器只从被 Git 忽略的 `v3/var/secrets/worker-provider.env` 读取 Provider；一次性迁移工具负责从旧配置复制允许的字段，新版运行时不依赖旧目录。
- OpenAI-compatible Provider 不支持 `json_schema` 时，Worker 会回退到 `json_object`，结果仍必须通过本地 Schema 校验。
- 创建成功后直接进入 `/projects/{run_id}`，项目列表默认最新优先。
- 失败步骤提供“重试此步骤”；重试上游失败时，会重新打开因依赖失败而取消的下游步骤。

### 复测运行

- Run ID：`ff0e994e-ffea-478f-ba0c-48bd68a5d898`
- 预检：能力清单已知，6 个标准步骤无缺口。
- 研究：真实 Provider 完成并经人工审核通过。
- 写作：真实 Provider 完成。
- 配音：真实本地 TTS 已完成。
- 素材：停在人工审核，`blocking_reason=asset_coverage`。

### 当前剩余卡点：Architecture 场景素材覆盖不足

素材库没有匹配以下 4 个脚本场景的已授权、已审核素材：城市航拍路网、道路扩建、高架与老街取舍对比、新路段无缝接入。自动素材获取在本次不可变运行快照中关闭，因此 `media.select` 正确地没有伪造素材清单，也没有强行进入渲染。

下一步需要先向所选素材库导入或生成上述场景的可用画面，完成版权状态、安全分析和标签，再在素材审核节点选择“退回本步修改”。完成素材覆盖后，才具备验收渲染与质量检查的条件。
