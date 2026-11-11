# Codex 兼容层运行记录

## 2026-11-07：实现与无模型诊断

范围：Hub 的 `codex` 兼容层，固定本机 Codex CLI `0.166.0-alpha.6`。全程没有发送
`turn/start`，没有读取、打印、复制或移动私有 CODEX_HOME 的凭据文件。
临时驱动在 `/tmp/ehai-codex-diag/`，协议生成物与诊断代码均未加入仓库。

### 实际运行

- 指定的已登录私有目录：resolve 通过，版本、规范路径与受保护路径正确；保护整个
  `/Applications/ChatGPT.app` 和私有 codex_home。inspect 的 initialize/initialized 通过，
  account/read 返回 `workspace routing discovery failed`。加入显式代理变量后仍失败。
- 最小连接诊断确认：连接 HTTP_PROXY/HTTPS_PROXY 的地址均返回 `PermissionError(EPERM)`；
  `127.0.0.1:0` bind 同样返回 EPERM。因此无法启动合成 MCP 服务，也无法完成指定的
  已登录 launch、工具清单与续用诊断。没有跳过登录检查或改动账户路由来继续启动。
- 空目录 `/tmp/ehai-codex-diag/empty-home`：真实 app-server inspect 返回 logged_in=false、
  model_requests=0 与 Codex 自带 login 命令；适配器 launch 在 account/read 后以
  HarnessRequestError 拒绝，没有发 thread/start。
- 为核对沙箱配置，单独用空目录 app-server 做无任务的原生 thread/start（ephemeral=true，
  不是适配器 launch）：返回模型 gpt-6.1-sol、reasoningEffort=low、approvalPolicy=untrusted、
  approvalsReviewer=user；sandbox 为 workspaceWrite、writableRoots=[]、networkAccess=false、
  excludeSlashTmp=true、excludeTmpdirEnvVar=true，适配器返回值校验通过。
  关闭后该子进程退出码为 0，已由 wait 回收。没有开启模型回合。
  同样以空目录进程核对 model/list，gpt-6.1-sol 明确支持 low；另一次无任务 thread/start
  返回 readOnly、networkAccess=false，关闭后退出码同为 0。
- 对用户给出的三份 11-06 run 录制做离线回放，调用实际 CodexSession 归一化与 await_tool_call，
  并用 HubEvent 校验输出：每份 1 次 input_prepared、2 次 native_action、2 次 native_item，
  两个 MCP 调用均在 assistant_message 交付后完成关联，正常结束为 turn_end → settled。
  命令批准响应为 `{decision:"accept"}`；文本与每次模型调用的 last 用量都有输出，未累计重复的 thread total。

### 批次结论

检查 ItemStartedNotification、ItemCompletedNotification 的 ThreadItem，以及
RawResponseItemCompletedNotification 的 ResponseItem：可关联 thread/turn/item，没有完整模型回复的
调用清单或结束边界。turn 内可以有多次模型回复，不能用 turnId 分批。
所以 MCP 仅按 `_meta.callId == mcpToolCall.id` 关联，每次返回 `batch=(key,)`。
这会限制核心对结束工具的同回复兄弟调用检查；没有用延时窗口猜测批次。

### 协议来源

使用用户提供的 appserver-schema 与 appserver-schema-exp（实验字段以 exp 快照为准），
对应调用处的代码注释注明文件名；没有提交 schema 生成物：

- 根目录：JSONRPCRequest.json、JSONRPCResponse.json、JSONRPCNotification.json、JSONRPCError.json、
  ClientNotification.json、ServerRequest.json。
- v1：InitializeParams.json、InitializeResponse.json。
- v2 请求/响应对（各含 Params.json 与 Response.json）：GetAccount、ThreadStart、ThreadResume、
  ModelList、ListMcpServerStatus、TurnStart、TurnSteer、TurnInterrupt、ThreadBackgroundTerminalsClean。
- v2 通知：ItemStartedNotification.json、ItemCompletedNotification.json、TurnStartedNotification.json、
  TurnCompletedNotification.json、ThreadTokenUsageUpdatedNotification.json、ErrorNotification.json。
  另检查 RawResponseItemCompletedNotification.json 的批次关联能力，不启用内部 raw events。
- 根目录审批请求/响应对：CommandExecutionRequestApproval、FileChangeRequestApproval、
  PermissionsRequestApproval、McpServerElicitationRequest；旧式回复 ExecCommandApprovalResponse.json、
  ApplyPatchApprovalResponse.json（请求名来自 ServerRequest.json，作为 other 交核心）。

### 检查与限制

- `UV_FROZEN=1 uv run` 在当前受限环境触发 uv 的 macOS system-configuration panic；
  改为 `UV_FROZEN=1 uv run --no-sync`，使用已安装环境，不改 uv.lock。
- Ruff check、Ruff format --check、mypy、mypy --platform win32、lint-imports 通过。
- 重新生成 API Schema 与 TypeScript Client 后，`schemas/` 与 `control-plane/src/generated/` 无漂移。
- 产品 E2E 已执行，但在 ScriptedProvider 创建监听 socket 时因 EPERM 失败，尚未运行到 EHAI/Pi 路径。
  本 worktree 也没有安装 Pi 的 node_modules；当前网络限制下未尝试下载依赖。
- 本环境也禁止 ps。空目录探查中没有原生命令，进程直接回收已验证；实际命令运行中的
  cancel/clean、POSIX PTY 后代清理及 Windows taskkill 仍待真实运行。
- 已登录正常 launch、MCP 联通、thread/resume、fileChange 与 permissions 审批、steer 的实际输入回显、
  模型执行及完整线程的规划/执行/审查均未在本次运行验证；不能把离线回放或无任务 thread/start 当成这些路径的验收。

## 2026-11-07：沙箱外复核（team leader）

上一节的受阻项在 Codex 沙箱外重跑（同一私有 CODEX_HOME，不读凭据文件）。

- 产品 E2E 通过（Pi 已安装；`UV_FROZEN=1 uv run pytest tests/test_product_e2e.py`）。
- 已登录 inspect：account/read 正常，`logged_in=true`。上一节的 `workspace routing discovery failed` 来自沙箱禁网。
- 已登录 launch 首次失败：`Codex loaded MCP servers outside this session`。mcpServerStatus/list 显示除 `ehai`
  外还有 `codex_apps`（ChatGPT 连接器：邮件发送与删除、GitHub 合并、云盘删除等数百个工具）和三个插件服务。
  兼容层的清单核对按设计拒绝了启动。修正：启动时用 `-c features.<名称>=false` 关闭该版本默认开启、
  会增加工具、MCP 服务、子 Agent 或钩子的功能（清单见 HUB），清单核对保留。之后只剩 `ehai`，
  工具恰为会话工具；正常 close 退出码 0，abort 为 -9，空目录 launch 拒绝。
- 没有回合的线程不能 thread/resume（`no rollout found`）：Codex 在第一次回合后才写 rollout。续用改在真实回合后核对。
- 原生命令的 PATH 原先只有 Codex 目录与 `/usr/bin:/bin`，`uv` 这类已授权命令会找不到；改为 Codex 目录加 Hub 的 PATH，
  每条命令仍由核心审批。

## 2026-11-07：真实试用（经 `ehai-api` 与 CLI，ChatGPT 订阅，`gpt-6.1-sol`）

仓库外一次性仓库 textkit（`textkit.py`、`test_textkit.py`、README），目标为加 `slugify` 与测试、
`python3 -m unittest` 通过。宿主 `ehai-api --worker pi --planner pi --harness-config <codex 文档>`，
允许命令 `["python3","-m","unittest"]`、`git.read`，端口 8791；全部只经 `ehai` CLI 驱动。

1. **Planner 首次失败**：`Harness settled without a host-validated role result`。rollout 显示模型两次调用 `exec`
   都得到 `code-mode host is disabled`：该版本只经 `exec`（JavaScript 编排）向模型提供全部工具，包括 MCP，
   而上面关闭功能时把 `code_mode_host` 也关了，模型没有任何可用工具，只能用文字作答。核心按设计拒绝。
   修正：保留 `code_mode_host`。重试后 Planner 经 EHAI 图工具产出计划（实现节点 + 审查节点，完成合同为该命令）。
2. **Worker 首次中断**：`Codex requested an MCP tool outside this session`，核心挂起节点并开出干预。
   原因是模型调用了 Codex 自带的 `list_mcp_resources`；仓库外单回合诊断确认它的 item 为
   `mcpToolCall{server:"codex", tool:"list_mcp_resources"}`。修正：这三个只读资源工具记为 kind=other 的 native_item，
   不交给核心；其他非本会话 MCP 调用仍使会话失败；未映射的完成 item 也记为 other。同一诊断复跑，回合正常 settled。
   重启宿主后经 `reply-intervention` 说明原因（只读、无外部效果、未改文件），节点在原批准边界内继续。
3. **Worker 第二次停下（report_blocked）**：计划要求不用 Shell，但核心按最初的第 11 条去掉了 `workspace_read` 等读取工具、
   `command` 与 `git`，Worker 唯一的读取途径是 Codex 的 `exec_command`（Shell）。这是核心设计缺口，不是模型问题。
   修正：只去掉被原生工具替代的写工具与 `shell_*`，读取工具、精确 argv 的 `command` 与 `git` 保留（ADR 0007 第 11 条与
   DECISIONS 已改）。重启后回复干预，节点继续。
4. **完成**：Worker 用 `workspace_read/list` 读取，Codex 原生 `apply_patch` 改 `textkit.py` 与 `test_textkit.py`
   （1 次 file_change 审批，核心批准，原因 `Workspace file changes`，审批、决定与完成项均在轨迹），用 `command` 跑
   `python3 -m unittest`，`phase_context_publish` 后 `submit_candidate`；Reviewer（read-only 沙箱）用 `workspace_read` 与
   `git diff` 审查后完成；Run `completed`，`integrate-run` 返回 `integrated`。实现与 7 个测试正确（扫描 + 待插入分隔符，
   覆盖大小写、混合分隔符、首尾修剪、空输入、仅分隔符与 Unicode）。宿主停止后无残留 app-server 进程。产品 E2E 复跑通过。

另行记录（与 Codex 无关）：试用仓库没有 `.gitignore`，`python3 -m unittest` 在 Attempt 工作区留下的 `__pycache__/*.pyc`
被并入整合提交；Pi 路径同样如此。

本次未覆盖：thread/resume 的真实续用（每个会话都是全新会话）；运行中取消与命令清理；原生命令审批（Worker 没用原生命令）
与拒绝路径的真实触发；同一回复的并行调用批次；Windows；独立 Hub。

## 2026-11-07：管理器路径选 Codex

上一节的试用经 `ehai-api --harness-config`，而用户平时用的 `ehai init/up/add` 只配置 Pi。本节把 Codex
接入管理器路径并按该路径重跑。

实现（Codex 编写，team leader 审查）：`LocalConfig` 增加 `default_harness` 与可选 `codex` 段
（可执行文件、私有 codex_home、模型、思考级别），旧 `config.json` 不改即可读取；`init --harness codex`
经 Hub 探查安装与登录并写 `<EHAI_HOME>/codex-harness.json`（未登录也保存，打印登录命令）；
`RuntimeSettings.harness_kind` 只在 `codex` 时序列化，旧登记与指纹不变；`pi_config_path` 沿用旧名，
指向所选 harness 的文件；管理器登记时为 Codex 固定完整 harness 文档，子进程环境仍只按其
`environment_names` 放行（Codex 用自己的登录，不传 API Key）；`doctor` 报告 Codex 版本与登录状态，
`--probe` 仍只探测 Pi；页面工作区行显示实际 harness。仓库外诊断确认旧 config、旧登记与旧 manifest
序列化逐字节不变。

试用：全新 EHAI_HOME、端口 8792，一次性仓库 textkit（这次带 `.gitignore`）。
`init --harness codex --codex-home <已登录私有目录> --codex-model gpt-6.1-sol` → `doctor`（Codex 已登录）
→ `up` → `add <仓库> --allow-command '["python3","-m","unittest"]' --git-permission git.read`。之后只经
`ehai --api-url http://127.0.0.1:8792/workspaces/repo`：建项目与 Goal，`discuss-plan` 得到实现 + 审查两步，
批准、开始。约 1 分 20 秒 Run completed，无干预：Worker 用 `workspace_read/list` 读取，原生 `apply_patch`
改 `textkit.py` 与 `test_textkit.py`（1 次 file_change：审批请求、核心批准 `Workspace file changes`、完成项
均在轨迹），`command` 跑测试，`phase_context_publish` 后提交；Reviewer 审查后完成。`integrate-run` 返回
`integrated`，实现与 8 个测试正确，整合补丁只含两份源文件。`ehai down` 后管理器、工作区宿主与 Codex
子进程均已退出。

发现：默认选 Codex 时 `init` 结尾仍只提示设置 Pi 凭证变量，已改为说明 Codex 用自己的登录。

本次未覆盖：页面“添加文件夹”选择 harness（页面用默认 harness，只显示不选择）；未登录时登记后的失败表现；
续用、运行中取消、原生命令审批与拒绝路径；Windows。

## 2026-11-09：不再按确切 CLI 版本设门限

ChatGPT.app 把内置 Codex 自动更新到 0.167.0-alpha.3.2 后，兼容层的确切版本检查（0.166.0-alpha.6）拒绝启动，
管理器里选 Codex 的工作区无法使用。用户要求不要按确切版本号设门限：Codex 更新频率很高。

协议核对（仓库外）：用 0.167 重新生成 app-server 协议，与 0.166 逐项对比兼容层用到的 10 个请求、4 个服务端请求、
5 个通知。12 个不变；其余只有输入内容新增变体（音频输入可用 URL 或文件 ID）、`initialize` 能力多一个可选字段、
命令审批 `kind` 新增 `humanCommand`。兼容层不读取前两者；`humanCommand` 走现有“不支持的审批”分支，按原规则不批准。
协议处理无需修改。

改动（Codex `codex exec` 实现，团队负责人审查）：harness `version` 改为协议标签 `app-server`；resolve 仍实测
CLI 版本并写入设置的 `cli_version` 以便追溯，但不再与任何常量或旧授权比较，指纹也不再包含它，所以自动更新不改变
授权与会话指纹。协议若出现不兼容变化，表现为会话启动或调用失败，届时按 [HUB](../HUB.md#codex-兼容层) 的步骤重新核对。
示例文件与文档同步修改。

试用：全新 EHAI_HOME、端口 8797，一次性仓库 textkit。`init --harness codex` 报 `Codex 0.167.0-alpha.3.2`，
`doctor` 报已登录，保存的 harness 文档为 `version: app-server`、`cli_version: 0.167.0-alpha.3.2`。`up`、
`add <仓库> --allow-command '["python3","-m","unittest"]' --git-permission git.read` 后只经 `ehai` CLI：
导入实现 + 审查两步方案（一个 Phase，最终 Gate 为 `python3 -m unittest`，不调用 Planner），批准、开始。
约 1 分钟 Run completed，无干预：Worker 经 MCP `workspace_list/read` 读取，原生改动两份文件（1 次 file_change
审批，核心批准 `Workspace file changes`），`command` 跑 7 个测试通过并提交；Reviewer 只读审查后建议通过。
`integrate-run` 返回 `integrated`，整合提交只含两份源文件，`slugify` 符合设计，测试通过。`ehai down` 后无残留
Codex 子进程。按量费用 0（Codex 用 ChatGPT 订阅）。静态检查与产品 E2E 通过。

已知：此前登记的 Codex 工作区固定的 harness 文档仍写 `version: 0.166.0-alpha.6`，启动时报版本不符，没有提供迁移；
用户自己的 EHAI_HOME 没有 Codex 工作区，受影响的只有之前的试用目录。

本次未覆盖：同一会话跨 CLI 版本续用（指纹不含版本是按构造保证，没有实际更新 Codex 后续用）；运行中取消、
原生命令审批与拒绝路径；Windows。
