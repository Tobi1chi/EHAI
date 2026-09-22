# 多工作区后端

EHAI 可以通过一个本机管理入口托管多个 workspace。每个 workspace 有独立的核心进程、
SQLite、Artifact 目录、Planner 配置和 Worker 容量；工作区内继续使用原来的 Run、Gate、
便签与恢复机制。单工作区 `ehai-api` 入口继续可用。

## 启动与登记

在仓库根目录使用 `uv sync` 安装当前入口，然后启动管理服务：

```powershell
uv run ehai-manager --data-dir 'D:/ehai-data/manager' --port 8765 `
  --max-worker-capacity 8 --max-planner-capacity 4
```

管理入口只监听本机 `127.0.0.1`。数据目录只能由一个管理进程持有。
在管理器中登记仓库，不会自动启动 Worker、批准方案或授权执行。

将下面内容保存为 `workspace-a.json`；工作区和 Pi 配置文件使用本机实际存在的绝对路径。
Pi 配置格式见 [Usage](USAGE.md)，其中凭证仍只引用允许的环境变量。

```json
{
  "workspace_id": "project-a",
  "path": "D:/projects/project-a",
  "runtime": {
    "worker_kind": "pi",
    "planner_kind": "pi",
    "worker_model": "YOUR_WORKER_MODEL_ID",
    "planner_model": "YOUR_PLANNER_MODEL_ID",
    "worker_capacity": 2,
    "planner_capacity": 1,
    "pi_config_path": "D:/private/ehai/pi-backend.json"
  }
}
```

```powershell
uv run ehai --api-url http://127.0.0.1:8765 register-workspace --file workspace-a.json
uv run ehai --api-url http://127.0.0.1:8765 start-workspace --workspace-id project-a
uv run ehai --api-url http://127.0.0.1:8765 list-workspaces
```

登记相同内容是幂等的，不同内容不能覆盖同一 ID。工作区路径不能相同或相互嵌套，
也不能与管理器数据目录相同或相互包含；管理数据必须放在所有可执行工作区之外。
Pi 的原生设置会复制到此 workspace 的私有配置目录并固定指纹，原配置后续变化不会静默作用于它。
允许的命令、shell、Git 权限和 Check 设置可在 `runtime` 明确提供，默认均不增加副作用权限。
首版固定登记时的运行配置，不提供注销、原地配置替换或既有 Run 跨 workspace 迁移。

## 在指定 workspace 执行业务

把原来单宿主的 API 地址改为带工作区前缀的地址，其他业务命令保持相同：

```powershell
uv run ehai --api-url http://127.0.0.1:8765/workspaces/project-a list-projects
uv run ehai --api-url http://127.0.0.1:8765/workspaces/project-a create-project `
  --name 'Project A' --idempotency-key project-a-create
uv run ehai --api-url http://127.0.0.1:8765/workspaces/project-a get-planner-capacity
```

继续创建 Goal、讨论计划、批准并授权执行，参照 Usage。启动 Pi Run 前，经管理接口获取精确
执行配置，将响应中的 `execution_config` 对象保存为执行配置文件，审阅后交给现有
`start-run --execution-config ... --authorize`；读取配置本身不构成授权。

```powershell
uv run ehai --api-url http://127.0.0.1:8765 get-workspace-execution-config `
  --workspace-id project-a
uv run ehai --api-url http://127.0.0.1:8765 get-workspace-overview
```

总览按 workspace 分组返回项目详情、有效成果和人工待办。不可用的工作区带明确错误，
不展示成空项目或零待办；不同核心之间不是同一个数据库快照。
所有命令、模型请求和结果只发送到明确选中的 workspace，不按对象 ID 猜测来源，不自动换宿主重试。
各 workspace 的事件 consumer、offset、对象 ID 与业务幂等键都属于自己的核心。

## Planner 并发

每个宿主默认 `planner_capacity=1`。独立目标可拥有各自的规划讨论和物理 Pi Session，
同一个讨论仍拒绝上一轮尚未结束时启动下一轮。多个 Planner 会话共用该 workspace 登记的模型配置，
没有新增多个 Planner 共同编辑一个计划的编排器。

规划、修订、便签模型决定、过程提案和过程审查共用容量；满额在写入规划/便签决定意图前返回
`409 planner_capacity_exceeded`。调用方可读取容量，待空闲后重新提交。已受理但结果未知的模型操作
仍须按原接口核对，不能把容量释放误当作请求失败或重放许可。

单宿主也可设置 `ehai-api --planner-capacity 2`。Planner 容量独立于 Worker 容量，
不是模型 Provider 的速率限制。管理层按已启动工作区配置的容量总和预留额度，超额拒绝启动；
首版不动态借用其他 workspace 的空闲额度，也不实现跨项目公平调度。

## 停止、重启与失败

```powershell
uv run ehai --api-url http://127.0.0.1:8765 stop-workspace --workspace-id project-a
uv run ehai --api-url http://127.0.0.1:8765 start-workspace --workspace-id project-a
```

有活动执行或规划时，正常停止返回冲突；先通过原业务接口暂停或完成任务。
管理器正常退出会关闭其托管子进程。子进程监视父进程连接，在父进程退出时结束宿主；
未确认的运行结果仍由原核心恢复规则处理，不保证任意强杀时序下执行恰好一次。
重启管理器保留登记、各核心数据库与成果，但不会自动启动所有工作区或重放模型调用。
启动失败可查询 `failed` 和失败类别，明确修复后再启动。

## HTTP / MCP / TypeScript

管理路由位于 `/api/v1/workspaces`，含登记、查询、`/{workspace_id}/start`、
`/{workspace_id}/stop`、`/{workspace_id}/execution-config`；总览为 `/api/v1/workspace-overview`。
业务路由位于 `/workspaces/{workspace_id}/api/v1/...`，只转发原核心公开路由，支持 SSE。
管理 OpenAPI 为 `/openapi.json`，核心契约为 `/core-openapi.json`，工作区契约为
`/workspaces/{workspace_id}/openapi.json`。

```powershell
uv run ehai-mcp --api-url http://127.0.0.1:8765
```

连接管理根地址时，MCP 同时提供管理工具和核心工具，核心工具需要明确 `workspace_id`。
只连接一个工作区时使用 `--api-url http://127.0.0.1:8765/workspaces/project-a`，核心工具
保持单宿主参数形状。`get_request_schema` 分别返回真实的管理或核心写入契约。

生成的 `EhaiWorkspaceManagerClient` 提供管理操作，`manager.workspace("project-a")`
返回绑定该工作区的 `EhaiApiClient`。页面仍只消费这些公开能力。

实际薄验证、失败修复与未覆盖范围见 [P3 实施记录](P3_IMPLEMENTATION_PLAN.md)。

2026-09-23 已完成本机同一 Pi/Go 模型的真实跨 workspace Planner/Worker/Reviewer 并发、
同工作区双 Planner 容量限制与分别人工 Gate 验收。Windows 上托管核心的 Git 子命令现在显式
使用空 stdin，保留需要 input_bytes 的 Git 输入，避免继承父进程监护管道而阻塞。
管理入口限制退出时等待 HTTP 请求的时间，再进入子宿主清理；这不是整个清理过程固定 15 秒完成的保证。
