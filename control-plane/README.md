# EHAI Control Plane Client

当前 `control-plane` 只包含从 [`schemas/v1`](../schemas/v1) 生成的严格 TypeScript API Client，供后续
Control Plane 使用；这里还没有 P3 Dashboard、组件、路由或浏览器状态管理。

平台定位与交互目标见 [Product Scope](../docs/PRODUCT_SCOPE.md)。顶层通用 Agent 是平台交互角色，
不等同于 TypeScript UI，也不等同于 Planner；二者和 CLI、未来 Routines 一样使用公开核心能力。
计划审查、人工阻塞回复和最终代码验收在 P3 映射为界面，不能由 UI 单独实现另一套执行状态机。
R1 Client 已生成 `discussPlan`、`getPlanningConversation` 和可选 `PlanGraph.design_document`，
可由后续界面使用；R1 已有真实 CLI 试用记录，但不表示 P3 UI 或完整编码流程已完成验收。

## 目录

| 文件 | 职责 |
| --- | --- |
| [`src/generated/ehai-client.ts`](src/generated/ehai-client.ts) | 已提交的生成类型、`EhaiApiClient`、错误类型和 SSE URL helper；不要手工编辑。 |
| [`src/index.ts`](src/index.ts) | 包的稳定导出入口。 |
| [`scripts/generate-client.mjs`](scripts/generate-client.mjs) | 读取 JSON Schema/OpenAPI、校验 operation ID 并确定性生成 Client。 |
| [`package.json`](package.json) | `generate`、`typecheck` 和 `build` 脚本及固定 TypeScript 依赖。 |
| [`package-lock.json`](package-lock.json) | npm 依赖锁文件。 |
| [`tsconfig.json`](tsconfig.json) | NodeNext/ES2022 严格配置，启用精确可选属性和索引访问检查。 |

## 使用与更新

```powershell
npm.cmd ci
npm.cmd run generate
git diff --exit-code -- src/generated/ehai-client.ts
npm.cmd run typecheck
npm.cmd run build
```

修改公开契约时先更新 [`schemas/v1`](../schemas/v1)，核对接口并重新生成 Client，运行类型检查和构建。
旧 Python 契约测试已退役，新的定位测试只在实际失败后放到仓库外；见 [测试策略](../tests/README.md)。
Control Plane 只能通过公开 Command、Query 和 Event 契约访问 Execution Plane；它不能读取 SQLite、导入
Python 内部模型，也不能复制 Run、Attempt、Branch、Check 或 Gate 的状态规则。交互和展示状态属于
未来 P3 UI，执行状态与合法转换始终属于 Python Execution Plane。
