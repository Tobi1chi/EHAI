# ADR 0004：P1 Evaluator 选择协议与 Artifact 输入快照

- 状态：已接受
- 日期：2026-09-01
- Roadmap：P1（I5、I6）

## 目的

修正 P1 真实 Demo 暴露出的语义偏差：Evaluator 生成的 Artifact 必须实际决定分支选择，依赖 Worker
必须能读取前序 Artifact 的受控内容，而不能只看到不可访问的 Artifact Store 元数据路径。

## 决策

P1 Worker 输入使用不可变 `ArtifactInputSnapshot`。快照包含 Artifact ID、PlanNode ID、名称、媒体
类型、大小、SHA-256、编码和内容；不暴露 Artifact Store `relative_path`。UTF-8 内容直接传递，非
UTF-8 内容使用 base64。P1 输入预算固定为单 Artifact 256 KiB、总输入 1 MiB；超过预算必须
fail closed。

Evaluator PlanNode 仍通过 Worker 返回普通候选 Artifact，但该 Artifact 的内容必须是结构化
BranchSelection proposal。proposal 至少包含 `selected_branch_id`、`pruned_branch_ids`、
`criterion`、`explanation`、`compared_artifact_ids` 和 `selected_artifact_ids`。

Orchestrator 是唯一状态转换入口。Evaluator 完成并通过本节点 Gate 后，Orchestrator 读取其持久化
Artifact，严格解析 proposal，验证 Branch、Artifact、Run 和 PlanRevision 范围，确认比较证据覆盖
所有可行候选分支，并只将 `selected_artifact_ids` 对应的选中分支快照传给 Merge。Evaluator 不得直接
修改 Branch 状态。`DeterministicBranchEvaluator` 仅保留为显式注入的测试替身。

## 结果与边界

- BranchSelected/BranchPruned Event 记录实际 criterion、explanation、比较证据和选中输出引用。
- 普通依赖 Worker 和 Evaluator 都能看到前序 Artifact 内容快照；Merge 只能看到选中分支内容。
- 失败分支没有候选 Artifact 时可以被剪枝；Evaluator 不需要比较不存在的内容。
- P1 不引入多 Worker Registry、并发调度、插件式 Check Runner 或通用上下文摘要系统。

