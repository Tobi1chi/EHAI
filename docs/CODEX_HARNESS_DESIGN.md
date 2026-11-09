# Codex harness 设计

> 设计 · 状态：已实现（11-07），用法以 [HUB](HUB.md#codex-兼容层) 为准。

更新：2026-11-07。状态：**已实现**，11-07 经正常入口真实跑完一条线程（[EVIDENCE](EVIDENCE.md#核心执行) 11-07 行）；本文保留设计依据，实现与本文不同处以 HUB 与 [ADR 0007](adr/0007-agent-harness-port.md) 第 11 条为准。对应 [ROADMAP](ROADMAP.md#阶段) 阶段 ③。
现有 Hub 契约见 [HUB](HUB.md) 与 [ADR 0007](adr/0007-agent-harness-port.md)；旧 Codex 后端按
[ADR 0003](adr/0003-codex-cli-process-channel.md) 之后的决定已删除，本设计不恢复它。

## 1. 用户结果与验收

用户在工作区执行配置里选 Codex，用自己的 ChatGPT 账号，从正常入口跑完一条线程的规划、执行与审查。
验收沿用 ROADMAP 阶段 ③：先录制真实 Codex 会话并能不调用模型回放（仓库外诊断，不新增永久测试），
再在正常入口完成一条线程。本文不扩大这个范围。

## 2. 已定的决定（2026-11-06，用户）

| 编号 | 决定 | 含义 |
| --- | --- | --- |
| X1 | 保留 Codex 原生工具，受 EHAI 授权约束 | Codex 用自己的 shell 与改文件工具；EHAI 把角色授权翻译成 Codex 的 sandbox 与审批策略，并逐项批准或拒绝原生操作；每个原生操作记入工作轨迹。这改变“每个角色只提供 EHAI 工具”的现有做法（见第 6 节） |
| X2 | 控制接口用 `codex app-server`（stdio），仅限用户本机自用 | EHAI 不托管、不对外提供 Codex；app-server 标为 experimental。11-09 调整：harness `version` 为 `app-server`，不限定 CLI 版本；实测版本只记入设置、不进入指纹；协议不兼容时重新核对新版本 |

X2 的依据与风险：OpenAI 文档建议自动化用 Codex SDK 或 API key，并写明 app-server 的认证从未允许用于商业或托管服务
（[app-server](https://learn.chatgpt.com/codex/app-server)、[认证](https://learn.chatgpt.com/codex/auth)、
[非交互模式](https://learn.chatgpt.com/codex/non-interactive-mode)）。EHAI 是用户本人在本机驱动自己的账号，
文档没有明确允许或禁止这种用法；用户已知情并选择 app-server。条款变化时重新评估。

## 3. 调研结论（Codex CLI 0.166.0-alpha.6，本机 ChatGPT.app 自带）

| 需要 | Codex app-server 提供 | 状态 |
| --- | --- | --- |
| 先建会话、后发任务 | `initialize` → `thread/start` → `turn/start` | schema 已确认 |
| 角色指令、模型、工作区 | `thread/start` 的 `baseInstructions`、`developerInstructions`、`model`、`cwd`、`config`；`turn/start` 的 `effort` | schema 已确认 |
| 运行中注入 | `turn/steer{threadId, expectedTurnId, input}` | schema 已确认 |
| 输入送达确认（`input_prepared`） | `userMessage` item、`clientUserMessageId` | 诊断：`userMessage` item 在 `turn/start` 响应后出现并回显 `clientId`；是否等于“已进入模型请求”仍未确认 |
| 文本、工具调用、用量事件 | `item/started`、`item/completed`、`item/agentMessage/delta`、`mcpToolCall` item、`thread/tokenUsage/updated` | schema 已确认 |
| 回合结束 | `turn/completed`（含 `interrupted` 等状态） | schema 已确认；完成仍只经 EHAI finish 工具 |
| 取消 | `turn/interrupt{threadId, turnId}`，再调 `thread/backgroundTerminals/clean`（实验接口） | 诊断：只发 interrupt 时运行中命令继续执行完；加 clean 后命令被终止（item `failed`，exit -1） |
| 续用 / 分叉 | `thread/resume`、`thread/fork` | schema 已确认 |
| 原生操作审批 | 服务端请求 `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`；`approvalPolicy` 可为 `untrusted`、`on-request`、`never` 或 granular | 诊断：`untrusted` 下每条 shell 命令都先审批；`fileChange` 路径未触发 |
| sandbox | `read-only`、`workspace-write`、`danger-full-access` | schema 已确认 |
| 注入 EHAI 的每会话 MCP | `mcp_servers.<名>.url` 与 `bearer_token_env_var`（`-c` 覆盖或 `thread/start.config`） | 诊断：两种方式都连通并列出工具；调用 EHAI 工具前 Codex 发 `mcpServer/elicitation/request`，Hub 回 accept 或配置 `default_tools_approval_mode = "approve"` |
| 隔离配置 | `CODEX_HOME` 指向 EHAI 私有目录 | 空目录下 `codex login status` 为 `Not logged in`，已确认不继承用户登录 |
| 订阅登录 | `account/login/start`，类型 `chatgpt` 或 `chatgptDeviceCode` | 诊断：私有目录（`cli_auth_credentials_store = "file"`）内用 `codex login` 登录后 app-server 可开线程；经 `account/login/start` 登录未试；`chatgptAuthTokens` 标为内部专用，不使用 |

工具调用关联：诊断中 Codex 发给 MCP 的 `tools/call` 带 `_meta.callId`，等于 app-server `mcpToolCall` item 的 id，
另带 `threadId` 与 turn 元数据。

`codex exec --json`（含 `resume`/`fork`）未标 experimental，但没有确认的中途注入与中断，所以不选。
本版本没有 `codex mcp-server`；`codex mcp` 只管理 Codex 调用的外部 MCP。

## 4. 设计

**进程模型。** 每个 Hub 会话一个 `codex app-server` 子进程，经 stdio 交换 JSON-RPC。Hub 管它的启动、关闭和进程表，
与 Pi 兼容层相同；不使用共享的 `app-server daemon`。每会话令牌只放进该子进程的环境变量，不写入配置文件。

**配置与登录。** `CODEX_HOME` 指向 EHAI 状态目录下的私有目录，不读用户的 `~/.codex`（PRODUCT_SCOPE 第 5 节的隔离规则）。
用户在 EHAI 里发起一次 ChatGPT 登录（`account/login/start`），凭据只存在这个私有目录中；EHAI 不复制、不读取用户已有的 `auth.json`。
未登录时兼容层的 `inspect` 报告缺少凭据，界面提示登录，不回退到 API key。

**工具。** Codex 角色的工具 = Codex 原生工具 + EHAI 经 MCP 提供的控制工具（finish、计划、检查等角色工具）。
EHAI 自己的文件与 shell 工具不提供给 Codex 角色，避免两套同类工具。完成仍只认 EHAI 的 finish 工具。

**授权（X1）。** 兼容层把角色授权翻译为：

- 凭据存储：私有目录的 `config.toml` 设 `cli_auth_credentials_store = "file"`，凭据不进系统钥匙串、不与用户的 Codex 登录混用。
- sandbox：只读角色（如审查）用 `read-only`；执行角色用 `workspace-write`，可写范围是该 Attempt 的工作区；不使用 `danger-full-access`。
- 审批：用会把命令与文件改动交给客户端审批的策略；Hub 把每个审批请求转给核心，核心按角色授权批准或拒绝，并把请求与结果写入轨迹。
  超出授权且需要用户决定的，走现有人工介入（Intervention），不在 Hub 内决定。
- EHAI 自己的 MCP 工具已由角色工具清单授权，Hub 对 `ehai` 服务的工具调用审批（`mcpServer/elicitation/request`）直接回 accept，
  或在注入配置中设 `default_tools_approval_mode = "approve"`；其他 MCP 服务的审批照常交给核心。

**取消。** `turn/interrupt` 后再调 `thread/backgroundTerminals/clean`，等 `turn/completed` 与命令 item 结束才报告已停止；
clean 是实验接口，核对新版本协议时一并核对。

**轨迹。** 原生命令、文件改动、MCP 调用和审批结果从 app-server 事件归一化为 Hub 事件，由核心记入工作轨迹 [N11]。
EHAI 不同步 Codex 的消息历史或上下文，只记录步骤；续用靠 `thread/resume` 与 Codex 自己的原生历史。

## 5. 运行前必须确认的事项

用真实 Codex 在仓库外的隔离工作区确认，结果记入 [EVIDENCE](EVIDENCE.md)。11-06 诊断结果
（Codex CLI 0.166.0-alpha.6，`gpt-6.1-sol`，诊断脚本在仓库外，不提交）：

| # | 事项 | 结果 |
| --- | --- | --- |
| 1 | 私有 `CODEX_HOME` 内登录 ChatGPT 后 app-server 能开线程 | 通过（`codex login`）；经 app-server `account/login/start` 登录未试 |
| 2 | 注入的 HTTP MCP 被加载，EHAI 工具可调用 | 通过：`-c` 与 `thread/start.config` 都可；需处理 MCP 工具审批（见第 4 节） |
| 3 | MCP 调用与原生 item、同一模型消息完整调用批次的对应 | 单个调用已对应（`_meta.callId` = item id）；schema 没有完整回复边界，兼容层只报单个调用（11-07，限制见 HUB） |
| 4 | `workspace-write` 内每条命令、每次文件改动都发审批 | 命令：`untrusted` 下全部先审批；文件改动：11-07 试用中 `apply_patch` 触发 `fileChange` 审批，核心批准后执行 |
| 5 | 输入送达确认；中断后进程与在途命令停止 | `userMessage` 回显 `clientId`，送达语义未确认；只 interrupt 不停命令，interrupt + clean 停止 |
| 6 | 录制原生 JSONL，不调用模型回放 | 通过：回放事件与运行时一致 |

剩余未确认的 3、4（文件改动）、5（送达语义）在实现兼容层时继续核对；不能确认时按现有规则处理
（Hub 不合成调用批次，`input_prepared` 不由请求成功推断）。

## 6. 与现有规则的变化

- [ADR 0007](adr/0007-agent-harness-port.md) 中 `tools.native.restrictable` 的定义是“原生工具能否关闭**或按授权限制**”，
  Codex 按后者满足；但 `hub_runtime.py` 现在假定“每个角色只提供 EHAI 工具”。实现时把这一假定改为按能力区分：
  能关闭原生工具的 harness（Pi）照旧，按授权限制的 harness（Codex）由核心审批其原生操作。这项改动在实现前补进 ADR 0007。
- 核心不再亲自执行 Codex 角色的文件与 shell 操作，只批准并记录它们；成果仍由核心按 Attempt 工作区与 Git 收集。

## 7. 实施顺序

1. 仓库外诊断：确认第 5 节第 1–2、6 项，产出录制文件（11-06 完成）。
2. 补 ADR 0007 与本设计中仍未确认的结论，再实现 Hub 的 Codex 兼容层与核心的原生操作审批（11-07 完成）。
3. 正常入口：在工作区执行配置里选 Codex，跑一条线程的规划、执行与审查，并在 STATUS 与 EVIDENCE 记录（11-07 先经 `ehai-api --harness-config` 完成，随后经管理器 `ehai init --harness codex` → `up` → `add` 再完成一次）。

## 8. 不做

- 不恢复旧 Codex 后端，不用 `codex exec` 作为首版接口，不用 `chatgptAuthTokens`。
- 不读取或复制用户的 `~/.codex` 登录，不在无登录时回退到 API key。
- 不托管 Codex，也不让他人通过 EHAI 使用用户的 Codex 账号。
