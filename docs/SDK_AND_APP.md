# SDK、命令行与 Agent App

本轮将本地开发、HTTP 服务和前端应用分开交付，沿用同一个持久化运行器与 Workflow 契约。

## 交付范围

1. `easyagent`：Python 本地 SDK、运行器和 CLI。普通函数通过 `@node` 或 `@tool` 注册；`Module.forward` 与 `Sequential` 编译为现有工作流。同步脚本与异步服务均可使用，不需要先启动 Web 服务。
2. `easyagent[server]`：HTTP 服务适配器；`easyagent serve` 只启动 API。浏览器、MCP、JavaScript/WASM 扩展等依赖按 extra 安装。
3. `apps/agent` / `easyagent-app`：独立前端资源与同源代理，不导入运行器。`easyagent-app --backend URL` 连接服务；桌面发行版显式组合两者。
4. `easyagent-client`、JavaScript 和 Rust SDK：保留独立 HTTP 客户端，增加直接执行已保存工作流的便利入口。没有 Python 本地运行器的环境继续通过 HTTP 使用完整能力。

## 语法与执行约定

- 模块可调用、可组合、可导出；复杂 DAG 用 `Module.forward` 表达。类型标注生成工具 Schema。
- 编译只构图，不调用模型或工具；运行继续经过校验、审批、预算、日志、持久化与恢复。
- Python 条件判断不能隐式依赖构图期占位值；动态分支继续使用底层 Workflow 的 `when`、`foreach`、`goal`。
- 函数工具是受信任的本地代码，导出的 JSON 不包含 Python 源码。跨机器共享代码使用已有扩展包。
- CLI 默认本地执行，`--url` 指向远程；stdout 输出 JSON，诊断写 stderr。暂停、失败、超时返回不同退出码，能够查看、审批和恢复同一任务。
- API 凭证仍由后端保存；独立 App 代理只转发浏览器提供的运行器令牌，不向浏览器注入服务密钥，不开放任意目标代理或宽泛 CORS。

## 验收

- 无 FastAPI、前端或浏览器依赖的隔离安装：函数工具、Agent、组合、导出、CLI 和恢复。
- 模型与工具通过本地 HTTP fixture 验证数据流，保留原审批机制。
- 三语言媒体 demo 与 CLI 走同一个命名工作流接口。
- 独立 App 连接受认证服务：页面、API、上传、事件流、跨源拦截。
- 现有集成测试、契约导出、三语言构建、独立 wheel 与桌面 smoke。

进度和最终测试结果见 [NEXT_DELIVERY.md](NEXT_DELIVERY.md)。

## 节点与子工作流

- `Node` / `@node`：任意普通 Python 操作的封装，整个函数只生成一个 Step；同步和异步均支持。`@tool` 保持兼容，并可供 Agent 选择调用。
- `Sequential` / `Module`：组织同一工作流中的节点，不隐式增加子运行。
- `Subflow(module)`：明确保留内部图，返回模块结果；`Subflow(id, revision=...)` 调用保存版本并返回命名输出。
- `NodeDefinition`：库中的单步模板，实例化为普通 Step；工作流来源始终实例化为子工作流。界面分类型展示，包导入校验来源与依赖。
- 本地 Runtime 只执行选定运行树，不启动工作区投递、计划和维护循环。显式传入 Hub 时保留调用者的执行范围。

入门文档与高级参考分开：[第一次写程序](GETTING_STARTED.zh-CN.md) → [SDK 用法](SDK_GUIDE.zh-CN.md) → [开发参考](DEVELOPER_GUIDE.zh-CN.md)。
