# EHAI 当前用法

2026-09-14：自研 Agent Loop / Responses Adapter 已删除。Planner、Worker、阶段 Reviewer
与过程边界 Reviewer 已接到完整 Pi 公共 RPC；这是接线状态，尚无真实模型验收证据。
旧数据库可查询，旧 `builtin` 执行配置不能启动或恢复为 Pi。Codex 后端保留。
架构和证据见 [迁移记录](PI_BACKEND_MIGRATION.md)。迁移前完整指南及试用事实保留于
[历史用法](USAGE_PRE_PI.md)，其中已删除的参数不能继续使用。

## 安装与配置

需要 Python 3.12、uv、Node >=22.19.0；Pi 固定为 0.85.1，不全局安装、不修改上游源码。

```powershell
uv sync
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund
uv run ehai --help
uv run ehai-api --help
```

以 [backend.json](../agent-backends/pi/examples/backend.json) 为模板，在仓库外创建私有配置。
`node`、`cli`、`agent_dir` 必须改为本机真实绝对路径。将示例
[settings.json](../agent-backends/pi/examples/settings.json) 和
[models.json](../agent-backends/pi/examples/models.json) 放入该 `agent_dir`。

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
不能据此删除新阶段/过程字段或手动降低数据库版本。来源见 [成果迁移记录](MIGRATION_STATUS.md)。

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

## API 宿主与查询

### 节点受阻与恢复权限

当前节点状态以 `stalled` / `suspended` 取代原 `blocked`：

- `stalled` 是宿主确认可安全重试的执行受阻。当前用于既有 RetrySafety 认可的停止结果；
  持久化安全重试依据，下次调度核对最近 Attempt 已结束后恢复为 `pending`，重新检查依赖。
  不新增模型轮询；仍受原 Attempt 和 Goal 预算、容量及隔离限制。
- `suspended` 必须等人工回复。现有 `report_blocked`
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
未知副作用不能通过标记 stalled 自动重放。

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
HTTP 路由以 [OpenAPI](../schemas/v1/http-api.openapi.json) 为准，业务状态语义仍归 EHAI。

## 破坏性变化与验证

- 已删除旧 Agent Loop、Runtime 装配、Responses Adapter、其 Visualizer 实现及 Python OpenAI SDK。
- 已删除 `--builtin-*` / `--responses-*`；`--worker builtin` / `--planner builtin` 不再接受。
- Pi 管模型历史/压缩；agent_trace 只存业务审计、投递边界和安全事件，不重建模型上下文。
- 当前通过静态检查、Client 构建、离线 RPC 及历史副本查询；真实 Pi 工具、候选、取消中写入、
  压缩、恢复和 Gate 尚未实测，唯一产品 E2E 未运行。

开发使用 uv；Schema 更新运行 `uv run control-plane/scripts/generate-api-schema.py`，再在
control-plane 运行 `npm.cmd run generate`、`npm.cmd run typecheck`、`npm.cmd run build`。
