# P2 Implementation Plan

## 状态与目标

- 状态：已确认；从 `P2-I0` 开始实施。
- Roadmap 阶段：P2——稳定的多 Worker 执行内核。
- P1/P1.1 执行语义基线：提交 `a1a41b1`。

P2 将 P1 的单 Worker、同步串行执行升级为可持久化、可观察、可恢复的异步 Agent Runtime。
Planner 生成 PlanGraph，Orchestrator 判断 ready 并推进领域状态，Scheduler 管理队列与并发，
Dispatcher 选择 Worker/Endpoint，Built-in Agent 或 Codex Connector 执行 Attempt。Worker 仍只能提交
候选结果，Check/Gate 仍是 PlanNode 和 Run 完成的唯一入口。

## P2 完成后的能力

- `StartRun` 持久化并排队后快速返回，调用方通过 Query/SSE 观察后台进展。
- 多个 ready PlanNode 可以在 capacity 与 Workspace 隔离允许时并发执行。
- 同一 PlanRevision 可以混合调度 Built-in Agent、Codex CLI 和 Codex App Server Worker。
- EHAI 拥有一个使用 OpenAI Responses API 的内置 Agent Loop，不依赖外部 Agent CLI 才能执行。
- 同一个 Codex App Server 可以运行多个相互隔离、可分别观察、取消和恢复的 Thread/Turn。
- 每个 Attempt 都持久化 Worker、Endpoint、Session/Execution、事件游标、租约和进展时间。
- 探索分支可使用独立 Git worktree 和 Agent Session；Merge 只接收明确选中的 Artifact。
- Heartbeat、无进展超时、绝对截止时间、取消、重试和资源预算由统一策略管理。
- 调度、Agent Step、Tool、Artifact、Check、Gate 和恢复决定都能留下可回放证据。
- OpenAPI/JSON Schema 保持版本化，并生成供 P3 使用的严格 TypeScript API Client。

P2 不实现 P3 Dashboard、P4 Workflow、P5 OpenCode/Claude Code/DSH Connector、插件 SDK/市场或跨主机
分布式调度。

## P2.1 Planner 契约修复

Planner provider 只输出 provider-specific plan content，必须先转换为 provider-neutral `PlanTemplate`，
再由应用层统一 builder 创建 `CheckSpec`、`CompletionContract` 和 `PlanRevision`。`single`、
`exploration`、`codex` 和 `builtin` Planner 都不得各自决定 Criterion 到 CheckKind 的映射。

Built-in Planner 可以复用 Built-in Agent 的 Responses `ModelClient` seam，但不得创建 Worker Attempt、
Workspace、AgentSessionRef 或长期 Planner Session。Worker request 必须显式携带已确认的
`CompletionContract` 和 required `CheckSpec` 快照；Provider 只能读取这些契约，不能决定或降低完成标准。

## DSH 借鉴范围

Built-in Agent 借鉴 DeepSeek Harness 的基本 Agent Spine：Session、System Prompt、Tool Runtime、
Agent、Agent Loop 与 LLM Adapter seam，以及 `Turn → Step → Model Request → Tool Call/Result` 生命周期。
参考 [DeepSeek Harness Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)
和 [`@deepseek-ai/dsh-agent-loop`](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/core/agent-loop)。

EHAI 对应关系：

| DSH 概念 | EHAI P2 实现 |
| --- | --- |
| Session/Event Log | `BuiltinSession` / append-only `BuiltinSessionEvent` |
| Turn | 一个 Built-in Worker Attempt 内的完整任务 |
| Step | 一次 Responses API 请求及其 Tool Calls |
| System Prompt | `PromptBuilder` |
| LLM seam | `ModelClient` / `OpenAIResponsesModelClient` |
| Tool Registry/Pipeline | Session 创建时固定的 `ToolSet` / `ToolExecutor` |
| Agent/Agent Loop | `BuiltinAgent` / `BuiltinAgentLoop` |
| Live Agent Event | 标准化 `WorkerEvent` |

只借鉴职责与生命周期，不复制 DSH 的组成系统。P2 明确不实现或引入：

- Cordis Context、Event Waterfall 和 effect lifecycle。
- 动态 Plugin 注册、卸载或发现。
- Bundle、Profile overlay 和 Patch 配置树。
- DSH TypeScript/Node Runtime 依赖。
- Subagent、Skills、MCP、长期记忆、Self-modification、Web/ACP/SDK Host。
- DSH Goal、Job、Workflow 或 UI；EHAI 继续使用自己的 PlanGraph、Run、Attempt 和 Gate。

## 核心映射与职责

```text
Planner → PlanGraph
             ↓ ready PlanNode
        Orchestrator
             ↓ runnable Attempt
          Scheduler
             ↓ capacity slot
          Dispatcher ← WorkerProfile / WorkerEndpoint Registry
             ↓ ExecutionHandle
  Built-in Agent Runtime 或 Codex Connector
             ↓ WorkerEvent / Artifact
        Orchestrator → Check/Gate → Checkpoint
```

```text
PlanNode  1 ── N Attempt
Attempt   1 ── 1 Builtin Turn 或 External Execution
Turn      1 ── N Step
Step      1 ── 1 Model Request + 0..N Tool Calls
Session   1 ── N 顺序 execution
Endpoint  1 ── N 并发 Session（受 capacity 限制）
```

- Planner 声明节点能力要求，不选择具体 Endpoint。
- Orchestrator 是 PlanNode、Attempt、Run 和 Branch 状态转换的唯一应用层入口。
- Scheduler 管理可运行队列、并发槽位和 lease，不判断任务是否完成。
- Dispatcher 根据能力、capacity、Workspace 与 SessionPolicy 绑定 WorkerProfile/Endpoint。
- Built-in Agent Session 内同时只运行一个 Turn；并发 Attempt 使用不同 Session。
- Connector 只适配平台启动、事件、查询、取消和恢复，不复制 EHAI 领域规则。
- `WorkerCompleted` 或 `submit_candidate` 只产生候选，不能绕过 Check/Gate。

## 范围控制

- 以下 Increment 的“必须交付”和“边界”是实现授权；未列出的功能不得顺手加入。
- 可选能力通过 capability 表达，不要求所有 Worker 模拟同一功能。
- 不新增消息代理、服务发现、自动扩缩容、抢占、插件系统、跨主机协调或新身份系统。
- 不为 P5 OpenCode、Claude Code 或 DSH Connector 添加依赖、Profile 字段、兼容层或测试夹具。
- Secret 只从环境或配置引用读取，不进入 PlanGraph、Event、Artifact 或数据库明文字段。
- 新依赖必须直接服务当前 Increment；同一外部协议不同时维护多套 Client。
- 每个 Increment 完成前不提前搭建后续 Increment 的抽象、TODO 实现或占位服务。

## 编码增量

### P2-I0：执行契约与迁移基线

固定契约见 [P2 执行契约与迁移基线](P2_EXECUTION_CONTRACT.md)。

**前置**

- P1/P1.1 全量测试、Ruff、格式和 mypy 通过。
- 当前数据库可以从备份副本验证迁移。

**必须交付**

- 固定 `PlanNode → Attempt → Builtin Turn/External Execution` 映射。
- 固定 Connector Port：`start`、`events`、`inspect`、`cancel`、`recover`。
- 固定 SessionPolicy：`new`、`reuse`、`fork`。
- 区分 Attempt 生命周期与 queued/running/waiting/stalled 等活动观察。
- 固定 `StartRun` 在 P2 中持久化 Run 与 dispatch work 后返回。
- 固定 Built-in Agent 使用官方 OpenAI Python SDK 调用 Responses API；凭证引用为
  `env:OPENAI_API_KEY`，model 由 WorkerProfile 显式指定。
- 为 P1 Check/Gate、Artifact 选择、Checkpoint 和恢复语义增加必要迁移保护。
- 只读确认当前 Codex App Server `--help` 和 Schema 生成能力。

**边界**

- 不实现 Runtime、Scheduler、新数据库模型或真实 API 调用。
- 不新增 ADR，除非发现本计划无法表达的不可逆冲突。
- 不为每个契约字段创建独立测试类或脚本。

**最小验证**

- 复用 P1 E2E 证明完成语义未变。
- 一个契约测试覆盖已知/未知 capability 与 SessionPolicy。
- 一个幂等测试证明重复 StartRun 不创建第二个 Run。

**退出条件**

后续 Increment 使用的名词、状态所有权、API 行为和 Provider 入口均已固定。

建议提交：`test(runtime): pin p2 execution contracts`

### P2-I1：Worker、Endpoint、Session 与执行绑定模型

**前置**

- `P2-I0` 已合入，旧数据库迁移路径已明确。

**必须交付**

- 实现 WorkerProfile、WorkerCapability、WorkerEndpoint、AgentSessionRef、ExternalExecutionRef、
  BuiltinExecutionRef 和 ExecutionHandle。
- WorkerProfile 保存 Worker 类型、模型、能力、SessionPolicy 和预算引用，不保存 secret。
- WorkerEndpoint 保存 Endpoint 类型、命令/地址引用、capacity 与 enabled/draining/disabled 管理状态。
- Attempt 保存不可变分配、provider execution ID、event cursor、Heartbeat、progress time、deadline 和
  lease expiry。
- PlanNode 只增加 `required_capabilities` 与 SessionPolicy，不增加 provider 专用字段。
- 实现最小 Registry Repository 与 Query。

**边界**

- Registry 不安装 Worker、不扫描服务、不动态 import，也不是插件市场。
- 不使用通用 JSON `extras` 绕过类型边界。
- 不实现调度或 Connector；本 Increment 只负责模型、迁移与查询。

**最小验证**

- 一个参数化领域测试覆盖合法绑定、重复绑定和跨 Run 引用。
- 一个 SQLite 往返测试同时覆盖 Profile、Endpoint、SessionRef、ExecutionRef。
- 一个旧 P1 数据库迁移测试；不为每张新表各写一份脚本。

**退出条件**

可以持久化、恢复和查询完整 Attempt 分配，旧 P1 数据仍可读取。

建议提交：`feat(runtime): persist worker routing and execution refs`

### P2-I2：DSH-inspired Built-in Agent Spine

**前置**

- `P2-I1` 的 BuiltinExecutionRef 与 Session 引用可用。
- 本 Increment 只使用 ScriptedModelClient。

**必须交付**

- 实现 BuiltinAgent、BuiltinAgentLoop、BuiltinSession、BuiltinSessionStore、BuiltinSessionEvent、
  PromptBuilder、ModelClient Port、ToolDefinition、固定 ToolSet 与 ToolExecutor。
- 一个 Attempt 对应一个 Turn；一个 Step 对应一次 ModelClient 请求及其 Tool Calls。
- `turn/start/end`、`step/start/end`、模型消息、tool/call/result 和 final 是 append-only SessionEvent。
- 每个进入 ModelClient 的 EHAI 输入必须能从 SessionEvent 重建；Provider continuation ID 只是句柄，
  不是唯一真相来源。
- Durable SessionEvent 与 Live WorkerEvent 分离；Token delta 不默认进入全局 Event Log。
- Session 在已完成 Step 边界持久化；中断中的写 Tool 不自动重放。
- Agent、Session、Tool 和取消令牌由一个明确 ExecutionScope 拥有并按序关闭。

**边界**

- 不接真实模型，不实现 Responses Adapter。
- ToolSet 在 Session 创建时固定，不支持动态注册、卸载或 plugin hook。
- 不实现 Cordis、Bundle、Patch、Subagent、Skills、MCP、Memory 或 UI。
- Tool Calls 在本 Increment 串行执行，不增加 DSH 的并行 Tool rolling pool。

**最小验证**

- 一个 Happy Path 覆盖两 Step、一次 Tool Call 和 final。
- 一个取消/恢复测试证明完成 Step 可恢复、未完成写 Tool 不重放。
- 一个非法 Tool 或非法状态转换测试；不排列所有 Event 组合。

**退出条件**

ScriptedModelClient 可驱动 Turn/Step/Tool Loop，Session 重载后得到相同模型可见历史。

建议提交：`feat(agent): add built-in agent spine`

### P2-I3：OpenAI Responses Built-in Agent

**前置**

- `P2-I2` Agent Spine 与 ScriptedModelClient 测试通过。
- 真实 Smoke 使用用户提供且有目标模型权限/额度的 `OPENAI_API_KEY`。

**必须交付**

- 实现唯一 P2 真实 ModelClient：OpenAIResponsesModelClient；使用官方 OpenAI Python SDK。
- WorkerProfile 必须显式提供 model；数据库只保存 `credential_ref=env:OPENAI_API_KEY`。
- Responses 请求使用 streaming、`store=true`、持久化 previous_response_id 和
  `parallel_tool_calls=false`。
- EHAI Tool 映射为严格 custom function tools；使用 call_id 回传对应 tool result。
- P2 固定工具：Artifact 读取、Workspace 列举/搜索/读取、受控 patch、无 shell argv command，以及
 严格 `submit_candidate` final tool。
- 持久化完成 Response 的 ID、status、usage 和必要 output item；完整 thinking 不持久化。
- 支持 Step、Tool Call、wall-clock 和输出字节预算。

**边界**

- 不启用 OpenAI hosted tools、MCP、Realtime、Batch、Assistants、Agent SDK 或 background mode。
- 不实现第二个 Model Provider，不从 Codex 登录推导 API key。
- 不支持 `store=false` 或 Responses Conversation 资源。
- 不允许 Agent 修改 WorkerProfile、扩大 Tool 权限、自动 commit/push 或调用外部 Connector。

**最小验证**

- ScriptedModelClient 继续覆盖所有错误路径。
- 一个 Responses 协议测试覆盖 stream、function call/result、previous_response_id 和 final。
- 一个显式真实 Smoke 完成无副作用的小任务；不为每种 API error 做真实调用。

**退出条件**

Built-in Agent 能完成“读取→修改→运行允许命令→提交 Artifact”的小任务，候选仍由 Gate 验证。

建议提交：`feat(agent): add responses model client`

### P2-I4：单槽位后台 Runtime

**前置**

- `P2-I3` Built-in Agent 可作为 Worker 执行；后台测试默认使用 TestConnector。

**必须交付**

- 实现单 Runtime 进程、单 Endpoint、capacity=1 的后台循环。
- `StartRun` 在 Run 与 pending dispatch work 同一事务提交后返回。
- Runtime claim work、启动 Worker、消费 WorkerEvent、持久化 Artifact 并通知 Orchestrator 推进。
- 标准 Connector Port 以 Attempt ID 作为 start 幂等键；事件必须去重。
- Runtime 启动时 recover 非终态 Attempt；找不到原 execution 时标记 interrupted，不新建替代任务。
- 现有 SSE 实时公开标准化 Event。

**边界**

- 不并发、不路由多个 Endpoint、不自动 retry。
- 不引入 Celery、Redis、Kafka、第二个数据库或独立 daemon framework。
- 不在 FastAPI request task 中常驻执行 Agent。

**最小验证**

- 一个集成场景证明 StartRun 快速返回且后台完成。
- 同一场景加入 Runtime 重启，证明不重复 start。
- 一个 cancel/completed 竞态测试；不穷举所有时序排列。

**退出条件**

P1 单节点和探索场景可以通过单槽位后台 Runtime 完成并恢复。

建议提交：`feat(runtime): execute attempts in the background`

### P2-I5：Scheduler、Dispatcher 与受控并发

**前置**

- `P2-I4` 单槽位 Runtime 稳定。
- 只使用 TestConnector/ScriptedModelClient 验证并发，不运行真实写任务。

**必须交付**

- Scheduler 可领取多个 ready PlanNode，并以 SQLite claim/lease 防止重复执行。
- 支持全局、Project、Run、WorkerProfile 与 WorkerEndpoint 整数 capacity。
- Dispatcher 过滤 capability、disabled/draining、无 capacity 或 Workspace 冲突的 Endpoint。
- 多个合格 Endpoint 使用显式 Profile priority 与 Endpoint ID 的稳定顺序。
- 无 capacity 时保持 queued；保存 queued 原因、分配和调度 Event。
- queued Attempt 可以取消且不占 Worker capacity。

**边界**

- 只支持单 Execution Plane 进程；lease 用于崩溃恢复，不宣称分布式一致性。
- 不实现抢占、动态优先级、自动扩容、工作窃取、随机负载均衡或推测执行。
- 同一 Session 不并发两个 execution。
- 自动 retry 留到 `P2-I8`。

**最小验证**

- 一个并发场景同时证明两个分支重叠运行和第三个任务因 capacity 排队。
- 同一场景覆盖 queued cancel 或 lease 重领中的一个关键竞争。
- 一个稳定性断言证明不同完成顺序不改变最终 Branch/Gate 结果。

**退出条件**

多个分支可受控并发，调度决定能由 Event Replay 重建。

建议提交：`feat(scheduler): dispatch concurrent attempts`

### P2-I6：Workspace 与 Session 隔离

**前置**

- `P2-I5` 并发仅在 TestConnector 上通过；本 Increment 完成前不并发运行真实写任务。

**必须交付**

- 实现 WorkspaceRef、WorkspaceLease 与 EHAI-owned Git worktree 生命周期。
- 并发探索分支默认使用不同 worktree 和新 Session。
- 线性后继节点只有显式 `reuse` 且 Session 可恢复时才复用；`fork` 产生新 Session ID。
- 非 Git Workspace 的并发写任务串行排队，不复制目录模拟 worktree。
- Merge 从 selected Artifact/patch 获取输入，不依赖未选 worktree 的可变状态。
- 清理仅作用于所有权和路径记录匹配的 EHAI worktree；脏 worktree 保留并产生 Event。

**边界**

- 不自动 commit、merge、rebase、cherry-pick、push 或删除用户 branch/worktree。
- 不实现通用 SCM、容器或虚拟机 sandbox。
- 两个 write-capable Attempt 不得共享路径；read-only Attempt 可共享只读 Workspace。

**最小验证**

- 一个真实 Git 集成场景让两个分支并发修改同名文件并保持隔离。
- 同一场景验证 selected Artifact 与 Merge 输入。
- 一个失败清理场景覆盖脏 worktree 保留和所有权边界。

**退出条件**

真实并发写任务不会互相覆盖，取消/恢复不会清理用户资源。

建议提交：`feat(workspace): isolate concurrent branch execution`

### P2-I7：Codex App Server 多 Session Connector

**前置**

- 重新运行 `codex --version`、`codex app-server --help` 和 Schema 生成；不只依赖版本快照。
- 在临时 Workspace 完成 initialize/thread/turn/interrupt Spike。

**必须交付**

- 只使用 `codex app-server --listen stdio://` JSONL JSON-RPC；一个 Endpoint 管理一个进程/连接。
- 实现 initialize/initialized、thread start/resume/fork/read、turn start/interrupt，以及相关通知。
- Codex thread 映射 AgentSessionRef，turn 映射 ExternalExecutionRef；同一 thread 最多一个 active turn。
- 按 thread/turn/item ID 分流交错事件；连接 request ID 只关联响应，不充当 execution ID。
- Approval/Input Request 进入 waiting，由 P2 Command resolve；Connector 不自动同意。
- 断线后使用持久化 thread ID resume/read；找不到 turn 时不创建替代 turn。
- 保留 `codex exec` 作为 process-per-Attempt Connector，不伪装 Session resume。

**边界**

- 不接 App Server WebSocket、Unix socket、远程 listener、Review、Thread Goal、Dynamic Tools、Skills、
  Apps、Auth 登录、Archive/Delete 等无关接口。
- 不修改用户全局 Codex 配置。
- Desktop 可见性和 Project 归属仅作为 provider 元数据，不作为调度/完成条件。

**最小验证**

- 一个受控协议测试覆盖双 thread 交错事件与单 turn interrupt。
- 一个断线 resume/waiting request 测试。
- 一个显式真实双 Session Smoke；其余错误全部使用假 App Server。

**退出条件**

同一 Codex App Server 的两个 Session 可并发、独立观察/取消，并在断线后协调原 execution。

建议提交：`feat(worker): add codex app-server connector`

### P2-I8：租约、超时、重试与恢复

**前置**

- Built-in Agent、Codex CLI 与 Codex App Server 已通过共享 Worker Contract。

**必须交付**

- 分别实现 start timeout、Heartbeat lease、no-progress timeout、absolute deadline 和 cancel grace。
- waiting 暂停 no-progress timeout，但不自动延长 absolute deadline。
- Endpoint health 由连接/health 结果维护；Session progress 只由对应 execution 事件更新。
- 自动 retry 仅允许：尚无 ExecutionHandle、provider 确认 execution 不存在、或 Worker 明确声明失败点
  可安全重试。其他未知状态标记 interrupted。
- 支持最大 Attempts、Run 时间、调用、并发与可取得的 provider cost 预算。
- 有界保存 Artifact、日志与上下文摘要；Event Replay 重建 claim/dispatch/binding/retry。
- Check Runner 使用静态内部 Registry，不动态装载插件。

**边界**

- 不因 Server Heartbeat 刷新全部 Session progress。
- 不对可能已经写 Workspace 的未知 execution 自动重试。
- 不实现 Saga、补偿事务、通用 Circuit Breaker 或自动成本优化。
- Provider 不报告 token/cost 时标记 unavailable，不估算虚假值。

**最小验证**

- 一个故障注入集成场景覆盖 Heartbeat 正常但无进展、lease 过期和 waiting。
- 一个 retry 场景同时证明安全重试与未知状态不重试。
- 一个 Replay 断言证明恢复后的投影一致。

**退出条件**

Run 不会因 Worker/Runtime 故障永久悬挂，每次等待、重试、失败和恢复都有 Event 原因。

建议提交：`feat(runtime): enforce execution leases and retry policy`

### P2-I9：API、Schema、端到端验收与系统审查

**前置**

- `P2-I0` 至 `P2-I8` 均满足退出条件。
- 数据库迁移已在空库和 P1 副本验证。

**必须交付**

- 稳定 P2 Command/Event/Query Schema，生成并编译严格 TypeScript 类型与 API Client。
- Query 公开 Run/Attempt、Worker 分配、Session 活动、lease、progress time、waiting request 与诊断。
- 增加 resolve/decline Worker Request、延长 deadline、取消 queued/running Attempt 的 Command。
- SSE 从 Event ID 恢复；P2 不新增 WebSocket。
- README 只同步验收通过的能力，Usage 更新实际命令。
- 完成系统性代码、架构、安全和测试审查，只修复阻止 P2 退出的问题。

**边界**

- 不开发 TypeScript UI，不新增账号、组织、RBAC 或 Secret API。
- 不接 OpenCode、Claude Code 或 DSH Runtime。
- 不引入常驻外部测试基础设施。
- 不公开 provider 原始 transcript、thinking、secret 或内部路径。

**最小验收场景**

1. Built-in Agent 在隔离 worktree 完成小型代码任务并通过 Gate。
2. 同一 Codex App Server 的两个 Session 并发探索，分别可观察和取消。
3. 同一 PlanRevision 混合 Built-in Agent 与 Codex Worker。
4. Runtime 中途重启并恢复原 Session/Execution，不重复创建任务。
5. Evaluator 使用全部候选证据，Merge 只接收 selected Artifact，最终创建 Checkpoint。
6. Event Replay 重建调度、Agent Step、Tool、waiting、retry、Check 和 Gate。

**退出条件**

- Roadmap P2 退出条件全部满足。
- Python 全量测试、Ruff、格式、mypy、Schema 契约和 TypeScript Client 编译通过。
- 显式真实 Smoke 覆盖 Responses Built-in Agent、Codex App Server，现有 Codex CLI Smoke 保持通过。
- 系统审查没有阻断项；其余建议明确留到 P3+。

建议提交：`test(e2e): verify p2 multi-worker runtime`

## P2→P3 Planning & Execution Readiness Gate

P2-I0–I9 固定并实现执行内核；以下增量只关闭真实项目使用前仍缺少的组合证据，不重新设计 P2，
也不提前开发 P3 UI。每项必须复用现有 Test Double、API、Trace 和 Smoke 基础，避免再次扩张测试框架。

### P2-I10：真实 Planner 闭环

**必须交付**

- Built-in Planner 接收显式注入的 Goal、Completion Criteria、预算和必要仓库上下文。
- 真实 Responses 调用必须通过严格 `submit_plan` 产生 provider-neutral PlanTemplate。
- 统一 builder 校验并持久化 draft PlanRevision、CheckSpec 和 CompletionContract。
- Planning Trace 可以查看最终 PlanGraph 和稳定 Planner event，不保存模型思维链。

**边界**

- Planner 不读取整个仓库、不创建 Worker Attempt/Session，也不批准或执行计划。
- 不要求模型生成任意 DAG；P2 继续使用有界双分支结构。
- 真实 Smoke 默认跳过，只在显式开关和凭证存在时调用一次。

**退出条件**

真实 Built-in Planner 能生成与输入上下文一致的双分支 draft，持久化后可重载，且数据库中没有 Run。

### P2-I11：失败恢复与证据驱动 Replan

**状态：** 已完成（2026-09-04）。离线故障注入闭环及真实 `gpt-5.6-luna/high` Replan Smoke 均通过。

**必须交付**

- 固定 retry、resume、Checkpoint restore 和 Replan 的选择规则。
- Replan 输入包含有界、脱敏的失败节点、Attempt 结果、Check 失败、预算和 Checkpoint 引用。
- 新 PlanRevision 保留 base lineage，不修改旧 Plan/Run/Trace，不自动重放未知写副作用。

**边界**

- 不把完整 transcript、思维链或无界 Artifact 内容交给 Planner。
- 不自动批准新 PlanRevision；失败上下文不能降低 CompletionContract。

**退出条件**

一个故障注入 E2E 能从失败 Run 形成可解释 ReplanContext，生成并批准新 revision，最终由新 Run 完成
同一 Goal；旧轨迹保持可查询。

### P2-I12：EHAI 自举代码任务

**必须交付**

- 选择一个低风险、少文件、可自动验证的 EHAI 代码改动作为 Goal。
- 两个隔离分支分别实现，Evaluator 使用测试和 diff 证据选择，Merge 只晋升 selected ChangeSet。
- CompletionContract 同时要求聚焦测试、受影响全量测试、Ruff、格式、mypy、diff scope 和人工 Gate。

**边界**

- 不自动 push、merge 或修改用户分支；最终 commit 仍需显式授权。
- 不用 `artifact:non-empty` 代替代码正确性，不把未选中 Workspace 的修改带入结果。

**退出条件**

EHAI 使用真实模型完成一次自身代码变更，最终 diff、Check/Gate/Checkpoint、Session/Attempt 和 Workspace
lease 均可从 trajectory 复核。

### P2-I13：Readiness 最终验收

**必须交付**

- 汇总 P1/P2 的规划、执行、并发、控制、恢复、重试、Replan 和自举证据。
- 验证 Python、Schema 和 TypeScript 全量门禁，并确认文档与实际 CLI/API 一致。
- 记录仍属 P3+ 的非阻断项，不通过新增兼容层或测试矩阵掩盖缺口。

**退出条件**

Roadmap Readiness Gate 的四类 E2E 全部通过，工作树 clean，无已知阻断缺陷，才允许启动 P3 UI。

## 最小充分测试策略

测试只用于证明 Increment 退出条件、保护已发生回归、阻止状态损坏/重复副作用或跨 Session/Workspace
串线。以上“最小验证”是风险清单，可以由同一场景覆盖多项，不代表每一条创建独立测试脚本。

### 测试层次

- 纯状态机/映射使用 unit test。
- SQLite、Runtime、Scheduler、Workspace 使用 integration test。
- Built-in/Codex Worker 共用参数化 contract test；只为各自特有协议补最少测试。
- P2 只维护一个代表最终退出条件的 E2E 场景。
- 真实外部调用保留一个 Responses Worker Smoke、一个 Responses Planner Smoke、一个 Codex App Server
  双 Session Smoke 和现有 Codex CLI Smoke；默认测试不访问网络或用户 Session。

预计新增的主要测试模块不超过以下职责集合；优先复用现有文件：

```text
tests/unit/test_builtin_agent.py
tests/integration/test_async_runtime.py
tests/integration/test_scheduler.py
tests/integration/test_workspace_sessions.py
tests/contract/test_p2_workers.py
tests/e2e/test_p2_runtime.py
```

### 禁止测试膨胀

- 不设置行覆盖率目标，不为覆盖不可达分支修改生产接口。
- 不为 dataclass getter、Python/SQLite/OpenAI SDK/Codex 自身保证的行为重复测试。
- 不对状态、capacity、timeout 和平台做笛卡尔积矩阵；选择能证明不变量的代表值。
- 同一行为不同时复制为 unit、integration、contract 和 E2E；使用最低且足够的层级。
- 默认不新增 `tests/helpers/*.py` 独立脚本。只有真实子进程边界无法在 pytest Fixture 内表达时才允许，
  并必须在测试旁说明必要性。
- Test Double 默认放在使用它的测试模块；至少三个模块复用后才提取公共 Fixture。
- 不建立自定义测试 DSL、Fixture Framework、Snapshot 系统或 P3–P5 预留 Fixture。
- Bug 修复增加一个最小回归测试；不得借机扩成相邻功能的全量审计。
- 行为迁移时更新现有测试，不保留两套互相矛盾的旧/新测试。

### 执行节奏

```text
修改代码
→ 运行最小相关测试
→ 修复并复测失败子测试
→ Increment 准备提交时运行全部受影响测试与检查
→ P2 最终验收时运行仓库全量测试
```

## 接口依据与环境快照

- [DeepSeek Harness Architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)：
  Session/Turn/Step、Agent Loop、Tool Pipeline 与 LLM seam 的参考，不作为运行时依赖。
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)：
  streaming、custom function tools、usage 和 previous_response_id。
- [Codex App Server](https://developers.openai.com/codex/app-server/)：stdio JSONL JSON-RPC、Thread/Turn
  与事件通知。

2026-09-02 的只读快照为 `codex-cli 0.147.0`。实现 Connector 时必须重新读取当时安装版本的
`--help`；该快照不是版本锁定或兼容承诺。
