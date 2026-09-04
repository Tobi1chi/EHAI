# EHAI Roadmap

## 产品目标

EHAI（Enhanced Human-Agent Interface）用于建立可规划、可探索、可检查、可恢复且允许人类干预的 Agent 执行环境。Roadmap 按可验收的纵向能力划分；每一期都必须形成独立的产品闭环，而不是只完成一组孤立组件。

## 总体原则

- P1 优先证明最小闭环，不做过度工程化，也不单独进行系统性代码审查。
- P2 至 P5 每期结束后进行代码、架构和测试审查。
- Execution Plane 使用 Python，拥有 Agent 执行状态和领域规则；Control Plane 使用 TypeScript，拥有交互与展示状态。
- 两个 Plane 通过版本化 API、Command/Event Schema 和生成类型通信，不直接读写对方的数据存储。
- `PlanGraph` 表示计划，`ExecutionTrace` 表示实际轨迹；二者分开存储、可叠加展示。
- Worker 只能提交候选结果，任务完成必须由预先确认的检查条件决定。
- 每一期必须定义范围、非目标、演示场景和退出条件。

## P1：可运行的探索闭环

**状态：** 已完成（包含真实端到端验收及 P1.1 语义修复）。

**目标：** 验证从目标对齐到自动执行、检查和恢复的完整链路。

详细编码顺序和验收方法见 [P1 Implementation Plan](P1_IMPLEMENTATION_PLAN.md)。

范围：

- Project、Goal、PlanRevision、PlanNode、Branch、Run、Attempt 等核心模型。
- 非线性 PlanGraph，支持分支、汇合、选择和剪枝。
- Planner 生成计划和版本化 `CompletionContract`。
- Orchestrator 计算就绪节点并调度 Worker。
- Codex External Worker Connector。
- Event、Artifact、Check、Gate 和 Checkpoint 服务。
- SQLite 持久化及中断恢复。
- Python 实现的最小 CLI/API 和事件流，用于确认计划、启动、暂停和查看结果。
- 为 Command、Event 和查询模型建立语言无关、可版本化的 Schema。

退出条件：用户确认目标和检查条件后，系统能让 Codex 探索至少两个分支，自动比较结果，完成最终检查，创建 Checkpoint，并在重启后恢复轨迹。

非目标：正式 TypeScript Control Plane、多 Worker、分布式执行、通用插件系统和复杂权限模型。

## P2：稳定的多 Worker 执行内核

**状态：** 多 Worker 执行内核与 Planning & Execution Readiness Gate 已完成；进入 P3 前先完成
Built-in Agent Foundation 扩展。

**目标：** 将 P1 原型升级为可靠、可扩展的 Agent Runtime。

详细编码顺序和验收方法见 [P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md)。

范围：

- Built-in Worker Agent Framework。
- 统一 Worker Adapter 与能力声明协议。
- Codex App Server External Worker Connector，并保留现有 Codex CLI Connector。
- Orchestrator 保留就绪判断和领域状态推进；Scheduler 管理执行队列与并发；
  Dispatcher 根据能力与容量选择 Worker Endpoint。
- 将每个 Attempt 持久化绑定到外部 Agent Session 和一次 provider execution，并通过事件、心跳、
  查询和租约跟踪运行状态。
- 并发、重试、超时、取消和资源预算。
- 分支上下文与 Git worktree 隔离。
- Artifact、日志、上下文摘要和 Event Replay。
- Check Runner 静态注册与恢复测试。
- 稳定 OpenAPI/JSON Schema，并建立 TypeScript 类型和 API Client 的生成流程。

退出条件：同一 PlanRevision 能混合调度多个 Worker；运行失败或进程重启后可恢复；每个决策均能追溯到事件和证据。完成首次系统性代码审查。

### P2→P3 Planning & Execution Readiness Gate

**状态：** 已通过（2026-09-04）。随后确认的 Built-in Agent Foundation 扩展成为新的 P3 前置门禁，
因此 P3 尚未进入实现。

**目标：** 在开发 Control Plane 前，把 P1/P2 的规划、执行、干预、恢复和重规划能力调整到可用于
真实项目的稳定状态，并用 EHAI 开发 EHAI 的一次小型代码任务证明完整闭环。

范围：

- 真实 Built-in Planner 根据受控仓库上下文生成、校验并持久化 PlanGraph。
- 真实 Built-in/Codex Worker 在隔离 Workspace 中执行小型代码任务。
- 验证分支选择、选中变更晋升、组合 Check、Gate、Checkpoint 和可读 Trace。
- 对可安全重试、未知副作用、Checkpoint 恢复和带失败上下文的版本化 Replan 分别验收。
- 完成一次 `Goal → Plan approval → isolated implementation → verification → selection → delivery`
  自举闭环。

退出条件：真实规划 E2E、真实执行 E2E、失败恢复/重规划 E2E 和自举代码任务 E2E 全部通过；没有
pending/running Attempt 或活跃 Workspace lease；最终变更通过人工 Gate 和仓库完整验证。

验收结果：真实 `gpt-5.6-luna/high` Planner、Built-in 双分支执行、失败证据 Replan 与 EHAI 自举代码
任务均通过；真实 Codex CLI Worker Smoke 通过。自举任务的两个实现分支并发且隔离，Evaluator 选择后
Merge 精确复现 selected ChangeSet，宿主完整验证通过，用户 main 工作树未被自动修改。

本 Gate 不实现 P3 UI、P4 Workflow、插件生态、新 Worker Provider 或跨主机分布式调度。详细增量见
[P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md) 的 `P2-I10` 至 `P2-I13`。

### P2→P3 Built-in Agent Foundation 扩展

**状态：** 已确认，待实现。P3 UI 暂不开始，先将现有 Built-in Worker 提炼为所有内部 Agent 角色
共用的运行地基。

**目标：** Planner、Worker、Evaluator、Merge、Visualizer、Reviewer 和 Assistance Role 共用同一套
ModelClient、Session/Event Store、Agent Loop、Tool Registry、取消、预算、恢复和 Trace；角色之间只
配置 Prompt、Tool Profile、Context Builder 与 Finish Tool，禁止各自复制 Agent Loop。

范围：

- 完整 Workspace 能力：读取、搜索、创建、修改、统一 diff、删除、移动与创建目录。
- 可按 Endpoint 选择的 Shell，以及覆盖全部 Git 子命令的结构化 Git Tool；实际权限继续按角色、沙箱
  和审批策略授予，远端写入与危险操作不得因“通用能力”而默认放行。
- 统一 Web Search Tool；MCP Tool Schema 接入；Skill 说明与资源加载。提前的是运行时消费能力，P5 的
  插件 SDK、市场、动态安装与第三方生态仍不在本阶段。
- 持久 Session Mailbox，使已存在的 Agent Session 能发送、读取和等待消息；跨 Workspace 协作通过
  Message 与 Artifact，不直接写入其他 Session 的工作区。
- Planner 使用工作区读取、分析命令和 PlanGraph Tool；Visualizer 从已校验的 draft PlanRevision 生成
  可视化 Artifact；用户批准后仍由现有 Orchestrator/Scheduler 调度 Worker。

退出条件：至少一个真实任务能完成 `Goal → Agent-assisted planning → validated PlanGraph → visualization
→ human approval → multi-Worker execution → Check/Gate/Checkpoint`；参与角色共用同一 Agent Runtime，
Session 通信、权限、恢复和 Trace 可从持久证据复核。

## P3：可观察、可干预的 Control Plane

**目标：** 让用户通过可视界面理解并控制 Agent 的执行过程。

范围：

- 使用 TypeScript 构建 Control Plane；启用严格类型检查，具体 UI 框架和包管理器通过 ADR 固定。
- Dashboard 与实时 PlanGraph/ExecutionTrace 可视化。
- 分支结果、Artifact、Check 和 Checkpoint 展示。
- 暂停、继续、取消、重新规划和人工 Gate。
- Projects、Settings、Calendar、Gantt 和 Activity Heatmap。
- 节点级人—Agent 对话与反馈。
- 使用生成的 API Client 发送 Command，通过 SSE 或 WebSocket 消费带版本的 Event。
- Control Plane 不直接修改 Execution Plane 数据库或复制其领域状态机。

退出条件：用户无需 CLI 即可完成任务创建、计划确认、过程观察、分支干预和最终验收。完成交互、性能与代码审查。

## P4：可复用工作流与外部连接

**目标：** 将一次性任务扩展为可重复运行的自动化能力。

范围：

- Workflow 定义、模板、版本和运行历史。
- TypeScript 提供 Workflow 编辑体验，Python 负责校验、调度和实际执行。
- Routines：定时、事件触发和周期执行。
- Universal Connector Hub。
- Email、Weather、Navigation 和 Notification Service。
- Secret、权限范围、审批、幂等与副作用补偿。
- Workflow 级 Checkpoint 和恢复。

退出条件：用户能组合 Agent 与外部服务形成可重复工作流，并安全地定时执行、暂停、恢复和审计。完成安全与代码审查。

## P5：开放生态与高阶自主

**目标：** 建立无需修改核心代码即可扩展的 Agent 协作平台。

候选范围：

- Claude Code、OpenCode External Worker Connectors。
- External Assistance Agent Framework 与 OpenClaw 系列。
- DSH 接入；其职责和边界明确后再固定具体位置。
- Worker、Checker 和 Connector 插件 SDK。
- 语言无关的扩展协议，以及 Python/TypeScript 对应的 SDK。
- 多 Agent 协作及分层 Orchestrator。
- 跨项目上下文、策略、配额、权限、审计和沙箱。

退出条件：第三方扩展可通过稳定 SDK 安装运行；复杂协作任务仍具备明确权限、预算、证据和恢复路径。完成发布级架构、安全及稳定性审查。

## 阶段管理

新功能必须归属一个阶段，并说明是否影响当前退出条件。未通过本期端到端验收前，不提前实现后续阶段的大型能力。范围变化应通过 Roadmap 变更记录，而不是静默扩大实现范围。

每个 P2 Increment 完成后，只把已经验证可用的能力同步到 README，并更新对应限制；设计中或尚未
验收的能力继续保留在 Roadmap，不得在项目首页写成已交付功能。
