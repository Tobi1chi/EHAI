> 历史快照（2026-09-15 归档），非当前操作说明。当前状态见 [STATUS](../STATUS.md)。

# EHAI

EHAI（Enhanced Human-Agent Interface）是通用 Agent 平台的规划与执行核心。长期平台包含顶层通用
Agent、不同外部 Agent 框架、服务 Connector 和事件驱动 Routines；它们复用目标、方案、审批、
调度、检查、恢复与人工介入机制。

当前首先做实编码场景：用户与 Planner 对齐需求并审查详细方案，批准后在指定 workspace 调度 Worker，
最终获得代码和需求相关证据。编码不是平台的永久边界，顶层通用 Agent 也不等于 Planner。

> 当前状态：已有 P1/P1.1 与 P2 执行内核及历史验收记录；P2 正按新的产品范围重整，尚未完成
> 正常 CLI 下的完整交互与验收闭环。当前工作树已整合 Foundation 扩展代码，产品接入与验收仍待完成，P3 UI 尚未开始。
> 产品定义以 [Product Scope](../PRODUCT_SCOPE.md) 为准，操作与限制见 [Usage](../USAGE.md)。
> 2026-09-06 的 [执行模型共识](../EXECUTION_MODEL.md) 明确 Worker 实例化、阶段/分支 Gate、
> 过程自主调整、阶段 Session 与 handoff 恢复；这是当前目标，不是已交付功能清单。

2026-09-14 起按 [ADR 0005](../adr/0005-external-agent-backends.md) 迁移为上层编排框架：
由完整 Pi 后端拥有具体 Agent Loop、模型调用和上下文管理。当前已接通无模型调用的
`inspect-agent` 控制通道检查；Worker、Planner 和 Reviewer 已接线到 Pi，自研 Runtime 和
Responses Adapter 已删除。真实模型、工具与产品 E2E 尚未验证，不把接线当成迁移验收。
安装、用法与实跑范围见 [Pi 后端迁移记录](../PI_BACKEND_MIGRATION.md)。

提供 Pi 配置后，Planner 默认选择 Pi；API 的 P2 宿主也默认选择 Pi Worker。
规划模型可独立指定；缺少必要配置明确报错，不静默生成固定模板。显式无模型演示后端仍保留。

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
阶段决策树 + PlanGraph + CompletionContract → 用户审查 / 外部 Agent 意见
          ↓ 批准需求、接口、Gate 和授权；中间过程可追踪地自主调整
Orchestrator → Scheduler / Dispatcher → 按预设实例化的 Worker
          ↓ 尽可能并行探索，各路径有独立分支 Gate
阶段 Review Agent → 阶段 Gate（自动 / 人工判断）→ 下一阶段
          ↓
最终验收 → 选中结果交付 + Checkpoint / ExecutionTrace
          ↘ 发现 gap：便签提示；无法继续时等待人判断
```

`PlanGraph` 描述预期执行路径；`ExecutionTrace` 保存实际发生的 Attempt、事件、检查和结果。Worker
只能提交候选结果；中间任务由宿主接收交接，最终 Run/Goal 必须通过获批 Gate 才能完成。此图描述目标流程；
多轮规划与设计版本已接入 CLI/API，规划侧代表性真实 CLI 试用已通过；完整执行人工回路和需求验收尚未完成。
Worker 是经 Connector 按预设创建的 Agent 实例，不是 Connector、模型名称、任务节点或 Session。
阶段内共享 Session，跨阶段可新建；有效 handoff 可交给新 Session，未交接的脏状态则回到相关已完成节点。
主规划模型偏好为 GPT-6 Astra，明确执行任务偏好为 GPT-5.6 Luna/max；不改写下文历史试用配置。

当前 R1 入口：`discuss-plan` 接收目标和用户意见，`get-discussion` 查询持久讨论；
`get-plan` 返回该版本的 `design_document` 和图，`get-plan-checks` 查看实际检查配置。
用法、能力边界与兼容端点参数见 [Usage](../USAGE.md)。

R2 已新增前台 `execute-plan`、`resume-session` 和只读 `get-result`，接入实际代码快照、并行成果衔接、
最终行为 Gate 和失败修复；当前选择 Pi 或 Codex App Server Worker。以下为迁移前证据：Built-in 真实 CLI
编码试用已通过：三槽并行、两个实际代码分支、选中成果整合和一个最终行为 Gate。
已补充真实模型受控试用：固定 Gate 失败后自主修复，以及 Ctrl+C 暂停后同一 Run 立即恢复、
不重跑已完成的上游任务。Codex App Server 也已完成真实 CLI 并行编码、Ctrl+C 和同一 Run 立即恢复，
由 5.5 Planner 配合 5.6 Luna Server Worker 通过获批 Gate，并保留中断代码和已完成任务。
任意关窗/强杀、文件写操作中途恢复及完整长运行仍待验证，不据此宣布整个 R2/P2 完成。
证据与边界见 [R2 实施记录](../R2_IMPLEMENTATION_PLAN.md)。

## Development Status

| 阶段 | 状态 | 范围 |
| --- | --- | --- |
| P1/P1.1 | 已完成 | 单 Worker 串行闭环、Codex CLI、分支评估、Check/Gate、Checkpoint、API/SSE |
| P2 | 执行地基已有，产品闭环重整中 | 方案讨论与批准、指定 workspace 编码、人工介入、真实入口验收 |
| 历史 Readiness Gate | 当时场景已通过 | 规划/执行 Smoke、失败 Replan、自举任务；不代表新 scope 完成 |
| 顶层通用 Agent | 职责已明确，交付增量待定 | 协助用户审查、查询、决策并调用平台，不替代 Planner |
| P3–P5 | 已规划 | UI、Workflow/Routines、外部服务事件与开放 Agent 生态 |

P2-I0–I9 曾提供可恢复 Built-in Agent、OpenAI Responses ModelClient（两者已退役）、受控并发 Scheduler、
Codex App Server 多 Session Connector、EHAI-owned Git worktree、超时/租约/安全重试与 Event Replay。
Git 分支写任务使用独立 worktree；非 Git 写任务串行，dirty EHAI worktree 会保留并产生 Event。
历史 Responses Smoke 不作为当前 Pi 验收。真实 Pi 模型调用需要私有配置及授权的环境凭证。

## Quick Start

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node >=22.19.0。
先按 [Pi 配置指南](../USAGE.md) 创建仓库外的 backend.json、settings.json 和 models.json。
凭证仅通过 backend.json 的 environment_names 白名单传入，不写入配置文件；示例使用
OPENAI_API_KEY，自定义端点由 Pi models.json 配置，不再由 EHAI 读取 OPENAI_BASE_URL。
Codex CLI/App Server 后端仍需要单独安装并认证 Codex。

```powershell
uv sync
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund
uv run ehai --help
uv run ehai-api --help
$env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI API key"
uv run ehai-api --database .ehai/p2.sqlite3 --artifacts .ehai/p2-artifacts `
    --worker pi --worker-workspace (Get-Location).Path `
    --pi-config C:/private/ehai-pi/backend.json `
    --agent-model "<exact-model-id>" --agent-reasoning-effort high `
    --worker-capacity 2 --p2-runtime

Set-Location control-plane
npm.cmd ci
npm.cmd run generate
npm.cmd run typecheck
```

已有 API 宿主时，另一个终端可以直接使用 CLI 客户端，无需编写 HTTP 脚本：

```powershell
uv run ehai --api-url http://127.0.0.1:8000 get-runtime-health
uv run ehai --api-url http://127.0.0.1:8000 get-worker-profiles
uv run ehai --api-url http://127.0.0.1:8000 get-result --run-id '<run-id>'
```

API 模式不访问本地数据库，不启动额外的 Pi/Scheduler；规划、批准、启动、运行控制和查询均由原宿主
处理。客户端退出不会关闭宿主或自动暂停任务。完整工作流、执行授权 JSON 和定向挂起命令见
[CLI API 模式](../USAGE.md#cli-连接正在运行的-api-宿主)。

完整的真实 Codex 执行流程、CLI 参数、HTTP API 和 SSE 示例见
[Usage Guide](../USAGE.md)。不要使用裸 `pip` 或裸 `python` 管理、运行本项目。

## Documentation

- [Product Scope](../PRODUCT_SCOPE.md)：产品定位、模块职责、用户流程和统一验收基线；优先阅读。
- [Usage Guide](../USAGE.md)：真实 Codex、CLI、API、SSE 和离线验证。
- [Roadmap](../ROADMAP.md)：P1 至 P5 的产品阶段、范围和退出条件。
- [P1 Implementation Plan](../P1_IMPLEMENTATION_PLAN.md)：P1 增量、验收与完成记录。
- [P2 Implementation Plan](../P2_IMPLEMENTATION_PLAN.md)：当前产品重整顺序，以及保留的历史执行增量。
- [Development Guidelines](../DEVELOPMENT_GUIDELINES.md)：领域语言、模块边界和开发规范。
- [Architecture Decision Records](../adr/)：持久化、接口和 Codex 通道等关键决策。
- [Codex CLI Spike](../spikes/codex-cli-local-channel.md)：真实 Codex 通道与验收观察。

### Architecture Notes

- [Domain](../../src/ehai/domain/README.md)：领域对象、不变量与状态所有权。
- [Application](../../src/ehai/application/README.md)：用例、规划、编排、调度与恢复边界。
- [Infrastructure](../../src/ehai/infrastructure/README.md)：外部服务、持久化、Worker 与 Workspace Adapter。
- [Interfaces](../../src/ehai/interfaces/README.md)：CLI、HTTP、SSE 与本地 Composition Root。
- [Schemas](../../schemas/README.md)：跨 Plane Schema、OpenAPI 与生成验证流程。
- [Control Plane](../../control-plane/README.md)：当前严格 TypeScript API Client 的范围与用法。

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
收集当作验收通过。实际失败时再用仓库外临时单测定位，定位文件不提交。见 [测试策略](../../tests/README.md)。
