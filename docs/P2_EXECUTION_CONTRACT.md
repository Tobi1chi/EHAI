# P2 执行契约与迁移基线

## 目的与范围

本文固定 `P2-I0` 后续 Increment 必须遵守的执行名词、状态所有权、API 行为和 Provider 入口。
本 Increment 不实现 Runtime、Scheduler、Worker Registry、新数据库模型或真实模型调用。P1 的同步
执行实现继续用于回归验证；`P2-I4` 才按本文固定的 `StartRun` 行为切换为后台 Runtime。

## 执行映射与状态所有权

映射固定为：一个 `PlanNode` 可以产生多次 `Attempt`；每个 `Attempt` 必须且只能绑定一个
`builtin_turn` 或 `external_execution`。Turn/Execution 是 Provider 执行句柄，不拥有
`PlanNode`、`Attempt` 或 `Run` 的领域状态。

`AttemptStatus` 是持久化生命周期；`AttemptActivity` 是非终态执行的活动观察，两者不得互相替代：

| 所有者 | 值 | 语义 |
| --- | --- | --- |
| Orchestrator | `pending/running/succeeded/failed/timed_out/cancelled/interrupted` | Attempt 生命周期 |
| Runtime/Connector observation | `queued/running/waiting/stalled` | 当前活动，不直接推进领域状态 |

Worker 正常返回或提交候选只能使 Attempt 得到候选结果。Checker 产生 `CheckResult`，Gate 是
`PlanNode` 和 `Run` 完成的唯一入口；Scheduler 和 Connector 不得判断完成。

## Connector Port 与 SessionPolicy

所有 P2 Connector 必须实现异步 `start`、`events`、`inspect`、`cancel`、`recover` Port：

- `start` 启动或解析幂等执行。
- `events` 从可选 Provider cursor 后输出标准化事件。
- `inspect` 只观察活动状态。
- `cancel` 请求取消，不直接写领域终态。
- `recover` 只恢复已引用的执行；找不到时不得创建替代执行。

`SessionPolicy` 只允许 `new`、`reuse`、`fork`。Capability 使用小写不透明名称并做显式集合匹配；
未知 requirement 可以保留，但在 Worker 未显式声明时必须 fail closed。具体 WorkerProfile、Endpoint、
SessionRef 和 ExecutionRef 结构属于 `P2-I1`，不在本 Increment 定义。

## StartRun 行为

P2 的 `StartRun` 必须在同一个事务中持久化新 `Run`、pending dispatch work 和 Command receipt，提交后
立即返回，不在请求调用栈中执行 Worker。相同幂等键和相同 fingerprint 必须返回同一个 `Run`，不得
创建第二个 Run 或第二份 dispatch work；同键不同 fingerprint 必须冲突失败。

`P2-I0` 只固定上述行为。pending dispatch work 的持久化模型和后台领取实现属于 `P2-I4`，因此本
Increment 不修改当前 P1 `ExecutionService.start_run` 的同步执行路径。

## Built-in Agent Provider 入口

P2 Built-in Agent 的唯一真实模型入口固定为官方 OpenAI Python SDK 的 Responses API。凭证只允许保存
引用 `env:OPENAI_API_KEY`；不得把密钥写入 PlanGraph、Event、Artifact 或数据库。模型名必须由
`WorkerProfile` 显式提供，不允许从环境、登录态或 Provider 默认值推导。SDK 依赖和真实调用属于
`P2-I3`，本 Increment 不提前引入。

## 失败后的选择规则

- `retry` 只用于 Runtime 能证明原执行未创建或没有执行句柄的 Attempt；未知写副作用一律不创建替代
  Attempt，并受原 Plan 的 Attempt 预算约束。
- `resume` 只继续已暂停的同一个 Run。受控取消、失败、中断或超时的节点可以重新进入 ready；Worker
  已成功但 Check/Gate 失败时不得用 resume 绕过验证。
- `Checkpoint restore` 只恢复同一 Run 最新、已持久化且通过 Gate 的 Checkpoint，恢复后保持 paused，
  由调用方显式 resume。
- `Replan` 面向 failed/cancelled 的终态 Run。调用方通过 `source_run_id` 显式选择证据来源；系统不猜测
  “最新 Run”。Planner 只接收有界、脱敏的 Attempt/Check/Checkpoint 摘要，产生保留 lineage 的 draft，
  仍需再次批准。
- 探索分支可以局部失败并由 Evaluator 比较剩余可行候选；若所有 active 分支均已失败，Orchestrator
  必须在创建 Evaluator Attempt 前将 Run 明确收敛为 failed，不能要求模型伪造空 BranchSelection。

## P1 迁移保护

P2 的数据库迁移必须从当前 P1 schema version 2 单向前进，并在 P1 数据库备份副本上验证。I0 不新增
表或改变 schema version。后续迁移不得削弱以下已持久化语义：

- Attempt 必须属于同一 Run 的 PlanRevision 中的 PlanNode。
- Check/Gate 必须引用同一 Run、PlanNode、Attempt 和证据 Artifact；Worker 成功不能绕过 Gate。
- Evaluator 的比较证据必须覆盖候选分支，Merge 只能读取明确选中的 Artifact。
- Checkpoint 必须引用确定的 PlanRevision、Run、通过的 Gate Event offset、分支选择和 Artifact。
- 恢复只能使用已持久化的最新 Checkpoint 和原执行事实，不得重复外部副作用或创建替代执行。

现有 P1 exploration E2E、单节点 StartRun 幂等测试和 recovery 集成测试是这些语义的迁移保护。
后续首次增加 P2 数据表时，必须用数据库备份 API 生成一致副本并同时验证迁移前后的上述事实。

## Built-in Planner 图操作 Tool 契约（P2-I14）

Built-in Planner 不再通过一次性 `submit_plan` 固定双分支模板。模型在本次 Planner 调用的普通内存图
中直接调用图操作 Tool 构造 PlanGraph：`add_plan_node`、`update_plan_node`、`remove_plan_node`、
`add_plan_edge`、`remove_plan_edge`、`set_plan_branch`、`inspect_plan`、`finish_plan`。Tool 操作不经过
HTTP 回调、不立即写数据库，也不存在 Plan IR/Operation/Patch 第二套图表达。只有 `finish_plan` 完整
校验通过后，应用层才通过现有 `build_plan_proposal()` 分配 UUID 并创建 CheckSpec、CompletionContract
和 draft PlanRevision；模型使用稳定本地 key，永远不生成 UUID。依赖只有一个模型侧真值：模型通过
`add_plan_edge` 声明 dependency，`finish_plan` 时确定性派生
`PlanNodeTemplate.required_dependency_keys`。

预算语义固定为：图修改操作共 128 次（被拒绝的修改同样计数），`inspect_plan`/`finish_plan` 不计数；
达到 128 次后不再接受修改但允许 inspect 和最后一次 finish；前 4 次 finish 完整校验失败把带本地 key
位置的 diagnostics 返回给模型并允许继续修复，第 5 次失败才以明确的 validation budget exhausted 终止。
diagnostics 结构为 `{"accepted": false, "issues": [{"code", "location", "message", "related_keys"}]}`，
错误位置必须使用模型能继续操作的本地 key，不能只返回 UUID 或 Python 异常文本。模型不得创建、删除、
降低或绕过 CompletionContract 与 Check；执行节点一律保留必要 Check。

## Provider 长程超时语义（P2-I14）

timeout 是连接/无进展检测，不是短时总任务寿命：

- Built-in Planner 默认不设 Tool Loop 的固定 wall-clock deadline；正常终止由 128 次图操作、4 次校验
  修复、明确取消和 Provider 终态决定。
- `OpenAIResponsesModelClient` 默认 stream idle timeout 300 秒；HTTP 请求失败最多重试 4 次；流中断
  最多重连 5 次。已获得 response_id 时必须 retrieve/恢复同一个 Response，不得重新创建重复 Response。
- queued/in_progress 表示仍在执行并继续等待；只有 completed、failed、cancelled、incomplete 等明确
  终态，或恢复次数耗尽，才结束本次 Provider 执行。
- 官方 Responses Background Mode 可用时，Built-in Planner 优先 background response + retrieve；兼容
  端点明确拒绝 background 时回退 stream=true、store=true 与现有 continuation 机制，回退不得静默创建
  重复请求或重复执行图操作。
- `ExecutionPolicy.absolute_attempt_timeout` 与 `max_run_duration` 默认 None，即绝对截止时间为调用方
  显式配置项（`ehai-api --attempt-deadline-seconds`）；heartbeat、no-progress、lease、cancel、用户
  显式 deadline 和 RetrySafety 对未知外部副作用的限制全部保留。

## 通用 Built-in Agent Runtime 契约（P2-I15–I18）

Built-in Agent Runtime 是 EHAI 内部所有模型驱动 Role 的唯一 Agent 地基。Planner、Worker、Evaluator、
Merge、Visualizer、Reviewer 和 Assistance 必须复用同一 ModelClient、Session/Event Store、Agent Loop、
Tool Registry、取消、预算、恢复与 Trace；Role 差异只能通过 Prompt、Tool Profile、Context Builder、
Finish Tool 和权限表达。任何模块不得为了特殊输出协议复制模型循环或另建 Session Store。

Runtime 提供统一能力目录，但 Session 只能获得 Role、Endpoint 与用户策略共同授权的 ToolSet。ToolSet 在
Session 创建时冻结并随恢复事实持久化；新增 MCP Server、Skill 或宿主 Shell 不能静默改变已存在 Session
的工具和权限。Skill 只提供指令与资源引用，不授予能力；MCP/Web/Shell/Git 结果都必须进入相同的有界、
脱敏 Tool Event。

Workspace 写操作只能作用于分配的 Workspace/worktree。统一文本 diff 可以创建、修改、删除和移动文件；
Shell 由 Endpoint 声明，Git 使用结构化 argv 并按只读、本地写、远端写和危险操作分类。能力存在不构成
授权：远端写、commit/merge/rebase、破坏性 Git 和越界文件操作必须遵守显式策略与审批。Tool 成功不等于
任务完成，角色仍须使用其 Finish Tool，Worker 结果仍须经过 Check/Gate。

Session Mailbox 只传递持久消息与 correlation，不共享可写 Workspace、不替代 Artifact，也不推进
PlanNode/Attempt/Run 状态。跨 Session 协作的产物必须通过 Artifact 引用；Orchestrator 仍是执行领域状态
推进的唯一入口。Visualization Role 只能从已校验 PlanRevision 派生可视化 Artifact，不能修改计划真值。

本阶段提前 MCP/Skill 的“运行时消费能力”，不提前 P5 的插件 SDK、市场、热安装、第三方 Agent Framework
或跨主机分布式协调。
