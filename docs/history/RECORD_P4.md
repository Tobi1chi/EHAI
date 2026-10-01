# P4：精简生活事务后端

> 归档（2026-10-01 文档重组）：本文按原样保留为运行证据原文，其中的“当前”“下一步”指当时上下文。
> 现状见 [STATUS](../STATUS.md)，证据摘要见 [EVIDENCE](../EVIDENCE.md)，生活 Workflow 用法见 [WORKFLOWS](../WORKFLOWS.md)。

更新：2026-09-24。先做一条日常闭环，再由子 Agent 模拟使用；页面单独开发。

## 本轮交付边界

在一个独立的生活 Project 里，把原文和整理后的事项保存成待办，经人工确认后生效，
完成事项，再生成可以回看的待办回顾。固定流程直接写本地业务数据；不启动 Planner、
Worker 或 Git 工作树，也不使用假 Goal 表达生活事项。
外部顶层 Agent 负责把自然语言整理为结构化输入，后端保留原文、候选事项和明确决定。

| 模块 | 输入与输出 / 所有权 | 失败行为与正常入口验收 |
| --- | --- | --- |
| Workflow | 只读目录提供 `life.capture` / `life.review` v1；固定步骤由应用服务实现 | 不开放任意脚本、DAG 或动态步骤；未知名称拒绝 |
| Workflow Run | Project 拥有；输入快照、候选待办、决定、task_ids 或回顾快照 | 与核心 Run 身份分开；确认前无正式待办；批准一次，拒绝零待办 |
| LifeTask | Project 拥有，回指来源 Workflow Run；标题、截止时间、状态和版本 | 显式修改/完成/取消/重开；旧版本修改 409，不覆盖别人更新 |
| Routine | Project 拥有；绑定 `life.review` v1，UTC 时间、固定间隔、启停和版本 | 到期后事务性生成回顾并推进下次时间；宿主停机不触发，重启合并错过的触发 |
| Inbox | 聚合 `workflow_confirmation`；Goal/core Run 为空，提供 workflow_run_id | 源 Workflow Run 决定状态；批准仅创建待办，不执行生活事务，不替代 Gate |

只读目录是本轮可复用 Workflow 定义；暂不提供用户创建 Workflow 的接口。
新增本地业务服务和 SQLite schema 19，沿用核心事件日志、错误返回、CLI/HTTP/MCP 和生成客户端。
没有复制核心的 Worker/Attempt 状态机。既有编码执行仍走原有代码工作区路径。

## 时间、幂等和恢复语义

- API 写入必须带 `idempotency_key`。同一命令重放返回当时的响应快照，不是最新状态；
  请再 GET 查看当前对象。Workflow 命令之间全宿主共享 key 空间，不同输入/对象复用返回 409。
- 候选待办不可原地修改；需要改方案时拒绝旧请求，再以新 key 提交修正后的事项。
  `expected_version` 约束决定、待办修改和 Routine 修改。决定后再次决定返回 409；
  原决定同 key 重试仍返回原回执。
- `life.capture` 默认等待确认；调用者显式传 `require_confirmation=false` 才立即存待办。
  `life.review` 只读项目待办，不适用人工确认；`source_text` 必须为空、`tasks` 必须为空。
- 回顾保存当时所有 open 待办快照、逾期 ID、全项目 done/cancelled 总数，不是历史区间统计，
  不调用模型总结。后续修改待办不改变旧回顾。所有待办由 API 显式修改，不自动执行。
- Routine 仅支持 60 秒至 365 天固定间隔，不支持 cron、事件触发或按本地日历/DST 调整。
  首次触发时间由调用者提供且必须带时区；调度时间统一 UTC。
  停机期间错过 N 次时，只生成一份当前回顾，记录最早 `scheduled_for` 和 N 次
  `coalesced_occurrences`，保持原间隔相位向前推进到未来。手动回顾不改变 Routine 时间。
- Routine 修改是完整替换可编辑字段；调度也增加 version，编辑前必须重读。
  关闭后不生成新回顾；重启/重新开启时仍按显式 next_due_at 判断。
- 每条到期 Routine 的回顾、事件、下次触发及 last_workflow_run_id 在一个 SQLite 事务里提交。
  两个调度器会重新读取并竞争同一写事务，不各自重复写入。失败会回滚该事务并在下次 tick 重试；
  已提交的其他 Routine 不回滚。宿主约每秒扫描，停机前等待正在执行的本地 tick 收尾。
  `/routines/scheduler` 提供 active、last_tick_at 和 last_error；后台异常记本地日志。

## 正常接口

HTTP 均位于 `/api/v1`；多 workspace 仍使用 `/workspaces/<workspace_id>/api/v1`。
CLI 使用 `uv run ehai --api-url <宿主地址> <命令>`；所有新增写命令均接收
`--file <JSON文件> --idempotency-key <key>`，文件可省略 key，CLI 会补入。
MCP 名称由 CLI 命令的连字符改为下划线，写入通过 `request_json`；先读 `get_request_schema`。

| HTTP | CLI 命令 | 路径参数 |
| --- | --- | --- |
| GET `/workflows` | `list-workflows` | 无 |
| POST/GET `/projects/{project_id}/workflow-runs` | `start-workflow` / `list-workflow-runs` | `--project-id` |
| GET `/workflow-runs/{workflow_run_id}` | `get-workflow-run` | `--workflow-run-id` |
| POST `/workflow-runs/{workflow_run_id}/decisions` | `decide-workflow` | `--workflow-run-id` |
| GET `/projects/{project_id}/life-tasks` | `list-life-tasks` | `--project-id` |
| GET/POST `/life-tasks/{task_id}` | `get-life-task` / `update-life-task` | `--task-id` |
| POST/GET `/projects/{project_id}/routines` | `create-routine` / `list-routines` | `--project-id` |
| GET/POST `/routines/{routine_id}` | `get-routine` / `update-routine` | `--routine-id` |
| GET `/routines/scheduler` | `get-routine-scheduler` | 无 |

生活事项归 Project，不归 Goal；InboxOwner 新增 nullable workflow_run_id，goal_id 和
goal_objective 也变为 nullable。现有核心请求仍保留原 Goal/Run 字段。前端必须消费生成类型，
不能假定每条 Inbox 记录都有核心 Run。`run_id` 筛选只查核心 Run，不匹配 Workflow Run。
事件 `WorkflowRunChanged`、`LifeTaskChanged`、`RoutineChanged` 的 core run_id 均为 null，
correlation_id 是相应实体 UUID，payload 含 project_id 和完整当次快照，可用既有 consumer 补领。

## 使用示例

在源码目录启动轻量宿主，状态放仓库外；无需 Pi 配置或 Git 仓库：

```powershell
uv run ehai-api --database C:/private/ehai-life/state.sqlite --artifacts C:/private/ehai-life/artifacts --worker fake --planner single --host 127.0.0.1 --port 8787
```

这里没有请求 Worker 执行；两个生活 Workflow 在应用服务中执行真实业务写入。
在另一终端创建项目并录入：

```powershell
$api = 'http://127.0.0.1:8787'
$project = uv run ehai --api-url $api create-project --name '生活' --idempotency-key life-project-1 | ConvertFrom-Json
$projectId = $project.project_id
@{
    workflow = 'life.capture'
    source_text = '买牛奶；预约周末保洁'
    tasks = @(@{ title = '买牛奶'; due_at = $null }, @{ title = '预约保洁'; due_at = $null })
} | ConvertTo-Json -Depth 5 | Set-Content C:/private/ehai-life/capture.json -Encoding utf8
$capture = uv run ehai --api-url $api start-workflow --project-id $projectId --file C:/private/ehai-life/capture.json --idempotency-key life-capture-1 | ConvertFrom-Json
uv run ehai --api-url $api list-inbox --project-id $projectId
```

查看候选事项后明确批准，填写实际操作者身份和理由：

```powershell
@{ expected_version = $capture.version; decision = 'approve'; actor = 'user'; reason = '确认这两项' } |
    ConvertTo-Json | Set-Content C:/private/ehai-life/decision.json -Encoding utf8
uv run ehai --api-url $api decide-workflow --workflow-run-id $capture.workflow_run_id --file C:/private/ehai-life/decision.json --idempotency-key life-decision-1
uv run ehai --api-url $api list-life-tasks --project-id $projectId
```

完成/修改任务的 JSON 必须同时提供 `expected_version`、`title`、`due_at` 和 `status`
（`open` / `done` / `cancelled`）。`due_at: null` 明确清空时间，不是保留旧值。
人工确认不是完成任务；需通过 `update-life-task` 显式设置 done。

手动回顾的文件只需 `{"workflow":"life.review"}`，以新 key 调用 start-workflow。
每七天的回顾可通过 create-routine 配置：

```powershell
@{
    name = '每周生活回顾'; interval_seconds = 604800
    next_due_at = [DateTimeOffset]::UtcNow.AddDays(7).ToString('o'); enabled = $true
} | ConvertTo-Json | Set-Content C:/private/ehai-life/routine.json -Encoding utf8
uv run ehai --api-url $api create-routine --project-id $projectId --file C:/private/ehai-life/routine.json --idempotency-key life-routine-1
uv run ehai --api-url $api get-routine-scheduler
```

## 验证记录

复用 event_consumption 子 Agent 模拟人类，在仓库外非 Git 目录启动 production
create_local_app（fake/single、不启 P2 Worker runtime），通过正式 API/CLI/MCP 完成：

- Project `1f041007-5234-48f3-8890-403c0c483d56`：两项原文事务整理为候选任务，确认前 tasks 为空；
  Inbox 归属仅 Project，Goal/core Run 均为空。`simulated-human` 批准后创建两项真实 LifeTask。
- CLI update-life-task 模拟完成一项，手动 life.review 保存 done_count=1、open_tasks=1。
- Routine 到期生成一份回顾；通过公开 update 设置下一次到期后停机，离线错过时间，
  同数据库重启补一份，总计两份自动回顾。后续 tick 不重复；原手动快照及 tasks 保持。
- 真实 stdio MCP 发现 78 个工具并调用 list_life_tasks，返回重启后保留的两项任务。
  退出前 Scheduler active=true、last_error=null，Inbox 已清空；所有试用宿主已退出。

最初操作者脚本误用 `kind=workflow` 查找返回值导致断言失败；公开类型实际是
`workflow_confirmation`。修正临时脚本后用原幂等键继续原路径，没有因此改生产行为。
本地证据目录为 `%TEMP%/ehai-life-human-f7ad6606ce814856832edeca2007c1ac`，包含
summary.json、requests.json 和操作脚本，不提交到仓库。

收尾审查修复新增 HTTP 端点未规范化 UUID 大小写的问题：Schema 接受大写 UUID，
数据库保存小写，原样查询可能错误返回 404。所有新路径参数现统一 normalize_id。
子 Agent 复用原数据，用大写 Project/Task ID 正常查询、创建回顾，再以小写路径和同 key 重试；
返回同一小写 ID 的回顾，数量只增加一份。证据 uppercase-summary.json，验证宿主也已停止。

Python Ruff/format/mypy、Schema/Client 生成和 TS typecheck/build 通过。
0 模型调用，无新增模型角色或修改 Pi ToolSet；本次 MCP 是客户端协议调用，
不声明新增工具已经过外部模型 Provider 全部调用。未新增永久测试，不是唯一产品 E2E；
拒绝、并发决定、跨项目隔离、事务中强杀与多个调度进程未逐项做运行试验。

## 2026-09-24：独立执行记录与关闭的版本兼容代码

本增量按用户授权先拆分执行记录，版本兼容代码先实现但不启用。
仍只执行 life.capture / life.review；没有同时实现自定义图或外部 Connector。

| 模块 | 输入输出、所有权 | 失败行为与正常入口验收 |
| --- | --- | --- |
| 独立 Run 记录 | Project 归属、定义版本/快照、通用 inputs/result/trigger、状态和来源；不包含 LifeTask 专属顶层字段 | 原生活 Workflow DTO 由该记录投影，只有一个 Run 状态来源；新查询能看到定义快照与记录来源 |
| 步骤记录 | 定义节点 node_id 与该次 step_execution_id 分离，保存执行序号、输入输出、等待/完成/跳过状态 | 与当前业务写入在同一事务保存；人工决定继续使用原步骤 ID |
| 调用记录 | invocation_id 关联 Run 和步骤，记录动作契约、幂等身份、结果与种类 | 本轮仅记录真实本地业务动作，无远端调用；外部意图/未知结果字段是结构预留，不代表外部执行已接通 |
| 等待记录 | wait_id 关联 Run 和步骤，保存等待条件与明确决定收据 | 新 capture 的人工等待与决定可跨重启读取；完成后原 wait_id 保持并标记 resolved |
| 版本兼容模块 | 明确的源/目标契约身份、版本和摘要；显式适配器及目标结果校验回调 | 生产固定 enabled=false，适配器数量为零；未开放配置开关，不自动比较、转换或迁移运行中定义 |

存储升级为 SQLite schema 20，分别使用 workflow_execution_runs、workflow_step_executions、
workflow_invocations、workflow_waits，步骤/调用/等待通过外键约束同一 Run 的来源。
Run 从旧 workflow_documents 拆出后不双写旧行；原有事件、命令回执、任务、Routine 与 UUID 保留。
旧 get-workflow-run / list-workflow-runs 的公共字段和语义保持，幂等重放仍返回原回执。

旧 schema19 数据在首次打开时原子迁移，失败回滚该次迁移；旧运行标记 recording_origin=legacy_snapshot。
原来没有记录的定义全文和步骤轨迹不补造：definition_snapshot/digest 为空，子记录为空。
旧待确认流程可以继续，之后产生的真实决定与本地动作才写入新子记录；来源标记仍保留。
新运行 origin=native，保存内置定义快照和摘要；当前步骤 recorded_at 是事务中的观察时刻，
不是模型 trace 或精确的每个步骤耗时。没有成功提交的本地事务不会留下已成功调用的收据。

兼容代码位于 application/workflow_compatibility.py，提供契约匹配、显式适配器登记、
隔离 JSON 文档转换及目标校验接口。不做版本推断、适配链搜索、最新版本回退，
也不直接写入运行状态。所有生产 WorkflowService 均显式使用 enabled=false，
CLI/API/环境变量/Routine 都没有启用入口。数据库存储布局迁移不转换 Workflow 定义版本。

新增只读正式入口：

```text
GET /api/v1/workflow-runs/{workflow_run_id}/execution
uv run ehai --api-url <宿主地址> get-workflow-execution --workflow-run-id <UUID>
MCP: get_workflow_execution
TypeScript: getWorkflowExecution(workflowRunId)
```

返回 data.run、steps、invocations、waits 及当前宿主 compatibility 状态，来自同一只读数据库快照。
compatibility 应为 `{ "enabled": false, "registered_adapters": 0 }`。
读取不会创建记录或触发适配；既有 Inbox 操作仍通过 decide-workflow 提交明确决定。

正常入口迁移与重启试用通过：复用子 Agent 在修改前用 production API 准备 schema19 数据，
再正常启动新宿主迁移到 schema20。原 completed/pending capture、review DTO、旧任务、
禁用 Routine 和所有原 POST 幂等回执保持一致；三条历史运行子记录均为空且来源为 legacy_snapshot。
旧 pending 批准后仅追加新发生的确认/建任务步骤及本地调用。
新 capture 的确认步骤和 human wait 在批准前后保持 UUID，等待变为 resolved；
重复提交决定不增加执行记录或任务。重启后新旧执行记录及任务一致，CLI 新查询通过；
所有查询中的 compatibility 均为 enabled=false、registered_adapters=0。
证据位于 `%TEMP%/ehai-p4-split-migration-cd5bd0a74505451da031e58cfe24dc07` 的
legacy-state.json、legacy-requests.json、migration-summary.json 和 migration-requests.json。
补充新 life.review 的实际记录：native 来源、read_project_tasks/save_review 两步骤、
local life.save_review 调用，回顾与任务事实一致且 waits 为空；真实 stdio MCP
get_workflow_execution 与 HTTP 返回完全一致，证据 review-summary.json。
全部试用宿主已停止，未重启或修改用户正在使用的宿主数据。

静态检查、Schema/Client 生成与 TS 构建通过。客户端生成曾因 FastAPI 为递归 JSON 类型
生成 JsonValue-Output 引用而产生非法 TS 类型名；生成器现映射到已有的公开 JsonValue 契约，
重新生成后 typecheck/build 通过，没有编辑生成文件来绕开问题。
无永久测试新增，无模型调用；不将本轮正常接口试用视为唯一产品 E2E 或所有恢复情况的覆盖。
生产未启用版本兼容，不能声称跨版本适配行为已经完成实际运行验收。

## 下一增量

2026-09-24 用户明确：Routine 面向订票、日程提醒等日常事务，需要自定义流程、条件分支和
可扩展外部 Connector。现有待办/回顾只是固定流程原型；仅增加默认参数不能满足该要求。
下一增量的目标设计、步骤与连接器边界见 [自定义 Workflow 设计](../P4_WORKFLOW_DESIGN.md)。
当前尚不支持这些通用能力；上述历史试用不能作为新目标已实现的证据。
先推进该后端，再单独进入 P3.3 页面。

## 2026-09-24：Jev 双反馈环只读实验

根据用户提供的双反馈环构想，加入显式创建的 Project-local Routing lab。
正常入口、模块所有权及限制见 [实验说明](../DUAL_FEEDBACK_EXPERIMENT.md)。
SQLite schema22 保存目录/请求/回放快照、反馈及幂等回执；独立 Jev 进程复用既有
Connector HTTP 队列，不内置第二个 Planner。主 LLM/System 2 由外部 Agent 经公开接口承担。
没有启用 Workflow 版本兼容，也没有将实验 recipe 混入定时 Routine 或通用分支图。

用户提供专用 TypeSafe key 后，复用 event_consumption 子 Agent，在仓库外全新非 Git
工作区启动 production API（fake/single 核心，无 Pi 模型），通过正式 HTTP/CLI 和
独立 ehai-jev 进程试用。准备了一项真实生活待办和一项待确认 capture，保证查询有实际事实。

- 最初一轮首次真实 Jev 判断成功；随后 Connector 因 manifest description 在开发期间变动
  拒绝调用，错误为 `Jev contract does not match this connection`。诊断发现 schema 相同，
  仅描述变化；属于本地注册契约不一致，不是 TypeSafe 失败。恢复并冻结契约后用全新实验重跑，
  未关闭契约检查、换模型或盲目重试未知调用。
- 最终轮 9 次真实调用，连同最初成功的 1 次累计 10 次，均在授权的 20 次上限内。
  请求模型 jev-latest，响应实际模型 jev-1.13.0。最终轮保存 input_tokens=5274、
  output_tokens=963；这里只是保存的响应用量，不是费用账单或基准成本。
- 初始批准生活待办查询，Jev 选择后直接返回 core_query 实际结果。两个学习案例和一个
  独立 validation 人工决策查询在初始目录下均升级；子 Agent 读取真实 Inbox，以外部
  System 2 提交有依据的处理报告。没有在 EHAI 内另外调用慢模型。
- 用两个 learning 请求提出 inbox.list 候选；4 条真实回放全部通过，含候选的两个学习
  正例、一个独立 validation 正例和原 life.tasks.list 正例。期望标签没有发送给 Jev；
  confidence 为 .98/.98/.96/.99，选中概率 .99/.99/.98/.99；没有下调默认阈值。
- 子 Agent 模拟人类明确发布后，新措辞直接命中 Inbox 配方，confidence=.97、probability=.98，
  读取到原待确认 capture。随后**人为注入** misroute 标签以验证控制机制：配方暂停、
  原请求 escalated、旧 result 保留，System 2 再 resolve 后 previous_result 仍保留。
  这不是观察到的 Jev 误判，不能计入模型准确率评价。
- 有效配方的显式绑定通过代码规则完成且无新 Jev 调用；暂停配方的显式绑定升级而不执行。
  CLI metrics、原幂等收据和重启后持久化一致。metrics 中 provider_judgements=5 仅表示
  请求判断；另 4 次回放在 replay.entries 中，不能将 5 写成最终轮总模型调用数。

收尾同时补齐：回放至少需要一个未用于候选提案的 validation 候选正例，防止全是升级
期望时误放行；纠错后原请求可继续慢环处理，手动/反馈暂停保留 actor 和原因。
请求生命周期记录的是可观察结果和事件，不是模型隐藏推理。

共享 Connector client/outbox 提取后，原 Google 本地协议场景也在全新目录重跑通过：
四动作、调用/事件去重、CLI 和重启一致；模拟 POST 已落地但返回503、首次核对404后
记录 unknown，明确 reconcile 后只 GET 恢复 completed，全程只有一次外部 POST。
没有使用真实 Google/OAuth，没有从本地替身推断真实账号能力。

本地证据（不入库）：

- `%TEMP%/ehai-routing-final-af9cae75143d47df974662182abf8329`：summary.json、
  public-trace.json、model-observations.json；最初中断轮位于
  `%TEMP%/ehai-routing-live-d372d1e26068407fb8976176b1f14a56`。
- `%TEMP%/ehai-shared-bridge-calendar-fc935b0afc62469d91a7956b81f21d54`：Google 替身试用。

所有试用服务已停止；专用 key 仅由独立 Connector 从仓库外私有文件读取，不写入证据或代码。
Python Ruff/format/mypy、锁文件检查、Schema/Client 生成及 TS typecheck/build 通过。
没有新增永久测试；这是授权下的接口和模型集成试用，不是唯一产品 E2E，也不是准确率、
成本节约或高并发吞吐实验。新增 MCP 已随契约注册，本轮未逐个通过外部模型调用验收。

## 2026-09-24：自然表达的日常操作者模拟

用户要求模拟日常使用，复用原子 Agent，在新隔离生活 Project 中走正常 API 和真实 Jev。
场景与提问在调用前冻结：今天18点到期的未完成电费、明天12点的未完成整理书架、
今天已完成的快递，以及尚未确认录入的灯泡事项。内容为模拟数据，不是真实生活账户。
初始目录仅批准完整 life.tasks.list，不支持筛选、排序建议或人工 Inbox。

| 自然提问 | 真实观察及处理 |
| --- | --- |
| 我手头还有什么事？ | Jev 首选生活任务，probability=.87、confidence=.74；低于 .85 门槛，uncertain_judgement 升级。外部 Agent 查事实后区分未完成和已完成事项 |
| 看看哪些事情还等着我点头 | 目录外需求，Jev escalate；外部 Agent 查询 Inbox，答复灯泡录入等待确认 |
| 今天这些事怎么排，先做哪个？ | Jev escalate；外部 Agent 依据日期和任务标题中的轻重描述建议优先处理电费，未声称核心自带优先级字段或排序能力 |
| 还有没有等我确认的？ | 候选尚未发布，Jev escalate；外部 Agent 再查事实后答复 |
| 把今天要做的和等我决定的放一起说一下 | Jev escalate；外部 Agent 分别读任务与 Inbox 后汇总，未将单一查询误记作复合需求完成 |

用第二条学习请求提出 Inbox 候选，另以「现在有什么需要我拿主意的吗？」作为独立
validation，通过 force_slow 保存，不参与提案。冻结的3条回放为学习 Inbox、独立
validation Inbox 和第一条生活查询。首选项分别符合期望，但 confidence=.78/.67/.81，
均低于原 .85 门槛；validation 的选中 probability=.78 也低于 .8 门槛。回放 failed，
**候选没有发布**。未降低阈值、替换样本、修改措辞或追加回放追求通过。

本轮8次真实 Jev 调用（5次请求判断、3次回放），加前轮10次累计18次；没有替身判断。
6条请求（包含force_slow验证样本）均由外部 System 2 根据实际公开查询结果提交处理报告，
最终 fast_completed_count=0、system2_resolved_count=6、routing_count=0。
本轮没有观察到代码异常或执行错误，也没有注入 misroute 标签。
低置信升级和发布门正常拦截，不能将其描述为快环完成了日常任务或减少了主模型调用。

这一小样本暴露了使用边界：简短自然表达仍容易进入慢环；当前快环仅返回原始查询数据，
没有完整自然语言答复、日期/状态筛选或多来源组合。Jev 只接收当前消息和目录，
对话上下文、结果筛选及解释由外部 Agent 处理。本轮保留机制，不直接调阈值或扩接口。
证据位于 `%TEMP%/ehai-routing-daily-3ce7809049654690b292eaa153698a34`，公开轨迹与
逐条答复保存在仓库外的 transcript.md、summary.json、public-trace.json 中。
全部临时服务已停止；未新增永久测试，未作新的准确率或成本结论。

## 2026-09-24：Pi / DeepSeek 慢环提出改进后复测

用户要求使用项目自带 Pi 和留存 OpenCode Go 测试凭证，让 DeepSeek V4.1 承担慢环，
并明确 Codex 子 Agent 仅模拟用户和监测状态，不参与内部任务。本轮依此分工：
Pi / DeepSeek 分析学习证据并提交候选；Codex 子 Agent 只准备隔离项目、冻结用户侧案例、
提交回放/模拟明确发布并核对公开结果，没有编写配方、慢环答复或代行 resolve。

使用生产 `PiRoleRunner`、Pi 0.85.1 原生 RPC 和已安装的工具桥，模型精确指定
`opencode-go / deepseek-v4.1-flash`、thinking off。正常 Provider 响应确认该模型，
Go key 可用且会话头存在；没有以其他模型或裸 completion 冒充 Pi Agent。
仓库外薄适配提供 read_learning_evidence、propose_improvement、finish_improvement，
业务读取/候选写入均走核心正常 HTTP API。只允许一个 inbox.list 候选，无发布、阈值修改、
任意命令或读验证集权限。薄适配本身是本地实验程序，不是已经交付的常驻慢环调度服务。

原日常试用数据库通过 SQLite backup 复制到新隔离工作区，旧失败记录保持。
给慢模型的证据只含原 Q2 学习请求、该条旧回放、目录与实际 Inbox/任务事实，
不包含旧 validation 文本或本轮新验证措辞。新验证案例在 Pi 提案前冻结，包含
独立 Inbox 正例、写入负例、复合排序负例。

Pi 真实调用读取工具，再调用提案工具创建 `34621864-bab8-4744-8d82-0039727419e0`，
target=inbox.list，名称为“待人工确认事项查询”。主要改动由 DeepSeek 生成：
显式描述 pending 确认请求、确认后才创建记录的情形，以及“等我点头/拍板”等口语含义，
同时保留不代决、不写状态、不查询生活任务、不处理复合需求的限制。
原提案轮3次 Go 请求、9987报告 token，候选通过公开 API 持久化后以结束工具收尾。

| 一次固定回放 | 真实选择 | confidence / selected probability |
| --- | --- | --- |
| 原 Q2 学习请求 | 新 Inbox 候选 | .98 / .99 |
| 独立 Inbox 正例 | 新 Inbox 候选 | .98 / .99 |
| 写入负例 | escalate | .99 / .99 |
| 复合排序负例 | escalate | .99 / 1.00 |

四项均 passed；confidence_threshold=.85、probability_threshold=.8 未变。
原 Q2 的旧候选回放为 confidence=.78、probability=.85，未过置信门，本轮新候选该条通过。
这是单条学习案例及小样本新验证的观察，不足以把差异全部归因于文字改动或推断泛化提升。
没有变更模型、替换未通过案例、降低阈值或多轮抽样直至通过。

操作者核对后在隔离项目以 simulated-human 明确发布。新的未参与提案/回放的问题
“请把本项目目前尚待我给出确认意见的请求列出来，先不要替我操作。”
实际命中新配方，confidence=.99、probability=.99，返回 completed/core_query，
内含原灯泡 workflow_confirmation。前后公开查询确认 capture 仍 awaiting_confirmation、
LifeTask 和原失败候选/回放完全不变；没有批准生活事项，也没有用 Codex 生成答复冒充快环。

### 慢环说明的事实纠错与未收尾记录

根任务核对发现 DeepSeek 初稿把“目录没有 Inbox 候选时的原请求判断”与“旧候选回放”
混淆：初始 .92 为 escalate、.08 为生活配方，旧候选真实回放为 .85 概率、.78 置信度。
没有修改模型提交的配方或回放结果，而是让 Pi 对其证据说明作只读更正。
同一 Pi Session 的续轮输出了更正文案，但未调用结束工具；宿主正确拒绝把自然语言终止
当作完成，保存 `PiExecutionUnknownError`，不伪造成功事件或盲目恢复该未完成轮次。

核对持久轨迹确认该续轮没有工具调用、没有候选写入后，使用明确的只读纠错任务：
保留原未完成 Session，另建 Pi Session，仅开放读取与结束工具，明确要求调用结束工具。
Pi 重新读取真实证据并提交更正，区分观测事实与原因假设，正式收尾；候选仍保持原样。
这是实验适配提示的局部修正，没有放宽生产 Pi 的完成/恢复判定，也没有让 Codex 子 Agent
代填结束结果。自动处理此类未收尾慢环任务仍未实现。

本轮共6次真实 Go请求、21145报告 token（含原提案3次、未正式收尾续轮1次、只读纠错2次），
以及5次真实 Jev调用（4回放+1发布后查询，模型jev-1.13.0），在本轮分别8次的界限内。
所有试用服务/模型子进程已停止；密钥仍只从仓库外私有文件加载，无永久测试或代码机制改动。
没有运行无关静态/模型检查；文档 diff 检查通过。

证据（仓库外）：

- `%TEMP%/ehai-pi-improvement-c6a950b38b5744d79d9fedfdfb5d9494`：summary.json、
  candidate-intent.json、learning-evidence.json、Provider 输入和 Pi 持久轨迹。
  summary 保留原模型说明与更正，candidate 字段是提案时快照，后续状态以公开查询为准。
- `%TEMP%/ehai-routing-pi-retest-ca2909cf1ee44cd880853efb272312d8`：
  operator-replay-summary.json、operator-publish-summary.json、operator-transcript.md。

本轮证明了“指定慢模型提出候选 → 独立 Jev 复测 → 明确发布 → 新请求只读执行”能走通。
自动触发/调度慢环、把改进理由纳入专门产品实体、通用流程图与自动发布均不由此认定完成。

## 2026-09-24：正式宿主的固定 Pi 回退分支

用户确认采用精简的 `Jev escalate → Pi` 分支。本增量将此前仓库外适配接入生产宿主，
正常配置、模块输入输出/所有权及边界见[实验说明](../DUAL_FEEDBACK_EXPERIMENT.md#固定-pi-回退分支)。
新增 `--routing-model/--routing-reasoning-effort/--routing-timeout-seconds`，继续使用已有
私有 Pi 配置；每个 lab 显式选择 pi 模式。默认 external 和已有实验保持手动推进。
只支持直接 API 宿主，Workspace Manager 暂未提供这些新启动参数。

Pi 共用现有 PlannerCapacity 准入，单条请求最多一次自动调用。请求的 fallback 字段保存
调用身份、配置指纹、状态、回答、候选理由和复测引用；复用 schema22 的 JSON 文档，
旧记录缺失字段时使用兼容默认值，不改现有 Workflow 定义或启用版本兼容。
模型结果只在结束工具与原生 settled 确认后原子写入；故障/中断不自动重试，旧失败不覆写。
有独立验证案例时自动触发一次回放，案例在排队前持久冻结，发布仍需明确操作。
新配置入口已贯穿 HTTP、CLI、MCP 路由表、OpenAPI 和生成 TS Client。

### 实际失败、局部修复与原路径复试

复用子 Agent 仅模拟用户与查询状态。生产 API 宿主、真实独立 Jev Connector、
Pi 0.85.1 / Go deepseek-v4.1-flash（thinking off）均由根任务配置，用户侧不手动
advance、启动某次 Pi、提案、发起回放或 resolve。场景仍是生活待办和灯泡待确认事项，
独立正例/写入负例/复合排序负例在调用前冻结，模型无法读取其文本或期望标签。

首次两个正常用户请求均经真实 Jev 自动升级，并生成持久 Pi 调用身份，但在发给 Go 前失败：

1. Pi 严格工具契约拒绝 finish_routing_fallback 的 `$defs`。
2. 展开引用后，又明确拒绝 object|null 联合类型。

没有关闭 strict、换模型或改权限。最终将模型参数改为简单的必填/nullable 标量：
response、evidence、needs_human、candidate_name/applicability/target、change_reason。
候选四字段必须同时填写或同时为 null，核心仍组合成严格的候选模型校验。
直接运行安装版本的 `getJsonSchemaToolParameters(..., true)` 验证接受，再重跑正式宿主。
两次失败时 Go 上游请求均为0；原 request/attempt 留存 failed，不伪造成功或复用原幂等键重派。

第三次真实 Jev 升级后，Go 接受工具并实际读到两类事实。模型却把“查询结果含待确认对象”
误当成“当前查询仍未完成”，同时设置 needs_human=true 并提候选，触发核心拒绝。
原错误提示只说检查字段，不足以指导修正，产生重复提交。根任务有序停机，保存
interrupted/host_shutdown；没有候选或回答写入。修复字段说明、提示词及具体校验反馈：
needs_human 指当前请求是否仍需人工，不是所列对象是否待确认；保持原校验不变。

用户侧明确以新 key、原问题及 force_slow=true 重试，避免重复已确认的 Jev 分类。
宿主自动执行 Pi，模型调用读取和结束工具后保存 completed/pi_agent_report，并产生
候选 `463387df-877e-4e0c-b392-221b827d0550`。自动回放
`d4f57cff-b574-487e-8ad8-bd176abfe098` 的学习/独立正例 confidence=.90/.94，
写入/复合负例都选择 escalate、confidence=1，四项通过，门槛仍为 .85/.8。
回放通过前未发布；模拟用户明确发布后，新自然问句自动快环 completed，
confidence=.89、probability=.93，返回原灯泡待确认记录。capture 和任务均未改变。
同 key 重提交返回同一受理回执，没有重复派发；原失败和中断记录保持。

在一次 GET 观察中发生 WinError10054，宿主仍运行；重连只读 GET 后取得已完成回放，
没有重试未知写入，也未因此修改生产恢复机制。

本地证据位于 `%TEMP%/ehai-auto-fallback-operator-fd35abd0b71148f484b39678497e47d6`，
包括用户提交/观察/发布轨迹、usage.json、实际 Go 输入和核心持久 Pi 轨迹。
Codex 子 Agent 没有生成慢环答复、修改配方或代填结束结果；无永久测试新增。
本轮不代表通用工作流、自动发布、任意写操作或多进程抢占同一数据库已支持。

### 修复后完整正常入口与收尾

为补足修复后不依赖 force_slow 的证据，仅追加一个正常用户请求：
“读取项目里的生活待办和待确认事项，帮我给出一个先后处理建议。”
请求 `62d8b14c-f978-4941-ad48-140fba284929` 保存 force_slow=false，真实 Jev 自主
选择 escalate（confidence=.99、probability=1），宿主自动进入 Pi；DeepSeek 实际读取
life.tasks.list 和 inbox.list，通过结束工具提交事实及建议，最终 completed/pi_agent_report。
它没有把排序/复合需求包装成单查询配方，candidate_id/replay_id 均为 null。
本次没有任何手动 advance、启动 Pi、提案、回放或 resolve。用户同 key 重复提交只对应
一条 Connector 调用；容量恢复为 in_use=0，任务与灯泡确认完整 DTO 保持不变。

最终13次真实 Go 请求均200，共45252报告 token；包含未完成轮次，非账单或节省量。
Jev共9次：前三次正常升级、四条自动回放、发布后查询、最后正常复合请求。
为完成修复后正常入口验证，将仓库外观测界限从 Go12/Jev8 明确调整为 Go16/Jev9；
保留既有计数，只追加上述一个场景，未改变产品门槛或用户模型授权范围。
验证样例完整消息在全部实际 Go 输入中出现数为0；不把这当作一般防泄漏证明。
所有宿主、代理和模型/Connector 子进程已退出，final-root-summary.json 保存最终计数；
正常入口记录另见 normal-chain-summary.json。用户侧模拟仍只承担输入、明确发布和状态观察。

Python Ruff/format/mypy（163个源文件）、锁文件检查、Schema/Client生成及TS类型检查/构建
通过，文档与代码 diff 检查通过。模型面对的可选候选用 null 标量表达，省略不合法，
没有新增永久测试或 Provider fallback。强杀任意写入时点、多宿主共库、自动故障重试与
任意外部写操作没有在本轮验收，正常只读分支的通过不代替这些边界。

## 2026-09-25：慢环向独立 Project 转交代码修改

新增 lab 的显式 change-project 绑定及 Pi 的 project_change 工程目标字段。宿主读取目标
核心的 Project 和 runtime/context，解析实际路径并拒绝与运行源码、私有配置/运行库、
数据库和成果目录重叠；独立 EHAI clone/worktree 可以作为目标。绑定快照随调用保存，
转交前再次核对。Pi 收尾后释放本地模型槽位，宿主仅创建目标 Goal 和规划讨论，不批准、
执行、合并或部署。实际接口和后续通用流程引擎设计见
[Project 修改边界](../P4_WORKFLOW_REVISION_DESIGN.md)。

隔离试用以真实 EHAI 克隆中的 examples/routing_revision_demo.py 为目标：保留函数签名，
仅将 status=open 的项送入 tasks 分支，无 open 项走 inbox 分支，仅允许修改该文件。
绑定运行源码返回409，绑定独立克隆成功；正常自然语言请求经真实 Jev 升级，Pi 使用
deepseek-v4.1-flash 实际提交 project_change，目标 Goal 自动创建，同 key 提交不重复受理。

首轮目标 Planner 过度读取无关框架实现，尚未提交草稿便触及仓库外观测器32次 Go调用
护栏；32次上游响应均200，累计625515报告token，不是认证或工具 Schema 拒绝。
目标保存 PlanningTurnFailed；来源保存 unknown/StateConflictError 及已创建的 Goal ID，
没有盲目重派、批准或执行。修复转交提示：调查限定在请求文件和必要指南，使用已提供的
工具契约，明确 finish_plan 只提交草稿。保留失败记录，模拟用户在原 Goal/讨论以新key
补充范围澄清；观测上限明确调整为累计80次 Go/10M token，既有计数保留，模型和权限不变。

该澄清仍未使 Planner 收敛。第二轮读取大量无关源码/Schema 后以 length 结束，最后一次
Go请求的 max_tokens=1，报告 prompt_tokens=124664、completion_tokens=1；私有试用配置
的自动压缩为关闭。原生轨迹末尾只有文本“I”，没有 finish_plan，核心正确拒绝把它记为
完成。第二轮没有触及80次总调用护栏；该失败不能归为认证失败或已修复的工具 Schema 问题。
转交提示的调查范围已改进，但提示调整的实际收敛效果尚未得到成功证据，未扩大为新的
调查预算或自动重试机制，也未切模型或代写计划以使试验通过。

最终65次真实Go响应均200，累计2507781报告token；真实Jev调用1次。公开API确认原讨论
两turn均failed、目标plans/runs均为空、Planner容量释放；source转交的unknown原貌保留。
没有批准、执行、Reviewer或人工Gate，故**本轮仅验证路径保护和工程意图→目标Goal，
尚未验证该任务的代码修改闭环**，也不证明通用流程图/脚本执行器已交付。
目标克隆Git状态干净；根任务更新转交提示后拍摄的164个运行Python文件摘要没有再变化。
所有试用宿主、代理及所属子进程已退出。证据保留在
`%TEMP%/ehai-project-change-d03db1cd6ccb4df8959e44ece0b91f69` 的
final-root-summary.json、final-user-summary.json、user-clarification.json及原生轨迹中。
Codex子Agent仅模拟用户与观察状态；未新增永久测试，未提交或推送仓库改动。

### 后续复测：独立最小项目

按用户要求继续试验，保留上述全部历史。先在原 EHAI 克隆/Goal/讨论启用同模型的 low
推理，单请求输出上限从4096调至16384。实际请求确认 thinking.enabled；上游响应200后
流中断，仓库外观测代理报 httpx.ReadTimeout，原生消息以 terminated 结束，只留下不完整
工具调用，核心未接受任何计划。该请求没有用量回执，不能将原生零值当作实际消耗为零；
未自动重放未知调用，第三轮失败和空plans/runs均保留。

随后在同一临时目录建立只含 README 与原分支函数的独立 Git Project，明确重新绑定 lab，
恢复同模型 thinking=off、单请求输出8192，重新从自然语言/真实Jev入口运行。
这验证普通目标项目的完整链路，不替代 EHAI 整仓目标 Planner 的收敛验证。
首轮慢环反复将 candidate_target 写成字符串 "null"，以及在 needs_human=true 时填写
候选，校验正确拒绝。停止并保留 interrupted/host_shutdown 后，仅修改生产提示词，明确
JSON null 与字符串的区别，提供工程转交相关字段的格式示例；response/evidence/工程目标
仍由模型产生，未放宽严格 Schema 或替内部填答。

修复后新正常请求由真实Jev升级，Pi成功提交工程目标，宿主自动生成目标Goal和草稿，
来源转交保存 waiting_approval。目标Planner实际使用设计/图/Gate工具，并自行修复超长
指令、非法Session策略和最终Gate重复的校验反馈。首份草稿仍提出未授权的Python/Git
命令；模拟用户未批准，按原空命令/shell/git权限在同一讨论中要求模型自行修订。
在规划完成、尚无Run时有序重启观测宿主，取消原临时总请求数护栏，仅保留用户授权的
累计10M报告token界限和角色超时；既有计数92次/2741325报告token未清零。

权限修订首次因上游 RemoteProtocolError 返回502，目标保存failed且不自动重试。核对只
发生读文件/未提交设计、没有批准或Run后，模拟用户以新key明确补试一次。补试返回正常
用户问题：Planner误将未批准草稿的命令Check当成需要保留的已批准条件。模拟用户通过
正常讨论接口澄清原计划仍draft，并显式提交单条human完成条件，未直接改图或检查存储。
该轮后续长响应再次在200之后发生观测代理ReadTimeout，原生terminated，无修订计划。
同模型/同凭证的独立极小只读探测完整返回200与流结束标记，因此不能把这些长流故障
归因于凭证完全失效；网络中断的更深层原因尚未确认，没有因此关闭strict或更换模型。

本次实际结果：正式请求 `19e2fc6f-4c1f-4982-91b7-8f02b7752309` 经Jev→Pi自动创建
Goal `7580d7be-588b-4df5-ae42-bdfdc71f1408`，并生成草稿
`35edd9c0-8746-4aa0-91af-fc5f56e32eec`；转交记录为waiting_approval。原草稿因命令
范围不合规而未获批准，修订尚未完成，没有Worker/Reviewer执行、代码修改或人工Gate。
本轮因此补足了目标草稿的证据，**没有完成修改闭环**；整仓目标的调查收敛、未批准条件
的修订理解和较长Provider工具流稳定性仍需后续处理，不用小请求成功替代这些验证。

本次新增37次Go请求、2次Jev调用；其中Go第66/77/96/101次没有完整用量回执（含有序
停机时中断的第77次），其消耗未知。
其余新增请求报告341805 token，连同此前合计2849586报告token（不是精确账单）。
没有新增永久测试；生产代码仅补充空值提示示例，Ruff/format及mypy通过。
本轮证据沿用上述临时目录，文件以retest-、minimal-、minimal-fixed-区分，保留此前失败；
最终结果见minimal-final-user-summary.json及retest-final-root-summary.json。
收尾确认两个目标仓库Git状态均干净，提示修复之后的164个运行Python文件摘要没有再变；
唯一计划仍draft、approved_at=null，Run数量0、模型容量占用0。试用宿主、代理和所属
子进程已全部退出，未合并、部署或推送。

### 2026-09-27：传输对照与超时遗漏修复

用户再次要求多试，继续使用留存测试key、同一DeepSeek模型和隔离项目。发现当前HTTPX
默认会继承Windows系统代理，过去并未保存该设置快照，因此不能据此倒推此前每次连接。
使用原失败请求101的同一份76594字节请求体，比较继承系统代理与trust_env=false直连，
只观察返回流，完全不执行其中的工具调用，也不将返回内容注入EHAI。

首次观测脚本没有处理choices/tool_calls的合法null，在系统代理侧报了本地TypeError，
修正仓库外解析器后重新比较；该脚本错误不计作Provider失败。首次直连独立出现真实
ReadTimeout：已收3190字节，末数据后静默180.016秒，总耗时213.36秒，无完整结束标记。
随后对照中系统代理/直连分别在30.172/40.562秒完整结束，均200且finish_reason=tool_calls，
报告18627/21039 token；最长块间隔18.843/34.047秒。中断因此并非本地观测代理特有，
同时也不能据此认定系统代理或Go服务是唯一根因；间歇性服务/网络停顿仍需保留不确定性。

生产Pi随后直接连接Go，绕过本地观测代理，沿原Goal/讨论继续仅人工验收要求。读文件/
inspect_plan工具及结果交接完成后再次长时间等待；实际Pi进程保持到API的TLS连接。
这时定位了本地超时配置遗漏：现有planner_timeout_seconds对Pi没有接线，超过300秒
仍running。终止该次所属进程后保留失败；补上接线，通过8秒挂起端点正常API诊断验证。
详细修复和验证边界见R2记录；不是通过增大token或取消严格校验解决问题。

修复后重新启动同一生产装配、Pi直连Go，同Goal/讨论以新key继续明确的human-only条件。
实际读文件/inspect_plan及set_plan_design完成，但后续生成仍停顿；本轮从
16:31:27.657598Z到16:36:27.721815Z共300.064秒，正常API保存PlanningTurnFailed，
错误明确为300s deadline，原生调用aborted，所属Pi进程退出。未自动重放，原未批准
草稿保留；没有新修订、批准、Worker或代码修改。故本轮验证了真实Provider路径的截止
行为，尚未验证修改闭环成功，也不能宣称传输停顿已经解决。

两次生产直连会话报告47943/50785 token，加上完成的传输对照39666，共新增138394
报告token；连同以前的报告值2987980。中断/观测解析失败/未完成请求缺少用量，不能当作
零消耗或精确账单。测试只修改仓库外启动/诊断文件；生产修复限于Pi Planner的超时接线。
证据仍在原临时目录：transport-20260927*/、deadline-diagnostic-20260927/result.json、
direct-20260927-*及transport-20260927-summary.json；诊断脚本未加入仓库。

### 2026-09-27：最小目标项目修改链路完成

用户再次要求复测，继续使用此前真实Jev→Pi转交创建的同一Goal/讨论；本轮没有重新做
Jev分类。保留所有失败历史与原未批准草稿，仍由生产Pi直接调用同一Go模型
deepseek-v4.1-flash（off、8192输出上限），规划时限300秒。模拟用户再次明确原单文件
需求、空命令/shell/git权限、仅human验收，并要求简洁的设计/节点说明；没有代填图或代码。

本轮规划52.6秒完成，提交修订 `07b49825-6c47-4ad5-b63d-bb8d201cd372`。命令Check及
Worker/Reviewer命令要求已删除；保留三节点：实现、独立静态检查、Reviewer。出现两条
语义重复的human Check，均表达同一已授权验收要求；本轮记录该问题，未静默删除或绕过。
模拟用户审查实际草稿、明确批准、使用最新执行配置显式授权启动
Run `d0b17348-9579-430e-a7b5-ce67f09c7f3e`。

内部Pi Worker通过workspace_write真实修改候选工作树，另外两节点只读检查。四个角色
会话的实际工具轨迹没有命令/shell调用；Reviewer提交pass建议，未替用户决定Gate。
最终候选commit `b70614fa87019b371c6ec9b7b06307d3c35dcfb2` 相对基线
`b8093c9c54cad094902975f00247a37a498af9fb` 仅修改 examples/routing_revision_demo.py：
按status精确等于open筛选，再决定tasks/inbox，函数签名不变。

根任务先审查真实代码/patch，再在仓库外用uv内存加载已审查的纯函数，验证open、混合、
全closed、空列表、缺失status、大小写/顺序六项，结果全通过，输入没有被修改。
最终Reviewer工作树的文件SHA256与受测文件相同
`ca9d5f2e6b57afa022ecf14f350f1434ac350e0d832ce745b3d60b4be31191be`。
模拟用户据此逐条明确通过两条human Check，正常查询确认Run=completed、Inbox为空。
行为验证由根任务在外部完成，不是Worker在空权限下运行命令，也不是自动Gate。

四个内部角色会话共23个带用量的Provider响应，报告318677 token；本轮未出现此前的
上游中断，但单次完成不证明间歇性传输问题已根治，也不能把简洁提示当作网络修复证据。
运行框架164个Python文件摘要不变，目标原检出保持干净；所有改动停留在隔离候选工作树，
未integrate、merge、push或部署。子Agent仅模拟用户和观察状态。本轮无生产代码改动或
永久测试新增，仍是隔离能力试用，不是已约定的唯一产品E2E。

这补足了独立小项目从工程转交、人工修订/批准、实际执行到人工验收的证据。EHAI整仓
目标的规划收敛、任意代码/分支修改的可靠性、原生通用流程引擎及自动发布仍不能据此
推断完成。source.project_change保存转交当时的waiting_approval，不镜像目标Run；
执行进度从目标Goal/Run查询。重复human Check的生成仍待单独处理。
证据位于同一临时目录的retest-20260927-concise-*，包括原生会话、计划/授权、行为结果、
最终候选、逐项human决定及root-summary/final-user-summary。
