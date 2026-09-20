# Agent 在运行中开发 API 节点与工作流

公开文档中的 `local-record-*` 是脱敏占位符，真实运行、流程和产物编号仅保留在本机验收记录。

框架现在提供真正的 Agent 开发工具。模型通过正常工具调用读取文档、构造 HTTP API 定义、保存新节点与工作流、更新相同 ID 的版本、执行指定版本的子流程，并取得执行回执。调用和生成结果都进入原有运行记录；无需外部管理脚本替 Agent 注册节点。

## 执行与版本规则

- `development.catalog`：查看当前开发授权、可读文档、已保存版本。
- `development.read_document`：获取已授权的公开文档 URL；禁止跳转，限制响应体，返回来源与内容摘要哈希。文档属于不可信参考材料。
- `development.save_api` / `get_api`：创建、更新和查询 API 定义。
- `development.save_workflow` / `get_workflow`：校验完整工作流，保存和读取版本。
- `development.run_workflow`：提交指定版本的子运行，让出工作线程，等待其结束后把输出返回原 Agent。
- `development.call_api`：直接执行刚创建的 API 的指定版本。

创建时 `expected_revision=0`；更新时提供读取到的当前版本。版本冲突返回可供 Agent 修正的错误，不覆盖其他写入。工具调用幂等 ID 同时用于定义保存，处理“数据库已提交，但回执尚未记录就中断”的情况。

API 定义和工作流的历史版本保存在 SQLite。提交运行时，把 API 的 `tool_revision` 固定在步骤、Agent 工具列表和补偿操作内；嵌套子流程也递归固定。更新注册表不会改变已经提交的任务。重启后按版本重新装载 HTTP handler，密钥在调用时从服务器环境读取。

运行中可以创建和执行新子流程；不会改写当前任务已经执行或等待中的图。需要修改未完成计划时，Agent 保存新版本并执行该版本。父子关系、调用预算、取消、等待输入、写操作审批、租约和断点恢复使用原有执行器；一个 worker 也能运行。

## 配置开发权限

在 Agent 的 `tools` 中选择需要的 `development.*` 工具，在 `input.development` 提供拥有者授权。例如：

```json
{
  "namespace": "life_project",
  "documents": ["https://docs.typesafe.ai/api"],
  "services": {
    "typesafe": {
      "url": "https://api.typesafe.ai/v1/systemone",
      "methods": ["POST"],
      "effect": "read",
      "api_key_env": "TYPESAFE_API_KEY"
    }
  },
  "tools": ["core.to_text"],
  "models": ["live-gpt"]
}
```

`services` 是可开发的确切端点/路径模板，不是任意地址访问权；`documents` 是可读取的确切公开 URL。服务的鉴权位置、方法、读写声明和幂等属性由服务拥有者配置。Agent 只能选择服务和定义参数/响应映射，不能选择环境变量、注入鉴权头、更换目标地址或把写操作降级成读取。新 API 和工作流 ID 必须以 `namespace.` 开头。

`tools` / `models` 控制生成的子流程可复用哪些现有能力；新 API 自动限于本开发命名空间和兼容的服务授权。子 Agent 不能自行追加开发权、技能、知识库或策略。开发授权不自动提供给普通零代码助手，也不把内部管理工具放进普通助手的自动选型目录。

`effect=local` 表示受明确开发授权控制的本地定义保存/执行管理。外部业务写入仍由底层工具独立审批。`run_workflow` 遇到子流程失败会返回失败回执，让模型能够修改方案；模型最后回答并不自动构成验收通过，仍应检查子运行、产物及任务断言。

## HTTP 与 Studio

- `POST /v1/studio/apis` 创建并持久化 API。
- `PUT /v1/studio/apis/{name}?expected_revision=N` 更新。
- `GET /v1/studio/apis/{name}?revision=N` 读取指定版本，省略版本读取最新。
- `GET /v1/studio/apis/{name}/revisions` 获取版本记录。
- 工作流使用相同模式的 `/v1/studio/workflows/{id}` 接口；POST 创建时由服务器生成 ID。

Studio 已保存列表显示版本，继续编辑后保存会更新同一 ID；冲突时保留编辑内容并提示错误。运行期间生成的流程也进入该列表，可打开连线图、检查参数及重新执行。运行记录展示父子流程与产物。

API 定义不保存明文密钥。通过管理接口临时输入的原始密钥只存在当前进程中，返回的配置含环境变量占位；重启时需要提供相应环境变量。旧版、没有版本号的工作流仍可读取，首次更新建立版本 1。

## 验证

`tests/integration/test_runtime_authoring_boundary.py` 使用实际 HTTP 服务与可控模型协议夹具，验证模型工具调用、保存后故障重试、版本更新、旧版执行、单 worker 子流程、重启输入恢复、写操作确认、预算与取消、拒绝越权、数据库不含凭证。无单元测试。

真实验收入口：

```sh
uv run --extra app python -m examples.demos.runtime_development --url http://127.0.0.1:8766
```

服务端需已通过环境变量配置 GPT、TinyFish、TypeSafe；模型别名为 `live-gpt`，搜索工具为 `search.tinyfish`。测试脚本只提交一个父 Agent 工作流并读取证据。创建 API、保存/更新工作流、执行子流程全部由 GPT 的工具调用完成。具体目标是搬家分类（两节点 v1）→旅行分类（三节点 v2）→复核旧版本；不使用复杂报告质量审核代替本能力的验收。

当前边界：本模块支持配置式 HTTP 节点和工作流的运行时开发。另一个代码开发模块已支持 JS/WASM 纯计算候选的创建、测试和发布，见[扩展验收](EXTENSION_ACCEPTANCE.md)；受信任 Python/Node/Rust 扩展属于独立授权路径。这些能力不允许任意修改框架进程。生产多租户隔离、真实长稳运行和通用任务成功率仍须另外验证。

真实执行结果：2026-09-19 运行 `local-record-08` 通过，102.95 秒。API 与工作流各保存两个版本；两个子运行分别返回 moving、travel，旧 API 版本仍可调用。证据及最初协议兼容失败记录见 [真实验收记录](LIVE_WORKFLOW_ACCEPTANCE.md#修复后的真实-agent-开发验收)。


跨工作流复用已补充：使用 workflow_ref 引用其他已保存流程的固定版本，Agent 的 development.workflows 可授予其他命名空间组件；内部工具与模型权限仍递归检查。代码生成运行环境与跨平台发行按独立阶段推进，见 [复用与代码执行](REUSE_AND_CODE_EXECUTION.md)。

## 零代码在线纠错与长程扩展

2026-09-20 已实现持久目标控制器，详见 [目标执行与自动修复](GOALS.md)。零代码入口勾选“检查结果并自动改进”后，执行结果必须经过业务检查；未通过时生成完整的新工作流、保存不可变版本、执行后续子任务。支持有界动态代码生成、用户反馈、断点续跑及跨版本写入保护。

目标控制器不会修改原目标与验收条件。子流程、修订和验收共用根预算；停滞、预算不足或缺少信息会显式停止或等待。确定性字段断言与模型语义判断分别记录，不能把模型判断当作外部成功证明。

LoopX 参考固定在 [916763e2](https://github.com/loopx-project/loopx/tree/916763e2c7fc9c19274f877dd234f7db6a65390c)，借鉴目标、证据、配额和续跑控制，不依赖其运行时。没有进行同任务长期质量对比。
