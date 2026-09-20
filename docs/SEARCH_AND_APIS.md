# 搜索与用户自定义 API

核对 TinyFish 官方文档日期：2026-09-19。内置搜索接入和用户自定义 API 共用 ToolRegistry、Schema 校验、审批、运行记录和三种构建入口。

## 先接入搜索

在 **设置 → 模型与服务 → 联网搜索** 中填写自己的 TinyFish API Key，点击「保存连接」或「保存并测试」。这个入口常驻显示，不需要先理解工具名称、Schema 或环境变量。密钥加密保存；以后留空保留，填写新值即可替换。状态会区分设置中保存的密钥和服务端环境变量。已有连接还可直接点击「测试当前连接」。

「保存连接」不会发起搜索；「保存并测试」及「测试当前连接」各执行一次真实查询，在任务记录保留结果或失败原因。测试本身会使用相应搜索账户的额度。高级设置可选择环境变量或受信任的兼容服务地址。「添加搜索 / API」弹窗仍支持自定义工具名称及更多 API 接入。

默认工具名 `search.tinyfish`。在「低代码 · 编排流程」选择它并添加 API 节点，填写搜索内容；可将上游文字输出连接到 `query`。零代码构建由模型自动选择已接入工具。代码模式通过 Python、JS 或 Rust 提交同一份 Workflow。

代码部署也可使用 `examples/config.search.json`，从环境变量 `TINYFISH_API_KEY` 读取密钥。在自己的终端设置变量后运行：

```sh
uv run --extra app easyagent studio --config examples/config.search.json
# 另一个终端：实际调用 TinyFish，需有效账号权限
uv run easyagent run examples/search-workflow.json
```

Python 内嵌入口是 `easyagent.search.register_tinyfish(hub, {"api_key_env":"TINYFISH_API_KEY"})`。SDK 无需单独安装 TinyFish 客户端。

## TinyFish 协议

- `GET https://api.search.tinyfish.ai`，请求头 `X-API-Key`，查询参数 `query` 必填。
- 支持 `purpose`、`location`、`language`、`include_domains`、`exclude_domains`、`page`（0–10）。location 必须是 HK/US/GB 等两位地区代码，不能是 Hong Kong 等名称；language 是 en/zh 等语言代码。域名过滤为逗号分隔字符串。
- `domain_type` 为 web、news 或 research_paper。
- `recency_minutes` 为 1–5256000 的整数；不可与 `after_date` / `before_date` 同用。日期必须为有效 YYYY-MM-DD，起始日期不得晚于结束日期。
- 论文搜索使用 `pub_year_min` / `pub_year_max`（0–9999）；不接受日期和相对时间过滤。年份过滤只用于论文搜索。
- 返回原生 `query/results/total_results/page`；结果保留 position、site_name、title、snippet、url，以及可选 date、publisher、authors、venue、year、cited_by_count、pdf_url。
- 搜索结果是外部材料；不作为指令授权执行其他操作。不自动访问链接、不抓取全文，也不直接证明摘要内容真实。

无效组合在发出 HTTP 请求前拒绝；单次请求默认 30 秒；响应最多 1 MB、不自动跟随重定向。408、429、5xx 和网络错误由工作流按 `max_attempts` 有界重试，其他 HTTP 错误立即失败。没有额外嵌套重试。尚未实现供应商级配额队列或 Retry-After 精确调度；官方文档当前写明默认 30 请求/分钟，生产并发需自行限制。

官方来源：[概览](https://docs.tinyfish.ai/search-api)、[参数和响应](https://docs.tinyfish.ai/search-api/reference)、[示例](https://docs.tinyfish.ai/search-api/examples)。已使用用户环境变量完成真实账号调用：英文关键词查询返回相关政府资料；首轮中文整句查询相关性不足，且混入域名外结果。应用仍应校验来源和相关性，不能把 HTTP 成功当作检索质量通过。完整记录见 [真实流程验收](LIVE_WORKFLOW_ACCEPTANCE.md)。

## 添加自己的接口

### 导入 OpenAPI

「搜索与 API」→「导入 OpenAPI」，选择 JSON 文件或粘贴 OpenAPI 3.0/3.1 文档，预览支持的操作，再勾选导入。可覆盖服务地址、设置工具前缀。错误会列出具体不支持的操作；所选操作全部校验成功才注册，避免部分导入。

支持本地非递归 `$ref`、路径/查询/请求头/cookie 参数、普通标量及重复键查询数组、JSON/URL 编码请求体、JSON/纯文本响应。整个 requestBody 映射到工具输入 `body`，可在画布填写对象或连接上游对象输出。GET/HEAD/OPTIONS 默认 read，其他方法默认 write 并要求审批；真实只读 POST 可改用手动配置显式声明 read。

支持 apiKey header/query、HTTP bearer/basic、已有 OAuth/OpenID access token。Basic 输入为 `username:password` 的 Base64 值。自动登录/令牌刷新、多重鉴权方案、multipart 文件上传、递归或远程 schema、XML/二进制/流式响应不在此导入器范围；遇到这些能力可使用手动适配、三语言插件或 MCP。

### 手动配置 HTTP API

填写名称、用途、方法、URL、输入和返回 JSON Schema。高级字段：

- URL 路径 `{id}` 来自输入必填属性；相同占位符可出现多次。GET/HEAD 的其余字段默认 query，其他方法默认 JSON body。
- `parameter_locations` 指定每个输入字段为 query/header/cookie/body，例如 `{"page":"query","X-Region":"header"}`。
- `body_parameter` 将某个输入属性整体作为请求体，适用于嵌套对象或数组；不能和零散 body 字段混用。
- `request_encoding` 为 json/form；`response_mode` 为 json/text。text 输出为 `{"text":"...","content_type":"..."}`。
- Bearer、Basic、自定义 Header/Query API Key 或无鉴权。`auth_header` 是鉴权字段名，`auth_prefix` 是前缀。凭证只放入 `api_key` 或 `api_key_env`，固定请求头和 URL 中不要写密钥。
- `effect` 可显式 read/write；改变外部数据必须声明 write。`idempotent=true` 仅用于已知支持 Idempotency-Key 去重的写接口。
- 所有出站请求带稳定 Idempotency-Key；不承诺第三方一定处理它。非幂等写操作发生不确定错误时暂停核验。

远端 URL 必须 HTTPS；localhost/127.0.0.1/::1 支持 HTTP 调试。API 地址由服务拥有者配置，模型只看到工具名称、说明和 Schema。此功能属于本地开发者管理入口，不是可公开匿名注册任意目标的多租户网关。

## 保存连接与重启

TinyFish 设置保存连接信息与本机凭证库中的 Fernet 密文，重启自动恢复，无需设置系统环境变量。更新同名连接后，下一次搜索使用新配置。设置中保存的连接优先于同名启动配置或演示环境变量；如果明确切换为环境变量，则改用所选变量。独立数据库不会继承另一份数据库的凭证。密钥不回填到表单，不包含在状态接口、工具目录、工作流或导出配置中。

`GET /v1/studio/search/tinyfish?name=search.tinyfish` 返回连接状态和凭证来源；`POST /v1/studio/search/tinyfish` 创建或更新连接；`POST /v1/studio/search/tinyfish/test` 创建一次查询任务并返回运行 ID。测试支持可选 `name`、`query`，不自动重试失败查询。空密钥只会保留已有的设置密钥；没有凭证时明确拒绝保存。

面板「导出本页已接入的配置」只导出 `search` / `http_tools` 定义和凭证环境变量名，接收者须绑定自己的凭证。配置文件或 `register_tinyfish` 可建立进程内连接；要持久保存用户输入的搜索密钥，使用设置入口或上述 POST。HTTP API 定义已有独立的版本持久化；其凭证使用环境变量或凭证库别名，具体生命周期见 [连接与凭证](CONVERSATIONS_AND_CONNECTIONS.md)。

服务端配置可包含多个 `http_tools`，不限于内置供应商。Python 可直接 `register_http_tool(hub, definition)`；HTTP 调用 `POST /v1/studio/apis`；三个 SDK 的通用 request 方法都能调用这个入口。

## 可复现的离线界面验收

```sh
uv run --extra app python -m examples.demos.api_fixture
```

这会在 127.0.0.1:8771 启动合成服务。TinyFish 连接中展开「自定义网关或本地调试」，地址填 `http://127.0.0.1:8771/search`，key 填 `synthetic-demo-key`，工具名建议 `demo.tinyfish`。它始终返回明确标注的合成结果，不调用 TinyFish。`/lookup` 是可用于手动配置或 OpenAPI 导入的 POST JSON 接口，输入 `{"query":"演示"}`。结束验收后可停止这个独立服务。

自动验收：`uv run --extra app --extra dev pytest -q tests/integration/test_search_and_apis.py`，所有用例使用真实本地 HTTP、真实 SQLite 和工作流；不伪造供应商在线通过结论。

2026-09-20 设置验收覆盖：无环境变量的新项目、用户 A/B 密钥替换、加密存储、重启恢复、启动配置不覆盖用户设置、独立项目使用用户 C 的密钥。Playwright 覆盖设置页填写、保存并测试、刷新后测试、401 失败提示与恢复、手机宽度，无页面错误；本轮使用独立本地服务与测试密钥，没有覆盖预览环境中用户现有的真实凭证。
