# Agent harness Hub

> 用法 · 只写已实现的接口与限制。

更新：2026-11-07。Hub 是 EHAI 核心运行 Agent harness 的唯一入口，作为独立进程 `ehai-hub` 运行。
设计依据见 [ADR 0007](adr/0007-agent-harness-port.md)；兼容层有 Pi 与 Claude Code 两个。

## 分工

| 部分 | 负责 | 不负责 |
| --- | --- | --- |
| 核心（`ehai-api`、`ehai` 本地命令） | 轨迹记录、工具执行与持久化、注入消息、判断本轮是否以合法结果结束 | 启动 harness、解析原生输出 |
| Hub（`ehai-hub`） | 启动和关闭 harness 会话、转发事件与工具结果、取消、空闲回收 | 任何 EHAI 事实；不执行工具，不判定完成 |
| Pi 兼容层（`ehai.hub.adapters.pi`） | 以 RPC 模式启动未修改的 Pi、隔离配置目录、业务工具桥、事件归一化 | 工具语义与业务规则 |
| Claude Code 兼容层（`ehai.hub.adapters.claude_code`） | 以 stream-json 模式启动未修改的 Claude Code，经每会话 MCP 端点提供工具、权限翻译、事件归一化 | 工具语义与业务规则 |

## 使用方式

**默认（本机子进程）**：不需要任何配置。核心第一次运行 Pi 角色时启动一个只监听 `127.0.0.1`
随机端口的 Hub 子进程，使用随机令牌；核心退出（包括被强杀）后，Hub 和 Pi 随之退出。
命令行与原来相同，见 [USAGE](USAGE.md)。

**独立 Hub**：先启动 Hub，再让核心指向它。

```powershell
$env:EHAI_HUB_TOKEN = '<至少 32 个字符的随机令牌>'
$env:OPENAI_API_KEY = Read-Host '模型 API Key' -MaskInput   # 凭证只设在 Hub 一侧
uv run ehai-hub --host 127.0.0.1 --port 8788
```

```powershell
$env:EHAI_HUB_URL = 'http://127.0.0.1:8788'
$env:EHAI_HUB_TOKEN = '<同一个令牌>'
uv run ehai-api @Server
```

| 变量 / 参数 | 位置 | 含义 |
| --- | --- | --- |
| `EHAI_HUB_URL` | 核心 | 设置后使用该 Hub，不再启动本机子进程 |
| `EHAI_HUB_TOKEN` | 核心与 Hub | 共享的 Bearer 令牌；Hub 要求至少 32 个字符 |
| `--host` / `--port` | Hub | 监听地址，默认 `127.0.0.1:8788` |
| `--public-url` | Hub | harness 访问 MCP 端点使用的基础地址，默认取监听地址 |
| `--session-idle-seconds` | Hub | 会话租约时长：在此时间内没有收到核心的任何请求（含心跳）即关闭会话，默认 600，最小 180 |

Hub 本身只提供令牌认证，不提供 TLS；跨机器使用时应放在受控网络或 TLS 反向代理之后。

### 当前限制

- harness 凭证从 **Hub 进程**的环境读取（按 Pi 配置中的 `environment_names`），不经协议传输。
- Pi 配置由 Hub 校验并计算授权指纹（`/v1/harnesses/resolve`），但其中的路径（`node`、`cli`、
  `agent_dir`）、Pi 会话状态目录和工作区路径仍必须在 Hub 所在机器上存在。真正跨机器执行还需要
  决定工作区如何到达 Hub 一侧。
- 读取 Pi 配置的本地命令（`ehai init`、`doctor`、`add`、带 `--pi-config` 的主机）也经 Hub 校验；
  未配置远端 Hub 时会先启动本机 Hub 子进程。
- [多工作区管理器](WORKSPACE_MANAGER.md)按白名单传递环境变量，工作区宿主不继承 `EHAI_HUB_URL`，
  各自使用本机 Hub 子进程。
- Pi 使用 `--no-builtin-tools`，文件、命令和 Git 操作都是核心执行的业务工具，因此 Pi 本身不直接读写工作区。

## 协议 v1

全部接口在 `/v1` 下，要求 `Authorization: Bearer <EHAI_HUB_TOKEN>`。消息为 JSON，
定义在 `src/ehai/hub/protocol.py`。核心发起全部连接。

| 接口 | 请求 → 响应 |
| --- | --- |
| GET `/v1/health` | → 协议版本、已安装的 harness、版本及能力清单（ADR 0007 第 5 条） |
| POST `/v1/harnesses/resolve` | `kind`、`settings`、可选 `pin_directory` → 规范化 `settings`、`version`、指纹、所需凭证变量名、受保护路径、能力清单。给出 `pin_directory` 时先把原生配置文件复制进去，返回的设置指向副本。设置无效时返回 422 |
| POST `/v1/harnesses/inspect` | `kind`、`settings`（Pi：`node`、`cli`）→ 无凭证、无模型请求的安装探查报告 |
| POST `/v1/sessions` | 调用身份、状态键、原生会话 ID、harness `{kind, version, settings}`（`version` 可省略，给出时须与 Hub 安装的版本一致）、是否全新会话、系统提示词、工具清单、模型、推理强度、工作区、可选 `native_policy`（默认 `null`）→ `session_id`、原生会话引用。启动并核对会话，但不发送任务 |
| POST `/v1/sessions/{id}/steer` | 要注入的用户消息 |
| POST `/v1/sessions/{id}/prompt` | 任务消息（指令与上下文） |
| GET `/v1/sessions/{id}/events?after=N&wait=S` | 长轮询，返回序号大于 N 的事件和会话状态，最多等待 30 秒 |
| POST `/v1/sessions/{id}/tool-results` | `call_id`、结果、是否错误、是否结束本轮 |
| POST `/v1/sessions/{id}/native-actions/{action_id}` | `NativeDecision{approved: bool, reason: str\|null}`（`reason` 默认 `null`）→ 204；Hub 转交该会话的 `native_decision`，由兼容层关联原生审批请求 |
| POST `/v1/sessions/{id}/cancel` | 请求 harness 停止当前轮 |
| GET `/v1/sessions/{id}` | 会话状态与最新事件序号 |
| POST `/v1/sessions/{id}/heartbeat` | 续约：核心在调用期间每 60 秒发送一次，包括执行耗时工具时 |
| DELETE `/v1/sessions/{id}?abort=true\|false` | 关闭会话；`abort=true` 先请求停止再结束进程 |

### 原生工具策略与审批

能力清单增加 `tools.native.kept`（保留原生文件/命令工具）与 `tools.native.approval`
（原生审批请求经 Hub 交给核心）。声明 `kept=true` 时，核心在启动前要求 `approval=true`
与 `tools.native.restrictable=true`，缺少任一项即报配置错误。Pi 与 Claude Code 显式声明
`kept=false`、`approval=false`，继续关闭原生工具，调用审批回传方法会报错。

核心为保留原生工具的会话设置 `native_policy={sandbox, network:false}`：HostTools 授权角色
可写工作区时 `sandbox="workspace-write"`，否则为 `"read-only"`。未声明保留时为 `null`。
同时从工具清单中去掉 `workspace_list/search/read/write/patch/apply_patch/delete/move/mkdir`
（均带 `workspace_` 前缀）、`command`、`shell_exec/write/terminate`（均带 `shell_` 前缀）与 `git`；
成果读取、web/MCP 提供者、skills、会话、handoff、结束与规划/审查工具照旧。

核心的审批规则使用 HostTools 同一份授权数据：文件改动必须获准写工作区，且全部路径解析后
位于本次工作区内，并且不与 harness resolve 返回的受保护路径重叠；相对路径基于请求的 `cwd`
（省略时基于工作区）解析。命令必须显式提供工作区内的 `cwd`，并满足有可用 Shell、
`shlex` 切分后精确匹配一条 `allowed_commands`、Git 类别在 `git_permissions` 中、或沙箱只读之一。
Git 类别判断复用 HostTools；含命令串联、重定向或展开语法的字符串不能只凭首个 Git 子命令获批。
权限提升与其他请求一律拒绝。审批请求、决定与已执行原生操作均脱敏写入现有 `backend/event` 轨迹；
记录决定后才回传，记录本身只表示核心已作决定。完成仍只认结束工具与 `settled`。

目前已实现上述协议与核心处理，尚无保留原生工具的兼容层；Codex 兼容层未实现。

### 每会话 MCP 端点

供支持 MCP 的 harness 调用 EHAI 工具，Hub 在启动会话时把地址和令牌交给兼容层：

| 项目 | 说明 |
| --- | --- |
| 地址 | `<public-url>/v1/mcp/`，流式 HTTP MCP；`--public-url` 默认取监听地址（`0.0.0.0` 时为 `127.0.0.1`） |
| 认证 | 每个会话一个随机 Bearer 令牌，与 Hub 令牌分开；Hub 令牌不能访问该端点 |
| 工具 | 只列出该会话的工具；不在其中的名称直接拒绝 |
| 调用 | 转为 `tool_call` 事件交给核心执行和记录，核心回传的结果返回给 harness；参数由核心校验 |
| 批次 | MCP 不带模型轮次信息；兼容层按调用的请求元数据把它对到发起它的模型消息，报告该消息请求的全部工具调用（按模型顺序），核心据此检查结束工具是否单独或最后调用。对不上（缺少标识、10 秒内没在输出中出现、名称或参数不同）时会话失败，调用不交给核心。使用 MCP 的兼容层须在每条模型消息的工具调用执行前发出 `assistant_message` |
| 结束后 | 核心接受结束工具后，该会话的后续 MCP 调用由 Hub 直接拒绝，与 Pi 扩展桥一致 |
| 启动中 | 令牌在兼容层启动前即生效：可列出工具，调用会被拒绝；启动失败即撤销 |
| 关闭 | 会话关闭后令牌失效（401），等待中的调用返回错误 |

Pi 不支持 MCP，继续使用扩展桥。Claude Code 兼容层使用该端点（见下节）。

### Claude Code 兼容层

`ehai.hub.adapters.claude_code` 以 `claude --bare -p` 的 stream-json 模式启动未修改的 Claude Code（2.x，
自 2.1.0 起；harness `version` 为 `"2"`，设置里记录确切的 CLI 版本并计入指纹，CLI 升级后旧授权会被拒绝）。

| 项目 | 做法 |
| --- | --- |
| 设置 | `executable`（原生可执行文件）、`environment_names`（凭证变量名，从 Hub 进程环境读取）、可选 `base_url`（`ANTHROPIC_BASE_URL`）、`state_dir`；`cli_version` 由 resolve 填写 |
| 权限翻译 | `--tools ""` 关闭全部内置工具，`--strict-mcp-config` 只加载本会话的 MCP 端点（令牌写在私有文件里，不进命令行），`--permission-mode dontAsk` 加 `--allowedTools mcp__ehai`；启动时用 `mcp_status` 核对只有该端点且工具与本会话一致，每轮开始再核对 `init` 中的工具清单，不符即失败 |
| 隔离 | 每个状态键一个目录，作为 `CLAUDE_CONFIG_DIR`、`HOME` 与固定工作目录；`--bare` 不读钩子、插件、CLAUDE.md 与钥匙串；环境变量只传本地化与 Windows 进程基本项 |
| 事件 | 回放的用户消息 → `input_prepared`；每条模型消息在其第一个工具调用前发出 `assistant_message`，MCP 调用按请求元数据 `claudecode/toolUseId` 对到其 `tool_use` 块，等该消息入日志后才转给核心，批次为该消息全部 `tool_use`；`result` 成功 → `settled`，否则 `failed` |
| 续用 | 全新会话用 `--session-id`，续用用 `--resume`；原生历史按固定工作目录保存，可跨 Attempt 续用；全新会话遇到已有历史、续用时缺少历史都拒绝启动 |
| 取消 | `interrupt` 控制请求；进行中的 MCP 调用由 Claude Code 中止，本轮以 `failed` 结束，不确认已停止 |
| 中途消息 | `steer` 写入用户消息，Claude Code 在当前工具返回后加入本轮，回放即为送达确认 |

能力：`session.resume`、`tools.native.restrictable`、`tools.mcp`、`usage.reported` 为真，其余为假。
已知：每个会话 Claude Code 自己会发一次不带工具的小请求（生成标题）；`inspect` 只核对控制通道，不调用模型。

事件类型：

| 事件 | 含义 |
| --- | --- |
| `input_prepared` | harness 即将把这些用户输入（SHA-256）发给模型，核心据此确认注入消息已送达 |
| `assistant_message` | 模型可见文本、停止原因、归一化用量 |
| `tool_call` | 一次工具调用及同一批次的调用 ID；核心执行后经 `tool-results` 回传 |
| `native_action` | 待审批原生操作：`seq`、`action_id`、`kind`（`command/file_change/permissions/other`）、`command`、`cwd`、`paths`、`reason`。`command/cwd/reason` 默认 `null`，`paths` 默认空列表；核心经 `native-actions/{action_id}` 回传决定 |
| `native_item` | 已执行完的原生操作（含 harness 自行放行的）：`seq`、`item_id`、`kind`（`command/file_change/other`）、`command`、`cwd`、`paths`、`status`、`exit_code`。`command/cwd/exit_code` 默认 `null`，`paths` 默认空列表 |
| `lifecycle` | harness 生命周期事件（开始、轮结束、压缩、重试等） |
| `settled` | harness 报告本轮已结束；是否成功仍由核心按结束工具判断 |
| `failed` | 控制通道失败；进行中的结果未知 |

错误响应为 `{code, message}`：`invalid_request`（未启动任何东西，核心按普通配置错误处理）、
`harness_failed`、`not_found`、`unauthorized`。除 `invalid_request` 外，核心一律把这次调用记为结果未知，
不换会话重放。

## 失败处理

| 情况 | 结果 |
| --- | --- |
| Hub 中途停止或不可达 | 核心记录 `backend/error`（unknown），抛出结果未知；不重放 |
| 核心被强杀 | 本机子进程 Hub 在 stdin 关闭后先关闭所有会话再退出；独立 Hub 在租约到期（无心跳）后关闭会话 |
| Hub 关闭（SIGTERM / Ctrl-C） | 先关闭所有会话并唤醒等待中的事件轮询，再停止服务 |
| 远端 Hub 缺少凭证或配置无效 | 返回 `invalid_request`，不启动 harness，不写轨迹 |

两个方向的处理方式不同，因为两边拥有的东西不同：核心拥有全部事实并主动发起请求，Hub 失败时核心在下一次请求就能发现，
按结果未知处理即可；Hub 只持有正在运行的 harness，核心消失后它无法得知，只能靠心跳租约判断，
到期即关闭 harness，避免无人服务的进程继续运行或产生核心不知道的副作用。

已知取舍：核心执行耗时工具期间，心跳失败不会中断该工具；要等工具结束、回传结果失败时，才记为结果未知。
这样避免中途取消工具带来新的副作用不确定（2026-10-02 用户决定保持）。

## 验证

见 [EVIDENCE](EVIDENCE.md#核心执行) 2026-10-02 一行与 [重构计划执行记录](records/refactor.md)。
产品 E2E 的每个 Worker 节点都经本机 Hub 与真实 Pi 运行（模型为脚本化服务），由 CI 在 Linux 与 Windows 上执行；
独立 Hub 模式不在 E2E 中。Claude Code 兼容层只有仓库外诊断（真实 Claude Code + 脚本化 Anthropic 服务），
见 [EVIDENCE](EVIDENCE.md#核心执行) 2026-11-03 一行。
