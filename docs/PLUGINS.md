# 插件开发

一个插件就是 `plugins/<name>/` 下的四件套。插件实现约定：`impl.py` 暴露 `def run(args: dict, ctx: dict) -> dict`。

## 四件套格式

### 1. `manifest.json` —— 身份与信任等级

对应 `contracts.ToolManifest`：

```json
{
  "name": "weather",
  "version": "0.1.0",
  "description": "查询指定城市的当前天气",
  "trust": "untrusted"
}
```

`trust` ∈ `trusted` | `untrusted`。首批内置工具在 `tools/__init__.py` 里注册为 `trusted`，`shell.exec` 除外（`untrusted`）。`untrusted` 插件的调用要过风险门控（见"人工确认"）。

### 2. `tool.json` —— 参数 schema

```json
{
  "name": "weather",
  "description": "查询指定城市的当前天气",
  "parameters": {
    "type": "object",
    "properties": {
      "city": { "type": "string", "description": "城市名，如 Beijing" }
    },
    "required": ["city"]
  }
}
```

### 3. `impl.py` —— 实现

```python
import httpx

def run(args: dict, ctx: dict) -> dict:
    city = args["city"]
    # 纯函数式：只用 args + ctx，不读全局状态
    resp = httpx.get(
        "https://api.example.com/weather",
        params={"city": city},
        timeout=15,
    )
    data = resp.json()
    return {"ok": True, "city": city, "temp_c": data["temp_c"], "desc": data["desc"]}
```

约定：返回 dict；失败时返回 `{"ok": False, "error": "..."}` 而不是抛异常（除非是不可恢复的编程错误）。需要网络/文件访问时，遵守与内置工具相同的约束（SSRF 防护、workspace 路径约束）。

### 4. `fixtures.json` —— 离线用例

对应 `contracts.PluginFixture`（`expect` 如 `{"ok": true, "stdout_contains": "..."}`）：

```json
[
  {
    "name": "basic query",
    "args": {"city": "Beijing"},
    "expect": {"ok": true}
  },
  {
    "name": "missing city",
    "args": {},
    "expect": {"ok": false}
  }
]
```

`promote` 会离线干跑全部用例；至少写 1 个用例（`scaffold` 生成的模板保证这点）。

## inbox → active 转正流程

```
scaffold / 手工编写
      ↓  写 plugins/inbox/<name>/{manifest.json, tool.json, impl.py, fixtures.json}
POST /tools/promote {"name": "<name>"}   →  registry.promote(name)
      ↓  离线干跑 fixtures.json 全部用例（+ Jev 门控 4 plugin_judge 裁决）
   全过 → 移入 plugins/active/ 并注册，可被 loop 调用
   失败 → 留在 inbox，失败原因写入 tool_versions 表（status=rejected）
```

注意：

- `promote` 只做离线验证，不调付费 API、不产生外部副作用（fixture 本身应该是可离线跑的）。
- 转正后的旧版本进 `plugins/_archive/` 归档，registry 保留最近 N=3 个版本。

## 热重载与回滚

`registry.watch()` 启动 1s 轮询的 watcher 线程：

1. 检测到 `plugins/active/<name>/manifest.json` 变化 → importlib 重载 `impl.py`（模块名带 version/hash 做 cache-bust，避免旧模块缓存）。
2. 重载成功 → 新版本生效，旧版本归档保留（最近 3 个）。
3. 加载失败 → 自动回滚到上一个可用版本，并在 `tool_versions` 表记录 `status=rolled_back`，不中断正在跑的 run。

改插件不需要重启服务：直接改 `plugins/active/<name>/` 下的文件，1 秒内生效。

## trusted / untrusted 与人工确认

调用路径 `registry.call(name, args, ctx)`：

- `trusted` 工具：直接执行。
- `untrusted` 工具且该工具累计人工确认次数 < K（K=3，可配置）：走 `decisions.risk_gate(tool_name, args)`（Jev 门控 1，noul："这个工具调用有风险吗"）。
  - P(risky) < 0.5 → 执行，并累计一次"通过"计数。
  - P(risky) ≥ 0.5 → 抛 `ApprovalRequired`，run 进入 `awaiting_confirm` 状态，SSE 推送等待事件。
- 人工在 `awaiting_confirm` 下通过 `POST /runs/{id}/steer {"action": "resume"}` 放行（等价于一次人工确认），或 `{"action": "cancel"}` 取消 run。

`shell.exec` 标记为 `untrusted`；需要 shell 解释时必须显式传 `use_shell=true`（同样走 untrusted 路径）。

最小验证：写完四件套后先放 inbox，调 `POST /tools/promote` 看转正结果；再用 `EASYAGENT_MOCK_LLM=1` 起服务发 mission 走一遍调用链。
