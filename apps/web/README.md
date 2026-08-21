# Vistora Web

Vistora 新版前端壳层与 Skill Studio。默认运行路径通过 typed HTTP adapter 连接 `apps/api`；Mock adapter 仅保留为测试 fixture。

## Local development

```bash
npm install
copy .env.example .env.local
npm run dev
npm test
```

Node.js 需要 `>=22.13.0`。`npm test` 会先完成 Sites/vinext 生产构建，再验证核心路由、信息架构、可访问性与响应式源码契约。

## Routes

- `/create`：以主题为入口的组合式创作页
- `/projects`：项目与持久步骤状态
- `/skills`、`/skills/new`：Skill 列表与四种创建入口
- `/skills/:skillId/edit`：结构化 Skill 编辑器
- `/skills/:skillId/test`：双版本测试台
- `/skills/:skillId/versions`：不可变版本历史
- `/assets`、`/channels`、`/settings`：素材、发布频道、个人账户与工作区边界

## API boundary

页面只依赖 `lib/api/adapter.ts` 中的 `FrameFactoryAdapter`，运行时工厂固定创建 `http-adapter.ts`。浏览器默认请求 `http://127.0.0.1:8200`，也可通过 `NEXT_PUBLIC_FRAMEFACTORY_API_URL` 指向其他 API 地址。HTTP adapter 负责 snake_case 资源映射、标准错误、幂等键、草稿 `If-Match` 并发控制，以及 Skill 测试状态轮询；`mock-adapter.ts` 与 `mock-data.ts` 不会进入默认运行路径。

素材库支持本地文件上传，以及经版权确认的 YouTube、Bilibili、小红书公开视频链接下载入库；网络导入由控制 API 完成哈希校验和来源登记，不会回退到 Mock 数据。频道列表、创建、详情、替换与暂停/恢复直接调用 Channel HTTP 端点，更新请求携带 `If-Match`，服务不可用时保留并展示原始 API 错误，不会回退到 Mock 数据。Skill、版本草稿、校验、测试、发布、分叉、Run 创建、步骤状态、取消和人工审核同样使用真实 HTTP 请求。

`.openai/hosting.json` 保留 Sites 构建约定。只有生产 API、HTTPS 身份网关、精确 CORS 来源和执行供应商均已配置并通过发布验收后，才保存 Sites 版本并优先私有发布；本地默认 API 地址不会进入公开部署。
