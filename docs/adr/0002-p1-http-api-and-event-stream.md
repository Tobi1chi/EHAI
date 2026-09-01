# ADR 0002：P1 最小 HTTP API 与事件流

- 状态：已接受
- 日期：2026-08-31
- Roadmap：P1（I0、I7）

## 目的

为 P1 选择一个 Python 原生、可测试的最小 HTTP 入口，暴露既定 Command、Query 和可恢复 Event 流；不提前建设 TypeScript Control Plane、账号权限或通用实时通信平台。

## 决策

P1 必须使用 FastAPI 构建 ASGI HTTP 接口，并使用 Uvicorn 作为本地开发和演示服务器。请求和响应边界使用 Pydantic 模型校验。CLI 与 HTTP 层必须调用同一组 application service，不得在接口层复制批准、Gate 或状态转换规则。

HTTP 表面只覆盖 P1 Implementation Plan 已列出的能力：

- Command：`CreateProject`、`CreateGoal`、`ProposePlan`、`ReplanPlan`、`ApprovePlan`、`StartRun`、`PauseRun`、`ResumeRun`、`CancelRun`。
- Query：`GetRun`、`GetPlanGraph`、`GetExecutionTrace`、Check、Checkpoint、Artifact 元数据与当前状态。
- Event：`ListEvents(after_event_id)` 和基于同一游标语义的 Server-Sent Events（SSE）订阅。

具体路由使用版本前缀 `/api/v1`。写操作使用资源/动作明确的 REST 路由，并在请求中携带幂等键；查询使用 `GET`。P1 不为了表面上的纯 REST 风格隐藏领域 Command。

`POST /plans/replan` 必须引用一个已批准的 base PlanRevision，且 Goal 仍须处于 open。它创建新的 draft PlanRevision 和未确认的下一版 CompletionContract/CheckSpec，保留 `supersedes` lineage；调用方仍须通过 `POST /plans/approve` 明确批准新版本。旧 PlanRevision、Run 和 ExecutionTrace 保持可查询，初次 `ProposePlan` 不承担隐式重规划语义。

事件流使用 Starlette 自带的 `StreamingResponse`，媒体类型为 `text/event-stream`，不额外引入 SSE 扩展包。每个 SSE frame 必须至少包含：

```text
id: <event-id>
event: <event-type>
data: <JSON event envelope>
```

客户端可以通过标准 `Last-Event-ID` 请求头恢复，也可以通过 `after_event_id` 查询参数显式指定游标；两者同时出现时必须拒绝不一致值。服务端按 Event Log 顺序发送晚于游标的 Event，并周期性发送注释 heartbeat 以保持连接，但 heartbeat 不得写入 Event Log。未知或已不可定位的游标必须返回明确错误，不得静默从最新位置开始。

P1 采用短轮询 SQLite Event Log 的简单异步生成器实现 SSE。该生成器必须在客户端断开时停止，且不得持有写事务或长期数据库连接。进程内发布/订阅总线、WebSocket 和跨进程消息代理留到后续阶段。

FastAPI 自动生成的 OpenAPI 只作为接口发现和测试辅助；`schemas/v1/` 中审核过、带版本的语言无关 Schema 才是跨 Plane 契约来源。P1 不生成 TypeScript Client，也不创建 TypeScript Control Plane。

## 结果与边界

- FastAPI/Pydantic 提供足够的输入校验和 OpenAPI 可见性，且保持 Execution Plane 全部为 Python。
- SSE 适合服务端单向传送不可变 Event，并天然支持 Event ID；Command 继续使用普通 HTTP。
- SQLite 短轮询简单可恢复，但有固定轮询延迟，不用于 P2 的高吞吐或多进程部署。
- API 层只做协议转换、认证前置占位和错误映射；执行真相仍由 Python Domain/Application 层拥有。
- P1 不实现用户账户、多租户、角色权限、WebSocket、通用订阅过滤或 UI 状态机。

## 未选择方案

- **WebSocket**：P1 不需要双向实时通道，会增加连接状态和恢复协议。
- **`sse-starlette` 等额外依赖**：P1 frame 与断线恢复需求可由 `StreamingResponse` 清晰实现。
- **Flask 或标准库 HTTP server**：可以提供端点，但异步断连处理、类型校验和契约测试需要更多自建代码。
- **进程内消息总线**：重启后不可恢复，且不能替代 SQLite Event Log。

## 可逆性

HTTP handlers 和 SSE producer 必须仅依赖 application service/Query Port。未来可以替换 ASGI 框架、引入消息代理或生成 TypeScript Client，而不改变领域状态机和持久化的 Event ID/offset 语义。
