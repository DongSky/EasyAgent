# 从一条消息开始读源码

[English](CODE_MAP.en.md) · [离线最小示例](FIRST_STEPS.zh-CN.md) · [开发参考](DEVELOPER_GUIDE.zh-CN.md)

先运行最小示例，再按下面的调用顺序阅读。目录按“谁负责什么”查找，执行问题按“请求走到哪一步”定位。

## 仓库入口

- `examples/getting_started/first_steps.py`：最小用法。先改这里，验证普通函数如何变成节点。
- `src/easyagent/`：Python SDK 和后端。`contracts.py` 定义数据契约，`modules.py` 构图，`local.py` 提供脚本用的 Runtime。
- `apps/agent/src/easyagent_app/static/`：浏览器页面。使用 HTTP API，不直接访问数据库或模型凭证。
- `sdk/python/`、`sdk/javascript/`、`sdk/rust/`：远程客户端。它们调用同一个后端，不重新实现执行器。
- `tests/integration/`：回归场景；`scripts/live_agent_acceptance.py`：通过产品页面执行真实模型验收。
- `docs/`：当前指南从本页和开发参考进入；带日期的验收记录描述当时的结果。

## 一条自然语言消息的路径

1. **页面发送**：在 `workspace-chat.js` 找 `send()`。它收集文字、附件和执行方式，调用 `/v1/conversations/{id}/messages`。
2. **接收与保存**：在 `sessions.py` 找 `install_conversations()` 和 `Conversations.send()`，再进入 `workspace_chat/controller.py` 的 `WorkspaceChat.send()`。消息先保存，再异步执行。
3. **选择处理方式**：`WorkspaceChat.begin()` 匹配固定版本的已有工作流，或者进入自主 Agent / 编译器。`decide()` 处理匹配结果，`bind()` 绑定业务输入。
4. **构造工作**：`autonomy.py` 为自主 Agent 提供工具和任务要求；`assistant_builder.py` 为编译路径生成、验证流程。两条路径都提交同一种 `Workflow`，不是两套执行器。
5. **执行节点**：`runtime.py` 的 `Hub.submit()` 固定依赖，worker 领取步骤，`execute()` 按节点类型执行。`store.py` 保存步骤、检查点和事件。
6. **Agent 调用工具**：`Hub.agent()` 转入 `execution/agent.py`，在模型请求和工具结果之间迭代；`execution/context.py` 管理上下文压缩，`execution/model_calls.py` 管理单次模型调用；`tools.py` 的 `ToolRegistry.invoke()` 处理校验、授权、审批和回执。工具调用失败和工作流重新编排是不同层次的恢复。
7. **显示结果**：`WorkspaceChat.tick()` 推进会话状态；页面轮询进度，在完成后读取结果并展示附件。展开详情才加载完整执行记录。

只读 SDK 的路径更短：`Sequential / Module → Runtime.run → Hub.submit → worker → execute`，不经过页面和对话匹配。

## 常见修改该从哪里开始

- 新增普通计算：从 `@node` 示例开始；无需改调度器。
- 修模型工具调用或流式片段：检查 `models/http.py` 和 `models/streaming.py`，回归 `test_agent_execution_integrity.py`。
- 修并行、暂停和恢复：检查运行器与 `store.py`，回归 `test_runtime.py`、`test_run_retry.py`。
- 修自然语言选择或构造流程：检查 `workspace_chat/controller.py`、`autonomy.py`、`assistant_builder.py`，回归 `test_workspace_chat.py`、`test_workflow_planning.py`。
- 修对话显示或下载：从 `workspace-chat.js` 进入；请求、状态、模板、图和活动记录位于相邻的 `chat/` 目录，运行 `test_workspace_chat_browser.py`。

## 修改后的验证

先运行与改动相关的测试，再跑完整回归。全量测试需要开发及应用依赖，浏览器测试需要 Chromium：

```sh
uv sync --locked --extra app --extra dev
uv run playwright install chromium
uv run pytest -q
uv run ruff check src apps tests scripts examples
```

真实服务验收会产生 API 费用，不能用离线夹具的成功代替它。入口和所需连接见[Agent 执行验收](AGENT_EXECUTION_REPAIR.md)。
