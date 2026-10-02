# 重构与推进计划

更新：2026-10-02。本文定义当前重构的顺序、边界和完成条件；
能力现状见 [STATUS](STATUS.md)，产品顺序见 [路线图](ROADMAP.md)。

## 为什么现在重构

2026-10-01 用户确认：先建立安全网，再收窄范围，最后拆分大文件，文档同步修改。
起因是以下已观察到的事实，不是对代码风格的泛泛不满：

| 现象 | 依据（2026-10-01） |
| --- | --- |
| 缺少自动化保护 | 无 CI；旧测试已退役，唯一产品 E2E 尚未编写 |
| 复杂度集中 | `orchestrator.py` 4133 行，`sqlite/repository.py` 2931 行，`service.py` 2242 行 |
| 提交粒度大 | 最近两次提交分别 +20k / +13k 行，schema 18→22 一次完成 |
| 范围先于收尾 | P2 总验收未完成时已推进 P4 多项实验 |
| 后端切换未收尾 | Pi Planner 仍从 `codex_protocol` 取图 IR，宿主工具使用 `redact_codex_bytes` |
| 单平台证据 | 主要运行证据来自 Windows，Linux 上 mypy 曾有 10 个平台相关错误 |

## 边界

- 重构保持行为：除下文记录的阻断修复外，不改变公开契约、状态值或授权语义。
- 一次一个逻辑变更，每步重跑产品 E2E 与静态检查；拆分以"只移动"提交为主。
- 不借重构扩展功能；旁支发现记入下文"待决事项"，不顺手修改。
- 以下核心语义在重构中必须保持：Worker 只产生候选，完成须经宿主 Gate 与证据；
  `stalled` 与 `suspended` 分开；结果未知不重放；讨论不等于批准；领域状态令牌与迁移白名单不被绕过。

## 四步顺序

| 步骤 | 用户结果 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| 1. 安全网 | 每次修改都能自动确认主路径未被破坏 | 产品 E2E 入库；CI 在 Linux 跑静态检查、契约生成比对和 E2E，并在 Windows 跑 E2E | 完成：E2E 与 CI 已入库，首次 CI 全部通过（含 Windows E2E） |
| 2. 低风险清理 | 代码只保留实际使用的后端和清晰命名 | Codex 去留已决定并执行；图 IR、脱敏工具移到中性模块；`legacy_config.py` 处置；Run/Routine 命名冲突有决定 | 完成：Codex 已删除（选项 A）；图 IR 与脱敏已移出；`legacy_config.py` 已删除；命名决定为不改名、统一术语 |
| 3. 拆分大文件 | 新贡献者能按职责定位代码 | Orchestrator 拆为门面与若干职责模块；Repository/Service 按聚合拆分；幂等回执合并为一个机制；E2E 全程通过 | 完成：三个大文件已拆分，幂等回执已合并为一个机制（2026-10-02） |
| 4. 收尾 P2 | 在真实项目上完成一次可核对的开发任务 | 真实 Pi 按[手动验收](#真实-pi-手动验收)完成并记录；随后在 P3.3 与 P4 通用 Workflow 中选一项 | 未开始 |

### 步骤 3 的拆分方向

Orchestrator 按代码中已存在的边界拆分，保留 `Orchestrator` 作为对外门面：

| 模块 | 内容 |
| --- | --- |
| 图就绪判断 | `ready_nodes`、分支终态/失败判断等纯函数（因抛出应用层 `OrchestrationError`，留在 `application/orchestration/readiness.py`，未移入 `domain/`） |
| Attempt 生命周期 | queue / start / retry / timeout / interrupt / fail |
| 校验与 Gate | Check 执行、`finish_pending_gate`、人工判定 |
| 分支评估 | Evaluator 候选校验、`_record_branch_selection` |
| 采纳与接续 | adoption、`accept_adopted_result` |
| 人工介入 | intervention、`waiting_for_*` |

`sqlite/repository.py` 与 `application/service.py` 按聚合拆分（note / workflow / connector 本来就在独立模块中）。
幂等回执原有四处实现（核心 `CommandReceipt`，以及 Workflow、Connector、路由实验各自的 `receipt/remember`），
已合并为一个共用机制（[idempotency.py](../src/ehai/application/idempotency.py)，见
[执行记录](#2026-10-02-第三步幂等回执合并)），供后续自定义 Workflow 复用。

## 产品 E2E

唯一产品 E2E 为 [tests/test_product_e2e.py](../tests/test_product_e2e.py)，运行：

```powershell
npm ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund   # 首次，需要 Node >=22.19
uv run pytest tests/test_product_e2e.py
```

它启动真实 `ehai-api` 宿主进程（Pi Worker、`single` Planner、`--p2-runtime`），
只通过 `ehai` CLI 与 HTTP API 操作，不读写数据库。每个节点都经本机 Hub、Pi 兼容层和锁定版本的
真实 Pi 运行，代码成果来自 EHAI 管理的 Git worktree；只有模型被替换为 E2E 内置的脚本化
OpenAI 兼容服务（工作节点先用 workspace_write 写一个以节点命名的文件再提交成果，Reviewer 提交引用全部输入的
通过 review.json），没有真实模型调用。

| 覆盖 | 内容 |
| --- | --- |
| 外部计划导入 | 两个独立任务、一个汇合节点、Reviewer 阶段；A 带人工 Gate，最终 Gate 执行宿主命令 |
| 批准与授权 | approve-plan 与 start-run 分开调用 |
| 并发与挂起 | A 等待人工判定时 B 完成，汇合节点保持 pending |
| 统一待办 | 人工请求出现在 Inbox，带可执行动作和 request token |
| 崩溃恢复 | 强杀宿主后在同一数据库重启；Run、节点状态、请求 token 保持不变 |
| 事件消费 | 未确认批次在重启后以相同 token 和事件重新返回，确认后前进 |
| 人工决定 | CLI 判定通过后 Run completed；同一幂等键重复判定被接受且不产生新请求 |
| 成果查询 | get-result 的 Check 均完成；成果是新的 worktree commit，包含 A、B 与汇合节点各自写入的文件，patch 非空；get-project 只列出该 Run 并带已验证成果 |
| Hub 与 Pi | 每个节点经本机 Hub 与真实 Pi 调用脚本化模型；强杀重启后，汇合与 Reviewer 节点经重新启动的本机 Hub 完成 |

不覆盖：真实模型调用与模型行为、Pi Planner/过程审查/轨迹审查/路由回退、独立 Hub、`integrate-run`、
MCP、生活 Workflow、Connector 和 Jev 实验。这些不能由本 E2E 推断已验证。

### 真实 Pi 手动验收

需要模型凭证，不在 CI 中运行。按 [README 快速开始](../README.md#快速开始) 配置后，
在一个隔离的小仓库中经正常入口完成：规划或导入 → 批准 → 两个写代码节点并发执行 →
Reviewer/Gate → 人工验收 → `integrate-run`，并在执行中强杀一次宿主后恢复。
结果在 [EVIDENCE](EVIDENCE.md) 追加一行，未覆盖部分明确列出。

## 执行记录

### 2026-10-01 步骤 1

- 新增产品 E2E 与 CI（`.github/workflows/ci.yml`）。
- 阻断：以 scripted Worker 运行任何导入计划都无法完成。导入要求至少一个 Phase，
  Phase 以 Reviewer 结束；而 `FakeWorker` 对 Reviewer 返回纯文本，核心要求恰好一个覆盖
  全部输入的 `review.json`，Run 以 `Reviewer candidate name must be review.json` 失败。
  修复：`FakeWorker` 对 Reviewer 返回通过的 `review.json`。去掉该修复后 E2E 复现原失败。
- 阻断：Linux 上 `uv run mypy` 报 10 个错误，均为 Windows 专属 API 在 `os.name` 判断后使用。
  修复：改用 mypy 可识别的 `sys.platform` 判断；Linux 与 `--platform win32` 均无错误。
  运行时行为不变。
- 本地验证（Linux）：ruff、format、mypy（两个平台）、`uv lock --check`、Schema/Client
  重新生成无差异、TS 类型检查与构建通过；E2E 连续 3 次通过，单次约 21 秒。
- GitHub CI 首次运行（PR #1，提交 4333e9d）：静态检查与契约、Linux E2E、Windows E2E 均通过；
  Windows E2E 约 1 分钟。
- 评审发现：Windows 上 console-script 启动器以子进程运行宿主，只杀启动器可能让旧宿主继续占用端口，
  重启步骤因而可能对旧进程通过。改为 taskkill 结束整个进程树，并在所有平台等待端口释放后再重启
  （64b6759），CI 两平台通过。

### 2026-10-01 文档重组

- 新增按角色的 [文档索引](README.md)；STATUS 改为每项能力一行（状态、边界、证据链接）。
- 新增 [EVIDENCE](EVIDENCE.md)，把 R2/P3/P4 实施记录的每次试用压缩为一行；原文按原样移入
  `history/RECORD_*.md`，P2 收尾计划并入 [路线图](ROADMAP.md#p2围绕真实项目收尾)。
- P4 记录中的现行用法与契约移到 [WORKFLOWS](WORKFLOWS.md)；USAGE 按任务重排并加目录，内容不变。
- 顶层文档（含 README、AGENTS）由约 3500 行减到约 2400 行；链接检查除历史快照原有的 12 个失效链接外无错误。

### 2026-10-02 第二步：删除 Codex 后端

- 用户选择 A：删除 Codex，同时删除只有 Codex app-server 实现的运行中 Worker 请求路径。
  多 harness 的后续设计记为 [ADR 0007](adr/0007-agent-harness-port.md)，ADR 0003 标为已取代。
- 先抽出共用部分：规划图文档移到 `planners/plan_documents.py`，宿主工具改用
  `sanitization.redact_secret_bytes`（额外覆盖 authorization 头和 TypeSafe 密钥）。
- 删除 6 个 Codex 模块、`--codex-*` 启动参数、`--worker codex|codex-server`、`--planner codex`，
  以及 `worker_request_forms.py`、`get/resolve/decline-worker-request`、Inbox 的 `worker_request` 类型、
  `worker_form` 与 `worker_requests` 来源字段。Schema 与 TS Client 重新生成。
- 兼容：执行配置的规范文档仍带 `codex_server` 块，已授权 Run 的指纹不变；`codex-server`
  与 `builtin` 的历史配置仍可解析、不可执行；`WorkerKind` 保留 codex 值以读取历史记录。
  HTTP 执行配置只接受 `worker_kind: pi`，但仍接受无执行效果的 `codex_server` 块：
  `get-workspace-execution-config` 返回的规范文档带有该块，需能原样用于 start-run（评审发现）。
- 验证：ruff、format、mypy（两个平台）、Schema/Client 重新生成两次结果一致、TS 构建、E2E 通过。
- 用户要求核心只访问一个 Hub 模块，由 Hub 加各 harness 的兼容层适配多种 harness。ADR 0007 据此修订：
  三层结构、两层接口、Hub 无持久状态、先进程内并按进程外设计边界；迁移顺序改为先建 Hub 并搬迁 Pi。

### 2026-10-02 Hub 独立服务与 Pi 兼容层

- 用户决定 Hub 一开始就是独立服务，为远端执行做准备；ADR 0007 第 6 条随之修订，并完成其迁移第 3 步。
  这是 ADR 0007 的迁移工作，不属于本计划四步中的任何一步，按同样的"保持行为"规则执行。
- 新增 `src/ehai/hub/`：协议 v1、`ehai-hub` 服务、核心侧客户端（未配置 `EHAI_HUB_URL` 时启动本机子进程）、
  兼容层接口和 Pi 兼容层。`pi_config`、`pi_rpc`、扩展桥移入 Pi 兼容层；原 `PiRoleRunner` 拆为
  核心侧 `HubRoleRunner`（轨迹、工具执行、消息注入、结束判断）与 Hub 侧的 Pi 会话（启动、核对、事件归一化）。
  Planner、Worker、过程审查、轨迹审查和路由回退的调用点只换了类名。
- 导入方向用 import-linter 检查（新开发依赖，CI 静态任务新增一步）；核心读取 Pi 配置和探查是记录在案的过渡期例外。
- 可见变化：结果未知的异常改名为 `HarnessExecutionUnknownError`，CLI 以 JSON 错误报告（原为 `PiExecutionUnknownError`）；
  错误文字中的 "Pi invocation" 改为 "Harness invocation"；`inspect_pi_backend` 移到 `ehai.hub.adapters.pi.probe`。
- 验证（仓库外诊断，未提交）：
  - 在临时目录安装锁定的 Pi 0.85.1，接一个脚本化的 OpenAI 兼容 Provider（无真实模型调用），
    直接驱动角色运行器跑 7 个场景：新会话（含注入消息）、续用原生会话、可恢复工具错误、结束工具与其他工具同批被拒、
    Provider 500、取消、已完成调用的重放。改动前后结果一致，轨迹除随机会话路径外逐事件相同；
    同一组场景经单独启动的 `ehai-hub` 再跑一次，结果相同。
  - 独立 Hub 缺少凭证时返回 invalid_request，不启动 Pi、不写轨迹。
  - Hub 在调用中途收到 SIGTERM：核心记录 backend/error（unknown）并报告结果未知，Pi 进程被关闭。
  - 强杀核心：首次实测本机 Hub 约 15 秒后才退出（正常关闭要等待事件长轮询结束）。改为关闭开始时先关闭会话、
    唤醒轮询后，Hub 与 Pi 在 2 秒内退出。
  - 正常入口：`ehai-api --worker pi --pi-config ... --p2-runtime` 导入一个任务节点加 Reviewer 阶段的计划，
    经 HTTP 启动 Run；Worker 提交候选、Reviewer 提交 review.json、最终 Gate 通过，Run completed，代码成果来自 Git worktree。
  - ruff、format、mypy（两个平台）、lint-imports、产品 E2E 通过。
- 评审发现（Codex）：独立 Hub 的闲置时限若短于核心心跳间隔，核心执行耗时工具期间会话会被回收。
  修复：心跳间隔与最小闲置时限（3 个间隔，180 秒）放入协议模块，`ehai-hub` 拒绝更短的设置。
  随后按用户要求把心跳改为明确的 `POST /v1/sessions/{id}/heartbeat`（原借用状态查询）。
  验证：独立 Hub 闲置时限 180 秒、工具执行 200 秒，调用正常完成；关闭心跳的对照组同一场景结果未知。
  心跳失败不中断正在执行的工具（用户决定保持），见 [HUB](HUB.md#失败处理)。
- 未覆盖：真实模型；Pi Planner、过程审查、轨迹审查、路由回退经 Hub 的实际运行（共用同一个运行器，未单独跑）；
  Windows 上的本机子进程与 Pi；跨机器部署。产品 E2E 不经过 Hub（后由 ADR 0007 第 4 步解决，见下一条记录）。

### 2026-10-02 产品 E2E 经 Hub 与真实 Pi

- ADR 0007 第 4 步。用户在三种方式中选择"真实 Pi + 脚本化模型服务"：E2E 宿主改用 `--worker pi`，
  E2E 内置 OpenAI 兼容的脚本化服务，CI 安装 Node 与锁定版本的 Pi。原计划的 Scripted 兼容层不再需要，
  它需要新增 Worker 类型并改执行配置契约，且只能测到替身；执行配置的 `harness` 形状留到接入第二个 harness 时做。
- 场景结构不变，新增断言：脚本化模型至少被调用 4 次（A、B、汇合、Reviewer）；最终成果是新 commit，
  包含三个工作节点各自写入的文件（上游成果确实传到汇合节点），patch 非空。
  `--worker fake` 保留，E2E 不再使用。
- 验证：本地 Linux 连续 4 次通过（约 24 秒），每次结束后都没有遗留的宿主、Hub 或 Pi 进程；
  把 Pi 兼容层回传工具结果的 nonce 改坏后 E2E 失败（等待人工请求超时），恢复后通过。
- 评审发现（Codex）：最初的脚本化模型不写文件，成果 commit 等于基线，commit/diff 断言形同虚设；
  夹具仓库提交未关闭签名。修复：工作节点先写文件，断言改为新 commit、文件齐全、patch 非空，
  夹具提交加 `commit.gpgsign=false`。让脚本化模型不写文件时 E2E 在 commit 断言处失败。
  Windows 由 CI 验证。

### 2026-10-02 第二步收尾

- `legacy_config.py` 只剩已退役自研 Responses 运行时的 `ResponsesEndpointCapabilities`，对执行没有影响，
  但每份执行配置规范文档都带有 `endpoint_capabilities`，参与授权指纹。处置与 `codex_server` 相同：
  类移到 `session_host.py` 并注明仅为指纹保留，删除 `legacy_config.py`；HTTP 执行配置中该块改为可选，
  省略时按原默认值计入规范文档；示例配置去掉该块。Schema 与 TS Client 重新生成。
- 验证（仓库外诊断）：经 HTTP 请求模型解析，省略该块与写出默认值得到相同的规范文档与指纹；
  非默认值仍得到不同文档。ruff、format、mypy（两个平台）、lint-imports、TS 类型检查、产品 E2E 通过。
- Run / Workflow Run / Routine 命名：用户决定不改名（接口已用 `workflow-run` 前缀区分，改名牵动数据库与全部接口）。
  [文档索引](README.md#术语)新增术语表；WORKFLOWS 与 P4 设计中单独写 "Run" 指 Workflow Run 的 5 处改为全称。

### 2026-10-02 第三步：拆分三个大文件

- 方式：大类的方法原样移入按职责划分的 mixin 模块，原类保留为门面并继承这些 mixin，公开导入路径不变。
  mixin 方法的 `self` 标注为从原类自动生成的宿主 Protocol（mypy 官方的 mixin 写法），
  因此跨 mixin 调用仍受类型检查，且在原类上调用 mixin 方法时 mypy 会核对原类满足该 Protocol。
  新增方法若被其他 mixin 调用，需同时加入对应的宿主 Protocol。
- `application/orchestrator.py` 4133 → 243 行：`application/orchestration/` 下 attempts、worker_context、
  candidates、verification、branches、adoption、interventions、readiness、common、host。
- `infrastructure/sqlite/repository.py` 2931 → 198 行：`sqlite/state/` 下 planning、process、adoptions、runs、
  checks、common、host；事件日志与命令回执留在 repository.py。
- `application/service.py` 2242 → 140 行：`application/execution_service/` 下 planning、process、runs、common、host。
- 唯一非搬移改动：Orchestrator 中 4 处经类名调用的静态方法改为经 `AdoptionMixin` 调用。
- 验证（仓库外脚本，未提交）：逐个对比原文件与新模块中每个函数、方法和类的 AST，忽略 `self` 标注与上述限定名，
  三个文件共 297 个定义全部一致；运行时三个类的 201 个方法都可解析。ruff、format、mypy（两个平台）、
  lint-imports、产品 E2E 每次拆分后均通过。
- 未做：幂等回执合并（会改行为，单独进行）；其余超过 1000 行的文件（plan_graph_tools、planner、host_tools、
  queries 等）不在本步范围。

### 2026-10-02 ADR 0007 第 5 步：PlannerRole

- 规划逻辑（图工具、校验重试、`raise_note`、讨论、重新规划、过程草稿）原样移到 `infrastructure/planners/role.py`
  的 `PlannerRole`，只依赖新的 application 层端口 `RoleRunner`（`agent_roles.py`）；`HubRoleRunner` 与过程审查改用
  该端口与共用的 `RoleExecution`。`PiPlannerAdapter` 只负责用 Pi 配置构造 Hub 运行器，并传入已持久化的事件类型
  `planner.pi.completed`。
- 留在 infrastructure 而非 application：只读工作区工具（`HostToolRuntime`）和过程审查实现在 infrastructure，
  上移需要再抽象两个端口，目前没有第二个调用方。
- 提示词一字未改（其中仍有 "Pi"）：改动会改变模型输入，需要真实模型验收。错误信息改为 "Planner"，
  CLI 的 error_type 变为 `PlannerError`（`PiPlannerError` 保留为别名）。
- 验证（仓库外诊断）：`ehai-api --planner pi` + 锁定版本的 Pi + 脚本化模型服务，经 CLI 执行 discuss-plan
  （模型调用 ask_user）与 propose-plan（模型调用 raise_note），在 main 与本分支各跑一次，输出除路径外一致：
  讨论回复正确返回，便签正确保存。产品 E2E、ruff、format、mypy（两个平台）、lint-imports 通过。
- 顺带发现（main 上已存在，未在本步修复）：propose-plan 只得到便签时 HTTP 返回 500，因为 API 没有
  Planner 错误的处理器；USAGE 描述应为明确报无方案。随后单独修复：Planner 错误返回 422 `planner_failed`，
  harness 结果未知返回 502 `harness_outcome_unknown`；修复前后用同一诊断确认（500 → 422，便签仍保存）。

### 2026-10-02 ADR 0007 第 7 步：每会话 MCP 端点

- 锁定版本的 Pi 按设计不支持 MCP，当前没有使用该端点的 harness；用户决定仍然现在实现。
- Hub 挂载流式 HTTP MCP 端点 `/v1/mcp/`（`hub/mcp_endpoint.py`），每个会话一个与 Hub 令牌分开的随机令牌；
  端点只列出该会话的工具，调用转为现有 `tool_call` 事件，核心回传的结果返回给调用方而不发给原生桥。
  会话关闭即撤销令牌并让等待中的调用失败。兼容层接口 `launch` 增加 `McpEndpoint` 参数，Pi 忽略。
  `ehai-hub` 新增 `--public-url`。核心代码无改动。
- 验证（仓库外诊断）：模拟一个讲 MCP 的 harness（假兼容层收到任务后用官方 `mcp` 客户端调用端点），核心为真实
  `HubRoleRunner`：列出工具只有该会话的两个；可恢复错误、正常结果、结束工具往返正确，核心轨迹完整；未知工具被拒；
  会话关闭后原令牌、Hub 令牌、无令牌均为 401。产品 E2E 与独立 Hub 的 7 个对比场景无回归。
- 评审发现（Codex）：每个 MCP 调用被当作单独一批，结束工具与同轮其他调用一起时仍被接受；令牌在 `launch` 返回后才生效，
  启动中连接 MCP 得到 401；核心接受结束后端点仍转发后续调用，使有效完成变成结果未知。修复：按兼容层报告的模型消息分批；
  启动前登记令牌（可列工具、拒绝调用，失败即撤销）；接受结束后由 Hub 拒绝后续调用。先在修复前的提交上用同一诊断复现三项，
  修复后：启动中列出工具成功、同轮 probe + finish 时 finish 被拒、单独 finish 被接受、之后的调用被 Hub 拒绝，核心正常完成。
- 未覆盖：真实 MCP harness；多会话并发下的令牌隔离只经代码路径确认，未单独运行。

### 2026-10-02 第三步：幂等回执合并

- 合并前有四套实现：核心 `command_receipts`，以及 `workflow_receipts`、`connector_receipts`、`routing_receipts`
  三张表和各自的 `receipt/remember` SQL（计划原文写"至少三处"，路由实验是第四处）。
- 合并后是一张表、一个存储类、一个应用层入口：
  - 表 `command_receipts` 的主键改为 `(scope, idempotency_key)`。`scope` 是键的命名空间：核心命令为 `command`，
    Workflow 为 `workflow`，Connector 与路由实验为 `connector:<原 scope>`、`routing:<原 scope>`。
    每个入口的键仍只在原来的范围内唯一，同一个键可以分别用于核心命令和 Workflow。
  - `CommandReceipt` 增加 `scope`；`CommandReceiptStore.get` 增加 `scope` 参数（默认 `command`，核心调用点不变）。
    Workflow、Connector、路由实验的事务接口去掉 `receipt/remember`，改为提供同一个 `receipts` 存储。
  - [idempotency.py](../src/ehai/application/idempotency.py) 的 `recorded_result` / `record_result` 负责查找、
    比对命令名与指纹、记录结果；核心服务、便签和上述三个服务都经它使用回执。键被另一条命令或另一份内容占用时
    抛出什么错误由调用方传入，因此各入口的 HTTP 状态码、`error.code` 和错误文字都没有变。
- Schema 22 → 23（只前进）：四张表的行复制到新表后删除旧表，在同一个迁移事务内完成。Workflow 回执原先保存整个请求
  JSON 作为比对依据，迁移时换成它的 SHA-256；Workflow、Connector、路由回执原先没有时间，迁移后 `created_at` 为空，不补造。
  升级后的数据库不能再由旧版本程序打开。
- 未改动：StartRun 重放（含历史 CLI 授权的兼容分支）和定向挂起的专用判断仍直接读取回执；`worker_event_receipts`
  是事件去重，不属于命令回执。
- 验证（仓库外诊断，未提交）：先在改动前的 main 上启动 `ehai-api`，经 HTTP 执行 25 个操作，在四张表各写入回执
  （核心 7、Workflow 6、Connector 5、路由 5 行）：核心的创建项目/目标，便签的创建/回复/决定，Workflow 的全部 5 个命令，
  Connector 的全部 5 个命令，路由实验的 create / pause / change-project / fallback / submit，以及同一个键用于另一条命令、
  同一个键分别用于核心命令和 Workflow。然后把这个数据库复制两份，分别用改动前和改动后的程序打开（后者迁移到 schema 23），
  对每个操作做同键同内容重放和同键不同内容的误用：两边的状态码与响应体逐字节相同。迁移后旧表不存在，新表 23 行；
  之后的新命令及其重放正常。把迁移中 Workflow 指纹的哈希去掉后，诊断在 Workflow 重放处失败。
  产品 E2E（连续 3 次，无遗留进程）、ruff、format、mypy（两个平台）、lint-imports 通过；API Schema 与 TS Client
  重新生成无差异，TS 类型检查与构建通过。本次在 macOS 上运行，此前的证据来自 Linux 与 Windows；
  PR 的 CI（静态检查与契约、Linux E2E、Windows E2E）通过。
- 未覆盖：真实使用中的旧数据库（只验证了诊断生成的 schema 22 数据库）；路由实验的 resolve / feedback / propose /
  replay / publish 五个命令的重放（需要 Jev 连接器的实际往返，与已验证的命令共用同一段代码）。

## 待决事项

| 事项 | 说明 |
| --- | --- |
| `--worker-timeout-seconds` | 原只用于 Codex Worker，现无使用方；参数与多工作区登记字段保留以免破坏接口，是否删除待定 |
| CLI 启动 scripted 宿主 Run | `ehai --api-url ... start-run` 要求 `--execution-config`，而执行配置只接受 pi；scripted 宿主只能经 HTTP 启动。E2E 暂用 HTTP，是否调整 CLI 待定 |
| 幂等重放返回值 | 重复人工判定返回当前 Run 状态而非原回执；如需原回执语义需单独设计 |
| 幂等键冲突的错误码 | 同一个键用于不同内容时，核心命令返回 409 `conflict`，Workflow/Connector/路由返回 409 `state_conflict`，便签返回 422 `invalid_request`；回执合并时按"保持行为"原样保留，是否统一待定 |
| 便签命令重放的响应 | create-note / add-note-message / decide-note 同键重放返回便签的当前状态，不是当时的响应：没有首次响应中的 `stale_reason: null`，便签之后有回复或决定时内容也随之不同（合并前已如此）；与上一条同类，是否改为原回执语义待定 |
