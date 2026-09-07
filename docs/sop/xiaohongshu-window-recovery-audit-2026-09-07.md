# 小红书窗口恢复审计 · 2026-09-07

结论：**窗口恢复与等待逻辑修复通过，真实连接已进入 `awaiting_scan`；本人扫码与平台放行尚未验收。** 不将“页面打开”视为已登录或完成采集。

## 已确认的问题与修复

1. 排查开始时，5556 服务仍存活，但 `/browser/managed/status` 返回 `unavailable`、`cdp_port=null`，桌面没有 Google Chrome for Testing 窗口。不能断言浏览器是被用户关闭还是崩溃。现在只在显式登录 POST 时串行恢复断开的浏览器，保留原登录档案；GET 不自动重开。[实现](../../apps/api/src/framefactory_api/xhs_browser.py#L60)。
2. 旧实现只调用 `bring_to_front`，没有恢复窗口位置/最小化状态；人工验证任务 180 秒过期会关闭验证页。现在通过 Chromium 原生窗口协议恢复普通窗口及可见位置，验证任务过期仅清除临时结果，最多保留一个人工验证页供用户继续操作，重试复用。[实现](../../apps/api/src/framefactory_api/xhs_browser.py#L112)、[窗口恢复](../../apps/api/src/framefactory_api/xhs_browser.py#L181)。窗口与标签页的区分参考 [Playwright 文档](https://playwright.dev/python/docs/api/class-page#page-bring-to-front)及 [Chrome 窗口协议](https://chromedevtools.github.io/devtools-protocol/tot/Browser/#method-setWindowBounds)。
3. 初始化登录状态不明确时，旧代码仅返回 `checking`，显式连接也不会进入内置登录操作。现在允许内置 Provider 继续打开验证入口；不将不明确状态认定为已登录。[实现](../../apps/api/src/framefactory_api/benchmark_auth.py#L453)。
4. 登录检查现在核对人工验证窗口是否仍存在；关闭窗口或断开后撤下旧提示。恢复覆盖协议异常及隔离浏览器进程异常，保留既有传输失败幂等重试语义。[实现](../../apps/api/src/framefactory_api/benchmark_auth.py#L444)。前端明确说明独立 Chromium 窗口、三分钟轮询上限与重新连接方式；SOP 已同步。

## 检查与实际工作区审计

| 检查 | 结果 |
| --- | --- |
| `pytest apps/api/tests/test_xhs_browser.py apps/api/tests/test_benchmark_auth.py apps/api/tests/test_browser_process.py tests/contract tools/tests -q` | **passed：176**。包含真实 Chromium 断开重建、并发恢复、最小化恢复、任务到期后保留人工验证页、关闭页撤下提示、非确定登录状态、幂等重试及启动/进程回收测试。离线页面夹具不算真实扫码证据。 |
| `ruff check apps/api/src apps/api/tests` | **passed**。 |
| `npm run lint`、`npm run typecheck`、`npm test` | **passed**：Web 完整构建及 90 项测试。 |
| `start.cmd -Xiaohongshu -NoInstall -NoBrowser -Restart` | **passed**：保留登录档案及数据卷，当前四个受管进程身份正确，API `/readyz` 返回 PostgreSQL 持久化正常。 |
| 真实 API `POST /v1/benchmark-auth/xiaohongshu/qrcode` | **passed：awaiting_scan**，返回真实二维码有效期；未输出或保存二维码、Cookie、令牌。 |
| Windows 原生窗口枚举 | **passed**：出现“小红书 - 你的生活兴趣社区 - Google Chrome for Testing”独立窗口。 |
| 原生窗口截图/强制前台激活 | **blocked**：Computer Use 对 Google Chrome for Testing 的应用标识解析异常；没有把窗口枚举或 CDP 状态说成截图验收，也没有声称已经强制置顶。可用 Alt+Tab 切换。 |
| `git diff --check`、实际工作区哈希核对 | **passed**：最终快照 579 个文件，本审计为之后新增记录。保留原有及其他任务的修改。 |

## 残余发现

- **P2 · 未验收：** 本人扫码、平台验证码/网络限制放行、登录后详情及付费分析未执行；窗口出现不能证明这些功能成功。[验证边界](../../apps/api/src/framefactory_api/xhs_browser.py#L169)。
- **P3 · 工具限制：** 本次未取得原生窗口截图或证实操作系统前台焦点；CDP 窗口恢复有真实 Windows 回归，实际窗口存在有原生枚举证据。

## 分领域检查边界

- **功能/架构：** 审阅 Web 连接状态、API 轮询/过期/幂等路径、内置 Provider 生命周期及 CDP 连接。保持公共状态枚举与现有契约；真实 UI 文案不再承诺三分钟后关闭人工验证窗口。
- **认证/权限/隐私：** 复核回环 peer/Host/Origin、1 KiB JSON 输入、HTTPS 平台来源、headless 拒绝人工交互、Provider 类型限制。明确用户点击才重开，不触碰其他浏览器，不绕过平台验证，不导出登录档案。相关安全回归通过。
- **数据/并发/恢复：** 未改数据库迁移、Worker、Redis、对象存储协议；恢复由异步锁串行执行，复用原档案，回收旧驱动及 janitor。临时任务结果仍到期清除，人工页保留上限一个。核对原有取消、进程回收与不确定传输重试测试。
- **性能/成本：** 启动 30 秒、窗口恢复 5 秒、连接轮询最多 180 秒仍有界；不无限重开窗口或累积验证页，没有调用付费模型。没有执行容量与平台限流压力测试。
- **可复现性/部署/运维：** 本轮未增加依赖；使用锁定的 Playwright/Chromium 与现有项目启动器。Worker 全量、数据库/S3 全量、Python 漏洞库扫描、远端 CI、生产恢复/负载/告警演练本轮 **skipped**；参照前一轮本机启动审计的已运行证据，仅将本轮上述检查列为此次通过项，不声称生产就绪。
