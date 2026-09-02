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
