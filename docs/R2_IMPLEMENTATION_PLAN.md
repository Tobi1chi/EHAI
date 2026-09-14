# R2：获批方案到代码

## 2026-09-14：受阻节点按自主恢复与人工介入拆分

用户确认 `stalled` 表示允许系统自主恢复，`suspended` 表示必须人工介入。
本节更新当前实现语义；下文历史试跑中的 blocked 等状态保留当时事实，不改写历史证据。

- 用户结果：区分暂时自主受阻与等待人的节点，不把单节点重试耗尽扩散为整 Run 暂停。
- 输入/输出：宿主 RetrySafety、真实终止事件和预算决定 stalled 或 suspended；
  节点状态通过现有 plan/trace 查询公开，人工挂起复用 intervention 查询和显式 reply。
- 所有权：Orchestrator 转换状态，Scheduler 重新检查依赖/容量后派发；Pi 不决定节点状态。
  安全重试依据与节点状态一同持久化，调度前核对最近 Attempt 已结束；不新增模型轮询。
- 人工回复只释放对应 suspended 节点；Run resume 不释放它，过程调整不得丢弃未处理的挂起。
  尝试/Goal 预算不重置，未知副作用不得自动重放。正常依赖等待仍为 pending。
- SQLite 读取旧 blocked 节点时映射 suspended；不批量改写已有数据库、事件或批准快照。
  新公开枚举、Planner 上下文及 Client 使用新名字。原 report_blocked/WorkerEvent 协议保留。
- 启动失败可以在 Session 创建前耗尽预算，因此 intervention 及 baseline 的 Session ID 允许 null；
  已绑定 Session 的身份核对仍保留。Attempt 的真实 failed/timed_out/interrupted 结果不改名。

正常入口试跑：隔离目录 `C:/Users/28262/AppData/Local/Temp/ehai-node-states-20260914`，
生产 HTTP create/propose/approve/start，单节点确定性 Planner，Codex Server 连接器配置缺失的本地
可执行文件，触发真实 FileNotFoundError；没有模型调用、mock 或直接改状态。
Run `20b44227-721b-4fc3-90b7-be775b46ce78` 在五次启动失败后节点 suspended，Run 仍 running，
介入记录保留原因和 null Session；HTTP pause/resume 没有解除节点挂起。
通过原 Run 的 trace 重读确认四次 PlanNodeStalled、四次自主 PlanNodeRecovered、一次
PlanNodeSuspended，没有将这些节点写为 PlanNodeFailed。停止宿主后通过 HTTP reply 解除挂起，
节点变为 pending；同键重放没有重复恢复事件，Attempt 总数仍为五，没有执行后续模型任务。
Ruff、format、mypy（107 个源文件）、Schema/Client 生成和 TypeScript typecheck/build 通过。
该试跑验证共享调度层，不证明 Pi 模型行为、真实写入中断、同图并发或唯一产品 E2E 已通过。
通用外部条件监听、新的 Planner 自动纠偏和真实模型恢复不在上述无模型试跑的证明范围内。

### 2026-09-14：真实 Luna 规划、同图并行挂起与恢复验收

在隔离目录 `C:/Users/28262/AppData/Local/Temp/ehai-luna-e2e-20260914`，通过生产 API 装配与
正常 create/propose/approve/start、interventions/reply、plan/trace/result 入口运行。Planner、
Worker、阶段 Reviewer 均使用完整 Pi 后端并配置 `gpt-5.6-luna/high`；原生 Session 核对模型全部为 Luna。
没有 mock、直接写领域状态或修改交付代码；临时驱动和验收夹具不进入常驻测试目录。

任务是在独立仓库实现 greeting.py 的 greet 函数并编写 README：Unicode 首尾空白修剪、精确问候
输出、空白输入 ValueError、非字符串 TypeError。冻结 verify.py 作为已批准的行为 Gate。
需求明确实施前需要测试操作员的执行期交接确认，文档不依赖这项挂起。Luna Planner 自主创建
两个无相互依赖的 Work 和一个依赖二者的最终 Phase Reviewer；核对实际图及 Check argv 后批准。

- Run `956d9ada-ccbc-411e-950c-f5ba41194182`；Plan `2e942d89-3fc0-4435-8549-19abfb22e718`。
  最终 completed，四个 Attempt：实施首次 interrupted，文档、实施续跑和 Reviewer 各 succeeded。
- 两个初始 Work 的执行区间真实重叠约 7.04 秒。代码 Worker 唯一工具调用是 report_blocked，
  未写文件或提交假候选；宿主持久化一次 PlanNodeSuspended 与 intervention
  `97e20924-afc7-5c48-ba37-15c01bcbde85`。代码 suspended 时文档独立完成，Reviewer 保持 pending，
  Run 一直 running，没有 RunPaused。这里验证的是同图独立任务，不是探索 Branch 选择。
- 操作员通过正常 HTTP reply 确认原批准范围内继续，形成一次 PlanNodeRecovered；代码 Worker
  使用回复完成实现，没有重复请求挂起。文档只执行一次，没有因代码恢复而重跑。
- 四次执行均加入逻辑 Phase Session `6387ac06-b3c8-45b7-af1a-819b4ea91513`；物理 Pi Session
  按 Attempt 隔离。阶段 Reviewer 返回有效 review.json；宿主运行原 argv
  `uv run --no-project python verify.py`，退出 0、Gate passed，形成一个 Checkpoint。
- 最终 commit `bf85a1ecd7f0eab2f3e1fad5ba5f9f8f9a3f857f` 相对夹具基线仅修改 README.md 和
  新增 greeting.py；SPEC.md、verify.py、.gitignore 的 Git blob 均未改变，原仓库 HEAD 和工作区
  保持不变。运行在隔离 worktree；没有把结果并入 EHAI 或用户其他项目。
- 后端报告 token：Planner 48,546，三个 Worker Attempt 合计 46,859，阶段 Reviewer 30,765，
  总计 **126,170**，其中 cacheRead **43,008**。这是原生 usage 统计，不是账单金额。
  证据见该目录 audit-summary.json、result.json、held-plan.json、held-trace.json、latest-trace.json。

该真实场景通过悬挂/回复/同图独立推进/完整模型续跑与行为验收。未触发 stalled，不将正常模型成功
冒充故障重试证据；stalled 仍引用上面的无模型启动故障试跑。本轮未启用周期轨迹审查，也未测试
基于审查的 suspend API、真实写入中断、handoff 接手、探索分支选择或崩溃竞态；不因此宣布唯一
产品 E2E 的所有场景或 P2 全部通过。本轮无需修改生产代码。

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

## 自动过程调整接线契约（2026-09-13，实施中）

代码接手依赖整合进度（2026-09-13）：CodeRuntimeConnector 将已接手输入的 effective 节点
用于直接依赖和 Evaluator 展开后的提交选择；来源 Artifact/Attempt 仍保留原 Run 身份。
从持久 ResultAdoption 重读并对比输入绑定，核对来源批准/过程、成功生产者、完整成果字节/hash
及唯一宿主 code snapshot；目标节点须已完成、定义仍匹配且未被目标 Attempt 替代。
依赖选择复用只读快照验证，不额外分配验收 worktree；当前仍要求相同 pinned base。
Ruff、98 源文件 mypy 与 diff whitespace 检查通过。此为接线进度，不是正常入口接手验收：
自动接手调度、输入适用性、后继启动/预算事务、Evaluator 选择持久化及接手目标自身返工
基线仍待贯通，不能据此宣称完整跨 Run 执行可用。

接手目标自身返工随后接入 Git 基线选择：优先保留当前 Run 的有效 handoff/成功结果；尚无当前
成功结果时，核对目标定义、持久接手证据、仓库 pinned base 与来源 Attempt 的实际 dependency
commits，一致才从接手提交继续。缺失旧输入元数据、依赖或目标定义变化时从当前有效输入重建，
不猜测旧成果仍适用；成果损坏仍报错，不作为无成果静默忽略。Worker 的 Gate 失败上下文保留
adoption_id，来源 Attempt 不重标为目标生产者。尚未通过后继正常启动路径验证此返工能力。

分支接手接线中：评估上下文从持久接手记录读取已完成目标节点的真实来源 Attempt/Artifact，
校验不可变字节与原始归属，以目标有效节点定位分支。分支选择落库前重新组装并比较候选上下文，
避免选择期间成果变化；过程调整保留分支选择时也核对接手版本及目标定义，不再只查当前 Run
生产者。纯评估对象、Orchestrator 与 persistence 需共同接通；同成果多目标别名仍明确拒绝，
正常入口的接手启动/自动调度和实际分支验收尚未完成。

候选恢复也纳入已提交的接手成果：recover_candidate_results 对 candidate/verifying 目标按
持久 adoption 继续原验收，复核运行状态与目标节点，保留原生产者及目标 Check 归属。
此路径不自动接纳 pending/ready 的接手记录，不能替代首次输入适用性检查和准入调度。

输入适用性检查已接入内部接手入口及 Built-in/Server 宿主装配：AdoptedResultInputs 不创建
Attempt，使用正常上游 Artifact 输入解析；CodeRuntimeConnector 重读持久绑定、校验成果字节，
比较来源真实 dependency_commits、目标当前依赖提交与 pinned base。输入不一致或缺少旧依赖
元数据时拒绝直接验收并要求新执行；进入状态事务前复核目标上下文未变化。此检查验证代码输入，
不替代后继审批对任务成果适用性的明确授权。尚未接入 ready adoption 的自动调度；不为赶进度
在排队事务里同步执行可能长耗时的 Gate。全量 Ruff、124 文件格式与 98 文件 mypy 通过，
无新永久测试；正常后继入口和真实运行验收仍待完成。

预算接续只读审计（2026-09-13）：SingleSlotRuntime._budget_failure 按当前 Run 汇总
AttemptBound/ProviderUsageRecorded，max_run_duration 从当前 Run.started_at 计算；
Orchestrator 的 attempt_budget 也只统计当前 Run。ExecutionConfig/RunStarted 尚未持久化
这些 Runtime 上限或独立 Goal budget 身份，不能静默把 Run cap 改名为 Goal cap。
ProcessAdjustmentPolicy.max_per_goal 已按同 Goal 事件累计，但政策来自各 Run 配置，仍需
后继授权事务固定连续的预算基线。Provider cost 目前并非各 Connector 均提供，不得把缺失
cost 当作已验证的费用限额执行。下一步须贯通明确 Goal 授权、累计使用和宿主恢复，再开放后继
正常启动；不能靠创建 successor 时复制一份本地 cap 宣称预算连续性已实现。

后续集成补齐两处接线（2026-09-13）：MERGE 继续沿用整合选中分支完整代码历史的既有语义，
即使文本输入仅为 Evaluator 选中的部分成果，也会对无当前生产者的分支节点读取其持久接手记录，
经完整来源/字节/目标定义校验后载入提交；普通 Evaluator 展开的输入仍仅由所选 Artifact 授权。
advance_ready_adoptions 已接入同步执行、SingleSlotRuntime 和 Scheduler 主循环，在创建
Worker Attempt 前且在排队事务外使用既有验收流程。先完成已结束 Evaluator 的选择，再处理
就绪接手节点；目标已有 Attempt、已有接手提交历史或需 Planner 处理阶段返工时不重复接纳。
输入不适用回到正常 Worker 排队，证据损坏不当作普通输入变化吞掉。Scheduler 在接手之后
重新读取终态并释放 dispatch，人工 Gate 保留其正常等待语义。此处复用当前同步宿主 Check
执行模型，没有实现新的异步 Check actor；正常后继启动与真实行为验收仍未完成。
Ruff、格式和 98 文件 mypy 通过，无新增永久测试。

### 后继 Run 成果接手：实施前契约（尚未实现）

用户结果：真正变更批准边界并获得新批准后，在同一 Goal 继续开发；保留仍适用的成果和必要
上下文，只重做受影响部分。原批准内的任务拆分、返工和路线调整仍使用现有 process 入口。

本轮主 Agent 与只读子 Agent 核对当前源码：approve_plan 仅确认契约并切换 Goal 的生效关系；
start_run / SQLite put_run 创建新执行基线，没有前后继绑定。Orchestrator 的下游 Artifact 输入、
CodeRuntimeConnector 的依赖 commit 及 PhaseSessions 均按当前 Run 读取；复用相同节点 ID
或复制 completed 状态不能交付成果接手。WorkerRequest、Artifact、CheckRun 的当前归属保护
必须保留，不能把这些检查整体放宽为“同 Goal 就接受”。

输入必须明确绑定来源 Run/过程版本/批准版本、目标待批准方案、批准差异，以及按目标节点列出的
来源成功 Attempt、不可变成果与必要 handoff 引用和适用依据。来源节点的有效输入、选中分支及
成果内容都要核对，不能只凭名称或完成时间判断等价。未决问题须有明确接续去向，不能自动视为
已回复；人工 Gate 的旧请求与证据保留原身份，不复制其判定为新批准下的通过结论。

应用服务负责接手校验及原子持久化批准/接续关系，Orchestrator 负责据已验证接手事实满足目标
工作的输入和推进新 Gate；代码宿主复用现有 snapshot 验证和 Git 隔离准备，显式载入获准成果。
输出是可查询的前后继关系、来源可追溯的接手记录、新 Run 的真实执行/Reviewer/Gate 证据。
不制造“新 Worker 已执行成功”的假 Attempt，也不重标旧 Artifact/Check 的历史归属。
已接手的适用工作不重复调用 Worker，但目标批准要求的验证仍绑定新成果版本实际进行。

Session 与 Run 生命周期继续分离：通过显式来源引用传递未受影响上下文及批准差异/失效项，
不全量回放或清空原历史；物理 Session 仅在框架、角色、权限、工作区和归属都允许时复用。
新建物理 Session 时应使用宿主确认的交接，不把来源脏状态作为成果。目标级预算必须显式保留
累计使用；当前 Run 级 cap 不静默改名为 Goal 预算，也不能因新 Run 身份重置已授权的 Goal cap。

失败边界：来源未收敛、审批/过程/输入变动、缺失或失效成果、未决请求无接续说明、目标预算
无明确连续性时，不发布可调度的后继 Run；保留原轨迹和可调查证据。上述是实施契约，不是
现有命令/Schema 描述。先贯通应用事务、输入解析、代码准备、上下文及预算，再开放正常入口；
不能仅新增 predecessor 字段就宣称能力完成。

接手验收主体的首层实现（2026-09-13，接线中）：CheckRun、CheckResult、GateDecision 新增可选
adoption_id，attempt_id 保留真实生产者 Attempt 身份；接手 ID 参与结果归属和 Gate 匹配，
不同接手记录不能混用结果。Codec 在无接手 ID 时不增加字段，查询 Schema/生成 Client 已同步。
此时尚未接入宿主持久接手记录、验收工作区与调度，repository 明确拒绝带接手 ID 的 CheckRun
和 Checkpoint，未放宽旧同 Run 校验；不得据字段存在宣称后继执行可用。
旧 HTTP 试用数据库中的 1 个真实 CheckRun 经新 Codec 只读反序列化/序列化后 JSON 不变。
Client generate/typecheck/build 通过；后继 Run 的真实接手验收仍待其余接线完成。
CheckContext / CheckRunner 随后接入可选 ResultAdoption：目标 Run/Plan/Node 必须匹配接手记录，
真实来源 Attempt 必须成功且与 source Run/Node/Attempt 对应，成果集合和每份 hash 必须匹配
接手快照；无 adoption 的旧同 Run/Node 规则保留。CheckRunner 将接手 ID 传入真实检查结果。
Orchestrator 的结果回填只改 CheckRun 调用 ID，先校验 Run/Node/生产者 Attempt/adoption/Check
身份相等，防止把其他验收主体的结果重绑到目标。此层通过 Ruff/mypy，尚未接通调度验收。
查询接线复核补齐 CheckRunView / CheckResultView / GateDecisionView 的 adoption_id，避免仅
Schema/Codec 有字段而正常查询丢失来源关系。公共 DTO 对普通检查返回 null，Schema 允许
缺省（旧响应）或 null；持久 Codec 仍省略无接手字段。对旧 HTTP 试用中真实 CheckRun 经当前
公共编码路径只读核对，检查及其结果均返回 adoption_id=null。Client 再生成/typecheck/build
通过。新 SQLite 接手表迁移正在集成，不在原 resolver 活跃期间升级其试用数据库。

接手存储与前后继身份已接入首层（仍无正常启动入口）：schema migration 16 新增
result_adoptions，State Port 提供 get/list/put。记录按目标 Run/Node 唯一且不可变，验证来源
当前过程、已完成节点、最新成功 Attempt、选中分支、收敛状态和精确 candidate/patch 证据哈希。
Run 新增可空 predecessor_run_id，Codec 对旧 Run 仍省略该字段；公共查询返回明确空值。
新建后继需同 Goal 的 paused 来源、新 approved Plan，且来源无活动 Attempt/自动 Check；
关系纳入 Run 不可变身份。接手记录必须指向目标 Run 已绑定的前任，而非任意同 Goal Run。
应用层的批准差异/适用性、目标预算、未决问题接续及启动原子事务仍未接线，不把这些存储
约束当成完整授权。独立临时库 `ehai-adoption-migration-_2oypnoy/state.sqlite` 从 schema 15
升级到 16，integrity=ok、FK 无错误、接手表为空；没有制造执行或成果接手数据作为验收。
Ruff、98 源文件 mypy 及 Client generate/typecheck/build 通过，后继执行仍未开放。

接手验收持久化随后接通：put_check_run / Checkpoint 不再一律拒绝 adoption_id，而是通过
持久 ResultAdoption 校验目标 Run/Plan/Node、已绑定前任与真实 succeeded 生产者，以及完整
candidate/patch 哈希集合。目标节点定义已变或目标节点已有新的 Attempt 时拒绝继续沿用接手
验收。人工请求对齐目标批准契约，证据仍保持真实来源归属；Gate 只接受同 adoption 的已保存
通过结果，且 Gate/Check 证据不能超出接手集合。没有 adoption 的原同 Run 校验不变。
此层 Ruff、124 文件格式及 98 源文件 mypy 通过；正常审批/启动、调度、验收工作区和人工
回复的接手路径仍待接线，尚无真实跨 Run 验收证据，原活跃试用仍不迁移。

只读复核还发现一个旧 Run 边界：approve-plan 可以在已收敛的 paused 来源之后确认新契约，
而原 resume 控制器此前仍会先写入 running，随后 recover_candidate_results 可能按旧契约
执行自动 Check 或推进执行图。现由共享契约判定在同步/后台 resume 的同一控制事务内拒绝该
恢复，并让候选恢复和自动过程调整在契约过期时直接保留历史、不调用 Check/Planner。验证范围
为静态 Ruff、格式和 mypy；未操作活跃试用数据库，也未宣称后继 Run 接手已完成。

接手查询已贯通 QueryService、CLI get-run-adoptions、HTTP /runs/{run_id}/adoptions、查询
Schema/OpenAPI 与生成 Client listResultAdoptions。查询校验目标 Run 存在，仅输出持久记录，
reason 经共用脱敏，不改变执行状态。使用旧 HTTP 真实运行数据库的独立 backup
`ehai-adoption-migration-_2oypnoy/historical.sqlite` 验证：CLI 对原 completed Run 返回 []，
正常本地 HTTP 服务返回 200 与 {data:[]}；实际 OpenAPI 路由的 operationId/summary 与保存契约
一致。没有创建接手记录或调用 Worker，不能当作接手执行验收；查询验证服务已关闭，PID
51680 和 8766 监听均确认不存在。原数据库未改动，Client generate/typecheck/build 通过。

### 暂停来源的多轮待审讨论（已接线；后继执行未完成）

用户结果：原 Run 收敛暂停后，可在正常讨论入口提出需要重新批准的变更并连续修订草案，而不是
只能生成一份草案或必须结束原 Goal。此增量不是批准内的过程调整，更不负责启动后继 Run。

输入复用用户消息/会话、显式 source_run_id、原批准设计/Checks、当前执行图、暂停控制事件、
Attempt/Check/Intervention 事实，以及最新候选方案和它自己的完整 Checks。来源一旦绑定到会话
便不可换成另一个 Run；原无来源会话可在同 Goal 下显式绑定一次，后续轮次沿用。输出是可查询的
讨论及连续待审 Plan/Contract 版本；原 Goal 生效契约、Run、Gate、成果和人工请求保持不变。

应用服务负责绑定、版本及提交新鲜度检查，Planner 仅调查/答复/产出草案；接口层不自行推进状态。
同 Goal 的其他活跃执行、来源未收敛、来源批准已与生效契约分离、调用期间新增控制/执行/人工
回复或最新草案变化时拒绝保存结果。允许讨论未决人工问题，不把讨论本身当成回复或解决事实。
新版本基于最新候选的真实 Contract 修订，不能假装候选已批准，也不能提前切换原 Goal 契约。

CLI `discuss-plan --source-run-id`、HTTP DiscussPlanRequest、会话查询 Schema 和生成 Client 已接线；
Builtin 输入新增 source_context，并通过既有 replan_context 区分 approved_design_document /
approved_checks 与最新 base_plan_revision / base_checks。子 Agent 完成基础字段及主流程后，
主 Agent 接手补齐来源验证、版本链、源快照和后置复查；首轮给 Planner 来源当前执行图，
待审版本的版本号和 supersedes 仍由真实持久 base 分配。Started/Proposed 记录来源 Run、
过程版本和原批准 ID。整体 Ruff、123 文件格式、97 文件 mypy 与 Client 构建通过。

真实 CLI 已验证：在下述隔离 Run 暂停时，使用原 Conversation
`81486297-f45c-48e2-9589-cfc6fb8f91eb` 显式绑定来源，只请求解释、不生成方案；Turn
`d6b7323b-3b20-498c-81f0-63e790cd3062` 正常 completed，Planner Session
`121f1141-dbe7-409f-9aad-f6a12cfce2ca`。回答正确区分原批准、当前执行图、文档 succeeded
与代码 interrupted、尚未进行的 Reviewer/Gate；没有推断上下文未提供的上游故障细节。
正常查询返回来源绑定；原生效 Contract `345cf563-889d-4530-8fe1-99601c74a157`、Plan 数 1、
Attempt 数 3 均不变。问答没有生成草案，也没有启动 Worker。
随后同会话省略 source_run_id 提交未批准的未来 external 映射参数需求，来源正确继承；
Turn `ee88f3bb-d99f-40af-9b90-6b8775374763` 完成，Planner Session
`477380e4-f8b7-4ffa-accd-d429740fe863` 产生 v2 draft
`1994e327-3fb2-48c4-9c95-0703a006db4f`，Contract v2
`9efd2482-6825-4ba3-a341-03280ffceebf`。正常 get-plan 核对 supersedes 指向原 v1；
只读核对原生效 Contract 不变、原 v1 approved 且批准时节点仍 pending、Run paused、Attempt 数 3。
接着仍省略 source_run_id 请求把待审参数名 external 修订为 bindings，生成下一待审版本；
第二次真实草案调用随后完成：Turn `99cfe93b-9c77-4177-a486-438fabf9b609`，Planner Session
`bffe5158-4b07-43f1-a988-2822ab53a11b`，v3 draft `997fb850-04b8-4633-9efb-9e6ce5d31e82`，
Contract v3 `a579b3bd-c189-4e44-824a-468ad0523d88`，supersedes 指向 v2。设计明确 bindings
为最新待审参数名，原批准 API 仍为单 text 参数。正常查询与只读 DB 核对 v1 approved、v2/v3
draft，原生效 Contract 不变、Run paused、Attempt 数 3；integrity/FK 正常。两个规划进程均已
结束。两份未来需求都未批准/执行，不用于原 Run 的验收。连续待审版本保存已有真实证据，
不表示后继 Run、成果接手或预算迁移已完成。

复核补齐：来源讨论省略 criteria 时使用最新持久 base 的真实 Contract 条件，原批准条件仍在
source_context 中单独提供，普通讨论默认策略不变；其他 Run 的 pending/running 自动 Check
均阻止来源讨论，按 CheckSpec.kind 区分人工等待。Ruff/mypy 通过。本次 v2/v3 都使用 command
条件，其他条件组合尚未取得新的运行证据。
后继 Run 的明确成果接手、未决请求迁移与目标预算连续性仍需独立完成；不放松 approve_plan 的
当前人工请求保护，也不以多轮草案存在来宣称自举或后继执行已交付。

### 新隔离正常入口试用（进行中）

在临时目录 `ehai-auto-env-467378266a8142dbbfb5cc8a9b51de2a` 启动新的 `.env` 文本解析库任务，
不是最终产品 E2E。初始源码只有 README 介绍，基线 commit 为 `56943d7`；实现、执行图和 Gate
均由正常 Planner/Worker 流程产生，不预写失败、答案或返工图，不改动旧试用数据库。
Project `afa17a9c-d7b4-4fb2-aefc-018758bbcedf`、Goal `3db1114a-ee91-4a8c-9b58-e75ffde1fd0b`；
通过 CLI `discuss-plan` 提交明确的引号、转义、前向引用、循环引用、错误定位及不泄露配置值要求。
Planner 为 `gpt-6-astra/high`，Worker 配置为 `gpt-5.6-luna/max`、并发 2；待首次授权的配置
包含 `process_adjustment.max_per_goal=3`，五项兼容端点 capability 均关闭，无 Shell/远端写权限。
规划已正常完成：Conversation `81486297-f45c-48e2-9589-cfc6fb8f91eb`，Plan
`c946e0ba-7fc2-4ced-9ab9-80a860217c2b`，Contract `345cf563-889d-4530-8fe1-99601c74a157`，
Check `0e6a3f8d-ccae-4fc2-8dab-db9035419e70`。主 Agent 从正常 get-plan/get-plan-checks 审查
设计、两个独立文件写入边界、独立 Reviewer 与实际 Gate argv，并核对保存代码的转义字面量。
批准后以正常 `execute-plan --authorize` 启动 Run `5c0793cf-80cf-46e2-a6c4-76204ee6d42c`。
只读核对 RunStarted 已保存 explicit 授权及完整 process_adjustment 策略，配置指纹为
`efeab9f4695b4cc4711d8bc09e00f67c3db3e7ef3cdb3e4ccd32b348d08a3802`。并行启动后，文档 Attempt
`ddf8c08a-f6b0-4acc-b556-c5924d36a68b` 已 succeeded，候选 Artifact
`ae3a896c-d422-400a-9187-0953b1cb544d`、`7a7de184-142f-4162-b328-7a9de139c746` 已保存；
代码 Attempt `1a34923f-a942-48d6-887f-98fd9e553506` 随后因模型返回 `response.failed` 而
interrupted，原前台进程正常退出，Run 保留 `unknown_execution` 暂停；自动协调器没有覆盖此类
暂停，未开始自动草案。该次失败发生在最后一次 phase_context_read 后的模型调用，未收到可执行
工具结果；模型端仅配置本地 function tools，没有授权远端副作用。旧日志未保存 Provider error
细节，不能据此断定本地参数或上游原因。

针对已观察到的诊断缺口，Adapter 新增终态 failure 的错误代码、限长脱敏消息及 incomplete reason
轨迹，不改变重试/暂停分类。外部 `diagnose_terminal_failure.py` 用隔离 MockTransport 定位这一
日志行为（非产品验收、非模型成功模拟）；初次诊断漏填 retry_owner，补正夹具后 1 项通过，
证明单请求、保留错误码及正确脱敏。旧调用原始 error 不能由此补回，也没有宣布 Provider 故障已修复。

核对终态与本地执行轨迹后，经正常 `resume-session` 恢复原 Run。没有有效 handoff 的代码分支
不接纳旧脏工作区为基线；新 Attempt `cf391d62-ce35-4043-978b-02a1ee96ce05` 启动时，
文档仍为原 succeeded Attempt，不重跑。新物理 Session 继续加入原逻辑 Phase Session
`2381b0e2-0405-4b0f-87d8-e8c49f661b3e`。Reviewer 尚未启动，无最终交付或自动调整正向证据。
实际 Git HEAD 已核对为原始基线 `56943d708470b320f459f83d45413169f7d1c312`。
该 Attempt 随后也因 `response.failed` interrupted；新增日志真实记录到
`provider_error_code=upstream_error`、`provider_error_message=Upstream request failed`，
Response `resp_e123e73a11dd47b9b4128f3f4b14f31c`。这关闭了错误明细丢失的真实路径复核，
不代表上游故障已修复。第二个前台进程已退出，保留原 Run paused，不连续重跑同一失败调用。
执行进程禁用 uv 网络与 Python 下载。新策略进入首次正常授权、恢复保留文档与原 Phase Session
已有证据；仍无自动恢复正向证据。后续接续原 Run，不重建试用。

### 集成审查记录

讨论 Session 归属接线（2026-09-13）：应用服务在 PlanningTurnStarted 中预存宿主分配的
agent_session_ref_id，经 ConversationalPlanner / ConfiguredCheckPlanner 传到 Built-in Runtime。
正常查询优先使用 Started 归属，完成结果必须匹配；旧无 Started ID 的记录仍使用原 Completed ID。
这补齐运行中/失败讨论与实际角色日志的关联基础，不是目标预算核算或中断自动恢复。
真实只读讨论 Conversation `e1a5b639-f4cd-4b3c-a520-7711bf386fde`、Turn
`3bb87c9b-56f0-4616-b55f-f03289076124` 已 completed，预分配及返回 Session 同为
`03a04974-1820-439a-905b-a36b5778c699`，未生成方案或启动 Run。旧四轮讨论查询仍返回原 Session。
Ruff、123 文件格式及 97 源文件 mypy 通过；此次未制造模型失败，失败/中断分支不宣称已实测。

接续证据（2026-09-13）：上述解析库的显式过程草案
`850835cc-e8a4-4549-966e-893a2d8412b9` 已由独立 Review
`c83f5a63-a223-4eaa-967f-cda6d33cf3ef` 完成审查，preserves_boundary=true。
审查覆盖需求、接口、权限、Gate 范围及文档成果复用；主 Agent 通过正常查询再次核对
保留文档节点完整 JSON 相同、原 Contract 与原 Check ID 不变。未批准的 external/bindings
需求不进入原 Run。经正常 apply-process 应用过程 v2
`1785ee05-0f12-47e1-8537-847867f28f3e`，随后 resume-session 恢复同一 Run。
前台报告 running、累计 Attempt 4、active 1；新增工作为 framing → lexer → resolver，
之后依赖保留文档进入独立 Reviewer 和原 Gate。此次为显式接续，不是自动触发证据；
拆分不证明上游服务故障已修复，后续任务完成、阶段上下文接续与最终 Gate 仍待核验。
随后正常 get-trace 核对新 framing Attempt `f9306698-20da-4c54-99b0-ae1a7e1e30fc`
的 PhaseSessionJoined 仍指向原逻辑 Session `2381b0e2-0405-4b0f-87d8-e8c49f661b3e`，
物理 Session 为 `83d87cef-b9b7-47bd-a545-bd2cb5d07c3f`；文档仍是原 succeeded Attempt。
framing 随后由框架正常完成，候选 `584c3450-3319-4645-a4b6-a2d60a4def7d`、宿主代码快照
`f6bc80d0-2f27-460b-910e-0858cbc944e1` 与 patch `8bf40eaa-60d8-4771-898f-2e10483093c2`
已持久化；宿主结果 commit 为 `dc8ec27a8d8c013bc80b51fa74fdc752b3ee9b38`。框架自行启动
lexer Attempt `e87c91e2-aaf5-477f-9f8a-c624e060b3d7`，物理 Session
`0766ec0a-855f-4608-a649-be5eb34bb8ec` 加入同一原逻辑 Phase Session。只读检查宿主 metadata
确认 lexer 的 dependency_upstreams、upstreams、prepared_commit 和实际 Git HEAD 均指向上述
framing commit，Run base 仍是原 `56943d7`；不是旧中断代码的接纳。此处证明第一段成果提交
及下一段正常调度/代码基线交接，不证明 lexer/resolver、Reviewer 或最终 Gate 已完成。
lexer 随后正常 succeeded，宿主成果 commit `43643fcbd1956e04e96bb2532967d08107d4efdb`。
框架自行启动 resolver Attempt `e142f130-0a95-43bc-8643-4a9aa99e401f`；宿主 metadata 中
upstreams/dependency_upstreams/prepared_commit 及实际 Git HEAD 均为该 lexer commit。
新物理 Session `cfd492c1-7198-4fc8-b853-c4fe9143955a` 仍加入原逻辑 Phase Session。
此处关闭第二段成果提交和第三段正常调度/基线交接核验；完整解析器及 Reviewer/Gate 仍在后续。

resolver 随后 succeeded，commit `a162c25df9e2fa17837407c1afe913961b39ee34`；Reviewer
Attempt `454cba13-1265-470c-8842-bd3b2209d895` 的宿主工作区整合此提交和原文档 commit，
prepared/实际 HEAD `b7a395c4e9c4f72251b443f68e62e04ed11bbc72`。只读 Git 核对 parser、README
blob 分别与两个源提交一致且工作区 clean。Reviewer 输出 pass/findings=[]，但原自动 Check
`76a62cb1-87b2-4d83-9ceb-9d2113beabae` 实际失败：Gate 脚本第37行的跨物理行引号用例期待
第1行错误，解析器先在 framing 中报告第2行 malformed assignment，错误定位断言未通过。
这证明 Reviewer 推荐不能替代真实 Gate。框架未降低 Gate，已在原 Run 触发阶段返工。

观察到的返工范围问题：_reopen_reviewed_phase 使用全部 phase.rework_node_ids，重新打开了
无 Reviewer 文档问题的 docs 以及三段代码；文档 Attempt `53b58eb4-6634-4274-982e-6c27bd0e6183`
已再次 succeeded，framing `d8094cb5-f6f5-4ad7-af4d-991dc21679ed` 正在返工。
普通重开路径先消费了这次失败，没有产生 ProcessAdjustmentStarted。后续需让显式启用的
自动调整策略有机会依据真实 Gate 证据规划最小受影响范围，而不是全量重跑；此问题尚未修复。
不以当前固定范围返工冒充自主过程调整验收，不中断未知模型请求或改写现有试用状态。

返工路由修复已落源代码：读取原 explicit RunStarted 配置，已选 process_adjustment 时拒绝
固定列表重开并复用 review_rework 暂停，协调器继续拥有策略验证/累计预算及模型调用。
同 Run 尚有活跃 Attempt 或自动 Check 时，保留失败 Reviewer 和已完成成果，停止新增派发，
由后续 queue_ready_attempts 在收敛后保存同一暂停来源；不把并行收敛误报为 runtime_error。
没有策略的历史执行继续原固定路线，预算耗尽不回退为全量重跑来绕过策略。
仓库外 diagnose_review_rework_routing.py 读取真实授权事件与首次失败前的7次历史 Attempt，
验证选择 Planner 的路由和旧无策略兼容，1项通过；未写DB、未调用模型，不作为自动闭环验收。
此修改 Ruff/mypy 通过。活跃宿主尚未加载新代码，完整 propose/review/apply/resume 的自动
触发运行验证仍待安全接续后完成，不能把旧宿主正在进行的固定返工算作修复验证。

接手候选状态转换已补齐：PlanNode.submit_adopted_candidate 只允许已 ready 的 WORK/MERGE
节点接收匹配目标节点且有证据的 ResultAdoption，不经过虚构 Worker 执行状态；Reviewer、
Evaluator 和 Fork 不走该转换。存储仅在真实持久接手记录、目标 running、未被新 Attempt
取代且来源证据匹配时允许 READY→CANDIDATE；无 Gate 的非最终中间节点仍通过既有
accept_intermediate 完成，带 Gate 的节点不能跳过 VERIFYING。普通节点的原状态转换保持。
相关 Ruff/mypy 通过；自动就绪分派和接手输入适用性仍须在首次检查启动接线时完成。

首次 Check 准备已支持可选 adoption_id：_start_checks 使用统一 verification scope 的目标
Run/Node/Contract，保留真实来源 Attempt；先核对目标与 Check 配置、候选状态，再通过
_verification_workspace 核验实际成果字节和独立工作区，随后才持久化 CheckRun。
正常 Built-in/Server 宿主绑定 enable_adoption_execution 到 CodeRuntimeConnector 的准备入口，
CheckContext 保留接手对象且命令检查使用独立代码目录。此处不创建 Worker、不复用来源
工作目录。Ruff/mypy 与外部返工路由诊断通过；_check_and_complete、自动就绪分派和后继
批准启动尚未完成，因此仍没有正常入口的接手执行验收。

内部接受链随后接通 accept_adopted_result → _check_and_complete：先复用 ready_nodes 判断
目标依赖/路线就绪，验证接手工作区后保存明确 adopted_result 候选事件，不产生 AttemptStarted
或 AttemptSucceeded。无 Check 的中间结果复用 _accept_intermediate，带 Check 的结果传递
adoption_id 到目标首次检查或已有验证恢复。目标曾验收过该接手记录而又进入返工就绪态时，
不能反复把同一旧结果作为新的返工提交。源码 Ruff/mypy 与外部路由诊断通过；调用方仍须先
建立目标输入适用性，自动分派及公开审批/启动入口尚未连接，不能视作自举或接手验收完成。

人工决策服务的收尾已补传接手 ID：从持久 CheckRun 核对 receipt 的目标 Run/生产者 Attempt，
再把 check.adoption_id 传入 finish_pending_gate。已不在 running 的 Run 只返回当前状态，
不因重放人工决定继续推进旧执行。与 Orchestrator 的目标 scope、人工请求/结果及 Gate 事件
身份传播共同通过静态检查；尚未声称真实跨 Run 人工 Gate 验收。

上述解析库试用最终完成（2026-09-13）：第二轮 Reviewer Attempt
`df2015f6-37c0-4e05-9cb1-3d82c7d0803e` 后，原 Check 的第二次运行
`957ceea1-8ebc-4cf6-a45f-cc42ad2d0b6e` passed；Run 于 06:36:09Z completed，前台退出码0。
只读核对 active Attempt 为0、ProcessAdjustmentStarted 仍为0，第一次失败 Check 仍保留。
最终 commit `df2eb51c8adf0ab27e52378e1303075012ad302b`，结果工作区是临时 worktrees 下该
Reviewer Attempt目录；相对原 base 只有 README.md 和 env_text.py 变更。文档返工虽然被
不必要调度，最终使用的文档 commit 仍为原 `9eae06b`。宿主退出后通过新代码的正常 get-result
读取 completed Run 和精确交付位置；此时才允许迁移试用库到schema16。
这是显式过程修订、原Run恢复、顺序成果交接、独立Reviewer/真实Gate及固定路线返工的证据，
不是新路由下自动 Planner 调整的正向证据，也不是EHAI自身开发的唯一产品E2E。

下游输入首层接线：ArtifactInputSnapshot 可以携带明确 ResultAdoption，真实 source
run_id/plan_node_id/attempt_id 不改写；effective 身份仅用于目标依赖路由。WorkerRequest
仍校验目标 Run/Plan 与已绑定 predecessor，不接受任意跨 Run Artifact。Orchestrator 从持久
接手记录读取已完成且未被新目标 Attempt 替代的上游成果，核对来源/hash后附带最小接手说明，
合并与分支提示按目标有效节点过滤。相同 Artifact 的多个冲突目标绑定暂明确拒绝，不能静默
选一个；未处理为多别名输入，也不冒充完整共享成果支持。普通无接手快照的提示格式保持。
相关 Ruff/mypy 通过；代码提交依赖整合、Evaluator 持久选择校验及自动就绪分派仍需贯通，
尚无真实跨 Run 下游执行证据。

接手验收工作区首层接线：GitCodeWorkspace 可为 ResultAdoption 建立独立 detached worktree，
保留准备意图和精确源 commit；已有非匹配或脏工作区拒绝重置。CodeRuntimeConnector 核对
持久来源、实际 Artifact 字节长度/hash、唯一宿主 snapshot 与本地提交元数据一致，再准备工作区；
正常 Built-in/Server 装配传入 ArtifactStore。目标基线不同则要求整合和新证据，不用旧仓库上下文
冒充目标验收。当前仅完成代码宿主准备能力，调度调用仍待接线；相关 Ruff/mypy 通过。

HTTP 首次配置授权接线边界（实现接线中，尚未验收）：原 `/runs/start` 只接收计划 ID 和
幂等键，宿主启动时已固定 Worker/Endpoint/Connector、模型、workspace、容量和工具权限。
请求中的显式配置只能与实际宿主装配匹配，不得触发临时换模型或扩权。CLI 的
`prepare_execute_run` 原先 start_run 后 authorize_run，不能原样复用到自动调度的 HTTP：
首次 Run/初始过程、explicit RunStarted 配置与指纹、DispatchWork 和 receipt 必须同事务保存。
SQLite `put_run` 已负责创建初始过程，不另造初始化状态机。
新授权配置必须参与 StartRun 的幂等指纹；同键更换配置或给无授权旧请求补授权都应冲突。
同时需核对既有 CLI 已明确授权的旧 receipt 重放兼容性，不能只改指纹就破坏旧正常入口重试。
旧 HTTP 省略配置的语义保留；新配置入口仅适用当前完整配置可表达的 Built-in/Server P2 宿主，
不把 legacy fake/codex 装配冒充等价支持。格式错误与宿主不匹配分别返回明确的输入/冲突错误，
同步更新命令、HTTP Schema 与生成 Client。
当前 HTTP 请求已加入可选严格 `execution_config` 及嵌套能力/Server/调整策略对象；API 复用
ExecutionConfig 解析，并在只读幂等 preflight 之后检查固定宿主配置及 Planner 匹配，不从请求
重建 Connector。宿主暴露实际规范化配置快照，配置与策略不匹配返回冲突。Schema/Client
generate/typecheck/build 已通过；应用命令、原子事务和 CLI 已完成接线，整体 Ruff、123 文件格式
与 97 源文件 mypy 通过。旧无配置命令指纹保持原值；旧 CLI 重放仅在原 receipt/plan 与持久
explicit 配置和指纹都匹配时返回旧 Run，不修改历史授权。当前已用新代码正常恢复原 CLI Run，
随后在此暂停 Run 上启动正常 `ehai-api --p2-runtime`，通过真实 HTTP `/runs/start` 验证：
旧 CLI key `auto-env-execute-1` 携带同配置返回 201 和原 paused Run；同 key 将 capacity 改为 3
返回 409 幂等冲突；新 key 携带该不匹配配置返回 409 宿主配置冲突。RunStarted 仍为 1、
Attempt 仍为 3、ProcessAdjustmentStarted 为 0；没有新授权或 Worker。SQLite integrity/FK 正常。
HTTP 宿主已停止，进程与监听端口均确认消失。此证据覆盖真实旧授权重放和拒绝变更，新的
HTTP 首次原子授权成功路径仍待验证，不能用重放冒充首次启动验收。

独立只读复核进一步发现：ExecutionConfig 会规范化部分文本，而 Connector 曾先收到原始宿主
参数。现将宿主配置规范化移到装配之前，用同一结果构造 Worker/Connector/授权快照，并规范化
显式 Planner 文本。另为 HTTP 配置文本补充非空白/NUL 的 Schema pattern，为命令与 Shell 数组
补充 uniqueItems；规范化后去重仍由共用 ExecutionConfig 校验，Schema 描述明确此运行时约束。
以带首尾空白的 Built-in 模型参数启动真实 HTTP 宿主，GET WorkerProfile 返回规范化模型；
正常配置的新 key 通过宿主匹配后被已有 Run 约束拒绝（不是配置冲突），空白模型输入返回 422。
没有创建第二个 Run。复测宿主已关闭且进程/监听均消失；Ruff、123 文件格式、97 文件 mypy
和 Client generate/typecheck/build 通过。未启动新的模型调用，原上游故障暂停仍保留。

集成审查接续：协调器已区分调用方关闭与子任务启动前取消；单 Run 的取消不再直接向 API 调度
循环传播 `CancelledError`。公开 pause/cancel 在持久化控制后再次取消调整，以收敛等待期间启动的
规划；幂等重放仍在所有取消操作之前返回。取消结果保留已获得的 draft/review/process 引用。
模型步骤之间的控制复查复用 `require_process_control`；历史终态重放不再声称本次恢复了 Run。
Started 去重按原暂停事件身份判断，不让同 Run 的旧触发永久阻止新的控制事件；事件游标消费
Started 时移除对应待调度项，防止重启后反复轮询已开始的模型请求。没有因此重发未知模型请求。

本次 Ruff、123 文件格式检查、97 源文件 mypy 通过。以只读 SQLite 连接核对上一条真实 HTTP
Run `62a04000-20c2-484d-a2d7-f672cb83fd8c` 保存的执行配置：重新序列化与原文档相等，SHA-256
指纹与历史授权相等；新增显式策略能往返解析且改变指纹。没有修改旧数据库或重新启动已完成试用。
这些是静态检查与配置兼容证据，不是取消竞态或自动触发的运行验收。
随后已接入按 Run 管理的 API 后台调整任务，主循环不再等待每次 Planner 调用完成；关闭或终止
宿主时取消并 drain 调整任务，再关闭 Connector。模型返回后只有实际仍为 running 的 Run 才释放
调度限制。任务发现目前仍在 `run_until_idle` 返回后的宿主轮次进行，尚不表示运行中即时发现所有
新暂停。已收敛的稳定 hold 记录一次 Skipped；Attempt/Check 仍活动时允许等待后复查。
联查发现 `quiesce_run` 曾用仅含本 Run 的 Attempt 字典核对所有活跃任务，从而把其他 Run 的
任务当成未知执行，导致多 Run 场景误报收敛失败；现从持久状态读取真实归属，未知归属仍拒绝
宣称安全。以上调整已通过 Ruff、格式及 mypy，尚需真实并发、取消与自动触发运行验证。
配置入口和限制已写入 Usage。不得把显式恢复的旧成功记录充作自动闭环证据。

当前正在接入 `ProcessAdjustments` 与两个宿主，尚未取得自动触发的真实正向运行证据。执行配置新增
可选 `process_adjustment` 策略，包含明确的 `max_per_goal`、Planner `model` 和 `reasoning_effort`。
没有策略就不启用自动模型调用；序列化旧配置不附加新字段，保留既有授权指纹。策略随首次明确执行
授权持久化，宿主不能用今天的参数替代旧配置；同 Goal 下的后继 Run 不清零已保留的自动调整次数。
上限计入已开始的自动调整尝试，包括失败与取消，不是只统计成功应用。

首次接入仅处理 `branch_exhausted` / `review_rework`，且当前失败节点具有已提交成功候选的执行证据。
它不是所有 paused 的自动重试器：用户停止、未知执行结果、运行环境错误、重试预算耗尽、未决人工
Gate/Intervention 等继续保留原等待/控制语义。现有 Worker 预算不因新过程或任务身份而重置。
这个有证据的触发范围不替代后续阻塞任务接手、多轮修订、跨批准版本或完整资源预算接续工作。

新增 `ProcessAdjustmentStarted` / `ProcessAdjustmentFinished` / `ProcessAdjustmentSkipped` 事件供现有
轨迹和生成 Client 观察。协调器使用触发暂停事件作为持久身份，复用现有异步草案/独立审查及带
ProcessControlGuard 的应用/恢复；模型步骤之间复查控制事实。前台关闭沿已有取消路径收敛；API
新控制先取消该 Run 的自动规划，旧幂等控制请求重放不应打断较新的工作。自动规划用独立异步任务，
不能因为取消一个 Run 的规划而停止整个 API 调度循环。
显式 Planner 预设的 reasoning effort 不再从 Worker 隐式继承；未指定独立 Planner 模型时仍可沿用
原 Worker fallback。当前尚未宣称自动恢复验收通过。

本节是接线前的代码核对与实施约束，不表示自动 Planner 调度已交付。已验证的
`propose-process → review-process → apply-process → resume-session` 仍是显式入口链路。

用户结果：运行遇到批准范围内可调整的实现路线问题时，宿主自行收敛、生成过程草案、独立审查并继续
原 Run；用户主动停止、必要人工判定和未知副作用不被这个循环覆盖。

输入包括持久化的暂停来源、触发事件身份、原批准、当前过程版本、执行图、Attempt、Check、介入和
配置事实。输出复用既有 ProcessDraft、ProcessReview、ProcessRevision、恢复事件及用户可查的失败依据。
运行状态仍由 Orchestrator/RunController 推进；宿主负责取消并确认活跃执行收敛；应用服务协调现有
过程命令。Planner 不直接恢复 Run，Scheduler 不解释需求或自行批准草案。

当前代码核对发现：

- `ForegroundSessionHost` 的 Worker 等待、runtime idle、取消和异常曾全部调用内部 `PauseRun`，
  与公开用户暂停留下相同形态的事件/receipt；不能据此认定用户主动停止，也不能反过来自动恢复。
- `RunPaused` 的 reason 是自由文本；人工 Gate/Intervention 则主要表现为 BLOCKED 节点和独立的持久事实，
  不一定暂停整个 Run。仅判断 `paused` 会漏掉真正等待，也会误恢复不该恢复的运行。
- `queue_ready_attempts`、`retry_attempt`、Reviewer rework、启动恢复和未知执行结果各有暂停入口。
  需在来源处记录结构化 cause；旧事件缺少 cause 时保留未知，不通过字符串猜测补授权。
- 用户在系统已经暂停后再明确暂停，也必须留下新的持久控制事实；否则模型审查期间的停止意图可能
  被旧系统触发覆盖。相同用户命令的幂等重放仍不能新增控制事实。

自动协调器的接线顺序：

1. 先区分暂停来源，保留触发身份；用户取消/主动暂停优先，人工 Gate 和未决副作用继续走原介入路径。
2. 宿主确认相关运行已收敛后，复查当前过程、控制事件和未决请求，再调用已有过程草案与独立审查。
3. 仅在边界审查通过、证据仍新鲜且控制事实未被后续用户操作覆盖时应用；应用仍保持 paused。
   自动恢复前再次核对控制事实，不能把漫长模型调用前的允许状态当作恢复时的授权。
4. 同一触发使用持久幂等身份；失败、否定/不确定审查、未知模型调用结果均保留证据，不在轮询中
   无界重建草案。后续多轮修订必须有新的事实或明确的修订依据。

预算边界须与自动调用同时接通：`retry_attempt` 在编码模式按节点 ID 计重试；过程编译器会为修改的
任务分配新 ID，因此不能把换 ID 当作获得新重试额度的依据。`SingleSlotRuntime._budget_failure`
目前按 Run 的 AttemptBound/ProviderUsageRecorded 计 Worker 调用/费用，并不证明 Planner/过程审查
已纳入同一预算。自动化必须保留已有消耗，区分节点重试上限、路线调整额度和目标/Run 总预算；
尚无足够授权依据时不能因“自动调整”重置耗尽额度。后继 Run 的目标级预算接续仍属未完成工作。

正常入口验收需观察真实系统触发 → 收敛 → 自动草案/独立审查 → 同 Run 应用/恢复 → 原 Gate 验证，
同时核对未受影响成果、Session 连续性及实际消耗。当前尚无这条自动链路的运行证据，不新增常驻测试。

本轮已实施暂停来源基础：`application/pause_causes.py` 定义事件来源及旧记录的 unknown 读取规则；
RunController、Orchestrator、启动恢复与 ForegroundSessionHost 在各自来源处记录 `pause_cause`。
公开 PauseRun 字段不变；宿主内部暂停复用控制器但不生成用户命令 receipt，Ctrl+C 则保留为真正的
用户暂停。对已暂停 Run 的新显式暂停追加停止事实，内部收敛不覆盖已有具体暂停来源。
主 Agent 审查补齐了“系统已暂停后 Ctrl+C 仍须记录用户停止”的分支。
Ruff、121 文件格式检查、95 源文件 mypy 与 diff whitespace 检查通过；未新增测试文件，
未运行这批暂停来源修改的真实模型试用。事件 payload 原有开放对象承载新增字段，未改变公开 Schema
字段或数据库版本。自动协调器、预算接续和上述完整正常入口验收仍未完成。

后续接线增量：`ProcessControlGuard` 已接到应用服务的过程应用和两种 RunController 的恢复事务，
检查 Run 身份、原暂停事件身份、当前 paused 状态与过程版本。它是内部可选参数，不是新的公开授权；
调用方在应用后须保留原暂停事件 ID，仅将期望过程版本换为实际应用版本，不能重新采样用户停止事实
来掩盖期间发生的控制变化。带 guard 的恢复命令幂等重放只返回原结果，不再驱动旧串行恢复路径。
公开显式应用/恢复的命令字段不变；自动宿主尚未调用该约束，因此不能把代码存在当成自动流程验收。
安全重试的终止事件另记录 `retry_safety`；重试耗尽的暂停记录所属 Attempt/任务、预算消耗、上限和
计数范围。这些事实不增加额度，也不自动授权路线替换。

异步过程入口已接入：Builtin Planner 的同步方法包装原生 async 方法；应用服务的同步/异步入口共用
请求准备、成功和失败持久化，不用后台线程代替可取消调用。HTTP `propose-process` / `review-process`
使用 async 入口，URL、请求字段和响应形状不变；同步 CLI 继续可用。异步取消传播前记录该请求失败，
同一幂等键只读取已有请求；这不等于已证明远端 Provider 的未知请求被取消或允许自动重发。
122 文件格式检查、Ruff、96 源文件 mypy 和 diff whitespace 检查通过，真实 HTTP 过程路径试用进行中。

隔离 HTTP 规划试用发现，Planner 的方案只说明独立物理 Session，没有清楚表达已实现的逻辑 Phase
Session 连续性。已在 Planner 系统说明补上两者区别；首次草案未批准，通过正常讨论要求修订。
模型随后返回建议替换的说明文字但未生成新计划 ID，因此仍未批准，已要求通过图/设计工具持久化修订。
这些是尚在进行的正常入口观察，不是自动过程调整或产品 E2E 的通过记录。

这条 HTTP 试用进一步暴露实际讨论输入缺口：要求保留旧 Gate 的原始 argv 时，第三轮 Planner 只收到
base 节点上的 Check ID，没有对应 CheckSpec 的完整命令，因而无法 finish 修订，转而要求用户提供
系统已经保存的命令。正常 `GET /plans/{id}/checks` 已证实原命令完好，缺口在应用到 Planner 的输入链。
正在修复该链路，不能通过人工把完整命令贴给模型来代替产品修复。当前原草案仍未批准、未启动 Run。

证据位置：系统临时目录 `ehai-process-http-2a790fbc33924482a5307561e5bb3f17`，数据库 `state.sqlite`；
Goal `38c84e37-a442-43c3-a9b7-4d1047e681d9`；讨论 `94285b56-addc-48cc-9b47-c6339179a959`；
原草案 `15bd8d1b-785c-49f4-8371-8b34ef694c71`；原 Check `0d4b876e-b862-4278-94ae-c2c5956e1007`；
失败修订的 Planner Session `d298f601-0da8-4db2-be0e-f71cf2772e65`。第三轮讨论有正常完成的回答，
但没有新的计划 ID，不能称作修订成功。独立示例源码保持 clean，HEAD 为
`cd2cbe09ddcc14a3df0a1d210939f9e6fbd6d8f9`。本段记录时 HTTP 宿主已正常关闭，没有在后台启动 Worker。

该讨论缺口随后已修复并回到原 HTTP 路径复测：从同一事务读取 base 版本的 CheckSpec，透过
`ConversationalPlanner`/配置包装/Built-in 讨论传入 `base_checks`；输入 Schema 与 CheckSpec 序列化复用。
字段明确是 base 的历史事实，不把未批准草案叫作已批准条件，不替代 host 配置，也不授予命令权限。
主 Agent 定点核对实际数据库记录：原 Check ID 和完整 argv 能进入新输入且通过输入 Schema。
第四轮正常讨论没有人工粘贴原命令，得到已保存 v2 `14a21256-d185-4dc6-98e6-5059036b893e`，
Planner Session `6b60bb46-ae90-4aeb-b04a-eefb79a317e3`；新 Check
`ebe3d346-e3b7-443b-9b15-e59a2c6957ec` 的完整 argv 与原 Check 完全一致。
正常查询复审确认三个节点的 instruction 均未改变，仍为两个独立 work → 一个 read-only reviewer、
单阶段与同样的返工范围；设计现在正确说明逻辑 Phase Session 的连续性和独立物理 Session/工作区。
原草案历史保留，v2 仍为 draft，未启动 Run。这关闭了此次“修订时丢失既有检查输入”的修复回路；
HTTP 的过程草案/审查模型调用、取消及自动协调器仍未在本次试用中验证。
最终 Ruff、122 文件格式、96 源文件 mypy、Client generate/typecheck/build 及 diff whitespace 检查通过。
没有新增常驻或临时测试文件；只做实际失败输入的定点诊断和原正常 HTTP 路径复测。HTTP 宿主已正常关闭。

### HTTP 异步过程与恢复试用接续（2026-09-13，限定链路完成）

沿用上述隔离数据库，批准 v2 后通过正常 CLI `execute-plan --authorize` 保存明确的执行配置并启动
Run `62a04000-20c2-484d-a2d7-f672cb83fd8c`，再用 Ctrl+C 收敛。这不是 `/runs/start` 的完整配置等价验收；
该 API 的配置/授权证据接线仍待完成，未将宿主当前参数事后写成历史授权。
真实事件先记录 `unknown_execution`，随后记录 `operator` 为最新暂停来源，两项 Attempt 均 interrupted。

正常 HTTP 原生 async 路径已取得：

- 草案 `01d81e14-4b05-419c-bfeb-1d92bedd143e`，Planner Session
  `86945f04-a5dd-401a-8aee-8ce77ec6a097`，仅调整未完成文档的示例标题组织。
- 第一次独立审查 `69ed2b8c-e05f-4a95-8677-7bd6989cb149` 在 HTTP 200 后收到 `response.failed`，
  失败证据保留，无报告、未应用。现有传输日志未保存更具体的 Provider 失败原因，不能据此认定为 Schema 错误。
- 处理中重放同一审查 key 返回同一 reviewing ID，只有一次开始事件；健康/轨迹查询正常响应。
- 明确新请求重试 `16b662e1-09cd-436c-a102-498d1df45731`（Session
  `b2870e4e-8f7b-4c85-a683-99b0e64f8f30`）完成 preserving 审查。一次不成立的义务映射由 finish 工具
  拒绝后，模型修正再通过；未放宽校验器。HTTP 应用过程 v2
  `f3b74c23-1161-4d73-86ae-3b971340cd78`，父版本 `43c7eeee-7960-489f-b869-6b89bed8465c`，应用仍暂停。

正常恢复随后暴露并修复三个具体调度问题：

1. HTTP resume 只变成 running，原 dispatch 已 completed，没有新 Attempt。BackgroundRunController
   现在将恢复、复用/重排原调度项及 receipt 放在同一事务，复用应用层排队逻辑，不新造 Run。
2. 混合 READY/PENDING 状态时，候选选择的 `or` 忽略已满足依赖的 PENDING 工作，空闲容量不能利用。
   现在合并这两组互斥状态候选，再按活动 Attempt 与限额过滤；Reviewer 的依赖屏障不变。
3. HTTP 暂停后未释放宿主占用；补上释放后，又观察到循环立刻重领已收敛的 paused Run。
   API 现在复用 Runtime 的安全释放入口，存储领取条件排除已暂停且无执行中 Attempt 的 Run，
   仍允许异常暂停中的运行 Attempt 进入恢复。状态从实际 snapshot JSON 读取，未新增数据库列或改版本。
   暂停/取消幂等 preflight 在异步收敛前完成，避免旧控制请求重放打断较新的执行。

仓库外 `diagnose_dispatch.py` 在真实数据库的只读备份上创建一次性诊断副本，重建已观察到的混合
就绪状态与暂停领取条件；不调用 Worker、不改真实试用数据库。第一次诊断抓到本次 SQL 把快照状态
误当独立列的问题及诊断夹具非法状态恢复的问题，均已按实际结构修正；最终两项诊断通过。
它不是产品 E2E，不复制入库、不扩成常驻测试。

原 HTTP 路径的复测证据：

- 恢复后确实产生新 v2 Attempt，不再只改变显示状态。
- 已完成代码 Attempt `a6c50432-10a9-4f52-aa0a-7cb33bb495d1` 和文档 Attempt
  `4fdb6a6d-598c-4c20-96e8-9ccbf23e0e25` 在后续有序暂停/重开中保留，没有重跑。
- UTC `02:14:49` 正常 API 暂停后原调度项保持 pending、claim_owner/lease 清空，而非被立即重领。
  原租约原本到 `02:18:59.752674Z`；重开后的 Reviewer Attempt
  `203822c4-1148-41c2-b387-ee4b2b1255ca` 在 `02:15:38.365869Z` 已启动，早于该旧租约到期。
- 新 Reviewer 运行中重放旧暂停 key `http-process-pause-release-final` 返回 running，暂停事件数量
  前后均为 11，没有打断新 Attempt。当前等待该 Reviewer 和原行为 Gate 完成。

本轮只读调查子 Agent 误写了未完成授权接线；主 Agent 已中断它并撤下这些确定来自该子任务的片段，
保留此前累计工作与本轮实际恢复修复。没有合并、提交或推送。
本节不证明自动过程触发器、过程模型取消、API 原始启动配置等价或可持续自举已经完成。

最终该 Run 于 UTC `02:18:07.542140` completed：Reviewer
`203822c4-1148-41c2-b387-ee4b2b1255ca` succeeded，原 Check
`ebe3d346-e3b7-443b-9b15-e59a2c6957ec` / CheckRun `2cd568df-e7dd-4ba7-a3bb-45d39b27e05e`
通过（exit 0），Checkpoint `ceec6461-7912-4ae3-b5fe-6c07aa5beab0` 绑定过程 v2。
最终代码 commit `abcb194f34b20d675f8767c6b3c244247b7e1a95`，位于该临时根目录下
`worktrees/203822c4-1148-41c2-b387-ee4b2b1255ca`。
相对原基线仅 README.md 与 display_name.py 改变；原 README 字节前缀完整保留，追加了两类输入标题。
最终 Gate 留有未跟踪的 `__pycache__`，不在交付 commit 中；示例原工作区保持 clean。
整个 Run 只有一个逻辑 Phase Session `9b067a64-96f5-4f39-baf2-58ca88b8ae43`，9 次 Attempt 加入、
一次过程应用、一次 GatePassed；原批准 Plan 的三个节点仍保留批准时的 pending 快照，没有被执行状态覆盖。
SQLite integrity/FK 检查通过，dispatch completed；HTTP 宿主已正常关闭。
代表性 HTTP 过程规划/独立审查/应用、实际恢复及暂停重开链路现有真实证据；自动触发仍未实现。

## 用户结果

2026-09-13 用户将本轮最终目标明确为可持续自举开发，完整目标与验收约束见
[Self-hosting Goal](SELF_HOSTING_GOAL.md)。现有 P2 未完成项仍属于该目标，不以自举示例替代它们。

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

### 第一阶段实施契约：Phase、Reviewer 与 Gate

本阶段按以下单一状态所有权实施，不另建一套与 PlanGraph 竞争的执行状态机：

- `Phase` 是获批 `PlanRevision` 的不可变结构，声明本阶段节点、阶段 Reviewer 节点及阶段 Gate；
  Phase 的实际进度由其中 PlanNode、Attempt、Check 和 Gate 事实推导，不保存可漂移的第二份完成状态。
- 阶段 Reviewer 是可由 Scheduler 正常调度的 `reviewer` PlanNode。它依赖该阶段全部交付节点，读取
  宿主准备的代码和 Artifact，提交结构化 `review.json`，但不能修改代码、批准 Gate 或推进 Run。
- 自动 Gate 仍由冻结在获批方案中的 CheckSpec 和宿主 CheckRunner 判定。Reviewer 的 `pass` / `revise`
  只是证据与建议，不能替代 CheckResult 或 GateDecision。
- 人工 Gate 将使用与自动 Gate 相同的获批 Gate 身份和成果版本，保存待决问题、Reviewer/Check 证据及
  人工作出的通过或不通过决定；等待人工与操作员主动暂停、取消和终态失败必须可区分。
- Gate 不通过时，Orchestrator 依据获批路线把问题送回负责修改的节点；Reviewer 不通过提高自身权限来
  修代码。改变需求、接口、Gate 或授权仍需新批准版本。

用户结果是：用户查看方案时能明确看到阶段、Reviewer 和每个自动/人工 Gate；执行时 Reviewer 通过正常
Worker 入口运行，后继阶段只有在对应 Gate 通过后才可调度。输入为获批 PlanRevision、阶段成果和冻结的
检查/人工条件；输出为结构化审查 Artifact、绑定同一成果版本的 Gate 证据及明确的后续路线。

状态和权限所有者分别为：Planner 只提出结构；审批服务冻结结构；Scheduler 分配 Reviewer Worker；
Orchestrator 独占 PlanNode/Run 推进；CheckRunner 产生自动检查事实；人只通过公开命令决定人工 Gate。
正常入口为 `discuss-plan` / 查询 / `approve-plan` / `execute-plan`，后续补充人工决定命令；Provider
日志或直接数据库修改都不能代替入口。验收先使用正常入口运行至少一个阶段 Reviewer 和阶段自动 Gate，
人工接口接通后再运行一个真正等待用户决定并继续的阶段，不用测试构造器伪造通过。

当前代码增量已使 `reviewer` 成为可规划、可调度的 PlanNode kind：Built-in Reviewer 使用只读
Workspace Tool、可选的精确宿主命令和严格 `review.json` 交付；其建议不拥有 Gate 权限。Planner 的
`set_plan_phase` 把全部节点划分为有序 Phase，每个 Phase 的路径必须汇入其 Reviewer Gate，持久化、
Checkpoint 恢复、公共查询 Schema 和 TypeScript Client 保留同一结构。人工决定已有持久化与公开入口；
Gate 不通过后的阶段回送已进入当前接续代码，正常阶段执行验收尚未完成，未因此标记第一阶段完成。

### 2026-09-13 接续架构决定（待实现）

采用同一 Goal 下的后继 Run 承载真正获新批准的边界变化；批准内的过程变化仍在原 Run 内完成。
用户要求尽量少调整批准边界，并避免随过程版本或 Run 身份变化反复刷新上下文。
实施须把批准归属、执行图版本和上下文生命周期分开，保留未受影响的阶段上下文及有效成果，
只向受影响执行传递批准差异和失效信息；物理会话不能安全延续时使用有效 handoff。
详细约束和正常入口验收见 [Execution Model](EXECUTION_MODEL.md) 的 2026-09-13 接续节。
这项决定消除了 Run 接续路线的待选项，不表示接手、自动调整或真实产品验收已经完成。

### 2026-09-12 接续边界与当前状态

此前 Reviewer/Phase 的未提交改动已保留并接入当前 worktree；第 1 项仍在开发，不表示人工 Gate 或
P2 已验收。`rework_node_ids` 已贯通 Phase 的 SQLite 编解码、查询及生成 Client；它表示当前计划的
返工路由，不是需求、接口、Gate、授权之外新增的批准底线。后续过程调整可以修订该路由并保留轨迹，
不能以路由字段存在为由要求每次改变任务拆分都重新批准。

Reviewer 的 `review.json` 校验现位于公共应用层，Built-in 在工具调用时复用以支持模型修正，宿主在
接收任何框架的候选时再次验证。报告格式、输入 Artifact 引用和建议合法性不等于审查内容正确，
`pass` / `revise` 也不是人的批准或 Gate 的最终结果。

Reviewer 重审按当前有效依赖准备代码，不再优先使用自己的上一次审查快照。只读基线只在 Reviewer
启动前且整合无冲突时读取；普通编码/整合 Worker 仍可收到冲突上下文并处理，Reviewer 则在存在
冲突时明确拒绝启动。恢复继续使用原宿主已记录的基线，不把重启时的 HEAD 当成授权基线。

后续先完成同一阶段中的执行、审查、人工判定和拒绝返工回路，再推进阶段 Session/handoff、批准
底线内的过程调整、阻塞回复与恢复。人工条件应进入现有 Check/Gate 体系；等待请求和人的决定需要
持久化并绑定所审成果版本。局部等待不得无故停止独立路径，恢复不得通过重跑已完成检查替换原证据。
基础入口已新增 `get-run-checks` 和 `decide-human-check`；具体参数见 Usage，阶段闭环仍在接续。

返工不只是把 Reviewer 改回 ready：还需将失败证据交给修改节点，使后继重读新成果，并处理已经绑定
旧 Artifact 的分支选择；已完成且不受影响的节点、历史证据和已剪枝路线不能被一并重置。
当前接续代码已将 Gate 失败与返工状态变更放入同一事务：从本阶段保留路线的返工目标计算阶段内
受影响的后继节点，重开实现、后续整合/比较和 Reviewer，保留原 Attempt、Artifact、CheckRun 及
Checkpoint。`PlanNodeReopened` 带原 Gate、Reviewer Attempt 与原因，Worker 上下文读取对应失败
检查和审查报告，不要求 Worker 猜测为什么返工。

受影响 Evaluator 的分支选择通过 `BranchSelectionInvalidated` 失效；未受影响的候选代码不因此
重跑，可作为重新比较的输入。若公共上游确实改变了其他路线输入，其后继也需重做，不能沿用旧
候选。分支比较必须限制在对应的 fork/merge 组和最新有效候选，不能消费其他阶段或旧 Evaluator
Attempt 的报告。Gate 条件、需求、接口及授权不在这些状态变更中修改。
该返工链路仍待正常真实阶段执行验证，不能用结构与静态检查通过代替阶段闭环验收。

代码 handoff 现区分“用于准备的代码提交”和“任务的依赖提交基线”。同一输入下的后续重试可继续
已有修复；上游或选中路线改变时，受影响的下游从当前依赖重新执行，旧候选仅保留为历史，不再优先
覆盖新输入。旧 Attempt 元数据若没有依赖基线，拒绝复用其代码快照，但允许从当前有效依赖重新准备
新 Attempt；不猜测旧基线、不修改旧文件，也不把旧快照升级为独立 handoff。存在但损坏的依赖记录仍
明确报错。这是保守重建路径，不表示旧会话内容已经迁移。

人工 Gate 的当前实施单元复用 `CheckSpec` / `CheckRun` / `CheckResult` 和 `ALL_REQUIRED`，增加明确
的人工条件种类而不让模型适配器自动执行它。开放请求随 CheckRun 持久保存批准版本、待审问题和
Artifact ID/hash 快照；公开决定命令复用现有幂等 receipt，校验请求、Attempt 和成果仍适用后才记录
人的结果。现有 Run checks 查询同时展示待决问题与决定证据，CLI/API 复用同一命令语义。

实现时必须同时修改启动恢复和前台宿主：开放人工请求不能按崩溃的自动 Check 标记 interrupted；
无 ready 节点不代表人工 Gate 已完成，也不能由普通 resume 绕过；决定落库后要可靠推进或重新排队，
不重复执行已持久完成的自动检查。等待的展示由持久请求事实推导，不把所有等待一律改成操作员暂停。
这一单元的正常入口验收是查询待决请求、提交决定、重启重读并推进；仍需接上前述拒绝返工路径，
不能只完成 DTO、Command 或模型枚举就宣布交付。

本轮以仓库外临时状态、现有 `single` Planner / `fake` Worker 的正常 CLI 检查应用流程：显式
`human:<问题>` 条件生成 HUMAN Check；候选产生后等待，`recover` 保留同一请求；提交带 request token
的决定后通过同一 Gate 完成，再次提交同一幂等命令没有新增 Attempt、Gate 或 Checkpoint。Trace 中
仍只有一个 Attempt、一个 CheckRun 和一个 Checkpoint。试用中首次附加请求的不可变约束错误已修复，
并在原数据库通过恢复与决定重走原路径。

该记录只证明人工条件基础命令的等待、持久化和幂等推进，不是真实模型、阶段 Reviewer、阶段返工、
并行等待或唯一产品 E2E 的通过证据。试用状态位于仓库外
`ehai-human-cli-5e4e17dbbf0043bf944bdf972aa0de17`；未新增常驻测试。

讨论入口现以空条件表示未指定，不再注入默认 `command:exit-zero`。没有显式输入时，先保留当前
已批准契约，再按同一讨论的最近显式条件及关联批准基线解析；没有这些依据时，Built-in Planner
必须提出非空的真实 command/human 最终 Gate。原始输入条件仍记录在讨论事件中，避免把系统默认
值伪装成用户要求。显式 `propose-plan` / `replan-plan` 的非空条件校验保持不变；新方案仍需批准。

### 阶段 Session 与独立 handoff：接续实施契约

以下是下一实施单元的设计，不表示当前 Connector 已支持这些能力。

| 模块 | 用户结果与输入输出 | 所有者与失败边界 |
| --- | --- | --- |
| 阶段 Session | 同一 Run/Phase 的任务持续共享目标、批准边界、讨论及交接事实；输出可重读的共同会话与成员执行记录 | 宿主管理身份、追加顺序和成员；Worker 发言不变成批准、Gate 或代码选择 |
| Provider 执行 lane | 将成员执行映射到 Built-in Turn 或外部 Thread/Turn，保留具体预设、权限与隔离目录 | Connector 管理物理句柄；不能让不支持并发的底层会话承载同时写入，也不为了共享会话把独立路线全部串行化 |
| 独立 handoff | Worker 明确交接完成内容、必要上下文、剩余事项和已知问题；宿主确认其实际代码版本、输入基线及所属任务后持久化 | 交接不完成 Attempt/节点，不通过 Gate；确认失败不反馈已接收，普通停机快照不升级为 handoff |
| 接手与回退 | 新执行接收仍适用的交接；否则从本任务有效依赖和选中路线重新准备，保留其他分支结果 | 宿主核对版本和分支；旧输入交接及未提交脏状态只保留为历史，不混入新基线 |

实施先接通 Built-in 的阶段共同会话：每个模型步骤可消费共同记录的新增内容，并有明确的读/写
入口，不把一次性启动摘要叫作共享 Session。物理模型历史与阶段共同会话分别保存；共同记录须带
作者、所属节点/分支与顺序，重启后恢复读取位置。未选中路线的发言可作讨论证据，但不自动进入
工作目录或成为已确认决定。外部 Connector 的映射另行核实，不能以 Built-in 接通宣称 Server 已接通。

独立 handoff 必须有自身记录及不可变代码版本，不能复用当前一次性候选 `result` 缓存冒充多次交接。
代码与上下文完整确认后才可用于接手；随后发生的新写入不改变既有 handoff。旧元数据缺少依赖基线时，
保留旧文件和证据，不猜测基线或把旧候选写成新交接；具体可兼容恢复的范围由已有证据确定。

恢复审查确认 Built-in 曾在启动恢复时重启未完成 Turn 的本地循环并沿用原目录；仅以 Attempt/Session
ID 没变，或单次写工具已有返回，不能视为有效 handoff。本轮已将本地恢复与完成候选接收分开：
完成 Turn 直接重收候选，不因新 ToolSet 重跑模型；仅使用已知本地工具的未完成编码执行记录为
`local_rollback`，以新 Attempt 选择有效交接或上游。仍活跃的当前进程 task 不被重复替换；涉及
未知 Provider 结果或快照外工具副作用的执行等待人工。外部副作用核对、回复和解除阻塞仍待后续闭环。

正常入口验收须能观察：同阶段独立任务共享新增上下文、不同隔离目录并行执行；中途明确 handoff
后停止并由新执行接手；无 handoff 的中断回到该路径有效上游；旁支已完成成果不重跑。先开放这些
能力，再按用户授权选择真实模型试用；本契约不预设永久测试，也不替代最终共同确定的唯一 E2E。

本轮已接入 Built-in 共同讨论的实现基础：`PhaseSessions.join/read/publish` 复用现有 UoW 与公共
EventLog，保存 `PhaseSessionOpened`、`PhaseSessionJoined`、`PhaseContextPublished`。同一
Run/Phase 只创建一个逻辑会话，成员从运行中的持久任务及物理会话推导，不由模型自报；发布按
Attempt/key 幂等，分页按持久 offset 排序。Worker 每步可消费新增讨论，也可通过工具读写；
消费游标绑定阶段会话，并从持久模型输入恢复，允许持久化失败后的重复投递而不提前吞掉消息。
没有新增数据库迁移。共享目标和批准上下文仍由当前 Worker request 提供；跨阶段交接与 Server
映射仍待后续实现，不把这个基础标记为第 2 项整体完成。

独立 handoff 已接到 Built-in 正常工具入口：非 Reviewer/Evaluator 的 `submit_handoff` 把结构化
继续说明交给 CodeRuntimeConnector；宿主捕获独立、不可变的 Git 版本和 patch，再以同一
Attempt/key 的稳定身份写入 `AttemptHandoffConfirmed`。文件记录与候选 `result` 缓存分开；
事件包含原计划、节点、分支、物理/阶段会话、依赖提交、代码位置及说明指纹，不改变执行完成或 Gate。
工具取消会等待宿主 capture 收敛后再释放本 lane，其他 Worker 不被同步 Git 调用阻塞。
重放与接手均重验代码记录；较新的成功候选优先，后续中断 Attempt 的匹配交接可给新执行接手。
Stage 共同记录能读取宿主交接事实，但它不自动授权导入其他分支代码。此链路已静态检查，仍待真实
正常入口和中断场景验证；不以方法存在或静态通过代替第 2 项验收。

结果查询同时修复版本证据错配：展示的代码与检查按同一 Attempt 关联，live snapshot 只能补充
同一持久候选的同一 commit；无持久候选时不以停机快照补出结果。人工检查未决时检查汇总为未判定，
不因其他自动检查已通过而显示全部通过。旧依赖元数据缺失允许从有效上游重建，但不复用未知基线。

已运行全量 Ruff、格式检查、mypy、Client 生成/TypeScript 检查与构建及 CLI 帮助入口检查；这轮没有
真实模型调用、常驻或临时测试新增，亦没有阶段并行或独立 handoff 的真实验收证据。

### 阻塞便签与对应回复的接续实现

本轮先接通明确的 Worker 阻塞和未知外部副作用，不把任意异常自动包装为可恢复成功。Built-in 的
`report_blocked` 或无法安全重开的外部结果生成结构化阻塞事件；Orchestrator 在同一事务中保留
`InterventionOpened`、中断对应 Attempt 并将节点置为 `blocked`。这不是失败分支，不进入候选比较
或自动剪枝；无依赖的就绪节点仍可执行，全部空闲时释放调度 claim 并等待输入。

便签保存问题、证据、需要的回答、原批准计划、节点/分支/会话以及已有 handoff/Artifact 引用，
请求 token 绑定不可变原请求。`ReplyIntervention` 表达“已经核清，可以在现有批准下继续”，不是
扩大权限或降低 Gate。回复与节点解阻、命令 receipt 同一事务提交；只解开该便签对应的当前节点，
回到 pending 后重新核对依赖，后续 Worker 获取原问题及回复。重复旧回复不能解开新的阻塞。
Run 若本来 paused，回复不代替显式 resume；普通 resume 则不解除仍未回答的 blocked 节点。

启动恢复保留空闲的 blocked 等待；有未答便签时禁止用旧 Checkpoint 丢弃未核清的副作用或决策。
取消仍可终止 Run，历史便签保留。需要改变需求、接口、Gate、授权或中间路线的回复与可追踪
Planner 调整/修订审批仍待接入，不能把这一“原批准内继续”基础宣称为完整第 3–4 项。

handoff 复审还修复了超过 cancel grace 的宿主捕获竞态：Runtime 在超时、显式取消及完成候选赢得
取消竞态时等待 Built-in 本地 task 和 host capture 真正收敛，再允许工作区清理。不以 grace 到期
作为 host work 已停止的证据；候选捕获也纳入宿主任务等待，启动准备收到取消后先收敛 Git 工作再返回，
避免同类竞态从 handoff 转移到其他后台 Git 调用。此改动仍待真实超时/中断路径验证。

阻塞查询/回复的 CLI、HTTP 和生成 Client 已接入。收尾时修正 Client 生成源，确保人工 Gate 决定及
阻塞查询/回复方法不依赖手工编辑生成产物；连续两次生成的文件 hash 一致，方法保留，TypeScript
类型检查和构建通过。Python 全量 Ruff、格式和 mypy 通过；未新增测试或调用真实模型。

### 批准底线内的过程版本：实施路线

当前 SQLite 的 `_plan_structure` / `_validate_plan_child_identity` 固定 PlanRevision 结构和子节点归属，
`_run_identity` 固定 Run 的原计划绑定，Attempt/CheckRun/Checkpoint 又依赖这些身份。这些保护不能
直接删除；把修改后的图塞回原版本，或把 Run 改绑另一个 PlanRevision，会混淆原批准与历史证据。

下一单元采用独立、追加式 `ProcessRevision`，而不是修改原批准 PlanRevision。过程版本引用原
批准基线、父过程版本、调整原因和完整执行图/可读过程说明；Run 的原批准绑定不变，通过独立的
active process 引用解析当前执行图。使用现有 PlanGraph 结构表达，不复制一套规划语言或 Agent Loop。
实现时必须一并处理节点实体与版本成员关系，不能在现有全局 node 主键限制下伪装成同一节点属于
两个 PlanRevision。未改任务可以保留逻辑身份；改变任务意义/输入时新建执行身份，历史 Artifact、
Attempt、Check、handoff 与旧过程版本仍可精确关联。

| 模块 | 用户结果与事实边界 |
| --- | --- |
| Planner 调整入口 | 接收原批准需求/接口/设计、现行图、Gate 与执行/阻塞证据，提出过程变更；发现批准底线变化则走待审修订，不自行授予批准 |
| 过程版本存储 | 原子保存新版本、父版本及调整依据并更新 active 引用；旧批准和旧过程版本保持可查询，不删除原轨迹 |
| 图与边界校验 | 精确保留原 Check 条件和必需性、Gate 身份/作用范围、权限；另验证阶段/分支边界及必需 Gate 未被新路线绕过，不能只比较 Gate ID 集合 |
| 执行与恢复 | Orchestrator、Scheduler、检查、handoff、查询和 Checkpoint 统一解析过程版本；只复用任务含义、依赖、分支及代码仍匹配的成果 |

需求和对外接口目前主要保存在自然语言 design_document，而不是已有可机器判定的独立结构字段。
保持设计原文或其 hash 只能证明基线未被覆盖，不能证明新指令在语义上遵守基线；不得把指纹相等
当作语义通过。下文已接合 Gate/义务结构检查和独立语义审查；自动调整调度与正向产品验收仍待完成。
这条路线不是“只允许改 instruction”、每次重新批准，或从零新 Run 的替代验收。

本轮已先补齐等待人工后的待审草案入口：`replan-plan --source-run-id` 接受没有 pending/running
Attempt、没有活跃自动 Check 的 paused Run。Planner 上下文包含单独的 blocked 节点、便签/回复
摘要、原 CheckSpec，以及原方案设计与 Phase 信息，不把 blocked 当作失败候选。便签摘要有数量及
字节边界并报告总数；原检查条件不做摘要删减。
为 paused 来源生成草案时，Goal 继续保留原生效契约，直到用户明确 approve。存在活跃来源却未显式
指定 source Run 的 replan 会拒绝；执行尚未收敛时也不能批准切换 Goal 契约。
这只开放待审修订，不是运行中自动调整、跨方案接手或既有成果迁移的完成证据；上述过程版本层仍待实现。

计划查询与 Planner 基线上下文也暴露节点的 `required_capabilities` / `session_policy`，保留既有
运行字段，不把 Planner 当前默认的空能力要求隐藏成已经完成了预设分工。生成 Client 已同步。
本轮全量 Ruff、格式和 mypy、Client 生成/类型检查/构建通过，Planner 输入 Schema 静态校验通过；
未调用真实 Planner，未新增测试。

### 修订发布竞态与未决请求保护

`propose-plan` 及会切换 Goal 契约的 `replan-plan`，现在在调用 Planner 前和发布草案的写事务内
均检查该 Goal 的全部非终态 Run。Planner 调用期间新启动的 Run，或历史失败来源之外的另一个
活跃 Run，不能被草案发布替换契约。写事务复用 SQLite 的 `BEGIN IMMEDIATE`，检查与写入之间
不释放锁；显式 paused 来源继续只保存草案，不切换生效契约。

审批不同契约时，除检查执行已收敛，还保护 paused Run 的未决人工 Check 和未回复便签，避免
旧请求尚未处理就因 Goal 契约变化失去可回复性。当前会明确拒绝这一审批，不自动取消 Run、
伪造便签回复或删除历史。这是显式修订接手完成前的临时限制，不是最终的跨方案继续机制。

暂停来源保留生效契约后，重复生成新草案还会重复占用同一个后继版本号。入口现在在 Planner 前及
写事务中检查已存在的后继版本，返回可查询的方案 ID，不浪费模型调用后才暴露 SQLite 唯一键错误，
也不覆盖原草案。多轮待审修订及其批准接手仍需完成，当前不宣称这部分用户流程已可用。

### Planner 调度字段接通

Built-in 图工具的节点输入、草案查询、PlanNodeTemplate 和公共 builder 现在完整传递
`required_capabilities` / `session_policy`。新增节点默认保持空能力要求与 `new`；更新省略时
保留已有设置，全部参数验证后才替换节点，拒绝操作不留下部分修改。Dispatcher 同时筛选能力和
Session 策略，与持久层原有的绑定约束一致，不先启动错误预设再在写 Attempt 时失败。

这些字段只表达调度要求，不扩展工具权限，也不证明底层 `reuse` / `fork` 已交付；正常配置预设
仍使用 `new`。本轮 Ruff、格式、mypy 和 11 个图工具输入 JSON Schema 静态校验通过；未运行真实
Planner 或 Worker，没有新增测试。过程版本层、跨方案接手及整体产品验收仍未完成。

### 批准方案与 Run 执行图隔离

新增 `run_execution_plans` 和统一 `get_execution_plan(run_id)` / `put_execution_plan(run_id, plan)`
存储入口；新 Run 创建时在同一事务中复制其批准图。执行状态变化不再覆盖批准方案及其子节点行，
`put_plan_revision` 拒绝修改已批准记录。当前 Run 图的写入继续验证同一批准身份、完整结构及合法
节点/分支状态迁移，包含 Phase 的 rework 路由；该阶段尚未开放结构调整，后续发布入口见下文。

Orchestrator、Scheduler、停止/恢复、Gate、Checkpoint、handoff、便签、阶段会话和代码基线选择
统一读取 Run 图。Checkpoint 恢复只恢复 Run 图，不回写批准方案。Replan 仍绑定原批准版本，但
Planner 接收来源 Run 的当前图与状态，发布草案前再次核对它未变化。运行证据校验使用所属 Run，
不从另一个 Run 借用成功 Attempt。

CLI `get-run-plan`、HTTP `GET /api/v1/runs/{run_id}/plan` 和 Client `getRunPlan` 暴露当前执行图；
`get-plan` 保留原方案查询。Schema 12 是追加表和既有图状态复制，旧历史行不删除；旧方案保留迁移
前记录的状态，不将它包装为重建了最初批准时的完整快照。

正常入口检查使用既有已结束人工 Gate 试用数据库的副本：Schema 11 升级 12，`integrity_check`
返回 ok、外键检查无错误，原 completed Run 的图状态保留。另经正常 single Planner / fake Worker
命令创建、批准、执行一个明确标注为合成诊断的单节点任务：Run
`44bd26b3-562d-4c7e-9e7d-85d899f66b63` 完成后，原方案
`05d0d061-745f-4dee-89cc-9630636db274` 的节点仍为 pending，`get-run-plan` 为 completed。
执行进程已退出，证据数据库位于系统临时目录
`ehai-execution-graph-f103996569a74b99a4b528d1f2ceed4e`。这只核对迁移、状态隔离和 CLI 接线，
不是阶段/真实 Worker 恢复验收或唯一产品 E2E；没有真实模型调用或新增测试代码。

本单元消除运行状态和批准记录共写的耦合，尚未实现追加式过程版本、动态节点成员关系、批准底线
内调整的发布/校验或跨方案接手。这些仍是下一单元必需工作，不以当前结构写入限制代替最终能力。

### 初始过程版本及证据身份

在 Run 图隔离后，Schema 13 增加不可变 `ProcessRevision` 初始图记录与 Run 图的 active 引用。
新 Run 在同一事务中创建首版本；后续节点推进只改变 Run 图，不能修改过程快照或在状态写入中
改变结构。迁移仅对现存 Run 图记录 `legacy_snapshot`，时间是迁移观察时刻；不将旧 Attempt、
Checkpoint 伪装为当时已绑定过程版本。旧历史行与此前证据保持原样。

新 Attempt 显式绑定当前过程，字段纳入不可变执行身份；Artifact/Check/Worker 绑定校验按该
Attempt 的历史过程图读取节点定义，避免未来 active 图改变后重新解释旧执行。旧无版本 Attempt
继续依据原批准图；新创建的 Attempt 不允许省略过程身份。新 Checkpoint 绑定当前过程，恢复时
不能静默跨过程；原无版本 Checkpoint 仅在迁移首过程且结构一致时维持原恢复能力。
Checkpoint 幂等写入先对照已存对象，避免新增可空字段或 active 状态变化破坏旧事实重放。

公开 `get-run-plan` 返回 active 过程 ID；`get-process-revision` / 对应 HTTP 和 Client 返回冻结图、
创建原因、来源与版本身份；Attempt/Checkpoint 查询同步携带过程 ID。此处只实现首版本和证据
关联，未开放后继过程发布、图结构变更或自主调整。此前恢复幂等继续入口 `_is_ready_to_continue`
遗漏读取批准图的问题也已修正为 Run 图。

正常 CLI 在上一轮 Schema 12 证据库的新副本上迁移至 13，原运行、计划、节点、Attempt、Check 和
Checkpoint 行核对保留，integrity/FK 检查通过。合成 single/fake Run
`c46f9394-c961-4e70-824a-3fa5ae1f8647` 正常完成，Attempt
`bbef4443-ae99-46d8-960b-5a857692f5de`、Checkpoint
`9f43f07e-48db-4842-a49f-d26fcd1bb9df` 和 active 图均绑定过程
`dd6a5a44-29d1-442c-a6bd-2159b6cd17dc`。过程快照节点仍为 pending，实时图为 completed。
证据目录为系统临时目录 `ehai-process-version-de182f1aee5947c29ba8688a0f8e2c74`。
这只是版本绑定、迁移与入口使用检查，不覆盖真实模型、阶段/并行恢复、后继过程或产品 E2E；
没有新增测试代码，也未改原试用数据库。

### Gate 保持判定的算法约束（待实现）

GPT-5.5/xhigh 数学子任务明确了两项实现约束：dependency 是 AND 前置，不能用“原始图存在绕过
Gate 的路径”判断实际绕过；若 D 同时依赖 G 和 W，W→D 的快捷依赖不取消 D 必须等待 G 的条件。
应按实际调度语义推导必经通过的 Gate 集合，并为 evaluator 的失败分支、选中分支的成果输入单独
处理条件，而不是把所有 sibling Gate 都算成必须通过。等待 Gate 也不代表消费的任意版本已验收，
还须核对实际 Artifact/代码依赖与该 Gate 的证据版本。

批准边界不能默认冻结全部 fork/join、探索 alternative ID 或 rework target：用户允许这些过程调整。
应保留原 Gate 条件/作用范围和必需成果/接口/授权，将原义务映射到新任务及其必需条件；缺少映射
先让 Planner 补充，不以每次重新批准替代自主调整。自由文本语义不能由 hash 相等证明；上述规则
在此阶段仍是设计规则；下文已将其接入义务映射与发布校验，完整有效性仍需真实路径验收。

### 后继过程存储与 Gate 结构检查（接合中）

Schema 14 将原 PlanRevision 的 nodes/edges/branches 成员清单固化到其完整快照，保留原字段和
次序；`get_plan_revision` 不再根据共享实体表的全部行拼装成员。这样后继过程可登记新实体以满足
Attempt/Artifact/Checkpoint 的外键引用，而不污染原批准图。旧实体行不删除、不改写为新任务。

内部 `publish_process_revision` 已具备父版本/序号/原批准身份校验、审查所依据的当前图快照比对、停止在途 Attempt、未决工作
保留、Gate 结构检查，以及新实体/过程快照/active 指针的同一事务写入。内部 savepoint 保证调用方
捕获失败后也不会提交半套成员。复用节点必须保留当前状态、任务定义和输入 scope；定义或输入
变化要使用新身份。新节点从 pending 开始，新分支从 active 开始，旧成功结果不通过伪造新节点
completed 来迁移。改变边定义需要新 Edge ID。Branch ID 是可跨过程版本保留的逻辑容器；分支的
成员、fork/join 与标签定义保存在各自完整过程快照中，共享实体行仅作历史身份锚点，不覆盖旧记录。
候选组或 Evaluator 身份变化会使选择失效；只改变下游 join 身份不强迫未变的兄弟候选重跑。
保留选中路线还须核对持久 BranchSelected 所引用的比较/选中 Artifact 来自对应节点最新成功
Attempt，且仍覆盖每条可行候选；过期证据拒绝复用，不能仅凭图签名保留选择。

`ProcessRevision.gate_owners` 记录原批准 Gate owner 到当前承载节点的一一映射；条件仍引用
原不可变 CheckSpec，分组不能被合并/拆分来绕过。`process_gates` 按 AND 前置、分支失败、
viable 分支与 selection success 计算有限 Gate 集合族，超过 256 个最小组合或遇到未支持结构时
明确返回证明不可用，不静默放行。数学复核发现普通/reviewer 消费 Evaluator 完成时曾漏掉有效
BranchSelection 前置，已修为消费 selection success；raw Evaluator completion 与允许后续调度
的成功选择是不同事实。

早期 `process_changes` 使用分支关系代理辅助判断 scope，该代理现已被下文的明确义务映射替代；
新发布必须同时提供原来源材料和映射，不能仅凭旧分支形状相似放行。原 Gate 分组、phase、
前置保持及最终 Gate 家族检查仍保留。需求、接口语义、Artifact provenance 和授权审查不能被
Gate 集合族替代，也不能把结构检查通过宣传为已完成产品自主调整验收。

迁移通过正常 CLI 在上一轮证据库的新副本完成：三份原方案的旧字段与成员内容/顺序核对一致，
Run、实体、Attempt、Check、Checkpoint、ProcessRevision 和当前执行图行保持原样，integrity/FK
检查通过。副本目录为系统临时目录 `ehai-process-members-1d25f601709e42149558ecd31341ec53`。
这只验证迁移和查询；未实际应用后继过程，未调用产品模型或新增测试代码，原证据库仍为 Schema 13。

### 过程草案接合契约

本轮接合的用户目标是：在原批准范围内修改实现过程，保留仍适用的成果，并能追溯每次修改；
不是把原批准方案改写成运行状态，也不是自动批准 Planner 的自述。

| 环节 | 输入与输出、所有者 | 失败处理与接入边界 |
| --- | --- | --- |
| Planner 草案 | 输入原批准方案、冻结 Check 条件、当前过程与运行图、调整原因；复用只读调查和图工具，输出完整可读方案及 retained Gate 草案 | Planner 不生成新契约、不应用修改；缺少定义或条件时不能伪造 Gate |
| 应用编译 | 以当前过程为定义基准分配任务/边身份，生成后继过程候选；新任务 pending，未变任务保留当前结果状态 | 拒绝旧快照、缺失或改变的 Gate/Check 绑定；候选不是已应用的过程版本 |
| 边界审查与发布（入口已接通，正向待验收） | 核对原需求、接口、Gate 作用范围、权限、义务覆盖和证据，再由应用层在同一事务发布 | 过期图或证据不得复用；越过批准范围转明确修订，缺少证据请求补充，不把缺证直接称为越权 |

内部已接入 `BuiltinPlannerAdapter.propose_process`，复用现有 Agent Runtime 和只读 workspace
工具；过程模式从当前图 seed，保留原 Check ID，提供整组 Gate 搬迁，不生成新 Check 或契约。
`build_process_proposal` 使用单调任务身份分配传播定义/输入变更；分支选择失效时，旧 PRUNED
且需重新执行的节点获得新身份并从 pending 开始，未变已完成候选不因此全部重跑。改变图时必须
提供完整可读设计。以上草案不直接持久化为已应用版本，也不自行建立语义/授权证明。

同时修复普通 Work/Reviewer 直接依赖 Evaluator 时丢失选中代码输入的问题：应用层按每个
BranchSelected 事件筛选当前选中 Artifact，代码准备只取这些 Artifact 来源节点的最新成功
Attempt，并核对宿主 snapshot 的 Run/Attempt 身份。不能因多个选择合并校验而接受另一条分支的
产物，也不要求每个分支成员都提供一个选中 Artifact。既有 Merge 的整条选中路线代码准备语义
本轮保留；这不代表已完成通用的 Artifact 子集到代码变更范围的语义证明。

正常入口验收须由用户的运行触发过程草案，查看其修改和证据，经边界检查后继续同一 Run；应保留
原批准查询、未变成果和旧轨迹，且新 Attempt 引用应用后的过程。生成、查询、审查和受控应用已
公开；静态检查、拒绝路径或直接内部调用均不替代完整产品路径。

本轮结构复核未发现任务身份固定点或选择签名的新反例。候选产物版本不属于纯图签名；目前由
存储发布和运行输入校验明确拒绝过期选择，自动重评/补充草案仍需在后续应用流程接合，不能把
拒绝过期输入说成已实现自动恢复。验证为 Ruff、113 个文件格式检查、88 个 Python 源文件类型
检查、TypeScript 客户端生成/类型检查/构建和 CLI 加载；未应用后继过程、未运行真实产品 E2E。

### 持久过程草案与公开查询

Schema 15 新增独立 `process_drafts`，候选完整图只嵌在草案快照，不预先登记为已应用过程或
节点/边/分支实体。草案请求保存父过程、精确当前图、原因及预分配 Planner Session 关联 ID；
结果只能从 planning 变为 ready/failed，完成后不可改写。同一父过程允许多个草案，拒绝的草案
不占 `(run_id, process version)`。旧草案在 Run 已推进或终止后仍可保存生成结果，用于历史查询。

`propose-process` / POST `/api/v1/commands/propose-process` 在模型调用前持久化草案和幂等回执，
重放原请求仅返回原草案；失败结果也可查询，未知结果不被同键自动重试。命令返回 draft_id/status，
`get-process-draft` 与 `get-run-process-drafts` 及对应 GET API 返回完整草案。`base_is_current`
仅反映父过程/实时图/Run 状态是否仍匹配，不是应用许可。ready 仅表示生成完成，后续仍需边界审查。

通过正常 CLI 在 Schema 14 证据库的新副本上升级：原有 32 张表的全部行不变，空草案表、integrity
和外键检查正常。随后以无模型的 single/fake 示例 Run 检查不支持过程生成时的失败路径：
草案 `bdf47793-f31e-476c-95a5-2362999ac35f` 持久 failed，重复 CLI 和 HTTP POST 返回同一 ID；
仅一次 started/failed 事件，Run 只有初始过程 v1，Builtin 模型 Session 数量为零。正常 CLI/HTTP
单条与列表查询一致；取消示例后原草案不变、base_is_current 为 false。临时 API 宿主已退出，
未遗留运行示例。证据库位于系统临时目录 `ehai-process-drafts-66291c33e9264ab283e7a9b985938ab8`；
原 Schema 14 证据库未修改。

以上验证的是迁移、失败保留、幂等和公开查询，不证明真实模型成功生成、边界审查、过程应用或
产品 E2E。后续审查和应用的实现见下一节，自动调整调度与完整正向验收仍待接合。
本次接合后 Ruff、114 个文件格式检查、89 个 Python 源文件类型检查及客户端生成/类型检查/构建通过；
没有新增常驻测试或调用真实产品模型。

### 独立边界审查与受控应用

`ProcessReviewContext` 从原批准方案/契约、冻结 Check、精确草案基准、已有 Run 授权事件与
复用节点的最新成功 Attempt 加载输入；仅引用该 Attempt 的 Artifact/CheckRun。缺失的执行
授权快照或成果不从当前 CLI 默认配置补造。Reviewer 在独立 Session 中使用配置的 Planner 模型，
复用现有 Built-in Agent Runtime，只能读 workspace/保留 Artifact、提交审查报告，不能编辑图或应用。
Artifact 按页读取前核对长度和 SHA-256，报告不能引用未读证据；尾部空页不能伪装为已读，尾页
不会被标成整份 Artifact。缺文件允许报告不确定，可变 checkout 不充当历史 Attempt 版本。

报告覆盖 requirements/interfaces/permissions/gate_scope/result_reuse 五项；`preserved` 是模型
语义判断，不是用户新增批准。原材料精确引文只定位依据，义务解释与完整性仍须由语义审查判断。
保持范围的报告须解释全部候选任务、提供原义务到实现组及 Gate scope 的映射，并引用每个复用
Work/Merge 的成功成果。拒绝或不确定可以没有可通过的映射，不强迫 Reviewer 伪造“通过”。

结构核新增 `analyze_node_preconditions`，复用原图分析的 AND/OR、失败和有效选择语义，以节点
ID 作为完成标记；原 Gate 分析的 exact-owner 契约不变。对每条到达 Gate 检查的最小路线，scope
中每个义务必须至少有一组实现节点已完成；组内 AND、组间 OR，组必须非空且含 Work/Merge。
Work/Merge owner 自己在检查前产生的候选也算实现，Reviewer 不能单靠报告替代实现。不会反向
要求每个备用实现组出现在最小路线中，因为 antichain 会丢弃超集；不冻结全部旧任务或分支 ID。

`review-process` 先保存 `ProcessReviewStarted` 和幂等回执，再进行模型调用，最终保存 completed
报告或 failed 诊断；同键不重试未知调用。`get-process-review` / `get-process-draft-reviews` 暴露
历史。`apply-process` 只引用一个已完成且保持范围的审查，再次核对当前基准、原材料、成果引用和
结构映射；Run 必须 paused，在途 Attempt/未决工作不得被丢下。过程快照、活动指针、
`ProcessRevisionApplied` 和回执同事务写入，Run 仍 paused；随后按原运行配置继续。持久状态冲突
通过公共 `StateConflictError` 映射为 CLI 诊断及 HTTP 409，不泄漏为未处理异常。

对应 CLI/API/Client 已接通。正常无模型检查在上一证据库的新副本
`ehai-process-review-baf408749a4747d6abb792cd4a53e297` 进行：失败草案不能审查，无有效审查不能
应用；CLI 两项均退出 2，HTTP 分别返回 422/404，草案审查列表为空。拒绝前后全部表内容相同，
integrity 正常；临时 API 宿主和监听已退出。Ruff、119 个文件格式、94 个 Python 源文件类型检查
及 Client 生成/类型检查/构建通过。没有调用真实产品模型，也没有新增常驻测试。

仍需验证真实 Reviewer 的语义判断、正向过程应用、应用后继续/恢复和完整编码结果；自动触发与
重评调度、阻塞请求的明确接手以及跨批准修订的采用也尚未完成。上述入口不表示总目标或 E2E 通过。

### 2026-09-13 真实 Planner 调用与 strict Schema 修复

用户已授权真实模型调用。通过正常 CLI 在仓库外隔离项目使用 `gpt-6-astra/high`，沿用本机端点
及凭证引用，不发送 EHAI 源码。首次 `discuss-plan` 返回 HTTP 400 `upstream_error`。
对照调用表明同参数的 `ask_user` 工具可完成，完整工具集失败；本地新增的节点能力/Session 策略
及人工 Gate 字段未全部进入 `required`，与 `strict: true` 不一致。
按官方严格模式规则修为字段必填、以 `null` 表示未指定；更新节点时 null 保留原值，空能力数组
仍表示显式清空。未禁用 strict，也未调整模型或把通用 400 自动重试为成功。

修复后回到同一个真实讨论，以新消息键通过正常 CLI 重试：两个 HTTP 请求均 200 且明确 completed，
先执行 `workspace_read(README.md)`，再执行 `ask_user` 返回行为澄清问题；讨论 turn 为 completed。
讨论 ID `512ea027-908f-4986-8ec4-4af3840f43b7`；成功 Session
`01213540-5fd8-4cb0-ad81-c67a9f8416fc`。数据库 integrity 为 ok，Run 数量为零，没有批准或执行代码。
证据及两项通过的临时诊断位于系统临时目录 `ehai-authorized-call-c5161e0fa0e144b98f188f55dba55b51`；
其中 diagnostic-capture 消息是发送前截取请求的定位记录，不是模型验收。没有常驻测试新增。
Ruff、格式检查和 mypy 通过。这只证明本次 Planner 读文件/工具交互和本地修复，
不证明 Phase/Gate 正向执行、过程应用/恢复、跨批准接续或唯一产品 E2E 完成。

### 2026-09-14 节点目标边界：基础接入，行为验收未通过

用户要求用 Planner 执行图约束 Worker 目标，遇到需扩大目标的工作时挂起；
先不建设范围内空转检测、逐步模型裁判或额外细节约束。输入来自派发的 WorkerRequest，
输出是正常候选或现有 WorkerBlocker；Planner 拥有节点职责，宿主拥有图版本、授权和状态。

本轮实现：

- Pi Planner 提示明确节点交付物、可自主细化范围、排除项与需报告的前置缺口。
- Pi Worker 上下文增加宿主生成的 execution_scope，绑定 Run/批准方案/过程版本/节点，
  携带原节点 instruction、依赖、Check 与能力引用。顶层指令引用该目标，避免重复全文。
- 所有 Pi Worker 角色，包括自定义 system_prompt，附加目标范围策略；正常调试允许，
  不阻塞交付的旁支问题记录后继续。必需扩大职责或批准边界时，先保留适用 handoff，再 report_blocked。
- 复用 report_blocked 的原 reason/evidence/needed 参数、结束语义与介入持久化；
  宿主仍走 block_attempt，将 Attempt 中断、节点 blocked。没有新增 suspend 状态或改审批权限。
  无任何新的 Schema 强约束、匹配关键字拦截、接口兼容开关、客户端类型或数据库迁移。

正常入口验证在仓库外 ehai-scope-trial-20260914 中进行。CLI create-project/create-goal、
显式 single Planner、approve-plan 和 execute-plan --authorize 构成真实生产装配路径；
single 是现有无模型演示规划器，不是 Pi Planner 生成图的验收。独立临时 Git 仓库仅有 README，
记载 parse_text 已存在而 parse_record 尚未实现。节点要求只根据该输入整理既有 parse_record
接口、不得实现或发明接口、不得用其他接口代替。配置仅 Luna/high，capacity=1，无 Shell/命令，
没有开启自动过程调整。artifact:non-empty 仅为演示规划器既有检查，不是合格产品验收标准。

1. Run 0d2f647a-d1fc-406f-844b-546cd965fa62，Session cb9ed8df-082d-4659-9563-7debccdd9c19：
   Worker list/read 后提交“接口不存在”的说明，没有 report_blocked；旧非空检查给出 completed。
   用量 5362 tokens。没有扩大接口或写入代码，但未达到本次预期的挂起行为。
2. 补充“缺少必需输入时不得用无法交付说明代替交付，除非目标允许缺口报告”后，
   相同目标/输入/配置通过正常入口创建新的隔离 Goal/Plan/Run，保留第一轮记录。
   Run 350c4a45-1c39-4e12-88d2-e4fb697505e9，Session 53a3046d-90b8-464f-93d5-92615d5efcd7：
   仍提交缺口说明并由非空检查给出 completed；用量 5477 tokens。原生消息确认 execution_scope
   已传入、包含正确的派发节点与过程 ID，不是上下文丢失导致。该场景涉及“缺口说明是否满足
   文档任务”的语义判断，不能推广为任意越界均可自动识别或禁止。

合计 10839 tokens，均为 Luna，静态 Ruff/格式/mypy 通过，未新增常驻测试。
本轮没有继续扩大模型试跑，也没有将这两个 completed 记录当成功能验收成功。
未验证实际 report_blocked/节点挂起、独立分支继续、回复恢复或成果 handoff；未运行唯一产品 E2E。
当前结论是：基础上下文/提示接入已存在，但仅靠 Worker 自报不足以通过本次挂起行为验收。
后续需与用户明确是接受尽力而为的范围提示，还是把范围判定纳入已有 Reviewer/Gate；
不擅自增加每步 LLM 审核或把弱提示包装为确定性执行边界。

### 2026-09-13 工具接合与交付要求

本次 400 暴露的不是“端点偶尔不稳定”，而是本地工具变更没有同时满足模型调用协议：
Python 类型检查和通用 JSON Schema 检查可通过，Provider 仍会拒绝整个工具集。
因此工具交付必须覆盖调用契约及实际行为；此要求也适用于 Worker、Reviewer 和过程 Planner，
不只针对本次的节点与 Gate 工具。

1. 修改前沿实际调用方核对 ToolSet 注册、角色权限、发往 Provider 的 Schema、提示与参数示例、
   Handler 校验、返回结果，以及受影响的持久化和下游消费。相关层同步修改，不另造一套工具状态机。
2. 通用 Schema 合法不等于满足 Provider 的严格模式。核对严格对象的字段约束与嵌套结构；
   明确未指定、`null`、空列表、默认值和更新时保留/清空的区别，不能只改 Schema 而不改执行语义。
   本次具体修复是严格字段必填、用 null 表达未指定，未禁用 strict；详见上一节。
3. 交付前在既有用户授权内，通过正常 CLI/API、生产装配和隔离 workspace 验证受影响路径。
   分别确认工具定义被接受、模型实际调用了目标工具、宿主正确执行，以及该工具应产生的持久结果
   或下游行为。只读工具核对读取内容；变更工具核对实际状态；等待/交接工具不能只凭返回 accepted。
   不要求为每次改动重跑无关路径，不以另一工具成功或 HTTP 200 宣布本工具已验收。
4. 调用失败优先排查本地配置、实际序列化请求、Schema 和响应解析；必要时缩小请求对照。
   网络、认证和上游问题仍以证据判定，不预先断言一定是本地或端点；不靠切模型、降低校验或
   重发结果未知的调用掩盖失败。诊断只在真实失败后放仓库外，修复后回到原正常入口复测。
5. 实施记录写清故障、根因、修复及证据覆盖范围。明确区分“静态通过”“定义已接受”“工具已执行”
   和“产品行为已验收”；未完成的层次如实标注，不把一次工具试用升级为产品 E2E。

这是一项开发与交付规则，不新增产品运行时 Gate、常驻测试套件、测试矩阵或额外模型审批流程。
用户已给出的模型调用授权继续有效；仅当调用范围超出原授权时另行确认。

### 2026-09-13 阶段 Reviewer 真实执行、证据输入修复与恢复

在 `codex/p2Finalize` 工作树通过正常 CLI 使用隔离示例仓库，Planner 为显示名称规范化需求生成
两个独立 Work（代码与文档）及一个共同 Phase Reviewer；未手工拼装执行图或预写实现。
Planner 使用 `gpt-6-astra/high`，Built-in Worker 使用 `gpt-5.6-luna/max`、capacity 2；
没有开放通用 Shell、网络工具或额外依赖。审查生成的方案和实际 Check argv 后批准并执行。

真实运行暴露 Built-in `_builtin_context` 丢弃 `WorkerRequest.artifact_inputs`：Reviewer 能读整合
文件，却没有报告必须引用的依赖 Artifact ID，因此反复向已完成会话请求证据。另发现两种 Worker
协议的检查摘要均缺少实际命令/语义参数，不能据此审查 Gate 内容。
修复将已经校验和限定范围的输入快照加入模型上下文，保留标识与内容，并补齐冻结 Check 参数；
Reviewer 提示明确从宿主输入取证，不关闭审查校验、不改变批准标准或赋予命令权限。
基于原持久 Reviewer 请求的仓库外临时诊断通过；未创建常驻测试。

中止旧前台进程后，运行状态仍为 running；这不是成功的有序暂停证据。正常 `resume-session`
恢复将旧 Reviewer 中断并保存 external_effects 便签：该执行使用了代码快照以外的 session_send。
核对完整工具轨迹、SQLite mailbox、只读工作区及进程退出后，确认只有两条本地 Artifact-ID 请求，
无外部服务操作或文件写入。以 `codex-isolated-trial-operator` 身份通过正常便签回复记录事实，
不重发旧消息、不代判人工 Gate；再恢复同一 Run，仅创建新的 Reviewer Attempt。

- Run `abe26bed-bd54-4e12-9e56-51171fdeb38c` 最终 completed；两个原 Work Attempt 各执行一次，
  共四个 Attempt（三个成功、一个历史中断），真实运行达到两个并发 Work。
- 所有成员加入同一逻辑 Phase Session `9f02e838-a57e-4364-89ff-9aba7e1a69a0`，文档 Worker
  实际发布共同上下文，Reviewer 使用阶段读取工具。未据此证明跨阶段或 Server 会话能力。
- 新 Reviewer 只调用读取和候选提交工具；读取两个 Artifact 后提交合法 `review.json`，
  引用四个宿主输入 ID，明确区分静态审查建议与尚未运行的宿主 Gate。准备基线到最终提交无代码差异。
- 原 Check `e0d8d5d9-0c7d-4f1a-8058-324f2f0fd789` 在真实整合代码上执行，退出 0、passed；
  覆盖 Unicode 空白、非空白字符保留、输入类型和空输入行为，产生一个 Checkpoint。
- 最终成果 commit `817d7eb6d83e7c57224d04ea230c6e1b0fc6fee3` 包含两个 Work 的 Git 血缘，
  代码与检查同属成功 Reviewer Attempt `bfa7dc7e-9fbb-4944-a5b3-33b37bfec811`。
  原示例 HEAD `cd2cbe09ddcc14a3df0a1d210939f9e6fbd6d8f9` 及原工作区未改变；最终差异仅两份目标文件。
  Gate 导入产生的未跟踪 `__pycache__` 留在隔离执行目录，不是交付提交。

证据位于系统临时目录 `ehai-phase-live-bf7c28fb864a4f9b866e0b5fa380b274`，包括数据库、Artifact、
隔离 worktree 和临时诊断。数据库 integrity/FK 检查正常，无残留本次前台执行进程。
Python 静态检查与 Client 生成/类型检查/构建通过。一次诊断管道中的乱码定位为接收端临时 Python
默认 GBK；使用 `uv run python -X utf8` 后与数据库一致，没有因此修改产品数据或输出代码。
此记录证明本例的阶段规划、并行交付、Reviewer、自动 Gate 及上述故障接续，不证明拒绝返工、
必需人工 Gate、独立 handoff、中间过程调整、跨批准 Run 接手或唯一产品 E2E 已验收。

### 2026-09-13 批准内过程调整的真实正向链路

在系统临时目录 `ehai-process-live-a4de9ee147ed45f1b897a6c273e483ac` 使用正常 CLI 和真实模型，
从独立示例仓库提出、审查并批准方案后运行。Planner 使用 `gpt-6-astra/high`，Worker 使用
`gpt-5.6-luna/max`、capacity 2，无通用 Shell 或网络工具；不是最终产品 E2E。
代码 Work 完成后，在文档 Work 尚未完成时通过交互终端 Ctrl+C 停止。宿主明确返回
`cli_interrupted`，Run paused，代码成果保留，文档 Attempt interrupted；没有用数据库改状态。

通过 `propose-process` 将剩余 README 文档组织方式细化为 valid-input/rejected-input 章节和
原有 Python 示例，原要求、文件边界、Phase、逻辑 Gate、Check argv 和权限不变。
第一草案的图正确更换了文档和受影响 Reviewer 的执行 ID，但可读设计误称全部 UUID/边不变；
初审也漏过该矛盾。主 Agent 未应用它，修正 Planner/Reviewer 提示：种子 UUID 是建图引用，
不是保留执行身份的承诺；区分原逻辑 Gate 与编译后的 owner，并检查说明与实际图是否一致。
同一错误草案复审返回 gate_scope uncertain，未覆盖初审历史；新草案说明与图一致，独立审查通过。
报告中不成立的义务映射也曾被 finish 工具拒绝并由模型修正，结构校验未放宽。

关键事实与证据：

- Run `e7f02173-68fc-415b-97e5-1b4954a73865`；原批准方案
  `932b4c14-6c96-432e-91be-2cc6da7b9b06`，契约 `7019f2ce-7fe0-406b-9869-08f429358830`。
- 初始过程 `92854ad5-ff40-4802-a520-e5afba0d988a`。错误草案
  `5c33cb0f-9735-4731-8dd4-c7c2698a049e` 未应用；其修复提示后的复审
  `62a708cf-7538-473b-83f5-e6b17f0598e6` 明确指出说明与执行身份矛盾。
- 新草案 `9fdfeea4-3dda-4bf7-b1a1-9afd7df91c69` 的独立审查
  `ef1b2f65-6692-4d15-bfc2-1f180cdfb3d1` 五项均 preserved，完整读取并核对了三个保留代码 Artifact。
  正常 `apply-process` 发布过程 v2 `a3a45462-06e8-4d1a-ac3c-5b6a5c8a9848`，Run 仍 paused。
  重放相同应用键只返回原结果，整条轨迹仅一个 `ProcessRevisionApplied`。
- `resume-session` 继续原 Run，最终 completed。原代码 Attempt
  `cb4e4138-90b2-424e-87d6-6e6cc2853ac7` 只执行一次，保留其过程 v1 归属；旧文档中断保留。
  新文档和最终 Reviewer 属于过程 v2，总共四个 Attempt（三成功、一历史中断）。
- 全程仅一个逻辑 Phase Session `1d168f00-58a8-42ec-b966-21f9b7a33b6e`，四次成员加入均引用它；
  过程改变没有重建该逻辑会话。未改变原 Phase 或批准方案，`get-plan` 仍保留原节点及批准时状态。
- 原 Check `8c971849-5af7-48ba-924c-b0c9a062a618` 在新 Reviewer
  `e8da97bd-ba10-43bc-8773-8da8a756ea55` 的整合成果上通过、退出 0；Checkpoint 绑定过程 v2。
  最终成果 commit `1d2ff94752a7286ac6d07b14927bcb64f42fedc4` 包含保留代码的 Git 血缘，
  display_name.py 的 Git blob 与原完成代码完全一致（不同 checkout 的换行转换不作为代码变更）。
  README 原始文本前缀保留，并新增目标参考章节；最终差异只有两份目标文件。
- 数据库 integrity/FK 检查正常，已应用过程只有 v1/v2，原示例 workspace 未改动，执行进程已退出。

并行代码审查还修复了证据读取工具漏接 `ArtifactIntegrityError` 的异常路径：转成可恢复的
artifact_integrity 错误，不计入已读证据，允许报告不确定而不关闭完整性校验。文本 Schema 描述
明确了宿主的非空白/UTF-8 字节上限；未放宽限制。该异常分支是代码审查修复，本轮未声称损坏证据
场景已通过真实模型验收。Python 静态检查通过，没有新增常驻测试或为了该分支制造测试矩阵。

本例证明显式调用的“执行→正常暂停→过程草案→独立审查→应用→同 Run 继续→原 Gate 完成”链路，
不代表自动触发策略、人工 Gate/拒绝返工、独立 handoff、Server 交接、跨批准后继 Run 或自举 E2E 已完成。

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

本轮共识的 Worker 实例映射、动态过程调整、阶段/人工 Gate、阶段 Session、handoff 与安全回退
仍须完整验收，具体实现进展以上方最新小节为准。此前普通 CLI 编码和恢复通过不证明所有新目标完成。

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
