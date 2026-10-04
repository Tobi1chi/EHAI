# Planner 的授权与验收约束

状态：已实现（2026-10-04，用户确认 A、B、C 一起做）。起因与验收见下文[执行记录](#执行记录)，
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
  可用 shell、Git 权限、工作节点能否写工作区，以及 Reviewer 的限制（只读工作区、无 shell、
  仅 `git.read`）。开启 shell 时，工作节点可经 shell 运行任意命令行，精确命令只约束 shell 之外的直接执行。
  没有执行配置的宿主不提供该字段。
- 提示词新增规则：节点只能依赖预览中的能力，不得要求 Worker 运行预览之外的命令、解释器或临时脚本；
  目标需要缺失的能力时，在设计中说明并用 `raise_note` 请用户决定，而不是安排注定阻塞的节点。
  预览是事实说明，不是授权；"Gate 命令不赋予 Worker 执行权"的既有规则保留。
- 初次规划、讨论、重新规划与过程草稿都提供预览（同一宿主的 Run 使用同一配置）。
- 不在本次范围：把预览随方案持久化、在 `start-run` 时比较预览与授权（同一宿主上二者相同，跨宿主时再做）。

### C. 同一节点反复请求人工帮助时暂停 Run

- `block_attempt` 打开干预时统计该节点在本 Run 中的干预次数；达到 3 次（含本次）且 Run 仍在运行时，
  按 Goal 预算耗尽的先例暂停 Run，新增内部暂停原因 `repeated_intervention`，
  事件载荷带 `plan_node_id` 与 `intervention_count`，原因文字说明"普通回复没有解除阻塞；
  请给出明确决定、调整过程或取消，然后显式恢复 Run"。
- 干预照常打开并可回复；Run 暂停时回复不会隐式恢复执行（既有语义），需要 `resume-run`。
  恢复后计数继续累积，再次达到 3 的倍数时再次暂停。
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

所有真实模型调用都用 OpenCode Go `deepseek-v4.1-flash`（Pi 0.85.1），成本按 Pi 模型配置的单价
（每百万 token 输入 $0.22、输出 $0.66、缓存读 $0.007）由会话用量算出，不是账单。
评分器对 Agent 不可见；"人工判据"由驱动脚本自动通过。临时证据在会话 scratchpad，未提交。

### 2026-10-03 规划实验（修复前）

两个任务：cdls 仓库新增三种导出格式（隐藏评分：69 个回归测试与每种设计、每种格式的逐项检查），
EHAI 仓库新增 Run 摘要查询（隐藏评分：静态检查、CLI/HTTP/本地模式/OpenAPI/MCP 行为、范围）。
宿主检查命令为任务自带的 `verify_task.py`，Worker 只有 7 条精确命令。

| 任务 | 组 | 方式 | 得分 | 总时长 | 成本 | 干预 |
| --- | --- | --- | --- | --- | --- | --- |
| cdls | D1、D2 | 直接规划 | 100%、100% | 20.0、21.1 分钟 | $0.27、$0.30 | 0 |
| cdls | M1 | 人工拆成 3 个 Goal | 100% | 49.5 分钟 | $0.53 | 1 |
| cdls | P1、P2 | 先粗后细（过程调整细化） | 100%、100% | 35.6、22.6 分钟 | $0.43、$0.31 | 1、0 |
| cdls | X1 | 直接规划（V4 Pro） | 100% | 25.0 分钟 | $0.63 | 0 |
| EHAI | D1、D2 | 直接规划 | 100%、100% | 14.3、19.4 分钟 | $0.28、$0.35 | 0 |
| EHAI | G1 | 提示按层分阶段 | 100% | 26.5 分钟 | $0.46 | 0 |
| EHAI | M1 | 人工拆成 3 个 Goal | 0%（取消） | 29.3 分钟 | $0.42 | 9 |
| EHAI | X1 | 直接规划（V4 Pro） | 100% | 21.7 分钟 | $0.81 | 0 |

结论：中型任务直接规划已满分且最便宜；拆分不提高质量。P2 的细化草稿失败，原因是过程调整
看不到已完成节点的产出（另见 PROCESS_OUTPUTS_DESIGN）；P1 的过程审查失败当时被报告为结果未知，
10-04 查明是审查工具 Schema 被 Pi 严格模式拒绝，另行修复。EHAI M1 的失败即本页"观察到的问题"。

### 2026-10-04 验收

- 静态检查（ruff、format、mypy Linux/Windows、lint-imports）、Schema 生成无差异、产品 E2E 通过。
- 诊断 A：开关打开时 `set_final_gate` 带命令被拒（`FINAL_COMMAND_NOT_SELECTED`），只带人工问题被接受；
  判据映射：无判据或含 `command:exit-zero` 时允许命令，只有人工判据时不允许。
- 诊断 C：脚本化模型每次 `report_blocked`、驱动自动回复；第 3、6 次干预时 Run 暂停
  （`RunPaused` 的 `pause_cause=repeated_intervention`，`intervention_count` 为 3、6），
  暂停期间回复不产生新 Attempt，`resume-run` 后继续。
- 真实模型回放（修复后，同一基线、同一 7 条命令、同一评分器）：

| 组 | 场景 | 得分 | 总时长 | 成本 | 干预 | 契约判据 |
| --- | --- | --- | --- | --- | --- | --- |
| F1、F2 | 复现 EHAI M1 的第 1 个 Goal（只做查询层，只给人工判据） | 100%、100%（静态检查全过，只改 `queries.py`） | 7.7、7.2 分钟 | $0.11、$0.09 | 0 | 只有人工判据 |
| M | EHAI M1 全流程（3 个 Goal） | 100%（修复前 0%、取消） | 31.4 分钟 | $0.52 | 0（修复前 9） | 前两个 Goal 只给人工判据，契约也只有人工判据；第 3 个给 `command:exit-zero`，使用宿主命令 |
| E | EHAI 直接规划（回归） | 100% | 18.5 分钟 | $0.29 | 0 | 命令判据保留 |
| D | cdls 直接规划（回归） | 100% | 21.3 分钟 | $0.30 | 0 | 命令判据保留 |

  - A：只给人工判据的 Goal 契约中不再出现命令判据；给了 `command:exit-zero` 的回归组仍使用宿主命令。
    Planner 会话中没有出现 `FINAL_COMMAND_NOT_SELECTED`：提示词改写已足够，工具拒绝是兜底，只由诊断 A 验证。
  - B：所有组的 Planner 会话都收到 `worker_capability_preview`；节点只要求 7 条授权命令。
    修复前第 1 个 Goal 的方案有 3 个节点，其中"在本地 fake-worker 宿主上为 get_run_summary 取运行时证据"
    注定阻塞，契约是"人工 + 命令"；修复后同一 Goal 只有实现与 Reviewer 两个节点、只有人工判据。
    F2 的节点写明"不运行 `python -c` 或临时脚本"，M 的 HTTP Goal 写明"不做 HTTP 运行时探测（宿主没有提供相应命令）"，Reviewer 避开会改写
    `schemas/` 的命令。
  - C：本轮没有节点达到 3 次干预，未触发暂停；只由诊断 C 验证。

## 不做

- 自动分解或"先粗后细"规划（实验表明中型任务无此需要；过程调整看不到已完成产出的缺口另行记录）。
- 子目标级别的验收命令（宿主只有一个全局检查命令，是否由项目配置承载另行决定）。
