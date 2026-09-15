# Workspace 文本工具换行一致性修复

本文是 2026-09-08 的故障记录，不是当前完整换行策略。2026-09-15 另发现 Git worktree
物化继承 core.autocrlf 的问题，宿主 Git 已局部关闭隐式转换；见 [当前实施记录](../R2_IMPLEMENTATION_PLAN.md)。

## 已观察的故障

2026-09-08 七节点真实编码试用中，映射路线先通过 `workspace_read` 读取 greetings.py，
随后把相同文本原样作为 `workspace_patch.expected`，却收到 precondition_failed。
该 Attempt 为 `a449d2e2-e025-4d94-95b4-fa56885a3e51`。

原因是读取工具以 UTF-8 字节解码，保留 CRLF；补丁工具却通过文本模式读取，
把文件的 CRLF 转为 LF。因此 Agent 提交的正确 expected 无法匹配。
随后 Agent 使用整文件覆盖，Windows 文本模式写入又把已有 CRLF 转成 CRCRLF，
造成多余空行。这不是 API 代理故障，也不应归咎于 Agent 没有复制正确文本。

## 修复范围与契约

- `workspace_read` 保持现有原样 UTF-8 解码。
- `workspace_patch` 同样按 UTF-8 字节解码读取，不再隐式转换换行；
  替换后直接编码写回，未替换的文本保持原样。
- `workspace_write` 将传入文本直接编码写入，不把 LF 或 CRLF 转成操作系统默认格式。
- CRLF、LF、混合换行、Unicode 和文件末尾是否有换行，均由实际输入决定。
- 精确且唯一匹配规则不变；匹配不到、出现多次、或仅换行不同的 expected 仍被拒绝，
  失败时不改文件。不增加模糊匹配、自动整文件覆盖或静默格式化。
- 路径边界、覆盖许可和输出大小限制不变；`/` 不被放宽为可访问工作区外部的路径。
  不改 unified diff 工具或全局 Git 换行配置。

旧成果中的 CRCRLF 不会被自动清理：本修复防止后续工具调用再次引入问题，
不重写历史试用成果或历史失败证据。

## 验证方式

诊断文件保存在仓库外：

```text
%TEMP%\ehai-newline-fix-898e961a3b044b1a9c5655bb6db86d69\test_newline_roundtrip.py
```

从真实 trace 提取原 workspace_patch / workspace_write 参数，通过正常 ToolExecutor
调用工作区工具并检查实际文件字节，不使用模型替身或直接调用私有 Handler：

1. 重放原 CRLF 读取与补丁，确认 expected 原样匹配、写回没有 CRCRLF，
   并使用原 names.py 上游成果运行未改动的 mapping-greet Gate。
2. 检查创建、重读、覆盖的换行往返，不增长空行或补上原本没有的末尾换行。
3. 检查重复匹配与换行不一致仍被拒绝，文件保持不变。

修复前上述三项均失败；修复后三项全部通过，原 mapping-greet Gate 返回零退出码。
全仓 Ruff 检查、格式检查和 mypy 通过。
临时诊断不提交、不迁移到仓库；不把它扩展成常驻测试套件，
也不把这次工具回放称为重新执行完整七节点产品场景。
