# 当前能力与验证

更新：2026-10-04。每项能力一行：实现状态、关键边界、证据位置。
证据摘要见 [EVIDENCE](EVIDENCE.md)，产品边界见 [PRODUCT_SCOPE](PRODUCT_SCOPE.md)，后续顺序见 [ROADMAP](ROADMAP.md)。

**状态含义**：`真实验证` 有真实模型的正常入口证据；`无模型验证` 只有 scripted/fake 或协议级证据；
`部分` 主路径可用但列出的边界未验证；`未实现`、`暂缓`、`关闭` 按字面理解。
接口存在、工具数量或 HTTP 200 不算验证。

## 版本

0.1.0 是首个正式版本（2026-09-16 决定），公开范围是规划与执行核心、Pi 后端及 CLI/HTTP/MCP 入口。
它不表示 P2 全部目标已验收；这里记录版本定位，不代表已有 Git tag 或远端 Release。

## 核心执行

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| Pi 规划 / 执行 / 审查 | 真实验证 | 同图并发、挂起回复、Reviewer/Gate completed | [核心](EVIDENCE.md#核心执行) |
| Planner 授权与验收约束 | 真实验证 | 规划上下文含 Worker 能力预览；只给人工判据时最终 Gate 不带命令；同节点每 3 次干预暂停 Run（暂停仅脚本化诊断验证）；预览不随方案持久化 | [设计](PLANNER_CONSTRAINTS_DESIGN.md#执行记录) |
| Pi 规划调用时限 | 真实验证 | 超时失败并结束进程，不自动重放；不解决上游传输停顿 | [核心](EVIDENCE.md#核心执行) |
| 外部计划导入 | 真实验证 | 只支持尚未规划的 Goal；拒绝伪造状态/批准 | [核心](EVIDENCE.md#核心执行) |
| CLI API 客户端 / MCP | 部分 | MCP 默认开放全部操作；只有少数工具做过模型调用 | [核心](EVIDENCE.md#核心执行) |
| 周期轨迹审查 / 定向挂起 | 部分 | 审查只给建议，需显式采纳；不是自治纠偏 | [RECORD_R2](history/RECORD_R2.md#本批交付) |
| Agent 后端 | 部分 | 只有 Pi；Codex 已删除；Planner 与 harness 无关；Hub 已有每会话 MCP 端点（仅合成客户端验证）；能力清单与执行配置形状未实现 | [ADR 0007](adr/0007-agent-harness-port.md) |
| Agent harness Hub | 无模型验证 | 所有 Pi 角色经独立 `ehai-hub`（默认本机子进程）；Worker 路径由产品 E2E 在 Linux/Windows CI 覆盖，未用真实模型；跨机器需同路径 | [HUB](HUB.md) · [核心](EVIDENCE.md#核心执行) |
| 阶段上下文 | 部分 | 逻辑 Phase Session 共享；物理 Pi Session 按 Attempt 隔离 | [EXECUTION_MODEL](EXECUTION_MODEL.md#实例与会话) |
| 批准内过程调整 / Block 清单 | 真实验证 | 清单只描述变化，不批准、不授权复用成果；草稿可读已完成成果，先粗后细在中型任务上端到端通过（10-04）；暂停时不能决定人工 Gate | [核心](EVIDENCE.md#核心执行) |
| Git 自动整合 | 无模型验证 | 用历史真实成果整合；冲突路径未实跑 | [核心](EVIDENCE.md#核心执行) |
| 跨批准后继 Run | 真实验证 | 单次接续通过；基线变化重执行、多次接续、崩溃未验证 | [核心](EVIDENCE.md#核心执行) |
| 长任务可靠性 | 部分 | Worker/Reviewer 运行中强杀后可恢复（重启后等旧租约过期，最长 5 分钟，10-03 真实验证）；写入中断、长链接续未系统验证 | [未决问题](EVIDENCE.md#已知未决问题) |
| Token / 费用硬限额 | 暂缓 | 2026-09-16 决定暂不实现；用量记录保留 | — |
| 产品 E2E | 无模型验证 | 真实 Pi 经本机 Hub、Git worktree，模型为脚本化服务；不覆盖真实模型、独立 Hub、integrate-run、MCP | [REFACTOR_PLAN](REFACTOR_PLAN.md#产品-e2e) |
| CI | 无模型验证 | 静态检查、两平台 mypy、契约比对、TS 构建、Linux/Windows E2E | [REFACTOR_PLAN](REFACTOR_PLAN.md#执行记录) |
| P2 总验收 | 真实验证 | 导入路径真实手动验收通过，含强杀恢复；Pi Planner 规划路径补充验收通过，未含强杀（单个小仓库、macOS，10-03）；Windows、多轮讨论与 replan 未做 | [REFACTOR_PLAN](REFACTOR_PLAN.md#四步顺序) |

## 平台后端（P3）

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| P3.1 项目总览 | 无模型验证 | 按来源宿主查询，不做跨宿主聚合 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.2 统一人工待办 | 无模型验证 | 人工 Gate 闭环通过；类型为干预、人工验收、便签与生活确认（运行中 Worker 请求已删除） | [P3](EVIDENCE.md#p3-平台后端) |
| P3.2 便签 | 真实验证 | 便签→修订→执行→人工验收通过；propose_process 路径未验证 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.3 薄 Web 工作台 | 未实现 | 后端先行，页面单独设计 | [ROADMAP](ROADMAP.md#p3从多项目管理到日常工作台) |
| P3.4 事件消费 | 无模型验证 | 重启后补领与确认通过；不自动唤醒、不执行业务动作 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.5 项目配置与 Run 快照 | 真实验证 | 更新只影响新 Run；不是可编辑角色库 | [P3](EVIDENCE.md#p3-平台后端) |
| 多工作区管理 / Planner 容量 | 真实验证 | 本机同模型；无跨工作区迁移、动态借用或多 Planner 共编 | [P3](EVIDENCE.md#p3-平台后端) |

## 生活事务与外部连接（P4）

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| 生活 capture / review 与 Routine | 无模型验证 | 两个固定流程、固定间隔；不是通用 Workflow 引擎 | [WORKFLOWS](WORKFLOWS.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 执行记录 | 无模型验证 | schema 19→20 迁移与续办通过；历史轨迹不补造 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 版本兼容 | 关闭 | 代码接口存在，生产无适配器、无启用开关 | [WORKFLOWS](WORKFLOWS.md#执行记录) |
| 自定义 Workflow / 条件分支 / 事件触发 | 未实现 | 只有目标设计 | [P4_WORKFLOW_DESIGN](P4_WORKFLOW_DESIGN.md) |
| Connector HTTP 协议 | 无模型验证 | 本地分进程试用；结果未知需显式核对 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md) |
| Google Calendar Connector | 无模型验证 | 只有本地替身；真实 OAuth/日历未授权验证 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Jev 双反馈环实验 | 真实验证 | 只读、显式推进；不是自动学习或通用 Workflow | [DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) |
| 固定 Pi 回退 | 真实验证 | 显式启用；故障保留不重试；发布仍需明确决定 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 独立 Project 代码修改转交 | 部分 | 最小项目修改闭环通过；整仓规划收敛与传输稳定性未解决 | [P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 生活日程 / 经验固化 / 扩展生态 | 未实现 | P5 候选，未排期 | [ROADMAP](ROADMAP.md#p5真实使用后再立项) |

## 下一步

[重构与推进计划](REFACTOR_PLAN.md) 的四步（安全网 → 低风险清理 → 拆分大文件 → P2 收尾）已于 10-03 完成。
下一步是 P3.3 薄工作台（[ROADMAP](ROADMAP.md#当前顺序)）；P4 新功能继续暂停。
