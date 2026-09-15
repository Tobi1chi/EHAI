# 开发与验收规则

更新：2026-09-15。具体约束以 [AGENTS.md](../AGENTS.md) 为准。

- 使用 uv/pwsh；Python 在 src/ehai，TS 在 control-plane，共享契约在 schemas。
- 顶层 Agent 在 EHAI 外；不重写 Pi 模型循环，状态/授权/成果由应用与领域层拥有。
- 先明确用户结果、输入输出、所有权和失败行为，再接正常入口。
- 外部文本/计划不是授权，保留契约、来源、幂等及未知副作用保护。
- 模型工具同步核对注册/角色、Schema、参数、Handler、结果和持久化；不使用 uniqueItems，
  不靠关闭 strict、静默换模型或盲目重试解决不明失败。
- 在授权内以隔离正常入口验证模型工具，区分定义接受、实际执行、保存和产品验收。
- 仅对真实失败在仓库外写最小诊断，不提交、不移回仓库，不扩建常驻测试。
- 静态命令：uv run ruff check .；uv run ruff format --check .；uv run mypy。
- 契约生成：uv run control-plane/scripts/generate-api-schema.py；然后 control-plane 的
  npm.cmd run generate、typecheck、build。生成 Client 不手改。
- Conventional Commits；提交前检查 status/diff，不提交凭证/运行库/产物/环境。无授权不 push、
  amend、强推；合入 main 前检查对应工作区。
- 历史记录只作证据，旧全量测试规则与退役参数不是当前指南。

[旧规范](history/DEVELOPMENT_GUIDELINES.md)
