# 主线与迁移状态

更新：2026-09-15。本批整理周期审查/单 Worker 挂起、草稿 Gate 修复、外部导入、CLI/MCP 和文档，
合入本地 main，不推送远端；不改写旧 Run、批准、Artifact 或历史事件。

功能接口提交为 7af6876，Git 字节一致性修复为 90cfab0；文档收口与代码同批进入 main。

当前支持阶段/过程与成果查询结构，不能用旧 M1/schema 11 迁移边界降级数据库。
builtin 仅保留必要历史兼容词汇，旧执行授权不能直接变成 Pi。

内部图模块移至 application/plan_graph_tools.py，Planner 和导入共用。
新增 ImportPlan、导入 Schema、TS Client 方法、ehai-mcp；MCP SDK 1.x 及实际版本由 uv.lock 固定，
JSON Schema 校验成为运行依赖。历史文档归档，正常操作以 [Usage](USAGE.md) 为准。

[能力与证据](STATUS.md) · [旧迁移记录](history/MIGRATION_STATUS.md)
