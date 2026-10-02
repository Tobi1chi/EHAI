# ADR 0007：经 Hub 接入多种 Agent harness

- 状态：已接受（2026-10-02 修订：加入 Hub 层，Hub 为独立服务）；迁移第 1–3 步已完成，其余未实现
- 日期：2026-10-01
- 关系：细化 [ADR 0005](0005-external-agent-backends.md)（Runtime 外置）；取代
  [ADR 0003](0003-codex-cli-process-channel.md)（Codex 本地进程通道，已随 Codex 后端删除）

## 背景

EHAI 计划接入多种 Agent harness（Pi、Codex、Claude Code、OpenCode 等），且不修改 harness 本身，
只使用它们公开的配置与控制接口。现状有可复用的部分，也有绑定单一后端的部分：

| 现有抽象 | 位置 | 评估 |
| --- | --- | --- |
| `RuntimeConnector`（start / events / inspect / cancel / recover）与 `WorkerEvent` | `application/async_runtime.py` | 与后端无关，但只用于 Worker |
| `AgentRoleConfig`、`ToolRegistry`、`ToolDefinition`、`ToolExecutor` | `application/agent_roles.py`、`agent_contracts.py` | 角色、工具集和唯一结束工具已与后端无关 |
| `PiRoleRunner`、`PiPlannerAdapter` | `infrastructure/pi_runtime.py`（已拆入 Hub）、`planners/pi.py` | Planner、Worker、Reviewer、过程/轨迹审查、路由回退都直接调用 Pi |
| `pi_business_tools.mjs` | Pi 扩展 | 业务工具经 Pi 专用 RPC 通知通道注入 |
| `WorkerKind` 枚举、执行配置的 `worker_kind` 与后端专属字段 | domain、`ExecutionConfig` | 新后端需改核心枚举、配置结构和校验 |
| 运行中 Worker 请求（中途审批/输入） | 仅 Codex app-server 实现 | 单一后端的能力被做成全局功能；已随 Codex 删除 |

## 决定

### 1. 三层结构：核心 → Hub → 兼容层

```text
EHAI 核心（Orchestrator / Scheduler / Gate / 授权 / 角色定义 / 工具执行）
   │ ① Hub 接口：核心唯一依赖的接入面
Hub（会话生命周期、每会话 MCP 端点、事件归一化、取消与恢复、能力汇总、配置隔离）
   │ ② 兼容层接口：每种 harness 实现一份
Pi 兼容层 │ Scripted 兼容层（E2E）│ 以后的 Claude Code / Codex / OpenCode 兼容层
   │ 只用公开 CLI/SDK、配置文件与控制接口
原样不动的 harness
```

| 层 | 拥有 | 不拥有 |
| --- | --- | --- |
| 核心 | 计划、批准、调度、Gate、成果；角色定义；工具的执行与持久化；全部持久事实 | harness 进程、协议细节、原生输出格式 |
| Hub | 运行中会话表（调用身份 ↔ 原生会话 ↔ 进程）、每会话 MCP 端点与令牌、事件归一化、能力汇总 | 任何业务状态；不判定完成；不持久化 |
| 兼容层 | 某个 harness 的启动参数、隔离配置目录、事件解析、权限翻译、取消与续用方式 | 工具语义、业务规则 |

Planner、Worker、Reviewer、Evaluator、过程/轨迹审查、路由回退等所有角色都经 Hub 运行。
规划逻辑（图工具、校验重试、`raise_note`）上移为与后端无关的角色实现，不留在兼容层中。

### 2. Hub 接口（核心 → Hub）

| 操作 | 内容 |
| --- | --- |
| `start` | 调用身份（Attempt 或角色调用）、角色规格（提示词、工具清单、结束工具、权限）、指令与上下文、工作区、模型设置、会话策略（new / reuse / fork）、恢复引用 → 会话句柄 |
| `events` | 归一化事件：进度、心跳、工具调用、结束工具调用、本轮已结束、失败、阻塞、用量、原生会话引用 |
| `inspect` / `cancel` | 检查活动状态；取消并报告能否确认已停止 |
| `recover` | 按核心传回的持久引用重新接上会话；接不上时明确报告，不猜测 |
| `capabilities` | 各兼容层能力清单的汇总，供 Dispatcher 匹配 |

- 工具调用以 `tool_call` 事件交给核心，核心执行并持久化后回传结果，Hub 再交还 harness；Hub 只转交。
  每会话 MCP 端点（第 4 条）实现后，MCP 调用也转成同样的事件。
- 协议 v1 已实现 start、steer、prompt、events、tool-results、cancel、inspect、close（见 [HUB](../HUB.md)）；
  `recover` 与 `capabilities` 尚未实现。
- 接口参数只用可序列化数据（角色规格由核心从 `AgentRoleConfig` 转换得到），不传对象引用，
  使 Hub 可以运行在另一个进程或另一台机器上（见第 6 条）。
- Hub 不持久化 EHAI 事实：原生会话引用和恢复句柄随响应和事件返回，由核心记录在 Attempt 或角色调用上，
  `recover` 时再交给 Hub。Hub 重启只丢失进程表，不丢失事实。harness 自己的原生历史（如 Pi 会话文件）
  属于 harness，留在 Hub 一侧的状态目录。
- 不提供"重新发送 prompt"的通用操作。完成只认结束工具，并在兼容层确认本轮已结束后由核心校验；
  自然语言表示"完成"不算完成。结果未知时保留未知状态，不换会话盲目重放（沿用 ADR 0005 与执行模型）。

### 3. 兼容层接口（Hub → 兼容层）

| 操作 | 内容 |
| --- | --- |
| `launch` | 在独立配置目录中启动 harness，挂上 Hub 提供的 MCP 端点、角色指令和翻译后的权限 |
| `parse_events` | 把原生输出转换为 Hub 的归一化事件 |
| `cancel` / `resume` | 使用 harness 自身的取消与续用机制 |
| `capabilities` / `settings_schema` | 声明能力清单与本 harness 的配置格式 |

兼容层不修改 harness 安装和用户的全局配置，只写自己的隔离配置目录。

### 4. EHAI 工具经每会话 MCP 端点交付

- 每次 Attempt 或角色调用由 Hub 开一个**只暴露该角色工具集**的 MCP 端点，绑定一次性令牌与调用身份；
  不能跨会话或跨 Attempt 调用，会话结束即关闭。
- 工具 Schema 由 `ToolDefinition` 生成；Hub 按兼容层声明的 Schema 限制收窄，不在核心加后端分支。
- 不支持 MCP 的 harness 才在兼容层内使用专用桥（如现有 Pi 扩展桥）；专用桥必须实现与 MCP 相同的
  身份、授权和结果确认语义，并同样回调核心执行。
- 核心向 harness 的方向（启动、注入指令、取消、续用）只经兼容层的控制接口，不经 MCP。

### 5. 能力清单与按能力调度

每个兼容层登记能力清单，Dispatcher 经 Hub 汇总按节点所需能力匹配，计划节点不写后端名称：

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

### 6. 进程形态：Hub 是独立服务

2026-10-02 用户决定 Hub 一开始就作为独立服务，为远端执行留出位置（原方案为先进程内）。

- Hub 是 `ehai-hub` 进程，代码在 `src/ehai/hub/`，经 HTTP 上的版本化协议（v1）与核心通信，
  见 [HUB](../HUB.md)。核心发起全部连接：启动、长轮询事件、回传工具结果、取消、关闭，
  因此 Hub 可以在另一台机器上，核心不需要对外开放端口。
- 未配置远端 Hub 时，核心按需启动一个只监听本机的 Hub 子进程（随机令牌），并在核心退出时一起退出；
  使用方式与原来相同。配置 `EHAI_HUB_URL` 与 `EHAI_HUB_TOKEN` 后改用该 Hub。
- harness 凭证只从 Hub 进程自己的环境读取，不经协议传输。
- 导入方向由 CI（`lint-imports`）检查：核心只导入 `ehai.hub.protocol` 与 `ehai.hub.client`；
  Hub 与兼容层不导入核心模块（唯一例外是纯函数 `application.sanitization`）。
  过渡期例外：核心仍直接读取 Pi 配置（`ehai.hub.adapters.pi.config`、`probe`）做授权指纹与本机探查，
  在第 6 步执行配置改为 `harness` 形状后移除。

### 7. 失败处理

| 情况 | 处理 |
| --- | --- |
| harness 或 Hub 中途崩溃 | 核心看到结果未知，按现有规则保留并请求核对，不换会话重放 |
| Hub 重启 | 核心用持久引用逐个 `recover`；接不上的会话明确报告 |
| 兼容层无法翻译或强制某项授权 | Hub 在启动前拒绝，核心记为调度失败，不静默降级执行 |
| MCP 端点收到错误令牌或跨身份调用 | 拒绝并记录，不转交核心 |

### 8. 安全边界留在 EHAI

harness 不是沙箱。工作区隔离（EHAI 拥有的 Git worktree）、凭证暴露和副作用控制由核心负责。
兼容层把执行授权（allowed_commands、available_shells、git_permissions、工作区）翻译为 harness
配置；无法翻译或无法强制时拒绝启动，不静默放宽。

### 9. 执行配置按 harness 描述

新配置形状为 `harness: {kind, version, settings}`：

- `settings` 的 Schema 由兼容层登记，核心只做闭合对象校验与指纹；新增 harness 不改核心枚举。
- 授权指纹覆盖整个 harness 配置；变化时不得静默沿用旧授权。
- 旧配置（`worker_kind` + `pi` 等字段，以及无执行效果的 `codex_server` 块）继续可读并保持原指纹，
  供已授权 Run 恢复。

### 10. 验收

- Scripted 兼容层实现同一兼容层接口（包括经 MCP 端点调用工具），产品 E2E 改走 Hub，使 Hub 本身受 CI 保护。
- 每个真实 harness 按接入清单手动验收：工具 Schema 被 Provider 接受、实际工具调用、结束确认、
  取消、恢复、权限翻译与拒绝。结果在 [EVIDENCE](../EVIDENCE.md) 记一行。
- 不建立永久多 harness 测试矩阵（AGENTS.md 规定）；如需放宽由用户决定。

## 迁移顺序

每步只改结构、保持行为，产品 E2E 全程通过；执行记录写在 [重构计划](../REFACTOR_PLAN.md)。

1. 抽出共用部分：规划图结构离开 Codex 模块，脱敏函数改为中性名称（重构第二步，已完成）。
2. 删除 Codex 后端与运行中 Worker 请求路径；保留历史记录可读与已授权配置指纹（重构第二步，已完成）。
3. 建立 `hub/` 包、Hub 服务与两层接口；Pi 相关代码（`pi_runtime` 的进程一半、`pi_rpc`、`pi_config`、
   扩展桥）移入 Pi 兼容层；所有角色调用点改为经 Hub；加入导入方向检查。保持行为（2026-10-02 完成）。
4. 新增 Scripted 兼容层，产品 E2E 改走 Hub。
5. 规划逻辑上移为与后端无关的 `PlannerRole`。
6. 能力清单；执行配置支持 `harness` 形状，旧形状保持可读与原指纹。
7. Hub 提供每会话 MCP 端点，Scripted 先用；确认锁定版本的 Pi 是否支持 MCP，支持则改走 MCP 并确认无退化，
   不支持则保留扩展桥。
8. 需要第二个 harness 时按接入清单编写兼容层（例如 Claude Code 或 Codex）。
9. 远端执行：Pi 设置中的路径与工作区目前须在 Hub 所在机器上存在，核心仍在本机校验 Pi 配置；
   跨机器使用需先完成第 6 步，并决定工作区如何到达 Hub 一侧。

## 后果

- 新增 harness 只需编写兼容层、登记能力与配置 Schema，不改 Orchestrator、Scheduler、Hub 接口或执行配置核心。
- 核心只依赖 Hub 接口一处；harness 的升级、故障和协议差异止于兼容层。
- Hub 无持久状态，恢复依赖核心记录的引用；兼容层必须能从这些引用续上会话，否则如实报告无法恢复。
- 多一个进程和一次 HTTP 往返；本机子进程模式下由核心管理其生命周期，强杀核心后 Hub 与 harness 随之退出。
- 删除 Codex 后，短期内只有 Pi 一个真实后端；第二个后端通过兼容层重新接入，而不是恢复旧适配器。
- 运行中 Worker 请求的 CLI/HTTP/MCP 入口与 Inbox 类型随 Codex 删除；需要时作为可选能力重新设计。
- 每会话 MCP 端点增加端口与令牌管理成本，需在第 7 步验证启动、清理和令牌隔离。
