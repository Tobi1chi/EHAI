# EHAI

**把开发任务交给 Agent，把计划、执行和验收留在可控的框架里。**

EHAI（Enhanced Human-Agent Interface）是一个可本地部署的 Agent 规划与执行框架。
你可以直接使用 CLI，也可以让外部顶层 Agent 通过 CLI 或 MCP 调用它：
提出需求、审查计划、启动任务，在需要时介入，最后取得可追溯的代码成果。

**当前版本：0.1.0，首个正式版本。** 本版交付执行核心，编码是第一个实际使用场景。
模型调用、工具循环和上下文压缩由 Pi Agent 承担；EHAI 负责计划、批准、并发调度、
工作区隔离、人工介入和结果验收。

## 能做什么

- **规划或导入计划**：让 Planner 调查项目并生成任务图，也支持导入外部 Agent 生成的计划。
- **执行与审查**：按依赖和阶段派发 Worker，在独立 Git worktree 中工作，由 Reviewer 与 Gate 核验成果。
- **介入与继续**：暂停运行、处理人工问题、决定人工 Gate；单个节点挂起时，独立任务可继续。
- **调整与交付**：追踪任务块的变化、整合有效代码成果，并在重新批准后显式接续适用的旧成果。
- **多种入口**：CLI、HTTP API 和独立 stdio MCP 共用同一个核心。

一次开发任务的流程：

```text
需求 → Planner / 外部计划 → 审查与批准 → 执行授权
     → Worker 执行 → Reviewer / Check / Gate → 成果与 Git 整合
```

顶层 Agent 在 EHAI 之外，Planner 是按需调用的规划角色。
API 宿主持有运行任务，Pi 按角色调用启动子进程，不需要额外部署 Pi 服务。

## 快速开始

下面使用 **Windows + PowerShell 7**，以一个已有 Python Git 项目为例。
需要 Python 3.12、uv、Git、Node.js >= 22.19.0 和 npm，以及支持工具调用的模型访问配置。
其他系统需调整路径和命令；当前示例与主要运行证据基于 Windows。

### 1. 安装

```powershell
git clone https://github.com/Tobi1chi/EHAI.git C:/work/EHAI
Set-Location C:/work/EHAI
uv sync --frozen
npm.cmd ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund
uv run ehai --help
```

从源码目录使用 `uv run`，无需全局安装 EHAI。Pi 版本由仓库锁定为 0.85.1。

准备目标仓库 `C:/work/my-project`：已有至少一个 Git commit，开发基线已提交，
并能运行现有验收命令。示例使用 `uv run pytest`；你的项目使用其他命令时，
需要同步修改宿主命令列表、执行配置与计划中的验收要求。
未提交的本地修改不应被当作执行基线。

### 2. 配置 Pi 与执行权限

将配置放在仓库外，例如：

```text
C:/private/ehai/
├── backend.json
├── execution-api.json
└── pi/
    ├── settings.json
    └── models.json
```

准备以下文件，示例中的占位符必须替换为本机真实值：

| 文件 | 从哪里开始 | 需要配置什么 |
| --- | --- | --- |
| `backend.json` | [后端示例](agent-backends/pi/examples/backend.json) | Node 可执行文件、Pi CLI 的绝对路径、`agent_dir`、Provider 与凭证环境变量名 |
| `pi/settings.json` | [设置示例](agent-backends/pi/examples/settings.json) | 使用配套的压缩与重试设置 |
| `pi/models.json` | [模型示例](agent-backends/pi/examples/models.json) | 内置 Provider 可保留空配置；自定义网关需补充 Provider、端点和模型定义 |
| `execution-api.json` | [执行配置示例](examples/execution-api.json) | 模型 ID、目标仓库，以及与 `backend.json` 一致的 `pi` 对象 |

在这个例子中，`agent_dir` 为 `C:/private/ehai/pi`，Pi CLI 为
`C:/work/EHAI/agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js`。
用 `(Get-Command node).Source` 查找本机 Node 路径，并确认 `node --version` 满足要求。

Provider 和模型 ID 必须是当前 Pi 配置实际支持的值。自定义模型的配置格式见安装后
`agent-backends/pi/node_modules/@earendil-works/pi-coding-agent/docs/models.md`；
EHAI 的配置说明见 [使用指南](docs/USAGE.md#安装与私有-pi-配置)。

在启动宿主的终端设置凭证。配套示例使用的环境变量为：

```powershell
$env:OPENAI_API_KEY = Read-Host '模型 API Key' -MaskInput
```

使用其他 Provider 时，相应调整环境变量与 `environment_names`。密钥留在宿主环境，
不写入计划、执行配置或 Git。EHAI 不自动继承用户全局的 Pi 扩展和认证文件。

### 3. 启动项目宿主

在 EHAI 源码目录启动服务；将 `<model-id>` 替换为执行配置中的同一个模型 ID：

```powershell
$Model = '<model-id>'
$Server = @(
  '--database', 'C:/private/ehai/state.sqlite',
  '--artifacts', 'C:/private/ehai/artifacts',
  '--worker', 'pi', '--planner', 'pi',
  '--pi-config', 'C:/private/ehai/backend.json',
  '--planner-model', $Model, '--agent-model', $Model,
  '--worker-workspace', 'C:/work/my-project',
  '--worker-capacity', '2',
  '--agent-allowed-command', '["uv","run","pytest"]',
  '--agent-git-permission', 'git.read',
  '--command-check-timeout-seconds', '60',
  '--p2-runtime'
)
uv run ehai-api @Server
```

保持这个终端运行。示例允许模型使用宿主文件工具、执行指定测试命令和读取 Git；
不开放任意 Shell 或模型 Git 写操作。代码快照与整合由 EHAI 宿主管理。

宿主默认监听 `127.0.0.1:8000`。当前 API 没有内置认证层，按本地服务使用。
模型、工作区、并发数、命令权限和超时等必须与 `execution-api.json` 一致，
配置不匹配时启动 Run 会被拒绝。改变授权配置后，不会静默替换旧 Run 的配置。

### 4. 创建开发任务并规划

另开一个 PowerShell 终端，进入 EHAI 源码目录：

```powershell
$Api = @('--api-url', 'http://127.0.0.1:8000')
uv run ehai @Api get-runtime-health
uv run ehai @Api create-project --name 'My project' --idempotency-key project-001
```

从结果取 `project_id`，再创建一个具体、可验收的目标：

```powershell
$ProjectId = '<project-id>'
uv run ehai @Api create-goal --project-id $ProjectId --objective '修复日期格式化函数对空输入的处理，保持现有接口并通过测试' --idempotency-key goal-001
```

从结果取 `goal_id`，请 Planner 调查和制定方案：

```powershell
$GoalId = '<goal-id>'
uv run ehai @Api discuss-plan --goal-id $GoalId --message '先调查相关实现和已有测试，只修改与空输入处理相关的代码。保留对外接口，以 uv run pytest 验收，安排 Reviewer，并给出范围、依赖和失败处理。' --idempotency-key discuss-001
```

规划会调用模型。返回的 `conversation_id` 用于继续讨论；`turns` 记录回复与可能生成的
`plan_revision_id`。如果 Planner 需要澄清，使用同一 `--conversation-id` 补充信息，
每条新消息使用新的幂等键，直到产生可审查草稿。

如果计划由外部 Agent 编写，可以改用：

```powershell
uv run ehai @Api get-plan-import-schema
uv run ehai @Api import-plan --goal-id $GoalId --file C:/private/plan.json --idempotency-key import-001
```

导入适用于尚未规划的 Goal，与调用 Planner 是两条可选路径。
[计划格式示例](examples/plan-import.json) 展示结构，不是任意项目可直接运行的方案；
其中引用的验收脚本需要由目标项目提供。导入后仍须审查和批准。

### 5. 审查、批准并执行

```powershell
$PlanId = '<plan-revision-id>'
uv run ehai @Api get-plan --plan-revision-id $PlanId
uv run ehai @Api get-plan-checks --plan-revision-id $PlanId
```

审查需求范围、任务图、阶段、Reviewer 和验收条件。从计划查询结果取得
`completion_contract_id`，确认后再批准和授权执行：

```powershell
$ContractId = '<completion-contract-id>'
uv run ehai @Api approve-plan --plan-revision-id $PlanId --completion-contract-id $ContractId --idempotency-key approve-001
uv run ehai @Api start-run --plan-revision-id $PlanId --execution-config C:/private/ehai/execution-api.json --authorize --idempotency-key start-001
```

批准固定计划与验收边界，`--authorize` 明确授权本次执行配置。
`start-run` 返回 `run_id` 表示已受理，任务由宿主继续推进。

每一步先确认命令成功，再使用实际返回的 ID。新操作换新的幂等键；
遇到写请求超时或结果未知，先查询宿主，不直接重复创建任务。

### 6. 查看、介入与取得代码成果

```powershell
$RunId = '<run-id>'
uv run ehai @Api get-run --run-id $RunId
uv run ehai @Api get-run-checks --run-id $RunId
uv run ehai @Api get-run-interventions --run-id $RunId
uv run ehai @Api get-result --run-id $RunId
```

需要详细过程时使用 `get-trace --run-id $RunId`。人工处理使用查询返回的当前 ID 和
`request_token`，具体参数见 [人工回路](docs/USAGE.md#控制人工回路与过程调整)。

| 情况 | 对应操作 |
| --- | --- |
| 需要暂停或继续整个 Run | `pause-run` / `resume-run` |
| Worker 需要人工输入或决定 | `reply-intervention`，仅解决对应问题 |
| 人工验收等待决定 | `decide-human-check`，明确通过或拒绝 |
| 批准范围内需要改执行过程 | `propose-process` → `review-process` → `apply-process` |
| 需求、接口或验收边界变化 | 重新规划与批准，再显式启动后继 Run |

`stalled` 表示可有限自主恢复的阻塞，`suspended` 表示必须人工处理。
恢复整个 Run 不会自动解除尚未解决的人工问题。

完成后查看成果和 Gate 结果。需要物化当前有效代码成果时，从 `get-run-plan` 取得当前
`process_revision_id`，再执行：

```powershell
uv run ehai @Api get-run-plan --run-id $RunId
uv run ehai @Api integrate-run --run-id $RunId --expected-process-revision-id '<process-revision-id>'
```

该操作要求 Run 已暂停或完成，且没有未结束的 Attempt。成功返回独立整合工作区、
commit 与 diff；冲突会保留待处理工作区。检查实际差异后，再按你的 Git 流程将成果纳入项目。
EHAI 不自动合并到用户当前分支或推送远端，整合成功也不代替 Gate 验收。

不需要常驻服务时，也可以使用本地 `execute-plan` / `resume-session` 前台模式；
完整操作见 [使用指南](docs/USAGE.md#审查批准和前台执行)。

## 接入外部 Agent

外部顶层 Agent 可以使用上述 CLI，也可以通过 stdio MCP 调用同一宿主：

```powershell
uv run ehai-mcp --api-url http://127.0.0.1:8000
```

常见 stdio 客户端配置形式如下，实际配置位置以客户端为准：

```json
{
  "mcpServers": {
    "ehai": {
      "command": "uv",
      "args": ["run", "--directory", "C:/work/EHAI", "ehai-mcp", "--api-url", "http://127.0.0.1:8000"]
    }
  }
}
```

先启动 API 宿主，再连接 MCP。MCP 默认开放全部已支持的查询和写操作，
没有 `--allow-writes` 参数；业务批准和执行授权仍由核心校验。
写工具使用 `request_json` 传递 HTTP 请求体，可用 `get_request_schema` 查询契约。
断开 CLI/MCP 客户端不会取消宿主任务。HTTP 契约见 [OpenAPI](schemas/v1/http-api.openapi.json)。

## 0.1 的范围与后续方向

0.1 已有真实模型的编码、并发、人工挂起/回复、Reviewer/Gate 和成果接续试用证据。
它是执行核心的正式版本，尚不包含多项目 Dashboard、统一待办页面或 Workflow/Routine。

长时间运行、强杀及写入中断恢复仍有待完善的验证范围；物理 Pi 会话目前按 Attempt 隔离。
Token/费用硬限额暂未实现。具体覆盖与限制见 [当前状态](docs/STATUS.md)。

接下来依次建设多项目总览、统一人工待办和薄 Web 工作台，再逐步加入事件消费、
配置管理与重复事务自动化。每次扩展交付一个可使用的小闭环，详见 [路线图](docs/ROADMAP.md)。

## 文档与参与开发

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/USAGE.md) | 配置、控制、恢复、Git 整合与跨批准成果接续 |
| [产品范围](docs/PRODUCT_SCOPE.md) / [执行模型](docs/EXECUTION_MODEL.md) | 产品边界、角色职责与执行语义 |
| [当前状态](docs/STATUS.md) / [路线图](docs/ROADMAP.md) | 已验证能力与后续工作 |
| [开发规则](docs/DEVELOPMENT_GUIDELINES.md) / [AGENTS](AGENTS.md) | 项目结构、检查命令与协作约定 |
| [契约索引](schemas/README.md) / [TS Client](control-plane/README.md) | HTTP Schema、跨层接口与客户端生成 |

Python 核心位于 `src/ehai/`，TypeScript Client 位于 `control-plane/`。
提交问题时，请提供脱敏的版本、入口命令、预期结果、实际结果和相关错误；
不要附带 API Key、私有配置或未经清理的模型轨迹。

常用静态检查：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

项目采用 [Apache License 2.0](LICENSE)。
