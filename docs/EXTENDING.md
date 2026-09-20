# 扩展开发指南

先启动 Studio 完成一个助手，再导出项目或使用以下入口。源码模块按职责拆分，公开接口来自 `contracts.py` 和 `/openapi.json`。

## Python：嵌入内核与注册工具

```python
import asyncio
from easyagent.runtime import Hub
from easyagent.contracts import ToolSpec

async def main():
    hub = Hub(".eah/custom.db")
    async def add(args, context):
        return {"value": args["a"] + args["b"]}
    hub.tools.register(ToolSpec(
        name="math.add", description="Add two numbers",
        input_schema={"type":"object", "properties":{"a":{"type":"number"},"b":{"type":"number"}}, "required":["a","b"], "additionalProperties":False},
        output_schema={"type":"object", "properties":{"value":{"type":"number"}}, "required":["value"]},
    ), add)
    await hub.start()
    try:
        run_id = hub.submit({"name":"addition", "steps":[{"id":"add", "target":"math.add", "input":{"a":2,"b":3}}]})
        print(await hub.wait(run_id))
    finally:
        await hub.stop()

asyncio.run(main())
```

handler 必须 async，阻塞计算移到插件进程。会修改外部系统的工具声明 `effect="write"`；只有外部系统能使用 `context.invocation_id` 去重时才能承诺幂等，否则设置 `idempotent=False`，失去回执时交给人工核验。

## JS / Rust：同一服务

```js
import { HubClient } from './sdk/javascript/index.js';
const hub = new HubClient('http://127.0.0.1:8765');
const created = await hub.submit({name:'hello', steps:[{id:'a', target:'core.echo', input:{text:'hello'}}]});
console.log(await hub.wait(created.id));
```

Rust 见 `sdk/rust/examples/demo.rs`；`submit_typed(&Workflow)` 覆盖条件、子流程、输入和预算，`submit(&Value)` 与 `request` 可调用所有 v1 扩展。三个 SDK 的 wait 遇到 waiting_input / waiting_approval / needs_attention 会返回，由调用者显示问题或许可，不会假装运行已经完成。

## 插件

`examples/plugins/python.json` 是完整 manifest。`examples/config.extensions.example.json` 将其和示例 Skill 接入服务。JS 插件与 Rust 原生插件采用相同 stdin/stdout 协议；复制 manifest，调整 command 与工具名称即可。命令相对 manifest 目录运行。

新的源码扩展请使用[完整扩展协议](EXTENSIONS.md)，支持安装、权限/签名、版本、生命周期、UI 和服务。旧插件仅保留受信任 process 路径，Docker 已移除。普通进程不构成安全沙箱；纯计算可用内置 QuickJS/WASM。

## 模型配置

可在 Studio 连接并保存到本机加密凭证库，或通过服务端 JSON 配置（密钥可来自环境变量）：

```json
{
  "models": [{
    "alias": "my-chat", "dialect": "chat",
    "base_url": "https://YOUR-PROVIDER/v1", "model": "YOUR-MODEL-ID",
    "api_key_env": "MY_MODEL_KEY", "capabilities": ["chat", "decision"]
  }],
  "plugins": [], "skills": [], "mcp": []
}
```

把占位地址和模型 ID 改为供应商实际提供值。`dialect` 可为 chat、responses、anthropic；image/embedding 分别声明。可加 `fallback` alias 和 `input_price_per_million` / `output_price_per_million`（美元），价格必须由部署者维护。配置费用上限但价格未知时，调用会被拒绝；图片计费暂不做通用估价。

自定义模型实现 `async generate(request: ModelRequest, model: str) -> ModelResult`，通过 `hub.models.register` 注册。能力名允许自定义字符串，所以规则分类器、小模型、图像服务可以共用入口。Jev 已有通用 HTTP 工具接入示例，见 `examples/demos/live_workflow.py`；保留其 state/questions/answers 类型化原语，不伪装成普通聊天模型。

## 工作流高级节点

- `transform`：通过 JSON 常量与 `$ref` 重组数据，不执行任意脚本。
- `retrieve`：`namespace`、`query`、`mode`，返回带来源 citations。
- `artifact`：`name`、`content`、`media_type`，返回哈希与下载 id。
- `foreach`：`input.items` 与 `body` Workflow；子流程 `$input.item` / `$input.index` 可引用元素。
- `subworkflow`：把 input 合并到子流程初始输入；输出包含子 run id 与 results。
- `input`：`prompt` 与 JSON Schema；持久化暂停后由 `POST /v1/inputs/{id}` 恢复。
- `approval`：显示待确认对象，批准后继续；write 工具自身也强制确认。

实际组合见 `examples/demos/advanced.py` 和 `examples/scenarios/*.json`。领域应用可参考 `examples/life_assistant/app.py`：注册工具、调用 Hub.submit、添加业务表和 API；不要把特定生活领域塞进框架调度器。

## 无需编写适配器的 API

TinyFish Search 专用接入、自定义 HTTPTool 和 OpenAPI 导入见 [SEARCH_AND_APIS.md](SEARCH_AND_APIS.md)。三者注册到相同 ToolRegistry，代码、表单助手和节点画布均可使用；其他协议可经 Python/JS/Rust 插件或 MCP 实现。


## 复用已保存工作流与代码包路线

subworkflow/foreach 可使用 `workflow_ref: {"id": "组件 ID", "revision": 1}` 引用已保存组件。保存/提交时固定版本并展开 body 快照；输入映射、子运行和恢复仍使用统一内核。Studio 的“用作子流程”创建独立调用方。Agent 跨命名空间使用组件需 development.workflows 授权，并通过组件内部能力检查。

现有 Python/JS/Rust 插件可跨多个工作流使用；Agent 已能生成纯 JS/WASM 候选，经真实试跑后审批发布和复用。任意第三方依赖的自动安装与系统沙箱仍不提供。Python/Node/Rust 完整插件需要兼容工具链和精确源码信任，详见 [组件与跨平台方案](REUSE_AND_CODE_EXECUTION.md)。
