# 验收代码边界

当前仓库保留 test_self_hosting.py 的历史部分驱动；它不代表当前 Pi 产品 E2E 已完成，
也不要求恢复旧 unit/integration/contract/smoke 套件。具体当前状态见 [STATUS](../docs/STATUS.md)。

只有与用户约定的唯一产品 E2E 才是长期验收代码目标。正常使用发生具体失败时，仅在仓库外
临时目录创建必要诊断，用 uv 执行，修复后重试原路径；不提交、不复制回仓库、不自动升级为常驻测试。
静态 lint/format/mypy、Schema/Client 生成与构建继续执行。零测试收集不算产品通过。
