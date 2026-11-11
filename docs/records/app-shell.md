# 应用外壳执行记录

本页只追加执行与验证叙述，不是当前指南；结论以 [STATUS](../STATUS.md) 与对应设计、用法文档为准。

## 执行记录

### 2026-10-22 第 3 步补齐与真实模型验收

补齐：管理器 `from-folder` 与 `GET /api/v1/local`（Codex），整合结果放到新分支（Codex），
页面添加项目文件夹、分支表单、后台系统通知，桌面外壳的对话框/通知插件与仅限本机守护进程页面的权限。

真实模型验收（用户授权，DeepSeek 官方端点 `deepseek-flash`，凭证存于 macOS 钥匙串，由 `~/.zshrc` 导出，
只经环境变量进入守护进程）：临时 EHAI_HOME 中 `ehai init` → 从登录 shell `ehai up`（doctor 报告终端与守护进程
均有凭证）→ 页面“添加项目文件夹”登记仓库外小项目 textkit（允许 `python3 verify.py`、git.read）→ 新线程。

- 首次失败：讨论在 0.8 秒内失败，页面只显示 “Harness invocation did not yield a confirmed result”。
  仓库外临时诊断直接经本机 Hub 启动同配置 Pi 会话，得到 Hub 原始错误
  `Pi did not accept the authorized thinking level`：`ehai init` 默认推理强度 medium，Pi 对
  `deepseek-flash` 只接受 off/low/high/max，会把 medium/xhigh 换成其他级别，而 EHAI 要求实际级别与授权一致。
- 修复：`ehai init` 的推理强度默认改为 `default`（保存为 null，不向 Pi 指定，交给模型默认）；
  指定级别仍按原规则严格校验。核心在“结果未知”错误里附上 Hub 的原始消息，页面能看到真正原因。
  严格校验本身不放宽。之后按用户决定，默认改为 `high`：Pi 1.3.0 模型表的 1348 个推理模型中只有 19 个没有该档
  （DeepSeek、Anthropic、Google 全有，OpenAI 缺 1 个），非推理模型 gpt-6-luna 以 high 启动会话也被接受；
  `default` 仍可选，表示交给模型默认。
- 重跑（全新 EHAI_HOME 与工作区）：真实 Planner 20 次工具调用（含 add_plan_node、set_final_gate 等嵌套参数）
  给出 stats/slug 并行 → 汇合运行 verify → Reviewer 的四节点方案；页面批准并开始（执行配置显示模型、并发 2、
  允许的命令）；Worker 与 Reviewer 共 40 次工具调用、未见工具错误，Run completed；页面整合（2 个文件 +10 −3）、
  按文件看差异、创建分支 `ehai/run-1387f806`。核对：分支指向整合 commit，在独立 worktree 运行 `python3 verify.py`
  输出 ok；用户仓库仍在 main，工作树干净。从建线程到完成约 2 分 40 秒。
- 未覆盖：系统通知未实际弹出（浏览器面板拒绝通知权限，桌面窗口未实跑），桌面文件夹选择器未实跑，
  人工 Gate 与待办（方案没有人工检查），整合冲突，Windows。

### 2026-11-03 第二轮：doctor 探测与终端交互会话（11-03 计划第 3 步）

- `ehai doctor --probe`：本地命令自己启动一个本机 Hub（不读 `EHAI_HUB_URL`，与工作区宿主一致），用 `config.json`
  的 Pi 设置、模型和思考级别发出 Hub `start`，兼容层照常核对 Pi 选中的 Provider/模型/思考级别，随后在发送任务前
  以 `abort` 关闭会话。`HubClient` 增加 `close()` 以停止它启动的本机 Hub。
- 无参数 `ehai` 进入 `interfaces/interactive.py` 的会话：从 `EHAI_HOME/manager.pid` 找到守护进程，经管理器的
  `/api/v1/threads`、`execution-config` 与 `/workspaces/<id>/api/v1/...` 调用核心；不读库、不推导状态。
- 验证（临时 EHAI_HOME，仓库外小项目 textkit，允许 `python3 verify.py` 与 git.read）：
  - doctor --probe：`deepseek-flash` + high 通过（约 2 秒）；medium 报告 `Pi did not accept the authorized thinking level`；
    不存在的模型 ID 也通过——Pi 启动时不校验模型是否存在，已写入用法。
  - 会话（标准输入脚本驱动，真实 DeepSeek `deepseek-flash`，用户终端已有的凭证经 `ehai up` 进入守护进程）：
    `new` → Planner 约 30 秒给出 Worker + Reviewer 两节点草稿 → `plan` → `approve`（显示执行配置并确认）→ `watch`
    看到节点依次 running/completed/verifying → `todo` 列出人工 Check → 读过产出代码后 `pass` → Run completed →
    `integrate` → `diff` → `branch ehai/terminal-slugify`（重复执行报告已指向同一 commit）。核对：分支上 `python3 verify.py`
    输出 ok，用户仓库仍在 main、工作树干净。首次运行发现并修复：项目列表响应是 `{projects: [...]}` 而不是数组。
- 评审修正：`todo` 记下每个编号对应的待办（类型与 request_id），`pass`/`fail`/`reply` 按编号找回同一项并用其当前
  令牌；该项已不在待办时提示重新 `todo`，不再把编号套到刷新后的列表上。`show` 与讨论续接读取线程时间线的全部分页
  （原先只读前 100 条）。仓库外桩诊断确认：A、B、C 中先 `pass 1` 再 `pass 2` 决定的是 B，重复的编号被拒；
  对真实管理器 `open`/`show`/`todo` 照常。
- 未覆盖：`reply` 与 `fail`、多工作区下的选择、Ctrl-C 中断 `watch`、写请求结果未知时的提示、Windows。
