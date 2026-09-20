# EasyAgent 许可说明

**简体中文** | [English](../LICENSING.md)

版权所有：2026 EasyAgent contributors。各组件按下列范围授权，已有第三方声明和许可证继续适用于对应材料。

## 默认：AGPL-3.0-only

除下文列出的 Apache-2.0 组件外，EasyAgent 原创源码和文档采用 **GNU Affero General Public License 第 3 版，仅此版本**。完整文本见根目录 [LICENSE](../LICENSE)。

范围包括 `src/easyagent/` 中的 Python 本地 SDK/运行器和可选服务端、`apps/agent/` 中独立打包的前端、`examples/life_assistant/` 中的生活事务管家后端，以及 `platforms/` 中的桌面和移动应用宿主。

AGPL 允许商用和收费。分发受其覆盖的软件时，需要遵守源码和声明保留等要求；修改程序后让用户通过网络与它交互，第 13 条要求向这些用户显著提供获取对应源码的方式。具体要求以许可证全文为准。

## Apache-2.0 组件

以下 EasyAgent 原创组件采用 [Apache License 2.0](../LICENSES/Apache-2.0.txt)：

- `sdk/`：独立 Python、JavaScript / TypeScript、Rust 客户端，插件开发 SDK，以及各自的示例。
- `core/`：可独立嵌入的 Rust 执行核心及其示例。
- `docs/contracts/`：公开的 JSON Schema 接口定义。
- `examples/getting_started/`、`examples/plugins/`、`examples/extensions/`、`examples/skills/`：入门工具、扩展和 Skills 示例。
- `examples/first-workflow.json`：通过同名 `.license` 文件标明许可。
- 根目录中英文 README、中英文使用指南和开发指南里的代码示例；周围的说明文字采用默认许可。

这些组件可以用于闭源产品，需遵守 Apache-2.0 中保留许可证、声明、修改说明和专利等条款。每个独立发布的 SDK 和 Rust core 都附带自己的许可证及 NOTICE 文件。包名不改变授权范围。

## 开发自己的应用时

只需要 Python HTTP 客户端或插件 SDK，可以安装 `sdk/python/` 中的独立包，导入 `easyagent_client`。它依赖 `httpx`，不导入 AGPL 服务端。原有的 `easyagent.client` 和 `easyagent.extension_sdk` 是服务端包内保留的兼容入口。

独立应用通过 HTTP API 调用服务，通常与服务端属于独立程序。插件或嵌入服务端代码的应用是否构成受许可覆盖的组合，需要结合实际调用和组合方式判断。SDK 或示例采用 Apache，并不会免除 AGPL 依赖自身的义务；例如 `embedded_tool.py` 会嵌入 AGPL Python 运行器。

用户的工作流、提示词、自写插件、数据、密钥、生成的媒体和其他输出，不会仅因使用 EasyAgent 自动成为 AGPL。若内容包含受许可覆盖的代码，需按实际内容判断。这份说明不会额外授予第三方材料、服务内容或用户文件的权利；本机 `output/` 和 `.eah/` 内容不在源码授权范围内，也不进入 Python 发行归档。

## 包元数据和 GitHub 显示

服务端发行包同时包含 AGPL 服务端和 Apache 示例，因此包元数据使用 `AGPL-3.0-only AND Apache-2.0`。独立 HTTP/扩展 SDK 和 Rust core 使用 `Apache-2.0`。独立 `easyagent-app` 包为 `AGPL-3.0-only`，前端迁移不改变许可。每个组件适用指定的许可，`AND` 表达的是发行包里包含的不同组件。

根目录 `LICENSE` 保留标准 AGPL 原文，GitHub 可以据此识别主许可证。仓库摘要无法完整表达各目录的许可范围，因此 README 同时显示两种许可的徽章，并链接到这份范围说明。实际识别结果由 GitHub 在推送后生成，见 [GitHub 官方说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)。

## 贡献和声明

除非与维护者另行约定，贡献按所属组件的许可证提交。复制或分发组件时，保留适用的版权、许可证和 NOTICE 文件。第三方依赖继续使用各自的条款。移动组件代码时，同步维护许可范围、包元数据、生成的 SDK 文件头及中英文说明。
