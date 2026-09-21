# 后端源码入口

先运行[离线最小示例](../../docs/FIRST_STEPS.zh-CN.md)，再按[一条消息的路径](../../docs/CODE_MAP.zh-CN.md)阅读。英文阅读路线见 [Code map](../../docs/CODE_MAP.en.md)。

## 按职责找代码

- `contracts.py` 定义数据，`modules.py` 把函数组合成工作流，`local.py` 提供脚本使用的 `Runtime`。
- `runtime.py` 组装 `Hub`、固定依赖并调度节点。`execution/agent.py` 执行 Agent 的模型—工具循环；`execution/context.py` 压缩上下文；`execution/model_calls.py` 处理一次模型请求的钩子、预算、回退和回执。
- `store.py` 保存运行、步骤、租约和检查点；`tools.py` 管理工具校验、审批和结果。移动执行代码时不能改变这些持久化边界。
- `models/registry.py` 注册和选择模型，`models/http.py` 适配 HTTP 协议，`models/streaming.py` 合并流式结果。
- `sessions.py` 接收会话消息；`workspace_chat/controller.py` 推进办事会话，`state.py` 定义阶段，`inputs.py` 绑定工作流输入。
- `autonomy.py` 提供自主 Agent 的任务要求与工具；`assistant_builder.py` 构造和验证工作流；`delegation.py` 管理子 Agent。它们都使用同一个 Hub 执行器。
- `extensions/host.py` 管理扩展生命周期，`package.py` 构建和校验扩展包，`provider.py` 适配扩展贡献的模型。扩展协议仍在 `extension_contracts.py`。
- `api.py` 和 `studio.py` 提供 HTTP 接口；浏览器页面在 `apps/agent/`，独立客户端在 `sdk/`。

## 保持一个入口、一份状态

`execution/` 的函数接收现有 Hub，不创建另一套运行器或状态容器。Hub 保留原方法签名并直接委托给对应实现，已有调用方和扩展不需要改动。

`models/`、`workspace_chat/`、`extensions/` 的 `__init__.py` 只公开原有导入入口。例如 `from easyagent.models import HTTPProvider` 仍然可用。旧的 `model_streaming.py` 仅保留导入兼容，新的实现集中在 `models/streaming.py`。

新增功能先放进已有职责，不要为一个调用增加新的注册器、服务层或工厂。先跑相关集成测试；完整回归和真实模型验收的入口见源码阅读路线。
