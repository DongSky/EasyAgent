import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from conftest import live_server


async def test_managed_mcp_oauth_pkce_refresh_discovery_and_restart(api):
    url, hub = api
    server = FastMCP("managed-fixture", stateless_http=True, json_response=True)

    @server.tool()
    async def add(a: int, b: int) -> dict:
        return {"value": a + b}

    remote = server.streamable_http_app()
    app = FastAPI(lifespan=lambda app: server.session_manager.run())
    endpoint = ""
    tokens = set()
    registration = {}
    challenge = None
    exchanges = []

    @app.middleware("http")
    async def authorization(request: Request, call_next):
        if (
            request.url.path.startswith("/mcp")
            and request.headers.get("authorization", "").removeprefix("Bearer ") not in tokens
        ):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{endpoint}/.well-known/oauth-protected-resource"'
                },
            )
        return await call_next(request)

    @app.get("/.well-known/oauth-protected-resource")
    async def resource():
        return {
            "resource": endpoint + "/mcp",
            "authorization_servers": [endpoint],
            "scopes_supported": ["tools"],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def metadata():
        return {
            "issuer": endpoint,
            "authorization_endpoint": endpoint + "/authorize",
            "token_endpoint": endpoint + "/token",
            "registration_endpoint": endpoint + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }

    @app.post("/register")
    async def register(request: Request):
        registration.update(await request.json())
        return {**registration, "client_id": "integration-client"}

    @app.post("/token")
    async def token(request: Request):
        data = parse_qs((await request.body()).decode())
        exchanges.append(data)
        if data["grant_type"] == ["authorization_code"]:
            verifier = data["code_verifier"][0]
            assert (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                == challenge
            )
            assert data["code"] == ["valid-code"]
        else:
            assert data["refresh_token"] == ["refresh-secret"]
        access = "access-" + str(len(exchanges))
        tokens.add(access)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 60,
            "refresh_token": "refresh-secret",
            "scope": "tools",
        }

    app.mount("/", remote)
    async with live_server(app) as base, httpx.AsyncClient(base_url=url, timeout=30) as client:
        endpoint = base
        profile = {
            "id": "oauthdemo",
            "url": endpoint + "/mcp",
            "oauth": True,
            "redirect_uri": url + "/v1/mcp/oauth/callback",
            "permissions": {"add": {"effect": "read", "idempotent": True}},
        }
        response = await client.post("/v1/mcp/connections", json=profile)
        assert response.status_code == 202, response.text
        async with asyncio.timeout(30):
            while "oauthdemo" not in hub.mcp.pending:
                if hub.mcp.errors:
                    raise AssertionError(hub.mcp.errors)
                await asyncio.sleep(0.02)
        authorization_url = hub.mcp.pending["oauthdemo"]["url"]
        params = parse_qs(urlsplit(authorization_url).query)
        challenge = params["code_challenge"][0]
        assert params["code_challenge_method"] == ["S256"]
        assert (
            await client.get("/v1/mcp/oauth/callback", params={"state": "wrong", "code": "x"})
        ).status_code == 403
        good = await client.get(
            "/v1/mcp/oauth/callback", params={"state": params["state"][0], "code": "valid-code"}
        )
        assert good.status_code == 200, good.text
        await asyncio.wait_for(hub.mcp.tasks["oauthdemo"], 30)
        assert not hub.mcp.errors, hub.mcp.errors
        flow = {
            "name": "managed",
            "steps": [{"id": "add", "target": "mcp.oauthdemo.add", "input": {"a": 7, "b": 2}}],
        }
        r = await hub.wait(hub.submit(flow))
        assert r["status"] == "succeeded", r
        connection = hub.mcp.connections["oauthdemo"]
        connection.auth.context.token_expiry_time = time.time() - 1
        r = await hub.wait(hub.submit(flow))
        assert r["status"] == "succeeded", r
        assert any(x["grant_type"] == ["refresh_token"] for x in exchanges)

        @server.tool()
        async def multiply(a: int, b: int) -> dict:
            return {"value": a * b}

        profile["permissions"]["multiply"] = {"effect": "read", "idempotent": True}
        await hub.mcp.connect(profile)
        await asyncio.wait_for(hub.mcp.tasks["oauthdemo"], 30)
        assert "mcp.oauthdemo.multiply" in hub.tools.entries
        r = await hub.wait(
            hub.submit(
                {
                    "name": "discovered",
                    "steps": [{"id": "m", "target": "mcp.oauthdemo.multiply", "input": {"a": 3, "b": 4}}],
                }
            )
        )
        assert r["status"] == "succeeded"
        from easyagent.runtime import Hub

        restored = Hub(hub.store.path, poll_seconds=0.01)
        await restored.start()
        try:
            await asyncio.wait_for(restored.mcp.tasks["oauthdemo"], 30)
            assert not restored.mcp.errors, restored.mcp.errors
            r = await restored.wait(restored.submit(flow))
            assert r["status"] == "succeeded"
        finally:
            await restored.stop()
