# Pi 后端迁移：实现与验证记录

## 当前结论（2026-09-14）

按用户最新决定，直接删除自研 Agent Runtime，不维持双底座。完整 Pi 0.85.1 的接线已覆盖
Planner、Worker、阶段 Reviewer、过程 Planner/边界 Reviewer。EHAI 只拥有角色提示、业务
上下文、授权工具、输出校验、编排、Gate 和成果来源。正常用法见 [Usage](USAGE.md)。

这不是产品验收完成：目前证据覆盖静态接口、无模型 RPC 和旧数据读取；当前进程没有可用于
真实模型调用的配置凭证，没有发送模型请求，没有运行唯一产品 E2E。

## 删除与保留

已删除 application/builtin_agent.py、application/builtin_runtime.py、
infrastructure/openai_responses.py、infrastructure/builtin_visualizer.py，以及 Python OpenAI SDK
及不再需要的传递依赖。旧 Built-in CLI 选择和模型/Responses/自研循环参数已移除。
旧 Visualizer 未迁移成新角色，不声称仍可调用。

保留并抽出 agent_contracts、agent_roles、agent_trace、host_tools、agent_traces；
它们是宿主工具/审计/角色契约，不是模型循环。builtin 数据表名、历史事件词汇和配置解码仍保留，
以读取已存在的记录。新 Pi WorkerKind=pi，使用 ExternalExecutionRef；
builtin 枚举仅用于历史和确定性 Fake，不提供旧模型执行实现。

不删除旧数据库、产物或试用工作区；旧 Built-in 授权不转成 Pi，不重放旧 reasoning 或工具请求。
旧 Usage 的完整工作树内容保存在 USAGE_PRE_PI.md，当前 Usage 只描述新入口。

## 实施边界

- 完整上游依赖在 agent-backends/pi，npm lockfile 固定；Node >=22.19.0。工具扩展作为
  Python 包资源 pi_business_tools.mjs 分发，通过 Pi 公开扩展 API 加载，没有改上游源码。
- pi_config 要求绝对路径、明确 Provider、环境变量名白名单和原生 settings/models，
  检查 CLI 包/版本与 Node 版本。授权保存配置指纹；恢复遇到漂移拒绝。
- 每个角色使用私有 Pi 目录和原生 Session，禁用全局资源发现、工作区插件和原生文件/Shell 工具；
  只注册当前角色授权的 EHAI 宿主工具，顺序执行，请求原生 strict JSON Schema 采样。
- Pi 拥有 Responses、历史、压缩；EHAI RPC 只关联请求，不重连重发，不解释 reasoning。
  native provider/model/thinking 必须精确匹配配置，不接受静默回退。
- prompt ACK 不等于完成，agent_end 不等于收敛；必须有经过宿主校验的 finish 结果，
  再收到 agent_settled，才能写入本次角色完成事实。完成结果仍须经过 EHAI 的成果捕获与 Gate。
- 业务消息先排入 Pi steer；仅在原生 context 出现、进入 before_provider_request 后，
  才记录 message/received 和更新 Mailbox 投递标记。记录边界明确为
  native_provider_input_prepared；这是请求构建证据，不是远端接收或模型理解的证明。
- 安全用量投影只包含原生整数 token 计数和估算 cost，不公开消息正文/不透明 reasoning。
  原生目录本身可能包含敏感信息，应作为私有运行数据保存。估算 cost 不是服务商账单，
  尚未接到 EHAI PROVIDER_USAGE_RECORDED 的预算累计，不宣称成本预算已经生效。
- 保留无隐式模型时限的长运行模式；可选 Attempt deadline、宿主尝试/授权预算仍归 EHAI。
  未继续实现旧每步/工具次数/历史字节数预算，也不擅自增加 120/300 秒截断。初始集成禁用 Pi 自动重试，
  不让底层错误在未知结果下反复消耗 token。
- 取消尝试 clear_queue/abort，再有界关闭拥有的 Pi 进程。宿主工具的进程/写入清理由
  HostToolRuntime 与 CodeRuntimeConnector 负责。不是任意进程树隔离，不是副作用回滚。
  中断/断线/超时结果未知时保留审计，不自动重发；恢复仍受有效 handoff/上游成果与非本地副作用限制。

## 配置与公开接口

新增 --pi-config；规划选择 --planner pi / --planner-model / --planner-reasoning-effort。
前台 execution.json 使用 worker_kind=pi 和 pi 对象；API 宿主使用 --worker pi /
--agent-model / --agent-reasoning-effort / --worker-capacity 及明确的宿主工具权限参数。
新 HTTP 输入不接受 builtin；旧查询仍接受历史 builtin，新增 pi 和 backend 审计事件。
commands Schema、OpenAPI 和 TypeScript Client 已同步；模型端工具契约仍待真实 Provider 验证。

脱敏示例在 agent-backends/pi/examples，不含 key，不虚构模型容量。兼容 endpoint_capabilities
仅为历史 JSON 词汇，不控制 Pi。原生配置的 Provider API、URL、压缩和模型元数据才是后端设置。

## 本次实际验证

- 分支 codex/pi-agent-backend；没有提交或推送。删除前源码恢复点：
  C:/Users/28262/AppData/Local/Temp/ehai-before-runtime-removal-0650c730d44a4187840f7a025a8bfecf。
  更早快照为 ehai-before-pi-5fa91413625147f8ac832a186e3c97ca。快照不包含依赖/虚拟环境/旧数据库，
  是本机临时恢复点；分支自身不冻结未提交内容。
- 修复 inspect-agent 的 check_node_version 导入回归后，经正常 CLI 使用 Node 24.19.0
  启动锁定的 Pi 0.85.1；get_state 和并发 get_available_models/get_session_stats 返回匹配应答。
  空白 Session、消息数 0、stderr 0、工具禁用、模型请求 0、进程已关闭。
  返回 control_channel_verified=true、worker_integration_available=true、
  model_execution_verified=false；指定数据库和产物路径仍不存在。
- 从旧已完成自举库通过只读 SQLite backup 制作
  C:/Users/28262/AppData/Local/Temp/ehai-pi-history-pkfztbd_/state.sqlite。
  正常 get-result / get-trace 可查询 Run 5bcdbe79-0c1d-49dc-85ae-0e7f6b1c4f24，
  status=completed、22 个 Attempt。没有继续或重跑旧 Run。
- Ruff、格式、mypy（103 个源文件）、扩展 JavaScript 语法与生成 Client typecheck/build 通过。
  无新增常驻测试；Schema 生成器使用临时空库的真实应用装配，不开启 lifespan 或调用模型。

## 尚需真实入口证据

### 2026-09-14 角色入口收口

正常 CLI 发现：仅提供 --planner-model 而省略 --planner，会忽略模型选择并用 single Planner
生成固定方案。隔离目录 ehai-pi-entry-dd425379e31843388beaf2728e9f16fb 下，未配置后端的模型
ehai-unconfigured-model 实际返回了草稿 2df4179a-bd47-4d9e-b184-d373ddfe0f25，退出 0。
修复默认解析后，重试同一正常命令明确返回 Pi Planner requires --pi-config，不回退模板。

现在提供 Pi 配置或规划模型会默认选择 Pi Planner；API P2 宿主提供 Pi 配置，或指定 agent-model，
默认选择 Pi Worker。显式后端选择优先，无模型演示与只读查询仍保留；不自动启用过程调整权限。
Worker 预设的 system_prompt 原来保存后未传给角色配置，现已传入同一 PiRoleRunner。
新 Worker 工具 profile 使用 pi 前缀，新 Planner 提案元数据标记 planner.pi.completed；旧记录不重写。

通过正常 ehai-api 在隔离 api.sqlite 启动，无 --worker/--planner 显式选择：
GET /api/v1/workers/profiles 返回 kind=pi；创建 Project/Goal 后调用 POST /api/v1/planning/discuss，
进入 Pi 角色层并因故意未设置的 EHAI_PI_ENTRY_PROBE_KEY_NOT_SET 环境变量而停止（409），
持久化失败讨论 ef01f885-afbb-4a9c-baea-fa5047ab6d6e，没有生成方案或发出模型请求。
检查宿主已停止。这只验证生产装配、路由与失败前置行为，不证明工具执行或任务验收。

### 后续验证

1. 配置实际 Provider、精确模型和授权环境凭证，在隔离 workspace 调用正常讨论/执行入口。
2. 确认 Provider 接受全部工具定义；实际调用图/候选/审查工具，并检查持久结果。
3. 验证 finish 批次顺序、turn_end 停止与 agent_settled；不能只凭一次 HTTP 200。
4. 验证消息入上下文、压缩、运行中取消及宿主写入收敛，确认未知效果不会被自动重试。
5. 进行获批任务、成果、Reviewer、Gate 的纵向验证，再回到唯一产品 E2E。
   没有这些证据前不能称迁移已验收，更不能宣称已解决 token 长尾问题。

依据：[ADR 0005](adr/0005-external-agent-backends.md)、[产品范围](PRODUCT_SCOPE.md)、
[执行模型](EXECUTION_MODEL.md) 和锁定 Pi 包内 rpc/sdk/settings/models 文档。
