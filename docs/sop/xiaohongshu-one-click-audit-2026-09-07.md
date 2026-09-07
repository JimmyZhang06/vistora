# 小红书一键连接采集：本轮验收

结论：一键连接、等待用户验证、验证成功后继续原账号采集的代码与受控浏览器回归通过；真实平台扫码、登录后详情采集和生产基础设施尚未验收，不能据此宣称完整功能或生产环境已可用。

本轮仅补齐交互和登录恢复，不修改主任务的启动器、Docker 配置、数据库、Worker 或用户已有改动。工作区存在主任务并发写入，以下将本轮实测和继承的背景问题分开记录。

## 用户路径与修复

- 输入主页，点击「连接小红书并采集」。已有有效登录直接继续；需要登录时获取二维码，内置可视浏览器遇到验证或二维码定位超时则保留验证页面。
- 验证成功以新的平台页面登录状态为准，自动刷新用户刚才提交的主页及笔记身份；不使用登录前缓存，也不触发付费视频任务。
- 后台初始检查未结束时，显式点击不会丢失。过时响应不能覆盖当前输入或连接状态。
- 初始页面准备好后才允许输入，避免快速输入的新账号被默认账号覆盖。
- 手动验证等待最多三分钟；重复轮询不会重复创建二维码任务，页面隐藏时暂停前端轮询，返回后恢复。过期、失败和未配置状态仍显示实际结果。

实现证据：`apps/web/components/benchmark-account-demo.tsx`、`apps/web/components/benchmark-connection.tsx`、`apps/web/lib/benchmark-connection.ts`、`apps/api/src/framefactory_api/benchmark_auth.py`、`apps/api/src/framefactory_api/xhs_browser.py`。

## 尚未通过的发布条件

### P1：真实平台闭环未验证

触发：小红书返回网络风险页，或需要真实账号扫码/安全验证。影响：即使项目正确展示验证页面，也不能保证平台放行或返回详情。

证据：`xhs_browser.py` 的 `_manual_verification` / `_read_qr`、`benchmark_auth.py` 的 `_fresh_platform_login`；本轮浏览器回归拦截全部 `/v1/**` 请求，明确仅验证交互。先前的 IP 风险报告是背景证据，本轮未重新进行真实扫码或网络诊断。

待办：在可正常访问小红书的本机网络下，用真实账号完成一次按钮→扫码/验证→登录核验→目标主页→可验证笔记详情的完整验收。若仍为风险页，保留失败状态并检查平台提示；本次改动没有解除平台限制。

### P1：基础设施集成与生产部署未验证

触发：未提供测试 PostgreSQL 与 S3 环境，且之前报告 Docker 启动失败。影响：不能证明完整生产栈、持久化及恢复流程可用。

证据：`apps/api/tests/test_postgres_channel_e2e.py`、`test_postgres_webpage_artifact_e2e.py`、`test_s3_storage_integration.py` 的环境门控；`.github/workflows/release-gates.yml` 的独立集成和恢复门禁。本轮未重启 Docker，也未将受控服务替代真实依赖后标为 E2E。

待办：由启动器主任务处理环境后执行上述集成测试、部署健康检查和恢复门禁。

## 本轮检查

| 检查 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest apps/api/tests -ra` | 通过 488；跳过 4（PostgreSQL 三项、S3 一项）；两条依赖弃用警告 |
| `.venv\Scripts\python.exe -m pytest apps/api/tests/test_browser_process.py` | 通过 10 |
| `npm run lint`，`npm run typecheck`（`apps/web`） | 通过；使用失败即退出的命令执行 |
| `npm test`（`apps/web`，包含生产构建） | 通过 90，构建通过 |
| `.venv\Scripts\python.exe -m pytest tests/contract tools/tests -ra` | 通过 111；因并发启动器编辑，另重跑受影响项 |
| `.venv\Scripts\python.exe -m pytest tests/contract/test_windows_launcher.py tests/contract/test_runtime_wiring.py -ra` | 通过 17；最终文件稳定性另见交付说明 |
| `.venv\Scripts\python.exe -m ruff check apps/api/src apps/api/tests` | 通过 |
| `git diff --check` | 通过；仅 Git 换行格式提示 |
| Playwright + 本轮独立生产预览端口 4192 | 受控交互通过：快速输入不同账号、一次登录请求、验证后一次刷新采集、目标账号正确、零付费任务请求；全部 API 请求被拦截，非平台 E2E |
| Worker 全量、真实平台扫码与详情、Docker/数据库/S3、备份恢复、容量及外部模型成本实测 | 本轮未执行/受阻，不能引用主任务以前的结果作为本轮新通过 |

首次全量 API 曾有一个浏览器子进程关闭测试未及时写出标记；未更改超时，随后该文件 10 项及全量 488 项均通过。时序/负载是可能原因，尚未确证。初次 Web lint 发现 effect 内直接更新状态，已移入现有初始化定时回调，重新 lint、类型检查、构建和测试通过。一次临时浏览器验证脚本使用错误响应字段，按 API 模型生成响应后通过；此问题属于验证脚本，不计作产品通过证据。

## 收尾审计覆盖

- **可行性与完整性**：审阅页面提交、连接回调、接口映射、后端状态机和真实登录判断。受控回归覆盖自动恢复；真实平台可行性仍受上述 P1 限制，公开主页样本不等于完整详情。
- **架构与合同**：Web→API→本机 Provider 沿用现有接口；未新增公共 schema、队列、存储或种子。审阅 `main.py` 的连接/报告端点以及 `benchmark_jobs.py` 的独立本地任务边界，合同测试覆盖接口配套。
- **权限与隐私**：审阅 `require_local_auth_access` 的本机所有者、回环地址和 Origin 检查，Provider 的本机限制、JSON 大小限制及 no-store。手动验证只接受精确小红书 HTTPS 来源及可视浏览器，登录以当前访问者身份核验；不导出 Cookie 或放宽媒体来源校验。采集登录不代表内容再利用授权；未新增第三方代码或依赖。
- **数据与并发恢复**：审阅取消、请求序号、显式点击排队、Provider 请求去重/过期清理；`benchmark_history.py` 仍使用工作区条件和调用方事务，付费任务幂等、恢复与数据库迁移未被改动。数据库备份恢复未实测。
- **性能与成本**：审阅 3 秒检查节奏、前端隐藏暂停、180 秒总等待、Provider 单任务及关闭期限；媒体采集的时长/大小上限和任务预算未被本轮修改。受控按钮测试未产生付费任务。未进行容量或外部供应商成本实测。
- **构建与运维**：审阅锁定依赖清单、API/Web/Worker/合同的 CI 门禁和连接 SOP；本轮本机构建成功，不代表全新环境供应链审计通过。失败码仍清洗原始异常，保留可区分的未配置、不可用、受限和过期状态。平台监控告警、生产恢复及完整 Docker 启动未验收。

仅停止了本轮创建的 4192 预览进程，确认该端口不再监听；没有停止主任务服务。以工作树文件 SHA-256 检查并发变化，保留所有已有、未跟踪和主任务新增文件；启动器等仍在主任务修改时，不把旧测试结果称为最终稳定发布结果。
