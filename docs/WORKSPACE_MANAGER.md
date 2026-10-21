# 多工作区后端

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
uv run ehai status
uv run ehai doctor
uv run ehai down
```

配置和非交互参数见 [安装与私有 Pi 配置](USAGE.md#安装与私有-pi-配置)。
`EHAI_HOME` 默认 `~/.ehai`；管理数据在其 `manager/`，PID/本地控制令牌在 `manager.pid`，
日志在 `manager.log`，子宿主日志在 `manager/workspaces/<id>/child.log`。
PID 文件保留为上次启动记录，是否运行以令牌保护的管理器健康探测为准，不凭 PID 猜测或杀进程。

`up` 使用当前 Python 环境后台启动 `ehai-manager` 的同一入口，读取 config 中的端口、
总容量和 UI 目录；目录存在时传 `--ui-dir`。健康后显示 `/ui/` 地址并逐个显式启动已登记工作区，
某个工作区启动失败会报告并返回非零，管理器继续运行。重复 `up` 只报告已有进程，
不会再次启动工作区。没有 UI 构建时会提示构建方法，构建后 down/up。

`add [path]` 默认为当前目录；工作区 ID 从目录名转小写、将非法字符换为 `-`、截断至 64 字符，
空名称用 `workspace`，Windows 保留名加 `workspace-` 前缀；可传 `--workspace-id` 明确指定。
它调用已有登记和启动接口，沿用路径隔离、配置固定和容量预留限制；
不批准计划、不启动 Run、不授权模型执行。相同配置登记保持幂等。

```sh
uv run ehai add /absolute/path/to/repo --workspace-id project-a \
  --allow-command '["uv","run","pytest"]' --git-permission git.read
```

`--allow-command JSON_ARGV`、`--git-permission`、`--shell` 都可重复；默认三个列表为空。
自定义 Shell 和 Git 权限仍受核心执行授权规则约束。
`status` 显示守护进程和各工作区的真实生命周期状态；离线时只列出登记信息并说明健康未知，返回 1。
`doctor` 逐项检查配置、Node/Pi 版本与私有设置、终端/守护进程凭证变量存在性、
管理器和工作区运行时健康；全部通过返回 0，检查失败返回 1，命令本身出错返回 2。

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

实际薄验证、失败修复与未覆盖范围见 [运行证据](EVIDENCE.md#p3-平台后端)。

2026-09-23 已完成本机同一 Pi/Go 模型的真实跨 workspace Planner/Worker/Reviewer 并发、
同工作区双 Planner 容量限制与分别人工 Gate 验收。Windows 上托管核心的 Git 子命令现在显式
使用空 stdin，保留需要 input_bytes 的 Git 输入，避免继承父进程监护管道而阻塞。
管理入口限制退出时等待 HTTP 请求的时间，再进入子宿主清理；这不是整个清理过程固定 15 秒完成的保证。

## 模块 B 验证记录（2026-10-22）

macOS，仓库外临时目录 `/private/tmp/ehai-module-b-final-eaq033sd`，Node v25.8.1、Pi 1.3.0；
使用有空基线 commit 的临时 Git 仓库 `Sample Repo`，没有模型调用。每步输出保存在该目录
`transcript.jsonl`（无凭证值）。

| 步骤 | 输出摘要 | 退出码 |
| --- | --- | --- |
| init（全部参数） | 写 home/config.json、私有 Pi settings/models 与生成 backend；校验 Node/Pi | 0 |
| up | 后台进程 ready，UI 为 `http://127.0.0.1:60576/ui/` | 0 |
| status | 管理器 healthy | 0 |
| add 临时 Git 仓库 | 自动 ID `sample-repo`，工作区 ready；默认三种权限列表为空 | 0 |
| doctor | 配置、Node、Pi、管理器、工作区 runtime healthy；终端与守护进程均缺少故意未设置的 `EHAI_MODULE_B_MISSING_KEY` | 1 |
| down | 正常停止，保留登记 | 0 |

补充：离线 status 返回 1 并列出工作区健康未知；再次 up 显式恢复 `sample-repo: ready`；
重复 up 保持同一 PID，只报告 already running；重复 add 仍为同一工作区 ready；
重复 down 报告未运行并返回 0。UI 静态页 GET 返回 200（470 字节），未做浏览器交互验证。
未遇到沙箱端口/后台进程限制。未验证真实模型、doctor 凭证齐全、交互确认、活动任务停止冲突，
也未在 Windows/Linux 实跑；本记录不是产品 E2E，不扩展其场景。

审查修正后，在新的 `/private/tmp/ehai-b-review.EUdpD6/home` 下复跑
init → up → add 临时 Git 仓库 → status → down，全部退出 0；工作区为 `sample-repo: ready`。
supervisor 已恢复原有停止行为，JSON 写入改用公开 `write_private_json`。
Ruff check/format、mypy 默认平台及 win32、两条 import 契约全部通过，原产品 E2E 为
1 passed（16.25 秒）。试跑时 lsof 记录管理器 PID 19520、工作区子进程 PID 19536；
down 后两个 PID 均不存在、日志文件无进程持有、manager.lock 与 host.lock 均已释放。
pgrep 因环境缺少 sysmond 无法列举进程，ps 也被沙箱拒绝，因此退出确认采用上述具体 PID、
lsof 和锁检查；未据此宣称已检查系统上所有不相关进程。未保存新的试跑脚本。
