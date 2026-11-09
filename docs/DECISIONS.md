# 决定索引

更新：2026-11-09。本页是产品与计划决定的**唯一索引**：一行一条，写决定是什么、落在哪份文档、现在是否有效。
完整论证留在“落点”所指的文档；被取代的决定不删，只标明被什么取代。
设计内部的小选择写在各设计文档的“决定”一节，架构决定见 [adr/](adr/)，二者不在此重复。

现状只用四个值：**有效**；**已完成**（一次性的计划或步骤，已做完）；**已被取代**（写明取代者）；**实现已放弃**（需求或决定仍在，代码没有保留）。

## 待决

| 事项 | 出处 |
| --- | --- |
| ⑤ 改用 catbus 命令行查询 12306，是否满足“浏览器或 computer use”的完成条件 | [ROADMAP 推进规则](ROADMAP.md#推进规则2026-11-07-定) |
| Hub 跨机器运行时工作区如何到达 [E42] | [PRODUCT_SCOPE 第 5 节](PRODUCT_SCOPE.md#5-agent-harness) |
| 重复人工判定的幂等重放返回当前 Run 状态而非原回执；如需原回执语义需单独设计 | [归档的重构计划](history/REFACTOR_PLAN.md#待决事项) |

## 决定（新的在前）

| 日期 | 决定 | 落点 | 现状 |
| --- | --- | --- | --- |
| 11-09 | Codex 不按确切 CLI 版本设门限；harness `version` 为 `app-server`，实测 CLI 版本只记录、不进入指纹，自动更新不改变授权与会话指纹；协议不兼容时重新核对 | [HUB Codex 兼容层](HUB.md#codex-兼容层) | 有效（取代 11-06 X2 的固定版本约束） |
| 11-07 | 保留原生工具的 harness（Codex）：原生命令与文件改动逐个经核心按 HostTools 授权审批；核心只去掉被替代的写工具与 `shell_*`，读取工具、精确 argv 的 `command` 与 `git` 保留（最初也去掉，试用中 Worker 因此无法读取工作区） | [ADR 0007 第 11 条](adr/0007-agent-harness-port.md) | 有效 |
| 11-07 | 放弃 11-04 至 11-05 未提交的全部实现（卡片工作台、内置助手及其终端命令与生活待办、访问级别、一键初始化、移除工作区、快环改用 Decisions、连接器随引擎运行），从 `origin/main` 重新开始；需求保留，代码备份在本地分支 `backup/2026-11-07-uncommitted` | [ROADMAP 现在](ROADMAP.md#现在) | 有效 |
| 11-07 | 11-04 那条实现路线整体放弃，只保留对话中确认下来的决定；一键初始化 [E54]、三档访问级别 [E55]、助手运行终端命令 [E62] 不再是目标；卡片工作台与助手由阶段 ② 按 11-05 决定重新设计 | [ROADMAP 11-04 路线的处理](ROADMAP.md#11-04-路线的处理) | 有效 |
| 11-07 | 以长程目标推进：Claude 带 Codex 连续构建阶段 ③ → ① → ② → ④ → ⑤，每个阶段做到可验收，只在必须由用户来时停下 | [ROADMAP 现在](ROADMAP.md#现在) | 有效 |
| 11-07 | 先整理文档：一类结论只放一处，执行记录移到 `records/`，完成的计划归档到 `history/` | [docs/README](README.md) | 有效 |
| 11-07 | 推进规则：只在必须由用户来时停下；一个逻辑改动一个提交，全部阶段做完后开一个 PR；每阶段按量费用不超过 ¥50 | [ROADMAP 推进规则](ROADMAP.md#推进规则2026-11-07-定) | 有效 |
| 11-07 | ① 的真实任务是 RSS Connector；④ 的数据源是 RSS/网页订阅；⑤ 用 catbus 查询 12306 并整理方案，不下单 | [ROADMAP 推进规则](ROADMAP.md#推进规则2026-11-07-定) | 有效 |
| 11-07 | 先验收并提交 11-04 工作台改动，再开始 ③ | — | 已被取代（同日决定放弃这些改动） |
| 11-06 | X1：保留 Codex 原生工具，受 EHAI 授权约束（sandbox、审批、轨迹记录） | [CODEX_HARNESS_DESIGN 第 2 节](CODEX_HARNESS_DESIGN.md#2-已定的决定2026-11-06用户) | 有效 |
| 11-06 | X2：控制接口用 `codex app-server`（stdio），仅限用户本机自用，固定版本 | [CODEX_HARNESS_DESIGN 第 2 节](CODEX_HARNESS_DESIGN.md#2-已定的决定2026-11-06用户) | 有效（固定版本约束已被 11-09 决定取代） |
| 11-05 | 按产品点清单重写 PRODUCT_SCOPE，现有的点全部保留，只排序归并（N16） | [PRODUCT_SCOPE](PRODUCT_SCOPE.md) | 有效 |
| 11-05 | C1：页面内置助手是唯一的对话入口，经 Hub 运行在用户选的 harness 上；用户自己的 Codex 会话也能经 MCP 使用同一套能力和轨迹 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效 |
| 11-05 | C2：默认由 EHAI Planner 规划，运行在用户选择的 harness 上；导入外部方案照常可用 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效 |
| 11-05 | C3：生活与工作合成一个注意力队列，只作标签 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效（取代 10-06 的两区页面） |
| 11-05 | C4：路由实验（双反馈环）降为隐藏的实验；Decisions 改做 Workflow 里的 Connector 动作，按数据流批量、并行筛选（S5） | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效（取代 09-24 的快慢环用途） |
| 11-05 | C5：立刻适配 Codex/GPT，先做录制/回放（S2）；同日决定先不做 Claude Code，移出计划，现有兼容层保留 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效（取代 11-03 “第二个 harness 为 Claude Code”） |
| 11-05 | C6：到期的生活待办经桌面外壳发系统通知 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效（取代 E63 的“不发提醒”） |
| 11-05 | C7：不依赖 Git 工作区的 Agent 委托是阶段 ⑤ 的前置工作 | [产品点 D 节](PRODUCT_POINTS.md#d-用户决定2026-11-05) | 有效 |
| 11-05 | S1：一个授权/决定原语覆盖工作与生活；S3：助手只查询结构化事实再用富元素回答 | [产品点 S 节](PRODUCT_POINTS.md#s-产品经理的建议11-05-用户已同意见第-d-节) | 有效 |
| 11-05 | 阶段顺序 ③ → ① → ② → ④ → ⑤（S4 按 C5 调整），完成一个再进下一个 | [ROADMAP 阶段](ROADMAP.md#阶段) | 有效 |
| 11-04 | 试用反馈四项的方案：一键初始化 Git、三档访问级别、工作台内置助手、卡片工作台 | [归档设计](history/WORKBENCH_ASSISTANT_DESIGN_1104.md) | 实现已放弃（11-07）；需求见 PRODUCT_SCOPE |
| 11-04 | 第二、三、四轮：移除工作区、精简助手输入框、助手运行终端命令（只读以外由用户批准）、助手管理生活待办 | [归档设计](history/WORKBENCH_ASSISTANT_DESIGN_1104.md#执行记录) | 实现已放弃（11-07） |
| 11-04 | 快环从 Jev 改用 OpenAI Decisions（E81） | [PRODUCT_SCOPE 第 6 节](PRODUCT_SCOPE.md#6-生活待办流程与外部服务) | 实现已放弃（11-07）；用途已被 C4 改变 |
| 11-03 | 一次走完八步计划：路线图同步 → 小决定落地 → 应用外壳第二轮 → ADR 0007 第 6、8 步 → P4 自定义 Workflow → P2 加固 → 真实验收清单；P4 暂停解除 | [归档路线图](history/ROADMAP_BEFORE_1107.md#11-03-推进计划) | 已完成 |
| 11-03 | 桌面 App 凭证只继承登录 shell 环境，不做钥匙串，EHAI 不保存凭证 | [归档路线图](history/ROADMAP_BEFORE_1107.md#11-03-已定的架构决定) | 有效 |
| 11-03 | 开 PR 不进核心：核心止于创建分支，推送和开 PR 归用户或外部 Agent | 同上 | 有效 |
| 11-03 | `ehai doctor --probe` 经 Hub 真实启动一次会话，核对模型与思考级别被接受，不调用模型 | 同上 | 有效 |
| 11-03 | 做终端交互会话：无参数 `ehai` 进入交互模式，基于线程接口 | 同上 | 有效 |
| 11-03 | 第二个 harness 为 Claude Code：原生 MCP、可关闭内置工具 | 同上 | 已被取代（11-05 C5） |
| 11-03 | 执行配置为 `harness: {kind, version, settings}`，旧 Pi 形状可读且指纹不变；能力清单由兼容层声明、Hub 在 `/v1/health` 汇总，调度按能力匹配 | 同上 | 有效 |
| 11-03 | 删除 `--worker-timeout-seconds`；scripted 宿主下 `--execution-config` 可选；幂等键冲突统一为 409 `conflict`；便签命令重放返回原回执 | 同上 | 有效 |
| 11-03 | 自定义 Workflow 第一版：有限无环单路径图，步骤为动作/条件/人工/等待/结束；调用先存意图再发请求；Routine 绑定流程版本 | 同上 | 有效 |
| 10-22 | 线程成为面向用户的一级对象；单一守护进程入口；Tauri 桌面外壳 | [APP_SHELL_DESIGN](APP_SHELL_DESIGN.md) | 有效 |
| 10-22 | P4 新功能继续暂停 | [归档路线图](history/ROADMAP_BEFORE_1107.md#当前顺序) | 已被取代（11-03 解除） |
| 10-06 | P3.3 工作台先做生活/工作两区与共用页面；System 1 暂以路由实验配方呈现 | [ADR 0008](adr/0008-web-workbench.md) | 已被取代（两区由 11-05 C3 取代，路由由 C4 取代） |
| 10-03 | P2 收尾完成；下一步选 P3.3 薄工作台 | [归档的重构计划](history/REFACTOR_PLAN.md#四步顺序) | 已完成 |
| 10-01 | 暂停 P4 新功能；按四步完成安全网、低风险清理、大文件拆分，再以真实 Pi 验收收尾 P2 | [归档的重构计划](history/REFACTOR_PLAN.md) | 已完成 |
| 10-02 | 不重命名代码或接口，文档按术语表区分 Run 与 Workflow Run | [docs/README 术语](README.md#术语) | 有效 |
| 09-25 | 慢环可把代码或流程修改交给明确绑定的独立 Project，走正常计划与批准；不改运行中的框架，不自动部署 | [P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md) | 有效（属于已降为隐藏实验的双反馈环） |
| 09-24 | Routine 面向订票、行程和日程提醒；下一目标为自定义 Workflow、条件分支与可扩展 Connector（先后端） | [PRODUCT_SCOPE 第 6 节](PRODUCT_SCOPE.md#6-生活待办流程与外部服务) | 有效 |
| 09-24 | Jev 双反馈环实验：快环在批准的只读配方目录内处理熟悉请求，慢环处理升级；允许固定 `escalate → Pi` 回退 | [DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) | 已被取代（11-05 C4 降为隐藏实验） |
| 09-23 | 先推进独立生活 Project 的日常事务与 Workflow/Routine 后端，再集中做前端 | [归档决定沿革](history/PRODUCT_SCOPE_DECISIONS_BEFORE_1105.md#渐进交付边界09-22-起) | 已完成 |
| 09-22 | 后端优先：P3.2 便签 → P3.4 事件消费 → P3.5 配置 → P3.3 页面 | 同上 | 已完成 |
| 09-16 | 核心优先、增量交付平台；Personal Dashboard 只是选取想法的来源 | [PRODUCT_SCOPE 第 8 节](PRODUCT_SCOPE.md#8-交付原则) | 有效 |
| 09-16 | 0.1.0 为首个正式版本；Token/费用硬限额暂缓 | [STATUS 版本](STATUS.md#版本) | 有效 |
