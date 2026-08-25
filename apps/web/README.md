# Vistora Web

> 文档状态：当前组件说明
> 运行边界：HTTP adapter 连接真实 API；Mock 仅用于测试

Vistora Web 是 React 19 + Vinext 工作台。默认运行路径通过 typed HTTP adapter 连接 Control API；Mock adapter 只用于测试 fixture，不会在 API 失败时伪造生产数据。

## 启动

仓库根目录的 `start.ps1` 是完整本地环境的首选入口。单独开发 Web 时：

```powershell
Set-Location apps/web
npm ci
$env:NEXT_PUBLIC_FRAMEFACTORY_API_URL = "http://127.0.0.1:8200"
npm run dev
```

要求 Node.js `>=22.13.0`。默认 Web 地址为 <http://localhost:4173>；实际端口由 Vinext 启动参数决定。

## 现行路由

- `/create`：创建单个视频任务。
- `/create/ai`：创建独立 Full-AI 生成任务。
- `/create/webpage-video`：创建网页发现、截图审核和成片任务。
- `/webpage-video/[id]`：审核页面范围、截图与 Storyboard，并查看后续成片状态。
- `/batches`：批量生成任务。
- `/projects`、`/projects/[runId]`：Run 列表、步骤、产物、审核和重试。
- `/skills` 及 `/skills/[skillId]/*`：创建、编辑、测试、发布、使用情况和版本历史。
- `/assets`、`/assets/[libraryId]/[assetId]`：素材库、明细、上传、分析和审核。
- `/channels` 及其新建/编辑路由：频道管理。
- `/settings`：账户与当前单工作区设置。

## API 边界

运行时工厂创建 `lib/api/http-adapter.ts`，浏览器默认请求 `http://127.0.0.1:8200`，可用 `NEXT_PUBLIC_FRAMEFACTORY_API_URL` 覆盖。HTTP adapter 负责资源映射、标准错误、幂等键、`If-Match` 并发控制和状态轮询。

素材、频道、Skill、Run、Step 和审核操作均使用真实 API。服务不可用时页面展示原始错误，不回退到 Mock 数据。公开部署不得使用本地 API 地址；必须先配置 HTTPS 身份网关、精确 CORS 来源和生产 Provider。

## 验证

```powershell
Set-Location apps/web
$env:Path = (Resolve-Path ..\..\.venv\Scripts).Path + ";" + $env:Path
npm test
npm run lint
```

`npm test` 会先完成生产构建，再执行路由、信息架构、可访问性和响应式契约测试。`.openai/hosting.json` 是 Sites 构建约定，不会替代后端部署或身份边界。
