# 文档索引

更新：2026-11-07。一类结论只写在一个地方，其余文档链接过去。

## 阅读规则

文档分四层。**判断“现在是什么、要做什么”只读第 1 层**；第 2 层回答“怎么用、怎么设计”；
第 3、4 层是证据和存档，只在查证时翻，里面的“当前”“下一步”都属于当时的上下文，不是指南。

| 层 | 文档 | 只回答什么 |
| --- | --- | --- |
| 1 当前事实 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md) | 产品的目标形态：是什么、各模块拥有什么、不拥有什么（不写实现与顺序） |
| | [EXECUTION_MODEL](EXECUTION_MODEL.md) | Worker、阶段、Gate、状态、隔离与恢复的约定语义 |
| | [ROADMAP](ROADMAP.md) | 当前计划：现在做到哪、阶段顺序、完成条件、推进规则 |
| | [DECISIONS](DECISIONS.md) | 每条决定的日期、落点和现状（有效 / 已完成 / 已被取代 / 实现已放弃），以及待决事项 |
| | [STATUS](STATUS.md) | 已提交代码里每项能力是否实现、验证到什么程度 |
| 2 用法与设计 | 用法：[USAGE](USAGE.md)、[WORKFLOWS](WORKFLOWS.md)、[WORKSPACE_MANAGER](WORKSPACE_MANAGER.md)、[HUB](HUB.md)、[CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md)、[DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) | 已实现的命令、接口与限制 |
| | 设计：[AUTHORIZATION_DESIGN](AUTHORIZATION_DESIGN.md)、[ASSISTANT_DESIGN](ASSISTANT_DESIGN.md)、[CODEX_HARNESS_DESIGN](CODEX_HARNESS_DESIGN.md)、[APP_SHELL_DESIGN](APP_SHELL_DESIGN.md)、[P4_WORKFLOW_DESIGN](P4_WORKFLOW_DESIGN.md)、[P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md)、[PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md)、[PROCESS_OUTPUTS_DESIGN](PROCESS_OUTPUTS_DESIGN.md)；架构决定 [adr/](adr/) | 一个模块怎么设计；每份开头写明状态 |
| | 规则：[DEVELOPMENT_GUIDELINES](DEVELOPMENT_GUIDELINES.md)；编号来源：[PRODUCT_POINTS](PRODUCT_POINTS.md) | 开发、检查、产品 E2E 与提交规则；PRODUCT_SCOPE 中 `[编号]` 的出处 |
| 3 证据 | [EVIDENCE](EVIDENCE.md)、[records/](records/README.md) | 每次试用一行摘要；较长的执行叙述按主题存放，只追加 |
| 4 存档 | [history/](history/README.md)、[spikes/](spikes/) | 完成或被取代的计划、旧快照、技术探查 |

每份用法与设计文档开头有一行 `> 类型 · 状态`；状态写“目标，未实现”的，不能当作已有能力。

## 按角色阅读

| 你是 | 先读 | 需要时再读 |
| --- | --- | --- |
| 使用者 | [README 快速开始](../README.md#快速开始) → [USAGE](USAGE.md) | [WORKFLOWS](WORKFLOWS.md)、[多工作区](WORKSPACE_MANAGER.md)、[Connector](CONNECTOR_PROTOCOL.md)、[STATUS](STATUS.md) |
| 开发者（含 Agent） | [AGENTS](../AGENTS.md) → [ROADMAP](ROADMAP.md) → [开发规则](DEVELOPMENT_GUIDELINES.md) | [DECISIONS](DECISIONS.md)、[EXECUTION_MODEL](EXECUTION_MODEL.md)、各模块 README（`src/ehai/*/README.md`） |
| 外部顶层 Agent | [README 接入外部 Agent](../README.md#接入外部-agent) → [USAGE](USAGE.md#独立-mcp-服务端) | [STATUS](STATUS.md)，确认能力是否已验证 |
| 做产品/架构决定 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md) → [DECISIONS](DECISIONS.md) → [ROADMAP](ROADMAP.md) | [EXECUTION_MODEL](EXECUTION_MODEL.md)、[STATUS](STATUS.md)、设计文档 |

契约与客户端见 [schemas](../schemas/README.md) 与 [control-plane](../control-plane/README.md)。

## 术语

| 词 | 只指 | 不要混用 |
| --- | --- | --- |
| Run | 核心的计划执行：批准一个计划修订后启动的一次执行（`get-run`、`run_id`） | 不用来指生活 Workflow 的执行 |
| Workflow Run | 固定生活 Workflow 的一次执行（`get-workflow-run`、`workflow_run_id`） | 文档中始终写全称 |
| Routine | 按固定间隔触发 Workflow 的规则，每次触发产生一个 Workflow Run | 不是计划、Run 或调度器本身 |
| Attempt | 计划节点的一次执行尝试，属于某个 Run | — |

2026-10-02 决定：不重命名代码或接口，接口已用 `workflow-run` 前缀区分；文档按上表书写。

## 维护规则

- 新决定先在 [DECISIONS](DECISIONS.md) 加一行，再改它的落点文档；推翻旧决定时把旧行的现状改为“已被取代”，不删除。
- ROADMAP 只留当前计划；一个计划做完或被取代，整段移到 history/，在 DECISIONS 标“已完成”或“已被取代”。
- 用法只写已实现的命令和状态值；目标设计写在设计文档并在开头标明状态。
- 能力变化改 STATUS 的对应行；试用结果在 EVIDENCE 追加一行，较长叙述写进 [records/](records/README.md) 对应主题。
- 设计与用法文档不再追加执行记录。
- 代码与对应文档在同一变更中修改。
