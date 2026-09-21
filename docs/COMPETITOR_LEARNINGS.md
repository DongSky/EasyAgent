# Pi / Hermes / Codex / LoopX 对照与落地

2026-09-21。本文分四部分：系统提示词、上下文压缩、多智能体编排、工作流与循环优化。每条先给对方的实际做法（**出处到文件**），再给"我们这条做了什么"。标注了哪些是直接借用、哪些是既有设计的印证、以及**没有采纳**的项及其原因。

来源两类：本地已归档的竞品材料（`.eah/audits/2026-09-20/`，pi / hermes / loopx 的官方文档与 diff），以及本次直接抓取的源码（`raw.githubusercontent.com`，分支 `main`，2026-09-21）。抓取不到正文的只列路径与存在性，不编造措辞。

---

## 1. 系统提示词

### 1.1 对方的做法

**pi：transcript 拥有系统提示词，可增量补丁。** `packages/agent/README.md` 原文："The transcript owns the system prompt and tool declarations: the leading system message is the prompt, later system messages patch it... before every request the loop diffs it against the tools the transcript declares and, if they differ, announces the change in a system message"。`coding-agent/docs/extensions.md` 明确缓存代价："Prefer changing `sections`, `selectedTools`, or `promptGuidelines`: Pi diffs the resulting prompt sections against what the model already has and appends one system message patching only the changed sections"，整体替换则 "a cache miss when it changes"。同一文档给出**guidelines 的写法纪律**："promptGuidelines bullets are appended flat to the Guidelines section with no tool name prefix. Each guideline must name the tool it refers to — avoid 'Use this tool when...' because the LLM cannot tell which 'this' means. Write 'Use my_tool when...' instead."

**pi：技能以索引形式注入，并给出路径解析基准。** `packages/agent/src/harness/system-prompt.ts` 渲染 XML 块，并显式说明 "When a skill file references a relative path, resolve it against the skill directory... and use that absolute path in tool commands."

**hermes：三层有序 tier + 缓存前缀意图。** `website/docs/developer-guide/prompt-assembly.md`：`stable`（identity/tool guidance）→ `context`（项目 context 文件、operator 指令、平台提示）→ `volatile`（技能索引、记忆快照、时间戳、cwd）。排序理由原文："Inside the context tier the shared project files come before anything naming the current worktree. Sessions of one project running in different git worktrees then share a prompt prefix covering the whole context block... that prefix is what a longest-prefix provider cache reuses." 只存在于 API 调用期的层（`pre_llm_call` 插件上下文）刻意**追加到当轮 user message 而不是写进 cached system prompt**。

**hermes：提示词纪律被工程化。** `agent/prompt_builder.py` 的 diff 把 scratch dir 写进提示词行，理由原文："The model reaches for the system temp dir by reflex (tmpfs on most Linux hosts, fills RAM); naming Hermes' scratch dir here is what makes the TMPDIR export a habit rather than a hidden default." 同批提交含 `ci: forbid literal /tmp paths outside a burn-down baseline` —— 提示词文本被 CI 约束。

**codex：按模型分文件 + 程序化拼装 + 具体长度约束。** `codex-rs/protocol/src/prompts/base_instructions/default.md` 含 `# How you work` / `## Personality` / `## Planning` / `## Task execution` 等节；前导消息被要求 "no more than 1-2 sentences... (**8–12 words for quick updates**)"；计划质量用 high/low 两组反例示范。多 agent 指令由 `codex-rs/prompts/src/multi_agent_instructions.rs` 以 `ContextualUserFragment`（role `"developer"`，marker `<multi_agent_role>`）拼装，并逐字写明共享环境："All agents share the same directory... edits made by one agent are immediately visible to all other agents."

### 1.2 我们做的

**（已实现）技能索引移到尾部。** `runtime.py` 的 `prepare()` 里，`skill_access` 生成的 "Available skills" 段原本追加在指令中部；技能目录每有人新建技能就会变化，因此改到最末尾（`Available skills (read the full file with skills.read when a task matches)`），与 hermes 的 volatile tier 同理。

**（已实现）Operator 提示词分层。** `autonomy.py` 的 `instructions()` 现在把易变内容放在最后：角色、原则、环境、工具指南、记忆/技能说明保持字节稳定，`Saved workflows` 目录（每保存一条流程就变）压到最后一段。代码注释写明意图："Volatile tier last... A provider that reuses the longest matching prefix then keeps the expensive part cached."

**（已实现）输出纪律。** 新增一条原则："Tool output belongs in files and artifacts, not in your reply. When a command or page returns a lot of text, summarise the finding you need and keep the raw output in the workspace or an artifact." —— 借自 pi 的工具输出硬截断（`extensions.md`："The built-in limit is 50KB (~10k tokens) and 2000 lines"）与 hermes 的临时目录纪律，用提示词层表达我们已有的 artifact 机制。

**（已实现）子 agent 提示词写明共享环境与不可再派发。** `delegation.py` 的默认子指令现在包含："You share one machine, workspace and files with the agent that delegated to you and with any sibling sub-agents, so never revert or delete work you did not create... That agent receives only your final answer, not your transcript, so make it complete and self-contained. Do not delegate further." 对应 codex `experimental_prompt.md` 的 "you must tell them that they are not alone in the environment" 与 "you must tell this agent that it can't spawn another agent himself (to prevent infinite recursion)"。

**（未采纳）** pi 的 transcript 级提示词补丁需要 provider 侧支持会话中途 system message，我们的 `HTTPProvider` 三个方言都按首条 system 组装，改动面过大且收益只在缓存；guidelines 前缀改写（"Use my_tool..."）我们本来就在工具描述里写清工具名，未单独建段。

---

## 2. 上下文压缩

### 2.1 对方的做法

**pi：阈值 + 溢出 + 手动三触发，检查点式折叠。** `coding-agent/docs/compaction.md` 给出常量：`reserveTokens = 16384`、`keepRecentTokens = 20000`，触发条件 `contextTokens > contextWindow - reserveTokens`；检查时机 "after tools finish and their results are appended, before starting the next assistant response"。切割规则 "Never cut at tool results (they must stay with their tool call)"；单个 turn 超过保留量时 `isSplitTurn = true`，生成 History + Turn prefix **两份摘要再合并**。摘要模板为 8 段 Markdown（Goal / Constraints & Preferences / Progress / Key Decisions / Next Steps / Critical Context + `<read-files>`/`<modified-files>`）。序列化时 "Tool result 在序列化时截断到 2000 字符"。事件三件套 `session_before_compact` / `session_compact` / `session_compact_failed`，`reason` 为 `"manual" | "threshold" | "overflow"`。

**hermes：双阈值、结构化保留、失败阶梯、micro-compaction。** `context-compression-and-caching.md` 给出 gateway 卫生阈值 **85%** 与 agent 压缩阈值 **默认 50%**（"Setting it at 50% (same as the agent) caused premature compression on every turn in long gateway sessions"），`compression.protect_last_n` 默认 **20**，"tool call/result 消息对保持在一起，永不拆分"。失败冷却阶梯 **60s → 300s → 900s**，超时与 stall 共用一个计数器，`finish_reason=length` 的截断走独立计数器但同一阶梯；JSON-decode/closed-stream/empty-content 保持**扁平 30s**。**Provider 过载则中止并保留 transcript**："aborting compression. %d message(s) preserved unchanged; the session was NOT rotated." `micro-compaction`（默认关）每轮只吸收**一个 exchange**，且**永不压缩用户消息**，理由原文："Your instructions are a different kind of thing. They're the intent everything else is derived from... Paraphrasing 'use the existing retry helper, don't add a new one' into a summary is exactly how an agent ends up confidently doing the thing you told it not to, six turns later." 可插拔 `ContextEngine` 契约要求 **fail-open**："A missing hook, an exception, or an invalid return value leaves the request untouched — so a failing engine is never worse than not installing one."

**codex：摘要式与"直接换新窗口"两种模式。** `codex-rs/prompts/templates/compact/prompt.md` 全文即四要点；`summary_prefix.md` 是恢复方看到的前缀（"Another language model started to solve this problem and produced a summary..."）。另一条 `compact_token_budget.rs` 的 doc comment："Token-budget compaction **skips model/server summarization and installs a fresh context window instead**"，并把窗口身份（`first_window_id` / `previous_window_id` / `window_id`）作为 developer message 注入，让模型自己知道上下文被换过。

**loopx：压缩的是控制平面信封，不是对话。** `turn-envelope-v0` 有 8 KiB 性能目标，按 section 分配字节（action 800 / boundary 2000 / ...），超预算只报 `warning.code=turn_envelope_budget_exceeded` 并保持路由正常，"packet growth alone no longer produces `contract_error` or stops a Turn loop"；同时明令禁止："**Never trim write scope, executable arguments, signatures or required reads to silence a warning, and do not simply raise the target.**"

### 2.2 我们做的

**（已修真实缺陷）压缩曾可能删掉任务本身。** 原实现有会话历史时 `prefix_count` 只保护 1 条（system），本轮用户请求落在可删除区，且删除循环末尾会吃掉最后一条消息。现在按**身份**而不是下标保护：`_pin_request` 把每条 user 指令与其编码值记入 `pinned_requests`，`_protected` 用编码比对定位（修掉了一个"存的是已编码字符串、读取时又编码一次"的双重编码 bug，该 bug 使保护完全失效），`_prefix_end` 只固定连续头部，`_free` 保证不删被保护消息、不动当前轮。检索注入的 "Reference data" 同样被 pin——它已经付过检索成本且任务依赖它。对应 hermes 的 "永不压缩用户指令"。

**（已实现）摘要模板与截断。** 新增 `COMPACTION_INSTRUCTIONS`（沿用 pi 的分节结构，明确"Keep every user instruction and constraint verbatim in meaning"）与 `SUMMARY_PREFIX`；`_for_summary` 在送入摘要器前把**单条 tool 结果截断到 2000 字符**并附截断标记，理由与 pi 相同：一条命令输出会挤掉摘要本该保留的指令。

**（已实现）fail-open。** `backends.call("context", ...)` 现在被包在 try 里，失败或返回不可用结果时记 `context.compaction_failed` 事件并**落入纯删除路径**，而不是让整轮失败。对应 hermes 的 "a failing engine is never worse than not installing one"。前端活动流新增该事件标签。

**（已实现）会话历史中不会被丢掉的任务提示。** 走会话历史的 run 若自带 `prompt` 而历史末尾不是它，现在会追加进去——原先该请求可能完全不进上下文。

**（已实现）失败冷却阶梯。** 摘要器失败后进入 60s → 300s → 900s 递增冷却（每级只在再次失败时升级，成功即清零），避免 fail-open 退化成"每个回合重拨同一个坏摘要器"。对应 hermes 的 `_TIMEOUT_COOLDOWN_LADDER`。失败事件带 `retry_after_seconds` 与 `attempt`。

**（未采纳）** pi 的 `reserveTokens/keepRecentTokens` 双旋钮我们已有等价物：`model_limits.discover()` 读服务端声明的窗口，`compact_for_model` 按 `available*0.8` 折算字符并保留 25% 给输出。hermes 的 micro-compaction 需要每轮一次额外摘要调用，与"压缩失败要能中止"的我们当前实现相比成本更高，暂不引入；codex 的"直接换新窗口"依赖服务端支持，我们不做。

---

## 3. 多智能体编排

### 3.1 对方的做法

**pi：不内置 subagent，官方以扩展给出完整实现。** `coding-agent/examples/extensions/subagent/`：每个 subagent 是**独立 pi 子进程**；agent 定义为带 YAML frontmatter 的 markdown；项目级 `.pi/agents/*.md` **默认不加载**（"repo-controlled prompts that can instruct the model to read files, run bash commands"）；并行模式**最多 8 个任务、4 个并发**；"Parallel model-visible output is capped at 50 KB per task; full results remain in tool details"；内置 `scout` 的 prompt 面向"没读过文件的下游 agent"："Your output will be passed to an agent who has NOT seen the files you explored."

**hermes：三层 API + 明确隔离边界。** `delegate_task` 默认 **10 并发**（"configurable, no hard ceiling"）、`MAX_DEPTH = 1`（扁平，深层要显式开）、`child_timeout_seconds` 是**无进度**上限而非墙钟上限、`subagent_auto_approve` 默认 False（审批回调默认返回 "deny" 让子看到可恢复的拒绝）。隔离模型原文："Subagents start with a **completely fresh conversation**. They have zero knowledge of the parent's conversation history, prior tool calls, or anything discussed before delegation." 结构化输出：每个 task 可带 JSON Schema，校验失败父发**恰好一次**有界纠正轮；"重试后仍不匹配不丢弃 child 的工作"，结果保留 `schema_valid: false` 与 `schema_errors`。`subagent_lifecycle.py` 的常量逐字：`_MAX_RESULT_CHARS = 32_000`、"Terminal results are immutable, idempotent, bounded to 32k characters, **omit transcripts and hidden reasoning**, and include a stable result hash"；`cancel` 是**协作式**的（返回 `CANCEL_REQUESTED`，不声称完成）。请求 fail-closed："goal/context/metadata sizes are capped, unknown or parent-broadening toolsets are rejected"。

**codex：共享目录 + 显式并发槽声明。** `multi_agent_instructions.rs` 把 "All agents share the same directory... edits made by one agent are immediately visible to all other agents" 写进 developer 消息，并声明 "There are {max_concurrency} available concurrency slots, meaning that up to {max_concurrency} agents can be active at once, including you."；orchestrator 模板给出角色纪律："**your only role becomes to coordinate them. Do not perform the actual work while they are working**" 与 "wait for them before yielding"。

**loopx：不执行 agent，只记录拓扑。** `subagent_execution_topology.py` 有 `MAX_CHILD_EXECUTION_RECEIPTS = 32`、`CHILD_CONTEXT_MODES = {fresh, forked_snapshot, resume}` 与其继承映射、`CHILD_FALLBACK_ACTIONS = (retry_fresh, replace_child, serial_takeover, ignore_optional_result)`，并且 `raw_transcript_copied` 是必填字段**默认应为 false**。

### 3.2 我们做的

**（已实现）有界、带 hash、不含 transcript 的结果。** `delegation.result()` 现在返回 `{id, status, output, error, usage, result_hash}`，其中 `output.text` 截断到 **32000 字符**并附截断标记；失败子任务的 `data` 置空。嵌套 `output` 结构保留，因为已保存的工作流与测试按 `output.text` 读取——这是公共契约，不做破坏性变更。对应 hermes 的 32k 上限与稳定 hash。

**（已实现）子 agent 提示词约定共享环境、不得再派发**（见 1.2）。

**（既有设计得到印证，未改）** 我们的父子预算共享（`store.reserve` 逐级上溯）、`max_children` / `max_depth` 收窄继承、`spawn` 幂等槽位、合并写回执的 `reuse_writes`，与 hermes 的 fail-closed 继承和 loopx 的"子结果不可变"方向一致。`agents.wait` 的 `WaitingChildren` 让出 worker 的做法，等价于 loopx 强调的"子任务生命周期不占用父的执行权"。

**（已实现）可选结果契约。** `agents.spawn` 接受 `response_schema`：子任务按该 schema 返回结构化结果；违反时父侧触发**恰好一轮**有界纠正（把子任务自己的被拒答案放回上下文，只修格式、不重做），仍不符则**不丢弃已完成的工作**——结果保留原文，并带 `schema_valid: false`、`schema_errors` 与 `schema_note` 标记为未验证。对应 hermes："重试后仍不匹配不丢弃 child 的工作"。契约在 `spawn` 当场校验：模型不支持 `decision` 能力时直接拒绝，而不是让子运行内部失败（父只能干看）。

**（未采纳）** 未引入独立子进程隔离（pi 的做法），因为我们已有跨运行持久化与收窄授权，跨进程会破坏 `child_runs` 恢复；未引入 `forked_snapshot` 上下文模式，因为共享历史会让子任务继承父的隐私与 token 成本。

---

## 4. 工作流与循环优化

### 4.1 对方的做法

**loopx：八态 disposition 决策表，纯函数、不授权任何副作用。** `turn-loop-controller-v0.md` 定义 `run_now / capability_action_required / wait / stop / user_action_required / repair / replan / terminal`，并强调 "There is no `contract_error` disposition: contract failures are rejected at the typed-input boundary"，每个 payload 带 `spends_quota=false, launches_host=false, writes_state=false`。两条硬规则：**"validated_completion proves a Todo transition, not Goal closure"**（只有 durable `no_followup` + 新的 terminal frontier 才是 `terminal`），以及 **预算耗尽导向 `replan` 而不是 `terminal`**："Budget exhaustion routes to `replan`, **not `terminal`, because a bounded Turn chain ending is not evidence that the Goal ended.**" 重试不得换模型："A typed retryable Host failure never authorizes a different model or a new Todo... `model_fallback_allowed=false`." Replan 的三条边界：`requires_bounded_delta=true`、`fresh_envelope_required=true`、`stale_todo_rerun_allowed=false`。

**hermes：`/loop` 自适应节奏 + 由 judge 判定停止。** `loops.md`：固定间隔或**自发节奏**（地板 1 分钟，回复不变则指数退避到 15 分钟上限，一变就回地板）；`--until` 条件由 goal judge 检查，判定不可达成则**暂停并给原因**（fail-open，"坏 judge 永不卡死 loop"）；兜底预算 `max_ticks` 默认 100。

**pi：结束有三层语义。** `agent_start` → `agent_end` → `agent_settled`，"Use `agent_settled` for status integrations that need to know Pi will not continue running automatically"（因为其后还可能有自动重试、压缩重试、排队续跑）。

### 4.2 我们做的

**（已实现）失败任务也写入记忆。** `workspace_chat.tick` 在 operator 阶段失败时同样调用 `autonomy.finalize`，让下一次尝试带着上次的证据开始——对应 loopx 的"失败也是可继续的证据"。

**（既有实现已符合，未改）** 我们的 `retry_run` 已经是**同一模型重试**（不换模型、不换步骤），`continue_automatically` 只在可重生成媒体或有回执时才允许重发；`GoalController` 的 `max_revisions` 耗尽返回 `"incomplete"` 而非伪造成功，与 loopx "预算耗尽不是 Goal 结束"一致；编译器的停滞检测（同一草稿对同一错误重复两轮即停止）等价于 loopx 的 two-stall 规则。

**（未采纳）** loopx 的八态决策表需要把"回合"建模成独立的可签发凭据体系，我们以轮次 + 事件流实现同等约束，引入整张表会与现有 `Phase` 词表重复。`/loop` 的自适应节奏属产品未定的新功能，不在本次范围。

---

## 落地清单（本次实际改动）

| 文件 | 改动 |
| --- | --- |
| `src/easyagent/runtime.py` | 压缩按身份保护用户指令与检索数据（修双重编码与删任务两个真实缺陷）；`COMPACTION_INSTRUCTIONS` / `SUMMARY_PREFIX` / `_for_summary`（工具结果 2000 字符截断）；摘要失败 fail-open 并记 `context.compaction_failed`；会话历史中补回 run 自带 prompt；技能索引移到指令尾部 |
| `src/easyagent/autonomy.py` | 提示词分层（易变内容置尾）；新增"工具输出写入文件与产物"纪律 |
| `src/easyagent/delegation.py` | 子 agent 结果 32k 上限 + `result_hash`；子指令写明共享环境与不得再派发 |
| `apps/agent/.../workspace-chat.js` | 活动流新增 `context.compaction_failed` 标签 |
| `tests/integration/{test_backends,test_learning_delegation,test_autonomy}.py` | 五个新用例：压缩后端失败降级、失败摘要器退避、子结果有界且带 hash、子结果契约（纠正一轮 / 保留未验证工作 / 不可满足即拒）、提示词稳定段先于易变段 |

## 第二轮：结果契约与失败退避（2026-09-21）

在两处已识别但首轮未做的项上继续，理由都是"不补则会留下首轮改动带来的副作用"：

1. **子 agent 结果契约**（hermes 的有界纠正轮）：见 3.2。
2. **压缩失败冷却**（hermes 的 60/300/900 阶梯）：首轮让摘要器失败降级为纯删除（fail-open），代价是**每个后续回合都会重试同一个坏摘要器**。现在失败后进入递增冷却，成功清零。

## 验证

- 改动前基线：`281 passed`（`.eah/competitor-baseline.log`）。
- 改动后：见 `.eah/competitor-final.log`。
- 新增用例分别验证：摘要器抛错时任务仍成功且指令保留；子 agent 超长输出被截断且返回稳定 hash；Operator 提示词中 `Saved workflows` 位于 `Toolkit guide` 之后、技能索引位于基础指令之后。

## 未采纳项与理由

1. **pi 的 transcript 级提示词补丁**：需要会话中途 system message，三个 HTTP 方言都不支持按此组装。
2. **hermes 的 micro-compaction**：每轮一次额外摘要调用，成本与 cache 前缀破坏频率都更高；我们的压缩是确定性删除 + 可选摘要。
3. **codex 的"直接换新窗口"**：依赖服务端 token-budget 压缩能力，我们不做供应商特例。
4. **pi 的子进程隔离**：会破坏 `child_runs` 的跨重启恢复。
5. **loopx 的八态决策表与凭据体系**：与现有 `Phase` 词表重复，收益不足。
6. **独立子 agent 并发池**：子任务与父共享 worker 池并让出执行权，加池会引入第二套调度真相。

以上各项如后续要推进，应作为独立议题立项，而不是塞进本轮。
