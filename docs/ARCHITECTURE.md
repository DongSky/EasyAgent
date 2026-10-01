# 架构

## 固定骨架 vs Agent 自由区域

用工作流保证它不变坏，用 Agent 让它能变强。

**锁死的（骨架，代码保证）：** mission 三件套（SSE + steer + checkpoint + 熔断三件套 max steps/cost/wall clock）；
插件转正四道门（fixtures → 确定性重跑 → 泄漏审查 → `plugin_judge`）；RRSI 正则化（工具调用全量记账 +
`plugin.prune` 降级；系统提示词禁止为转正削弱门禁）；`risk_gate` 永远 fail-closed。

**自由的（Agent 自己决定）：** 工具组合、探索路径（装什么、查什么、impl 怎么写）、何时 `memory.append`
留 recipe。自由不许绕开治理：见"绕路计数器"。

## 三层

整个系统是单进程 + SQLite + 文件系统，按数据流向分为三层。模块名与接口名以 SPEC 为准。

### 1. 发现层（`discovery.py`）——缺什么，现找什么

入口 `ensure_capability(need: str, ctx) -> str | None`，解决"能力缺口"（capability gap）：

1. `registry.get(need)` 命中 → 直接返回工具名。
2. `memory.search(need)` 找到某条 learning 里可落地的 recipe → `scaffold` 生成插件（仍需 `promote` 转正）。
3. 探索子任务（有界：≤10 步、≤300s、禁止付费调用）：`fetch_docs` 拉文档 → `probe` 沙盒试调 → `scaffold` 写 `plugins/inbox/<name>/` 四件套 → `promote`。
4. 失败 → 返回 `None`，并 `append_learning("explored <need>: failed because ...")` 把教训记下来。

决策"要不要探索"分两条路（见门控表）：agent 自己决定调用 `scaffold`
是它的自主探索（写 inbox，不直接可调；转正仍走门控 4 的全套 RRSI 检查），
这是架构要保留的自由度；只有**系统替 agent 做决定**的 bypass 强制路径
（`_force_scaffold` → `ensure_capability`）才走 Jev 门控 2（`gap_triage`）。

### 2. 注册表（`registry.py`）——插件的生命周期管理

`ToolRegistry` 管三件事：

- **发现** `discover(plugins_dir)`：扫描 `plugins/active/*/manifest.json` + `tool.json`，动态 import `impl.py`（约定暴露 `def run(args: dict, ctx: dict) -> dict`）。
- **热重载** `watch()`：1s 轮询 watcher 线程；manifest 变化 → importlib 重载（模块名带 version/hash 做 cache-bust）；保留最近 N=3 个版本；加载失败自动回滚上一可用版本，并在 `tool_versions` 表记 `status=rolled_back`。
- **转正** `promote(name)`：对 `plugins/inbox/<name>/fixtures.json` 做离线干跑，全部用例通过 → 移入 `active/` 并注册；失败 → 留在 inbox，原因写入 `tool_versions`（`status=rejected`）。

调用侧 `call(name, args, ctx)`：`untrusted` 工具且累计人工确认次数 < K（K=3，可配置）→ 走 `decisions.risk_gate`；P(risky) ≥ 0.5 → 抛 `ApprovalRequired`，run 进入 `awaiting_confirm`，等人工 steer 放行/取消。详见 `PLUGINS.md`。

### 3. 长循环（`loop.py` + `server.py`）——任务执行

`MissionRunner(store, registry, decisions, llm)`：

- `start(mission_id) -> run_id`：后台线程跑单链 ReAct（thought → 决策 act|finish → tool_call → observation）。骨架参考原仓库 `autonomy.py`，但**删掉了三层并发控制**（`max_children` / `max_depth` / `max_active_children`）和 reflection 子循环。
- 每步 emit `RunEvent`；每 N=5 步 `save_checkpoint`；每步检查熔断（steps / cost / wall clock），到线 → `status=done(reason=budget)` 并生成总结 artifact。
- `steer`：`queue_command(run_id, SteerCommand)`；pause 挂起、resume 继续、cancel 终止、redirect 把 message 注入下一步 context。`awaiting_confirm` 状态下 steer `action=resume` 视为人工放行。
- 结束时调 Jev 门控 3（`completion_score`），<3 分且步数有余 → 继续迭代；否则写总结。
- **绕路计数器**：mission 成功结束（`completion_score` ≥ 3，非熔断）且 `shell.exec`/`file.write`/`file.edit`
  调用 ≥ `EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD`（默认 3）次、期间没调过 `scaffold`/`plugin.promote` →
  记一次 bypass（`bypass_events` 表按目标签名计数 + learning + `bypass` 事件）。同一目标家族累计 ≥
  `EASYAGENT_BYPASS_THRESHOLD`（默认 2）次 → 强制走发现层：用录制的 shell transcript 做 recipe
  （`{"kind": "shell", "commands": [...]}`）调 `ensure_capability` 自动 scaffold 出 macro 插件，
  仍过全部转正门（含 `EASYAGENT_GATE_POLICY`）。`EASYAGENT_BYPASS_AUTO=0` 关闭。

`server.py` 是薄 HTTP 层：missions API + SSE 事件推送（`GET /v1/runs/{id}/events?after_seq=N`），`/` 挂载 `frontend/` 静态文件。前端不跑 agent 逻辑、不持有状态机。接口细节见 `API.md`。lifespan 退出时调 `registry.stop_watch()` 停掉热重载 watcher。

**认证设计**（`server.py`）：`EASYAGENT_API_KEY` 设置后，全路由显式绑定认证依赖（SSE 路由用 `require_key_sse`）：通用 API 只认 `Authorization: Bearer`（常量时间比较，`?key=` 后门已删）；浏览器 EventSource 拿不到 header，走一次性 token（`POST /v1/sse-tokens` 用 Bearer 换取，60s 过期、单次使用），前端 `connectSSE()` 自动换 token、断线重连时重新换，长效 key 永不出现在 URL 里。

**checkpoint 续跑**：`load_latest_checkpoint()` 不再是死代码——`MissionRunner.resume_from_checkpoint(run_id)` / `POST /v1/runs/{id}/resume` 从最新 checkpoint 恢复 `history`/`step`/`cost`/`mission_id`，开新 run 继续（旧 run 不篡改，发 `resumed`/`resumed_from` 事件）。checkpoint 存档时也写入 store（事件可回放）。

**完成通知**：`EASYAGENT_NOTIFY_WEBHOOK` 设置后，`_finish()` 走 `notify_run_finished_async`（daemon 线程），DNS/TLS/重试永不阻塞任务完成；loopback 目标直连（不走环境代理），公网保留代理行为。

**prompt 工具裁剪**（`EASYAGENT_MAX_PROMPT_TOOLS`，默认 48）：超限时按自愈核心（scaffold/plugin.promote/plugin.merge/plugin.prune/memory.*）→ 其他 builtin → 按调用量排序裁，裁掉的发 `tools_trimmed` 事件；每次 `_finish()` 还会出 `prune_candidates` 事件（默认 dry-run，`EASYAGENT_AUTO_PRUNE=1` 才真降级）。

记忆（`memory.py`）是横切支撑：`learnings.jsonl` 追加写，CJK bigram 词法检索（不上向量），进程启动时最近 20 条注入 llm system prompt。

## 删了什么（对照原仓库）

SPEC §13 明令删除、不得复活：

| 删掉的 | 位置/形态 | 为什么删 |
|---|---|---|
| Rust core | `core/` | 单进程 Python 已够用；双语言构建链是过度设计 |
| 双插件协议 | `extensions/` | 只保留 `manifest.json`/`tool.json`/`impl.py`/`fixtures.json` 单一插件协议 |
| 打包脚本 | `*_packages.py` | 不再需要多形态分发 |
| 组件/模块注册表 | `components.py`、`modules.py`、`node_recipes.py` | 被 `ToolRegistry` + 文件 watcher 替代 |
| SDK 与平台适配 | `sdk/`、`platforms/` | 不做对外 SDK 与多平台抽象 |
| 网关/语音/媒体 | `gateway.py`、`voice.py`、`media.py` | 非核心链路，一律砍掉 |
| 模型目录 | `model_catalog.py`、`model_limits.py` | 模型只由 `EASYAGENT_MODEL` 环境变量指定，不在代码里维护目录 |

架构层面同步简化的：

- **三层并发控制**（`max_children`/`max_depth`/`max_active_children`）：`MissionRunner` 保持单链 ReAct loop，不再做树形任务并发。
- **reflection 子循环**：从 loop 里移除；迭代改进只靠结束时的 `completion_score` 门控。
- **向量检索**：`memory.py` 只用 CJK bigram 词法 + TF 打分，不上向量库。

取舍原则：凡是"为未来可能的需求"存在的抽象层，一律删除；只保留 mission → run → tool_call 这条主链路上真实被调用的东西。

## Jev 四个门控位（TypeSafe 原生 API）

`decisions.py`。OpenRouter 转调 Jev 与 `typesafe-sdk`/`TYPESAFE_API_KEY` 方案均已作废，唯一走 workspace skill CLI `~/workspace/skills/typesafe/bin/jev`（subprocess 调用，`--state`/`--questions` 传 noul|choice|score 问题；认证走保险库 surrogate，代码零接触 key）。这是全架构**唯一允许双实现**的地方：`JevProvider` 失败 → 抛 `ProviderUnavailable` → 调用方降级 `FallbackProvider`（用 `llm.py` + 严格 prompt 自判）。真机验证：高风险 `rm -rf /` P(risky)=0.99 vs 良性 `echo hello` P(risky)=0.06，0.5 阈值保留（`EASYAGENT_RISK_THRESHOLD` 可覆盖）。

| # | 门控 | kind | 语义 |
|---|---|---|---|
| 1 | `risk_gate(tool_name, args) -> bool` | noul | "这个工具调用有风险吗"，P(risky) ≥ 0.5 → 需人工确认 |
| 2 | `gap_triage(need) -> explore\|skip\|ask` | choice | 能力缺口分诊：探索 / 跳过 / 问人。只约束**系统强制**的探索（bypass 计数器触发的 `ensure_capability`）；agent 自己调用 `scaffold` 是自主探索，不走此门控（scaffold 只写 inbox，转正仍受门控 4 约束） |
| 3 | `completion_score(summary) -> 0..5` | score | mission 完成度打分，<3 且步数有余则继续迭代 |
| 4 | `plugin_judge(name, fixture_report) -> bool` | noul | "该插件是否达到转正标准" |

### 门控降级模式（`EASYAGENT_GATE_POLICY`）

Jev 不可用时的降级链：**Jev → subagent judge（`FallbackProvider`，主模型 + 严格 prompt 自判）→ 策略**。
两个 provider 都不可用时的最终策略（默认 `open`，保持历史行为）：

| 策略 | 门控 2 `gap_triage` | 门控 4 `plugin_judge` |
|---|---|---|
| `open`（默认） | fail-open → `explore` | fail-open → 照样转正（记录 `unavailable (fail-open)`） |
| `ask` | → `ask`，转人工 | 不转正，记 `pending_human`，留在 inbox 等人工 |
| `halt` | 抛 `GateHalted`，停止构建（`ensure_capability` 记 learning 后返回 `None`） | 拒绝转正，记 `halted` |

门控 1 `risk_gate` 永远 fail-closed（决策异常 → 要求人工确认）；门控 3 异常 → 中性 3.0。

默认模型 `jev-latest`（可配 `JEV_MODEL`，不硬编码版本号）；questions 形状 `{qid: {"type": "noul"|"choice"|"score", "instructions": ..., "criteria": ...}}`，取返回 answers 的概率 → `Decision(answer, probability, provider="jev")`。key 绝不进日志。
