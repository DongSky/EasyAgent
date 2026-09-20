# Android / iOS 兼容策略与当前实现

更新：2026-09-20。不使用 Docker。桌面 Python Hub 是完整执行引擎；手机使用签名内置 Rust 核心与 Swift/Kotlin Host，可按用户输入连接个人 HTTPS Hub。

## 已交付

- `core/` 是独立可嵌入 Rust crate，不是 Rust HTTP SDK。提供图校验、依赖和数据引用、条件/transform、工具能力授权、写审批、稳定 invocation、checkpoint、pending outbox、崩溃后 needs_attention 与回执核验。C ABI/JNI 使用 JSON 与宿主通信，不依赖 Python/Node/shell。
- `platforms/ios/`：UIKit/WebKit、Rust 静态链接、原子检查点文件、本机离线两步流程和 HTTPS Hub 入口。arm64 iOS 模拟器应用已经构建、启动，并通过界面实际运行离线流程，两个节点成功，检查点落盘。
- `platforms/android/`：Kotlin/WebView、Rust JNI、AtomicFile、本机同样的流程和 HTTPS Hub 入口。Gradle/NDK 脚本及发行 CI 已提交在工作区；本机无 Android 工具链，尚未构建 APK。
- `platforms/desktop/`：PyInstaller 打包完整 Python Hub、QuickJS、WASM 和工作室资源。本机 macOS `.app` smoke 已运行；Windows/Linux 构建矩阵尚未远程执行。

## 手机核心的边界

目前 Rust 核心是声明式流程执行子集，尚未承载 Python 引擎的完整 Agent/model 循环、所有节点种类、重试预算、知识库、MCP 或后台维护。iOS/Android Host 是可运行的原生起点，尚未完成 Keychain/Keystore 配对、原生通知/文件插件、操作系统后台任务调度与自动跨设备转交。不得把 WebView 连接到 Hub 算作全部能力在手机本地执行。

QuickJS/Wasmtime 动态源码扩展当前在桌面 Hub 运行。手机本地仅签名内置能力与声明式流程，不下载并执行任意 Python/Node/Rust 二进制。模型生成新代码时，手机可通过已连接的 Hub 使用；不会默认上传到第三方执行器。共享包固定代码版本，完整 Python/Node/Rust 进程需要兼容电脑工具链和用户信任。

## 后台与恢复

操作系统决定后台运行时间；不承诺 iOS/Android 永久常驻。先持久化意图再执行外部动作，未知写结果必须核验，不能重启后盲目重发。长周期 Agent 和投递队列可运行在用户电脑/服务器，手机负责交互；真正的后台切换、网络中断及跨设备恢复仍需要真机和配对协议验收。

## 构建

```sh
platforms/ios/build.sh
# Android 需要 JDK 17、Gradle 8.9、Android SDK 35、NDK 与 cargo-ndk
platforms/android/build.sh
uv run --extra desktop python platforms/desktop/build.py
```

构建证据与限制见[本轮验收](EXTENSION_ACCEPTANCE.md)，发行 CI 见 `.github/workflows/releases.yml`。iOS 产物是模拟器包，不是已签名真机 IPA 或商店发行。

## 官方依据

2026-09-19 实际读取：

- [Apple App Review Guidelines：2.5.2、2.5.4、4.7](https://developer.apple.com/app-store/review/guidelines/)
- [Apple：Choosing Background Strategies](https://developer.apple.com/documentation/backgroundtasks/choosing-background-strategies-for-your-app)
- [Apple：BGContinuedProcessingTask（iOS 26+）](https://developer.apple.com/documentation/backgroundtasks/bgcontinuedprocessingtask)
- [Google Play：Device and Network Abuse](https://support.google.com/googleplay/android-developer/answer/16559646)
- [Android：Persistent background work / WorkManager](https://developer.android.com/develop/background-work/background-tasks/persistent)

这些资料支持上述约束，不代表本项目已经通过应用商店审核或完成手机实机验收。
