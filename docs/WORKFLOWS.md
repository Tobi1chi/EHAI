# 生活 Workflow 与 Routine

更新：2026-10-01。本页是已实现的两个固定生活流程的用法与契约。
自定义流程、条件分支和事件触发尚未实现，目标设计见 [自定义 Workflow 设计](P4_WORKFLOW_DESIGN.md)；
运行证据见 [EVIDENCE](EVIDENCE.md#p4-生活事务与外部连接)。

## 范围

在一个独立的生活 Project 里，把原文和整理后的事项保存成待办，经人工确认后生效，
完成事项，再生成可以回看的待办回顾。固定流程直接写本地业务数据；不启动 Planner、
Worker 或 Git 工作树，也不使用假 Goal 表达生活事项。
外部顶层 Agent 负责把自然语言整理为结构化输入，后端保留原文、候选事项和明确决定。

| 模块 | 输入与输出 / 所有权 | 失败行为 |
| --- | --- | --- |
| Workflow | 只读目录提供 `life.capture` / `life.review` v1；固定步骤由应用服务实现 | 不开放任意脚本、DAG 或动态步骤；未知名称拒绝 |
| Workflow Run | Project 拥有；输入快照、候选待办、决定、task_ids 或回顾快照 | 与核心 Run 身份分开；确认前无正式待办；批准一次，拒绝零待办 |
| LifeTask | Project 拥有，回指来源 Workflow Run；标题、截止时间、状态和版本 | 显式修改/完成/取消/重开；旧版本修改 409，不覆盖别人更新 |
| Routine | Project 拥有；绑定 `life.review` v1，UTC 时间、固定间隔、启停和版本 | 到期后事务性生成回顾并推进下次时间；宿主停机不触发，重启合并错过的触发 |
| Inbox | 聚合 `workflow_confirmation`；Goal/core Run 为空，提供 workflow_run_id | 源 Workflow Run 决定状态；批准仅创建待办，不执行生活事务，不替代 Gate |

暂不提供用户创建 Workflow 的接口。Workflow 不复制核心的 Worker/Attempt 状态机；
编码执行仍走核心计划与代码工作区路径。

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

## 接口

HTTP 均位于 `/api/v1`；多 workspace 仍使用 `/workspaces/<workspace_id>/api/v1`。
CLI 使用 `uv run ehai --api-url <宿主地址> <命令>`；所有写命令均接收
`--file <JSON文件> --idempotency-key <key>`，文件可省略 key，CLI 会补入。
MCP 名称由 CLI 命令的连字符改为下划线，写入通过 `request_json`；先读 `get_request_schema`。

| HTTP | CLI 命令 | 路径参数 |
| --- | --- | --- |
| GET `/workflows` | `list-workflows` | 无 |
| POST/GET `/projects/{project_id}/workflow-runs` | `start-workflow` / `list-workflow-runs` | `--project-id` |
| GET `/workflow-runs/{workflow_run_id}` | `get-workflow-run` | `--workflow-run-id` |
| GET `/workflow-runs/{workflow_run_id}/execution` | `get-workflow-execution` | `--workflow-run-id` |
| POST `/workflow-runs/{workflow_run_id}/decisions` | `decide-workflow` | `--workflow-run-id` |
| GET `/projects/{project_id}/life-tasks` | `list-life-tasks` | `--project-id` |
| GET/POST `/life-tasks/{task_id}` | `get-life-task` / `update-life-task` | `--task-id` |
| POST/GET `/projects/{project_id}/routines` | `create-routine` / `list-routines` | `--project-id` |
| GET/POST `/routines/{routine_id}` | `get-routine` / `update-routine` | `--routine-id` |
| GET `/routines/scheduler` | `get-routine-scheduler` | 无 |

生活事项归 Project，不归 Goal；InboxOwner 的 workflow_run_id、goal_id 和 goal_objective
均可为 null。前端必须消费生成类型，不能假定每条 Inbox 记录都有核心 Run。
`run_id` 筛选只查核心 Run，不匹配 Workflow Run。
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

## 执行记录

每个 Workflow Run 拆分为独立的运行、步骤、调用与等待记录（SQLite schema 20 起），
通过 `get-workflow-execution` 查询；返回 data.run、steps、invocations、waits 及当前宿主
compatibility 状态，来自同一只读数据库快照。读取不会创建记录或触发适配。

| 记录 | 内容 | 语义 |
| --- | --- | --- |
| Run | Project 归属、定义版本/快照、inputs/result/trigger、状态和来源 | 原生活 Workflow DTO 由它投影，只有一个状态来源 |
| 步骤 | 定义节点 node_id 与该次 step_execution_id 分离，执行序号、输入输出、等待/完成/跳过 | 与业务写入在同一事务保存；人工决定沿用原步骤 ID |
| 调用 | invocation_id 关联 Run 和步骤，动作契约、幂等身份、结果与种类 | 当前只记录本地业务动作；外部意图/未知结果字段是结构预留 |
| 等待 | wait_id 关联 Run 和步骤，等待条件与明确决定收据 | 人工等待跨重启可读；完成后原 wait_id 标记 resolved |

schema 19 的旧运行在首次打开时迁移，标记 `recording_origin=legacy_snapshot`，
不补造当时没有记录的定义全文和步骤轨迹；之后发生的决定与本地动作才写入子记录。
新运行为 `native`，保存内置定义快照和摘要；步骤 recorded_at 是事务观察时刻，不是精确耗时。

版本兼容模块（`application/workflow_compatibility.py`）已有代码接口，生产固定关闭：
compatibility 应为 `{ "enabled": false, "registered_adapters": 0 }`，
CLI/API/环境变量/Routine 都没有启用入口，不能声称跨版本适配已完成运行验收。

## 相关文档

- [Connector 协议与 Google Calendar](CONNECTOR_PROTOCOL.md)：外部进程领取动作、回传结果与事件。
- [Jev 双反馈环实验](DUAL_FEEDBACK_EXPERIMENT.md)：只读路由配方（与 Routine 无关）。
- [自定义 Workflow 设计](P4_WORKFLOW_DESIGN.md)：尚未实现的通用流程目标。
