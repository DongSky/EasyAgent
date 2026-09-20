# 真实服务全流程验收

公开文档中的 `local-record-*` 是脱敏占位符，真实运行、流程和产物编号仅保留在本机验收记录。

日期：2026-09-19。目标是用一个简短、无外部业务写操作的任务，检验自然语言构建、真实 API、决策审查、确定性校验、文件下载和项目导出是否连接起来。

## 任务与通过标准

问题：「香港搬家后，如何向政府部门申报通讯地址变更？请查找官方的一站式通知及税务局相关指引。」

期望流程：GPT 生成简短检索词 → TinyFish 搜索 → 将结果转为模型上下文 → GPT 整理中文清单 → TypeSafe Jev 审查 → 程序校验来源 → 保存 Markdown。

工作流由助手 build 接口根据自然语言需求和工具注册表生成，验收脚本没有手写待执行的节点图。每个网络节点最多尝试一次，不自动反复付费重试。

通过条件：

1. 生成图包含真实 TinyFish、live-gpt、TypeSafe 和文件产物节点，没有用本地夹具代替。
2. 所有节点实际成功；Jev 审查结论为可供人工参考。
3. 至少两条建议引用两个不同的政府 URL，URL 属于本次搜索结果；原文摘录与同一结果的 snippet 逐字匹配，至少 20 个字符。
4. 下载文件的 SHA-256 与运行记录一致。
5. 页面预览、实际执行、导出包中的 workflow.json 相同，执行时仅替换本次输入。

这些条件能验证调用和来源闭环，不能证明政府指引已被完整阅读、模型判断总是准确，或任何地址变更已经办理。报告明确写出这一区别。

## 环境与凭证

服务进程读取 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`TYPESAFE_API_KEY`、`TINYFISH_API_KEY`。需要重新启动服务才能载入 shell 新增的变量；仅刷新网页不会刷新服务器环境。

本次重新加载用户的 zsh 配置后，四项均存在。模型使用用户网关模型目录实际列出的 `gpt-6-astra`，本地别名为 live-gpt；不把网关调用等同于直接调用 OpenAI 官方服务。凭证只作为相应供应商的认证请求头，不进入工作流、模型上下文或导出包。

TypeSafe 使用通用 HTTP API 注册，无需改写运行引擎：`POST https://api.typesafe.ai/v1/systemone`，Bearer 认证，model 为 `jev-latest`，输入 state 与 questions，返回 answers 与 usage。它在本流程中负责有约束的二选一判断，不负责写报告。

官方契约：[TypeSafe API](https://docs.typesafe.ai/api)、[TinyFish Search](https://docs.tinyfish.ai/search-api/reference)。TinyFish 的 location 必须是两位地区代码，如 HK；编排器不应传入 Hong Kong。

## 复现

先在自己的 shell 中设置四项环境变量；不要将密钥写进 JSON 或命令参数。然后运行：

```sh
uv run --extra app python -m examples.demos.live_workflow serve --model gpt-6-astra
```

这个独立服务默认使用 8772 和 `.eah/live-validation.db`。在另一个终端执行：

```sh
uv run --extra app python -m examples.demos.live_workflow run --url http://127.0.0.1:8772
```

run 会新建助手并调用真实服务，可能计费。结果保存到 `.eah/live-acceptance/<UTC 时间>/`，包括构建结果、工作流、示例输入、项目 ZIP 和运行记录；成功时另有报告。遇到失败返回非零状态并保留证据。ZIP 是可复用流程，运行前需要用目录中的 input.json 填写本次材料，并指定实际 EAH_URL。这里没有自动修复或无限重试循环。

当前用户工作室 8766 已加载同一组真实服务，可直接打开保存的验收助手查看节点及运行记录。此前 8765 的模型连接进程仍保留。

## 首轮实际失败

证据目录：`.eah/live-acceptance/20260919T150227Z/`。构建成功，生成六步图；运行 `local-record-03` 在来源校验节点失败，未保存报告。

TinyFish 成功返回七项，但与中文整句问题不相关，也混入政府域名外的结果。请求中的 location 是 Hong Kong，不符合官方地区代码约定。无法仅凭这一轮断言是哪个因素导致相关性问题。GPT 返回证据不足、空清单；Jev 实际模型为 `jev-1.13.0`，选择 needs_revision；本地校验拒绝空清单。

修正：增加单独的检索词生成步骤，使用英文关键词、HK/en 参数；为 TinyFish location 增加格式校验和集成回归用例。保留原来至少两个政府来源和逐字引用标准，不因失败放宽标准。

## 第二轮：真实服务成功，但交付关卡未通过

证据目录：`.eah/live-acceptance/20260919T150532Z/`。构建生成七步图，运行 `local-record-02`。

TinyFish 返回十项，包含 GovHK 更改地址服务、税务局通讯地址变更及商业登记指引。GPT 生成三条有原文和 URL 的建议。Jev 返回 needs_revision，概率 0.57，置信度 0.15；它的类型化接口没有给出拒绝理由，不能断言该判断正确或错误。来源校验节点遵照既定 Jev 放行要求停止，最终产物节点未执行。

本轮从生成到停止耗时 68.76 秒，三个真实服务均响应成功，但**完整报告交付未通过**。未降低通过标准或将草稿伪装成验收报告；草稿另存为 `draft-needs-review.json`。需要补充低置信度人工复核或有界修改流程，当前没有自动修复闭环。

## 首次运行时能力检查（修复前历史记录）

真实 HTTP 验收记录：`.eah/live-acceptance/runtime-authoring-audit.json`。

- 不重启服务，通过管理接口注册新的 TypeSafe 工具，返回 201；保存包含该工具的新节点流程，返回 201。
- 运行 `local-record-10` 的新节点实际调用 Jev，选择 moving，两个节点均成功，产出「运行时新增节点回执.json」。保存的流程 ID 为 `local-record-05`。
- 同名 API 再注册返回 422；没有原位更新接口。
- `PUT /v1/studio/workflows/{id}` 返回 404；修改流程只能 POST 另存为新 ID，尚无版本关联、乐观锁或发布/回滚机制。
- Agent 请求 `studio.register_api` 被 422 拒绝，因为工具不存在。这次成功操作由外部管理客户端完成，不能等同于 Agent 自主创建节点。
- 集成测试还验证：原运行保留提交时的图快照；保存新图不会向在途运行插入节点。数据库保留新图，但新注册的 HTTP 工具不会在重启后自动恢复，仍需显式保存和加载配置。

当时只能宣称管理接口支持动态新增和另存流程；“Agent 阅读 API 文档 → 生成/更新工具定义 → 持久保存节点版本 → 在运行中使用新版本”的能力尚未完成。

## 本地集成回归

`test_live_workflow.py` 通过真实本地 HTTP 服务验证 TypeSafe 请求、Bearer 凭证隔离、决策输出到文件的链路，并验证即使决策模型放行，伪造摘录仍会被程序拦截。它是合成协议回归，独立于上面的真实供应商验收。

`test_runtime_authoring_boundary.py` 已改为正向验收：Agent 工具调用创建/更新/执行、保存后故障重试、运行版本固定、重启恢复、单 worker、输入/审批、预算/取消及权限拒绝。


## 修复后的真实 Agent 开发验收

2026-09-19，`examples/demos/runtime_development.py` 只提交一个父工作流，然后读取证据；没有通过外部管理接口替 Agent 创建 API 或保存流程。

- 父运行：`local-record-08`，状态 succeeded，102.95 秒。
- 模型：用户网关的 `gpt-6-astra`，Responses 协议。最初 Chat Completions 请求被网关以 400 拒绝（该模型默认推理设置不支持此协议的工具调用）；切换协议后完整通过，保留失败记录。
- GPT 实际调用 TinyFish 查询 TypeSafe 文档，再读取 `https://docs.typesafe.ai/api`，自行构造新 HTTP 定义。
- API `live_02700edbad.classify`：revision 1 和 2；鉴权只通过服务端 `TYPESAFE_API_KEY` 引用提供。
- 工作流 `live_02700edbad.life`：revision 1 为 classify→receipt；revision 2 为 classify→summary→receipt。不同版本分别固定 API revision 1 和 2。
- 子运行 `local-record-13`：真实 Jev 返回 moving；`local-record-07`：返回 travel。两个产物节点均执行并成功下载 JSON。
- 更新后，Agent 再读取工作流旧版，并通过 `development.call_api` 成功调用旧 API 版本。

证据目录：`.eah/live-acceptance/runtime-20260919T152959Z/`。包括父工作流、完整运行/事件、两版 API/工作流、子运行、两份真实回执和 `acceptance.json`。不包含凭证值。

本验收证明配置式 API/工作流的运行时开发与执行；不改变上面政府资料报告未通过最终质量门禁的结论。权限及恢复机制见 [运行时开发说明](RUNTIME_DEVELOPMENT.md)。

重启后的补充复验：`local-record-06` 执行原工作流 revision 1 成功，API revision 1 返回 moving，完整结果保存为 `restart-run.json`。Studio 三节点 v2 图及版本号已人工操作浏览器核对，导出 JSON 与该版本逐字段一致；11 份证据文件通过凭证值扫描。
