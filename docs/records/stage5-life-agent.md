# 阶段 ⑤：由 Agent 执行生活事务

## 2026-11-10：经 EHAI 由 Codex 实现

范围：按用户 11-10 批准的 [LIFE_AGENT_DESIGN](../LIFE_AGENT_DESIGN.md) 实现 M1 Agent 查询步骤、M2 12306 Connector、
M3 出行示例与记录页启动流程。计划由团队负责人按导入格式写成（3 个阶段、6 个节点：每阶段实现 + Reviewer；节点关卡为
静态检查与 schema/客户端无漂移 + 人工检查，最终关卡加网页类型检查与构建；产品 E2E 由团队负责人在整合后的分支上另跑），
经 `import-plan` 导入。

宿主：`ehai-api`，工作区为本仓库，端口 8793，Codex `gpt-6.1-sol`（用户 ChatGPT 订阅）；全程只经 `ehai` CLI 操作。
人工检查由团队负责人按用户授权代办，结论写在各 Check 的 comment 中。运行时验收用阶段 ② 的验收管理器（端口 8798）：
trial 工作区（Codex）与新加的 pilife 工作区（Pi DeepSeek `deepseek-flash`），各有一个生活项目；每个候选换上后重启
管理器，网页由候选自身的构建提供。行程条件由团队负责人按用户“自定即可”设定：北京 → 上海，2026-11-20，
07:00–10:00 出发，二等座，预算 600 元，优先直达，可接受一次换乘，只要 G/D。

Run aaef4937 于 11-10 约 50 分钟内完成三个阶段的实现与返工（M3 的人工检查于 11-11 判定），1 次干预，M1–M3 各返工
1 次；`integrate-run` 无冲突，整合 27 个计划内文件。

### M1 Agent 查询步骤

返工 1 次。团队负责人在验收管理器上实跑发现：每个 Agent 步骤在 Codex 上 0.02 秒内失败，记录只有
`Agent harness failed: HubRequestError`。临时让候选输出原因后得到 Hub 的拒绝：Codex 设置只接受 executable、
codex_home、environment_names 与 cli_version——宿主给 Agent 委托的 Hub 运行器传了只有 Pi 适配器认识的
`state_root`（阶段 ② 的助手也遇到过同一原因）。Reviewer 另提两项：参数未通过 schema 的查询在处理器之前被拒，
既不计次数也不记录；`submit_result` 原样返回业务对象，结果含 `accepted: false` 时与 Hub 的拒绝信号冲突，步骤
无法结束。改为只对 Pi 传 `state_root`、失败原因保留脱敏后的说明、被拒查询计次并记为 refused、提交后返回固定回执。

验收（trial 工作区，Codex）：

1. 工具含 Google Calendar 的 `calendar.events.create`：保存被拒（422，`agent tools must be read-only`）。
2. 工具为 RSS `feed.fetch_many`、次数上限 6：27 秒完成，调用 1 次（记录带 ConnectorCall ID 与用时），提交的
   `{headlines[3], notes}` 通过 schema，结束步骤取用了它；三条入选条目都在该次 ConnectorCall 的输出中。

参数被拒、次数用尽、结果含 `accepted: false`、中断后重启只读代码核对，未实跑。

### M2 12306 Connector

返工 1 次：`rail.route.get` 把出发站和到达站作为 `--from`/`--to` 选项传给 catbus，每次都以
`未知选项 --from` 失败；catbus 要求按位置传入。

验收：`ehai-rail init` 连接真实 catbus 后，五个动作放在同一个自定义 Workflow 中依次调用（14.3 秒完成），每个结果
与之前一刻直接调用 catbus 的结果比对：

| 动作 | 输入 | 结果 |
| --- | --- | --- |
| `rail.stations.search` | 上海 | 13 个车站，顺序一致，未截断 |
| `rail.tickets.search` | 北京 → 上海，11-20，G/D，只看有票 | 50 趟，顺序一致；每个席别的余票（`{min: 21}` → 21 且“至少”，0 → 0）与票价正确 |
| `rail.tickets.price` | G531 | 票价一致 |
| `rail.route.get` | G531 北京南 → 上海虹桥 | 经停一致，`----` 为 null |
| `rail.transfers.search` | 北京 → 成都，最多 5 个 | 5 个方案，中转站与车次一致，各段席别正确 |

未知车站与过期日期分别以 `找不到车站：不存在的站`、`日期 2025-01-01 已过` 失败。catbus 超时、缺少 catbus 时的
init、60 趟/20 个方案的截断上限未实跑。

### M3 出行示例与页面

第一版在页面上跑通，但被打回 7 项呈现问题：等待连接器的查询记为 `timeout`，页面实时显示“超时 · 0 毫秒”；
记录的参数把非字符串值存成 JSON 文本，页面露出 `["G","D"]`、车次内部编号与席别编码 `9MOO`；步骤列表显示节点
ID；存储的 JSON 按键名排序，表单与卡片字段顺序错乱（日期、最早出发、出发地……，说明排在方案前）；两个模型写的
说明都带工具字段名与电报码（VNP/AOH、`remaining_at_least=true`）；表单显示给开发者的“保存前替换
connector_id”；Agent 查询中标题仍为“等待中”。修复需改 M1 的后端记录，Codex 为此提出 1 次干预，团队负责人只批准
这两项后端改动（查询中记为 running、尝试结束时转 interrupted；输入保留 JSON 值），Gate 不变。

验收（rail.trip 第 2 版由示例保存，在记录页“开始流程”表单中填入上述条件启动）：

| 工作区 | 模型 | 查询 | 结果 |
| --- | --- | --- | --- |
| pilife | Pi `deepseek-flash` | 12 次（含 2 次中转查询），约 63 秒 | G549 ¥576 / G599 ¥598 / G669 ¥598，均直达；说明列出窗口内超预算车次与中转查询结论，通俗中文 |
| trial | Codex `gpt-6.1-sol` | 9 次 | 同样三个方案 |

三个方案与 12306 直查一致：G549 北京南 07:13 → 上海虹桥 13:03（5 小时 50 分）、G599 08:15 → 14:14、
G669 09:07 → 14:59，二等座均至少 21 张，均在预算内；G549 是窗口内最便宜的车。记录页实时显示每次查询（查车站、
查车次与余票、查票价、查中转、查经停，参数行如“北京南 → 上海虹桥 · 2026-11-20 · 车型 G/D”）；决定卡片按方案
显示车次、时间、席别、票价、余票、总价、总时长、换乘与理由，1440 px 与 375 px（第一版）下无横向滚动。两次运行停在
review 卡片。

整合后的分支上静态检查（ruff、format、mypy 两个平台、import 约束、schema 与客户端无漂移、网页类型检查与构建）与
产品 E2E 通过。

未覆盖：Pi 那次运行中记录页一度停在 12 次中的第 6 次，并连续重复请求数百次（其中 1 次 409），刷新后完整；随后的
Codex 运行在测量的 30 秒内请求频率正常，未复现，原因未查明。返工后的卡片在 375 px 下与卡片上的批准/拒绝未重新实点。
Pi 的 DeepSeek 实际费用未取得（两次 `rail.trip` 运行，估计远低于 ¥5）。

### 另记的发现（本轮不修）

- 路由兜底（`PiRoutingFallback`）同样把 `state_root` 传给 Hub，在 Codex 宿主上会以同一原因失败。
- Connector 进程（`ehai-rail run`、`ehai-rss run` 共用的桥接）在宿主重启时以退出码 1 结束，不输出原因、不重连。
- 验收管理器上首页与单项决定页加载约 10 秒，在本阶段之前的候选上也出现。
