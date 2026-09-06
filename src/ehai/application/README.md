# Application Architecture

`ehai.application` 把 Command 转成领域状态转换，并通过 Port 调用持久化、Artifact、Check 和 Worker。
它定义用例与执行顺序，但不包含 HTTP/CLI 表示，也不绑定 SQLite、Responses 或 Codex 传输。

## 当前实现与目标职责

下表是当前代码索引，不是产品完成声明。目标职责以
[Product Scope](../../../docs/PRODUCT_SCOPE.md) 为准：Planner 需要调查、讨论和修订详细方案，不只是
`propose/replan` 图生成；顶层通用 Agent 是平台调用者，不等于 Planner。R1 的规划讨论、设计版本和
修订审批已接入；执行挂起/回复与完整需求验收继续按 [P2 重整顺序](../../../docs/P2_IMPLEMENTATION_PLAN.md) 实现。
Foundation 分支新增共享 Runtime 和 Mailbox，不代表这些交互已经形成正常入口。现有调度和存储优先复用，
不得为 CLI、顶层 Agent、UI 和 Routines 分别实现一套执行用例。R2 的真实 CLI 试跑仍在进行，尚不是 R2/P2
验收结论。

## 主要职责

本页表格描述当前代码。2026-09-06 的 [Execution Model](../../../docs/EXECUTION_MODEL.md) 要求阶段/分支
Gate、阶段 Review、批准底线内自主调整、阶段 Session 及 handoff 回退；这些不能由下列单最终 Gate
与现有 resume 的类名推定为已经实现。

| 区域 | 文件 | 职责 |
| --- | --- | --- |
| Service | [`service.py`](service.py)、[`commands.py`](commands.py)、[`queries.py`](queries.py) | 处理幂等 Command、事务边界和只读查询。 |
| Planner | [`planner.py`](planner.py) | 定义 `Planner` Port、provider-neutral Plan 模板、图预算、最终行为 Gate 和确定性 Planner。 |
| Planning Discussion | [`planning_dialogue.py`](planning_dialogue.py)、[`service.py`](service.py) | 从现有 Event Log 查询持久讨论；串行接收消息、调用 Planner、保存澄清或新版本，失败不盲目重复模型调用。 |
| Orchestrator | [`orchestrator.py`](orchestrator.py)、[`evaluation.py`](evaluation.py)、[`checks.py`](checks.py) | 推进节点/Attempt，接收无 Check 的中间候选交接，评估分支，运行最终 Check/Gate 并创建 Checkpoint。 |
| Scheduler | [`scheduler.py`](scheduler.py) | 按能力、Endpoint 状态、capacity 和 Workspace 隔离分配可运行 Attempt；驱动并发 Runtime。 |
| Runtime | [`async_runtime.py`](async_runtime.py)、[`runtime_control.py`](runtime_control.py) | 定义 Connector 执行协议、后台 DispatchWork 循环、活动状态和 Worker Request 控制。 |
| Built-in Agent | [`builtin_agent.py`](builtin_agent.py) | 提供持久 Session、模型步骤、固定 Tool 调用、预算、取消与事件重放。 |
| Run Control | [`run_control.py`](run_control.py) | 协调 pause、resume、cancel 与运行中 Attempt 的收敛。 |
| Recovery | [`checkpointing.py`](checkpointing.py) | 从 Checkpoint 和持久状态恢复，并拒绝不一致的恢复输入。 |
| Contracts/Ports | [`workers.py`](workers.py)、[`execution_contracts.py`](execution_contracts.py)、[`ports.py`](ports.py) | 定义 Worker 请求/结果、Connector 契约、Unit of Work、Repository、Event Log 和 Artifact Store Port。 |
| Policy | [`execution_policy.py`](execution_policy.py)、[`sanitization.py`](sanitization.py) | 保存执行边界并限制进入日志、事件和诊断的数据。 |

## 职责边界

- Planner 把 Goal、CompletionContract 和预算转换为 `PlanProposal`；它不创建 Attempt、分配 Workspace、
  调用 Worker 或推进 Run。
- Orchestrator 是执行语义的所有者：判断 ready node、创建/推进 Attempt、验证候选与分支选择，并在
  Check/Gate 后提交节点和 Run 状态。它不选择具体 Worker Endpoint，也不实现供应商协议。
- Scheduler 只做可执行工作选择、Worker/Endpoint 路由、capacity 记账和 Workspace Lease 管理；它不
  复制 Planner 的图验证或 Orchestrator 的 Gate 规则。
- Connector 将一个 `WorkerRequest` 映射到外部或内置执行，并以有序 Worker Event 返回结果。Connector
  不直接写领域终态；Orchestrator 消费并去重 Event 后决定状态转换。
- Service 拥有 Command 幂等和事务入口。同步模式可在 `StartRun` 内推进执行；P2 后台模式只持久化
  pending Run/DispatchWork，由 Runtime 随后推进。

## R2 执行交接

`PlanNode.required_check_ids == ()` 只表示该节点是中间任务交接点，不表示它是最终成果。`Orchestrator` 在
候选 Artifact 已持久化且该节点仍有后续图边时，可以通过 `PlanNode.accept_intermediate()` 将节点交给下游；
这一步不运行假 Gate、不创建 Checkpoint、不满足 Goal。最终单一 sink 必须携带 CompletionContract 的全部
required Check IDs，并由获批行为 Gate 决定节点和 Run 是否完成。

代码执行由 Infrastructure 的 `CodeRuntimeConnector` 负责真实 worktree 准备和代码捕获；Application 只拥有
节点、Attempt、Check/Gate 和 Run 状态，不把模型报告当作代码交付。Planner 的 `ExplorationBudget`/内部
`AgentBudget` 与 Worker 的 `LONG_RUNNING_AGENT_BUDGET` 和 Runtime 观察时钟是不同边界。

## 能力开放与验证

先通过正常入口使用应用能力；CLI 的 `get-plan`、`get-plan-checks`、`get-trace`、`get-discussion`
与 HTTP 共用 QueryService，`discuss-plan` 复用 ExecutionService 的事务和版本逻辑。
旧常驻测试已退役，唯一产品 E2E 待能力暴露后确认。实际失败需要定位时，单测写在仓库外临时目录，
不提交或迁回仓库。见 [测试策略](../../../tests/README.md)。
