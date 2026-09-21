# Agent 执行链路修复与验收（2026-09-21）

这次检查覆盖产品对话入口、模型协议、Agent 循环、工具回执、工作流执行器、动态委派和验收脚本。
修复依据是下面的具体缺陷与执行证据；不以提示词里写了“像 pi/Hermes”作为能力证明。

## 1. 模型与工具调用

- Responses 与 Anthropic 适配器原来会丢掉带工具调用的 assistant 正文。现在保留正文、调用和供应商续接状态；Anthropic 流式 thinking/signature 片段和 Responses reasoning 续接数据也会保留。
- 多个流式工具调用按 index 拼接和排序，Responses 的终结消息缺少 output 时从输出事件恢复。非完整终结仍拒绝执行，避免执行残缺参数。
- 单个调用参数不是 JSON 对象时，只给该调用返回可修正错误；同批其他合法调用照常执行。
- 用户补充消息与协作消息在完整工具结果组之后进入模型，避免拆开 assistant tool_calls 与对应结果。
- 压缩删除消息后重新计算保护位置，保留原始要求、后续要求和完整工具调用/结果组。
- 输出截断最多连续恢复两次；相同调用和相同结果反复出现时提示换方法，第四次停止。大工具结果保留完整附件，仅把有限预览送入模型；附件支持分段读取。

## 2. 核心文件工具

新增 `files.read`、`files.write`、`files.edit`、`files.find`、`files.grep`。

读文件可分页，搜索结果有路径、行号和截断标记。编辑匹配原文件中的唯一文本，支持一次多处修改；重叠、缺失、歧义或摘要不匹配时不写文件。通过临时文件和原子替换保存，保留换行与权限；可用读取结果的 SHA-256 检查并发修改。搜索优先用 ripgrep，没有安装时仍支持字面量搜索。

工具遵循已配置工作目录与执行模式。路径不能越出目录，写操作沿用确认/自动执行边界。默认工具批次并行读取，修改操作是顺序屏障；提供显式 `execution_mode` 给确实可以并行的工具。

## 3. 工作流与子 Agent 分层

- 工作流依赖图负责可持久化的串行、并行与汇合。独立计算不需要额外规划 Agent；只有需要判断或迭代工具的步骤使用 Agent 节点。
- `agents.parallel` 一次接受多个完整子任务，返回有序结果。每个子任务使用独立上下文；等待释放父 worker，不花模型调用轮询。已有 spawn/wait 接口继续支持父节点同时做其他工作。
- 工具调用的完成结果单独检查点保存。暂停或重启后，已经完成的并行兄弟调用不重做，调用 ID、结果顺序和计费保持一致。
- 委派深度只计算 Agent 委派边，不计算工作流包装层。`max_children`/`max_active` 按 Agent 节点计算，`RunLimits` 独立约束整个运行树。真实验收发现的“工作流内第一次委派就超深度”由此修复。
- 子任务省略 tools 时继承父工具与委派授权的交集；明确 `tools: []` 表示只推理。进一步委派不能放宽并发权限；大结果保留完整附件和稳定摘要。
- `workflows.schema` 提供真实的工作流与 Agent 节点契约，避免模型为了解字段而读取应用源码。保存工具还直接解释 artifact 节点、输入位置和依赖引用。

## 4. 产品与验收

选择“创建新流程”后，必须有保存回执和同一最新版本的成功执行回执才能结束；空口声称完成会被要求补做，持续不执行则失败。模型仍需检查业务结果，运行成功本身不证明任务正确。

对话卡现在区分工作流执行与协作助手，并通过运行结果接口收集整棵运行树的附件。修复了子流程或 Agent 工具生成文件后，对话下载区没有文件的问题。

`scripts/live_acceptance.py` 明确标注为执行冒烟记录，运行成功显示“交付物未验证”。严格验收入口是：

```sh
uv run python scripts/live_agent_acceptance.py --database .eah/hub.db --model gpt
```

它只读取一个已配置模型连接，在全新隔离数据库里通过浏览器输入自然语言，不预置节点、图或模型回复。独立检查以下结果：

1. 模型新增、测试并发布计算节点，生成两个独立分支和汇合的流程，保存并运行；下载的 `totals.json` 必须精确等于 Alice=19、Bob=12.5、Carol=9。
2. 第二条自然语言指令更换数据，调用原版本；结果必须精确等于 Dana=12、Eli=9.75、Finn=7，不能新增节点或改流程版本。
3. 一个工作流 Agent 节点派出两个子 Agent，模型执行时间必须实际重叠；保存的 `review.md` 必须包含正确总额 36 元和周六／周日矛盾。

2026-09-21 使用已连接的 `gpt-6-astra` / Responses 完成了这三条真实浏览器链路。
验收证据写入本地 `.eah/live-agent-acceptance/<run-directory>/`，不随代码发布。该目录可能包含运行输入、模型回复和本机路径；数据库、凭证、截图与完整调用记录均由 Git 忽略。
复核链路从首次 19 次模型调用降至 8 次（均包含子任务），定向复跑没有失败工具调用；这只是该案例的观察，不是所有任务的性能承诺。

定向复跑后的验收器曾把 model_calls.created 误写成 started，导致验证脚本报错；已修正查询，保留原始失败记录，并用原始执行证据重新验证，无额外模型调用。`verification.json` 记录修正后的校验结果：

```sh
uv run python scripts/live_agent_acceptance.py --verify ".eah/live-agent-acceptance/<run-directory>"
```

离线回归位于 `test_agent_execution_integrity.py`、`test_workspace_files.py`、`test_autonomy.py`、`test_learning_delegation.py` 与 `test_workspace_chat_browser.py`，检查协议、真实文件修改、重启、单 worker、节点内委派、额度、完成门槛和浏览器下载。它们使用明确的协议替身，不能替代上述真实模型验收。

发布前全套集成回归：`319 passed`，包含补充要求保护与协作消息的增补修改。Ruff、契约重新生成一致性、差异空白检查和凭证扫描通过；后端、前端及 SDK 构建包在独立环境中安装验证通过。待提交快照与构建包均检查过，未包含本地凭证或运行数据。

另一次真实产品验收只提交一条自然语言需求：从网上搜索游戏里若娜瓦的形象，然后生成一个若娜瓦的 Q 版头像。保持自动安排、自动选择模型和自动执行，不附带参考图或补充指令。产品自动选用了已有的角色头像流程，搜索并下载参考图，约三分钟生成 1024×1024 PNG；浏览器预览成功，下载文件与产物字节一致。这次验证的是单指令自动完成任务与已有流程复用，不是从零创建流程。原始记录和图片仅保存在被 Git 忽略的本地目录。

## 参考实现

本次实际读取了固定版本源码，参考其策略而未复制实现代码：

- [pi Agent 循环](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/agent/src/agent-loop.ts)：并行工具批次、结果顺序、顺序工具声明。
- [pi 精确编辑](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/src/core/tools/edit.ts)：对原文件做唯一、不重叠的多处替换。
- [pi subagent 插件](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/examples/extensions/subagent/index.ts)：独立上下文、并行任务集合、顺序链与结果限制。
- [Hermes delegate](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/tools/delegate_tool.py)：父能力继承、完整子任务说明、独立生命周期和有界结果。
