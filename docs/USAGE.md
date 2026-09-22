# EHAI 当前用法

更新：2026-09-22。能力成熟度见 [STATUS](STATUS.md)，历史用法不作为当前参数说明。
首次用 0.1 开发项目，先按 [README 快速开始](../README.md#快速开始) 配置宿主并完成一个任务；
本页补充控制、恢复、计划变更和成果接续的详细契约。

同时管理多个仓库、配置各自 Planner 或限制规划并发，见 [多工作区后端](WORKSPACE_MANAGER.md)。
原业务接口可通过管理地址加 `/workspaces/{workspace_id}` 前缀使用，批准、授权和恢复仍在对应核心完成。

## 项目总览（P3.1）

已有 API 宿主时，从项目入口发现目标、计划、Run 和成果：

```powershell
$Api = @('--api-url', 'http://127.0.0.1:8000')
uv run ehai @Api get-runtime-context
uv run ehai @Api list-projects
uv run ehai @Api get-project --project-id '<project-id>'
```

`list-projects` 返回 `projects` 摘要列表；`get-project` 返回项目及其 `goals`，每个目标含
`plans` 和 `runs`。Run 摘要含原始状态、当前 `process_revision_id`、`node_counts`、
历史授权的 `configured_workspace`、实际 Attempt 的 `worker_endpoint_ids` 和
`completed_results`。可用返回的 ID 继续调用 `get-run-plan`、`get-run-checks`、`get-result`
或 `get-trace`；这些查询不启动 Worker，也不执行 Git 整合。

`completed_results` 只列当前图中完成、分支已选定并具有匹配生产者 Gate 证据的节点。
每项带 checkpoint、Gate、Attempt、可空 adoption ID 和成果元数据；跨 Run 接续保留
Artifact 原始来源。没有匹配 Gate 的节点不会被当作已验证成果。元数据查询不重新验证
磁盘字节或保证 Git 可整合；需要整合时仍使用 `integrate-run`。

列表和详情的 `observed_at`、`event_offset` 标明只读快照的读取时间和事件位置。
`get-runtime-context` 返回当前宿主工作区、Endpoint、执行配置指纹及调度健康；
它与 Run 的历史授权信息分别呈现。未记录或未装配的值为 null，不能推断在线或已完成。
历史 Endpoint ID 也不表示该 Endpoint 当前在线。不存在的项目返回 404，空项目返回空列表。

沿用一宿主一工作区配置。不同仓库使用各自宿主、数据库、产物目录和 API 地址，
调用方保留来源 API 地址与项目/Run ID 的组合，再向同一来源提交后续操作。
不要让不同工作区的宿主共享执行数据库。Project 不自动绑定当前目录或授权新的仓库；
本轮没有单宿主动态多仓库路由或跨宿主聚合服务。

本地只读查询也支持 `list-projects` 和 `get-project`，沿用 `--database`、`--artifacts`
参数；此时没有在线宿主事实，不能使用 `get-runtime-context`。
MCP 对应工具为 `list_projects`、`get_project` 和 `get_runtime_context`；
TypeScript Client 对应 `listProjects()`、`getProject(projectId)`、`getRuntimeContext()`。

## 统一人工待办（P3.2）

```powershell
$Api = @('--api-url', 'http://127.0.0.1:8000')
uv run ehai @Api list-inbox
uv run ehai @Api list-inbox --project-id '<project-id>'
uv run ehai @Api list-inbox --run-id '<run-id>'
uv run ehai @Api get-inbox-item --kind human_check --request-id '<check-run-id>'
```

HTTP 为 `GET /api/v1/inbox?project_id=...&run_id=...` 和
`GET /api/v1/inbox/{kind}/{request_id}`。两种筛选均可省略，同时提供时必须符合归属，
否则 422；不存在的项目、Run 或待办返回 404。首版返回该宿主范围内的完整当前待办列表，
没有分页或新的待办存储。`kind` 包含 `intervention`、`human_check`、`worker_request`、`note`。
运行前便签可以只有项目/目标归属，因此 owner 的 Run、节点、Attempt 和 Run 状态允许 null。

每项包含 `owner` 归属、问题、证据、源状态、`pending`、`actionable`、不可处理原因、
`actions` 和 `next_step`。`request_token` 保留人工干预/验收原请求版本；Worker 请求为 null。
列表按创建时间排序；Worker 请求没有持久创建时间，`created_at=null`，按宿主首次观察时间排序。
历史详情可查询已处理或被后续批准明确处置的请求，`disposition` 保留操作者、原因和批准来源；
这些旧请求不会重新进入当前列表，也不会被改写为旧 Gate 通过。

按 `actions[].operation` 调用原操作，`arguments` 是已绑定的源 ID/token/判定值，
`input_fields` 是用户仍需提供的字段；写入端会再次检查当前状态、证据及批准边界：

| operation | 现有接口 |
| --- | --- |
| reply-intervention | POST /api/v1/interventions/{intervention_id}/reply |
| decide-human-check | POST /api/v1/check-runs/{check_run_id}/decision |
| resolve-worker-request | POST /api/v1/worker-requests/{worker_request_id}/resolve |
| decline-worker-request | POST /api/v1/worker-requests/{worker_request_id}/decline |
| add-note-message | POST /api/v1/notes/{note_id}/messages |
| decide-note | POST /api/v1/notes/{note_id}/decisions |

例如人工验收通过时，向 decision 提交 `idempotency_key`、原 `request_token`、
`passed=true`、`actor` 和 `comment`；CLI 仍使用 `decide-human-check --passed`。
回复干预不扩大批准，人工 Check 决定由核心继续评估 Gate。提交后重读待办和 Run；
Run 已暂停时，普通回复不隐式恢复；人工 Check 需先明确恢复 Run 才能作出决定。

外层 `observed_at` / `event_offset` 描述持久事实的只读快照；`worker_requests` 单独给出
运行期观察时间及 `available` / `partial` / `unavailable`，并列出无法读取的 Attempt。
它们不是同一原子快照。空列表且 Worker 来源不可用，不表示没有任何人工请求。
本地 `--database` / `--artifacts` 模式可查询持久待办，Worker 来源明确为 unavailable。

Worker 详情的 `worker_form.context` 只包含选定的请求字段，`resolution_schema` 给出
发送到原 resolve 接口的 `resolution` 对象格式。输入问题保留问题 ID、选项和自由输入/秘密输入提示；
命令批准只建议本次 accept，权限批准只建议明确列出的权限及 turn 范围。
上下文缺失、脱敏/截断或仅提供不支持的授权范围时，表单给出不可回答原因，仍可明确拒绝。
表单是该待办建议的回答子集，不覆盖原 Provider 的全部可选响应；旧 resolve 接口继续承担传输。

Worker 请求 ID 和处理回执只在当前宿主进程有效。回答前重新核对源请求；相同幂等键不能用于
不同的请求或答案。回答发送中或发送失败后结果未知时，详情禁用后续回答并要求核对 Run；
不自动重发。宿主重启后的旧 Worker ID 不可用于重放，需重新查询当前请求。

MCP 对应 `list_inbox(project_id, run_id)`（不筛选时参数显式为 null）及
`get_inbox_item(kind, request_id)`。TS Client 对应 `listInbox({ projectId, runId })`、
`getInboxItem(kind, requestId)`；筛选项均可省略。处理操作继续使用现有 Client 方法。

## 便签讨论与明确决定（P3.2 后端）

便签可以关联尚未运行的 Goal，也可以关联 Run 及其 Intervention/HumanCheck。
每条便签保存问题、证据、来源批准/过程版本、消息和决定结果；普通消息只推进讨论。
当前新入口使用 API 宿主，CLI 是同一接口的客户端。

创建文件 `C:/private/note.json`：

```json
{"goal_id":"<goal-id>","actor":"user","question":"需要明确哪条实现路线？","evidence":"目前已确认的约束与证据"}
```

```powershell
uv run ehai @Api create-note --file C:/private/note.json --idempotency-key note-001
uv run ehai @Api list-notes --project-id '<project-id>'
uv run ehai @Api get-note --note-id '<note-id>'
```

若便签要处理现有请求，创建时额外提供 `run_id`、`source_kind`（intervention 或 human_check）、
`source_id` 和原 `source_token`，宿主核对归属与请求版本。公共创建 origin 仅允许 user/agent；
Planner 工具生成的 origin=planner 和 actor=planner 由宿主固定。

向 `add-note-message --note-id ... --file ... --idempotency-key ...` 提供
`{"request_token":"<note-token>","actor":"user","message":"补充事实或回复"}`。
每次消息产生新的 note request_token，下一次消息/决定使用新 token；旧 token 拒绝。
`list-notes` 可按 project/goal/run 筛选，`--include-resolved` 包含已关闭讨论。

向 `decide-note --note-id ... --file ... --idempotency-key ...` 提供：

```json
{"request_token":"<latest-note-token>","actor":"user","action":"continue","message":"已核实，可以继续","passed":true}
```

| action | 实际后续行为 |
| --- | --- |
| resolve | 关闭讨论，不改变执行或批准 |
| continue | 仅针对绑定的原 Intervention/HumanCheck，调用原回复/判定命令；人工 Check 必须明确 passed，其他情形省略 passed 或为 null |
| propose_process | 对明确暂停的 Run 调用现有过程 Planner，返回实际 ready 草稿；后续仍须 review-process / apply-process |
| revise_plan | 将便签问题、证据、讨论和决定传入现有 discuss-plan，保存规划会话/可能的草稿；后续批准和执行授权仍独立 |

修改方案前先明确暂停来源 Run；普通讨论、便签创建不会擅自暂停全部执行。
`propose_process` / `revise_plan` 需要对应 Planner 装配，可能调用模型；单个规划输入仍受现有
8000 字符限制，超出时需整理为精简后续便签。新增需求/接口/Gate/权限不会因便签决定隐式获批。
结果记录在 `decision.result`；讨论完成与计划已批准、Run 已完成分别读取。

决定先记录操作意图，再以固定幂等键调用核心，最后持久保存结果。
结果未知时不重放模型/业务操作；同键再次请求只核对已有下游持久回执，已有确定结果时收敛记录。
源请求/批准/过程已失效时，新执行决定拒绝；历史仍可读，关闭讨论不等于把旧执行结果改成成功。

Pi Planner 已接入 `raise_note(question,evidence)`，发现差距或取舍可生成持久便签并结束本轮规划。
建议通过 discuss-plan 使用这一能力；直接 propose-plan 仍要求返回方案，只有便签时会保留便签并报无方案。
它不赋予 Planner 任意暂停执行的权限；运行期阻塞继续沿现有 Intervention 和核心挂起语义。
MCP 工具对应 create_note/list_notes/get_note/add_note_message/decide_note，写入仍用 request_json。

## 外部 Agent 持久事件消费（P3.4 后端）

每个 consumer_id 标识该宿主数据库上的一个消费进度；允许 1–128 位字母、数字、点、下划线或连字符，
首位必须是字母或数字。注册从保留历史的开头读取；重复注册返回原进度，不重置、不跳到最新。

```powershell
uv run ehai @Api register-event-consumer --consumer-id assistant-main
uv run ehai @Api read-consumer-events --consumer-id assistant-main --limit 100
uv run ehai @Api ack-consumer-events --consumer-id assistant-main --batch-token '<batch-token>'
uv run ehai @Api get-event-consumer --consumer-id assistant-main
```

HTTP 对应 POST /event-consumers、GET /event-consumers/{consumer_id}、
POST /event-consumers/{consumer_id}/batches（body 为 limit）、POST /event-consumers/{consumer_id}/ack
（body 为 batch_token），均位于 /api/v1 下。

同一 consumer 同时只有一个待确认批次，重复读取和宿主重启均返回相同事件与 token；
确认后才前进到下一批。空批次 token=null，无需确认。未知或其他 consumer 的 token 拒绝。
重复确认返回该 token 原来的确认回执；查询 consumer 才能看到它后来推进到的当前位置。
一个 consumer 是共享订阅而非独占 Worker 租约；多个调用者可能收到同一批次。

当前按宿主消费完整事件流，不提供项目过滤、重置或任意位置跳跃。外部 Agent 自行选择相关事项，
使用现有业务命令幂等键防止重复动作，核对实际处理结果后确认批次。
消费和确认不执行任务，不等于外部 Agent 自动唤醒；原 HTTP 事件查询/SSE 继续可独立使用。

## 项目规则、宿主绑定与执行快照（P3.5 后端）

```powershell
uv run ehai @Api get-configuration-host
uv run ehai @Api get-project-configuration --project-id '<project-id>'
uv run ehai @Api configure-project --project-id '<project-id>' --file C:/private/project-rules.json --idempotency-key config-001
uv run ehai @Api get-project-configuration-versions --project-id '<project-id>'
uv run ehai @Api get-run-configuration --run-id '<run-id>'
```

配置文件完整替换示例：

```json
{
  "expected_version": 0,
  "workspace": "<workspace returned by get-configuration-host>",
  "execution_config_fingerprint": "<fingerprint returned by get-configuration-host>",
  "role_configuration_ref": "<role ref returned by get-configuration-host>",
  "static_rules": ["代码和文档使用项目约定的命名规范"]
}
```

首次版本使用 expected_version=0，后续填当前版本；`static_rules=[]` 明确清空新版本规则。
无完整执行配置的 fake 宿主 fingerprint 为 null，按宿主原值填写，不能编造授权指纹。
错误的版本、工作区或宿主角色/配置引用返回冲突，不能切换到另一个项目工作区继续执行。
HTTP 为 GET /project-configuration-host，GET/POST /projects/{project_id}/configuration，
GET /projects/{project_id}/configuration/versions 和 GET /runs/{run_id}/configuration，均在 /api/v1 下。

配置版本不可变。新 Run 在创建事务中捕获所用版本和规则，Worker 从该快照获取上下文；
更新项目配置只影响后续 Run，已有 Run 和运行中的过程 Planner 保留原版本。
初始规划使用当前项目规则，旧 Run 的修订讨论使用原 Run 快照。无配置的项目显式捕获 version=0；
升级前没有快照的历史 Run 返回 null，不用当前规则填补历史。

角色引用来自当前宿主实际装配的 worker_kind/model/reasoning_effort，多个项目可引用同一宿主角色；
它不是新增的可编辑角色库。Pi 私有配置和凭证仍在宿主文件/环境，项目接口不保存密钥或远程任意模型配置。
精确执行配置继续显式授权、由原宿主校验；每 Run 的过程调整策略沿原校验规则独立处理。
本轮不改变一宿主固定工作区的拓扑，不提供跨宿主调度器或单宿主动态多仓库执行。

新数据库版本为 18：新增事件消费进度/批次回执和项目配置版本表。
便签及执行配置快照使用现有事件日志；所有新增数据均在宿主指定数据库，升级前请保留自己的数据库备份。

## 安装与私有 Pi 配置

需要 Python 3.12、uv、Node >=22.19.0。运行 uv sync --frozen，
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund。

在仓库外配置 backend.json，参照 [示例](../agent-backends/pi/examples/backend.json)：
node、cli、agent_dir 都是宿主真实绝对路径；provider 必须与原生 models.json 一致；
environment_names 是允许传入 Pi 的模型凭证环境变量名。settings/models 放在 agent_dir。
密钥仅经宿主环境传入，不放计划、命令参数、Git 或日志。原生配置见锁定 Pi 包 docs/models.md。

自定义端点由 Pi models.json 配置，EHAI 不读取 OPENAI_BASE_URL 切换端点。
当前禁用隐式资源发现和自动模型重试；配置指纹随执行授权保存，变更时不能静默恢复旧授权。
模型 ID/推理等级须精确匹配 Provider。示例占位符必须替换，不保证任意模型可用。

旧自研 Runtime 与 --builtin-* / --responses-* 用法已退役；旧执行授权不能直接当作 Pi 授权。
恢复旧数据时使用当前代码的迁移流程，不按历史文档中的数据库版本号手工降级。

## 本地规划或导入

所有本地业务命令需要 --database 和 --artifacts。先 create-project，再 create-goal。
可选 Planner 用 --pi-config、--planner-model、--worker-workspace 配置，
discuss-plan --goal-id ... --message ... --idempotency-key ... 进行讨论，
继续讨论加 --conversation-id；也支持 --message-file UTF-8 文件。

不需要 Planner 时：

~~~powershell
$Local = @('--database','C:/private/ehai/state.sqlite','--artifacts','C:/private/ehai/artifacts')
uv run ehai get-plan-import-schema
uv run ehai @Local import-plan --goal-id '<goal-id>' --file C:/private/plan.json --idempotency-key import-001
uv run ehai @Local get-plan --plan-revision-id '<plan-id>'
uv run ehai @Local get-plan-checks --plan-revision-id '<plan-id>'
~~~

[示例计划](../examples/plan-import.json) 和 [生成 Schema](../schemas/v1/plan-import.schema.json)
使用与 Planner 工具相同的节点/边/阶段/Gate 字段。顶层字段全部显式给出：
schema_version=1、design_document、nodes、edges、branches、phases、node_gates、final_gate。

空集合用 []；可空项按 Schema 用 null；不接受 remove 操作、节点状态、批准、真实执行 ID、
retained Gate/Check ID 或任意扩展字段。依赖来自 edges，真实 UUID 由核心分配。
当前使用共同图预算：最多 128 次构建修改、探索宽度 3/深度 2；导入正文最多 1,000,000 UTF-8 字节。
最后必须有合法 Phase/Reviewer 和自动或人工最终 Gate，不能空图、绕 Gate 或重复定义同一项。
导入错误返回 issues，不保存半成品。成功仍是 draft，既不批准也不运行。

仅导入 open 且尚无计划契约的 Goal。同键同文档重放返回原方案；变更文档复用键会冲突。
不是已批准计划的覆盖入口；既有版本用讨论/replan 或批准内过程调整。
本地导入不需要 Pi/模型配置，不触发 Planner 调用。

## 审查、批准和前台执行

~~~powershell
uv run ehai @Local approve-plan --idempotency-key approve-001 --plan-revision-id '<plan-id>' --completion-contract-id '<contract-id>'
uv run ehai @Local execute-plan --idempotency-key execute-001 --plan-revision-id '<plan-id>' --execution-config C:/private/execution.json --authorize
uv run ehai @Local resume-session --run-id '<run-id>'
~~~

执行文件指定 worker_kind=pi、model、reasoning_effort、workspace、capacity、allowed_commands、
available_shells、git_permissions、command_timeout_seconds、pi 后端配置。
权限不因工作区可读而自动扩为写入、Shell 或 Git 写。前台文件允许解析器提供缺省值。

execute-plan 是前台宿主；Ctrl+C 尝试收敛后保存恢复事实，不保证强杀或未知副作用可自动重放。
resume-session 读取原授权，不是用今天的配置替换旧模型/权限。查询用 get-run-plan、
get-trace、get-result；候选 Artifact、Reviewer 结论和最终 Gate 是不同事实。

## API 宿主与 CLI 客户端

先运行宿主，按真实模型和私有路径替换以下示例：

~~~powershell
$Server = @('--database','C:/private/ehai/state.sqlite','--artifacts','C:/private/ehai/artifacts',
  '--worker','pi','--planner','pi','--pi-config','C:/private/backend.json',
  '--planner-model','<model-id>','--agent-model','<model-id>',
  '--worker-workspace','C:/work/project','--worker-capacity','2','--p2-runtime')
uv run ehai-api @Server
~~~

宿主拥有运行和 Pi 子进程。另一个终端的客户端不读取本地数据库、不启动另一套 Scheduler：

~~~powershell
$Api = @('--api-url','http://127.0.0.1:8000')
uv run ehai @Api get-runtime-health
uv run ehai @Api get-worker-profiles
uv run ehai @Api create-project --name 'My project' --idempotency-key project-001
uv run ehai @Api create-goal --project-id '<project-id>' --objective '<goal>' --idempotency-key goal-001
uv run ehai @Api import-plan --goal-id '<goal-id>' --file C:/private/plan.json --idempotency-key import-001
uv run ehai @Api get-plan --plan-revision-id '<plan-id>'
uv run ehai @Api approve-plan --plan-revision-id '<plan-id>' --completion-contract-id '<contract-id>' --idempotency-key approve-001
uv run ehai @Api start-run --plan-revision-id '<plan-id>' --execution-config C:/private/execution-api.json --authorize --idempotency-key start-001
~~~

API 模式也可用 discuss-plan/propose-plan。每一步先确认退出码和结果，再使用实际返回 ID。
后台 start-run 要完整 HTTP ExecutionConfigRequest 对象，路径在宿主解析；不是前台简写文件。
可复制 [执行配置示例](../examples/execution-api.json)，修改 model、workspace 和 pi 路径/Provider。
该示例与 README 的宿主启动参数配套；调整命令、并发、推理档位或超时也须同步修改两端。
endpoint_capabilities 是现有配置契约保留的兼容字段，示例沿用宿主默认值；不代表 Pi
实际请求支持这些 Responses 特性，也不会使模型工具输出 uniqueItems。Pi 协议由原生配置决定。
必须与宿主模型、权限、workspace、capacity、Pi 配置等一致，否则 409。格式见
[OpenAPI](../schemas/v1/http-api.openapi.json)。API 客户端不接受本地 --database/--pi-config/模型参数混用。
API URL 可为 origin 或 /api/v1；不接受内嵌凭证。成功 stdout 是 data 中的 JSON；
HTTP 错误 stderr 为 {http_status,response}，退出 2，不重试、不跟随重定向、不回退本地。
--api-timeout-seconds 是可选 socket 超时，不是任务期限。Ctrl+C 退出 130，不取消宿主任务。

## 控制、人工回路与过程调整

- pause-run / resume-run / cancel-run：--run-id、--idempotency-key；由持有执行的 API 宿主收敛。
- get-run-interventions → reply-intervention：--intervention-id、--request-token、--actor、
  --message、--idempotency-key。只解除对应人工问题，不批准新需求/权限。
- get-run-checks → decide-human-check：--check-run-id、--request-token、--passed 或 --rejected、
  --actor、--comment、--idempotency-key。
- propose-process --run-id --reason；review-process --draft-id；apply-process --review-id；
  写入均需幂等键。API 提案/审查为异步受理，get-process-draft/get-process-review 查询真实状态。
  不改变需求、对外接口、Gate、权限；不等于跨批准后继 Run 已交付。
- get-attempt-runtime / get-worker-requests：--attempt-id。
- resolve-worker-request：--worker-request-id、--resolution-file JSON 对象、--idempotency-key；
  decline-worker-request 使用同目标和幂等键。
- cancel-attempt：--attempt-id、--idempotency-key。取消不是人工 suspend。
- extend-attempt-deadline：--attempt-id、--deadline-at（含时区）、--idempotency-key。

运行中控制新增项要求 --api-url；本地前台执行尚无跨进程控制通道。
get-run、get-run-plan、get-run-checks、get-run-adoptions、get-trace、get-result 等保留。
恢复/探查的 execute-plan、resume-session、restore-run、recover、inspect-agent 不映射成远程重置。

## 周期轨迹审查和定向挂起

新 Pi execution_config 的 trajectory_review={} 启用默认 600 秒或 30 步先到触发，
可指定 interval_seconds、step_count、model；默认审查模型 gpt-5.6-luna/high。
宿主启动加 --trajectory-review 及对应间隔参数，授权配置须匹配；旧 Run 不自动启用。

一步是一轮有效模型响应及其工具批次完成，不是流式片段或工具数；无新增轨迹跳过。
审查只读取授权范围和轨迹快照，不读动态工作区，不自动 steer/挂起/改图。
get-run-trajectory-reviews 提供意见和覆盖序号。

显式采纳：suspend-attempt --attempt-id --review-id --through-sequence --actor --reason --idempotency-key。
序号取实际审查值，不是默认步数。目标/会话/批准或过程版本变化会拒绝。
控制结果 requested / suspended / not_suspended 不等于 Run 状态；还须看节点、Attempt 与 intervention。
独立节点可继续；人工回复前不自动恢复。脏工作区只作证据，不伪装成已确认 handoff。

## Block 变更清单

block 是计划图中的任务节点，不是代码 diff hunk。通过已有入口读取：

~~~powershell
uv run ehai --api-url http://127.0.0.1:8000 get-process-draft --draft-id <draft-id>
uv run ehai --api-url http://127.0.0.1:8000 get-process-revision --process-revision-id <revision-id>
~~~

前者的 candidate.block_changes、后者的 block_changes 给出同一份宿主生成的清单；
本地 CLI、HTTP 和对应 MCP 查询共用该投影。规划尚未完成时 candidate 为 null。

| 字段 | 含义 |
| --- | --- |
| block_id | 逻辑任务标识，沿保留旧节点键的修改连续追踪 |
| version | 初始为 1；执行身份变化时递增，未变或删除时保留原版本 |
| previous_node_id / node_id | 本次调整前/后的执行节点 ID；新增无前者，删除无后者 |
| change | added、modified、removed、unchanged；这是变更类型，不是执行状态 |
| changed_fields | 修改的定义字段、input_scope，或仅重置执行身份的 execution_identity |

新增 Run 的基线列出全部 added。过程编译器使用草稿中的旧节点 UUID 键追踪修改；
改用全新键就是删除旧 block 并新增 block，不根据标题猜测对应关系。
拆分/合并时可保留其中一个旧键，其余部分明确新增/删除，不声称多对一成果自动继承。
缺少历史清单的旧版本返回 block_changes=null；首次新调整从该旧版本节点建立追踪起点，
不补造更早的版本关系。清单在草稿生成时冻结，旧记录不随之后的执行状态改变。

例如 A → B → C，另有独立 D：修改 B 会保留 B 的 block_id 并递增版本，
C 因输入变化也获得新版本/节点身份；A、D 保持身份。删除 B 则列出 removed，
所有受到新输入影响的下游仍由现有编译器重置。通过这些节点 ID 关联 get-trace 中的
Attempt 和 Artifact；也可用 HTTP GET /api/v1/runs/{run_id}/artifacts 查询成果。
代码成果内容中的 base_commit/commit
描述 Run 基线和结果快照；结果可能包含上游改动，不能将其当作单 block 的独立补丁。

清单只说明变化，不判定批准范围或允许复用。unchanged 仍需现有独立审查核对成果，
modified/removed 的历史记录仍保留。应用仍需 review-process → apply-process，
批准需求、接口、Gate、权限发生变化时不能通过清单绕过重新批准。
清单覆盖节点定义及其输入，不替代整个方案文档、Phase 等其他结构的差异审查。
Git 整合使用下述入口；不把清单直接转换成 revert/cherry-pick。

## Git 自动整合

在拥有该代码工作区的 API 宿主上运行：

~~~powershell
uv run ehai --api-url http://127.0.0.1:8000 integrate-run --run-id <run-id> --expected-process-revision-id <current-process-id>
~~~

对应 POST /api/v1/runs/{run_id}/integrate；请求体只有 expected_process_revision_id。
MCP 名称为 integrate_run，启动即可调用。调用后宿主自动选择当前图中已完成且
所属分支已选定的代码成果，从 Run 固定 Git 基线合并；Run 必须 paused/completed 且
没有未结束的 Attempt。被删除、重置、未完成、未选中分支的成果不会选入。
输入基线引用了未选中或已被替代成果时拒绝整合，要求重新执行受影响节点。

返回 sources 列出 block、生产 Attempt、prepared_commit、commit；旧 block 版本为 null。
同一过程及成果集合对应同一 integration_id，重试继续原合并记录，不需要额外幂等键。
integrated 返回独立工作区、commit、完整 diff；conflicted 返回冲突路径和保留工作区，
commit/diff_path 为 null。可显式处理并暂存冲突后重试，宿主不自动改写冲突内容。
整合成功只代表代码可物化，不改变 Gate/Run 状态，不自动提交到用户分支或推送。

## 跨批准后继 Run

先暂停并排空旧 Run，再通过 replan-plan 生成其直接后继方案。重新批准后，用原 start-run
入口显式指定 predecessor_run_id；不再把旧 Run 的批准、完成状态或 Gate 结果复制过去。
当前支持同一 Goal、同一代码宿主的 paused 前驱；已 satisfied 的 Goal 仍需另建 Goal。

~~~powershell
uv run ehai --api-url http://127.0.0.1:8000 approve-plan --idempotency-key approve-v2 --plan-revision-id <new-plan-id> --completion-contract-id <new-contract-id> --supersession-file supersession.json
uv run ehai --api-url http://127.0.0.1:8000 start-run --idempotency-key start-v2 --plan-revision-id <new-plan-id> --execution-config execution.json --authorize --predecessor-run-id <old-run-id> --result-adoptions-file adoptions.json
~~~

supersession.json 对应 HTTP ApprovePlanRequest 的 supersession 对象：

~~~json
{
  "predecessor_run_id": "<old-run-id>",
  "expected_process_revision_id": "<old-current-process-id>",
  "actor": "<user-or-authorized-operator>",
  "reason": "<why this new plan replaces the old approval>",
  "resolutions": [
    {"kind":"human_check","request_id":"<check-run-id>","request_token":"<current-token>","disposition":"superseded","reason":"<why this old question is withdrawn under the new plan>"}
  ]
}
~~~

kind 为 human_check 或 intervention；分别从 get-run-checks、get-run-interventions 取 ID/token。
必须覆盖全部未决事项且 token 精确匹配。resolved 表达操作者已处理该问题并提供说明；
superseded 表达新批准明确撤销旧问题，不表达旧 Gate 通过。external_effects 类型只能 resolved，
需要先核对真实副作用并解释安全继续的依据。框架记录操作者决定，不假装代码证明了外部事实。
没有未决事项时 resolutions=[]，也可以不提供 supersession 文件；显式提供可固定来源过程版本。

adoptions.json 是数组，对应 HTTP StartRunRequest 的 result_adoptions：

~~~json
[{"source_plan_node_id":"<old-completed-node>","target_plan_node_id":"<new-work-or-merge-node>","reason":"<why this exact old result fits the newly approved task>"}]
~~~

只声明来源/目标当前执行节点及复用理由，宿主查出真实 Attempt、Artifact 和哈希并验证字节。
省略文件或传 [] 表示全部重新执行，不自动猜测相似任务。来源必须已完成、最新 Attempt 成功；
不能用旧成功覆盖后来的中断/失败。映射为一对一，不接续 Reviewer/Gate 的完成状态。
Run、接续记录、授权、派发意图和幂等回执在同一事务保存；任一验证失败整体回滚。

输入依赖与 Git 基线仍适用时，宿主接纳旧成果并重新运行目标 Gate；输入/基线变化则按新图
派发 Worker 重做。旧生产者身份和字节不改写。沿用已有 Goal Worker Attempt 预算，换 Run
不清零，Token/费用硬限额仍暂缓。新 Run 的 get-trace 中 RunSuccessorCreated 保存来源、
操作者处置及接续记录，get-run-adoptions 可查询实际映射；Worker 上下文也获得这些来源事实。

旧 Run 保持 paused 历史状态，原未决记录保留原貌；处置事实记录在新 Run，不能向旧 Gate
回填成功。get-run 的 predecessor_run_id / successor_run_ids 连接两侧，旧 Run 不可再恢复执行。
新 Run 在新人工 Gate 等待时可能仍是 running，须结合节点和 Check 状态查看；不是自动完成。

## 草稿 Gate 工具

set_node_gate/set_final_gate 完整替换条件；[] 清命令、null 清人工条件，两者不能同时为空。
remove_node_gate/remove_final_gate 是明确删除，保留图结构；必需 Gate 仍由 finish_plan 校验。
NODE_GATE_FINAL_CONFLICT 返回节点键与修复入口。过程模式不暴露这些删除/设置工具。
这些是 Planner 工具，不是修改持久批准的 CLI 命令。

## 独立 MCP 服务端

先启动同一个 ehai-api，再由外部 Agent 的 MCP 客户端启动：

~~~powershell
uv run ehai-mcp --api-url http://127.0.0.1:8000
~~~

使用官方 Python MCP SDK 1.x，uv.lock 固定实际版本；[上游维护分支](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
本轮支持 stdio，不另开 MCP HTTP 监听、不启动 EHAI 服务。协议 stdout 专用，诊断在 stderr。
启动即列出全部已支持的查询和写工具，不设 MCP 读写开关。
工具可调用不等于方案已批准或执行已授权；这些业务校验仍由 HTTP 宿主负责。

工具名对应 CLI 名称的下划线形式。GET 工具接收路径 ID；写工具接收路径 ID 和 request_json，
后者是原 HTTP JSON 请求体字符串，不是文件路径。通过 get_request_schema(tool_name)
按需读取该宿主的请求 Schema，避免在每轮模型输入重复嵌入复杂配置。HTTP 仍做完整业务校验。
例如 create_project 的 request_json 是 {"idempotency_key":"project-001","name":"My project"} 的 JSON 字符串。

启动时检查宿主 OpenAPI 对应路由；不兼容版本拒绝启动。结果有文本和 structuredContent.result，
失败 isError=true 且保留 HTTP 错误；取消 MCP 等待不保证取消已发送的 HTTP 写入，不自动重发。
长任务使用受理 ID 后续查询。当前不提供 sampling、资源写入、任意 URL 代理或模型自主批准。

通用 stdio 客户端可配置 command=uv，args 为
["run","--directory","C:/work/EHAI","ehai-mcp","--api-url","http://127.0.0.1:8000"]；
旧配置中的 --allow-writes 参数需移除。各外部产品的配置文件格式遵循其自身文档。

当前 API 无新增认证层，默认仅回环使用，不直接暴露公网。模型 key 留在宿主，不传给 MCP 客户端。
静态检查和正常入口试用范围见 [STATUS](STATUS.md)。
