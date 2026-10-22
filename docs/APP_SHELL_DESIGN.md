# 应用外壳：引擎、线程与桌面 App

更新：2026-10-22。状态：目标设计，第一轮实施中；实现与验证以 [STATUS](STATUS.md) 为准。

## 为什么

EHAI 的核心、HTTP/SSE、生成 Client 与多工作区管理器已经接通，但用户入口仍是逐条 RPC：
约 120 个平级 CLI 子命令、手抄 ID 与幂等键、四个手写配置文件，Web 工作台不能发起任务。
目标体验参照“一个本机引擎 + 终端与桌面 App 两个前端共享同一状态”：在仓库里或 App 里
提出需求，看着它规划和执行，离开后继续推进，回来在同一条线程里处理决定并取得成果。

## 第 0 步决定（2026-10-22）

| 决定 | 内容 | 不改变 |
| --- | --- | --- |
| 线程是面向用户的一级对象 | 一个线程对应一个 Goal：其规划讨论、方案版本、全部 Run（含后继 Run）、待办与成果。`thread_id` 就是 `goal_id` | 不新增持久实体或状态机；Run/Attempt/Gate 语义不变 |
| 单一用户入口 | 对用户只暴露本机守护进程（`ehai-manager`）；`ehai-api` 留给开发、E2E 与外部嵌入 | 每个 workspace 仍是独立核心进程 |
| 线程对话 = Planner 讨论 + 控制动作 | 第一版不内置顶层 Agent；顶层 Agent 仍在 EHAI 外 | PRODUCT_SCOPE 的 Planner 与顶层 Agent 区分 |
| 批准与授权 | 前端可在一次确认中依次提交 `approve-plan` 与 `start-run --authorize` | 核心仍记录为两个事实，各自幂等 |
| 桌面外壳 | Tauri 包装 `control-plane/web`，连接或拉起本机守护进程 | 页面仍只消费正式 API/生成 Client |

## 模块 A：线程读模型（后端）

**用户结果**：打开 App 或终端即可看到所有项目下的线程、各自状态和下一步，点进一条线程按时间看到
讨论、方案、执行、待办和成果，无需知道内部 ID。

**所有权**：核心拥有线程状态的推导（`application/thread_queries.py` 一处）；管理器只按工作区聚合；
前端只渲染，不推导状态。

### 接口

核心（每个 workspace）：

- `GET /api/v1/threads`：本宿主全部线程，按最近活动倒序。
- `GET /api/v1/threads/{goal_id}`：单个线程摘要。
- `GET /api/v1/threads/{goal_id}/timeline`：时间线条目，按时间升序。

管理器：`GET /api/v1/threads` 按 workspace 分组聚合；不可用工作区带明确错误，不显示成空列表。

线程摘要至少包含：`goal_id`、`project_id`、`project_name`、`objective`、`goal_status`、`phase`、
`next_action`、`attention_count`、`created_at`、`last_activity_at`、最新 `plan_revision_id`（可空）、
最新 `run_id` 与其 `run_status`（可空）、`observed_at`、`event_offset`。

`phase` 由核心从现有事实推导，取值固定为：

| phase | 条件（按优先顺序） |
| --- | --- |
| `needs_attention` | 线程内任一 Run 有未处理的统一待办项 |
| `running` | 最新 Run 为 `pending` / `running` |
| `paused` | 最新 Run 为 `paused` |
| `awaiting_approval` | 最新方案版本为 `draft` 且没有基于它的 Run |
| `planning` | 有规划讨论但尚无方案版本 |
| `completed` | 最新 Run 为 `completed` |
| `failed` | 最新 Run 为 `failed` / `cancelled` |
| `idle` | 以上都不满足（例如刚创建的 Goal） |

`next_action` 为 `{kind, refs}`，`kind` 取值：`start_discussion`、`continue_discussion`、
`review_plan`、`handle_inbox`、`watch_run`、`resume_run`、`review_result`、`none`；
`refs` 只放执行该动作所需的现有 ID（如 `conversation_id`、`plan_revision_id`、
`completion_contract_id`、`run_id`、inbox 的 `kind`/`request_id`）。动作仍调用各自的源接口。

时间线条目为 `{at, kind, refs, summary}`，`kind` 至少覆盖：`goal_created`、`discussion_message`
（用户消息与 Planner 回复，含 `conversation_id`，回复带出的 `plan_revision_id`）、
`plan_revision_created`、`plan_approved`、`run_started`、`run_status_changed`、`node_completed`
（通过的 Gate 与成果）、`inbox_opened`、`inbox_resolved`、`succession_started`、`integration_created`、`integration_branch_created`。
条目只来自已持久化的事实；查不到来源的不补造。支持 `limit` 与按偏移的 `after` 分页；
实时更新沿用现有事件流触发重读。

需要时补齐的只读能力：按 Goal 列出规划讨论（`conversation_id` 列表）。不新增写接口。

### 失败行为与验收

- 未知 `goal_id` 返回 404；推导所需事实缺失时 `phase` 取保守值，不推断完成。
- 产品 E2E 的唯一场景在已有步骤中断言：创建后为 `idle`/`planning`，草稿后为 `awaiting_approval`，
  执行中为 `running`，待人工 Gate 时为 `needs_attention`，结束后为 `completed`，时间线含对应条目。

## 模块 B：引擎守护进程与单一配置（后端）

**用户结果**：新机器上 `ehai init`、`ehai up`、`ehai add <仓库路径>` 后，浏览器或 App 打开即可使用，
不手写 JSON、不记启动参数。

- `ehai init`：交互式（也可全部用参数）写用户级配置 `~/.ehai/config.json`（可用 `EHAI_HOME` 覆盖）：
  Provider、模型 ID、推理强度、凭证环境变量名、Worker/Planner 容量默认值；Node 与 Pi CLI 路径
  从本仓库 `agent-backends/pi` 自动解析并校验版本；生成私有 `agent_dir` 与配套 Pi settings。
  不写入密钥本身。
- `ehai up` / `ehai down` / `ehai status`：后台启动或停止 `ehai-manager`（数据目录、端口、
  UI 目录取自用户配置），写 pid 与日志到 `~/.ehai/`；`up` 等到健康后返回 UI 地址，
  并显式启动已登记的工作区（这是用户命令，不是管理器重启后的自动行为）。
- `ehai add [path]`：按用户配置默认值登记并启动工作区（`workspace_id` 由目录名派生，可覆盖）；
  可用参数追加允许的命令与 Git 权限，默认不增加副作用权限。
- `ehai doctor`：检查配置、Node/Pi、凭证变量是否存在（不显示值）、守护进程与各工作区健康。

凭证：守护进程从启动它的环境继承凭证变量。由桌面 App 拉起时环境可能缺失，第一版由 App 连接已运行的
守护进程或提示用 `ehai up` 启动；系统钥匙串等方案列为待决事项。

验收：在 macOS 新的 `EHAI_HOME` 下手动走通 init → up → add → 浏览器打开 UI → doctor 全绿 → down；
不进入产品 E2E（E2E 继续直接启动 `ehai-api`）。

## 模块 C：线程式界面（前端）

工作分区改为三栏：左栏“工作区/项目 → 线程”与状态标记，中栏线程时间线与输入框（新建线程、
继续讨论），右栏按需显示方案、Run 执行图、Attempt 轨迹或成果 diff。复用现有 Run、Attempt、
执行图与待办组件。方案卡片提供“批准并开始执行”，执行配置取自管理器
`get-workspace-execution-config` 并展示给用户确认。成果卡片调用 `integrate-run` 并显示 diff。
生活分区保留，第一版不改动。

## 模块 D：桌面外壳（前端）

`desktop/`（Tauri 2）：窗口加载守护进程提供的 `/ui/`；启动时探测守护进程，未运行则提示或调用
`ehai up`；系统通知用于新的待办。不在外壳内实现业务逻辑或保存凭证。

## 待决事项

- 桌面 App 拉起守护进程时的凭证来源（钥匙串、Pi 私有 auth 文件或仅继承环境）。
- 终端交互式 `ehai` 会话（下一轮，与本轮的线程接口共用）。
- 开 PR 是否进入核心（提交到新分支已实现：核心只建 `refs/heads/<branch>` 指向整合 commit，不切换、不推送）。
- `ehai doctor` 是否应当实际启动一次 Pi 会话（不调用模型）来确认 Provider、模型与思考级别被接受。

## 执行记录

### 2026-10-22 第 3 步补齐与真实模型验收

补齐：管理器 `from-folder` 与 `GET /api/v1/local`（Codex），整合结果放到新分支（Codex），
页面添加项目文件夹、分支表单、后台系统通知，桌面外壳的对话框/通知插件与仅限本机守护进程页面的权限。

真实模型验收（用户授权，DeepSeek 官方端点 `deepseek-flash`，凭证存于 macOS 钥匙串，由 `~/.zshrc` 导出，
只经环境变量进入守护进程）：临时 EHAI_HOME 中 `ehai init` → 从登录 shell `ehai up`（doctor 报告终端与守护进程
均有凭证）→ 页面“添加项目文件夹”登记仓库外小项目 textkit（允许 `python3 verify.py`、git.read）→ 新线程。

- 首次失败：讨论在 0.8 秒内失败，页面只显示 “Harness invocation did not yield a confirmed result”。
  仓库外临时诊断直接经本机 Hub 启动同配置 Pi 会话，得到 Hub 原始错误
  `Pi did not accept the authorized thinking level`：`ehai init` 默认推理强度 medium，Pi 对
  `deepseek-flash` 只接受 off/low/high/max，会把 medium/xhigh 换成其他级别，而 EHAI 要求实际级别与授权一致。
- 修复：`ehai init` 的推理强度默认改为 `default`（保存为 null，不向 Pi 指定，交给模型默认）；
  指定级别仍按原规则严格校验。核心在“结果未知”错误里附上 Hub 的原始消息，页面能看到真正原因。
  严格校验本身不放宽。
- 重跑（全新 EHAI_HOME 与工作区）：真实 Planner 20 次工具调用（含 add_plan_node、set_final_gate 等嵌套参数）
  给出 stats/slug 并行 → 汇合运行 verify → Reviewer 的四节点方案；页面批准并开始（执行配置显示模型、并发 2、
  允许的命令）；Worker 与 Reviewer 共 40 次工具调用、未见工具错误，Run completed；页面整合（2 个文件 +10 −3）、
  按文件看差异、创建分支 `ehai/run-1387f806`。核对：分支指向整合 commit，在独立 worktree 运行 `python3 verify.py`
  输出 ok；用户仓库仍在 main，工作树干净。从建线程到完成约 2 分 40 秒。
- 未覆盖：系统通知未实际弹出（浏览器面板拒绝通知权限，桌面窗口未实跑），桌面文件夹选择器未实跑，
  人工 Gate 与待办（方案没有人工检查），整合冲突，Windows。
