# Connector HTTP 接入与 Google Calendar 第一版

更新：2026-09-24。Connector 是独立进程；主进程通过版本化 JSON 协议接收数据，
不加载 Google 适配器、不共享数据库连接，也不向 Connector 传递 Planner/Orchestrator 对象。
任意语言可以按 [协议 Schema](../schemas/v1/connectors.schema.json) 实现同一接入。

## 三种消息与职责

| 消息 | 方向与用途 | 主进程负责 |
| --- | --- | --- |
| 动作请求 | EHAI 入队，Connector 领取；包含 call_id、动作版本、输入、连接配置和写入授权 | 校验已注册动作/输入、保存调用意图、原子领取 |
| 动作结果 | Connector 回传 completed / failed / unknown、结构化 output 和远端对象引用 | 验证连接身份、claim_token 和输出 Schema，保存结果/投递回执 |
| 外部事件 | Connector 主动提交规范数据；可独立于动作调用 | 按已登记事件类型校验，绑定 Project、去重并写入持久事件日志 |

动作结果不替代外部事件；收到事件不直接执行 Workflow、批准 Gate 或修改计划。
事件先成为 `ConnectorEventReceived` 事实，由既有持久 consumer 读取/确认，后续再接 Routine 触发器。
本轮独立 Connector 调用归 Project，保存在 connector_calls；不为一次外部调用伪造生活 Workflow Run。
未来 Workflow 步骤通过 call_id 引用这些调用记录，避免复制外部执行的所有权。

主进程生成 connector_id，并固定 Project、manifest 和非敏感 configuration。
manifest 描述 connector_type/version、动作名/版本/read_only/输入输出 Schema，以及允许的事件名/版本/data Schema。
协议版本 `protocol_version=1` 与业务 `schema_version`、动作版本相互独立；当前不自动协商版本。
版本兼容模块仍关闭。新增 Connector 只需注册新契约并实现这个 HTTP 协议，不向调度器加供应商分支。

JSON Schema 必须使用对象根节点，仅支持本地 `$ref`，禁止改变解析基址的 `$id` 和 `uniqueItems`。
不会联网解析第三方 Schema。动作输入、结果输出及事件 data 有 1 MB 序列化上限。
HTTP 请求整体大小仍需由部署层控制；此上限不等于网络层上传限制。

## 正式接口

下列路径均在 `/api/v1` 下；Workspace Manager 仍可通过
`/workspaces/<workspace_id>/api/v1` 代理，并转发 Connector 身份头。

| 操作接口 | 对应 CLI（均需 `--api-url`） |
| --- | --- |
| POST `/projects/{project_id}/connectors` | `register-connector --project-id ... --file ... --idempotency-key ...` |
| GET `/projects/{project_id}/connectors` | `list-connectors --project-id ...` |
| GET `/connectors/{connector_id}` | `get-connector --connector-id ...` |
| POST `/connectors/{connector_id}/calls` | `invoke-connector --connector-id ... --file ... --idempotency-key ...` |
| GET `/connectors/{connector_id}/calls` | `list-connector-calls --connector-id ...` |
| GET `/connector-calls/{call_id}` | `get-connector-call --call-id ...` |
| POST `/connector-calls/{call_id}/reconcile` | `reconcile-connector-call --call-id ... --file ... --idempotency-key ...` |

操作接口的 MCP 工具名把 CLI 连字符改为下划线；生成的 TypeScript Client 提供对应方法。
创建调用只表示 queued，实际结果须查询 call_id；HTTP 200 不能当作外部日程已创建。
写动作要求请求明确 `authorize_write=true`，仍以用户实际授予调用者的权限为边界。

Connector 进程使用单独的桥接接口，均要求 `X-EHAI-Connector-Token`：

| 接口 | 请求与响应 |
| --- | --- |
| POST `/connector-bridge/{connector_id}/claims` | protocol_version、稳定 worker_id → data=null 或 connection/call/claim_token/mode |
| POST `/connector-bridge/{connector_id}/results` | protocol_version、delivery_id、call_id、claim_token、status、output/error、external_operation_ref → 已保存调用结果 |
| POST `/connector-bridge/{connector_id}/events` | 标准事件信封 → connector_id、project_id、event_id、core_event_id、received_at |

桥接接口不暴露为面向模型的 MCP 工具；Connector 自己持有凭证并调用。
凭证由随机 token 派生，主进程只保存 SHA-256 摘要；token 原文和 Google OAuth 凭证在 Connector 私有目录。
凭证只能向匹配 connector_id 的桥接接口投递，Project 从登记信息推导，不信任发送方自报归属。
注册及操作 API 沿用当前宿主的本机可信管理模式；桥接 token 不构成整个宿主的多租户访问控制。
首版连接配置不可变，未增加热更新、删除或 token 轮换接口。

事件信封字段：

| 字段 | 含义 |
| --- | --- |
| protocol_version | 固定为 1，错误版本拒绝 |
| event_id | 发送方保存的稳定事件身份，同一事件重投不换 ID |
| event_type / schema_version | 匹配该连接 manifest 中登记的事件契约 |
| occurred_at | 带时区的事件时间；接收时间由主进程独立记录 |
| subject | 外部业务对象，例如日程 ID |
| data | 经过 Schema 校验的业务对象，不携带执行状态指令 |

例如 Google 适配器发送 `calendar.event.created` v1，data 包含规范化的 event：
calendar_id、event_id、title、description、location、start/end、status、etag、url、updated_at、
use_default_reminders、reminder_minutes。时间同时支持读取日期或带时区时间，创建只支持一次性带时区日程。
Google 专有字段在适配器内转换，主进程只根据登记 Schema 校验，不认识 Google API 的资源结构。
event_id 在 connector_id 内去重，规范化后的同内容重投返回原回执；同 ID 不同内容返回 409。
事件及接收回执在同一事务提交，返回成功即表示已持久保存，不表示业务已消费。

## 领取、幂等、恢复

调用创建按连接/idempotency_key 去重，重复键相同内容返回原排队回执；最新状态另行 GET。
领取在短数据库事务中进行，网络 I/O 在 Connector 进程中执行，数据库事务不跨网络等待。
同 worker_id 再次领取未完成调用时返回 `mode=reconcile`；未完成调用不自动因超时换人重发。
其他 worker_id 不接管这条已领取调用。首版没有租约失效/跨机器自动接管，必须保留原 worker_id。

Connector 在动作前保存 pending.json，回传前保存稳定 delivery_id 和结果/事件信封。
重启后有回传记录就重投，不再调用外部动作；只有未完成调用意图时按 reconcile 核对远端。
主进程按 delivery_id 保存结果回执，同 ID 内容冲突拒绝；unknown 可被同 claim 的明确核对结果更新。
已 completed/failed 的调用不接受另一份相冲突结果。

本地 JSON 写入使用临时文件、flush/fsync 和原子替换，单个 state-dir 有进程锁；
receipt 文件保留用于追溯。用户仍应按宿主私有文件管理这些目录，不把凭证或回执提交到 Git。
网络/授权失败会退出 Connector 并保留本地状态；修复后重新 run 接续，不无限重试写操作。

unknown 表示可能已经发生外部效果，不等于失败。操作端可用 reconcile-connector-call 明确请求再次核对；
该命令只把原调用交给原 worker 核对，保持 call_id/claim_token，不变成一个新的 queued 写请求。
Google 创建时使用原 call_id 派生合法稳定事件 ID，并写入调用标记和请求摘要。
超时/5xx/冲突后按 ID GET 核对来源、内容与提醒参数；无法确认时保留 unknown，不换 ID 重建。
取消或内容已变更的远端对象也不会被静默重建。该机制不承诺跨系统严格 exactly-once。

## Google Calendar 配置和调用

新增依赖 google-auth-oauthlib（锁定结果见 uv.lock），用于官方桌面 OAuth 流程与刷新凭证。
从源码目录执行 `uv sync --frozen`。先启动 EHAI API 并创建生活 Project，然后：

```powershell
uv run ehai-google-calendar init --api-url http://127.0.0.1:8787 --project-id <项目UUID> --state-dir C:/private/ehai-google --calendar-id primary
```

命令生成随机桥接 token、登记动作和数据 Schema，返回 connector_id；不调用 Google。
默认只允许 primary；需要其他日历时在 init 中重复传 `--calendar-id`，配置固定后不默许扩大范围。
列表返回实际 calendar ID，但 primary 绑定仍用别名 primary 调用；任意其他 ID 必须显式列入允许范围。

在 Google Cloud 启用 Calendar API，配置 OAuth 同意屏幕和 Desktop 应用，下载客户端 JSON 到仓库外。
再由用户运行登录授权：

```powershell
uv run ehai-google-calendar authorize --state-dir C:/private/ehai-google --client-config C:/private/google-desktop-client.json
uv run ehai-google-calendar run --state-dir C:/private/ehai-google
```

OAuth 使用 loopback 回调和 PKCE；请求 calendar.events 与 calendar.calendarlist.readonly。
refresh token 和 client secret 不发送给 EHAI 主进程，授权文件仅由该 Connector 读取。
Google 端 API 权限与连接允许的日历是两层约束，连接配置不能扩张 OAuth 已授予的权限。

四个已注册动作：calendar.list、calendar.events.list、calendar.events.get、calendar.events.create，版本均为 "1"。
输入/输出的精确 Schema 可由 get-connector 查询；事件查询显式分页，默认每页 50、最多 100。
创建禁止传 attendees、会议链接或重复规则，只设置个人弹窗提醒；通知是否显示取决于日历客户端设置。
修改/删除、多方邀请、全天创建、重复日程编辑及 Google 日历变更订阅不在首版。

创建日程的 invoke-connector 请求文件示例（CLI 另传 idempotency_key）：

```json
{
  "action": "calendar.events.create",
  "action_version": "1",
  "authorize_write": true,
  "inputs": {
    "calendar_id": "primary",
    "title": "EHAI 临时试用日程",
    "start": "2026-10-01T15:00:00+08:00",
    "end": "2026-10-01T15:30:00+08:00",
    "description": "由用户明确授权的试用",
    "location": "",
    "reminder_minutes": [30]
  }
}
```

实际写入需由用户确认目标日历与内容。第一版成功创建后主动向核心提交 calendar.event.created；
尚不监听用户在 Google 页面手动修改的日程。独立 Connector 可使用 events 入口提交其他已登记事件，
不要求必须先执行某个 EHAI 动作。

## 验证与实际边界

用户本机尚无 Google OAuth 配置，本轮只做代码与本地协议试用，不访问真实 Google 账号。
`run --test-provider-url http://127.0.0.1:<port>/calendar/v3/ --once` 仅用于隔离替身协议试用：
只允许 127.0.0.1 HTTP，固定使用试用 token，完全不加载 Google 凭证。此参数不改变核心的授权/Schema 校验。
不能把替身返回的日程或本地试用结果当作真实 Google Calendar 接入证据。

复用子 Agent，在仓库外目录使用 production core 和独立 CLI Connector，配合 loopback
Google API 替身走通四个动作。创建请求在替身端落地后返回 503，紧随的核对 GET 一次 404，
核心如实保存 unknown；经正式 reconcile 接口后，下一 Connector 进程仅 GET 即核对成功。
全程只有一次 Provider POST；同 key 调用仍只有原四条 call；标准事件重复投递返回同一
core_event_id，consumer 只读到一条 ConnectorEventReceived。CLI 查询及核心重启后的调用回执、
消费位置保持一致，新 worker 没有再次写入。临时宿主、Provider 和 worker 已全部退出。

证据：`%TEMP%/ehai-calendar-final-f36df5ba45f646c4abded9387376bf19` 下的
summary.json、public-requests.json 与 protocol_trial.py。没有永久测试新增，也没有真实模型调用。
Python Ruff/format/mypy、锁文件检查、Schema/Client 生成及 TS typecheck/build 通过，数据库 schema=21。
生成客户端时实际遇到 FastAPI 输入/输出 Schema 名称带连字符导致 TS 无法解析，已由生成器
一致转换名称和引用后重新生成/构建通过。原有输入输出契约仍分开保留，不手改生成文件。

这些是本地替身协议证据，不是 Google OAuth 或真实日历验收，也不是唯一产品 E2E。
Workspace Manager 的桥接身份头已接线，跨 workspace 的 Connector 实跑未单独验证。
版本兼容保持关闭；完整自定义 Workflow 图、Connector 事件触发 Routine 和外部账号实际授权仍未完成。

官方依据：[创建日程](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)、
[日程查询](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)、
[日历列表](https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list)、
[OAuth 桌面应用](https://developers.google.com/identity/protocols/oauth2/native-app)。
