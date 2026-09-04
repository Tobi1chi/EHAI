# Cross-Plane Schemas

`schemas/v1` 是 Execution Plane 与 Control Plane 之间经审核、带版本的公开契约。内部 Python 模型可以
演进，但公开字段、状态值和 route 必须先在这里明确，并通过契约测试。

## 文件

| 文件 | 内容 |
| --- | --- |
| [`common.schema.json`](v1/common.schema.json) | ID、时间戳、错误、Project、Goal、Plan、Run、Attempt、Artifact、Check、Worker 等共享定义。 |
| [`commands.schema.json`](v1/commands.schema.json) | Create/Propose/Approve/Start/Pause/Resume/Cancel 及 Attempt/Worker Request Command。 |
| [`queries.schema.json`](v1/queries.schema.json) | Run、PlanGraph、ExecutionTrace、Check、Checkpoint、Artifact、Worker 与 Runtime health 查询响应。 |
| [`events.schema.json`](v1/events.schema.json) | 公开 Event envelope、Event type 与分页结果。 |
| [`http-api.openapi.json`](v1/http-api.openapi.json) | `/api/v1` HTTP route、operation ID、请求体、响应和 SSE 入口。 |

## 生成与验证

[`control-plane/scripts/generate-client.mjs`](../control-plane/scripts/generate-client.mjs) 读取四份 JSON Schema
和 OpenAPI，检查必需 operation ID 与重复定义，然后生成
[`control-plane/src/generated/ehai-client.ts`](../control-plane/src/generated/ehai-client.ts)。生成文件不手工编辑。

从仓库根目录运行：

```powershell
uv run pytest tests/contract/test_v1_schemas.py -q
Set-Location control-plane
npm.cmd ci
npm.cmd run generate
git diff --exit-code -- src/generated/ehai-client.ts
npm.cmd run typecheck
npm.cmd run build
```

契约测试验证 JSON Schema 样例、公开状态值、FastAPI 实际 route 与审核 OpenAPI 的一致性。TypeScript
步骤验证生成器仍能消费 Schema，生成结果已提交且严格类型检查/构建通过。破坏性契约变化应新增版本，
不能在 `v1` 中静默改变已有调用方语义。
