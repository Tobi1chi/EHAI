# ADR 0001：P1 SQLite 持久化策略

- 状态：已接受
- 日期：2026-08-31
- Roadmap：P1（I0、I2、I4）

## 目的

为 P1 的单进程 Python Execution Plane 固定最小 SQLite 使用方式，使当前执行状态、追加式 `Event` Log 和 `Checkpoint` 能够原子提交并在重启后恢复，同时避免把 P1 扩展成完整 Event Sourcing 或通用数据库层。

## 决策

P1 必须使用 Python 标准库 `sqlite3`，不引入 ORM。数据库文件由应用配置指向项目数据目录；测试必须使用隔离的临时数据库。

每个应用用例必须在一个显式 Unit of Work 中使用一条连接。写入当前状态和对应 `Event` 必须处于同一个事务，写事务使用 `BEGIN IMMEDIATE`，成功后提交，任一写入失败则整体回滚。不得在 Repository 内部隐式提交。

每条新连接必须设置：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

P1 同一时刻只运行一个 `Attempt`，所以不实现写连接池、跨进程锁服务或并发调度。`busy_timeout` 只用于吸收短暂的读写竞争；超时后必须返回可追踪错误，不得无限重试。

Schema 采用按序、只前进的 SQL migration，并通过 `PRAGMA user_version` 记录版本。启动时必须在事务内应用尚未执行的 migration；检测到高于当前程序支持范围的版本时必须失败关闭，不得猜测降级。

数据库存储两类数据，但不混淆其语义：

- 关系表保存当前可查询状态，包括 `Project`、`Goal`、`PlanRevision`、`PlanNode`、`Branch`、`Run`、`Attempt`、`CheckResult`、`Checkpoint` 和 `Artifact` 元数据。
- `event_log` 只追加已经发生的 `Event`，使用单调递增的数据库序号作为订阅 offset，同时保留领域 Event ID。P1 不通过重放所有 Event 重建当前状态。

应用分配的领域 ID 使用小写、带连字符的规范 UUID 字符串，默认由 UUID v4 生成；全零 UUID 保留为无效哨兵，不得作为领域 ID。时间统一为 UTC、序列化为带 `Z` 且固定六位微秒的 RFC 3339 字符串；JSON 使用 UTF-8、禁止 `NaN`/`Infinity`，持久化和校验摘要使用排序键与紧凑分隔符的确定性编码。同一约定必须用于 API、事件和 Artifact 元数据。

`Artifact` 内容必须写入不可变文件存储，SQLite 只保存 ID、媒体类型、大小、摘要和相对路径。数据库不得保存大型 Worker 原始输出正文。

## 结果与边界

- SQLite 的原子事务可直接满足“状态与 Event 同时提交”和连续 Event Offset 的 P1 要求。
- WAL 允许事件订阅读取与单写入者并存；它不代表 P1 支持多 Worker 并发。
- 不使用 ORM 让状态转换继续由 Domain/Application 层拥有，Repository 只负责映射和持久化。
- `PRAGMA` 是逐连接设置；连接工厂必须集中配置，测试必须覆盖漏配风险。
- 数据库备份或复制必须同时处理数据库文件及其 WAL 状态；P1 不承诺在线热备份。

## 未选择方案

- **完整 Event Sourcing**：超过 P1 范围，且 Roadmap 明确要求 SQLite 同时保存当前状态和追加式 Event Log。
- **SQLAlchemy/其他 ORM**：P1 模型和查询面较小，引入迁移框架与对象生命周期语义的收益不足。
- **每次写入后全局文件锁**：单 `Attempt` 调度下没有必要，并会提前引入 P2 并发设计。
- **内存数据库作为生产默认值**：无法满足进程重启恢复；仅可用于隔离单元测试。

## 可逆性

Repository 与 Unit of Work 必须是应用层 Port。以后可以增加其他数据库 Adapter，而不改变 Domain 状态机、`PlanGraph`/`ExecutionTrace` 边界或公开 Command/Event Schema。迁移只前进，避免依赖 SQLite 特有行为进入领域层。
