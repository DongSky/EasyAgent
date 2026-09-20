# 开源架构对照与设计修订

检查日期：2026-09-19。方法：拉取五个项目的 shallow checkout，读取 README、核心实现、相应回归测试。不是全面安全审计，也没有运行这些项目的全部测试。所有结论绑定下列 commit；不把历史 bug 当作当前缺陷。

## LangGraph

Commit `aa742fb31e2827d569b843e3600aeda2e0528e4b`。

- [SQLite saver](https://github.com/langchain-ai/langgraph/blob/aa742fb31e2827d569b843e3600aeda2e0528e4b/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py) 明确定位轻量同步用途；另有 async/Postgres 实现。这是具体后端的取舍，不是整个框架无法长时间运行。
- [Timed attempt scope](https://github.com/langchain-ai/langgraph/blob/aa742fb31e2827d569b843e3600aeda2e0528e4b/libs/langgraph/langgraph/pregel/_retry.py) 对超时后的写入做保护；仅有 cancel flag 不足以防止后台任务回写。
- 对我们的影响：所有 checkpoint、结果和调用记录提交必须检查租约 owner；保留 PostgreSQL 扩展位置，不把 SQLite 开发版包装成分布式引擎。

## PydanticAI

Commit `c4898abb54dc25ae6f6aef208a4c0661b30a455e`。

- [Durable execution](https://github.com/pydantic/pydantic-ai/blob/c4898abb54dc25ae6f6aef208a4c0661b30a455e/docs/durable_execution/overview.md) 已有多个执行引擎集成，还明确区分 run durability 与 conversation storage。
- [Usage](https://github.com/pydantic/pydantic-ai/blob/c4898abb54dc25ae6f6aef208a4c0661b30a455e/pydantic_ai_slim/pydantic_ai/usage.py) 在调用前检查请求数和工具预算，而非执行后才发现失控。
- 对我们的影响：模型/工具计数在调用前 checkpoint，恢复不能重置预算；记忆与运行状态分表。生产版应优先考虑复用成熟 durable backend，避免在自研调度上无限扩张。

## Microsoft Agent Framework

Commit `723256961e8e46980b869ee2c75ef05f16644921`。

- [WorkflowCheckpoint](https://github.com/microsoft/agent-framework/blob/723256961e8e46980b869ee2c75ef05f16644921/python/packages/core/agent_framework/_workflows/_checkpoint.py) 包含 graph signature、checkpoint lineage、消息与等待中的人类响应。
- Python/.NET、MCP、A2A、工作流和 durable 扩展已有完整布局，不能声称它缺少多语言或长期运行方案。
- 对我们的影响：run 保存不可变 workflow 和策略快照；未来迁移必须检验 schema 与定义兼容性。我们缩小成 Python/JS/Rust 的同一 HTTP/进程契约以降低入门配置量，代价是没有各语言原生完整执行引擎。

## Mastra

Commit `ed8f1d2b647bd3ab03e260471808dbbf47577e87`。

- [并发恢复实现](https://github.com/mastra-ai/mastra/blob/ed8f1d2b647bd3ab03e260471808dbbf47577e87/packages/core/src/workflows/workflow.ts) 使用 compare-and-set 抢占 suspended 状态。
- [并发恢复回归测试](https://github.com/mastra-ai/mastra/blob/ed8f1d2b647bd3ab03e260471808dbbf47577e87/packages/core/src/workflows/concurrent-resume.test.ts) 解释了历史 issue #20443：两个调用者从相同暂停快照恢复，可能重复执行下游副作用。当前检查的代码已修复，不能当作我们独有的优势。
- 对我们的影响：审批和恢复的数据库更新使用事务；加入并发恢复集成场景。无法确认外部写结果时保留待核验状态，不用全流程重跑掩盖不确定性。

## Rig

Commit `2d16c1b25f6749b3a2cd841beddf767106495069`。

- [Completion abstraction](https://github.com/0xPlaygrounds/rig/blob/2d16c1b25f6749b3a2cd841beddf767106495069/crates/rig-core/src/completion/mod.rs) 将统一 request/response 与供应商转换隔离。
- [Durable approval example](https://github.com/0xPlaygrounds/rig/blob/2d16c1b25f6749b3a2cd841beddf767106495069/examples/agent_with_durable_approval/src/main.rs) 已演示可序列化 AgentRun 跨进程审批；真正授权仍由工具内部执行。
- 对我们的影响：Rust 不应只是 README 上的“计划支持”；必须编译 SDK、通过 HTTP 跑任务、启动 Rust 工具进程。Tool 权限必须由执行器强制执行，不能仅放在提示词里。

## 我们能补什么

### 开发难度：以没有 Agent 开发经验的用户为基准

以下是根据 README 和所读示例判断的入门前提，不是经过小白用户测试的难度评分：

- PydanticAI：最短路径是创建 Python Agent 并调用 run；代码入口简洁，但自定义工具/类型、异步和部署仍需要 Python 知识。
- Rig：统一 trait 很适合 Rust 开发者，但 Rust 类型、Cargo、async 和工具参数结构都属于门槛，不能作为小白的默认入口。
- LangGraph：图、状态、节点、边、checkpoint 提供控制力；新手需要先理解这些概念，生产配置又增加存储与部署选项。
- Mastra：TypeScript 开发者有 CLI 和集成式开发体验；自定义 agent/tools/workflows 仍涉及 TS、schema 和环境配置。其产品中可能另有可视化功能，本次未做全产品可用性实测，不宣称完全没有无代码入口。
- Microsoft Agent Framework：模板覆盖较广，适合 Python/.NET 和已有工程环境；模型 client、executor、workflow、hosting 的组合需要学习。

**修订结论：只有三语言 SDK 并不等于简单。** EasyAgent 的默认入口必须是 Studio：先选可运行模板，再填自然语言用途和能力选项；SDK、DAG、MCP 配置只在进阶开发时出现。模型供应商差异集中在“连接模型”界面；权限暂停应显示具体操作和参数。

可衡量的后续比较：给 5–8 位没有框架经验的用户相同任务，观察首次成功运行时间、求助次数、错误恢复成功率、能否解释助手会执行什么。不先虚构“10 分钟零门槛”的结论。

本次检查没有证据证明“五个项目都解决不了某个关键能力”。值得验证的是特定组合的使用成本：本地一条命令启动；三语言一致的运行/审批/事件接口；非幂等生活操作的回执核验；可评估可回滚的策略；围绕事务办结组织的应用。

这些是产品和工程假设。需要用同一搬家项目比较首次接入时间、代码/配置量、故障恢复结果、重复副作用数量和人工介入次数，才能证明优势。0.1 不宣称超越成熟框架的可靠性。
