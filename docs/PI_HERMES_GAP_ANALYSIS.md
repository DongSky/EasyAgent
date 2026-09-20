# Pi / Hermes 对照与当前状态

更新：2026-09-20。本次重新读取官方仓库，固定到 Pi `d1230ea2000d876b479a69b8b061f9d670f262f5` 和 Hermes `30de041b011aa3d3830a7ffa05815e2cb2f063be`。对照对象是用户指定的 `earendil-works/pi`。本次是代码、官方文档和运行证据复核，没有安装竞品做同任务、同模型性能比赛；官方文档所述能力也不等于我们亲自验收过竞品。

**结论：扩展系统已经实现并执行验证，但不能称为与 Pi / Hermes 全面对等。** 通用工具、事件、服务或 provider 的存在，不等于覆盖对方所有专用接口、开发体验和现成生态。开发前的逐项理由保存在[历史基线](PI_HERMES_GAP_BASELINE.md)；前一版依据为 Pi `3c75b274`、Hermes `d86a1687`，以下更新了比较范围。

## 当前已实现

1. **扩展能力**：工具、命令/快捷键、生命周期事件、上下文和工具调用拦截、模型 provider、Skill/模板、状态/设置/选项、Web UI 贡献。支持带 Schema 的服务、固定依赖、常驻进程、迁移、状态 CAS、事件重放、不可变版本及热更新/回滚。采用独立协议；不是 Pi TypeScript 包的直接运行兼容层，也未覆盖 Chord 的完整服务与状态同步机制。
2. **持续会话**：独立 Conversation/Turn、持久原文、follow-up/steer、取消、fork、FTS 检索、保留来源的摘要和可选后台压缩。真实 HTTP SSE 支持 Chat/Responses/Messages 的文本与工具参数分片。
3. **学习与记忆**：任务证据生成经验策略候选，执行评估后发布/复用/回滚；记忆命名空间授权、合并去重与来源归档。后台复盘默认关闭，只处理明确声明的任务，不自动发布。
4. **新代码**：Agent 通过完整 Schema 创建 JS/WASM 源码候选，实际试跑、失败拒绝发布、审批发布后跨流程复用。Python/Node/Rust 完整进程扩展要求源码摘要信任，不自动下载依赖。前置参数错误返回 Agent 修正，权限错误与不确定写入仍中止或等待处理。
5. **动态协作**：持久子 Run 和邮箱，spawn/send/reply/status/wait/cancel/result，父子权限收窄、层级/数量限制和根预算共享。单 worker 等待不会阻塞子任务。
6. **连接与提醒**：webhook/Telegram/CalDAV、版本化配置、独立投递 outbox/回执、时区 cron 与错过调度处理；签名消息网关去重并接续会话。MCP 长连接、OAuth/PKCE/refresh、动态工具发现和健康状态。
7. **分发与运维**：源码包摘要/发布者签名、跨完整工作流分享、生成开发脚手架、macOS 独立应用、doctor/metrics/trace、加密 SQLite 备份恢复与可重复 soak 脚本。
8. **手机基础**：独立 Rust 核心已承担 DAG/数据引用/状态机/授权审批/checkpoint/outbox/不确定结果核验，Swift/Kotlin Host 接入。iOS 模拟器通过界面运行了本地离线示例。

具体规范见[扩展指南](EXTENSIONS.md)，实测见[验收记录](EXTENSION_ACCEPTANCE.md)。

## 扩展生态、在线修复与 Skills 本轮交付

2026-09-20 已完成本轮 A/B/C 实现，详见 [交付计划与验收](NEXT_DELIVERY.md)、[Skills](SKILLS.md)、[目标修复](GOALS.md)、[专用后端](BACKENDS.md)。新增 GitHub Skills 导入/版本管理、自然语言编写技巧、在线包目录、7 类后端契约、扩展流式 provider、本机浏览器/终端、3 个发送渠道及语音输入输出。目标控制器已用真实模型完成结果检查→修订→保存新版本→再次验收。

本轮最终全套集成测试 116 项通过；其中语音和消息服务用本地 HTTP 协议夹具，浏览器和终端实际运行。真实模型读取 Hermes 技巧并生成带引用草稿，零代码技巧生成/预览/安装界面通过。具体日志与最新补测见交付记录。

## 上轮核实的验证证据（历史）

- **本次重新执行**扩展、代码包、持续会话、网关与维护四组集成测试：`14 passed in 28.12s`，无跳过。覆盖真实 Python/Node/Rust 扩展进程、JS/WASM、签名/权限/超时、版本固定、重启/回滚/迁移、服务依赖、分享后导入执行、会话控制、工具参数错误反馈等。日志：`.eah/audits/2026-09-20/extension-recheck.log`。
- **上一轮全套回归**：`101 passed in 87.50s`，日志 `.eah/build/ui-integration-final.log`。本次核对日志，没有重复执行全套。
- **此前真实模型记录，本次复核**：持久会话两轮实际调用 `guide.quote@2` 得到 100 HKD、50 HKD；核对数据库调用版本、参数与输出。模型还实际完成 `code.create → code.test → code.publish`，新节点在两个独立工作流复用得到 30、0.3。证据 `.eah/live-acceptance/extensions-final/real-conversation.json` 和 `code/`。本次没有新发起付费模型调用。
- **此前界面验收**：扩展安装/升级、自动生成嵌套表单、运行和分享等关键路径已操作；未完成所有自定义 HTML 插槽交互的穷尽式浏览器验收。
- **此前短稳记录**：600 秒，5,518 次流程、55 次宿主重启、0 次失败，见 `.eah/extension-soak-final/report.json`。不能据此宣称达到 72 小时或生产长期稳定性。

这些是执行证据，不是模型质量或竞品性能基准。HTTP/OAuth/连接器集成场景中使用的本地协议夹具，不替代真实第三方账户验收。

## 与 Pi 仍有的差距

1. **扩展分发与现成生态**。Pi 支持 npm/git 安装、项目/用户范围、资源发现和更新；我们目前提供带摘要/签名的源码包导入导出、版本管理与脚手架，本轮已增加可配置的在线目录和远程 Skills 固定提交导入、检查更新；尚无运营中的公共市场、npm/git 通用扩展包解析及项目/用户多层安装范围。Pi 的包需要移植，不能直接导入运行。对普通用户而言，现成能力能否找到并顺利装好，比底层可注册工具更关键。
2. **扩展接口的深度**。Pi 有会话树切换/导航生命周期、思考级别事件、原始模型请求与响应头拦截、缓存预热决策、命令自动补全、用户 bash 等明确接口。我们已有上下文/工具拦截和会话服务，但没有逐项等价实现，不能以事件数量宣称对等。
3. **自定义模型的流式协议**。内置 HTTP 模型适配器支持 SSE，本轮扩展 provider 增加游标式分片与取消协议；尚不兼容 Pi 的原生异步生成器或完整鉴权扩展面。
4. **终端开发体验**。Pi 的编辑器、渲染器、主题、终端命令与 TUI 组件扩展更深入。我们是 Schema 表单与 Web 插槽体系，受信任进程扩展也不等于内置交互终端/PTY。面向 Web/手机用户可以采用不同体验，但不能声称 API 兼容。
5. **Chord 的跨进程组合能力**。Chord 是 Pi 单仓库中的独立包，不应全部算作 Pi CLI 已接入的功能。它定义了分环境 facet、singleton/keyed 服务、稳定服务代理、远程订阅、状态增量复制和断线重新载入。我们的服务 continuation、SQLite CAS 与事件日志没有实现这整套语义。

## 与 Hermes 仍有的差距

1. **专用后端插件契约**。最新 Hermes 官方文档已经包括渠道适配器、图像/视频生成后端、上下文压缩引擎、记忆后端、终端执行环境、审批展示通道及模型 provider 等扩展点。我们已有部分对应产品能力和通用接口，本轮已补充 7 类专用后端和固定版本选择。差距转为实际可安装的提供者数量、复杂提供者兼容性及长期运行证据；统一契约不代表 Hermes 插件可以直接运行。
2. **开箱可用的浏览器与代码环境**。Hermes 文档包含本地浏览器、已有登录状态、会话隔离和模型编写代码操作浏览器的方案。本轮已加入 Playwright 浏览器、隔离根任务会话、加密 Cookie/localStorage 恢复和有界 argv 终端。仍缺交互式 PTY、第三方依赖隔离安装以及各平台同等完整的本机执行环境。
3. **渠道和语音成品体验**。Hermes 提供更多原生消息渠道，以及语音输入、语音回复和实时语音交互。本轮增加 Slack/Discord/飞书发送协议，以及录音转写和回复合成。原生双向渠道、真实账户验证、实时全双工语音与打断仍有差距。
4. **技能与插件的发现安装**。Hermes 有插件目录、git 安装/更新、pip 分发和现成技能路径。我们的候选生成、评估、发布、复用链路已跑通，本轮已补齐 Skills 远程浏览、导入、固定提交、更新、回滚、导出和自然语言创作。通用 git/pip 安装、现成业务能力数量及未经引导的新手成功率仍不足。未做同任务评测，不能由此推断任一方的“学习效果”更强。

## 实现和验证仍需区分的边界

- **尚未实现**：手机本地完整 Agent 循环/动态代码/后台调度、自动跨设备转交、完整第三方依赖隔离安装、原生邮箱/支付、账户与多租户、分布式扩展注册表一致性。手机可以连接自行配置的 HTTPS Hub；本地 Rust 核心是执行子集。
- **有代码或基础验收，缺少目标环境证据**：Android 未产出 APK；iOS 只有模拟器离线子集与检查点验证；Windows/Linux 配置了 CI 但尚未实际执行；macOS 只有本地 ad hoc 签名，未公证发行。真实 Telegram/CalDAV 账户投递也未验收。
- **没有完成证据**：72 小时长稳、容量/SLO、真机后台恢复、5–8 位新手完整安装任务、正式发行与升级链路。不能把这些普遍的产品成熟度关卡说成竞品已经全部解决。

## 后续需要真实环境验证与产品补充的部分

1. 给目录接入更多经过实际任务验证的提供者；验证现成 Skills 对工具名称和依赖的兼容性。现有目录协议不是公共生态规模。
2. 在用户真实消息/语音账户、浏览器业务站点和手机设备上进行端到端验收；目前不得冒充已经完成。
3. 做长稳、发行签名、公证、跨平台构建以及新手完整任务测试。保留 Pi 原生接口/TUI、Chord 复制、完整依赖管理、实时语音等边界，不能只用通用扩展点把它们写成完成。

## 现在的产品价值

三种构建入口落到可检查、可执行、可迁移的同一工作流；扩展贡献进一步进入同一目录和权限/版本体系。普通用户可安装能力、填写可视表单，开发者可逐步深入代码，无需更换项目格式。把持续对话、可视工作流和可分享扩展结合起来，是明确的产品方向。未审计竞品全部第三方生态，因此不称这些能力为独有，也不以测试数量宣称性能领先。

## 本次依据（固定提交）

本地保存的官方材料和提交元数据位于 `.eah/audits/2026-09-20/`；其中的功能描述按仓库文档理解，没有冒充竞品运行测试。

- Pi：[扩展接口](https://github.com/earendil-works/pi/blob/d1230ea2000d876b479a69b8b061f9d670f262f5/packages/coding-agent/docs/extensions.md)、[包管理](https://github.com/earendil-works/pi/blob/d1230ea2000d876b479a69b8b061f9d670f262f5/packages/coding-agent/docs/packages.md)、[Chord](https://github.com/earendil-works/pi/blob/d1230ea2000d876b479a69b8b061f9d670f262f5/packages/chord/README.md)。
- Hermes：[插件接口](https://github.com/NousResearch/hermes-agent/blob/30de041b011aa3d3830a7ffa05815e2cb2f063be/website/docs/user-guide/features/plugins.md)、[插件目录](https://github.com/NousResearch/hermes-agent/blob/30de041b011aa3d3830a7ffa05815e2cb2f063be/website/docs/user-guide/features/plugin-catalog.md)、[浏览器](https://github.com/NousResearch/hermes-agent/blob/30de041b011aa3d3830a7ffa05815e2cb2f063be/website/docs/user-guide/features/browser.md)、[语音](https://github.com/NousResearch/hermes-agent/blob/30de041b011aa3d3830a7ffa05815e2cb2f063be/website/docs/user-guide/features/voice-mode.md)、[桌面扩展 SDK](https://github.com/NousResearch/hermes-agent/blob/30de041b011aa3d3830a7ffa05815e2cb2f063be/website/docs/developer-guide/desktop-plugin-sdk.md)。
- 本项目关键边界：`src/easyagent/extension_contracts.py`、`extensions.py` 中的 `ExtensionProvider`、`extension_host_services.py`、`skills.py`、`models.py`；测试为 `test_extensions.py`、`test_code_packages.py`、`test_conversations.py`、`test_gateway_maintenance.py`。

## 上一轮原始依据（历史固定提交）

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
