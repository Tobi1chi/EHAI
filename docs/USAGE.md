# EHAI Usage Guide

本文档记录 P1/P1.1 兼容命令与 P2 稳定执行面的本地运行方式。普通 `ehai` 命令保持同步兼容；
`ehai-api --p2-runtime` 使用持久 dispatch work 和后台 Runtime，使 `StartRun` 快速返回并通过
Query/SSE 观察进展。

## 安装与前置条件

需要 Python 3.12 和 `uv`。Standalone Built-in Agent 和 Built-in Planner 通过官方 OpenAI Python SDK 调用 Responses
API，只需要 `OPENAI_API_KEY`；自定义兼容端点可另外设置 `OPENAI_BASE_URL`，不需要安装 Codex。
Codex CLI 和 Codex App Server Worker 才要求本机 `codex` 可执行文件已经完成认证。

```powershell
uv sync
uv run ehai --help
uv run ehai-api --help
codex --version
```

所有 Python 命令均通过 `uv run` 执行，不使用裸 `python` 或 `pip`。

## Standalone Built-in Agent

Built-in Agent 是 EHAI 自带的 Session、Agent Loop、固定 Tool Runtime 和 Responses ModelClient，
不是 Codex 的包装层。以下命令启动一个 capacity 为 2 的本地后台 Runtime；每个并发 Attempt 使用
独立 Session，Git 写任务使用 EHAI-owned worktree，非 Git 写任务自动串行：

```powershell
$env:OPENAI_API_KEY = Read-Host -MaskInput "OpenAI API key"
# 使用兼容端点时再设置：$env:OPENAI_BASE_URL = "https://api.example.com"
uv run ehai-api `
    --database .ehai/builtin.sqlite3 `
    --artifacts .ehai/builtin-artifacts `
    --worker builtin `
    --worker-workspace (Get-Location).Path `
    --planner builtin `
    --builtin-planner-model gpt-5.6-luna `
    --builtin-model gpt-5.6-luna `
    --builtin-reasoning-effort high `
    --builtin-capacity 2 `
    --builtin-allowed-command '["uv","--version"]' `
    --p2-runtime `
    --host 127.0.0.1 `
    --port 8000
```

Built-in Planner 使用同一 Responses ModelClient seam，但只生成 provider-neutral PlanTemplate；它不创建
Worker Attempt、Workspace 或 Agent Session。Built-in Agent 只能通过严格 `submit_candidate` Tool 产生
候选 Artifact；普通 assistant 文本不会被当作执行结果。`Fake` Worker 仅用于离线测试，Codex CLI 是每
Attempt 一个外部进程，Codex App Server Connector 则管理持久 Thread/Turn；三者不共享 Agent 框架。

Built-in Worker 默认完全不注册 `command` Tool。每个 `--builtin-allowed-command` 接受一个完整 JSON
argv 数组并只允许该精确调用，例如上面的 `uv --version`；只配置 executable basename 不会授权其他
参数。Runtime 创建时从绝对 PATH/PATHEXT 目录解析并固定可信 executable，使用不含 API 凭据的最小
子进程环境，并在读取期间限制、脱敏 stdout/stderr。取消、超时或输出超限会清理该命令的进程树。

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
独立的 Codex Planner 协议生成受预算约束的探索图；改为 `--planner builtin` 并提供
`--builtin-planner-model` 会使用 Responses Built-in Planner。`--planner-timeout-seconds` 只控制 Codex Planner，
`--worker-timeout-seconds` 只控制 Worker Attempt。Worker 还可使用 `--codex-model` 和
`--codex-reasoning-effort` 覆盖本次调用配置，不修改用户全局 Codex 配置。

CLI 还提供 `pause-run`、`resume-run`、`cancel-run`、`restore-run` 和启动恢复用的 `recover`；参数以
`uv run ehai <全局参数> <子命令> --help` 为准。

## 完成条件与 Check

P1/P1.1 每个 Plan 只接受一个完成条件；`single`、`exploration`、`codex` 和 `builtin` Planner 都通过同一个
PlanProposal builder 创建 CheckSpec、CompletionContract 和 PlanRevision：

- `artifact:non-empty`：候选 Artifact 必须存在且非空。
- `command:exit-zero`：宿主配置的命令必须以退出码 0 完成。
- `semantic:required-terms`：候选内容必须满足透明的必需词项 rubric。

Command Check 通过 `--command-check-argv` 接收 JSON 字符串数组，不经过 shell。候选 Artifact 会被
物化到本次 Attempt/Check 的临时目录，不写入项目 worktree。Semantic Check 使用可重复提供的
`--semantic-required-term` 配置词项。两者会绑定到不可变 CheckSpec 并随 PlanRevision 持久化；恢复
Run 时 Checker 使用该快照，而不依赖新进程重新提供相同的全局配置。

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
    --p2-runtime `
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
Invoke-RestMethod "$Api/workers/profiles"
Invoke-RestMethod "$Api/workers/endpoints"
Invoke-RestMethod "$Api/runtime/health"
$Events = Invoke-RestMethod "$Api/events?limit=100"
```

`/runtime/health` 单独报告后台调度循环的 `starting/healthy/degraded/failed/stopped` 状态；HTTP API
仍可响应不代表 Runtime 仍在调度。Runtime 对瞬时循环异常执行至多两次有界重启，连续失败后保留可观测
的 `failed` 状态。Built-in background Run 的 pause/cancel 会先停止新调度并收敛所有活跃 Attempt，resume
再恢复该 Run 的调度；pause/cancel 完成后不会遗留 pending/running Attempt。

从 ExecutionTrace 取得 `attempt_id` 后，可以读取分配、Session/Execution、活动、heartbeat、progress、
deadline、lease 和有界诊断：

```powershell
$Trace = Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/trace"
$AttemptId = $Trace.data.attempts[0].attempt_id
Invoke-RestMethod "$Api/attempts/$AttemptId/runtime"
Invoke-RestMethod "$Api/attempts/$AttemptId/worker-requests"
```

运行中的 Attempt 可以显式延长 deadline 或取消；waiting request 必须由用户显式 resolve/decline，
Connector 不会自动同意：

```powershell
Invoke-RestMethod -Method Post -Uri "$Api/attempts/$AttemptId/deadline" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "deadline-1"; deadline_at = "2026-09-02T14:00:00.000000Z" } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$Api/attempts/$AttemptId/cancel" `
    -ContentType "application/json" `
    -Body (@{ idempotency_key = "cancel-attempt-1" } | ConvertTo-Json)
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

## 真实 OpenAI Responses Smoke

Built-in Agent 只从 `OPENAI_API_KEY` 读取凭证；数据库保存的是引用
`env:OPENAI_API_KEY`，不会保存 key。显式 Smoke 固定使用 `gpt-5.6-luna` 和 `high`，并验证
Runtime、Responses、Workspace Tool、Artifact、Gate 与 Checkpoint 的完整链路：

```powershell
$env:EHAI_RUN_OPENAI_SMOKE = "1"
uv run pytest tests/smoke/test_openai_responses_smoke.py -q
Remove-Item Env:EHAI_RUN_OPENAI_SMOKE
```

请求优先使用持久化的 `previous_response_id`。兼容端点若明确拒绝该字段，Adapter 会省略该句柄并从
durable Session Event 重放 function call/result；`store=true`、streaming 和严格 Tool Schema 保持
不变。

不要把 `OPENAI_API_KEY` 写入命令历史、配置文件、Event、Artifact 或数据库。

## 真实 Codex App Server 双 Session Smoke

App Server Connector 只使用一个 Endpoint 对应一个 `codex app-server --listen stdio://` JSONL 连接。
显式 Smoke 会启动两个独立 Thread/Turn，并精确中断其中一个：

```powershell
$env:EHAI_RUN_CODEX_APP_SERVER_SMOKE = "1"
uv run pytest tests/smoke/test_codex_app_server_smoke.py -q
Remove-Item Env:EHAI_RUN_CODEX_APP_SERVER_SMOKE
```

该 Connector 不使用 WebSocket、远程 listener、Review、Skills、Apps 或 Auth 登录接口，也不修改用户
全局 Codex 配置。

## TypeScript API Client

`control-plane/` 仅包含由 `schemas/v1/` 生成的严格类型和 API Client，不包含 P3 UI：

```powershell
Set-Location control-plane
npm.cmd ci
npm.cmd run generate
npm.cmd run typecheck
npm.cmd run build
```

## 离线确定性验证

开发或 CI 环境不应默认依赖网络、认证和模型额度。需要验证 EHAI 自身闭环时，可将 CLI 示例中的
`--worker codex` 改为 `--worker fake`；Fake Worker 只用于可重复测试和离线诊断，不代表产品运行
路径。

## 当前基线限制

- P2 只支持单 Execution Plane 进程；SQLite lease 用于崩溃恢复，不宣称分布式一致性。
- 本地 Built-in `ehai-api --p2-runtime` 使用单 Endpoint `ConcurrentRuntime`，并发上限由
  `--builtin-capacity` 控制；本地 Fake/Codex CLI 组合仍为单槽位。多 Endpoint 路由由应用层组合，
  不是 P3 Dashboard。
- 不支持 OpenCode、Claude Code、DSH Connector、动态插件、P4 Workflow 或 P3 UI。
- Provider 不报告 cost 时公开为 unavailable，不进行虚假估算。
- Artifact、日志、Worker context 和诊断均有边界，超限会 fail closed。
