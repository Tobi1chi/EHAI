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
6. 独立 stdio MCP：官方 SDK 1.x，转发同一 HTTP API，启动即开放全部已支持的查询和写操作、
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

### 2026-09-16 MCP 默认开放写操作

按用户决议移除启动参数 --allow-writes 和条件注册；查询与写工具统一注册，
HTTP 业务批准、执行授权、幂等和状态校验保留，工具读写提示仅作为语义注解。
README、用法与 ADR 同步；旧 MCP 客户端配置需删除该参数。

隔离目录：C:/Users/28262/AppData/Local/Temp/ehai-mcp-default-writes-n_eorjz3。
正常 ehai-mcp 入口不带写开关，经官方客户端与真实回环 HTTP 宿主验证：
列出 45 个工具（22 查询、22 写操作、1 个 Schema 查询），读取 create_project Schema，
创建项目、重复请求返回同一结果，并成功创建引用该项目的 Goal。
Ruff 检查、格式检查及 mypy 通过。宿主已停止；没有模型调用，
本次仅证明默认工具暴露与上述写入链路，不宣称所有工具的模型验收或产品 E2E。

### 既有 Windows Git 换行问题

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
截至上述导入批次，MCP/导入入口已有正常证据；完整 Pi 长运行、跨批准接续和唯一产品 E2E 未完成。
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
本节提交时按 block 自动整合与跨批准后继 Run 尚未实现，后续交付见下文；Token/费用硬限额暂缓。

## 2026-09-16：Git 自动整合

新增 integrate-run / POST /runs/{run_id}/integrate / MCP integrate_run，客户端 Schema 同步。
宿主依据当前过程版本筛选完成且有效的成果，验证 Artifact 字节/来源、输入 commit 仍在
有效集合内，以 Run 固定基线重建独立 worktree。内容绑定的 integration_id、合并游标和
结果 commit 保留在 Git 元数据目录；冲突返回 conflicted，不默认解冲突、回退用户分支或推送。
输入是 Run 与期望过程版本，输出为 block/Attempt/输入/结果 commit 映射及整合结果。
过程变化或活动 Attempt 拒绝调用；没有输入基线的历史成果需要重新执行。

复制 Luna 试用数据库到 Temp/ehai-git-successor-20260916/luna.sqlite，保留原任务数据库。
生产 create_local_app 配置代码后端但不启动 Runtime，通过 HTTP ASGI 入口整合历史实际
Luna Run 956d9ada-ccbc-411e-950c-f5ba41194182 的两个并行实现成果与 Reviewer 快照。
两次返回 integration_id=69d6f949-31d0-5ac7-aa43-d0d36506bb1b、相同 commit
a075da26eae8b53eca785fb477c7b724a6e27535。Git 对比其 tree 与原已验收 Reviewer
bf85a1ecd7f0eab2f3e1fad5ba5f9f8f9a3f857f 无差异；用户试用工作区仍干净。
新整合 worktree/patch 位于原隔离试用的 worktrees 下，旧成果未改写。本次没有模型调用。
Ruff、格式、mypy、Schema/客户端生成及 TS typecheck/build 通过；冲突恢复未做本轮实跑，
新增 MCP 工具未做真实模型调用，以上不是唯一产品 E2E。后继 Run 在下一独立提交接通。

## 2026-09-16：跨批准后继 Run

approve-plan / POST /plans/approve 新增可选 supersession：绑定 paused 前驱、期望过程、
操作者和逐项未决请求处置。每个旧 human_check/intervention 均需精确 token；
external_effects 不能标记 superseded，必须先明确解决。旧历史记录不改写成成功。
start-run 新增 predecessor_run_id 和 result_adoptions，明确授权下原子写入新 Run、
ResultAdoption、来源处置、派发意图与回执。旧结果字节和生产者哈希由宿主读取验证，
不接受调用方伪造 Attempt/Artifact。重复请求返回原 Run，已接续前驱不能再次创建后继。

get-run 显示双向关系；get-trace 的 RunSuccessorCreated 和 get-run-adoptions 展示接续事实。
新 Worker 上下文获得旧问题的处置和来源；旧 Run 保持 paused、禁止恢复，旧未决记录保留
原貌，由新 Run 的明确处置解释其去向。已有 Goal Worker Attempt 预算累计，未新做 Token 限额。
适用的代码输入无需新 Worker，但新 Gate 仍执行；基线/依赖不适用时走原正常执行路径。

真实试用：Temp/ehai-git-successor-20260916/live，临时启动文件不入库。
首次选用系统 Node 22.18.0 被 Pi >=22.19.0 版本检查拒绝，0 模型 token；定位后以本机
已有 Node 24.19.0 创建独立试用，不重放未知网络请求，不变更模型或工具严格性。
当前 Pi/Go deepseek-v4.1-flash、thinking off 执行只读 note.txt 的 Worker 与 Reviewer，
两者 succeeded；源 Run 143a0f4f-a175-4dbb-96ca-8cebb8e6c987 的 Work completed，
Reviewer verifying，原人工 Gate 等待。最后一次响应累计 32,049 报告 token，触发试用
28,000 响应后检查阈值；显式暂停并关闭 Runtime。没有继续发模型请求。

后续只运行宿主：正常 HTTP replan/approve 明确撤销原人工问题，start-run 映射完成的 Work。
新 Run 8091b681-f831-43c3-827d-0271237a0b7c，两次相同启动返回同一 ID。
关闭后台模型派发，直接调用生产 advance_ready_adoptions 执行实际输入与 Gate 校验；
新 Run 在人工 Gate 等待时仍 running，但没有任何 Worker Attempt。HTTP 对新 request token
作出人工批准后 completed，integrate-run 返回 integrated、commit
3ad881c8198519b8ff6a5db501a88feaf473a228，与未变更 note 的源 Git 基线相同。
这是零目标模型调用的真实成果接续，不伪造新 Attempt 或复制旧 Gate pass。

正常 HTTP 路径及静态检查/客户端生成构建通过。完整产品 E2E、变更基线后的新 Worker
实跑、多次后继链与崩溃注入仍未覆盖；新增 MCP 调用未做真实模型协议复测。
所有本轮宿主已退出；旧历史试用和 key 未改动、未提交。
