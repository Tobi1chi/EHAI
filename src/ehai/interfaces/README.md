# Interfaces Architecture

`ehai.interfaces` 是 Execution Plane 的输入/输出边界。它把 CLI、HTTP 和 SSE 请求转换为 Application
Command/Query，并把结果转换为公开 JSON；状态规则仍由 Domain/Application 层拥有。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| [`cli.py`](cli.py) | `ehai` 命令解析、Adapter 选择和同步本地组合；输出机器可读 JSON。 |
| [`api.py`](api.py) | FastAPI Command/Query route、错误映射和公开响应组装。 |
| [`http_models.py`](http_models.py) | 严格 Pydantic 请求/响应 envelope；拒绝未知字段。 |
| [`public_events.py`](public_events.py) | 领域 Event 到稳定公开 Event 文档的映射。 |
| [`sse.py`](sse.py) | 可恢复 SSE；支持 Event ID、`Last-Event-ID` 和 `after_event_id` cursor。 |
| [`runtime.py`](runtime.py) | `ehai-api` Composition Root；装配数据库、Planner、Worker/Connector、Runtime、健康状态和 Uvicorn。 |

## 同步 CLI 与 P2 Runtime

普通 `ehai` CLI 保留同步执行语义：`StartRun` 可以在调用内推进 Run，适合一次性本地命令。启用
`ehai-api --p2-runtime` 时，`StartRun` 只创建 `pending` Run 和持久 DispatchWork，然后立即返回；
后台 `SingleSlotRuntime` 或 `ConcurrentRuntime` 执行 Worker，调用方通过 Query 或 SSE 观察状态。

两种模式共享同一 `ExecutionService`、Orchestrator、领域状态机和持久化契约。HTTP handler 不等待
Worker 完成，也不复制调度逻辑；Composition Root 负责启动/关闭后台 Runtime 和 Connector，并通过
`/api/v1/runtime/health` 暴露循环健康状态。

公开跨 Plane 契约位于 [`schemas/v1`](../../../schemas/v1)，不是从内部 dataclass 自动推断的替代品。
API route 与审核后的 OpenAPI 一致性由
[`tests/contract/test_v1_schemas.py`](../../../tests/contract/test_v1_schemas.py) 验证。CLI、HTTP 和 SSE 测试
分别位于 [`tests/unit/test_cli.py`](../../../tests/unit/test_cli.py)、
[`tests/integration/test_api.py`](../../../tests/integration/test_api.py) 和
[`tests/integration/test_sse.py`](../../../tests/integration/test_sse.py)。
