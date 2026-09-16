# 执行模型

更新：2026-09-16。用户/外部 Agent → Planner 或外部导入 → 校验草稿 → 明确批准 →
执行配置授权 → Orchestrator/Scheduler → Worker → Reviewer/Check/Gate → 成果查询。

## 实例与会话

WorkerProfile 表达角色、模型、能力与 Session 策略；Endpoint/Connector 承载实际执行。
Worker 不等于节点、模型或连接器。EHAI API 常驻，Pi 按角色调用启动 RPC 子进程。
进程退出不等于删除会话记录。当前逻辑 Phase Session 共享，物理 Pi Session 按 Attempt 隔离；
不能描述成多个 Worker 共用一个可写工作区或同一物理上下文。

## 阶段与验收

计划支持分支、依赖汇合、有序 Phase 和 Reviewer Gate。跨阶段不能绕过前阶段 Gate。
最终节点使用最终 Gate，不重复附加节点 Gate。Reviewer 提供证据，宿主执行自动/人工判定。
候选、handoff、节点完成、Gate 通过和 Run 完成是不同事实，检查结果绑定实际成果版本。

草稿 Gate 可设置/替换/明确删除；提交时必需条件必须完整。
过程模式保留原 Gate/Check，只能整体移动归属，不能删改冻结条件或虚构完成状态。

## 状态与控制

- pending：正常依赖等待。
- stalled：允许有限、安全的自主恢复，不授权无限重试。
- suspended：必须人工回复/决定，整 Run resume 不自行解除。
- Attempt 保留真实成功、失败、中断或未知结果；不复制节点状态成为另一套 Agent 生命周期。

独立任务可在兄弟节点挂起时继续。人工介入带目标、原因、证据和 request token；
reply-intervention 只解决对应问题，不扩权。周期审查默认 600 秒或 30 步先到触发，
仅建议；显式 suspend-attempt 需匹配 Attempt、review_id 和覆盖序号，不自动采纳。

批准内过程调整使用同一 Run 的提案/独立审查/应用路径。
过程版本保存 block_changes：稳定 block_id、版本、前后执行节点与字段变化；
下游输入变化仍产生新执行身份。它描述修改/删除，不自行批准或授权 Git 成果复用。
旧版本未记录的清单为 null；不会从标题推断历史。使用细节见 Usage 的 Block 变更清单。
改变批准边界后，approve-plan 可逐项记录旧未决事项的明确处置，start-run 显式绑定
paused 前驱与来源/目标成果映射；授权、Run、接续记录和派发意图在同一事务保存。
适用的旧成果跳过 Worker 执行但重新经过新 Gate；输入或 Git 基线变化则重新执行。
旧 Run/问题保留历史原貌，来源关系和操作者决定记录在新 Run，旧 Run 不再恢复执行。
已有 Goal Worker Attempt 预算累计沿用，Token/费用硬限额暂缓。

## 隔离与恢复

并行写入使用 EHAI-owned Git worktree，按依赖整合有效成果，不重标来源身份。
宿主 Git 命令关闭隐式 core.autocrlf 转换，避免机器设置改变候选字节；
显式 .gitattributes 和用户直接运行 Git 的规则仍适用，不修改全局配置。

有效 handoff/已完成上游成果是恢复依据；未知副作用不能靠换 Session 盲目重放。
跨批准成果接续已有真实 Pi 成果与新人工 Gate 的试用证据；强杀、写入中崩溃和长链恢复
仍不属于已验证保证。integrate-run 可从固定基线自动整合有效完成成果，冲突保留待处理。
本地 execute-plan 是前台宿主；HTTP/MCP 客户端退出不取消 API 宿主的任务。
实际命令和配置见 [Usage](USAGE.md)，验证范围见 [STATUS](STATUS.md)。
