# 阶段 ④：统一授权与生活接入

## 2026-11-10：经 EHAI 由 Codex 实现

范围：按用户 11-10 批准的 [AUTHORIZATION_DESIGN](../AUTHORIZATION_DESIGN.md) 实现 M1 决定视图、M2 写授权绑定、
M3 Decisions 订阅摘要、M4 决定卡片与队列通知。计划由团队负责人按导入格式写成（4 个阶段、8 个节点：每阶段实现 +
Reviewer；节点关卡为静态检查与 schema/客户端无漂移 + 人工检查，最终关卡加网页类型检查与构建；产品 E2E 由团队负责人
在最终候选上另跑），经 `import-plan` 导入。

宿主：`ehai-api`，工作区为本仓库，端口 8793，Codex `gpt-6.1-sol`（用户 ChatGPT 订阅）；全程只经 `ehai` CLI 操作。
人工检查由团队负责人按用户授权代办，结论写在各 Check 的 comment 中。运行时验收用阶段 ② 的验收管理器（端口 8798，
trial 工作区与生活项目），每个候选换上后重启管理器，网页由候选自身的构建提供。

### M1 决定视图

返工 1 次，原因在合同措辞：grant 的有效秒数按步骤开始计算，会缩短等待决定的窗口。改为待批准时 expires_at 只取
步骤超时，批准后取已记录授权的到期时间。

验收：Inbox 列出 plan_approval、两种 workflow_confirmation（自定义步骤冻结 `{item, price}` 并以 24 小时期限为
expires_at；生活录入带拟建待办）与 human_check，每项带绑定对象及其 SHA-256；线程阶段不变（草稿方案线程
awaiting_approval，注意数 0）；管理器队列的线程行带该线程的待决定项。approve-plan 后该项变为“方案已批准”、
无动作，线程回到 idle。

### M2 写授权绑定

返工 1 次，团队负责人读代码发现：Google Calendar 恢复一次结果未知的创建时，若授权已过期或回执已计入，会直接报
failed 而不先查日程是否已建。改为恢复路径跳过过期与上限拒绝，只查询不重放。

验收（`ehai-google-calendar` 对 loopback 日历替身，替身记录每个请求）：

1. 写动作的标题引用 `input.title`：保存被拒（422，指出步骤与输入名）。
2. count 上限 1、有效 300 秒：待批准时 Inbox 显示冻结对象、上限 1、无到期时间；批准后替身恰收到 1 次创建，
   Connector 调用快照带 authorization（ID、subject_sha256、limit、expires_at）。
3. 有效 60 秒，批准后等待 75 秒：Run 失败，`Authorization for ask expired at …`，替身未收到请求。
4. count 上限 1、两次创建：第一次完成，第二次由 Connector 拒绝 `Write authorization limit reached`，替身只收到 1 次。

恢复路径只读代码核对，未做崩溃试验。

### M3 Decisions 订阅摘要

1 次干预，原因同样在合同措辞：示例写成 `steps.fetch.output.items`，引擎实际只认 `steps.fetch.items`。
答复改用现有引用写法，不改核心。

返工 1 次，真实 OpenAI Decisions 上发现：Provider 返回 200 且回答符合约定，但 `usage` 另带
`input_tokens_details`、`output_tokens_details`、`total_tokens`，严格模型拒绝多余字段，38 条全部记为
“Invalid provider answer”，Run 只显示“Every item failed classification”。仓库外用一次真实调用对比确认原因。
修正：usage 只读两个计数；只对 429 与 500–599 重试（Reviewer 指出原条件包含 ≥600）；全部失败时错误附最多的原因。

验收（示例定义，两个 Connector 进程从候选运行，真实网络与 `gpt-6-luna`）：9 个订阅源（BBC、少数派、阮一峰 Atom、
uv Releases Atom、Python Insider、V2EX、Planet Python、Solidot、36kr）合并为 38 条、带源地址与源名，36kr 单独记为
“response is HTML, not a feed”；38 条约 3 秒分完（有界并行），保留 26、失败 0；条件分支进入人工步骤，队列出现
“今天筛出的这些值得看吗？”，绑定对象为保留条目（带结论与置信度）及统计。用户自己的订阅源尚未提供，未在其上验收。

### M4 决定卡片、通知与导航

返工 1 次（Reviewer 4 项 + 页面实测）：未知字段名一律显示“其他内容”（购物步骤的 `{item, price}` 看不出内容）；
所有 `*_id` 都被遮成“已绑定到本次决定”，日历 ID 也看不到；摘要条目逐字段铺开（空字段、ISO 时间、原始置信度），
375 px 下值被挤到约 50 px 宽；剩余有效期显示为分钟数；执行者的英文说明直接作为卡片正文；没有归属 Run 的待办，
通知只能打开笼统页面。改为：未知字段显示原键名、只隐藏内部 ID、嵌套项省略空字段、时间与百分比格式化、
标题+链接的条目紧凑显示、窄屏标签在上；有效期超过两小时显示小时或天；执行者说明收进折叠区；新增
`/decisions/<工作区>/<类型>/<请求>` 单项页（只用现有读取），独立待办的通知与“查看所属页面”指向它。

验收（无头 Chrome，1440 与 375 px，候选自身构建）：首页队列各行展开为对应卡片；购物步骤显示 item/price；
摘要卡片前 5 条为链接标题、“BBC News · 今天 06:17 · read · 86%”与摘要，其余 21 项折叠；375 px 无横向溢出；
单项页渲染实时卡片。窗口未聚焦时：首次读取不通知；导入新草稿方案约 4 秒后通知“方案等你批准”；生活待办到期时
无核心事件也通知“生活待办到期”，点击打开待办页。导航不再显示“问”“自动回答”，两地址仍可直接打开。
在摘要卡片上填写说明并点“批准”：该行离开队列，核心记录决定与授权（authorization_id、subject_sha256），
摘要 Run 以 reviewed 结束。控制台出现过一次 404 资源错误，在首页与单项页上均未复现。

### 结果

Run fe95a867 completed（约 54 分钟），1 次干预，M1–M4 各返工 1 次。最终候选上产品 E2E 1 passed。
`integrate-run` 无冲突，40 个计划内文件（核心、Connector、网页、文档、示例、重新生成的 schema 与 TypeScript 客户端）。
按里程碑提交为 4 个提交，提交后的树与整合 commit 相同；在分支上重跑 ruff、format、两套 mypy、lint-imports、
schema/客户端无漂移、类型检查与网页构建均通过。

用量：Codex 走订阅；OpenAI Decisions 两次摘要运行与两次诊断调用共约 2.8 万输入 token（`gpt-6-luna`，
第二次摘要运行 13,705），按量费用未单独核算。

### 未覆盖

- 用户自己的订阅源与筛选标准；每日 Routine 的实际到点触发。
- Google Calendar 只对 loopback 替身；真实 OAuth 与日历、amount 上限、崩溃后恢复路径的实跑。
- 桌面 App 的系统通知：原生通知桥没有点击回调，点击只把应用调到前台，不打开对应项；浏览器通知可打开。
- 决定卡片上只实点了 Workflow 步骤的批准；人工检查、方案“批准并开始”、干预答复、便签与调用核对的按钮只看渲染
  （沿用现有调用）。用户本人验收、Windows。

### 用户验收

11-10 用户验收通过，开始阶段 ⑤。用户自己的订阅源尚未提供，摘要尚未在其上运行。
