# ADR 0003：P1 Codex 本地进程通道

- 状态：已接受（成功调用需有效本地认证）
- 日期：2026-08-31
- Roadmap：P1（I0、I5、I6）

## 目的

固定 P1 `Codex External Worker Connector` 的本地调用边界，并决定 Planner 是否复用同一传输实现。该决策只选择通道，不提前实现完整 Adapter，也不赋予 Codex 判断节点或 Goal 完成的权限。

## 决策

P1 必须通过本机 `codex exec` 非交互式子进程调用 Codex。Connector 使用 Python 异步子进程 API 直接传递参数，不经过 shell；prompt 通过 stdin 发送，事件通过 `--json` JSONL stdout 增量读取，诊断日志从 stderr 单独捕获。结构化最终结果使用受控的 `--output-schema`，并可用 `--output-last-message` 保存最后消息作为辅助捕获，不得只解析人类可读终端文本。

每次调用必须：

- 指定受控工作目录与最小所需 sandbox；默认不得使用 `--dangerously-bypass-approvals-and-sandbox`。
- 非交互运行并明确 approval policy；不得因等待人工 TTY 提示而无限挂起。
- 使用 P1 自己的 wall-clock timeout 和输出大小上限；CLI 内部重试不替代 Orchestrator 的 Attempt 策略。
- 将 stdout JSONL、stderr、退出码、超时或取消原因映射为统一 `Event`/`Artifact`；写入前清理凭证、环境变量和其他秘密。
- 在取消时先终止子进程并限时等待，随后强制结束仍存活的进程树。Windows 上不能假定 POSIX signal 语义。
- 把 Codex 输出视为候选结果。Connector 不得将 `PlanNode`、`Run` 或 `Goal` 标记为完成；Checker 和 Gate 仍是完成状态的唯一入口。

Planner 在 P1 **复用同一个 `codex exec` 进程传输实现和本机认证配置**，但必须通过独立的 `Planner` Port 暴露，使用独立 prompt、输入 Schema、输出 Schema、预算、超时和事件映射。`WorkerAdapter` 与 `Planner` Port 不得互相继承领域职责，也不得共享可变会话状态。默认每次调用使用新的非交互进程；P1 不依赖 session resume。

自动化测试必须使用受控假进程，不访问真实 Codex。真实 Codex 测试必须是显式启用的 smoke test，并在无有效认证时跳过或给出清晰失败原因。

## 技术验证依据

2026-08-31 在 Windows PowerShell、`codex-cli 0.147.0` 上进行了限时验证。结果记录在 [Codex CLI 本地通道技术验证](../spikes/codex-cli-local-channel.md)：

- CLI 可启动，并提供 `exec`、stdin、`--json`、`--output-schema`、`--output-last-message`、sandbox 与 ephemeral 参数。
- stdin prompt 被接收，stdout 可捕获结构化的 `thread.started`、`turn.started`、错误项和 `turn.failed` JSONL；失败以非零退出码返回。
- 交互 PTY 中按 Ctrl+C 能终止进程并得到非零退出码。
- 本机保存的 API key 在验证时返回 HTTP 401，因此没有验证成功 final payload；这属于运行环境认证前置条件，不改变进程通道可行性。I5 的真实 smoke 必须在有效认证下补验成功路径。

## 结果与边界

- 选用稳定、可观测的本地进程边界，避免把 Codex SDK 或协议细节泄漏到 Domain。
- Planner 与 Worker 复用传输代码减少 P1 配置面，但仍保持可替换的独立应用接口。
- 每次新进程有启动成本；P1 单 `Attempt` 顺序执行可以接受，常驻服务留到有证据需要时再评估。
- CLI JSONL 事件格式和退出行为属于外部边界，Adapter 必须宽容读取未知事件类型并对缺少必需最终结果采取 fail-closed。
- `--ephemeral` 适合无持久会话的验证和默认 P1 调用；EHAI 的 `ExecutionTrace`、Event 和 Artifact 才是可恢复记录。

## 未选择方案

- **Codex MCP/app-server/exec-server**：均扩大协议和生命周期管理面，P1 没有常驻双向会话需求。
- **交互 TUI 自动化**：输出不稳定、取消和输入控制复杂，不适合作为 Adapter 协议。
- **Planner 单独模型 Provider**：增加凭证、错误模型和测试矩阵；P1 尚无证据证明有必要。
- **复用 Codex session**：会引入隐式上下文和恢复耦合，不利于可重放的独立 Attempt。

## 可逆性

进程细节必须封装在 infrastructure Adapter。`Planner` Port 和 `WorkerAdapter` 分别由 application 层定义，未来可以为任一角色替换 Provider，而不改变 `PlanGraph`、`ExecutionTrace`、Checker、Gate 或 Checkpoint 语义。
