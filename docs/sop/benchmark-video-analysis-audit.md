# 真实视频深析交付审计 · 2026-09-05

## 结论

**本机 Windows 单用户试用可用；生产发布不通过。** 已完成真实账号→媒体→多模态 Worker→持久报告→Web 适配器与产物读取，不是 Mock 成功。当前是全时长场景扫描＋最多 36 张代表帧 OCR/视觉分析，以及完整音轨测量；不代表逐帧无遗漏或准确识别所有口播。

本次保留了已有及并行工作区修改，没有 reset、清理或回滚无关代码。构建遵循既有 Sites/Vinext 结构；未变更 hosting 身份、D1/R2 或发布站点。本机受管浏览器、Python/FFmpeg 和登录会话不能直接部署到 Cloudflare Worker。

## 真实验证结果

| 目标视频 | 时长 | OCR/视觉代表帧 | 本地 ASR 时码 | 叙事段 | 任务 |
| --- | ---: | ---: | --- | ---: | --- |
| 夏天的最后手搓一个薄荷绿 | 17.783 s | 10/10 | VAD 未发现语音，未伪造转写 | 8 | ready |
| 16 个旅行转场 | 114.617 s | 36/36 | 7 段、18 个词级条目 | 4 | ready |
| 普通女孩的二十年 | 269.466 s | 36/36 | 2 段、23 个词级条目 | 7 | ready |

以上条目数是机器输出数量，**不是识别准确率**；背景音乐/唱词可能被识别为语音，少量转写不能证明全片没有其它口播。没有人工完整标注集，转写质量仍待核对。观察和策略解释来自模型；“爆款原因”只可作为待验证假设。

- 实际 Qwen 调用成功，报告有具体画面描述、OCR 原文、时间码及证据帧引用。
- 额外调用已配置的 SenseVoice 云转写真实成功，返回 268 字全文、0 个段/词时间戳；系统明确标记 `text_only`，没有用其全文伪造时间轴。这是单独的 Provider 冒烟，不冒充上述任务执行时使用了云 ASR。
- 另用明确标识的合成中文语音验证真实本地 Whisper：3 段、38 个词级时间戳与周期测量；该样本不作为博主结论。
- 3 份真实任务响应通过 canonical job Schema；检查未输出 `xsec_token=`、CDN 签名地址、Bearer 或凭据字段。
- 成功任务源 MP4 已清理；代表帧真实读取 200/JPEG，音轨 Range 请求返回 206/RIFF，均 private/no-store。
- 重启 API 后真实报告仍可通过 Web 生产适配器恢复，首帧实际时间码为 0。备份到独立临时目录并恢复 SQLite，57 个已完成产物哈希一致、报告一致；没有改写原研究存储。
- Windows 独立 API stub 强杀测试确实回收 Worker 和孙进程，恢复 SQLite 后标 interrupted、重试成功。正常取消、幂等、重试和权限测试另行覆盖。

## 检查清单

| 检查/命令 | 状态 | 结果及未验证项 |
| --- | --- | --- |
| `python -m pytest apps/api/tests -ra -o addopts='' -q` | 通过＋跳过 | 335 passed，4 skipped；3 项真实 PostgreSQL、1 项 MinIO 未配置测试地址 |
| `python -m pytest services/worker/tests -q` | 通过＋跳过 | 514 passed，1 skipped，116 subtests；旧 Edge/素材目录联调未启用 |
| `python -m pytest services/worker/tests/test_benchmark_analysis.py -ra -o addopts='' -q` | 通过 | 14 passed，真实 FFmpeg/OCR/语音算法辅助测试 |
| `python -m pytest tests/contract -q` | 通过 | 50 passed，OpenAPI 路径与 Schema 引用/合同门禁 |
| `npm test`（apps/web） | 通过 | Vinext 完整构建＋60 项测试 |
| `npm run lint`（apps/web） | 通过 | 零警告 |
| `npx tsc --noEmit`（apps/web） | 失败 | 6 个原有诊断，位置列于 P1-1；本次 benchmark 代码无类型诊断 |
| `python -m ruff check apps/api services/worker/framefactory/worker/benchmark_analysis.py services/worker/tests/test_benchmark_analysis.py` | 通过 | 本次 API/Worker 代码与测试检查 |
| `git diff --check`、`python -m pip check` | 通过 | 无补丁空白错误、无依赖版本冲突 |
| 可选依赖锁 `pip install --dry-run --ignore-installed --only-binary=:all: --require-hashes -r services/worker/requirements-benchmark-lock.txt` | 通过 | 全部直接/传递依赖已锁定版本与 SHA256；不等同于漏洞扫描 |
| `start.ps1` PowerShell 语法解析 | 通过 | 新开关及配置语法通过；完整 Docker 栈启动未重新执行 |
| 三条真实媒体任务＋实际 Web 适配器＋JPEG/WAV 读取＋持久恢复 | 通过 | 来源和模型未用 Mock 替代 |
| 全 UI 浏览器自动回归 | 跳过 | 做过前期页面连接检查并修正旧 API 地址；最终以生产适配器真实 HTTP/渲染测试与用户可用入口验收，没有宣称完整浏览器操作测试覆盖 |
| 生产部署/鉴权网关/Redis/对象存储集成 | 阻塞 | 该新通道明确本机限定，未冒充生产分布式链路 |

曾尝试把 API 与合同目录合并到一次 pytest 调用，遇到两个 `conftest.py` 同名导入冲突；已按仓库原有独立命令重跑，不将该错误隐藏为产品通过。真实长片首次发布报告时发现 PCM 重采样尾部比视频时钟多 2 ms；已限制性裁剪最多 50 ms 的尾部差异并补反例测试，再通过真实重试完成。

## 按严重性排列的后续事项

### P1-1 · 完整 TypeScript 门禁仍失败（已确认，原有问题）

触发：`npx tsc --noEmit`。影响：不能宣称仓库通过所有发布门禁。

- [run-detail.tsx](C:/Users/Zhang/Desktop/开发项目/vistora/apps/web/components/run-detail.tsx:256)：summary 可能 undefined。
- [settings-view.tsx](C:/Users/Zhang/Desktop/开发项目/vistora/apps/web/components/settings-view.tsx:22)：主题对象的 label 字段不符合声明类型（22–24 行）。
- [skill-creator.tsx](C:/Users/Zhang/Desktop/开发项目/vistora/apps/web/components/skill-creator.tsx:137)：已收窄的联合类型仍比较 distill。
- [mock-adapter.ts](C:/Users/Zhang/Desktop/开发项目/vistora/apps/web/lib/api/mock-adapter.ts:994)：旧网页视频选项缺少 4 个 crawl limit 字段。

建议作为独立、针对这些既有业务页面的修复处理；本次未为得到绿色结果而改动无关功能。

### P1-2 · 不能公网开放本机研究通道（已确认）

触发：把回环 API、受管浏览器或本地分析器暴露到多用户/公网环境。影响：默认上下文不验证来访者身份，本机 FFmpeg/原生推理没有恶意文件扫描和 OS 沙箱。

证据：[默认上下文](C:/Users/Zhang/Desktop/开发项目/vistora/apps/api/src/framefactory_api/context.py:38)；[设置保护](C:/Users/Zhang/Desktop/开发项目/vistora/apps/api/src/framefactory_api/settings.py:64) 已 fail closed 禁止 production 配置；[本地媒体入口](C:/Users/Zhang/Desktop/开发项目/vistora/services/worker/framefactory/worker/benchmark_analysis.py:591)。

建议：正式部署前增加真正鉴权/授权、隔离执行器与扫描，并接入生产队列、对象存储、付费预算账本、监控与审计事件。现有 Origin 检查、目录隔离与权限位不替代上述措施。

### P2-1 · 采样及声音分离限制（已确认）

触发：短暂字幕、快切、复杂背景音乐、唱词或低声口播。影响：可能漏 OCR/语音，声高候选可能受音乐污染；当前没有人声/音乐分离、说话人识别或配音真假鉴定。账号总体报告也尚未汇总每条视频的深析证据。

证据：[声音测量](C:/Users/Zhang/Desktop/开发项目/vistora/services/worker/framefactory/worker/benchmark_analysis.py:417)、[采样范围](C:/Users/Zhang/Desktop/开发项目/vistora/services/worker/framefactory/worker/benchmark_analysis.py:658)、[总体报告范围](C:/Users/Zhang/Desktop/开发项目/vistora/docs/sop/benchmark-video-analysis.md:5)。建议下一步建立人工标注的口播/音乐混合评测集，按预算增加字幕变化采样或声音分离，而不是把成功状态解释为准确率保证。

### P2-2 · 可复现性与运维边界（已确认）

触发：更换机器、云模型别名更新、DNS 极端阻塞、非 Windows 主进程强杀。影响：结果可能漂移、采集槽位超时后仍被占用，非 Windows 崩溃回收未经验证。

证据：[缓存模型](C:/Users/Zhang/Desktop/开发项目/vistora/services/worker/framefactory/worker/benchmark_analysis.py:497)、[线程采集](C:/Users/Zhang/Desktop/开发项目/vistora/apps/api/src/framefactory_api/benchmark_jobs.py:392)、[Windows 专用 Job Object](C:/Users/Zhang/Desktop/开发项目/vistora/apps/api/src/framefactory_api/benchmark_jobs.py:932)。建议固定模型修订，记录模型及 FFmpeg 供应链，增加采集进程隔离与 Linux 崩溃回收测试。当前 2 GiB/10 任务/6 次模型调用/30 分钟 Worker 上限已经落实，但不是跨用户财务预算控制。

### P2-3 · 嵌套分析契约仍可扩展（已确认）

触发：更换 Worker 或引入第三方消费者。影响：Job 外层与报告已严格建模，但 `analysis` 内部仍允许扩展 JSON；需要更细的稳定嵌套 Schema 与版本兼容测试。

证据：[嵌套分析载荷](C:/Users/Zhang/Desktop/开发项目/vistora/apps/api/src/framefactory_api/benchmark_job_models.py:55)。当前 Web 有类型映射、结果锚点验证及真实响应 Schema 冒烟；这些不等于所有新字段都具备契约强约束。

## 已审阅的控制及证据

- 用户路径：主页采集、真实笔记 ID、按账号＋笔记恢复、无结果/失败/部分完成/取消/重试与预览失败状态。
- 架构：独立 SQLite 研究域；不伪写业务 PostgreSQL/Redis/素材库；生产禁用；严格报告、版本化 Job Schema、旧全局缓存不再供新 UI 使用。
- 安全：规范主页、作者/笔记匹配、公网固定 IP/TLS CDN 下载、无跳转、无 Cookie、React 文本输出、模型输入不可信声明、时间/帧/引文校验、产物白名单与穿越拒绝。
- 完整性：WAL/FULL、幂等键别名映射、单进程锁、尝试级不可混用产物、原子 JSON、Windows 子树生命周期、源文件精确清理、备份恢复与哈希。
- 容量：200 MiB/600 秒来源、36 帧/6 视觉调用、1 云 ASR、CPU 限制、队列上限、磁盘预算、HTTP 超时/响应上限、7 天保留。
- 文档：SOP 已说明安装、凭据不入库、模型数据出站、登录过期、权利/来源、恢复、保留和部署边界；开源库来源链接可查。没有完成法律授权审查、完整 SBOM/CVE 扫描或生产告警演练，不声称这些方面已全面安全。

最后的代码快照比较覆盖 Web/API/Worker/契约与启动脚本；必要修复后重跑受影响测试。报告文件本身随后只读复核，不用先前绿色结果替代修改后的验证。
