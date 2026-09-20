"""Browser assets and a fixed-upstream proxy. No execution-runtime dependency."""
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

ASSETS = Path(__file__).with_name("static")


def mount_app(app):
    """Explicit composition for desktop/combined installs; only public assets."""
    app.mount("/assets", StaticFiles(directory=ASSETS), name="agent-assets")

    async def index(request):
        return FileResponse(ASSETS / "studio.html", headers={"Cache-Control": "no-cache"})

    app.add_route("/", index, methods=["GET"])
    life = Path(__file__).with_name("life")
    app.mount("/life-assets", StaticFiles(directory=life / "static"), name="life-assets")

    async def life_index(request):
        return FileResponse(life / "index.html", headers={"Cache-Control": "no-cache"})

    app.add_route("/life", life_index, methods=["GET"])


def create_app(backend="http://127.0.0.1:8765"):
    parsed = urlsplit(backend)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("backend must be an HTTP(S) origin without credentials or a path")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("remote backends must use HTTPS")

    @asynccontextmanager
    async def lifespan(app):
        # trust_env=False keeps local traffic out of ambient system proxies.
        async with httpx.AsyncClient(base_url=backend.rstrip("/"), follow_redirects=False,
                                     timeout=httpx.Timeout(120, read=None), trust_env=False) as client:
            app.state.client = client
            yield

    async def proxy(request):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "cross-origin requests are disabled"}, status_code=403)
        # Never let a path/query change the fixed upstream or forward cookies.
        path = request.url.path
        if not (path.startswith("/v1/") or path in ("/health", "/docs", "/openapi.json", "/docs/oauth2-redirect")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        limit = 50_000_000 if path == "/v1/artifacts/upload" else 2_000_000
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                return JSONResponse({"detail": "request exceeds upload limit"}, status_code=413)
        headers = {k: v for k, v in request.headers.items() if k.lower() in
                   ("authorization", "content-type", "accept", "idempotency-key", "last-event-id")}
        client = request.app.state.client
        upstream = client.build_request(request.method, backend.rstrip("/") + path,
                                         params=request.query_params.multi_items(), content=bytes(data), headers=headers)
        try:
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"detail": "backend unavailable"}, status_code=502)
        outgoing = {k: v for k, v in response.headers.items() if k.lower() in
                    ("content-type", "content-disposition", "cache-control", "etag", "content-encoding")}
        return StreamingResponse(response.aiter_raw(), status_code=response.status_code, headers=outgoing,
                                  background=BackgroundTask(response.aclose))

    app = Starlette(lifespan=lifespan)
    mount_app(app)
    app.routes.append(Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]))
    return app
