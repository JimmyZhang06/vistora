# 对标功能整合与小红书连接审计 · 2026-09-06

结论：代码已整合为可审查的单一分支，本地代码检查通过；提交供合并审查，不能作为生产发布验收。真实用户扫码闭环、运行中旧 API 升级和生产基础设施验收尚未完成。

## 本次结果

- 将此前独立工作区的跨账号采集增量整合到当前代码，保留报告历史、持久视频任务、媒体证据与已有并行修改。没有清理、重置或覆盖不相关文件。
- Web → 独立 benchmark API → 本机浏览器 Provider 的连接流程已接通。初始只检查登录，点击才生成二维码；已有登录不重置。新临时平台页验证真实登录，不能仅依赖 Cookie 存在或 Provider 缓存标记。
- 显式二维码操作复用内部 request ID；较慢任务由原 POST 的后台操作继续等待并消费结果。HTTP 约 12 秒先返回 checking；QR 重试预算 45 秒，最后一次 Provider I/O 另有 8 秒期限。GET 不生成二维码。取消调用者不会提前释放浏览器操作锁。
- Web 对轮询期限、二维码过期、失败重试、隐藏暂停、账号切换和迟到响应做了约束。连接成功只恢复当前免费主页/身份/详情操作；视频创建与重试仍需用户操作。
- 修复详情采集重复取消后提前释放锁的问题，等待底层线程退出才放行；最多 32 个待处理请求，排队期限 30 秒。
- ASR 策略报告明确标记未分类语音候选，不能把唱词或背景人声直接当作口播结构。历史读取继续独立于当前平台登录和模型调用。
- 修复 6 处既有 Web TypeScript 错误，并将 `npm run typecheck` 加入 PR 的 Release gates。

## 验证结果

| 检查 | 结果 | 范围与限制 |
| --- | --- | --- |
| `python -m pytest apps/api/tests -q -r s` | 通过：462；跳过：4 | 466 项收集；跳过 3 项真实 PostgreSQL 测试和 1 项 MinIO 测试，未设置专用测试连接。一个既有 Starlette TestClient 弃用警告 |
| `python -m pytest services/worker/tests -q -r s` | 通过：514 与 116 个子测试；跳过：1 | 未启用旧版 Edge/FFmpeg 实机适配测试 |
| `python -m pytest tests/contract tools/tests -q -r s` | 通过：85 | 包含新 Schema、示例、API/Worker/公共契约和发布工具边界 |
| `python -m ruff check apps/api services/worker` | 通过 | 完整 API、Worker 静态检查 |
| Web `npm run typecheck`、`npm run lint`、`npm test` | 通过：84 项测试及生产构建 | 包含授权传输/状态机、恢复/历史、真实 HTTP 适配器及 SSR 路由；不能替代交互式真实扫码 |
| `python -m framefactory.worker.temporal_benchmark … --minimum-f1 0.90 --maximum-unsafe-rate 0` | 通过 | 3 个既有基线样本：F1=1，unsafe_cut_rate=0；不泛化为所有视频质量 |
| `python -m pip check` | 通过 | 当前安装环境依赖一致；不代表干净机器安装和依赖漏洞扫描 |
| `docker compose -f deploy/docker-compose.persistence.yml config --quiet` | 通过 | 配置解析，不是数据库/队列/对象存储运行验收 |
| PowerShell Parser 对 `start.ps1` 的 AST 解析 | 通过 | 无语法错误，未重启服务 |
| `git diff --check` | 通过 | 未发现差异空白错误 |
| `npm audit --json` | 失败：4 个 moderate | 既有 drizzle-kit → esbuild-kit → esbuild 开发依赖链；未强制降级迁移工具 |
| `npm audit --omit=dev --audit-level=moderate` | 通过 | 当前生产依赖查询返回 0 项已知漏洞，不是全面安全保证 |
| 实际 API TestClient → Provider 5556 → 新平台页登录核验 | 通过 | HTTP 200、authorized、QR=null、private/no-store，并符合 canonical auth Schema；未调用真实二维码接口、未重置已有会话 |
| 真实退出登录 → 用户扫码 → 当前免费操作恢复 | 未验证 | 需要用户使用独立测试会话扫码；fixture 和既有登录状态不能替代 |
| 运行中 8210 API 新接口 | 失败：auth status 与 history 均 404 | 该进程仍是旧版本，本轮未进行运行切换；源码 TestClient 成功不能替代该进程升级 |
| 生产部署、真实生产恢复/负载/对象存储门禁 | 未执行 | 本机研究域不等于 PostgreSQL/Redis/生产对象存储集成；须在受保护验证环境执行发布流程 |

上述 Python 命令使用仓库 `.venv/Scripts/python.exe`，Web 命令在 `apps/web` 运行。API 最终验证期间，对 552 个已跟踪及非忽略待提交文件的 SHA256 检查未发生变化。Web 和 Worker 检查后的相关源码未再修改；最终审计文档是随后补充的结果记录。

此前三个真实主页与六条详情的脱敏验证仍见 [跨账号记录](cross-account-resume-real-validation.json)；实际旧 SQLite 副本迁移四份报告、产物读取及二次启动不重复归档见 [上次续接审计](cross-account-resume-audit.md)。这些是此前检查的证据，本轮未重新触发付费分析，也不把这些旧结果描述成新扫码闭环。

## 待处理发现

| 级别 | 触发、影响及证据 | 后续动作 |
| --- | --- | --- |
| P1，确认的验收缺口 | 用户首次扫码和登录过期后的完整交互尚未经真实扫码验证；运行中 8210 仍返回 404，新 Web 暂不能在该进程上连接或读历史。[连接验收要求](xiaohongshu-connection.md#验证要求)、[API 路由](../../apps/api/src/framefactory_api/main.py#L894) | 升级本机 API/Web 后，在独立测试会话由用户扫码并验证免费恢复及显式分析；完成前保持合并审查状态，不宣称正式可用 |
| P2，确认依赖告警 | 当前开发依赖树包含 vulnerable esbuild；在使用其开发服务并访问恶意页面等公告条件下有读取开发服务响应风险。[锁文件](../../apps/web/package-lock.json#L859) | 在隔离分支验证兼容升级或替换 drizzle-kit 依赖链；避免未经验证的 `npm audit fix --force` 降级 |
| P2，静态推断 | CDP `new_page`/`close` 在极端永久阻塞时仍可占用线程、操作锁和关闭流程，当前线程不能强杀。[登录采集](../../apps/api/src/framefactory_api/benchmark_auth.py#L172)、[详情锁](../../apps/api/src/framefactory_api/benchmark_note_sources.py#L126) | 公网或长期无人值守前使用可终止的隔离进程；现有串行限制仅防叠加，不能保证硬实时回收 |
| P2，静态推断 | 磁盘检查预留 256 MiB，但没有运行中的硬配额；源媒体与派生音轨/帧叠加可能超过预留或配置预算。[容量检查](../../apps/api/src/framefactory_api/benchmark_jobs.py#L785) | 增加执行中计量/硬配额和有界中止策略；在此之前监控专用研究目录容量 |

未确认新增 P0。未执行的真实基础设施门禁属于发布验收缺口，不能由本地单元测试替代。旧审计中的 TypeScript 失败已在本次解决，以本记录的最终检查为准。

## 审阅覆盖与未验证边界

- 用户流程：审阅连接面板、账号/笔记切换、身份恢复、视频显式操作、历史只读路径及错误/空态；运行时状态机和 HTTP 测试通过。未完成真实扫码和新页面交互布局验收。
- 架构与契约：审阅 API 路由、Web adapter、Worker 证据、JSON Schema/OpenAPI、SQLite 历史与本机任务。研究通道未接入业务 PostgreSQL、Redis 或对象存储；生产设置继续拒绝启用。
- 权限与输入：审阅默认用户/工作区、assets 权限、回环 peer/Host/Origin、禁止转发与代理、规范平台 URL、CDN/公网 DNS 固定、工作区 SQL/游标/产物路径过滤。接口失败和敏感响应投影有测试；不据此声称已有公网身份认证。
- 隐私与权利：待提交源文件扫描未发现真实凭据；命中项是测试 fixture。未提交运行数据、二维码、Cookie、模型密钥和媒体。Vistora 不落盘二维码的保证仅适用于自身；外部 Provider 超时未消费任务的留存仍由其管理。登录和公开内容研究不授予作品再利用许可。
- 完整性与恢复：审阅 WAL/FULL、事务、幂等键、按尝试归档、取消、线程串行、Windows 子进程回收、媒体保留、SQLite 迁移与备份说明。既有 Windows 崩溃回收测试通过；生产数据库备份恢复/并发租户模式未验收。
- 容量与成本：单执行通道、任务数量/尝试上限、有界浏览器扫描、媒体限量、最多 36 帧、模型调用限额和显式付费重试有实现及回归。底层 CDP 硬卡死、运行中存储配额和长期历史管理保留上述风险。
- 发布与运维：审阅 CI 类型/测试/构建步骤、锁文件、启动器、SOP、独立 Provider 契约及旧 API 升级限制。本轮 GitHub 交付是源码审查，不执行 Sites 发布；没有新增生产监控告警或替代受保护环境发布门禁。
