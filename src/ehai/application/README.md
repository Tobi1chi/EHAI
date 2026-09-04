# Application Architecture

`ehai.application` 把 Command 转成领域状态转换，并通过 Port 调用持久化、Artifact、Check 和 Worker。
它定义用例与执行顺序，但不包含 HTTP/CLI 表示，也不绑定 SQLite、Responses 或 Codex 传输。

## 主要职责

| 区域 | 文件 | 职责 |
| --- | --- | --- |
| Service | [`service.py`](service.py)、[`commands.py`](commands.py)、[`queries.py`](queries.py) | 处理幂等 Command、事务边界和只读查询。 |
| Planner | [`planner.py`](planner.py) | 定义 `Planner` Port、Plan 模板、预算和确定性单/双分支 Planner。 |
| Orchestrator | [`orchestrator.py`](orchestrator.py)、[`evaluation.py`](evaluation.py)、[`checks.py`](checks.py) | 推进节点/Attempt，持久化候选，评估分支，运行 Check/Gate 并创建 Checkpoint。 |
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

## 测试位置

- Planner 与分支协议：[`tests/unit/test_planner.py`](../../../tests/unit/test_planner.py)、
  [`tests/unit/test_evaluation.py`](../../../tests/unit/test_evaluation.py)。
- Orchestrator 与 Check/Gate：[`tests/unit/test_orchestrator.py`](../../../tests/unit/test_orchestrator.py)、
  [`tests/e2e/test_exploration.py`](../../../tests/e2e/test_exploration.py)。
- 后台 Runtime 与恢复：[`tests/integration/test_async_runtime.py`](../../../tests/integration/test_async_runtime.py)、
  [`tests/integration/test_recovery.py`](../../../tests/integration/test_recovery.py)。
- Scheduler、capacity 和 Workspace：[`tests/integration/test_scheduler.py`](../../../tests/integration/test_scheduler.py)、
  [`tests/integration/test_workspace_sessions.py`](../../../tests/integration/test_workspace_sessions.py)。
- Run Control：[`tests/integration/test_run_control.py`](../../../tests/integration/test_run_control.py)、
  [`tests/integration/test_pause_resume_race.py`](../../../tests/integration/test_pause_resume_race.py)。
- Built-in Agent：[`tests/unit/test_builtin_agent.py`](../../../tests/unit/test_builtin_agent.py)、
  [`tests/e2e/test_p2_runtime.py`](../../../tests/e2e/test_p2_runtime.py)。
