# 当前能力与验证

更新：2026-11-07。每项能力一行：实现状态、关键边界、证据位置。本页只写已提交代码的现状，不写目标。
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
| Codex harness | 部分 | Hub 的 `codex` 兼容层，固定 Codex CLI 0.166.0-alpha.6（实验 app-server），私有 CODEX_HOME 的 ChatGPT 登录；保留原生文件改动与命令，逐个由核心按 HostTools 授权批准，EHAI 工具经每会话 MCP。11-07 真实验收：经 `ehai-api` 与 CLI 跑完一条线程（规划、执行、审查、整合）。未验证：真实续用、运行中取消、原生命令审批与拒绝路径、同回复并行调用批次（只报单个调用）、Windows、独立 Hub。11-07 起管理器路径也可选 Codex：`ehai init --harness codex` 配置，`ehai add` 按默认或 `--harness` 逐个工作区选择，已真实验收一条线程 | [CODEX_HARNESS_DESIGN](CODEX_HARNESS_DESIGN.md) |
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
| 线程读模型与线程式页面 | 真实验证 | 核心摘要/详情/时间线、管理器聚合与 CLI/MCP 已接线；产品 E2E 覆盖创建/草稿/运行/人工 Gate/完成、404、时间线及分页；Web 新建线程、时间线、批准并开始、人工处理在 fake Worker 工作区走通；整合、按文件看差异、刷新后重开、创建新分支（核对仓库 HEAD 与工作树未变）与非 ehai up 管理器的添加提示，在脚本化 Pi 工作区走通；页面添加文件夹与系统通知尚未在浏览器实跑；管理器离线聚合、MCP 调用、Pi 工作区与真实模型未验证；10-22 用真实 DeepSeek 在页面走通 添加项目文件夹 → 新线程与 Planner 讨论 → 批准并开始 → 完成 → 整合看差异 → 创建分支，系统通知、桌面文件夹选择器、人工待办未在该次覆盖 | [线程查询](USAGE.md#线程查询) · [P3](EVIDENCE.md#p3-平台后端) |
| 引擎守护进程与单一配置（模块 B） | 无模型验证 | macOS 临时 EHAI_HOME 下 init/up/local/from-folder/down 通过，三同名 Git 目录依次 ready（-2/-3）；重复路径 409 带已有 ID，非 Git/子目录/缺失目录 422，手动管理器 local=false 且添加 409；CLI add 重复原登记仍幂等，MCP local 查询/Schema/重复路径错误实跑；既有 init/add/doctor 证据保留；本轮不覆盖前端、真实模型、活动任务停止冲突或 Windows/Linux 实跑 | [本地引擎验证](records/workspace-manager.md#模块-b-验证记录2026-10-22) · [本轮记录](records/workspace-manager.md#文件夹添加与整合分支后端验证2026-10-22) |
| `ehai doctor --probe` | 无模型验证 | 经本机 Hub 启动一次 Pi 会话核对 Provider/模型/思考级别后关闭，不调用模型；不存在的模型 ID 也会通过（Pi 启动时不校验） | [应用外壳](records/app-shell.md#2026-11-03-第二轮doctor-探测与终端交互会话11-03-计划第-3-步) |
| 终端交互会话（无参数 `ehai`） | 真实验证 | 经管理器与核心正式接口；真实 DeepSeek 下走通 新建 → 讨论 → 批准开始 → watch → 人工 Check → 整合 → diff → 新分支（macOS，标准输入驱动）；reply/fail、多工作区选择、Ctrl-C 中断、Windows 未验证；便签与生活确认不在会话内处理 | [应用外壳](records/app-shell.md#2026-11-03-第二轮doctor-探测与终端交互会话11-03-计划第-3-步) |
| Tauri 桌面外壳 | 部分 | 启动页探测守护进程后跳转到工作台，可编辑并记住启动命令、经交互式登录 shell 执行；macOS 调试构建的窗口连上本机管理器并加载工作台（日志核对）；守护进程页面只获准选择文件夹和发系统通知（对话框与通知插件，已编译未实跑）；点击“启动 EHAI”、打包与 Windows 未验证 | [desktop](../desktop/README.md) |
| P3.2 统一人工待办 | 无模型验证 | 人工 Gate 闭环通过；类型为干预、人工验收、便签与生活确认（运行中 Worker 请求已删除） | [P3](EVIDENCE.md#p3-平台后端) |
| P3.2 便签 | 真实验证 | 便签→修订→执行→人工验收通过；propose_process 路径未验证 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.3 薄 Web 工作台 | 无模型验证 | 生活/工作两区与共用页面均接入正式 API（fake Worker、Jev 协议替身）；Run 执行图与逐次尝试轨迹用一次真实 Run 数据库的只读副本核对，分支形态只用改写的响应看过；不含创建 Goal/规划、通用 Workflow；生活分区靠浏览器偏好 | [P3](EVIDENCE.md#p3-平台后端) · [ADR 0008](adr/0008-web-workbench.md) |
| P3.4 事件消费 | 无模型验证 | 重启后补领与确认通过；不自动唤醒、不执行业务动作 | [P3](EVIDENCE.md#p3-平台后端) |
| P3.5 项目配置与 Run 快照 | 真实验证 | 更新只影响新 Run；不是可编辑角色库 | [P3](EVIDENCE.md#p3-平台后端) |
| 多工作区管理 / Planner 容量 | 真实验证 | 本机同模型；无跨工作区迁移、动态借用或多 Planner 共编 | [P3](EVIDENCE.md#p3-平台后端) |

## 生活事务与外部连接（P4）

| 能力 | 状态 | 关键边界 | 证据 |
| --- | --- | --- | --- |
| 生活 capture / review 与 Routine | 无模型验证 | 两个固定流程、固定间隔；不是通用 Workflow 引擎 | [WORKFLOWS](WORKFLOWS.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 执行记录 | 无模型验证 | schema 19→20 迁移与续办通过；历史轨迹不补造 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 版本兼容 | 关闭 | 代码接口存在，生产无适配器、无启用开关 | [WORKFLOWS](records/workflows.md) |
| 自定义 Workflow / 条件分支 | 无模型验证 | 有限无环单路径图、人工与时间等待、Routine 绑定版本；Connector 替身走通订票场景；无真实服务、无事件触发/取消 | [WORKFLOWS](WORKFLOWS.md#自定义-workflow) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| Workflow 事件触发 / 并行 / 子流程 | 未实现 | 只有目标设计 | [P4_WORKFLOW_DESIGN](P4_WORKFLOW_DESIGN.md) |
| Connector HTTP 协议 | 无模型验证 | 本地分进程试用；结果未知需显式核对 | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md) |
| Google Calendar Connector | 无模型验证 | 只有本地替身；真实 OAuth/日历未授权验证 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| RSS/Atom Connector | 真实验证 | 只读 `feed.fetch`（RSS 2.0 / Atom 1.0、5 MiB 上限、最多 5 次重定向、失败按类别回传原因）；11-08 隔离宿主上用真实订阅源走通自定义 Workflow（抓取 → 人工步骤），HTML 页与 404 按原因失败；重定向不逐跳核对允许清单；不计入产品 E2E | [CONNECTOR_PROTOCOL](CONNECTOR_PROTOCOL.md#rssatom-connector-配置和调用) |
| Jev 双反馈环实验 | 真实验证 | 只读、显式推进；不是自动学习或通用 Workflow。11-05 决定降为隐藏实验，阶段 ④ 移出页面主路径（C4） | [DUAL_FEEDBACK_EXPERIMENT](DUAL_FEEDBACK_EXPERIMENT.md) |
| 固定 Pi 回退 | 真实验证 | 显式启用；故障保留不重试；发布仍需明确决定 | [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 独立 Project 代码修改转交 | 部分 | 最小项目修改闭环通过；整仓规划收敛与传输稳定性未解决 | [P4_WORKFLOW_REVISION_DESIGN](P4_WORKFLOW_REVISION_DESIGN.md) · [P4](EVIDENCE.md#p4-生活事务与外部连接) |
| 生活日程 / 经验固化 / 扩展生态 | 未实现 | 远期候选，未排期 | [PRODUCT_SCOPE](PRODUCT_SCOPE.md#9-不做与远期) |

## 下一步

见 [ROADMAP](ROADMAP.md#现在)。本页各行随实际实现与验证更新。
