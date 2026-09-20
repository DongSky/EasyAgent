# 本轮新增的规范接口

完整扩展协议见 [EXTENSIONS.md](EXTENSIONS.md)，持续会话与连接见 [CONVERSATIONS_AND_CONNECTIONS.md](CONVERSATIONS_AND_CONNECTIONS.md)。[机器可读契约](contracts/extension-manifest.schema.json)由运行时 Pydantic 类型生成，JavaScript SDK 类型声明使用同一生成器。Python 可直接使用这些模型；Rust 使用 serde JSON/HTTP 与共享 Schema，移动核心通过 C ABI/JNI 的 JSON dispatch 对接。REST `/openapi.json` 包含可用端点与输入结构。

# 接口契约 v1

公共 API 使用 `/v1`，请求/响应 JSON，错误统一包含 `detail`。服务提供 `/docs` 和 `/openapi.json`。JSON Schema 来自 Pydantic，插件 schema 由 jsonschema 校验。

## 完整工作流分享

`easyagent.workflow-package.v1` 携带冻结 Workflow、固定版本 API、明确授权的额外工作流、能力/权限依赖和摘要。`POST /v1/workflow-packages/export` 接受 `{workflow}`；`POST /v1/workflow-packages/preview` 与 `/import` 接受 `{package, model_bindings, tool_bindings, credential_bindings, knowledge_bindings, memory_bindings, allow_development, workflow_id?}`。导入使用本机别名、原子写入独立流程，返回 `execution_started=false`。

`GET /v1/studio/workflows/{id}/package?revision=N` 下载已保存版本；`GET /v1/workflow-packages/exports/{digest}` 下载当前画布导出的本地副本。全部端点沿用服务认证。完整范围、限制和三语言 SDK 见 [分享契约](WORKFLOW_SHARING.md)。

## Workflow

```json
{"name":"first-run","steps":[
  {"id":"first","kind":"tool","target":"core.echo","input":{"text":"hello"}},
  {"id":"second","kind":"tool","target":"core.echo","depends_on":["first"],"input":{"previous":{"$ref":"first"}}}
]}
```

Step 的 `max_attempts`、`timeout_seconds`、`not_before`、`requires_approval` 控制执行。提交时 `Idempotency-Key` 防止重复创建；同 key 不同请求报冲突。事件 id 为数据库递增序号，读取时带 `after` 支持断线续读。

## 按名称调用保存的流程

- `PUT /v1/workflows/{id}?expected_revision=N`：保存定义；新建使用 `N=0`，更新时指定已有版本，冲突返回 409。
- `GET /v1/workflows/{id}?revision=N`：读取完整定义和版本；省略版本时读取当前版本。
- `POST /v1/workflows/{id}/runs`：接收 [WorkflowCall](contracts/workflow-call.schema.json) `{inputs, revision?}`，输入与流程默认值合并后按 `metadata.input_schema` 校验。使用 `Idempotency-Key` 重试同一操作；定义在第一次提交时冻结，之后修改已保存流程不影响原任务。同 Key 配不同输入或版本返回 409。
- `GET /v1/runs/{id}/result`：返回 `id/status/outputs/artifacts/approvals/input_requests/errors`；产物包括子流程产物。成功时解析 `metadata.outputs` 中的命名引用，可用 `metadata.output_schema` 校验。子流程结果保留 `results/runs` 并增加命名 `outputs` 数组。

三个 SDK 的 `workflow(id).start(inputs)` 返回任务句柄，`result()` 成功时返回命名输出，遇审批、缺少输入、待核验、失败或取消时抛出带任务 ID 和状态的 `RunStopped`。超时只结束客户端等待，不取消服务端任务。完整调用示例见[生图与视频](../examples/getting_started/media/README.md)。

## Tool 与插件

Tool 定义 `name`、`description`、`input_schema`、`output_schema`、`effect`（read/write）、`idempotent`。部署端声明的 write 工具必须经过审批；用户请求不能降低工具风险。handler 接收参数与 InvocationContext，其中包含稳定 invocation_id。

插件 manifest 使用 `api_version: "1"`、`name`、`command`、`tools` 和可选 `env_allow`。每次调用启动独立进程，stdin 写入一条 JSON，stdout 返回一条 JSON：

```json
{"protocol_version":"1","id":"invocation-id","method":"tools/call","params":{"name":"math.add","arguments":{"a":2,"b":3}},"context":{"invocation_id":"invocation-id"}}
```

响应为 `{"id":"invocation-id","result":{"value":5}}`，失败为 `{"id":"invocation-id","error":{"message":"reason"}}`。日志写 stderr。响应关联 id、大小、超时和 schema 均验证。启动命令不经过 shell；manifest 属于受信任的部署配置。

## 模型

统一请求包含 model alias、capability、messages、tools、response_schema、prompt、parameters。统一结果包含 text、data、tool_calls、images、embeddings、usage。模型名由配置提供，不能把某个供应商的营销名称硬编码为架构要求。decision 是结构化输出能力，不假定某个独立“决策模型”协议。

## MCP / Skills

MCP 由官方 SDK 处理版本协商和传输。连接配置只来自部署者。导入的 MCP tool 必须显式定义本地风险与是否幂等，不能信任远端提示信息来提升权限。stdio/Streamable HTTP client 和 stdio server 均纳入验收。

Skills 使用目录中的 SKILL.md（name/description YAML frontmatter）。启动只读取元数据；选中时加载正文并作为指令补充。脚本不会自动执行；allowed-tools 是建议，实际权限仍以运行 allowlist 为上限。路径必须留在 skill 根目录，拒绝越界引用和 symlink。

## 兼容性

0.x 阶段不承诺跨次版本 ABI。公共变更必须更新此文档、SDK、OpenAPI 与集成场景；数据库带 schema version，未来版本数据库须明确拒绝。未实现 A2A 不对外宣称 A2A 兼容。

## 工作流扩展与运行控制

Step kind 为 tool/model/agent/transform/retrieve/artifact/foreach/subworkflow/approval/input。Workflow 包含 `inputs`、`limits`、`metadata`；画布位置和视口保存在 `metadata.editor`，不影响执行。引用格式 `{"$ref":"step.field"}` 或 `{"$ref":"$input.field"}`；条件 `when={"source":"step.field","equals":value}`；子流程定义为 `body`；失败补偿为 `compensate={"target":"tool.name","input":{...}}`。

`POST /v1/inputs/{id}` 提交缺少的内容，按该请求的 JSON Schema 校验；日期时间要求有效时区。`POST /v1/approvals/{id}` 提交 `approved`；`POST /v1/reconciliations/{id}` 提交 output 与已核验 receipt，结果和回执原子保存。状态 waiting_input、waiting_approval、needs_attention 由三个 SDK 的 wait 返回供调用者处理。

`POST /v1/studio/workflows/validate` 使用当前工具/模型/技能注册表进行无执行检查。模型/Agent 的 `input.response_schema` 和人工输入节点的 `input.schema` 中 `$ref` 属于 JSON Schema，不作为工作流数据引用解析。

## 零代码自动构建

- `POST/PUT /v1/studio/assistants[/{id}]`：`construction="automatic"`、name、purpose、`model="auto"` 或已连接别名；自动模式不接受手动工具/Skill/资料库/策略权限。
- `POST /v1/studio/assistants/{id}/build`：提交持久化模型编译任务，返回 run id。编译阶段不执行业务工具。
- `GET /v1/studio/assistants/{id}/workflow`：构建中返回运行状态，完成后返回 ready + workflow + explanation；缺少能力返回 clarification + questions，非法计划返回 invalid，需求已改返回 stale。
- `POST /v1/studio/assistants/{id}/run`：仅把 message 填入已保存流程的 inputs；未构建或过期返回 409。
- `GET /v1/studio/assistants/{id}/export`：导出预览的同一流程、输入、依赖清单、说明及 Python/JS/Rust 客户端；未构建或过期返回 409。
- `GET /v1/studio/assistants/{id}/workflow.json`：以附件形式下载同一工作流 JSON，沿用过期检查和服务认证。

编译器使用构建时注册表快照校验工具、模型、技能、资料库和数据依赖。禁止将 API 隐藏在 Agent.tools 内；写工具沿用运行审批。旧版 `construction="manual"` API 保留，页面提示重新生成；旧独立 `/v1/studio/generations` 草稿接口默认不注册。

`POST /v1/studio/connections/test-and-save` 接受 Connection，在连接测试成功后注册别名。失败返回可理解的 502/504，不回传供应商正文或密钥，也不占用名称。既有连接注册及按别名测试接口保留。

## HTTP API / 搜索注册

- `POST /v1/studio/apis`：HTTPTool 定义 → ToolSpec + 不含凭证的重启配置。
- `GET /v1/studio/search/tinyfish`：可选 name，返回搜索连接状态、凭证来源和是否持久保存，不返回密钥。
- `POST /v1/studio/search/tinyfish`：name、api_key 或 api_key_env、可选 endpoint/timeout_seconds → 创建/更新专用搜索工具；用户密钥加密保存，重启恢复。
- `POST /v1/studio/search/tinyfish/test`：可选 name、query → 创建一次真实搜索测试，返回运行 ID；每次测试最多尝试一次。
- `POST /v1/studio/apis/openapi/preview`：document、prefix、可选 server_url/凭证 → 支持和不支持的 operationId。
- `POST /v1/studio/apis/openapi`：同上并包含选定 operations → 原子注册工具并返回脱敏配置。

配置文件的 `search` 与 `http_tools` 在启动阶段载入。注册 API 不保存明文密钥到数据库、不把密钥传给模型；运行时仍经过 ToolRegistry 的 Schema、预算、审批、幂等和事件机制。`POST /v1/studio/apis` 创建的 HTTP 定义现已按版本持久化，PUT 支持 expected_revision 更新，GET 支持历史版本。Agent 通过单独的 development 授权调用相同底层能力，见 [运行时开发](RUNTIME_DEVELOPMENT.md)。TinyFish/OpenAPI 快捷导入及配置加载的原有生命周期见 [SEARCH_AND_APIS.md](SEARCH_AND_APIS.md)。

## 系统通知连接

`POST /v1/connections` 的 Connector 新增 `kind="system"`，要求 `device_id` 为接收浏览器持久保存的 32 位小写十六进制标识。系统通知不填写 `url/credential/recipient`，不能设置 `idempotent=true`；其他类型仍要求有效 HTTP 地址。工具 `connection.<id>` 接受 `text`（1–4000 字符）及可选 `title`（1–160 字符）。创建/更新沿用不可变版本、审批和持久队列。

- `POST /v1/connections/{id}/test-system`：`{device_id}`，校验与保存的系统连接一致，返回 `{delivery_id,status:"queued",delivered:false}`。
- `POST /v1/connections/system/claim`：`{device_id}`，原子领取一个待发通知，返回 `{notification:null}` 或 `{notification:{id,claim_token,title,text,run_id}}`；多标签页只能有一个领取成功。
- `POST /v1/connections/system/{delivery_id}/receipt`：`{device_id,claim_token,outcome:"submitted"|"failed",reason?}`。失败原因可为 `permission_denied/unsupported/notification_error`。重复同一回执幂等；其他浏览器、错误 claim 或覆盖已确认结果返回 409。
- `GET /v1/connections/deliveries`：每条增加 `kind`，系统成功提交为 `submitted`；回执明确 `display_confirmed=false,read_confirmed=false`。超时未回执为 `uncertain`，不自动重发。测试记录 `run_id` 为空，工作流记录保留实际运行 ID。

全部端点沿用 Hub 的认证与同源边界；设备标识用于路由而非多租户认证。`Connector/NotificationDevice/NotificationReceipt` 从同一 Pydantic 契约生成公开 Schema 和 JavaScript 类型。
