# EasyAgent 文档

**简体中文** | [English](README.en.md)

## 从这里开始

- **第一次写代码**：[离线最小示例](FIRST_STEPS.zh-CN.md)，无需 API Key。
- **准备修改框架**：[源码阅读路线](CODE_MAP.zh-CN.md)，从一条消息追踪到工具结果。
- **从真实任务学开发**：[关键词搜索 → 生图](GETTING_STARTED.zh-CN.md)，用参考图案例理解多输入、分叉、汇合与命令行。
- **查 SDK 用法**：[本地 SDK 与 CLI](SDK_GUIDE.zh-CN.md)；[SDK / App 包边界](SDK_AND_APP.md)。

- [项目介绍](../README.md)：了解 EasyAgent，启动第一个工作流。
- [使用指南](USER_GUIDE.zh-CN.md)：连接模型、创建助手、查看结果和处理失败。
- [开发指南](DEVELOPER_GUIDE.zh-CN.md)：编写工具、插件和应用，接入自己的服务。
- 示例代码：[Python](../examples/getting_started/python_client.py)、[JavaScript](../examples/getting_started/javascript_client.mjs)、[Rust](../sdk/rust/examples/demo.rs)、[自定义工具](../examples/getting_started/embedded_tool.py)。
- [三种语言生成图片和视频](../examples/getting_started/media/README.md)：安装可复用流程，上传参考图，一次调用下载表情和动画。

## 按需查阅

### 创建助手和应用

- [构建方式](DEVELOPMENT_MODES.md)
- [工作室界面](UI_REDESIGN.md)
- [对话办事与附件](CONVERSATION_WORKSPACE.md)
- [生活事务管家](LIFE_ASSISTANT.md)
- [节点库](NODE_LIBRARY.md)
- [复用组件与执行代码](REUSE_AND_CODE_EXECUTION.md)
- [导入和导出节点包](COMPONENT_PACKAGES.md)
- [分享完整工作流](WORKFLOW_SHARING.md)

### 接口与扩展

- [架构](ARCHITECTURE.md)与[接口规范](INTERFACES.md)
- [添加工具和模型](EXTENDING.md)
- [扩展系统](EXTENSIONS.md)与[服务后端](BACKENDS.md)
- [Skills](SKILLS.md)
- [运行时添加节点和流程](RUNTIME_DEVELOPMENT.md)
- [自主执行架构（Operator）](AUTONOMY.md)
- [Agent 工具、编排与真实自然语言验收修复](AGENT_EXECUTION_REPAIR.md)
- [两批精简与工作流构造改进](WORKFLOW_STREAMLINING.md)
- [自动检查与修复](GOALS.md)
- [搜索和自定义 API](SEARCH_AND_APIS.md)
- [模型与媒体接口](MODEL_API_COMPATIBILITY.md)
- [对话、通知和服务连接](CONVERSATIONS_AND_CONNECTIONS.md)
- 接口定义：[JSON Schema](contracts/)与[TypeScript 类型](../sdk/javascript/contracts.d.ts)。

### 部署与维护

- [权限、备份和故障处理](OPERATIONS.md)
- [Android 和 iOS](MOBILE_RUNTIME_STRATEGY.md)
- [集成测试配置](../.github/workflows/integration.yml)
- [应用打包配置](../.github/workflows/releases.yml)
- [许可范围与商用](LICENSING.zh-CN.md)

## 计划与测试记录

[最新开发与验收记录](NEXT_DELIVERY.md)汇总当前进度。以下文件保留了各阶段的设计和测试结果；旧文档中的功能状态以记录日期为准。

- 工具脚本：[真实模型验收](https://github.com/DongSky/EasyAgent/blob/main/scripts/live_acceptance.py)、[发布前脱敏扫描](https://github.com/DongSky/EasyAgent/blob/main/scripts/check_secrets.py)、[导出示例流程](https://github.com/DongSky/EasyAgent/blob/main/scripts/export_examples.py)。
- 开发计划：[原始计划](PLAN.md)、[扩展路线](EXTENSION_ROADMAP.md)、[功能检查](COMPLETENESS.md)。
- 设计参考：[开源项目研究](OPEN_SOURCE_REVIEW.md)、[模型与研究方法](MODELS_AND_RESEARCH.md)。
- 项目对比：[Pi/Hermes 早期对比](PI_HERMES_GAP_BASELINE.md)、[后续差距复核](PI_HERMES_GAP_ANALYSIS.md)、[Pi/Hermes/Codex/LoopX 学习与落地](COMPETITOR_LEARNINGS.md)。
- 集成测试：[历次记录](VALIDATION.md)、[构建方式](CONSTRUCTION_ACCEPTANCE.md)、[任务样例](TEST_SCENARIOS.md)。
- 专项验证：[扩展系统](EXTENSION_ACCEPTANCE.md)、[真实工作流](LIVE_WORKFLOW_ACCEPTANCE.md)、[图片和视频](MEDIA_ACCEPTANCE.md)。

## 维护文档

修改功能时，同步更新相关的中英文指南。示例命令应注明前提和运行位置，使用独立数据库验证，不包含密钥或个人文件路径。历史测试记录保留原日期，新结果另行补充。

- [Python 本地 SDK 与 CLI](SDK_GUIDE.zh-CN.md) · [Local SDK and CLI](SDK_GUIDE.en.md)
- [SDK / App 包边界](SDK_AND_APP.md)
