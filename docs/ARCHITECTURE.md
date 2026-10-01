# 架构

## 三层

整个系统是单进程 + SQLite + 文件系统，按数据流向分为三层。模块名与接口名以 SPEC 为准。

### 1. 发现层（`discovery.py`）——缺什么，现找什么

入口 `ensure_capability(need: str, ctx) -> str | None`，解决"能力缺口"（capability gap）：

1. `registry.get(need)` 命中 → 直接返回工具名。
2. `memory.search(need)` 找到某条 learning 里可落地的 recipe → `scaffold` 生成插件（仍需 `promote` 转正）。
3. 探索子任务（有界：≤10 步、≤300s、禁止付费调用）：`fetch_docs` 拉文档 → `probe` 沙盒试调 → `scaffold` 写 `plugins/inbox/<name>/` 四件套 → `promote`。
4. 失败 → 返回 `None`，并 `append_learning("explored <need>: failed because ...")` 把教训记下来。

决策"要不要探索"本身走 Jev 门控 2（`gap_triage`，见下）。

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

`server.py` 是薄 HTTP 层：missions API + SSE 事件推送（`GET /runs/{id}/events?after_seq=N`），`/` 挂载 `frontend/` 静态文件。前端不跑 agent 逻辑、不持有状态机。接口细节见 `API.md`。

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
| 2 | `gap_triage(need) -> explore\|skip\|ask` | choice | 能力缺口分诊：探索 / 跳过 / 问人 |
| 3 | `completion_score(summary) -> 0..5` | score | mission 完成度打分，<3 且步数有余则继续迭代 |
| 4 | `plugin_judge(name, fixture_report) -> bool` | noul | "该插件是否达到转正标准" |

默认模型 `jev-latest`（可配 `JEV_MODEL`，不硬编码版本号）；questions 形状 `{qid: {"type": "noul"|"choice"|"score", "instructions": ..., "criteria": ...}}`，取返回 answers 的概率 → `Decision(answer, probability, provider="jev")`。key 绝不进日志。
