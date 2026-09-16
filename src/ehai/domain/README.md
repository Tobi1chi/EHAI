# Domain Architecture

`ehai.domain` 拥有执行对象、合法状态转换与不变量，不依赖数据库、HTTP 或 Agent SDK。
Application 编排这些对象，接口与未来 UI 不另行维护状态规则。
业务语义见 [执行模型](../../../docs/EXECUTION_MODEL.md)，验证范围见 [STATUS](../../../docs/STATUS.md)。

| 文件 | 职责 |
| --- | --- |
| [goal.py](goal.py) | Project、Goal、CompletionContract 与确认关系 |
| [planning.py](planning.py) | PlanRevision、PlanNode、Phase、Edge、Branch 与图校验 |
| [execution.py](execution.py) | Run 与 Attempt 生命周期 |
| [process.py](process.py)、[process_drafts.py](process_drafts.py)、[blocks.py](blocks.py) | 过程版本、草稿及 block 身份/变更 |
| [adoptions.py](adoptions.py) | 成果接续与来源记录 |
| [workers.py](workers.py) | WorkerProfile、Endpoint、Session/Execution 引用 |
| [workspaces.py](workspaces.py) | 工作区与 Lease |
| [artifacts.py](artifacts.py) | 不可变产物元数据、归属与内容摘要 |
| [checking.py](checking.py) | Check、Gate 决定和可恢复 Checkpoint |
| [events.py](events.py)、[runtime.py](runtime.py) | 已发生事件与持久 DispatchWork |

Worker 提交候选，核心依据成果与 Check/Gate 推进状态；中间交接不满足最终 Goal。
Lease 释放、进程退出或 Session 结束也不等于业务完成。
后继 Run 保留来源，旧成果适用性与新 Gate 重新核对，不复制历史成功状态。

Personal Dashboard 的术语只作产品参考：其单 Task Execution Run 不等于这里的 Run，
其页面 Block 不等于计划 block。未来工作台必须展示本核心的真实状态与来源。
