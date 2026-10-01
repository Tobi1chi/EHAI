# 验收代码边界

唯一产品 E2E 是 [test_product_e2e.py](test_product_e2e.py)：启动真实 `ehai-api` 宿主
（scripted Worker，无模型调用），只经 `ehai` CLI 与 HTTP API 完成导入计划、批准、并发与人工挂起、
强杀重启恢复、人工判定和成果查询。运行：

```powershell
uv run pytest tests/test_product_e2e.py
```

CI 在 Linux 和 Windows 上运行它。覆盖范围与不覆盖的部分（真实 Pi、Git worktree、`integrate-run` 等）
见 [重构与推进计划](../docs/REFACTOR_PLAN.md#产品-e2e)；真实 Pi 验收仍为手动。

主路径需要新覆盖时，扩展这一个场景，不新建测试文件。test_self_hosting.py 是历史部分驱动，
不代表当前 Pi 产品 E2E，也不要求恢复旧 unit/integration/contract/smoke 套件。

正常使用发生具体失败时，仅在仓库外临时目录创建必要诊断，用 uv 执行，修复后重试原路径；
不提交、不复制回仓库、不自动升级为常驻测试。静态 lint/format/mypy、Schema/Client 生成与构建继续执行。
