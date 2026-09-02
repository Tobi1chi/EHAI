# EHAI

EHAI P1 是一个可运行的 Python Execution Plane。它把版本化 `PlanGraph` 与实际
`ExecutionTrace` 分开保存，由 Planner 提出计划、Orchestrator 顺序执行、Worker 只提交候选，
最终由 Check/Gate 决定节点与 Run 是否完成。

当前状态：P1 与 P1.1 已完成，保留为单 Worker、串行执行基线；当前开发阶段为 P2，重点是
多 Worker、异步调度、平台路由、Agent Session 状态与恢复。

## 安装

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。项目依赖及开发工具均由 uv 管理：

```powershell
uv sync
uv run ehai --help
uv run ehai-api --help
```

不要使用裸 `pip` 或裸 `python` 管理、运行本项目。

## CLI：确定性 Fake Worker 演示

下面的 PowerShell 脚本创建独立数据库，完成 Project → Goal → Plan → Approve → Run 闭环。
`FakeWorker` 不访问网络，结果可重复。把 `$Planner` 设为 `single` 可运行单节点计划；设为
`exploration` 可运行固定的双分支 `fork → explore → evaluate → select/prune → merge` 计划。
exploration 模式下 evaluator 节点会提交结构化 BranchSelection artifact；Orchestrator 读取该
artifact 后执行 selected/pruned 状态转换，Merge 只接收被选中分支的 Artifact 内容快照。

```powershell
$Planner = "exploration" # 可改为 "single"
$DemoRoot = Join-Path ".ehai" ("demo-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
New-Item -ItemType Directory -Force $DemoRoot | Out-Null
$Db = Join-Path $DemoRoot "state.sqlite3"
$Artifacts = Join-Path $DemoRoot "artifacts"

$Project = (uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner create-project --idempotency-key demo-project --name "EHAI demo" | ConvertFrom-Json)
$Goal = (uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner create-goal --idempotency-key demo-goal --project-id $Project.project_id --objective "produce a checked candidate" | ConvertFrom-Json)
$Plan = (uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner propose-plan --idempotency-key demo-plan --goal-id $Goal.goal_id --criterion "artifact:non-empty" | ConvertFrom-Json)
uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner approve-plan --idempotency-key demo-approve --plan-revision-id $Plan.plan_revision_id --completion-contract-id $Plan.completion_contract_id
$Run = (uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner start-run --idempotency-key demo-run --plan-revision-id $Plan.plan_revision_id | ConvertFrom-Json)
uv run ehai --database $Db --artifacts $Artifacts --worker fake --planner $Planner get-run --run-id $Run.run_id
```

Planner 选择是显式的全局参数，必须放在子命令前。可选值为 `single`、`exploration` 和
`codex`；前两者是确定性实现，`codex` 使用独立 Planner 协议调用本地 `codex exec`：

```powershell
uv run ehai --database .ehai/single.sqlite3 --artifacts .ehai/single-artifacts --planner single propose-plan --idempotency-key plan-1 --goal-id <GOAL_UUID> --criterion "artifact:non-empty"
uv run ehai --database .ehai/exploration.sqlite3 --artifacts .ehai/exploration-artifacts --planner exploration propose-plan --idempotency-key plan-1 --goal-id <GOAL_UUID> --criterion "artifact:non-empty"
uv run ehai --database .ehai/codex-planner.sqlite3 --artifacts .ehai/codex-planner-artifacts --planner codex --planner-timeout-seconds 120 propose-plan --idempotency-key plan-1 --goal-id <GOAL_UUID> --criterion "artifact:non-empty"
```

P1 支持三个单项完成 criterion：

- `artifact:non-empty`：使用内置 Artifact Check，确认候选 Artifact 存在且非空。
- `command:exit-zero`：使用宿主通过 `--command-check-argv` 配置的 argv，在 EHAI Check 内执行。
- `semantic:required-terms`：使用宿主通过 `--semantic-required-term` 配置的透明词项 rubric。

Command Check 的 argv 是 JSON 字符串数组，不经过 shell；候选 Artifact 会被物化到本次
Attempt/Check 专属临时目录，不写入项目 worktree。例如：

```powershell
uv run ehai --database .ehai/command.sqlite3 --artifacts .ehai/command-artifacts --command-check-argv '["uv","run","pytest","-q"]' --planner single propose-plan --idempotency-key plan-1 --goal-id <GOAL_UUID> --criterion "command:exit-zero"
```

`--planner-timeout-seconds` 只控制 Planner 调用期限，与 Worker Attempt 的期限相互独立。
Codex Worker 还可显式配置 `--worker-timeout-seconds`、`--codex-model` 和
`--codex-reasoning-effort`；这些参数只作为本次 `codex exec` argv/config override 传递，不修改
用户全局 Codex config。自动化测试使用受控假进程验证 Codex Planner 和 Worker；当前真实 Codex
smoke 仅覆盖 Worker connector。

每个 Goal 只能对齐一个当前 CompletionContract；比较两种 Planner 时请使用不同 Goal 或独立演示数据库。
CLI 还提供 `pause-run`、`resume-run`、`cancel-run`、`restore-run` 和启动恢复用的 `recover`；
参数以 `uv run ehai <全局参数> <子命令> --help` 为准。

## HTTP API 与 SSE

本地 API 默认使用单进程 Uvicorn、Fake Worker 和 single Planner。以下命令显式启动
exploration 演示服务：

```powershell
New-Item -ItemType Directory -Force .ehai | Out-Null
uv run ehai-api --database .ehai/api.sqlite3 --artifacts .ehai/api-artifacts --worker fake --planner exploration --host 127.0.0.1 --port 8000
```

另开一个 PowerShell 窗口可调用主要 Command：

```powershell
$Api = "http://127.0.0.1:8000/api/v1"
$Project = Invoke-RestMethod -Method Post -Uri "$Api/projects" -ContentType "application/json" -Body (@{ idempotency_key = "api-project"; name = "API demo" } | ConvertTo-Json)
$Goal = Invoke-RestMethod -Method Post -Uri "$Api/goals" -ContentType "application/json" -Body (@{ idempotency_key = "api-goal"; project_id = $Project.data.project_id; objective = "run an exploration plan" } | ConvertTo-Json)
$Plan = Invoke-RestMethod -Method Post -Uri "$Api/plans/propose" -ContentType "application/json" -Body (@{ idempotency_key = "api-plan"; goal_id = $Goal.data.goal_id; criteria = @("artifact:non-empty") } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$Api/plans/approve" -ContentType "application/json" -Body (@{ idempotency_key = "api-approve"; plan_revision_id = $Plan.data.plan_revision_id; completion_contract_id = $Plan.data.completion_contract_id } | ConvertTo-Json)
$Run = Invoke-RestMethod -Method Post -Uri "$Api/runs/start" -ContentType "application/json" -Body (@{ idempotency_key = "api-run"; plan_revision_id = $Plan.data.plan_revision_id } | ConvertTo-Json)
```

主要只读接口如下：

```powershell
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)"
Invoke-RestMethod "$Api/plans/$($Plan.data.plan_revision_id)"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/trace"
Invoke-RestMethod "$Api/plans/$($Plan.data.plan_revision_id)/checks"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/checks"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/checkpoints"
Invoke-RestMethod "$Api/runs/$($Run.data.run_id)/artifacts"
$Events = Invoke-RestMethod "$Api/events?limit=100"
```

`POST /runs/{run_id}/pause|resume|cancel` 使用 `{ "idempotency_key": "..." }` 请求体；cancel
还可带非空 `reason`。Fake Worker 的 `start` 同步完成得很快，因此暂停/取消更适合用于运行中的 Codex
Worker 或专门的控制测试。

SSE 按 Event ID 断线续传。以下命令持续输出 Event frame，使用 `Ctrl+C` 停止：

```powershell
curl.exe -N -H "Accept: text/event-stream" "$Api/events/stream"

$LastEventId = $Events.data.events[0].event.id
curl.exe -N -H "Accept: text/event-stream" -H "Last-Event-ID: $LastEventId" "$Api/events/stream"
curl.exe -N -H "Accept: text/event-stream" "$Api/events/stream?after_event_id=$LastEventId"
```

`Last-Event-ID` 与 `after_event_id` 同时提供但不一致时会被拒绝；未知游标不会静默跳到最新位置。
公开 JSON 契约位于 `schemas/v1/`，FastAPI OpenAPI 用于接口发现和辅助测试。

## 真实 Codex smoke test

自动化测试默认不会调用真实 Codex。只有显式设置开关后，下面的测试才使用本机已有的 `codex`
可执行文件和认证状态：

```powershell
$env:EHAI_RUN_CODEX_SMOKE = "1"
uv run pytest tests/smoke/test_codex_worker_smoke.py -q
Remove-Item Env:EHAI_RUN_CODEX_SMOKE
```

历史传输通道 Spike 曾在本机认证请求中收到 HTTP 401；在 2026-08-31 的 I8 验收中，上述真实
smoke test 已通过（`1 passed`）。EHAI 和测试都不会自动登录、替换 API key 或修改用户凭证；未来
若再次出现 401，应先由用户自行确认本机 Codex 认证，而不是让测试修改认证状态。

同次验收还用 `--worker codex --planner exploration` 跑通了完整真实场景：5 个串行 Attempts 全部
`succeeded`，两个分支进入 `selected/pruned`，5 个节点和最终 Run 均为 `completed`，并创建 5 个
Checkpoints。完整真实场景不会加入默认测试套件，避免隐式消耗网络、时间或凭证配额。

## P1 边界与安全说明

- P1 只有 Python Execution Plane；没有 TypeScript Control Plane，也不提前实现 P2+ UI、账号、
  多租户、WebSocket、消息代理或分布式调度。
- 一个进程内同一时刻只执行一个 Attempt，探索分支按稳定顺序串行运行。
- 每个应用实例只选择一个 Worker connector（`fake` 或 `codex`）；不支持 Worker 池和并发 Worker。
- `ehai-api` 按单进程使用。进程内有串行执行保护，但多个服务进程共享 SQLite 时不保证全局串行；
  不要用多 Uvicorn worker 运行 P1。
- Artifact 内容保存在不可变文件存储中；HTTP 仅公开安全元数据，不公开内部 `relative_path`。
- Worker 依赖输入使用有上限的 Artifact 内容快照传递：UTF-8 直接传文本，非 UTF-8 使用 base64；
  超过单 Artifact 或总输入预算会 fail closed，不会静默丢弃内容。
- 真实 Codex 必须使用受控工作目录和 sandbox；不要使用跳过批准或 sandbox 的危险参数，也不要把
  secrets 写入日志、Artifact 或测试夹具。
- Windows descendant cleanup 覆盖包含一个受 Windows Job Object 隔离的真实后代进程探针，以及
  mock 的进程发现和 `taskkill` 调用契约测试；它不会清理测试 Job 之外的用户进程树。

常规验证命令：

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
