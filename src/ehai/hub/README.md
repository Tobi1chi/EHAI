# Agent harness Hub

`ehai.hub` 是核心运行 Agent harness 的唯一入口，作为独立进程 `ehai-hub` 运行。
用法、协议与失败处理见 [HUB](../../../docs/HUB.md)，设计见 [ADR 0007](../../../docs/adr/0007-agent-harness-port.md)。

| 文件或目录 | 职责 | 谁可以导入 |
| --- | --- | --- |
| [protocol.py](protocol.py) | 协议 v1 消息（纯 JSON） | 核心、Hub |
| [client.py](client.py) | 核心侧 HTTP 客户端；未配置 `EHAI_HUB_URL` 时启动本机 Hub 子进程 | 核心 |
| [server.py](server.py) | `ehai-hub` 服务：会话表、长轮询事件、令牌认证、空闲回收 | 仅 Hub |
| [mcp_endpoint.py](mcp_endpoint.py) | 每会话 MCP 端点：会话令牌、只暴露该会话工具、调用转为 tool_call 事件 | 仅 Hub |
| [adapters/](adapters/__init__.py) | 兼容层接口 | 仅 Hub |
| [adapters/pi/](adapters/pi/adapter.py) | Pi 兼容层：RPC 进程、业务工具桥、配置与探查 | 仅 Hub（`config`、`probe` 为过渡期例外） |

导入方向由 `uv run lint-imports` 检查（`pyproject.toml` 中的两条约定）。
Hub 不保存 EHAI 事实，不执行业务工具，也不判定任务完成。
