# 扩展开发与使用

2026-09-20，协议版本 1。以 Pi coding-agent 的扩展表面和 Chord 服务机制为对照，采用独立的语言无关协议。**可实现对应类别的扩展，不直接兼容 Pi 的 TypeScript 包、终端组件或 npm 安装命令。**

## 三种入口共享一份定义

- 普通用户：工作室「设置 → 扩展能力」导入包，查看贡献与执行权限，安装后在画布、自然语言构建、「对话」中使用。设置、命令参数、选项由 JSON Schema 自动生成表单。
- 低代码：扩展工具与普通 API 使用同一个工具目录和端口 Schema；继续使用画布的数据引用、审批、重试和子流程。
- 代码：Python `HubClient.install_extension`、JS `installExtension`、Rust `install_extension`；也可直接调用 OpenAPI 中 `/v1/extensions/*`。命令提交返回持久 `run_id`，通过普通 run API 读取、批准、取消和追踪。

快速创建一个项目：

```sh
uv run easyagent extension init /tmp/my_extension --id my_extension --language javascript
uv run easyagent extension package /tmp/my_extension --output /tmp/my_extension.eah-extension.json
uv run easyagent extension install /tmp/my_extension.eah-extension.json
```

`--language` 支持 javascript（受控计算）、python、node、rust。后三者需要授予 `trusted_process` 并传入预览所示 `trust_digest`；这是精确源码包的信任，不会因为签名正确而自动获得本机权限。Rust 构建离线并使用 Cargo.lock；先在开发机准备依赖，不在安装时自动下载依赖或执行安装脚本。

## 扩展结构

Manifest、包、请求、响应的机器可读规范见 [契约目录](contracts/extension-manifest.schema.json)，REST 以 `/openapi.json` 为运行实例的权威定义。公共契约拒绝未知字段。`uv run python scripts/export_contracts.py` 从运行时类型重新生成 JSON Schema 与 JS SDK 类型声明，避免多语言接口漂移。

- `id`、`revision`：包身份和不可变整数版本，同一 ID/版本不能替换成不同内容。
- `runtime`、`entrypoint`、`transport`：javascript/wasm/python/node/rust；默认每次调用独立进程。受信任进程可用 `ndjson` 保持资源，所有请求串行响应。超时、取消会结束进程树；下一次调用重启并激活资源。
- `tools`、`services`、`commands`：均使用 ToolSpec 的输入/输出 Schema、读写效果和幂等声明。名字以 `id.` 开头。命令是有界持久工作流，写服务仍走审批。
- `providers`：注册模型别名、实际模型、能力与处理方法；返回规范 ModelResult。语言、判断、图像等数据使用同一模型契约。自定义 provider 必须如实提供 usage；未申报价格不能承诺费用上限。
- `hooks`：事件、优先级、处理方法、失败是否阻断；不能通过改写结果扩大工具列表、预算或审批范围。
- `dependencies`：扩展 ID → 固定版本。安装/激活检查依赖，停用被依赖包会被拒绝。
- `required_services`：声明跨扩展服务；可用 `host.session.*` 操作所选会话。调用已注册普通工具要另声明 `tool:<name>` 权限，版本化工具还要填 `service_versions`。
- `settings_schema/settings`、`flags_schema/flags`：可视化设置与选项；不得放凭证明文，服务端凭证库和 `env_allow` 保留引用。
- `state_schema/initial_state`：每个扩展代际独立的持久状态，CAS revision 防止覆盖并发变更。事件带单调 sequence，可增量读取。
- `migrations`：旧版本号 → 迁移 handler。迁移返回 `{state, settings}`，先验证新 Schema 再激活。旧运行继续使用旧代际，回滚恢复旧代际状态。
- `views`、`skills`、`prompts`：面板/侧栏/编辑器/消息/状态视图，以及 `id.name` 命名的 Skill 和模板。
- `files`、`lock`、`digest`、`publisher/signature`：相对文本源码、逐文件摘要、整体 SHA-256 和可选 Ed25519 身份签名。拒绝越界路径、符号链接缓存和篡改。

## 请求与响应

宿主向 stdin 写一行 JSON：

```json
{"protocol_version":1,"id":"request-id","method":"quote","params":{"price":12.5,"count":4,"currency":"HKD"},"context":{"extension":"guide","revision":2,"settings":{},"flags":{},"state":{"revision":0,"value":{}},"run_id":"run-id"}}
```

处理器向 stdout 输出一行 `{ "result": ... }`，可追加 `state` 提交 CAS 状态变更。stdout 只写协议，日志写 stderr。Python `Extension` 和 JS `Extension` 辅助类支持常驻模式；Rust 提供 `serve_extension`，也可自行实现 NDJSON。纯 JS 定义同步 `handle(request)`，不能访问 Node、网络、文件、环境变量或凭证。

服务调用使用可重放 continuation：

```json
{"calls":[{"name":"calculator.sum","input":{"a":2,"b":3}}],"continue":"after_sum"}
```

宿主执行声明过的服务，再调用 `after_sum`，参数为 `{input, results}`。每层最多 16 个调用、最多 16 层；调用计入运行预算。服务的 Schema、写审批、未知副作用核验不因来自扩展而省略。

`error` 支持 `code/message/retryable`。对外运行错误只暴露受限错误类型/代码；重试仍由步骤的尝试次数和副作用语义决定，不由扩展自行重复写操作。

`lifecycle.activate` / `lifecycle.dispose` 是处理方法。新包激活前会做候选校验；常驻进程首次启动会激活。候选验证和迁移不能提交持久 state 或调用服务。工具产生外部副作用的长连接应实现 dispose 清理；进程退出只是资源回收，不等于撤回外部操作。

## 执行事件

已接入的事件类别：

- workflow.before_step / after_step
- agent.start / end
- turn.start / end（持续会话的一轮 Run）
- context.transform
- model.before_request / after_response / delta / select
- message.start / update / end
- tool.before_call / after_result / error
- session.start / input / info_changed / before_fork / fork / before_compact / compact / shutdown
- resources.discover
- custom.* 事件总线

允许的 patch：工具调用的 arguments、工具 result、上下文 messages、模型请求 prompt/messages、模型响应 text/data、用户输入 text。改写后再次验证 Schema；工具入参只在第一次计算并持久保存，重试和审批恢复不重新改写。其他事件只观察或拒绝。不是每个观察事件都能撤回已完成动作：after_step 失败记录错误，不重跑已完成步骤。

这里没有复制 Pi 的终端渲染器 API、全局 bash shell 或 provider 原始认证头钩子。对应能力通过 Web 视图、受信任代码/工具服务和 provider 贡献实现，凭证仍在服务端。

## UI 与会话服务

HTML 视图使用 sandbox iframe，禁止网络、同源读取和 Hub token。允许向父页面发送 `eah.command`，且只能调用该视图声明的 command；父页面返回持久 run ID。命令结果和审批可在运行记录检查。编辑器视图可以发送 `eah.editor` 设定草稿；不会自动发送。消息视图接收当前会话展示数据。关闭或重载视图会释放事件监听。

先在「对话」选择会话，再运行会话命令；REST 传 `conversation_id`。`host.session.read/send/interrupt/fork/compact/configure` 和 `host.resources` 是规范工具服务，每项需显式权限。compact 提交后台请求，避免单 worker 相互等待。configure 只修改空闲会话；新增工具权限仍需用户重新配置助手。

命令可声明 Alt + 单键快捷键；设置/选项和命令共享 Schema 表单，支持嵌套对象、数组增删、布尔/枚举和包内 $ref，自由结构才使用高级 JSON 输入。Skills 在准备运行时展开成指令快照，提示模板在持续对话中可插入编辑。

## 发布、更新、分享

「设置 → 扩展能力」支持安装、启用指定版本、停用和导出源码包。卸载 API 会拒绝删除被其他包、持久运行历史或已保存流程引用的版本。热更新面向新运行，旧版本 handler/provider 和状态保持可恢复。

完整工作流包会包含执行快照所需扩展及声明的普通工具/API依赖。接收端先配置凭证名称，点击检查扩展时通过 `/v1/workflow-packages/dependencies` 暂存声明式 API，解决服务依赖循环；再明确安装扩展并导入流程。MCP/连接器作为外部依赖重新配置；源码服务固定名称/版本不能静默改绑。安装与流程导入是两步，导入不会静默执行来源不明的本机代码。签名公钥通过发布者信任 API 管理；源码包不复制凭证库、用户状态或运行历史。

## 从任务中写新代码

`code.create` → `code.test` → `code.publish`，使用显式 `code_development: {namespace: ...}` 授权。候选只允许纯 JS/WASM 工具，所有工具必须有输入与预期输出的集成场景；试跑失败不能发布。发布需要写审批，发布后可以被其他流程引用、导出和复用。不同源码必须使用新版本或新候选。

WASM 使用 WAT 源码和 JSON ABI：导出 memory、alloc(length)→pointer、handle(pointer,length)→i64；返回值高 32 位为输出指针、低 32 位为字节长度。禁止所有 imports/WASI，限制内存、fuel、时间和输出长度。当前桌面 Wasmtime 运行器不作为 iOS 动态代码运行器；手机使用签名内置核心，完整动态代码交给已选电脑。

## 限制与部署边界

Python/Node/Rust 本机进程拥有启动账户权限，env 白名单不是系统沙箱。QuickJS/WASM 是受控计算环境，但仍需要及时更新底层引擎。当前单机宿主支持持久状态和故障恢复，不承诺分布式注册表一致性。多个进程可执行数据库队列；扩展安装/升级由一个管理宿主执行，其他宿主重启加载新注册表。
