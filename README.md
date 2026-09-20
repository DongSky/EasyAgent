<div align="center">

# EasyAgent

**搭建、运行和分享你自己的 AI 助手。**

从自然语言、拖拽连线，到 Python、JavaScript 和 Rust 开发。

<p>
  <strong>简体中文</strong> | <a href="./README.en.md">English</a>
</p>

<p>
  <a href="./pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0%20preview-orange" alt="版本：0.1.0 开发预览"></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11 及以上"></a>
  <a href="#development"><img src="https://img.shields.io/badge/SDK-Python%20%7C%20JavaScript%20%7C%20Rust-6366F1" alt="Python、JavaScript 和 Rust SDK"></a>
</p>

<p>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/Server-AGPL--3.0--only-blue" alt="服务端：AGPL-3.0-only"></a>
  <a href="./docs/LICENSING.zh-CN.md"><img src="https://img.shields.io/badge/SDK%20%26%20Rust%20core-Apache--2.0-green" alt="SDK 与 Rust core：Apache-2.0"></a>
</p>

<p>
  <a href="#features">核心功能</a> ·
  <a href="#quick-start">快速开始</a> ·
  <a href="#examples">应用示例</a> ·
  <a href="#deployment">部署与平台</a> ·
  <a href="#development">开发与扩展</a> ·
  <a href="#documentation">文档</a>
</p>

</div>

---

## 项目介绍

EasyAgent 是一个带有可视化工作室的 AI Agent 开发框架。把模型、搜索、文件和外部 API 连接起来，就能让助手完成信息整理、内容生成、日程安排等多步骤任务。

当前为 **0.1 公开预览阶段**，适合体验和开发个人流程；已验证范围与已知边界见[预览说明](docs/PREVIEW_RELEASE.md)。

不熟悉编程，可以描述需求生成工作流，或在画布上拖拽连线；需要更多控制，可以修改流程配置、编写工具，或通过 SDK 开发自己的应用。这些方式使用同一套工作流，生成后可以继续编辑、运行和分享。

你可以看到每一步的输入、结果和错误。任务需要补充信息或确认操作时会暂停；启用目标检查后，助手还可以在指定的权限和预算内尝试修复失败、补充步骤。

<a id="features"></a>

## 核心功能

### 按自己的方式搭建

- **零代码**：用自然语言描述需求，查看生成的完整工作流；也可以在画布上添加节点、连接输入输出。
- **低代码**：修改 JSON 流程和节点参数，通过表单接入 HTTP API，或导入 OpenAPI 接口定义。
- **代码开发**：使用 Python、JavaScript / TypeScript、Rust 客户端调用服务；用 Python 内嵌框架，开发工具、扩展和应用。

### 执行、检查和跟进任务

- **对话办事**：发送需求和图片、音视频或文档，自动匹配已有流程，或生成并保存新流程；在对话里看执行进度、处理确认、下载结果。
- **补齐任务能力**：自动绑定已连接的图像编辑接口；缺少计算节点时写代码、独立测试并修正；需要新接口时搜索和读取文档，自己创建适配节点。通过验证的节点和 Skill 可供后续复用，见[全新任务如何完成](docs/AUTONOMOUS_TASKS.md)。
- **工作流编排**：条件分支、并行步骤、循环、子流程和多 Agent 协作。
- **运行记录**：查看进度、产物和错误；支持审批、补充输入、取消，以及从保存的进度恢复。
- **自动修复与改进**：检查目标是否达成，生成新的流程版本或代码节点，并评估候选改进。按配置的权限、预算和审批规则执行。
- **日常使用**：持续对话、资料检索、记忆、定时任务、通知和数据备份。

### 连接服务，复用能力

- **模型与媒体**：接入 Chat Completions、Responses、Messages 等模型协议，以及图像、视频和音频接口。
- **搜索与 API**：配置自己的 TinyFish 搜索连接，添加其他 HTTP 服务，管理各自的密钥。
- **扩展、MCP 与 Skills**：安装工具和操作技巧，添加命令、界面组件与事件处理逻辑。
- **导出与分享**：将节点、子流程或完整工作流打包，在其他工作区导入使用；接收方配置自己的服务凭证。

具体模型和参数的可用性取决于上游服务；协议接入范围见[模型与媒体接口](docs/MODEL_API_COMPATIBILITY.md)。

<a id="quick-start"></a>

## 先选一个入口

**写 Python 脚本或从命令行执行**：先读[从关键词搜索到生图](docs/GETTING_STARTED.zh-CN.md)，不需要启动服务。**使用浏览器画布**：按下面的工作室步骤操作。两者共用后端运行器，App 独立安装。

## 快速开始

### 1. 启动工作室

准备 [Python 3.11+](https://www.python.org/downloads/)（推荐 3.12）和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。下载仓库后，在项目根目录执行：

```sh
uv sync --locked --extra app --python 3.12
uv run --extra app easyagent studio
```

浏览器会打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。使用期间保持终端运行，按 `Ctrl+C` 停止服务。只使用工作室不需要安装 Node.js 或 Rust，也不需要单独构建前端。

### 2. 跑通第一条流程

保留服务终端，在另一个终端进入同一项目目录，执行：

```sh
uv run easyagent run examples/first-workflow.json --url http://127.0.0.1:8765
```

这个例子不需要 API Key：输入一段文字，经过回显节点，保存为 `hello.json`。运行状态为 `succeeded` 后，到工作室的「任务 → 查看结果」下载文件。

### 3. 创建自己的助手

1. 进入「设置 → 模型与服务」，填写模型地址和自己的 API Key，读取可选模型或手填模型 ID，保存并测试。已连接模型可编辑、删除，并可设置默认模型。
2. 打开「对话办事」，添加材料，描述希望完成的任务，例如：

   > 把学校通知整理成待办清单，列出日期、负责人和需要准备的物品。缺少的信息标为待确认，最后保存一份文件。

3. 保持「自动安排」，让它选择已有流程或生成并保存新流程。执行图会实时显示进度；需要补充信息或确认时，直接在对话里处理。也可以点击「创建助手」，先编排再运行。

对话输入框的「模型」和创建助手时的「构建工作流的模型」都可以单独选择；选择“自动”时优先使用设置中的默认模型。已保存工作流中的模型步骤保持原配置。

需要联网搜索时，在同一设置页面填写自己的搜索 API Key。模型和搜索费用由对应服务收取。安装或连接遇到问题，参阅[常见问题](docs/USER_GUIDE.zh-CN.md#常见问题)。

### 推荐接入的 API

建议先准备以下三类能力，就能跑通“关键词搜索 → 整理资料与提示词 → 生成图片”的完整案例。它们可以来自不同服务，也可以共用一个支持相应接口的服务。

- **大语言模型 API**：用于理解需求、编排流程、整理资料和撰写提示词。优先选择支持工具调用和结构化 JSON 输出的模型；需要理解参考图时，再确认模型支持图片输入。在「设置 → 模型与服务」填写服务地址、模型名称和 API Key，并按实际协议选择 Chat Completions、Responses 或 Messages。
- **联网搜索 API**：用于按关键词获取网页链接和摘要，为后续生成提供来源。项目已内置 **TinyFish** 搜索适配，在同一页面的「联网搜索」中填写自己的 API Key 即可接入。内置适配不包含服务额度；也可以通过自定义 HTTP API 接入其他搜索服务。
- **图片生成 / 编辑 API**：用于文字生图、参考图生成和图片修改。连接支持已知编辑协议的图片模型后，自动构建会绑定编辑节点并传入上传的原图，无需用户手写 schema。其他服务可以通过目录、接口文档发现，或按[生图教程](docs/GETTING_STARTED.zh-CN.md)配置。模型、尺寸和文件格式以实际服务支持为准。

按任务需要再添加：**视频生成 API**（图片转动画，通常还需要任务状态查询接口）、**语音识别 / 合成 API**（语音输入与朗读），以及 **Embedding API**（知识库语义检索）。不必一次配齐；各类能力需由所选服务明确支持，接入文字模型不会自动获得生图、搜索或语音能力。

配置前准备好接口文档、服务地址、模型名称（如适用）和 API Key。工作室接入步骤见[连接模型与搜索](docs/USER_GUIDE.zh-CN.md#连接模型与搜索)，脚本和命令行配置见[开发指南](docs/DEVELOPER_GUIDE.zh-CN.md#接入模型搜索与自定义-api)。

<a id="examples"></a>

## 应用示例

### 生活事务管家

第一个示例应用，用来跟进搬家、旅行、学校通知和家庭维修：从模板创建计划，分配负责人，设置截止时间，记录进度和待确认事项。

在另一个终端启动：

```sh
uv run --extra app easyagent life --port 8770 --database .eah/life.db
```

打开 [http://127.0.0.1:8770/life](http://127.0.0.1:8770/life)。这是一个本地单用户示例，可以作为开发自己应用的起点。[使用教程](docs/USER_GUIDE.zh-CN.md#生活事务管家) · [应用源码](examples/life_assistant/)

### 更多示例

- **调用工作流**：[Python](examples/getting_started/python_client.py)、[JavaScript](examples/getting_started/javascript_client.mjs)、[Rust](sdk/rust/examples/demo.rs)。
- **编写工具**：[内嵌 Python 工具](examples/getting_started/embedded_tool.py)、[扩展示例](examples/extensions/)、[Skill 示例](examples/skills/careful-planner/)。
- **生成图片和视频**：[Python / JavaScript / Rust 调用示例](examples/getting_started/media/README.md)，一次调用复用生图与视频子流程，获取命名结果；底层协议调试见[多媒体工作流](examples/demos/multimedia_workflow.py)和[验收记录](docs/MEDIA_ACCEPTANCE.md)。
- **分享工作流**：[可导入的示例包](examples/workflows/shared-classification.eah-workflow.json)和[分享教程](docs/WORKFLOW_SHARING.md)。

<a id="deployment"></a>

## 部署与平台

**桌面 App 打包**：GitHub Actions 的 [Desktop packages](.github/workflows/desktop.yml) 会为 macOS（Apple Silicon / Intel）和 Windows 64 位生成 ZIP 与 SHA-256 文件。在对应运行的 **Artifacts** 下载；也支持手动 **Run workflow**。桌面包自带 Python 和后端，启动后在系统浏览器打开工作室。下载、启动与签名状态见[桌面包说明](platforms/desktop/README.md)。

默认仅监听本机，使用 SQLite 保存工作区数据，数据库为 `.eah/hub.db`。使用同一数据库重新启动，可继续读取已保存的助手、连接和任务记录。

如需指定端口、使用独立工作区或关闭自动打开浏览器：

```sh
uv run --extra app easyagent studio --port 8780 --database .eah/my-project.db --no-browser
```

随后手动打开 [http://127.0.0.1:8780](http://127.0.0.1:8780)。客户端连接也要使用对应端口；命令行提交可通过 `--url` 指定地址。配置文件接入见[开发指南](docs/DEVELOPER_GUIDE.zh-CN.md#接入模型搜索与自定义-api)，认证、备份和恢复见[运维说明](docs/OPERATIONS.md)。

**当前为 0.1.0 开发预览版**，主要面向本地单用户使用，界面以中文为主。主要在 macOS 上开发和验证，已提供 Windows / Linux 构建配置。Android / iOS 原生宿主与嵌入式执行子集仍处于实验阶段，完整执行服务运行在电脑或服务器上；详见[移动端方案](docs/MOBILE_RUNTIME_STRATEGY.md)。

<a id="development"></a>

## 开发与扩展

Python 本地 SDK、HTTP 服务和前端 App 分开安装与启动；JavaScript / Rust 通过独立客户端调用 HTTP API，Rust core 提供嵌入式执行子集。

### 从代码调用

将下面的代码保存为 `hello.py`，执行 `uv run python hello.py`。无需启动服务：

```python
from easyagent import Agent, Sequential, tool

@tool
def clean(text: str) -> str:
    return text.strip()

flow = Sequential(clean, Agent("mock"))
print(flow("  Hello, EasyAgent!  "))
flow.export("flow.json")
```

`mock` 是离线回显夹具。替换成模型名称，并设置 `OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL` 即可连接真实模型。支持 `Module.forward` 自定义组合、异步调用、导出、审批和恢复，见 [SDK 与 CLI 指南](docs/SDK_GUIDE.zh-CN.md)。

分别启动后端与前端（两个终端）：

```sh
uv run --extra server easyagent serve --port 8765
uv run --package easyagent-app easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

`serve` 只提供 API；独立前端不导入执行引擎。`studio` 和桌面包显式组合两者。

### 添加自己的能力

- **已有 API**：从[搜索与自定义 API](docs/SEARCH_AND_APIS.md)开始，为接口定义输入、输出和凭证。
- **开发扩展**：参阅[扩展系统](docs/EXTENSIONS.md)，添加工具、命令、事件钩子或界面组件。
- **复用操作方法**：通过 [Skills](docs/SKILLS.md)打包说明与资源，或连接现有 MCP 服务。
- **开发应用**：参阅[开发指南](docs/DEVELOPER_GUIDE.zh-CN.md)和[接口契约](docs/contracts/)，复用运行、审批、事件和产物接口。

### 参与开发

欢迎修复问题、补充示例、适配服务和改进文档。反馈问题时，请附上环境、复现步骤、预期结果和去掉密钥的错误信息。

完整集成测试环境需要 Node.js 20+ 和 Rust/Cargo：

```sh
uv sync --locked --extra app --extra dev --python 3.12
uv run --extra app playwright install chromium
uv run --extra app --extra dev pytest -q tests/integration
```

源码入口为 `src/easyagent/`，多语言客户端在 `sdk/`，应用与工具示例在 `examples/`。更多结构说明见[代码地图](docs/DEVELOPER_GUIDE.zh-CN.md#代码地图与架构)。

<a id="documentation"></a>

## 文档

- [使用指南](docs/USER_GUIDE.zh-CN.md)：从连接服务到运行、查看结果和处理失败。
- [开发指南](docs/DEVELOPER_GUIDE.zh-CN.md)：SDK、工作流、扩展、MCP、Skills 和平台适配。
- [API 参考](http://127.0.0.1:8765/docs)：启动本机服务后可直接查看和调试接口。
- [全部文档](docs/README.md)：专题说明、开发计划和测试记录。

## 许可证

EasyAgent 按组件采用不同许可证：

- **框架服务端、工作室和生活事务管家**：[AGPL-3.0-only](LICENSE)。允许商用；修改后提供网络服务时，需按许可要求向用户提供对应源码。
- **独立 SDK、Rust core、公开接口定义和指定入门示例**：[Apache-2.0](LICENSES/Apache-2.0.txt)。方便集成到自己的应用，包括闭源产品。

完整目录范围及插件、生成内容的说明见[许可说明](docs/LICENSING.zh-CN.md)。仅需 Python 客户端或插件 SDK，可单独安装 [sdk/python](sdk/python/README.zh-CN.md)。
