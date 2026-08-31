# P1 Implementation Plan

## 目标

P1 要交付一个可运行的 Python Execution Plane：用户创建 Goal，与 Planner 对齐 `PlanRevision` 和 `CompletionContract`，确认后由 Orchestrator 调用 Codex 执行包含探索分支的计划；系统根据检查证据选择路线，在 Gate 通过后创建 Checkpoint，并能在进程重启后恢复。

P1 验证的是端到端语义，不追求通用平台能力。TypeScript Control Plane、并发执行、多 Worker、分布式调度和通用插件系统不在本期范围内。

## P1 技术边界

- Execution Plane 全部使用 Python，环境和命令统一通过 `uv`。
- SQLite 保存当前执行状态和追加式 Event Log；P1 不实现完整 Event Sourcing。
- `PlanGraph` 保存计划，`ExecutionTrace` 从 Attempt、Event 和 Artifact 构建，二者不得混用。
- P1 同一时刻只执行一个 Attempt。多个就绪分支按稳定顺序运行；并发留到 P2。
- Command、Event 和查询模型在 `schemas/v1/` 中提供语言无关 Schema，为后续 TypeScript Client 做准备。
- Worker 只能提交候选结果；Gate 是进入 `completed` 的唯一入口。

## 代码结构

```text
src/ehai/
  domain/
    goal.py              # Goal 与 CompletionContract
    planning.py          # PlanRevision、PlanNode、Edge、Branch
    execution.py         # Run、Attempt 与状态机
    checking.py          # CheckSpec、CheckResult、Gate
    events.py            # Event Envelope 与领域事件
  application/
    commands.py          # 应用 Command 与处理器
    queries.py           # 只读查询模型
    planner.py           # Planner Port 与计划用例
    orchestrator.py      # 就绪计算、调度与状态推进
    checkpointing.py     # 检查、提交与恢复流程
  infrastructure/
    sqlite/              # Repository、事务和 Schema 版本
    workers/             # Fake Worker 与 Codex Adapter
    checks/              # 确定性和语义检查 Adapter
    artifacts/           # Artifact 文件存储
  interfaces/
    cli.py               # 本地操作入口
    api.py               # Command/Query API 与事件流
schemas/v1/
tests/{unit,integration,e2e}/
```

依赖方向固定为 `interfaces/infrastructure → application → domain`。Domain 不得依赖数据库、Web 框架、Codex SDK 或进程调用实现。

## 核心状态机

```text
PlanNode: pending → ready → running → candidate → verifying
                                              ├→ completed
                                              └→ failed / ready(retry)
                         pending/ready ─────────→ pruned

Attempt: pending → running → succeeded | failed | timed_out | cancelled
Run:     pending → running ↔ paused → completed | failed | cancelled
```

`Attempt.succeeded` 只表示 Worker 正常返回。只有必需 CheckResult 满足 Gate 后，PlanNode 才能进入 `completed`；只有最终 Gate 满足 CompletionContract 后，Goal 才能进入 `satisfied`。

## 编码增量

### I0：仓库与工具基线

交付：

- 创建 `pyproject.toml`、`uv.lock`、`src/`、`tests/` 和 `schemas/v1/`。
- 配置 Ruff、pytest 和基础类型检查；确定支持的 Python 版本。
- 建立统一 ID、UTC 时间和 JSON 序列化约定。
- 用 ADR 记录 SQLite 策略、最小 API 方式和 Codex 调用通道。
- 对 Codex 调用做一个限时技术验证，只确认启动、输入、取消和结果捕获是否可行，不提前实现完整 Adapter。

验证：包可导入，最小测试、lint 和格式检查均可通过。

建议提交：`build: initialize Python execution plane`

### I1：领域模型与图不变量

交付：

- 实现 Project、Goal、CompletionContract、PlanRevision、PlanNode、Edge 和 Branch。
- 定义边类型：依赖、探索候选、条件和汇合。
- 验证节点引用、无非法环、必需依赖和分支汇合关系。
- CompletionContract 和已批准 PlanRevision 不可静默修改；重新规划产生新版本。
- 实现 Run、Attempt、Check、Gate、Checkpoint 的状态转换方法。

重点测试：非法转换、未通过 Gate 就完成节点、修改已批准契约、无效 Edge、剪枝后历史丢失。

建议提交：`feat(domain): define P1 execution model`

### I2：SQLite、Event 与 Artifact

交付：

- Repository 和 Unit of Work Port，以及 SQLite Adapter。
- 当前状态表与追加式 Event Log 在同一事务中提交。
- Event Envelope 包含 ID、类型、时间、版本、Run ID、关联 ID 和载荷。
- Artifact 以不可变文件保存，数据库只保存元数据、校验信息和路径。
- Checkpoint 保存 PlanRevision、Run 状态、分支选择、Artifact 引用和 Event Offset。

重点测试：事务回滚、Event 顺序、重复 Command 幂等、Artifact 不可变、Checkpoint 引用一致。

建议提交：`feat(storage): persist execution state and events`

### I3：单节点纵向闭环

交付：

- 实现应用 Command：创建 Project/Goal、提出计划、批准计划、启动 Run。
- 使用 Deterministic Planner 和 Fake Worker 跑通单节点任务。
- Orchestrator 计算 ready 节点，创建 Attempt，并接收候选结果。
- 使用最小通过 Gate 完成节点和 Run。
- CLI 能创建、批准、启动并查询运行。

重点测试：一个真实 SQLite 数据库上的端到端 happy path，以及 Worker 失败后的状态。

建议提交：`feat(runtime): execute a single-node plan`

### I4：Check、Gate、Checkpoint 与恢复

交付：

- 实现 Command Check、Artifact Check 和 P1 最小 Semantic Check。
- 每次 CheckRun 保存状态、输出、证据和失败原因。
- Gate P1 只支持清晰的 `all-required` 策略，复杂评分留到后续。
- Gate 通过后原子提交节点状态和 Checkpoint。
- 进程启动时识别未结束 Attempt，将其标记为 interrupted，并按策略等待重试或人工恢复。
- 支持暂停、继续、取消以及从最新 Checkpoint 恢复。

重点测试：检查失败不得创建 Checkpoint、恢复不重复副作用、重启后 Event Offset 连续。

建议提交：`feat(checkpoint): verify and resume runs`

### I5：Codex External Worker Connector

交付：

- 定义 P1 `WorkerAdapter`：执行、事件输出、取消和候选结果返回。
- 将 PlanNode 指令、上下文、CompletionContract 和 Artifact 输入映射到 Codex。
- 将输出、补丁、日志和错误转换为统一 Artifact/Event。
- 保留原始 Worker 输出作为调试 Artifact，但不得把敏感环境变量写入日志。
- 使用受控假进程完成自动化契约测试；真实 Codex 测试作为显式启用的 smoke test。

重点测试：正常完成、非零退出、超时、取消、无效结构化结果和输出截断。

建议提交：`feat(worker): add Codex connector`

### I6：非线性 Planner 与分支选择

交付：

- Planner 通过结构化输出提出 PlanRevision 和 CompletionContract，并在持久化前验证 Schema 和图不变量。
- 用户批准前不得启动 Run。
- 支持 `fork → explore → evaluate → select/prune → merge`。
- P1 默认限制分支宽度为 3、深度为 2，并记录预算消耗。
- 每个分支先通过局部 Gate；Evaluator 根据预先定义的准则生成选择结果和证据。
- 未选择分支进入 `pruned`，但保留 Attempt、Event 和 Artifact。
- 重新规划通过 GraphPatch 产生新的 PlanRevision，旧轨迹保持可查询。

重点测试：两个分支均成功、一个分支失败、全部失败、选择证据缺失、分支预算耗尽。

建议提交：`feat(planner): support exploratory plan branches`

### I7：API、Schema 与运行轨迹

交付：

- 用同一 application service 暴露 CLI 和最小 HTTP API，禁止在接口层复制业务规则。
- Command：创建 Goal、生成/批准计划、启动、暂停、继续和取消 Run。
- Query：读取 PlanGraph、ExecutionTrace、Check、Checkpoint、Artifact 元数据和当前状态。
- 提供基于 Event ID 的事件流及断线续传。
- 将公开 Command、Event 和 Query 模型导出到 `schemas/v1/` 并进行契约测试。

重点测试：API 不能绕过批准或 Gate；旧 Event ID 可恢复订阅；Schema 与实际响应一致。

建议提交：`feat(api): expose P1 execution controls`

### I8：P1 验收与收尾

使用真实 Codex 运行一个受控场景：

```text
创建 Goal 和 CompletionContract
→ Planner 提出两个探索分支
→ 用户批准
→ Codex 依次执行两个分支
→ 分支局部检查
→ Evaluator 选择并剪枝
→ 合并节点执行
→ 最终 Gate
→ Checkpoint
→ 重启进程并恢复完整轨迹
```

收尾要求：

- 自动化端到端测试使用 Fake Worker，可重复且不依赖网络。
- 真实 Codex smoke test 单独执行并保存结果摘要。
- 运行完整 pytest、Ruff 和格式检查。
- 更新 README 的安装、运行、演示和已知限制。
- 只修复阻止 P1 闭环的问题，不在本阶段开展通用化重构。

建议提交：`test(e2e): verify P1 exploration loop`

## P1 API 最小表面

具体 URL 由实现 ADR 固定，但应用层只需要以下能力：

```text
CreateProject
CreateGoal
ProposePlan
ApprovePlan
StartRun
PauseRun / ResumeRun / CancelRun
GetRun
GetPlanGraph
GetExecutionTrace
ListEvents(after_event_id)
```

暂不加入用户账户、组织、多租户、权限角色、Workflow、Routine 或第三方 Service Connector。

## P1 完成条件

- 用户可在执行前确认 PlanRevision 和 CompletionContract。
- PlanGraph 至少包含两个探索分支，并能选择、剪枝和汇合。
- Codex 只能提交候选结果，不能直接宣布节点或 Goal 完成。
- 局部 Gate 和最终 Gate 都产生可追溯证据。
- Gate 通过后才能提交 Checkpoint。
- 中途停止并重启后，系统可从持久化状态恢复且不重复已确认副作用。
- PlanGraph 与 ExecutionTrace 可分别查询，所有关键决策能追溯到 Event 和 Artifact。
- 自动化端到端测试、完整测试、lint 和格式检查通过。

## 风险控制

- **Codex 调用不确定性：** I0 先验证传输通道；核心逻辑始终使用 Fake Worker 测试。
- **分支爆炸：** 固定宽度、深度和预算；P1 不并发执行。
- **模型输出漂移：** 所有 Planner/Worker 结构化输出先经过 Schema 和领域验证。
- **自我检查偏差：** 确定性检查优先；Semantic Check 必须保存评分准则和证据，不能作为不可解释的布尔值。
- **恢复时重复操作：** Command 使用幂等键，状态与 Event 原子提交，Checkpoint 记录 Event Offset。
- **范围膨胀：** 新需求若不直接服务于 P1 退出条件，记录到 P2+，不进入当前实现。

## 开始编码前仅需确认的决策

1. Codex Connector 通过哪一种本地调用方式接入。
2. P1 的最小 HTTP API/事件流采用哪一个 Python 实现方案。
3. Planner 是否复用 Codex 通道，还是使用单独的模型 Provider；无论选择哪种方式，两种角色仍保持独立接口。

这些决策不阻塞 I0 的仓库初始化、Domain 编码和 Fake Worker 纵向闭环。
