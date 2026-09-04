# EHAI Control Plane Client

当前 `control-plane` 只包含从 [`schemas/v1`](../schemas/v1) 生成的严格 TypeScript API Client，供后续
Control Plane 使用；这里还没有 P3 Dashboard、组件、路由或浏览器状态管理。

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

修改公开契约时先更新 [`schemas/v1`](../schemas/v1)，运行 Python 契约测试，再重新生成并提交 Client。
Control Plane 只能通过公开 Command、Query 和 Event 契约访问 Execution Plane；它不能读取 SQLite、导入
Python 内部模型，也不能复制 Run、Attempt、Branch、Check 或 Gate 的状态规则。交互和展示状态属于
未来 P3 UI，执行状态与合法转换始终属于 Python Execution Plane。
