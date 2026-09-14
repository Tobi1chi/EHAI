# Pi 迁移前用法与历史验证记录

本文保留迁移前的完整内容及未提交的实施记录，不是当前命令参考。文中的 Built-in、
Responses 参数、旧 Runtime 与其试用结论只适用于当时版本，不能套用于 Pi。
当前入口见 [Usage](USAGE.md)，删除与验证边界见 [Pi 迁移记录](PI_BACKEND_MIGRATION.md)。

Pi 后端迁移已开始。新增 `inspect-agent --node <Node> --pi-cli <Pi CLI 文件>` 可在无模型调用、
无任务写入的隔离进程中检查控制通道；仍需提供 CLI 全局 `--database` / `--artifacts` 参数，
但检查不会访问它们。安装与完整示例见 [Pi 后端迁移记录](PI_BACKEND_MIGRATION.md)。
这不是 `--worker pi`，现有执行、讨论与恢复入口尚未切换到 Pi。

本文档记录 P1/P1.1 兼容命令与 P2 稳定执行面的本地运行方式。普通 `ehai` 命令保持同步兼容；
`ehai-api --p2-runtime` 使用持久 dispatch work 和后台 Runtime，使 `StartRun` 快速返回并通过
Query/SSE 观察进展。

## 实现与目标的边界

本页是当前入口参考，不是目标产品说明。目标流程与职责见 [Product Scope](PRODUCT_SCOPE.md)，
实施状态见 [P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md)。
现有命令已接入规划讨论、草稿修订、批准及查询；仍不能据此宣称需求相关最终验收以及
“执行阻塞 → 用户回复 → 继续”的完整产品流程已完成。真实模型交互与最终 E2E 另行验证。
本页不为尚未实现的交互编造命令；下列演示中固定计划或产物的成功只证明其对应协议。
2026-09-06 的 [Execution Model](EXECUTION_MODEL.md) 已确认 Worker 预设实例化、阶段/分支 Gate、
人工 Gate、阶段 Review、过程自主调整和阶段 Session。当前已开放中间工作/整合节点的自动 Gate，
并提供人工 Check 决定、过程草案/审查/应用和 Built-in 逻辑阶段共同会话入口。
过程自动触发、跨批准版本接手、Server 共同会话及真实产品验收仍未完成。
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
    get-run-plan --run-id $RunId
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-trace --run-id $RunId
```

数据库必须已经存在。`get-plan` 返回方案版本、批准状态、节点指令、依赖和分支；`get-plan-checks`
返回该版本持久化的检查配置；`get-trace` 返回运行、Attempt、产物、事件及有界 Built-in Session 轨迹。
`get-run-plan` 返回该 Run 当前执行图及节点/分支状态，HTTP 对应 `GET /api/v1/runs/{run_id}/plan`，
生成 Client 方法为 `getRunPlan`。新 Run 的执行推进不再覆盖批准方案：例如工作完成后，`get-plan`
中的原节点仍为批准时的 `pending`，`get-run-plan` 中对应节点为 `completed`。

数据库 Schema 12 为每个 Run 单独保存执行图。旧数据库升级时复制现存图状态，不删除原计划、
Attempt 或 Checkpoint；旧方案视图保留升级前已有的状态，不伪造当年批准时的节点状态。
当前图结构仍不能任意改写：过程版本发布与批准底线校验尚未接通，新增查询不代表自主调整已交付。

Schema 13 为 Run 创建不可变的初始过程版本。`get-run-plan` 的 `process_revision_id` 指向当前
过程快照，可用 `get-process-revision --process-revision-id <ID>` 查询；HTTP 对应
`GET /api/v1/process-revisions/{process_revision_id}`，Client 为 `getProcessRevision`。
其中 `graph` 是创建过程版本时冻结的图，节点状态不会随执行更新；实时状态仍读 `get-run-plan`。
图内的 `version` 仍是原 PlanRevision 版本，外层过程的 `version` 才是过程序号。

新 Attempt 和 Checkpoint 在 `get-trace` 中携带 `process_revision_id`；Check、Artifact、handoff
等可通过所属 Attempt 追溯。旧记录无身份时返回 null，不猜测它属于迁移后的过程。迁移创建的
首版本标记为 `legacy_snapshot`，新 Run 的初始版本标记为 `run_started`；后者指 StartRun 创建
的执行基线，不表示 Worker 已运行完成。`get-plan` 的过程 ID 为 null，它仍是批准方案查询。
后继过程草案须经独立审查后才能应用，具体入口见下文。Checkpoint 恢复不会静默跨过程；历史无版本 Checkpoint 仅在
迁移首版本、原图结构仍一致时保持原恢复能力。

Schema 14 将原方案的完整节点、边与分支成员清单写入方案快照；方案查询不再根据实体表当前归属
重新拼装。`get-process-revision` 还返回 `gate_owners`，键为原批准 Gate 的节点 ID，值为该过程
中的 Gate 承载节点 ID；初始版本为身份映射。`planner_adjustment` 已是底层存储支持的后继来源，
可由下文 `apply-process` 在审查和运行状态检查后应用；枚举或命令存在不代表已完成产品验收。

`design_document` 保存该版本的可读方案，旧版本可能为 null。多项检查仍使用现有检查器，
不是任意需求到执行检查的自动转换；人工 Check 的请求和决定通过下文的公开入口处理。

## 生成与查看过程草案

`propose-process` 为 running/paused Run 生成过程调整草案，不改写其当前执行图、不应用修改，
也不重新批准契约。生成目前需要 Built-in Planner；调查范围仍由 `--worker-workspace` 指定，
应指向该任务允许调查的仓库，并先配置有权限的模型和凭证。

```powershell
$Draft = (uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    --planner builtin --builtin-planner-model "<model-id>" --worker-workspace . `
    propose-process --idempotency-key process-draft-1 --run-id $RunId `
    --reason "说明为什么需要调整实现任务、依赖或路线" | ConvertFrom-Json)
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-process-draft --draft-id $Draft.draft_id
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-run-process-drafts --run-id $RunId
```

命令返回 `draft_id` 和 `status`，查询返回基准执行图、父过程 ID、生成原因、候选图和错误信息。
Schema 15 单独保存草案；多个草案可引用同一父过程，它们不会占用已应用过程的版本号。

- `planning`：请求已保存，结果尚未确定。进程强制退出后可能保留此状态，不等于模型仍在运行。
- `ready`：候选已生成，**不是已审查、已批准或已应用**。
- `failed`：生成失败，`error` 给出有界诊断；正常生成失败也返回该状态，不能只按 HTTP 200 或 CLI 退出码判断成功。

同一幂等键重复调用只读回原草案，包含 planning/failed 状态，不重复模型调用；新的生成请求使用
新的键。`planner_session_ref_id` 在请求保存时预分配，用于关联模型轨迹，失败可能发生在对应
Session 实际创建之前。`base_is_current` 只表示父过程和基准执行图仍匹配且 Run 未终止，不代表
满足应用条件。Run 在生成期间继续推进时，旧草案可保存但该标志可能为 false。

候选中的 `graph.status`、`approved_at` 和契约 ID 仍指向原批准；应用前，候选过程 ID 尚未进入
`get-process-revision` 的已应用版本历史。过期草案不能直接应用，需要基于当前状态重新生成。

HTTP 对应 `POST /api/v1/commands/propose-process`（请求为 `idempotency_key`、`run_id`、`reason`）、
`GET /api/v1/process-drafts/{draft_id}`、`GET /api/v1/runs/{run_id}/process-drafts`；均使用正常 `data`
响应封装。生成 Client 方法为 `proposeProcess`、`getProcessDraft`、`getRunProcessDrafts`。

## 审查与应用过程草案

为避免生成期间执行图继续变化，建议先通过正常暂停入口停止在途工作，再生成草案。`review-process`
只接受 ready 且基准仍匹配的草案，使用配置的 Built-in Planner 模型创建独立 Reviewer 会话；
它不复用 Planner 的讨论历史，不提供图编辑、代码写入或应用工具。这是独立会话和职责，不表示
自动选择了另一个模型。

```powershell
$Review = (uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    --planner builtin --builtin-planner-model "<model-id>" --worker-workspace . `
    review-process --idempotency-key process-review-1 --draft-id $Draft.draft_id | ConvertFrom-Json)
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-process-review --review-id $Review.review_id
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-process-draft-reviews --draft-id $Draft.draft_id
```

审查状态为 `reviewing`、`completed`、`failed`；completed 只代表有审查结果，还须查看
`preserves_boundary` 和完整 report。报告分别评估需求、接口、权限、Gate 作用范围与成果复用，
判断为 `preserved`、`not_preserved` 或 `uncertain`。原材料引文用于定位依据，不是语义正确的
机械证明；已有成果通过受限 Artifact 读取核对字节摘要和来源，当前 checkout 不替代历史成果。

保持范围的报告必须给出原义务到候选实现的映射：每组任务为 AND，多组替代实现为 OR；每条
到达对应 Gate 检查的可行路线均须覆盖其义务。WORK/MERGE 自身在检查前产生的候选可以满足
义务，Reviewer 报告不能单独替代实现。原 Gate 条件、分组、阶段边界与前置约束仍须保持。

仅当审查 completed 且 preserves_boundary 为 true 时，可以显式应用：

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    apply-process --idempotency-key process-apply-1 --review-id $Review.review_id
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-run-plan --run-id $RunId
```

应用不会调用模型或重新批准契约。它重新核对草案基准、引用证据和义务映射，并要求 Run 已 paused、
没有排队/运行中的 Attempt，且不会丢下未决候选、人工 Check 或阻塞节点。成功时，过程快照、
活动版本指针、`ProcessRevisionApplied` 事件和幂等回执在同一事务保存；Run 保持 paused。
编码前台任务随后通过 `resume-session` 按原运行配置继续；旧 Checkpoint 不会跨版本静默恢复。

草案以请求事件位置固定当时的 Worker 问题与用户回复，供 Planner 和独立 Reviewer 使用；
请求后问题或回复发生变化，旧草案不能继续审查或应用，需要重新生成。应用后的 Worker 也会
直接收到这份历史上下文，保留原节点、Attempt 和过程版本来源，即使任务已被替换。
这些回复是原批准范围内的继续执行信息，不是新权限、Gate 批准或未决外部副作用已经解决的证明。
恢复执行保留全部旧 Attempt 审计记录，但不会再按已移除的旧任务查找当前节点。
当前尚无可核验的外部副作用影响范围记录；存在未回复的 `external_effects` 便签时，
过程审查可以保存不确定/不保持范围的报告，但不能给出可应用的全部通过结论。
需先通过正常便签入口明确回复，再生成包含该回复的新草案；这不暂停原方案中独立且不受影响的路径，
也不把回复当作副作用已回滚的证据。

同一审查/应用幂等键只读回原结果，不重复模型调用或重新激活旧版本。失败和不确定报告不能应用；
越过原批准需求、接口、Gate 或授权应走明确方案修订，而不是修改报告为通过。

HTTP 对应 `POST /api/v1/commands/review-process`、`POST /api/v1/commands/apply-process`、
`GET /api/v1/process-reviews/{review_id}`、`GET /api/v1/process-drafts/{draft_id}/reviews`。
Client 方法为 `reviewProcess`、`applyProcess`、`getProcessReview`、`getProcessDraftReviews`。
HTTP 的过程草案与审查使用原生异步 Planner 入口；当前 Built-in Planner 支持它，只有同步过程方法的
自定义 Planner 仍可走同步应用/CLI 入口，但不支持这两个 HTTP 模型调用入口。异步取消会记录请求失败
并传播取消；幂等键重放不重发模型请求，也不代表远端未知结果已确认取消。
除无模型的拒绝/查询路径外，2026-09-13 已通过一条隔离示例的真实正向审查、应用后同 Run 续跑和
原自动 Gate 完成路径，保留了已完成代码和同一逻辑阶段会话；证据见 R2 实施记录。
这不代表过程自动触发、全部调整/恢复场景或唯一产品 E2E 已验收。

2026-09-13 另已完成 CLI 显式授权启动后，经 HTTP 原生异步过程草案、独立审查、应用、恢复到原
行为 Gate 通过的隔离试用。恢复现在在同一事务中重新排队原 dispatch；API 暂停释放已收敛的宿主
占用，后台不会立刻重新领取静止的 paused Run。已处理的暂停/取消 key 重放在收敛前返回已有结果，
不应打断后来恢复的执行。该试用没有验证 `/runs/start` 与 CLI 执行配置记录完全等价，也不是自动触发验收。

暂停事件现在在 `RunPaused.payload.pause_cause` 记录内部来源：`operator`、`worker_waiting`、
`runtime_idle`、`runtime_error`、`startup_recovery`、`unknown_execution`、`branch_exhausted`、
`retry_exhausted`、`review_rework`。通过现有执行轨迹/事件查询读取，不是新增 Run 状态或可提交的
暂停命令参数。历史事件没有该字段时按未知处理，不从 reason 文本反推来源。
显式暂停已暂停的 Run 仍记录用户停止事实；相同幂等键重放不会重复记录。前台 Ctrl+C 属于用户停止，
内部等待/收敛不再生成伪装的用户暂停命令。此来源记录尚未接入自动 Planner/恢复循环，
也不说明预算耗尽、未知执行结果或人工等待已获自动续跑许可。

## 与 Planner 讨论并修订方案

配置了自动过程调整的前台 Run，在 Reviewer Gate 拒绝后可用 `resume-session` 接续。
入口将协调器发现的待处理暂停交给现有过程草案、独立审查、应用和恢复流程，不先把失败的
Reviewer 节点当作普通 Attempt 重试。原 Gate 失败、已完成成果及暂停事实保持，直到正常调整
获准应用；没有待处理调整时仍走原恢复校验，不绕过人工判定或过程调整预算。

讨论使用 Built-in Planner，先配置有权限的模型和正常凭证；只读调查范围由 `--worker-workspace` 指定。
下面沿用已创建的 `$GoalId`。每条新消息使用新的幂等键，首次不传 conversation ID：
示例的非空产物条件只用于演示规划操作，不是编码任务的充分验收条件。

```powershell
$Planning = @(
    '--database', '.ehai/state.sqlite3',
    '--artifacts', '.ehai/artifacts',
    '--worker-workspace', (Get-Location).Path,
    '--planner', 'builtin',
    '--builtin-planner-model', 'gpt-6-astra',
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
明确批准返回版本，讨论不会自动批准或启动 Run。普通讨论不允许 Goal 有活跃 Run；已收敛暂停的
来源 Run 可使用下文的 `--source-run-id` 进入待审讨论，不切换原生效契约，也不解决未决人工请求。
重复成功消息键只读取已有结果，不重复调用模型。

缺少配置或 Provider 失败会留下可查询的 failed turn；重复该消息键不会重新调用模型，应查看错误后用
新键继续。若进程中断留下 running/未知结果，同一讨论暂不继续；需先调查原执行，必要时显式开新讨论，
不宣称此入口已经实现自动模型执行恢复。

新讨论轮次在调用 Planner 前持久化宿主分配的 `agent_session_ref_id`；查询 running/failed turn
也可取得该关联，完成结果必须返回同一 Session。该 ID 表示预分配归属，不保证模型已开始调用；
实际调用需核对对应 Session 事件。旧记录继续读取原完成事件中的 ID，不补造失败会话历史。

HTTP 使用相同能力：

- `POST /api/v1/planning/discuss`：`idempotency_key`、`goal_id`、`message`，可选 `criteria` 和
  `conversation_id`；省略 `criteria` 表示未指定完成条件。
- `GET /api/v1/planning/{conversation_id}`：查询讨论；TypeScript Client 提供对应生成方法。

### 多项完成条件

成果接手的只读入口为 `get-run-adoptions --run-id <目标Run>`，HTTP 对应
`GET /api/v1/runs/{run_id}/adoptions`，生成 Client 方法为 `listResultAdoptions(runId)`。
返回目标 Run 的不可变接手记录，包括来源 Run/过程/Attempt、目标节点及成果哈希；无接手记录
返回空列表。不存在的 Run 报未找到，不与空列表混淆。该查询不创建后继 Run、不批准接手，
也不启动 Worker；后继执行的审批和调度入口仍在接线中。

`propose-plan`、`replan-plan`、`discuss-plan` 的 `--criterion` 可以重复，最多组合三种自动检查；
也可使用 `human:<question>` 声明一个非空人工条件。编码讨论 `discuss-plan` 可以省略该参数，表示
未指定条件；此时 Built-in Planner 必须提出非空的 command 或 human 最终 Gate，不会用
`artifact:non-empty` 占位。`propose-plan` 和 `replan-plan` 仍要求显式条件。
`command:exit-zero` 的 argv 可由宿主通过 `--command-check-argv` 显式提供，或由 Built-in Planner
通过 `set_final_gate` 提议后随方案审查批准；持久化前必须有实际 argv。`semantic:required-terms` 必须
配置 `--semantic-required-term`。Planner 不能覆盖显式宿主 argv 或减少已传入的条件。
条件变化随新契约版本再次批准。Plan 查询同时返回显式 Phase、阶段 Reviewer 和对应 Gate 节点；
人工 Gate 决定使用与自动 Check 相同的 CheckRun 事实和幂等语义。

### 人工 Check 决定

当运行进入人工 Check 时，先用只读命令查看该 Run 的 CheckRun 和 `human_request` 快照：

```powershell
$Checks = uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-run-checks --run-id $RunId | ConvertFrom-Json
```

请求中的 `request_token` 绑定获批方案、待审问题和 Artifact 版本。决定必须明确选择
`--passed` 或 `--rejected`，并提供 `--actor` 与 `--comment`；重复幂等键只能重放同一决定：

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    decide-human-check --idempotency-key human-check-1 `
    --check-run-id $CheckRunId --request-token $RequestToken `
    --passed --actor operator@example.com --comment '已核对请求中的 Artifact 版本。'
```

该决定只完成对应人工 Check，不代表整个 Run 已完成。若返回的 Run 仍为 `running`，前台 CLI
可用 `resume-session --run-id $RunId` 继续当前执行；不会替用户批准、暂停或改变方案边界。
HTTP 使用同一语义：`POST /api/v1/check-runs/{check_run_id}/decision`，请求体省略路径中的
`check_run_id`，字段与上述命令一致。

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

P1/P1.1 的公开入口最多组合三种自动检查及人工条件；`single`、`exploration`、`codex` 和 `builtin` Planner 都通过同一个
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

默认请求优先使用持久化的 `previous_response_id`。兼容端点若明确拒绝该字段，Adapter 会省略该句柄并从
durable Session Event 重放完整 provider output items 和 function call/result；也可显式禁用续接，
从第一轮就发送本地保留的完整上下文，不再每轮先失败再回退。请求包含
`include=["reasoning.encrypted_content"]`，保留并按原顺序回传端点返回的不透明推理项，不解密或
展示其正文；不得只重建可见文本/函数调用而丢弃它们。旧日志中已丢失的项不能恢复，显式工作摘要
边界之后使用新摘要与后续输出继续。`store=true`、streaming 和严格 Tool Schema 保持不变。

用户测试中转端点的逐项结果见
[aws-sub2 Responses 能力实测](spikes/aws-sub2-responses-capabilities.md)。
用户确认该端点经 Sub2API 反代；其当前 HTTP 账号/路由拒绝续接，Response 查询、幂等创建和非流式
响应也不能按完整兼容能力使用。不要据此硬编码所有 Sub2API 部署的行为。

2026-09-08 起，规划 CLI 和 `ehai-api` 启动参数均支持
`--no-responses-previous-response-id` 与 `--no-responses-response-retrieval`；
该测试端点同时使用 `--no-responses-background`、`--no-responses-unique-items`、
`--no-responses-idempotent-create`。这些是 `discuss-plan` 子命令之前的全局参数。
`execute-plan` / `resume-session` 则使用下文获授权、随 Run 持久化的执行配置。

禁用查询后，丢失终态的响应不能通过 retrieve 恢复；未保证幂等创建时也不重新 POST。
Built-in Worker 会进入等待，前台暂停并返回 `provider_outcome_unknown` notice，
用户查看证据和外部状态后再决定是否显式恢复；不自动把未知结果当成可重试失败。
这不改变需求、Gate 或已有 Session 历史，也不是跨阶段共享 Session 的实现。

`get-trace` 的 `session_events` 已包含 `model/transport`：关联逻辑请求 ID、每次 HTTP ID、
操作、状态码、服务端 request ID、流式终态、回退/错误和耗时。
正常内置客户端禁用 SDK 隐藏重试；若自行注入由 SDK 管理重试的客户端，记录的 SDK 调用
不保证覆盖其内部每次 HTTP，事件中的 `retry_owner` 会区分该情况。
传输事件不作为模型上下文重放，不保存凭证、请求/响应正文或完整错误正文。
查询仍有事件数/载荷上限；`session_events_truncated=true` 时不是完整轨迹，完整事件保留在本地库。

不要把 `OPENAI_API_KEY` 写入命令历史、配置文件、Event、Artifact 或数据库。

### Built-in Planner 的当前边界

当前 propose 操作生成并持久化 draft PlanRevision，不批准计划、创建 Run 或启动 Worker。
生成后可通过 `get-plan` 和 `get-plan-checks` 查看；`discuss-plan` 已接入多轮讨论、详细方案与草稿修订，
代表性真实 CLI 试用已完成需求澄清、设计生成、审查修订和审批隔离，见 P2 实施文档；
这不等于 Worker 编码或最终 E2E 已通过。

## R2 前台编码会话

代表性 Built-in 真实 CLI 编码已通过：并行任务、双代码分支、选中成果整合和一个最终行为 Gate。
固定 Gate 失败后修复、Ctrl+C 后同一 Run 立即恢复且已完成上游不重跑，已通过真实模型受控试用。
Codex App Server 已通过真实 CLI 并行编码和 Ctrl+C 后立即恢复，保留已完成任务与中断代码。
任意关窗/强杀、文件写操作中途恢复和长时间运行仍需验证，不当成完整 P2 验收。
R2 使用 Git 仓库和 EHAI 拥有的隔离 worktree，不直接改写用户当前分支。运行基线固定为仓库的 Git HEAD；
开始前先提交希望纳入任务的源码，未提交的源文件修改不会自动纳入基线。

Planner 可通过 `set_final_gate` 提出 `command:exit-zero` 的具体 argv，随方案和契约批准。
编码讨论省略 `--criterion` 时不会隐式加入 `command:exit-zero`；模型必须通过 `set_final_gate` 提出
具体 command 或 human 条件。如果显式传入 `artifact:non-empty` 等旧条件，Planner 实际调用
`set_final_gate` 提出命令后，构建器保留旧条件并追加 `command:exit-zero`，不丢弃命令。
只在设计文档里描述检查仍不等于配置 Gate；批准前以实际 CheckSpec 为准。

Planner 可调用 `set_node_gate(node_key, name, argv)`，为已存在的中间 `work` / `merge` 节点设置
独立自动 Gate。它可用于上游阶段边界或某条探索路线，只检查该节点应交付的内容，不提前要求后续成果。
一个节点最多配置一个这样的 Gate，重复设置替换草稿配置；删除节点同步移除配置。
最终节点使用 `set_final_gate`，不能再叠加同节点局部 Gate；`fork` / `evaluator` 不接受局部命令 Gate。

`get-plan` 中的 `required_check_ids` 显示节点绑定，`get-plan-checks` 返回最终及局部检查。
CompletionContract 的 required IDs 只表示最终成果条件；局部 Check 仍是所属节点的必需 Gate，
不要求被剪枝的替代路线也满足最终条件。显式 `--command-check-argv` 只约束最终 Command Check，
不覆盖局部 Gate。所有命令均需随方案审查批准，Worker 仍不自动获得执行宿主命令的权限。

后继任务等待依赖节点通过自己的 Gate；无检查的中间交接不生成假 Gate。
这是多节点自动 Gate 基础；人工 Check 决定入口已接通，但仍不是完整 Phase 模型或阶段 Reviewer
闭环。
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
    "supports_idempotent_create": null,
    "supports_previous_response_id": true,
    "supports_response_retrieval": true
  },
  "command_timeout_seconds": 30
}
```

模型名按端点实际支持填写；可以与 Planner 的模型不同。兼容端点不支持 background 或 uniqueItems 时，
将相应配置改为 `false`。凭证仍由进程环境提供，不放入 JSON；当前模型 base URL 仍取自
`OPENAI_BASE_URL`，恢复前应使用同一提供方。
2026-09-06 实测的 aws-sub2 / Sub2API HTTP 账号路由应将上述五个 capability 字段均设为 `false`；
该建议不适用于所有兼容端点，具体证据与当前续接/查询适配的边界见
[端点专项记录](spikes/aws-sub2-responses-capabilities.md)。
旧配置省略两个新字段时保持 true 的历史行为；已有获授权 Run 的配置不会被静默替换。
禁用查询却启用实际 background 执行的配置会在模型请求前被拒绝。
`allowed_commands` 是允许的精确 argv 数组；`available_shells` 显式开放 Shell，Git 操作需对应
`git.read` / `git.local_write` / `git.remote_write` / `git.dangerous` 权限。
不需要命令时保持空数组。Shell 授权不是操作系统沙箱；全局删除黑名单及可撤销删除尚未实现。

### 可选自动过程调整（已接线，真实自动触发验收待完成）

首次执行授权前，可以在上述执行配置顶层加入以下对象；省略它不会启用自动 Planner 调用：

```json
"process_adjustment": {
  "max_per_goal": 5,
  "model": "<your-planner-model>",
  "reasoning_effort": "high"
}
```

三个键都必须提供；`reasoning_effort` 可以为 `null`。`max_per_goal` 是正整数，限制同 Goal 已开始
的自动调整次数，失败和取消也计数，不替代或重置 Worker 预算。前台宿主按此策略配置 Built-in
Planner；API 后台宿主必须配置一致的 Built-in Planner 模型和 effort，不能用宿主当前参数替换
Run 的历史授权。这项配置可通过 `execute-plan --execution-config ... --authorize` 保存。
`POST /api/v1/runs/start` 也已接入可选 `execution_config`：提供它表示明确确认该执行配置，
只接受与当前 Built-in/Server P2 宿主匹配的设置，不动态换模型或扩权；省略时保留旧启动语义。
HTTP 使用完整配置字段（`codex_server` 和 `process_adjustment` 可省略），无凭证字段；格式错误
返回 422，宿主不匹配或同幂等键更换配置返回 409。新授权与 Run 启动、调度项、receipt 同事务
保存，CLI 也复用此原子路径。新增 HTTP 首次授权仍待真实入口验收，不代表自动恢复已经通过。

当前仅接受有成功候选证据的 `branch_exhausted` / `review_rework` 系统暂停，保持原批准边界、
独立审查和同 Run 恢复。用户暂停、重试预算耗尽、未知执行结果及必要人工介入不会被自动覆盖。
已授权的旧 Run 不能事后添加策略；旧配置的序列化和授权指纹不变。轨迹中的
`ProcessAdjustmentStarted`、`ProcessAdjustmentFinished`、`ProcessAdjustmentSkipped` 记录尝试和结果。
此处描述已接入配置及代码语义，不表示真实自动闭环或自举产品验收已通过。

### 启动与恢复前台执行

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
- 以上版本限制描述首次执行；同一批准边界内的过程调整使用前述
  `propose-process` → `review-process` → `apply-process` 入口，应用后恢复同一 Run。
  未显式授权上述自动策略时仍由调用方触发，不通过修改数据库代替审查和应用。
- `resume-session` 使用持久配置，不隐式扩大工具权限；一个前台宿主独占其数据库内的活动 Run。
- 进展输出到 stderr，结果 JSON 输出到 stdout。使用 Ctrl+C 停止并等待进程退出，宿主收敛 Worker、
  保存进度并暂停，释放自己的调度租约；重开后可立即继续同一 Run，无需等待旧租约到期。
  已完成的上游任务不重跑，未完成的最终节点可创建后续 Attempt，沿用保留代码和原 Gate。
  Built-in 已验证最终审查/Gate 中断，Server 已验证编码 Worker 中断及已写代码快照保留。
  直接关窗、硬杀进程和文件写操作进行到一半时的恢复未验证，未知外部操作仍需核对。
  暂停持久化失败会报错，不能视作成功暂停。
- 整体 Built-in 会话不设总时长、总 Step、总工具调用或总输出字节上限。每 16 个完整步骤由同一模型
  作一次工作摘要，仅提供 `save_working_context` 结构化返回协议，不调用任务工具。该内部调用/结果
  与摘要 `step/end` 原子持久化成功后才替换旧模型/工具历史；原任务和收到
  的消息及最近 8 个完整任务步骤的模型/工具往返保留，避免刚读取的代码与验证输出只剩摘要；
  内部摘要步骤不进入该近期窗口，原始事件不裁剪。摘要是可能有误的工作笔记，不是批准、handoff 或 Gate 证据；失败时
  保留原历史和被拒绝响应并报错，不静默丢弃。摘要调用计入已有 Step/Tool/输出统计，不重置预算。当前按步骤触发，
  尚非 token 自适应压缩；单次调用、工具输出和授权边界仍受控。
- 完成的 worktree 保留；宿主捕获实际代码的 Git 提交与 `solution.patch`。`get-result` 返回成果位置、
  基线/候选 commit、diff Artifact 和最终 Attempt 的检查结果。失败历史保留，不误算为最终 Gate 仍失败。
- 最终 Gate 在整合后的真实 worktree 执行。失败时返回原始检查证据，在同一获批方案下修代码并重新检查；
  `report_blocked` 用于说明阻塞原因、证据和所需条件，不伪造成功。便签查询与回复见下文；
  完整人工问答和跨批准版本接手的产品验收尚未完成。

当前 `resume-session` 保留 Run，未完成任务仍可新建 Attempt/Thread。代码准备仅复用同节点已成功提交
Attempt 的宿主代码快照；没有该交接证据时使用已完成的有效上游或 Run 基线，不再从中断、取消、
超时尝试的脏快照自动续跑。已提交交接缺少代码快照时拒绝继续，不静默丢掉交接。
中断快照仍可作为调查材料保留。历史 Server 试用曾接续缺失文件的脏快照；那不是当前选择续跑基线的规则。
Built-in 还可使用下文宿主确认的独立 handoff 和逻辑阶段共同会话；
Server handoff 与外部副作用通用回滚尚未实现。

### Codex App Server 的接入边界

App Server Connector 只使用一个 Endpoint 对应一个 `codex app-server --listen stdio://` JSONL 连接。
前台执行配置可将 `worker_kind` 改为 `codex-server`，`model` 填写该端点支持的显式模型，
并配置 `"codex_server": {"executable": ["codex"], "approval_policy": "on-request",
"sandbox": "workspace-write"}`。默认启动本机 stdio App Server，而不是 `codex exec`。
也可从 `ehai-api --p2-runtime --worker codex-server` 进入同一装配。当前不承诺连接现有桌面进程或在桌面显示会话，
不以历史 Thread/Turn 验证代替本轮真实调用。

该 Connector 不使用 WebSocket、远程 listener、Review、Skills、Apps 或 Auth 登录接口，也不修改用户
全局 Codex 配置。

Thread 启动和恢复时，通过 `config/read` 读取目标 cwd 的配置，并对本 Thread 禁用继承的 MCP、
插件、hooks 和 web search，避免只有 workspace 授权的任务启动本机其他 Connector。恢复沿用原 Thread
的 cwd，不覆盖为目标仓库基线目录。原生命令仍由 Codex sandbox/approval 管理，不宣称 Built-in 的
`allowed_commands`、`available_shells` 就是 Codex 原生命令白名单。

Windows 子 Server 使用独立进程组，由 EHAI 接收 Ctrl+C 后发送 `turn/interrupt` 并关闭自有 Server，
不依靠广播中断碰巧结束子进程。旧故障记录在启动恢复中被收敛后，可能先返回 paused notice；
核对后再次 `resume-session`，会复用同一个调度项，不插入重复 Run 调度记录。

若独立 Codex 未登录，可在 `codex_server.executable` 中使用 `-c` 指定自定义 provider；
凭证通过该 provider 的 `env_key` 对应环境变量提供，不写入执行配置、argv 或仓库。
本轮真实试用使用 `gpt-5.5/high` Built-in Planner 和 `gpt-5.6-luna/high` Server Worker；
这是规划与执行的角色分工，不是同一 Run 内两个不同模型的混合 Worker 路由。

## 从暂停的执行生成待审修订

当需要更改原批准边界时，可以先通过现有 `pause-run` 停止执行，再调用
`replan-plan --base-plan-revision-id <原方案> --source-run-id <原Run> --criterion <待审条件>`
（仍须提供该命令的 `--idempotency-key` 及全局数据库/产物参数）。来源必须没有仍 pending/running
的 Attempt 和活跃自动检查；单纯把展示状态改为 paused 不足以通过检查。

Planner 会收到原设计、Phase、检查条件和有界阻塞/回复证据。生成草案不会替换暂停 Run 的生效契约；
便签摘要保留阻塞类型、原 Attempt 和过程版本（历史未知版本保留为空），不把未知副作用当作普通失败。
模型输入只包含最近的有界摘要及总数；生成期间会核对完整便签/回复历史，摘要之外的回复变化也会
使本次生成结果拒绝入库。摘要缺失不表示问题已解决。
新方案需要明确 `approve-plan`。这还不是自主过程调整或跨方案复用成果入口，原 Run 不会自动换绑
新方案，现有结果与历史会保留。执行仍活跃时不能批准切换 Goal 契约。
暂停 Run 尚有未决人工检查或未回复便签时，当前入口也拒绝批准不同契约，保留原请求的可回复性。
这是跨方案接手尚未实现时的保护，不要求用户把便签伪装成已解决；明确接手修订和有效成果复用仍待实现。
`replan-plan` 本身仍只生成一份暂停来源草案；相同幂等键可重读原结果，新键不会覆盖它。
继续讨论和修订可使用 `discuss-plan --source-run-id <原Run>`，同时提供已有的 `--goal-id`、
`--idempotency-key`、`--message` 或 `--message-file` 及全局 Planner/数据库/工作区配置。
可以用 `--conversation-id` 延续原会话；首次绑定后，后续轮次省略 source-run-id 会沿用来源，
不能将该会话改绑另一 Run。HTTP DiscussPlanRequest 对应可选 `source_run_id`，查询会话返回绑定。

来源必须为同 Goal 下已收敛的 paused Run，且仍绑定 Goal 当前生效批准；原人工请求可以继续未决，
讨论不会替它们回复或宣布解决。首轮使用来源当前执行图，已有待审草案时使用最新草案及其 Checks，
另提供原批准设计/Checks和来源执行摘要。新草案沿最新 Plan/Contract 版本递增，保留历史，
不提前替换 Goal 契约或 Run。模型调用期间来源控制、执行、人工回复或最新草案改变会拒绝入库。
当前来源问答及连续 v2/v3 待审草案保存已取得真实入口证据；后继 Run 接手、预算迁移及最终
自举仍未完成。

## Planner 节点调度要求

Built-in Planner 的 `add_plan_node` / `update_plan_node` 工具可以填写 `required_capabilities` 与
`session_policy`。这些值经过模板构建进入持久方案，可由 `get-plan` 查看；调度时必须匹配 Worker
预设的能力和 Session 策略。模型工具使用严格 Schema：这些字段必须出现，传 `null` 表示未指定。
新增节点未指定时使用空能力要求和 `new`，更新节点未指定时保留原值；`[]` 则显式清空能力要求。
自动 Gate 工具的 `human_question` 也必须出现，无人工条件时传 `null`，不改变 Gate 必需性。

能力标签只是调度条件，不授予 Shell、Git 或外部操作权限。当前正常启动入口的预设使用 `new`；
声明 `reuse` / `fork` 不会自动实现会话复用或分叉，没有匹配预设时不会分配 Worker。
底层 Session 策略与下面的逻辑阶段共同会话是不同概念。

## Built-in 阶段共同会话

Built-in Worker 的 `context.input_artifacts` 包含宿主为本次任务选择并校验的依赖 Artifact 标识、
元数据和内容；Reviewer 从这些输入取证，不需要向已经结束的同伴会话索取 Artifact ID。
`required_checks` 包含冻结 Check 的命令 argv 和语义条件，供检查方案审查；这些字段是验收约定，
不是 Worker 新增的命令执行权限。Codex Worker 的对应检查输入也保留这些参数。

带显式 Phase 的计划使用 Built-in 执行时，同一 Run/Phase 的任务加入同一个逻辑阶段会话。
Worker 工具 `phase_context_publish` 发布带作者、节点和分支归属的讨论；`phase_context_read`
按 offset 分页读取共同记录。模型每步开始前还会收到新增讨论，消费位置由持久模型输入恢复，
不是在消息送达前先标为已读。没有显式 Phase 的历史方案不自动补造阶段。

这与物理 `AgentSessionRef` / Turn 分开：并行任务仍有独立模型执行历史和隔离工作目录，共同
会话不授予额外工具、审批或 Gate 权限，也不改变宿主选择的代码输入。阶段讨论不等于有效 handoff；
宿主确认的 handoff 以独立类型加入共同记录，跨阶段会话交接与 Server 接入仍未完成。
共同会话的打开、成员加入和讨论事件可通过现有 `get-trace` 查询；未据此宣称真实并行场景已验收。

### Built-in 代码 handoff

正常编码配置下，非 Reviewer/Evaluator Worker 可调用 `submit_handoff`，提交 `key`、`completed`、
`context`、`remaining`、`known_issues`。宿主在本执行 lane 暂停写入、无活跃后台 Shell 时捕获实际
代码；确认代码版本、输入基线和上下文落库后才返回成功。该工具不结束任务，不替代候选提交或 Gate。
同一 Attempt/key 重试返回原交接，内容变化必须使用新 key；后续写入不改变旧交接。

`get-trace` 可查看 `AttemptHandoffConfirmed`。重新执行同一节点时，宿主只选择依赖基线仍匹配的
已确认交接；更新的成功候选优先。交接代码丢失或损坏会报错，无交接则回到该路径有效上游，保留
其他分支成果。停机时保存的普通快照不会自动变成交接，也不会作为未验收交付覆盖结果。

启动恢复不会为已完成 Built-in Turn 再调用模型。未完成的本地编码循环若只使用已知本地工具，
会保留旧目录、以新 Attempt 从交接或有效上游重建；不会因物理 Session ID 相同继续脏状态。
涉及未知 Provider 结果或快照之外的工具副作用时等待人工处理。完整的外部副作用核对/用户回复闭环
及 Server handoff 仍待实现，本轮尚无真实模型交接/强杀恢复验收证据。

### Worker 阻塞与干预回复

Worker 发现外部依赖、未确认副作用或其他阻塞时，应使用 `report_blocked` 提交原因、证据和所需
条件。宿主会中断当前 Attempt、保留获批方案和节点路线，并持久化一个带请求 token 的干预便签；
便签不是成功结果、Gate 决定或新的权限授权。可先查询一个 Run 的历史便签：

```powershell
$Interventions = uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    get-run-interventions --run-id $RunId | ConvertFrom-Json
```

确认阻塞已核清、可以按现有批准继续时，使用便签中的 `intervention_id` 和 `request_token` 回复：

```powershell
uv run ehai --database .ehai/state.sqlite3 --artifacts .ehai/artifacts `
    reply-intervention --idempotency-key intervention-reply-1 `
    --intervention-id $InterventionId --request-token $RequestToken `
    --actor operator@example.com --message '阻塞已核清，可按现有批准继续。'
```

回复只表示在不改变需求、接口、Gate 和工具权限的前提下继续，不会自动扩大边界。Run 为 `running`
时，宿主会重新排队对应节点；Run 为 `paused` 时仍须显式 `resume-run` 或 `resume-session`。相同
幂等键或相同便签回复只重放原结果，改变请求 token、actor 或 message 会被拒绝。HTTP 对应
`GET /api/v1/runs/{run_id}/interventions` 与 `POST /api/v1/interventions/{intervention_id}/reply`，
后者请求体包含 `idempotency_key`、`request_token`、`actor`、`message`。

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
- 公共计划构建仍支持三种自动检查及 `human:<question>` 人工条件；Builtin 新方案将最终检查集中到单一最终节点，非空 Artifact 不代表 solution 正确。
- 编码模式的阻塞和外部安全重试耗尽可暂停并给出 notice；复杂回复、改授权与跨方案恢复仍待 R3。
- 顶层通用 Agent、外部意见的完整平台交互和事件驱动 Routines 尚未交付。
- P2 只支持单 Execution Plane 进程；SQLite lease 用于崩溃恢复，不宣称分布式一致性。
- 本地 Built-in `ehai-api --p2-runtime` 使用单 Endpoint `ConcurrentRuntime`，并发上限由
  `--builtin-capacity` 控制；本地 Fake/Codex CLI 组合仍为单槽位。多 Endpoint 路由由应用层组合，
  不是 P3 Dashboard。
- 不支持 OpenCode、Claude Code、DSH Connector、动态插件、P4 Workflow 或 P3 UI。
- Provider 不报告 cost 时公开为 unavailable，不进行虚假估算。
- Artifact、日志、Worker context 和诊断均有边界，超限会 fail closed。
