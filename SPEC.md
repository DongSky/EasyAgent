# EasyAgent Rewrite — Build Spec (v1)

目标：单进程 + SQLite + 文件系统，三层架构。Nemotron 模型（经 OpenRouter，`EASYAGENT_MODEL` 可配置）做推理底座。
仓库：~/workspace/easyagent-rewrite。只读参考：/tmp/easyagent-analysis（原仓库浅克隆，commit 7137c77，绝不修改）。

## 0. 目录布局

```
~/workspace/easyagent-rewrite/
  SPEC.md
  pyproject.toml            # python>=3.11; deps: fastapi, uvicorn, pydantic, httpx; optional: typesafe = ["typesafe-sdk"]（已作废，Jev 只走 skill CLI）
  easyagent/
    __init__.py
    contracts.py            # ≤10 个 pydantic 模型（见 §1）
    store.py                # SQLite（见 §2）
    llm.py                  # ModelClient：模型只由 EASYAGENT_MODEL 指定（已验证 Nemotron 经 OpenRouter）；EASYAGENT_MOCK_LLM=1 时走 Mock
    decisions.py            # DecisionProvider 接口 / JevProvider（TypeSafe 原生 API）/ FallbackProvider
    registry.py             # ToolRegistry + 文件 watcher 热重载 + fixtures 转正门槛
    discovery.py            # capability gap 流程
    memory.py               # learnings.jsonl + CJK bigram 词法检索（不上向量）
    loop.py                 # MissionRunner：ReAct loop + checkpoint + 熔断 + steer
    server.py               # FastAPI：missions API + SSE
    tools/
      __init__.py           # 工具注册 helper
      shell.py              # shell.exec
      files.py              # file.read / write / edit
      search.py             # hermes_search
      meta.py               # fetch_docs / probe / scaffold
  plugins/
    active/                 # 转正插件：<name>/{manifest.json, tool.json, impl.py, fixtures.json}
    inbox/                  # 待验证插件（同上结构）
    _archive/               # 旧版本归档
  learnings.jsonl           # 追加写记忆
  frontend/
    index.html
    app.js
    assets/                 # 从原仓库 apps/agent/static 拷贝的视觉文件（graph-editor.js 等）
  docs/                     # ≤5 个文档
  work/                     # agent 工作区（file.* 工具默认根目录）
```

## 1. Contracts（contracts.py，pydantic，≤10 个）

- `Budget { max_steps: int = 50, max_cost_usd: float | None = None, max_wall_clock_s: int = 1800 }`
- `Mission { id: str, goal: str, budget: Budget, created_at: str }`
- `RunState { run_id: str, mission_id: str, status: str, step: int, plan: list[str], started_at: str, updated_at: str }`
  status ∈ pending|running|paused|awaiting_confirm|cancelled|done|failed
- `RunEvent { run_id: str, seq: int, type: str, payload: dict, ts: str }`
  type ∈ plan|thought|tool_call|tool_result|artifact|checkpoint|status|steer|done|error
- `Checkpoint { run_id: str, step: int, state_json: str, created_at: str }`
- `ToolManifest { name: str, version: str, description: str, trust: str }`  # trust ∈ trusted|untrusted
- `DecisionQuestion { kind: str, question: str, options: list[str] | None, state: dict }`  # kind ∈ noul|choice|score
- `Decision { answer: str | bool | float, probability: float, provider: str }`
- `SteerCommand { action: str, message: str | None }`  # action ∈ pause|resume|cancel|redirect
- `PluginFixture { name: str, args: dict, expect: dict }`  # expect 如 {ok: true, stdout_contains: "..."}

## 2. Store（store.py，SQLite）

表：`missions`、`runs`、`events`、`checkpoints`、`tool_versions`、`learnings`。
方法：`init_db(path)`、`create_mission(goal, budget) -> Mission`、`get_mission(id)`、
`create_run(mission_id) -> RunState`、`update_run(run_id, **fields)`、`get_run(run_id)`、
`append_event(run_id, type, payload) -> RunEvent`（seq 自增）、`get_events(run_id, after_seq=0)`、
`save_checkpoint(run_id, step, state_json)`、`load_latest_checkpoint(run_id)`、
`record_tool_version(name, version, manifest_json, status)`、`list_tool_versions(name)`、
`append_learning(text, tags)`、`get_learnings(limit)`。

## 3. ToolRegistry（registry.py）

- `discover(plugins_dir)`：扫描 `plugins/active/*/manifest.json` + `tool.json`，动态 import `impl.py`。
  插件实现约定：`impl.py` 暴露 `def run(args: dict, ctx: dict) -> dict`。
- `watch()`：轮询 watcher 线程（1s），manifest 变化 → 重载（importlib，模块名带 version/hash 做 cache-bust）；
  保留最近 N=3 个版本；加载失败自动回滚上一可用版本并记 `tool_versions`（status=rolled_back）。
- `get(name) -> ToolDef | None`；`list_tools()`。
- `call(name, args, ctx)`：untrusted 且累计确认次数 < K（K=3，可配置）→ 走 decisions.risk_gate；
  P(risky) ≥ 阈值（默认 0.5）→ 抛 `ApprovalRequired`（run 进入 awaiting_confirm，steer 可放行/取消）。
- `promote(name)`：跑 `plugins/inbox/<name>/fixtures.json` 全部用例（离线干跑）→ 全过则移入 `active/` 并注册；
  失败则留在 inbox，失败原因写入 `tool_versions`（status=rejected）。

## 4. Discovery（discovery.py）—— capability gap

`ensure_capability(need: str, ctx) -> str | None`：
1. `registry.get(need)` 命中 → 返回。
2. `memory.search(need)`：某条 learning 含可落地的 recipe → `scaffold` 生成插件（需经 promote）。
3. 探索子任务（有界：≤10 步，≤300s，禁止付费调用）：用 `fetch_docs` 拉文档 → `probe` 沙盒试调 →
   `scaffold` 写 `plugins/inbox/<name>/{manifest.json, tool.json, impl.py, fixtures.json}` → `promote`。
   失败 → 返回 None，并 `append_learning("explored <need>: failed because ...")`。

## 5. Decisions（decisions.py）—— ★ Jev 走 workspace skill CLI（SDK/原生 HTTP 方案作废）

- `class DecisionProvider`：`decide(q: DecisionQuestion) -> Decision`。`class ProviderUnavailable(Exception)`（任一 provider 不可用时抛）。`class GateHalted(Exception)`（降级策略为 halt 且无可用 provider 时抛，调用方必须停止）。
- `JevProvider`：
  - 唯一实现：subprocess 调 workspace skill CLI `~/workspace/skills/typesafe/bin/jev`
    （`--state`/`--questions` 传 noul|choice|score 问题，`--model` 指定模型）；
    认证由 CLI 内部经保险库 surrogate 处理，代码零接触原始 key、不设 secret 环境变量。
  - questions 形状：`{qid: {"type": "noul"|"choice"|"score", "instructions": ..., "criteria": ...}}`；
    state 可为 string/object/array；取返回的 answers 概率 → `Decision(answer, probability, provider="jev")`。
  - 默认模型 `jev-latest`（不硬编码版本号，可配 `JEV_MODEL` 环境变量；`JEV_TIMEOUT` 默认 60s；`JEV_CLI` 可覆盖 CLI 路径）。
  - CLI 不可用（`JEV_ENABLED=0`、文件不存在/不可执行）或调用失败/超时/返回非 JSON → 抛 `ProviderUnavailable`。
  - 日志级别保持 info 或 off，绝不打 request body；key 绝不进日志（代码里根本没有 key）。
- `FallbackProvider`：subagent judge——用 `llm.py` 的 ModelClient + 严格 prompt 自判，解析 yes/no/choice/score。Jev 不可用时先降级到它。
- `make_provider() -> DecisionProvider`：`JEV_ENABLED`（默认 1）且 jev CLI 可用 → `JevProvider`，
  否则 → `FallbackProvider`。这是全架构唯一允许双实现的地方。
- 降级链：**Jev → subagent judge（FallbackProvider）→ `EASYAGENT_GATE_POLICY`**。
  两个 provider 都不可用时的最终策略（默认 `open`，保持历史行为）：
  - `open`：fail-open。`gap_triage` → `explore`；`plugin_judge` → 照样转正（记录 `unavailable (fail-open)`）。
  - `ask`：转人工。`gap_triage` → `ask`；`plugin_judge` → 不转正，记 `pending_human` 留在 inbox 等人工。
  - `halt`：停止。`gap_triage` 抛 `GateHalted`（`ensure_capability` 捕获后记 learning 并返回 `None`，不构建）；
    `plugin_judge` → 拒绝转正（记 `halted`）。
- 四个门控位（kind 映射）：
  1. 工具风险 `risk_gate(tool_name, args) -> bool`：noul("这个工具调用有风险吗")，P(risky)≥0.5 → 需人工。**永远 fail-closed**：决策异常 → 要求人工确认。
  2. gap 分诊 `gap_triage(need) -> explore|skip|ask`：choice 三选一；不可用时走 `EASYAGENT_GATE_POLICY`。
  3. mission 完成度 `completion_score(summary) -> 0..5`：score；异常 → 中性 3.0。
  4. 插件转正裁判 `plugin_judge(name, fixture_report) -> bool`：noul("该插件是否达到转正标准")；不可用时走 `EASYAGENT_GATE_POLICY`。

## 6. LLM（llm.py）

- `ModelClient(model: str | None)`：model 默认取环境变量 `EASYAGENT_MODEL`；
  未设置则报错并提示设置 OpenRouter 上的 Nemotron 模型 id（不要在代码里写死某个具体模型）。
  经 OpenRouter `/chat/completions`（httpx），key 只从 `OPENROUTER_API_KEY` 读。
- `chat(messages, tools=None) -> {content, tool_calls, usage}`；usage 累计 cost 供熔断。
- `EASYAGENT_MOCK_LLM=1` → `MockClient`：返回固定 canned ReAct 轨迹（thought→tool_call→observation→done），
  供离线冒烟测试。测试一律用 Mock，禁止真实付费调用。

## 7. Loop（loop.py）

`MissionRunner(store, registry, decisions, llm)`：
- `start(mission_id) -> run_id`：后台线程跑 ReAct（thought → 决策 act|finish → tool_call → observation）。
  骨架参考 `/tmp/easyagent-analysis/src/easyagent/autonomy.py`，**删掉三层并发控制**（max_children/max_depth/max_active_children）
  和 reflection 子循环，保持单链 loop。
- 每步 emit RunEvent；每 N=5 步 `save_checkpoint`；每步检查熔断（steps / cost / wall clock），
  到线 → status=done（reason=budget）并生成总结 artifact。
- steer：`queue_command(run_id, SteerCommand)`；pause 挂起、resume 继续、cancel 终止、redirect 把 message
  注入下一步 context。`awaiting_confirm` 状态下 steer action=resume 视为人工放行。
- 结束时调 `completion_score`（decisions 门控 3），<3 分且步数有余 → 继续迭代；否则写总结。
- **绕路计数器**（防 Agent 用裸 shell 绕过 capability→plugin 主链路）：
  mission 成功结束（`completion_score` ≥ 3，非熔断）且原始工具（`shell.exec`/`file.write`/`file.edit`）
  调用 ≥ `EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD`（默认 3）次、期间没调过 `scaffold`/`plugin.promote` →
  记一次 bypass（`bypass_events` 表按目标签名计数 + learning + `bypass` 事件）。
  同一目标家族累计 ≥ `EASYAGENT_BYPASS_THRESHOLD`（默认 2）次 → 强制走发现层：
  用本次录制的 shell transcript 做 recipe（`{"kind": "shell", "commands": [...]}`），
  调 `ensure_capability` 自动 scaffold 出 macro 插件（仍过全部转正门）。
  `EASYAGENT_BYPASS_AUTO=0` 关闭。一次性临时命令（< 阈值）不受影响。

## 8. Server（server.py，FastAPI）

- `POST /missions {goal, budget?}` → `{mission_id, run_id}`（run_id 后台启动）
- `GET /runs/{id}` → RunState；`GET /runs/{id}/events?after_seq=N` → SSE `text/event-stream`
- `POST /runs/{id}/steer {action, message?}` → ok
- `GET /tools` → registry 列表；`POST /tools/promote {name}` → promote 结果
- `/` 挂载 `frontend/` 静态文件。
- 启动：`uvicorn easyagent.server:app`。依赖缺失时 worker 自行 `pip install fastapi uvicorn pydantic httpx`。

## 9. Tools（easyagent/tools/）

- `shell.exec {command, timeout_s=30, max_output=65536}` → `{ok, exit_code, stdout, stderr}`。
  参考原 `backends.py` 的 `terminal.execute` + `bounded_read` 思想：subprocess 超时杀、输出截断；
  空命令拒绝；默认 `shell=False`（list 形式），需要 shell 时显式 `use_shell=true`（标记 untrusted）。
- `file.read {path, offset?, limit?}` / `file.write {path, content}` / `file.edit {path, old_text, new_text}`：
  路径约束在 `EASYAGENT_WORKSPACE`（默认 `~/workspace/easyagent-rewrite/work`）内，越界拒绝。
- `hermes_search {query, limit=5}` → `[{title, url, snippet}]`：免费无 key 的 web 搜索
  （DuckDuckGo instant answer API），15s 超时，失败/离线返回空列表不抛错。
- `fetch_docs {url}` → `{title, text}`：SSRF 防护**照抄**原 `capability_research.py` 的 `public_url` 校验
  （只许 http/https、禁私网 IP/元数据地址、≤2MB、20s 超时）。
- `probe {method, url, headers?, body?}` → `{ok, status, data}`：同 SSRF 防护；成功调用记为 recipe 返回。
- `scaffold {name, description, recipe}` → 写 `plugins/inbox/<name>/{manifest.json, tool.json, impl.py, fixtures.json}`；
  recipe 有两种：`{"url": ...}`（HTTP，`impl.py` 按 recipe 模板生成 httpx 调用）或
  `{"kind": "shell", "commands": [...], "cwd"?}`（macro 插件：`impl.py` 用 `shlex.split` 无 `shell=True`
  按序重放录制的命令，单条 30s 超时，输出截断；`trust: untrusted`）。fixtures.json 至少 1 个用例。
- 首批内置工具在 `tools/__init__.py` 里向 registry 注册为 `trusted`（shell.exec 除外：`untrusted`）。

## 10. Memory（memory.py）

`append_learning(text, tags=[])` → `learnings.jsonl`（`{ts, text, tags}`）；
`search(query, limit=5)`：CJK bigram 分词 + 简易 TF 打分（参考原 `knowledge.py` 词法检索思想，**不上向量**）；
进程启动时把最近 20 条 learnings 注入 llm system prompt。

## 11. Frontend（frontend/）

- 薄前端：`index.html` + `app.js`：任务下发表单（goal + budget 三输入）、run 视图（SSE 事件流渲染：
  plan/thought/tool_call/tool_result/artifact/status）、产物浏览、steer 控件（pause/resume/cancel/redirect 输入）、预算显示。
- **动画与画布全保留**：从 `/tmp/easyagent-analysis/apps/agent/static/` 拷贝视觉文件
  （`graph-editor.js` 画布、`studio.js`、`workspace-chat.js`、`chat/` 视觉部分）到 `frontend/assets/`，
  只做最低限度合并保证能加载；run 画布面板尽量接 SSE 事件渲染节点，无需完美。
- 不跑 agent 逻辑，不持有状态机。

## 12. 环境变量（绝不进仓库）

`OPENROUTER_API_KEY`（llm）、`EASYAGENT_MODEL`、`JEV_ENABLED=1`、`JEV_MODEL`（默认 jev-latest）、
`JEV_TIMEOUT`（默认 60s）、`JEV_CLI`（覆盖 skill CLI 路径）、`EASYAGENT_GATE_POLICY`（open/ask/halt，默认 open）、
`EASYAGENT_RISK_THRESHOLD`（默认 0.5）、`EASYAGENT_BYPASS_AUTO=1`、`EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD=3`、
`EASYAGENT_BYPASS_THRESHOLD=2`、`EASYAGENT_WORKSPACE`、`EASYAGENT_MOCK_LLM=1`（测试）。
Jev 认证走保险库 surrogate（CLI 内部处理），**没有** `TYPESAFE_API_KEY` 这类 secret 环境变量。

## 13. 全 worker 硬约束

- 密钥/token/key 绝不写进任何文件、绝不打进日志；测试禁止真实付费 API 调用
  （llm 用 Mock，decisions 用 MockProvider，Jev 联调等用户给 key 后再做）。
- 每个模块交付时 `python -c "import easyagent.<mod>"` 必须通过，并附最小冒烟脚本/命令。
- 不要 git commit（协调人统一集成）；不要动 `/tmp/easyagent-analysis`；不要 push 任何远端。
- 删掉的东西不许复活：core/（Rust）、extensions/、双插件协议、*_packages.py、components.py、modules.py、
  node_recipes.py、sdk/、platforms/、gateway.py、voice.py、media.py、model_catalog.py、model_limits.py。
