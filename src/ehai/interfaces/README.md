# Interfaces Architecture

`ehai.interfaces` 将 CLI、HTTP、MCP 和 SSE 交互转换为应用 Command/Query 与公开 JSON。
状态、批准和调度归核心；具体命令见 [Usage](../../../docs/USAGE.md)，证据见 [STATUS](../../../docs/STATUS.md)。

| 文件 | 职责 |
| --- | --- |
| [cli.py](cli.py) | ehai 命令解析与本地入口 |
| [cli_api.py](cli_api.py) | 共用查询/命令路由及 HTTP 客户端，不回退直写数据库 |
| [session_host.py](session_host.py) | execute-plan / resume-session 前台宿主 |
| [api.py](api.py)、[http_models.py](http_models.py) | HTTP 路由、严格请求、错误与响应 |
| [mcp_server.py](mcp_server.py) | 独立 stdio MCP，转发已支持的 HTTP 操作 |
| [public_events.py](public_events.py)、[public_documents.py](public_documents.py) | 公开事件与文档编码 |
| [sse.py](sse.py) | Event ID 游标与可恢复 SSE |
| [runtime.py](runtime.py) | ehai-api 的数据库、角色、Runtime 和服务装配 |

HTTP 宿主拥有后台任务，start-run 受理不表示完成；客户端退出不取消宿主任务。
本地 execute-plan 是前台宿主，两者共用核心执行语义。MCP 不启动 API，
启动时开放全部已支持的查询和写工具；实际业务授权与批准由核心校验。

规划、导入、人工回复、定向挂起、过程调整、Git 整合和后继 Run 已有入口；
各传输覆盖范围以实际路由和 Usage 为准，不声明所有 HTTP 端点均有 CLI/MCP 对应项。
事件分页/SSE 不等于完整外部 Agent 消费信箱。

后续总览、统一待办和 Web 工作台按 [路线图](../../../docs/ROADMAP.md) 增量实现，
只通过公开契约调用；新增能力必须进入生产装配，不能靠外部脚本补业务步骤。
