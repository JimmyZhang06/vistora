# 本机启动修复与收尾审计 · 2026-09-07

结论：**本机启动验收通过，完整栈保持运行；不是生产发布就绪声明。** 在普通 Windows PATH、项目外目录下，CMD 启动和重启退出码均为 0；重复启动保留全部 PID。三个真实页面均为 HTTP 200，浏览器控制台、页面异常和当前服务日志错误计数均为 0。此前小红书审计中的 Docker 启动阻塞已在本次解除。

## 使用入口

```bat
start.cmd -Xiaohongshu -NoInstall
start.cmd -Xiaohongshu -NoInstall -Restart
stop.cmd
```

第一次安装依赖时去掉 `-NoInstall`；双击 `start.cmd` 启动标准栈。需要小红书时保留 `-Xiaohongshu`。当前 Web 为 `http://127.0.0.1:4173/create`，API 为 `http://127.0.0.1:8200`，内置采集浏览器为 `127.0.0.1:5556`。默认端口无法绑定时以启动器输出为准。

## 修复内容与证据

- [CMD 启动入口](../../start.cmd) 自动发现 PowerShell；缺失时使用[便携运行时引导](../../tools/bootstrap-powershell.ps1)，从官方发布下载固定的 PowerShell 7.6.5，校验 SHA-256 与签名，保存在项目 `var/tools`。没有修改系统 PATH 或升级 Docker。[Microsoft 安装说明](https://learn.microsoft.com/en-us/powershell/scripting/install/install-powershell-on-windows)、[对应官方发布](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.5)。
- [进程管理](../../start.ps1#L411) 对参数正确引用，只选择 PATH 中首个 Node/Docker 可执行文件，并隔离后台进程继承的标准句柄；修复了多套 Node 路径合并以及重定向 CMD 等待后台服务退出的问题。真实管道回归见[测试](../../tests/contract/test_windows_launcher.py#L98)。
- [共享启动原语](../../tools/local-launcher.ps1) 提供独占启动/停止锁、实际端口绑定探测、精确创建时间核对、原子状态写入、代码/配置/工具路径指纹和已安装 Web 依赖复用。代码、模式或进程不一致时要求显式 `-Restart`，不把其他程序的健康响应当成本项目。
- [Docker 恢复](../../start.ps1#L675) 只在没有 Desktop 进程时检查并备份指定的残留 socket 目录，直接启动 Desktop 后有界等待本机引擎。实际恢复后引擎版本为 29.7.2；没有删除数据卷、重置或重装 Docker。备份保留在原目录旁的 `.stale-vistora-*` 目录。
- [本地 Cloudflare 配置](../../start.ps1#L506) 禁用不必要的开发地理信息请求，消除该请求超时日志；本地持久服务[强制绑定回环地址](../../start.ps1#L952)，Web/API/MinIO CORS 使用本次实际端口。
- [登录期限测试](../../apps/api/tests/test_xhs_browser.py#L198) 使用模块内固定时钟验证精确的 180 秒上限，修复浮点差值略大于 180 的偶发断言；未更改真实期限或放宽验证。

## 检查结果

Python 命令均使用项目 `.venv/Scripts/python.exe`；npm 命令在 `apps/web` 执行。

| 检查或命令 | 最终结果 |
| --- | --- |
| 普通 Windows PATH、从 `C:\Users\Zhang` 调用 `start.cmd -Xiaohongshu -NoBrowser` | **passed**：完整依赖安装、真实基础设施启动、迁移、Worker 健康检查与应用启动；中途发现的文件占用已修复恢复。 |
| `start.cmd -FrontendOnly -NoBrowser` | **passed**：复用已验证的依赖，正常返回 0；明确提示此模式没有 API。 |
| `start.cmd -Xiaohongshu -NoInstall -NoBrowser -Restart` | **passed**：停止已记录前端，启动完整栈并返回 0。最新日志 `var/runtime/startup-final.log`。 |
| 相同参数重复启动 | **passed**：PID `43004,27240,40516,43700` 不变，核对身份及 HTTP 就绪状态。 |
| `stop.cmd`、`stop.cmd -Infrastructure`，随后重新启动 | **passed**：进程及容器正确停止，三个 Vistora 数据卷保留并成功复用；未操作其他项目的数据卷。 |
| `start.cmd -FrontendOnly -WebPort 8205 -NoInstall -NoBrowser` | **passed（预期拒绝）**：本机 Windows 保留端口返回 1，无受管进程记录遗留。真实占用端口、自动候选端口、锁和句柄回归也通过。 |
| `pytest tests/contract tools/tests -q` | **passed：112**，包含 6 个 Windows 真实进程/端口/锁测试。 |
| `pytest apps/api/tests -ra`（接入独立真实测试数据库及 MinIO） | **passed：492，skipped：0**；2 条第三方弃用警告。相关测试文件最后的格式调整另以 `pytest apps/api/tests/test_xhs_browser.py -q` 在稳定文件上重验，6 项通过。 |
| `pytest services/worker/tests -ra` | **passed：517；skipped：1**，未启用 live Edge/FFmpeg 媒体集成。 |
| `pytest apps/api/tests/test_browser_process.py -q` | **passed：10**；首次并行全量执行曾有一个驱动心跳超时，专项及后续全量均通过，未放宽该测试。 |
| `ruff check apps/api/src apps/api/tests services/worker tests/contract/test_windows_launcher.py` | **passed**。 |
| `npm run lint`、`npm run typecheck`、`npm test` | **passed**：最新稳定前端源码，完整生产构建及 90 项测试通过。 |
| `npm audit --omit=dev --audit-level=high`、`pip check` | **passed**：生产 npm 漏洞报告为 0，Python 安装依赖无冲突；本次 `npm ci` 的完整 npm 报告也为 0。不是 Python 漏洞库扫描。 |
| PowerShell 解析四个脚本、`git diff --check` | **passed**。Windows PowerShell 5.1 实际运行引导脚本并验证安装成功。 |
| PostgreSQL / Redis / MinIO、API `/readyz`、Worker | **passed**：三个真实容器 healthy；API 返回 PostgreSQL 持久化；Worker 启动健康检查通过，当前进程身份匹配。 |
| 真实浏览器访问 `/create`、`/benchmarks`、`/benchmarks/history` | **passed**：三页均 200 且内容非空；页面异常 0、控制台错误 0、HTTP 错误 0；实际 API 请求唯一来源为 `http://127.0.0.1:8200`。记录在 `var/runtime/startup-ui-smoke-result.json`。 |
| `verify_benchmark_runtime.py --api-url http://127.0.0.1:8200 --web-origin http://127.0.0.1:4173`（启动器内执行） | **passed**：compatible=true、history=passed、connection_state=login_required；真实扫码与登录后采集未验收。 |

早期失败记录保留在 `var/runtime/startup-*` 和归档日志中，不能用它们描述当前运行状态。期间有其他操作修改前端，某次构建读到尚未完成的 JSX 编辑；未回滚这些修改，待稳定后重新完成 lint、类型检查、构建和全部 Web 测试。最终代码快照覆盖 578 个非忽略文件；本审计为之后新增的记录。

## 残余发现与未验收范围

没有发现尚未解决的 P0/P1 **本机启动**阻塞。以下不是已证明的运行错误，仍限制功能/发布验收：

| 严重性 | 触发、影响、下一步与证据 |
| --- | --- |
| **P2 · 未验收** | 小红书当前为 `login_required`；必须由本人扫码或处理平台验证后，才能验收登录后详情。启动成功不代表平台登录成功。[人工验证边界](../../apps/api/src/framefactory_api/xhs_browser.py#L155)。未调用付费深析服务。 |
| **P2 · 确认的可选依赖缺失** | 普通系统 PATH 缺少 `pdfinfo`/`pdftoppm`，启动器正确地不声明 PDF 文档能力；PDF 工作流需要安装 Poppler 并加入用户 PATH 后重启。[能力条件](../../start.ps1#L1086)。小红书启动不受影响。 |
| **P2 · 发布验证缺口** | 远端 CI、Windows CI、生产身份网关、恢复/负载/告警演练未在本次执行。现有 CI 的 Linux 测试不能执行新增 Windows 行为测试，应增加 Windows 验证再做跨机器发布。[CI](../../.github/workflows/release-gates.yml#L40)、[Windows 条件](../../tests/contract/test_windows_launcher.py#L16)。 |

## 按领域的收尾审阅

- **功能与架构：** 核对 Web→本次 API、Worker→PostgreSQL/Redis/MinIO、迁移/Seed 路径、可选能力条件和研究历史契约；真实页面、真实基础服务和真实数据库/对象存储测试通过。单元夹具未被算作平台登录或完整付费业务 E2E。
- **认证与安全：** 核对回环监听、Docker 本机端点及连接覆盖变量拒绝、CORS、进程身份、Provider 变量隔离/恢复、状态文件不含 Provider 值、官方运行时来源和摘要/签名。API 现有隔离、输入验证、SSRF 回归通过。开发默认身份与凭据仍仅适合本机，没有声明公网安全。
- **数据与恢复：** 迁移账本 unchanged；停止不删除数据卷；启动失败只回收本次已记录进程，PID 创建时间精确匹配。状态原子写入与互斥锁有真实测试。真实集成使用 `vistora_startup_test_*` 独立数据库及 `vistora-startup-test-20260907` 测试桶，保留供复核；未对用户业务库执行测试清空。未执行生产备份恢复。
- **性能与资源：** 端口搜索有上限，Docker 启动轮询有超时；依赖复用避免重复卸载原生绑定，安装失败仅重试一次。后台标准句柄、日志轮转及停止流程有实测。未启动付费任务，未开展容量、外部限流或服务商成本压力测试。
- **可复现性与运维：** 使用现有 npm 锁文件及项目虚拟环境；更新 README 的准确入口、重启、端口和恢复说明；保留所有预先存在及并发修改。Python 漏洞库扫描、live Edge/FFmpeg、远端发布门禁及生产演练标记为 **skipped / 未验收**，不能据本次本机通过称为生产就绪。
