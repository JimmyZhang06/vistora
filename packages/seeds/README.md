# Vistora 官方 Seed

> 文档状态：当前声明式目录说明
> 导入边界：经 manifest 和公共契约校验；不执行任意代码

Seed 是经过审查的声明式启动数据，不是 Worker 插件，也不会获得执行代码权限。

`official-skills/v1/` 保存官方 Skill 起点，以及 `standard-production`、`full-ai-production` 和 `webpage-video-production` 的版本化 Pipeline。官方与用户资源使用相同契约、Repository、API 和执行路径；`ownership_type=system` 与 `publisher_type=system` 只是数据属性，运行时代码不得按固定业务 ID 分支。

标准 Pipeline 组织研究、写作、配音、素材选择、渲染、质量检查和人工审核。Seed 只声明能力要求；实际能力是否可用由 Worker 部署配置决定。缺少 Provider 时步骤 fail closed，不会因为资源是“官方”而绕过。

## 边界

- 不复制 `_research`、原始语料、用户媒体、运行产物、密钥或旧账号目录。
- 写作指导只保留人工整理的方法摘要，旧公式和案例文档属于迁移证据。
- UUIDv5 按 manifest 声明的 namespace/key 规则生成；代码不得依赖具体 UUID。
- `content_hash` 按契约规定的 RFC 8785 JCS 域计算。
- manifest 是打包和导入入口；缺失时不能启用隐藏内置 Skill。
- 已发布的旧 PipelineVersion 保持不可变；新行为通过新版本文件发布。

只生成幂等导入计划：

```powershell
.\.venv\Scripts\python.exe tools/migrate-v1/skill_seed_plan.py
```

使用 `--output <path>` 只会保存供审核的计划文件。工具没有数据库 apply 模式，也不会移动旧文件。生产导入由 API 启动/迁移路径在契约校验后完成。
