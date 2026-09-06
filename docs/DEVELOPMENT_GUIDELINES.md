# EHAI 开发与文档写作规范

## 产品依据与模块细化

[Product Scope](PRODUCT_SCOPE.md) 是产品定位、职责和用户流程的基线；
[Execution Model](EXECUTION_MODEL.md) 是 2026-09-06 确认的 Worker、阶段决策树、Gate 与恢复语义；
[Roadmap](ROADMAP.md) 固定阶段，[P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md) 开头固定当前
重整顺序。历史增量、ADR 和原型测试按其适用范围使用，不能覆盖新 scope。
EHAI 是通用平台的规划执行核心，编码只是当前验证场景，不把长期平台缩成编码工具或把未来功能提前塞入 P2。

每个模块先说明用户目标、输入输出、调用方、状态与权限所有者、失败处理和正常入口验收，再决定复用、
接通或重构。不能凭类名、角色枚举、底层构造器或测试数量宣称产品完成。

## 领域语言

代码、API、数据库和文档必须使用一致的领域名词。不得用多个名称表示同一概念，也不得因实现方便而改变其语义。

- `Project`：长期工作空间，不是一次任务。
- `Goal`：用户希望满足的结果，不包含执行步骤。
- `CompletionContract`：执行前确认的版本化完成标准。
- `PlanRevision`：某一版本的执行计划；可读方案与计划图须对应同一版本，重新规划必须创建新版本。
- `PlanNode`：可调度或可判断的计划单元，不是一次 Agent 调用。
- `Worker`：按预设经 Agent 执行 Connector 创建、管理的 Agent 实例，不等于框架、模型、任务节点或 Session。
- `Worker 预设`：实例的职责、模型、工具、权限与运行配置；同一职责可以使用不同 Agent 框架。
- `Phase`：任务决策树内的执行阶段，以计划中的 Gate 界定，不是 Roadmap 的 P1–P5 阶段。
- `Session`：会话与上下文；阶段内共享、跨阶段可新建，具体 Provider 映射仍需实现。
- `Handoff`：经宿主确认并持久化的交接成果、上下文及剩余事项，不等于任意脏快照。
- `Run`：对指定 PlanRevision 的一次执行。
- `Attempt`：Worker 执行 PlanNode 的一次尝试。
- `Event`：已经发生的不可变事实。
- `Artifact`：不可变的产物或证据引用。
- `Gate`：统一自动与人工判定，依据获批条件及证据决定分支/阶段能否推进。
- `Checkpoint`：Gate 通过后创建的可恢复状态快照。

`PlanGraph` 与 `ExecutionTrace` 必须分离：前者描述预期路径，后者描述实际发生的 Attempt、事件和结果。
以上新增产品概念不表示已有同名类、API 或状态；不得只改字段名称就宣布目标已实现。

P2 路由术语：`WorkerProfile` 表示可调度的 Agent 配置；`WorkerEndpoint` 表示一个实际运行的
平台服务；`AgentSessionRef` 引用平台的长期上下文；`ExternalExecutionRef` 引用 Session 中与一个
Attempt 对应的一次 turn 或 job。平台原生 ID 通过这些引用保存，不进入 PlanGraph 语义。

## 职责边界与不变量

- 顶层通用 Agent 协助用户讨论、审查和使用平台；Planner 是专门规划能力，二者不能混为一谈。
- Planner 调查仓库、澄清需求、提出设计及验收建议，再形成或修订 PlanRevision、探索分支和节点能力
  要求；不执行编码节点，不自行批准，也不选择运行时 Endpoint。
- 可读方案与可执行图保持一致；批准固定需求、接口、Gate 和授权。Planner 可自主调整中间过程，
  包括跨节点拆分与依赖安排，须保存调整记录；改变批准底线才重新批准。
- Orchestrator 计算就绪节点并推进领域状态，但不负责平台容量与 Session 分配。
- Scheduler 管理可运行 Attempt 的队列、并发、重试、超时和资源预算；Dispatcher 根据能力、容量、
  Project 隔离和 Session 策略选择 WorkerProfile 与 WorkerEndpoint。
- Agent 执行 Connector 接入内部或外部框架并管理实例执行，不是 Worker 本身，不决定需求或 Gate。
- 服务/事件 Connector 不强制采用 Worker 协议；未来 Routines 根据事件与授权复用核心，外部内容不是授权。
- Worker 只提交候选结果、Artifact 和事件，不得直接标记节点或 Goal 完成。
- Checker 产生带证据的 CheckResult；Gate 根据策略作出状态转换决定。
- 阶段 Reviewer 进行测试审查并提供修改建议，不取代 Gate 或必需的人判断，不擅自增加验收要求。
- 没有通过必需 Gate，PlanNode 不得进入 `completed`。
- 已确认的 CompletionContract 不得被静默修改。
- 默认尽可能并行，Planner 必须细化依赖和隔离边界；共享阶段上下文不等于共享可写工作区。
- 有有效 handoff 可交给新 Session；无 handoff 的脏状态回到相关已完成节点，不丢其他有效并行成果。
- 禁止不可恢复操作，覆盖命令、patch、删除与覆盖等路径，不只禁用特定命令名。
- 被剪枝的 Branch 必须保留历史轨迹。
- Checkpoint 必须引用确定的 PlanRevision、Run 和 Event Offset。
- 自动重试/剪枝受批准条件、预算和副作用安全约束；无法决定、无可行路线或尝试耗尽时挂起求助，保存
  问题和证据。当前 failed/paused/Replan 限制需迁移，不用修改展示文案代替实现。

状态变更应经过领域方法或应用服务，禁止业务代码直接修改持久化字段。

## 技术栈与所有权边界

- Execution Plane 使用 Python，负责 Planner、Orchestrator、Worker、Check、Checkpoint、Event Store 和执行状态。
- Control Plane 使用 TypeScript，负责 Dashboard、图形交互、用户输入、展示模型和本地 UI 状态。
- Workflow 编辑器属于 TypeScript Control Plane；Workflow 校验、调度和副作用执行属于 Python Execution Plane。
- TypeScript 只能通过 API 发送 Command 和查询数据，不得直接读写 Execution Plane 数据库。
- Python 是执行领域状态的唯一真相来源；Control Plane 不得复制状态机或自行推断完成状态。
- TypeScript UI 框架和包管理器必须在开始 P3 前通过 ADR 选定，并提交对应锁文件。

## 项目结构

```text
src/ehai/
  domain/          # 领域实体、值对象和状态机
  application/     # 用例、Planner 与 Orchestrator 协调逻辑
  infrastructure/  # 数据库、事件、外部进程和连接器实现
  interfaces/      # CLI、API 和输入输出模型
tests/
  unit/
  integration/
  e2e/
control-plane/
  src/             # TypeScript UI、交互和展示模型
  tests/           # TypeScript 单元、组件和端到端测试
schemas/           # OpenAPI/JSON Schema 与生成配置
docs/
```

Python 领域层不得依赖具体 Worker SDK、数据库或 Web 框架。外部实现通过 Protocol/Adapter 接入。Control Plane 不得导入 Python 内部模型，必须使用从 `schemas/` 生成的 TypeScript 类型和 API Client。
CLI、顶层 Agent、UI 和 Routines 都通过公开应用能力操作核心，不各自实现审批或执行状态机。

## Python 代码规范

- 使用 `uv` 管理环境和运行命令，不使用裸 `pip`。
- 使用四空格缩进；公共 API 必须提供类型标注。
- 模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- 使用 Ruff 格式化和检查；配置集中在 `pyproject.toml`。
- 优先使用小型、显式、可组合的对象，避免未经需求验证的抽象层。
- 核心状态使用枚举和值对象，避免散落的字符串和布尔标志。
- 异常必须携带运行、节点或 Attempt 标识；不得静默吞掉错误。

## TypeScript 代码规范

- `tsconfig.json` 必须启用严格类型检查；不得用 `any` 绕过边界，应使用 `unknown` 并显式收窄。
- 变量和函数使用 `camelCase`，组件、类型和接口使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- API、Event 和领域查询类型必须生成，不得在 UI 中手写重复定义。
- 组件负责展示和交互；Command 构造、事件消费和展示模型转换放入独立模块。
- 不根据按钮点击预先改变执行真相；界面应等待 Execution Plane 返回状态或 Event。
- 格式化、lint、类型检查和测试必须通过 `control-plane/package.json` 中的项目脚本运行。

## API 与跨语言契约

跨 Plane 契约使用 OpenAPI 或 JSON Schema 并带显式版本。Python 生成或验证契约，TypeScript Client 和类型由契约生成。破坏性变化必须新建版本并提供迁移说明。REST 用于 Command 和查询；SSE 或 WebSocket 用于实时 Event。Event 必须携带可恢复订阅所需的 Event ID。

## Built-in Agent 能力复用规范

- 角色与框架解耦；选择 Built-in 承载模型推理、Tool 调用与 Session 时，必须复用通用 Built-in Agent Runtime；
  不得为 Planner、Worker、Evaluator、Merge、Visualizer、Reviewer 或 Assistance 复制 Agent Loop、
  ModelClient、Session Store、取消、预算、恢复或 Trace。
- 顶层通用 Agent 遵守相同框架接入与复用规则；其交付阶段需单独确认，不因基础 Role 配置存在而默认已交付。
- Role 特有行为只通过 Prompt、Tool Profile、Context Builder、Finish Tool 与权限表达。Tool Registry
  提供能力目录，实际 Session ToolSet 必须由 Role、Endpoint 与用户策略显式授权并在创建时冻结。
- Workspace、Shell、Git、Web、MCP、Skill 与 Session Message 使用统一 ToolDefinition/ToolExecutor 和
  Event 语义；新增能力不得绕过输出限制、凭证脱敏、Workspace 隔离或审批。
- Skill 是指令和资源，不是权限；Session Message 是协作输入，不是 Artifact 或完成证据；任何 Role 的
  Finish Tool 都不能绕过 CompletionContract、Check、Gate 或 Orchestrator 的状态所有权。
- Codex CLI/App Server 等外部平台继续通过 Worker Connector 接入，不因内部 Runtime 复用而复制或混合
  其平台状态机。

## Commands、Events 与幂等

Command 使用祈使语义，例如 `StartRun`、`CancelAttempt`；Event 使用过去式，例如 `RunStarted`、`AttemptFailed`。Event 至少包含唯一 ID、类型、时间、Run ID、关联 ID、版本和载荷。事件处理器必须允许安全重放；外部副作用应使用幂等键。

## 测试与完成标准

- 先暴露正常 CLI/API 能力，再与用户确定唯一产品 E2E；仓库只保留这一条最终 E2E 的必要测试代码。
- 当前 E2E 尚未制定。旧 unit、integration、contract、smoke 和原型 E2E 已退役，不恢复或迁移到其他
  仓库目录，也不提前另建专项测试体系。
- 正常使用或 E2E 失败时，必要的定位单测放在仓库外临时目录，用 `uv run pytest <临时文件>` 运行。
  修复后复测原失败路径，临时测试不提交，也不默认永久保留为回归测试。
- 不为推测风险、低概率组合、覆盖率或模型协议提前堆测试；不创建 Fixture Framework 或测试矩阵。
- E2E 不得替换生产装配、代办用户决策、预写获胜方案或答案；消息消费和需求相关代码行为必须来自真实路径。
- 精简开发测试不等于删除产品运行时 Check/Gate；后者仍负责获批任务的验收。
- Ruff、format、mypy 及生成 Client 构建仍是适用的静态检查。纯文档检查内容、相对链接和 diff。
- 真实调用显式运行，失败先区分代码、配置和外部服务，不降低断言或无限重试。

能力已暴露、可实际使用、已通过最终 E2E 是不同状态。未实现或未运行 E2E 不得报告为产品通过；
阶段完成仍须满足产品退出条件，而不是只减少了测试文件或通过了静态检查。

## 文档写作规范

- 文档使用清晰标题，先说明目的，再说明行为、边界和示例。
- 规范性要求使用“必须”“不得”“可以”，避免含糊措辞。
- 首次出现的领域名词使用固定英文名称，并给出中文解释。
- 示例必须与当前实现一致；未实现内容标注为“计划中”或对应阶段。
- 产品范围更新先修改 Product Scope，再同步 Roadmap 和当前实施计划；Usage 仅写真实入口，README
  区分已有地基与目标流程。历史记录明确当时范围，不能与当前产品状态混写。
- 架构决策记录在 `docs/adr/`，文件名使用 `NNNN-short-title.md`。
- 功能或语义变化必须同步更新相关文档，禁止只修改代码。

## 提交与评审

使用 Conventional Commits：`type(optional-scope): imperative summary`。每个提交只包含一个逻辑变更。提交前检查 `git status` 和 diff，并运行适用的测试、格式化及 lint。Pull Request 应描述动机、实现边界、验证命令、风险和 Roadmap 归属；涉及界面时附截图，涉及模型变化时附迁移说明。未经用户明确要求，Agent 不得 amend、rebase、force-push 或 push。
