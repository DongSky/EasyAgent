# EasyAgent Rewrite

单进程 agent 运行时：SQLite 存状态、文件系统存插件，一个可中断、可 steer 的 ReAct 长循环执行任务。

一句话架构：**发现层**（缺能力就地探索并孵化插件）→ **注册表**（插件热重载、转正门槛、风险门控）→ **长循环**（MissionRunner 跑 ReAct，带 checkpoint、熔断、人工 steer），推理底座是经 OpenRouter 的 Hermes 系列模型，关键决策位由 Jev（TypeSafe 原生 API）裁决。

## 5 分钟 quickstart

```bash
git clone <repository-url>      # 或直接使用已有目录 ~/workspace/easyagent-rewrite
cd easyagent-rewrite
pip install -e .
```

设置环境变量（key 只从环境变量读，绝不写进文件）：

```bash
export OPENROUTER_API_KEY=...   # 推理底座
export EASYAGENT_MODEL=...      # OpenRouter 上的 Hermes 系列模型 id
# Jev 决策位：通过 workspace skill CLI `~/workspace/skills/typesafe/bin/jev` 调用，
# 认证走保险库 surrogate 机制（CLI 内部处理），代码不碰原始 key、不设 secret 环境变量
```

起服务：

```bash
uvicorn easyagent.server:app
```

发第一个 mission：

```bash
curl -X POST localhost:8000/missions \
  -H 'Content-Type: application/json' \
  -d '{"goal": "列出 work/ 目录下的文件", "budget": {"max_steps": 20}}'
# → {"mission_id": "...", "run_id": "..."}
```

看 SSE 事件流（`<run_id>` 换成上一步返回的值）：

```bash
curl -N "localhost:8000/runs/<run_id>/events"
```

离线冒烟（不花一分钱、不调任何真实 API）：

```bash
EASYAGENT_MOCK_LLM=1 uvicorn easyagent.server:app
```

测试一律用 `EASYAGENT_MOCK_LLM=1`；decisions 测试用 MockProvider；Jev 联调等用户提供 key 后再做。

## 环境变量

| 变量 | 用途 | 说明 |
|---|---|---|
| `OPENROUTER_API_KEY` | 推理底座 | `llm.py` 经 OpenRouter `/chat/completions` 调用，key 只从这里读 |
| （无） | Jev 决策 | 经 skill CLI `~/workspace/skills/typesafe/bin/jev` 调用（保险库 surrogate 认证，代码零接触 key）；CLI 不可用或调用异常时 `JevProvider` 抛 `ProviderUnavailable`，调用方降级到 `FallbackProvider` |
| `EASYAGENT_MODEL` | 模型选择 | OpenRouter 上的 Hermes 系列模型 id；未设置则报错（代码里不写死具体模型） |
| `JEV_ENABLED` | Jev 开关 | 默认 `1`；为 `1` 且有 key 才用 `JevProvider` |
| `JEV_MODEL` | Jev 模型 | 默认 `jev-latest`，不硬编码版本号 |
| `EASYAGENT_WORKSPACE` | 工作区 | `file.*` 工具的默认根目录，默认 `~/workspace/easyagent-rewrite/work`；路径越界拒绝 |
| `EASYAGENT_MOCK_LLM` | 离线测试 | 设为 `1` 时 `ModelClient` 走 `MockClient`，返回固定 canned ReAct 轨迹 |

硬约束：密钥绝不写进任何文件、绝不打进日志；SDK/httpx 日志级别保持 info 或 off，绝不打 request body。

## 目录速览

```
easyagent/
  contracts.py   # 10 个 pydantic 模型（Mission / RunState / RunEvent / …）
  store.py       # SQLite：missions / runs / events / checkpoints / tool_versions / learnings
  llm.py         # ModelClient（OpenRouter）/ MockClient
  decisions.py   # DecisionProvider / JevProvider / FallbackProvider
  registry.py    # ToolRegistry：discover / 热重载 / promote / 风险门控
  discovery.py   # capability gap：ensure_capability
  memory.py      # learnings.jsonl + CJK bigram 词法检索（无向量）
  loop.py        # MissionRunner：ReAct + checkpoint + 熔断 + steer
  server.py      # FastAPI：missions API + SSE
  tools/         # 内置工具：shell / file / hermes_search / fetch_docs / probe / scaffold / meta
plugins/
  active/        # 转正插件
  inbox/         # 待验证插件
  _archive/      # 旧版本归档
frontend/        # 薄前端：任务下发 + SSE 事件渲染 + steer 控件
work/            # agent 工作区
docs/            # 本目录
```

深入阅读：`ARCHITECTURE.md`（三层架构与删了什么）、`PLUGINS.md`（插件开发）、`API.md`（HTTP 接口）。
