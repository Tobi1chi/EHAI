# 多工作区后端

> 用法 · 只写已实现的命令与契约；执行记录见 [records/workspace-manager](records/workspace-manager.md)。

EHAI 可以通过一个本机管理入口托管多个 workspace。每个 workspace 有独立的核心进程、
SQLite、Artifact 目录、Planner 配置和 Worker 容量；工作区内继续使用原来的 Run、Gate、
便签与恢复机制。单工作区 `ehai-api` 入口继续可用。

## 启动与登记

推荐使用本地引擎命令，无需手写登记 JSON 或提供 `--api-url`：

```sh
uv run ehai init
# 在本终端设置 init 中声明的凭证环境变量，然后：
uv run ehai up
uv run ehai add /absolute/path/to/repository
# 已用 init --harness codex 配置 Codex 时，可按工作区覆盖：ehai add <路径> --harness pi
uv run ehai status
uv run ehai doctor
uv run ehai down
```

配置和非交互参数见 [安装与私有 Pi 配置](USAGE.md#安装与私有-harness-配置)。
`EHAI_HOME` 默认 `~/.ehai`；管理数据在其 `manager/`，PID/本地控制令牌在 `manager.pid`，
日志在 `manager.log`，子宿主日志在 `manager/workspaces/<id>/child.log`。
PID 文件保留为上次启动记录，是否运行以令牌保护的管理器健康探测为准，不凭 PID 猜测或杀进程。

`up` 使用当前 Python 环境后台启动 `ehai-manager` 的同一入口，读取 config 中的端口、
总容量和 UI 目录；目录存在时传 `--ui-dir`。健康后显示 `/ui/` 地址并逐个显式启动已登记工作区，
某个工作区启动失败会报告并返回非零，管理器继续运行。重复 `up` 只报告已有进程，
不会再次启动工作区。没有 UI 构建时会提示构建方法，构建后 down/up。

`add [path]` 默认为当前目录；工作区 ID 从目录名转小写、将非法字符换为 `-`、截断至 64 字符，
空名称用 `workspace`，Windows 保留名加 `workspace-` 前缀；可传 `--workspace-id` 明确指定。
省略 ID 且目录名派生的 ID 已占用时，依次尝试 `-2`、`-3`，截短前缀以保持 64 字符上限。
目录必须存在且是 Git 工作树根；子目录错误会提示根路径。
它调用已有登记和启动接口，沿用路径隔离、配置固定和容量预留限制；
不批准计划、不启动 Run、不授权模型执行。相同配置登记保持幂等。

```sh
uv run ehai add /absolute/path/to/repo --workspace-id project-a \
  --allow-command '["uv","run","pytest"]' --git-permission git.read
```

`--allow-command JSON_ARGV`、`--git-permission`、`--shell` 都可重复；默认三个列表为空。
自定义 Shell 和 Git 权限仍受核心执行授权规则约束。

### 从 App 选择文件夹

`ehai up` 给管理器显式传入 `--local-home <EHAI_HOME>`；管理器只使用该位置的用户配置，
与 CLI `ehai add` 调用同一登记构造函数。手动管理器没有此参数时不读取默认 `~/.ehai`。

- `GET /api/v1/local`（`getLocalEngine`）：`{data: {available, home, provider, model}}`，
  不含凭证。没有 local-home 时 available=false，其余字段均为 null。
- `POST /api/v1/workspaces/from-folder`（`addWorkspaceFolder`）：
  `{path: string, workspace_id?: string, allowed_commands?: string[][], git_permissions?: string[], available_shells?: string[]}`；
  返回与 `startWorkspace` 相同的 `{data: WorkspaceDescriptor}`。权限列表省略为 `[]`（null 非法），
  workspace_id 省略或 null 时自动派生。登记后立即启动，仍遵守目录隔离、不可变配置和容量预留。

无 local-home 时添加返回 409，提示通过 `ehai up` 启动。无效目录、非 Git 或非工作树根返回可读的 422；
同一路径（解析符号链接后）已登记返回 409，并说明已有 workspace_id。显式 ID 已绑定其他配置返回 409。
启动冲突或失败保留登记，可查状态并修复后 `start-workspace`；重试文件夹添加会报告路径已登记。
这与 CLI `ehai add` 的同配置重复登记幂等行为有所区别；两者推导的运行配置一致。
读取 local、登记和启动工作区都不授权 Run 或启动模型任务。

CLI 为 `get-local-engine` 和 `add-workspace-folder --file <请求JSON>`（使用管理根 `--api-url`）；
MCP 为 `get_local_engine`、`add_workspace_folder`，写入仍传 `request_json`，契约由 `get_request_schema` 返回。
`status` 显示守护进程和各工作区的真实生命周期状态；离线时只列出登记信息并说明健康未知，返回 1。
`doctor` 逐项检查配置、Node/Pi 版本与私有设置、终端/守护进程凭证变量存在性、
管理器和工作区运行时健康；`--probe` 另经本机 Hub 启动一次 Pi 会话核对模型与思考级别（不调用模型，见
[USAGE](USAGE.md#安装与私有-harness-配置)）；全部通过返回 0，检查失败返回 1，命令本身出错返回 2。

`down` 通过私有本地控制接口逐个调用正常停止，再退出管理器；
有活动执行、规划或写请求时返回冲突，须先完成或暂停相应任务；子进程停止超时仍沿用
supervisor 现有的清理与失败报告行为，`down` 不另行发送强制终止请求。
停止不具有跨工作区原子性：遇到冲突时前面已停止的工作区保持停止，
用 `ehai up` 或 `start-workspace` 重新启动。
管理器仍运行时，重复 `up` 只报告状态，应使用下方 `start-workspace`；
`up` 批量恢复登记工作区发生在管理器重新启动后。
管理器继承启动终端的环境，改凭证后 down/up；健康不代表真实模型调用已验证。

### 高级/手动管理器入口

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

## Web 工作台

先在 `control-plane` 构建页面（`npm.cmd ci`、`npm.cmd run web:build`），再让管理器同源提供：

```powershell
uv run ehai-manager --data-dir 'D:/ehai-data/manager' --port 8765 --ui-dir control-plane/web/dist
```

浏览器打开 `http://127.0.0.1:8765/ui/`。页面只调用本管理器及其 `/workspaces/{id}` 代理，
不读数据库、不保存凭证；未传 `--ui-dir` 时不提供页面。开发时可运行 `npm.cmd run web:dev`，
它把 API 请求转发到 `EHAI_MANAGER_URL`（默认 `http://127.0.0.1:8765`）。
页面范围与边界见 [control-plane README](../control-plane/README.md#web-工作台)。

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

管理器还提供跨工作区线程读取 `GET /api/v1/threads`，以及以下生活绑定与注意力队列接口：

- `GET /api/v1/life-binding`（`getLifeBinding`）返回 `{data: {binding}}`；binding 为
  `{workspace_id, project_id}` 或 null。`PUT /api/v1/life-binding`（`setLifeBinding`）接收
  `{binding}` 并返回同样结构；字段必须完整，不接受额外字段，null 清除绑定。只检查工作区已登记，
  不读取核心验证项目，因此核心停止时也能设置。绑定用私有 JSON 文件保存于管理器数据目录的
  `life-binding.json`，不保存在业务核心中。
- `GET /api/v1/attention`（`getAttention`）返回 `{data: AttentionData}`，包含读取时间
  `observed_at`、`life_binding`、`needs_you`、`in_progress`、`done`、`done_truncated`、
  `unavailable`、`notes` 与固定为 false 的 `consistent_across_workspaces`。
  每项带源类型、工作区、项目、对象 ID、标题、核心状态、下一步、Run ID 和时间；与绑定的工作区和
  项目均相符时标签为 life，否则为 work。线程需要注意、等待批准、暂停或失败时归入“需要你处理”，
  规划、运行或尚未开始时归入“进行中”；未归属现有线程的未决待办单独归入“需要你处理”，不重复列出
  已由线程承载的待办。仅绑定生活项目的待办参与聚合：过期未完成项需要处理，其余未完成项进行中，
  已取消项不显示。完成的线程和生活待办只保留最近七天，合计最多二十项，超过时标记
  `done_truncated`。需要处理按时间从旧到新，另两组从新到旧，空时间排最后。
  未绑定或生活项目不存在时以简短英文 `notes` 说明；工作区读取失败则该工作区不返回条目，
  在 `unavailable` 中保留工作区 ID、既有错误代码和说明，不影响其他工作区。聚合只读、不重试，
  也不是跨核心一致快照。页面与其他管理器内调用者可复用同一队列计算。

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
`/{workspace_id}/stop`、`/{workspace_id}/execution-config`、`/from-folder`；本地配置摘要为
`/api/v1/local`，总览为 `/api/v1/workspace-overview`。
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
管理 Client 新增 `getLocalEngine()`、`addWorkspaceFolder(request)`；核心 Client 新增
`createIntegrationBranch(runId, integrationId, {branch})`，语义见 [Git 自动整合](USAGE.md#git-自动整合)。

实际薄验证、失败修复与未覆盖范围见 [运行证据](EVIDENCE.md#p3-平台后端)。

2026-09-23 已完成本机同一 Pi/Go 模型的真实跨 workspace Planner/Worker/Reviewer 并发、
同工作区双 Planner 容量限制与分别人工 Gate 验收。Windows 上托管核心的 Git 子命令现在显式
使用空 stdin，保留需要 input_bytes 的 Git 输入，避免继承父进程监护管道而阻塞。
管理入口限制退出时等待 HTTP 请求的时间，再进入子宿主清理；这不是整个清理过程固定 15 秒完成的保证。

## 执行记录

执行与验证记录已移到 [workspace-manager.md](records/workspace-manager.md)。

