# 运行证据

更新：2026-10-04。本页把每次正常入口试用压缩为一行：做了什么、结果、哪些没有覆盖。
能力结论见 [STATUS](STATUS.md)；原始叙述（Run ID、用量明细、临时证据目录）保留在下列归档：

- [RECORD_R2](history/RECORD_R2.md)：核心执行、导入/MCP、Git 整合、后继 Run 与模型工具契约（2026-09-14 ~ 09-27）
- [RECORD_P3](history/RECORD_P3.md)：项目总览、待办、便签、事件消费、项目配置、多工作区（2026-09-21 ~ 09-23）
- [RECORD_P4](history/RECORD_P4.md)：生活 Workflow、Connector、Jev 实验、Pi 回退、Project 转交（2026-09-23 ~ 09-27）
- 更早的试验见 [历史目录](history/README.md)

## 记录规则

- 新证据追加一行；Run ID、用量和临时证据位置等较长叙述写在驱动该工作的计划文档的执行记录中
  （如 [REFACTOR_PLAN](REFACTOR_PLAN.md#执行记录)）。归档记录不再追加。不提交凭证。
- "模型"列写真实 Provider 调用情况；`0` 表示无模型调用。报告 token 不是账单。
- 一次通过只证明该路径；未覆盖范围必须写明，不能由接口存在、HTTP 200 或工具数量推断。
- 薄验证、模型工具试用和隔离能力试用都不是产品 E2E；产品 E2E 见 [REFACTOR_PLAN](REFACTOR_PLAN.md#产品-e2e)。

## 核心执行

| 日期 | 场景 | 模型 | 结果 | 未覆盖 | 原文 |
| --- | --- | --- | --- | --- | --- |
| 09-14 | 真实 Pi 并发编码（Luna） | 真实 | 两个 Work 重叠；代码挂起时文档独立完成，回复后 Reviewer/Gate passed，Run completed | 强杀、完整 handoff、周期纠偏 | [R2](history/RECORD_R2.md#既有代表性证据) |
| 09-15 | Planner 草稿 Gate 冲突自修 | 真实 Go | 重复 Gate 被拒后 remove_node_gate → finish_plan，草稿保存 | — | [R2](history/RECORD_R2.md#既有代表性证据) |
| 09-15 | CLI API 客户端 | 0 | 回环宿主创建/提案/批准/查询、幂等与错误保留 | 真实 Pi 编码 | [R2](history/RECORD_R2.md#既有代表性证据) |
| 09-15 | 外部计划导入与 stdio MCP | 真实 Pi（2 次请求） | 导入 draft、权限不匹配 409 后显式授权启动；MCP 44 工具；模型实际调用 get_runtime_health / create_project | 其余控制工具的模型调用 | [R2](history/RECORD_R2.md#本轮外部导入与-mcp) |
| 09-15 | Windows Git 换行导致 Gate 失败 | 真实 | 定位 core.autocrlf，局部修复后诊断通过；原 Run 触达诊断 token 阈值，停在 paused | 原 Run 完整复测 | [R2](history/RECORD_R2.md#既有-windows-git-换行问题) |
| 09-16 | MCP 默认开放写操作 | 0 | 45 工具；读 Schema、创建项目、幂等重放、创建 Goal | 模型调用 | [R2](history/RECORD_R2.md#2026-09-16-mcp-默认开放写操作) |
| 09-16 | Block 版本与变更清单 | 0 | 修改/删除节点的版本与下游身份变化正确；旧库返回 null | 真实 Pi 过程调整重跑 | [R2](history/RECORD_R2.md#2026-09-16block-版本和变更清单) |
| 09-16 | Git 自动整合 | 0 | 历史 Luna 成果两次整合得到同一 commit，tree 与已验收快照一致 | 冲突路径、MCP 模型调用 | [R2](history/RECORD_R2.md#2026-09-16git-自动整合) |
| 09-16 | 跨批准后继 Run | 真实（源 Run） | 新 Run 零 Worker Attempt 接续旧成果，新人工 Gate 后 completed 并整合 | 基线变化后重执行、多次接续、崩溃注入 | [R2](history/RECORD_R2.md#2026-09-16跨批准后继-run) |
| 09-27 | Pi Planner 超时接线修复 | 诊断 + 真实 Go | 8 秒挂起端点 409 且不重派；真实调用 300 秒按时失败、进程退出 | 上游传输停顿根因 | [R2](history/RECORD_R2.md#2026-09-27pi-planner-超时配置接线) |
| 10-01 | 产品 E2E（scripted Worker） | 0 | 导入、批准、并发与人工挂起、强杀重启、事件补领、人工判定、成果查询；Linux/Windows CI 通过 | 真实 Pi、Git worktree、integrate-run、MCP | [REFACTOR_PLAN](REFACTOR_PLAN.md#执行记录) |
| 10-02 | Pi 角色改经独立 Hub | 0（真实 Pi 0.85.1 + 脚本化 Provider） | 7 个调用场景轨迹与改动前逐事件一致（本机子进程与独立 Hub）；Hub 停止→结果未知不重放；强杀核心 2 秒内 Hub 与 Pi 退出；`ehai-api --worker pi` 经 Worker、Reviewer、最终 Gate 完成 Run | 真实模型、Planner/过程审查/路由回退经 Hub、Windows、跨机器 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-hub-独立服务与-pi-兼容层) |
| 10-02 | 产品 E2E 改经 Hub 与真实 Pi | 0（脚本化模型服务） | 原场景全部通过；每节点经 Pi 调用模型并写入文件，最终 worktree commit 含全部上游文件；破坏兼容层或不写文件时 E2E 失败 | 真实模型、独立 Hub、integrate-run、Pi 非 Worker 角色 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-产品-e2e-经-hub-与真实-pi) |
| 10-02 | Planner 改为与 harness 无关的 PlannerRole | 0（真实 Pi + 脚本化模型服务） | discuss-plan（ask_user）与 propose-plan（raise_note）在 main 与改动后输出一致 | 真实模型、完整建图、replan/过程草稿路径 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-adr-0007-第-5-步plannerrole) |
| 10-02 | Hub 每会话 MCP 端点 | 0（合成 MCP harness） | 工具列表限于本会话，调用经核心执行与记录后返回；关闭后令牌失效，Hub 令牌无权访问 | 真实 MCP harness、并发会话 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-adr-0007-第-7-步每会话-mcp-端点) |
| 10-02 | propose-plan 只得到便签时的 HTTP 500 | 0（真实 Pi + 脚本化模型服务） | 复现 500 后修复：返回 422 `planner_failed` 且便签保存；Hub 不可达时返回 502 `harness_outcome_unknown` | 真实模型 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-adr-0007-第-5-步plannerrole) |
| 10-02 | 幂等回执合并为一张表（schema 22→23） | 0 | 同一个改动前生成的数据库，分别由改动前后的程序重放 25 个操作（含同键不同内容），响应逐字节相同；键的命名空间互不影响；迁移后新命令及重放正常 | 真实旧数据库、路由实验 5 个命令的重放 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-02-第三步幂等回执合并) |
| 10-03 | 真实 Pi 手动验收（导入 → 并发写代码 → Reviewer → 命令与人工 Gate → integrate-run） | 真实 OpenCode Go DeepSeek V4.1 Flash（25 个响应） | Run completed，成果只改两个模块文件，integrate-run 重试得到同一 commit；Reviewer 运行中强杀后 Run 卡住，见下一行 | Pi Planner、Windows、写入中途崩溃 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-03-第四步真实-pi-手动验收与强杀恢复) |
| 10-03 | 强杀后重启卡住的修复 | 真实 OpenCode Go DeepSeek V4.1 Flash（26 个响应） | 修复前：租约未过期时重启，running Attempt 永不恢复；修复后：两个写代码节点运行中强杀并立即重启，旧租约到期时自动中断并重试，Run 完成并整合；恢复受容量限制（脚本化诊断：修复前一次恢复超出容量 2，修复后不超出） | 多次连续崩溃、独立 Hub、Windows | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-03-第四步真实-pi-手动验收与强杀恢复) |
| 10-03 | 真实 Pi Planner 规划 → 执行 → 验收 → integrate-run | 真实 OpenCode Go DeepSeek V4.1 Flash（规划 12 + 执行 34 个响应） | discuss-plan 产出可批准草稿（并行节点、Reviewer、verify.py 与人工判据），Run completed，integrate-run 成功 | 多轮讨论、propose/replan、Windows | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-03-第四步补充pi-planner-规划路径) |
| 10-03 | 规划方式实验：直接规划、提示分层、人工拆 Goal、先粗后细（cdls 导出、EHAI Run 摘要两个任务，11 组，隐藏评分） | 真实 OpenCode Go DeepSeek V4.1 Flash 与 V4 Pro（1753 个响应，按单价约 $4.8） | 直接规划两任务均满分，约 $0.3、14–21 分钟；拆分不提高质量，成本或时长更高；先粗后细的细化失败（过程调整看不到已完成产出）；EHAI 人工拆 Goal 因不可满足的最终 Gate 与越权节点陷入 9 次干预后取消 | 大型任务、其他模型族、Windows | [PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md#2026-10-03-规划实验修复前) |
| 10-04 | Planner 授权与验收约束（能力预览、人工判据不加命令 Gate、同节点 3 次干预暂停） | 真实 OpenCode Go DeepSeek V4.1 Flash（662 个响应，按单价约 $1.3）+ 脚本化诊断 | 复现上一行失败场景：单 Goal 两次各 7 分钟完成、全部 5 组零干预；人工拆 3 个 Goal 全流程满分（修复前 0%、9 次干预）；两任务直接规划回归仍满分；契约只在用户选了命令判据时含命令，节点只用授权命令；暂停逻辑由脚本化诊断验证（第 3、6 次干预暂停，恢复后继续） | 真实模型触发暂停与工具拒绝、跨宿主授权比较、Windows | [PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md#2026-10-04-验收) |

## P3 平台后端

| 日期 | 场景 | 模型 | 结果 | 未覆盖 | 原文 |
| --- | --- | --- | --- | --- | --- |
| 09-21 | 项目总览查询 | 0 | 两个 fake 宿主隔离；CLI/API/MCP（48 工具）结果一致；历史 Pi 库保留接续成果来源 | 真实模型、多仓库真实执行 | [P3](history/RECORD_P3.md#2026-09-21-实施结果) |
| 09-22 | 统一人工待办 | 0 | A 等人工 Gate 时 B completed；CLI 判定后 A completed，列表清空；MCP 50 工具 | 运行期 Worker 回答、干预回复真实闭环 | [P3](history/RECORD_P3.md#2026-09-22-实施与薄验证) |
| 09-22 | 便签 / 事件消费 / 项目配置 | 0 | 便签讨论不解阻、continue 后 completed；宿主重启后批次补领；新旧 Run 规则快照分离；MCP 64 工具 | 任意崩溃时序 | [P3](history/RECORD_P3.md#薄验证与边界) |
| 09-22 | 真实便签→修订→执行→人工验收 | 真实 Go（20 次） | Planner raise_note、revise_plan 草稿、Worker/Reviewer、便签人工 Gate，Run completed；项目更新后真实输入仍为旧规则 | propose_process 路径、任意崩溃恢复 | [P3](history/RECORD_P3.md#2026-09-22opencode-go-真实便签试用) |
| 09-22 | 多工作区管理层与规划容量 | 0 | 两个子核心独立执行、停启后数据保留；容量 201/409/409 后释放；MCP 72/65 工具 | 真实模型并发、强杀 | [P3](history/RECORD_P3.md#正常入口薄验证) |
| 09-23 | 真实多工作区 Pi 并发 | 真实 Go（24 次） | 跨工作区 Planner/Worker/Reviewer 请求重叠，输入只含本项目规则；修复 Git 继承 stdin 阻塞后原 Run 接续完成 | 多 Provider、远程工作区、任意强杀 | [P3](history/RECORD_P3.md#2026-09-23真实多-pi-并发与-windows-git-阻塞修复) |

## P4 生活事务与外部连接

| 日期 | 场景 | 模型 | 结果 | 未覆盖 | 原文 |
| --- | --- | --- | --- | --- | --- |
| 09-23 | 生活 capture/review 与 Routine | 0 | 确认前无待办、批准后落库；停机错过的 Routine 重启补一份；MCP 78 工具 | 拒绝、并发决定、事务中强杀、多调度进程 | [P4](history/RECORD_P4.md#验证记录) |
| 09-24 | Workflow 执行记录迁移（schema 19→20） | 0 | 旧数据与回执保留，旧待确认流程续办，新等待跨重启一致 | 版本兼容运行（生产关闭） | [P4](history/RECORD_P4.md#2026-09-24独立执行记录与关闭的版本兼容代码) |
| 09-24 | Google Calendar Connector（本地替身） | 0 | 四动作、去重；POST 落地但 503 → unknown，reconcile 后只 GET 恢复 | 真实 OAuth 与日历 | [P4](history/RECORD_P4.md#2026-09-24jev-双反馈环只读实验) |
| 09-24 | Jev 双反馈环只读实验 | 真实 Jev（10 次） | 4 项回放通过、发布后快环命中；人为注入纠错后配方暂停、请求回到慢环 | 模型准确率、成本、并发 | [P4](history/RECORD_P4.md#2026-09-24jev-双反馈环只读实验) |
| 09-24 | 日常自然表达模拟 | 真实 Jev（8 次） | 低置信全部升级；候选回放未过门槛，未发布 | — | [P4](history/RECORD_P4.md#2026-09-24自然表达的日常操作者模拟) |
| 09-24 | Pi 慢环提案后复测 | 真实 Go 6 + Jev 5 | 候选 4 项回放通过、明确发布后新问题快环命中 | 自动触发慢环、自动发布 | [P4](history/RECORD_P4.md#2026-09-24pi--deepseek-慢环提出改进后复测) |
| 09-24 | 固定 Pi 回退（正式宿主） | 真实 Go 13 + Jev 9 | 修复 `$defs`/object\|null 严格 Schema 拒绝后，自动升级→Pi 回答→回放→发布→快环 | 强杀、多宿主共库、任意写操作 | [P4](history/RECORD_P4.md#2026-09-24正式宿主的固定-pi-回退分支) |
| 09-25 | 慢环转交 EHAI 克隆 | 真实 Go（65 次） | 运行源码绑定 409、克隆允许；目标 Goal 创建，但目标 Planner 两轮未收敛 | 整仓规划收敛、修改闭环 | [P4](history/RECORD_P4.md#2026-09-25慢环向独立-project-转交代码修改) |
| 09-25 | 慢环转交最小项目 | 真实 Go（37 次） | 生成目标草稿，未授权命令被拒待修订；长流多次中断 | 修改闭环、传输稳定性 | [P4](history/RECORD_P4.md#后续复测独立最小项目) |
| 09-27 | 最小项目修改链路 | 真实 Go（23 个响应） | 修订→批准→Pi 修改单文件→Reviewer→外部行为验证→人工验收，Run completed | 整仓目标、重复 human Check 生成、传输停顿 | [P4](history/RECORD_P4.md#2026-09-27最小目标项目修改链路完成) |

## 已知未决问题

由上述证据暴露、尚未处理的事项（处理后从此处移除并在对应行注明）：

- Planner 可能生成语义重复的 human Check（09-27）。
- 目标为整个 EHAI 仓库时，Planner 调查范围过大、未收敛（09-25）。
- 较长 Provider 工具流间歇停顿/中断，根因未确认（09-25、09-27）。
- 干预回复的真实模型闭环只有 10-03 一例（恢复时打开的外部影响干预，回复后新的 Reviewer Attempt 完成）；运行中 Worker 请求路径已于 10-02 删除。
- 写入中断与长链接续的恢复未系统验证。强杀：产品 E2E 覆盖人工等待期间的强杀重启；10-03 的真实运行覆盖 Worker 与 Reviewer 运行中的强杀（修复后重启的宿主在旧派发租约过期后自动恢复，最长等待 5 分钟）。
