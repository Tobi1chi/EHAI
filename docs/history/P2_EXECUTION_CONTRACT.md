> 历史快照（2026-09-15 归档），非当前操作说明。当前状态见 [STATUS](../STATUS.md)。

# P2 执行契约与迁移基线

## 目的与范围

本文固定执行核心的不变量，并区分原型行为和重整后的目标语义。产品定义以
[Product Scope](../PRODUCT_SCOPE.md) 为准，工作顺序见
[P2 Implementation Plan](../P2_IMPLEMENTATION_PLAN.md) 的当前重整章节。
2026-09-06 的 [Execution Model](../EXECUTION_MODEL.md) 是 Worker、阶段决策树、Gate、Session 与恢复的
目标语义；本文下述具体 Port、枚举和已接入的自动节点 Gate 是当前代码，不用它们限制目标，也不虚构已完成迁移。
R1 已增加规划讨论接口、讨论事件及版本化设计；执行挂起求助和完整需求验收仍需要后续状态迁移。
普通 `ehai` 保留 P1 同步兼容，P2 后台模式使用持久 DispatchWork。
当前 R2 已接入任务图、最终行为 Gate 和代码工作区；Built-in/Server 的代表性编码与有序停止恢复已验证。
阶段/人工 Gate、阶段 Review、共享 Session、按 handoff 回退和过程自主调整仍待实现；
长运行等未验证，不能据此宣布整个 R2 或 P2 已通过。

## 平台调用边界

CLI、顶层通用 Agent、未来 UI 和 Routines 使用同一公开应用语义。Planner 是调查、讨论、提出与修订
方案的专门能力，不等于顶层通用 Agent。只读规划授权与指定 workspace 的执行授权分开。
可读设计、执行图和实际 Gate 必须一致，批准固定需求、接口、Gate 与授权。
Planner 可自主调整中间过程，包括拆分、依赖、并行与路线，保存批准基线和调整版本；
改变批准底线才重新批准。外部审查意见是规划输入，不构成批准。

Worker 是按预设经 Connector 创建、管理的具体 Agent 实例；职责、模型和运行框架独立。
计划目标为带阶段/分支 Gate 的决策树，阶段 Review Agent 提供测试审查和建议，
Gate 统一承载自动与人工判定。具体类和状态映射必须在实现时确定，不能把 WorkerProfile 当作活跃 Worker。

## 执行映射与状态所有权（当前实现）

映射固定为：一个 `PlanNode` 可以产生多次 `Attempt`；每个 `Attempt` 必须且只能绑定一个
`builtin_turn` 或 `external_execution`。Turn/Execution 是 Provider 执行句柄，不拥有
`PlanNode`、`Attempt` 或 `Run` 的领域状态。

`AttemptStatus` 是持久化生命周期；`AttemptActivity` 是非终态执行的活动观察，两者不得互相替代：

| 所有者 | 值 | 语义 |
| --- | --- | --- |
| Orchestrator | `pending/running/succeeded/failed/timed_out/cancelled/interrupted` | Attempt 生命周期 |
| Runtime/Connector observation | `queued/running/waiting/stalled` | 当前活动，不直接推进领域状态 |

Worker 正常返回或提交候选只能使 Attempt 得到候选结果。带 required Checks 的 `PlanNode` 必须由 Checker
产生 `CheckResult` 并由 Gate 完成；无 required Check 且仍有后续边的中间节点可由宿主接收任务交接，但不满足
Goal。Scheduler 和 Connector 不得判断最终完成。

## Agent 执行 Connector Port 与 SessionPolicy（当前协议）

所有 P2 Agent 执行 Connector 必须实现异步 `start`、`events`、`inspect`、`cancel`、`recover` Port：

- `start` 启动或解析幂等执行。
- `events` 从可选 Provider cursor 后输出标准化事件。
- `inspect` 只观察活动状态。
- `cancel` 请求取消，不直接写领域终态。
- `recover` 只恢复已引用的执行；找不到时不得创建替代执行。

`SessionPolicy` 只允许 `new`、`reuse`、`fork`。Capability 使用小写不透明名称并做显式集合匹配；
未知 requirement 可以保留，但在 Worker 未显式声明时必须 fail closed。具体 WorkerProfile、Endpoint、
SessionRef 和 ExecutionRef 结构由领域对象与公开 Schema 定义。未来服务与事件 Connector 不强制采用
此 Worker 协议；Routine 通过应用入口发起任务，不能绕过同一批准、执行和恢复规则。

以上是当前执行句柄协议，不是阶段会话的完整实现。目标为阶段内共享 Session、跨阶段可新建；
有效 handoff 可以交给新 Session，经新的显式执行记录接手，不把新执行伪装成 `recover` 找回的旧句柄。

## StartRun 行为

P2 的 `StartRun` 必须在同一个事务中持久化新 `Run`、pending dispatch work 和 Command receipt，提交后
立即返回，不在请求调用栈中执行 Worker。相同幂等键和相同 fingerprint 必须返回同一个 `Run`，不得
创建第二个 Run 或第二份 dispatch work；同键不同 fingerprint 必须冲突失败。

上述行为适用于后台模式，不应误读为现有同步 CLI 已具备独立后台宿主。
R2 的 `execute-plan` / `resume-session` 拥有前台宿主生命周期，`get-result` 只读查询交付；
不能由验收 Harness 隐式补齐生命周期。API 的后台宿主仍由启动它的进程负责关闭。

## Built-in Agent Provider 入口

P2 Built-in Agent 的唯一真实模型入口固定为官方 OpenAI Python SDK 的 Responses API。凭证只允许保存
引用 `env:OPENAI_API_KEY`；不得把密钥写入 PlanGraph、Event、Artifact 或数据库。模型名必须由
`WorkerProfile` 显式提供，不允许从环境、登录态或 Provider 默认值推导。
Provider capability、Role Tool Profile、凭证引用与 workspace 必须经正常装配入口传入；
测试专用配置不构成用户可用的配置方式。

## 失败后的目标选择规则

- 仍有可行路线时，依据获批条件比较、继续或取消局部分支，不自动猜测用户偏好。
- 自动修复和重试必须在授权、预算及副作用安全范围内。未知写入结果不盲目创建替代执行；
  Check/Gate 失败不能通过 resume 直接跳过验证。
- 全部分支阻塞、某步尝试耗尽或无法决定取舍时，应停止相关自动调度、保留状态和产物，挂起求助。
  不得要求模型伪造空 BranchSelection，也不一律以终态 failed 代替等待用户。
- 挂起需记录阻塞任务、尝试和证据、具体问题及可选行动；用户回复后继续原方案或提出修订。
- 改变需求、接口、Gate 或授权必须重新批准；仅改变中间过程可以在批准基线下自主修订并留痕。
  证据来源显式选择，不猜测“最新 Run”，保留原计划、执行和产物 lineage。
- Checkpoint 保留已通过 Gate 的恢复事实；它不是整个文件系统或外部服务的任意回滚快照。
  恢复与继续执行分开，不能重复未知副作用或无条件重新运行已完成任务。
- 用户明确放弃、取消或确定不可恢复时仍可终止；终止、暂停和等待人工决定应可区分。
- 可实现性 gap 通过便签向用户说明；无法自动验收的条件交给人工 Gate，不用弱检查冒充。
- 有宿主确认的 handoff 可由新 Session 接手；无 handoff 的脏状态回到相关最近完成节点，
  不回退无关并行成果。禁止不可恢复操作，约束覆盖所有副作用工具，不只检查命令名称。

### 当前实现与待迁移限制

现有基线的 `retry` 采用保守 RetrySafety；`resume` 继续 paused Run，Checkpoint restore 后保持 paused。
Replan 仅支持 failed/cancelled 来源，所有探索分支失败会收敛为 failed。后两项是待迁移的原型行为，
不再是目标产品规则。实施人工回路时必须定义状态转换、在途 Worker 收敛、用户回复入口、旧数据库兼容
和新旧 Run 的关联，不能仅把 failed 文案改成“挂起”。
自动选择更符合偏好的分支也须经批准条件和 Orchestrator，不从本契约推导出已经存在提前取消工具。

## 方案与验收契约

Planner 可提出需求、设计和验收建议，获批方案将其绑定到明确的版本。公共 builder/校验边界创建领域
CheckSpec 和 CompletionContract，模型不能绕过它直接推进状态或在执行中改写条件。
目标按分支、阶段和最终成果定义 Gate；自动与人工判定统一，阶段 Reviewer 负责测试审查，不取代 Gate。
没有验收职责的小任务只交接，不要求每个中间节点都满足最终 solution 的全部条件。
过程自主调整须保持获批 Gate，不能新增、删除或绕过验收来制造成功。

### 当前 R2 的自动节点 Gate 实现

R2 的 Built-in
proposal 允许中间节点的 `required_check_ids` 为空；宿主在候选证据已持久化后调用 `accept_intermediate` 完成
任务交接。该交接不创建假 Gate 或 Checkpoint，不满足 Goal，也不能让最终节点跳过 Gate。
最终单一 integration/terminal 节点持有 CompletionContract 的 required Check IDs；中间 `work` / `merge`
节点可有自己的 required Check IDs。它们的不可变 CheckSpec 同属获批 PlanRevision，但不加入最终
CompletionContract，避免要求被剪枝的替代路线也通过。required Check 必须被计划节点引用，
契约的最终 IDs 必须包含在真实 required Checks 中；不允许创建未被执行图使用的“必需”检查。
各 Gate 在对应节点的实际代码工作区上运行，后继按节点完成状态推进；没有独立的 Phase 状态对象。
探索节点必须等待 fork 交接完成后再调度。中间候选落库与接收之间若发生中断，恢复可继续接收已持久化的
成功 Attempt，不重复调用 Worker，也不生成假检查。SQLite 仅允许有成功候选证据的无检查中间节点直接完成交接。
评估器的候选在 Built-in 提交工具中使用宿主同一规则预校验；遗漏比较证据时返回可修正的工具错误，
不得以放宽证据覆盖规则来接受结果。
现有三种检查可以组合，配置随契约持久化。
Artifact 非空、命令成功或指定词匹配只能证明具体检查项；代码完成需要需求相关行为及交付证据。
宿主 Check Runner 在候选提交后执行配置的检查；Planner 不应额外创建仅用于重复这些检查的 Worker 节点。
宿主检查 argv 不等于 Worker 的工具执行授权，不能要求 Worker 绕过自身 Tool Profile 执行它。

Planner 可以通过 `set_final_gate` 提出最终行为命令 argv。应用层将其写入 `PlanTemplate.final_gate_argv`，
再冻结到 `CheckSpec.command_argv`；用户批准的 PlanRevision/CompletionContract 是执行时的真值，不再依赖
一个只存在于宿主配置中的模板。显式 host argv 若与 Planner 提议冲突，必须 fail closed，不能静默改写批准内容。
`set_node_gate` 通过 `PlanTemplate.node_gates` 将独立命令绑定到中间节点；显式 host argv 只作用于最终
检查，不覆盖局部 Gate。已有原型 criterion 未选择 command 时，真实 final_gate_argv 会在草稿中追加
command 条件并保留原条件，不再被静默丢弃。CLI 编码讨论默认 command；HTTP 既有 criteria 字段不变。

## R2 代码交接与工作区（当前实现与迁移）

`CodeRuntimeConnector` 包装既有 `RuntimeConnector`，在启动 Attempt 前通过 `GitCodeWorkspace` 准备 EHAI-owned
worktree：Run 首次固定 base commit，依赖节点和已选分支的代码提交作为上游合并；缺失上游快照或冲突时不能
伪造候选。候选返回时由宿主从实际 worktree 捕获 `GitCodeResult`，并附加受保护的代码快照与 diff；模型不能
冒充这些 host-owned code artifacts。完成、停止或恢复时保留可核对的 worktree/metadata，供最终 Gate 和交付查询
使用。

当前停止时可捕获未提交的部分代码，快照可能含缺失文件，只作为调查材料。
`CodeRuntimeConnector` 准备续跑时仅复用同节点 succeeded Attempt 的宿主代码快照；没有成功提交的
交接证据则使用已完成的有效上游或 Run 基线，不能因曾分配 worktree 就捕获/继承未交接状态。
已提交交接的快照缺失时拒绝继续。此处 succeeded 表示宿主接受候选，不表示其 Gate 已通过；
失败 Gate 的已提交候选仍可用于按原 Gate 修复。独立 handoff 文档模型与阶段 Session 尚未接入。

## Responses 端点适配与请求证据（2026-09-08）

Built-in Planner/Worker 使用同一 ModelClient 端口。端点能力包括续接与查询开关：
禁用 `supports_previous_response_id` 时重放本地保留的消息及工具结果；
禁用 `supports_response_retrieval` 时不调用 retrieve。它们描述 HTTP 传输能力，不改变业务 Session。
使用哪个 Sub2API 部署或域名本身不构成能力保证；当前测试配置见
[端点实测及适配](../spikes/aws-sub2-responses-capabilities.md)。

每轮模型调用可产生多条 `model/transport` Session Event，关联逻辑请求与 HTTP 请求，
并记录状态、服务端 request ID、终态与失败/回退。这些事件只作证据，不作为模型消息重放，
不改变既有 Turn/Step 状态迁移。正常内置 SDK 的自动重试关闭，HTTP 失败恢复由 Adapter 明确处理。
查询接口有截断标识，不能把截断后的事件列表当成完整请求统计。

无法恢复且结果未知时，不提交候选或自动重新创建响应：Built-in Connector 发出 WAITING，
前台暂停 Run 并返回 `provider_outcome_unknown` notice，供人核实后显式决定继续。
这复用现有等待/暂停状态，不是已交付通用人工 Gate 或跨方案恢复。
旧执行配置缺省两个新字段时保持历史行为，已有授权不被静默扩大或替换。

最终 Gate 检查的是最终 integration 节点准备好的实际合并工作区，而不是模型文字报告或单独的候选 Artifact。
Planner 的图预算与 Worker 的模型执行预算相互独立：Planner 使用有限的图操作/校验和 `ExplorationBudget`；
默认 Built-in Worker 使用 `LONG_RUNNING_AGENT_BUDGET`，其模型步数、Tool 次数、wall-clock 和输出上限可不设，
但仍受取消、heartbeat、显式 deadline、Provider 终态和恢复策略约束。
代表性真实试用及其边界见 [R2 Implementation Plan](../R2_IMPLEMENTATION_PLAN.md)；
本节是实现契约，不是长时间连续运行或完整产品 E2E 的验收记录。

R1 的 `discuss-plan` / `POST /planning/discuss` 在执行前接收讨论。用户消息和模型回复通过现有 Event Log
关联为一个逻辑 conversation；每轮引用共享 Runtime 的执行 Session，不创建 Worker Attempt 或 Run。
澄清回复不产生 PlanRevision；形成方案时设计与图一起保存。修订草稿或已批准方案均创建新版本和
CompletionContract，旧批准不能批准新内容。`design_document` 属于不可变计划结构，Checkpoint、查询
和 Worker 上下文保留该版本内容；旧存量计划按 null 读取。

同一消息幂等键在模型调用前持久领取，重复请求不会再次调用模型。失败可查询；进程中断的未知结果
不自动重放。当前不在 active Run 的 Goal 上修订，运行期人工回路仍归后续 R3，不把规划回复冒充执行恢复。

## P1 迁移保护

P2 的数据库迁移必须从当前 P1 schema version 2 单向前进，并在 P1 数据库备份副本上验证。I0 不新增
表或改变 schema version。后续迁移不得削弱以下已持久化语义：

- Attempt 必须属于同一 Run 的 PlanRevision 中的 PlanNode。
- 最终 Check/Gate 必须引用同一 Run、PlanNode、Attempt 和证据 Artifact；Worker 成功不能绕过最终 Gate。
- Evaluator 的比较证据必须覆盖候选分支，Merge 只能读取明确选中的 Artifact。
- Checkpoint 必须引用确定的 PlanRevision、Run、通过的 Gate Event offset、分支选择和 Artifact。
- 恢复只能使用已持久化的最新 Checkpoint 和原执行事实，不得重复外部副作用或创建替代执行。

旧 P1 exploration、幂等和 recovery 测试已退役，不是要求恢复的长期测试套件。迁移仍需保护上述事实：
在备份副本上核对实际迁移结果；发现失败时才在仓库外写必要的临时定位单测，遵守当前测试策略。

## 已有 Built-in Planner 图操作 Tool 契约（P2-I14）

本节定义图构建协议，不是完整 Planner 产品接口。只读仓库调查、多轮用户对齐和方案审查在同一规划
能力中补齐；图校验通过不等于方案已被用户批准，也不等于需求被正确理解。

Built-in Planner 不再通过一次性 `submit_plan` 固定双分支模板。模型在本次 Planner 调用的普通内存图
中直接调用图操作 Tool 构造 PlanGraph：`add_plan_node`、`update_plan_node`、`remove_plan_node`、
`add_plan_edge`、`remove_plan_edge`、`set_plan_branch`、`set_plan_phase`、`set_node_gate`、
`set_final_gate`、`inspect_plan`、`finish_plan`。Tool 操作不经过
HTTP 回调、不立即写数据库，也不存在 Plan IR/Operation/Patch 第二套图表达。只有 `finish_plan` 完整
校验通过后，应用层才通过现有 `build_plan_proposal()` 分配 UUID 并创建 Phase、CheckSpec、CompletionContract
和 draft PlanRevision；模型使用稳定本地 key，永远不生成 UUID。依赖只有一个模型侧真值：模型通过
`add_plan_edge` 声明 dependency，`finish_plan` 时确定性派生
`PlanNodeTemplate.required_dependency_keys`。

Built-in 图必须有一个最终 integration sink，所有工作路径都指向它；普通 dependency DAG 可以有多个并行
前驱并在该节点收敛，替代方案则使用 fork、至少两个分支、evaluator 和 merge。`set_final_gate` 接收非 shell
的 argv 数组。应用层在 Built-in final-node 模式下只把全部 required Check IDs 绑定到该 sink，中间节点可以
保持空的 `required_check_ids`，由宿主以候选证据完成任务交接。

预算语义固定为：图修改操作共 128 次（被拒绝的修改同样计数），`inspect_plan`/`finish_plan` 不计数；
达到 128 次后不再接受修改但允许 inspect 和最后一次 finish；前 4 次 finish 完整校验失败把带本地 key
位置的 diagnostics 返回给模型并允许继续修复，第 5 次失败才以明确的 validation budget exhausted 终止。
diagnostics 结构为 `{"accepted": false, "issues": [{"code", "location", "message", "related_keys"}]}`，
错误位置必须使用模型能继续操作的本地 key，不能只返回 UUID 或 Python 异常文本。模型不得创建、删除、
降低或绕过 CompletionContract 与 Check；最终节点必须保留全部 required Check，中间节点不通过假 Check 宣布
Goal 完成。

## Provider 长程超时语义（P2-I14）

timeout 是连接/无进展检测，不是短时总任务寿命：

- Built-in Planner 默认不设 Tool Loop 的固定 wall-clock deadline；正常终止由 128 次图操作、4 次校验
  修复、明确取消和 Provider 终态决定。
- Planner 的 `ExplorationBudget` 和内部 `AgentBudget` 仍限制图规模、模型步数、Tool 次数及输出；这与默认
  Built-in Worker 的 `LONG_RUNNING_AGENT_BUDGET` 不同，不能用 Planner 的有限预算解释或截断 Worker 的长任务。
- `OpenAIResponsesModelClient` 默认 stream idle timeout 300 秒；HTTP 请求失败最多重试 4 次；流中断
  最多重连 5 次。已获得 response_id 时必须 retrieve/恢复同一个 Response，不得重新创建重复 Response。
- queued/in_progress 表示仍在执行并继续等待；只有 completed、failed、cancelled、incomplete 等明确
  终态，或恢复次数耗尽，才结束本次 Provider 执行。
- 官方 Responses Background Mode 可用时，Built-in Planner 优先 background response + retrieve；兼容
  端点明确拒绝 background 时回退 stream=true、store=true 与现有 continuation 机制，回退不得静默创建
  重复请求或重复执行图操作。
- `ExecutionPolicy.absolute_attempt_timeout` 与 `max_run_duration` 默认 None，即绝对截止时间为调用方
  显式配置项（`ehai-api --attempt-deadline-seconds`）；heartbeat、no-progress、lease、cancel、用户
  显式 deadline 和 RetrySafety 对未知外部副作用的限制全部保留。

## 通用 Built-in Agent Runtime 目标契约（P2-I15–I18）

以下为复用约束；Foundation 分支有相关实现，角色与工具在正常入口中的可用性仍需分别验收。

Built-in Agent Runtime 是选择 Built-in 框架的 Role 的统一 Agent 地基，不把职责限定为只能使用 Built-in。
Planner、Worker、Evaluator、Merge、Visualizer、Reviewer 和 Assistance 选择 Built-in 时复用同一 ModelClient、Session/Event Store、Agent Loop、
Tool Registry、取消、预算、恢复与 Trace；Role 差异只能通过 Prompt、Tool Profile、Context Builder、
Finish Tool 和权限表达。任何模块不得为了特殊输出协议复制模型循环或另建 Session Store。

Runtime 提供统一能力目录，但 Session 只能获得 Role、Endpoint 与用户策略共同授权的 ToolSet。ToolSet 在
Session 创建时冻结并随恢复事实持久化；新增 MCP Server、Skill 或宿主 Shell 不能静默改变已存在 Session
的工具和权限。Skill 只提供指令与资源引用，不授予能力；MCP/Web/Shell/Git 结果都必须进入相同的有界、
脱敏 Tool Event。

Workspace 写操作只能作用于分配的 Workspace/worktree。统一文本 diff 可以创建、修改、删除和移动文件；
Shell 由 Endpoint 声明，Git 使用结构化 argv 并按只读、本地写、远端写和危险操作分类。能力存在不构成
授权：远端写、commit/merge/rebase、破坏性 Git 和越界文件操作必须遵守显式策略与审批。Tool 成功不等于
任务完成，角色仍须使用其 Finish Tool，Worker 结果仍须经过 Check/Gate。
工具支持删除/移动不是不可恢复操作的授权；目标中的恢复保障必须覆盖这些路径，现有命令分类不能代替它。

Session Mailbox 只传递持久消息与 correlation，不共享可写 Workspace、不替代 Artifact，也不推进
PlanNode/Attempt/Run 状态。跨 Session 协作的产物必须通过 Artifact 引用；Orchestrator 仍是执行领域状态
推进的唯一入口。Visualization Role 只能从已校验 PlanRevision 派生可视化 Artifact，不能修改计划真值。

本阶段提前 MCP/Skill 的“运行时消费能力”，不提前 P5 的插件 SDK、市场、热安装、第三方 Agent Framework
或跨主机分布式协调。未来顶层通用 Agent 选择 Built-in 时也复用此地基，通过平台能力协助用户；它不是 Planner 别名，
不能因定义 Assistance Role 就宣称已经实现平台交互产品。
