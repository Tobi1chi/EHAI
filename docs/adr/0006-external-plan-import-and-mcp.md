# ADR 0006：外部顶层 Agent、计划导入与 MCP

- 日期：2026-09-15
- 状态：已接受，入口已实现；验证范围见 STATUS
- 决策：顶层 Agent 在 EHAI 外，Planner 可选，CLI/MCP 是同一核心的入口。

声明式导入使用 Planner 相同图 Schema、验证和 PlanTemplate Builder，不复制状态规则。
初始导入只产生待批准计划；既有批准不得通过导入覆盖。
HTTP、CLI 与 TS Client 同步生成导入契约。

MCP 独立 stdio 进程只转发已有 HTTP API，不直接写库、启动调度或维护模型循环。
使用官方 MCP SDK 1.x 的维护接口，依赖上界 <2，实际锁定版本见 uv.lock；不手写协议。
2026-09-16 调整：启动即开放全部已支持的查询和写工具，移除 MCP 读写开关。
方案批准、执行授权和状态校验仍由 HTTP 宿主负责。写工具接收 HTTP request_json，
通过 get_request_schema 按需取契约，减少重复模型上下文并保留 HTTP 作为参数真相来源。
没有根据错误切换 Schema、关闭模型 strict 或隐式重试。

MCP 初始化需可用且兼容的 HTTP 宿主；不承担 API 启动/认证/多租户/任意 URL 代理。
中断客户端等待不保证取消已发出的 HTTP 操作，未知结果先查询。具体操作仍需用户授权。
