# Interfaces Architecture

`ehai.interfaces` 是 Execution Plane 的输入/输出边界。它把 CLI、HTTP 和 SSE 请求转换为 Application
Command/Query，并把结果转换为公开 JSON；状态规则仍由 Domain/Application 层拥有。

目标交互见 [Product Scope](../../../docs/PRODUCT_SCOPE.md)。CLI、顶层通用 Agent、未来 UI 和
Routines 都使用同一公开应用语义。R1 已接入多轮规划讨论、设计版本和查询；执行挂起回复及通用 Agent
交互尚未完成。新能力必须接入正常 Composition Root；不得依赖测试 monkeypatch 或直接构造组件补全
用户流程。CLI 与后台宿主的生命周期也属于入口验收。

## 文件职责

目标中的阶段决策树、统一人工/自动 Gate、过程调整便签和 handoff 恢复见
[Execution Model](../../../docs/EXECUTION_MODEL.md)。本页列出的现有 CLI/API 尚未完整承载这些语义，
不能仅修改展示文案就宣称实现，具体命令仍以 [Usage](../../../docs/USAGE.md) 为准。

| 文件 | 职责 |
| --- | --- |
| [`cli.py`](cli.py) | `ehai` 命令解析、Adapter 选择和同步本地组合；输出机器可读 JSON。 |
| [`api.py`](api.py) | FastAPI Command/Query route、错误映射和公开响应组装。 |
| [`http_models.py`](http_models.py) | 严格 Pydantic 请求/响应 envelope；拒绝未知字段。 |
| [`public_events.py`](public_events.py) | 领域 Event 到稳定公开 Event 文档的映射。 |
| [`public_documents.py`](public_documents.py) | CLI/HTTP 共用的公开 DTO 编码，保留字段边界和时间格式。 |
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
CLI 已开放 `get-plan`、`get-plan-checks`、`get-trace`，直接使用现有 QueryService 和与 HTTP 相同的
公开编码，不构造模型或 Worker。另有 `discuss-plan`、`get-discussion` 与对应 HTTP 讨论接口；
讨论可以澄清而不生成计划，也可以创建新草稿版本。执行期间的人工回复仍待后续实现。
旧 CLI/API/契约测试已退役；实际失败用仓库外临时文件定位，唯一产品 E2E 待能力开放后确认，
见 [测试策略](../../../tests/README.md)。
