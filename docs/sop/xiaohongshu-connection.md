# 小红书连接与采集恢复

本流程用于单用户、本机对标研究。Web 中的「连接小红书」调用本机受管浏览器 Provider，用户在小红书 App 扫码确认。Vistora 不要求用户复制 Cookie，不保存登录令牌，也不自动创建付费视频任务。

## 连接类型

这是本机浏览器登录。小红书开放平台当前快速接入文档列出的 `basic_info` 仅用于昵称、头像等基础用户信息；申请应用、审核和官方 OAuth 不能直接替代任意公开博主主页/笔记的采集会话。不要将本连接描述为官方 API 授权、平台合作或作品再利用授权。依据：[小红书开放平台快速接入](https://openaccount.xiaohongshu.com/docs/quick-start)，2026-09-06 查阅。

## 安装与操作

1. 配置并运行可信的本机浏览器 Provider。它必须支持 `GET /browser/managed/status` 和 `POST /xhs/login/qrcode`；状态提供本机 `cdp_port`，登录任务支持 `request_id` 幂等、`wait_seconds` 有界等待，以及完成响应后消费短期二维码结果。Provider 是独立的外部依赖，仓库不会自动安装、购买或启动它；不要将只提供笔记下载的任意服务当作兼容实现。
2. 在本机忽略提交的 `var/secrets/worker-provider.env` 设置 `FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL=http://127.0.0.1:5556`（端口按实际服务调整），再运行 `./start.ps1 -BenchmarkAnalysis`。此模式会把 Web 研究接口明确指向本次启动的 API 端口，优先于旧 `.env.local` 中独立研究 API 的地址，不修改该文件。可选分析依赖、模型及预算见 [视频深析 SOP](benchmark-video-analysis.md)。Web 与 API 均须使用更新后的代码。
3. 打开 `/benchmarks`。面板只检查连接，已登录则保留会话；尚未登录时点击连接按钮才生成二维码。打开页面或读取历史不会生成二维码。
4. 使用小红书 App 扫码并确认。后端通过一个新的临时平台页面重新验证登录，不能仅凭 Provider 缓存的 `is_logged_in` 或 Cookie 存在判定成功。二维码最多展示 120 秒；过期后需要主动重取。Web 隐藏时暂停轮询，连续失败或达到轮询期限后停止并提供重试入口。
5. 登录成功后，仅恢复当前账号/笔记已请求的免费主页、身份补全或详情读取。切换账号/笔记后忽略旧响应。视频创建、取消后的重试以及重新分析继续要求用户点击。
6. 遇到平台验证码、风险检查或扫码异常，由用户在本机浏览器处理后重新检查连接。应用不会绕过平台限制。服务未配置或不可达时按明确错误修复；不要重复提交二维码请求。

## 契约和边界

- `GET /v1/benchmark-auth/xiaohongshu/status`：非破坏检查，状态为 `not_configured`、`provider_unavailable`、`checking`、`login_required`、`awaiting_scan`、`authorized`、`expired` 或 `error`。
- `POST /v1/benchmark-auth/xiaohongshu/qrcode`：JSON `{}`，可带 `Idempotency-Key`。仅显式操作可产生二维码；正在处理的请求复用同一任务，已登录时不发起新登录。去重按本机 API 的活动操作执行，客户端键不提供跨重启持久重放保证。生成较慢时 HTTP 先返回 `checking`，原 POST 的后台操作在有限期限内沿用相同 Provider request ID 等待并消费结果；GET 不请求新二维码。超时后的显式重试继续原 ID。
- 响应遵守 `benchmark-auth-state.schema.json`，只返回经过校验的短期 PNG data URL，不返回 Provider task ID、Cookie、登录账号资料、任意图片 URL 或签名媒体地址。响应 `private, no-store`；Vistora 的二维码仅存在于内存和当前面板，不进入 Vistora SQLite、浏览器持久存储或历史归档。外部 Provider 的任务结果和浏览器目录由该服务管理，须按其保留/消费机制清理，不能将 Vistora 的不落盘保证扩展到外部服务。
- 接口限非生产配置、默认用户/工作区、`assets:write`、回环 peer 和 Host；有 Origin 时必须同时匹配配置和回环地址，拒绝转发头及跨站来源。Provider 请求禁代理、禁重定向、限回环地址和响应大小。
- 状态检查使用串行通道和短期缓存，不关闭用户已有页签。登录核验、二维码 Provider I/O、主页和详情采集进入独立 Python 子进程，超时或取消后回收自有客户端和驱动进程树。Windows 使用 Job Object，POSIX 使用独立进程组及父进程存活监测。登录核验期限 35 秒，Provider I/O 期限 10 秒，最终等待进程退出最多 5 秒；HTTP 仍可先返回 checking。已有受管浏览器不属于该进程树，不会被终止。CDP 完全无响应时无法保证已创建的临时页签被关闭，平台恢复后可手动关掉残留页签。进程边界不是 OS 安全沙箱，公网和多人服务仍需要正式身份和运行隔离。
- 本地二维码服务及浏览器会话属于安装者，不能扩展为租户共享登录。只运行一个 API 进程，不使用 `--workers`；扫码串行和去重是进程内保证。生产模式继续拒绝此通道。已保存报告不依赖当前登录态；历史读取不重新采集或执行模型。

## 验证要求

API 回归覆盖权限/Origin、只读检查、已有登录保护、幂等、PNG 校验、过期、故障、串行及新页面登录判断；Web 回归覆盖有限轮询、取消、迟到响应和免费恢复。接口的 fixture 测试不能证明真实扫码完成。

真实验收需要兼容 Provider 和更新后的 API/Web：退出登录的独立测试会话 → 用户扫码 → 当前免费采集恢复 → 显式视频分析 → 历史读取。不得为测试清空用户已有浏览器会话。对既有已登录会话只执行新页面状态核验，不触发二维码请求。

升级后先运行只读兼容性检查：`python tools/verify_benchmark_runtime.py --api-url http://127.0.0.1:8210 --web-origin http://127.0.0.1:4173`。端口按实际配置调整。该工具核对运行进程的路由、授权/历史公共 Schema、CORS 和 no-store，不生成二维码或付费任务，也不打印报告内容、二维码或登录资料。`compatible=true` 只代表接口配套；`connection_state=provider_unavailable` 仍需启动外部 Provider，`scan_flow_verified` 始终为 false。完整启动器在 `-BenchmarkAnalysis` 模式下自动执行此检查；旧版本 404/405 会阻止误报启动完成，Web 同样提示更新并重启 API/Web。
