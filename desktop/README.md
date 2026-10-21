# EHAI 桌面外壳

Tauri 2 窗口，显示本机 EHAI 守护进程提供的工作台（`/ui/`）。设计见
[应用外壳](../docs/APP_SHELL_DESIGN.md#模块-d桌面外壳前端)，验证程度见 [STATUS](../docs/STATUS.md)。

窗口先打开随包的启动页：探测守护进程是否应答；没有在运行时提供“启动 EHAI”，
通过用户的登录 shell 执行启动命令，使守护进程继承与终端相同的 PATH 和模型凭证变量；
应答后跳转到工作台。外壳不保存凭证，也不实现业务逻辑；跳转后的页面没有调用外壳命令的权限。

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `EHAI_MANAGER_URL` | `http://127.0.0.1:8765` | 守护进程地址 |
| `EHAI_UP_COMMAND` | `ehai up` | 启动守护进程的命令，在登录 shell 中执行 |

## 开发

需要 Rust 工具链（`Cargo.toml` 声明 `rust-version = 1.89`，依赖解析会选择兼容的 Tauri 2 版本）
和 Node.js。工作台页面需先在 `control-plane` 构建，由守护进程提供。

```bash
npm ci --prefix desktop
npm --prefix desktop run dev
```

打包当前平台的安装包（macOS 为 `.app`/`.dmg`，Windows 为 NSIS 安装程序）：

```bash
npm --prefix desktop run build
```

图标为占位图，由 `npx tauri icon <1024px png>` 生成到 `src-tauri/icons/`。
