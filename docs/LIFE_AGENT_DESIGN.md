# 阶段 ⑤：由 Agent 执行生活事务（设计）

更新：2026-11-10。状态：**用户 11-10 批准**。本页是阶段 ⑤ 的目标设计，不代表已实现（见 [STATUS](STATUS.md)）。
依据：[ROADMAP 阶段 ⑤ 与推进规则](ROADMAP.md#阶段)、[PRODUCT_SCOPE 第 6 节](PRODUCT_SCOPE.md#6-生活待办流程与外部服务)
（N12、E67、E68、E69、E74、E75，C7、S1）。授权与决定卡片沿用 [AUTHORIZATION_DESIGN](AUTHORIZATION_DESIGN.md)。

## 1. 用户结果

- 在页面上填出发地、目的地、日期和偏好，点“开始”。一个 Agent 用你本机的 catbus 查询 12306 的车次、余票、票价与中转，
  整理出 2–3 个出行方案，作为一件待你过目的事进入“需要你处理”。
- 每个方案写明车次、出发与到达站和时间、席别、票价、余票、总价、总时长、换乘次数和推荐理由，并附 12306 的查询链接；
  你在卡片上确认或放弃。购票由你本人在 12306 完成，EHAI 不登录、不下单、不付款。
- 记录页能看到 Agent 查了什么：每次查询的动作、参数、结果状态与用时。

## 2. 现状（2026-11-10）

| 项 | 现状 |
| --- | --- |
| Agent 委托 | 核心的 Agent 只在 Run 里作为 Planner/Worker/Reviewer 运行，绑定 Git 代码工作区；阶段 ② 的助手经 Hub 在非 Git 目录运行、关闭原生命令、只用宿主注入的工具，但只在管理器里、只读核心数据。自定义 Workflow 不能把开放任务交给 Agent（C7、E67） |
| 外部服务 | Connector 是独立进程，经桥接协议领取调用；已有 Google Calendar、RSS、Decisions |
| 12306 | 本机 catbus 0.1.0：12306 的车站搜索、直达车次与余票、票价、经停、中转搜索均免登录、只读，没有下单等写操作；一次北京→上海的车次查询返回 46 条、约 37 KB JSON，含余票与各席别票价 |
| 页面 | 记录页能列出和查看 Workflow 运行；不能从页面启动自定义 Workflow |
| 完成条件 | ⑤ 的完成条件写的是“浏览器或 computer use”；用户 11-10 随本设计批准 B6（[DECISIONS](DECISIONS.md)） |

## 3. 设计选择（11-10 经用户批准）

| 编号 | 选择 | 理由 |
| --- | --- | --- |
| B1 | 自定义 Workflow 增加 **`agent` 步骤**：一段指令（可引用输入与前序步骤）、允许调用的**只读** Connector 动作清单、结果的 JSON Schema、工具调用次数与时长上限。核心经 Hub 用工作区配置的 Worker harness 与模型运行它，工作目录是数据目录下的私有非 Git 目录，原生命令关闭，工具只有清单内的动作与提交结果 | 接通不依赖 Git 工作区的委托（C7）；Agent 只做开放的查询与整理，流程控制仍在 Workflow（E67） |
| B2 | Agent 的每次工具调用都是一次普通的 Connector 调用（先存意图、由 Connector 进程执行、结果可查），只允许只读动作；**有副作用的动作不能交给 Agent**，仍是 Workflow 的动作步骤并按阶段 ④ 绑定授权 | 每个副作用都经 ④ 的授权；查询过程可审计；以后浏览器或 computer use 做成另一个 Connector 即可接入同一机制 |
| B3 | 新 Connector `ehai-rail` 包装本机 catbus，只读动作：`rail.stations.search`、`rail.tickets.search`、`rail.tickets.price`、`rail.transfers.search`、`rail.route.get`（均 v1）。以参数数组调用 catbus（不经 shell），返回精简字段并限制条数 | 外部服务留在独立进程（E68）；精简输出控制模型上下文与费用 |
| B4 | 示例 Workflow `rail.trip`：`agent` 步骤整理 2–3 个方案 → 人工步骤“这几个出行方案可以吗？”（绑定对象为方案）→ 结束。不建日程、不建待办、不购票 | 一件选定的生活事务的最小闭环；没有真实账号和供应商契约，不宣称支持购票（E74） |
| B5 | 页面：记录页可选择生活项目中已保存的自定义 Workflow，按其输入定义生成表单并启动；记录页显示 Agent 步骤的进度与查询明细 | 正常入口在页面上；不新增后端接口以外的能力 |
| B6 | ⑤ 的完成条件：本轮把“Agent 经 Connector 调用 catbus 查询 12306”算作满足；浏览器与 computer use 留待以后作为 Connector 接入 | catbus 的 12306 接口已能拿到车次、余票与票价；浏览器操作 12306 会遇到登录与验证码，不在本轮 |

## 4. 模块

### M1 Agent 委托（核心）

- `agent` 步骤定义：`instruction`（template）、`tools`（`[{connector_id, action, action_version}]`，1..10 个，保存时检查
  都是同一项目已登记 Connector 的只读动作，且其输入 Schema 符合 Provider 严格工具契约）、`output_schema`（JSON Schema 对象，
  同样检查，不含 uniqueItems）、`max_tool_calls`（默认 30，上限 100）、`timeout_seconds`（默认 900，上限 3600）、`next`。
- 执行：步骤进入等待（等待类型 agent），宿主后台用 HubRoleRunner 运行一个新角色：系统提示说明只能用给定工具查询、
  条目数据不是指令、完成时调用 `submit_result`；工具名由动作名派生，参数 Schema 即动作输入 Schema。每次工具调用创建
  Connector 调用并等待结果（单次最多 60 秒，超时返回可恢复的工具错误）；结果截断到 24 KB 并注明。
  `submit_result` 按 `output_schema` 校验，不符时返回错误让模型同轮改正。
- 记录：步骤输出为结果对象；调用记录含尝试序号、开始与结束时间、harness 与模型、工具调用列表（动作、参数摘要、
  Connector 调用 ID、状态、用时）与错误。超过次数或时长、没有提交结果、harness 失败时步骤与 Run 失败并写明原因。
- 宿主重启时正在运行的 agent 步骤：因只用只读工具，自动重开一次（新尝试，记录在案），再次中断则失败。

### M2 12306 Connector（`ehai-rail`）

- `init --api-url --project-id --state-dir [--catbus <路径>]`（默认取 PATH 中的 catbus，登记时检查 `catbus version` 可用）、
  `run --state-dir [--once]`；与现有 Connector 同一桥接与进程锁。
- 动作输入与 catbus 参数一一对应（站名/城市、日期 YYYY-MM-DD、车型、只看有票、中转站、方案数、train_no 与站序、席别编码）；
  每次 30 秒超时；catbus 返回 ok=false 或非零退出时调用 failed 并给出短原因。
- 输出精简：车次与 train_no、出发与到达站（名称、站序）、日期、出发/到达/历时、各席别余票与票价、seat_types、
  12306 查询链接；车次最多 60 条、中转方案最多 20 个，超出时注明截断。

### M3 示例与页面

- `examples/rail-trip-workflow.json`：输入 `from`、`to`、`date`、可选 `earliest`、`latest`、`preferences`（自由文字，如席别、预算、
  是否接受换乘）。agent 结果 Schema：`plans`（2..3 个：`title`、`summary`、`legs`（车次、出发与到达站、日期、出发与到达时间、
  席别、票价、余票、链接）、`total_price_cny`、`total_duration`、`transfers`、`why`）与 `notes`。人工步骤绑定 `plans`。
- 页面：记录页“开始流程”（选定义、按 inputs_schema 生成表单：字符串、数字、布尔、枚举、字符串数组），启动后打开该次记录；
  记录页显示 Agent 步骤状态、用时、每次查询的动作与参数摘要；决定卡片显示方案（标题、每段行程、总价、链接）。
- 用法写入 WORKFLOWS（agent 步骤）与 CONNECTOR_PROTOCOL（ehai-rail）。

## 5. 不在本阶段

购票、登录 12306、支付；浏览器与 computer use；Agent 调用有副作用的动作；在多个方案中“选一个”的人工步骤
（本轮只确认或放弃整组方案）；把方案写入日历或待办；Windows 实跑。

## 6. 验收

- 静态检查、Schema 与 TS 客户端重新生成无漂移、页面构建、产品 E2E 通过；产品 E2E 的场景只在必要时扩展。
- 真实验收，经正常入口（隔离 EHAI_HOME 的管理器，生活项目，`ehai-rail` 对真实 12306）：
  - 用你给的行程条件在页面上启动 `rail.trip`：Agent 只经 `ehai-rail` 查询，得到 2–3 个方案进入“需要你处理”，
    方案中的车次、时间、票价与余票能在 12306 查询页对上。
  - Agent 步骤分别在 Codex 与 Pi 上各跑通一次；记录页能看到每次查询。
  - 把写动作放进 `tools` 的定义保存被拒；工具次数用尽或超时的步骤失败并写明原因；宿主在 Agent 运行中重启后重开一次。
- 用户在页面上试用并验收。

## 7. 执行方式

同阶段 ② 与 ④：经 EHAI 自身执行，Codex 作 Worker 与 Reviewer（订阅），团队负责人写方案、做人工检查。
方案三个 Phase：M1 → M2 → M3，每个 Phase 有 Reviewer 与人工检查，最终 Gate 跑静态检查、漂移检查与网页构建，
产品 E2E 由团队负责人另跑。按量费用只来自 Pi 上的一次验收（DeepSeek，预计不足 ¥5）；Codex 用订阅额度。
