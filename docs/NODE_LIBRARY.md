# 能力库：节点与子工作流

更新：2026-09-20。能力库分别保存节点和子工作流，两者共用版本、参数说明、依赖和验证信息。运行仍使用原有的预算、审批、持久化与恢复机制。

## 先区分保存的是什么

**节点**是一个独立操作，有一组输入输出。它可以执行多个普通代码操作，并不限于调用一个 API。例如清洗并校验一条记录，可以整体作为一个节点。Python 使用 `@node` 或 `Node(function)` 定义；代码分享走扩展包。

**工作流**保存步骤图；被其他流程调用时就是**子工作流**。提交、轮询、整理结果这样的多步流程，若保留各步状态与恢复点，就仍然是子工作流。画布上把它折叠为一张卡片不会改变这个事实。

能力库 API 的 `component_type` 明确返回 `node` 或 `subworkflow`，由来源定义决定，不允许靠改显示名称伪装类型。`source.kind=api/node` 实例化为一个普通 Step；`source.kind=workflow` 实例化为 subworkflow。

`NodeDefinition` 保存一个 Step 模板、输入输出 Schema 和默认值；拒绝把子工作流、foreach、goal 或外部连线塞进节点定义。工具节点背后的函数仍可包含任意普通代码。模板中的 `$input` 绑定到公开参数，消费者的引用和连线在其工作流中处理。

## 工作室用法

1. 打开“低代码 · 编排流程”的能力库，选择“节点”或“子工作流”，搜索能力，点击“加入画布”。
2. 已接入的服务直接复用。新服务配置一次凭证；环境变量适合重启后持续使用，界面填写的密钥只保留在服务端内存。
3. 填写组件输入并连接其他节点。实例固定到 API 或子流程版本；升级节点库不会替换已保存流程的旧版本。
4. 使用“将已有能力加入库”选择已经保存的 API、节点或工作流版本，弹窗会明确显示保存后的类型。Agent 在运行时创建的版本也会出现在来源列表。
5. 点击“导出组件包”，把文件交给其他项目；接收方使用“导入组件包”预览依赖、服务地址、权限和凭证要求。详见 [组件包](COMPONENT_PACKAGES.md)。

“加入库”是本机登记，不代表公开发布或经过质量认证。单节点组件包不打包模型/Agent 绑定和补偿步骤；这类完整流程请使用已实现的[工作流分享包](WORKFLOW_SHARING.md)。

## 通用分类与回执

`library.typesafe.evaluate` 接收任意 state 及命名 questions，支持 choice、noul、score，保留概率与原始响应。它不再固定为搬家/旅行分类。

`library.decision.classify_receipt` 组合结构化判断、结果提取和 JSON 回执，输入为 state、instructions、criteria、filename。输出包含类别、置信度、概率与回执。`library.json.receipt` 是可独立复用的单个 artifact 节点，新建分类流程直接嵌入这个节点，不再为保存文件额外创建子运行。

此前真实验收使用同一组件分别进行售后/销售分流、紧急/普通设备事件判断；导出后在空数据库重新绑定凭证，执行并重启后再执行成功。这只证明这两份具体输入的表现。

## 媒体组件

- `library.runway.image` / `.video`：提交图片/视频任务。安装时同时提供等待节点和 `_flow` 复合流程。
- `library.runway.wait`：GET 状态轮询；等待释放 worker，轮询次数/截止时间持久化，重启继续。
- `library.runway.cancel`：明确执行取消/删除远端任务。停止本地工作流只停止本地等待。
- `library.elevenlabs.speech`：文字转语音，二进制 MP3 保存为产物。
- “模型与接口目录”可按模型和具体接口生成其他原生节点，随后在此库复用/打包。见 [模型接口](MODEL_API_COMPATIBILITY.md)。

媒体提交属于外部写操作，先展示参数审批；缺失提交回执时保留不确定状态，不盲目重发。直连媒体模板通过本地 HTTP 协议集成测试，尚未真实生成。Runway 结果是会过期的远端链接，当前不自动下载 CDN 文件。

## 开发接口

`POST /v1/library/nodes` 保存单步节点定义（`id`、`definition`、`expected_revision`）；`GET /v1/library` 查看类型和可用性；`GET /v1/library/sources` 查看可登记版本；`POST /v1/library/publish` 创建组件版本；`POST /v1/library/{id}/instantiate` 生成可提交的固定版本 Step。`/v1/library/schema` 返回组件、运行器和代码候选元数据契约。

```python
async with HubClient() as hub:
    item = await hub.instantiate("library.decision.classify_receipt", inputs={
        "state": "已购设备需要维修", "instructions": "区分售后和售前",
        "criteria": {"support": "已购产品售后", "sales": "新产品咨询"},
        "filename": "decision.json"
    })
    run = await hub.submit({"name": "客服分流", "steps": [item["step"]]})
```

Python/JS/Rust SDK 均提供查询、实例化、组件包导入导出方法。Rust 示例为 `sdk/rust/examples/component.rs`。

## 移动端边界

组件声明平台、能力、网络来源、凭证别名和内存要求；解析器选择兼容位置，远端必须显式允许。它只返回执行计划，没有启动手机运行时或跨设备任务。

已实现共享元数据契约和 Python Hub 的能力校验；Rust Core、Android/iOS Host、JS/WASM 受限执行器和跨设备协议仍待研发。`CodePackage` 是候选元数据契约，不是已实现的自动编译/沙箱。后续不依赖 Docker，详见 [移动端策略](MOBILE_RUNTIME_STRATEGY.md)。

## 保存一个单步节点

以下代码在已有 `hub` 上注册模板；随后可在 App 的“将已有能力加入库”中选择来源，也可以直接发布：

```python
saved = hub.library.save_node({
    "id": "text.copy",
    "definition": {
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "step": {"id": "copy", "kind": "transform", "input": {"text": {"$ref": "$input.text"}}},
    },
})
hub.library.publish({
    "id": "text.copy", "kind": "node", "source_id": "text.copy",
    "source_revision": saved["revision"], "title": "复制文字", "description": "返回输入文字",
})
item = hub.library.instantiate("text.copy", {"input": {"text": "hello"}})
```

这里 `item["step"]` 是一个 transform 步骤，可放进任何工作流。接入注册工具时使用 `kind=tool` 和 `target`；复用版本化 API 时指定 `tool_revision`。组件包仍不自动安装自定义工具源码。

## 已有数据的兼容

已有工作流不会因为包含一步就被自动改成节点；历史子工作流和运行记录继续保持原结构。唯一自动升级的是内容完全匹配旧内置模板的 `library.json.receipt`：新增节点定义及组件版本，保留旧工作流和旧组件版本。固定旧版本的消费者仍按子工作流运行，自定义修改过的同名定义不会被转换。

新安装的分类流程不再嵌套回执流程，因此回执结果直接位于 `results[0].receipt`。已有保存版本继续保持原输出路径；按固定版本读取结果。

## 清理本地实验条目

对已被真实流程取代的调试节点、重复导入和验收草稿，可以归档。归档从工作室列表、能力库、来源选择及自动编排候选中移除条目，保留历史版本、固定引用、运行记录和产物。它不是禁用权限：知道 ID 的已有调用仍然可以执行。

目前通过本地 Python 管理（`hub` 是连接目标数据库的 `Hub`）：

```python
hub.development.set_archived("workflow", "experiment.flow", reason="已被正式流程取代")
hub.development.set_archived("api", "experiment.lookup", reason="一次性测试接口")
hub.development.set_archived("component", "experiment.component", reason="重复能力库条目")
hub.development.set_archived("workflow", "experiment.flow", False)  # 恢复到列表
```

`kind` 可以是 `api`、`node`、`workflow` 或 `component`。组件卡片与其来源分别管理，归档来源不会级联隐藏依赖它的有效组件。`get` 和默认 `list_versions` 仍能读取归档历史；发现可用定义时使用 `list_versions(kind, include_archived=False)`。不存在的 ID 会报错；保存新版本不会自动解除归档。

批量清理前先备份数据库，按明确的 ID 核对用途与引用，不要根据“测试”“验收”等名称自动删除。正式回归测试与隔离 fixture 应留在测试目录，不导入日常工作区。
