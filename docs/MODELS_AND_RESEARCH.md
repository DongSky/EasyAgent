# 模型、范式与研究依据

核对日期：2026-09-19。接口以部署时供应商文档和账户能力为准；不预设某个尚未验证的模型可用，也不保证特定产品名称或价格。

## 模型接入策略

- OpenAI：Responses adapter；Chat Completions compatible adapter；Images/Embeddings 分别适配相应接口。
- Anthropic：Messages adapter；统一转换 tool_use / tool_result，不向外泄漏供应商格式。
- Qwen / DeepSeek / 本地 Ollama / vLLM / LM Studio：可用其 OpenAI-compatible endpoint，实际支持的工具和结构化输出须通过 live contract 验证。
- 图像：独立 image capability，支持供应商返回 URL 或 base64；不假装文本模型能生图。
- 决策：可注册小模型、规则模型或分类器，统一输出 JSON Schema。TypeSafe Jev 的 state/questions/answers 协议已按[官方文档](https://docs.typesafe.ai/api)以通用 HTTP 工具接入，并真实验证 choice 决策；示例见 `examples/demos/live_workflow.py`。它的 noul/choice/score 原语不是通用文本生成或任意 JSON Schema 输出，不能直接等同于 GPT decision。
- 开发：内置确定性 mock 可在没有 key 时验证全流程；生成的演示图片会明确标记为 mock。

API key 从服务端环境变量读取，不写入数据库、前端或插件默认环境。用户输入只能选择已注册 alias，不能指定任意 URL。能力不匹配直接报错。模型版本、上下文长度、工具支持和实际 token 消耗由供应商决定；框架限制请求轮数和每次输出上限。

## 采用的架构证据

- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)：已读取。标准为 stdio 与 Streamable HTTP，后者替代旧 HTTP+SSE；使用官方 SDK 避免自行实现残缺协议。
- [Agent Skills specification](https://agentskills.io/specification)：已读取。frontmatter、目录命名、渐进加载与资源引用约定。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：已读取 durable-execution 路径重定向内容。区分运行 checkpoint 与跨运行 memory，并将恢复作为核心能力。
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)：已读取。明确 workflow 与 agent 的差别，并参考 chaining、routing、parallelization、orchestrator-workers、evaluator-optimizer。
- [A2A specification](https://a2a-protocol.org/latest/specification/)：已读取。跨 Agent 协议和 MCP 不是同一层；本版预留 API 边界，不实现其全部任务/发现/安全协议。
- [OpenAI Responses](https://platform.openai.com/docs/api-reference/responses/create) 与 [Images](https://platform.openai.com/docs/api-reference/images/create)：当前网络返回 403，无法完成官网核对。适配器以明确的请求契约和本地 HTTP 夹具验证，真实供应商兼容性列为待 live 验证；不据此声称使用了最新模型。

## 有意限制

本版本覆盖工程上可验证的主要组合范式。没有声称复现所有最新论文、训练新模型、自动微调、在线 RL、无限自治协作或自动编写并部署插件。后续研究应先在相同评估集上量化任务成功率、成本与人工介入次数，再决定是否引入更复杂架构。
