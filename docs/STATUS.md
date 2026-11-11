# 当前能力与验证

更新：2026-11-10。每项能力一行：实现状态、关键边界、证据位置。本页只写已提交代码的现状，不写目标。
证据摘要见 [EVIDENCE](EVIDENCE.md)，产品目标见 [PRODUCT_SCOPE](PRODUCT_SCOPE.md)，当前计划见 [ROADMAP](ROADMAP.md)。
11-04 至 11-05 的工作台、助手与快环改动从未提交，已于 11-07 放弃，本页不含它们。

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
| Planner 授权与验收约束 | 真实验证 | 规划上下文含 Worker 能力预览；只给人工判据时最终 Gate 不带命令；同节点每 3 次干预暂停 Run（暂停仅脚本化诊断验证）；预览不随方案持久化 | [设计](records/planner-constraints.md) |
| Pi 规划调用时限 | 真实验证 | 超时失败并结束进程，不自动重放；不解决上游传输停顿 | [核心](EVIDENCE.md#核心执行) |
| 外部计划导入 | 真实验证 | 只支持尚未规划的 Goal；拒绝伪造状态/批准 | [核心](EVIDENCE.md#核心执行) |
| CLI API 客户端 / MCP | 部分 | MCP 默认开放全部操作；只有少数工具做过模型调用 | [核心](EVIDENCE.md#核心执行) |
| 周期轨迹审查 / 定向挂起 | 部分 | 审查只给建议，需显式采纳；不是自治纠偏 | [RECORD_R2](history/RECORD_R2.md#本批交付) |
| Agent 后端 | 部分 | Pi 与 Claude Code。Claude Code 2.x 经每会话 MCP 端点运行全部角色，内置工具全部关闭（11-03 仅仓库外诊断：真实 Claude Code + 脚本化 Anthropic 服务走通产品 E2E 场景、续用、取消与中途消息；11-04 真实 Claude Code 经 DeepSeek 的 Anthropic 兼容端点完成一次 Hub 级会话，完整 Run 与 Anthropic 模型未验证；管理器只配置 Pi）。Pi 锁定 1.3.0；工具不用严格采样，参数由核心按 Schema 校验；10-22 经 DeepSeek 官方端点真实验证规划→执行→整合）；旧 Codex 后端已删除（新的 Codex harness 见下一行）；Planner 与 harness 无关；Hub 已有每会话 MCP 端点（仅合成客户端验证）；11-03 起执行配置为 `harness` 形状，Pi 设置由 Hub 校验，旧形状指纹不变（仅无模型诊断验证）；能力清单随 Hub 健康返回，角色启动前检查所需能力；管理器可按工作区选 Pi 或 Codex（Claude Code 仍只经独立宿主） | [ADR 0007](adr/0007-agent-harness-port.md) |
| Codex harness | 部分 | Hub 的 `codex` 兼容层，讲实验 app-server 协议，harness `version` 为 `app-server`，不限定 CLI 版本；resolve 实测版本只记入设置、不进入指纹，自动更新不改变授权与会话指纹。私有 CODEX_HOME 的 ChatGPT 登录；保留原生文件改动与命令，逐个由核心按 HostTools 授权批准，EHAI 工具经每会话 MCP。11-07 真实验收：经 `ehai-api` 与 CLI 跑完一条线程（规划、执行、审查、整合）。未验证：真实续用、运行中取消、原生命令审批与拒绝路径、同回复并行调用批次（只报单个调用）、Windows、独立 Hub。11-07 起管理器路径也可选 Codex：`ehai init --harness codex` 配置，`ehai add` 按默认或 `--harness` 逐个工作区选择，已真实验收一条线程。11-09：移除确切 CLI 版本门限，协议不兼容时会话启动或调用失败，需重新核对新版本协议；已用 0.167.0-alpha.3.2 经管理器路径真实跑完一条线程；跨 CLI 版本续用未验证；此前登记的 Codex 工作区固定的旧文档无迁移 | [CODEX_HARNESS_DESIGN](CODEX_HARNESS_DESIGN.md) |
| Agent harness Hub | 无模型验证 | 所有 Pi 角色经独立 `ehai-hub`（默认本机子进程）；Worker 路径由产品 E2E 在 Linux/Windows CI 覆盖，未用真实模型；跨机器需同路径 | [HUB](HUB.md) · [核心](EVIDENCE.md#核心执行) |
| 阶段上下文 | 部分 | 逻辑 Phase Session 共享；物理 Pi Session 按 Attempt 隔离 | [EXECUTION_MODEL](EXECUTION_MODEL.md#实例与会话) |
| 批准内过程调整 / Block 清单 | 真实验证 | 清单只描述变化，不批准、不授权复用成果；草稿可读已完成成果，先粗后细在中型任务上端到端通过（10-04）；暂停时不能决定人工 Gate | [核心](EVIDENCE.md#核心执行) |
| Git 自动整合 | 无模型验证 | 用历史真实成果整合；结果事件持久化、Run 整合列表、1 MiB 截断 diff 与新分支发布的 HTTP/CLI/MCP 已接线；产品 E2E 覆盖无冲突整合、重试同 ID、列表、diff、创建 ehai/e2e 分支与同 commit 幂等，HEAD/工作树状态不变，两种整合时间线均去重；此前 Web 整合与差异查看经脚本化 Pi 走通，本轮分支前端、真实模型及冲突路径未实跑；MCP 本轮只读 Schema | [Git 自动整合](USAGE.md#git-自动整合) · [文件夹与分支验证](records/workspace-manager.md#文件夹添加与整合分支后端验证2026-10-22) |
| 跨批准后继 Run | 真实验证 | 单次接续通过；基线变化重执行、多次接续、崩溃未验证 | [核心](EVIDENCE.md#核心执行) |
| 长任务可靠性 | 部分 | Worker/Reviewer 运行中强杀后可恢复（重启后等旧租约过期，最长 5 分钟，10-03 真实验证）；写入前/写入后未提交/Reviewer 中强杀的长链接续经脚本化 Pi 验证（11-03）；真实模型下的写入中断、工具执行中强杀未验证 | [未决问题](EVIDENCE.md#已知未决问题) |
| Token / 费用硬限额 | 暂缓 | 2026-09-16 决定暂不实现；用量记录保留 | — |
| 产品 E2E | 无模型验证 | 真实 Pi 经本机 Hub、Git worktree，模型为脚本化服务；覆盖无冲突的 integrate-run、diff、新分支创建/同 commit 幂等、HEAD/工作树状态不变及时间线单条事件；不覆盖真实模型、独立 Hub、整合冲突、管理器 from-folder、MCP | [开发与验收规则](DEVELOPMENT_GUIDELINES.md#产品-e2e) |
| CI | 无模型验证 | 静态检查、两平台 mypy、契约比对、TS 构建、Linux/Windows E2E | [执行记录](records/refactor.md) |
| P2 总验收 | 真实验证 | 导入路径真实手动验收通过，含强杀恢复；Pi Planner 规划路径补充验收通过，未含强杀（单个小仓库、macOS，10-03）；Windows、多轮讨论与 replan 未做 | [归档的重构计划](history/REFACTOR_PLAN.md#四步顺序) |

## 平台后端（P3）

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| P3.1 项目总览 | 无模型验证 | 按来源宿主查询，不做跨宿主聚合 | [P3](EVIDENCE.md#p3-平台后端) |
| 线程读模型与线程式页面 | 真实验证 | 核心摘要/详情/时间线、管理器聚合与 CLI/MCP 已接线；产品 E2E 覆盖创建/草稿/运行/人工 Gate/完成、404、时间线及分页；Web 新建线程、时间线、批准并开始、人工处理在 fake Worker 工作区走通；整合、按文件看差异、刷新后重开、创建新分支（核对仓库 HEAD 与工作树未变）与非 ehai up 管理器的添加提示，在脚本化 Pi 工作区走通；页面添加文件夹与系统通知尚未在浏览器实跑；管理器离线聚合、MCP 调用、Pi 工作区与真实模型未验证；10-22 用真实 DeepSeek 在页面走通 添加项目文件夹 → 新线程与 Planner 讨论 → 批准并开始 → 完成 → 整合看差异 → 创建分支，系统通知、桌面文件夹选择器、人工待办未在该次覆盖；11-09 时间线加入人工检查结论与意见、人工暂缓、过程调整、Reviewer 结论、Gate 结果与返工，线程页与 Run 图显示这些判断并链接沿用成果，在阶段 ① 真实数据副本上经 CLI 与管理器页面核对；同日修复含 `"Bearer " + key` 类文本的 Run 轨迹与结果读取 422（脱敏改为按值进行） | [线程查询](USAGE.md#线程查询) · [P3](EVIDENCE.md#p3-平台后端) |
| 引擎守护进程与单一配置（模块 B） | 无模型验证 | macOS 临时 EHAI_HOME 下 init/up/local/from-folder/down 通过，三同名 Git 目录依次 ready（-2/-3）；重复路径 409 带已有 ID，非 Git/子目录/缺失目录 422，手动管理器 local=false 且添加 409；CLI add 重复原登记仍幂等，MCP local 查询/Schema/重复路径错误实跑；既有 init/add/doctor 证据保留；本轮不覆盖前端、真实模型、活动任务停止冲突或 Windows/Linux 实跑 | [本地引擎验证](records/workspace-manager.md#模块-b-验证记录2026-10-22) · [本轮记录](records/workspace-manager.md#文件夹添加与整合分支后端验证2026-10-22) |
| `ehai doctor --probe` | 无模型验证 | 经本机 Hub 启动一次 Pi 会话核对 Provider/模型/思考级别后关闭，不调用模型；不存在的模型 ID 也会通过（Pi 启动时不校验） | [应用外壳](records/app-shell.md#2026-11-03-第二轮doctor-探测与终端交互会话11-03-计划第-3-步) |
| 终端交互会话（无参数 `ehai`） | 真实验证 | 经管理器与核心正式接口；真实 DeepSeek 下走通 新建 → 讨论 → 批准开始 → watch → 人工 Check → 整合 → diff → 新分支（macOS，标准输入驱动）；reply/fail、多工作区选择、Ctrl-C 中断、Windows 未验证；便签与生活确认不在会话内处理 | [应用外壳](records/app-shell.md#2026-11-03-第二轮doctor-探测与终端交互会话11-03-计划第-3-步) |
| Tauri 桌面外壳 | 部分 | 启动页探测守护进程后跳转到工作台，可编辑并记住启动命令、经交互式登录 shell 执行；macOS 调试构建的窗口连上本机管理器并加载工作台（日志核对）；守护进程页面只获准选择文件夹和发系统通知（对话框与通知插件，已编译未实跑）；点击“启动 EHAI”、打包与 Windows 未验证 | [desktop](../desktop/README.md) |
| P3.2 统一人工待办 | 无模型验证 | 人工 Gate 闭环通过；类型为干预、人工验收、便签与生活确认（运行中 Worker 请求已删除）；11-10 加入方案批准（plan_approval，批准不启动 Run），每项带绑定对象、其 SHA-256、上限与到期时间，过期项不可处理（阶段 ④，在验收管理器上核对） | [P3](EVIDENCE.md#p3-平台后端) · [阶段 ④ 记录](records/stage4-authorization.md) |
| P3.2 便签 | 真实验证 | 便签→修订→执行→人工验收通过；propose_process 路径未验证 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.3 薄 Web 工作台 | 无模型验证 | 生活/工作两区与共用页面均接入正式 API（fake Worker、Jev 协议替身）；Run 执行图与逐次尝试轨迹用一次真实 Run 数据库的只读副本核对，分支形态只用改写的响应看过；不含创建 Goal/规划、通用 Workflow；11-10 起去掉生活/工作分区开关，首页改为注意力队列 + 助手，生活绑定从浏览器偏好改存管理器（阶段 ②） | [P3](EVIDENCE.md#p3-平台后端) · [ADR 0008](adr/0008-web-workbench.md) |
| 注意力队列与生活绑定（阶段 ②） | 无模型验证 | 管理器把各运行中工作区的线程、未决待办与绑定项目的生活待办合成一个队列（需要你处理/进行中/已完成），生活只作标签；绑定存管理器数据目录；在阶段 ① 数据副本与种子工作区上与核心逐项核对，停止的工作区列为不可用；不是跨核心一致快照，读取失败不重试 | [管理器](WORKSPACE_MANAGER.md#web-工作台) · [P3](EVIDENCE.md#p3-平台后端) |
| 管理器只读助手（阶段 ②） | 真实验证 | 只在 `ehai up` 的管理器中；Codex `gpt-6.1-sol` 与 Pi DeepSeek `deepseek-flash` 上回答为一句话加引用（线程、Run、diff、生活待办、待办项），页面按实时数据画卡片；原生命令被拒、用户原文脱敏、一次一轮（忙时 409）；diff 统计只来自已有整合；校验拒绝后同轮改正与提醒后的纯文本回退未实跑；11-10 用户验收通过 | [管理器只读助手](WORKSPACE_MANAGER.md#管理器只读助手) · [阶段 ② 记录](records/stage2-assistant.md) |
| 首页：队列 + 助手（阶段 ②） | 真实验证 | 1440 px 两栏、375 px 助手在前；卡片按钮走现有接口并随工作区事件刷新（空闲不轮询），完成待办、确认录入、经线程页通过人工检查后新旧卡片与队列同步；在验收管理器上核对，Windows 未验证；11-10 用户验收通过 | [阶段 ② 记录](records/stage2-assistant.md) |
| 决定卡片与队列通知（阶段 ④） | 无模型验证 | 一个卡片渲染任一待办：问题、绑定对象（列表 5 项后折叠，订阅条目为链接标题）、上限、剩余有效期（过期禁用按钮）与该项动作；首页队列、引用卡片、待办页及单项页 `/decisions/…` 共用；通知跟随队列（首次读取只记基线，窗口聚焦时不通知），按最早到期的生活待办定时重读；在验收管理器上用无头 Chrome（1440/375 px）核对，实点了 Workflow 步骤的批准；桌面 App 原生通知点击不能打开对应项；“问”“自动回答”移出导航；11-10 用户验收通过 | [阶段 ④ 记录](records/stage4-authorization.md#m4-决定卡片通知与导航) |
| Workflow Agent 查询步骤（阶段 ⑤） | 真实验证 | 自定义 Workflow 的 `agent` 步骤把开放任务交给 Hub 角色，不用 Git 工作区、原生命令关闭，只能调用所列的只读 Connector 动作（写动作保存时被拒）；每次查询是不带写授权的普通 ConnectorCall，计入次数上限并记录状态与用时；结果须通过 output_schema；中断后重启一次、最多 2 个并行；11-10 在 Codex（RSS、12306）与 Pi DeepSeek（12306）上跑通，参数被拒、次数用尽与重启只读代码 | [WORKFLOWS](WORKFLOWS.md#agent-查询步骤) · [阶段 ⑤ 记录](records/stage5-life-agent.md#m1-agent-查询步骤) |
| 12306 Rail Connector（catbus，阶段 ⑤） | 真实验证 | 独立进程 `ehai-rail`，经本机 catbus 只读查询车站、车次与余票、票价、中转、经停（argv 无 shell、30 秒超时、间隔 0.5 秒），余票与票价规范化、超限截断；不登录、不下单、不付款；11-10 五个动作与 catbus 直查一致，未知车站与过期日期按原因失败；超时与截断未实跑 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md#12306-rail-connectorcatbus) · [阶段 ⑤ 记录](records/stage5-life-agent.md#m2-12306-connector) |
| 记录页启动流程与出行示例（阶段 ⑤） | 真实验证 | 记录页按定义的输入 schema 生成“开始流程”表单；Agent 步骤记录实时显示每次查询（中文动作名、可读参数、状态、用时）；决定卡片显示出行方案；示例 `rail.trip` 由 Agent 整理 2–3 个方案后人工 review；11-10 在 Codex 与 Pi 上从页面启动，方案与 12306 一致；Pi 那次记录页一度停止刷新（刷新后完整，未复现）；等用户验收 | [WORKFLOWS](WORKFLOWS.md#铁路出行方案railtrip) · [阶段 ⑤ 记录](records/stage5-life-agent.md#m3-出行示例与页面) |
| P3.4 事件消费 | 无模型验证 | 重启后补领与确认通过；不自动唤醒、不执行业务动作 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.5 项目配置与 Run 快照 | 真实验证 | 更新只影响新 Run；不是可编辑角色库 | [P3](EVIDENCE.md#p3-平台后端) |
| 多工作区管理 / Planner 容量 | 真实验证 | 本机同模型；无跨工作区迁移、动态借用或多 Planner 共编 | [P3](EVIDENCE.md#p3-平台后端) |

## 生活事务与外部连接（P4）

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| 生活 capture / review 与 Routine | 无模型验证 | 两个固定流程、固定间隔；不是通用 Workflow 引擎 | [WORKFLOWS](WORKFLOWS.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 执行记录 | 无模型验证 | schema 19→20 迁移与续办通过；历史轨迹不补造 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 版本兼容 | 关闭 | 代码接口存在，生产无适配器、无启用开关 | [WORKFLOWS](records/workflows.md) |
| 自定义 Workflow / 条件分支 | 无模型验证 | 有限无环单路径图、人工与时间等待、Routine 绑定版本；Connector 替身走通订票场景；无真实服务、无事件触发/取消；11-10 人工步骤可带 grant（上限、有效秒数），受保护写动作的输入只能取自批准对象或授权，过期或超限在排队前失败（阶段 ④，经 Google Calendar 替身验收） | [WORKFLOWS](WORKFLOWS.md#自定义-workflow) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 事件触发 / 并行 / 子流程 | 未实现 | 只有目标设计 | [P4_WORKFLOW_DESIGN](P4_WORKFLOW_DESIGN.md) |
| Connector HTTP 协议 | 无模型验证 | 本地分进程试用；结果未知需显式核对 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md) |
| Google Calendar Connector | 无模型验证 | 只有本地替身；真实 OAuth/日历未授权验证；11-10 创建前检查调用授权的到期与 count 上限（按本地回执累计），替身上验收，恢复路径只读代码 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| RSS/Atom Connector | 真实验证 | 只读 `feed.fetch`（RSS 2.0 / Atom 1.0、5 MiB 上限、最多 5 次重定向、失败按类别回传原因）；11-08 隔离宿主上用真实订阅源走通自定义 Workflow（抓取 → 人工步骤），HTML 页与 404 按原因失败；重定向不逐跳核对允许清单；不计入产品 E2E；11-10 加 `feed.fetch_many`（最多 20 源并发、合并倒序、逐源失败），9 个真实源上走通 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md#rssatom-connector-配置和调用) |
| OpenAI Decisions Connector | 真实验证 | 独立进程 `ehai-decisions`，只读 `decisions.classify`：逐条以 JSON 数据发送、最多 8 路并行、429/5xx 重试一次、逐条失败不使整批失败；11-10 在真实 `gpt-6-luna` 上分类 38 条并经示例摘要 Workflow 进入人工 review；用户自己的订阅源与每日 Routine 到点触发未验收 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md#openai-decisions-connector) · [阶段 ④ 记录](records/stage4-authorization.md#m3-decisions-订阅摘要) |
| Jev 双反馈环实验 | 真实验证 | 只读、显式推进；不是自动学习或通用 Workflow。11-05 决定降为隐藏实验，阶段 ④ 移出页面主路径（C4） | [DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) |
| 固定 Pi 回退 | 真实验证 | 显式启用；故障保留不重试；发布仍需明确决定 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 独立 Project 代码修改转交 | 部分 | 最小项目修改闭环通过；整仓规划收敛与传输稳定性未解决 | [P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 生活日程 / 经验固化 / 扩展生态 | 未实现 | 远期候选，未排期 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md#9-不做与远期) |

## 下一步

见 [ROADMAP](ROADMAP.md#现在)。本页各行随实际实现与验证更新。
