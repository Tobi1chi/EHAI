# Domain Architecture

`ehai.domain` 是 Execution Plane 的领域核心。这里的对象拥有状态转换和不变量，不依赖数据库、
HTTP 框架、OpenAI/Codex SDK 或具体 Worker 实现。Application 层可以编排这些对象，但不能在对象外
伪造状态转换。

以下说明现有领域对象；目标语义见 [Product Scope](../../../docs/PRODUCT_SCOPE.md)。
R1 已将可读设计绑定到 PlanRevision，保存、批准与恢复保留同一版本内容。
“挂起等待人工”与失败终态、任务检查与最终验收的对应关系仍需迁移细化。
不要从产品词语推断已有同名状态枚举，也不要因现有原型枚举有限而缩减产品 scope。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| [`goal.py`](goal.py) | `Project`、版本化 `Goal` 与 `CompletionContract`；保存目标、完成条件及确认关系。 |
| [`planning.py`](planning.py) | `PlanRevision`、`PlanNode`、`Edge` 与 `Branch`；验证图结构、探索分支和节点/分支状态转换。 |
| [`execution.py`](execution.py) | `Run` 与 `Attempt` 生命周期；区分计划状态和实际执行状态。 |
| [`workers.py`](workers.py) | Worker Profile/Endpoint、能力、Session/Execution 引用和运行活动状态。 |
| [`workspaces.py`](workspaces.py) | Workspace 引用与 Lease 生命周期；表示隔离范围和占用状态。 |
| [`artifacts.py`](artifacts.py) | 不可变 Artifact 元数据、归属、内容摘要和大小。 |
| [`checking.py`](checking.py) | `CheckSpec`、`CheckRun`、`CheckResult`、`GateDecision`、`Gate` 与 `Checkpoint`。 |
| [`events.py`](events.py) | 不可变领域 Event 及其类型；记录已发生事实，不承担命令处理。 |
| [`runtime.py`](runtime.py) | 持久化后台 `DispatchWork` 状态，用于 Runtime 调度与恢复。 |

## 不变量与状态所有权

- `Goal` 和 `CompletionContract` 决定“完成”需要什么证据；`PlanRevision` 只能引用已确认且版本匹配的
  Contract。
- `PlanRevision` 拥有节点、边和 Branch 状态。`Run`/`Attempt` 只描述实际执行，不反向改写图结构。
- 状态只能通过对应聚合的方法转换；非法跳转、终态回退、跨 Run/Node 绑定和无效时间戳会被拒绝。
- Artifact 元数据一经创建即不可变，并绑定产生它的 Run、Attempt 和 PlanNode；内容存储由 Port 提供。
- Check 产生 `CheckResult`，Gate 根据必需 Check 作出决定，Checkpoint 固化可恢复状态。Event 记录这些
  已提交的事实。
- Worker 只执行 `WorkerRequest` 并提交候选 Artifact。它不能把 PlanNode 或 Run 标记为完成，也不能
  绕过 Check/Gate。只有 Orchestrator 在必需 Check 完成且 Gate 通过后才能完成节点，最终完成 Run。
- Workspace Lease、Worker Session/Execution 引用和 DispatchWork 分别拥有自己的生命周期；释放或恢复
  不等于业务完成。

旧领域单测已退役；状态规则仍由产品代码执行，不因测试精简而移除。
实际失败时在仓库外写临时定位单测；长期仅保留待确认的产品 E2E，见 [测试策略](../../../tests/README.md)。
