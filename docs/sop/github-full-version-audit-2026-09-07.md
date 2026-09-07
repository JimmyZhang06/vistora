# GitHub 完整版本整合审计 · 2026-09-07

结论：本次完整源码具备推送条件，本机验证通过；不等同于生产部署验收。推送目标为 `JimmyZhang06/vistora` 的 `main`，采用普通快进推送，不覆盖远端历史。

## 完整性与合并核对

- 远端当前只有 `main`，GitHub 开放 PR 查询为空。`codex/benchmark-auth-integration` 的提交 `bf978bd` 已是 `main` 的祖先，无需重复合并。
- 另一工作目录停留于 `07ffa6f`，工作区干净，该提交也已包含在 `main`。
- `stash@{0}` 的批量生产组件与当前文件完全相同；当前 CSS 在备份基础上仅增加 275 行对标样式，没有遗漏备份内容。备份保留，不删除用户资料。
- 本次提交包括原有本地源码、界面、测试、启动脚本、开发工具和 SOP，再加本审计，共 42 个文件。不是单独上传小红书补丁或构建产物。
- `.env`、浏览器档案、数据库、运行时、密钥及生成媒体维持既有忽略边界。额外忽略 5 个根目录临时探针/PID/日志文件；文件保留本机。正式测试位于既有测试目录。独立 mock 工具明确标注测试用途，正式启动默认使用原生浏览器。

## 最终工作区检查

| 检查 | 结果 |
| --- | --- |
| `python -m pytest apps/api/tests -ra` | **passed：497**，首轮 3 个 PostgreSQL 测试因环境变量未设置跳过；MinIO 真实签名上传/下载测试已执行。 |
| `python -m pytest apps/api/tests/test_postgres_channel_e2e.py apps/api/tests/test_postgres_webpage_artifact_e2e.py -ra` | **passed：3**，在新建的独立测试数据库设置正确的测试变量及 mutation opt-in 后补跑。API 合计 500 项均已通过；未对业务数据库执行测试清空。 |
| `python -m pytest services/worker/tests -ra` | **passed：517；skipped：1**，跳过需要显式启用的 live Edge/FFmpeg 测试；不是全量外部供应商验收。 |
| `python -m pytest tests/contract tools/tests -q` | **passed：112**。 |
| `python -m ruff check apps/api/src apps/api/tests services/worker` | **passed**。 |
| `npm run lint`、`npm run typecheck`、`npm test` | **passed**，Web 完整构建与 91 项测试。 |
| `python -m framefactory.worker.temporal_benchmark services/worker/benchmarks/temporal_cut_v1.json --minimum-f1 0.90 --maximum-unsafe-rate 0` | **passed**，F1=1.0，unsafe cut rate=0。 |
| `docker compose -f deploy/docker-compose.persistence.yml config --quiet` | **passed**。 |
| `python tools/verify_benchmark_runtime.py --api-url http://127.0.0.1:8200 --web-origin http://127.0.0.1:4173` | **passed**，当前 API/Web 兼容、历史接口通过，真实登录仍为 authorized。该只读检查没有重新扫码或调用付费模型。 |
| `python tools/release/security_gate.py --static-only --report var/runtime/github-security.json` | 密钥与生产默认配置检查 **passed**，569 个可读源码文件未匹配已知密钥模式；Python/npm 在线漏洞审计 **skipped**，因此工具诚实返回 `release_ready=false`。未把静态检查宣称为完整供应链验收。 |
| `git diff --cached --check`、源码 SHA-256 快照比对 | **passed**，验证期间 576 个已暂存/跟踪文件未发生变化；本报告为验证后新增。提交前再次核对工作区和暂存区。 |
| 远端 CI、完整生产恢复/备份/容量/告警演练 | 提交前 **pending / skipped**，远端 CI 必须以推送后该提交的结果为准。 |

## 按严重性排列的剩余发现

- **P2 · 部署能力限制：** 没有配置视觉/策略模型时，视频深析只能部分完成；源码完整不会自动提供第三方密钥。上一轮真实报告已显示这一状态，本轮未发起付费重分析。[视觉检查](../../services/worker/framefactory/worker/benchmark_analysis.py#L263)、[策略检查](../../services/worker/framefactory/worker/benchmark_analysis.py#L341)。
- **P2 · 发布验证缺口：** 在线依赖漏洞审计、live Edge/FFmpeg、生产恢复/容量演练尚未执行；Windows 启动回归在本机执行，当前常规 CI 为 Linux。跨机器生产发布前仍需这些门禁。[CI 范围](../../.github/workflows/release-gates.yml#L38)、[安全门禁](../../tools/release/security_gate.py#L453)。
- **P3 · 既有文案漂移：** 快照限制声称数据未写数据库，但报告接口可保存历史，应修正文案以区分缓存与报告持久化。本次按用户要求整合推送，不扩展功能变更。[提示](../../apps/api/src/framefactory_api/benchmark_accounts.py#L941)、[历史保存](../../apps/api/src/framefactory_api/main.py#L1016)。

## 分领域审阅

- **用户功能/端到端：** 检查新增原生浏览器、登录恢复、主页/详情、界面加载与失败状态、批量生产请求竞态，以及正式 Windows 启动入口。此前真实主页与详情证据见同日登录审计；本轮重新验证运行时兼容和实际登录，不把单元夹具算成真实平台采集。
- **架构/契约：** Web/API/Worker 与公共 Schema、队列、存储和种子的现有边界保持不变；对照契约测试和 API/PostgreSQL/MinIO 集成结果。所有开发分支及备份中的已完成代码均已包含。
- **安全/来源：** 复核回环 Host/Origin、当前访问者判定、URL 与响应边界、无跨源重定向、登录凭据不返回 Web；暂存源码密钥扫描通过。保留现有许可证及官方运行时校验，不提交安装包、账号档案或生成媒体。扫描覆盖有大小与文本格式限制，不承诺不存在任何未知秘密或供应链风险。
- **数据/并发/恢复：** 审阅源码中有限排队、取消后的进程树回收、二维码幂等、档案复用、请求响应序号和启动 PID 身份检查；测试独立数据库/桶与业务数据隔离。未改迁移或执行用户数据删除，也未删 stash 或内部快照。
- **性能/成本：** 状态检查不再导航页签；浏览器采集 25 秒、登录轮询 3 分钟有界，历史读取不自动触发付费任务。未执行真实供应商成本/限流压测。
- **可复现性/发布/运维：** 现有锁文件与 CI 保留，构建、Ruff、静态安全及本机真实基础服务检查通过；临时文件忽略规则明确。源码推送与生产部署分别判断，远端采用普通 push，拒绝用 force 覆盖并行改动。
