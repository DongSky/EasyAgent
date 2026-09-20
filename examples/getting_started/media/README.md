# 用代码生成表情和动画

**简体中文** | [English](README.en.md)

上传一张参考图，生成表情图片，再用图片生成短视频。Python、JavaScript 和 Rust 调用同一个已保存流程，下载 `expression.png` 和 `animation.mp4`。这些示例和三个 SDK 均采用 Apache-2.0。

默认模型为 `gpt-image-2.5-flare` 和 `doubao-seedance-2-5-260628`。服务地址、模型密钥和媒体协议配置在 Hub 服务端；客户端只需要 Hub 地址和可选的 Hub 访问令牌。

## 从关键词开始构建工作流

如果要学习如何编排，而不是只调用已保存流程，请先看[关键词搜索 → 生图完整教程](../../../docs/GETTING_STARTED.zh-CN.md)和[源码](research_image.py)。它接收关键词、参考图、画风与尺寸，搜索后分成提示词/生图和来源文件两路，再汇合交付。可直接在本地 SDK/CLI 中运行，不需要先启动工作室。下文继续介绍已保存媒体流程的三语言远程调用。

## 1. 准备一次

启动当前版本的工作室，并在模型接口目录连接自己的服务。使用已经连接的目录安装示例：

```sh
export EAH_URL=http://127.0.0.1:8766
uv run python examples/getting_started/media/setup.py
```

这一步保存三个流程，不生成图片或视频：`media.image`、`media.video`、`media.expression_video`。组合流程引用两个子流程的固定版本，可在画布里编辑、复用和分享。

没有连接目录时，可以给安装脚本传 `--base-url https://your-provider.example/v1 --api-key-env OPENAI_API_KEY`。密钥必须能由 **Hub 服务端** 的环境变量或凭证连接解析，不放入这三份调用代码。

Windows PowerShell 用 `$env:EAH_URL="http://127.0.0.1:8766"` 设置地址。启用 Hub 认证时，另设置 `EAH_TOKEN`；这是 Hub 的访问令牌，不是模型 API Key。

## 2. 任选一种语言运行

从仓库根目录执行，替换参考图片路径：

```sh
export EAH_DEMO_KEY=my-first-animation
uv run python examples/getting_started/media/python_demo.py path/to/reference.png
node examples/getting_started/media/javascript_demo.mjs path/to/reference.png
cargo run --manifest-path sdk/rust/Cargo.toml --example media -- path/to/reference.png
```

三个命令使用同一个 `EAH_DEMO_KEY` 和同一张图片时，会获得同一任务，不会生成三份视频。输出分别保存在 `output/media-sdk/python`、`javascript`、`rust`；可用第二个命令行参数指定输出目录。Node.js 需要 20+；Rust 需要 Cargo；Python SDK 随 `uv sync` 安装，也可单独安装 `./sdk/python`。

首次运行可能返回 `waiting_approval`。打开工作室「任务」，检查并确认对应调用，然后以同样的 Key 和输入重跑命令。已经完成的步骤会复用。示例没有自动批准外部写操作。

**同一操作继续使用原 Key；换图片、换提示词或希望重新生成时，使用新的 Key。** 同一 Key 配不同输入返回冲突，避免把一次重试误当成新任务。不设置时三个示例都使用 `media-demo`。超时只停止本地等待，不取消远端生成；不要为了恢复而换 Key。

## 3. 修改需求

三种语言的主干相同：上传 → 选择流程 → 提交业务输入 → 等待结果 → 下载。以 Python 为例，放在异步函数中：

```python
async with HubClient.from_env() as client:
    reference = await client.upload_file("reference.png")
    task = await client.workflow("media.expression_video").start(
        {"reference_image": reference["id"], "image_prompt": "画一个开心的 Q 版表情",
         "video_prompt": "角色轻轻挥手，固定镜头", "duration": 4},
        key="happy-wave-01",
    )
    result = await task.result(timeout=1900)
    await result.download("image", "expression.png")
    await result.download("video", "animation.mp4")
```

可以分别调用 `media.image`、`media.video`，输入为 `reference_image` 和 `prompt`，视频另有默认 `duration=4`。想在图像与视频之间插入审核或处理步骤，编辑组合流程即可，三个客户端无需复制这套编排逻辑。

Python 的 `workflow(..., revision=1)`、JavaScript 的 `workflow(..., {revision:1})`、Rust 的 `workflow(...).revision(1)` 可固定流程版本。不给版本时，第一次提交冻结当前版本，后续同 Key 重试仍使用它。

## 接口差异与二次开发

默认先上传图片取得 HTTPS 地址，再提交视频；服务支持内联图片时，安装用 `--reference-mode inline`。任务回执采用 `data.task_id` 时，安装用 `--task-id-path result.data.task_id`。上传操作、文件字段、返回图片路径、视频结果路径和模型名称均有安装参数，参阅 `setup.py --help`。修改已有示例需显式添加 `--replace`，会保存新版本。

命名输出在工作流 `metadata.outputs` 中声明，例如 `image` 指向实际图片产物。父流程使用子流程的 `outputs`，原始步骤输出仍保留在 `results`。模型名不保证所有服务都有相同参数；换协议时应在节点/流程层适配，运行一次验收再复用。

SDK 的 `request`、`submit`、`run`、`events`、`approve`、`respond`、节点库及分享接口继续开放。`workflow(...).definition()` 取得完整定义；`run_handle(id)` / `runHandle(id)` 恢复已知任务。需要源码内嵌执行仍使用原框架，HTTP SDK 不强制带入服务端。

附件和下载限制为 50 MB。视频下载是显式操作，使用独立匿名请求，不转发 Hub 令牌；支持 HTTPS 与本机 HTTP，不自动跟随重定向。远端临时链接过期时，需要服务方重新提供可访问地址，示例不会重新生成一份视频。失败下载不会覆盖已有目标文件。

## 这次简化解决了什么

原来的媒体验收程序需要发现协议、创建节点、处理多种回执、保存各阶段 ID、轮询和下载。这些步骤应由节点作者或流程作者配置一次。普通应用只关心任务输入和命名结果。

这次增加的是通用流程调用层，适用于文档、搜索、通知等流程。可扩展性来自完整定义、Schema、版本和底层接口；简化来自复用已保存的组合流程。Rust 仍需要显式类型与错误传播；首次接入全新媒体协议仍需要配置节点，不能把这部分工作说成已经消失。

后续最有价值的改进是让工作室从输入/输出 Schema 导出针对具体流程的类型和调用代码，并提供模板依赖检查。这样能进一步减少拼写错误和首次配置工作，同时继续使用同一套公开契约。本轮没有宣称已完成这套代码生成器。

集成测试会实际启动三个语言的进程，通过本地 HTTP 媒体协议 fixture 验证完整链路、两种参考图传递方式、两种任务 ID 回执、跨语言去重、审批续跑与下载。fixture 是协议测试材料，不能当作模型生成质量验收；既有真实媒体结果见[媒体验收记录](../../../docs/MEDIA_ACCEPTANCE.md)。
