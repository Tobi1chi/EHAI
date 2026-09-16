# Infrastructure Architecture

`ehai.infrastructure` 实现 Application 的 Port，将供应商协议、文件系统和 SQLite 隔离在 Adapter 内。
核心不复制 Pi 的模型循环或原生历史；接入及恢复语义见 [执行模型](../../../docs/EXECUTION_MODEL.md)。

| 文件或目录 | 职责 |
| --- | --- |
| [pi_rpc.py](pi_rpc.py)、[pi_runtime.py](pi_runtime.py)、[pi_config.py](pi_config.py) | Pi 公共 RPC、角色调用、显式配置与指纹 |
| [pi_business_tools.mjs](pi_business_tools.mjs) | Pi 扩展与宿主业务工具之间的 notification 桥 |
| [host_tools.py](host_tools.py)、[agent_traces.py](agent_traces.py) | 宿主工具、受控执行与轨迹持久化 |
| [sqlite/](sqlite/) | Migration、Unit of Work、Repository、事件与 codec |
| [artifacts/](artifacts/)、[checks/](checks/) | Artifact 内容存储及 Check Adapter |
| [workers/](workers/)、[planners/](planners/) | Worker/Planner 后端适配；当前首选 Pi |
| [workers/code.py](workers/code.py) | Worker 工作区准备与实际代码候选捕获 |
| [code_workspaces.py](code_workspaces.py)、[workspaces.py](workspaces.py) | EHAI-owned 工作区、固定基线、上游成果合并与隔离 |
| [codex_transport.py](codex_transport.py) | 仍保留的 Codex 子进程传输适配 |

代码成果来自真实工作区快照，不能由模型文字冒充。Git 冲突或上游缺失应返回明确问题；
候选、整合 commit 与 Gate 证据保留对应关系。用户工作区和隐式换行转换的处理见执行模型。

配置与凭证显式传入；Pi 不是安全沙箱。宿主工具仍校验路径、调用参数和授权。
SQLite 事务提交状态、回执与事件；重复事件不应重复推进状态。未知副作用不能靠重新调用掩盖。
连接器恢复、租约收敛和长任务保证的实际验证范围以 [STATUS](../../../docs/STATUS.md) 为准。

未来业务连接器围绕真实场景实现，不预建没有调用方的通用插件底座，也不强行套用 Worker 协议。
正常入口和诊断约束见 [开发指南](../../../docs/DEVELOPMENT_GUIDELINES.md)。
