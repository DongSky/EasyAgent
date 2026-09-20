# 节点与子工作流的导出、导入与分享

更新：2026-09-20。支持 `easyagent.component.v1` JSON 文件包，扩展名 `.eah-component.json`。分享无需专用云平台：可以把文件放进 Git、团队文件库，或交给另一个项目导入。本次没有公开上传或建立在线市场。

## 为什么需要组件包

单独导出画布 JSON，只记录流程本身，换一个 Hub 会缺少它引用的 API 和子流程。组件包包含根组件、嵌套组件、实际 API、NodeDefinition 或 Workflow 定义、固定版本及依赖摘要，接收端能够检查并安装。适合团队协作、迁移项目、复用 Agent 新建的能力和长期保留可运行版本。

当前可以完整打包声明式 API、单步节点模板和确定性子工作流。HTTP 节点本身就是 URL、方法、参数、Schema、鉴权引用与轮询等声明；通用执行代码由 EasyAgent 提供。

Python/JS/Rust 进程插件源码、依赖锁、平台二进制，以及模型/Agent 绑定尚未纳入此包。外部 MCP/插件工具只能作为宿主能力要求，不能凭一个配置文件自动部署。未来源码包需要锁定依赖、构建产物、兼容平台、集成场景和验证记录，仍列在研发计划中。

包含模型/Agent 和补偿节点的流程现可使用单独的[完整工作流分享包](WORKFLOW_SHARING.md)，该包会冻结指令并提供接收端模型/工具映射。

## 文件与安装语义

- 根组件 ID/版本、工作流契约版本、最多 500 个定义；每个定义与总包均有 SHA-256。
- 摘要基于排序 JSON，数字中的整数浮点数归一化，例如 `90.0` 与 `90` 相同，兼容 JS 的解析/序列化。跨语言业务整数应保持在 JSON 安全整数范围；超范围值建议以字符串传递。
- API 的 `api_key_env` 只包含凭证变量名称；不会复制运行时配置的密钥。禁止 inline api_key、常见敏感静态请求头与 URL 查询字段。
- 用户在默认参数、描述或 URL 路径中手写的任意隐私内容不会被自动识别；分享前检查自己填写的数据。运行历史和产物文件不在组件包内。
- 导入核验摘要、依赖完整性、实际读写效果与权限声明。写入为数据库事务；失败不留下半包。
- 同 ID/版本/内容重复导入幂等；同版本不同内容拒绝。已有 API 版本的凭证映射不能由导入悄悄替换。
- 导入不会运行组件；执行时仍走当前 Hub 的权限、审批与预算。缺少凭证可先导入，运行前需要配置。
- 包目前未签名。SHA-256 检查内容一致性，不认证发布者。导入的“已验证”信息属于发布者声明，界面不把它显示为本机已验证。

凭证映射保存在接收方 Hub，按 **API ID + 版本 + 凭证引用** 限定作用域；不修改包内不可变定义，也不覆盖其他节点的凭证。

## API / SDK

- `GET /v1/library/{id}/package?revision=N`：下载完整包。
- `POST /v1/library/packages/preview`：核验内容与冲突，返回组件列表、依赖数量及所需凭证。
- `POST /v1/library/packages/import`：原子导入。

后两个接口都接受 `{package, credential_bindings}`，例如 `credential_bindings={"TYPESAFE_API_KEY":"MY_DECISION_KEY"}`。右侧是服务端环境变量名，不是密钥值。

```python
package = await source.export_component("library.decision.classify_receipt", revision=1)
await destination.import_component(package, {"TYPESAFE_API_KEY": "MY_DECISION_KEY"}, preview=True)
await destination.import_component(package, {"TYPESAFE_API_KEY": "MY_DECISION_KEY"})
```

JS 对应 `exportComponent` / `importComponent`，Rust 对应 `export_component` / `import_component`。所有接口也可直接从三种语言使用 REST 调用。

## 验证与演示

`tests/integration/test_component_packages.py` 使用真实 HTTP 与两个独立 SQLite Hub，验证导出、JS 客户端导入、凭证重绑、执行、重启、篡改拒绝、依赖缺失、权限不一致及原子性。媒体与模型协议另有独立集成场景。

可复现真实分类服务演示（会调用服务并可能计费）：

```sh
uv run --extra app python -m examples.demos.component_library --live --base-url http://127.0.0.1:8766
```

服务端先安装通用分类节点，演示进程设置 `TYPESAFE_API_KEY`；模型目录同步还需要服务端的 `OPENAI_BASE_URL` 与 `OPENAI_API_KEY`。脚本保存两种业务分类、组件包、空库导入与重启复用的证据，不输出密钥。

可直接导入的真实样例：[`classification.eah-component.json`](../examples/components/classification.eah-component.json)，需要接收端设置 `TYPESAFE_API_KEY` 或绑定到自己的变量名。

旧开发预览版使用未归一化的数字摘要；启动时为这些组件追加新版元数据，不覆盖执行定义或历史版本。跨语言分享请导出最新组件版本，旧版本依然可供已保存的本地工作流使用。
