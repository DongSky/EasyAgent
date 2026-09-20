# 模型名称与 API 协议兼容

更新：2026-09-20。以账户返回的原始模型 ID 和公开接口定义为依据，不按模型名猜测 API。目录名称代表服务方的声明，不代表本项目独立认证模型身份或上游能力。

当前快照：343 个模型，311 个文档操作，运行时适配 4 个路径参数名称变体，共 315 个操作。321 个模型可以映射到声明的协议；22 个模型缺少协议元数据，明确显示待补充，不提供假接入。192 个对话模型可选用现有标准化文字适配器，其余通过原生接口节点调用。

## 接口类型

- **Chat Completions**：`POST /v1/chat/completions`，messages、工具调用、结构化输出和模型专有参数。服务目录中的 GPT、DeepSeek、Qwen 等模型按实际声明映射。
- **Responses**：`POST /v1/responses`，input、function_call/function_call_output、reasoning。模型同时声明 Responses 和 Chat 时，默认优先 Responses。
- **Messages**：`POST /v1/messages`，system、messages、工具块；标准化适配器提供相应鉴权与版本头。
- **Gemini generateContent / 其他原生多模态协议**：contents、parts、generationConfig，适用于目录中声明这些端点的文字、图片、语音模型；保留模型名和原生 JSON 字段。
- **图像生成/编辑**：`/v1/images/generations`、`/v1/images/edits`，JSON 或 multipart；目录包含 gpt-image、Gemini Image、Qwen Image、Seedream、Grok Image，以及专用图像操作。
- **语音合成/识别/翻译**：speech、transcriptions、translations 和各模型原生语音协议；例如 tts-1、gpt-4o-transcribe、Gemini TTS、speech 系列。multipart 文件字段引用 Hub 产物 ID，不读取任意宿主路径。
- **视频、音乐及其他异步任务**：提交 → 查询状态 → 结果/取消，目录有 Kling、Vidu、Veo、Sora、Seedance、Wan、Runway、Suno 等接口系列。各系列保留自己的路径、参数和状态含义，没有强行套用统一文本接口。
- **Embedding / Rerank**：原生数组和评分结果保留；例如 text-embedding、Gemini embedding、BGE/Qwen rerank。是否可作为现有知识库 embedding 适配器须按相应返回协议确定。

具体模型、方法、路径和参数定义以 `src/easyagent/data/model_protocols.json` 和 Studio 目录为准。文档中的 `*path` 通配路由不能直接作为可执行节点，必须选择具体操作。

## 接入流程

工作室右上角“模型与接口目录”可搜索模型/协议、按类型过滤、同步当前账户。连接使用服务端 `OPENAI_BASE_URL`、`OPENAI_API_KEY`，也可指定其他环境变量名称。可连接标准化文字模型，或将某个原生操作加入节点库和画布。同步与添加节点不触发媒体生成。

模型目录连接信息和公开列表持久化；实际标准化模型绑定目前保留在本次服务进程，重启后重新连接，或通过项目配置注册。原生 API 节点和环境凭证引用按版本持久化。

原生请求保留模型专有字段；文档 Schema 帮助填写，不能替代供应商完整业务约束。模型枚举不锁死在文档示例中。固定服务鉴权从连接读取，不把文档里的 `key` 等鉴权参数作为工作流输入要求。

管理端接口为 `/v1/studio/model-catalog`、`/discover`、`/operations`、`/connect`、`/nodes`。创建节点可传 `operation_id`、`model`、`defaults`，以及 GET 状态查询的 `polling` 策略。轮询必须按该接口真实的状态字段和成功/失败枚举配置。

## 结果、可靠性与限制

原生节点返回 `{result, artifacts, inline_result}`；base64 图像和 Gemini inlineData 自动保存为产物，二进制音频/图片/视频也保存。较大 JSON 以 `result_artifact` 返回。界面支持下载，以及 PNG/JPEG/WebP/GIF、MP4/WebM、MP3/WAV/Ogg 的受认证预览；媒体回执默认折叠。临时远端链接保留在 result，当前不会自动下载所有 CDN 内容。

`POST /v1/artifacts/upload?name=...` 接收原始文件字节，沿用认证、同源检查，实际请求体上限为 **50 MB**；普通 JSON 请求仍为 **2 MB**。对话附件支持该上传上限；画布参考文件控件仍采用较小的 **2 MB** 默认值。产物存储单文件最多 **50 MB**，multipart 文件总量和单次媒体响应仍最多 **10 MB**，不同处理接口需各自满足限制。PCM 保留原始 MIME 与字节，不自动封装成 WAV。原生 HTTP 媒体节点的 SSE 可保存为回执文件；标准化 Chat Completions、Responses、Messages 模型已支持文本增量并写入运行事件。扩展 provider 可使用游标式流协议。实时双向语音和通用 WebSocket 交互尚未实现。

JSON 媒体接口可配置 `artifact_url_parameters`（例如 `content.*.image_url.url`），在这些字段填写产物 ID。运行时将已保存的媒体转为 data URL，仅发送时展开；工作流、审批和导出保留小型文件引用。普通 URL/data URL 不变。`/api/v3/contents/generations/tasks` 已自动声明图片、视频和音频路径。导出的产物 ID 仍属于原 Hub，跨项目使用须重新上传并替换引用。

模型专用 `oneOf`/`anyOf` 分支在提取 multipart 字段前匹配，避免把文件 ID 当文本发送。再次添加同一接口时，修改过的默认参数会发布新的组件版本；已有固定版本不变。HTTP 失败记录状态码及有界、脱敏的 JSON 错误摘要，不记录整份错误响应或凭证；非幂等写调用失败仍不自动重试。

异步提交与状态轮询分开。提交使用写操作审批及不确定结果核验；GET 等待带持久次数和截止时间预算，不占住 worker，不重复消耗逻辑调用次数。停止本地等待不会自动取消已付费的远端任务，需显式取消接口。

## 验证范围

本地真实 HTTP 夹具遍历 315 个操作的 URL、方法、路径参数、鉴权、JSON/multipart 和媒体回执，另测试三种文字协议、模型 ID/专有参数保留、运行时审批和持久轮询。这是协议传输测试，不是 343 个真实模型的生成测试，也不保证每个供应商文档的业务字段均正确。

真实账户目录已同步；通用 Jev 分类和干净项目复用通过。已有 GPT Responses 真实调用证据沿用前序记录。尚未为全部图片、音视频模型支付生成费用并验证输出质量。22 个缺少协议信息的模型继续明确保留为未接入。

2026-09-20 多媒体实测：`gpt-image-2.5-flare` 参考图编辑成功，得到 1024×1024 PNG；`doubao-seedance-2-5-260628` 在改用上传后 HTTPS 图片地址、`ratio=adaptive` 并处理 `pending` 状态后，成功生成约 4 秒的 720×720 参考图动画。当前服务的 PNG/JPEG data URL 请求返回 503，且 `generate_audio=false` 未阻止输出音轨；不要将这些服务差异当成官方协议限制。图像接口的 `format` 字段也曾被真实服务拒绝。详细请求、来源、修复和边界见 [真实多媒体验收](MEDIA_ACCEPTANCE.md)。

## 文档快照维护

`scripts/build_model_catalog.py` 从已下载的 `models.json`、`pricing.json` 和 `spec-*.json` 重建精简公开快照，解析本地引用，保留字段名、MIME、方法和路径，剔除密钥参数、营销描述与例子。`--docs-base` 指定文档来源前缀。YAML 解析保留 `16:9` 等字符串。

`/discover` 更新账户模型与声明的端点；完整参数 Schema 快照更新需要重新运行构建脚本并做协议集成测试。未收录的端点显示 `route_only`，必须补充参数资料；不能据此称为已经全面适配。

直连模板来源：[TypeSafe](https://docs.typesafe.ai/api)、[Runway](https://docs.dev.runwayml.com/api/)、[ElevenLabs](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)、[Gemini 图片](https://ai.google.dev/gemini-api/docs/image-generation)、[Gemini 语音](https://ai.google.dev/gemini-api/docs/speech-generation)。当前官方 Gemini 指南中的其他接口形态与目录的 generateContent 分别处理。OpenAI 官方文档本次访问受阻，未将未读取页面内容作为新功能的验证依据。
