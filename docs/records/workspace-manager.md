# 多工作区执行记录

本页只追加执行与验证叙述，不是当前指南；结论以 [STATUS](../STATUS.md) 与对应设计、用法文档为准。

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

## 文件夹添加与整合分支后端验证（2026-10-22）

macOS；临时目录 `/private/tmp/ehai-app-backend-7k2wqx15`；端口 50905（up）和 50906（手动管理器），
Node v25.8.1、Pi 1.3.0。用户配置指定占位 openai/placeholder-model，凭证变量
`EHAI_FOLDER_MISSING_KEY` 未设置，没有真实模型调用。下列命令在该临时目录执行，
`repo` 指向本次源码目录；没有创建测试脚本或仓库内临时测试文件。

```sh
repo=/Users/tobiichieigetsu/Workspace/AI/EHAI-app
trial=/private/tmp/ehai-app-backend-7k2wqx15
cd "$trial"
export EHAI_HOME="$trial/home"
mkdir -p home 'one/Sample Repo' 'two/Sample Repo' 'three/Sample Repo' not-git
git init -q 'one/Sample Repo'
git init -q 'two/Sample Repo'
git init -q 'three/Sample Repo'
uv run --no-sync --project "$repo" ehai init --provider openai --model placeholder-model \
  --credential-env EHAI_FOLDER_MISSING_KEY --worker-capacity 1 --planner-capacity 1 --port 50905 --yes
uv run --no-sync --project "$repo" ehai up
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:50905/api/v1/local
curl -sS -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"path":"/private/tmp/ehai-app-backend-7k2wqx15/one/Sample Repo"}' \
  http://127.0.0.1:50905/api/v1/workspaces/from-folder
curl -sS -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"path":"/private/tmp/ehai-app-backend-7k2wqx15/not-git"}' \
  http://127.0.0.1:50905/api/v1/workspaces/from-folder
```

| 命令/请求 | 实际结果 |
| --- | --- |
| init；up | 均退出 0；私有配置生成；管理器 ready |
| GET local | 200，data={available:true, home:"…/home", provider:"openai", model:"placeholder-model"} |
| POST one/Sample Repo | 200，workspace_id=sample-repo，status=ready；权限三列表均空；无需初始 commit |
| POST not-git | 422 invalid_workspace，`Workspace path must be a Git worktree root: …/not-git` |
| 重复 POST one/Sample Repo（同上原命令） | 409 workspace_conflict，`Path is already registered as workspace_id=sample-repo` |
| 同上 POST，分别把 path 换成 two/Sample Repo、three/Sample Repo | 各 200、ready，ID 分别 sample-repo-2、sample-repo-3 |
| mkdir -p one/Sample Repo/subdir 后，同上 POST 该子目录 | 422，`Select the Git worktree root …/one/Sample Repo, rather than …/subdir` |
| 同上 POST，path 换成 …/missing | 422，`Workspace path must be an existing directory: …/missing` |
| ehai add one/Sample Repo（使用同一 home） | 退出 0，原 sample-repo: ready，CLI 同配置重加仍幂等 |
| ehai --api-url http://127.0.0.1:50905 get-local-engine | 退出 0，显示同一可用 home/Provider/模型 |
| ehai down | 退出 0，Manager stopped；保留登记 |

手动管理器不传新参数（另一终端；不依赖 EHAI_HOME）：

```sh
uv run --no-sync --project "$repo" ehai-manager --data-dir "$trial/manual-manager" --port 50906
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:50906/api/v1/local
curl -sS -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"path":"/private/tmp/ehai-app-backend-7k2wqx15/one/Sample Repo"}' \
  http://127.0.0.1:50906/api/v1/workspaces/from-folder
```

GET 为 200，`{data:{available:false,home:null,provider:null,model:null}}`；POST 为 409，
`Folder registration requires a manager started with ehai up`。最后 Ctrl+C 停止；日志确认
application shutdown complete、server process finished，进程因 KeyboardInterrupt 退出 130。

补充 MCP 协议检查：再次 up 显式恢复三工作区，使用安装入口 `ehai-mcp --api-url http://127.0.0.1:50905`
连接 stdio ClientSession。工具列表包含 get_local_engine、add_workspace_folder、create_integration_branch；
get_request_schema 能读取两个新写接口的封闭请求 Schema，get_local_engine 返回上述摘要，
add_workspace_folder 重复已登记路径返回 isError=true 和原 409/已有 ID。然后 down 退出 0。
首次检查误用 `-m ehai.interfaces.mcp_server`（该模块没有 `__main__` 启动入口），连接提前关闭；
改用正式安装入口后通过，未改动 MCP 服务实现。未验证模型 Provider 接受或实际模型调用这些工具，
也未通过 MCP 成功新增工作区或创建分支。

核心分支能力由唯一产品 E2E 末尾验证：CLI create-integration-branch 创建 ehai/e2e，
rev-parse 与整合 commit 一致；HTTP 同名重试 created=false；前后用户 HEAD、status --porcelain 一致；
线程时间线只有一条 integration_branch_created，refs 的 run_id/integration_id/branch/commit 匹配。
产品 E2E 使用真实 Pi 与脚本化 Provider，不验证真实模型，也不承载管理器 from-folder。
本轮未验证前端、Windows/Linux 实跑、整合冲突或已有分支指向其他 commit 的运行路径。

最终检查：Schema 生成、TS Client 生成与 npm build 均退出 0；Ruff check 通过，format --check
为 261 files already formatted；mypy 默认平台和 win32 均为 198 source files 无问题；
lint-imports 两条契约 kept；产品 E2E 为 1 passed（19.79 秒）；git diff --check 无输出。
本轮无功能范围偏离：local 摘要沿用管理 API 的 data 封套，CLI add 保留原重复登记幂等语义，
文件夹 API 按要求对重复路径返回 409。未 git commit，未改 uv.lock；前端和 desktop 的并行改动保留。
