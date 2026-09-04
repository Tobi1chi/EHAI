# Infrastructure Architecture

`ehai.infrastructure` 实现 Application 层定义的 Port，并把供应商协议、文件系统和 SQLite 行为隔离在
Adapter 内。领域层和 Application 层不导入这些具体实现。

## Adapter 目录

| 文件或目录 | 职责 |
| --- | --- |
| [`openai_responses.py`](openai_responses.py) | OpenAI Responses `ModelClient`，负责严格 Tool Schema、streaming、响应续接和兼容回放。 |
| [`codex_transport.py`](codex_transport.py) | Codex 子进程传输、超时、取消和有界输出的公共实现。 |
| [`builtin_tools.py`](builtin_tools.py) | Workspace read/list/search/patch、受控 command 与 `submit_candidate` Tool Runtime。 |
| [`builtin_sessions.py`](builtin_sessions.py) | Built-in Agent Session Event 的 SQLite 持久化与重放。 |
| [`sqlite/`](sqlite/) | Schema migration、Unit of Work、Repository、Event Log 和状态 codec。 |
| [`artifacts/`](artifacts/) | 文件系统 Artifact Store；按不可变 ID 保存并校验内容。 |
| [`checks/`](checks/) | Artifact、command 和 semantic Check Adapter。 |
| [`workers/`](workers/) | Fake、Built-in、Codex CLI、Codex App Server Connector 及 WorkerAdapter→Runtime Connector 桥。 |
| [`planners/`](planners/) | Built-in Responses 与 Codex Planner Adapter；只返回 Application `PlanProposal`。 |
| [`workspaces.py`](workspaces.py) | Workspace Manager；分配 EHAI-owned Git worktree 或受控本地 Workspace，并管理 Lease 清理。 |

## 安全、资源与恢复边界

- Built-in `command` 默认不注册。启用时按完整 argv 精确匹配允许列表，固定可信 executable，不经过
  shell，并使用不含 API 凭证的最小子进程环境。取消、超时和输出超限会清理进程树。
- Workspace Tool 在读取内容前执行路径约束和大小预检；list/search/patch 对条目、文件数、扫描字节、
  匹配数、返回体和写入范围设置硬限制。Worker 输入、候选 Artifact、日志和诊断同样有界。
- SQLite transaction 同时提交状态、Command receipt 和领域 Event。Worker Event receipt 以稳定 ID
  持久化，重复重放不会再次推进 Attempt 或重复创建 Artifact。
- Artifact Store 只保存内容；Run/Attempt/PlanNode 归属和摘要由领域元数据决定。Check Adapter 只返回
  Check 结果，不能直接通过 Gate。
- Connector 的 `recover` 只恢复已有 provider execution/session 或报告不可恢复；它不猜测业务终态。
  Runtime 根据持久 DispatchWork、Attempt、cursor、deadline 和 Lease 决定重连、重试、暂停或失败。
- Workspace 清理与 Lease 释放必须在成功、失败、取消和恢复路径收敛；不能把“外部进程已退出”当成
  Run 已完成。

对应验证见 [`tests/integration/test_sqlite_uow.py`](../../../tests/integration/test_sqlite_uow.py)、
[`tests/integration/test_artifact_store.py`](../../../tests/integration/test_artifact_store.py)、
[`tests/unit/test_check_adapters.py`](../../../tests/unit/test_check_adapters.py)、
[`tests/integration/test_codex_worker.py`](../../../tests/integration/test_codex_worker.py)、
[`tests/integration/test_codex_planner.py`](../../../tests/integration/test_codex_planner.py)、
[`tests/integration/test_workspace_sessions.py`](../../../tests/integration/test_workspace_sessions.py) 和
[`tests/contract/test_p2_workers.py`](../../../tests/contract/test_p2_workers.py)。
