# 开发前差距基线（历史记录）

2026-09-20 扩展开发前的审计，不代表当前实现。当前状态见 [更新后的差距对照](PI_HERMES_GAP_ANALYSIS.md)。

## 1. 小白安装、连接与故障恢复：P0

**Pi** 已有安装和升级入口、模型登录、会话选择器，默认是终端产品。**Hermes** 已有 Desktop 安装器、首次配置、模型/凭证 UI、流式工具输出、文件和网页预览，并有更新入口；不能把当前 Hermes 简化为只适合程序员的 CLI。[P1][H1]

**我们** 有模型连接、节点画布和自然语言生成，但启动仍需 Python/uv，复杂 API、导入凭证映射和故障处理仍会暴露环境变量/Schema。图形界面不自动等于小白可用。

建议：桌面安装器管理运行环境；连接中心保存一次凭证，导入流程自动匹配能力；缺失项转为明确的“连接服务”步骤。验收：5–8 位未接触框架的用户，无终端、无指导完成安装→连接→导入→运行→定位一次错误。依据：`cli.py`、`static/model-connection.js`、`static/workflow-sharing.js`。

## 2. 长会话、追问、打断与上下文：P0

**Pi** 原生提供 streaming events、steering/follow-up 队列、abort、会话树、fork/clone、自动/手动摘要压缩。[P1][P2][P3] **Hermes** 有持久会话、恢复、自动压缩、会话检索和跨聊天渠道延续。[H2][H3]

**我们** 有 Run、Agent checkpoint、暂停/取消、人工输入和事件流；缺少独立 Conversation/Turn 契约、运行中追加指令、分支会话、逐 token 输出与可追溯的摘要压缩。`compact_context` 目前删除旧消息组；标准化模型适配器明确拒绝 `stream=true`，不能把运行事件流称为模型流式交互。

建议：会话负责持续沟通，已有持久 Run 负责实际执行；保存原始消息和摘要来源，支持安全边界上的打断/转向。验收：长对话跨重启保留约束、运行中改需求、取消后无遗漏副作用、摘要后能检索原证据。依据：`runtime.py`、`models.py`、`store.py`。

## 3. Self-evolve、长期记忆和技能沉淀：P0

**Pi** 提供 Skills/扩展和上下文挂钩；本次核对的 coding-agent 没有默认的“每次任务后自动沉淀并治理经验”产品闭环。**Hermes** 有 MEMORY.md/USER.md、Agent 记忆修改、FTS5 会话检索、`skill_manage`、`/learn` 和后台复盘；也有记忆/技能写入审批选项。[P1][H3][H4]

**我们** 已有记忆命名空间、RAG、策略候选→评估→批准→发布→回滚，以及 Agent 创建/更新声明式 API/工作流。缺少自动从完成/失败任务生成可复用经验、记忆合并与过期、技能候选评估、跨会话经验检索。已有自进化是有限范围，不能等同于 Hermes 的学习体验，也不能宣称会自行训练模型权重。

建议：保留发布门槛，增加“任务证据→经验候选→集成场景验证→发布→失败回滚”。验收：两个不同工作流复用同一学习成果；错误经验被拒绝或回滚。依据：`evolution.py`、`skills.py`、`knowledge.py`、`development.py`。

## 4. 临时写代码与工具编程：P0 架构，P1 交付

**Pi** 默认 read/write/edit/bash，适合 Agent 编写、执行和修改代码；其安全文档明确项目 trust 不是执行沙箱。**Hermes** 的 `execute_code` 提供 Python 程序经 RPC 调用工具，支持会话内核与不同执行后端；工具允许列表、超时和输出上限不等于 OS 隔离，真实隔离取决于后端。[P1][P4][H5][H6]

**我们** 能执行部署者提前安装的 Python/JS/Rust 插件，不能让 Agent 临时写任意源码、解析依赖、试跑修复、发布为可移植节点。`CodePackage`/运行器匹配当前只是元数据契约，子进程不是安全沙箱。

建议：按已确定的无 Docker 方案，实现受控 JS/WASM、宿主权限代理和不可变代码包；完整 Python/Node/Rust 工具链由兼容的电脑/服务器运行器承担。验收：网络/文件/凭证越权拒绝、超时和取消、重启恢复、源码包在第二个业务工作流和第二台兼容执行器复用。依据：`components.py`、`plugins.py`、[移动端策略](MOBILE_RUNTIME_STRATEGY.md)。

## 5. 动态多 Agent 协作：P1

**Pi coding-agent** 明确不内置 subagents，但仓库有子 Agent 扩展示例，可自建或装包。**Hermes** 原生有委派工具和受限子会话生命周期 API；当前该 API 元数据/结果仅在进程内保留一小时，重启 `reconnect` 返回不可恢复，不能推断为全持久分布式协作。[P5][H7]

**我们** 已有多角色节点、并行 DAG、foreach、子流程和有界运行时开发子任务；没有通用动态 Agent 身份/邮箱、双向消息、父子工具权限继承、任务重新委派和统一子成本回收。

建议复用现有持久子 Run，实现 spawn/send/wait/cancel/result 契约并收窄子权限。验收包含父/子分别崩溃、消息去重、子任务限额和结果汇总，不以多个 Agent 节点代替动态协作。依据：`runtime.py`、`development.py`、`contracts.py`。

## 6. 生活渠道、主动提醒与实际交付：P0 产品闭环

**Pi** 仓库主页把聊天网关和部分工作流放在独立 `pi-chat` 项目，本次未审计该仓库，不能计入 coding-agent 默认能力。**Hermes** 有多聊天平台 gateway、语音、cron；cron 明确区分执行成功和通知投递失败，并有失败事件、补跑等配置。[P0][H0][H8]

**我们** 有定时/间隔/webhook、去重和本地提醒，但没有真实邮件/日历接入、跨渠道持续对话、可靠通知投递回执；当前定时器也不是完整时区/夏令时 cron。生活 app 仍以本地管理为主。

建议先选一个真实通知渠道和一个日历服务，实现接入→工作流→确认→投递→回执，建立 outbox 和投递重试。验收覆盖断网、重复事件、错过定时、夏令时和“任务成功但消息没送达”。依据：`scheduling.py`、`examples/life_assistant/`。

## 7. MCP 和连接器的产品深度：P1

**Pi** MCP 属于扩展选择，非 coding-agent 默认内核。**Hermes** 已有连接引导、远程 OAuth/PKCE 与 token 刷新、动态工具列表、惰性发现缓存、连接健康和重试等功能。[P1][H9]

**我们** 有官方 MCP SDK 的 stdio/Streamable HTTP 客户端、服务端和工具 allowlist，但目前每次调用创建会话；没有 MCP OAuth 引导、长连接池、动态能力刷新、断线生命周期和服务健康面板。

建议先补 OAuth 和连接生命周期，缺少的高级 MCP 能力按真实服务需求逐项实现。验收：令牌过期、断线重连、工具列表变化、远端取消、更新后权限保持收窄。依据：`mcp_bridge.py`。

## 8. 插件生态与组件治理：P1

**Pi** 已有 npm/git 包安装、更新、固定引用、资源启停；Chord 实现 typed services、facet 生命周期、状态复制、内容摘要加载和热替换，并保持传输无关。它是 Pi monorepo 内的独立通用包，不意味着 coding-agent 的每个表面都已使用它。[P1][P6]

**Hermes** 有 Skills Hub、技能创建与维护、Desktop/plugin 扩展结构。[H1][H4] **我们** 已实现组件包和完整工作流包，可检查依赖、版本、权限并重绑凭证；仍缺源码/依赖锁打包、插件安装升级卸载、发布者信任、签名、兼容矩阵、弃用/迁移、验证场景随包共享。

建议在现有包格式上补 Package/Publisher/Compatibility/Validation 契约，不先做大市场。验收：签名异常拒绝、依赖冲突可解释、旧流程固定旧版、升级失败回滚。依据：`component_packages.py`、`workflow_packages.py`、`node_library.py`。

## 9. 长期运行与运维：P0 持续关卡

**Pi** 有会话持久化、发布供应链约束和运行事件；独立 `pi-durable` 的公开导出当前是存储/记录契约与 `MemoryStorage`，Pico 的完整设计不能都计入现有实现。Pi core 另有 SQLite 会话 backend，这是不同模块。Chord 远程服务存在实现，完整对称 RPC 仍有规划部分。[P0][P3][P6][P7]

**Hermes** 有 doctor/update、网关运行管理、会话库、cron 失败/投递诊断；这仍不能作为我们的性能或可靠性证明。**我们** 已有 SQLite 持久 DAG、lease/fencing、幂等、补偿、未知写结果核验和集成故障场景，但缺 72 小时真实负载、恢复演练、稳定的 trace/metrics 导出、容量/SLO，以及分布式执行。生产长期稳定性仍未验证。

建议先完成单机故障恢复和可观测性，再按需求引入远程 worker。验收：72 小时长稳、强杀/断网/磁盘故障、备份恢复、队列堆积可诊断。依据：`store.py`、`runtime.py`、[运行手册](OPERATIONS.md)。

## 10. Android/iOS：独立里程碑

**Pi** 有 Termux 文档；**Hermes** 将 Android Termux 列为 Tier 2，桌面和手机原生应用是不同承诺。本次证据不能证明任一项目已有可直接复用的 iOS 本地任意代码运行方案。[P1][H10]

**我们** 有跨平台契约和架构计划，尚无 Rust 执行核心、Android/iOS Host、后台生命周期或真机验收。Rust SDK 是客户端，并非可嵌入内核。

继续按共享契约→Rust Core→平台能力桥→受控运行器→可选跨设备执行推进。手机端先交付声明式流程与允许的本地能力，对需要完整工具链的节点显示执行位置和能力要求。不引入 Docker。验收以安装包和真机任务为准。

## 我们已有的方向性价值

可视 DAG、自然语言编译到显式 Workflow、Python/JS/Rust 统一接口、固定 API/子流程版本、完整工作流迁移、审批与补偿、媒体接口目录，都与“普通人组装自己的助手”的目标一致。Pi 会话分享不是我们的可执行流程包；Hermes 通过自然语言执行任务也不等于 ComfyUI 式编辑器。本次所读默认界面/文档未找到与我们同形式的完整 DAG 分享入口，但没有审计其所有第三方扩展，不能宣称独有。

同时，节点数量、模型目录数量和测试数不能代表产品成熟度或 Agent 能力排名。媒体协议夹具不证明真实生成质量，自然语言生成的一条成功流程不证明普遍可靠。

## 建议研发顺序

1. **S8 新手闭环与可持续会话**：安装/连接中心、依赖修复引导、Conversation/Turn、流式输出、转向与摘要。验收以真实新手任务和重启会话为准。
2. **S9 生活事务闭环与经验学习**：一个通知渠道、一个日历服务、投递回执、经验候选与可回滚技能。验收以真实多步骤生活任务办结为准。
3. **S10 受控代码与协作扩展**：无 Docker 的运行器、源码包、工具编程、动态委派、MCP 连接治理。
4. **S11 移动端与发行治理**：共享 Rust Core、Android/iOS、签名/升级/兼容、远程执行；长稳/故障验收贯穿各阶段。

这份顺序是后续计划，不把差距项标为本次已开发。已经实现的完整工作流分享见 [指南](WORKFLOW_SHARING.md)，集成与真实服务证据见 [验收](VALIDATION.md)。


## 原始依据（固定提交）

- [P0 Pi repository README](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/README.md)
- [P1 coding-agent README](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/README.md)
- [P2 sessions](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/docs/sessions.md)；[compaction](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/docs/compaction.md)
- [P3 core README](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/agent/README.md)；[agent.ts](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/agent/src/agent.ts)；[agent-loop.ts](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/agent/src/agent-loop.ts)
- [P4 security](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/docs/security.md)
- [P5 subagent extension](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/coding-agent/examples/extensions/subagent/README.md)
- [P6 Chord README](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/chord/README.md)；[public exports](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/chord/src/index.ts)
- [P7 durable README](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/durable/README.md)；[public exports](https://github.com/earendil-works/pi/blob/3c75b2747965e8d69ad9e17cbe788b2e33bf4d99/packages/durable/src/index.ts)
- [H0 Hermes README](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/README.md)
- [H1 Desktop](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/apps/desktop/README.md)
- [H2 sessions](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/sessions.md)
- [H3 memory](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/features/memory.md)；[session_search source](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/tools/session_search_tool.py)；[memory tool](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/tools/memory_tool.py)
- [H4 skills](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/features/skills.md)
- [H5 code execution source](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/tools/code_execution_tool.py)
- [H6 security](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/security.md)
- [H7 subagent lifecycle](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/developer-guide/subagent-lifecycle-api.md)；[delegate source](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/tools/delegate_tool.py)
- [H8 cron](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/features/cron.md)
- [H9 MCP](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/user-guide/features/mcp.md)
- [H10 platform support](https://github.com/NousResearch/hermes-agent/blob/d86a1687ca86da3612d55916eb7c96c0c887ac34/website/docs/getting-started/platform-support.md)
