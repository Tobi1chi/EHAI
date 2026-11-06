# Planner 的授权与验收约束

> 设计 · 状态：已实现（2026-10-04）。执行记录见 [records/planner-constraints](records/planner-constraints.md)。

状态：已实现（2026-10-04，用户确认 A、B、C 一起做）。起因与验收见下文[执行记录](records/planner-constraints.md)，
摘要见 [EVIDENCE](EVIDENCE.md#核心执行)。

## 观察到的问题

2026-10-03 的规划实验（真实模型，cdls 与 EHAI 两个任务、11 组）中，规划质量本身没有问题；
失败集中在"人工拆分子目标"一组，原因是 Planner 不把授权与验收当作约束：

1. **不可满足的最终 Gate。** 子目标的判据只有 `human:<问题>`，Planner 却按提示词
   "If host_check_configuration contains an explicit command argv, use that exact argv"
   把宿主的 `verify_task.py` 设为最终 Gate；`PlanProposalBuilder` 发现 Gate 有命令而判据没有
   `command:exit-zero`，静默补上该判据（`application/planner.py`）。该命令覆盖子目标范围之外的功能，
   注定不通过，Agent 只能请求人决定。
2. **越权节点。** Planner 看不到 Worker 的授权命令（规划上下文只有规划输入、项目配置和宿主检查命令），
   安排了"编写并运行探测脚本"的取证节点；Worker 无权运行，反复 `report_blocked`。
3. **干预循环。** 每次回复后节点自动恢复、重新派发，同一阻塞再出现，9 次干预、10 次中断后人工取消。

## 时间线与两套命令

规划 → 批准方案 → 授权执行配置（`start-run --authorize`）→ 执行。

| | Worker 授权命令 | Gate / Check 命令 |
| --- | --- | --- |
| 来源 | 执行配置 `allowed_commands`、`available_shells`、`git_permissions` | 宿主 `--command-check-argv`；Planner 在方案中写的 Gate argv |
| 把关 | 授权执行配置时 | 批准方案时（`get-plan-checks`） |
| 执行 | Worker 工具按精确 argv 匹配 | 宿主 Check Runner 运行，不受 Worker 授权约束 |

同一宿主上 `start-run` 的执行配置必须与宿主配置一致（否则 409），因此宿主配置就是该宿主上
Run 实际会获得的 Worker 权限。

## 决定

### A. 最终 Gate 不得静默扩大用户判据

- 用户给出明确判据且其中没有 `command:exit-zero` 时，最终 Gate 不能带命令：
  `PlanGraphToolRuntime` 新增开关，`set_final_gate` 带 argv 时拒绝，并返回问题说明
  （用 `argv=[]` 加判据要求的人工问题；需要更多验证时写进设计说明，由用户决定）。
- Planner 只在"判据包含 `command:exit-zero`"或"讨论未给判据"时使用宿主检查命令；提示词据此改写。
- 未给判据的讨论、导入计划、过程模式不受影响；`PlanProposalBuilder` 的补判据逻辑保留给这些路径
  （未给判据时由最终 Gate 推断判据，是既有约定）。
- 节点 Gate（阶段 Gate）不在本次范围：它们是方案的一部分，批准时可见。

### B. 规划上下文包含 Worker 能力预览

- 宿主有 Pi 执行配置时，`PlannerRole` 的上下文新增 `worker_capability_preview`：允许的精确命令、
  可用 shell、Git 权限、工作节点能否写工作区，以及 Reviewer 的限制（无写入工具、无 shell、
  仅 `git.read`；授权命令仍在其工作区执行，任何文件改动都会使其提交失败）。开启 shell 时，工作节点可经 shell 运行任意命令行，精确命令只约束 shell 之外的直接执行。
  没有执行配置的宿主不提供该字段。
- 提示词新增规则：节点只能依赖预览中的能力，不得要求 Worker 运行预览之外的命令、解释器或临时脚本；
  目标需要缺失的能力时，在设计中说明并用 `raise_note` 请用户决定，而不是安排注定阻塞的节点。
  预览是事实说明，不是授权；"Gate 命令不赋予 Worker 执行权"的既有规则保留。
- 初次规划、讨论、重新规划与过程草稿都提供预览（同一宿主的 Run 使用同一配置）。
- 不在本次范围：把预览随方案持久化、在 `start-run` 时比较预览与授权（同一宿主上二者相同，跨宿主时再做）。

### C. 同一节点反复请求人工帮助时暂停 Run

- `block_attempt` 打开干预时统计该节点在本 Run 中的干预次数；距该节点上次因此暂停又累计 3 次（含本次）、
  Run 仍在运行且没有其他运行或排队的 Attempt 时，暂停 Run，新增内部暂停原因 `repeated_intervention`，
  事件载荷带 `plan_node_id` 与 `intervention_count`，原因文字说明"普通回复没有解除阻塞；
  请给出明确决定、调整过程或取消，然后显式恢复 Run"。
- 干预照常打开并可回复；Run 暂停时回复不会隐式恢复执行（既有语义），需要 `resume-run`。
  恢复后计数继续累积，再累计 3 次时再次暂停。
- 直接暂停只在 Run 空闲时才等于"已排空"（批准内过程调整据此发布新过程）；若当时仍有其他 Attempt 在运行，
  推迟到之后第一次空闲的干预再暂停（审查返工路径的同一原则）。
- 暂停原因不在公开 Schema 中，不改对外契约；`repeated_intervention` 不进入自动过程调整的资格列表。

## 验收

- 静态检查、产品 E2E 通过；Schema 无差异。
- 仓库外诊断（不提交）：
  - A：图工具在开关打开时拒绝带命令的最终 Gate，关闭时行为不变。
  - C：脚本化模型每次都 `report_blocked`，自动回复后第 3 次干预时 Run 暂停，`resume-run` 后继续。
- 真实模型回放（OpenCode Go `deepseek-v4.1-flash`，用户提供的测试凭证）：
  - 复现失败场景：EHAI 仓库、只做查询服务层的子目标、只给人工判据、Worker 只有 7 条命令。
    期望契约中没有命令判据、方案中没有越权节点，子目标完成。
  - 回归：cdls 导出任务直接规划（判据 `command:exit-zero`），隐藏评分仍为满分。

## 执行记录

执行与验证记录已移到 [planner-constraints.md](records/planner-constraints.md)。

## 不做

- 自动分解或"先粗后细"规划（实验表明中型任务无此需要；过程调整看不到已完成产出的缺口另行记录）。
- 子目标级别的验收命令（宿主只有一个全局检查命令，是否由项目配置承载另行决定）。
