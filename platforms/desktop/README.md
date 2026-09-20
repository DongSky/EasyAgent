# 桌面包 / Desktop packages

桌面启动器包含 Python 后端和独立 Agent App，启动后打开系统浏览器访问本机工作室，无需用户安装 Python。窗口关闭不代表后端已停止；Windows 的终端窗口需保持打开。模型与搜索仍需配置自己的服务。

The desktop launcher bundles Python, the backend and the independent Agent App. It opens Studio in your system browser; users do not need Python installed. Closing the browser does not stop the backend. Keep the Windows console window open while using the app. Configure your own model and search services.

## GitHub Actions

[Desktop packages](../../.github/workflows/desktop.yml) 在推送到 `main`、推送 `v*` 标签、相关 PR 或手动 **Run workflow** 时构建：

- macOS Apple Silicon：`macos-arm64`。
- macOS Intel：`macos-x86_64`。
- Windows 64 位：`windows-x86_64`。

每个任务先构建，再解压 ZIP 到临时目录，运行纯 JavaScript 扩展自检并启动实际应用，验证 HTTP 健康检查、App 静态资源和 OpenAPI。通过后上传 ZIP、SHA-256 文件；日志单独上传。进入 Actions → Desktop packages → 对应运行 → Artifacts 下载，保留 14 天。标签触发会生成 Actions 产物，不自动发布 GitHub Release。

The workflow runs on pushes to `main`, `v*` tags, relevant pull requests, or **Run workflow**. Each runner builds a ZIP, extracts it outside the checkout, tests a pure JavaScript extension, and starts the application to check HTTP health, frontend assets and OpenAPI. Download ZIPs and SHA-256 files from the run’s **Artifacts**, retained for 14 days. Logs are uploaded separately. Tag builds do not automatically publish a GitHub Release.

macOS 解压后打开 `EasyAgent.app`；Windows 解压整个目录后运行 `EasyAgent/EasyAgent.exe`，不要单独移动 EXE。当前为预览包：macOS 只有本地 ad-hoc 签名，未进行开发者签名、公证；Windows 未签名，系统可能提示来源未验证。Playwright 浏览器二进制需另行安装，使用普通工作室无需浏览器自动化依赖。

On macOS, open the extracted `EasyAgent.app`. On Windows, extract the entire folder and run `EasyAgent/EasyAgent.exe`; keep its bundled files together. Preview builds have no release signing or notarization: macOS uses ad-hoc signing, and Windows is unsigned, so the OS may warn about an unverified publisher. Playwright browser binaries are separate; ordinary Studio use does not require browser automation.

## 本机构建 / Local build

在对应操作系统上运行 / Run on the target operating system:

```sh
uv sync --extra desktop --locked --python 3.12
uv run --no-sync python platforms/desktop/build.py
uv run --no-sync python platforms/desktop/package.py
```

应用目录：`.eah/build/desktop/`；已验证压缩包：`.eah/build/releases/`；日志：`.eah/build/logs/`。构建不会复制本地配置、凭证或运行数据库，验证使用临时工作区且不会调用模型 API。应用日常数据使用系统的 `EasyAgent` 应用数据目录，可通过 `EAH_DATA_DIR` 覆盖。

Application bundles are in `.eah/build/desktop/`, verified archives in `.eah/build/releases/`, and logs in `.eah/build/logs/`. Builds exclude local configuration, credentials and run databases. Verification uses temporary workspaces and makes no model API calls. Normal application data uses the OS application-data directory under `EasyAgent`; override it with `EAH_DATA_DIR`.
