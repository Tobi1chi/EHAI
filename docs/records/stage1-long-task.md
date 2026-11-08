# 阶段 ①：长任务不跑偏（RSS Connector）

## 2026-11-08：Codex 执行阶段 1–2，换 Pi 接续阶段 3

范围：在 EHAI 自身实现 RSS Connector（抓取与解析 → Workflow 动作 → 用法文档），用来检验阶段 ① 的完成条件。
计划由团队负责人按 EHAI 计划导入格式写成并经用户批准后导入（`import-plan`），不经 EHAI Planner 起草；
3 个阶段、6 个节点，阶段 1、2 各有 Reviewer、命令关卡和人工检查，最终关卡为命令 + 人工检查。

宿主：`ehai-api`，工作区为本仓库，端口 8793，Worker 只允许 ruff、mypy、lint-imports 与 `git.read`；全程只经
`ehai` CLI 操作。人工检查由团队负责人按用户授权代办，结论写在各 Check 的 comment 中。

### Run 1（Codex，`gpt-6.1-sol`）

1. 阶段 1：Codex 实现 `rss.py`/`rss_contract.py`（约 1.5 分钟），Reviewer 通过，mypy 关卡通过。
2. 人工检查（真实订阅源）：BBC、少数派、阮一峰（Atom）、GitHub uv Releases（Atom）、Python Insider、V2EX、
   Planet Python、Solidot 解析正确；hnrss 一次 TLS 断连后重试正常；36kr 返回 HTML 拦截页。离线样本发现：
   XML 声明前的空白/BOM、gb2312、`dc:creator`、`content:encoded`、`-0000` 时区、HTML 页报错措辞，以及只有
   `updated` 的 Atom 需要排序回退。处理为 **通过并暂停**。
3. 批准内过程调整：`propose-process`（Codex Planner 起草）在阶段 2 开头新增“按真实源发现加固解析”节点，
   修改连接器实现与审查节点的指令；`review-process` 判定未越出批准边界；`apply-process` 后 `resume-run`。
4. 阶段 2：加固 → 连接器（`ehai-rss`、`feed.fetch`、Workflow 示例）→ Reviewer 通过 → lint-imports 通过。
5. 人工检查（隔离宿主 8794 + 候选连接器 + 真实源）：两个真实源走到 Workflow 人工步骤，统一待办出现
   `workflow_confirmation`；但所有失败都只报 “RSS fetch or input validation failed”，违反“失败给出清楚原因”。
   处理为 **拒绝**。返工先派到阶段的第一个返工节点（加固节点，只能改 `rss.py`），Worker 按边界停下并开出
   干预；回复“本节点原样提交，修复属于连接器节点”后，连接器节点修正。复测：HTML 页报
   “response is HTML, not a feed”，404 报 “Feed returned HTTP 404”。处理为 **通过并暂停**。

### 换 harness 与 Run 2（Pi，DeepSeek `deepseek-flash`）

停掉 Codex 宿主，用同一数据库以 Pi harness 重启。`replan-plan`（Pi Planner 读取 Run 1 起草，契约与 6 项发现
均带入）→ 带 supersession 的 `approve-plan` → `start-run --predecessor-run-id … --result-adoptions-file …
--execution-config … --authorize`（后继 Run 必须显式提交执行授权）。沿用 3 个工作节点成果，Reviewer
与关卡在 Pi 上重跑；两个节点人工检查按字节相同（宿主已核对哈希）引用 Run 1 的真实源证据通过。
阶段 3 由 Pi 读取前序成果与交接写中文文档；最终关卡上团队负责人核对文档与代码一致，候选结果上
ruff、format、两套 mypy、lint-imports 与产品 E2E 通过。Run 2 completed，`integrate-run` 无冲突，补丁只含
8 个预期文件。后继方案未带回原最终命令关卡（`replan-plan` 只接受宿主提供的命令），改由人工关卡执行。

用量：Codex 走订阅（输入约 38 万 token，缓存读约 175 万）；Pi/DeepSeek 自身估算约 0.31 美元（估算，不是账单）。

### 对照阶段 ① 完成条件

| 条件 | 结果 |
| --- | --- |
| 真实模型、正常入口；方案 ≥3 阶段，含阶段 Gate、人工检查、至少一次批准内过程调整 | 满足 |
| 最终成果满足批准判据，审查中没有方案外改动，干预次数与原因有记录 | 满足：1 次干预（返工派到加固节点），原因与回复在轨迹 |
| 同一 Goal 的后续阶段换一个 harness 后，读取轨迹接着做 | 满足：Codex → Pi，后继 Run 沿用成果并完成阶段 3 |
| 工作轨迹可按 Goal 读出步骤、判断、Gate 结果与决定 | **部分**：`get-thread-timeline` 跨两个 Run 列出计划、批准、节点完成、人工检查开关、干预、暂停、接续与整合；但人工检查只显示“Human Check closed”，不区分通过/拒绝/暂停，也没有意见；过程调整（提案/审查/应用）、Reviewer 结论与返工不在时间线中，只能分别用 `get-run-checks`、`get-process-*`、`get-trace` 查到 |
| 图显示尝试过的路径与判断 | **部分，未实看**：`PlanGraphView` 按单个 Run 显示每个节点的尝试次数并链接到 Attempt 轨迹，不显示判断，也不跨后继 Run；本次宿主无页面，未在浏览器核对 |

另记：EHAI 对 Pi 宿主的执行配置没有读取入口，显式授权只能按宿主参数手工拼出执行配置文档。

## 2026-11-09：补齐“按 Goal 读判断”与“图显示判断”

范围：上面两条只部分满足的完成条件。只投影已有事实（事件、CheckRun 快照、`review.json`），不新增事件类型、
命令或执行行为。计划由团队负责人写成并经用户批准后导入：2 个阶段、5 个节点（时间线投影 + Reviewer；线程页、
Run 图 + Reviewer），阶段 1 为命令关卡（静态检查与 schema/客户端无漂移）+ 人工检查，最终关卡为命令（再加类型
检查与网页构建）+ 人工检查。全程 Codex（`gpt-6.1-sol`，订阅），宿主 8793，工作区为本仓库。

1. 阶段 1 开头 1 次干预：允许的 `npm …` 命令都报 `env: node: No such file or directory`。原因：允许命令的
   PATH 只含该可执行文件解析后所在目录，而 npm 是 Node 脚本。改为经 `node` 调用的等价命令（`npm-cli.js ci`、
   `generate-client.mjs`、`tsc`、`vite build`，先在同样精简的环境里逐条试过），重启宿主后回复干预继续；
   关卡脚本自行设定 PATH。
2. 阶段 1 人工检查（阶段 ① 数据库副本 + 候选代码，端口 8795，只读不调模型）：时间线读出 6 次人工决定（结论、
   决定人、意见全文）、2 次人工暂缓、过程调整 3 步、6 个 Reviewer 结论与问题计数、1 次关卡失败、3 个返工节点，
   含过程调整新增节点的名称；产品 E2E 通过。处理为 **拒绝**：每个 Reviewer 提交都重读整份事件日志。返工改为在
   建索引时读一次；输出与返工前逐字相同（57 条），读取从约 80 ms 降到约 35 ms。处理为 **通过**。
3. 阶段 2 人工检查（隔离管理器 8796，候选代码与构建好的网页，放入阶段 ① 数据库副本）：线程页完整；但 Run 图
   为取检查记录调用 Run 轨迹，该接口在两个 Run 上都 422，图上没有任何判断。另有问题等级未翻译、待办人工检查
   不再显示问题文本（越出批准契约）。处理为 **拒绝**；返工后图只读时间线与成果沿用。复看：Run 1 图的审查节点
   显示人工与 Reviewer 结论，3 个返工节点显示“返工 1 次”；Run 2 图 3 个沿用节点链接到 Run 1 的 Codex
   Attempt 轨迹（点开核对），两个建议修改的 Reviewer 标红；线程页等级为 阻塞/重要/次要/备注。
   最终命令关卡与产品 E2E 通过。处理为 **通过**，Run completed，`integrate-run` 无冲突，补丁只含 12 个计划内文件。

时间线新读出的一个事实：阶段 ① 后继 Run 中两个 Reviewer 建议修改（一个带阻塞问题），这两个阶段由团队
负责人的人工检查放行，当时这些建议没有被摆到决定者面前。

另记（未修）：Run 轨迹与 Run 结果在这两个 Run 上返回 422。`sanitize_json_object` 对序列化后的 JSON 文本跑
凭据脱敏正则，模型读到的代码含 `"Bearer " + key` 时正则吞掉转义符，结果不再是合法 JSON，整个 Run 的轨迹与
结果都读不出；在本次改动前的提交上同样复现。

### 对照（更新）

| 条件 | 结果 |
| --- | --- |
| 工作轨迹可按 Goal 读出步骤、判断、Gate 结果与决定 | 满足：`get-thread-timeline` 跨两个 Run 读出人工检查结论与意见、暂缓、过程调整、Reviewer 结论、Gate 结果与返工 |
| 图显示尝试过的路径与判断 | 满足：Run 图按节点显示尝试次数、人工与 Reviewer 结论、返工次数，沿用成果链接到前一个 Run 的 Attempt；在管理器页面实看。限制：Run 结果区因上面的脱敏缺陷在这两个 Run 上读不出 |
