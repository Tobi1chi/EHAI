# 过程调整读取已完成成果

状态：设计（2026-10-04，用户确认先做）。起因见 [PLANNER_CONSTRAINTS_DESIGN](PLANNER_CONSTRAINTS_DESIGN.md#2026-10-03-规划实验修复前)
中"先粗后细"两组。

## 观察到的问题

先粗后细的做法是：第 1 阶段只做调研与设计，产出设计文档；之后用批准内过程调整（`propose-process`）
把后续阶段细化成具体节点。2026-10-03 实验中两组都没能细化成功：

- P2：过程规划连续 8 次提出同一张便签，说第 1 阶段的产出 `docs/design-exports.md` 不在工作区里，
  无法"按该设计"细化，草稿失败。该工作节点在草稿开始前 2.5 分钟已完成，文档就在它的候选成果中。
- P1：草稿 ready，但过程审查失败，报告为 harness 结果未知。2026-10-04 查明这是审查工具 Schema 被 Pi
  严格模式拒绝（对象联合），审查在真实 Pi 上从未运行过，与本问题无关，另行修复
  （[REFACTOR_PLAN](REFACTOR_PLAN.md#2026-10-04-过程审查工具被-pi-严格模式拒绝)）。

原因：过程规划的输入只有已批准方案、当前过程图、冻结的 Check 与干预记录；它的只读工作区工具指向
项目工作区（Run 的基线），而节点成果在 EHAI 自有的 worktree 和 Artifact 中。过程审查反而能读到
被复用节点的候选 Artifact（`read_evidence_artifact`），两边不对称。

P2 的便签还指出第 1 阶段的人工 Gate 没有答复，这一点属实（驱动在人工检查点等待时发起调整）。
过程规划应当能看到 Check 的真实状态，把待决的 Gate 当作待决，而不是猜测。

## 决定

### 已完成成果清单

- 准备过程草稿时（`_prepare_process_draft`，同一事务内）由持久状态生成 `completed_outputs`，
  随 `propose_process` 传给 Planner，放入 `planner_input`：
  - 当前过程图中每个已完成节点：`plan_node_id`、标题、类型、最新成功 Attempt，以及该 Attempt 的
    候选类 Artifact 元数据（`artifact_id`、名称、类型、媒体类型、字节数）。代码节点的 `solution.patch`
    是该节点相对其输入的完整 diff，新建文档的全文在其中。
  - 每个节点的 Check 运行：Check 名称、类型、状态（运行中、等待人工、通过、失败）、失败原因与人工意见。
- 只列元数据，不内联内容；清单有上限（节点数与 Artifact 数），超出时截断并标明。
- 由复用（Result Adoption）完成、没有本 Run 成功 Attempt 的节点只列出节点和"已复用"，不列 Artifact
  （首版不展开复用来源）。

### 只读读取工具

- 过程模式下 Planner 新增 `read_completed_output`（`artifact_id`、`offset`、`limit`），只能读清单中的
  Artifact，分页返回并校验大小与 SHA-256；与过程审查的 `read_evidence_artifact` 共用实现。
- 不新增 worktree，不让 Planner 访问 EHAI 自有工作区，不改变"项目工作区是 Run 基线"的语义。

### 提示词

- 过程规划提示词说明 `completed_outputs` 与读取工具：细化依赖已完成成果的节点前先读相关 Artifact；
  清单中列出的成果不得声称缺失；等待人工的 Check 是待决事项，不能当作已通过，可在设计中说明依赖关系。

### 审查核对旧 ID 引用

- 2026-10-04 的回放中，两组草稿都读取了成果并完成细化，过程审查也首次在真实模型上完成，但都把
  Gate 范围判为"不确定"：Planner 在编译前写设计说明，用当前过程图中的节点 ID 指称 Gate 归属；被改动的
  节点编译后获得新 ID，审查看不到对应关系，按规则不能认可。
- 候选过程版本的 `block_changes` 已记录每个旧节点 ID 对应的编译后 ID。审查上下文的候选过程版本加入
  `block_changes`，审查提示词说明：经 `block_changes` 映射后与所述角色或 Gate 归属一致的旧 ID 引用视为一致；
  映射后仍不一致才判为不确定。不改设计说明文本，不放宽其他判断。

## 验收

- 静态检查、产品 E2E 通过；Schema 无差异（不改公开接口）。
- 仓库外诊断（不提交）：脚本化模型在过程草稿中调用 `read_completed_output` 读取已完成节点的
  `solution.patch` 并得到内容；读清单外的 ID 被拒绝。
- 真实模型回放（OpenCode Go `deepseek-v4.1-flash`）：重跑 cdls 先粗后细一组（同一驱动、同一隐藏评分）。
  期望草稿 ready 且细化节点引用设计文档的内容，过程审查通过后应用，Run 完成且评分满分。

## 不做

- 让过程规划读取 worktree 或整合预览（Artifact 已足够；需要时另议）。
- 展开复用来源的 Artifact。
- 改变过程审查的证据范围。
