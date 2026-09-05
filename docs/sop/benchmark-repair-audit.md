# 小红书连接与媒体资源修复审计 · 2026-09-06

结论：上轮依赖漏洞、浏览器客户端永久占用和媒体执行中容量控制问题已修复，源码可合并；本机服务切换和真实扫码验收未完成，不能认定生产发布就绪。本记录接续 [主线整合审计](benchmark-integration-audit.md)，旧记录中的依赖告警和线程边界以本轮结果为准。

## 修复结果与证据

- **旧地址接线**：原 Web `.env.local` 仍指向独立研究 API 8210。`start.ps1 -BenchmarkAnalysis` 现在显式把 Web 研究请求指向本次 API 端口，避免启动 8200 后浏览器继续请求旧 8210；不修改私密环境文件。已用当前 Vinext 的真实 dotenv loader 验证进程环境优先于原文件。[启动接线](../../start.ps1#L914)
- **运行进程兼容性**：新只读工具核对 OpenAPI 路由、授权/历史规范 Schema、CORS 响应及 GET/POST 预检（包括 Idempotency-Key）、no-store。旧路由、坏 HTTP 响应和不兼容配置返回固定安全码；CLI 错误写 stderr，启动器可显示原因。Web 在授权和历史列表缺接口时提示配对升级，单份历史记录不存在仍保持原错误。兼容检查不生成二维码、付费任务或扫码成功证明。[检查工具](../../tools/verify_benchmark_runtime.py#L80)
- **依赖**：Drizzle 保持 0.31.10，仅把旧 loader 的 esbuild 精确覆盖到已在项目使用的 0.28.2，移除旧 0.18.20 子树。完整 npm audit 由 4 moderate 降为 0；安全门禁阈值从 high 提高至 moderate，未削弱检测。新增测试验证实际 TypeScript 转换、SQLite 迁移执行、默认值和重复生成。[覆盖](../../apps/web/package.json#L49)、[兼容性测试](../../apps/web/tests/drizzle-toolchain.test.mjs#L20)、[门禁](../../deploy/production/security-policy.json#L12)
- **浏览器回收**：登录核验、二维码 Provider I/O、主页发现、详情和媒体采集均通过独立 Python helper。Windows Job Object/POSIX 进程组拥有本次客户端与驱动，取消/超时会终止并回收；API 崩溃也有回收边界。子进程固定导入当前检出的 API 包，避免旧 editable 安装执行旧代码。凭据仅走私有管道，不进入命令参数或异常返回。[进程边界](../../apps/api/src/framefactory_api/browser_process.py#L35)、[真实 OS 故障测试](../../apps/api/tests/test_browser_process.py#L41)
- **容量与失败恢复**：API 在采集和分析中监测全部任务数据、当前尝试和剩余磁盘；单次尝试 256 MiB，保留至少 32 MiB 磁盘及持久化余量。Worker 写入前预算、FFmpeg 文件/600 秒时长限制、JSON 原子替换计量与 TEMP/TMP 定位共同限制增长。超限中止采集/Worker 树，清理失败尝试并保留显式重试；不会退化为 partial 成功。受管任务禁用模型运行时下载，预置模型仍可用。[监测和清理](../../apps/api/src/framefactory_api/benchmark_jobs.py#L902)、[Worker 预算](../../services/worker/framefactory/worker/benchmark_analysis.py#L49)

依赖选择依据：[esbuild CORS 公告](https://github.com/evanw/esbuild/security/advisories/GHSA-67mh-4wv8-2f99)、[Windows 公告](https://github.com/evanw/esbuild/security/advisories/GHSA-g7r4-m6w7-qqqr)；未直接迁移到存在格式变化的 [Drizzle 1.x](https://orm.drizzle.team/docs/v0-v1-changes)。

## 验证结果

命令在本次隔离检出中执行，Python 使用原仓库 `.venv/Scripts/python.exe` 并显式设置本检出的 API/Worker PYTHONPATH；Web 使用官方 Node 22.13.0/npm 10.9.2。

| 检查 | 结果 | 实际范围 |
| --- | --- | --- |
| `python -m pytest apps/api/tests -q -r s` | **通过 480，跳过 0** | 真实专用 PostgreSQL 数据库、MinIO 桶开启原先跳过的 4 项测试，测试后只清理新建资源；数据库网页产物测试仍使用其既有队列/存储 fixture，不等于整条生产生成链路 |
| `python -m pytest services/worker/tests -q -r s` | **通过 517 和 116 个子测试；跳过 1** | 未启用旧版 Edge 实机适配检查；新的真实 FFmpeg 600 秒/字节限制与子进程超额写入检查通过 |
| `python -m pytest tests/contract tools/tests -q -r s` | **通过 106** | 最终版本重新运行，包含运行时检查的 21 项测试；丢弃工具修改前的 102 项结果 |
| `python -m ruff check apps/api services/worker tools/verify_benchmark_runtime.py tools/tests/test_benchmark_runtime.py` | **通过** | 全 API/Worker 及新增工具 |
| Web `npm ci`、`npm run lint`、`npm run typecheck`、`npm test` | **通过，87 项，跳过 0** | 干净依赖、生产构建、旧接口恢复、Drizzle 实际迁移生成及已有 Web 回归 |
| `npm audit --json`；安全工具 moderate 阈值检查 | **通过，已知漏洞 0** | 包括开发依赖；旧 loader 仍有弃用提示，不能解释为无未来风险 |
| PowerShell AST 解析；真实 Vinext dotenv 优先级检查 | **通过** | 启动脚本零语法错误；本次 API 地址覆盖原 8210 配置，无需改私密文件 |
| 源码秘密扫描与生产静态配置 | **通过** | 扫描包含未 stage 的新源码，未发现真实秘密；6 份生产配置符合既有策略；不将静态配置检查称为生产部署 |
| 新版 API 实际监听、运行切换与扫码闭环 | **受阻 / 未执行** | 本轮最初 8210/5556 未监听；自动审批审核拒绝了“备份研究数据库并启动新版 API”的组合命令，仅给出 `blocked by policy`，命令未执行，没有切换或修改原研究数据库 |
| 生产受保护恢复、负载及真实付费 Provider 验收 | **未执行** | 需要对应受保护环境和真实交互；本轮不触发付费分析 |

## 收尾审阅范围与剩余发现

- **功能/接线**：已审阅 Web → 研究 API → Provider、二维码过期/取消/迟到响应、旧接口恢复、只读历史与显式付费入口。规范 Schema 未改；运行兼容工具不能代替真实扫码。已有 4 条本机任务只读检查均为 ready，未启动重算。
- **权限/隐私/来源**：保留本机 owner/workspace/assets 权限、回环 Host/Origin 与生产禁用边界；CDP 只连接独立现有受管浏览器，不将它纳入终止进程树。审阅了输入身份、可信媒体下载、凭据管道、固定错误响应、二维码内存边界；没有新增素材或内容使用授权。
- **持久化/恢复**：审阅 SQLite 事务、目录所有权、历史归档、取消与重试、失败清理和 Windows 进程死亡测试。新增专用数据库/桶真实验证已清理；用户原数据库及并行 UI 文件未被清理、重置或覆盖。
- **容量/成本**：文件计量与 Worker 输出预算有真实故障注入和 FFmpeg 验证；不增加模型调用或自动重试。100ms 监测不构成内核磁盘硬配额，回收期间可能短暂超限，仍应保留专用目录容量监控。自定义三参数采集 callback 兼容入口须自行遵守期限，真实 Provider 的可终止保证不扩展到任意第三方 callback。
- **发布/依赖/运维**：审阅生产策略、锁文件、跨平台子进程导入、启动器和 SOP。Web 干净安装及迁移兼容已验证；Python 依赖/生产锁未改。Linux 分支以本次提交的 GitHub CI 为准，不能用此前 main 的绿色结果代替。未执行的生产恢复/告警/容量门禁仍是发布验收缺口。

| 级别 | 触发、影响、证据 | 后续动作 |
| --- | --- | --- |
| **P1，确认的验收阻塞** | 当前浏览器 Provider 未运行，真实扫码未完成；本机启动被自动审批阻止，不能确认运行进程已使用修复版本。[连接要求](xiaohongshu-connection.md#验证要求) | 用户启动可信 Provider 后，在更新后的项目运行 `./start.ps1 -BenchmarkAnalysis`，在独立测试会话扫码并检查免费恢复及显式分析。完整生产验收仍需受保护环境 |
| **P3，运行边界** | CDP 完全失联时，自有 helper/driver 可回收，但已创建的浏览器临时页可能残留；终止真实浏览器会破坏现有登录，因此保留会话。[进程说明](xiaohongshu-connection.md#契约和边界) | 平台恢复后关闭残留页签；不能承诺失联浏览器自身资源完全回收 |

稳定性：全 API/Worker 验证期间业务源码无变化；仅运行时检查工具及其测试在审查后补修，受影响的工具/契约结果已在最后重跑。Web 最终 87 项验证后源码无变化。随后仅补充启动地址接线（已重做 PowerShell/Vinext 验证）和本记录。最终交付仍须核对 Git 差异和文件哈希；原 Desktop 目录的独立 UI 修改不混入本次 GitHub 修复提交。
