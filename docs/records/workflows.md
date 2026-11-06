# 生活 Workflow 执行记录

本页只追加执行与验证叙述，不是当前指南；结论以 [STATUS](../STATUS.md) 与对应设计、用法文档为准。

## 执行记录

每个 Workflow Run 拆分为独立的运行、步骤、调用与等待记录（SQLite schema 20 起），
通过 `get-workflow-execution` 查询；返回 data.run、steps、invocations、waits 及当前宿主
compatibility 状态，来自同一只读数据库快照。读取不会创建记录或触发适配。

| 记录 | 内容 | 语义 |
| --- | --- | --- |
| Workflow Run | Project 归属、定义版本/快照、inputs/result/trigger、状态和来源 | 原生活 Workflow DTO 由它投影，只有一个状态来源 |
| 步骤 | 定义节点 node_id 与该次 step_execution_id 分离，执行序号、输入输出、等待/完成/跳过 | 与业务写入在同一事务保存；人工决定沿用原步骤 ID |
| 调用 | invocation_id 关联 Workflow Run 和步骤，动作契约、幂等身份、结果与种类 | 固定流程记录本地业务动作；自定义流程的 Connector 调用先存意图，结果含 connector_call_id 与外部操作引用 |
| 等待 | wait_id 关联 Workflow Run 和步骤，等待条件与明确决定收据 | 人工等待跨重启可读；完成后原 wait_id 标记 resolved |

schema 19 的旧运行在首次打开时迁移，标记 `recording_origin=legacy_snapshot`，
不补造当时没有记录的定义全文和步骤轨迹；之后发生的决定与本地动作才写入子记录。
新运行为 `native`，保存内置定义快照和摘要；步骤 recorded_at 是事务观察时刻，不是精确耗时。

版本兼容模块（`application/workflow_compatibility.py`）已有代码接口，生产固定关闭：
compatibility 应为 `{ "enabled": false, "registered_adapters": 0 }`，
CLI/API/环境变量/Routine 都没有启用入口，不能声称跨版本适配已完成运行验收。

