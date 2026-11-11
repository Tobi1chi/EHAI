# 重构与推进计划（归档）

> 归档于 2026-11-07：四步已于 2026-10-03 完成，本文不再是当前指南。产品 E2E 说明见
> [开发与验收规则](../DEVELOPMENT_GUIDELINES.md#产品-e2e)，仍待决的事项见 [DECISIONS](../DECISIONS.md#待决)。

更新：2026-10-03。本文定义当前重构的顺序、边界和完成条件；
能力现状见 [STATUS](../STATUS.md)，产品顺序见 [路线图](../ROADMAP.md)。

## 为什么现在重构

2026-10-01 用户确认：先建立安全网，再收窄范围，最后拆分大文件，文档同步修改。
起因是以下已观察到的事实，不是对代码风格的泛泛不满：

| 现象 | 依据（2026-10-01） |
| --- | --- |
| 缺少自动化保护 | 无 CI；旧测试已退役，唯一产品 E2E 尚未编写 |
| 复杂度集中 | `orchestrator.py` 4133 行，`sqlite/repository.py` 2931 行，`service.py` 2242 行 |
| 提交粒度大 | 最近两次提交分别 +20k / +13k 行，schema 18→22 一次完成 |
| 范围先于收尾 | P2 总验收未完成时已推进 P4 多项实验 |
| 后端切换未收尾 | Pi Planner 仍从 `codex_protocol` 取图 IR，宿主工具使用 `redact_codex_bytes` |
| 单平台证据 | 主要运行证据来自 Windows，Linux 上 mypy 曾有 10 个平台相关错误 |

## 边界

- 重构保持行为：除下文记录的阻断修复外，不改变公开契约、状态值或授权语义。
- 一次一个逻辑变更，每步重跑产品 E2E 与静态检查；拆分以"只移动"提交为主。
- 不借重构扩展功能；旁支发现记入下文"待决事项"，不顺手修改。
- 以下核心语义在重构中必须保持：Worker 只产生候选，完成须经宿主 Gate 与证据；
  `stalled` 与 `suspended` 分开；结果未知不重放；讨论不等于批准；领域状态令牌与迁移白名单不被绕过。

## 四步顺序

| 步骤 | 用户结果 | 完成条件 | 状态 |
| --- | --- | --- | --- |
| 1. 安全网 | 每次修改都能自动确认主路径未被破坏 | 产品 E2E 入库；CI 在 Linux 跑静态检查、契约生成比对和 E2E，并在 Windows 跑 E2E | 完成：E2E 与 CI 已入库，首次 CI 全部通过（含 Windows E2E） |
| 2. 低风险清理 | 代码只保留实际使用的后端和清晰命名 | Codex 去留已决定并执行；图 IR、脱敏工具移到中性模块；`legacy_config.py` 处置；Run/Routine 命名冲突有决定 | 完成：Codex 已删除（选项 A）；图 IR 与脱敏已移出；`legacy_config.py` 已删除；命名决定为不改名、统一术语 |
| 3. 拆分大文件 | 新贡献者能按职责定位代码 | Orchestrator 拆为门面与若干职责模块；Repository/Service 按聚合拆分；幂等回执合并为一个机制；E2E 全程通过 | 完成：三个大文件已拆分，幂等回执已合并为一个机制（2026-10-02） |
| 4. 收尾 P2 | 在真实项目上完成一次可核对的开发任务 | 真实 Pi 按[手动验收](../DEVELOPMENT_GUIDELINES.md#真实-pi-手动验收)完成并记录；随后在 P3.3 与 P4 通用 Workflow 中选一项 | 完成：导入路径按手动验收完成一次，含强杀恢复（2026-10-03，见[执行记录](../records/refactor.md#2026-10-03-第四步真实-pi-手动验收与强杀恢复)）；另以 Pi Planner 规划路径做了一次补充验收，未含强杀（见[Planner 路径](../records/refactor.md#2026-10-03-第四步补充pi-planner-规划路径)）；用户选择下一步做 P3.3 |

### 步骤 3 的拆分方向

Orchestrator 按代码中已存在的边界拆分，保留 `Orchestrator` 作为对外门面：

| 模块 | 内容 |
| --- | --- |
| 图就绪判断 | `ready_nodes`、分支终态/失败判断等纯函数（因抛出应用层 `OrchestrationError`，留在 `application/orchestration/readiness.py`，未移入 `domain/`） |
| Attempt 生命周期 | queue / start / retry / timeout / interrupt / fail |
| 校验与 Gate | Check 执行、`finish_pending_gate`、人工判定 |
| 分支评估 | Evaluator 候选校验、`_record_branch_selection` |
| 采纳与接续 | adoption、`accept_adopted_result` |
| 人工介入 | intervention、`waiting_for_*` |

`sqlite/repository.py` 与 `application/service.py` 按聚合拆分（note / workflow / connector 本来就在独立模块中）。
幂等回执原有四处实现（核心 `CommandReceipt`，以及 Workflow、Connector、路由实验各自的 `receipt/remember`），
已合并为一个共用机制（[idempotency.py](../../src/ehai/application/idempotency.py)，见
[执行记录](../records/refactor.md#2026-10-02-第三步幂等回执合并)），供后续自定义 Workflow 复用。

## 产品 E2E

已移到 [开发与验收规则](../DEVELOPMENT_GUIDELINES.md#产品-e2e)。

## 执行记录

执行与验证记录已移到 [refactor.md](../records/refactor.md)。

## 待决事项

2026-11-03 已决定（[路线图](ROADMAP_BEFORE_1107.md#11-03-已定的架构决定)），已在 11-03 计划第 2 步落地（见上方执行记录）：

| 事项 | 说明 | 决定 |
| --- | --- | --- |
| `--worker-timeout-seconds` | 原只用于 Codex Worker，现无使用方 | 删除 CLI 参数；多工作区登记中的同名字段读时忽略 |
| CLI 启动 scripted 宿主 Run | `ehai --api-url ... start-run` 要求 `--execution-config`，而执行配置只接受 pi | scripted 宿主下 `--execution-config` 可选 |
| 幂等键冲突的错误码 | 核心命令 409 `conflict`，Workflow/Connector/路由 409 `state_conflict`，便签 422 `invalid_request` | 统一为 409 `conflict` |
| 便签命令重放的响应 | create-note / add-note-message / decide-note 同键重放返回便签当前状态 | 改为返回原回执，与其它命令一致 |

仍待决：

| 事项 | 说明 |
| --- | --- |
| 幂等重放返回值 | 重复人工判定返回当前 Run 状态而非原回执；如需原回执语义需单独设计 |
