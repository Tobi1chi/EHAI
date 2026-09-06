# EHAI Roadmap

## 产品目标与文档依据

长期目标是通用 Agent 平台；EHAI 的规划、审批、执行、检查、恢复与人工介入机制是平台核心。
编码是当前首先验证的完整场景，不是平台永久边界。产品职责和用户流程以
[Product Scope](PRODUCT_SCOPE.md) 为准；本文只划分阶段，不复制一套产品定义。
2026-09-06 确认的 [Execution Model](EXECUTION_MODEL.md) 细化 Worker 实例、阶段决策树及恢复规则；
其中 Phase 是单个任务内部的执行阶段，不是 Roadmap 的 P1–P5 产品阶段。

平台包含顶层通用 Agent、EHAI 核心、不同外部 Agent 框架，以及外部服务 Connector/Routines。
顶层 Agent 协助用户使用平台，不等于 Planner；Planner 是可被直接交互或调用的专门规划能力。
CLI、顶层 Agent、未来 UI 和 Routines 复用相同的公开应用语义，不各自拥有执行状态机。

## 总体原则

- 每期交付可验证的用户能力，不以组件数量、测试数量或模型调用成功作为完成标准。
- 区分目标设计、基线实现、分支实现和验收记录；历史 PASS 不覆盖后来确认的产品范围。
- Execution Plane 使用 Python；TypeScript Control Plane 拥有交互与展示状态，通过版本化 API、
  Command/Query/Event 和生成 Client 使用执行核心，不直接访问其数据库。
- PlanGraph 与 ExecutionTrace 分开；需求、接口、Gate 和授权保持批准基线，中间过程可自主调整并留痕。
- Worker 按预设经 Connector 实例化，框架、职责和模型分开；任务尽可能并行执行。
- 决策树包含阶段与分支 Gate；阶段 Reviewer 审查后，由获批的自动/人工 Gate 决定推进。
- 授权内的分支选择与安全重试自主完成；无法决定、无可行路线或预算耗尽时挂起求助。
- 每期定义范围、非目标、演示和退出条件。P2 至 P5 进行与当期变更相关的代码、架构和测试审查。

## P1：探索执行原型

**状态：** 历史增量已完成，包括 P1.1 修复；不代表新的交互式编码产品范围已通过验收。

**目标：** 验证 Goal、PlanRevision、Run/Attempt、候选 Artifact、分支选择、Check/Gate 和恢复的基本语义。

范围：

- 版本化 Goal/CompletionContract，非线性计划图和受控分支选择。
- Codex CLI Worker、串行 Orchestrator、SQLite 当前状态和追加式事件。
- 最小 CLI/API、Checkpoint 和公开 Schema。

历史退出场景为用户确认后执行双分支、比较并整合选中产物、通过检查并恢复轨迹。
原型固定模板、检查种类及终态恢复限制保留在
[P1 Implementation Plan](P1_IMPLEMENTATION_PLAN.md) 中，不作为通用平台的永久限制。

## P2：做实可审查、可干预的编码闭环

**状态：** 执行内核已有实现；当前产品闭环重新细化中，不能宣布 P2 整体完成。

**目标：** 用户从正常 CLI 提交真实编码目标，与 Planner 讨论和修订详细方案，批准后在确认的
workspace 执行，获得符合需求的代码、验证结果与交付证据。

范围：

- 规划时只读调查仓库，记录范围、约束、假设和偏好；用户可直接讨论或带回外部 Agent 的审查意见。
- 详细方案包含阶段、分支与 Gate、任务依赖、输入输出、探索规则、Reviewer 和授权边界。
- 批准固定需求、接口、Gate 和授权；中间过程调整可自主进行且记录版本，改变批准底线才重新批准。
- 复用多 Worker 调度、Built-in Agent 和 Codex Connector、Session、Git worktree 隔离与资源管理。
- 在授权与预算内自主选择或放弃分支、安全修复重试；无法自主继续时挂起并展示问题和证据。
- 用户回复后继续原方案，或批准修订方案；保留仍适用的成果与原执行轨迹。
- 阶段内共享 Session，跨阶段可新建；有效 handoff 可供新 Session 接手，无 handoff 则回退相关已完成节点。
- 发现可实现性 gap 用便签提示，无法自动判断的条件进入人工 Gate；禁止不可恢复的副作用。
- Provider、Role、工具、存储和恢复能力必须接入正常启动入口，不能只存在于测试构造器。
- 生成 Client、API 和事件接口继续为其他入口复用核心提供基础。

**演示目标：** 一个真实仓库编码任务，经需求讨论、用户修订、明确批准、隔离执行和实际代码验证后
交付；分支处理与人工介入如何进入唯一 E2E，待能力开放后与用户确定，不先扩建测试。

**退出条件：** 正常 CLI 路径可用；执行保持批准底线，过程调整可追踪；代码满足各必需 Gate；需人工决策的问题可
挂起、回复和继续；无遗留活跃执行或失控副作用。唯一产品 E2E 的真实用户路径证据可复核；
故障定位单测只放仓库外临时目录，不进入长期测试体系。

**非目标：** P3 UI、完整顶层通用 Agent 产品、P4 Workflow/Routines、插件市场、新外部 Agent 生态和
跨主机分布式调度。用户仍可携带外部审查意见；阶段 Review Agent 属于当前执行目标，
不把完整通用外部评审平台作为它的前置条件。

实现顺序、现有代码复用与历史增量见 [P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md)。

### 历史 Planning & Execution Readiness Gate

2026-09-04 的 I10–I13 记录了真实 Planner、双分支执行、失败证据 Replan、Codex Worker 和自举任务
通过的结果。这些证据仍有价值，但只覆盖当时的场景；部分验证由独立 acceptance runner 执行，不能
据此声称正常产品入口已实现完整需求验收和人工回路。详细记录保留在 P2 实施文档。

原先“该 Gate 通过即可进入 P3”的规则已由本次 P2 产品退出条件取代，不删除历史结果，也不重复开发
已经存在的能力。

### Built-in Agent Foundation 的位置

共享 Runtime/Role、Workspace/Shell/Git、Web/MCP/Skill、Mailbox 和 Visualizer 是支持核心流程的地基。
I14–I18 的分支实现需要对照正常入口重新验收；共享 Runtime 或角色枚举存在，不代表完整角色产品已交付。
工具接入不等于默认授权，Mailbox 不等于自动 spawn，MCP/Skill 消费不等于 P5 插件生态。

### 顶层通用 Agent 的交付安排

其产品职责已确定：与用户协作审查方案、查询执行、解释阻塞，并调用获授权的平台能力。
独立增量和交付阶段尚未固定，应在规划该增量时明确；不能静默归入 Planner、用 Assistance 枚举宣称完成，
也不能因未排期把它从平台长期范围删除。内部实现复用 Built-in Agent Runtime 和公开应用能力。

## P3：可观察、可干预的 Control Plane

**状态：** 目标设计，尚未开始 UI 实现。

**目标：** 让用户通过界面理解并控制同一核心流程，不另建执行引擎。

范围：

- TypeScript Dashboard、方案说明、PlanGraph 与实际 ExecutionTrace 叠加展示。
- 计划审查与修订、分支结果、Artifact、Check、Checkpoint 和最终代码交付展示。
- 暂停、继续、取消、重规划、审批、阻塞问题与用户回复。
- Projects、Settings、Calendar、Gantt、Activity Heatmap，以及节点级人—Agent 对话和反馈。
- 通过生成 Client 发送 Command/Query，通过 SSE 或 WebSocket 消费可恢复的版本化 Event。
- UI 框架与包管理器在实施前通过 ADR 固定；日历等展示不提前承担 P4 Routine 调度。

**退出条件：** 无需 CLI 即可创建任务、审查批准方案、观察执行、干预分支或阻塞并验收结果；
完成交互、性能和代码审查。

## P4：Workflow、外部服务与事件驱动 Routines

**状态：** 目标设计。

**目标：** 让用户组合 Agent 和外部服务形成可重复的自动化能力。

范围：

- Workflow 定义、模板、版本、运行历史；TypeScript 提供编辑体验，Python 校验并复用执行核心。
- 定时、周期和事件触发的 Routines；外部事件经 Connector、条件判断和授权后发起平台操作。
- 服务与事件 Connector Hub：Email、Weather、Navigation 和 Notification Service 等。
- 顶层 Agent 可参与理解复杂事件；简单规则或获批模板不强制每次调用 Planner。
- Secret、权限范围、审批、事件幂等、副作用补偿，以及 Workflow 级 Checkpoint 和恢复。
- 已获授权模板可自动运行；超出授权、新方案或无法决定时进入同一人工回路。

Agent 执行 Connector 与服务/事件 Connector 各守职责，不强行共用 Worker 协议。
收到邮件不等于获得执行权限，领域 Event 也不自动等于 Routine 触发器。

**退出条件：** 用户能配置一个外部事件驱动的真实工作流，自动执行授权任务、通知结果，并能够暂停、
恢复和审计；重复事件不造成重复业务副作用，未授权情况可请求批准。完成安全与代码审查。

## P5：开放扩展与高阶协作

**状态：** 候选范围，具体连接器和协作形式在对应增量中确认。

**目标：** 第三方无需修改核心代码即可扩展 Agent 协作平台。

候选范围：

- Claude Code、OpenCode 等 External Worker Connectors。
- External Assistance Agent Framework、OpenClaw 系列及职责待明确的 DSH 接入。
- Worker、Checker、Connector 插件 SDK，以及语言无关协议和 Python/TypeScript SDK。
- 多 Agent 协作、分层 Orchestrator、跨项目上下文与策略。
- 配额、权限、审计和沙箱，以及扩展安装、运行和兼容性边界。

**退出条件：** 第三方扩展通过稳定 SDK 安装运行；复杂协作仍具备明确授权、预算、证据、恢复和人工
介入路径。完成发布级架构、安全与稳定性审查。

## 阶段管理

新增或重构能力先说明用户路径、模块边界、阶段归属和验收，再决定实现。范围变化更新 Product Scope
和本文；操作变化同步 Usage，已验证交付同步 README。未确认的接口和状态明确标记待设计。
不得用固定节点示例、专项 Smoke、测试数量或分支提交名替代阶段退出条件，也不为未来阶段预建无调用方
的抽象或测试矩阵。
