# Application Architecture

`ehai.application` 编排 Command、Query 和领域转换，通过 Port 使用持久化、Artifact、Check 和 Worker。
CLI、HTTP、MCP、工作台和未来 Routine 复用这些用例，不各自实现执行逻辑。
产品边界见 [Product Scope](../../../docs/PRODUCT_SCOPE.md)，完成证据见 [STATUS](../../../docs/STATUS.md)。

| 区域 | 文件 | 职责 |
| --- | --- | --- |
| 事务与查询 | [service.py](service.py)、[commands.py](commands.py)、[queries.py](queries.py) | Command 幂等、事务入口与查询投影 |
| 规划与导入 | [planner.py](planner.py)、[planning_dialogue.py](planning_dialogue.py)、[plan_graph_tools.py](plan_graph_tools.py) | 讨论、方案版本和共享图构建契约；Planner 不派发 Worker |
| 执行语义 | [orchestrator.py](orchestrator.py)、[evaluation.py](evaluation.py)、[checks.py](checks.py) | 候选、节点/分支推进、Check/Gate 与 Checkpoint |
| 调度与宿主 | [scheduler.py](scheduler.py)、[async_runtime.py](async_runtime.py)、[runtime_control.py](runtime_control.py) | Endpoint、容量、隔离与租约、持久派发和执行事件 |
| Agent Harness | [agent_roles.py](agent_roles.py)、[agent_contracts.py](agent_contracts.py)、[agent_trace.py](agent_trace.py) | 角色提示、工具契约、结果与轨迹；模型循环归 Pi |
| 过程调整 | [process_adjustments.py](process_adjustments.py)、[process_reviews.py](process_reviews.py) | 批准范围内的提案、审查与过程应用 |
| 人工控制 | [run_control.py](run_control.py)、[interventions.py](interventions.py) | Run 控制、定向挂起和人工回复 |
| 接续与恢复 | [checkpointing.py](checkpointing.py)、[phase_sessions.py](phase_sessions.py)、[run_results.py](run_results.py) | 恢复依据、逻辑阶段上下文与成果查询 |
| 边界与 Port | [execution_policy.py](execution_policy.py)、[sanitization.py](sanitization.py)、[ports.py](ports.py) | 执行授权、数据清理、仓储和外部服务接口 |

Orchestrator 决定业务状态，Scheduler 分配资源，Connector 返回执行观察；三者不互相替代。
候选已持久化的中间交接不等于最终 Gate 通过或 Run 完成，成果必须绑定实际执行来源。
过程变更、跨批准后继 Run 和成果接续遵循 [执行模型](../../../docs/EXECUTION_MODEL.md)。

新增总览与待办只聚合核心事实；回复、批准和恢复仍使用原对象对应的应用命令。
正常入口及失败诊断规则见 [开发指南](../../../docs/DEVELOPMENT_GUIDELINES.md)。
