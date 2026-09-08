# aws-sub2 Responses 端点能力实测

## 结论与适用范围

2026-09-06 对用户授权的测试端点 `https://aws-sub2.20000719.xyz` 逐项验证。
当前账号/路由可用于 EHAI 的流式模型与工具交互，但不能按完整 Responses 服务端会话能力使用。
端点可信是测试授权，不代表所有协议功能兼容。
用户于 2026-09-08 确认该端点经 Sub2API 反代；以下 2026-09-06 实测仍只代表该 HTTP 账号/路由，
不是所有 Sub2API 部署、账号类型或传输通道的能力声明。

建议使用 **流式请求 + EHAI 本地上下文重放**；禁用 background、Schema `uniqueItems`，
不依赖 `previous_response_id`、Response 查询或幂等创建来恢复执行。
这不是要求换模型、放弃 OpenAI SDK，或每轮重新创建 Agent Session。

- 验证窗口：2026-09-06 11:44–11:47 UTC。
- Base URL 使用上述根路径，没有自动添加 `/v1`；实测路由为 `/models`、`/responses`、
  `/responses/{id}`。结论不外推到其他路径、账号、模型或中转站。
- 模型组合：`gpt-5.6-luna` / `reasoning.effort=max`，
  `gpt-6-astra` / `reasoning.effort=high`。
- 使用真实 HTTP、固定的小型文本和工具 Schema；无伪造响应、无代理内部状态修改，
  不发送项目源码或真实业务内容。原始凭证不进入脚本、证据文件或 Git。
- 这是实际代理报错后的协议诊断，不是新的产品 E2E，也不是全 API、全模型或并发压测。
  本轮仅验证和更新文档，没有修改生产 Adapter、全局设置或已有执行配置。

## 逐项结果

“通过”仅指表内样例实际成功；接受参数不等于证明代理严格执行其全部语义。

| 能力 | 实际请求与证据 | 结论及 EHAI 使用方式 |
| --- | --- | --- |
| 模型列表 | `GET /models` 返回 200 JSON，包含上述两个模型 ID | 列表可查询；可用性另以真实调用确认 |
| Luna/max | 200；回显模型和 `effort=max`；工具名称和 `["alpha","beta"]` 参数完全匹配 | 当前组合可调用，不据回显证明底层模型身份或实际推理强度 |
| Astra/high | 200；回显模型和 `effort=high`；相同工具样例参数匹配 | 当前组合可调用；不把 Luna 的全部负向结果直接外推给 Astra |
| 流式 Responses | 200 `text/event-stream`，收到 `response.created` 和 `response.completed` | 可用；必须读取事件到明确终态，不能只看 HTTP 200 |
| 流式输出重建 | 补查中的最终事件 `output` 为空，实际工具/文本在 `response.output_item.done` 等前序事件 | 不能只读最终事件的 `output`；EHAI 现有 Adapter 已保留前序输出项与文本增量 |
| 自定义工具与基础严格 Schema | `strict=true`、对象 required 字段、`additionalProperties=false`、字符串数组，配合 `tool_choice=required`、`parallel_tool_calls=false`，返回准确参数 | 该 Schema 样例可用；不代表全部 JSON Schema 关键词或服务端强约束已验证 |
| 完整上下文及工具结果重放 | 不传续接 ID，发送原用户消息、真实 function call、匹配 call ID 的 function output 和新消息；返回 `ACK alpha,beta`，没有再次调用工具 | 可用；本地历史可保持语义连续，不必依赖服务端续接 |
| `previous_response_id` | 使用本轮刚完成的真实 Response ID，返回 400，正文明确拒绝当前 HTTP 账号模式 | 当前账号/路由不可用；不要每轮先失败再回退 |
| `background=true` | 与普通请求相比添加后台参数，返回 400 `upstream_error` | 当前样例失败，配置禁用；错误正文未提供更细原因，不宣称整个服务永远不支持 |
| Schema `uniqueItems=true` | 在已成功的工具 Schema 上仅增加该关键词，返回 400 `upstream_error` | 当前 Schema 路径不可用，发送前移除；仅有通用错误，不冒充上游精确诊断 |
| 已存 Response 查询 | 创建时 `store=true`，随后对刚完成的真实 ID 执行 `GET /responses/{id}`，返回 404 `404 page not found` | 当前路由无法查询；不能依赖 retrieve 进行断流恢复，也不能据此判断代理内部是否存储 |
| `stream=false` 非流式 JSON | 请求显式设为 false，仍返回 200 **事件流**；按 SSE 解析可得到 `CAP_OK` | 不符合所请求的非流式 JSON 形态；当前统一使用 streaming，不能把此项记成非流式支持 |
| `store=true` / `store=false` | 两种取值的流式创建都被接受，结果可读取 | 仅确认参数接受；不证明留存策略、零留存或可查询性 |
| JSON Schema 文本输出 | `text.format.type=json_schema`、`strict=true`，样例返回可解析的 `{"ok":true}` | 基础输出样例通过；没有证明任意 Schema 的服务端强制约束，EHAI 本轮也未新增此入口 |
| `Idempotency-Key` | 相同正文和同一 key 连续提交两次，均 200，但返回不同 Response ID | 未观察到响应去重，不能视为已保证幂等创建；未知结果时不要盲目重新 POST |
| 用量与关联信息 | 成功响应含 usage；HTTP 返回 `x-request-id`；诊断另发送 `X-Client-Request-Id` | 可记录返回的用量与请求 ID；计费准确性、客户端 ID 在代理平台能否检索未验证 |

所有最终输出/参数的样例判断都基于重建后的内容，而不只是 status。
用量字段及推理配置回显不是模型质量、实际计算量或费用审计。

## 关键错误与代理对账

续接错误原文：

```text
previous_response_id requires an OpenAI API-key account for HTTP requests
```

background 与 uniqueItems 两项的原文均为：

```json
{"error":{"message":"Upstream request failed","type":"upstream_error"}}
```

这两项是主动验证不兼容能力时触发的失败，不应计入正常兼容配置的健康成功率。
同时也不能把它们从请求统计中抹去。

| 项目 | HTTP | 服务端 `x-request-id` |
| --- | --- | --- |
| `uniqueItems` | 400 | `8b8547fa-991d-4c08-bac6-414d68612f47` |
| background | 400 | `3478d57f-a23e-40ef-97d4-278dc24872a4` |
| `previous_response_id` | 400 | `b0b9c913-6fc7-4f6b-8165-92fac0b4ddb3` |
| retrieve | 404 | `499cb50c-469f-422d-b524-9ea91f7b12ba` |
| `stream=false` 实际返回 SSE | 200 | `ee06553a-fe57-462c-ba77-405ec0bf6958` |
| 幂等键首次创建 | 200 | `e5e581f6-02d8-4066-9233-10e88fe25f48` |
| 同幂等键再次创建 | 200 | `908eb2a2-3d44-463f-a93b-b87c6c74cc08` |

相同幂等键对应的两个 Response ID：

```text
resp_0d61048678e72213016a9d522d4bbc87d081707659944d4cc3
resp_0bb2cbf9714161f5016a9d522f630487d0937a4e0d27d7fbc1
```

这直接表明不能假定每个重复 POST 会拿回同一 Response；
是否额外计费、内部如何路由，仍需代理侧记录判断。

此前 11:03:33–11:03:43 UTC 的现有 EHAI Adapter 诊断也复现：
三轮逻辑调用产生五次 HTTP 请求，第二、三轮各先因续接 400，再完整重放成功。
本次逐项结果解释了这类错误，但不自动解释所有历史代理报错。

## 当前可配置项与适配状态（2026-09-08）

当前 `execution.json` 已支持以下字段，适用于本次测试账号/路由的保守配置为：

```json
{
  "endpoint_capabilities": {
    "supports_background": false,
    "supports_unique_items": false,
    "supports_idempotent_create": false,
    "supports_previous_response_id": false,
    "supports_response_retrieval": false
  }
}
```

这是完整执行配置中的一个片段，其他字段仍按 [Usage](../USAGE.md) 填写。
本轮没有代用户改写现有配置。此前 idempotent 值为 null 时，
EHAI 对非官方端点同样不把创建幂等性当成已保证能力。

2026-09-08 已接入以下能力，真实复测结果另行记录，不改写上面的历史失败：

1. 新增 `supports_previous_response_id`，false 时从本地 Session 保留的历史重放消息及工具结果，
   不发送续接 ID。默认 true 保留旧行为，不按域名或“Sub2API”名称自动猜测能力。
2. 新增 `supports_response_retrieval`，false 时不发 retrieve；丢失终态且不能安全恢复时
   抛出明确的未知结果，Built-in Worker 进入等待，前台暂停并给出 `provider_outcome_unknown`。
   未保证幂等创建时，在缺少 Response ID 的断流情况下也不重复 POST。
3. 正式 Built-in Session 持久化 `model/transport` 事件，`get-trace` 可查询关联 ID、
   HTTP 状态、错误类型、回退与流式终态；不保存密钥、请求/响应正文或原始错误正文。
   默认内置 SDK 不隐藏重试；自行注入 SDK-managed retry 的客户端不保证逐 HTTP 观测，
   事件会标识 `retry_owner`。查询截断不代表记录丢失，需留意 `session_events_truncated`。
4. 保留本地 Session、工具调用及结果的连续历史；禁用服务端续接只改变传输方式，
   不等于丢弃上下文或修改阶段 Session 的产品目标。

规划 CLI/API 的对应启动参数为 `--no-responses-previous-response-id`、
`--no-responses-response-retrieval`，并配合关闭 background、uniqueItems 与幂等创建假设。
参数及完整配置用法见 [Usage](../USAGE.md)。旧获授权配置保持原语义，不自动改变已有 Run。

### 2026-09-08 适配后的小链路复测

使用同一 Sub2API HTTP 测试端点、Luna/max 和上述五项 false 配置，
经过实际 `BuiltinAgentLoop` 与 `SQLiteBuiltinSessionStore` 完成三个工具交互 Turn。
本轮没有重跑七节点编码场景，不把协议诊断称为完整产品验收。

- 时间：2026-09-08 01:17:19–01:17:29 UTC（北京时间 09:17:19–09:17:29）。
- 三轮模型调用恰好对应三次 POST，HTTP 为 `[200, 200, 200]`，均收到 `response.completed`。
  没有 `previous_response_id`、没有 GET、没有兼容性回退或额外创建。
- 每轮真实 echo 工具参数与随机测试文本匹配；线上请求的上下文项数依次为 1、4、7，
  与本地累计用户消息、工具调用和工具结果逐项对应。
- 同一 Session 保存十八条 `model/transport` 事件。重新加载 SQLite 后，
  三个逻辑请求 ID、HTTP ID、服务端 request ID 与独立 HTTP 观测逐项对应。
  十二条重建模型消息保持一致，没有把诊断事件混入上下文，也没有在传输记录中保存密钥。
- 测试脚本及离线诊断由 Luna/max 子 Agent 准备；真实网络由主任务注入进程环境凭证执行，
  未将凭证交给脚本文件或数据库。

| 轮次 | 服务端 `x-request-id` |
| --- | --- |
| 1 | `146be285-8a17-4c6c-83c7-771a74268cd0` |
| 2 | `a6687c7c-639f-461d-8f05-15ddc278f182` |
| 3 | `1cbfa971-1d2e-4597-86b5-87685b0cb430` |

证据位于 `%TEMP%\ehai-proxy-diagnostics-2174-luna-max`：
`summary.json`、`http-events.jsonl`、
`session-42a353d61f874bbf9deea09984d7fa62.sqlite`。
Probe ID 为 `42a353d61f874bbf9deea09984d7fa62`，
Session ID 为 `0e4a4cc4-6daf-43e8-b565-d3eef8ff61e2`。

仓库外六项故障诊断通过：禁用查询后断流不 GET/重新 POST、默认可查询路径恢复同一 Response、
续接默认值与关闭后的历史重放、传输事件持久化和重载、取消唤醒未知结果等待、
进程恢复保留未知结果 WAITING 且不启动模型调用。断流及取消/恢复由本地诊断构造，
没有故意中断真实上游任务，因此不把它们宣称为代理侧断流实测。
另外，前台在启动恢复期间也观察等待 notice，避免先等恢复结束才显示人工介入提示。
Ruff、格式、mypy 以及生成 TypeScript Client 的类型检查和构建通过；没有新增常驻测试。

### 2026-09-08 七节点真实编码复跑

随后通过正常 CLI 完成 Astra/high 规划、审查批准、Luna/max 并行编码和交付。
七个 Attempt 成功，原四个 Gate 全部通过；条件路线获选，映射路线被剪枝，
最终代码只包含两个模块与 README。完整记录见
[R2 七节点 CLI 端到端复跑](../R2_IMPLEMENTATION_PLAN.md)。

完整 Session 审计统计 Planner 32 次、Worker 42 次逻辑调用，分别对应相同数量的 HTTP 请求；
合计 74 个 200 与 74 个 response.completed，零 HTTP error、非 200、fallback、retry 或未知结果。
这包含模型根据工具反馈修正操作所需的后续正常请求，不把三次 Worker 工具错误与两次 Planner
accepted=false 误记成 HTTP 故障，也不把最终成功描述成每个内部操作都一次成功。
证据在 `%TEMP%\ehai-sub2-e2e-090ee935313a4002835ab84bbd2c90be\verified-evidence.json`。

仍未验证：主动断流/失联恢复、进行中任务取消、并发限额、429/5xx 的真实恢复、
长期稳定性、大上下文上限、全部 Schema 关键词，以及 Chat Completions、Realtime、
图像/音频等不在当前 EHAI Responses 路径内的 API。不为这些能力填“支持”。

## 证据与诊断修正

本轮共 **19 次实际 HTTP 请求**：15 次 200、3 次 400、1 次 404；
首轮十次、补查九次。没有 HTTP 自动重试，也没有把回退隐藏在一个“成功次数”里。
15 次 200 不等于十五项协议能力通过，例如 `stream=false` 的响应形态仍不符合请求。

首轮诊断脚本只从最终事件读输出，因最终 `output` 为空而把内容样例标记为 false。
这属于诊断读取不完整，不是模型没有输出。修正为与现有 Adapter 一致的前序事件重建后，
补查确认 Luna、Astra、完整上下文工具结果、普通文本和 JSON 输出样例可读且正确。
首轮文件保留，不把其中的内容判断作为最终结论；首轮 HTTP 状态、幂等 ID 和错误正文仍有效。

仓库外证据目录：

```text
%TEMP%\ehai-endpoint-capabilities-148137bb092b4118aecf1993fd4d5ba6
```

- `capabilities-results.json`：首轮记录，Probe ID `b939a522367c4258b80045b67da6476a`。
- `capabilities-results-11231586c61747e0aa75353ba3258033.json`：修正读取方式后的补查记录。
- `capabilities.py`：临时诊断脚本，不提交到仓库；不要自动晋升为常驻测试套件。

本文件保存可复核的摘要及关键请求 ID，临时目录可能被系统清理。
用户更换代理路由、账号或模型后需重新验证受影响的能力，不将本次结果硬编码为所有端点的规则。
