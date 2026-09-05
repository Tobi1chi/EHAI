# EHAI Usage Guide

本文档记录 P1/P1.1 兼容命令与 P2 稳定执行面的本地运行方式。普通 `ehai` 命令保持同步兼容；
`ehai-api --p2-runtime` 使用持久 dispatch work 和后台 Runtime，使 `StartRun` 快速返回并通过
Query/SSE 观察进展。

## 实现与目标的边界

本页是当前入口参考，不是目标产品说明。目标流程与职责见 [Product Scope](PRODUCT_SCOPE.md)，
实施状态见 [P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md)。
现有命令已接入规划讨论、草稿修订、批准及查询；仍不能据此宣称需求相关最终验收以及
“执行阻塞 → 用户回复 → 继续”的完整产品流程已完成。真实模型交互与最终 E2E 另行验证。
本页不为尚未实现的交互编造命令；下列演示中固定计划或产物的成功只证明其对应协议。
旧测试脚本已退役，先通过正常入口暴露能力，再与用户确定唯一产品 E2E；失败定位文件仅放仓库外。

Foundation 扩展分支中的工具 Provider、共享 Role、Mailbox 和 Visualizer 需要与正常入口逐项核对。
底层构造器可注入，不代表所有工具已有 CLI 选项。Responses capability 已通过下文正式参数接入，
不能靠测试替换 Adapter 作为使用方法。

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
`ehai` 的 stdout/stderr JSON 固定使用 UTF-8，包括 Windows 管道和文件重定向；不依赖系统 GBK locale。

## 查看方案、验收条件与执行轨迹

CLI 查询直接复用 HTTP API 的 QueryService 与公开 JSON 编码，不构造 Planner/Worker，不要求模型凭证：

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-plan --plan-revision-id $PlanId
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-plan-checks --plan-revision-id $PlanId
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-trace --run-id $RunId
```

数据库必须已经存在。`get-plan` 返回方案版本、批准状态、节点指令、依赖和分支；`get-plan-checks`
返回该版本持久化的检查配置；`get-trace` 返回运行、Attempt、产物、事件及有界 Built-in Session 轨迹。
`design_document` 保存该版本的可读方案，旧版本可能为 null。多项检查仍使用现有检查器，
不是任意需求到执行检查的自动转换；执行阻塞的人工回路仍待实现。

## 与 Planner 讨论并修订方案

讨论使用 Built-in Planner，先配置有权限的模型和正常凭证；只读调查范围由 `--worker-workspace` 指定。
下面沿用已创建的 `$GoalId`。每条新消息使用新的幂等键，首次不传 conversation ID：
示例的非空产物条件只用于演示规划操作，不是编码任务的充分验收条件。

```powershell
$Planning = @(
    '--database', '.ehai/state.sqlite3',
    '--artifacts', '.ehai/artifacts',
    '--worker-workspace', (Get-Location).Path,
    '--planner', 'builtin',
    '--builtin-planner-model', 'gpt-5.6-luna',
    '--builtin-planner-reasoning-effort', 'high'
)
$Discussion = uv run ehai @Planning discuss-plan --idempotency-key planning-1 `
    --goal-id $GoalId --criterion artifact:non-empty `
    --message '先调查现有代码，指出需要澄清的问题，不要开始编码。' | ConvertFrom-Json
$ConversationId = $Discussion.conversation_id
uv run ehai @Planning get-discussion --conversation-id $ConversationId
uv run ehai @Planning discuss-plan --idempotency-key planning-2 `
    --goal-id $GoalId --conversation-id $ConversationId --criterion artifact:non-empty `
    --message-file .\review-feedback.txt
```

`--message-file` 读取 UTF-8 用户意见或外部 Agent 审查意见，与 `--message` 二选一；消息最多 8000 字符。
Planner 可通过 `ask_user` 提问或回应审查，此时该轮 `plan_revision_id` 为 null，不创建草稿或执行任务。
准备方案时调用 `set_plan_design` 记录需求、范围、非目标、假设、实现设计、验收与权限，再通过图工具
构建执行图并 `finish_plan`。返回的 turn 含新 `plan_revision_id`，可用 `get-plan` / `get-plan-checks` 查看。

继续讨论时会携带历史消息、当前计划及设计。逻辑讨论 ID 持久保存在现有 Event Log；每轮模型执行使用
共享 Built-in Runtime 和 Session Store，响应记录其 `agent_session_ref_id`，不另建 Agent Loop。
同一讨论固定 Goal 和 workspace，最多 24 轮且历史上下文有 64 KB 边界；达到边界时明确要求开新讨论，
新讨论仍可参考该 Goal 的当前方案，不静默丢弃旧记录。

新草稿形成新的计划版本和 CompletionContract 版本，旧草稿/批准记录保留；仍需用已有 `approve-plan`
明确批准返回版本，讨论不会自动批准或启动 Run。Goal 有 pending/running/paused Run 时暂不允许讨论修订，
执行中的人工介入留给后续 R3。重复成功消息键只读取已有结果，不重复调用模型。

缺少配置或 Provider 失败会留下可查询的 failed turn；重复该消息键不会重新调用模型，应查看错误后用
新键继续。若进程中断留下 running/未知结果，同一讨论暂不继续；需先调查原执行，必要时显式开新讨论，
不宣称此入口已经实现自动模型执行恢复。

HTTP 使用相同能力：

- `POST /api/v1/planning/discuss`：`idempotency_key`、`goal_id`、`message`、`criteria`，可选 `conversation_id`。
- `GET /api/v1/planning/{conversation_id}`：查询讨论；TypeScript Client 提供对应生成方法。

### 多项完成条件

`propose-plan`、`replan-plan`、`discuss-plan` 的 `--criterion` 可以重复，最多组合当前三种不同检查。
`command:exit-zero` 必须同时配置宿主允许的 `--command-check-argv`，`semantic:required-terms` 必须
配置 `--semantic-required-term`。Planner 能查看这些配置，但不能擅自创建命令权限或减少已传入的条件。
条件变化随新契约版本再次批准；按节点区分验收和最终交付的更丰富检查安排仍待 R2 实现。

### Responses Endpoint capability

`ehai` 与 `ehai-api` 共用以下参数，适用于 Built-in Planner；后台 Built-in Worker 也接收同一配置：

- `--no-responses-background`：仅在端点明确不支持 background 时关闭。
- `--no-responses-unique-items`：仅在端点不接受该 Tool Schema 关键字时关闭。
- `--responses-idempotent-create` / `--no-responses-idempotent-create`：显式声明端点是否保证 create 幂等。
  未指定时保持未知，不应为了绕过错误擅自声称端点有幂等保证。

这些参数不代替凭证，也不关闭协议检查或任意重放未知副作用。

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
    --builtin-agent-max-steps 64 `
    --builtin-agent-max-tool-calls 128 `
    --builtin-allowed-command '["uv","--version"]' `
    --p2-runtime `
    --host 127.0.0.1 `
    --port 8000
```

Built-in Planner 使用同一 Responses ModelClient seam，但只生成 provider-neutral PlanTemplate；它不创建
Worker Attempt、Workspace 或 Agent Session。模型通过 `add_plan_node`、`update_plan_node`、
`remove_plan_node`、`add_plan_edge`、`remove_plan_edge`、`set_plan_branch`、`inspect_plan` 和
`finish_plan` 图操作 Tool 直接在本次 Planner 调用的内存图中构造 PlanGraph，不存在第二套 Plan IR、
Operation 或 Patch 协议；只有 `finish_plan` 完整校验通过后，应用层 builder 才分配 EHAI ID、
CheckSpec 和 CompletionContract。图修改操作预算为 128 次，完整校验失败后的修复预算为 4 次。
Built-in Agent 只能通过严格 `submit_candidate` Tool 产生
候选 Artifact；普通 assistant 文本不会被当作执行结果。`Fake` Worker 仅用于离线测试，Codex CLI 是每
Attempt 一个外部进程，Codex App Server Connector 则管理持久 Thread/Turn；三者不共享 Agent 框架。

Built-in Worker 默认完全不注册 `command` Tool。每个 `--builtin-allowed-command` 接受一个完整 JSON
argv 数组并只允许该精确调用，例如上面的 `uv --version`；只配置 executable basename 不会授权其他
参数。Runtime 创建时从绝对 PATH/PATHEXT 目录解析并固定可信 executable，使用不含 API 凭据的最小
子进程环境，并在读取期间限制、脱敏 stdout/stderr。允许列表会出现在 Tool 描述中，但安全边界始终是
Runtime 对完整 argv 的精确匹配，不依赖模型供应商是否支持数组值 JSON Schema enum。取消、超时或
输出超限会清理该命令的进程树。

Workspace read/patch 在分配完整内容前执行大小预检并以有界块读取；list/search 对目录项、候选文件、
累计扫描字节、匹配数、匹配行与返回体分别限流。Search 结果通过 `truncated` 和
`truncation_reason` 显式说明未遍历完整的原因，并在遍历与读取期间响应取消。
Built-in Agent 的 Step、ToolCall、内部 wall-clock 和累计输出预算可分别通过
`--builtin-agent-max-steps`、`--builtin-agent-max-tool-calls`、
`--builtin-agent-wall-clock-seconds` 和 `--builtin-agent-max-output-bytes` 调整；所有值始终为有限正数。
外层 Attempt 不再默认设置短 wall-clock 截止时间：`--worker-timeout-seconds` 只控制 Codex CLI 子进程
超时，绝对 Attempt 截止时间改为显式可选项 `--attempt-deadline-seconds`（默认不设置）。缺省情况下，
长时间运行的 high/xhigh 模型调用由 heartbeat lease、no-progress 观察、Built-in Agent 自身预算和
显式 cancel 约束，而不是被外层秒数提前终止。Responses 客户端默认 stream idle timeout 为 300 秒：
HTTP 请求失败最多重试 4 次，流中断最多重连 5 次，已获得 response_id 时通过 retrieve 恢复同一个
Response 而不是重复创建；queued/in_progress 状态会继续等待，只有 completed/failed/cancelled/
incomplete 等明确终态才结束本次 Provider 执行。

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
`--builtin-planner-model` 会使用 Responses Built-in Planner（模型通过图操作 Tool 自行选择线性或探索
结构）。`--planner-timeout-seconds` 只控制 Codex Planner 子进程，
`--worker-timeout-seconds` 只控制 Codex CLI Worker 子进程，两者都不是 Built-in Planner/Agent 的
Provider 调用寿命。Worker 还可使用 `--codex-model` 和
`--codex-reasoning-effort` 覆盖本次调用配置，不修改用户全局 Codex 配置。

CLI 还提供 `pause-run`、`resume-run`、`cancel-run`、`restore-run` 和启动恢复用的 `recover`；参数以
`uv run ehai <全局参数> <子命令> --help` 为准。
失败后需要改计划时，使用 `replan-plan --base-plan-revision-id <plan-id> --source-run-id <run-id>`
显式绑定 failed/cancelled Run 的脱敏诊断；省略 `--source-run-id` 保留无执行上下文的兼容行为。新
PlanRevision 始终是 draft，必须再次执行 `approve-plan`。

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

Built-in Run 的 ExecutionTrace 还包含经过凭证模式脱敏的 `step/start`、`model/message`、`tool/call`、
`tool/result`、`tool/error` 和 `step/end` Session 事件。每个 payload 与总事件数都有硬上限，
`payload_truncated` / `session_events_truncated` 会显式标记截断。

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

## 历史真实验收与当前方式

旧 Codex、Responses、Planner 和 App Server Smoke 脚本已退役，不再提供对应 pytest 调用命令。
当前先使用正常 CLI/API，最终只保留与用户确认的一条产品 E2E。正常使用失败时，定位测试写在仓库外
临时目录；不能通过测试专用 Adapter 或直接调用 Tool Handler 来代替产品入口。

历史验证结果与 App Server 可见性观察见
[Codex CLI 本地通道技术验证](spikes/codex-cli-local-channel.md)。

### Responses 配置与恢复

Built-in Agent 只从 `OPENAI_API_KEY` 读取凭证；数据库保存的是引用
`env:OPENAI_API_KEY`，不会保存 key。模型、推理强度与调用权限由正常入口配置，不通过旧 Smoke 环境
变量代替产品设置，也不在查询方案时隐式调用模型。

请求优先使用持久化的 `previous_response_id`。兼容端点若明确拒绝该字段，Adapter 会省略该句柄并从
durable Session Event 重放 function call/result；`store=true`、streaming 和严格 Tool Schema 保持
不变。

不要把 `OPENAI_API_KEY` 写入命令历史、配置文件、Event、Artifact 或数据库。

### Built-in Planner 的当前边界

当前 propose 操作生成并持久化 draft PlanRevision，不批准计划、创建 Run 或启动 Worker。
生成后可通过 `get-plan` 和 `get-plan-checks` 查看；`discuss-plan` 已接入多轮讨论、详细方案与草稿修订，
代表性真实 CLI 试用已完成需求澄清、设计生成、审查修订和审批隔离，见 P2 实施文档；
这不等于 Worker 编码或最终 E2E 已通过。

## R2 前台编码会话

代表性 Built-in 真实 CLI 编码已通过：并行任务、双代码分支、选中成果整合和一个最终行为 Gate。
主动关闭后的恢复、长时间运行和 Codex App Server 实际编码仍需验证，不把单次试用当成完整 P2 验收。
R2 使用 Git 仓库和 EHAI 拥有的隔离 worktree，不直接改写用户当前分支。运行基线固定为仓库的 Git HEAD；
开始前先提交希望纳入任务的源码，未提交的源文件修改不会自动纳入基线。

Planner 可通过 `set_final_gate` 提出 `command:exit-zero` 的具体 argv，随方案和契约批准。
Builtin 新方案只有一个最终节点绑定行为检查；中间节点交接不运行虚构 Gate。
`get-plan-checks` 必须在批准前审查，尤其是 Planner 提议的命令。显式宿主 argv 与提议冲突时拒绝，
不在批准后改写检查。

将以下无凭证配置保存到仓库外或被 Git 忽略的 `execution.json`：

```json
{
  "config_version": 1,
  "worker_kind": "builtin",
  "model": "<your-worker-model>",
  "reasoning_effort": "low",
  "capacity": 3,
  "workspace": "D:/workspace/target-repository",
  "allowed_commands": [],
  "available_shells": [],
  "git_permissions": ["git.read"],
  "endpoint_capabilities": {
    "supports_background": true,
    "supports_unique_items": true,
    "supports_idempotent_create": null
  },
  "command_timeout_seconds": 30
}
```

模型名按端点实际支持填写；可以与 Planner 的模型不同。兼容端点不支持 background 或 uniqueItems 时，
将相应配置改为 `false`。凭证仍由进程环境提供，不放入 JSON；当前模型 base URL 仍取自
`OPENAI_BASE_URL`，恢复前应使用同一提供方。
`allowed_commands` 是允许的精确 argv 数组；`available_shells` 显式开放 Shell，Git 操作需对应
`git.read` / `git.local_write` / `git.remote_write` / `git.dangerous` 权限。
不需要命令时保持空数组。Shell 授权不是操作系统沙箱；全局删除黑名单及可撤销删除尚未实现。

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    execute-plan --plan-revision-id $PlanId --idempotency-key run-001 `
    --execution-config execution.json --authorize
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    resume-session --run-id $RunId
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-result --run-id $RunId
```

- `execute-plan` 只接受已批准的当前版本，`--authorize` 确认首次执行配置；相同 Run 不能更换配置。
- `resume-session` 使用持久配置，不隐式扩大工具权限；一个前台宿主独占其数据库内的活动 Run。
- 进展输出到 stderr，结果 JSON 输出到 stdout。正常关闭或 Ctrl+C 时收敛 Worker、保存并暂停，
  重开后继续同一 Run；硬杀进程后的未知操作仍需核对，不能承诺任意外部副作用自动续接。
- 整体 Built-in 会话不设总时长、总 Step、总工具调用或总输出字节上限；模型输入保留初始任务与最近
  完整步骤窗口，持久事件不裁剪。单次调用、工具输出和授权边界仍受控。
- 完成的 worktree 保留；宿主捕获实际代码的 Git 提交与 `solution.patch`。`get-result` 返回成果位置、
  基线/候选 commit、diff Artifact 和最终 Attempt 的检查结果。失败历史保留，不误算为最终 Gate 仍失败。
- 最终 Gate 在整合后的真实 worktree 执行。失败时返回原始检查证据，在同一获批方案下修代码并重新检查；
  `report_blocked` 用于说明阻塞原因、证据和所需条件，不伪造成功。完整人工问答回路属于 R3。

### Codex App Server 的接入边界

App Server Connector 只使用一个 Endpoint 对应一个 `codex app-server --listen stdio://` JSONL 连接。
前台执行配置可将 `worker_kind` 改为 `codex-server`，`model` 填写该端点支持的显式模型，
并配置 `"codex_server": {"executable": ["codex"], "approval_policy": "on-request",
"sandbox": "workspace-write"}`。默认启动本机 stdio App Server，而不是 `codex exec`。
也可从 `ehai-api --p2-runtime --worker codex-server` 进入同一装配。当前不承诺连接现有桌面进程或在桌面显示会话，
不以历史 Thread/Turn 验证代替本轮真实调用。

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

- Built-in Planner 已有独立讨论入口及只读 Workspace 工具；更完整的交互体验仍需真实使用确认。
- 公共计划构建仍支持三种检查；Builtin 新方案将最终检查集中到单一最终节点，非空 Artifact 不代表 solution 正确。
- 编码模式的阻塞和外部安全重试耗尽可暂停并给出 notice；复杂回复、改授权与跨方案恢复仍待 R3。
- 顶层通用 Agent、外部意见的完整平台交互和事件驱动 Routines 尚未交付。
- P2 只支持单 Execution Plane 进程；SQLite lease 用于崩溃恢复，不宣称分布式一致性。
- 本地 Built-in `ehai-api --p2-runtime` 使用单 Endpoint `ConcurrentRuntime`，并发上限由
  `--builtin-capacity` 控制；本地 Fake/Codex CLI 组合仍为单槽位。多 Endpoint 路由由应用层组合，
  不是 P3 Dashboard。
- 不支持 OpenCode、Claude Code、DSH Connector、动态插件、P4 Workflow 或 P3 UI。
- Provider 不报告 cost 时公开为 unavailable，不进行虚假估算。
- Artifact、日志、Worker context 和诊断均有边界，超限会 fail closed。
