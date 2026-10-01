# Jev 双反馈环实验

本轮交付是显式开启的只读实验：代码处理确定绑定，Jev 在批准目录中选择，升级可由
外部 Agent 或显式启用的固定 Pi 回退处理，回放通过后由操作者明确发布。它不表示通用 Workflow 分支引擎、
自动学习或生产自治调度已经完成。现有 Workflow 版本兼容仍关闭。

## 边界和所有权

这里的 **routing recipe（路由配方）** 表示可复用的批准处理方式；现有 LifeRoutine
仍表示定时触发器。两者不共享状态机，不暗中改变已有 Routine API。

| 模块 | 输入和输出、所有权 | 失败行为及正常入口验收 |
| --- | --- | --- |
| Routing lab | Project、批准目录、阈值和模式；保存请求、目录快照与反馈 | 显式创建；同 Project Jev 连接；默认 shadow 不执行查询 |
| 代码规则 | explicit_recipe_id、force_slow | 已批准绑定绕过 Jev；停用/未知绑定升级，不能绕过目录权限 |
| Jev Connector | 脱敏请求、有限 criteria → 选择、置信度、概率、模型及用量 | 复用独立 Connector 队列；错误/低置信度/无匹配升级；没有静默换模型 |
| 快环执行 | 批准 recipe → Project 本地查询结果 | 仅 life.tasks.list / inbox.list；查询失败升级，不执行写操作 |
| 外部 System 2 | escalated 请求 → 处理报告或候选配方 | 外部 Agent 通过公开 API 处理；报告标记 external_agent_report，不自动认定外部事实已验证 |
| 回放和发布 | 候选、已记录案例和期望标签 → 独立回放结果 | 至少两个不同消息，且含未用于提案的 validation 候选正例；通过当前目录回放后才能显式发布 |
| 纠错 | actor、misroute / execution_failed、说明 | 暂停命中配方、增加目录版本、原请求升级；保留旧结果以供慢环修复 |

批准 target 是代码枚举，Jev 不能提供命令、工具名、计划或新权限。当前没有买票、支付、
创建日程等副作用。`inbox.list` 读取持久事实，返回 Worker 来源可用性，不控制运行中 Worker。

默认 confidence_threshold=0.85、probability_threshold=0.8，分别比较模型 confidence
和选中项 probability；两者不是同一指标。返回的选项集合、概率总和、最大概率选择均校验。
请求保存模型、模板 routing-v1、目录版本、完整分布和可用 token 数据，不记录隐藏推理过程。

## 正常使用

启动已有 EHAI API 宿主并创建一个生活 Project；可用非 Git 工作区。
为该 Project 注册独立 Jev 进程，状态目录必须放在仓库外：

```powershell
uv run ehai-jev init --api-url http://127.0.0.1:8000 --project-id '<project-id>' --state-dir 'C:/private/ehai-jev' --model jev-latest
```

从输出取得 connector_id。通过 `POST /api/v1/projects/{project_id}/routing-labs` 创建：

```json
{
  "idempotency_key": "lab-1",
  "name": "生活只读试验",
  "connector_id": "<connector-id>",
  "mode": "read_only",
  "actor": "operator",
  "approved_recipes": [{
    "name": "查看生活待办",
    "applicability": "用户只想查看已有生活待办，不要求新建、修改或完成任务",
    "target": "life.tasks.list"
  }]
}
```

省略 mode 时为 `shadow`：保存判断但不执行查询。请求、推进和结果查询顺序如下：

1. `POST /api/v1/routing-labs/{lab_id}/requests`，提交
   `{"idempotency_key":"request-1","message":"我还有哪些生活待办？","case_role":"learning"}`。
2. `POST /api/v1/routing-labs/{lab_id}/advance`，空 body，将请求放入 Connector 队列。
3. 运行独立 Connector，密钥只从私有文件或 `JEV_API_KEY` 环境变量读取：

```powershell
uv run ehai-jev run --state-dir 'C:/private/ehai-jev' --key-file 'C:/private/typesafe-key.txt' --once
```

4. 再调用 advance，收集判断并查询或升级；GET `/api/v1/routing-requests/{request_id}` 查看结果。

不加 `--once` 的 Connector 持续领取任务；默认 external 模式的 **lab 仍需显式 advance**。
一个 `--once` 只处理一个 Connector 工作项；回放有多个案例时需要处理对应数量的工作项。
API 接受请求不等于已经调用 Jev，HTTP 200 也不等于业务完成。

CLI 使用同一 API，例如：

```powershell
uv run ehai --api-url http://127.0.0.1:8000 create-routing-lab --project-id '<project-id>' --file lab.json --idempotency-key lab-1
uv run ehai --api-url http://127.0.0.1:8000 advance-routing-lab --lab-id '<lab-id>'
uv run ehai --api-url http://127.0.0.1:8000 get-routing-request --request-id '<request-id>'
```

## 慢环、回放和纠错

外部 Agent 列出 requests，处理 `escalated` 请求，再提交 resolve，提供 actor、response、
evidence。核心保存这是外部报告，不会调用第二个内置 Planner。快环成功时无需走这个步骤。

候选通过 candidates 提交 recipe 及 source_request_ids；来源必须为同 lab 的 learning 请求。
保留未用于提案的 validation 请求。回放体包含 candidate_id 和
`cases: [{"request_id":"...","expected_choice":"<recipe-id 或 escalate>"}]`。
回放仅调用 Jev，不执行配方；expected_choice 和 case_role 不发送给 Jev。
当前采用所有案例通过的薄门槛，不能据此推断泛化准确率；新目录版本使旧回放失去发布资格。

| HTTP 路径（均以 /api/v1 开头） | CLI 命令；MCP 将连字符换为下划线 |
| --- | --- |
| GET /routing-labs/{lab_id} | get-routing-lab |
| GET /routing-labs/{lab_id}/requests | list-routing-requests |
| POST /routing-requests/{request_id}/resolve | resolve-routing-request |
| POST /routing-requests/{request_id}/feedback | record-routing-feedback |
| POST /routing-labs/{lab_id}/candidates | propose-routing-recipe |
| POST /routing-labs/{lab_id}/replays | start-routing-replay |
| GET /routing-replays/{replay_id} | get-routing-replay |
| POST /routing-recipes/{recipe_id}/publish | publish-routing-recipe |
| POST /routing-recipes/{recipe_id}/pause | pause-routing-recipe |
| GET /routing-labs/{lab_id}/metrics | get-routing-metrics |

publish 使用 actor、replay_id 和 idempotency_key。协议替身回放另需明确
`allow_protocol_trial=true`，否则拒绝；不将替身当真实 Jev 证据。
所有写操作除 advance 外都有 idempotency_key；重复键返回原收据，不是最新资源视图。
详细输入以 [OpenAPI](../schemas/v1/http-api.openapi.json) 为准；CLI 写入使用 `--file`
和 `--idempotency-key`，MCP 使用 request_json。前端可使用生成的 TypeScript Client。

feedback 可标记 correct、misroute 或 execution_failed，每个请求只接收一份标签。
后两者将原请求转为 escalated，保留旧 result；随后 resolve 的结果以 previous_result
保留它。暂停后的新请求和已排队请求不会执行该配方，候选重新启用也须回放和发布。
人工 pause 保存 actor/reason，发布和暂停均改变目录版本，历史请求快照保持。

## 固定 Pi 回退分支

2026-09-24 增量：已接通正式宿主内的 `escalate → Pi` 分支。它只执行上面的两个只读查询，
没有新增通用 Planner、任意命令或业务写权限。现有 lab 默认为 external，不自动触发模型。

| 模块 | 输入输出与所有权 | 失败行为、正常入口验收 |
| --- | --- | --- |
| 宿主推进 | 显式 pi lab 的排队请求、Connector 回执 → 快环完成或 Pi 回退 | 共用 PlannerCapacity；容量满时保留未领请求，不重复创建调用 |
| Pi 固定回退 | 当前 learning 请求、批准目录和受限事实查询 → 回答、可选单候选及理由 | 结束工具与原生 settled 都确认后才原子保存回答/候选；超时、异常、重启中断保持可见且不自动重试 |
| 自动复测 | 候选和操作者标注的独立 validation → 一次 RoutingReplay | 用例在发起前持久冻结；缺少候选正例就等待；失败不再生成下一轮，发布仍需明确调用 |

宿主复用私有 Pi 配置和模型凭证，新增启动参数：

```powershell
uv run ehai-api --database 'C:/private/ehai/state.sqlite3' --artifacts 'C:/private/ehai/artifacts' --worker fake --planner single --pi-config 'C:/private/ehai/backend.json' --routing-model deepseek-v4.1-flash --routing-reasoning-effort off --planner-capacity 1
```

`--routing-timeout-seconds` 默认300秒。这里 fake/single 仅表示本例没有启动编码 Worker/Planner；
回退实际使用指定的 Pi 模型。也可与已有 Pi Worker/Planner 同宿主，共享 planner-capacity 的
模型准入容量，Worker 槽位保持原义。本轮先支持直接 API 宿主，Workspace Manager 的启动
配置未增加 routing 参数。Pi 安装、models.json 环境变量引用和 Go 配置沿用[使用指南](USAGE.md)。

创建 lab 时设置 `mode:"read_only", fallback_mode:"pi"`。shadow 不允许启用 Pi 查询。
然后用普通 submit 接口登记独立验证请求，设置 `case_role:"validation", force_slow:true`，
再通过以下接口配置验证案例；它们不会交给 Pi，未配置案例不阻止当前请求由慢环回答：

```text
POST /api/v1/routing-labs/{lab_id}/fallback
CLI: configure-routing-fallback --lab-id <UUID> --file <JSON> --idempotency-key <key>
MCP: configure_routing_fallback
TS: configureRoutingFallback(labId, request)
```

```json
{
  "idempotency_key": "fallback-cases-1",
  "mode": "pi",
  "validation_cases": [
    {"request_id": "<independent-inbox-query-id>", "expected_target": "inbox.list"},
    {"request_id": "<write-request-id>", "expected_target": "escalate"}
  ]
}
```

验证标签可为 life.tasks.list、inbox.list 或 escalate，最多19条；来源必须为同 lab 的
独立 validation 请求。候选出现后，宿主添加当前学习请求并把标签映射到具体配方；
必须存在未用于提案的候选正例，否则 `fallback.replay_error` 显示等待原因。
标签是操作者的期望，不作为事实真值或模型训练输入。案例及期望不发送给 Jev。

此后正常提交 learning 请求即可，宿主自动推进，无需手动 advance 或启动某次 Pi。
Jev Connector 进程仍需运行。选择 escalate、低置信、Jev 调用失败、只读执行失败或
force_slow 都进入同一回退入口。Pi 通过 read_routing_context/read_routing_facts 获取事实，
使用 finish_routing_fallback 提交回答和可选候选；文字终止不等于已完成。
请求 `fallback` 保存 attempt/session/model/配置指纹、状态、候选、理由及回放引用。
结果标记 `pi_agent_report`，包含实际查询 facts 和模型答复，不能当作真实生活动作已经执行。

`needs_human=true` 时请求仍 escalated；缺失资料或要求写操作会留待人工/外部处理。
显式绑定独立目标 Project 后，Pi 也可通过 project_change 提出代码或流程定义文件修改，
宿主转交目标核心创建工程 Goal/计划，等待正常批准与执行授权。它不直接写入运行框架，
详情见[Project 修改入口](P4_WORKFLOW_REVISION_DESIGN.md)。
validation 请求不启动 Pi，以免复测样本进入慢环上下文。自动复测使用既有独立 Jev 队列，
只分类不执行业务动作，完成后通过 replay_id 查询 passed/failed。不会自动发布。

每条请求最多一次自动 Pi 调用、一个候选和一次自动复测。失败/中断不会自动再领；
操作者可检查事实后通过原 resolve 接口明确处理。Pi running 时禁止并发手工 resolve/feedback。
宿主重启把未收尾调用标为 interrupted，未领请求可继续。关闭 lab 的 pi 模式只停止新自动
推进，不撤销已经领取的只读调用。沿用单核心数据库单宿主所有权，不支持多进程抢同一数据库。
已保存的复测案例不会因后续配置变化被替换；已有失败回放不会被自动重新采样。

## 实验限制

- SQLite schema22 保存实验文档、幂等收据和事件；Connector 仍使用已有独立队列。
  pending 上限默认 32，计入 queued/routing/escalated，是积压上限而非完整公平并发调度。
- 未启动 Connector 或 external 模式下未 advance 时可能一直等待；没有 Connector worker
  心跳超时或自动重试策略。结果未知时不宣称已完成；read-only 分类在崩溃恢复时可能重新调用，
  因此已保存 token 并不等于完整账单。
- metrics 统计请求和人工标签，按配方归类；provider_judgements 只统计请求判断，
  **不包含 replay 的模型调用**。回放判断和用量从 replay.entries 读取。
- `--test-provider-url` 只接受 loopback HTTP，使用假凭证并标记 protocol_trial；真实
  请求固定使用 TypeSafe HTTPS，model 请求别名和响应实际版本分别保留。
- 没有自动修改 Jev 权重、自动挖掘模式、基准对照、目录回滚到任意历史版本或通用分支图。
  目前通过暂停与重新发布控制生效范围。不能从少量试用推断省钱、提速或准确率提升。

真实试用结果见 [运行证据](EVIDENCE.md#p4-生活事务与外部连接)。

后续自然表达试用中，5条日常提问和3条回放共8次真实 Jev 调用，最终没有快环完成；
Inbox 候选因回放置信度未达门槛而未发布。各请求由外部 Agent 读事实后答复，不能把
外部 Agent 的日期筛选、排序和文字呈现记为快环已有能力。阈值和目录契约未因此修改。

再下一轮使用真实 Pi / Go deepseek-v4.1-flash 提出只读 Inbox 新候选，保持阈值，
通过一次4项独立回放（含写入/复合需求负例）；模拟明确发布后，新请求完成实际 Inbox 查询。
慢模型只看学习证据，Codex 子 Agent 仅模拟用户和监测；此为仓库外薄适配的有界实验，
不是自动慢环调度已上线。事实纠错与未调用结束工具的失败记录也保留在实施记录中。
