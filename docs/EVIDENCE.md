# 运行证据

更新：2026-10-22。本页把每次正常入口试用压缩为一行：做了什么、结果、哪些没有覆盖。
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
| 10-03 | 规划方式实验：直接规划、提示分层、人工拆 Goal、先粗后细（cdls 导出、EHAI Run 摘要两个任务，11 组，隐藏评分） | 真实 OpenCode Go DeepSeek V4.1 Flash 与 V4 Pro（1753 个响应，按单价约 $4.8） | 直接规划两任务均满分，约 $0.3、14–21 分钟；拆分不提高质量，成本或时长更高；先粗后细的细化失败（过程调整看不到已完成产出；审查另因工具 Schema 无法运行）；EHAI 人工拆 Goal 因不可满足的最终 Gate 与越权节点陷入 9 次干预后取消 | 大型任务、其他模型族、Windows | [PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md#2026-10-03-规划实验修复前) |
| 10-04 | Planner 授权与验收约束（能力预览、人工判据不加命令 Gate、同节点 3 次干预暂停） | 真实 OpenCode Go DeepSeek V4.1 Flash（662 个响应，按单价约 $1.3）+ 脚本化诊断 | 复现上一行失败场景：单 Goal 两次各 7 分钟完成、全部 5 组零干预；人工拆 3 个 Goal 全流程满分（修复前 0%、9 次干预）；两任务直接规划回归仍满分；契约只在用户选了命令判据时含命令，节点只用授权命令；暂停逻辑由脚本化诊断验证（第 3、6 次干预暂停，恢复后继续） | 真实模型触发暂停与工具拒绝、跨宿主授权比较、Windows | [PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md#2026-10-04-验收) |
| 10-04 | 过程审查工具被 Pi 严格模式拒绝 | 真实 Pi 0.85.1 + 脚本化模型；真实 DeepSeek V4.1 Flash 下复现 3 次 | 修复前 `review-process` 在发出模型请求前失败（对象联合不受支持），被报告为结果未知；修复后 Reviewer 请求到达模型 | 真实模型完成审查并应用草稿 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-04-过程审查工具被-pi-严格模式拒绝) |
| 10-04 | 过程调整读取已完成成果（先粗后细回放） | 真实 OpenCode Go DeepSeek V4.1 Flash（12 组，1912 个响应，按单价约 $5.1） | 12 组草稿均读取成果并 ready（修复前 8 次便签称产出缺失）；审查侧补 `block_changes` 与 Check 状态后，修复齐全且先决定 Gate 的 2 组审查通过、应用细化并满分完成；此前 Q6、Q8 亦通过 | 大型任务、其他模型族、多次调整、Windows；暂停时不能决定人工 Gate | [PROCESS_OUTPUTS_DESIGN](PROCESS_OUTPUTS_DESIGN.md#执行记录) |
| 10-22 | 关闭严格采样后的真实模型规划 → 执行 → 验收 → integrate-run（Pi 1.3.0，DeepSeek 官方端点） | 真实 DeepSeek `deepseek-flash`（2 次运行共 60 个响应，Pi 估算约 $0.06） | discuss-plan 产出两个并行写代码节点 + 只读 Reviewer，verify.py 通过（附加 7/7），人工验收后 completed，integrate-run 只改两个模块文件、无冲突；第二次运行 55 次工具调用（Planner 22 次，含 add_plan_node/add_plan_edge/set_plan_phase/set_final_gate 等嵌套对象参数）零错误；第一次运行 43 次调用中 1 次语义错误（workspace_write 未设 overwrite 得到 already_exists，下一次即改正），零 `invalid_arguments`。第一次运行因测试仓库没有 .gitignore，两个节点的 `__pycache__` 冲突使 Reviewer 无法启动（见下方未决问题），补 .gitignore 后全新重跑 | 过程审查、路由与轨迹审查工具；强杀恢复；Windows；独立 Hub；较大任务下的参数出错频率 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-22-默认关闭严格采样核心统一校验工具参数) |
| 10-22 | 默认关闭严格采样，核心按工具 Schema 校验参数 | 0（真实 Pi 1.3.0 + 脚本化模型服务） | 直接调用 `ToolExecutor`：合法参数执行，缺字段/多字段/类型错/嵌套错误均返回 `invalid_arguments` 并逐处列出、处理器未运行，无效 Schema 构造失败；产品 E2E 方案下工具不再带严格标记，声明与未声明 strict 的端点都完成 | 真实模型参数出错频率与纠正；Planner、过程审查、路由工具只做静态核对；经 MCP 的其他 harness | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-22-默认关闭严格采样核心统一校验工具参数) |
| 10-21 | Pi 从 0.85.1 升级到 1.3.0 | 0（真实 Pi 1.3.0 + 脚本化模型服务） | 只改锁定版本时产品 E2E 超时：Pi 默认认为自定义 OpenAI 兼容端点不支持严格模式，EHAI 工具要求严格采样，Pi 发出请求前即拒绝（脚本化服务收到 0 个请求）；按用户决定改为优先而非强制严格模式：未声明的端点退回普通工具调用并完成，声明时工具仍带 `strict: true` 并完成；`inspect-agent` 控制通道通过；内置 OpenCode Go DeepSeek 自带声明 | 真实模型、Windows、独立 Hub、Planner/过程审查等 E2E 未经过的角色 | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-21-pi-升级到-130) |
| 10-05 | 人工 Check 决定后保持暂停（`--hold`） | 真实 Pi + 脚本化模型；真实 OpenCode Go DeepSeek V4.1 Flash（2 组，310 个响应，按单价约 $0.85） | 决定与暂停同一事务，保持期间无派发、Gate 未结算；恢复后结算并继续；先粗后细一组应用细化后满分且无中断 Attempt，另一组审查拦下遗漏义务后按原过程满分 | 多次保持、Windows（并行 Attempt 运行时拒绝 hold） | [REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-05-人工-check-决定后保持暂停) |

## P3 平台后端

| 日期 | 场景 | 模型 | 结果 | 未覆盖 | 原文 |
| --- | --- | --- | --- | --- | --- |
| 10-22 | 应用外壳第 3 步真实模型验收：页面添加文件夹 → 讨论 → 批准 → 执行 → 整合 → 新分支 | 真实 DeepSeek `deepseek-flash`（Planner 20 次、Worker/Reviewer 40 次工具调用） | 首次因 `ehai init` 默认 medium 被 Pi 换级、核心拒绝而在 0.8 秒失败，页面只见“结果未知”；改为默认不指定思考级别并在错误中附 Hub 原因后，全新环境重跑通过：四节点方案（并行两节点 → 汇合 verify → Reviewer），Run completed，整合 2 个文件，分支 `ehai/run-1387f806` 上 `python3 verify.py` 输出 ok，用户仓库 HEAD/工作树不变，约 2 分 40 秒 | 系统通知与桌面文件夹选择器实跑、人工 Gate/待办、整合冲突、Windows | [应用外壳](APP_SHELL_DESIGN.md#2026-10-22-第-3-步补齐与真实模型验收) |
| 10-22 | 整合结果持久化、整合列表与 diff 读取，线程页差异查看 | 0（真实 Pi + 脚本化模型） | 产品 E2E 1 passed：integrate 得到 integrated，重试同一 integration_id，列表一条，diff 与宿主补丁一致且未截断，时间线一条 integration_created（refs 另带 event_id）。本机管理器登记脚本化 Pi 工作区后，浏览器中批准并开始 → 人工 Gate 通过 → 整合成果 → 按文件显示差异，刷新后从时间线卡片重开同一结果，手机宽度为底部面板 | 冲突与冲突后重试、超过 1 MiB 截断、MCP、旧版本未持久化的整合、真实模型 | [Git 自动整合](USAGE.md#git-自动整合) |
| 10-22 | 模块 B 本地引擎与用户配置 | 0 | macOS 临时 EHAI_HOME：init→up→status→add 临时 Git 仓库→doctor→down；重启恢复登记、重复 up/add/down 通过；doctor 如实报告终端/守护进程缺凭证，UI 静态页 200；审查回退了停止超时改为不结束子进程的改动（failed 状态下可再次启动导致双进程）；独立复跑同一流程，并在页面打开批准确认框核对 Pi 执行配置 | 真实模型、凭证齐全、交互确认、活动任务停止冲突、Windows/Linux 实跑、浏览器交互 | [验证记录](WORKSPACE_MANAGER.md#模块-b-验证记录2026-10-22) |
| 10-22 | 文件夹添加与整合分支后端 | 0（产品 E2E 使用真实 Pi + 脚本化模型） | 产品 E2E 1 passed：ehai/e2e 指向整合 commit，同名重复 created=false，用户 HEAD/status 不变，时间线一条 integration_branch_created；macOS 临时 home：init/up、curl local=true、三同名 Git 目录 ready（-2/-3）、重复路径 409 带 ID、非 Git/子目录/缺失目录 422、down；无 local-home 的手动管理器 local=false、添加 409；MCP 暴露三新工具、两个写 Schema可读、local 查询与重复路径错误透传通过 | 前端、真实模型、Windows/Linux 实跑、整合冲突、已有分支指向不同 commit、MCP 成功创建分支/新增工作区与模型调用均未验证 | [命令与结果](WORKSPACE_MANAGER.md#文件夹添加与整合分支后端验证2026-10-22) |
| 10-22 | 线程读模型（应用外壳模块 A）与线程式 Web 页面 | 0（真实 Pi + 脚本化模型；页面用 fake Worker） | 产品 E2E 1 passed：创建/草稿/运行/人工 Gate/完成各阶段的 phase 与 next_action、时间线条目、分页与 404。审查中修正全零 UUID 得到 422、逐 Goal 全量扫描事件（改为一次扫描的归属索引）和 run_status_changed 缺少 refs.status。本机管理器 + fake Worker 工作区在浏览器中走通新建线程 → 规划失败显示（single Planner 不支持讨论，409）→ 导入方案 → 批准并开始 → 人工 Gate 通过 → 完成；整合因该宿主无 Git 整合后端返回 409 并如实显示 | 多 Goal、讨论/便签归属分支、管理器离线工作区、MCP 调用、Pi 工作区的执行配置展示与整合、真实模型均未验证 | [用法与取值](USAGE.md#线程查询) |
| 09-21 | 项目总览查询 | 0 | 两个 fake 宿主隔离；CLI/API/MCP（48 工具）结果一致；历史 Pi 库保留接续成果来源 | 真实模型、多仓库真实执行 | [P3](history/RECORD_P3.md#2026-09-21-实施结果) |
| 09-22 | 统一人工待办 | 0 | A 等人工 Gate 时 B completed；CLI 判定后 A completed，列表清空；MCP 50 工具 | 运行期 Worker 回答、干预回复真实闭环 | [P3](history/RECORD_P3.md#2026-09-22-实施与薄验证) |
| 09-22 | 便签 / 事件消费 / 项目配置 | 0 | 便签讨论不解阻、continue 后 completed；宿主重启后批次补领；新旧 Run 规则快照分离；MCP 64 工具 | 任意崩溃时序 | [P3](history/RECORD_P3.md#薄验证与边界) |
| 09-22 | 真实便签→修订→执行→人工验收 | 真实 Go（20 次） | Planner raise_note、revise_plan 草稿、Worker/Reviewer、便签人工 Gate，Run completed；项目更新后真实输入仍为旧规则 | propose_process 路径、任意崩溃恢复 | [P3](history/RECORD_P3.md#2026-09-22opencode-go-真实便签试用) |
| 09-22 | 多工作区管理层与规划容量 | 0 | 两个子核心独立执行、停启后数据保留；容量 201/409/409 后释放；MCP 72/65 工具 | 真实模型并发、强杀 | [P3](history/RECORD_P3.md#正常入口薄验证) |
| 09-23 | 真实多工作区 Pi 并发 | 真实 Go（24 次） | 跨工作区 Planner/Worker/Reviewer 请求重叠，输入只含本项目规则；修复 Git 继承 stdin 阻塞后原 Run 接续完成 | 多 Provider、远程工作区、任意强杀 | [P3](history/RECORD_P3.md#2026-09-23真实多-pi-并发与-windows-git-阻塞修复) |
| 10-06 | Web 工作台逐页试运行（管理器 `--ui-dir` 同源托管，home/dev 两个 fake 工作区） | 0（fake Worker；Jev 为本机协议替身） | 生活：录入（直接保存与待确认）、批准、完成/撤销、编辑、定时启停与修改、回顾记录；工作：人工 Gate“通过并暂停”后恢复至 completed、轨迹；问：快环选中配方并返回待办，纠错暂停配方，慢环处理结果回显；学习：提出候选→3 案例回放通过→确认替身后发布；工作区停止/启动；375px 宽无横向溢出，深色正常；产品 E2E 通过。发现并修复：时间按字符串排序（偏移不同）、回放刷新后丢失（新增 listRoutingReplays） | 真实模型、真实 Jev、真实 Connector、Windows、多浏览器并发 | [ADR 0008](adr/0008-web-workbench.md) |
| 10-09 | PR 评审：待处理面板的便签决定原先固定列出四种动作和「检查是否通过」，多数组合会被核心拒绝；改为宿主按 Notes 的同一套校验，为每个可用决定列一项 decide-note（arguments 带 action 和人工 Check 续办的 passed），页面只渲染这些 | 0（fake 环境） | 只有目标的便签只给关闭和修订方案；来源 Run 已结束的便签只剩关闭；未列出的 continue 写入仍返回原错误；页面上关闭便签成功 | 绑定人工 Check / Intervention 的便签（当前环境无待决请求）；暂停 Run 上的 propose_process | [ADR 0008](adr/0008-web-workbench.md) |
| 10-08 | Web 工作台执行图改为嵌入页面的画布组件：从左到右按依赖最长路径分列，阶段为列标题，跨列连线经独立通道绕开节点，探索/条件连线为虚线并标注分支名或条件；节点为带状态图标和耗时的卡片，悬停时高亮相连的线；画布限高，拖动平移、⌘/Ctrl+滚轮或双指缩放、看全图、全屏（Esc 关闭），两侧有更多内容时渐隐，打开时滚到第一个未完成的节点；点节点进入最新一次尝试的轨迹 | 0（同上真实 Run 数据库，只读） | 9 节点三阶段的真实 Run（含一处分叉与汇合）正确分列；在浏览器内改写该 Run 的执行图响应，加入运行中、失败、卡住、已放弃、待执行节点和探索/条件分支，打开即定位到运行中节点；拖动、看全图（居中）、全屏与 Esc、悬停高亮逐项点过；375px 宽无横向溢出；深色模式核对。上一版（从上到下的整页长图）用户认为不美观，改为本版。修复过程中发现并修复：画布高度未扣除横向滚动条导致出现纵向滚动、缩小后图停在左上角、过长的「收到」消息显示原始预览 JSON | 含分支的真实计划（目前真实数据没有分支，只用改写的响应看过）；数十个节点的大图；触屏双指缩放只在桌面模拟过 | [ADR 0008](adr/0008-web-workbench.md) |
| 10-08 | Web 工作台 Attempt 轨迹页：执行图节点、轨迹列表、求助记录、待处理节点行均可跳转；新增 list-run-attempts、get-attempt-trace | 0（复制的 10-04 真实 Run 数据库，只读） | 10 个 Attempt 的 Run：原 get-trace 合计 512 条后截断，第 6–10 个 Attempt 的事件全部缺失；逐个读取得到 178/152/93/71/77/93/128/49/61/99 条，均未截断。被打断的第 1 次与成功的第 2 次可互相切换；只看报错筛出唯一一次 apply_patch 报错；补丁参数按原文显示。发现并修复：超过 8KB 的工具返回被替换为预览后丢了 call_id，与调用配不上对且被误算为报错（改为从预览中读回 call_id 和内容）；375px 无横向溢出 | 待处理面板的跳转链接未在浏览器点到（当时无待处理项，仅类型检查）；运行中 Attempt 的实时刷新 | [ADR 0008](adr/0008-web-workbench.md) |
| 10-08 | Web 工作台对照可用性原则（Nielsen 十条）与中文文案规范（Ant Design）复查，改写全部页面文案 | 0（同一 fake 环境） | 从今天输入两件事→录入页已拆成两项，再点按行拆开不重复，默认直接保存 2 项；问页不再显示路由与置信度，答错后显示已转交；已暂停配方可在学习与发布重新回放（3 案例，因另一在用配方覆盖同类请求而未通过，页面显示配方名）；记录、定时、设置、工作首页逐页核对；375px 宽 12 个页面无横向溢出。发现并修复：录入不带入今天的文字、自己录入也默认要再确认、按行拆分覆盖已填事项、暂停的配方页面无法恢复、回放与反馈显示 ID 和英文枚举、错误先显示状态码 | 重新发布未走到成功（回放如实未通过）；真实模型、真实 Jev | [ADR 0008](adr/0008-web-workbench.md) |

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

- 目标仓库没有忽略构建产物时，并行节点各自运行测试产生的未跟踪文件（如 `__pycache__/*.pyc`）进入候选，整合时互相冲突，下游 Reviewer 启动即失败；该失败是确定性的，却在约 2 秒内被自动重试 5 次耗尽预算后才转人工（10-22）。
- Planner 可能生成语义重复的 human Check（09-27）。
- 目标为整个 EHAI 仓库时，Planner 调查范围过大、未收敛（09-25）。
- 较长 Provider 工具流间歇停顿/中断，根因未确认（09-25、09-27）。
- 干预回复的真实模型闭环只有 10-03 一例（恢复时打开的外部影响干预，回复后新的 Reviewer Attempt 完成）；运行中 Worker 请求路径已于 10-02 删除。
- 写入中断与长链接续的恢复未系统验证。强杀：产品 E2E 覆盖人工等待期间的强杀重启；10-03 的真实运行覆盖 Worker 与 Reviewer 运行中的强杀（修复后重启的宿主在旧派发租约过期后自动恢复，最长等待 5 分钟）。
