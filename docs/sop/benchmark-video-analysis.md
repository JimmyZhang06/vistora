# 对标视频真实多模态分析 SOP

## 1. 当前能力与边界

此实现是 **本机、单用户、单 API 进程的真实研究分析通道**，不是生产版分布式爬虫。输入小红书主页 URL 和该主页内发现的笔记 ID，自动生成一条耐久任务，再完成来源获取、媒体处理、模型分析与逐条报告。已有账号总体报告仍基于主页样本统计，**尚不汇总全部视频的多模态结论**。

链路：Web → API/SQLite 本地队列 → 已登录受管浏览器 Provider → 隔离研究文件 → Worker 子进程 → 证据与报告 → 按账号＋笔记恢复。

| 分析项 | 实现与证据 | 必须保留的限制 |
| --- | --- | --- |
| 视频技术信息 | FFprobe 实读容器、时长、画幅、音轨 | 容器检查不等于恶意文件扫描 |
| 视频画面 | FFmpeg 扫描全时长场景变化，保留开头 0/1/3 秒与均匀采样；Qwen 分析主体、动作、构图、光色 | 最多 36 帧；不是逐帧穷尽，运镜是抽帧推断 |
| OCR | RapidOCR CPU 实读每个采样画面，保留文字、置信度、位置框 | 短暂字幕、小字、遮挡和模糊可能漏检 |
| 语音 | Silero VAD；faster-whisper CPU int8，真实段级及词级时间码；配置的 ASR 服务作为补充/降级 | VAD 无语音不等于静音；唱词与口播尚未分离 |
| 配音表现 | 全音轨能量、峰值、动态、低能量区间；按转写时长算字速；VAD 区间内周期/声高候选 | 不判身份、性别、音色情绪或真人/合成；背景音乐会污染周期检测 |
| 叙事与策略 | Qwen 综合有界画面/OCR/ASR证据，输出时间段、观察、策略假设与证据键 | 不是爆款因果证明；不虚构曝光和完播率 |

API `ready` 只代表本次有界分析的配置阶段成功，不代表全量帧、全账号历史、逐字正确或策略因果已验证。任何阶段失败保留 `partial` 及限制。成功 OCR 返回空文本与 OCR 不可用是两种状态。

## 2. 安装与启动

前提：Windows/Python 3.12、仓库 API/Worker 基础依赖、PATH 中的 FFmpeg/FFprobe，以及已启动的兼容受管浏览器 Provider。可在 `/benchmarks` 内连接小红书并扫码登录；无需重复复制 Cookie、CDN URL 或上传视频。登录过期仍需用户扫码或处理平台验证，不能保证永久无人介入。详见 [连接流程](xiaohongshu-connection.md)。

首次安装可选多模态依赖（锁含直接及传递依赖 SHA256）：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r services/worker/requirements-benchmark-lock.txt
```

Whisper 使用本机已缓存的 `small` 模型。受管任务强制禁用模型下载，即使 Provider 环境文件设置 `FRAMEFACTORY_BENCHMARK_ALLOW_MODEL_DOWNLOAD=true` 也不在任务内下载，避免缓存目录绕过任务磁盘预算。新机器须在任务之外单独初始化模型缓存并规划容量，或固定已准备好的本地模型路径；没有缓存时明确报告缺失，或使用配置的云转写，不造时间戳。

在忽略提交的 `var/secrets/worker-provider.env` 配置现有 Provider：

```dotenv
FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL=http://127.0.0.1:5556
FRAMEFACTORY_ASSET_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
FRAMEFACTORY_ASSET_VISION_MODEL=qwen-vl-max
FRAMEFACTORY_ASSET_VISION_API_KEY=<自行配置，不要提交>
FRAMEFACTORY_ASR_BASE_URL=https://api.siliconflow.cn/v1
FRAMEFACTORY_ASR_MODEL=FunAudioLLM/SenseVoiceSmall
FRAMEFACTORY_ASR_API_KEY=<自行配置，不要提交>
FRAMEFACTORY_ASR_RESPONSE_FORMAT=json
FRAMEFACTORY_ASR_TIMESTAMP_MODE=none
```

SenseVoice 当前接口返回全文，不能从全文直接伪造词级对齐。需要真实词级时间码时使用本地 Whisper；云端全文会单列来源和时码缺失。截图会发送给已配置视觉服务，必要时音轨会发送给已配置 ASR；操作前应确认这些服务的数据处理设置与使用权限。

完整本地栈：`./start.ps1 -BenchmarkAnalysis`（需受管浏览器 Provider 已经运行，登录可在页面内完成）。此开关不自动购买或启动外部浏览器服务。

也可在已运行 Web 的开发环境单独启动回环 API：

```powershell
$env:FRAMEFACTORY_XHS_MANAGED_BROWSER_BASE_URL='http://127.0.0.1:5556'
$env:FRAMEFACTORY_BENCHMARK_JOBS_DIR=Join-Path (Get-Location) 'var/benchmark-analysis'
$env:FRAMEFACTORY_BENCHMARK_PROVIDER_ENV_FILE=Join-Path (Get-Location) 'var/secrets/worker-provider.env'
.\.venv\Scripts\python.exe -m uvicorn framefactory_api.main:app --app-dir apps/api/src --host 127.0.0.1 --port 8210
```

Web 的 API 地址必须匹配该端口。已有业务 API 时可在 `apps/web/.env.local` 仅设置 `NEXT_PUBLIC_FRAMEFACTORY_BENCHMARK_API_URL=http://127.0.0.1:8210`，不改动其它业务 API；未设置则随主 API。变更公开环境变量后重启 Web。不要使用 `--workers` 多进程；第二个进程会因目录所有权锁而失败。生产环境配置会拒绝启用该通道；默认工作区上下文不提供公网身份认证。

## 3. 使用与恢复

1. 打开 `/benchmarks`，输入主页并生成账号报告。可展示标题与点赞不代表笔记身份已完整；自动补采仍不完整时使用「补全笔记信息」入口。身份状态与恢复边界见 [Provider SOP](xiaohongshu-benchmark-provider.md)。
2. 选择已核验笔记，经过「补全笔记信息 → 获取媒体 → 分析 → 报告」实际阶段。缺失身份、未知媒体类型、图文、无权限或下架各自显示真实状态；未知类型先取详情确认，图文不能送入仅视频 Worker。只有媒体与视频类型已核验后才可开始完整视频分析。Web 生成幂等键，重复点击不重复计费执行同一活动笔记。
3. 离开页面不取消任务；回来恢复同账号、同笔记任务，不展示其它笔记的最新报告。
4. 查看每阶段状态、代表帧及 OCR、音轨播放器、转写时间码、配音测量和策略引用。
5. `partial` 时查看具体缺失项，补齐后显式「重新分析」；`failed/cancelled/interrupted` 可显式重试，每任务最多 3 次。
6. 浏览器登录失败时已保存报告仍能读取；源媒体重新获取则需要重新登录。
7. `/benchmarks/history` 可搜索、按账号/视频类型筛选和翻页查看已保存报告。打开历史记录不重新采集或执行模型。账号报告在采集成功后保存；视频报告按任务及尝试归档。同一次尝试的最终状态修订会更新归档，重试产生新记录。
8. 历史正文独立于媒体保留期；媒体到期后只展示仍存在的派生产物，并明确提示预览不可用。归档写入失败时，当前账号报告仍可查看，但会显示未保存提示。

恢复不会按同标题或卡片序号选择付费任务；仅按工作区、账号 ID 与笔记 ID 复用现有任务边界。切换账号或笔记期间的迟到响应必须丢弃。账号标题规则报告继续标为 `public_metadata_only`，不能当作视频深析完成结果。Web/API 升级需配对检查 `acquisition.discovery_version="1"`；旧 API 缺字段应显示恢复版本不匹配，且保留业务 API 与 benchmark API 的独立配置。

接口见 `packages/contracts/openapi/v1.yaml` 的 `/v1/benchmark-analysis/*`。来源 URL 只接受平台规范主页，媒体下载验证笔记作者和 ID；不接受任意客户端媒体 URL。

## 4. 安全、隔离与预算

- 媒体 URL 仅在 Provider 进程内短暂存在；不返回签名地址、Cookie、xsec token 或完整网络响应。HTTPS CDN 域白名单、公网 DNS 校验及固定 IP/TLS SNI，禁止重定向。
- 源视频最高 200 MiB、10 分钟；1080p 优先。最多 36 帧、5 个视觉批次＋1 次证据综合、至多 1 次云 ASR；不自动重试付费模型调用。Whisper CPU 2 线程；Worker 30 分钟超时。
- 全局单执行通道、最多 10 个待执行/活动任务；研究目录默认总量上限 2 GiB，单次尝试最多 256 MiB。API 在采集和分析阶段约每 100ms 计量源媒体、派生文件及数据库，保留至少 32 MiB 磁盘余量；超限中止自有采集/Worker 进程并精确清理失败尝试，状态可显式重试。Worker 在写入前检查预算，FFmpeg 限制单帧 2 MiB、单声道 16kHz 音轨约 20 MiB 和最多 600 秒，JSON 原子替换也计入双份占用。定时检测不是内核磁盘硬配额，检测间隔与进程回收期间仍可能短暂超限；不删除其它文件腾空间。
- SQLite WAL/FULL、幂等键映射、每次尝试独立目录、JSON 原子提交。正常取消停止 Worker 进程树；Windows 使用 Job Object 和启动握手，API 强杀会回收 Worker 及其子树（实际硬杀测试通过）。API 重启把未完成任务标记 interrupted，需显式重试。非 Windows 的强杀回收尚未验证。
- 回环浏览器获取用有界导航/滚动与下载期限；真实 Provider 采集进入可终止的独立子进程，API 取消或容量监控可要求终止，底层 CDP/DNS 卡死不会永久占用本次客户端进程树。嵌入方自定义三参数采集回调为兼容保留，必须自行遵守期限；新回调应接受 `cancel_event` 并及时退出，不能把真实 Provider 的进程回收保证扩展到任意自定义回调。
- 新任务的源 MP4/临时片在流程结束后精确清理；失败或取消尝试的派生文件在写入者回收后一起清理，已完成尝试的帧和音轨保留最多 7 天，在服务启动及周期检查时清理。SQLite 历史正文不会随媒体 TTL 删除；它计入目录预算，尚无归档删除/配额管理界面，长期使用需监控容量。停机期间不会运行清理。不要存放用户其它文件到这个专用目录。
- 对标证据不进入素材库，不自动授予 owned/licensed 状态，不提供源 MP4 下载，不采集评论正文或私信。研究用途不等于获得创作者作品再利用许可。
- 全部 API/产物返回 private/no-store；路径与工作区检查、产物白名单、禁止穿越。浏览器写操作额外校验 Origin，公网部署仍被禁止。
- 本地处理还没有恶意媒体扫描与 OS 沙箱；不得将此路径扩展成公开上传服务。正式上线需要已鉴权网关、隔离执行器、扫描、生产队列/对象存储、预算账本和审计事件。

## 5. 备份、升级与验证

停止 API 且无活动任务后，完整备份专用 `var/benchmark-analysis`（含 SQLite 与派生目录）；恢复到新目录并启用单个 API。不要只复制数据库而丢失帧文件。SQLite 当前支持 schema 0/1/2，未知版本拒绝启动；实际备份恢复演练须列为发布门禁。

```powershell
.\.venv\Scripts\python.exe -m pytest apps/api/tests/test_benchmark_accounts.py apps/api/tests/test_benchmark_jobs.py apps/api/tests/test_benchmark_job_routes.py apps/api/tests/test_benchmark_media_acquisition.py -q
.\.venv\Scripts\python.exe -m pytest services/worker/tests/test_benchmark_analysis.py -q
.\.venv\Scripts\python.exe -m pytest tests/contract -q
.\.venv\Scripts\python.exe -m ruff check apps/api services/worker/framefactory/worker/benchmark_analysis.py services/worker/tests/test_benchmark_analysis.py
```

在 `apps/web` 运行 `npm test`、`npm run lint` 和 `npx tsc --noEmit`。最终还必须用真实账号分别完成一条无口播和一条有口播视频，从 Web 创建到 API 取证、Worker 推理、报告、产物预览和重启恢复全链路验证；模拟 Provider 测试不能代替这一项。

机器、模型、依赖或代码变化后应重新验证。FFmpeg 版本随技术信息记录；云模型别名与本地缓存模型尚未固定权重修订，无法保证跨日输出完全一致。

## 6. 调研依据

- [faster-whisper 官方实现与 VAD/词级时间戳](https://github.com/SYSTRAN/faster-whisper)
- [RapidOCR 官方使用文档](https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/usage/)
- [FFmpeg 官方滤镜文档](https://ffmpeg.org/ffmpeg-filters.html)
- [阿里云 Qwen-VL 兼容接口](https://www.alibabacloud.com/help/en/model-studio/qwen-vl-compatible-with-openai)
- [SiliconFlow 官方 OpenAPI（ASR 返回结构）](https://github.com/siliconflow/siliconcloud/blob/main/openapi.yaml)

这些选择基于可执行能力与时码真实性；未将第三方下载站当作稳定 API 或授权来源。
