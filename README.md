# EHAI

EHAI（Enhanced Human-Agent Interface）是通用 Agent 平台的规划与执行核心。长期平台包含顶层通用
Agent、不同外部 Agent 框架、服务 Connector 和事件驱动 Routines；它们复用目标、方案、审批、
调度、检查、恢复与人工介入机制。

当前首先做实编码场景：用户与 Planner 对齐需求并审查详细方案，批准后在指定 workspace 调度 Worker，
最终获得代码和需求相关证据。编码不是平台的永久边界，顶层通用 Agent 也不等于 Planner。

> 当前状态：已有 P1/P1.1 与 P2 执行内核及历史验收记录；P2 正按新的产品范围重整，尚未完成
> 正常 CLI 下的完整交互与验收闭环。当前工作树已整合 Foundation 扩展代码，产品接入与验收仍待完成，P3 UI 尚未开始。
> 产品定义以 [Product Scope](docs/PRODUCT_SCOPE.md) 为准，操作与限制见 [Usage](docs/USAGE.md)。

## Why EHAI

现有 Agent 通常擅长执行单次指令，但复杂任务还需要明确的计划版本、探索边界、完成标准和恢复路径。
EHAI 将这些约束放入独立的 Execution Plane。目标是允许授权内自主探索，遇到无法决定或尝试耗尽时
挂起求助，而不是无限重试或把所有阻塞都当作终态失败。Check/Gate 只能证明其配置的条件，
不能用“文件存在”或“测试数量”替代用户需求是否得到满足。

## Existing Execution Foundations

以下是已有执行模块，不等于整个目标产品已完成；正常入口与专项 Adapter 的可用范围见 Usage。

- 使用版本化 `Goal`、`CompletionContract` 和 `PlanRevision` 对齐目标与完成标准。
- 使用非线性 `PlanGraph` 表达探索、分支、评估、剪枝和汇合。
- 通过 Codex Planner 与 External Worker Connector 生成计划和候选结果。
- 使用不可变 Artifact、Check 和 Gate 保存证据并决定状态转换。
- 使用 Event、ExecutionTrace 和 Checkpoint 支持审计、中断恢复与轨迹查询。
- 使用后台 Runtime、Scheduler、capacity、lease、deadline 和安全重试管理执行。
- 支持 Built-in Agent、Codex CLI 与 Codex App Server Thread/Turn Connector。
- 通过 CLI、HTTP API、OpenAPI/JSON Schema、SSE 和严格 TypeScript Client 暴露执行能力。

## Target User Flow

```text
CLI 目标 + 只读仓库调查 ↔ Planner 讨论与修订
          ↓
详细方案 + PlanGraph + CompletionContract → 用户审查 / 外部 Agent 意见
          ↓ 明确批准版本与执行边界
Orchestrator → Scheduler / Dispatcher → Worker → 代码与 Artifact
          ↓
需求相关 Check/Gate → 选中结果交付 + Checkpoint / ExecutionTrace
          ↘ 无法自主继续：挂起求助 → 用户回复 / 修订再批准
```

`PlanGraph` 描述预期执行路径；`ExecutionTrace` 保存实际发生的 Attempt、事件、检查和结果。Worker
只能提交候选结果；中间任务由宿主接收交接，最终 Run/Goal 必须通过获批 Gate 才能完成。此图描述目标流程；
多轮规划与设计版本已接入 CLI/API，规划侧代表性真实 CLI 试用已通过；完整执行人工回路和需求验收尚未完成。

当前 R1 入口：`discuss-plan` 接收目标和用户意见，`get-discussion` 查询持久讨论；
`get-plan` 返回该版本的 `design_document` 和图，`get-plan-checks` 查看实际检查配置。
用法、能力边界与兼容端点参数见 [Usage](docs/USAGE.md)。

R2 已新增前台 `execute-plan`、`resume-session` 和只读 `get-result`，接入实际代码快照、并行成果衔接、
最终行为 Gate 和失败修复；支持配置 Built-in 或 Codex App Server Worker。代表性 Built-in 真实 CLI
编码试用已通过：三槽并行、两个实际代码分支、选中成果整合和一个最终行为 Gate。
完整长运行、主动关闭后的恢复及 Codex App Server 实际编码仍待验证，不据此宣布整个 R2/P2 完成。

## Development Status

| 阶段 | 状态 | 范围 |
| --- | --- | --- |
| P1/P1.1 | 已完成 | 单 Worker 串行闭环、Codex CLI、分支评估、Check/Gate、Checkpoint、API/SSE |
| P2 | 执行地基已有，产品闭环重整中 | 方案讨论与批准、指定 workspace 编码、人工介入、真实入口验收 |
| 历史 Readiness Gate | 当时场景已通过 | 规划/执行 Smoke、失败 Replan、自举任务；不代表新 scope 完成 |
| 顶层通用 Agent | 职责已明确，交付增量待定 | 协助用户审查、查询、决策并调用平台，不替代 Planner |
| P3–P5 | 已规划 | UI、Workflow/Routines、外部服务事件与开放 Agent 生态 |

P2-I0–I9 已提供可恢复 Built-in Agent、OpenAI Responses ModelClient、受控并发 Scheduler、
Codex App Server 多 Session Connector、EHAI-owned Git worktree、超时/租约/安全重试与 Event Replay。
Git 分支写任务使用独立 worktree；非 Git 写任务串行，dirty EHAI worktree 会保留并产生 Event。
真实 Responses Smoke 仍需调用方显式提供有目标模型权限的 `OPENAI_API_KEY`；默认测试不访问外部服务。

## Quick Start

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。Standalone Built-in Agent 只需要
`OPENAI_API_KEY`（自定义兼容端点可设置 `OPENAI_BASE_URL`），不需要安装 Codex；Codex CLI/App
Server Worker 才要求本机安装并登录 Codex。

```powershell
uv sync
uv run ehai --help
uv run ehai-api --help
$env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI API key"
uv run ehai-api --database .ehai/p2.sqlite3 --artifacts .ehai/p2-artifacts `
    --worker builtin --worker-workspace (Get-Location).Path `
    --builtin-model gpt-5.6-luna --builtin-reasoning-effort high `
    --builtin-capacity 2 --p2-runtime

Set-Location control-plane
npm.cmd ci
npm.cmd run generate
npm.cmd run typecheck
```

完整的真实 Codex 执行流程、CLI 参数、HTTP API 和 SSE 示例见
[Usage Guide](docs/USAGE.md)。不要使用裸 `pip` 或裸 `python` 管理、运行本项目。

## Documentation

- [Product Scope](docs/PRODUCT_SCOPE.md)：产品定位、模块职责、用户流程和统一验收基线；优先阅读。
- [Usage Guide](docs/USAGE.md)：真实 Codex、CLI、API、SSE 和离线验证。
- [Roadmap](docs/ROADMAP.md)：P1 至 P5 的产品阶段、范围和退出条件。
- [P1 Implementation Plan](docs/P1_IMPLEMENTATION_PLAN.md)：P1 增量、验收与完成记录。
- [P2 Implementation Plan](docs/P2_IMPLEMENTATION_PLAN.md)：当前产品重整顺序，以及保留的历史执行增量。
- [Development Guidelines](docs/DEVELOPMENT_GUIDELINES.md)：领域语言、模块边界和开发规范。
- [Architecture Decision Records](docs/adr/)：持久化、接口和 Codex 通道等关键决策。
- [Codex CLI Spike](docs/spikes/codex-cli-local-channel.md)：真实 Codex 通道与验收观察。

### Architecture Notes

- [Domain](src/ehai/domain/README.md)：领域对象、不变量与状态所有权。
- [Application](src/ehai/application/README.md)：用例、规划、编排、调度与恢复边界。
- [Infrastructure](src/ehai/infrastructure/README.md)：外部服务、持久化、Worker 与 Workspace Adapter。
- [Interfaces](src/ehai/interfaces/README.md)：CLI、HTTP、SSE 与本地 Composition Root。
- [Schemas](schemas/README.md)：跨 Plane Schema、OpenAPI 与生成验证流程。
- [Control Plane](control-plane/README.md)：当前严格 TypeScript API Client 的范围与用法。

## Development

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
Set-Location control-plane
npm.cmd ci
npm.cmd run generate
npm.cmd run typecheck
npm.cmd run build
```

先暴露正常 CLI/API 能力，再与用户确定唯一产品 E2E。旧测试已退役，目前没有最终 E2E，不把无测试
收集当作验收通过。实际失败时再用仓库外临时单测定位，定位文件不提交。见 [测试策略](tests/README.md)。
