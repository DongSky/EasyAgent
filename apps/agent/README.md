# EasyAgent App

[简体中文](README.zh-CN.md) | **English**

The browser application is distributed independently from the Python runtime.
It contains HTML, CSS, JavaScript and a fixed-backend same-origin proxy; it has no
dependency on `easyagent` or access to its database.

```sh
pip install ./apps/agent
easyagent-app --backend http://127.0.0.1:8765 --port 8766
```

Open `http://127.0.0.1:8766`. Run the backend separately with
`easyagent serve --port 8765`. For a protected backend, enter its bearer token in
the application. Model/search keys stay in the backend. Remote backends require
HTTPS. The proxy does not inject credentials, enable CORS or accept arbitrary
upstream URLs. Upload and SSE endpoints use the same API as other clients.

You can instead deploy `src/easyagent_app/static` using your own web server:
serve `studio.html` at `/`, the directory at `/assets`, and proxy `/v1/`,
`/health`, `/docs` and `/openapi.json` to the backend. Preserve authentication and
disable proxy buffering for SSE. Desktop builds explicitly combine the app and runtime.

License: AGPL-3.0-only. See [LICENSE](LICENSE).
