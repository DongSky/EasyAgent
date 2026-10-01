# EasyAgent Rewrite

单进程 agent 运行时：SQLite 存状态、文件系统存插件，一个可中断、可 steer 的 ReAct 长循环执行任务。重做自 `DongSky/EasyAgent`（原仓库过度设计版），按"固定骨架 + Agent 自由探索"精简。

## 架构一句话

**发现层**（缺能力就地探索并孵化插件）→ **注册表**（插件热重载、转正门槛、风险门控）→ **长循环**（MissionRunner 跑 ReAct，带 checkpoint、熔断、人工 steer）。推理底座是经 OpenRouter 的 Nemotron 模型（`EASYAGENT_MODEL` 可配），关键决策位由 Jev（TypeSafe System One，经 workspace skill CLI 调用）裁决。

## 固定骨架 vs Agent 自由区域

用工作流保证它不变坏，用 Agent 让它能变强。

**锁死的（工作流保证）：**

- 任务三件套：SSE 事件流 + steer（pause/resume/cancel/redirect）+ checkpoint（含断点续跑 `POST /v1/runs/{id}/resume`）。
- 熔断三件套：max steps / 累计 cost / wall clock，到线即停。
- 插件转正四道门：离线 fixtures 全过 → 确定性重跑（噪声基线，默认复跑 2 次，`EASYAGENT_DETERMINISM_RUNS` 可调）→ 泄漏审查（文本扫描 + 行为探针：无视输入却返回预期答案的 impl 会被变异输入测试抓出来）→ Jev `plugin_judge` 终裁。
- RRSI 正则化：全量工具调用记账（内存 + `tool_usage` 持久表，prune 读持久表所以重启不丢数）+ `plugin.prune`（失败率≥50% 且≥5 次调用，或 30 天零调用 → 降级归档；builtin 豁免）；每次 run 结束自动出 prune 候选报告（`prune_candidates` 事件），`EASYAGENT_AUTO_PRUNE=1` 才真执行。系统提示词明令禁止为转正而削弱门禁。
- API 认证：`EASYAGENT_API_KEY` 设置后全接口走 `Authorization: Bearer`（常量时间比较）；SSE 用一次性 token（`POST /v1/sse-tokens` 换取，单次有效 60s），长效 key 永不进 URL。

**自由的（Agent 自己决定）：**

- 用什么工具组合完成 mission（shell / file / 搜索 / 已有插件）。
- 缺能力时走发现层：`fetch_docs` → `probe` → `scaffold` → `plugin.promote`，research 怎么做、impl 怎么写自己定。
- 什么时候 `memory.append` 留 recipe 给未来的自己。

自由不许绕开治理：见下面的绕路计数器。

## 门控降级模式

Jev 不可用时的降级链：**Jev → subagent judge（`FallbackProvider`，用主模型 + 严格 prompt 自判）→ `EASYAGENT_GATE_POLICY`**。

| 策略 | `gap_triage`（门控 2） | `plugin_judge`（门控 4） |
|---|---|---|
| `open`（默认） | fail-open → `explore` | fail-open → 照样转正（记录 `unavailable (fail-open)`） |
| `ask` | → `ask`，转人工 | 不转正，记 `pending_human`，留在 inbox 等人工 |
| `halt` | 抛 `GateHalted`，停止构建 | 拒绝转正，记 `halted` |

`risk_gate`（门控 1）永远 fail-closed：决策异常 → 要求人工确认。`completion_score` 异常 → 中性 3.0（不断循环、不误杀）。

## 绕路计数器：裸 shell 高频解决 → 必须 scaffold 成插件

Agent 可以用 `shell.exec`/`file.*` 直接解决问题，但"用裸 shell 绕过 capability→plugin 主链路"会被计数：

- 一次 mission 成功结束（`completion_score` ≥ 3）且原始工具调用 ≥ `EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD`（默认 3）次、期间没调过 `scaffold`/`plugin.promote` → 记一次 bypass（`bypass_events` 表 + learning + `bypass` 事件）。
- 同一目标家族 bypass 累计 ≥ `EASYAGENT_BYPASS_THRESHOLD`（默认 2）次 → **强制走发现层**：用本次录制的 shell 命令 transcript 做 recipe（`{"kind": "shell", "commands": [...]}`），调 `ensure_capability` 自动 scaffold 出 macro 插件。
- 自动 scaffold 出来的插件照样过全部四道转正门（含 `EASYAGENT_GATE_POLICY`）；过不了就老实留在 inbox。
- `EASYAGENT_BYPASS_AUTO=0` 可关掉整个检查。一次性临时命令（< 阈值）不受影响。

## 快速开始

```bash
git clone <repository-url>
cd easyagent-rewrite
pip install -e .
```

```bash
export OPENROUTER_API_KEY=...   # 推理底座（只用免费模型）
export EASYAGENT_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free
# Jev 决策位：经 workspace skill CLI ~/workspace/skills/typesafe/bin/jev 调用，
# 认证走保险库 surrogate（CLI 内部处理），代码不碰原始 key、不设 secret 环境变量
```

```bash
uvicorn easyagent.server:app
# 设了 EASYAGENT_API_KEY 后：
export KEY=<your-key>
curl -X POST localhost:8000/v1/missions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"goal": "列出 work/ 目录下的文件", "budget": {"max_steps": 20}}'
# SSE：先换一次性 token（EventSource 不能设 header，长效 key 不进 URL）
TOKEN=$(curl -s -X POST localhost:8000/v1/sse-tokens -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -N "localhost:8000/v1/runs/<run_id>/events?token=$TOKEN"
```

离线冒烟（零花费、零真实 API 调用）：

```bash
PYTHONPATH=$PWD python3 scripts/smoke_loop.py        # 骨架/门控/转正/发现
PYTHONPATH=$PWD python3 scripts/smoke_gates.py       # 降级策略/绕路计数器/shell scaffold
EASYAGENT_API_KEY=test-key-123 PYTHONPATH=$PWD python3 scripts/smoke_multiclient.py  # /v1+auth+SSE+webhook+裁剪+通知
PYTHONPATH=$PWD python3 scripts/verify_audit_fixes.py  # 审计驱动的加固项（泄漏探针/续跑/prune持久化）
```

## 环境变量

| 变量 | 用途 |
|---|---|
| `EASYAGENT_API_KEY` | API 认证 key；设了之后全接口要求 `Authorization: Bearer <key>`（SSE 除外，见下）。未设 = 开放模式（只适合 loopback 开发） |
| `OPENROUTER_API_KEY` | 推理底座（OpenRouter `/chat/completions`） |
| `EASYAGENT_MODEL` | 推理模型 id；未设置则报错（代码里不写死具体模型）。已验证：`nvidia/nemotron-3-ultra-550b-a55b:free` |
| `JEV_ENABLED` | 默认 `1`；为 `1` 且 jev CLI 可用才用 `JevProvider` |
| `JEV_MODEL` | 默认 `jev-latest`；`JEV_TIMEOUT` 默认 60s；`JEV_CLI` 可覆盖 CLI 路径 |
| `EASYAGENT_GATE_POLICY` | 门控降级策略：`open`（默认）/ `ask`（转人工）/ `halt`（停止） |
| `EASYAGENT_RISK_THRESHOLD` | 风险门阈值，默认 0.5 |
| `EASYAGENT_BYPASS_AUTO` | 绕路计数器开关，默认 `1`；`0` 关闭 |
| `EASYAGENT_BYPASS_PRIMITIVE_THRESHOLD` | 单次 mission 原始工具调用几次算绕路，默认 `3` |
| `EASYAGENT_BYPASS_THRESHOLD` | 同一目标家族绕路几次强制 scaffold，默认 `2` |
| `EASYAGENT_MOCK_LLM` | `1` 时走 `MockClient`，离线测试 |
| `EASYAGENT_WORKSPACE` | `file.*` 默认根目录；路径越界拒绝 |
| `EASYAGENT_MAX_PROMPT_TOOLS` | 发给模型的工具数上限，默认 48；超了先保自愈核心（scaffold/plugin.promote/plugin.merge/plugin.prune/memory.*）再保其他 builtin，其余按调用量裁；裁掉的发 `tools_trimmed` 事件 |
| `EASYAGENT_DETERMINISM_RUNS` | 转正噪声基线额外重跑次数，默认 2（任一次结果不同即拒绝） |
| `EASYAGENT_AUTO_PRUNE` | `1` 时每次 run 结束真执行 prune 降级；默认只报告候选（`prune_candidates` 事件） |
| `EASYAGENT_NOTIFY_WEBHOOK` | run 完成 webhook 地址；loopback 目标直连（不走代理），公网走环境代理；投递异步（后台线程），永不阻塞完成 |
| `EASYAGENT_NOTIFY_ON` | 通知哪些终态，默认 `done,failed` |

硬约束：密钥绝不写进任何文件、绝不打进日志；只用免费模型/接口，不产生付费。

## 目录

```
easyagent/       # contracts / store / llm / decisions / registry / discovery /
                 # memory / loop / server / tools(shell,file,search,meta,plugin)
plugins/         # active/ 转正插件；inbox/ 待验证；_archive/ 旧版本归档
frontend/        # 薄前端：任务下发 + SSE 渲染 + steer 控件（动画/画布效果保留）
work/            # agent 工作区
docs/            # ARCHITECTURE.md / PLUGINS.md / API.md / README.md（深入阅读）
scripts/         # smoke_loop.py / smoke_gates.py
SPEC.md          # 实现规格（以代码为准，文档随功能同步更新）
```

深入阅读：`docs/ARCHITECTURE.md`（三层架构与删了什么）、`docs/PLUGINS.md`（插件开发）、`docs/API.md`（HTTP 接口）。
