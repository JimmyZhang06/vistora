# 中断任务续接审计（2026-09-06）

判定：跨账号代码已合入主目录，历史页面和归档兼容修复已完成；原 4180 Web 已热更新，但 8210 API 仍运行旧代码。运行切换及新版浏览器成功路径未完成，整体不可发布。

## 本次交付

- 续接「修复小红书对标采集的跨账号通用性」，来源为独立 worktree `C:/Users/Zhang/.codex/worktrees/651b/vistora`。按其增量补丁整合，没有用整个 worktree 覆盖主目录；保留主目录原有报告历史代码及所有无关改动。
- 采集支持可核验笔记身份、缺字段补采、显式未知类型/时间、缓存刷新与取消；详情校验账号和笔记一致性，返回数据不包含分享令牌或签名媒体地址。
- Web 保留账号切换/请求取消/报告恢复保护。补齐 `/benchmarks/history` 的搜索、类型筛选、分页、深链接、加载/失败/空态与只读报告查看。打开归档不重新采集或调用模型。
- 修复 SQLite 归档写入失败导致已生成报告丢失的问题：返回当前报告和保存警告。修复清理失败使任务从 ready 变 partial 后历史仍显示 ready 的问题：同一尝试更新原归档 ID 和最终警告。
- 补齐历史 OpenAPI/JSON Schema，以及账号报告历史字段，更新正文与媒体分开保留的 SOP。

## 当前真实验证

使用本主目录源码执行 `tools/verify_benchmark_discovery.py --profile … --browser-origin http://127.0.0.1:5556 --details`，仅验证用户此前指定的三个账号。结果见 [脱敏验证记录](cross-account-resume-real-validation.json)。

| 账号 | 已核验笔记 | 视频/图文 | 实际视频详情 | 实际图文详情 |
| --- | ---: | ---: | --- | --- |
| 夏天妹妹 | 30/30 | 21/9 | ID/作者匹配，84.700秒 | ID/作者匹配，5图 |
| 白昼小熊 | 32/32 | 30/2 | ID/作者匹配，269.467秒 | ID/作者匹配，14图 |
| 小可 | 31/31 | 3/28 | ID/作者匹配，37.292秒 | ID/作者匹配，8图 |

没有启动新付费分析。三个账号与六条详情不能证明所有页面变体、真实下架/无权限/登录失效路径均已覆盖。DOM-only 分支仍是隔离 Chromium fixture 证据。

原 `var/benchmark-analysis/jobs.sqlite3` 的只读检查为 schema=2、quick_check=ok、4条ready、0条活动任务。通过 SQLite backup API 制作一致性备份（包含 WAL 中的数据），没有修改或替换原数据库。

在独立临时副本中使用真实应用的 **进程内 TestClient** 验证：4份原有报告迁移至历史、列表/详情 JSON Schema、账号及笔记匹配、private/no-store、每份报告一个现存产物 HTTP 200；两次应用生命周期后仍为4条，未重复回填。测试没有监听新端口，也没有调用采集或模型。此证据不等于新版 API 的真实浏览器成功路径已通过。

实际浏览器 `4180` 验证：新历史页面可以打开，旧 API 缺少接口时明确提示重启/升级，保留重试按钮；账号页面在旧接口拒绝新请求时显示失败，已保存视频的恢复仍可读取旧服务原有报告。未将旧报告显示说成新分析成功。

## 检查结果

使用仓库 `.venv/Scripts/python.exe`，源码路径为当前主目录。源码、测试、契约、工具、工作流及启动器共445个文件做哈希核对；最终仅历史错误文案发生一次有意调整，随后重跑 Web 构建/测试及 lint。API全量验证前后77个文件哈希一致。真实Provider验证涉及源码在最终审计时未变。

| 命令/检查 | 状态与结果 |
| --- | --- |
| `python -m pytest apps/api/tests -o addopts= -q -ra` | **passed：421；skipped：4**，3个PostgreSQL、1个MinIO缺测试环境变量 |
| `python -m pytest services/worker/tests -q -ra` | **passed：514 + 116 subtests；skipped：1**，缺真实Edge/FFmpeg联调开关 |
| `python -m pytest tests/contract tools/tests -q -ra` | **passed：85** |
| `npm test`（包括 `vinext build`） | **passed：72**，最终文案改动后重跑通过 |
| `npm run lint` | **passed**，零警告 |
| `python -m ruff check apps/api services/worker/framefactory/worker/benchmark_analysis.py services/worker/tests/test_benchmark_analysis.py tools/verify_benchmark_discovery.py` | **passed** |
| `npx tsc --noEmit --incremental false --pretty false` | **failed**，6条已知原有诊断，见下方 |
| `npm audit --json` | **failed**，4个moderate，0 high/critical，开发依赖旧esbuild链 |
| `git diff --check`、`start.ps1` PowerShell语法解析 | **passed** |
| 三账号主页/六详情、真实旧报告副本迁移和产物读取 | **passed**，范围如上 |
| 8210运行切换、新版Web→API成功路径 | **blocked**，自动审批拒绝进程管理，旧API未重启 |
| Linux CI、全栈PostgreSQL/Redis/S3发布与灾备、OS媒体沙箱 | **skipped / unverified**，本次未执行；本地SQLite副本不能代替生产灾备 |
| 新源码完整云端视觉/策略分析、有/无口播完整E2E | **skipped / unverified**，未新建付费任务；历史报告不替代重新验收 |
| 干净环境 `npm ci`、Python锁跨平台重装与模型权重固定 | **skipped / unverified**，本次复用已安装依赖，不能据此宣称全新安装可复现 |

## 剩余发现（按严重度）

1. **P1，确认，运行版本不一致**：新Web请求包含 `refresh_note_identity`，旧8210仍拒绝新请求且没有历史路由，导致主预览不能完成新账号/历史操作。需在无活动任务并保留数据库后重启研究API，再验收成功路径。[请求](../../apps/web/lib/api/http-adapter.ts:819)、[历史路由](../../apps/api/src/framefactory_api/main.py:1056)、[启动方法](benchmark-video-analysis.md:51)。本次强制结束原进程树和独立后台服务启动均被自动审批拒绝，只返回“blocked by policy”，没有提供更具体原因；未继续绕过进程管理限制。
2. **P1，确认，发布验收不足**：本地研究通道在production配置下被拒绝启用；尚无本轮生产基础设施、完整模型及新版浏览器成功路径证据。不能公开部署或称生产就绪。[开发模式限制](../../apps/api/src/framefactory_api/settings.py:64)、[分析与安全边界](benchmark-video-analysis.md:70)。
3. **P2，确认，既有TypeScript门禁失败**：6条诊断涉及可能为空的summary、主题选项类型、多余的模式分支及网页视频mock缺少爬取限制字段。[run-detail](../../apps/web/components/run-detail.tsx:256)、[settings-view](../../apps/web/components/settings-view.tsx:22)、[skill-creator](../../apps/web/components/skill-creator.tsx:137)、[mock-adapter](../../apps/web/lib/api/mock-adapter.ts:1006)。需修正后重新运行tsc。
4. **P2，确认，既有转写结论过强**：任何ASR文本都会进入高置信“口播”策略；实际旧音乐样本同时显示无法区分唱词的限制与高置信口播结论。应改成未分类语音候选或加入有证据的分类。[规则报告](../../apps/api/src/framefactory_api/benchmark_media_reports.py:247)。
5. **P2，静态推断，容量和卡死边界**：256MiB工作空间预留未覆盖最大源文件、36帧及完整音轨的叠加；CDP永久阻塞可能占据串行槽。需运行中配额及可终止采集工作进程，未注入真实卡死。[预留](../../apps/api/src/framefactory_api/benchmark_jobs.py:786)、[浏览器调用](../../apps/api/src/framefactory_api/benchmark_accounts.py:647)。历史正文计入目录预算但尚无删除/容量管理界面，长期使用需监控。
6. **P2，确认，既有开发依赖公告**：npm报告4个moderate，来源drizzle-kit的旧esbuild传递链；相关开发服务可能受跨源读风险影响。需兼容升级后验证；本次未执行强制依赖升级。[依赖](../../apps/web/package.json:32)。

## 审计范围与接续操作

审核了Web/API/Worker字段和媒体类型、旧响应/错误/归档兼容、账号和笔记身份核验、工作区权限过滤、输入及URL/产物边界、凭据投影与缓存头、SQLite事务/幂等/取消/重启/迁移、派生媒体保留和来源清理、采集串行/缓存/大小/队列/模型调用限制、依赖/CI/SOP。没有发现新的已确认P0；这不是对全项目所有运行路径的安全保证。生产鉴权、租户隔离和数据恢复不能以本机默认工作区的测试替代；研究证据不进入授权素材库，也不授予创作者作品再利用许可。

接续时先确认8210仍为原旧进程、且没有活动任务；关闭该研究API后，按 `benchmark-video-analysis.md` 第2节在原端口启动新版。不要停止8200业务API、4180Web或5556采集浏览器，不要覆盖原SQLite备份。然后验证OpenAPI含discovery_version和历史路由，打开历史页面确认4条原报告，完成真实账号补全/详情/账号切换/归档查看。最后在源码不再变化时更新本审计的运行阻塞结论。
