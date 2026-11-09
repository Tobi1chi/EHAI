# 阶段 ④：统一授权与生活接入（设计）

更新：2026-11-10。状态：**用户 11-10 批准**；M1–M4 同日实现，等用户验收（[阶段 ④ 记录](records/stage4-authorization.md)）。本页是阶段 ④ 的目标设计，实现状态以 [STATUS](STATUS.md) 为准。
依据：[ROADMAP 阶段 ④](ROADMAP.md#阶段)、[PRODUCT_SCOPE 第 6 节](PRODUCT_SCOPE.md#6-生活待办流程与外部服务)
（E63、E66、E68–E70、N13，C4、C6、S1、S5）。阶段 ② 的队列与助手见 [ASSISTANT_DESIGN](ASSISTANT_DESIGN.md)。

## 1. 用户结果

- 工作和生活中所有要你拍板的事——批准方案、人工检查、Workflow 人工步骤、Connector 写动作的确认——都出现在首页
  “需要你处理”里，用同一种卡片决定：看清**批准的是什么**（绑定对象）、**最多到哪**（价格或数量上限）、**多久内有效**。
- 生活待办到期、或有新事要你处理时，桌面弹出系统通知，点一下回到那件事。
- 每天自动读你给的 RSS 订阅，用 OpenAI Decisions 批量筛出值得看的几条，作为一件待你过目的事进入队列。
- “问”与“自动回答”（路由实验）不再出现在导航里。

## 2. 现状（2026-11-10）

| 项 | 现状 |
| --- | --- |
| 统一待办 | 核心 Inbox 已合并干预、人工检查、便签、Workflow 人工步骤（`workflow_confirmation`）与结果未知的 Connector 调用核对；**方案批准不在其中**，只能在线程页点“批准并开始” |
| 写动作授权 | 自定义 Workflow 的写动作需要前置人工步骤（`approval_step`）或持续授权（`standing_write`）；传给 Connector 的只是一个布尔 `write_authorized`。人工步骤冻结 subject，但动作的输入**可以不取自** subject；批准没有上限，也不会过期（批准后经过很长的等待仍会执行） |
| 通知 | 页面只在线程进入需要处理/待批准/完成/失败时发通知；待办项与到期的生活待办不发 |
| Decisions | 11-04 的快环实现已放弃；代码中没有 Decisions；`OPENAI_API_KEY` 在本机登录 shell 中可用 |
| RSS | `feed.fetch` 一次读一个订阅源（阶段 ① 交付）；Routine 只支持固定间隔 |
| 路由实验 | 导航里有“问”（生活）与“自动回答”（共用） |

## 3. 设计选择（11-10 经用户批准）

| 编号 | 选择 | 理由 |
| --- | --- | --- |
| B1 | 授权原语是一个统一的**决定视图**：问题、绑定对象（冻结的 subject 与其摘要）、上限（可选：金额+币种，或数量）、有效期（可选）、可选的决定与各自效果。四类决定都用它表达；各自的落库与状态机不变 | S1：同一种方式确认；不推翻已验证的检查、Workflow 与 Connector 状态机 |
| B2 | 方案批准进入核心 Inbox（新类型 `plan_approval`），队列中的卡片“批准并开始”打开现有的批准面板（含执行配置），与线程页同一套调用 | 批准方案也是一个要你拍板的决定；开始执行仍需看清执行配置 |
| B3 | 写动作的批准真正**绑定**：保存定义时要求受 `approval_step` 保护的写动作，每个输入只取自该人工步骤冻结的 subject 或常量；人工步骤可声明 `grant`（上限、有效秒数）；执行时授权已过期则不发调用、步骤失败并写明原因；调用请求把授权（ID、subject 摘要、上限、到期时间）传给 Connector，超出上限由 Connector 拒绝 | E69：确认要绑定候选项、价格或上限、对象和有效期；现在的布尔授权做不到 |
| B4 | Decisions 做成独立 Connector `ehai-decisions` 的只读动作 `decisions.classify` v1：一批条目、一个问题与若干选项，按条目并行调用 OpenAI Decisions（默认 8 路），返回每条的选择与置信度、保留下来的条目与失败条目；单条失败不拖垮整批 | S5/C4：批量、并行地筛选数据流；凭证留在 Connector 进程 |
| B5 | RSS Connector 增加只读动作 `feed.fetch_many` v1：一次读多个订阅源（并行），合并条目并逐源报告失败 | 自定义 Workflow 是单路径、没有循环或数组拼接；一个源失败不应让当天的筛选整体失败 |
| B6 | 通知改为按队列：条目进入“需要你处理”时通知（覆盖方案批准、各类待办、到期的生活待办），取代按线程阶段的通知；页面在下一个生活待办到期时刻重读队列 | C6：与新待办通知共用一套；到期本身不产生事件，需要按时间重读 |
| B7 | 路由实验从导航移除，页面地址保留可直达；文档标为隐藏实验 | C4 |

## 4. 模块

### M1 决定视图与方案批准（核心 + 管理器）

- 核心 Inbox：新增 `plan_approval`（线程最新方案为草稿且等待批准时出现；批准或被取代后不再待处理），
  动作 `approve-plan`，参数含 plan_revision_id 与 completion_contract_id。
- 每个 Inbox 条目增加 `authorization`：`subject`（JSON）、`subject_sha256`、`limit`（null 或 `{kind: amount|count, value, unit}`）、
  `expires_at`（null 或时间）。方案批准的 subject 为方案版本、节点数与验收契约摘要；人工检查为节点与证据；
  Workflow 人工步骤为冻结的 subject 与 `grant`。已过期的待办 `actionable=false` 并说明原因。
- 管理器的注意力队列按此显示方案批准，不再从线程阶段另算一条（避免重复）。

### M2 写动作授权绑定（核心 + Connector 协议）

- 人工步骤定义增加可选 `grant`：`{limit?: {kind, value: <值>, unit}, valid_seconds?}`；进入步骤时解析并与 subject 一起冻结；
  批准时记录授权（批准时间、到期时间 = 批准时间 + valid_seconds）。
- 保存定义时：受 `approval_step` 保护的写动作，每个输入必须是 `{"ref": "steps.<该人工步骤>.subject..."}`、
  `{"ref": "steps.<该人工步骤>.grant..."}` 或 `{"value": ...}`，否则拒绝并写明是哪个输入。`standing_write` 不变。
- 执行写动作前检查授权未过期；过期则不排队调用，步骤与 Run 失败，原因为授权过期。
- Connector 协议 v1 增加可选字段：动作请求的 `authorization`（授权 ID、subject_sha256、limit、expires_at）；
  旧 Connector 忽略即可。Google Calendar Connector 检查到期时间与数量上限。

### M3 Decisions 与订阅筛选（Connector + Workflow）

- `ehai-decisions`（`init / run`，与现有 Connector 同一桥接）：`decisions.classify` 输入
  `items`（对象数组，≤ 200）、`id_field`、`text_fields`、`question`、`choices`（值与说明）、`keep`（保留哪些选项），
  输出 `model`、`results`（id、choice、confidence）、`kept`（保留的原条目加 choice、confidence）、`failed`（id 与原因）、
  `usage`。条目作为 JSON 数据而非指令发给模型；HTTP 429/5xx 每条最多重试一次；模型在 init 时显式指定（默认 `gpt-6-luna`）。
- RSS：`feed.fetch_many` 输入 `urls`（≤ 20）与每源条数上限，输出合并的 `items`（附源标题）与 `failed`（URL 与原因）。
- 示例 Workflow `news.digest`：`feed.fetch_many` → `decisions.classify` → 有保留条目时进入人工步骤（subject 为保留条目，
  “今天筛出的这些值得看吗”）→ 结束；Routine 每 24 小时运行。用法写入 WORKFLOWS 与 CONNECTOR_PROTOCOL。

### M4 页面

- 决定卡片：一个组件用于队列条目与助手卡片里的待办引用：问题、绑定对象（按字段列出，列表折叠）、上限、
  有效期（剩余时间，过期后按钮不可用）、证据链接、决定按钮与可选说明；方案批准的“批准并开始”打开现有批准面板。
- 通知：按 B6；首次读取只记基线，不重放已有条目；窗口在前台且有焦点时不弹。
- 导航移除“问”与“自动回答”。

## 5. 不在本阶段

助手的写操作（建线程、记待办，届时也走这个原语）；Workflow 的日历规则触发（“每天 09:00”）、并行与子流程；
真实 Google Calendar 账号（需用户本人 OAuth，可选验收）；订票与支付；Windows 实跑。

## 6. 验收

- 静态检查、Schema 与 TS 客户端重新生成无漂移、页面构建、产品 E2E 通过；产品 E2E 的场景只在必要时扩展。
- 真实验收，经正常入口（隔离 EHAI_HOME 的管理器）：
  - 方案批准、人工检查、Workflow 人工步骤、Connector 写动作确认四类决定都出现在“需要你处理”，用同一种卡片决定生效。
  - 写动作（Google Calendar Connector 接本机替身）：批准后按 subject 建日程；输入不取自 subject 的定义保存被拒；
    批准后等待超过有效期，动作不发调用并写明授权过期；超出数量上限由 Connector 拒绝。
  - 用你给的订阅源跑一次 `news.digest`：真实 OpenAI Decisions 筛选，保留条目进入队列，失败的源与条目有原因。
  - 生活待办到期与新待办触发系统通知（浏览器通知核对；桌面外壳内的系统通知由你在桌面 App 中确认）。
  - 导航中没有“问”与“自动回答”。
- 用户在桌面 App 中试用并验收。

## 7. 执行方式

同阶段 ②：经 EHAI 自身执行，Codex 作 Worker 与 Reviewer（订阅），团队负责人写方案、做人工检查。
方案四个 Phase：M1 → M2 → M3 → M4，每个 Phase 有 Reviewer 与人工检查，最终 Gate 跑静态检查、漂移检查与网页构建，
产品 E2E 由团队负责人另跑。按量费用来自 OpenAI Decisions（每次筛选几十到一两百条，预计远低于 ¥50）与 Pi 验收（DeepSeek）。
