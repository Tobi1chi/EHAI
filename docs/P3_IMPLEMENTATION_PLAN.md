# P3 实施计划

## P3.1：项目总览与执行归属

本轮目标：从正常 CLI/HTTP/MCP 入口发现项目，进入目标、计划、Run 和当前有效成果，
无需事先保存全部 Run ID。只做必要静态检查和一次正常路径薄验证，不新增永久测试。

### 契约与所有权

- `list-projects`：当前数据库的项目摘要，包含目标/Run 数量、读取时间和事件位置。
- `get-project --project-id`：项目详情，按目标列出计划版本、Run、当前过程、节点进展，
  以及当前有效完成节点的 Gate 证据和成果元数据。保留成果实际来源，不改写跨 Run 接续身份。
- `get-runtime-context`：当前 HTTP 宿主的工作区、Worker Endpoint、执行配置指纹和调度健康。
  这是当前进程信息，不能用历史数据库中的 Endpoint 状态代替在线状态。
- Run 的工作区来自其持久授权事实；缺失时返回 null，不用宿主当前目录或成果 worktree 补全。
- 查询由 Python application 层在同一只读快照内投影，入口只做传输；Schema 与 TS Client 同步。

### 本轮多仓库边界

沿用现有单宿主工作区装配。多个仓库使用各自的宿主、数据库、产物目录和 API 地址；
CLI/MCP 的 API 地址就是操作路由，不能把一个数据库同时交给不同工作区的执行宿主。
Project 是数据库内的业务归属，不新增暗含授权的项目路径字段，也不把 Project ID 当全局路由。
跨宿主调用方必须保留来源 API 地址与项目/Run ID 的组合。后续页面可组合这些公开查询，
本轮不实现多宿主发现服务或让单宿主动态切换项目工作区。

### 失败与验收

不存在的项目明确报错；空项目返回空列表。读取时间/事件位置只表示快照新鲜度，
不表示调度器在线或 Run 已完成；连接失败沿用公开错误，不退回别的数据库。
成果列表只呈现当前图中已完成、分支已选定且有对应 Gate 证据的成果，历史轨迹仍走原详情入口。
成果元数据不是重新验证磁盘字节或 Git 可整合性的保证，实际整合仍走 integrate-run。

薄验证通过正常入口建立或读取两个项目，核对目标、Run、成果和来源；验证 CLI/API 查询一致，
MCP 正常协议查询可用。优先无模型路径，不扩大真实模型试验；真实模型协议验证范围单独标注。
本轮结果与实际限制写入 STATUS，完成后继续 P3.2 统一待办和 P3.3 薄 Web 页面。

## 2026-09-21 实施结果

三个查询已贯通 application、HTTP、CLI/MCP、Schema 和生成 TS Client。
公开文档为项目列表/详情的快照，不新增项目生命周期、批准或写操作。
节点成果根据当前执行图、分支选择、最新生产者和持久 Gate Checkpoint 筛选，
接续成果保留 adoption 和 Artifact 原始来源；不扫描模型会话或读取成果字节。

薄验证目录：`C:/Users/28262/AppData/Local/Temp/ehai-p31-cc780995d17d44a0a1fa56b27c576b8d`。
正式 API 启动两个隔离 fake/single 宿主，工作区与 Endpoint 不同；在第一个宿主中创建
两个项目并经 propose/approve/start 正常入口执行到 completed，均获得 Gate 成果。
`list-projects` 返回各自一个目标和一个 Run，详情中的成果归属对应 Run。
CLI 本地、HTTP CLI 与 API 返回的目标/Run 数据一致，公开响应 Schema 校验通过；
第二宿主列表为空，读取第一宿主项目返回 404。真实 stdio MCP 发现 48 个工具，
调用 get_project 返回对应项目的 Gate 成果。临时验证脚本留在仓库外，不作为永久测试。

验证客户端起初受 Windows 系统代理影响无法直连回环宿主；仅在临时验证进程设置
NO_PROXY=127.0.0.1,localhost 后原路径通过，未修改系统代理或生产模型配置。

使用 SQLite backup 复制既有 Pi 试用数据，原库不改动、不恢复执行。
正常 CLI 查询显示两个历史 Run 的原授权工作区；completed 后继 Run
`8091b681-f831-43c3-827d-0271237a0b7c` 返回一个带 adoption 的 Gate 成果，
保留源 Artifact 的 Run/Attempt 身份。记录见验证目录 `retained-summary.json`。

Ruff、格式、mypy、Schema/Client 生成、TS typecheck/build 通过。
本轮 0 模型调用，未新增永久测试；MCP 的真实模型调用、多仓库真实 Worker 执行、
长任务恢复和唯一产品 E2E 不在本轮证据范围。所有临时宿主已退出。

## P3.2：统一人工待办

以下描述最初实现的聚合入口。2026-09-22 后续便签增量及 P3.4/P3.5 的实现和证据见文末。

用户结果：在同一入口发现项目问题，查看证据和具体动作，处理后确认真实状态，其他项目继续。
输入为持久 Intervention/HumanCheck 及本宿主运行期 Worker 请求；输出为只读列表和详情。
不新增待办数据库或通用 complete 命令。处理仍由各源对象负责授权、版本检查、持久化与推进。

- `GET /api/v1/inbox`：按项目/Run 可选筛选，归属不一致拒绝；只列当前未处置事项。
- `GET /api/v1/inbox/{kind}/{request_id}`：查询问题、证据、源状态、可用动作或历史处置。
- 读取返回持久快照时间/事件位置；Worker 来源单独标示可用性、观察时间及不可读 Attempt。
- 人工请求保留原 token；后继批准明确处置的旧问题从列表移除，详情保留原状态和处置依据。
- Worker 表单仅暴露所需上下文、问题/选项和 resolution Schema；原处理接口不变。
  当前进程 ID 绑定 Attempt、Provider 请求 ID 和内容指纹，处理前重新读取源请求。
- Worker 回答结果未知时禁用重复提交；幂等键按请求及回答内容核对。回执仍只在进程内，
  不宣称跨重启的恰好一次执行。页面据源动作响应再查询，不乐观改写 Run 成功或恢复状态。

### 2026-09-22 实施与薄验证

上述查询已贯通 CLI（本地/API）、HTTP、MCP、公开 Schema 和 TS Client。
MCP 的列表筛选为必填 nullable UUID（null 表示不筛选），kind 为固定枚举；
参数对象闭合，不新增 uniqueItems。处理保留原源工具，不新增隐式批准动作。

验证目录：`C:/Users/28262/AppData/Local/Temp/ehai-p32-6edc7393b5d343f399b2303061a9f718`。
首次试用使用含 Reviewer 的外部计划，fake Worker 不提供 review.json，Run 正确 failed，
尚未进入人工 Gate；该限制不是 Inbox 故障，不修改 Reviewer 验收要求。
随后使用已有 single Planner 的 `human:<question>` 正常入口完成同一人工决定场景：

1. 项目 A2（5149b4c9-a0b0-4669-8dcd-65ee30b1fbd5）等待人工 Gate，列表返回原因、证据及判定动作。
2. 项目 B（f88634f2-1614-47da-80bf-89ab31d7dd10）在 A2 等待时独立 completed。
3. CLI/API 项目与 Run 筛选一致；错配筛选返回 422。公开列表/详情响应 Schema 校验通过。
4. 真实 stdio MCP 发现 50 个工具，分别调用 list_inbox 和 get_inbox_item，返回同一人工请求。
5. 经原 CLI decide-human-check 提交 token 和通过决定，A2 completed；列表清空，原请求详情保留且无可执行动作，B 不受影响。

对 P3.1 留存的 Pi 数据副本执行正常本地查询：旧请求
684cb086-dd93-40f4-abed-fb7541e6351e 显示新批准的 superseded 处置、操作者与原因，
不再列为当前待办；本地模式明确 Worker 来源 unavailable。原试用库未改动或恢复运行。

Worker 表单字段依据本机 `codex app-server generate-json-schema` 生成的公开协议核对，
源数据保留于仓库外 `Temp/ehai-p32-protocol`，未启动模型或修改 Provider 设置。
静态 Ruff/格式/mypy、Schema/Client 生成和 TS typecheck/build 通过；没有新增永久测试。
本轮 0 模型调用，人工 Gate 为实际闭环证据；干预回复和 Worker 运行期回答未做本轮真实后端闭环，
强杀/断线恢复、跨重启 Worker 回执、唯一产品 E2E 亦不在本轮验证范围。临时宿主已退出。

## P3.2 后续：便签后端范围

2026-09-22 用户明确希望先完成后端，后续单独打磨页面，避免业务实现和视觉设计耦合。
便签的创建、讨论、决定、失效检查和后续执行均由核心负责；P3.3 只提供相应交互页面。
后端按下列三个小闭环推进，每轮明确实际接口再更新 Usage，不预先编造已实现命令。

| 增量 | 输入/输出与所有权 | 失败与正常入口完成条件 |
| --- | --- | --- |
| 便签讨论与持久记录 | 关联目标/计划/Run 的问题、证据和用户/Agent 消息 → 可查询的讨论及当前待决事项；核心保存消息与来源 | 重复消息不重复落库；历史可重读；多轮澄清不自动触发 reply-intervention 或解除阻塞 |
| Planner 主动便签 | 调查或执行中发现的目标差距、不可行路线与取舍 → 关联相应版本的便签，进入统一待办 | Planner 只能请求决定，不能伪造用户批准；受影响路径按核心规则等待，独立路径继续；正常 Planner 装配能产生并查询便签 |
| 决定与执行/修订接续 | 用户对当前问题的明确决定 → 原批准内继续、过程调整提案，或需要重新批准的方案修订 | 复用已有过程审查、批准、后继 Run 与成果接续机制；旧 token/过期决定拒绝，未知结果先查询；经正常入口核对决定确已作用到对应任务 |

设计时先明确便签与现有 Intervention、HumanCheck、规划讨论的关系：复用现有事实和执行控制，
避免复制批准和 Run 状态机；不能强行把一般讨论都塞入“一次回复即解阻”的现有命令。
讨论消息与最终决定是不同动作；改变需求、接口、Gate 或权限时，必须沿明确的新批准路径。
“多轮讨论可读取”与“讨论结论已经改变计划/恢复执行”分别提供证据。

后端交付包括：核心应用服务、持久化、公开 Schema、HTTP、CLI/MCP、生成 TS Client 和使用说明。
通过正常入口独立使用，不依赖浏览器、页面状态或前端隐藏编排；后端不规定卡片布局、颜色与动效。
验证延续已约定的薄验证方式：必要静态检查，每轮正常路径核对；真实失败才在仓库外做临时诊断，
不新增永久测试矩阵，真实模型调用仍按当轮授权范围进行。

便签后端完成后，依次独立推进 P3.4 事件消费与 P3.5 配置后端，再集中进行 P3.3 页面设计和实现。
本段保留范围定义；本轮实现和验证结果如下，不用目标定义代替运行证据。

## 2026-09-22：P3.2 / P3.4 / P3.5 后端交付

按用户授权使用三个 Astra medium 子 Agent 并行实现，根任务负责生产装配、CLI/MCP、
Schema/生成 Client 和文档整合。前端未开始，未新增永久测试，目标系统模型调用为 0。

### 实现

- 便签基于现有 Event/UOW/receipt 持久化，创建、消息、明确决定分开；消息更新 token。
  决定 intent 使用固定下游幂等键，保存源操作或实际提案/讨论结果；未知结果不重放。
  继续操作只绑定原 Intervention/HumanCheck；过程提案必须 ready，修订讨论可生成待审草稿。
  Pi Planner 的 raise_note 固定 planner 身份、闭合 question/evidence 参数，结束规划轮次；
  discuss-plan 能返回待决问题，直接 propose-plan 没有方案时保留便签并返回无方案错误。
- 持久 consumer 每次只有一个未确认批次，含起止位置及 batch token；同 consumer 的重复读取
  补领相同批次，明确确认后推进，旧确认返回原回执而不回退当前位置。消费不执行业务动作。
- 项目配置以版本完整替换，校验宿主工作区/执行指纹/实际角色引用；新 Run 在事务内保存
  RunConfigurationCaptured，Worker 从该版本取规则，已运行任务不随新配置改变。
  初始 Planner 读当前规则，来源 Run 的过程/修订 Planner 读其旧快照。Pi 私有设置/凭证仍在宿主。
  只按现有宿主规则单独处理 process_adjustment，不放宽模型或其他授权字段匹配。
- 新 SQL migration 17/18 保存消费批次/回执及项目配置版本；旧 Run 无配置快照时明确返回 null。
  所有服务经 create_local_app/build_service 装配，新入口贯通 HTTP、CLI API 客户端、MCP 和 TS Client。

### 薄验证与边界

便签证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-notes-5564dcbe10114d0ebfb48a90205582a1`。
正式 fake/single HTTP 宿主，Run `ffc18160-a1cd-48da-83d3-a1cdc3878237` 等待人工 Gate，
便签 `fb4f25b6-b6c9-45fd-af8e-f2f5bc6026a0` 的两轮消息只更新讨论/token，Gate 仍 pending。
明确 continue/passed=true 后 Run completed，便签记录实际 Run 结果；重复决定没有再次派发。
运行前便签可创建/列出，关闭后项目 Inbox 不再列待办，NoteResponse Schema 校验通过。
根任务另经 CLI/MCP 串联新项目、配置 v1、便签/消息、明确关闭、事件注册/读取/确认、配置 v2，
共发现 64 个 MCP 工具；实际调用的新查询/写入返回和持久结果一致，记录于 transport-evidence.json。

事件消费证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-p34-224c6abd0bf3443286a5e72705b7a520`。
正常 create-project 产生首事件，重复注册/不同 limit 读取返回同一批次；停止宿主进程再启动后
相同 token/事件仍可补领。确认及重复确认通过，下次为空；新建第二项目后只返回新事件。
确认第二批后重试第一批确认只返回旧回执，当前位置仍在第二批；最终事件只有两次显式项目创建，
消费没有触发执行。证据 requests.json/summary.json，无直接数据库写入。

配置证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-p35-kua81dwz`。
生产 create_local_app + HTTP TestClient 正常入口，两个项目/三次 Run completed；
实际 FakeWorker.calls 中 WorkerRequest 的配置分别为 A/v1、B/v1、A/v2。
A 更新后旧 Run 配置完全相同，版本历史保留；错误工作区和旧版本返回标准 409。
后续局部比较确认 process_adjustment 沿现有宿主语义匹配而模型改变仍拒绝；该比较追加 A/v3，
与先前 HTTP 验证分开记录于 process-policy-comparison.json。配置响应 Schema 校验通过。

Ruff、格式、mypy、Schema/Client 生成、TS typecheck/build 通过。所有临时宿主均已停止。
上述是薄验证，非唯一产品 E2E。Planner 主动便签/修订提案没有做本轮真实模型调用，
不宣称任意崩溃时序的决定恢复或外部业务动作恰好执行一次。运行期 Worker 回答的早先验证边界仍保留。
P3.5 的 role reference 表示当前已装配角色，不是可编辑角色库；多仓库仍使用各自宿主/API 地址。

## 2026-09-22：OpenCode Go 真实便签试用

用户授权沿用留存测试 key 做小规模真实调用，并由 Codex 子 Agent 经正常接口模拟人工。
隔离证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-go-p3-ec2b0c33a9ad4b28a5a69aaec77c218c`。
生产 create_local_app 使用 Pi 0.85.1 / Node 24.19.0 / Go deepseek-v4.1-flash，thinking off；
只读临时 Git 工作区的 note.txt，不修改业务仓库。临时观测代理转发真实响应，不替换工具结果，
最多放行 16 次请求，已报告用量达到 160000 后不再放行新请求；这不是产品 Token 硬限额。

留存测试 key 的最小连接请求首次缺少 x-opencode-session，Provider 返回 400 MissingSessionID；
补齐会话头后返回 200/OK，用量 15 token。Pi 原生 provider-attribution 已支持 Go 会话头，
真实宿主请求确认带有该头，无须修改 EHAI Provider 或放宽工具 Schema。

项目 `4125574b-0263-45be-884d-82797cca5ed2`、Goal `fd354c1a-ec57-48ef-a5bd-b2f5f1063ff8`
经公开 API 创建。Planner 实际调用 workspace_list/workspace_read，随后调用严格闭合的
raise_note(question,evidence)，产生便签 `1b02354e-a69c-4798-87f8-0f0b23eec775`，
origin=planner，进入 Inbox。两个流式请求均返回 200，共报告 11499 token；
序列化模型输入确实包含项目 v1 规则及 RULE_V1_BLUE，不只核对宿主响应字段。
子 Agent 通过 CLI 添加英语澄清消息后，便签仍 open、decision=null、run_id=null，未批准或执行。

后续明确 revise_plan 请求在发送前被工具自动审批拒绝，仅返回 blocked by policy。
没有发送该模型请求，没有换通道绕过；已向用户说明并请求处理审批。
因此本次证据证明 Planner 主动便签与持久讨论，尚不证明便签修订生成草稿、真实 Worker/Reviewer、
人工 Gate 或新旧 Run 配置冻结的完整真实模型链路。这是执行工具的审批阻断，不是 EHAI 机制故障。
调用记录保存在 operator-evidence.json，Provider 参数/工具调用在 wire-summary.json，
完整待提交决定在 human-language-decision.json；均无凭证。没有新增永久测试或提交代码。

模拟操作者还经正常 API/CLI 完成 P3.4/P3.5 无模型部分：consumer
simulated-human-lantern 已确认到 offset=7，pending_batch_token=null；两次消费含真实
项目、规划和便签事件，没有执行任何业务动作。A 配置 v1/v2/v3、B 配置 v1/v2 的历史均可查询；
A v3 恢复最初 v1 的规则内容以保留待提交决定的任务上下文，版本号不回退。
本次尚无 Run，因此不把配置 CRUD 记为新的真实 Worker 快照验证。配置更新当前不写 EventLog，
上述消费证据不包含配置变化通知。git diff --check 通过；临时宿主在无活动模型调用后停止。

### 用户确认后的同一试用接续：Run completed

用户明确允许提交保存的 revise_plan 请求及继续模拟批准/执行/验收后，重启同一宿主，
沿用同一凭证、Pi 配置、数据库和调用计数。MCP get_note/get_project_configuration/
get_event_consumer 查询均通过，已确认消费位置仍为 7（64 个注册工具不等于逐个工具实测）。

1. 保存的决定正常生成计划 `bb289e9a-1169-48b2-b80f-9fd641c692c1`，便签 resolved 并保留
   实际 conversation/plan。Planner 首次设置了重复 node/final Gate，核心拒绝 finish_plan；
   模型通过 remove_node_gate 自纠，未放宽结构验证。最终只有只读 Worker→Reviewer、一个 Phase
   和明确 human final Gate，commands/shells/git permissions 均为空。
2. 模拟操作者核对方案后分别批准、授权并启动 Run `80a7327d-d309-49f6-b2de-702990400b6e`。
   Worker `53862250-a8d3-4828-a790-f4366e849658` succeeded，产生英文报告
   `32944d4f-109c-4f87-b8e9-b875b00af328`，原句与 RULE_V1_BLUE 均存在，代码 patch 为空。
3. 启动后项目更新为 v4/RULE_V2_GREEN，Run 查询快照仍为 v3/RULE_V1_BLUE，更新前后完全相同。
   实际 Worker、首次 Reviewer、恢复后的 Reviewer 请求均携带 v3，不含 RULE_V2_GREEN；
   这补充了真实 Pi 输入证据，不只依据配置查询或 FakeWorker。
4. Reviewer 在已完成第 16 次请求后触达临时代理上限，下一请求在发往 Go 前被本地拒绝。
   EHAI 保留 interrupted Attempt `694f630e-767e-46fd-b769-827fba267b0e` 和 external_effects
   Intervention，未自动重放。操作者暂停 Run；根任务核对代理和完整只读工具轨迹后，明确说明
   将临时次数上限调整为 22，160000 token 响应后停止阈值不变。通过绑定便签
   `33422abc-acc4-4988-8039-0550b51eafd8` 记录本地拒绝、无上游请求及只读证据，再明确 continue。
   返回仍为 paused，独立 resume-run 后才恢复调度；没有改 unknown 分类或其他恢复机制。
5. 新 Reviewer Attempt `5cd07922-eb56-4be9-a78a-cee356cf8ca3` succeeded。
   首次提交遗漏必需代码快照 evidence ID，被核心拒绝；模型补齐后提交有效 review.json
   `2fbeb46c-c06b-4c5c-909c-6c0df50f4e29`，recommended_action=pass。
   模拟操作者核对原句、英文、规则标记和无代码改动后，经绑定 HumanCheck 便签
   `4981ec44-afaa-4874-8e53-3c7c3f3b3cd1` 明确 continue/passed=true。
   Run 最终 completed；事件 consumer 确认至 offset=80，pending_batch_token=null。

共 20 次真实 Pi/Go 请求，全部返回 200，报告用量 152263 token；另有先前连接探测的 15 token。
本地限额拒绝不计作上游请求，Provider token 数不等于账单。临时脚本均在仓库外；
没有新永久测试，没有 EHAI 实现代码修复，没有改变机制或降低 Gate 要求。
实际覆盖便签→草稿→批准→真实执行→证据审查→人工决定，以及一次已查明原因的人工接续，
仍不代表唯一产品 E2E、任意崩溃恢复、所有模型工具或便签 propose_process 路径全部验证。
报告的句子引用与无代码改动已有证据；其中换行符说明不是经模型工具验证的原始字节保证。
完整公开响应、模型请求/结果、最终轨迹分别见 operator-evidence.json、wire-summary.json、
trace-final-root.json。完成后停止临时宿主；静态文档差异检查通过。

## 2026-09-22：多 workspace 管理层与 Planner 并发

用户确认在单工作区核心上增加管理层，复用既有三个子 Agent 分别实现 Planner 容量、子进程
托管及传输/Schema，根任务实现统一网关、汇总、文档与正常入口整合。
用户结果是一个本机 CLI/API/MCP 入口管理多个仓库；各核心仍持有 Run/批准/Gate/恢复事实。
本轮未实现页面，没有增加永久测试，没有新增目标模型调用。

### 实现与契约

- `ehai-manager` 监听本机，管理登记、启动、停止、精确执行配置读取与项目/成果/Inbox 聚合。
  `/workspaces/{workspace_id}/api/v1/...` 只转发已有核心公开路由，保留 HTTP/SSE 和来源，
  不猜测 workspace、不自动切换宿主或重放不确定的写入。离线工作区明确不可用，聚合不是跨库快照。
- 每个 workspace 独立核心进程、SQLite、Artifact、固定 Pi 配置副本与 Worker/Planner 容量。
  登记配置为闭合模型；重复一致请求幂等，变更既有登记拒绝。工作区彼此及与管理器数据目录
  均不能相同或相互包含。管理器锁和子宿主锁防止重复持有；子进程只继承必要系统环境及其
  Pi 配置声明的凭证变量，私有控制 token 不进入公开响应或调用参数。
- 启动等待带私有 token 的身份/健康核对，不自动重启；正常停止拒绝活动写入、执行、
  后台过程工作和仍占用容量的 Planner。管理器退出时有界关闭其子进程，子进程也监视父连接 EOF。
  首版按启动实例配置容量预留总额度，不做动态借用、跨项目公平调度或 Run 跨工作区迁移。
- `planner_capacity` 默认 1，单宿主 CLI 也可设置；所有规划/修订/讨论/过程提案与审查共用
  非阻塞容量。便签模型决定在写 NoteDecisionStarted 前入槽，跨 `asyncio.to_thread` 的嵌套
  调用共享租约；HTTP 取消后仍运行的子调用继续占用槽位。同讨论未决轮次保护保持不变。
  满额返回 409 `planner_capacity_exceeded`，查询为 `GET /api/v1/planning/capacity`。
- 正常客户端贯通：管理 root MCP 提供管理工具及显式 workspace_id 的核心工具；绑定某工作区
  的 MCP 保持单核心参数。生成独立管理 OpenAPI/Schema 与 `EhaiWorkspaceManagerClient`，
  `.workspace(id)` 返回已有业务 Client。httpx 移入运行依赖，新增入口安装由 uv.lock 固定。

### 实际问题与修复

Windows 虚拟环境启动器的 subprocess PID 与实际 Python PID 不同，首次两个子宿主被错误
标记 identity_mismatch。修复为核对私有 token、workspace_id、固定路径和 Runtime healthy；
PID 只作为进程信息，清理仍只针对自己创建的进程树。原正常子进程启动路径重跑通过。
整合审查另补上管理数据与执行目录隔离，以及 HTTP 取消后活跃 Planner 的停止保护；
目录重叠拒绝已经正常管理 API 验证，特殊取消时序仅做实现审查，未宣称崩溃恢复保证。

### 正常入口薄验证

主证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-workspaces-57f4f551c69b453187f588076f4e359b`。
正式管理器及两个实际 fake/single 子进程，均通过相同网关操作：

1. workspace a/b 分别登记并启动，Planner 容量 1/2，各自项目规则与 Run 快照正确。
2. A Run `1a139614-79c0-4d1a-bc60-3be84c323865` 等待人工 Gate 时，
   B Run `716cf5be-7a50-4634-b73e-0dc9c49c1df2` 独立 completed。
   正常 stop A 返回 409；用 B 的 scope 查询 A 的 Run 返回 404。
3. 人工明确验收 A 后 completed；停止 A，聚合返回 A 不可用/B 可用。
   重启 A 后原 Run/成果仍在。最后用最终代码重启管理器及子宿主，两个已完成 Run 仍可读取，
   最新目录隔离约束返回 409，容量查询保持 1/2。
4. CLI 管理/核心查询及带 `/api/v1` 后缀的 scoped URL 通过。
   真实 stdio MCP 管理 root 72 工具、单核心 65 工具，scope 必填/省略语义和请求 Schema 读取通过。
   生成 TS 管理/核心 Client 查询通过。工具数量与协议调用不等于真实 Provider 全工具验证。

原始证据：workflow-evidence.json、workflow-state.json、transport-summary.json、client-summary.json、
final-readback.json。独立子宿主启动/身份/隔离修复证据位于
`C:/Users/28262/AppData/Local/Temp/ehai-workspace-hosts-e3r9ayvm/summary.json`。
Planner 容量证据位于 `Temp/ehai-planner-capacity-3525b01e392048279c953c1b6fca5738/capacity-trial.json`：
正常 HTTP 同时提交三个不同 Goal 得到 201/409/409，拒绝未开始意图；释放后同键提交成功，in_use 归零。

Ruff、格式、mypy、Schema/Client 生成及 TS typecheck/build 通过。全部本轮临时子宿主和管理器已停止。
这验证后端隔离与正式调用闭环，真实多 Pi/多模型并发、强杀/网络断开时序与唯一产品 E2E 未覆盖。
实际使用说明见 [多工作区后端](WORKSPACE_MANAGER.md)。

## 2026-09-23：真实多 Pi 并发与 Windows Git 阻塞修复

用户明确要求实际模型测试，随后将本轮 token 停止阈值放宽到 10M。
沿用留存 Go 测试 key，Pi 0.85.1 / Node 24.19.0 / deepseek-v4.1-flash、thinking off。
两个新临时 Git 仓库分别只含 `ALPHA lantern is blue.` 和 `BETA compass points north.`，
项目规则分别为 RULE_ALPHA_729 与 RULE_BETA_418。配置和凭证变量引用分别固定到对应 workspace；
数据目录在两个仓库之外。所有业务经正式 manager/core API，模拟操作者明确批准及验收。
观测代理只转发实际请求/响应，不替换模型结果、不重试，不记录 Authorization/key。

证据目录：`C:/Users/28262/AppData/Local/Temp/ehai-multi-pi-28de78256f4c47969d13e07d0db51af8`。

### 真实并发与归属

- 两个 workspace Planner 同时读各自 note.txt 并实际调用 raise_note，各自问题/证据和规则正确，
  首请求实际重叠 6.206 秒。相同业务幂等键在两个核心中各归其主。
- workspace B 配置 planner_capacity=2；两个独立 Goal 的 Planner 请求实际重叠 7.705 秒，
  同期容量查询 in_use=2，第三个请求在模型调用前返回 409 planner_capacity_exceeded。
  两个便签均持久正确，显式关闭讨论后 in_use=0，没有产生额外计划或 Run。
- 修复后的同时启动重跑中，两个真实 Worker 首请求重叠 4.912 秒，两个 Reviewer 首请求重叠
  5.482 秒；均首次提交有效候选，人工 Gate 分别判定后 completed，未新增重试或更换模型。
- 共 12 个实际 Provider Session、24 次流式请求，全部 200；每个模型输入只包含本 workspace
  项目配置和标记，没有另一边的标记。并发峰值为 3（同工作区双 Planner 与另一工作区 Worker）。
  共 118857 报告 token，停止阈值为用户指定 10000000；另保留 24 次试用请求上限且没有触发拒绝。
  这是观测试用限制，不是新增产品 Token/费用机制，Provider 用量也不等于账单。

### 首次运行发现的问题、定位与原路径重试

首次并发 start 时 B 已返回 201，A 客户端超时；随后两个核心的业务查询也超时，管理入口仍响应。
没有重放 A 的未知启动请求。读取实际进程显示两边停在 `git rev-parse --is-inside-work-tree`，
没有 Worker 上游模型请求；此时代理仅记录最初四次已完成 Planner 调用。

环境变量最小比较（继承/过滤/补 Windows 身份变量）均正常，排除凭证或环境过滤导致的 Git 错误。
进一步复现托管 child 的父 stdin 监护读取：Git 继承该 stdin 时 5 秒超时，DEVNULL 时约 0.03 秒完成。
修复 `WorkspaceManager` 四处宿主 Git 命令显式 DEVNULL；`GitCodeWorkspace._git` 无输入时同样使用
DEVNULL，有显式 input_bytes（包括空字节）时保留 subprocess 的 PIPE。生产两个 Git wrapper
在同一故障条件下重跑约 0.172 秒通过，包含 `hash-object --stdin` 显式输入核对。
管理 CLI 另设置 `timeout_graceful_shutdown=15`，让卡住的 HTTP 等待能够结束并进入 lifespan 清理；
不把该设置描述为完整退出耗时保证。没有改 Planner、审批、Gate 或 unknown 恢复机制。

关闭旧阻塞宿主后，正常查询确认 A 原 Run `59f98ad3-662e-4150-805e-6c4640e25914` 确实已持久，
paused + interrupted Attempt；依据已查明的纯本地 Git 故障使用原 resume-run 重试，未重复 start。
B 原 Run `f514cf10-3f90-4c8b-8d4c-f5bdaa5411fd` 通过绑定 Intervention 的便签明确继续。
两边保留旧中断事实及工作区，真实 Worker/Reviewer 成功并分别人工验收后 completed。

为重跑最初失败的同时启动路径，另用同样已审阅的只读外部计划创建一对新 Goal，明确批准、
通过各自精确配置同时 start。A `0b370eb9-ba49-4359-b444-d351d9b474a1`、
B `437adbf6-6997-4ea3-a837-88f253fdb7e3` 均迅速返回 201，Worker/Reviewer 首次 succeeded。
report.txt 均只有各自原句与规则标记，Reviewer 双源证据完整、findings=[]、recommended_action=pass，
代码 patch 均为空。模拟人工通过绑定便签分别验收，两个 Run completed、Inbox 清空。

证据文件：planning-evidence.json、planner-capacity-evidence.json、operator-evidence.json、
wire-summary.json、final-evidence.json，以及 git-environment-diagnostic.json、git-stdin-diagnostic.json、
git-fixed-diagnostic.json。脚本/诊断全部在仓库外，无新增永久测试。
Ruff/格式/mypy 与原正常路径重跑通过；试用结束后关闭两个子宿主、管理器与观测代理。
本次覆盖本机同模型的真实并发与一次已查明原因的接续，不替代唯一产品 E2E，亦不保证不同
Provider/模型混用、远程工作区、任意强杀时序或外部写入的恢复结果。
