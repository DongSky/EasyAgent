# 扩展与短板补齐验收

日期：2026-09-20。仅集成测试。不以功能清单代替实测。下文区分扩展开发阶段、UI 改版回归与最新复核，不把历史记录写成本次新执行。

## 最新复核与 UI 改版回归

- 本次重新执行 `uv run --extra app --extra dev pytest -q tests/integration/test_extensions.py tests/integration/test_code_packages.py tests/integration/test_conversations.py tests/integration/test_gateway_maintenance.py`：**14 passed in 28.12s**，无跳过。日志 `.eah/audits/2026-09-20/extension-recheck.log`。
- 上一轮 UI 改版后的全套集成测试：**101 passed in 87.50s**，日志 `.eah/build/ui-integration-final.log`。本次核对结果，没有重复执行全套。UI 实测详情见 [UI 改版记录](UI_REDESIGN.md)。
- 本次复核下文真实模型记录及数据库实际调用，确认 `guide.quote@2` 的 100/50 HKD 和模型生成代码跨流程复用的 30/0.3；本次没有新发起付费模型调用。
- 五个 Web UI 插槽具有契约与实现，但尚未逐个完成自定义 HTML 交互的浏览器端到端验收。声明 UI manifest 的集成测试不能替代全部渲染和交互测试。
- 更新的 Pi / Hermes 比较见[差距分析](PI_HERMES_GAP_ANALYSIS.md)。扩展、会话和学习等能力的执行验证，不代表对竞品全部扩展 API 的等价认证。

## 扩展开发阶段执行结果

- `uv run --extra app --extra dev pytest -q tests/integration`：**99 passed in 89.07s**，无跳过，仅集成测试。
- `examples.demos.run_all`、`advanced`、`scenarios` 全部达到预期；其中恶意材料场景的预期为拒绝。夹具结果不等于真实模型质量。
- Ruff、18 个 JavaScript 文件语法、生成的 TypeScript 契约严格类型检查通过；Rust Core（含 JNI）与 Rust SDK 的 fmt/clippy `-D warnings` 通过。
- `uv build` 生成 sdist/wheel，核对扩展、运行器、表单、维护页面和示例资源随包存在。
- 最终 macOS `.app` `--smoke` 输出 `packaged_runtime:true`、`pure_extension:{bundled:true}`、126 个 API 路径；完整桌面入口含生活应用有 136 个路径。直接从应用启动 HTTP/界面，`guide.quote` 返回 100 HKD。
- 独立应用界面真实下载 9,593 字节加密备份，下载事件与磁盘文件均确认；使用备份密码成功恢复到新的数据库。证据 `desktop-http.json`。
- iOS 模拟器两步离线流程执行成功；重新安装并启动当前模拟器包后，检查点与原文件完全一致。该结果只证明本地状态文件跨启动保留，不代表完整后台 Agent 恢复。

## 已验证范围

- 扩展：纯 JS、受信任 Python/Node/Rust、WASM；包摘要/签名、进程信任、Schema、超时、升级后旧运行/重启/回滚、迁移状态、依赖与服务 continuation、审批绑定改写后参数、常驻进程、会话 host 服务。
- 分享：完整工作流包含源码扩展闭包，空数据库安装依赖后导入并执行；旧组件/API包兼容。
- 代码开发：候选→失败拒绝发布→通过试跑→发布→两个独立流程复用；无 WASI/宿主 imports 的计算边界。
- 持续会话：真实本地 HTTP SSE、跨块工具参数、缺失终止事件、转向/追问/分支/检索/摘要、重启原文保留。
- 学习与协作：复盘来源、评估失败拒绝发布、成功发布/复用/回滚；单 worker 子任务等待与权限限制；签名入站消息去重和回复审批；自动复盘与摘要。
- 连接：本地真实 HTTP 重试、稳定投递键、旧配置固定、独立回执、凭证不明文入库、CalDAV UTC与转义、DST cron 补跑。
- MCP：本地 HTTP OAuth/PKCE、state 拒绝、token刷新、增加工具后发现、重启恢复与旧 stdio/HTTP/server互通。
- 运维：SQLite 一致性加密备份恢复、凭证恢复、共享 Rust 核心 Host checkpoint/审批/未知结果恢复。

## 实际模型与界面

工作室 `127.0.0.1:8766` 通过界面安装/升级 guide v2，安装完成弹窗关闭。真实 `live-gpt` 在同一个持久会话内调用 `guide.quote@2`：首次 12.50 HKD × 8 = 100.00 HKD，追问加购 4 个 = 50.00 HKD。核对实际 invocations 的版本、输入、输出与会话显示，并非仅采信模型口述。两轮分别保存 18/19 个 model.delta 事件。初版硬编码 CNY 的问题已修复，v1 历史保留。

真实代码开发验收：模型自行生成 `verifycbd1c454_total.sum` 源码和三个场景，经过 code.create→test→publish 以及写审批。随后搬家/旅行两条独立流程 得到 30 和 0.3。首轮因 manifest 自由 JSON 缺少完整格式信息失败；现已改为严格 ExtensionManifest Schema，并增加执行前参数错误回传 Agent 修正。失败证据保留于 `code-schema-failure/`，没有计作成功。

浏览器验证嵌套对象、数组项和必填布尔 false，无需编辑 JSON；运行返回 `{label:"纸箱",fragile:false,total:8}`。服务连接、经验/代码、扩展中心与维护界面均加载，检查时无浏览器控制台错误。

证据保存在 `.eah/live-acceptance/extensions-final/`；复现代码开发使用 `uv run --extra app python -m examples.demos.live_extension_code --live --model live-gpt`。复现会使用已连接模型，并只批准该 demo 的已试跑纯计算包。生成场景不替代独立业务验收。

## 短稳记录

最终 `.eah/extension-soak-final/report.json`：600.0516 秒，5,518 次工作流、55 次宿主重启、0 次失败。早期 `.eah/extension-soak-v2/report.json` 为 90.0056 秒、1,058 次工作流、21 次重启、0 失败。首轮脚本把 transform 的 `{value:...}` 错当标量，因此全部标记失败；核对持久输出后修正测试断言，重新完整执行。该修正没有掩盖运行错误。

`uv run python scripts/soak.py --seconds 259200 --output .eah/soak-72h` 可执行 72 小时关卡。**本次没有 72 小时证据**；短稳不代表生产 SLO，强杀/租约故障另由既有恢复集成场景覆盖。

## 构建边界

- macOS 独立 `.app`：PyInstaller 打包 Python、QuickJS、WASM 和工作室资源；脱离源码入口的 `--smoke` 实际安装/执行纯扩展和加载 OpenAPI。签名为本地 ad hoc，无 Apple 公证。
- iOS：arm64 模拟器 `.app` 编译与本地签名，Rust 静态库实际链接；共享核心离线示例源码已接入。iOS 26 的 iPhone 17 Pro 模拟器已实际启动，通过原生界面完成本机离线流程并保存检查点。仍未做手机真机、后台生命周期或商店发行验收。
- Android：Kotlin Host、JNI、Gradle/NDK 构建脚本与 CI 已提供；JNI 代码在本机编译检查。当前机器无 JDK、Android SDK/NDK/Gradle，未产出 APK，也未做真机验收。
- Linux/Windows：配置独立应用构建与集成 CI，未在远程 CI 运行，不能记作本次已验证。

## 尚未获得的外部证据

真实通知与日历账户投递、5–8 位新手安装任务、Android/iOS 真机后台恢复、正式签名发行、72 小时长稳、分布式多租户。连接器、长稳脚本和构建入口可用于继续验收。手机完整离线 Agent 引擎、自动跨设备转交、原生邮箱/支付、账户/多租户及完整第三方依赖隔离安装仍未实现；不把路径、测试夹具或代码存在当作这些能力完成。
