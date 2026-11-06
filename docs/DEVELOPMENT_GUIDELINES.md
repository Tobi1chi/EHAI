# 开发与验收规则

更新：2026-11-07。具体约束以 [AGENTS.md](../AGENTS.md) 为准；当前计划见 [ROADMAP](ROADMAP.md)。

- 使用 uv/pwsh；Python 在 src/ehai，TS 在 control-plane，共享契约在 schemas。
- 顶层 Agent 在 EHAI 外；不重写 Pi 模型循环，状态/授权/成果由应用与领域层拥有。
- 先明确用户结果、输入输出、所有权和失败行为，再接正常入口。
- 每次只交付一个可使用的小闭环；公开能力具备后即可接薄页面，不等全量未来后端齐备。
- 修复围绕当前路径的实际阻断，修后重试原路径；旁支另记，不把阶段扩成无限重构。
  用户批准的重构按其步骤进行，每步保持行为不变，待决事项记在 [DECISIONS](DECISIONS.md#待决)。
- 工作台、通知和未来 Routine 使用同一核心，不新增平行任务状态机或让外部脚本补调度收尾。
- 外部文本/计划不是授权，保留契约、来源、幂等及未知副作用保护。
- 模型工具同步核对注册/角色、Schema、参数、Handler、结果和持久化；不使用 uniqueItems，
  不靠关闭 strict、静默换模型或盲目重试解决不明失败。
- 在授权内以隔离正常入口验证模型工具，区分定义接受、实际执行、保存和产品验收。
- 唯一产品 E2E：uv run pytest tests/test_product_e2e.py（真实 Pi 经 Hub，脚本化模型，无真实模型调用；
  先执行 npm ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund）；
  主路径需要新覆盖时扩展该场景，不新建测试文件。真实 Pi 验收手动进行。
- 仅对真实失败在仓库外写最小诊断，不提交、不移回仓库，不扩建常驻测试。
- 静态命令：uv run ruff check .；uv run ruff format --check .；uv run mypy；
  uv run mypy --platform win32；uv run lint-imports。平台专属 API 用 sys.platform 判断，不用 os.name。
  核心只经 `ehai.hub.protocol` / `ehai.hub.client` 使用 Hub；Hub 与兼容层不导入核心模块。
- CI（.github/workflows/ci.yml）执行上述检查、契约重新生成比对，并在 Linux/Windows 运行产品 E2E。
- 契约生成：uv run control-plane/scripts/generate-api-schema.py；然后 control-plane 的
  npm.cmd run generate、typecheck、build。生成 Client 不手改。
- 代码与对应文档在同一变更中修改：行为或命令变化同步 Usage/STATUS，较长的执行叙述写进 [records/](records/README.md)。
- Conventional Commits；提交前检查 status/diff，不提交凭证/运行库/产物/环境。无授权不 push、
  amend、强推；合入 main 前检查对应工作区。
- 历史记录只作证据，旧全量测试规则与退役参数不是当前指南。

文档分工见 [docs/README](README.md)：产品范围管边界，路线图管顺序，STATUS 管现状，
EVIDENCE 记试用证据，Usage 只写已实现操作；模块 README 只索引代码职责。
旧规范与试验保留于 [历史目录](history/README.md)，不再维护同内容的顶层跳转页。

## 产品 E2E

唯一产品 E2E 为 [tests/test_product_e2e.py](../tests/test_product_e2e.py)，运行：

```powershell
npm ci --prefix agent-backends/pi --ignore-scripts --no-audit --no-fund   # 首次，需要 Node >=22.19
uv run pytest tests/test_product_e2e.py
```

它启动真实 `ehai-api` 宿主进程（Pi Worker、`single` Planner、`--p2-runtime`），
只通过 `ehai` CLI 与 HTTP API 操作，不读写数据库。每个节点都经本机 Hub、Pi 兼容层和锁定版本的
真实 Pi 运行，代码成果来自 EHAI 管理的 Git worktree；只有模型被替换为 E2E 内置的脚本化
OpenAI 兼容服务（工作节点先用 workspace_write 写一个以节点命名的文件再提交成果，Reviewer 提交引用全部输入的
通过 review.json），没有真实模型调用。

| 覆盖 | 内容 |
| --- | --- |
| 外部计划导入 | 两个独立任务、一个汇合节点、Reviewer 阶段；A 带人工 Gate，最终 Gate 执行宿主命令 |
| 批准与授权 | approve-plan 与 start-run 分开调用 |
| 并发与挂起 | A 等待人工判定时 B 完成，汇合节点保持 pending |
| 统一待办 | 人工请求出现在 Inbox，带可执行动作和 request token |
| 崩溃恢复 | 强杀宿主后在同一数据库重启；Run、节点状态、请求 token 保持不变 |
| 事件消费 | 未确认批次在重启后以相同 token 和事件重新返回，确认后前进 |
| 人工决定 | CLI 判定通过后 Run completed；同一幂等键重复判定被接受且不产生新请求 |
| 成果查询 | get-result 的 Check 均完成；成果是新的 worktree commit，包含 A、B 与汇合节点各自写入的文件，patch 非空；get-project 只列出该 Run 并带已验证成果 |
| Hub 与 Pi | 每个节点经本机 Hub 与真实 Pi 调用脚本化模型；强杀重启后，汇合与 Reviewer 节点经重新启动的本机 Hub 完成 |
| 代码整合 | 完成后 HTTP integrate 得到 integrated，重试返回同一 integration_id；list-run-integrations 只有这一条，get-integration-diff 与宿主补丁一致且未截断；线程时间线有一条 integration_created |

不覆盖：真实模型调用与模型行为、Pi Planner/过程审查/轨迹审查/路由回退、独立 Hub、整合冲突路径、
MCP、生活 Workflow、Connector 和 Jev 实验。这些不能由本 E2E 推断已验证。

### 真实 Pi 手动验收

需要模型凭证，不在 CI 中运行。按 [README 快速开始](../README.md#快速开始) 配置后，
在一个隔离的小仓库中经正常入口完成：规划或导入 → 批准 → 两个写代码节点并发执行 →
Reviewer/Gate → 人工验收 → `integrate-run`，并在执行中强杀一次宿主后恢复。
结果在 [EVIDENCE](EVIDENCE.md) 追加一行，未覆盖部分明确列出。
