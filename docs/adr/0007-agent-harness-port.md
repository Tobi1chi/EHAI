# ADR 0007：多 Agent harness 的统一接入端口

- 状态：已接受（2026-10-01）；按下文迁移顺序实施，尚未实现
- 日期：2026-10-01
- 关系：细化 [ADR 0005](0005-external-agent-backends.md)（Runtime 外置）；取代
  [ADR 0003](0003-codex-cli-process-channel.md)（Codex 本地进程通道，已随 Codex 后端删除）

## 背景

EHAI 计划接入多种 Agent harness（Pi、Codex、Claude Code、OpenCode 等）。现状有可复用的部分，
也有绑定单一后端的部分：

| 现有抽象 | 位置 | 评估 |
| --- | --- | --- |
| `RuntimeConnector`（start / events / inspect / cancel / recover）与 `WorkerEvent` | `application/async_runtime.py` | 与后端无关，但只用于 Worker |
| `AgentRoleConfig`、`ToolRegistry`、`ToolDefinition`、`ToolExecutor` | `application/agent_roles.py`、`agent_contracts.py` | 角色、工具集和唯一结束工具已与后端无关 |
| `PiRoleRunner`、`PiPlannerAdapter` | `infrastructure/pi_runtime.py`、`planners/pi.py` | Planner、Reviewer、回退角色直接绑定 Pi |
| `pi_business_tools.mjs` | Pi 扩展 | 业务工具经 Pi 专用 RPC 通知通道注入 |
| `WorkerKind` 枚举、执行配置的 `worker_kind` 与后端专属字段 | domain、`ExecutionConfig` | 新后端需改核心枚举、配置结构和校验 |
| 运行中 Worker 请求（中途审批/输入） | 仅 Codex app-server 实现 | 单一后端的能力被做成全局功能；已随 Codex 删除 |

## 决定

### 1. 一个端口覆盖所有角色

定义 `AgentHarness` 端口，合并 `RuntimeConnector` 与 `PiRoleRunner`。Planner、Worker、Reviewer、
Evaluator、Assistance 等角色都经它运行；规划逻辑（图工具、校验重试、`raise_note`）上移为与
后端无关的角色实现，不留在某个适配器中。

| 方向 | 内容 |
| --- | --- |
| 输入 | `AgentRoleConfig`（提示词、工具集、结束工具、权限）、指令与上下文、工作区、模型设置、会话策略（new / reuse / fork）、Attempt 或角色调用身份 |
| 输出 | 归一化事件流（进度、心跳、等待、候选、完成、失败、阻塞、用量）、会话引用、恢复句柄 |
| 控制 | 取消、检查活动状态、按持久事实恢复；不提供"重新发送 prompt"的通用操作 |

完成只认结束工具，并在后端确认本轮已结束后由宿主校验。自然语言表示"完成"不算完成。
结果未知时保留未知状态，不换会话盲目重放（沿用 ADR 0005 与执行模型）。

### 2. EHAI 工具通过每会话 MCP 服务交付

EHAI 业务工具（提交候选、读写授权工作区、提出便签等）由宿主执行和持久化，harness 只负责调用。

- 每个 Attempt 或角色调用启动一个**只暴露该角色工具集**的 MCP 服务，绑定一次性令牌与调用身份；
  不能跨会话或跨 Attempt 调用。
- 工具 Schema 由 `ToolDefinition` 生成；按 harness 声明的 Schema 限制收窄，不在核心加后端分支。
- 不支持 MCP 的 harness 才使用专用桥（如现有 Pi 扩展桥）；专用桥必须实现与 MCP 相同的身份、
  授权和结果确认语义。

### 3. 能力清单与按能力调度

每个适配器登记能力清单，Dispatcher 按节点所需能力匹配，计划节点不写后端名称：

| 能力 | 含义 |
| --- | --- |
| `session.resume` / `session.fork` | 能否续用或分叉原生会话（对应 SessionPolicy） |
| `requests.interactive` | 能否在一轮中途发出审批/输入请求；不支持时人工介入统一走 Intervention |
| `tools.native.restrictable` | 原生文件/shell/git 工具能否关闭或按授权限制 |
| `usage.reported` | 是否上报用量 |
| `cancel.confirmed` | 取消后能否确认已停止 |
| `schema.*` | 工具 Schema 限制，例如不接受 `$defs`、`object\|null` 联合；全项目不使用 `uniqueItems` |

运行中 Worker 请求是可选能力，不是全局功能。未来某个 harness 支持时，经该能力接回 Inbox；
Inbox 不感知具体后端。

### 4. 安全边界留在 EHAI

harness 不是沙箱。工作区隔离（EHAI 拥有的 Git worktree）、凭证暴露和副作用控制由 EHAI 负责。
适配器把执行授权（allowed_commands、available_shells、git_permissions、工作区）翻译为 harness
配置；无法翻译或无法强制时拒绝启动，不静默放宽。

### 5. 执行配置按 harness 描述

新配置形状为 `harness: {kind, version, settings}`：

- `settings` 的 Schema 由适配器登记，核心只做闭合对象校验与指纹；新增 harness 不改核心枚举。
- 授权指纹覆盖整个 harness 配置；变化时不得静默沿用旧授权。
- 旧配置（`worker_kind` + `pi` 等字段）继续可读并保持原指纹，供已授权 Run 恢复。

### 6. 验收

- 新增 Scripted 适配器实现同一端口（包括经 MCP 调用工具），产品 E2E 改走该端口，使端口本身受 CI 保护。
- 每个真实 harness 按接入清单手动验收：工具 Schema 被 Provider 接受、实际工具调用、结束确认、
  取消、恢复、权限翻译与拒绝。结果在 [EVIDENCE](../EVIDENCE.md) 记一行。
- 不建立永久多 harness 测试矩阵（AGENTS.md 规定）；如需放宽由用户决定。

## 迁移顺序

每步只改结构、保持行为，产品 E2E 全程通过；执行记录写在 [重构计划](../REFACTOR_PLAN.md)。

1. 抽出共用部分：规划图结构离开 Codex 模块，脱敏函数改为中性名称（重构第二步）。
2. 删除 Codex 后端与运行中 Worker 请求路径；保留历史记录可读与已授权配置指纹（重构第二步）。
3. 定义 `AgentHarness` 端口，Pi 作为第一个实现；`RuntimeConnector` 与 `PiRoleRunner` 合并进来。
4. 规划逻辑上移为与后端无关的 `PlannerRole`。
5. 执行配置支持 `harness` 形状，旧形状保持可读与原指纹。
6. 新增 Scripted 适配器，产品 E2E 改走端口。
7. 工具交付改为每会话 MCP；先让 Pi 改走 MCP 并确认无退化。
8. 需要第二个 harness 时按接入清单接入（例如 Codex 或 Claude Code）。

## 后果

- 新增 harness 只需实现适配器、登记能力与配置 Schema，不改 Orchestrator、Scheduler 或执行配置核心。
- 删除 Codex 后，短期内只有 Pi 一个真实后端；第二个后端通过本端口重新接入，而不是恢复旧适配器。
- 运行中 Worker 请求的 CLI/HTTP/MCP 入口与 Inbox 类型随 Codex 删除；需要时作为可选能力重新设计。
- 每会话 MCP 服务增加进程与端口管理成本，需在第 7 步验证启动、清理和令牌隔离。
