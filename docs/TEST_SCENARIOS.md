# 公开测试任务 → 本地集成场景

检索与核对日期：2026-09-19。用户要求按不同任务编排测试，本文件维护来源、改编方式、运行入口和判定依据。代码和数据均为本项目独立编写的合成样例，没有复制官方数据集中的个人信息、订单或完整题目。

## 为什么采用这些任务

ToolSandbox 强调状态依赖、信息不足和中间里程碑；τ-bench 强调业务政策、用户确认和最终数据库状态；BFCL 区分多工具、缺函数/缺参数、长上下文及记忆；AgentDojo 把正常任务与恶意外部内容结合，并核对环境差异。这些分别补足纯「请求返回 200」测试的盲点。

## 固定来源

### ToolSandbox

- 仓库：<https://github.com/apple/ToolSandbox>
- 已检出 commit：`c8571d7854316d2e1c5f288e59fe1e34e53f6dd1`。
- [多工具场景](https://github.com/apple/ToolSandbox/blob/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1/tool_sandbox/scenarios/multiple_tool_call_scenarios.py)：`search_reminder_with_recency_upcoming`。
- [信息不足场景](https://github.com/apple/ToolSandbox/blob/c8571d7854316d2e1c5f288e59fe1e34e53f6dd1/tool_sandbox/scenarios/insufficient_information_scenarios.py)：相应 insufficient_information 变体，缺少当前时间时不应编造查询时间戳。
- 仓库使用 Apple 自定义许可；本项目只借鉴任务结构，不分发其实现或数据集。

### τ-bench 仓库（当前 README 为 τ³-bench）

- commit：`b7ea9074c1cba482b30687fecdb5c8425fd6f619`，通过官方 GitHub API 固定。
- [零售政策](https://github.com/sierra-research/tau2-bench/blob/b7ea9074c1cba482b30687fecdb5c8425fd6f619/data/tau2/domains/retail/policy.md) 与 [任务数据](https://github.com/sierra-research/tau2-bench/blob/b7ea9074c1cba482b30687fecdb5c8425fd6f619/data/tau2/domains/retail/tasks.json)。仓库 MIT。
- 已阅读身份验证、写入前列出动作并获得确认、仅可取消 pending 订单、取消原因限制等规则，以及含工具动作判定的换货任务样例。
- 本地改编为单订单取消模拟器，业务规则由工具在写入事务中再检查；不宣称复现完整零售对话。

### Berkeley Function Calling Leaderboard（BFCL）

- commit：`6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`。
- [官方分类](https://github.com/ShishirPatil/gorilla/blob/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard/TEST_CATEGORIES.md)；[多轮任务说明](https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html)。
- 参考 parallel、multiple、irrelevance、multi_turn_miss_func、multi_turn_miss_param、multi_turn_long_context、memory 等任务分类。
- 我们测试的是执行引擎的并行汇合、错误拒绝和上下文完整性。模型选择函数的准确率必须另外用真实模型评测。

### AgentDojo

- commit：`089ed468cf3ed0322acc66b0211f26d9d90dbf60`，仓库 MIT。
- [旅行用户任务](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/src/agentdojo/default_suites/v1/travel/user_tasks.py)：查询评价/价格后按条件操作，utility 检查前后状态变化。
- [项目说明](https://github.com/ethz-spylab/agentdojo/blob/089ed468cf3ed0322acc66b0211f26d9d90dbf60/README.md)：外部工具内容中的提示注入。
- 本地使用虚构酒店评论夹带越权指令，并让确定性对抗模型真的提出违规调用，验证框架拒绝权限扩张。**这不证明真实 LLM 不受提示注入影响**；已授权工具的恶意参数还需要更细的领域规则和确认。

## 场景与可执行判定

### S1 — 已知时间查询提醒

流程：可信时钟 → 带时区查询 → 过滤过去事项 → 保存带来源的 JSON 产物。

入口：`examples/scenarios/reminder_known.json`。检查真实工具入参、返回事项、来源及产物记录。

### S2 — 缺少信息、重启、继续

流程：子流程 input 节点 → 父任务 waiting_input → 停止并重新构造 Hub → 提交缺字段/无效日期/无时区日期 → 拒绝 → 有效答复 → 查询并完成。

入口：`reminder_missing.json`。答复前查询调用数为零；答复 schema 校验失败保持等待；重复答复和取消后的答复报 409；最终仅执行一次查询。Python/JS/Rust 的 respond 接口都走真实 HTTP。

### S3 — 订单取消与业务状态

流程：身份 → 所属订单查询 → pending 条件 → 精确参数确认 → 原子取消 → 持久化回执 → 产物。

入口：`order_cancel.json`。五种变体：允许、拒绝、已发货、等待确认时发货、身份不符。确认前 receipt 表为空；成功后恰好一条记录；重复请求复用同一 run；其他变体不能改变订单。等待期间业务状态变化，工具必须重新检查并停止。

### S4 — 独立工具并行与依赖汇合

流程：交通报价 ∥ 酒店报价 → 合计。

入口：`parallel_quotes.json`。检测同时活跃的 handler 数为 2，合计使用两个实际输出；缺必填参数不得进入 handler，未注册函数提交即被拒绝。

### S5 — 外部内容诱导越权

流程：查询酒店评论 → 工具返回伪造 SYSTEM 指令 → 对抗模型请求未授权发送工具 → 引擎拒绝。

入口：`untrusted_reviews.json`。正常读取有成功 invocation；越权工具连 invocation 都不得创建，handler 不得执行。这个 demo 预期 run 为 failed，属于边界断言通过，不是掩盖失败。

### S6 — 长上下文和记忆来源

流程：带来源的记忆 → 五轮较长工具结果 → 上下文压缩 → 输出。

入口：`test_task_scenarios.py`。每次模型请求保留原始任务、记忆来源；assistant 工具 id 和 tool 结果 id 成对出现；事件中出现 context.compacted；最终调用数量与结果吻合。

### S7 — 生活应用全流程

四种模板分别草稿 → 确认 → 按依赖办结 → 重启检查；另有搬家改期、等待对象、阻塞说明、过期版本冲突、费用、资料与提醒、原文/模型提取、未知日期、错误日期与虚构来源的事务回滚。入口：`test_life_assistant.py`，共 9 个参数化案例。

## 运行与解释结果

```sh
uv run --extra app --extra dev pytest -q tests/integration/test_task_scenarios.py tests/integration/test_life_assistant.py
uv run --extra app python -m examples.demos.scenarios
```

ScenarioWorld 使用独立 SQLite 模拟业务库，数据全部 synthetic；不会访问商家或执行真实预约、支付和发信。JSON 工作流需要由 `world.register(hub)` 注册测试工具；普通工作室默认不暴露这些夹具工具。

框架全套另外覆盖供应商 HTTP 协议、三语言插件、MCP、真实进程强制终止恢复、竞争 worker、子运行总预算、策略快照/评估/回滚等。详见 [验证记录](VALIDATION.md)。测试均为集成测试；模型夹具用于确定性刺激执行边界，不用于模型排名。

后续接入真实模型时：固定模型 ID/参数/提示词版本、重复执行同一任务集，分别记录任务成功率、误操作、人工介入、费用和耗时。不能把本地通过率写成 BFCL、τ-bench 或 AgentDojo 官方分数。

## 搜索与服务连接场景

根据 [TinyFish 官方参考](https://docs.tinyfish.ai/search-api/reference) 独立构造本地 HTTP 夹具：地域/论文过滤搜索→来源数据映射；过滤冲突禁止发请求；429 后恢复；401/超时/超限/坏响应失败；OpenAPI 导入查询→确认→写入回执。见 `tests/integration/test_search_and_apis.py`，这些是适配器与运行时集成测试，不是搜索相关性 benchmark。
