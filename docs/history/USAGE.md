> 历史快照（2026-09-15 归档），非当前操作说明。当前状态见 [STATUS](../STATUS.md)。

# EHAI 当前用法

2026-09-14：自研 Agent Loop / Responses Adapter 已删除。Planner、Worker、阶段 Reviewer
与过程边界 Reviewer 已接到完整 Pi 公共 RPC；这是接线状态，尚无真实模型验收证据。
旧数据库可查询，旧 `builtin` 执行配置不能启动或恢复为 Pi。Codex 后端保留。
架构和证据见 [迁移记录](../PI_BACKEND_MIGRATION.md)。迁移前完整指南及试用事实保留于
[历史用法](../USAGE_PRE_PI.md)，其中已删除的参数不能继续使用。

## 安装与配置

需要 Python 3.12、uv、Node >=22.19.0；Pi 固定为 0.85.1，不全局安装、不修改上游源码。

```powershell
uv sync
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund
uv run ehai --help
uv run ehai-api --help
```

以 [backend.json](../../agent-backends/pi/examples/backend.json) 为模板，在仓库外创建私有配置。
`node`、`cli`、`agent_dir` 必须改为本机真实绝对路径。将示例
[settings.json](../../agent-backends/pi/examples/settings.json) 和
[models.json](../../agent-backends/pi/examples/models.json) 放入该 `agent_dir`。

示例选择 Pi 自带 `openai` Provider，使用其原生模型目录和容量元数据。空 `providers` 不是
没有模型。选择有权限且 Pi 能精确识别的模型 ID；EHAI 核对实际 Provider、模型和 thinking level，
不接受静默回退。示例不假定某个模型或端点已可用。

凭证只通过执行进程环境传入，不写入 EHAI JSON、数据库或命令参数：

```powershell
$env:OPENAI_API_KEY = Read-Host -MaskInput "Model API key"
```

EHAI 只把 `environment_names` 列出的变量传给 Pi。自定义端点放在原生 `models.json` 的
`providers.<provider>`，使用 `baseUrl`、`api`、`apiKey`、`models`；`apiKey` 必须引用
`$已列入白名单的变量名`，不允许明文 key 或 `!shell`。`api: openai-responses` 使用 Pi 的
Responses 实现。按服务端真实资料填写 contextWindow/maxTokens，不把默认容量当成承诺。
Provider 名称须与 backend.json 相同，具体结构见锁定依赖内 `docs/models.md`。

EHAI 不再读取 `OPENAI_BASE_URL` 作为端点开关，也不读取全局 Pi auth.json 或工作区扩展。
首次载入计算 settings/models 的指纹并随授权保存；恢复遇到文件变化会拒绝，不能悄悄换后端。
保留私有目录及其 `ehai-sessions/`；后者保存 Pi 原生历史，可能含敏感工作内容。
初始集成禁用自动重试：`retry.enabled=false`、`retry.provider.maxRetries=0`。
压缩由 Pi 原生 `compaction` 设置控制，删除旧循环不等于已证明 token 消耗下降。

## CLI 连接正在运行的 API 宿主

`ehai` 有两种显式模式：原来的 `--database` / `--artifacts` 本地模式，以及
`--api-url http://127.0.0.1:8000` 客户端模式（也接受以 `/api/v1` 结尾的 URL）。
后者只发送 HTTP 请求，不打开本地数据库、不启动另一个 Scheduler 或 Pi，不自动切回本地执行。
先用本节后面的“API 宿主”说明启动 `ehai-api`；客户端退出后，任务由仍在运行的宿主继续管理。
它不等于 `execute-plan` 的前台生命周期，也不会自动安装后台服务。

```powershell
# 宿主已经启动；客户端不需要数据库路径或模型 API key。
$EhaiCli = @('--api-url', 'http://127.0.0.1:8000')
uv run ehai @EhaiCli get-runtime-health
uv run ehai @EhaiCli get-worker-profiles

$Project = uv run ehai @EhaiCli create-project --idempotency-key project-001 `
  --name 'My project' | ConvertFrom-Json
$Goal = uv run ehai @EhaiCli create-goal --idempotency-key goal-001 `
  --project-id $Project.project_id --objective '在指定仓库实现已描述的功能' | ConvertFrom-Json
uv run ehai @EhaiCli discuss-plan --idempotency-key discuss-001 `
  --goal-id $Goal.goal_id --message-file C:/private/request.md
```

继续复用 `get-discussion`、`get-plan`、`get-plan-checks` 和 `approve-plan`。每一步先确认上一命令
成功，再使用返回的真实 ID；示例幂等键只用于一次流程，同一意图重放保留键，不同意图使用新键。
模型、Pi 配置和工作区由宿主决定，不在 API 客户端传 `--planner-model` / `--pi-config` 等本地参数；
混用本地存储/模型选项会拒绝，不会静默忽略或改变宿主授权。

批准与执行授权仍是两步：

```powershell
uv run ehai @EhaiCli approve-plan --idempotency-key approve-001 `
  --plan-revision-id '<plan-id>' --completion-contract-id '<contract-id>'
uv run ehai @EhaiCli start-run --idempotency-key start-001 `
  --plan-revision-id '<plan-id>' --execution-config C:/private/execution-api.json --authorize
uv run ehai @EhaiCli get-run --run-id '<run-id>'
uv run ehai @EhaiCli get-result --run-id '<run-id>'
```

`execution-api.json` 是 HTTP `StartRunRequest.execution_config` 的完整对象，与宿主配置一致，
不包含外层 idempotency_key / plan_revision_id。它不是后文前台模式的可省略默认值的简写文件：
须提供 API Schema 要求的 `endpoint_capabilities` 等字段，格式见
[ExecutionConfigRequest](../../schemas/v1/http-api.openapi.json)。其中 workspace、Pi 等路径指向宿主，
客户端不解析这些宿主路径或读取宿主凭证。文件经 UTF-8（允许 BOM）读取，必须是 JSON 对象。
客户端不自动填入授权、默认扩大工具权限、替换模型或批准计划。
API `start-run` 返回并不表示任务完成；随后用 `get-run` / `get-result` 查询。

运行控制同样通过原命令 `pause-run`、`resume-run`、`cancel-run`、`reply-intervention`、
`decide-human-check` 调用宿主，既有 request-token、actor、幂等和状态校验保留。
`propose-process` / `review-process` 在 API 模式返回受理状态及 draft/review ID，不等待模型完成；
使用相应 get 命令查询，审查完成且符合批准边界后才能 `apply-process`。

新增仅 API 模式命令：

| 命令 | 输入与行为 |
| --- | --- |
| `get-runtime-health` | 查看宿主调度健康状态 |
| `get-worker-profiles` / `get-worker-endpoints` | 查看宿主预设与 Endpoint |
| `get-attempt-runtime` / `get-worker-requests` | `--attempt-id`，读取运行状态或待答请求 |
| `suspend-attempt` | `--attempt-id --review-id --through-sequence --actor --reason --idempotency-key`，明确采纳一份审查后定向挂起 |
| `cancel-attempt` | `--attempt-id --idempotency-key`，取消指定 Attempt；不等同于人工 suspend |
| `extend-attempt-deadline` | `--attempt-id --deadline-at --idempotency-key`，时间必须含时区 |
| `resolve-worker-request` | `--worker-request-id --resolution-file --idempotency-key`，resolution 是请求所需的 JSON 对象 |
| `decline-worker-request` | `--worker-request-id --idempotency-key`，拒绝该请求 |

定向挂起只封装既有审查驱动接口，不提供绕过审查证据的任意挂起或自动采纳策略。例如：

```powershell
uv run ehai @EhaiCli get-run-trajectory-reviews --run-id '<run-id>'
uv run ehai @EhaiCli suspend-attempt --attempt-id '<attempt-id>' `
  --review-id '<review-id>' --through-sequence 30 --actor operator `
  --reason '经审查确认偏离本节点目标，需要人工决定' --idempotency-key suspend-001
```

`through-sequence` 必须用查询所得该审查实际覆盖的序号，示例 30 不是步长默认值。
旧审查、错误目标或已变化的状态仍由宿主拒绝。`execute-plan` / `resume-session`、`recover`、
`restore-run` 和 `inspect-agent` 不支持 API 模式，不把本地恢复命令映射成远程重置。

成功时 stdout 输出 API `data` 中的一个 JSON 值；字段以 API 契约为准，不承诺与所有旧本地写命令
的简略输出完全相同。HTTP 错误写 stderr：`{http_status, response}`，保留服务端错误正文，退出 2。
可选 `--api-timeout-seconds` 是 HTTP socket 超时，不是任务截止时间；默认不额外设置该超时。
网络错误、非 JSON 结果、中断均不会自动重发，重定向也不自动跟随；结果未知时先查宿主。
Ctrl+C 退出 130，只停止客户端等待，不向宿主发取消；需要停止工作应另发明确的 pause/cancel 命令。
当前 API 无新增身份认证层；URL 不接受内嵌凭证，模型 key 留在宿主。默认使用回环地址，
不要直接将无认证的执行 API 暴露到公网。

## 查询代码交付结果

`get-result`、HTTP `GET /api/v1/runs/{run_id}/result` 和生成 Client 的
`getRunResult(runId)` 复用 `QueryService.get_run_result`。它在同一 ReadSession 中物化执行事实，
不读取 Artifact 正文、不调用模型、不推进运行状态。

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-result --run-id $RunId
```

CLI 返回 `{run, result, trace_ids}`；HTTP/Client 返回 `{data: {run, result, trace_ids}}`。
`result` 包含代码 workspace、base_commit、commit、diff_path、attempt_id、run_id、
diff_artifact_ids、check_result、configured_workspace 和绝对 artifact_root。
`trace_ids` 按持久轨迹顺序提供 Attempt、Artifact、CheckRun、Checkpoint、Event 的 ID。
完整类型和可空性见 `schemas/v1/queries.schema.json` 的 `RunResultDocument`。

迁移相对旧 main 修正了检查结果归属：有代码交付时只报告该交付 Attempt 的 CheckRuns，
没有对应检查则 `check_result=null`，不再拿其他 Attempt 的检查背书。无代码交付时保留最近
有检查 Attempt 的历史 fallback；无检查时 passed 为 null、checks/check_run_ids 为空数组。
聚合判定为 false 优先，其次未知 null，全部明确通过才是 true；它不是新增的 Run/Gate 状态机。
已有调用方必须处理 `check_result=null`，不能假设它始终是对象。

合法但不存在的 Run：CLI 返回原 not-found 错误，HTTP 返回 404 和 `error.code=not_found`；
非法 UUID 返回 HTTP 422。存储错误和冲突的 execution_config 不转换为“空结果”。普通本地 API
和 P2 API 装配都传入 artifact_root；直接调用 `create_app` 的嵌入方需显式提供该参数才能使用新路由。

当前已合入 Pi 后端和阶段执行能力；M1 单独迁移时的 schema 11 限制是历史记录，
不能据此删除新阶段/过程字段或手动降低数据库版本。来源见 [成果迁移记录](../MIGRATION_STATUS.md)。

## 无模型检查

```powershell
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  inspect-agent --node node `
  --pi-cli agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js
```

检查不访问给定数据库/产物目录，不读取 key，不发送 prompt。系统 Node 太旧时显式传兼容
Node 的绝对路径。`control_channel_verified` 和 `worker_integration_available` 不代表模型验收。

## 规划与批准

提供 `--pi-config` 或 `--planner-model` 时，省略 `--planner` 会自动选择 Pi；缺少配置或模型
会报错，不回退到固定模板。显式 `--planner single` / `exploration` 仍用于无模型演示。
没有任何 Pi 配置或模型参数时保留原有无模型默认入口，查询不依赖后端配置。

通过 `create-project`、`create-goal` 创建对象后，调用 Pi 讨论；每次新请求用新业务幂等 key：

```powershell
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  --planner pi --pi-config C:/private/ehai-pi/backend.json `
  --planner-model "<exact-model-id>" --planner-reasoning-effort high `
  --worker-workspace C:/work/target `
  discuss-plan --idempotency-key "<new-key>" --goal-id "<goal-id>" `
  --message "<requirements and acceptance expectations>"
```

继续讨论加 `--conversation-id`。Planner 有只读调查和方案工具，不拥有执行批准。
用 `get-discussion`、`get-plan`、`get-plan-checks` 查询后，再明确批准：
`approve-plan --idempotency-key <key> --plan-revision-id <id> --completion-contract-id <id>`。
这些子命令均须全局 `--database` / `--artifacts`，其余必填字段见各自 `--help`。

### Planner 草稿 Gate 编辑

以下是 Pi Planner 的模型工具，不是新增 CLI 子命令或直接修改已保存方案的 HTTP 入口。
它们编辑本轮内存草稿；只有 `finish_plan` 通过校验后，正常规划入口才保存方案供用户审查。

- `set_node_gate` / `set_final_gate` 创建或完整替换对应草稿 Gate。`argv=[]` 清空命令条件，
  `human_question=null` 清空人工条件；两者不能同时为空，不用空定义隐式删除。
  模型 Schema 要求显式提供这两个字段，不将省略解释成保留原条件。
- `remove_node_gate` 接收 `node_key`，删除该节点的整个草稿 Gate，保留节点、边、阶段及最终 Gate。
  节点存在但未设 Gate 时成功；节点不存在返回 `UNKNOWN_NODE`，不会创建节点。
- `remove_final_gate` 接收空对象，清空草稿最终 Gate 的命令和人工条件；原本为空时也成功。
  不删除宿主配置的完成条件。若最终 Gate 必需，仍须重新配置才能通过 `finish_plan`。
- `NODE_GATE_FINAL_CONFLICT` 表示最终节点被重复设置节点 Gate；错误返回对应 `node_key` 和
  `remove_node_gate` 修复入口。先确保必需条件保留在最终 Gate 中，无需删除或重建整个节点/阶段。
- 删除中间阶段必需 Gate 后，仍须修复才能提交。操作沿用修改预算，重复删除也消耗一次操作；
  `inspect_plan` 可重读状态，已成功 `finish_plan` 的草稿不能再修改。
- 过程调整不注册 Gate 设置/删除工具；直接调用也由 Handler 拒绝。已批准 Gate 和 Check 仍冻结，
  只允许通过原有 `move_process_gate` 在批准边界内整体转移归属，不允许降低验收条件。

## 前台执行

在仓库外创建 execution.json，以下占位路径和模型必须替换，`pi` 使用前述相同真实配置：

```json
{
  "config_version": 1,
  "worker_kind": "pi",
  "model": "<exact-model-id>",
  "reasoning_effort": "high",
  "capacity": 1,
  "workspace": "C:/work/target",
  "allowed_commands": [],
  "available_shells": [],
  "git_permissions": ["git.read"],
  "command_timeout_seconds": 30,
  "pi": {
    "node": "C:/tools/node/node.exe",
    "cli": "C:/work/EHAI/agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
    "agent_dir": "C:/private/ehai-pi",
    "provider": "openai",
    "environment_names": ["OPENAI_API_KEY"]
  }
}
```

`allowed_commands` 是完整 argv 数组，不是任意 Shell 授权；Shell/Git 写权限须单独允许。
实际编码使用 EHAI 工作区、宿主工具和候选校验，禁用 Pi 原生文件/Shell 工具。
工具 strict 采样由 Pi 请求，不支持时失败，不静默降级。

```powershell
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  execute-plan --idempotency-key "<new-key>" --plan-revision-id "<approved-id>" `
  --execution-config C:/private/ehai-pi/execution.json --authorize

uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  resume-session --run-id "<pi-run-id>"
```

Pi 保持长运行模式，不新增隐式模型调用时限；原有 Codex timeout 参数不控制 Pi。
API 可显式配置 `--attempt-deadline-seconds`，Goal Worker 尝试预算仍属宿主配置。
超时/取消不是完成或安全恢复证明；关闭队列/进程后保留未知
结果，按宿主 handoff/有效上游成果规则处理，不盲目重发 prompt。阶段讨论是共享逻辑 Session，
每个 Worker 有隔离的 Pi 原生执行会话，不强行共用物理历史。

## 节点目标边界（基础接入，挂起判定尚未验收）

Pi Worker 的 `context.execution_scope` 由宿主从当前派发节点生成，包含 Run、批准方案、
过程版本、节点目标、依赖和必需 Check 引用。Worker 自定义提示词也会附加目标边界策略。
Planner 提示要求在节点 instruction 中说明交付物、局部自主范围与需要反馈的缺口；
这是提示约定，不新增必填 Schema 或强制每一步工具操作。

发现必须超出节点目标或已批准边界时，Worker 应调用现有 `report_blocked(reason, evidence, needed)`。
这会结束该次 Pi 执行；宿主中断 Attempt、将节点置为 `suspended` 并持久化介入请求。
独立就绪任务仍按原调度规则执行；前台等待介入时可返回
`intervention_waiting`，不保证 Run 必须显示 paused。使用 `get-run-interventions` 查询，
`reply-intervention` 回复后再 `resume-session`；回复本身不批准扩大需求或修改 Gate。

该版本只提供目标上下文、行为提示和已有挂起通路，没有额外的逐步模型裁判、
语义越界检测器或重复失败次数规则。必要调查/调试不应被当作越界，无关小问题可记录后继续。
当前两次 Luna 试跑没有触发预期 report_blocked，不能据此宣称自动范围挂起已可可靠使用；
详见 R2 实施记录。已有 Reviewer/Gate 也不能由 artifact:non-empty 代替需求验收。

## 长任务轨迹审查（仅建议）

新 Pi Run 的 execution.json 加上 `"trajectory_review": {}` 即启用，默认 600 秒或
30 个执行步先到触发，审查模型为 gpt-5.6-luna/high。可显式设置：

```json
"trajectory_review": {
  "interval_seconds": 600,
  "step_count": 30,
  "model": "gpt-5.6-luna"
}
```

省略或设为 null 不启用；旧 Run 不会因升级代码自动增加模型审查，已授权 Run 的配置不静默改写。
API Pi/P2 宿主使用 `--trajectory-review`，可配 `--trajectory-review-seconds 600` 与
`--trajectory-review-steps 30`；StartRun 的 execution_config 必须与该宿主配置一致。

执行步是一次有效模型响应及其工具批次完成；多个工具不重复算多步，流式片段、心跳和
finish 后的空取消记录不算。每个 active Attempt 独立计数；无新增轨迹跳过，同一 Attempt
不重叠审查。已结束的短任务不补审，Worker 结束/取消时收敛其未完成的审查进程。

审查只使用节点范围、批准条件、上次意见和新增轨迹快照。工具输出中的 patch/差异与
handoff 信息作为证据；审查者没有工作区读取或写入工具，不读取正在变化的文件。
输入文本经过脱敏与长度裁剪，并标明截断和遗漏事件；证据不足应输出 insufficient_evidence。
默认不回传意见给 Worker，不自动 steer、挂起、改图或决定 Gate。失败记录不重放同一窗口。

```powershell
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  get-run-trajectory-reviews --run-id "<run-id>"
```

HTTP：`GET /api/v1/runs/{run_id}/trajectory-reviews`；Client：`getRunTrajectoryReviews(runId)`。
返回每份审查的最新状态、意见、Worker/Reviewer Session、覆盖的轨迹序号、记录时间、
Worker 当前状态和 reported_usage。意见结论是 continue、refocus、suggest_suspend 或
insufficient_evidence；这些不是 Run 状态。started/failed/cancelled 不能当成有效审查意见。
Worker 可能已推进到窗口之后；意见不是对最新工作区的自动判定。审查不替代最终验收。

当前已在正常 CLI 路径以 2 步阈值验证后台审查与结果查询；默认 600 秒/30 步已核对配置，
尚未完成真实 10 分钟长跑、多 Worker 压力、崩溃恢复和全部意见分支验收。

## 显式采纳意见并挂起单个节点

向实际承载目标 Pi Worker 的 API/P2 宿主发送：

```http
POST /api/v1/attempts/{attempt_id}/suspend
Content-Type: application/json

{
  "idempotency_key": "<new-decision-key>",
  "review_id": "<latest-completed-review-id>",
  "through_sequence": 123,
  "actor": "<decision-maker>",
  "reason": "<why this node should be held>"
}
```

review_id、through_sequence 和 attempt_id 来自 get-run-trajectory-reviews。
Client 方法为 `suspendAttemptFromReview(attemptId, request)`。这是显式的上层控制决定，
不是审查 Agent 自动执行的动作；调用方可依据证据作出不同于模型 verdict 的决定并记录理由。
只有最新完成意见且目标仍为同一运行中 Attempt、Session、节点目标和过程/批准版本时可执行；
完成、已挂起、换实例或图已变化时，新请求拒绝。意见窗口之后有新轨迹不代表自动失效，
因此调用方仍需判断该意见是否适用，而不是机械采纳过时建议。

宿主只停止该 Attempt，等待 Pi/宿主工具收敛，通过既有 block_attempt 持久化 intervention，
Attempt 为 interrupted、节点为 `suspended`；不调用整 Run pause，不加入整 Run 调度停止集合。
下游依赖未满足而等待，独立节点依旧按图调度。有效 handoff 与工作区保留；脏工作区仅为证据，
不自动当作可续跑成果。中断可能有未确认工具效果，介入记录要求恢复前核实，不假装副作用回滚。

响应 status 是本次控制操作状态，不是 Run 状态：suspended 表示已形成该决定对应的介入，
requested 仅表示请求已持久保存、尚无该挂起结果；not_suspended 表示执行已先行结束等未形成挂起。
完成先于取消时保留真实完成结果。另看 attempt_status、node_status 和 intervention.status 判断
当前事实。相同幂等键/参数重发只查询已有结果，不重新取消后来执行；参数不同会冲突。
请求结果未知或宿主崩溃时不自动重发原取消。actor 是调用者审计标识，不新增权限认证机制。

后续用 get-run-interventions 查看证据。保持原目标时，经 `reply-intervention` 或现有 HTTP
回复接口解释如何安全继续；若要调整路线，使用已有 propose-process/review-process/apply-process
流程，挂起证据会作为 Run 介入信息进入 Planner 上下文，审批边界不变。
回复可解除对应节点阻塞，后台宿主按原调度机制继续；前台/已停止宿主需恢复运行入口。
本次不自动启动 Planner、不授予 Planner 新的控制工具、不自动批准修改。

这个定向入口需要拥有活跃执行句柄的 API 宿主；不能另起一个不拥有该 Worker 的 CLI 进程只改数据库。
前台 execute-plan 尚未增加跨进程控制通道。该审查驱动入口已实测单节点挂起、另一 Run 的 Worker
继续、幂等重读和回复解除阻塞；同图探索分支、过程调整后续跑及崩溃竞态仍未验收。
另一次真实 Luna 正常 API 验收已通过 report_blocked 驱动的 suspended、同图独立文档任务继续、
人工回复后代码续跑、阶段 Reviewer 与最终行为 Gate；它没有启用周期审查，也不替代审查驱动入口
的未验证项。完整证据与 token 统计见 R2 实施记录中的“真实 Luna 规划、同图并行挂起与恢复验收”。

## API 宿主与查询

### 节点受阻与恢复权限

当前节点状态以 `stalled` / `suspended` 取代原 `blocked`：

- `stalled` 是宿主确认可安全重试的执行受阻。当前用于既有 RetrySafety 认可的停止结果；
  持久化安全重试依据，下次调度核对最近 Attempt 已结束后恢复为 `pending`，重新检查依赖。
  不新增模型轮询；仍受原 Attempt 和 Goal 预算、容量及隔离限制。
- `suspended` 必须等人工回复。现有 `report_blocked`、显式采纳轨迹审查的 suspend 操作，
  以及安全重试预算耗尽都会进入该状态。后者只挂起相关节点，不再因此暂停整个 Run。
- `get-run-interventions` / HTTP interventions 给出原因、证据和所需决定；
  `reply-intervention` / HTTP reply 解除对应挂起，回到 `pending`。整 Run resume 不解除它，
  回复不重置已消耗的预算。启动前即失败时介入的 `agent_session_ref_id` 可为 `null`。
- 正常等待上游仍是 `pending`。`stalled` 和 `suspended` 均不是终态，也不直接参与
  Worker 派发、分支失败判定或 Gate 完成。

旧持久节点 `blocked` 在读取时归一化为 `suspended`，不原地改写历史事件或批准快照；
旧事件文本可以保留 blocked。公开节点枚举和新 Planner 上下文使用新名字，Client 需重新生成。
工具 `report_blocked`、内部 WorkerEvent blocked、介入种类 worker_blocked 保留协议名字，
它们不是节点状态。Run 的 paused 与 Attempt 的 interrupted/failed 等状态不改名。

当前 `stalled` 接通的是确定性安全重试，不表示已交付通用外部条件监听或新的 Planner 自动纠偏链路。
未知副作用不能通过标记 stalled 自动重放；周期审查依旧只提供建议。

### 启动宿主

```powershell
uv run ehai-api --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  --worker pi --worker-workspace C:/work/target `
  --pi-config C:/private/ehai-pi/backend.json `
  --agent-model "<exact-model-id>" --agent-reasoning-effort high `
  --worker-capacity 1 --p2-runtime
```

API 提供 `--pi-config` 后默认选择 Pi Planner；用 `--planner-model <id>` 指定独立规划模型，
未指定时沿用 Pi Worker 的 `--agent-model` 与 thinking 设置。`--p2-runtime` 配合 Pi 配置，
或提供 `--agent-model`，省略 `--worker` 时默认选择 Pi Worker；缺少必要参数明确失败。
显式后端选择仍优先。前台自动过程规划/审查仍须 execution.json 的 process_adjustment 授权，
默认后端选择不会自行启用自动规划或批准执行。
`--attempt-deadline-seconds` 是可选 Attempt 期限；`--agent-allowed-command`、`--agent-available-shell`、
`--agent-git-permission` 配置宿主工具权限。HTTP execution_config 必须与宿主匹配，不能临时扩权。
HTTP 兼容字段 `endpoint_capabilities` 是旧记录词汇，不控制 Pi；真实能力来自 Pi 原生配置。

无需模型凭证的查询：

```powershell
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  get-result --run-id "<run-id>"
uv run ehai --database .ehai/state.sqlite --artifacts .ehai/artifacts `
  get-trace --run-id "<run-id>"
```

get-run-plan、get-run-checks、get-run-adoptions、过程查询及人工介入命令保留，精确参数见 help。
HTTP 路由以 [OpenAPI](../../schemas/v1/http-api.openapi.json) 为准，业务状态语义仍归 EHAI。

## 破坏性变化与验证

- 已删除旧 Agent Loop、Runtime 装配、Responses Adapter、其 Visualizer 实现及 Python OpenAI SDK。
- 已删除 `--builtin-*` / `--responses-*`；`--worker builtin` / `--planner builtin` 不再接受。
- Pi 管模型历史/压缩；agent_trace 只存业务审计、投递边界和安全事件，不重建模型上下文。
- 当前通过静态检查、Client 构建、离线 RPC 及历史副本查询；真实 Pi 工具、候选、取消中写入、
  压缩、恢复和 Gate 尚未实测，唯一产品 E2E 未运行。

开发使用 uv；Schema 更新运行 `uv run control-plane/scripts/generate-api-schema.py`，再在
control-plane 运行 `npm.cmd run generate`、`npm.cmd run typecheck`、`npm.cmd run build`。
