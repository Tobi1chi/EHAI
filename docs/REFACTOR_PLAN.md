# 重构与推进计划

更新：2026-10-01。本文定义当前重构的顺序、边界和完成条件；
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
| 2. 低风险清理 | 代码只保留实际使用的后端和清晰命名 | Codex 去留已决定并执行；图 IR、脱敏工具移到中性模块；`legacy_config.py` 处置；Run/Routine 命名冲突有决定 | 进行中：Codex 已删除（选项 A），图 IR 与脱敏已移出；`legacy_config.py` 与命名冲突未处理 |
| 3. 拆分大文件 | 新贡献者能按职责定位代码 | Orchestrator 拆为门面与若干职责模块；Repository/Service 按聚合拆分；幂等回执合并为一个机制；E2E 全程通过 | 未开始 |
| 4. 收尾 P2 | 在真实项目上完成一次可核对的开发任务 | 真实 Pi 按[手动验收](#真实-pi-手动验收)完成并记录；随后在 P3.3 与 P4 通用 Workflow 中选一项 | 未开始 |

### 步骤 3 的拆分方向

Orchestrator 按代码中已存在的边界拆分，保留 `Orchestrator` 作为对外门面：

| 模块 | 内容 |
| --- | --- |
| 图就绪判断 | `ready_nodes`、分支终态/失败判断等纯函数，移入 `domain/` |
| Attempt 生命周期 | queue / start / retry / timeout / interrupt / fail |
| 校验与 Gate | Check 执行、`finish_pending_gate`、人工判定 |
| 分支评估 | Evaluator 候选校验、`_record_branch_selection` |
| 采纳与接续 | adoption、`accept_adopted_result` |
| 人工介入 | intervention、`waiting_for_*` |

`sqlite/repository.py` 与 `application/service.py` 按 plan / run / note / workflow / connector 聚合拆分。
幂等回执当前至少有三处实现（Workflow、Connector 的 `receipt/remember` 与核心 `CommandReceipt`），
合并为一个共用机制，供后续自定义 Workflow 复用。

## 产品 E2E

唯一产品 E2E 为 [tests/test_product_e2e.py](../tests/test_product_e2e.py)，运行：

```powershell
uv run pytest tests/test_product_e2e.py
```

它启动真实 `ehai-api` 宿主进程（scripted Worker、`single` Planner、`--p2-runtime`），
只通过 `ehai` CLI 与 HTTP API 操作，不读写数据库，不调用模型。

| 覆盖 | 内容 |
| --- | --- |
| 外部计划导入 | 两个独立任务、一个汇合节点、Reviewer 阶段；A 带人工 Gate，最终 Gate 执行宿主命令 |
| 批准与授权 | approve-plan 与 start-run 分开调用 |
| 并发与挂起 | A 等待人工判定时 B 完成，汇合节点保持 pending |
| 统一待办 | 人工请求出现在 Inbox，带可执行动作和 request token |
| 崩溃恢复 | 强杀宿主后在同一数据库重启；Run、节点状态、请求 token 保持不变 |
| 事件消费 | 未确认批次在重启后以相同 token 和事件重新返回，确认后前进 |
| 人工决定 | CLI 判定通过后 Run completed；同一幂等键重复判定被接受且不产生新请求 |
| 成果查询 | get-result 的 Check 均完成；get-project 只列出该 Run 并带已验证成果 |

不覆盖：真实模型调用、Pi Planner、Git worktree 与 `integrate-run`、MCP、生活 Workflow、
Connector 和 Jev 实验。这些不能由本 E2E 推断已验证。

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
  HTTP 执行配置只接受 `worker_kind: pi`。
- 验证：ruff、format、mypy（两个平台）、Schema/Client 重新生成两次结果一致、TS 构建、E2E 通过。

## 待决事项

| 事项 | 说明 |
| --- | --- |
| `--worker-timeout-seconds` | 原只用于 Codex Worker，现无使用方；参数与多工作区登记字段保留以免破坏接口，是否删除待定 |
| CLI 启动 scripted 宿主 Run | `ehai --api-url ... start-run` 要求 `--execution-config`，而执行配置只接受 pi；scripted 宿主只能经 HTTP 启动。E2E 暂用 HTTP，是否调整 CLI 待定 |
| 幂等重放返回值 | 重复人工判定返回当前 Run 状态而非原回执；如需原回执语义需单独设计 |
