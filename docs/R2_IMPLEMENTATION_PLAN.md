# R2：获批方案到代码

## 依据与边界

依据 2026-09-05 的实现记录，并按 2026-09-06 用户确认的
[Execution Model](EXECUTION_MODEL.md) 更新目标，归属于 P2，不是新的产品阶段。
产品定义见 [Product Scope](PRODUCT_SCOPE.md)，R1–R4 的关系见
[P2 Implementation Plan](P2_IMPLEMENTATION_PLAN.md)。

- 开发在当前 EHAI worktree、`codex/ehai-p2` 分支进行，按逻辑能力提交，不修改其他 worktree 的 `main`。
  这是本次开发位置，不是要求所有产品 Worker 共同修改同一个目录。
- 先接通 Built-in Agent，再使用 Codex App Server 调用 Codex；桌面展示、接管 UI 不在 R2 范围内。
- 同时支持不同任务的并行分工，以及同一问题的多分支尝试。已有调度、存储和 Connector 优先复用。
- 先开放正常入口并实际试用，不新增常驻测试；最终唯一产品 E2E 在 R4 与用户确定。

## 用户结果

用户审查阶段决策树，批准需求、接口、阶段/分支 Gate，首次执行时确认 workspace、Worker 预设和工具授权。
EHAI 在隔离 workspace 中执行和整合代码，返回可审查的代码差异、成果位置、验收结果和执行轨迹。
阶段内共享 Session，跨阶段可新建。CLI 关闭即停止，重开时有有效 handoff 则交给新 Session；
无 handoff 的脏状态退回相关最近完成节点。当前保留 Run 并新建 Thread 的实现不等于该目标已经交付。
整体会话不以五六小时或任意的小步数上限自动结束。

## 实施单元

| 单元 | 输入与职责 | 输出与完成证据 |
| --- | --- | --- |
| 详细编码任务 | Planner 调查仓库，明确文件/接口、具体步骤、依赖、并行依据、局部自由度和比较标准 | 小模型 Worker 不必重新设计整个方案；可审查的节点说明和执行图 |
| 阶段决策树与 Gate | Planner 规划阶段和独立探索路径，各分支/阶段有相应自动或人工 Gate | 按各 Gate 推进、修复或结束分支，不要求只有最终节点才可验收，也不逐任务堆检查 |
| 阶段 Review Agent | 进入该阶段 Gate 时进行测试和审查，提交证据与修改建议 | 不绕过 Gate、不替代必需人工判定、不自行增加或降低标准 |
| Worker 实例化 | 按预设经 Connector 创建 Built-in 或外部框架的 Agent 实例 | 框架、职责、模型与 Session 分开，不把 Worker 等同于任务节点 |
| 代码 workspace | 宿主固定运行基线，Worker 接收适用的上游代码；不同探索路线隔离 | 实际文件变更和可恢复的 Git 快照，而不只有候选文本 |
| 并行整合 | 调度器处理依赖和容量；宿主整合上游版本，冲突交给 Worker 依据方案解决 | 不遗漏并行成果、不继承已剪枝路线、不从过期基线重新编码 |
| 修复与交付 | 中间结果交接不代表整个 Goal 完成；最终 Gate 失败反馈给 Worker | 在原批准范围内修代码并重新验收，不降低 Gate；最终 diff 与结果可查询 |
| 会话与授权 | 阶段内共享上下文、跨阶段交接；首次授权，禁止不可恢复操作 | 有 handoff 可接手，无 handoff 回到相关已完成节点；保留其他并行成果 |
| 外部 Worker | 接入现有 Codex App Server Connector 的启动、事件、取消和恢复协议 | 正常配置入口可选择 Server Worker，不退回 `codex exec` 代替 |

## 执行与失败语义

Planner 的任务说明至少包括目标修改、接口约定、实现步骤、依赖输入、预期输出、范围和允许的局部调整。
分工节点必须说明依赖，探索分支必须有获批的比较依据及选中结果的整合位置。
最终验收节点必须覆盖所有有效任务；不能让一个旁支通过后提前完成整个 Goal。

无验收职责的中间节点只做宿主确认的交接，不创建假 Gate；分支、阶段的验收节点则运行其真实 Gate。
阶段 Reviewer 测试审查后，由对应自动/人工 Gate 判断。最终 Gate 对整合成果负责，不用候选文字代替真实代码。
保持需求、接口、Gate 和授权时，Planner 可自主改变拆分、依赖和实现路线；只有改变批准底线才重新批准。
发现现实与目标的 gap 用便签提醒；代码修复仍使用原 Gate，不能把标准变化伪装成普通重试。

Worker 可以明确报告阻塞，必须给出原因、证据及需要的条件，不提交伪造成功产物。
基础 notice、CLI 停止和会话续接属于 R2；复杂人工问答、授权变更和跨方案恢复仍属于 R3。
外部调用的故障检测与整体会话时限分开：允许检测失联，但不能因长时间推理直接假定任务失败。
任何未知副作用仍先核实，不以“长运行”为由无限重复外部写入。

首次执行授权包括 Worker 配置、workspace、命令和工具范围，之后不静默扩大。
不可恢复操作的禁止规则必须覆盖命令、patch、删除与覆盖等副作用路径，不能只禁 `rm` 或依赖提示词；
R2 不把可自由执行的 Shell 黑名单宣传成操作系统级隔离。

当前目标优先尽可能并行，主规划偏好 GPT-6 Astra，明确执行任务偏好 GPT-5.6 Luna/max；
阶段 Reviewer 保留，其模型由预设表达。下文模型与单一最终 Gate 等信息是历史实际配置，不改写历史。

本轮共识尚需对齐 Worker 实例映射、动态过程调整、阶段/人工 Gate、阶段 Session、handoff 与安全回退。
此前普通 CLI 编码和恢复通过，不能证明这些新目标已完成。

## 验证与提交

### 2026-09-06 首个增量：多节点自动 Gate（代表性实跑通过）

输入为正常 Planner 讨论中的阶段/路径验收需求；Planner 用 `set_node_gate(node_key, name, argv)`
给中间工作或整合节点设置自己的自动行为 Gate，用 `set_final_gate` 设置最终成果条件。
输出沿用 PlanGraph 的 `required_check_ids` 和不可变 CheckSpec，不先创建没有运行语义的 Phase/Worker 空壳。
局部 Gate 只验收所属节点；最终 CompletionContract 不要求被剪枝的替代路线也通过。
Orchestrator 继续负责检查、状态、Checkpoint 和推进，后继必须等待所需节点 Gate；调用方不得绕过批准。
验收从正常 `discuss-plan`、查询、批准和 `execute-plan` 进入，检查实际中间/分支 Gate 及最终 Gate 的轨迹。

本增量同时避免 Planner 的最终命令被原型 criterion 静默丢弃，并为编码讨论提供行为验收默认值。
实际试用后，代码恢复也已改为只选成功提交的候选快照；未交接的中断尝试回到有效上游，不继承脏快照。
人工 Gate、阶段 Reviewer、显式 Phase/共享 Session、Worker 实例与过程自主调整仍是后续增量，
不能将本次自动 Gate 接通描述成整份执行模型已经交付。

当前证据（2026-09-06）：

- Astra/high 经正常 CLI 生成七节点、四 Gate 的草稿；审查后去除 mock 和过细 README 条件，
  批准版本 `355f2a31-5da2-4ccb-b65b-e257f20d2910`，对应一个上游 Gate、两条路线各自 Gate 和最终 Gate。
- Run `104427ed-4c75-4943-ada6-19dd9f6f7d88` 首次执行暴露 WorkerRequest 仍把局部 Check 限制为
  最终 contract 子集，已修为按本节点精确匹配。恢复又暴露对无准备记录中断尝试的错误捕获，
  已改为依据成功提交的交接选择代码。两个缺陷均由真实入口发现，仓库外最小诊断修复后共三项通过。
- 用户随后明确认可测试端点，使用同一 Run、获批 PlanRevision 和原四条 CheckSpec，
  通过正常 `resume-session` 恢复；Built-in `gpt-5.6-luna/max`、capacity 3，
  没有扩大 Worker 的工具权限。Run 于 2026-09-06 10:06:03 UTC 完成，七个节点均有成功 Attempt。
  共十一条 Attempt 包含修复前的失败、中断和取消记录，不将其抹去或统计为首次成功。
- 四个自动 Gate 均实际执行且 exit code 为零，执行 argv 与获批版本逐项一致。
  上游 names Gate 通过后才启动 fork；两条编码路线实际重叠约 46.81 秒，各自 Gate 完成后才进入 evaluator。
  最终 Gate 仅在整合成果上执行一次；此前成功的 README Attempt 未重跑，成果被保留。
- evaluator 选择 conditional 路线并剪枝 mapping 路线。最终 commit
  `0e98203b7efd5da095cc45c792dc6d30e26a5533` 的祖先包含选中路线、不包含被剪枝路线，
  `greetings.py` 与选中候选一致；相对运行基线只交付 `names.py`、`greetings.py`、`README.md`。
  临时原仓库 HEAD 仍为 `216130e5f4856b1b8fb265a3f5753a7c98ed4691`，工作区干净；
  执行进程退出后，正常 `get-result` / `get-trace` 仍可重读成果及证据。
- 仍存在审查证据交付缺口：evaluator 当前拿到候选 Artifact 和分支可用状态，
  未拿到逐条局部 Gate 的完整输出；本次另从宿主持久化轨迹核实四 Gate。
  这不是阶段 Review Agent 已接通，也不代表人工 Gate、阶段共享 Session 或完整产品 E2E 已验收。
- 证据位于系统临时目录 `ehai-node-gates-4311a309e53747ebb4978e3a12bb30a7`：
  `trace-final.json`、`result-final.json` 和 `verified-evidence.json` 保存轨迹、交付及核对摘要。
  仓库外三项临时诊断、Ruff 检查/格式和 mypy 均通过；没有新增常驻测试。

每个提交保持代码、公开契约和使用文档一致，使用 Conventional Commits，不推送或合并到 `main`。
静态检查与本仓库开发提交规则继续适用，不自动成为产品编码任务的验收要求。

### 2026-09-06 端点协议专项验证

针对代理后台出现错误但模型逻辑调用最终成功的现象，逐项验证用户授权的 aws-sub2 端点。
十九次 HTTP 请求确认当前模型、流式工具交互与本地上下文重放可用，同时发现服务端续接、
background、uniqueItems、查询、非流式形态和幂等去重的限制或差异。
详见 [Responses 能力实测](spikes/aws-sub2-responses-capabilities.md)，其中保留请求 ID、
诊断脚本输出读取修正及当前配置/待实现适配的边界。
本次仅追加文档，临时协议诊断不入库；不把模型返回成功或 Run 完成等同于每个 HTTP 请求健康。

### 2026-09-08 Sub2API HTTP 适配与最小链路复测

用户确认端点经 Sub2API 反代后，按实际 HTTP 能力配置新增续接与查询开关，不写死域名。
禁用续接时直接重放本地上下文；不能查询或保证幂等创建时，未知结果不自动重新 POST。
Built-in Worker 等待并由前台暂停、通知；正常 CLI/API 可查询持久化的 `model/transport` 事件。
两个新能力默认保留历史行为，已有 Run 的授权配置不静默替换。

真实 Luna/max 三轮工具交互通过：三次 POST 全部 200 与 completed，没有兼容性 400 或额外回退；
十八条传输事件重载后可与线上请求对账，工具参数和完整历史重放一致。
证据、参数与测试边界见 [端点适配记录](spikes/aws-sub2-responses-capabilities.md)。
本次没有重跑完整编码任务，也没有新增常驻测试；不据此宣布 R2/P2 整体完成。

### 2026-09-08 七节点 CLI 端到端复跑（通过）

从干净临时仓库开始，经过正常 `create-project` / `create-goal`、
Astra/high `discuss-plan`、查询与审查、`approve-plan`、Luna/max `execute-plan`，
最后在执行进程退出后通过 `get-result` / `get-trace` 重读结果。
用户明确授权把临时示例代码及上下文发送到指定 Sub2API 端点；没有发送 EHAI 产品源码。
审批后的执行首次曾被外发权限检查阻止启动，用户确认后才运行；不将这段等待算成模型失败或运行时长。

- PlanRevision：`2a928b14-9af4-490f-8a80-3c9a2239fd8e`；
  Run：`18b94c25-e9ae-4c45-9a01-afa83779fc84`；
  Goal：`8e1908b7-9950-4d7c-b47c-1f3744ce285d`。
- 执行窗口为 2026-09-08 02:25:19–02:32:42 UTC（北京时间 10:25:19–10:32:42）。
  Run 为 completed，Goal 为 satisfied；七个 Attempt 均 succeeded，四个 Gate 均 passed。
- 四条 command argv 与 2026-09-06 已审查版本逐字符一致；执行前后 CheckSpec、
  节点指令、依赖、检查绑定与 edges 一致，没有通过降低验收或改计划制造成功。
- names Gate 完成后才进入 fork；条件与映射路线实际并行重叠约 38.91 秒，
  各自 Gate 完成后才进入 evaluator。最终选择条件路线，剪枝映射路线。
- 最终 commit 为 `e33e965a1801b06522b5371e2b34c0292432411c`，
  Git 祖先包含选中候选而不包含被剪枝候选；最终 greetings.py 与选中候选一致，
  相对基线只变更 `names.py`、`greetings.py`、`README.md`。
  独立源码核对确认 normalize_name 调用关系、keyword-only locale、英中输出与 ValueError，
  不只依赖模型报告。原示例仓库 HEAD 仍为 `216130e5f4856b1b8fb265a3f5753a7c98ed4691`，
  工作区干净，结束后没有匹配本轮的遗留执行进程。
- 完整持久 Session 记录中，Planner 为 32 次逻辑调用 / 32 次 HTTP，Worker 合计为 42 / 42。
  总计 74 个 HTTP 200、74 个 response.completed、444 条 model/transport 事件；
  error、非 200、fallback、retry、unknown_outcome 均为零。Worker 的公开 trace 未截断；
  统计同时读取了 Planner Session，不能只把 Worker trace 当成整个端到端调用量。

**过程并非所有操作都一次成功。** Worker 出现三次可恢复工具错误：
两次 `workspace_list` 把 `/` 当工作区路径而被边界检查拒绝，
一次映射路线 `workspace_patch` 的 expected 文本未精确匹配。
Planner 另有两次工具返回 accepted=false：`finish_plan` 提示缺少两条分支的 merge edge，
随后一次 `add_plan_edge` 给非 conditional edge 附加了 condition。
Agent 根据反馈自行纠正；这些是工具/规划层反馈，不是 HTTP 错误或 Gate 失败。
最终 greetings.py 与 README.md 还有多余空行，行为 Gate 不检查排版；
本轮未手工美化模型成果，也没有添加 linter Gate 把它混入本次验收。

证据位于 `%TEMP%\ehai-sub2-e2e-090ee935313a4002835ab84bbd2c90be`：
`discussion.json`、`approval.json`、`plan.json` / `plan-final.json`、
`checks.json` / `checks-final.json`、`execution-result.json`、
`result-final.json`、`trace-final.json`、`verified-evidence.json` 以及 `state.sqlite3`。
Luna/max 子 Agent 执行只读审计并核对最终代码；临时审计脚本修正了角色关键词误匹配，
以及“Gate 必须位于 Attempt 时间窗内”的错误假设。实际顺序是宿主接收候选后运行 Gate，
仍核对同一 Attempt、节点、原命令和实际结果，没有修改生产状态或验收标准。

这证明该七节点场景在当前 Sub2API 配置下端到端成功且记录的请求链路无异常；
不等于任意编码目标、主动断流/关窗恢复、人工 Gate、阶段共享 Session 或整个 P2 已验收。
没有新增常驻测试或为此次通过修改生产源码；本记录不改写此前试用中的失败历史。

能力接通后通过正常 CLI、真实 Worker 和临时目标仓库试用。只在观察到实际失败时，
在仓库外建立必要的临时定位测试，修复后重跑原路径。真实凭证不进入配置文件、Git 或产物。
报告分别标明接口实现、实际试用结果、未验证项；不以文件数或静态检查通过宣布 R2/P2 验收完成。

## 本轮实现与验证记录（2026-09-05）

- 已实现详细编码任务与 Planner 提议的最终 Gate、无虚构 Gate 的中间交接、隔离代码快照和上游版本整合、
  最终失败反馈、长会话输入窗口、前台 `execute-plan` / `resume-session` / `get-result`。
- Built-in 和 Codex App Server 已接入正常配置入口；Server 当前启动自有 stdio 进程，不宣称附接桌面进程。
- 真实 Planner 经正常 CLI 调查临时三文件仓库，输出包含并行任务和两个探索分支的八节点方案。
  审查发现中文标点误改、探索只输出设计后，通过原讨论入口修订为实际代码候选和精确最终 Gate；
  v2 已批准，只有最终节点绑定检查。
- 首次 Worker 执行曾被权限系统拒绝，要求确认向第三方测试端点发送临时仓库、方案及工具输出。
  当时未绕过限制，Run、Attempt、Workspace lease 均为零；用户随后明确授权，继续记录见下节。
- 仓库外真实 Git 定位检查通过：捕获非忽略的新增/二进制文件、排除忽略文件、整合两个独立版本，
  同时保持用户基线和工作区不变。它只定位代码交接模块，不是产品 E2E。
- Ruff、格式、mypy、客户端生成、TypeScript 类型检查和构建通过；本机已确认存在 App Server 命令入口。

证据目录位于系统临时目录 `ehai-r2-live-f51ebfdf22e5486ab34e3f63d96cba5d`，不入库。

### 授权后的真实 Built-in 编码试用

用户明确授权后，正式 CLI 使用 `gpt-5.6-luna/low` Worker、capacity 3；没有 Shell 或 Worker 命令执行权限，
宿主只执行已批准的最终 Gate。全过程没有用假模型、预写代码答案或直接修改运行状态来代替产品行为。

首个 Run `d37aac54-6708-4a02-b78a-d26b55623a09` 暴露并修复：

1. 多 Worker 同时初始化基线文件的 Windows 写入冲突，改为同一宿主串行固定一次基线。
2. 探索节点提前调度，改为等待 fork 完成及其代码快照可用。
3. SQLite 拒绝新的中间交接状态迁移；同步持久化约束，并通过 `resume-session` 接收已保存的候选，
   没有重做已成功的 fork 调用。
4. 评估器提示和宿主规则对比较证据覆盖要求不一致；对齐全部候选 Artifact 的精确要求，
   并在提交工具里预校验，使 Worker 能根据缺失 ID 修正，而不是先结束会话再遇到协议错误。

前三项按实际失败使用仓库外临时诊断定位。原 Run 的失败历史保留；随后经正式 `replan-plan` 和
`approve-plan` 生成 v3，最终 Gate argv 与 v2 完全相同，没有降低验收标准。

- v3 方案：`68cb6df2-a442-40a2-9294-d8fcfabcb4d5`。
- 成功 Run：`6ca6f959-8b05-4706-acde-6f37b6ef7160`。
- 8 个 Attempt 全部 succeeded，运行中达到 3 个并发槽位；仅最终节点产生 1 个 CheckRun、1 个 Gate 和 1 个 Checkpoint。
- 两条路线均实际修改 `greetings.py`；选中显式条件分支实现，映射实现被剪枝。
  Git 血缘确认选中提交进入最终结果，被剪枝提交没有进入最终结果。
- 最终成果 commit：`1f04ebb7b13701adc69b93e870c72bc1c7fd7c2f`；
  保留在临时目录的 `worktrees/4d70e337-f6e6-40f4-b6e5-cab66817fe93`，只有三个目标文件的代码差异。
- 原目标仓库 HEAD 仍为 `72966b18e5741a90d4c7298b2d0ad875a21c0aaa`，工作区无修改。
- `get-result` 在执行进程退出后仍能查询成果和通过的 Gate。证据包括 `execution-v3.json`、持久轨迹、
  分支代码提交与 `solution.patch`。

以上试用证明代表性的 Built-in“方案到代码”真实链路，当时尚不覆盖 Gate 失败后的完整自主修复、
主动关闭/重开、5–6 小时运行和 Codex App Server 实际 Worker 调用。R2 尚未标记总验收完成，
最终产品 E2E 也尚未创建。

### 固定 Gate 修复与前台暂停恢复（2026-09-05）

本轮仅补充上述两条路径，不开展 Server 实跑或长时间运行。通过正常 Planner 讨论、审查修订、
批准及前台 CLI 使用真实 `gpt-5.6-luna/low` Built-in Worker，capacity 2。用户授权发送的内容仅为
仓库外临时示例、方案与工具输出；没有发送 EHAI 源码、预写实现答案或修改真实数据库状态制造通过。

**固定 Gate 失败后修复：**

- 受控方案明确要求首次候选保留未实现的示例基线，收到 `gate_failures` 后才由真实 Worker 实现；
  这是故障恢复试用，不是自然失败场景，也不是最终产品 E2E。
- Planner 初稿把最终 Gate 修复错误拆为两个节点，先经正常讨论入口修订，再批准单最终节点方案。
- Run `a1595669-be53-483e-98fa-1574e4a4bc66` 的首个 Gate 因 `greet` 尚不接受 `locale` 而失败；
  后续 Attempt 修改代码和文档后通过。两次 Check ID 和 argv 完全一致，没有修改或降低标准。
- 最终 commit 为 `c4cf1bcd5ff4c248f1f0eb1b1feab0870e2b5cae`，正常 `get-result` 可重读通过结果。

**Ctrl+C 与同一 Run 恢复：**

- Run `24f8902c-31bc-497b-bcc5-2609835c0f5d` 完成 helper、README、messages 三个上游任务后，
  在最终 Worker 审查阶段收到实际 Ctrl+C；进程退出、Run paused、最终 Attempt interrupted，
  已整合的代码 commit `0662da71bdd649ade5e444d41e25d87e2dfb0af5` 保留。
- 首次立即 `resume-session` 暴露 `runtime_idle`：宿主把 claimed 调度项直接重建为 pending，
  SQLite 拒绝迁移，而宿主吞掉错误，导致新宿主在旧租约到期前无法接手。
- 修复将释放操作交回 Runtime：只释放本宿主持有的、已收敛且停止的 Run 租约；领域和持久层支持
  claimed → pending，保留同一调度项身份；暂停错误不再吞掉，收敛失败不标记为成功。
- 修复后再次恢复原 Run 并发送 Ctrl+C，实际中断点落在最终 Gate 命令执行期间，检查如实记录
  中断退出码 `3221225786`，不判成功。退出后的调度项为 pending，随后原 Run 继续并通过原 Gate。
- 新宿主在 UTC `10:48:41` 接手，早于旧租约的 `10:52:38` 到期时间，不是等待租约自然到期。
  Run 最终 completed，共六个 Attempt；三个已完成上游各只有原来的一个 Attempt，记录不变。
  最终结果保留上述 commit，Gate 的 Check ID 和 argv 不变，未遗留示例执行进程。

两项试用均未改动原示例仓库 HEAD 或工作区。证据保存在系统临时目录
`ehai-r2-recovery-b7876b8b0c3b47a4905d3f0ae133a2b7`：`recovery-evidence.json`、两份最终查询、
暂停前后快照、SQLite 轨迹及保留 worktree。实际失败后的三项诊断只位于仓库外临时目录，
修复前复现两项失败，修复后三项通过；未增加常驻测试。

这覆盖有序 Ctrl+C、中间成果保留、立即续接和固定 Gate 重新验收，不证明任意关窗/强杀、
未提交文件写入途中恢复、5–6 小时运行或 Codex App Server 实跑；R2/P2 总验收仍未完成。

### Codex App Server 真实编码与生命周期（2026-09-05）

本轮按用户指定组合使用 `gpt-5.5/high` Built-in Planner 和 `gpt-5.6-luna/high` Codex App Server Worker，
capacity 2。不是使用 Codex CLI Worker 替代 Server，也不是两种 Worker 模型在同一 Run 内混合路由。
本机独立 CLI 未登录，使用进程级 provider 配置和环境凭证调用用户授权测试端点，没有修改全局配置。

- 正常 `discuss-plan` 调查临时仓库并提出代码、README、最终整合三个节点。调用方前两轮误传
  `artifact:non-empty`，实际检查不足，未批准或执行；改为 `command:exit-zero` 后生成有效 v3。
- 批准方案 `e31a1e20-409c-4921-998c-b4289cde10df`，只在最终节点绑定行为 Gate，
  Check ID 为 `f5e84239-6d1b-4e43-a613-a234249c3374`。
- Run `f80fe372-7157-4e9c-8543-b06789525ad1` 经正常 `execute-plan` 创建两个并行 Server Thread，
  实际实现标准化姓名、英文/中文 greeting、keyword-only locale、错误行为及 README。

真实执行与中断暴露并修复：

1. Server 默认继承全局 MCP/插件并启动额外服务。现在启动/恢复 Thread 时读取对应 cwd 配置，
   禁用继承的 MCP、插件、hooks 和 web search；不改全局配置，不扩大为完整 Connector 授权系统。
2. 两个 Worker 中断时重复执行 Run.pause，第二个 Attempt 因 paused → paused 异常未收敛。
   现在每个 Attempt 独立记录中断/节点失败，只有仍在 running 的 Run 产生暂停迁移。
3. 启动恢复完成旧调度项后，再次恢复错误地新建调度项，触发 `dispatch_work.run_id` 唯一键冲突。
   现在通过显式 requeue 复用原调度项身份；领域与 SQLite 迁移约束一致，无数据库 Schema 升级。
4. 相邻恢复路径覆盖了原 Thread.cwd，修为保留其隔离 worktree。Windows Server 置于独立进程组，
   由宿主处理 Ctrl+C 和协议取消，随后关闭其自有 Server。

修复后重跑原入口：

- README Attempt `9e9618b3-3359-4206-8bd4-a93824be5574` 已成功、代码 Worker 仍执行时发送实际 Ctrl+C。
  Run paused、代码 Attempt interrupted、调度项 pending，自有 Server PID 退出且无遗留子进程。
- UTC `11:55:15` 立即恢复，早于旧租约 `11:58:13` 到期；已完成 README Attempt 记录不变且未重跑。
  中断 Worker 的代码 commit `dbf27017d2876889ac338f3f5ba9501d2ce9366b` 被保留并进入最终 Git 血缘。
- 同一个 Run 最终 completed，共六个 Server Attempt/Thread（三个历史中断、三个成功），
  仅一次最终 Gate，Check ID 和 argv 与批准时相同并通过。最终 commit
  `8a31b9563b57ec3d2ba6ff976a2e44b85ca68f68` 同时包含代码与 README 上游成果。
- 正常 `get-result` / `get-trace` 可在进程退出后重读结果；原示例仓库 HEAD
  `a812c69732d596863c6de697fa7e5359cd246cae` 和工作区不变，最终差异仅三个目标文件。

证据位于系统临时目录 `ehai-r2-server-962feecd89474e8ab34ee99301770dfe`，包括 `server-evidence.json`、
三版讨论/检查、批准记录、暂停前后快照、最终查询/轨迹、数据库和保留 worktree。
实际故障只使用仓库外临时诊断；没有添加常驻测试或写入凭证。Schema 查询和无模型 Thread 配置探查
仅用于核对安装版本协议，不替代上述真实编码证据。

这证明本轮 Server 编码、协作分工和有序停止/立即恢复，不证明强杀、任意关窗、文件写操作进行到
一半时的原子恢复、桌面进程附接或 5–6 小时运行。唯一产品 E2E 及 R2/P2 总验收仍未完成。
