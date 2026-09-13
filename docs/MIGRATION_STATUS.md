# 成果迁移记录

## 本次边界

从 main `9a7017e93ca8b8e7b5036ee77e5145a0fcf56a80` 建立独立分支
`codex/stabilize-core`，只按用户可用能力迁移及验证。不复制旧工作区全部未提交代码，
不改变原自举 Run、goal 或数据库，不删除旧源码/试验成果，不推送。

稳定分支不是自举完成声明。旧的两代自举验收仍未完成；此处验证的是功能迁移。

## M1：共享结果查询、HTTP 和生成 Client

- 来源：第一轮交付 `2dcd2b9112abd1bf8e96a6b8475bd9a651f58875`，隔离根基线
  `929174496aad012610b01024351725065b980def`；独立 Git 历史，采用功能移植而非伪造 fast-forward。
- 用户结果：从 CLI、HTTP、生成 Client 查看同一份代码交付及检查证据。
- 输入：run_id、宿主 artifact_root；输出：RunResultDocument/RunResultResponse。
- 所有权：QueryService 在一个 ReadSession 物化事实；application/run_results.py 纯投影；
  CLI/HTTP 只编码或封装，Client 从 Schema 生成，不复制执行规则。
- 失败：保留未知 Run、非法 UUID、存储及执行配置冲突错误；没有交付/检查不是自动通过。
- 迁移的语义修复：检查按交付 Attempt 绑定；无对应检查为 null；false/unknown/true 正确聚合。
  无交付保留历史 fallback。旧 CLI 调用方需处理 check_result=null。
- 主实现 run_results.py 原样迁入；trace 提取适配 main 的 `_run_view(run)`，未带入试验版
  Run 预算/后继字段或 ProcessRevision/Phase 模型。
- 依赖闭包：queries、session_host、api、runtime、两份公开 Schema、生成器与生成 Client。
  无数据库迁移、执行器修改、新依赖或全局配置修改。

### 2026-09-14 已执行验证

- `uv sync --frozen` 成功；Ruff check、Ruff format --check、mypy src 均通过。
- `npm ci`、generate、typecheck、build 通过；重复 generate 的 SHA-256 不变，生成代码不手改。
- 所有 JSON Schema 自检通过；真实 HTTP 返回通过 RunResultResponse Schema 验证。
- 复用既有唯一 E2E 的冻结第一轮行为驱动 `self_hosting_e2e.py`，SHA-256
  `429d51d1b7bd7671add402c54571319ae3019e75cb07bc3fc9b4d67f72b6bc14`，未修改驱动或新增永久测试。
- 真实历史 fixture：Run `104427ed-4c75-4943-ada6-19dd9f6f7d88`，schema 11，源目录
  `%TEMP%/ehai-node-gates-4311a309e53747ebb4978e3a12bb30a7`。原模型编码及四 Gate 的历史记录
  仍见原 R2 实施记录。验证通过只读源库的 SQLite backup，在副本上运行正常 CLI/API。
- 驱动 `verify-result --workspace D:/workspace/EHAI-stabilize --database <fixture>/state.sqlite3
  --artifacts <fixture>/artifacts --run-id 104427ed-4c75-4943-ada6-19dd9f6f7d88` 通过。
  验证了正常 CLI、实际 HTTP 服务、生成 Client 默认 fetch 的深比较，完整 CheckRun 对应真实
  交付 Attempt/通过 Gate，以及未知 Run CLI/HTTP404/Client EhaiApiError；驱动结束时关闭 API 进程。
- 本次通过证据：`%TEMP%/ehai-result-gate-vkrkkt3m`。没有模型调用或原运行状态写入。
- 原 main `9a7017e` 使用相同驱动/真实 fixture，在结果 HTTP 路由返回 404（RED）；迁移版通过。
- 前一次对较新自举 fixture 的验证在入口被 SchemaVersionError 拒绝（16 > 11），证据
  `%TEMP%/ehai-result-gate-6n737bdw`。保留失败，不篡改 fixture 或把 schema 16 当作本批支持范围。
- 未验证：第二轮 phase_summary、模型运行时修复、两代版本链和全平台退出条件。

## 剩余成果分类

| 批次 | 来源与内容 | 当前决定 |
| --- | --- | --- |
| M2 工具修复 | 第一轮 builtin_tools.py shell PATH 修复 | 独立审查工具环境和授权后迁移；M1 不依赖它 |
| M3 阶段执行/人工介入 | 旧 worktree 的 Reviewer、human Gate、过程草案/审查/应用、PhaseSession、handoff、SQLite 12–16 与公开接口 | 依赖交织，尚未认定整体成熟；不混入 M1 |
| M4 第二轮有效子任务 | resume `d9d89404e9a8625517090c9253b72a0903c5aadd`；snapshot `ad0c47a593eb8452a770ff9a3659d218f48c3729` | 依赖 M3 的过程/恢复结构，暂缓；不是完整第二轮交付 |
| M5 未完成实验 | opaque/summary/replay、阶段历史和 phase_summary；原唯一 E2E 的第二轮/版本链扩展 | 留原实验区，不作为本批稳定能力 |
| M6 现成执行器接入收口 | main 已有 codex-server Connector，完整阶段共享/Server handoff 尚未接通 | 后续独立核验，不在迁移查询时切换旧 Run 配置 |

每批以自己的入口证据决定是否合入，不能按“已写了多少行”或旧账本里的 LIVE 标签判断成熟度。
