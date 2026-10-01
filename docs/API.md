# HTTP API 参考

Base URL：`http://localhost:8000`（`uvicorn easyagent.server:app` 默认端口）。`/` 挂载 `frontend/` 静态文件。

## 认证

设了 `EASYAGENT_API_KEY` 后，所有 API 接口（除 `/healthz`）要求：

```
Authorization: Bearer <key>
```

比较用常量时间比较（防时序攻击）。**通用 API 不再接受 `?key=`**（已删除该后门）。

SSE（EventSource 无法自定义 header）用一次性 token：

```bash
TOKEN=$(curl -s -X POST localhost:8000/v1/sse-tokens \
  -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
curl -N "localhost:8000/v1/runs/<run_id>/events?token=$TOKEN"
```

token 规则：`secrets.token_urlsafe(32)`，60 秒过期，**单次使用**（用过即焚），SSE 也可以直接用 Bearer header。

未设 `EASYAGENT_API_KEY` = 开放模式（仅适合 loopback 开发）。

## 路由说明

`/v1` 是当前版本；旧的不带前缀路由（`/missions`、`/runs/...` 等）保留为 deprecated alias，同样需要认证。

## POST /v1/missions —— 下发任务

```bash
curl -X POST localhost:8000/v1/missions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{
    "goal": "把 work/notes.md 的待办整理成清单",
    "budget": {"max_steps": 50, "max_cost_usd": 1.0, "max_wall_clock_s": 1800}
  }'
# → {"mission_id": "m_...", "run_id": "r_..."}
```

请求：`goal`（必填），`budget` 可选（`Budget`：`max_steps` 默认 50，`max_cost_usd` 可空，`max_wall_clock_s` 默认 1800）。响应里的 `run_id` 会在后台自动启动 ReAct loop。

## GET /v1/runs/{id} —— 查 run 状态

```bash
curl localhost:8000/v1/runs/r_xxx -H "Authorization: Bearer $KEY"
# → {"run_id": "r_xxx", "mission_id": "m_xxx", "status": "running",
#     "step": 7, "plan": ["..."], "started_at": "...", "updated_at": "..."}
```

`status` ∈ `pending` | `running` | `paused` | `awaiting_confirm` | `cancelled` | `done` | `failed`。

## POST /v1/runs/{id}/resume —— 断点续跑

从该 run 的最新 checkpoint 恢复 `history`/`step`/`cost`/`mission_id`，开一个**新 run**继续执行（旧 run 不被篡改）。新 run 会发 `resumed` 事件，旧 run 补一条 `resumed_from` 状态事件。

```bash
curl -X POST localhost:8000/v1/runs/r_xxx/resume -H "Authorization: Bearer $KEY"
# → {"run_id": "r_new...", "resumed_from": "r_xxx"}
```

无 checkpoint、checkpoint 损坏、mission 丢失 → 干净失败（400/404），不产生新 run。

## SSE：GET /v1/runs/{id}/events —— 订阅事件流

```bash
# Bearer 方式
curl -N "localhost:8000/v1/runs/r_xxx/events" -H "Authorization: Bearer $KEY"
# 一次性 token 方式（浏览器用）
curl -N "localhost:8000/v1/runs/r_xxx/events?token=$TOKEN"
# 增量拉取：断线续拉只返回该序号之后的事件
curl -N "localhost:8000/v1/runs/r_xxx/events?token=$TOKEN&after_seq=42"
```

`text/event-stream`。前端 `connectSSE()` 会自动调 `/v1/sse-tokens` 换 token 后建连接；断线重连时重新换（旧 token 已焚）。

### SSE 事件类型表

事件即 `contracts.RunEvent`（`{run_id, seq, type, payload, ts}`，`seq` 自增）：

| type | 含义 | payload 要点 |
|---|---|---|
| `plan` | 本轮计划 | 步骤列表 |
| `thought` | 模型思考 | 文本 |
| `tool_call` | 发起工具调用 | `tool` 名、`args` |
| `tool_result` | 工具返回 | `ok`、`result`/`error` |
| `artifact` | 产生工件 | 工件描述/路径 |
| `checkpoint` | 存档点 | `step`（每 5 步一次） |
| `status` | 状态变更 | 新 `status`（含 `awaiting_confirm`、`done(reason=budget)`、`resumed_from` 等） |
| `steer` | 收到人工指令 | `SteerCommand` 回显 |
| `tools_trimmed` | prompt 工具裁剪 | `total`/`sent`/`dropped`（被裁掉的工具名；自愈核心永不被裁） |
| `prune_candidates` | prune 候选报告 | 每次 run 结束：候选列表与原因；`EASYAGENT_AUTO_PRUNE=1` 才真执行 |
| `resumed` | 续跑开始 | `resumed_from`（旧 run id） |
| `done` | run 结束 | 总结 |
| `error` | 出错 | 错误信息 |

## POST /v1/runs/{id}/steer —— 人工干预

```bash
# 暂停 / 继续 / 取消
curl -X POST localhost:8000/v1/runs/r_xxx/steer \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"action": "pause"}'

# 重定向：把 message 注入下一步 context
curl -X POST localhost:8000/v1/runs/r_xxx/steer \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"action": "redirect", "message": "先只处理前 3 条待办"}'

# 人工放行：awaiting_confirm 状态下 action=resume 视为批准通过
curl -X POST localhost:8000/v1/runs/r_xxx/steer \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"action": "resume"}'
```

`action` ∈ `pause` | `resume` | `cancel` | `redirect`，对应 `contracts.SteerCommand{action, message?}`。语义：pause 挂起、resume 继续、cancel 终止、redirect 注入 message 到下一步 context。

## GET /v1/tools —— 工具列表

```bash
curl localhost:8000/v1/tools -H "Authorization: Bearer $KEY"
# → [{"name": "shell.exec", "version": "...", "description": "...", "trust": "untrusted"}, ...]
```

返回 registry 当前注册的工具（含 `ToolManifest` 字段）。

## POST /v1/tools/promote —— 插件转正

```bash
curl -X POST localhost:8000/v1/tools/promote \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "weather"}'
# → {"ok": true, ...} 或 {"ok": false, "reason": "..."}
```

四道门（详见 `PLUGINS.md` 与 `SPEC.md`）：离线 fixtures 全过 → 确定性重跑（噪声基线，默认额外 2 次）→ 泄漏审查（文本扫描 + 行为探针）→ Jev `plugin_judge` 终裁。失败留在 inbox，原因记入 `tool_versions`（`status=rejected`）。
