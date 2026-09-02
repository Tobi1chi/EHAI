# EHAI Usage Guide

本文档记录当前 P1/P1.1 稳定基线的本地运行方式。该基线一次只运行一个 Worker Attempt；P2 的
异步调度、多 Worker 路由和持久 Agent Session 尚未进入可用命令面。

## 安装与前置条件

需要 Python 3.12 和 `uv`。真实 Agent 执行还要求本机 `codex` 可执行文件已经完成认证。

```powershell
uv sync
uv run ehai --help
uv run ehai-api --help
codex --version
```

所有 Python 命令均通过 `uv run` 执行，不使用裸 `python` 或 `pip`。

## 真实 Codex CLI 闭环

以下 PowerShell 示例使用确定性的双分支 Planner 和真实 Codex Worker，完成
Project → Goal → Plan → Approve → Run。每次运行使用独立 SQLite 数据库和 Artifact 目录。

```powershell
$DemoRoot = Join-Path ".ehai" ("codex-demo-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
New-Item -ItemType Directory -Force $DemoRoot | Out-Null
$Db = Join-Path $DemoRoot "state.sqlite3"
$Artifacts = Join-Path $DemoRoot "artifacts"
$Common = @(
    "--database", $Db,
    "--artifacts", $Artifacts,
    "--worker", "codex",
    "--worker-workspace", (Get-Location).Path,
    "--planner", "exploration",
    "--worker-timeout-seconds", "600"
)

$Project = (uv run ehai @Common create-project `
    --idempotency-key demo-project --name "EHAI Codex demo" | ConvertFrom-Json)
$Goal = (uv run ehai @Common create-goal `
    --idempotency-key demo-goal --project-id $Project.project_id `
    --objective "produce a checked candidate" | ConvertFrom-Json)
$Plan = (uv run ehai @Common propose-plan `
    --idempotency-key demo-plan --goal-id $Goal.goal_id `
    --criterion "artifact:non-empty" | ConvertFrom-Json)
uv run ehai @Common approve-plan `
    --idempotency-key demo-approve `
    --plan-revision-id $Plan.plan_revision_id `
    --completion-contract-id $Plan.completion_contract_id
$Run = (uv run ehai @Common start-run `
    --idempotency-key demo-run `
    --plan-revision-id $Plan.plan_revision_id | ConvertFrom-Json)
uv run ehai @Common get-run --run-id $Run.run_id
```

把 `--planner exploration` 改为 `--planner single` 可创建单节点计划；改为 `--planner codex` 会使用
独立的 Codex Planner 协议生成受预算约束的探索图。`--planner-timeout-seconds` 只控制 Planner，
`--worker-timeout-seconds` 只控制 Worker Attempt。Worker 还可使用 `--codex-model` 和
`--codex-reasoning-effort` 覆盖本次调用配置，不修改用户全局 Codex 配置。

CLI 还提供 `pause-run`、`resume-run`、`cancel-run`、`restore-run` 和启动恢复用的 `recover`；参数以
`uv run ehai <全局参数> <子命令> --help` 为准。

## 完成条件与 Check

P1/P1.1 每个 Plan 只接受一个完成条件：

- `artifact:non-empty`：候选 Artifact 必须存在且非空。
- `command:exit-zero`：宿主配置的命令必须以退出码 0 完成。
- `semantic:required-terms`：候选内容必须满足透明的必需词项 rubric。

Command Check 通过 `--command-check-argv` 接收 JSON 字符串数组，不经过 shell。候选 Artifact 会被
物化到本次 Attempt/Check 的临时目录，不写入项目 worktree。Semantic Check 使用可重复提供的
`--semantic-required-term` 配置词项。相关全局配置必须在实际执行或恢复 Run 的进程中继续提供。

## HTTP API 与 SSE

以下命令使用真实 Codex Worker 和确定性探索 Planner 启动单进程本地 API：

```powershell
New-Item -ItemType Directory -Force .ehai | Out-Null
uv run ehai-api `
    --database .ehai/api.sqlite3 `
    --artifacts .ehai/api-artifacts `
    --worker codex `
    --worker-workspace (Get-Location).Path `
    --planner exploration `
    --worker-timeout-seconds 600 `
    --host 127.0.0.1 `
    --port 8000
```

另开一个 PowerShell 窗口发送主要 Command：

```powershell
$Api = "http://127.0.0.1:8000/api/v1"
$Project = Invoke-RestMethod -Method Post -Uri "$Api/projects" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "api-project"; name = "API demo" } | ConvertTo-Json)
$Goal = Invoke-RestMethod -Method Post -Uri "$Api/goals" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "api-goal"; project_id = $Project.data.project_id; objective = "run an exploration plan" } | ConvertTo-Json)
$Plan = Invoke-RestMethod -Method Post -Uri "$Api/plans/propose" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "api-plan"; goal_id = $Goal.data.goal_id; criteria = @("artifact:non-empty") } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$Api/plans/approve" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "api-approve"; plan_revision_id = $Plan.data.plan_revision_id; completion_contract_id = $Plan.data.completion_contract_id } | ConvertTo-Json)
$Run = Invoke-RestMethod -Method Post -Uri "$Api/runs/start" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "api-run"; plan_revision_id = $Plan.data.plan_revision_id } | ConvertTo-Json)
```

主要查询接口：

```powershell
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)"
Invoke-RestMethod "$Api/plans/$($Plan.data.plan_revision_id)"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/trace"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/checks"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/checkpoints"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/artifacts"
$Events = Invoke-RestMethod "$Api/events?limit=100"
```

SSE 支持按 Event ID 断线续传：

```powershell
curl.exe -N -H "Accept: text/event-stream" "$Api/events/stream"

$LastEventId = $Events.data.events[0].event.id
curl.exe -N -H "Accept: text/event-stream" `
    -H "Last-Event-ID: $LastEventId" "$Api/events/stream"
curl.exe -N -H "Accept: text/event-stream" `
    "$Api/events/stream?after_event_id=$LastEventId"
```

`Last-Event-ID` 与 `after_event_id` 同时提供但不一致时会被拒绝。公开 JSON 契约位于
`schemas/v1/`。

## 真实 Codex Smoke

自动化测试默认不会调用真实 Codex。显式设置开关后才使用本机可执行文件和认证状态：

```powershell
$env:EHAI_RUN_CODEX_SMOKE = "1"
uv run pytest tests/smoke/test_codex_worker_smoke.py -q
Remove-Item Env:EHAI_RUN_CODEX_SMOKE
```

历史验证结果与 App Server 可见性观察见
[Codex CLI 本地通道技术验证](spikes/codex-cli-local-channel.md)。

## 离线确定性验证

开发或 CI 环境不应默认依赖网络、认证和模型额度。需要验证 EHAI 自身闭环时，可将 CLI 示例中的
`--worker codex` 改为 `--worker fake`；Fake Worker 只用于可重复测试和离线诊断，不代表产品运行
路径。

## 当前基线限制

- 一个进程内同一时刻只执行一个 Attempt，探索分支按稳定顺序串行运行。
- 每个应用实例只选择一个 Worker Connector，不支持 Worker 池和动态 Worker 路由。
- `ehai-api` 按单进程运行，不要使用多个 Uvicorn Worker 共享 P1 SQLite 状态。
- 持久 Agent Session、异步 Scheduler、Dispatcher 和多 Worker 能力仍属于 P2 开发范围。
- Artifact 输入受单文件和总输入预算限制，超限会 fail closed。
