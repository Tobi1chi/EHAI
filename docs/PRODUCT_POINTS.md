# 产品点清单（PRD 重写的底稿）

更新：2026-11-07。状态：用户已核对，冲突决定见第 D 节。本文只汇总现有文档里已经写下的产品点，加上 11-05 讨论中用户新提的点，
并列出两者之间的冲突，作为重写 PRD 的底稿。它不改变任何现有决定，也不说明能力是否已实现（实现情况以 [STATUS](STATUS.md) 为准）。

11-07 补注：本文是 PRODUCT_SCOPE 中 `[编号]` 的来源，不是现行指南；决定的现状见 [DECISIONS](DECISIONS.md)。
E53–E55、E60–E63 与 E81 出自 11-04 至 11-05 的实现，这些实现未提交、已于 11-07 放弃；其中 E54、E55、E62 连需求一起放弃，其余需求由阶段 ② 重新设计；
出处里的 WORKBENCH_ASSISTANT_DESIGN 现归档为 [history/WORKBENCH_ASSISTANT_DESIGN_1104](history/WORKBENCH_ASSISTANT_DESIGN_1104.md)。

规则（用户 11-05 要求）：重写 PRD 时，现有的点一条都不删、不改含义，只排序和归并；有冲突的写进第 C 节，由用户决定。

- **E 编号**：现有文档中的点。出处写到文件与小节，措辞做了压缩，以出处原文为准。
- **N 编号**：11-05 讨论中用户新提的点，用用户的意思写。
- **S 编号**：产品经理的建议，用户还没有确认。
- **C 编号**：冲突或缺口，需要用户决定。

## A. 现有产品点

### A1 定位与边界

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E1 | EHAI 是通用 Agent 平台的规划/执行核心；编码是第一个场景，不是永久边界 | PRODUCT_SCOPE 开头；AGENTS |
| E2 | 常驻顶层 Agent 在 EHAI 之外，经 CLI/MCP 使用核心，不需要迁入 EHAI，也不等于 Planner | PRODUCT_SCOPE 开头；ADR 0006 |
| E3 | 目标：同时委托多个项目，离开后仍能推进，回来直接查看成果、处理必要决策 | PRODUCT_SCOPE 产品方向 |
| E4 | 价值看可信成果、人工介入负担和恢复能力，不看 Agent 活动量或新增模块数量 | PRODUCT_SCOPE 产品方向 |
| E5 | 项目是目标、任务、成果和执行边界；跨项目总览帮助找到当前工作 | PRODUCT_SCOPE（借鉴 PAO） |
| E6 | 统一的“需要我处理”入口：写明原因、证据、可选动作、影响和处理后的恢复路径 | PRODUCT_SCOPE（借鉴 PAO） |
| E7 | 结果先于轨迹：先显示已验证成果、当前进展和未决问题，原生会话与日志按需展开 | PRODUCT_SCOPE（借鉴 PAO） |
| E8 | 后续工作从有效成果、决定和 handoff 继续，减少重新解释与重复探索 | PRODUCT_SCOPE（借鉴 PAO） |
| E9 | 外部 Agent 通过事件消费连接核心；固定事务逐步交给 Workflow/Routine | PRODUCT_SCOPE（借鉴 PAO） |
| E10 | 旧 PAO 的全量范围、代码、测试门和状态机不自动成为 EHAI 的要求 | PRODUCT_SCOPE 产品方向 |
| E11 | Agent Runtime 外置：EHAI 不维护模型循环、Responses Adapter 或上下文算法，所有角色都用现成 Agent | ADR 0005 |
| E12 | Project 表示业务归属，workspace 表示执行环境，两者不是一个概念 | PRODUCT_SCOPE 渐进交付 |

### A2 规划与批准

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E13 | 两条入口：委托 EHAI Planner 生成方案和图，或导入外部 Agent 生成的方案和图；绕过 Planner 不等于绕过校验、Gate、批准和授权 | PRODUCT_SCOPE 核心与上层 |
| E14 | 方案须说明需求、范围/排除项、接口、任务输入输出、依赖、Phase/Reviewer、Gate、权限与失败处理 | PRODUCT_SCOPE 核心与上层 |
| E15 | 批准固定需求、对外接口、Gate 和授权；批准内的过程变化可追踪地进行，改变批准边界必须回到用户 | PRODUCT_SCOPE；EXECUTION_MODEL |
| E16 | 外部计划不能携带伪造的成功、批准或成果来源 | PRODUCT_SCOPE；ADR 0006 |
| E17 | 计划支持分支、依赖汇合、有序 Phase 和 Reviewer Gate；跨阶段不能绕过前一阶段的 Gate | EXECUTION_MODEL 阶段与验收 |
| E18 | Planner 不得静默扩大用户判据；只按 Worker 能力预览规划，缺少能力时用便签请用户决定 | PLANNER_CONSTRAINTS_DESIGN A、B |
| E19 | 批准内的过程调整走提案 → 独立审查 → 应用；Planner 能看到已完成成果清单和 Check 状态，可只读读取成果 | EXECUTION_MODEL；PROCESS_OUTPUTS_DESIGN |
| E20 | 过程版本记录 block_changes；改变批准边界后可显式接续前一个 Run，复用的成果重新经过新 Gate | EXECUTION_MODEL 阶段与验收、状态与控制 |
| E21 | 项目规则采用不可变版本，Run 捕获快照，规则更新只影响后续执行 | EXECUTION_MODEL 上层消费边界 |
| E22 | 不做自动分解或“先粗后细”规划（中型任务直接规划已满分） | PLANNER_CONSTRAINTS_DESIGN 不做 |

### A3 执行、Gate、人工与恢复

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E23 | 候选、handoff、节点完成、Gate 通过和 Run 完成是不同事实；检查结果绑定实际成果版本 | EXECUTION_MODEL 阶段与验收 |
| E24 | Reviewer 提供证据，宿主做自动/人工判定；Worker/Reviewer 不能自行宣布 Gate 或 Run 成功 | EXECUTION_MODEL；PRODUCT_SCOPE 模块表 |
| E25 | pending / stalled / suspended 区分；Attempt 保留真实的成功、失败、中断或未知结果 | EXECUTION_MODEL 状态与控制 |
| E26 | 人工介入带目标、原因、证据和 request token；回复只解决对应问题，不扩权 | EXECUTION_MODEL 状态与控制 |
| E27 | 同一节点每累计 3 次干预就暂停空闲的 Run，等待明确决定 | EXECUTION_MODEL；PLANNER_CONSTRAINTS_DESIGN C |
| E28 | 周期审查（600 秒或 30 步）只给建议，不自动采纳 | EXECUTION_MODEL 状态与控制 |
| E29 | 并行写入使用 EHAI 自有的 Git worktree，按依赖整合；integrate-run 自动整合，冲突保留待处理 | EXECUTION_MODEL 隔离与恢复 |
| E30 | 有效 handoff 和已完成成果是恢复依据；未知副作用不能靠换 Session 盲目重放 | EXECUTION_MODEL 隔离与恢复 |
| E31 | 有效成果/handoff 的业务接续由 EHAI 管理，模型上下文压缩由 harness 管理；EHAI 不重建模型历史 | EXECUTION_MODEL 上层消费边界；ADR 0005 第 7 条 |
| E32 | 核心止于创建分支，推送和开 PR 归用户或外部 Agent | ROADMAP 11-03 架构决定 |
| E33 | Pi 不是安全沙箱：EHAI 管工作区、凭证和副作用；自动 Check、人工判断和 Reviewer 意见是不同事实 | PRODUCT_SCOPE 核心与上层 |
| E34 | Token/费用硬限额暂缓；用量区分缺失、零值与估算，费用投影不等于账单 | ROADMAP 09-16；EXECUTION_MODEL |

### A4 Agent harness

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E35 | 经 Hub 接入多种 harness（Pi、Codex、Claude Code、OpenCode 等），不修改 harness，只用其公开接口 | ADR 0007 背景 |
| E36 | 三层结构：核心 → Hub → 兼容层；核心拥有全部事实，Hub 不持久化、不判定完成 | ADR 0007 第 1 条；HUB 分工 |
| E37 | 完成只认结束工具；自然语言说“完成”不算 | ADR 0007 第 2 条 |
| E38 | 新 harness 不改核心枚举：执行配置为 `harness: {kind, version, settings}`，兼容层声明能力，调度按能力匹配 | ROADMAP 11-03 架构决定 |
| E39 | EHAI 工具经每会话 MCP 端点交给 harness；Claude Code 兼容层关闭内置工具，只用 EHAI 工具 | HUB 每会话 MCP 端点、Claude Code 兼容层 |
| E40 | harness 使用隔离配置目录，不读用户的全局扩展和认证 | ADR 0005 接入方式第 4 条；HUB |
| E41 | Planner、Worker、Reviewer 是同一后端的不同预设，职责分开；harness 内部计划不能替代 PlanRevision、Run、Gate | ADR 0005 所有权 |
| E42 | Hub 可以作为独立进程运行（跨机器的工作区如何到达还未决定） | HUB 使用方式、当前限制 |

### A5 多项目、线程、引擎与页面

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E43 | 多工作区管理器托管多个独立的本机核心，按 workspace_id 路由和聚合；不让多个 Planner 共同编辑一个计划 | PRODUCT_SCOPE 渐进交付；WORKSPACE_MANAGER |
| E44 | 线程是面向用户的一级对象：一个 Goal 的讨论、方案、Run、待办与成果 | APP_SHELL_DESIGN 第 0 步 |
| E45 | 单一用户入口是本机引擎：init / up / add / doctor，不手写 JSON | APP_SHELL_DESIGN 模块 B |
| E46 | 线程的阶段和下一步由核心推导，前端只渲染 | APP_SHELL_DESIGN 模块 A |
| E47 | 时间线只来自已持久化的事实，查不到来源的不补造 | APP_SHELL_DESIGN 模块 A |
| E48 | 终端与桌面 App 两个前端共享同一状态 | APP_SHELL_DESIGN 为什么；ROADMAP 10-22 |
| E49 | Tauri 桌面外壳，新待办发系统通知；凭证只继承登录 shell，EHAI 不保存 | APP_SHELL_DESIGN 模块 D、待决事项 |
| E50 | 页面只消费生成 Client，不读库，不复制状态机 | ADR 0008 |
| E51 | 页面首版优先“需要我处理 / 正在推进 / 已验证成果”；看板、甘特图、日历、自定义组件不是首批依赖 | ROADMAP P3 |
| E52 | 执行图是页面内分层布局、可平移缩放的画布，Attempt 轨迹按需读取 | ADR 0008 |
| E53 | 工作台是卡片看板（需要你处理 / 进行中 / 已完成 / 工作区 / 便签），一眼看清，点开看细节，注意留白 | WORKBENCH_ASSISTANT_DESIGN 模块 4；ROADMAP 11-04 第 4 项 |
| E54 | 选中不是 Git 仓库的文件夹时，确认一次即可一键初始化 | WORKBENCH_ASSISTANT_DESIGN 模块 1 |
| E55 | 权限分三档，仿照 Codex（只改文件 / 改文件并运行命令 / 完全访问）；没有沙箱，页面写明 | WORKBENCH_ASSISTANT_DESIGN 模块 2 |
| E56 | 页面设计以 frontend-design skill 作为美学参考 | ROADMAP 11-04 第 4 项 |
| E57 | 持久事件消费：断线后补领，确认不执行任何业务动作 | EXECUTION_MODEL 上层消费边界；ROADMAP P3.4 |

### A6 便签与待办

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E58 | 便签保存问题、证据、多轮消息和明确决定；讨论不解除阻塞、不审批、不扩权 | EXECUTION_MODEL；PRODUCT_SCOPE 渐进交付 |
| E59 | 统一待办只聚合人工请求，保留源对象、版本和对应操作；不把回复、批准、Gate 判定和恢复合并为一个隐式动作 | PRODUCT_SCOPE 渐进交付；EXECUTION_MODEL |

### A7 个人助手

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E60 | 用对话代替手填：个人 Agent 是用户与实际编码 Agent 之间的桥梁 | ROADMAP 11-04 第 3 项 |
| E61 | 助手是独立于 Planner 的模型角色，只经公开接口；批准、授权、人工检查和整合只给按钮，由用户点击 | PRODUCT_SCOPE 11-04；WORKBENCH_ASSISTANT_DESIGN 模块 3 |
| E62 | 助手可以运行终端命令：只读命令自动运行，其余在弹窗里由用户批准 | WORKBENCH_ASSISTANT_DESIGN 终端命令 |
| E63 | 助手管理生活待办（记下、查询、改期、完成）；EHAI 不发提醒，到期的待办显示在“今天” | WORKBENCH_ASSISTANT_DESIGN 生活待办 |

### A8 生活事务：Workflow、Routine 与 Connector

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E64 | 独立的生活 Project 处理日常事务：原文或结构化录入、人工确认、待办管理、定时回顾 | PRODUCT_SCOPE 09-23；WORKFLOWS |
| E65 | Routine 面向订票、行程和日程提醒等，不限于待办和回顾 | PRODUCT_SCOPE 09-24 |
| E66 | 用户或外部 Agent 自定义 Workflow（条件分支、人工决定、等待）；Routine 只负责触发，业务由步骤和 Connector 完成 | P4_WORKFLOW_DESIGN 用户结果 |
| E67 | Workflow 内不再造 Planner/Worker 循环，开放的推理、研究和编码委托给核心；通用非 Git Agent 委托还要接通 | P4_WORKFLOW_DESIGN 模块职责 |
| E68 | Connector 是独立进程，使用版本化 HTTP 协议，任意语言都能实现；新增服务不改调度器 | CONNECTOR_PROTOCOL；PRODUCT_SCOPE 09-24 |
| E69 | 有副作用的动作（预订、支付、写日历、发通知）按具体动作授权，可以持续授权或逐次确认；确认绑定候选项、价格或上限、对象和有效期 | P4_WORKFLOW_DESIGN Connector 扩展契约 |
| E70 | 先保存调用意图再发请求；结果未知时交给人，不换新键重试 | P4_WORKFLOW_DESIGN；CONNECTOR_PROTOCOL |
| E71 | 业务结果（如无票）不等于技术失败 | P4_WORKFLOW_DESIGN 第一版自定义流程 |
| E72 | 触发方式：一次性、固定间隔、外部事件、带时区的日历规则；“每隔 24 小时”不等于“每天 09:00” | P4_WORKFLOW_DESIGN Routine 和时间 |
| E73 | 首个真实 Connector 是 Google Calendar；目标场景为查票 → 分支 → 批准 → 预订 → 写日程 → 等待 → 通知 | P4_WORKFLOW_DESIGN 正常入口验收 |
| E74 | 没有真实账号和供应商契约时，不宣称支持实际购票；需求本身不授权购买 | P4_WORKFLOW_DESIGN 通用流程目标 |
| E75 | 扩展点：动作注册、步骤处理器、触发适配器、子流程、Agent 委托 | P4_WORKFLOW_DESIGN 扩展点 |

### A9 双反馈环

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E76 | 快环在批准的只读配方目录内处理熟悉的请求，慢环处理升级并提出候选，回放通过后由用户明确发布 | PRODUCT_SCOPE 09-24；DUAL_FEEDBACK_EXPERIMENT |
| E77 | 用户纠错会暂停配方，并把请求交回慢环 | PRODUCT_SCOPE 09-24 |
| E78 | 固定的 escalate → Pi 回退：只读的 Pi 角色回答当前请求 | PRODUCT_SCOPE 09-24 后续 |
| E79 | 慢环可以把代码或流程修改交给明确绑定的独立 Project，走正常的计划和批准；不改运行中的框架，不自动部署 | PRODUCT_SCOPE 09-25；P4_WORKFLOW_REVISION_DESIGN |
| E80 | 版本化流程包（配方描述、脚本、分支）：Pi 提修订，用户看过 diff 后发布（目标设计，未实现） | P4_WORKFLOW_REVISION_DESIGN |
| E81 | 快环改用 OpenAI Decisions | PRODUCT_SCOPE 09-24；DUAL_FEEDBACK_EXPERIMENT |

### A10 交付原则

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E82 | 核心优先；每次交付一个经正常入口可用的小闭环，再按真实使用决定下一步，不要求先建齐全部后端 | PRODUCT_SCOPE 渐进交付；ROADMAP |
| E83 | 先走通正常入口，再做产品验收；HTTP 200、静态检查通过或字段存在都不等于交付 | PRODUCT_SCOPE 渐进交付 |
| E84 | 只保留一个产品 E2E；诊断只在实际失败时于仓库外进行 | AGENTS；PRODUCT_SCOPE |
| E85 | 多租户、跨项目公平调度和分布式平台不是前置条件 | PRODUCT_SCOPE 渐进交付 |

### A11 远期候选（P5）

| 编号 | 产品点 | 出处 |
| --- | --- | --- |
| E86 | 复用经验：把稳定成功的方法提炼为模板、Skill 或 Workflow 建议，来源可追溯，启用需确认 | ROADMAP P5 |
| E87 | 生活日程、日历及更多业务连接器；简单事务不包装成复杂计划 | ROADMAP P5 |
| E88 | 更多执行后端、扩展生态、跨项目资源策略、多设备执行 | ROADMAP P5 |

## B. 11-05 用户新提的点

| 编号 | 产品点 | 与现有点的关系 |
| --- | --- | --- |
| N1 | EHAI = Enhanced Human Agent Interface：用比聊天更高效的方式让人与 Agent 协作 | 新的总目标，统领 E3–E8 |
| N2 | 叠加在 ChatGPT / Claude Code 等订阅之上（订阅不开放成 API），在用户掌控下编排任务，尽量提高同时推进的项目数 | 扩展 E3、E35 |
| N3 | 生活和工作同属一个个人 Agent，因为用户的时间是连续的、单线程的 | 动机同 E64；与“两区并列”形态的冲突见 C3 |
| N4 | 任务复杂时，图和卡片读不过来，人工筛选也不现实：需要一个可以对话的 Agent 审阅多个任务，用图标和内嵌元素回答（像 Codex、Claude Code 那样） | 扩展 E60、E7 |
| N5 | 个人 AI 对话框只有一个注意力，即一个入口 | 与 E2、E53 的关系见 C1、C3 |
| N6 | Agent 能按用户思路长时间执行任务，不跑偏，有 Gate 把关 | E15、E17、E27 已有机制；在真实长任务上还没有证明 |
| N7 | 用图（如 Mermaid）显示工作树：阶段、Gate、子步骤、尝试过的路径和判断 | 扩展 E52（现有执行图只画计划，不画尝试过的路径和判断） |
| N8 | harness 要支持前沿闭源模型：Claude Code、ChatGPT/Codex，并继续适配 GPT | 落实 E35；排期与 E88 的冲突见 C5 |
| N9 | Pi 必须保留，作为最后的兜底：没有 Codex 也没有 Claude Code 时照样能用；能接其他模型；轻量、免费 | 补充 E35、E41 |
| N10 | 旧项目的教训：先用专有平台导致测试困难、GPT 陷入循环；所以先用透明的 Pi 实现 agent loop | E11 的来由 |
| N11 | 跨 harness 的工作轨迹：EHAI 告诉任何 harness 这件工作已经做了哪些步骤（阶段、节点、Attempt、关键工具操作、判断、Gate 结果、用户决定、成果），换到 Codex 或 Claude 时能接着做、流程对齐、迁移成本低。**不**同步各 harness 的 messages、MCP/system tools、prompt、skills，这些由各 harness 自己管 | 与 E31 一致，并落实 E8、E30；可以和 N7 共用同一份数据 |
| N12 | 有浏览器控制和 computer use 的 Agent，在用户授权后可以执行生活事务 | 接 E67、E75 的 Agent 委托；授权沿用 E69 |
| N13 | OpenAI Decisions 用来对生活中的大量数据做快速判断和并行处理（多平台社交媒体趋势、邮箱快速筛选、内容筛选，类似 Jev 提供的示例应用）：通用模型，不需要像垃圾邮件分类器那样训练；不让前沿模型处理这类数据，追求执行效率。对单个简单问题做快慢环路由没有价值 | 改变 E76–E81 的用途，见 C4 |
| N14 | 不把 JSON 直接丢给用户，回复要是人话 | 扩展 E7 |
| N15 | 页面要好看：在 GitHub 上找与人类审美对齐的 skill | 扩展 E56 |
| N16 | PRD 重写时保留全部现有的点，只排序，不删改 | 本文规则 |

## S. 产品经理的建议（11-05 用户已同意，见第 D 节）

| 编号 | 建议 | 依据 |
| --- | --- | --- |
| S1 | 用一个统一的“授权/决定”原语同时覆盖工作（批准方案、合并）和生活（确认购买、computer use 的具体操作），全部进入同一个注意力队列 | E6、E59、E69、N5、N12 |
| S2 | 接入专有 harness 前，先做一套录制/回放的测试手段，避免重演 N10 的测试困难 | N10；E84 |
| S3 | 个人 Agent 不自己推导状态，只查询 EHAI 的结构化事实（线程、待办、轨迹），再用富元素回答，可点开到图 | E46、E47、E50、N4、N7 |
| S4 | 阶段顺序：① 在 Pi 上证明一项长编码任务不跑偏 → ② 个人 Agent 用富元素回答 → ③ 录制/回放测试手段，然后接 Claude Code 与 Codex → ④ 统一授权与注意力队列，接入生活 → ⑤ 用浏览器/computer use 执行生活事务 | N6、N4、N8、S1、N12 |
| S5 | 把 Decisions 做成自定义 Workflow 里的一个 Connector 动作，按数据流批量、并行地筛选（E66、E68 已经支持 Connector 动作步骤），不再按单个问题路由 | N13 |

## C. 冲突与缺口（需要用户决定）

| 编号 | 冲突 | 选项 |
| --- | --- | --- |
| C1 | E2 规定“常驻顶层 Agent 在 EHAI 之外”。N4、N5 要一个唯一的对话入口；用户 11-05 又说“EHAI 维护上下文，给 Claude Code 或 Codex 提供能力去调用” | a) 内置助手（E61）就是唯一入口，外部 Agent 仍可经 CLI/MCP 使用；b) 唯一入口就是用户自己的 Claude Code / Codex 会话，EHAI 经 MCP 提供能力和轨迹（N11），内置助手只在 Pi 兜底（N9）时使用；c) 两者都做，同一份轨迹 |
| C2 | E13 的规划入口：用 EHAI 的 Planner 角色（可经 Hub 跑在任一 harness 上），还是由前沿 harness 直接规划后导入？ | a) 默认 EHAI Planner，Planner 跑在用户选的 harness 上；b) 默认外部规划后导入，Planner 只作 Pi 兜底 |
| C3 | 现有页面是生活/工作两区（ADR 0008）加工作看板（E53）；N3、N5 要一个注意力入口 | a) 合成一个队列，生活和工作只作为标签；b) 保留两区，只合并“需要我处理” |
| C4 | E76–E81 的双反馈环按单个请求路由；N13 认为这对简单问题没用，Decisions 应该用来批量处理数据 | a) 把双反馈环降为实验并隐藏，Decisions 转为 Workflow 动作（S5）；b) 保留路由，另加批量筛选；c) 撤掉路由实验。不论选哪项，E76–E81 都作为历史决定保留在文档里 |
| C5 | E88 把“更多执行后端”列为 P5、不做；N8 要求现在就适配 Claude Code 和 GPT/Codex | 把 Codex 兼容层提前到哪个阶段（S4 建议放在 ③） |
| C6 | E63 说 EHAI 不发提醒；E49 有系统通知。生活提醒不弹出就不实用 | a) 到期的生活待办也发系统通知；b) 交给 Google Calendar 等外部提醒（E73） |
| C7 | E67 说通用非 Git 的 Agent 委托尚未接通，现有 Pi 核心带代码工作区装配；N12 要 Agent 执行生活事务 | 是否把“非 Git Agent 委托”列为第 ⑤ 阶段的前置工作 |

## D. 用户决定（2026-11-05）

| 编号 | 决定 |
| --- | --- |
| C1 | 两者都做：页面上的内置助手是唯一的对话入口，经 Hub 运行在用户选的 harness 上（按 C5 后续决定，先是 Codex 或 Pi）；用户自己的 Codex 会话也能经 MCP 调用同一套能力和轨迹（N11）。E2 保留为“外部 Agent 可以使用核心”，不再是唯一形态 |
| C2 | 默认由 EHAI Planner 规划，Planner 运行在用户选择的 harness 上，保留 E18 等现有约束；导入外部方案（E13 第二条入口）照常可用 |
| C3 | 合成一个注意力队列，生活和工作只是标签，按“需要你处理 / 进行中 / 已完成”排列 |
| C4 | 路由实验降为隐藏的实验；Decisions 改做 Workflow 里的 Connector 动作，批量、并行地筛选数据流（即 S5）。E76–E81 作为历史决定保留 |
| C5 | 立刻开始适配 Codex/GPT，不再留在 P5。按 S2，先做录制/回放测试手段，再接专有 harness。同日后续决定：先不做 Claude Code，把它移出路线图（现有兼容层保留，不再推进） |
| C6 | 到期的生活待办经桌面外壳发系统通知，和新待办通知共用一套 |
| C7 | 不依赖 Git 工作区的 Agent 委托，作为第 ⑤ 阶段（浏览器 / computer use 执行生活事务）的前置工作 |
| S1–S4 | 同意。S4 的顺序按 C5 调整：第 ③ 阶段（录制/回放，然后接 Codex/GPT）提前到现在开始；其余阶段顺序不变 |
| S5 | 随 C4 确定 |

## E. 落点（11-05 重写后）

[PRODUCT_SCOPE](PRODUCT_SCOPE.md) 正文按下表收录全部 E 与 N 条目，每条都以 `[编号]` 标注，可直接搜索核对。
11-04 版原文保留在其“决定沿革”一节。阶段顺序（S4 按 C5 调整）见 [ROADMAP 11-05 阶段顺序](ROADMAP.md#阶段)。

| PRODUCT_SCOPE 小节 | 条目 |
| --- | --- |
| 1 一句话与目标 | N1、N2、E1、E3、E4 |
| 2 主线：一个人，一个入口，很多件事 | N3、N4、N5、N14、E2、E5、E6、E7、E51、E53、E58、E59、E60、E61；C1、C3、S3 |
| 3 工作：规划、执行与把关 | N6、E13–E21、E23–E30、E32、E33、E44、E46、E47；C2 |
| 4 轨迹与图 | N7、N11、E8、E31、E52 |
| 5 Agent harness | N8、N9、N10、E11、E35–E42；C5、S2 |
| 6 生活：待办、流程与外部服务 | N12、N13、E12、E63–E81；C4、C6、C7、S1、S5 |
| 7 引擎、页面与外部接入 | N15、E9、E43、E45、E48、E49、E50、E54、E55、E56、E57、E62 |
| 8 交付原则 | E10、E34、E82–E85 |
| 9 不做与远期 | E22、E86、E87、E88 |
| 文首规则 | N16 |

按用户决定改变了含义的条目，原文仍在“决定沿革”中：E2（C1：内置助手成为对话入口，外部 Agent 仍可使用核心）、
E63（C6：到期改为发系统通知）、E76–E81（C4：降为隐藏实验）、E88（C5：Codex/GPT 提前，Claude Code 暂移出计划）。
