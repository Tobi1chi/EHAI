# EHAI Control Plane Client

包含从 [schemas/v1](../schemas/v1) 生成的严格 TypeScript API Client，
以及只消费该 Client 的 [Web 工作台](#web-工作台)（`web/`）。[STATUS](../docs/STATUS.md) 记录实际交付。

P3.1 已提供 `listProjects()`、`getProject(projectId)`、`getRuntimeContext()`，
返回按宿主的项目总览、目标/Run/有效 Gate 成果与当前执行上下文。
P3.2 已提供 `listInbox({ projectId, runId })`、`getInboxItem(kind, requestId)`；
根据待办 actions 调用既有源操作，提交后重读详情及 Run。
便签、持久事件消费、项目配置已提供正式契约与生成 Client，新增方法包括 createNote/addNoteMessage/decideNote、
registerEventConsumer/readConsumerEvents/acknowledgeConsumerEvents、configureProject/getRunConfiguration。
Web 工作台不承担便签决定、批准或执行接续的业务编排，只把用户的决定提交给对应的源操作。
`EhaiWorkspaceManagerClient` 提供工作区登记/启动/停止/总览及执行配置读取；
`manager.workspace(workspaceId)` 返回绑定该工作区的 `EhaiApiClient`，包括 `getPlannerCapacity()`。
线程查询的生成入口为 `EhaiApiClient.listThreads()`、`getThread(goalId)`、
`getThreadTimeline(goalId, {limit, after})`；`EhaiWorkspaceManagerClient.listThreads()`
按工作区返回核心线程列表快照与不可用错误。时间线 `next_after` 是条目偏移，
不同于事件流的 `event_offset`。验证范围见 [STATUS](../docs/STATUS.md)。
聚合结果保留来源与不可用状态，不声称多个核心之间具有同一事务快照。
具体范围及完成条件见 [路线图](../docs/ROADMAP.md)，不在此重复定义。
页面先呈现成果、待办和下一步，轨迹按需展开；顶层 Agent 仍在 EHAI 之外。

| 文件 | 职责 |
| --- | --- |
| [src/generated/ehai-client.ts](src/generated/ehai-client.ts) | 生成类型、EhaiApiClient、错误与 SSE URL helper |
| [src/index.ts](src/index.ts) | 稳定导出入口 |
| [scripts/generate-client.mjs](scripts/generate-client.mjs) | 从 JSON Schema/OpenAPI 生成 Client |
| [package.json](package.json)、[package-lock.json](package-lock.json) | npm 脚本与锁定依赖 |
| [tsconfig.json](tsconfig.json) | 严格 TypeScript 配置 |
| [web/](web) | Web 工作台（React + Vite），构建产物 `web/dist` 不提交 |

在本目录运行：

~~~powershell
npm.cmd ci
npm.cmd run generate
git diff --exit-code -- src/generated/ehai-client.ts
npm.cmd run typecheck
npm.cmd run build
npm.cmd run web:build
~~~

修改公开契约先更新 Schema，再生成 Client；不手改生成文件。
页面仅消费正式 API/事件，不访问 SQLite，不复制 Run、Attempt、Branch、Check 或 Gate 状态机。
“需要我处理”聚合不同来源的请求，动作回到各自业务入口；普通回复不隐式批准或扩权。
当前模型与未来页面严格分开，开发和验收遵循 [开发规则](../docs/DEVELOPMENT_GUIDELINES.md)。

## Web 工作台

由 `ehai-manager --ui-dir control-plane/web/dist` 在 `/ui/` 同源提供，启动方式见
[多工作区后端](../docs/WORKSPACE_MANAGER.md#web-工作台)。页面分为生活与工作两个分区，
另有两区共用的连接、自动回答、设置；窄屏时改为底部标签栏，并支持浅色与深色。

| 分区 | 页面 | 接入的正式能力 |
| --- | --- | --- |
| 生活 | 今天、待办、录入、定时、记录、问 | life tasks、life.capture 确认、Routine 与调度状态、Workflow 执行记录、路由实验提问与纠错 |
| 工作 | 首页、新线程、线程、项目、Run、尝试轨迹 | 线程列表与线程时间线（讨论、方案、Run、待办与成果按时间排列，状态与下一步来自核心的线程读模型）；新建线程（创建 Goal 后发起规划讨论）；添加项目文件夹（经 ehai up 启动的管理器，用引擎配置登记并启动工作区；桌面外壳里可用系统文件夹选择器）；方案卡片“批准并开始”（展示管理器给出的执行配置，依次提交批准与授权执行两个写操作）；整合 Run 成果、按文件查看差异、把整合结果放到新分支（不切换、不推送）；窗口在后台时线程需要处理、方案待审、完成或失败发系统通知（设置里开关）；跨工作区总览、统一待办（人工 Gate 含“通过并暂停”、干预回复、便签）、Run 暂停/恢复/取消、执行图（嵌在页面里的画布：从左到右按依赖分列，阶段为列标题，跨列的线绕开节点；拖动平移、⌘/Ctrl+滚轮或双指缩放、看全图、全屏）、成果；从执行图节点、轨迹列表、求助记录和待处理进入单个 Attempt，逐条看模型回复、工具调用与返回、报错和 token |
| 共用 | 连接、自动回答、设置 | Connector 动作与调用记录（结果未知时请求核对）；路由实验的慢环处理、候选、回放与发布（已暂停的配方经回放重新发布）；工作区启停 |

- 生活分区使用哪个 Project 是本浏览器的偏好，核心尚无分区标记；其余项目归入工作分区。
- 录入默认直接保存；打开确认开关后先进入待处理，确认后才保存。
- 写操作带幂等键；结果未知（网络错误或 5xx）时重试沿用同一个键，版本冲突（409）提示重新读取。
- 实时更新读取各工作区事件流，只用来触发重新读取，不在页面推导业务状态。
- 凭证、OAuth 与 Connector 登记只在本机 CLI 完成，页面只给出命令；页面只能用 `ehai up` 的引擎配置添加工作区，自定义运行时登记仍使用 CLI。
- 规划讨论可能持续数分钟；发出的消息在本页记住直到核心记录这一轮，失败（结果未知）时用同一个幂等键重发。
- 未提供：通用 Workflow 与流程库、工作分区的定时、手工判定未知调用的结果、开 PR。
- 桌面外壳见 [desktop](../desktop/README.md)。

