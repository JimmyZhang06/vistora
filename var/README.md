# Vistora 本地运行数据

`var/` 是仓库内唯一的本地运行数据和私密数据边界。除本说明外，目录内容均被 Git 忽略，不属于源码或可部署配置。

```text
var/
├─ logs/         # API、Worker 与 Web 日志
├─ runtime/      # start.ps1 管理的进程状态
├─ secrets/      # 本机 Provider 配置和密钥
└─ test-inputs/  # 本地能力测试下载或制作的输入素材
```

`start.ps1` 默认读取 `var/secrets/worker-provider.env`，且只接受启动脚本列出的 Provider 字段。不要提交这里的密钥、日志、缓存、下载素材、分析中间文件或视频产物。

不要在 `var/` 放源代码、正式迁移、官方 Seed 或长期文档。需要长期保留的技术说明应放到相应组件 README 或 `docs/`；可复现测试夹具应去除版权/隐私风险后放到正式测试目录。
