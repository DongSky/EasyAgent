# EasyAgent App

**简体中文** | [English](README.md)

前端应用独立于 Python 运行器发布，包含 HTML、CSS、JavaScript 和固定后端的同源代理。
它不依赖 `easyagent`，不读取运行器数据库。

```sh
pip install ./apps/agent
easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

另一个终端运行 `easyagent serve --port 8765`，浏览器打开 `http://127.0.0.1:8766`。
后端有访问令牌时，在应用中输入。模型和搜索密钥仍存储于后端。
远程后端必须使用 HTTPS。代理不注入密钥，不开放 CORS，不接受任意目标地址。
上传和实时事件使用与其他客户端相同的 API。

也可以自行托管 `src/easyagent_app/static`：根路径提供 `studio.html`，
`/assets` 提供静态目录，`/v1/`、`/health`、`/docs` 和 `/openapi.json` 代理到后端。
保留认证，SSE 关闭代理缓冲。桌面版显式组合前端包和运行器。

许可：AGPL-3.0-only，见 [LICENSE](LICENSE)。
