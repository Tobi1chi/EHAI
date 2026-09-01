# Codex CLI 本地通道技术验证

## 目的与范围

本 Spike 只验证 P1 通过本地非交互进程启动 Codex、传入 prompt、取消运行和捕获结果/错误的技术可行性。它不实现 `WorkerAdapter`，不访问 EHAI 仓库内容，不允许 Codex 调用工具或写工作区。

- 日期：2026-08-31
- 平台：Windows PowerShell
- CLI：`codex-cli 0.147.0`
- 工作目录：`$env:TEMP` 下的临时目录，验证后删除
- 安全参数：read-only sandbox、never approval、ephemeral session、忽略项目 rules
- 时间边界：每次调用由宿主进程限时；本次未让失败重试无限运行

## 可复现命令

先确认可执行文件和所需参数：

```powershell
Get-Command codex
codex --version
codex exec --help
codex login status
```

建立临时目录，并通过 stdin 输入最小 prompt。注意：`--ask-for-approval` 是顶层参数，必须放在 `exec` 前；直接写在 `exec` 后会由 CLI 以参数错误和退出码 2 拒绝。

```powershell
$temp = Join-Path ([System.IO.Path]::GetTempPath()) 'ehai-codex-p1-channel-spike'
New-Item -ItemType Directory -Path $temp -Force | Out-Null
$last = Join-Path $temp 'last-message.txt'
$jsonl = Join-Path $temp 'events.jsonl'

'Reply with exactly P1_CODEX_CHANNEL_OK. Do not call tools.' |
    codex --ask-for-approval never exec `
        --ephemeral `
        --ignore-user-config `
        --ignore-rules `
        --sandbox read-only `
        --skip-git-repo-check `
        --color never `
        --json `
        --output-last-message $last `
        --cd $temp `
        - 2>&1 |
    Tee-Object -FilePath $jsonl

$LASTEXITCODE
Get-Content -LiteralPath $last -Raw -ErrorAction SilentlyContinue
Get-Content -LiteralPath $jsonl
```

生产 Adapter 不应使用上例的 `2>&1`，而应分别捕获 stdout JSONL 和 stderr，防止诊断日志破坏 JSONL 解码。合流只为本次人工观察方便。

取消验证在可分配 PTY 的宿主中启动同样命令，看到 `thread.started`/`turn.started` 后发送 Ctrl+C。自动化 Adapter 应使用子进程句柄完成等价的 terminate → 限时等待 → kill process tree，而不是依赖交互键盘输入。

## 观察

### 启动和输入

- `Get-Command codex` 找到本机 PowerShell launcher。
- `codex --version` 返回 `codex-cli 0.147.0`，退出码为 0。
- `codex exec --help` 明确列出 stdin prompt（省略 prompt 或传 `-`）、`--json` JSONL、`--output-schema`、`--output-last-message`、sandbox、工作目录和 `--ephemeral`。
- 管道输入后进程发出 `thread.started` 和 `turn.started`，证明 launcher、参数解析、stdin 输入与远端请求启动路径可达。

### 结果与错误捕获

- stdout 中捕获到逐行 JSON 对象；本次包含启动事件、重试错误、error item 和 `turn.failed`。
- 认证失败最终返回退出码 1；`--output-last-message` 文件为空，因为没有产生最终 Agent 消息。
- stderr 同时包含 PowerShell shell snapshot 不支持、插件目录认证和 API 传输警告。Adapter 必须把 stderr 当诊断流，不得把它当结构化候选结果。
- 输出可能包含认证错误细节。保存为 Artifact/Event 前必须脱敏；本文没有记录任何 key 内容、request ID 或 thread ID。

### 取消

- 在 PTY 中运行时发送 Ctrl+C，进程停止并返回退出码 1。
- 本次取消发生在认证重试期间，没有观察到独立的结构化 cancelled event。因此 Adapter 必须以自身的取消请求、进程退出和超时状态为准，不能依赖 CLI 一定输出取消事件。

## Spike 当时的未完成验证与局限

本机 `codex login status` 显示使用 API key 登录，但该凭证在实际请求时返回 HTTP 401。为避免新增权限或修改用户认证，本 Spike 没有尝试登录或更换凭证。因此以下项目仍必须由 I5 的显式真实 smoke test 在有效认证下补验：

- 成功响应的完整 JSONL event 序列及 `turn.completed` 形态。
- `--output-schema` 对最终结构化结果的实际约束行为。
- `--output-last-message` 在成功时的内容及编码。
- 长运行任务在非 PTY Windows 子进程中的 terminate/kill process-tree 行为。
- 输出截断、非零 Worker 退出、网络中断及 timeout 的 Adapter 映射。

这些局限不阻塞选择 `codex exec` 子进程通道：启动、输入、流式错误捕获、退出码和人工取消路径均已得到直接观察；在 Spike 当时，成功内容依赖一个当时缺失且不应由本 Spike 擅自更改的有效凭证。后续成功结果见下一节。

## I8 自动化 Smoke 结果

2026-08-31 在未修改登录或凭证的前提下，显式运行：

```powershell
$env:EHAI_RUN_CODEX_SMOKE = '1'
uv run pytest tests/smoke/test_codex_worker_smoke.py -q
```

结果为 `1 passed in 10.42s`。测试在 read-only sandbox 和 120 秒上限内，通过真实
`CodexWorkerAdapter` 获得了符合输出 Schema 的结构化候选，候选内容包含预期的
`EHAI_CODEX_SMOKE_OK` 标记。这关闭了成功结构化候选的环境验证缺口；历史 401 仍保留在上文，
用于说明先前环境状态，而不是当前阻塞。测试没有记录凭证、request ID 或 thread ID，也没有执行
真实 descendant-process 清理场景。

仍未由该成功 smoke 覆盖的项目包括 Windows 超时/取消的真实进程树行为、网络中断以及大输出
截断；这些路径继续由安全的确定性或 mocked 测试验证。

同日还使用 CLI 的 `--worker codex --planner exploration` 在非 Git 的系统临时工作区执行了完整
P1 场景。首次运行暴露 Adapter 未传递 `--skip-git-repo-check`，Codex CLI 因工作区信任检查在 fork
节点 fail-closed；补齐 ADR 已验证的参数并完成回归测试后，重新运行得到以下脱敏摘要：

```json
{
  "attempt_statuses": ["succeeded", "succeeded", "succeeded", "succeeded", "succeeded"],
  "attempts": 5,
  "branch_statuses": ["selected", "pruned"],
  "checkpoints": 5,
  "events": 66,
  "node_statuses": ["completed", "completed", "completed", "completed", "completed"],
  "run_status": "completed"
}
```

该运行覆盖 fork、两个探索分支、Evaluator、选择/剪枝、merge、最终 Gate 和 Checkpoint。摘要不含
凭证、Artifact 内容、request ID、thread ID 或领域实体 ID。

## App Server 创建 Desktop Project 内 Session 的观察

2026-09-01 另做了一次只读 app-server 可见性验证，用独立 `codex app-server --stdio`
子进程通过 JSON-RPC 创建一个非 ephemeral Thread，并让 Codex Desktop 任务索引刷新后确认其归属。
该验证不修改源码、文档、配置或 Git 历史；临时脚本和日志位于 `$env:TEMP` 唯一目录。

观察到的创建方式是：`thread/start` 不接收 `projectId` 参数；要让 Desktop 把新 Session 归入某个
saved Project，应将 `cwd` 设为该 Project 的保存路径或其下目录，并创建非 ephemeral Thread。Desktop
索引会根据 `cwd` 匹配 saved Project 路径，异步把 Thread 归属到对应 Project。

最小请求形态如下：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "thread/start",
  "params": {
    "cwd": "D:\\workspace\\EHAI",
    "ephemeral": false,
    "approvalPolicy": "never",
    "sandbox": "read-only",
    "serviceName": "ehai_app_server_probe"
  }
}
```

随后可以用 `thread/name/set` 设置可人工识别的名称，再用 `turn/start` 提交受控 prompt。创建后应同时
验证三件事，不能相互替代：

- app-server `thread/read` 能按 `threadId` 读到 Thread，且 `ephemeral` 为 `false`、`cwd` 正确。
- Codex Desktop `list_threads` 能找到同一 `threadId` 或同名任务。
- Desktop 索引中的 `projectId` 等于目标 saved Project ID。

本次验证的 EHAI Project ID 为 `a63840d0-6e2a-48c4-b5c8-ff5da307af59`。初次 Desktop
`list_threads` 曾短暂显示该 Thread 的 `projectId` 为 `null`；执行只读 `read_thread` 后再次刷新，
同一 Thread 显示 `projectId=a63840d0-6e2a-48c4-b5c8-ff5da307af59`。因此实现若依赖 Desktop
Project 归属，应把它视为索引层的异步结果：创建成功、Desktop 可见、Project 归属需要分别检查，并给
有限重试或延迟刷新窗口。

## 清理

验证完成后确认目标仍位于系统临时目录且名称匹配，再删除：

```powershell
$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$resolved = [System.IO.Path]::GetFullPath($temp)
if ($resolved.StartsWith($tempRoot) -and
    (Split-Path -Leaf $resolved) -eq 'ehai-codex-p1-channel-spike') {
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
```
