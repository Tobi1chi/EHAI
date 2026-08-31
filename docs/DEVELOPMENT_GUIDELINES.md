# EHAI 开发与文档写作规范

## 领域语言

代码、API、数据库和文档必须使用一致的领域名词。不得用多个名称表示同一概念，也不得因实现方便而改变其语义。

- `Project`：长期工作空间，不是一次任务。
- `Goal`：用户希望满足的结果，不包含执行步骤。
- `CompletionContract`：执行前确认的版本化完成标准。
- `PlanRevision`：某一版本的计划图；重新规划必须创建新版本。
- `PlanNode`：可调度或可判断的计划单元，不是一次 Agent 调用。
- `Run`：对指定 PlanRevision 的一次执行。
- `Attempt`：Worker 执行 PlanNode 的一次尝试。
- `Event`：已经发生的不可变事实。
- `Artifact`：不可变的产物或证据引用。
- `Gate`：根据检查结果决定能否转换状态。
- `Checkpoint`：Gate 通过后创建的可恢复状态快照。

`PlanGraph` 与 `ExecutionTrace` 必须分离：前者描述预期路径，后者描述实际发生的 Attempt、事件和结果。

## 职责边界与不变量

- Planner 创建 PlanRevision、探索分支和 GraphPatch，但不直接执行节点。
- Orchestrator 调度、重试、取消和推进状态，但不改变 Goal 或降低完成标准。
- Worker 只提交候选结果、Artifact 和事件，不得直接标记节点或 Goal 完成。
- Checker 产生带证据的 CheckResult；Gate 根据策略作出状态转换决定。
- 没有通过必需 Gate，PlanNode 不得进入 `completed`。
- 已确认的 CompletionContract 不得被静默修改。
- 被剪枝的 Branch 必须保留历史轨迹。
- Checkpoint 必须引用确定的 PlanRevision、Run 和 Event Offset。

状态变更应经过领域方法或应用服务，禁止业务代码直接修改持久化字段。

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
docs/
```

领域层不得依赖具体 Worker SDK、数据库或 Web 框架。外部实现通过 Protocol/Adapter 接入。

## Python 代码规范

- 使用 `uv` 管理环境和运行命令，不使用裸 `pip`。
- 使用四空格缩进；公共 API 必须提供类型标注。
- 模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- 使用 Ruff 格式化和检查；配置集中在 `pyproject.toml`。
- 优先使用小型、显式、可组合的对象，避免未经需求验证的抽象层。
- 核心状态使用枚举和值对象，避免散落的字符串和布尔标志。
- 异常必须携带运行、节点或 Attempt 标识；不得静默吞掉错误。

## Commands、Events 与幂等

Command 使用祈使语义，例如 `StartRun`、`CancelAttempt`；Event 使用过去式，例如 `RunStarted`、`AttemptFailed`。Event 至少包含唯一 ID、类型、时间、Run ID、关联 ID、版本和载荷。事件处理器必须允许安全重放；外部副作用应使用幂等键。

## 测试与完成标准

- 使用 `pytest`，测试文件命名为 `test_*.py`。
- 单元测试覆盖领域状态机、Gate 和图不变量。
- 集成测试覆盖持久化、Event Replay、Connector 和 Check Runner。
- 每一期至少维护一个代表其退出条件的端到端场景。
- Bug 修复必须包含回归测试。
- 测试不得默认访问真实外部服务；使用 Fake Adapter 或受控 Fixture。

功能只有在代码、测试、必要文档和适用检查全部完成后才满足 Definition of Done。P1 保持必要验证；P2 至 P5 每期结束后执行系统性审查。

## 文档写作规范

- 文档使用清晰标题，先说明目的，再说明行为、边界和示例。
- 规范性要求使用“必须”“不得”“可以”，避免含糊措辞。
- 首次出现的领域名词使用固定英文名称，并给出中文解释。
- 示例必须与当前实现一致；未实现内容标注为“计划中”或对应阶段。
- 架构决策记录在 `docs/adr/`，文件名使用 `NNNN-short-title.md`。
- 功能或语义变化必须同步更新相关文档，禁止只修改代码。

## 提交与评审

使用 Conventional Commits：`type(optional-scope): imperative summary`。每个提交只包含一个逻辑变更。提交前检查 `git status` 和 diff，并运行适用的测试、格式化及 lint。Pull Request 应描述动机、实现边界、验证命令、风险和 Roadmap 归属；涉及界面时附截图，涉及模型变化时附迁移说明。未经用户明确要求，Agent 不得 amend、rebase、force-push 或 push。
