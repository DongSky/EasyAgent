# EasyAgent Python SDK

**简体中文** | [English](README.md)

独立的 HTTP 客户端和插件开发 SDK，采用 [Apache-2.0](LICENSE)，不安装或导入 AGPL 服务端。

## 从仓库安装

在仓库根目录执行：

```sh
python -m pip install ./sdk/python
```

服务端工作区执行 `uv sync` 时也会安装这个包；目前不假定包已发布到公共包仓库。

## 调用已启动的服务

```python
import asyncio
from easyagent_client import HubClient

async def main():
    async with HubClient("http://127.0.0.1:8765") as client:
        run = await client.submit({
            "name": "hello",
            "steps": [{"id": "echo", "target": "core.echo", "input": {"message": "Hello!"}}],
        })
        result = await client.wait(run["id"])
        print(result["status"])

asyncio.run(main())
```

服务启用认证时传入 `token=`。`wait` 遇到待审批或待补充信息也会返回，需检查状态后再判断是否完成。

## 调用保存好的流程

```python
async with HubClient.from_env() as client:
    file = await client.upload_file("reference.png")
    task = await client.workflow("media.expression_video").start(
        {"reference_image": file["id"]}, key="my-animation-01"
    )
    result = await task.result(timeout=1900)
    await result.download("image", "expression.png")
    await result.download("video", "animation.mp4")
```

放在异步函数中执行；先按[示例说明](../../examples/getting_started/media/README.md)安装流程。`from_env()` 读取 `EAH_URL` 和 `EAH_TOKEN`。`RunStopped` 提供 `run_id`、`status`、`state`；审批或补充信息后可用 `run_handle(id).result()` 继续等待。同一 Key 配同一输入可安全重跑；需要新一轮生成时换新 Key。`workflow(id, revision=1).definition()` 读取完整定义，底层调用仍可使用 `request`。

## 编写 Python 扩展

从 `easyagent_client.extension_sdk` 导入 `Extension` 和 `ExtensionResponse`。SDK 处理 JSON 进程协议，不依赖服务端导入。使用 `extension.handler(name)` 注册处理函数，调用 `extension.run(persistent=True)` 启动常驻进程。

服务端包保留 `easyagent.client` 和 `easyagent.extension_sdk` 兼容入口。开发独立客户端或插件时使用 `easyagent_client`，详见[许可范围](../../docs/LICENSING.zh-CN.md)和[开发指南](../../docs/DEVELOPER_GUIDE.zh-CN.md)。
