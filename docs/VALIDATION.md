# 集成验收

公开文档中的 `local-record-*` 是脱敏占位符，真实运行、流程和产物编号仅保留在本机验收记录。

本文件按阶段保留历史结果，各段的测试数量和未交付描述只对应当时版本。2026-09-20 最新框架全套为 **128 passed**；后续功能、系统通知和媒体证据见 [NEXT_DELIVERY.md](NEXT_DELIVERY.md)。当前双语使用与开发入口见 [文档索引](README.md)。

遵循请求：不编写单元测试；仅通过真实 SQLite、HTTP、子进程、官方 MCP SDK 和跨语言 SDK 验证模块协作。

## 框架验收场景

1. DAG 依赖/参数引用、并行节点、非法图拒绝、请求幂等冲突。
2. 重试/超时/延时/取消、旧租约 fencing、重启恢复、未知写结果人工核验。
3. 工具输入输出 schema、allowlist、调用参数审批、拒绝后停止。
4. Agent 工具循环、checkpoint 恢复、轮数预算、结构化决策。
5. Chat/Responses/Anthropic/Image/Embedding HTTP 适配；模型错误与能力拒绝。
6. Python、JS、Rust 插件与 SDK；插件异常、协议/输出限制。
7. MCP stdio 与 Streamable HTTP client、MCP server；Skills 按需加载与路径边界。
8. 持久记忆、自进化候选评估、发布保护和回滚。

## 应用验收场景

四种项目模板；改期联动；负责人/等待对象；依赖阻塞；完成证据；费用/资料保存；本地提醒；材料提取 workflow；重启保存。

## 证据边界

Mock 和本地供应商协议夹具验证框架行为，不验证真实模型的理解、生图质量、账户额度或供应商在线可用性。没有凭证时仍交付可复现 demos；live 验证命令必须显式选择 provider/model，由开发者配置 key 后运行。

实际执行结果、平台和剩余限制记录在此文件末尾，失败不得以跳过或编译成功代替功能成功。


## 本机执行结果（2026-09-19）

- `uv run --extra app --extra dev pytest -q tests/integration`：**64 passed in 29.89s**。无跳过，全部为集成用例；包含 4 个零代码/连接综合场景，以及 TypeSafe 决策与来源门禁、运行时开发的五个正向综合场景。
- `uv run --extra app python -m examples.demos.run_all`：通过。实际启动 Python/JS/Rust 插件、MCP 和 HTTP 服务，三个 SDK 往返、审批与自进化链路完成。
- `uv run --extra app python -m examples.demos.advanced`：通过。检索→两个 Agent 角色→foreach→artifact，评估、webhook 去重和调度完成。
- `uv run --extra app python -m examples.demos.scenarios`：5 个业务任务均符合预期；恶意外部评价场景的预期结果是权限拒绝。不是模型 benchmark 分数。
- `uv run --extra app --extra dev ruff check .`、新增/修改 JavaScript 文件的 `node --check`：通过。Rust fmt、build 和 clippy `-D warnings` 在前序 SDK 验收通过；本轮完整 suite 和 demo 再次实际运行跨语言接口。
- 短时运行测试：20 秒内完成 368 个父运行、736 个子运行和 9 次干净的 Hub 重启，SQLite integrity 正常，批次 p95 约 0.2 秒。此结果不等于 72 小时长稳。
- 浏览器：三种构建入口的具体步骤与修复记录见 [CONSTRUCTION_ACCEPTANCE.md](CONSTRUCTION_ACCEPTANCE.md)。搜索→手动 API→导入接口→审批→回执完成；生活项目演示数据仅为合成。
- `uv build`：sdist/wheel 构建成功。先前验证了生活 app、API/画布资源、TinyFish 注册；本次在 `/tmp` 隔离安装并用 `--refresh-package easyagent` 验证新零代码/模型窗口资源、构建接口、无真实模型拒绝以及空项目导出保护。旧独立 generation 草稿接口仍默认关闭，新的助手 build 接口已启用。
- 下载修复后的定向复验：`test_no_code_builder.py` **4 passed in 5.73s**；JSON 附件与预览一致。浏览器实际下载 ZIP、JSON、文本产物，ZIP 已解压核对，不只验证 HTTP 返回成功。
- 产物中文文件名补齐后，零代码与完整工作流相关 **11 passed in 8.02s**；浏览器下载的「通知行动清单.txt」名称、字节内容均与运行产物一致。Ruff 与重新打包通过。

运行环境：macOS / Apple Silicon，Python 3.12.11、Node 26、Rust 1.93。GitHub Actions 平台矩阵仅已配置，未收到远程 Linux/Windows CI 结果。

## 未完成的独立验收

已复用用户连接的 gpt6 实际完成工作流生成和通知处理，四步全部成功并产出文本文件；详细证据见 [构建验收](CONSTRUCTION_ACCEPTANCE.md)。没有把凭证写入测试或导出包。此项不替代多模型、多任务的质量评估。

真实 TinyFish、GPT 网关、TypeSafe Jev 均已成功响应；七步报告流程被 Jev 复核关卡拦下，不能算完整交付通过。该阶段最初只有服务端注册 API 能力；随后已补齐 Agent 自主创建/更新/持久化声明式节点，后续通过记录见下文。详情和失败证据见 [真实流程验收](LIVE_WORKFLOW_ACCEPTANCE.md)。

该历史阶段的 72 小时压力/故障注入、生产备份恢复、小白用户无指导任务未验收；Docker 已排除。备份恢复的后续实现见最新扩展验收。图片输入/OCR 和自动修复生成失败尚未实现。首个生活 app 的本地项目管理已验证，真实邮件/日历/预订/支付服务和家庭账户协作不属于已通过能力。


- 运行时开发真实验收通过：父运行 `local-record-08`，102.95 秒；Agent 自行检索/读文档、创建并更新 API 与工作流、执行两个子流程、调用旧版本。详见 [证据](LIVE_WORKFLOW_ACCEPTANCE.md#修复后的真实-agent-开发验收)。
- 重启真实 Studio 后，重新执行 Agent 保存的工作流 revision 1：运行 `local-record-06` 成功，使用 API revision 1，Jev 返回 moving。页面显示三节点 v2 连线，浏览器下载事件成功；导出 JSON 的 3 个节点与持久化版本一致。11 份新证据文件未检出三个供应商的凭证值，SQLite integrity_check 为 ok。


- 组件复用新增三个集成场景：两个不同工作流共享 HTTP/子流程版本，更新后旧版及重启仍固定；Agent 跨命名空间与传递权限检查；旧版 revision 0 保留及嵌套上限。补充 Rust typed SDK 实际提交复用流程与固定 HTTP 版本、foreach 参数映射的定向场景，3 passed in 3.07s。
- Studio 实际“用作子流程”→保存独立流程→执行：父运行 `local-record-11`，子运行 `local-record-01`，真实 Jev 返回 travel，产物完成；证据 `.eah/live-acceptance/component-reuse/`。引用 `live_02700edbad.life` revision 2，调用方保存 ID `local-record-14`。
- Android/iOS 目前只有经过官方规则核对的 [架构方案](MOBILE_RUNTIME_STRATEGY.md)，没有手机内核或真机验收；上述测试结果不代表移动端兼容。Docker 不进入后续实现方案。

## 节点库、模型协议、媒体与组件包（2026-09-20）

- 全部集成测试：`uv run --extra app --extra dev pytest -q` **73 passed in 41.10s**，无跳过。新增 9 个综合场景，包括真实本地 HTTP、SQLite、Python/JS/Rust 客户端、媒体轮询恢复和跨 Hub 包迁移。
- 315 个具体协议操作逐一检查方法、URL、路径替换、固定鉴权、JSON/multipart；三种标准化文字协议分别验证原始模型 ID 与专有参数。该结果不等于 343 个模型的线上生成质量通过。
- 包集成验证：空 Hub 导入、重复导入、实际 JS JSON 往返、凭证重绑、执行、重启、摘要篡改/依赖缺失/权限不一致拒绝、冲突不产生部分写入。旧元数据新增兼容摘要的组件版本，原 API/Workflow 版本与历史引用保持不变。
- 媒体集成验证：提交只执行一次，等待期间 worker 处理另一任务，重启继续 GET；终态失败、未知状态、超时/轮询上限和取消；二进制音频/base64 图像进入产物库。
- `ruff check`、修改的 JS 文件语法检查、Rust fmt 和 clippy `--all-targets -- -D warnings` 通过。sdist/wheel 构建成功，核对包含协议快照、组件包模块、节点库/模型目录页面资源及新 demo。

### 真实服务与迁移

最终证据目录：`.eah/live-acceptance/library-20260919T164435Z/`。

- 售后工单分流运行 `local-record-16` 成功，Jev 返回 support。
- 设备故障优先级运行 `local-record-15` 成功，Jev 返回 urgent。
- 同一通用分类组件包包含 3 个组件、6 个定义。空数据库导入、凭证名称重绑、运行与重启后的再次运行均成功；具体输出与 SHA-256 回执保存在证据目录。
- 真实同步目录：343 个账户模型、315 个操作、22 个缺少协议。未发起全目录的图片/音视频收费生成。

### 工作室实际操作

浏览器验证节点库、模型搜索/类型过滤、添加原生接口节点、导入预览和导入完成后关闭弹窗。实际下载组件文件，并读取磁盘文件进行干净 Hub 核验；25,542 字节，包含 6 个定义，未检出已配置服务的凭证值。可分享副本保存在 `examples/components/classification.eah-component.json`。

浏览器验证发现并修复两处问题：JSON 的整数浮点数往返导致摘要误判；Blob 下载在内嵌浏览器未产生文件，未使用访问令牌时改用带附件头的下载端点。预览导入、JS SDK 导入与实际文件下载均重新通过。

分享包没有完整插件源码、签名认证或手机运行器。Rust Core/Android/iOS Host、受限代码执行、跨设备任务转交、大型媒体/实时流和 72 小时长稳仍未交付。本节不改变前序复杂报告质量验收未通过的记录。

## 完整工作流分享（2026-09-20）

- 最终全套：`uv run --extra app --extra dev pytest -q tests/integration` **77 passed in 46.01s**，无跳过。新增 4 个集成场景，使用真实 SQLite/HTTP、模型工具循环、JS 导入、资料/模型/凭证重绑、人工输入、重启、权限和事务拒绝、写审批与固定补偿版本。Rust 示例随互操作测试实际调用完整流程导出与预览。
- 分享校验补充：API/开发服务禁止常见敏感查询字段；已有版本凭证不可被导入改绑，事务内再次核对并发冲突。
- Ruff、修改的 JS 语法、Rust fmt/clippy 通过；sdist/wheel 构建与资源核对通过。
- 真实服务：源流程 `local-record-12` → 分享包 → 空数据库 `example.imported-classification`，模型别名 live-gpt 重绑为 local-gpt，凭证变量也重绑。导入时没有执行；显式运行 `local-record-04` 成功。Jev 返回 support，GPT Responses Agent 调用 core.echo，文件为「售后需要处理」。
- 证据 `.eah/live-acceptance/workflow-share-20260919T165941Z/`；复现 `uv run --extra app python -m examples.demos.workflow_share --live`。分享包副本 `examples/workflows/shared-classification.eah-workflow.json`，16,432 字节。
- 浏览器实际下载文件、选择文件上传到本机 Studio、预览 7 个嵌套步骤和 1 个 API、导入独立副本；弹窗关闭，画布显示 classify/respond/receipt 及数据/执行连线。浏览器未记录错误。
- 完整范围见 [分享指南](WORKFLOW_SHARING.md)。包不复制密钥、知识数据、插件源码或历史产物；不等于独立服务器发行，也不证明手机可执行。

分享调试通过后完成 [Pi/Hermes 差距复核](PI_HERMES_GAP_ANALYSIS.md)。该对比是固定提交的文档/源码核对，没有竞品运行或性能 benchmark。

## 完整扩展与持续助手（2026-09-20）

最新研发已补齐主要扩展、会话/学习/协作/连接和运维能力，新增源码分享与原生移动起点。最终测试数、真实模型失败修复、打包与移动证据统一维护于 [EXTENSION_ACCEPTANCE.md](EXTENSION_ACCEPTANCE.md)，本文件前文为各历史阶段记录。
