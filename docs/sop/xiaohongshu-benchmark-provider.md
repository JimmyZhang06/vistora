# 小红书对标分析 Provider SOP

> 状态：本机真实 Provider v1，2026-09-05 形成
> 适用范围：用户有权访问的小红书公开主页与公开笔记；不采集私信、收藏、关注列表或受限内容

## 1. 目标与分层

对标分析必须按证据深度逐层推进，任何上层都不能把缺失证据包装成已经分析：

1. **主页样本层**：获取首屏标题、形式、发布时间和互动量，形成账号总体与单条元数据报告。
2. **详情证据层**：通过用户已登录的本机受管浏览器，获取正文、当前互动量和媒体探针。
3. **媒体取证层**：将媒体以只读、隔离、有限保留的方式交给 Worker；短期 URL 不进入持久报告。
4. **多模态分析层**：Worker 生成镜头、OCR、ASR、静音和声音事件时间轴。
5. **策略报告层**：只基于实际证据生成开场、叙事、视觉、口播和节奏判断，并保留限制。

本机通道包含 1→5 层实现：详情探针保持独立，新任务接口负责安全媒体获取、CPU/云端多模态分析与 SQLite 持久化。代码存在不等于本次环境已通过真实端到端验收，跨账号修复必须另行完成第 7 节的三个真实主页验证。完整安装、预算与恢复流程见 [视频深析 SOP](benchmark-video-analysis.md)。

## 2. Provider 合同

### 2.1 输入

- `platform` 必须为 `xiaohongshu`。
- `profile_url` 必须是 `https://www.xiaohongshu.com/user/profile/{24位小写十六进制ID}`。
- `note_id` 必须是 24 位小写十六进制 ID，并且必须在该主页的有界扫描中被发现。
- 主页预览与账号报告接受 `refresh_note_identity: boolean`，默认 `false`；显式恢复受同一去重、串行与冷却限制，不会创建付费分析任务。
- 不接受客户端提供 Cookie、xsec token、CDP 地址或媒体 URL。

### 2.2 输出

`POST /v1/benchmark-notes/source-evidence` 返回：

- 账号 ID、笔记 ID、无查询参数的规范笔记 URL；
- 页面可见标题、正文、点赞、收藏、评论及其精度语义；
- 视频/图文类型、视频时长和分辨率或图片数量；
- `trusted_media_origin` 和明确的采集限制。

响应必须符合 `benchmark-note-source-evidence.schema.json`，并设置 `Cache-Control: private, no-store`。

主页快照的 `acquisition.discovery_version="1"` 标识身份发现协议。`note_identity_status` 为 `complete`、`partial` 或 `unavailable`，同时返回 `identified_note_count`、`unresolved_note_count`、`identity_error_code`。这些字段仅描述有界样本中的身份完整性，不证明全部历史已采齐或每条笔记均可访问。每条笔记的 `identity_status` 为 `verified` 或 `missing`；缺失 ID 必须保留 `null`。

DOM 卡片可能没有发布时间或可靠的媒体类型，此时分别返回 `published_at=null`、`format="unknown"`，统计计入 `analysis.unknown_count`。不能从 ID 推造发布日期，不能把未知类型默认为视频，也不能把未知日期计入最近 30 天发文数。

### 2.3 永不输出

- Cookie、Authorization、登录用户资料；采集/分析接口不返回二维码，独立连接接口只向本机用户短期展示 PNG 二维码，见 [连接流程](xiaohongshu-connection.md)；
- xsec token、签名 CDN URL、完整网络响应或浏览器 storage state；
- 评论正文、推荐流、私信、关注列表；
- 未经证据支持的完播率、曝光量、分享量或平台推荐因果。

## 3. 标准运行流程

1. 启动回环受管浏览器 Provider，地址只能是 `http://127.0.0.1:{port}`。
2. 将 `FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL` 指向该回环 Origin，启动更新后的 API/Web。
3. 在 `/benchmarks` 中检查连接；未登录时由用户点击生成二维码并在小红书 App 扫码。也可在持久化浏览器中登录后重新检查。验证码只能由用户处理；登录成功只恢复当前免费采集，不自动运行付费分析。
4. API 先尝试公开 SSR；来源失败或解析结果缺少可核验笔记身份时，自动连接 Provider 的回环 CDP，只新建临时页签。已登录页的 SSR 仍缺身份时，使用受限同页卡片链接发现。
5. 详情 Provider 打开规范主页，在最多 12 次滚动内查找目标笔记。
6. Provider **点击页面内链接**，不把含 xsec token 的 href 读出页面。
7. 跳转后校验最终主机和 `/explore/{note_id}` 路径，再提取 DOM 证据。
8. 详情接口只做媒体探针；深析任务另在同页校验作者和笔记后提取目标视频流，用 HTTPS、公网 IP 固定及限量下载进入研究隔离区。媒体 URL 不出现在客户端或报告中。MSE 为 `blob:` 时，探针以同页媒体响应类型与来源判定。
9. 临时页签在成功或失败时都必须关闭；持久化登录上下文和用户已有页签不得关闭。

## 4. 状态和错误恢复

| 错误码 | 触发条件 | 操作 |
| --- | --- | --- |
| `BENCHMARK_PROVIDER_UNAVAILABLE` | 回环服务、CDP 或 Playwright 不可用 | 检查 Provider 进程；可重试 |
| `BENCHMARK_AUTHENTICATION_REQUIRED` | 浏览器未启动、无上下文或跳到登录页 | 让用户重新登录，禁止自动绕过 |
| `BENCHMARK_NOTE_NOT_IN_SAMPLE` | 有界滚动内未找到笔记 | 刷新主页；确认笔记属于账号；不要无限滚动 |
| `BENCHMARK_NOTE_MISMATCH` | 最终 URL 与请求笔记不一致 | 立即失败；检查页面结构变化 |
| `BENCHMARK_SOURCE_CHANGED` | 标题或关键 DOM 不存在 | 更新选择器并补回归样本，禁止返回旧快照冒充成功 |
| `BENCHMARK_SOURCE_UNAVAILABLE` | 平台返回 429/5xx | 尊重限流，指数退避，不并发轰炸 |
| `BENCHMARK_NOTE_IDENTITY_INCOMPLETE` | 卡片可展示，但仍有身份字段缺失 | 使用「补全笔记信息」恢复；只选择可核验的新卡片，不手填 ID |
| `BENCHMARK_NOTE_IDENTITY_CONFLICT` | 同卡片结构字段与链接身份冲突 | 停止该次发现，更新脱敏结构样本和回归；禁止按标题猜测 |

恢复成功只更新当前账号的快照。切换账号或笔记后，Web 必须忽略旧请求返回；若原卡片没有 ID，不能因为新卡片同标题或同序号就续接媒体请求，需选择已核验的新卡片。已核验的卡片只有在 `profile_user_id + note_id` 一致时才可合并和继续原请求。

完整快照缓存最多 300 秒，不完整快照最多 10 秒。恢复请求可绕过完整缓存，但仍受 10 秒补采冷却、32 个排队请求上限与 30 秒排队等待上限约束，防止连续点击造成重复浏览器采集。公开主页请求设置 8 秒 socket 超时；受管浏览器的状态查询、连接、导航、等待到提取按剩余 8 秒预算检查。主页 CDP 操作另有独立子进程监督期限（调用预算加 5 秒，最高 65 秒），详情/媒体采集期限 115 秒，最终进程回收最多再等 5 秒。`evaluate/new_page/close` 卡死时终止本次客户端和驱动，恢复 API 通道；不终止既有受管浏览器，因此无法承诺 CDP 失联时临时页签必然关闭。30 秒仅是排队边界，不是总请求期限。部分失败返回已获得的元数据与具体恢复错误，不用旧账号的报告填补结果。

受管页面必须等到出现可核验结构身份或受限 DOM 卡片链接再投影，不能只等 basicInfo：真实页面可能先呈现缺 ID 的 SSR 卡片，随后 hydration 才补充身份。合并后样本仍最多 64 条，优先保留独立已核验记录，剩余缺身份记录保持缺失并重新编号；被截断部分不参与样本统计。重复取消仍须等待底层采集完成，不能提前释放串行锁。

## 5. 选择器与兼容策略

当前真实页面验证过的优先选择器：

- 标题：`h1.title`
- 正文：`.note-content .note-text`
- 互动栏：`.interactions.engage-bar`
- 点赞：`.like-wrapper .count`
- 收藏：`.collect-wrapper .count`
- 评论：`.chat-wrapper .count`
- 视频：`video`

选择器失效时，Provider 必须 fail closed。修复流程是保存脱敏 DOM 结构、更新解析器、补测试、重新执行真实账号验证；不得用固定文案、Seed 或系统样片替代。

身份发现目前仅支持有页面证据的卡片结构：`noteCard.noteId`、同一外层卡片的 `id`，以及同一 `section.note-item` 内的规范笔记链接。ID 严格限制为 24 位小写十六进制；同卡片作者与请求的主页账号必须一致。不同来源给出不同 ID 时明确失败。不要增加按标题、数组索引或未经验证的字段名称猜测身份的分支。

短期签名链接仅在原页面中用于点击，离开浏览器页面的发现结果只能包含无查询参数的身份与已筛选元数据；状态字段经白名单投影，不读取完整 HTML；DOM 发现最多 64 张卡片，不返回 href、token、完整 DOM 或网络响应。仅 DOM 发现的卡片作为独立有身份记录加入，不能给缺失 ID 的 SSR 卡片按位置配对。

## 6. 安全、隐私与权利

- 回环 Origin、CDP 主机、平台主机、最终路径和媒体来源都要分别校验，防止 SSRF 和越权跳转。
- Provider 串行执行浏览器任务，限制滚动次数、导航超时和响应大小。
- 对标内容只能进入隔离的研究证据域，不得默认标记为 `owned`、`licensed` 或可进入素材库。
- 研究任务的源视频在流程结束后清理；派生证据保留 7 天，具备来源哈希及专用目录配额。停机期间不执行清理。
- 日志只记录错误码、阶段、耗时和脱敏 ID；禁止记录 URL 查询参数和页面完整正文。
- 使用前应核对平台条款、适用法律以及账号主体的使用授权。

## 7. 验证清单

每次 Provider 或选择器变更至少验证：

```powershell
.\.venv\Scripts\python.exe -m ruff check apps/api/src apps/api/tests
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_benchmark_accounts.py -q
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_benchmark_discovery_recovery.py apps/api/tests/test_benchmark_note_identity.py -q
.\.venv\Scripts\python.exe -m pytest tests\contract -q
```

以上命令从仓库根目录执行；API、Worker、契约套件分开调用，避免同名 `conftest.py` 导入冲突。根目录没有虚拟环境时使用本机已安装的受信 Python，显式设置本 worktree 的源码路径，不能因解释器来自旧 checkout 而导入旧实现。

`tools/verify_benchmark_discovery.py --profile <规范主页> --browser-origin <受管浏览器回环地址> --details` 可执行低频真实主页及视频/图文详情验证；可重复传入明确允许的主页。输出只有校验结果、ID、数量及探针，不保存完整页面或创建付费任务。离线 DOM 测试在全拦截网络的独立 Chromium 中执行，API CI 安装对应浏览器；不能将这些 fixture 结果称为平台端到端。

真实冒烟请求使用已登录账号可见的主页和笔记 ID，检查：

- 返回标题与页面一致；
- 视频时长、宽高合理；
- `acquisition_method=authenticated_managed_browser`；
- 序列化响应中不出现 `xsec`、`xhscdn`、`cookie`；
- 退出后临时页签被关闭，用户登录仍然有效。

跨账号发布门禁至少包含三个用户允许低频访问、结构不同的真实主页，逐个记录规范账号 ID、时间、结构变体、发现数量、缺失数量、详情作者/笔记核验结果及视频/图文分支。每个真实主页要从输入 URL 走到所选正确笔记的详情；至少一条受支持视频连接实际 Worker 报告，图文不得进入仅视频引擎。未执行、登录受限、详情下架或缺少另外两个允许验证的主页时逐项记为 skipped/blocked；离线 fixture 测试不能替代这一门禁。

## 7.1 独立 worktree 接入检查

此次修复在独立 worktree 引入原 checkout 中稳定读取的最小 benchmark 基线；视频任务、Worker 及其原有测试属于基线引入，笔记发现、完整性、恢复交互与同步契约属于本次修复。无关的 launcher 端口改动及其测试期望没有引入。原 checkout 中仍有并发工作，不能将整个 worktree 覆盖回原目录。

接入前对原 checkout 和修复版本的相关文件重新比较，按文件审阅合并实际修复，保留并发改动；不要复制 `var/secrets`、Cookie、浏览器档案、环境密钥或本地任务数据库。先在独立端口、独立研究目录验证 Web/API 配对；读取实际浏览器请求地址，确认 benchmark 请求使用预期 API，业务 API 地址保持原配置。只有响应包含 `discovery_version="1"` 及身份状态字段才是新发现协议；缺字段应修正版本或地址，不能解释为小红书账号限制。合并后由运行原预览的任务协调重载，独立 worktree 通过不代表原来的 `4180/benchmarks` 页面已经生效。

## 8. 深析任务与尚未完成的生产能力

已新增独立耐久 `BenchmarkAnalysisJob`，不扩展当前详情响应：

1. API 创建幂等任务并冻结 `note_id`、来源证据哈希和预算上限；
2. Provider 将媒体流入本地研究隔离目录，不向客户端返回签名 URL；
3. Worker 执行容器验证、技术探测、场景扫描、抽帧、OCR、VAD/ASR；**本地通道未加入病毒扫描与 OS 沙箱**；
4. Worker 提交 `BenchmarkMediaEvidence`，API 生成并持久化 `BenchmarkDeepNoteReport`；
5. 单进程文件锁、SQLite 状态恢复、进度、重试、取消、保留期；Windows Job Object 支持主进程强杀后的子树回收。生产租约、分布式调度、审计删除事件仍未接入。

只有本次实际处理的各阶段返回成功才能显示完成；任何缺失项显示 partial。抽样分析不等于逐帧穷尽，也不等于爆款因果证明。
