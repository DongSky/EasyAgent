# HTTP API 参考

Base URL：`http://localhost:8000`（`uvicorn easyagent.server:app` 默认端口）。`/` 挂载 `frontend/` 静态文件。

## POST /missions —— 下发任务

```bash
curl -X POST localhost:8000/missions \
  -H 'Content-Type: application/json' \
  -d '{
    "goal": "把 work/notes.md 的待办整理成清单",
    "budget": {"max_steps": 50, "max_cost_usd": 1.0, "max_wall_clock_s": 1800}
  }'
# → {"mission_id": "m_...", "run_id": "r_..."}
```

请求：`goal`（必填），`budget` 可选（`Budget`：`max_steps` 默认 50，`max_cost_usd` 可空，`max_wall_clock_s` 默认 1800）。响应里的 `run_id` 会在后台自动启动 ReAct loop。

## GET /runs/{id} —— 查 run 状态

```bash
curl localhost:8000/runs/r_xxx
# → {"run_id": "r_xxx", "mission_id": "m_xxx", "status": "running",
#     "step": 7, "plan": ["..."], "started_at": "...", "updated_at": "..."}
```

`status` ∈ `pending` | `running` | `paused` | `awaiting_confirm` | `cancelled` | `done` | `failed`。

## SSE：GET /runs/{id}/events —— 订阅事件流

```bash
curl -N "localhost:8000/runs/r_xxx/events"
# 也可增量拉取：curl -N "localhost:8000/runs/r_xxx/events?after_seq=42"
```

`text/event-stream`。`after_seq` 用于断线续拉（只返回该序号之后的事件）。

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
| `status` | 状态变更 | 新 `status`（含 `awaiting_confirm`、`done(reason=budget)` 等） |
| `steer` | 收到人工指令 | `SteerCommand` 回显 |
| `done` | run 结束 | 总结 |
| `error` | 出错 | 错误信息 |

## POST /runs/{id}/steer —— 人工干预

```bash
# 暂停 / 继续 / 取消
curl -X POST localhost:8000/runs/r_xxx/steer \
  -H 'Content-Type: application/json' \
  -d '{"action": "pause"}'

# 重定向：把 message 注入下一步 context
curl -X POST localhost:8000/runs/r_xxx/steer \
  -H 'Content-Type: application/json' \
  -d '{"action": "redirect", "message": "先只处理前 3 条待办"}'

# 人工放行：awaiting_confirm 状态下 action=resume 视为批准通过
curl -X POST localhost:8000/runs/r_xxx/steer \
  -H 'Content-Type: application/json' \
  -d '{"action": "resume"}'
```

`action` ∈ `pause` | `resume` | `cancel` | `redirect`，对应 `contracts.SteerCommand{action, message?}`。语义：pause 挂起、resume 继续、cancel 终止、redirect 注入 message 到下一步 context。

## GET /tools —— 工具列表

```bash
curl localhost:8000/tools
# → [{"name": "shell.exec", "version": "...", "description": "...", "trust": "untrusted"}, ...]
```

返回 registry 当前注册的工具（含 `ToolManifest` 字段）。

## POST /tools/promote —— 插件转正

```bash
curl -X POST localhost:8000/tools/promote \
  -H 'Content-Type: application/json' \
  -d '{"name": "weather"}'
# → {"ok": true, ...} 或 {"ok": false, "reason": "..."}
```

对 `plugins/inbox/<name>/fixtures.json` 做离线干跑；全过则移入 `active/` 并注册，失败则留在 inbox（原因记入 `tool_versions`，`status=rejected`）。详见 `PLUGINS.md`。
