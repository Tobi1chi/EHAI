# EHAI

EHAI（Enhanced Human-Agent Interface）是一个面向人—Agent 协作的执行环境。它把用户目标、
完成条件、探索计划和实际执行轨迹组织成可检查、可恢复的图运行过程，让 Agent 能够探索多种方案，
但不能绕过预先确认的证据标准自行宣布任务完成。

> 当前状态：P1/P1.1 已完成；P2 多 Worker 执行内核正在开发。

## Why EHAI

现有 Agent 通常擅长执行单次指令，但复杂任务还需要明确的计划版本、探索边界、完成标准和恢复路径。
EHAI 将这些约束放入独立的 Execution Plane，使人类能够在执行前对齐目标，在执行中保留分支证据，
并在最终 Gate 通过后确认任务完成。

## Core Capabilities

- 使用版本化 `Goal`、`CompletionContract` 和 `PlanRevision` 对齐目标与完成标准。
- 使用非线性 `PlanGraph` 表达探索、分支、评估、剪枝和汇合。
- 通过 Codex Planner 与 External Worker Connector 生成计划和候选结果。
- 使用不可变 Artifact、Check 和 Gate 保存证据并决定状态转换。
- 使用 Event、ExecutionTrace 和 Checkpoint 支持审计、中断恢复与轨迹查询。
- 通过 CLI、HTTP API、OpenAPI/JSON Schema 和 SSE 暴露 P1 执行能力。

## How It Works

```text
Goal + CompletionContract
          ↓
       Planner → PlanGraph
                     ↓
               Orchestrator
                     ↓
              Worker Attempt
                     ↓
       Artifact → Check/Gate → Checkpoint
                     ↓
              ExecutionTrace
```

`PlanGraph` 描述预期执行路径；`ExecutionTrace` 保存实际发生的 Attempt、事件、检查和结果。Worker
只能提交候选结果，PlanNode 和 Run 只能在必需 Gate 通过后完成。

## Development Status

| 阶段 | 状态 | 范围 |
| --- | --- | --- |
| P1/P1.1 | 已完成 | 单 Worker 串行闭环、Codex CLI、分支评估、Check/Gate、Checkpoint、API/SSE |
| P2 | 进行中 | 异步调度、Built-in/Codex Worker、Agent Session、并发与资源管理 |
| P3+ | 已规划 | TypeScript Control Plane、可复用 Workflow 和开放扩展生态 |

P2 表中的能力仍处于开发阶段；当前可运行命令保持 P1/P1.1 的串行语义。

## Quick Start

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)；运行真实 Agent 还需要本机已安装并登录
Codex CLI。

```powershell
uv sync
uv run ehai --help
uv run ehai-api --help
```

完整的真实 Codex 执行流程、CLI 参数、HTTP API 和 SSE 示例见
[Usage Guide](docs/USAGE.md)。不要使用裸 `pip` 或裸 `python` 管理、运行本项目。

## Documentation

- [Usage Guide](docs/USAGE.md)：真实 Codex、CLI、API、SSE 和离线验证。
- [Roadmap](docs/ROADMAP.md)：P1 至 P5 的产品阶段、范围和退出条件。
- [P1 Implementation Plan](docs/P1_IMPLEMENTATION_PLAN.md)：P1 增量、验收与完成记录。
- [P2 Implementation Plan](docs/P2_IMPLEMENTATION_PLAN.md)：P2 增量、异步调度与多 Worker 验收计划。
- [Development Guidelines](docs/DEVELOPMENT_GUIDELINES.md)：领域语言、模块边界和开发规范。
- [Architecture Decision Records](docs/adr/)：持久化、接口和 Codex 通道等关键决策。
- [Codex CLI Spike](docs/spikes/codex-cli-local-channel.md)：真实 Codex 通道与验收观察。

## Development

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

开发期间先运行最小相关测试；准备提交时再运行受影响技术栈的完整测试。
