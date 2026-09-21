# 自主执行架构（Operator）

2026-09-21。本文说明"对话办事"背后的自主执行引擎：一个带完整默认工具集、可写代码、可派生子任务、会记忆和沉淀技能的持久 Agent 循环。它参考 [Pi](https://github.com/earendil-works/pi) 与 [Hermes](https://github.com/NousResearch/hermes-agent) 的组织方式（观察 → 行动 → 观察，直到完成；写代码、派子 agent、写记忆、写技能都是普通工具调用），但实现复用本项目的持久化运行器，没有复制两者源码，也不声称功能等价。

## 之前为什么"什么都不会自动做"

运行时本身（`store.py` 的租约/幂等/子运行、`runtime.py` 的 ReAct 循环与上下文压缩、`tools.py` 的调用台账）是健全的，问题全在产品层：

1. **主构建路径不是 Agent 循环，而是一次性大 JSON 编译流水线。** `assistant_builder.py` 要求模型在看到任何执行结果之前一次吐出完整 `BuildDraft`（流程 + 代码 + 接口 + 搜索请求 + 待接入清单）；`verify` 才去测试和装接口，出错就让模型整段重写或进入每轮 ≤4 个 edit 的分段模式。
2. **每一种自主能力都存在，但都需要"运营者显式授权"，而产品的任何入口都从不发这些授权。** `code.*` 要 `code_development` 命名空间，`agents.*` 要 `delegation`，`memory.*` 要 `memory_namespaces`，`skills.save` 要 `skill_namespace`，`development.*` 要 `development` grant；构建目录还显式排除了 `development.` / `code.` / `agents.` / `skills.save`。
3. **工具异常直接让整个步骤失败。** 除参数校验错误外，任何工具异常都不会作为 observation 回喂模型，所以谈不上自我纠错。
4. **自缚的硬限制卡死构建过程。** 独立测试 ≤12 个用例（真实模型写了 32 个高质量用例被拒后循环）、搜索 ≤2 条、接口候选 ≤3 个、代码 revision 必须精确等于某个数、纯 JS 超时被压到 5 秒、生成流程里的 agent 节点不能带任何工具或记忆、终端超时 ≤120 秒。
5. **记忆与进化没有触发点。** `learning.propose` 只能手动调用或依赖没人会打的 `metadata.learn_as` 标；`evolution.evaluate` 需要手写评估集；任务结束后不写任何记忆。

## 现在的架构

```
对话办事
  ├─ 已存流程匹配（DispatchDecision 路由，高置信 → 绑定输入并执行；保留）
  └─ 创建流程 / 无匹配 → Operator 运行
        └─ 一个 agent 步骤（Hub.agent ReAct 循环；无 max_turns / max_tool_calls / 步骤时限）
             ├─ 观察：attachments.*、web.search、web.read、skills.list/read、memory.search、workflows.list、tools.describe
             ├─ 本地执行：backend.terminal（Shell / 随包 Python，可 pip 安装依赖）、backend.browser、attachments.export_file / import_file
             ├─ 造能力：code.create → code.test → code.publish（纯 JS 节点，自动进入节点库）、api.define（读文档后定义 HTTP 节点）、tools.call
             ├─ 复用：workflows.save（校验后保存为可复用流程，进入对话路由目录）、workflows.run（作为子运行执行并等待）
             ├─ 协作：agents.spawn / send / wait / status / cancel（子 agent 继承同一工具组与代码/开发/记忆/技能授权）
             ├─ 记忆与技能：memory.put / merge / remove（user、tasks、conversation:<id>）、skills.save（learned-*）
             └─ 交互：task.ask_user（持久等待用户回答）、task.request_connection（记录待接入的模型/服务）
        └─ 结束 → finalize：写 tasks/<run_id> 任务记忆；识别本轮保存的流程作为"已选流程"；有待接入记录则转 waiting_connections；
                         有实质工作时后台跑一次反思（autonomy.reflect）→ 记忆条目 + learned-* 技能包
```

- **自动纠错**：`Hub.observed_invoke` 把工具异常（含参数错误、未知工具、权限拒绝、HTTP 失败）转成 `{"error": {code, message, executed}}` 回喂模型，并记 `tool.failed_observed` / `tool.input_rejected` / `tool.unknown_requested` 事件。暂停信号（审批、等待子任务/远端/用户输入、不确定写入）、租约丢失、取消和用户显式设置的预算仍按原语义中断。
- **自动执行**：Operator 运行沿用 Codex 加入的 `execution=automatic` 模式（对话默认），已启用的写工具不再逐项确认；`code.publish`、`skills.save`、`memory.*` 本身是本地状态，改为 `effect=local`。
- **自动记忆**：Operator 的 agent 步骤自动带 `memory_namespaces=['user','tasks','conversation:<id>']`，运行开始时注入这些命名空间的最近记录；结束时确定性写入 `tasks/<run_id>`（请求、结果摘要、用到的工具、保存的流程、是否发布代码）。
- **自动进化**：反思步骤读取本次调用记录，产出 `Reflection{memory_notes, skill}`；记忆写入 `user` / `tasks`，可复用流程沉淀为 `learned-<name>` 技能包并立即出现在技能目录中（下一次 Operator 运行通过 `skill_access` 看到）。发布的代码节点、定义的接口节点、保存的流程分别进入节点库和流程目录。原有的策略候选 → 评估 → 发布 → 回滚门槛保留给"策略"，不再是唯一的进化通道。
- **子 agent**：`agents.spawn` 现在把父步骤已持有的 `code_development` / `development` / `memory_namespaces` / `skill_namespace` 透传给子步骤（仅当子工具列表需要且父授权已含），并接受 `instructions` 参数。父子共享根预算、审批与取消。

### 授权模型

Operator 一次性发出全部授权，而不是让模型自己申请：

| 授权 | 值 | 作用 |
| --- | --- | --- |
| `code_development.namespace` | `op_<turn 前 12 位>` | 代码包 ID 前缀，防止覆盖他人节点 |
| `delegation` | 所有已连接 chat 模型；工具组减去 `agents.*` / `task.*`；`max_children=16`、`max_depth=3`、可再委派 | 子 agent 范围 |
| `memory_namespaces` | `user`、`tasks`、`conversation:<id>` | 读写记忆 |
| `skill_namespace` | `learned` | 技能名必须为 `learned-*` |
| `skill_access` | 全部技能目录 | 按需 `skills.read` |
| `api.define` 命名空间 | `op_<run 前 12 位>.<name>` | 新接口节点前缀；凭证只按同源绑定已连接服务 |

不授予的：`development.*`（由 `workflows.*` / `api.define` 替代）、`host.*`、`autonomy.reflect`。

### 设置

`GET/PUT /v1/autonomy/settings`：

- `engine`：`auto`（默认；所选模型支持工具调用 `chat` 时用 Operator，仅 `decision` 的模型退回编译流水线）、`operator`、`compile`。
- `reflection`：任务结束后是否后台反思（默认开）。
- `memory_namespaces`、`max_children`、`max_depth`。

### 两种引擎的关系

对话办事只保留一条生命周期：回合先 `routing`（匹配已存流程）或 `working`（Operator 自主处理），装载流程后进入 `executing`，结束落到 `completed` / `waiting_connections` / `failed`。编译器不再有独立的 `building` 旁路——它现在是 **`development.compile_build` / `development.verify_build` 两个工具**，由 `engine=compile` 或"模型不支持工具调用"时启用：

| 场景 | 谁在编排 | 阶段 |
| --- | --- | --- |
| 模型支持工具调用（`chat`） | Operator，必要时把编译当作它的一步 | `working` → `executing` |
| 模型仅支持结构化输出 | 编译流水线（`assistant_builder` 提交两步构建运行） | `working`（构建运行挂在这条回合计时下） |

Studio「创建助手」直接调用编译工具，不走回合状态机。两条路径共用 `api_binding.bind_api_definition`（接口绑定）、`code_nodes.save_code_nodes`（纯代码节点发布）和同一套 `Phase` 词汇，所以新增阶段不会再出现"前端标签有了、控制器判断没有"的半接线状态。

### 拆掉的限制

`IndependentChecks` 12 → 64 个用例；`research_queries` 2 → 6，`api_candidates` 3 → 8，搜索读取 2/3/2 → 6/5/3；纯 JS/WASM 超时使用清单值（≤120 秒）而非 5 秒；终端超时上限 120 → 3600 秒（默认 300，可按调用传 `timeout_seconds`）；`AgentConfig.max_output_tokens` 默认 2048 → 8192，`ModelRequest` 上限 32768 → 262144（供应商声明的上限仍会截断；已知上下文窗口时输出预留不超过窗口的四分之一）；`verify()` 增加停滞检测（同一草稿对同一错误重复两轮即停止并报告，而不是无限循环）；对话办事支持 `steer`（运行中追加要求）。

## 边界

- Operator 的自主性来自模型本身的工具调用能力：仅支持结构化输出、不支持工具调用的模型仍走编译流水线。
- 纯代码节点依旧是无宿主权限的 JS/WASM；需要文件、网络或依赖的计算请用 `backend.terminal`（不是操作系统沙箱，命令以当前用户身份运行）。
- 反思产出的记忆和技能不经过评估门槛，但都带来源（`reflection:<run_id>`）、可查看、可删除、可回滚（技能有版本）。它不训练模型权重。
- 自动执行不等于安全：写工具跳过确认是用户在对话中显式选择的执行方式；SDK 默认仍为逐项确认。

## 代码与测试

- `src/easyagent/autonomy.py`：设置、工具组、agent 步骤与系统提示、finalize、反思、新工具。
- `src/easyagent/runtime.py`：`Hub.observed_invoke`、`Hub.autonomy`。
- `src/easyagent/workspace_chat.py`：`begin_operator`、`complete_operator`、`steer`；`Phase` 类集中定义全部回合阶段及其 `LABELS` / `ACTIVE` / `TERMINAL` / `REPAIRABLE` 分类，并通过 `enrich()` 下发给前端，避免前端各写一份。
- `src/easyagent/api_binding.py`：`bind_api_definition`（命名空间、凭证只按同源绑定）与 `publish_api_node`，编译器与 Operator 共用。
- `src/easyagent/code_nodes.py`：`save_code_nodes`，把发布的纯代码工具注册成节点库里的独立节点，天然幂等。
- `src/easyagent/delegation.py`：子 agent 授权继承。
- `src/easyagent/build_capabilities.py`：`bind_api_definition`（编译器与 Operator 共用）、停滞检测。
- `apps/agent/src/easyagent_app/static/workspace-chat.js`：`working` 状态、活动记录、运行中补充要求。
- `tests/integration/test_autonomy.py`：脚本化工具调用夹具覆盖"搜索 → 写代码 → 测试 → 发布 → 调用 → 记忆 → 保存流程 → 派子任务 → 反思 → 复用"、工具失败回喂与自我纠错、运行中 steer、`task.ask_user` 暂停恢复、待接入记录、引擎选择。这些是运行时契约测试，不是模型效果测试。
