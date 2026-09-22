# EHAI Control Plane Client

当前仅包含从 [schemas/v1](../schemas/v1) 生成的严格 TypeScript API Client；
尚无 Dashboard、页面路由或浏览器状态管理。[STATUS](../docs/STATUS.md) 记录实际交付。

P3.1 已提供 `listProjects()`、`getProject(projectId)`、`getRuntimeContext()`，
返回按宿主的项目总览、目标/Run/有效 Gate 成果与当前执行上下文。
P3.2 已提供 `listInbox({ projectId, runId })`、`getInboxItem(kind, requestId)`；
根据待办 actions 调用既有源操作，提交后重读详情及 Run，并显示 Worker 来源可用性。
便签、持久事件消费、项目配置已提供正式契约与生成 Client，新增方法包括 createNote/addNoteMessage/decideNote、
registerEventConsumer/readConsumerEvents/acknowledgeConsumerEvents、configureProject/getRunConfiguration。
薄 Web 工作台之后单独设计与实现，不承担便签决定、批准或执行接续的业务编排。
`EhaiWorkspaceManagerClient` 提供工作区登记/启动/停止/总览及执行配置读取；
`manager.workspace(workspaceId)` 返回绑定该工作区的 `EhaiApiClient`，包括 `getPlannerCapacity()`。
聚合结果保留来源与不可用状态，不声称多个核心之间具有同一事务快照。
具体范围及完成条件见 [路线图](../docs/ROADMAP.md)，不在此重复定义。
页面先呈现成果、待办和下一步，轨迹按需展开；顶层 Agent 仍在 EHAI 之外。

| 文件 | 职责 |
| --- | --- |
| [src/generated/ehai-client.ts](src/generated/ehai-client.ts) | 生成类型、EhaiApiClient、错误与 SSE URL helper |
| [src/index.ts](src/index.ts) | 稳定导出入口 |
| [scripts/generate-client.mjs](scripts/generate-client.mjs) | 从 JSON Schema/OpenAPI 生成 Client |
| [package.json](package.json)、[package-lock.json](package-lock.json) | npm 脚本与锁定依赖 |
| [tsconfig.json](tsconfig.json) | 严格 TypeScript 配置 |

在本目录运行：

~~~powershell
npm.cmd ci
npm.cmd run generate
git diff --exit-code -- src/generated/ehai-client.ts
npm.cmd run typecheck
npm.cmd run build
~~~

修改公开契约先更新 Schema，再生成 Client；不手改生成文件。
页面仅消费正式 API/事件，不访问 SQLite，不复制 Run、Attempt、Branch、Check 或 Gate 状态机。
“需要我处理”聚合不同来源的请求，动作回到各自业务入口；普通回复不隐式批准或扩权。
当前模型与未来页面严格分开，开发和验收遵循 [开发规则](../docs/DEVELOPMENT_GUIDELINES.md)。
