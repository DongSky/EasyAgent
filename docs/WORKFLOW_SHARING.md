# 完整工作流分享

更新：2026-09-20。格式 `easyagent.workflow-package.v1`，文件扩展名 `.eah-workflow.json`。可以交给其他用户、存入 Git 或文件库，再导入另一个 Hub。本次提供文件分享，没有自动公开上传、在线分享链接或市场。

## 在工作室中使用

1. 在低代码画布点击「分享完整工作流」，或在已保存流程旁点击「分享完整流程」。零代码生成结果也有完整工作流分享入口。
2. 接收端点击「导入分享包」，选择文件。普通「导入配置」也识别此格式。
3. 预览步骤、服务地址、写操作和缺少的依赖。选择本机模型、已安装工具和本机资料库；API 凭证映射填写接收端服务进程的环境变量名，不填写密钥值。依赖缺失时先完成连接，再检查。
4. 点击「导入为独立工作流」。弹窗关闭，流程进入可编辑画布并保存；用户另行点击运行。

「导出 JSON」只导出流程定义；「导出客户端项目」提供该流程和三语言调用入口；「分享完整工作流」额外冻结依赖并携带可移植 API 定义。组件包则用于分享节点库里的单个 API 或确定性子流程。

## 随包内容

- 完整 Workflow：模型/Agent、条件、循环、嵌套流程、人工输入、审批、参数、数据引用、画布布局、预算、重试与补偿。
- 固定版本的 HTTP API：URL、方法、Schema、读写效果、鉴权引用、媒体请求/响应模式。补偿节点也固定 API 版本。
- 保存工作流的引用展开为当时的实际内容；原引用记录在元数据中。接收端不依赖源 Hub 的工作流 ID。
- 所选 Skill 正文和已生效策略冻结进 Agent 指令。不会在接收端安装 Skill 文件、复制关联脚本或激活策略。
- 模型所需能力、源模型名称/协议提示；外部工具的输入输出 Schema 与效果；知识库和记忆分组名称。
- Agent 的运行时开发授权及明确授权的额外工作流。导入含开发权限的包须明确勾选接受；这些额外工作流也生成独立本机副本。

模型连接、API 密钥值、知识/记忆内容、历史运行、历史产物、插件源码和二进制不包含在包内。MCP/进程插件仍需接收端安装；匹配 Schema 仅保证调用契约一致，不保证实现完全相同。Skill 引用的额外文件或输入中的本地产物 ID 也不会自动迁移，应由接收端准备或替换。

包保留当前输入、提示词、默认值和布局元数据。系统阻止 inline API key、常见敏感静态请求头和 URL 查询字段，但不能识别任意文本中的所有隐私信息；分享的是用户实际编辑的内容。

## 导入语义与边界

- 校验总包与各定义的 SHA-256，重新计算实际依赖和权限，与声明逐项比较。多余依赖、遗漏依赖和不匹配效果均拒绝。摘要不认证发布者，当前包未签名。
- 根据本机能力核对模型、外部工具、知识库、凭证；不兼容或缺失时阻止导入。记忆可以绑定到新的空分组。
- API ID/版本保留。同 ID/版本异内容拒绝；已有版本的凭证绑定不能被另一个包覆盖。数据库事务避免半包写入。
- 根流程默认生成 `shared.<fingerprint>`，授权工作流生成其下的独立 ID。相同包和绑定重复导入幂等；SDK 可指定新的 `workflow_id` 建立另一副本。
- 模型/工具/知识/记忆别名写入接收方流程。模型连接仍由接收端管理，API 凭证映射按 API ID + 版本持久化。
- 导入不会执行工作流、外部 API 或可执行插件；实际运行仍执行当前 Hub 的审批和预算规则。
- 导出限制序列化体积 1.5 MB，HTTP 通用请求上限 2 MB。大型文件应作为独立数据准备，不内嵌到分享包。
- 当前包承载可执行流程；调度器触发记录、webhook 地址、账户权限、运行日志、评估历史和外部数据不是迁移对象。导入后按需重新配置触发器。

## 接口与 SDK

- `POST /v1/workflow-packages/export`，请求 `{workflow}`，返回包并保存一份本地下载副本。
- `GET /v1/workflow-packages/exports/{digest}`，下载刚导出的包。
- `GET /v1/studio/workflows/{id}/package?revision=N`，直接下载已保存版本。
- `POST /v1/workflow-packages/preview`，检查依赖、能力、权限和绑定。
- `POST /v1/workflow-packages/import`，检查后原子导入。

预览/导入请求字段：`package`、`model_bindings`、`tool_bindings`、`credential_bindings`、`knowledge_bindings`、`memory_bindings`、`allow_development`、可选 `workflow_id`。映射均为“源名称 → 本机名称”。

```python
package = await source.export_workflow(workflow)
bindings = {
    "model_bindings": {"live-gpt": "my-model"},
    "credential_bindings": {"TYPESAFE_API_KEY": "MY_DECISION_KEY"},
}
preview = await destination.import_workflow(package, preview=True, **bindings)
if preview["ready"]:
    imported = await destination.import_workflow(package, **bindings)
    # 导入完成；需要运行时才调用 destination.submit(imported["workflow"])
```

JavaScript 为 `exportWorkflow` / `importWorkflow`；Rust 为 `export_workflow` / `import_workflow`。全部使用同一 REST 协议。

## 已调试通过的范围

集成场景见 `tests/integration/test_workflow_packages.py`：真实本地 HTTP 模型工具循环、子流程、Skill/策略冻结、JS 客户端往返、凭证与模型重绑、人工输入、条件产物、重启、篡改和冲突拒绝、资料库映射、开发权限、写审批与补偿版本。Rust SDK 的分享接口也随互操作集成场景实际调用。

真实服务演示：

```sh
uv run --extra app python -m examples.demos.workflow_share --live
```

需配置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`TYPESAFE_API_KEY`；会调用真实服务并可能计费。完整包导出后导入空数据库，模型别名和凭证变量重绑，Jev 分类为 `support`，GPT Responses Agent 调用 `core.echo`，最终文件内容为「售后需要处理」。运行编号仅保留在本地验收记录中。

可直接使用的脱敏样例包（来源 ID 已替换为示例名称并重新计算摘要）：[shared-classification.eah-workflow.json](../examples/workflows/shared-classification.eah-workflow.json)。证据目录：`.eah/live-acceptance/workflow-share-20260919T165941Z/`。浏览器已实际下载该文件、导入、关闭弹窗并显示完整节点与连线。此验收不代表所有媒体模型或所有外部插件已验证。

## 源码扩展依赖（2026-09-20）

工作流包现在支持随包携带不可变扩展源码、扩展之间的固定依赖以及扩展调用的声明式 API。接收端在预览中选择“检查并安装扩展”，先配置/暂存 API 和凭证名称，再明确授予源码扩展权限，最后导入流程。临时源码不会在普通流程导入时静默执行。

MCP 和通知/日历连接器为外部配置依赖，不复制账号、OAuth token 或本机配置版本号。Agent 和补偿节点导入时绑定接收端的兼容工具。扩展源码内部固定的服务名称及版本需在接收端保持兼容；不能静默改写已签名源码包。`/v1/workflow-packages/dependencies` 是仅暂存 API 的规范端点，依赖冲突/凭证重绑仍受事务检查。
