# EasyAgent Python SDK

[简体中文](README.zh-CN.md) | **English**

The standalone HTTP client and extension SDK for EasyAgent. Licensed under [Apache-2.0](LICENSE); it does not install or import the AGPL server.

## Install from this repository

From the repository root:

```sh
python -m pip install ./sdk/python
```

The server workspace's `uv sync` also installs this package. No public package-registry release is assumed.

## Call a running server

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

Pass `token=` when the server requires authentication. `wait` also returns when approval or input is needed; inspect the status before treating a task as complete.

## Call a saved workflow

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

Run inside an async function after installing the [example workflows](../../examples/getting_started/media/README.en.md). `from_env()` reads `EAH_URL` and `EAH_TOKEN`. `RunStopped` provides `run_id`, `status`, and `state`; resume after approval/input with `run_handle(id).result()`. Retry the same operation with the same key and inputs, and choose a new key for a new generation. `workflow(id, revision=1).definition()` exposes the complete definition; `request` remains available for low-level calls.

## Write a Python extension

Import `Extension` and `ExtensionResponse` from `easyagent_client.extension_sdk`. This SDK handles the JSON process protocol and needs no server import. Register handlers with `extension.handler(name)` and call `extension.run(persistent=True)` for a persistent process.

The server package preserves `easyagent.client` and `easyagent.extension_sdk` as compatibility imports. Use `easyagent_client` for an independent client or plugin project. See the repository's [license scopes](../../LICENSING.md) and [developer guide](../../docs/DEVELOPER_GUIDE.en.md).
