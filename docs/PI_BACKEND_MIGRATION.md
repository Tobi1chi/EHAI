# Pi 当前接入状态

更新：2026-09-15。[历史迁移过程](history/PI_BACKEND_MIGRATION.md) 不作为当前用法。

完整 Pi 0.85.1 经 stdio RPC 接入 Planner、Worker、阶段 Reviewer、过程 Planner/边界 Reviewer。
自研模型循环、Responses Adapter 和 Python OpenAI SDK 已退役。
EHAI 保留业务工具、权限、候选/审查结果校验、调度和 Gate。

私有 backend/settings/models 显式配置并保存指纹，不自动加载全局扩展、认证文件或项目插件。
prompt ACK 不等于完成；需宿主接受结果和原生 agent_settled，再记录角色完成。
断线/中断未知结果不自动重放；业务幂等键不等于模型请求幂等。
模型工具不含 uniqueItems，业务扩展经公开 notification 通道交互，不私写被接管的 stdout。

真实 Luna/Go 运行证据已存在，旧“尚未调用过真实模型”已不适用；覆盖范围见 [STATUS](STATUS.md)。
逻辑 Phase Session 不等于共用物理 Pi 对话。缓存不保证永远命中，需区分原始零值与字段缺失。
费用投影/估算不等于账单或已落实的全角色硬限额。
