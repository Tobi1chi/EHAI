# Infrastructure Architecture

`ehai.infrastructure` 实现 Application 层定义的 Port，并把供应商协议、文件系统和 SQLite 行为隔离在
Adapter 内。领域层和 Application 层不导入这些具体实现。

本页是已有 Adapter 索引。平台目标见 [Product Scope](../../../docs/PRODUCT_SCOPE.md)；
[Execution Model](../../../docs/EXECUTION_MODEL.md) 区分 Agent 框架、Connector、Worker 预设及活跃实例：
Connector 提供框架接入与实例执行管理，不能与 Worker、模型或职责混称。
Foundation 分支的工具 Provider/Role 需要正常入口装配与验收，不能以可注入构造器代替交付。
Agent 执行 Connector 管理 Session 和执行协议，未来服务/事件 Connector 管理外部输入输出，不强行
共用 Worker 协议。扩展应复用现有 Port，不提前搭建尚无调用方的插件或 Connector 抽象。

## Adapter 目录

| 文件或目录 | 职责 |
| --- | --- |
| [`pi_rpc.py`](pi_rpc.py)、[`pi_runtime.py`](pi_runtime.py)、[`pi_config.py`](pi_config.py) | 完整 Pi 公共 RPC、角色调用与显式配置；不自研模型请求和压缩。接线完成，真实模型链路待验证。 |
| [`codex_transport.py`](codex_transport.py) | Codex 子进程传输、超时、取消和有界输出的公共实现。 |
| [`host_tools.py`](host_tools.py) | Workspace read/list/search/patch、受控 command 与 `submit_candidate` 宿主工具，不是 Agent Loop。 |
| [`agent_traces.py`](agent_traces.py) | 宿主审计持久化及旧 Built-in 轨迹读取，不重建模型历史。 |
| [`sqlite/`](sqlite/) | Schema migration、Unit of Work、Repository、Event Log 和状态 codec。 |
| [`artifacts/`](artifacts/) | 文件系统 Artifact Store；按不可变 ID 保存并校验内容。 |
| [`checks/`](checks/) | Artifact、command 和 semantic Check Adapter。 |
| [`workers/`](workers/) | Fake、Pi、Codex CLI、Codex App Server Connector 及 WorkerAdapter→Runtime Connector 桥。 |
| [`workers/code.py`](workers/code.py) | `CodeRuntimeConnector`；在 Worker Connector 外包住实际代码 worktree 的准备、上游合并、快照和 diff 捕获。 |
| [`code_workspaces.py`](code_workspaces.py) | `GitCodeWorkspace`；固定 Run base commit，在 EHAI-owned Git worktree 中合并上游代码并保存不可变代码结果。 |
| [`planners/`](planners/) | Pi 与 Codex Planner Adapter；只返回 Application `PlanProposal`。 |
| [`workspaces.py`](workspaces.py) | Workspace Manager；分配 EHAI-owned Git worktree 或受控本地 Workspace，并管理 Lease 清理。 |

## Code Execution

`CodeRuntimeConnector` 实现现有 Runtime Connector 边界，不让模型文字或候选 Artifact 冒充实际代码。它在
`start` 前确认 Attempt 拥有 EHAI 分配的隔离 worktree，使用 `GitCodeWorkspace` 固定 Run 的 base commit，
把依赖节点和已选择分支的 `GitCodeResult.commit` 合并进当前 worktree；上游缺失或 Git 冲突会阻止继续。

候选事件到达时，Connector 从真实 worktree 调用 `capture_result`，生成受 host 控制的代码快照和 `solution.patch`。
`GitCodeResult` 同时记录 repository、worktree、base/结果 commit、diff 路径、变更路径和摘要。关闭、恢复和
最终交付保留可核对的 worktree 与 metadata；最终获批行为 Gate 运行在最终 integration 节点准备好的实际合并
工作区，不运行在模型的文本报告上。

Planner 的有限图/Agent 预算不等于 Worker 的执行预算。默认 Built-in Worker 使用
`LONG_RUNNING_AGENT_BUDGET`；Runtime 的 no-progress 和绝对 deadline 也可保持未设置，长任务仍由取消、
heartbeat、显式 deadline、Provider 终态和恢复策略收敛。

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

旧集成、契约和专项 Smoke 已退役。Adapter 能力先接入正常入口，实际使用失败时才在仓库外临时定位；
最终只保留用户确认的一条产品 E2E，不新增长期 Adapter 测试矩阵。见 [测试策略](../../../tests/README.md)。
