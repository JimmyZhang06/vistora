# 小红书连接与采集恢复

本流程用于单用户、本机对标研究。Web 中的「连接小红书」调用本机受管浏览器 Provider，用户在小红书 App 扫码确认。Vistora 不要求用户复制 Cookie，不保存登录令牌，也不自动创建付费视频任务。

## 连接类型

这是本机浏览器登录。小红书开放平台当前快速接入文档列出的 `basic_info` 仅用于昵称、头像等基础用户信息；申请应用、审核和官方 OAuth 不能直接替代任意公开博主主页/笔记的采集会话。不要将本连接描述为官方 API 授权、平台合作或作品再利用授权。依据：[小红书开放平台快速接入](https://openaccount.xiaohongshu.com/docs/quick-start)，2026-09-06 查阅。

## 安装与操作

1. 运行 `./start.ps1 -Xiaohongshu`。启动器安装 Playwright Chromium，启动项目自带的 `framefactory_api.xhs_browser`、API、Worker 和 Web；不需要安装外部采集插件或复制 Cookie。加 `-NoInstall` 时必须已安装依赖与 Chromium。完整栈仍需 Docker 中的 PostgreSQL、Redis 和 MinIO。
2. 如需视频深析，使用 `./start.ps1 -BenchmarkAnalysis`，它同时启用内置浏览器及可选分析依赖。两种模式都把 Web 研究接口指向本次 API，优先于旧 `.env.local`，不修改文件或 Provider 密钥。内置服务默认端口 5556，可用 `-XhsBrowserPort` 调整；端口占用时拒绝复用未知服务（包括 mock）。已有可信外部 Provider 的用户可显式加 `-ExternalXhsBrowser` 并配置 `FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL`。可选模型及预算见 [视频深析 SOP](benchmark-video-analysis.md)。
3. 打开 `/benchmarks`，输入主页并点击「连接小红书并采集」。项目先确认连接，已登录则直接采集，未登录则显示二维码或打开采集浏览器。成功后自动采集本次输入的主页，无需再次点击生成报告。页面被动检查和历史读取不会创建二维码；首次检查尚未结束时的显式点击会排队处理。
4. 使用小红书 App 扫码并确认。后端使用受管浏览器会话发起后台 HTTP 请求，从平台新响应中的当前访问者字段核验登录，不再新建、刷新或关闭标签页；不能仅凭 Provider 缓存或已有页面判定成功。二维码最多展示 120 秒；过期清除二维码但保留有界面登录页。Web 隐藏时暂停轮询，连续失败或达到轮询期限后停止；已在浏览器完成登录时，点击「检查登录状态」可继续之前等待的免费采集。
5. 登录成功后，仅恢复当前账号/笔记已请求的免费主页、身份补全或详情读取。切换账号/笔记后忽略旧响应。视频创建、取消后的重试以及重新分析继续要求用户点击。
6. 平台要求安全验证时返回 `error / BENCHMARK_AUTH_PLATFORM_RESTRICTED`，停止轮询。用户点击连接后，内置浏览器恢复独立 Chromium 窗口，返回 `checking / BENCHMARK_AUTH_MANUAL_VERIFICATION`，不把页面打开当作已登录。请在该窗口完成验证后返回项目；后台核验成功后自动恢复当前采集。自动检查最多 3 分钟，到期保留登录页；完成后可点击「检查登录状态」。窗口关闭或浏览器断开后，显式重新连接会重建或复用窗口，被动检查不重开。无界面浏览器和不支持该能力的外部服务不会宣称已打开验证页。平台提示 `300012 / IP 存在风险` 时仍需按平台提示处理网络，程序不绕过平台限制。

内置浏览器默认有界面，独立档案保存在被 Git 忽略的 `var/browser/xiaohongshu`，不会读取个人 Chrome/Edge 档案；登录凭据由 Chromium 存在此目录中。`stop.ps1` 只停止本次记录的进程树，保留档案。该目录仅供本机安装者使用，不可提交、同步给其他用户或放入普通报告备份。浏览器持久化与 CDP 接入采用 [Playwright BrowserType API](https://playwright.dev/python/docs/api/class-browsertype)。

只测试浏览器服务时可独立运行 `.\.venv\Scripts\python.exe -m framefactory_api.xhs_browser --port 5556`；此命令不提供 API、数据库或 Web。`--headless` 仅适合自动化检查，人工平台验证请用默认有界面模式。`start-benchmark-automation.ps1` 的默认路径也委托给正式启动器，不再自动回退到仿二维码 mock。

## 契约和边界

- `GET /v1/benchmark-auth/xiaohongshu/status`：非破坏检查，状态为 `not_configured`、`provider_unavailable`、`checking`、`login_required`、`awaiting_scan`、`authorized`、`expired` 或 `error`。
- `POST /v1/benchmark-auth/xiaohongshu/qrcode`：JSON `{}`，可带 `Idempotency-Key`。仅显式操作可产生二维码；正在处理的请求复用同一任务，已登录时不发起新登录。去重按本机 API 的活动操作执行，客户端键不提供跨重启持久重放保证。生成较慢时 HTTP 先返回 `checking`，原 POST 的后台操作在有限期限内沿用相同 Provider request ID 等待并消费结果；GET 不请求新二维码。超时后的显式重试继续原 ID。
- 响应遵守 `benchmark-auth-state.schema.json`，只返回经过校验的短期 PNG data URL，不返回 Provider task ID、Cookie、登录账号资料、任意图片 URL 或签名媒体地址。响应 `private, no-store`；Vistora 的二维码仅存在于内存和当前面板，不进入 Vistora SQLite、浏览器持久存储或历史归档。外部 Provider 的任务结果和浏览器目录由该服务管理，须按其保留/消费机制清理，不能将 Vistora 的不落盘保证扩展到外部服务。
- 接口限非生产配置、默认用户/工作区、`assets:write`、回环 peer 和 Host；有 Origin 时必须同时匹配配置和回环地址，拒绝转发头及跨站来源。Provider 请求禁代理、禁重定向、限回环地址和响应大小。
- 状态检查使用串行通道和短期缓存，不关闭用户已有页签。登录核验、二维码 Provider I/O、主页和详情采集进入独立 Python 子进程，超时或取消后回收自有客户端和驱动进程树。Windows 使用 Job Object，POSIX 使用独立进程组及父进程存活监测。登录核验期限 35 秒，Provider I/O 期限 10 秒，最终等待进程退出最多 5 秒；HTTP 仍可先返回 checking。已有受管浏览器不属于该进程树，不会被终止。CDP 完全无响应时无法保证已创建的临时页签被关闭，平台恢复后可手动关掉残留页签。进程边界不是 OS 安全沙箱，公网和多人服务仍需要正式身份和运行隔离。
- 本地二维码服务及浏览器会话属于安装者，不能扩展为租户共享登录。只运行一个 API 进程，不使用 `--workers`；扫码串行和去重是进程内保证。生产模式继续拒绝此通道。已保存报告不依赖当前登录态；历史读取不重新采集或执行模型。
- 内置 Provider 只监听回环地址，禁用转发头解释，拒绝浏览器 Origin、远端 peer、非本机 Host 和非 JSON 写请求；输入上限 1 KiB。CDP 端口由 Chromium 自动选择。一次只保留一个二维码任务，直接截取平台二维码元素，不自行绘制二维码、不保存截图文件。二维码及通常登录临时页最多保留 120 秒，人工验证的临时任务数据最多保留 180 秒，其他失败后 3 秒清理；人工验证页最多保留一个，重试复用，直到用户关闭或停止服务。任务身份的有限去重记录不含二维码。Chromium 自身的网页缓存和登录存储属于上述私有浏览器目录，不能宣称浏览器完全不落盘。

## 验证要求

API 回归覆盖权限/Origin、只读检查、已有登录保护、幂等、PNG 校验、过期、故障、串行及后台平台响应判断；Web 回归覆盖有限轮询、取消、迟到响应和免费恢复。接口的 fixture 测试不能证明真实扫码完成。内置 Provider 的状态必须包含 `cdp_host=127.0.0.1`、有效 `cdp_port` 及 `state=running`，主页和详情采集器均严格检查这些字段。

真实验收需要兼容 Provider 和更新后的 API/Web：退出登录的独立测试会话 → 用户扫码 → 当前免费采集恢复 → 显式视频分析 → 历史读取。不得为测试清空用户已有浏览器会话。对既有已登录会话只执行后台状态核验，不触发二维码请求。

升级后先运行只读兼容性检查：`python tools/verify_benchmark_runtime.py --api-url http://127.0.0.1:8210 --web-origin http://127.0.0.1:4173`。端口按实际配置调整。该工具核对运行进程的路由、授权/历史公共 Schema、CORS 和 no-store，不生成二维码或付费任务，也不打印报告内容、二维码或登录资料。`compatible=true` 只代表接口配套；`connection_state=provider_unavailable` 仍需启动外部 Provider，`scan_flow_verified` 始终为 false。完整启动器在 `-BenchmarkAnalysis` 模式下自动执行此检查；旧版本 404/405 会阻止误报启动完成，Web 同样提示更新并重启 API/Web。
