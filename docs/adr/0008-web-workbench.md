# ADR 0008：Web 工作台的形态与托管

- 日期：2026-10-06
- 状态：已接受，页面已实现；验证范围见 STATUS
- 决策：单页 React 应用只消费生成 Client，由多工作区管理器在 `/ui/` 同源提供。

页面位于 `control-plane/web`，使用 React 19 与 Vite 8，依赖精确锁定在 control-plane 的
package-lock.json，与生成 Client 共用同一套 npm 脚本和 TypeScript 严格配置。
不引入路由、状态管理或组件库：路由是十余条固定路径，数据读取是带缓存的查询与事件触发的重读。

`ehai-manager --ui-dir <dist>` 提供静态文件：`/` 跳转 `/ui/`，未知路径回到 index.html，
`assets/` 长期缓存、其余不缓存；路径解析在目录外时同样回到 index.html。未传参数时不提供页面。
同源托管让页面直接使用管理器及其 `/workspaces/{id}` 代理，不需要 CORS、令牌或另一个服务进程。

页面不读数据库、不复制状态机：状态文字来自核心返回值，事件流只触发重新读取。
写操作沿用核心的幂等键和版本号；结果未知时用同一个键重试，不在页面推断结果。
凭证与 OAuth 只在本机 Connector CLI 完成，页面只显示命令。

为页面补充的只读接口：`listRoutingLabs`（按 Project 列出路由实验）与
`listRoutingReplays`（按实验列出回放），使刷新后仍能看到实验、回放与发布资格。
生活与工作分区暂由浏览器偏好指定生活 Project；核心有分区标记前，这只是显示分组，不是权限边界。
