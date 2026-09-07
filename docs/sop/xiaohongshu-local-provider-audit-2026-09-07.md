# 小红书内置浏览器修复与收尾审计 · 2026-09-07

结论：**本机实现已修复，公开主页采集与独立 Web/API 冒烟通过；真实扫码及登录后详情尚未验收，完整部署仍 BLOCKED。** 不能称为生产就绪，也不能用离线二维码夹具代替真实扫码。

## 范围与工作区

- 新增项目自带的 `framefactory_api.xhs_browser`：持久化 Chromium、动态回环 CDP、真实页面二维码元素截图、有限任务去重与清理。
- `start.ps1 -Xiaohongshu` 启用浏览器和账号报告历史；`-BenchmarkAnalysis` 同时安装可选深析依赖。外部服务需显式 `-ExternalXhsBrowser`。兼容自动化脚本不再默认启动仿二维码 mock。
- 修复登录核验：公开作者资料不能证明访问者登录；初始及延迟平台安全跳转返回明确错误；Web 展示恢复提示并停止轮询。
- 修正旧进程回收测试夹具，使其接受 Playwright 的 `timeout` 参数；未放宽回收断言。
- 保留任务开始前的 README、全局样式、批量控制台及未跟踪脚本。只对 README 和兼容启动脚本的小红书相关内容做增量修改；mock 文件保留，未作为真实验收服务运行。未清空用户浏览器、数据库或未跟踪文件。

## 待解决发现

| 严重性 | 事实、触发与影响 | 处理及证据 |
| --- | --- | --- |
| **P1 · 确认的验收阻塞** | 真实 `www.xiaohongshu.com/explore` 跳到 `/website-login/error`，页面显示「IP 存在风险」「300012」。无法取得真实扫码二维码和完成账号确认；不能验证当前平台二维码 DOM、登录后身份补全、视频/图文详情及深析。 | 使用可信网络并由本人在采集浏览器完成平台验证，再走扫码验收。代码在 [benchmark_auth.py](../../apps/api/src/framefactory_api/benchmark_auth.py#L211) 和 [xhs_browser.py](../../apps/api/src/framefactory_api/xhs_browser.py#L147)；操作见 [连接 SOP](xiaohongshu-connection.md#安装与操作)。 |
| **P1 · 确认的部署阻塞** | `start.ps1 -Xiaohongshu -NoInstall -NoBrowser` 尝试启动 Docker Desktop，90 秒后失败。没有启动完整 PostgreSQL/Redis/MinIO 栈，因此完整启动器、持久化主业务链路、真实迁移及备份恢复无法验收。 | 修复 Docker Desktop/WSL 引擎后重跑启动器与保护性发布门禁。[启动器超时](../../start.ps1#L628)、[生产门禁](../../deploy/production/README.md#必需边界)。启动器已保留异常 socket 目录为 `.stale-vistora-20260907-145828`，未删除其内容。 |
| **P1 · 既有公网发布边界** | 当前 `DefaultWorkspaceContextProvider` 固定返回默认用户及工作区；若将业务 API 直接暴露到不可信网络，不能依靠工作区请求头实现身份认证或租户隔离。本次内置浏览器不改变这一产品边界。 | 只运行本机单用户模式；公网部署前接入实际身份网关及授权上下文。[context.py](../../apps/api/src/framefactory_api/context.py#L41)、[生产部署说明](../../deploy/production/README.md#必需边界)。本次未部署公网服务。 |

## 真实运行证据

- **passed · 内置浏览器**：以独立目录 `var/browser/xhs-validation` 启动最新内置服务 `127.0.0.1:5557`，真实 Chromium 提供动态 CDP；不复用 mock 或个人浏览器档案。
- **passed · 风控失败路径**：API 的隔离浏览器核验返回 `state=error`、`error_code=BENCHMARK_AUTH_PLATFORM_RESTRICTED`、无二维码。独立 Chromium 确认页面实际为平台 `300012` 安全限制。没有尝试绕过。
- **passed · 公开元数据；blocked · 笔记身份**：北京时间 15:01 起低频验证项目示例 `5a8cf39111be10466d285d6b`。真实 SSR 得到 32 条样本：31 视频、1 图文；0 条已核验身份、32 条未补全，`BENCHMARK_AUTHENTICATION_REQUIRED`。响应凭据扫描通过。详情分支因缺可核验 ID 被跳过，未手填 ID 或使用旧报告补齐。
- **passed · 独立 HTTP/UI 冒烟**：Web `127.0.0.1:4181` → API `127.0.0.1:18211` → 内置浏览器。页面显示风控提示和 0/32 身份状态，真实公开数据生成账号报告并写入独立 SQLite 历史；打开历史没有新增写请求，未创建视频分析任务。截图留在被忽略的 `var/runtime/xhs-ui-validation.png`。
- 上述 API 明确使用 **memory 主业务 Repository + 独立真实 SQLite 研究历史** `var/benchmark-xhs-validation`，不是 PostgreSQL/Redis/MinIO 端到端验收。测试端口 8211 被 Windows 拒绝绑定后改用 18211；未占用或终止用户已有服务。
- **skipped**：另两个真实主页未获指定；未执行三个不同主页的跨账号发布门禁。
- **blocked**：真实扫码、登录过期重新扫码、免费详情恢复、已登录视频与图文详情、真实 Worker/模型深析；本次未产生模型调用费用。
- **passed · 测试进程清理**：结束独立 Web/API/浏览器验证后，4181、18211、5557 均不再监听；无使用 `xhs-validation` 档案的 Chromium 主进程。保留测试截图、研究 SQLite 和独立浏览器目录，未删除任何已有数据。

## 命令与结果

Python 命令使用本仓库 `.venv/Scripts/python.exe`；npm 命令在 `apps/web` 执行。

| 检查 | 结果 |
| --- | --- |
| `python -m ruff check apps/api/src apps/api/tests` | **passed** |
| `python -m pytest apps/api/tests -ra` | **passed：485；skipped：4**。首次全量检查因旧夹具不接受 `timeout` 失败；修复后重跑全量通过。 |
| `python -m ruff check services/worker` | **passed** |
| `python -m pytest services/worker/tests -q` | **passed：517；skipped：1；116 subtests passed** |
| `python -m pytest tests/contract tools/tests -ra` | **passed：106** |
| `npm run lint`、`npm run typecheck` | **passed** |
| `npm test` | **passed：88**，含完整生产构建和真实 HTTP adapter 测试 |
| `npm audit --omit=dev --audit-level=high` | **passed：0 vulnerabilities**，仅此时 registry 报告及生产 npm 依赖范围 |
| `python -m pip check` | **passed**，无安装依赖冲突；不是 Python 漏洞扫描 |
| `python -m framefactory.worker.temporal_benchmark services/worker/benchmarks/temporal_cut_v1.json --minimum-f1 0.90 --maximum-unsafe-rate 0` | **passed**，3 个基线案例，F1=1，unsafe=0 |
| PowerShell Parser 检查 `start.ps1`、`start-benchmark-automation.ps1` | **passed：0 syntax errors** |
| `python tools/verify_benchmark_runtime.py --api-url http://127.0.0.1:18211 --web-origin http://127.0.0.1:4181` | **passed：compatible=true，history=passed**；`connection_state=error`，`live_login_verified=false`，`scan_flow_verified=false` |
| `python tools/verify_benchmark_discovery.py --profile https://www.xiaohongshu.com/user/profile/5a8cf39111be10466d285d6b --browser-origin http://127.0.0.1:5557 --details` | **passed：公开样本/凭据过滤；blocked：身份；skipped：详情**，不能按进程退出码 0 宣称完整成功 |
| `start.ps1 -Xiaohongshu -NoInstall -NoBrowser` | **blocked**，Docker Desktop 启动超过 90 秒 |
| `git diff --check`、570 个实际工作区文件 SHA-256 前后比较 | **passed**，验证期间没有并发文件变化；本审计文档为检查完成后的新增记录 |

API 的 4 个 skipped：2 个 PostgreSQL Channel E2E、1 个 PostgreSQL artifact E2E、1 个 MinIO 集成测试，缺少实际测试服务配置。Worker 的 1 个 skipped 为未启用的 live Edge/FFmpeg catalog 集成（`FRAMEFACTORY_RUN_LEGACY_MEDIA_INTEGRATION`）。Python 漏洞库扫描、npm 开发依赖完整漏洞扫描、远端 CI、本次部署的真实恢复/负载/告警演练均 **skipped**。

## 按领域的收尾审阅

| 领域 | 已审阅证据与边界 |
| --- | --- |
| 用户功能/完整性 | 检查连接面板、有限轮询与取消、公开主页报告、身份恢复、只读历史；实际 UI 没有把缺身份样本称为详情成功。真实扫码仍 blocked。 |
| Web/API/Worker/存储一致性 | 启动器将研究 API 指向本次端口；`-Xiaohongshu` 打开共享 SQLite 历史，`-BenchmarkAnalysis` 才额外安装模型依赖并配置模型文件；采集/历史不自动创建深析任务。契约和 Seed 门禁通过。没有把研究内容导入生产素材库或 Redis 队列。 |
| 权限/SSRF/隐私/来源 | 查看并测试回环 peer/Host/Origin、转发头、JSON/长度验证、规范平台 URL、PNG 投影与 no-store；当前登录核验只依据访问者 guest+ID。浏览器自身档案含登录存储/缓存，文档明确私有目录边界。账号/内容的使用权不因扫码而取得。公网身份限制见上表。 |
| 一致性/重试/崩溃/迁移 | 浏览器一次一个任务；内存 QR 120 秒、失败 3 秒后清理；有限去重身份记录不含二维码。真实 Chromium 离线测试验证 CDP 断开不关闭宿主、清理临时页及独立档案重启恢复。API 进程树取消/崩溃回归通过。研究 SQLite 使用 WAL、FULL、文件锁，历史按工作区过滤；未修改既有迁移。真实 PostgreSQL/S3 恢复没有环境可测。 |
| 性能/成本/清理 | 浏览器生成操作 35 秒期限、输入 1 KiB/5 秒、HTTP 并发上限 16、二维码元素尺寸上限 1024；复用已有主页采集预算、缓存和串行控制。检查研究任务的容量/磁盘/媒体超时与费用配置路径；没有执行付费任务。大规模并发及真实平台限流未做压力测试。 |
| 构建/供应链/部署/运维 | 生产 Web 构建、类型、lint、Python 回归及基础依赖检查通过；未新增第三方依赖，使用已有锁定 Playwright。审阅 release-gates.yml、生产部署、数据库迁移、恢复说明；同步连接 SOP、Provider SOP、README 与启动参数。未运行受保护发布环境的恢复、负载和告警演练。 |

此记录的通过项只覆盖表内证据，不对未运行的基础设施、真实登录或公网部署作安全与可用性承诺。
