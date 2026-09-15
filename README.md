# EHAI Core

EHAI 是供外部顶层 Agent 或用户调用的规划与执行核心。Codex、Claude Code 等顶层 Agent
处于 EHAI 之外，通过 CLI/MCP 接入。EHAI 管目标、计划、批准、调度、成果与 Gate；
完整 Pi 0.85.1 管模型调用、工具循环、会话和压缩，不再维护自研模型 Runtime。

Planner 是可选能力：可以委托 EHAI 规划，也可以导入外部生成的声明式计划。
两条路径都产生未批准草稿，不能跳过验收与执行授权。

## 状态（2026-09-15）

P1 历史原型完成；P2 已有可用开发核心，但总验收和部分接续能力未完成。
真实 Luna 已完成编码、并发、单节点挂起/回复、Reviewer 与行为 Gate；
新增导入/MCP 的验证及限制见 [当前状态](docs/STATUS.md)。不以接口齐备宣称 P2 全部完成。

## 安装与入口

需要 Python 3.12、uv、Node >=22.19.0。使用锁定依赖：

~~~powershell
uv sync --frozen
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund
uv run ehai --help
uv run ehai-api --help
uv run ehai-mcp --help
~~~

- 本地 CLI：讨论/导入/批准，再 execute-plan 前台执行；resume-session 恢复。
- API 客户端：ehai --api-url http://127.0.0.1:8000，复用规划、执行控制和查询命令。
- 独立 MCP：ehai-mcp --api-url http://127.0.0.1:8000，默认只读；明确授权时加 --allow-writes。

HTTP/MCP 客户端不启动后台服务，需先运行 ehai-api。模型凭证只保留在宿主私有环境，
不提交 Git。Pi 是按需子进程，不需要独立的 Pi HTTP 服务。

## 文档

[使用与配置](docs/USAGE.md) · [能力与证据](docs/STATUS.md) · [产品范围](docs/PRODUCT_SCOPE.md) ·
[执行模型](docs/EXECUTION_MODEL.md) · [路线图](docs/ROADMAP.md) ·
[实施计划](docs/P2_IMPLEMENTATION_PLAN.md) · [本轮记录](docs/R2_IMPLEMENTATION_PLAN.md) ·
[开发规则](docs/DEVELOPMENT_GUIDELINES.md) · [ADR](docs/adr/0005-external-agent-backends.md)。

[外部计划示例](examples/plan-import.json) · [导入 Schema](schemas/v1/plan-import.schema.json) ·
[历史快照](docs/history/README.md)。历史记录不是当前操作指南。

Python 核心拥有执行状态；TS Client 从 schemas 生成；MCP 转发现有 HTTP API。
故障诊断只放仓库外，不新增常驻测试矩阵，不把静态检查或部分自举当作产品 E2E。
