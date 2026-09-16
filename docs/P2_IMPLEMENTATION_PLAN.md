# P2 当前实施计划

更新：2026-09-16。旧逐日增量移至 [历史记录](history/P2_IMPLEMENTATION_PLAN.md)。
范围由 [产品定位](PRODUCT_SCOPE.md)、[执行模型](EXECUTION_MODEL.md) 定义。

| 工作项 | 当前实现 | 待收尾 |
| --- | --- | --- |
| R1 规划/批准 | 讨论、设计、版本、批准、外部导入、共享图校验 | 外部导入不覆盖已有批准；修订沿明确流程 |
| R2 执行/成果 | Pi、并发、隔离、Reviewer/Gate、过程接口 | 更完整恢复和长任务证据 |
| R3 失败/介入 | stalled/suspended、reply、审查采纳与定向挂起 | 后继接续；不默认自治纠偏 |
| R4 入口/验收 | CLI 本地及 API 模式、MCP、Schema/TS Client | 唯一产品 E2E 和当前版本总验收 |

2026-09-16：过程编译新增 block 稳定标识、版本和变更清单，保存在草稿候选及已应用
过程快照中，通过原查询入口返回。后续据此接续成果与整合 Git；本批不实现后继 Run
的批准/启动事务。Token/费用硬限额按用户决定暂缓，不作为本批交付项。

不继续自研 Responses Adapter、模型循环或压缩。顶层 Agent 在 EHAI 外；Planner 可选。
每项变更须说明用户结果、输入输出、所有权、失败和正常入口验证。
以 [STATUS](STATUS.md) 汇总现状，[本轮记录](R2_IMPLEMENTATION_PLAN.md) 保存证据；
不再用旧记录中的“尚未接线”重复开发已实现的能力。
