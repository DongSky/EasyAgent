# Python 本地 SDK 与命令行

**简体中文** | [English](SDK_GUIDE.en.md)

第一次使用请先完成[关键词搜索到生图教程](GETTING_STARTED.zh-CN.md)，本文作为接口和进阶用法参考。

普通脚本直接使用 `easyagent`。远程调用使用独立的 `easyagent_client`。
浏览器应用是第三个包 `easyagent-app`，通过同一 HTTP API 连接后端。

## 安装

本仓库尚不假定已发布到 PyPI。在仓库根目录执行：

```sh
uv sync --locked
uv run python examples/getting_started/media/research_image.py --export research-image.workflow.json
```

基础安装不依赖 FastAPI、前端包、MCP、浏览器、JavaScript 或 WASM 引擎。
上述命令只导出流程，无需密钥；实际执行的服务配置与参考图上传见入门教程。
按需要选择 `server`、`mcp`、`code`、`browser`、`documents`；`app` 安装全部应用依赖。
`uv build --all-packages` 生成三个独立 wheel。安装本地 SDK wheel 时同时提供 `easyagent-client` wheel。

## 多输入、分叉和汇合是默认能力

完整案例见[关键词搜索到生图](GETTING_STARTED.zh-CN.md)和[源码](../examples/getting_started/media/research_image.py)。`Module.forward` 的参数就是流程输入；节点可以接收多个参数、输出字典或列表，一份结果可以连接多个消费者。依赖根据结果引用推导，无依赖的节点可并行。

```python
class Picture(Module):
    def forward(self, keywords: str, reference_image: str, style: str):
        sources = evidence(self.search({"query": keywords}))
        prompt = self.writer(drawing_brief(sources, keywords, style))
        image = self.draw(prompt, reference_image, "1024x1024")
        notes = self.save_sources(source_notes(sources, keywords))
        return deliver(image, notes, prompt)
```

这是连接关系摘录，完整构造器与辅助节点见案例。`notes` 不依赖 `image`，可以先完成；`deliver` 等待所有参数的生产者。`Call("tool")(some_node(...))` 可以接收上一步返回的整份字典；它必须在运行时解析为对象。`Artifact("sources.md", media_type="text/markdown")(content)` 把结果保存为本次运行的产物。

`Sequential` 只把一个返回值传给下一个模块；不会猜测如何拆分字典或元组。后续节点需要多个参数时用 `Module.forward` 明确连线，错误会在构图阶段提示。

并行分叉与条件分支不同：当前 `forward` 不支持对符号值写原生 Python `if`。可恢复条件节点用 `Step.when` / `Workflow`，互斥分支的自动汇合不属于当前简写接口。不要依赖代码行顺序安排步骤。

## 定义、组合、执行

```python
from easyagent import Agent, Sequential, node

@node
def clean(text: str) -> str:
    return text.strip()

flow = Sequential(clean, Agent("mock"))
print(flow("  hello  "))
flow.export("flow.json")
```

这段代码可离线执行。`mock` 是明确的回显夹具，不是语言模型。
替换成真实模型名称，设置 `OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL`，即可使用已有 Chat Completions 适配器。
`Agent()` 读取 `EAH_MODEL`；未配置时明确报错。其他协议、备用模型和单价通过 `Runtime(config="config.json")` 或 `runtime.hub.models.register(...)` 配置。

`@node` 定义一个节点，`@tool` 保留同一套执行语义，侧重表达供 Agent 调用的工具；两者都可以放进 `Agent(tools=[...])`。`Node(function)` 是装饰器的显式写法。它们从类型标注生成输入输出 Schema，支持同步和异步函数。未标注的字段为任意 JSON。
默认用于本地计算；会对外写入的节点显式声明 `@node(effect="write")`，默认不假定幂等，执行前保留审批。
函数是受信任的本机代码，不是沙箱。同步函数在线程中执行；不能强行终止线程中的副作用，需要强隔离时使用进程扩展。

## 节点与子工作流的边界

`@node` 的函数体在执行时运行，允许普通 Python 分支、循环、计算和辅助函数调用。无论内部有几个操作，对工作流来说都是一个步骤；审批和重试作用于整个节点。不要在函数体里启动 SDK 模块，它们应在外层组合。

`Sequential` 和 `Module.forward` 在构图时展开到同一个工作流，不会自动创建子运行。`Subflow(module)` 才保留独立子图：

```python
from easyagent import Agent, Sequential, Subflow

review = Sequential(Agent("mock"), Agent("mock"))
flow = Sequential(Subflow(review), Agent("mock"))
print(flow("hello"))
```

父运行有两步：调用子工作流、调用 Agent；子运行有两步。内联 `Subflow(module)` 返回模块本来的结果，`Subflow("saved.id", revision=1)({...})` 返回已保存流程的命名输出字典。审批、预算、取消和恢复仍贯穿父子运行。

保存成工作流并不等于保存成节点，即使只有一步也保持工作流类型。能力库中的单步模板使用独立 `NodeDefinition` 契约，实例化直接返回一个 Step；完整图使用 Workflow 契约并实例化为 subworkflow。详见[能力库](NODE_LIBRARY.md)。

## 自定义结构

```python
from easyagent import Agent, Module

class Review(Module):
    def __init__(self):
        self.draft = Agent("mock", instructions="起草回复")
        self.check = Agent("mock", instructions="检查遗漏")

    def forward(self, prompt: str):
        draft = self.draft(prompt)
        return {"draft": draft, "review": self.check(draft)}

print(Review()("安排搬家"))
```

`forward()` 在构图时运行；模块调用只创建节点，不调用模型或工具。引用自动建立依赖，无依赖的步骤可并行。
可以使用索引取字段，例如 `value["image"]["id"]`。构图期不能对未知值做 `if`、循环、字符串格式化或数值计算；把数据处理写成 `@tool`，或用底层 `Workflow` 表达 `when`、`foreach` 和目标循环。它借鉴可调用模块和组合方式，不宣称完整复刻 PyTorch 的动态图。

`Agent` 默认返回文本；设置 `response_schema` 后返回结构化数据。
`Model` 执行单次模型调用，返回完整 `ModelResult`。
`Call("api.node")({"argument": value})` 复用已注册 API/工具节点。
`Subflow("saved.id", revision=1)({...})` 复用已保存子流程并返回命名输出。实际执行时固定版本。

## 生命周期、记录与异步

```python
from easyagent import Agent, Runtime

agent = Agent("mock")
with Runtime(".eah/project.db", key="operation-1") as runtime:
    result = runtime.run(agent, "hello")
    print(result.id, result.value, result.outputs)
```

无显式 Runtime 时，每次调用使用 `.eah/local.db`，也可设置 `EAH_DATABASE`。
同一会话多次调用可使用 `with Runtime()`。默认本地会话只执行自己提交或显式恢复的任务及其子运行，不领取其他脚本的排队任务，也不启动工作区定时调度、维护和消息投递。传入 `Runtime(hub=...)` 则沿用调用者提供的 Hub 执行范围。`key` 标识一次具体操作；相同 key 必须保持相同工作流与输入，否则报冲突。
批量执行不同操作时省略 key，或为各操作分别创建会话。底层 `runtime.hub` 保留模型、扩展、Skills、预算、日志和调度接口。

```python
async with Runtime(".eah/project.db") as runtime:
    result = await runtime.arun(agent, "hello")
    value = await agent.acall("another message")
```

异步示例放在已有协程中运行。同步调用不会偷偷嵌套事件循环。
`RunStopped` 提供 `run_id`、`status` 和完整 `state`；等待审批、补充输入和失败都不会被当成成功。
批准后调用 `runtime.resume(id)` / `await runtime.aresume(id)`。
超时保留运行记录；退出本地 Runtime 会停止其 worker，继续执行需重新打开同一数据库、加载原代码并恢复。后台无人值守任务应使用持续运行的 Hub/服务。

## 命令行

```sh
easyagent run examples/getting_started/media/research_image.py:flow --input @inputs.json --config research-image.config.json --database .eah/project.db
easyagent run flow.json --input @inputs.json --database .eah/project.db
easyagent export examples/getting_started/media/research_image.py:flow --output flow.json
easyagent inspect RUN_ID --database .eah/project.db
easyagent events RUN_ID --database .eah/project.db
easyagent approve INVOCATION_ID --yes --database .eah/project.db
easyagent resume RUN_ID --source examples/getting_started/media/research_image.py:flow --config research-image.config.json --database .eah/project.db
```

这里的 `inputs.json` 包含 `keywords`、已上传到同一数据库的 `reference_image` 产物 ID、`style` 和 `size`。首次使用可直接运行[教程中的脚本命令](GETTING_STARTED.zh-CN.md)，脚本会负责上传参考图。含 Python 节点的导出图执行与恢复需要原代码，使用上面的 `.py:flow` 入口；`easyagent run flow.json` 适用于只依赖已注册工具的流程。

`--input -` 从 stdin 读取 JSON 对象；stdout 只输出 JSON，工具诊断走 stderr。
退出码：`0` 成功，`1` 执行失败/取消，`2` 输入/配置/传输错误，`3` 等待人工处理，`4` 等待超时。
`--key` 提供幂等标识，`--timeout` 单位为秒。`python -m easyagent` 与 `easyagent` 等价。
运行 `.py` 文件等于执行本机 Python 源码，请只加载可信项目。

导出 JSON 不会包含 Python 函数源码。它保留函数指纹，恢复时需要原文件；缺少或改变已注册函数会拒绝恢复。
指纹用于发现函数源码变化，不替代对 Python 依赖、全局状态和环境的版本管理。跨机器分发源码使用[扩展包](EXTENSIONS.md)。

## 远程调用与三语言媒体示例

```python
from pathlib import Path
from easyagent_client import Client

with Client() as client:
    generate = client.workflow("media.expression_video", key="animation-1", timeout=1900)
    result = generate(reference_image=Path("reference.png"))
    result.download("image", "expression.png")
    result.download("video", "animation.mp4")
```

先安装[示例工作流](../examples/getting_started/media/README.md)。`Client()` 读取 `EAH_URL`、`EAH_TOKEN`；`Path` 输入自动上传，普通字符串保持原值。
异步客户端可使用 `await client.workflow(id).run(inputs, key=..., timeout=...)`，也保留 `start()` / `result()`。
JavaScript 使用 `.run(inputs, {key, timeoutMs})`，`file(path)` 上传 Node 文件，浏览器可传 `File`/`Blob`。
Rust 使用 `.workflow(id).timeout(Duration::from_secs(1900)).run(&inputs, Some(key)).await?`。
JavaScript/Rust 是独立 HTTP 客户端；它们没有因此获得完整 Python 本地执行引擎，Rust `core/` 仍是可嵌入执行子集。

```sh
easyagent run media.expression_video --url http://127.0.0.1:8765 --input @inputs.json --key animation-1 --timeout 1900
```

此处 JSON 中的 `reference_image` 是已上传的 artifact ID。远程运行超时只结束等待，不停止服务端任务。

## 分开启动后端与前端

```sh
uv run --extra server easyagent serve --port 8765
uv run --package easyagent-app easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

第二条在另一个终端执行。`serve` 只提供 API；前端可独立安装，也可使用任意静态服务器配合同源反向代理。
生活示例 API 由 `serve --with-life` 启用。`uv run --extra app easyagent studio` 是显式组合启动，桌面包同样组合两者。
部署与代理约束见[前端包说明](../apps/agent/README.zh-CN.md)。

Python 本地运行器/SDK 与 App 保持 AGPL；独立 HTTP/扩展 SDK 保持 Apache-2.0，详见[许可范围](LICENSING.zh-CN.md)。
