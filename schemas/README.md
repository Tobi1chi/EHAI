# Cross-Plane Schemas

`schemas/v1` 是 Execution Plane 与 Control Plane 之间经审核、带版本的公开契约。内部 Python 模型可以
演进，但公开字段、状态值和 route 必须先在这里明确，并与实际接口、生成 Client 保持一致。

本目录描述已实现的公开接口，不是完整产品范围；目标定义见
[Product Scope](../docs/PRODUCT_SCOPE.md)。R1 已新增规划讨论请求/查询、讨论事件与 PlanGraph 可选的
`design_document` 字段，并允许组合现有检查。旧计划缺少设计字段时按 null 读取，不改其批准状态。
执行阻塞的挂起与回复仍需后续迁移；CLI、顶层 Agent、UI 和未来 Routines 复用同一应用语义，
不能在各自 Client 中弥补执行状态规则。

2026-09-06 的多节点自动 Gate 沿用现有字段：PlanNode.required_check_ids 表示本节点的必需检查，
CompletionContract.required_check_ids 表示最终成果条件；CheckSpec 查询也可包含仅被中间节点引用的
局部检查。没有新增公开枚举、Phase 对象或人工 Gate route，不改变旧计划的检查配置。

## 文件

| 文件 | 内容 |
| --- | --- |
| [`common.schema.json`](v1/common.schema.json) | ID、时间戳、错误、Project、Goal、Plan、Run、Attempt、Artifact、Check、Worker 等共享定义。 |
| [`commands.schema.json`](v1/commands.schema.json) | Create/Propose/Discuss/Approve/Start/Pause/Resume/Cancel 及 Attempt/Worker Request Command。 |
| [`queries.schema.json`](v1/queries.schema.json) | Run、PlanGraph、ExecutionTrace、Check、Checkpoint、Artifact、Worker 与 Runtime health 查询响应。 |
| [`events.schema.json`](v1/events.schema.json) | 公开 Event envelope、Event type 与分页结果。 |
| [`http-api.openapi.json`](v1/http-api.openapi.json) | `/api/v1` HTTP route、operation ID、请求体、响应和 SSE 入口。 |

## 生成与验证

[`control-plane/scripts/generate-client.mjs`](../control-plane/scripts/generate-client.mjs) 读取四份 JSON Schema
和 OpenAPI，检查必需 operation ID 与重复定义，然后生成
[`control-plane/src/generated/ehai-client.ts`](../control-plane/src/generated/ehai-client.ts)。生成文件不手工编辑。

从仓库根目录运行：

```powershell
Set-Location control-plane
npm.cmd ci
npm.cmd run generate
git diff --exit-code -- src/generated/ehai-client.ts
npm.cmd run typecheck
npm.cmd run build
```

旧常驻契约测试已退役。修改公开接口仍必须核对实际 route、字段与审核 Schema；生成器、严格类型检查
和构建继续使用，不因此另建测试套件。若正常使用失败，需要的定位测试放在仓库外临时目录。
破坏性契约变化应新增版本，不能在 `v1` 中静默改变已有调用方语义。
