# 当前执行接口契约

更新：2026-09-15。具体字段以 [OpenAPI](../schemas/v1/http-api.openapi.json)、
[Commands](../schemas/v1/commands.schema.json)、[Queries](../schemas/v1/queries.schema.json) 为准。

- Planner 与外部导入都生成草稿；导入不接受状态、批准或真实执行/Gate ID。
- 批准与 execution_config 授权分开；配置必须匹配宿主。
- API start-run 受理不是完成；通过 get-run/get-trace/get-result 查询。
- 过程提案/审查异步受理后查询真实状态，再决定应用。
- 人工回复使用当前 request token；MCP 写操作开关不自动批准具体任务。
- CLI/MCP 不复制领域规则，不直接写数据库；HTTP API 当前无新增认证层，默认回环使用。
- 启动客户端不启动后台服务，客户端退出不自动取消任务。

[执行模型](EXECUTION_MODEL.md) · [历史契约](history/P2_EXECUTION_CONTRACT.md)
