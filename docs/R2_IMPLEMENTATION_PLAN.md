# 当前实施与验证记录

更新：2026-09-16。旧逐日记录、原始 Run ID 和历史试验保留于 [归档](history/R2_IMPLEMENTATION_PLAN.md)。
本页只记录当前代码对应的能力与本批证据，阶段结论见 [STATUS](STATUS.md)。

## 本批交付

1. 周期轨迹审查（600 秒/30 步），结果保存与查询，默认仅建议。
2. 显式采纳审查并仅挂起目标 Worker；保留幂等、目标/版本核对、收敛与人工回复边界。
3. 草稿 Gate 明确删除工具；冲突反馈给出实际修复入口，过程模式冻结条件。
4. CLI 的 API 模式及运行控制，不重复装配 Scheduler。
5. 外部初始计划 ImportPlan：CLI/API/TS Client、导入 Schema、示例，复用图校验与 Builder、
   幂等/重读/事务持久化，不调用 Planner，不接受状态或批准字段。
6. 独立 stdio MCP：官方 SDK 1.x，转发同一 HTTP API，默认只读、显式写开关、
   按需请求 Schema、结构化错误，无模型/调度/数据库业务逻辑副本。
7. Windows 宿主 Git 自动换行转换的局部修复，用户全局配置不变。
8. 当前文档统一，历史快照与有效入口分离；外部顶层 Agent 的产品边界明确。

## 既有代表性证据

- Luna Run 956d9ada-ccbc-411e-950c-f5ba41194182 completed：两项初始 Work 实际重叠，
  代码挂起时文档独立完成；回复后代码与 Reviewer 成功，原行为 Gate passed。
  逻辑 Phase Session 共享，物理 Pi 会话按 Attempt 隔离；未验证强杀、完整 handoff 或周期纠偏。
- Go Gate 修复复测：方案 5cf8fcc6-a36e-46aa-9c26-1caf1c2ef727 保存为 draft，
  重复 Gate 错误后直接 remove_node_gate → finish_plan；不改图或原最终条件。
- CLI API 试用：真实回环宿主，创建/提案/批准/查询及幂等/错误保留通过；
  当时仅确定性无模型路径，不冒充 Pi 编码证据。

详细原始记录在归档，对应临时目录 ehai-luna-e2e-20260914、ehai-gate-repair-20260915-retry、
ehai-cli-api-20260915。临时文件不提交 Git。

## 本轮外部导入与 MCP

隔离目录：C:/Users/28262/AppData/Local/Temp/ehai-import-mcp-20260915。

- CLI 导入 examples/plan-import.json，生成 draft e90ce25a-833b-4ccd-b7db-57d8592e5b71，
  2 节点、1 Phase、1 最终 command Gate；正常查询、批准及 API start-run 均进入真实服务。
- 首次执行配置中的 git.read 与宿主空权限不匹配，409 拒绝且无 Run 创建；
  减为宿主已有权限后重新显式授权，未扩大权限、未改变 Gate。
- MCP 官方客户端经真实 stdio 握手/工具列举/调用，44 个工具（包括按需 Schema）；
  创建 Goal、导入并重读 draft c697a73d-3208-4c40-89e9-a3e8ab689c27。
  导入 nodes.status=completed 返回 422/isError，未保存该无效方案。
- 真实 Pi/DeepSeek v4.1 Flash（thinking off）接受完整 MCP 工具输入定义，
  实际调用 get_runtime_health、create_project 并持久化。两次模型请求合计 10,854 token，
  其中缓存输入 5,248。证明这两项工具，不声明每项控制动作都被模型实跑。
- 声明式导入/协议往返不创建永久测试套件，依赖与客户端生成均可重复。

## 真实执行的失败、原因与修复范围

外部导入 Run cc140b88-5473-4ff6-9420-29f83270d892 实际调用 Pi Worker/Reviewer。
Worker 提交的 Git blob 是 Hello EHAI + LF；Reviewer 工作区却为 CRLF，冻结字节级 Gate 失败。
原因是宿主 Git 继承 Windows core.autocrlf，不是模型工具把写入内容改坏。

仅为 EHAI-owned worktree 创建、GitCodeWorkspace 的 Git 命令增加 core.autocrlf=false，
不改全局/仓库配置，不覆盖显式 .gitattributes。仓库外故障诊断使用原失败快照，
修复前重现 CRLF，修复后重新物化得到 LF，原 verify.py 返回 0。

原 Run 已经通过人工回复并在新宿主尝试继续，但达到本次累计 115,000 报告 token
的诊断阈值（最后一次响应后累计 115,163）后停止。共有 20 个模型 HTTP 请求，
预算是试用外部观察器，不冒充 EHAI 的完整硬费用限额。
Run 最终 paused，仍有人工介入；未通过完整复测，不把独立诊断通过改写为 Run completed。
全部宿主已停止，没有无限重试、放宽 Gate 或重写旧失败记录。

## 静态与剩余边界

Ruff、格式、mypy、Schema/Client 生成、TS typecheck/build 已执行。
MCP/导入入口已有正常证据；完整 Pi 长运行、跨批准接续和唯一产品 E2E 未完成。
这些未完成事项不能通过删旧文档、增加入口或标记阶段完成消失。

## 2026-09-16：Block 版本和变更清单

用户结果：在过程草稿和历史版本查询中看清哪些任务被修改/删除，哪些下游因输入变化
失去旧执行身份，为成果与 Git 接续提供明确节点对应关系。block 指计划节点。

- 输入：前一过程版本、编译后的候选图、Planner 保留的旧节点键。
- 输出：block_changes，包括稳定 block_id、version、前后节点 ID、change、changed_fields。
- 所有权：宿主编译器生成，SQLite 在草稿保存和过程应用时重新核对图、版本及对应关系。
  不新增模型填写字段，不更改 Pi 工具输入 Schema、角色权限或独立批准审查。
- 失败：身份重复、跨图引用、清单与实际图不符被拒绝；不根据标题推断对应。
  历史缺失字段读取为 null，首次新调整以旧节点为追踪起点，不重写旧快照。
- 入口：既有 get-process-draft、get-run-process-drafts、get-process-revision；
  CLI/HTTP/MCP 共用查询模型，公开 Schema 和生成的 TS 类型同步。

验证目录：C:/Users/28262/AppData/Local/Temp/ehai-block-changes-186dfa196c4d498c9b4bb7ae4b97def1。
CLI 创建、导入并批准一份仅供本地检查的 A→B→C、独立 D、最终 R 的计划；
生产应用服务以 background_start 建立 pending Run，没有启动 Runtime 或派发 Worker。
Run a69ca211-9bf5-4ae2-9a8f-1b5cfc98415d 的初始过程版本
b3118010-b774-4480-8251-47fb27e5eca5 经 CLI 重读返回 5 个 added block。

对该真实导入图直接调用生产过程编译器观察两次候选变化：修改 B 时 B/C/R 版本为 2，
A/D 为 1；随后删除 B，B 保留版本 2 的 removed 记录，C/R 增为 3，A/D 仍为 1。
两次候选均通过清单核对，codec 编解码后对象相等；候选未作批准或应用。
生产 create_local_app 的 HTTP 查询通过 ASGI transport 返回新清单，公开响应 Schema 校验通过；
复制旧试用数据库到隔离目录后，同一 HTTP 查询返回 block_changes=null，Schema 同样通过。
原历史数据库和原 Run 未恢复或修改。

Ruff、格式、mypy、客户端生成及 TS typecheck/build 通过。本轮没有模型调用、Git 成果
重整合或真实 Pi 修改过程的重跑，不把无模型编译/查询检查称为产品 E2E 或 MCP 模型调用证据。
按 block 自动回退/整合、完整跨批准后继 Run 仍未实现；Token/费用硬限额按用户决定暂缓。
