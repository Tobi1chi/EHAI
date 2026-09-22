# Cross-Plane Schemas

`schemas/v1` 是核心与调用方之间的版本化公开契约。实际 HTTP 路由、模型工具、
CLI/MCP 参数和生成 Client 必须按各自调用边界与契约一致，内部模型不直接成为公共格式。
产品目标见 [范围](../docs/PRODUCT_SCOPE.md)，现有命令见 [Usage](../docs/USAGE.md)。

| 文件 | 内容 |
| --- | --- |
| [common.schema.json](v1/common.schema.json) | ID、时间、Project、Goal、计划/阶段、Run、Attempt、Artifact、Check 和 Worker 等共享定义 |
| [commands.schema.json](v1/commands.schema.json) | 规划、执行控制、人工请求与过程操作等 Command |
| [queries.schema.json](v1/queries.schema.json) | 计划、过程、运行、轨迹、成果与 Runtime 查询 |
| [events.schema.json](v1/events.schema.json) | 公开事件类型、envelope 与分页 |
| [plan-import.schema.json](v1/plan-import.schema.json) | 外部声明式计划导入 |
| [http-api.openapi.json](v1/http-api.openapi.json) | HTTP 路由、operation ID、请求、响应和 SSE |
| [notes.schema.json](v1/notes.schema.json) | 持久便签、消息、明确决定及其来源/结果 |
| [project-configuration.schema.json](v1/project-configuration.schema.json) | 项目规则版本、宿主绑定和 Run 配置快照 |

block 变化、Git 整合和后继 Run 的公开字段以这些文件和实际接口为准；
这里不维护另一份阶段实现清单。Responses-facing Schema 不使用 uniqueItems。
后续总览和待办的查询契约应先接核心与公开入口，再由 UI 消费，不预先生成未实现接口。

从仓库根目录更新契约和 Client：

~~~powershell
uv run control-plane/scripts/generate-api-schema.py
Set-Location control-plane
npm.cmd ci
npm.cmd run generate
npm.cmd run typecheck
npm.cmd run build
~~~

检查生成差异与预期接口一致；生成 Client 不手工编辑。破坏性契约变化需明确版本和迁移，
不能静默改变已有调用方语义。文档修改不要求无关地重新生成代码或运行模型。
实际失败的临时诊断与唯一产品 E2E 边界见 [开发规则](../docs/DEVELOPMENT_GUIDELINES.md)。
