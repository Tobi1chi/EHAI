# 自定义 Workflow 执行记录

本页只追加执行与验证叙述，不是当前指南；结论以 [STATUS](../STATUS.md) 与对应设计、用法文档为准。

## 执行记录

### 2026-11-03 自定义 Workflow 第一版（11-03 计划第 6 步）

实现取舍，均按 11-03 已定决定“有限无环单路径图；动作/条件/人工/等待/结束；先存意图再发请求；
Routine 绑定流程版本”落地，未扩大范围：

- 定义保存为 Project 内不可变版本（`workflow_definitions`，schema 23→24），同名再保存需给出最新
  `expected_revision`。Run 的 `definition_snapshot` 带完整定义，执行只读快照，不回查定义表。
- 数据引用只能读每条路径都先经过的步骤（按支配关系判断），保存时拒绝；因此重新汇合后读不到只在
  某一分支执行的步骤输出。需要分支各自输出时用不同的结束步骤，或在汇合前分别处理。
- 写动作授权二选一：`approval_step`（人工步骤支配该动作，且其拒绝/超时出口到达不了该动作）
  或 `standing_write`。批准绑定进入人工步骤时冻结的 subject，示例流程的预订输入引用
  `steps.approve.subject.*`，执行的就是批准的车次与价格。价格/有效期的再确认由 Connector 自身校验
  （例如预订动作以批准价为上限），引擎不为特定服务加分支。
- 动作步骤与 Connector 调用排队同在一个 SQLite 事务；结果由调度 tick 轮询调用状态后应用，
  未在 Connector 回传事务里回调 Workflow，保持两个服务的边界。未知结果进 Inbox
  （新增 `workflow_reconciliation` 类别），沿用既有 reconcile，不自动重试写入。
- Connector 失败与引用/类型/Schema 错误使 Run `failed`，不选择业务分支；第一版不提供失败出口或取消 Run。
- Routine：旧记录缺少新字段时按 `life.review` 默认值读取，语义不变；自定义绑定要求显式
  `missed_run_policy`，不把回顾的合并规则默认用于订票；上一 Run 未结束时跳过本次触发。
  `skip` 的 60 秒宽限是为每秒一次的 tick 留余量。
- 人工步骤沿用 `DecideWorkflowRequest`（expected_version 为 Run 版本），走新端点
  `step-decisions`，固定流程的 `decisions` 端点与响应不变。

验证（仓库外临时驱动，未提交；0 次模型调用）：真实 `ehai-api`（fake Worker）+ CLI 写入，
桥接接口上的 Connector 替身（查票、预订、写日程、通知四个动作），32 项核对全部通过：
动作目录；保存时拒绝无授权写动作、循环、引用后续步骤；版本重放与 409；
查票 → 条件 → 人工（Inbox、过期版本 409）→ 预订 → 写日程 → 等待 → 通知 → booked；
等待期间停止并重启宿主后等待到期继续；通知回传 unknown 后进 Inbox，reconcile 后同一调用核对完成，
没有第二次执行；价格超限走 no_suitable_train；拒绝走 user_declined 且不预订；查票 503 使 Run failed；
不符 inputs_schema 的输入 400；Routine 绑定版本 1、错过 3 次只运行一次、上一 Run 等待时跳过；
无绑定 Routine 仍生成 life.review；固定流程列表不含自定义 Run。另用一个 schema 23 的既有数据库
验证迁移到 24。场景中修复了一处问题：CLI 路径参数表缺 `definition_id`，`start-custom-workflow`
无法传入版本 ID，已补上后重跑通过。

自审修正（评审机器人额度用尽，由作者自审）：模板引用校验改为整串匹配，末尾带换行的占位符保存时即拒绝，
不再到运行时才失败；`eq`/`ne` 两侧都是数字时按数值比较（`1` 与 `1.0` 相等），其余仍按 JSON 形式比较；
排队时 Connector 拒绝写授权（定义保存后 Connector 改变）也使该 Run 失败，不再让异常中断整次 tick、
卡住其他 Run。复跑同一驱动，新增两项检查（换行引用被拒、`1.0` 等于 `1`）在内全部通过。

未覆盖：真实票务/日历/通知服务、事件等待与事件触发、多宿主共库并发推进、Connector 长时间不回传、
Web 页面。产品 E2E 未扩展（主路径未变），仍为编码场景。
