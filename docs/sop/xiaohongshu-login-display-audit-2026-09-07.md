# 小红书登录后信息显示与刷新修复审计 · 2026-09-07

结论：本机登录 → 主页报告 → 真实详情链路已恢复并实测通过。保留原浏览器档案，重启后平台仍核验为 authorized。项目整体不作生产就绪声明；当前深析报告仍有模型能力缺失，另有一处历史存储提示文案不一致。

## 本次已修复

- **P1：内置 Provider 缺少采集协议字段。** 状态响应缺少 `cdp_host`，而主页与详情采集器要求它严格等于 `127.0.0.1`；已登录也会被采集接口拒绝。补齐字段，保留回环校验。[Provider](../../apps/api/src/framefactory_api/xhs_browser.py#L92)、[主页检查](../../apps/api/src/framefactory_api/benchmark_accounts.py#L637)、[详情检查](../../apps/api/src/framefactory_api/benchmark_note_sources.py#L373)。
- **P2：状态轮询不断开关页面。** 原实现每次核验都新建、导航、关闭标签页。改为浏览器会话的后台 HTTP 请求，从新平台响应中的当前访问者字段判定登录；禁止跟随重定向，不信任旧页面、作者资料或 Provider 的登录声明。[后台核验](../../apps/api/src/framefactory_api/benchmark_auth.py#L175)。真实响应含 `new Map([])`，仅将这一已知空容器字面量安全转换，不执行任何平台 JavaScript。[解析](../../apps/api/src/framefactory_api/benchmark_accounts.py#L1102)。
- **P2：手动检查成功不恢复待办采集。** 轮询过期后，显式检查可恢复已请求的免费采集；页面被动检查仍不自动发起采集。新的采集开始时清除过期的等待提示。[控制器](../../apps/web/lib/benchmark-connection.ts#L53)、[按钮](../../apps/web/components/benchmark-connection.tsx#L67)。
- **P2：普通 HTTP 与浏览器共用过短预算。** 原默认 8 秒同时覆盖浏览器启动连接、导航、字段准备，缺少余量。浏览器采集单独为 25 秒，隔离进程另有 5 秒回收余量；普通 HTTP 仍为 8 秒。这是已确认的配置问题，不能将所有早期 502 都断言为同一种超时。[预算](../../apps/api/src/framefactory_api/benchmark_accounts.py#L164)。
- 有界面二维码页在临时数据到期后继续保留，最多一个交互页；只清除二维码结果，避免打断扫码后的操作。[生命周期](../../apps/api/src/framefactory_api/xhs_browser.py#L199)。

## 真实路径证据

1. 使用原本机档案恢复浏览器，平台新响应 `guest=false` 且具有有效当前访问者标识；未输出、导出或保存登录资料。
2. 正式启动器重启 API、Web、Worker 与浏览器，运行时兼容检查返回 `compatible=true`、`connection_state=authorized`、`live_login_verified=true`。未让用户重新扫码。
3. 运行中的 API 返回 HTTP 200，白昼小熊主页 32 篇笔记，32 篇已核验，0 篇未解决，`authenticated_managed_browser`，实测一次约 6 秒。
4. 用户实际 Chrome 的 `/benchmarks` 页面显示已连接、32 篇笔记全部核验。点击“获取真实详情证据”后，当前笔记正文、赞藏评、视频时长及分辨率均显示。没有自动启动付费重分析。
5. 辅助自动化对真实本机 Web 执行首次读取和显式采集，两次 HTTP 200、32/32 核验；详情 HTTP 200、媒体类型 video，页面 JavaScript 错误数 0。观察期间原受管标签页未关闭或导航，前后数量均为 1。最初脚本把“开始采集”当作必然初始按钮，遇到 checking 而超时；改为实际连接入口后通过，没有把早期超时算成通过。
6. 辅助结果位于 `var/runtime/xhs-login-ui-result.json`，截图 `var/runtime/xhs-login-fixed-ui.png`。脚本只记录状态、计数和媒体类型，不记录凭据、二维码或签名链接。

## 检查结果

| 命令或检查 | 结果 |
| --- | --- |
| `pytest apps/api/tests/test_benchmark_auth.py apps/api/tests/test_xhs_browser.py apps/api/tests/test_benchmark_accounts.py apps/api/tests/test_benchmark_note_source_concurrency.py apps/api/tests/test_browser_process.py -q` | **passed：98**，最终源代码上执行。包含真实 Chromium 持久档案、Windows 有界面二维码到期保留、重建、进程树回收、身份匹配、序列化、并发/取消等。初次进程夹具仍模拟旧 new_page 路径而失败，改为模拟实际 HTTP 请求/响应回收后全部通过。 |
| `pytest tests/contract tools/tests -q` | **passed：112**。曾与 API 目录混跑导致同名 conftest 收集冲突，分开执行后通过。 |
| `ruff check apps/api/src apps/api/tests` | **passed**。 |
| `npm run lint`、`npm run typecheck` | **passed**，Web 最后编辑后复查。 |
| `npm test` | **passed**：完整构建及 91 项测试，最终代码上重跑。 |
| `start.cmd -Xiaohongshu -NoInstall -NoBrowser -Restart` | **passed**，正式启动与平台实际会话核验成功，保留数据卷和浏览器档案。 |
| 真实 Web 采集及详情读取 | **passed**，证据见上节；不以离线夹具冒充平台验证。 |
| `git diff --check`、工作区 SHA-256 快照 | **passed**；快照 580 个现存文件，审计报告为之后新增记录。保留原有及并行用户改动。 |
| 全量 API/Worker 持久化集成、生产备份恢复、容量/限流压测、远端 CI、完整付费模型链路 | **skipped**，本轮仅对受影响链路及契约执行上述检查，不沿用旧轮次结果充当本轮通过。 |

## 收尾审计的剩余发现

- **P2 · 已确认报告状态，运行配置未单独验收：** 用户正在查看的深析报告标记视觉内容理解与叙事策略不可用，提示模型地址、密钥或模型未配置。因此登录与采集恢复不等于完整深析可用。应核对运行该任务的 `FRAMEFACTORY_ASSET_VISION_*` 配置，再由用户显式重分析验收。[视觉能力检查](../../services/worker/framefactory/worker/benchmark_analysis.py#L263)、[策略能力检查](../../services/worker/framefactory/worker/benchmark_analysis.py#L341)。本轮没有为此填入密钥或发起模型调用。
- **P3 · 已确认文案漂移：** 快照限制仍写“数据未写入数据库”，而报告接口可自动保存历史，用户页面也显示保存成功。应将采集缓存与报告持久化分开说明，避免误导保存/隐私预期。[旧提示](../../apps/api/src/framefactory_api/benchmark_accounts.py#L941)、[历史保存](../../apps/api/src/framefactory_api/main.py#L1016)。这是收尾审计发现，未扩展本次登录修复范围。

## 各领域审阅边界

- **功能与架构：** 对照 Web 连接/恢复/账号报告、API 登录与采集网关、原生 Provider、主页/详情 CDP 协议及实际历史报告。空结果、字段不全、平台限制与断开仍有诚实状态，未改 Worker、数据库、队列、对象存储协议或种子数据。
- **权限与隐私：** 审阅本机 peer/Host/Origin/default-owner 限制、规范平台 URL、无跨源重定向、当前访问者判定、输入/响应限制；相关回归通过。没有执行平台代码解析，不发送身份凭据到项目 Web 或日志，未绕过平台验证。现有公开来源与限制说明保留；未进行第三方许可证或全依赖漏洞库扫描。
- **完整性与恢复：** 复核串行锁、有限排队、短期缓存、请求幂等、取消后的子进程回收、关闭后的档案复用与二维码到期；未改变数据库迁移、事务、备份或不可逆操作。
- **资源与成本：** 登录后台请求 10 秒、登录子进程 35 秒、浏览器主页采集 25 秒、三分钟前端轮询上限；保留至多一个交互页，响应在解析前校验 2 MB 上限（HTTP 客户端会先缓冲，不能声称传输流式硬限额）。未做平台压力测试；没有新增依赖、付费请求或无限后台任务。
- **发布与运维：** 正式启动使用项目锁定依赖与源代码指纹，当前服务已加载变更。最终 API 日志为正常启动记录，Web/浏览器 stderr 无新错误；这不是长时间稳定性或告警验收。故障应根据真实状态区分平台登录、采集协议、加载超时及模型能力缺失。
