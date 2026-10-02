# 文档索引

更新：2026-10-02。每个结论只写在一个地方，其余文档链接过去。

## 按角色阅读

| 你是 | 先读 | 需要时再读 |
| --- | --- | --- |
| 使用者 | [README 快速开始](../README.md#快速开始) → [USAGE](USAGE.md) | [WORKFLOWS](WORKFLOWS.md)、[多工作区](WORKSPACE_MANAGER.md)、[Connector](CONNECTOR_PROTOCOL.md)、[STATUS](STATUS.md) |
| 开发者 | [AGENTS](../AGENTS.md) → [开发规则](DEVELOPMENT_GUIDELINES.md) → [重构计划](REFACTOR_PLAN.md) | [EXECUTION_MODEL](EXECUTION_MODEL.md)、各模块 README（`src/ehai/*/README.md`） |
| 外部顶层 Agent | [README 接入外部 Agent](../README.md#接入外部-agent) → [USAGE](USAGE.md#独立-mcp-服务端) | [STATUS](STATUS.md)，确认能力是否已验证 |
| 做产品/架构决定 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md) → [EXECUTION_MODEL](EXECUTION_MODEL.md) → [ROADMAP](ROADMAP.md) | [STATUS](STATUS.md)、[EVIDENCE](EVIDENCE.md)、设计文档 |

## 文档分工

| 类别 | 文档 | 回答什么 |
| --- | --- | --- |
| 边界 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md) | 产品是什么、各模块拥有什么、不拥有什么 |
| 边界 | [EXECUTION_MODEL](EXECUTION_MODEL.md) | Worker、阶段、Gate、状态、隔离与恢复的语义 |
| 计划 | [ROADMAP](ROADMAP.md) | 优先顺序与各阶段完成条件 |
| 计划 | [REFACTOR_PLAN](REFACTOR_PLAN.md) | 当前重构步骤、产品 E2E 覆盖范围、待决事项 |
| 现状 | [STATUS](STATUS.md) | 每项能力是否实现、验证到什么程度 |
| 现状 | [EVIDENCE](EVIDENCE.md) | 每次试用做了什么、结果、未覆盖范围 |
| 用法 | [USAGE](USAGE.md) | 核心编码流程的命令与契约 |
| 用法 | [WORKFLOWS](WORKFLOWS.md) | 生活 Workflow 与 Routine |
| 用法 | [WORKSPACE_MANAGER](WORKSPACE_MANAGER.md) | 多工作区管理与 Planner 容量 |
| 用法 | [HUB](HUB.md) | Agent harness Hub：本机/独立运行、协议 v1、失败处理 |
| 用法 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md) | 外部 Connector 协议与 Google Calendar |
| 用法 | [DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) | Jev 双反馈环实验与固定 Pi 回退 |
| 设计 | [P4_WORKFLOW_DESIGN](P4_WORKFLOW_DESIGN.md) | 自定义 Workflow 目标设计（未实现） |
| 设计 | [P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md) | 慢环向独立 Project 转交修改的边界 |
| 设计 | [adr/](adr/) | 已采纳的架构决定 |
| 规则 | [DEVELOPMENT_GUIDELINES](DEVELOPMENT_GUIDELINES.md) | 开发、检查与提交规则 |
| 归档 | [history/](history/README.md)、[spikes/](spikes/) | 旧快照、原始试用记录、技术探查；不是当前指南 |

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

- 用法只写已实现的命令和状态值；目标设计写在设计文档并标明未实现。
- 能力变化改 STATUS 的对应行；试用结果在 EVIDENCE 追加一行，较长叙述写在对应计划的执行记录中。
- 代码与对应文档在同一变更中修改。
